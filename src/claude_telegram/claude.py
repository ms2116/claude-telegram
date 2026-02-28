"""Hybrid Claude session controller — tmux for existing sessions, SDK for new ones."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Coroutine

try:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ResultMessage,
        SystemMessage,
        TextBlock,
        ToolUseBlock,
    )
    HAS_SDK = True
except ImportError:
    HAS_SDK = False

if TYPE_CHECKING:
    from .config import Settings

log = logging.getLogger(__name__)

# Callback type: async fn(text_chunk, is_final)
StreamCallback = Callable[[str, bool], Coroutine[Any, Any, None]]

# ── Constants ──

SESSION_DIR = Path("/tmp/claude_sessions")
POLL_INTERVAL = 1.0      # seconds
MIN_WAIT = 5             # let Claude start processing
TIMEOUT = 300            # max wait

# ANSI escape 코드 패턴
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b\(B")

_PROCESSING_PREFIXES = (
    "·", "✻", "✽", "✢", "*", "●", "○", "◐", "◑", "◒", "◓",
    "⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏",
)


# ── Result ──

@dataclass
class SessionResult:
    text: str = ""
    session_name: str = ""


# ── Low-level tmux utils ──

def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def capture_pane(pane_id: str, lines: int = 2000) -> str:
    r = subprocess.run(
        ["tmux", "capture-pane", "-t", pane_id, "-p", "-S", f"-{lines}"],
        capture_output=True, text=True, timeout=5,
    )
    return r.stdout


def _is_pane_alive(pane_id: str) -> bool:
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-t", pane_id, "-p", "#{pane_id}"],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def _is_processing_line(stripped: str) -> bool:
    if "…" not in stripped:
        return False
    if len(stripped) > 80:
        return False
    if any(s in stripped for s in ("ctrl+o", "shift+tab", "esc to")):
        return False
    if stripped.startswith("⎿"):
        return False
    return any(stripped.startswith(p) for p in _PROCESSING_PREFIXES)


def is_claude_idle(pane_content: str) -> bool:
    cleaned = strip_ansi(pane_content)
    lines = cleaned.strip().split("\n")
    if not lines:
        return False
    check_lines = lines[-15:]
    has_prompt = False
    for line in check_lines:
        stripped = line.strip()
        if stripped == "❯" or stripped.startswith("❯ ") or stripped.startswith("❯\xa0"):
            has_prompt = True
        if stripped and all(c in "─━═" for c in stripped):
            continue
        if _is_processing_line(stripped):
            return False
    return has_prompt


async def send_to_tmux(pane_id: str, message: str) -> None:
    single_line = message.replace("\n", " ").strip()
    subprocess.run(["tmux", "send-keys", "-t", pane_id, "-l", single_line], timeout=5)
    await asyncio.sleep(0.1)
    subprocess.run(["tmux", "send-keys", "-t", pane_id, "Enter"], timeout=5)


def extract_response(before: str, after: str, user_msg: str) -> str:
    before_clean = strip_ansi(before).strip()
    after_clean = strip_ansi(after).strip()
    before_lines = before_clean.split("\n")
    after_lines = after_clean.split("\n")
    new_content = ""

    user_short = user_msg[:60].strip()
    for i, line in enumerate(after_lines):
        stripped = line.strip()
        if stripped.startswith("❯") and user_short in stripped:
            new_content = "\n".join(after_lines[i + 1:])
            break
        if user_short in line and i > len(after_lines) // 2:
            new_content = "\n".join(after_lines[i + 1:])
            break

    if not new_content:
        anchor_size = min(5, len(before_lines))
        anchor = "\n".join(before_lines[-anchor_size:])
        after_text = "\n".join(after_lines)
        idx = after_text.find(anchor)
        if idx >= 0:
            new_content = after_text[idx + len(anchor):]

    if not new_content:
        before_set = set(before_clean.split("\n"))
        new_content = "\n".join(l for l in after_lines if l not in before_set)

    cleaned_lines = []
    for line in new_content.split("\n"):
        stripped = line.strip()
        if stripped and all(c in "─━═" for c in stripped):
            continue
        if "shift+tab" in stripped or "esc to interrupt" in stripped:
            continue
        if stripped in ("❯", ">"):
            continue
        if _is_processing_line(stripped):
            continue
        cleaned_lines.append(line)

    result = "\n".join(cleaned_lines).strip()
    while "\n\n\n" in result:
        result = result.replace("\n\n\n", "\n\n")
    return result


# ── Session info ──

@dataclass
class SessionInfo:
    project: str
    pane_id: str
    work_dir: str


# ── TmuxSession — one per project ──

class TmuxSession:
    def __init__(self, info: SessionInfo) -> None:
        self.info = info
        self._running = False
        self._interrupted = False

    async def execute(
        self,
        prompt: str,
        stream_cb: StreamCallback | None = None,
    ) -> SessionResult:
        self._running = True
        self._interrupted = False
        pane_id = self.info.pane_id
        result = SessionResult(session_name=self.info.project)

        try:
            before = capture_pane(pane_id)
            await send_to_tmux(pane_id, prompt)
            log.info("Sent to %s/%s: %s", self.info.project, pane_id, prompt[:80])

            # Wait for response with streaming
            await asyncio.sleep(MIN_WAIT)
            elapsed = MIN_WAIT
            last_streamed = ""

            while elapsed < TIMEOUT and not self._interrupted:
                await asyncio.sleep(POLL_INTERVAL)
                elapsed += POLL_INTERVAL

                current = capture_pane(pane_id)
                # Stream incremental updates
                response_so_far = extract_response(before, current, prompt)
                if response_so_far and response_so_far != last_streamed:
                    if stream_cb:
                        delta = response_so_far
                        if last_streamed and response_so_far.startswith(last_streamed):
                            delta = response_so_far[len(last_streamed):]
                        if delta.strip():
                            await stream_cb(delta, False)
                    last_streamed = response_so_far

                if current != before and is_claude_idle(current):
                    log.info("Response complete (%ds)", elapsed)
                    break

            if elapsed >= TIMEOUT:
                log.warning("Timeout after %ds", TIMEOUT)

            # Final extract
            final = capture_pane(pane_id)
            result.text = extract_response(before, final, prompt)

            if stream_cb:
                await stream_cb("", True)

        except Exception:
            log.exception("Error in tmux session %s", self.info.project)
            raise
        finally:
            self._running = False

        return result

    async def interrupt(self) -> bool:
        if self._running:
            self._interrupted = True
            subprocess.run(
                ["tmux", "send-keys", "-t", self.info.pane_id, "C-c"],
                timeout=5,
            )
            log.info("Sent Ctrl+C to %s", self.info.project)
            self._running = False
            return True
        return False

    @property
    def is_running(self) -> bool:
        return self._running


# ── ClaudeManager ──

class ClaudeManager:
    def __init__(self, settings: "Settings") -> None:
        self.settings = settings
        self._sessions: dict[str, TmuxSession] = {}  # project_name -> TmuxSession

    def load_sessions(self) -> None:
        """Load sessions from /tmp/claude_sessions/ registry."""
        self._sessions.clear()

        if not SESSION_DIR.exists():
            log.warning("Session dir %s not found", SESSION_DIR)
            return

        for f in SESSION_DIR.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                project = data["project"]
                pane_id = data["pane_id"]

                if not _is_pane_alive(pane_id):
                    log.info("Session %s pane %s dead — skipping", project, pane_id)
                    f.unlink(missing_ok=True)
                    continue

                info = SessionInfo(
                    project=project,
                    pane_id=pane_id,
                    work_dir=data.get("work_dir", ""),
                )
                self._sessions[project] = TmuxSession(info)
                log.info("Loaded session: %s (pane %s, dir %s)", project, pane_id, info.work_dir)
            except Exception as e:
                log.warning("Failed to parse session file %s: %s", f, e)

    def refresh(self) -> None:
        """Reload sessions from registry."""
        self.load_sessions()

    def get_session(self, user_id: int, project_dir: str, **_: Any) -> TmuxSession | None:
        # Try exact project name match
        project_name = os.path.basename(project_dir.rstrip("/"))
        if project_name in self._sessions:
            return self._sessions[project_name]
        # Try by work_dir match
        for s in self._sessions.values():
            if s.info.work_dir.rstrip("/") == project_dir.rstrip("/"):
                return s
        # Try partial name match
        for name, s in self._sessions.items():
            if project_name.lower() in name.lower() or name.lower() in project_name.lower():
                return s
        return None

    async def interrupt_session(self, user_id: int, project_dir: str) -> bool:
        session = self.get_session(user_id, project_dir)
        if session:
            return await session.interrupt()
        return False

    def get_active_projects(self, user_id: int) -> list[str]:
        return [s.info.project for s in self._sessions.values() if s.is_running]

    def get_all_sessions(self) -> dict[str, SessionInfo]:
        return {name: s.info for name, s in self._sessions.items()}

    def get_or_create_sdk_session(self, project_dir: str) -> "SDKSession":
        """Get or create an SDK session for projects without tmux."""
        key = f"sdk:{project_dir}"
        if key not in self._sessions:
            if not HAS_SDK:
                raise RuntimeError(
                    "claude-agent-sdk not installed. "
                    "Install with: uv add claude-agent-sdk"
                )
            self._sessions[key] = SDKSession(project_dir, self.settings)  # type: ignore[assignment]
            log.info("Created SDK session for %s", project_dir)
        return self._sessions[key]  # type: ignore[return-value]

    def clear_sdk_session(self, project_dir: str) -> None:
        """Remove SDK session so next message creates a fresh one."""
        key = f"sdk:{project_dir}"
        if key in self._sessions:
            del self._sessions[key]
            log.info("Cleared SDK session for %s", project_dir)

    async def execute_with_retry(
        self,
        user_id: int,
        project_dir: str,
        prompt: str,
        stream_cb: StreamCallback | None = None,
        system_prompt: str = "",
        max_retries: int = 2,
    ) -> SessionResult:
        # Try tmux first
        session = self.get_session(user_id, project_dir)
        if not session:
            self.refresh()
            session = self.get_session(user_id, project_dir)

        if session:
            return await session.execute(prompt, stream_cb)

        # Fallback: SDK session for projects without tmux
        if not HAS_SDK:
            project_name = os.path.basename(project_dir.rstrip("/"))
            tmux_names = list(self._sessions.keys())
            raise RuntimeError(
                f"No tmux session for '{project_name}'.\n"
                f"Available tmux sessions: {tmux_names or 'none'}\n\n"
                f"Use /project <name> to switch, or install SDK:\n"
                f"uv add claude-agent-sdk"
            )
        sdk_session = self.get_or_create_sdk_session(project_dir)
        return await sdk_session.execute(prompt, stream_cb)


# ── SDKSession — fallback for projects without tmux ──

class SDKSession:
    """Connects to existing or creates new Claude Code session via SDK."""

    def __init__(self, project_dir: str, settings: "Settings") -> None:
        self.project_dir = project_dir
        self.settings = settings
        self._client: Any = None
        self._running = False
        self._sdk_session_id: str | None = self._find_latest_session()
        self.info = SessionInfo(
            project=os.path.basename(project_dir),
            pane_id="sdk",
            work_dir=project_dir,
        )
        if self._sdk_session_id:
            log.info("Found existing session for %s: %s", project_dir, self._sdk_session_id)

    def _find_latest_session(self) -> str | None:
        """Find the most recent session ID from ~/.claude/projects/."""
        # Check multiple possible locations:
        # 1. Windows user dir (via WSL /mnt/c/) — primary for Windows projects
        # 2. Native home (~/.claude/projects/) — for WSL projects
        # Windows first because SDK creates sessions in WSL home, but the
        # user's real active session is in Windows ~/.claude/
        candidates: list[Path] = []

        # If running on WSL, check Windows user dir first
        win_home = Path("/mnt/c/Users")
        if win_home.exists():
            for u in win_home.iterdir():
                p = u / ".claude" / "projects"
                if p.exists():
                    candidates.append(p)

        # Then check native home
        native = Path.home() / ".claude" / "projects"
        if native.exists() and native not in candidates:
            candidates.append(native)

        # Build encoded project dir name (path separators → hyphens)
        # Windows: D:\project_2026\flipking → D--project-2026-flipking
        # Linux /mnt/d/project_2026/flipking → also try D--project-2026-flipking
        project_path = self.project_dir.replace("\\", "/")
        encoded = project_path.replace(":", "-").replace("/", "-")
        encoded = encoded.rstrip("-")

        # Claude Code encodes underscores as hyphens too
        encoded = encoded.replace("_", "-")

        # For WSL /mnt/d/ paths, also try Windows-style encoding
        alt_encoded = None
        if project_path.startswith("/mnt/"):
            # /mnt/d/project_2026/flipking → D--project-2026-flipking
            parts = project_path.split("/")  # ['', 'mnt', 'd', 'project_2026', ...]
            if len(parts) >= 3:
                drive = parts[2].upper()
                rest = "-".join(parts[3:]).replace("_", "-")
                alt_encoded = f"{drive}--{rest}".rstrip("-")

        for claude_dir in candidates:
            if not claude_dir.exists():
                continue
            for enc in [encoded, alt_encoded]:
                if not enc:
                    continue
                project_session_dir = claude_dir / enc
                if project_session_dir.exists():
                    jsonl_files = sorted(
                        project_session_dir.glob("*.jsonl"),
                        key=lambda f: f.stat().st_mtime,
                        reverse=True,
                    )
                    if jsonl_files:
                        return jsonl_files[0].stem
            # Fallback: partial name match
            project_name = os.path.basename(self.project_dir).lower()
            for d in claude_dir.iterdir():
                if d.is_dir() and project_name in d.name.lower():
                    jsonl_files = sorted(
                        d.glob("*.jsonl"),
                        key=lambda f: f.stat().st_mtime,
                        reverse=True,
                    )
                    if jsonl_files:
                        return jsonl_files[0].stem
        return None

    async def execute(
        self,
        prompt: str,
        stream_cb: StreamCallback | None = None,
    ) -> SessionResult:
        if not HAS_SDK:
            raise RuntimeError("claude-agent-sdk not installed")

        self._running = True
        result = SessionResult(session_name=self.info.project)
        text_parts: list[str] = []

        try:
            opts = ClaudeAgentOptions(
                cwd=self.project_dir,
                permission_mode=self.settings.permission_mode,
                env={"CLAUDECODE": ""},
            )
            if self._sdk_session_id:
                opts.resume = self._sdk_session_id

            tools = self.settings.get_allowed_tools()
            if tools:
                opts.allowed_tools = tools
            if self.settings.model:
                opts.model = self.settings.model
            if self.settings.max_turns > 0:
                opts.max_turns = self.settings.max_turns

            self._client = ClaudeSDKClient(options=opts)
            await self._client.connect()
            await self._client.query(prompt)

            async for msg in self._client.receive_response():
                if isinstance(msg, SystemMessage):
                    if msg.subtype == "init" and hasattr(msg, "data"):
                        sid = msg.data.get("session_id")
                        if sid:
                            self._sdk_session_id = sid

                elif isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text:
                            text_parts.append(block.text)
                            if stream_cb:
                                await stream_cb(block.text, False)

                elif isinstance(msg, ResultMessage):
                    if msg.session_id:
                        self._sdk_session_id = msg.session_id
                    if not text_parts and msg.result:
                        text_parts.append(msg.result)
                        if stream_cb:
                            await stream_cb(msg.result, False)

            result.text = "".join(text_parts)
            if stream_cb:
                await stream_cb("", True)

        except Exception:
            log.exception("SDK error in %s", self.project_dir)
            raise
        finally:
            self._running = False
            if self._client:
                try:
                    await self._client.disconnect()
                except Exception:
                    pass
                self._client = None

        return result

    async def interrupt(self) -> bool:
        if self._client and self._running:
            try:
                await self._client.interrupt()
                self._running = False
                return True
            except Exception:
                log.exception("Failed to interrupt SDK session")
        return False

    @property
    def is_running(self) -> bool:
        return self._running
