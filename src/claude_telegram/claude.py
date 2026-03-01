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
    from .pty_session import WindowsPtySession

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
    "·", "✻", "✽", "✢", "✶", "*", "●", "○", "◐", "◑", "◒", "◓",
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
    """Detect any active processing line (spinners + running tools).

    Used by is_claude_idle() to know Claude is still working.
    """
    if "…" not in stripped:
        return False
    if len(stripped) > 80:
        return False
    if any(s in stripped for s in ("ctrl+o", "shift+tab", "esc to")):
        return False
    if stripped.startswith("⎿"):
        return False
    return any(stripped.startswith(p) for p in _PROCESSING_PREFIXES)


def _is_spinner_line(stripped: str) -> bool:
    """Detect spinner/thinking lines (NOT completed tool calls).

    Used by extract_response() to filter transient noise while preserving
    completed tool execution lines:
      ● Bash(echo "test…")              ← tool call, ( before … → keep
      ● Bash …                          ← spinner, no ( → filter
      ✽ Philosophising… (53s · ↑ 144t)  ← thinking, ( after … → filter
    """
    if "…" not in stripped:
        return False
    if len(stripped) > 120:
        return False
    if any(s in stripped for s in ("ctrl+o", "shift+tab", "esc to")):
        return False
    if stripped.startswith("⎿"):
        return False
    if not any(stripped.startswith(p) for p in _PROCESSING_PREFIXES):
        return False
    # Tool calls: ● Bash(cmd…) — ( appears BEFORE …
    # Thinking:   ✽ Thinking… (53s) — ( appears AFTER …
    paren_idx = stripped.find("(")
    ellipsis_idx = stripped.find("…")
    if paren_idx >= 0 and paren_idx < ellipsis_idx:
        return False  # tool call with truncated args → keep
    return True


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

    # Strategy 1: Find ❯ + user message start (short to handle tmux wrapping)
    user_short = user_msg[:15].strip()
    for i, line in enumerate(after_lines):
        stripped = line.strip()
        if stripped.startswith("❯") and user_short and user_short in stripped:
            # Skip user prompt + wrapped continuation lines
            j = i + 1
            while j < len(after_lines):
                next_s = after_lines[j].strip()
                if not next_s:
                    j += 1
                    continue
                # Response starts with ● or ⎿ or non-indented content
                if next_s.startswith(("●", "⎿")) or not after_lines[j].startswith(" "):
                    break
                j += 1
            new_content = "\n".join(after_lines[j:])
            break

    # Strategy 2: Anchor using lines BEFORE the ❯ prompt in `before`
    if not new_content:
        # Find the last ❯ in before, use content above it as anchor
        prompt_idx = -1
        for i in range(len(before_lines) - 1, -1, -1):
            if before_lines[i].strip().startswith("❯"):
                prompt_idx = i
                break
        # Use 3 non-empty lines before the prompt
        anchor_lines = []
        if prompt_idx > 0:
            for line in before_lines[max(0, prompt_idx - 5):prompt_idx]:
                s = line.strip()
                if s and not all(c in "─━═" for c in s):
                    anchor_lines.append(line)
        if anchor_lines:
            anchor = "\n".join(anchor_lines[-3:])
            after_text = "\n".join(after_lines)
            idx = after_text.find(anchor)
            if idx >= 0:
                remaining = after_text[idx + len(anchor):]
                # Skip to after the ❯ prompt line
                rem_lines = remaining.split("\n")
                start = 0
                for k, line in enumerate(rem_lines):
                    if line.strip().startswith("❯"):
                        start = k + 1
                        # Skip continuation/empty lines after prompt
                        while start < len(rem_lines):
                            ns = rem_lines[start].strip()
                            if ns and ns.startswith(("●", "⎿")):
                                break
                            if ns and not rem_lines[start].startswith(" "):
                                break
                            start += 1
                        break
                new_content = "\n".join(rem_lines[start:])

    # Strategy 3: Last resort — set difference
    if not new_content:
        before_set = set(l for l in before_clean.split("\n") if l.strip())
        new_content = "\n".join(l for l in after_lines if l not in before_set)

    # Clean noise lines
    cleaned_lines = []
    for line in new_content.split("\n"):
        stripped = line.strip()
        if stripped and all(c in "─━═" for c in stripped):
            continue
        if "shift+tab" in stripped or "esc to interrupt" in stripped:
            continue
        if "ctrl+o to expand" in stripped:
            continue
        if stripped.startswith("Tip:") or (stripped.startswith("⎿") and "Tip:" in stripped):
            continue
        if stripped in ("❯", ">"):
            continue
        if _is_spinner_line(stripped):
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
                # Stream full text each poll (not deltas — pane capture
                # is unstable between polls due to ANSI/whitespace changes)
                response_so_far = extract_response(before, current, prompt)
                if response_so_far and response_so_far != last_streamed:
                    if stream_cb:
                        await stream_cb(response_so_far, False)
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
        self._sessions: dict[str, TmuxSession | "WindowsPtySession"] = {}  # project -> session

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
                session_type = data.get("type", "tmux")

                if session_type == "pty":
                    self._load_pty_session(project, data)
                else:
                    self._load_tmux_session(project, data, f)
            except Exception as e:
                log.warning("Failed to parse session file %s: %s", f, e)

    def _load_tmux_session(self, project: str, data: dict, f: Path) -> None:
        pane_id = data["pane_id"]
        if not _is_pane_alive(pane_id):
            log.info("Session %s pane %s dead — skipping", project, pane_id)
            f.unlink(missing_ok=True)
            return

        info = SessionInfo(
            project=project,
            pane_id=pane_id,
            work_dir=data.get("work_dir", ""),
        )
        self._sessions[project] = TmuxSession(info)
        log.info("Loaded session: %s (pane %s, dir %s)", project, pane_id, info.work_dir)

    def _load_pty_session(self, project: str, data: dict) -> None:
        from .pty_session import WindowsPtySession

        host = data.get("host", "127.0.0.1")
        port = data.get("port", 50001)
        info = SessionInfo(
            project=project,
            pane_id=f"pty:{host}:{port}",
            work_dir=data.get("work_dir", ""),
        )
        session = WindowsPtySession(info, host, port)
        self._sessions[project] = session
        log.info("Loaded PTY session: %s (%s:%d, dir %s)", project, host, port, info.work_dir)

    def scan_tmux_panes(self) -> list[str]:
        """Scan all tmux panes for Claude Code sessions and auto-register.

        Returns list of newly added project names.
        Heavy operation — use only at startup.
        """
        new_projects: list[str] = []
        try:
            r = subprocess.run(
                ["tmux", "list-panes", "-a", "-F",
                 "#{pane_id}\t#{pane_current_path}\t#{pane_current_command}"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0:
                return new_projects

            known_panes = {s.info.pane_id for s in self._sessions.values()}

            for line in r.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                parts = line.split("\t")
                if len(parts) < 3:
                    continue
                pane_id, work_dir, cmd = parts[0], parts[1], parts[2]

                if pane_id in known_panes:
                    continue

                # Check if this pane runs Claude Code
                if cmd not in ("claude", "node"):
                    continue
                # Verify by capturing pane content
                content = capture_pane(pane_id, lines=50)
                cleaned = strip_ansi(content)
                if "Claude Code" not in cleaned and "❯" not in cleaned:
                    continue

                project = os.path.basename(work_dir.rstrip("/"))
                if project in self._sessions:
                    continue

                # Register
                info = SessionInfo(project=project, pane_id=pane_id, work_dir=work_dir)
                self._sessions[project] = TmuxSession(info)
                new_projects.append(project)
                log.info("Auto-detected Claude session: %s (pane %s, dir %s)",
                         project, pane_id, work_dir)

                # Save to session dir for persistence
                SESSION_DIR.mkdir(parents=True, exist_ok=True)
                session_file = SESSION_DIR / f"{project}.json"
                session_file.write_text(json.dumps({
                    "project": project,
                    "pane_id": pane_id,
                    "work_dir": work_dir,
                    "registered_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                }))
        except Exception:
            log.exception("Error scanning tmux panes")

        # Also clean dead sessions
        self._clean_dead_sessions()

        return new_projects

    def check_new_sessions(self) -> tuple[list[str], list[str]]:
        """Check /tmp/claude_sessions/ for new/removed session files.

        Called periodically instead of heavy tmux scan.
        Returns (new_projects, removed_projects).
        """
        new_projects: list[str] = []
        removed_projects: list[str] = []

        if not SESSION_DIR.exists():
            return new_projects, removed_projects

        known = set(self._sessions.keys())
        # Files currently on disk
        disk_projects: set[str] = set()

        for f in SESSION_DIR.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                project = data["project"]
                session_type = data.get("type", "tmux")
                disk_projects.add(project)

                if project in known:
                    continue

                if session_type == "pty":
                    from .pty_session import WindowsPtySession

                    host = data.get("host", "127.0.0.1")
                    port = data.get("port", 50001)
                    info = SessionInfo(
                        project=project,
                        pane_id=f"pty:{host}:{port}",
                        work_dir=data.get("work_dir", ""),
                    )
                    session = WindowsPtySession(info, host, port)
                    self._sessions[project] = session
                    new_projects.append(project)
                    log.info("New PTY session from hook: %s (%s:%d)", project, host, port)
                else:
                    pane_id = data.get("pane_id", "unknown")

                    # Resolve "unknown" pane by scanning tmux
                    if pane_id == "unknown":
                        pane_id = self._find_pane_for_dir(data.get("work_dir", ""))
                        if pane_id:
                            data["pane_id"] = pane_id
                            f.write_text(json.dumps(data))
                        else:
                            log.debug("Session %s pane unknown and not found in tmux", project)
                            continue

                    if not _is_pane_alive(pane_id):
                        log.info("Session %s pane %s dead — removing", project, pane_id)
                        f.unlink(missing_ok=True)
                        disk_projects.discard(project)
                        continue

                    info = SessionInfo(
                        project=project,
                        pane_id=pane_id,
                        work_dir=data.get("work_dir", ""),
                    )
                    self._sessions[project] = TmuxSession(info)
                    new_projects.append(project)
                    log.info("New session from hook: %s (pane %s)", project, pane_id)
            except Exception as e:
                log.warning("Failed to parse session file %s: %s", f, e)

        # Detect removed sessions (file deleted by unregister hook)
        for name in list(known):
            if name.startswith("sdk:"):
                continue
            if name not in disk_projects:
                del self._sessions[name]
                removed_projects.append(name)
                log.info("Session ended (hook): %s", name)

        return new_projects, removed_projects

    def _find_pane_for_dir(self, work_dir: str) -> str | None:
        """Find a tmux pane running Claude in the given directory."""
        if not work_dir:
            return None
        try:
            r = subprocess.run(
                ["tmux", "list-panes", "-a", "-F",
                 "#{pane_id}\t#{pane_current_path}\t#{pane_current_command}"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0:
                return None
            for line in r.stdout.strip().split("\n"):
                parts = line.split("\t")
                if len(parts) < 3:
                    continue
                pane_id, pdir, cmd = parts[0], parts[1], parts[2]
                if pdir.rstrip("/") == work_dir.rstrip("/") and cmd in ("claude", "node"):
                    return pane_id
        except Exception:
            pass
        return None

    def _clean_dead_sessions(self) -> None:
        """Remove sessions whose tmux pane / PTY connection is no longer alive."""
        from .pty_session import WindowsPtySession

        dead = []
        for name, s in self._sessions.items():
            if name.startswith("sdk:"):
                continue
            if isinstance(s, WindowsPtySession):
                if not s.is_alive:
                    dead.append(name)
            elif not _is_pane_alive(s.info.pane_id):
                dead.append(name)
        for name in dead:
            del self._sessions[name]
            f = SESSION_DIR / f"{name}.json"
            f.unlink(missing_ok=True)
            log.info("Removed dead session: %s", name)

    async def connect_pty_sessions(self) -> None:
        """Connect all loaded PTY sessions (call after load_sessions)."""
        from .pty_session import WindowsPtySession

        for s in self._sessions.values():
            if isinstance(s, WindowsPtySession) and not s.is_alive:
                await s.connect()

    def refresh(self) -> None:
        """Reload sessions from registry."""
        self.load_sessions()

    def get_session(self, user_id: int, project_dir: str, **_: Any) -> "TmuxSession | WindowsPtySession | None":
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
        from .pty_session import WindowsPtySession

        # Try tmux / PTY first
        session = self.get_session(user_id, project_dir)
        if not session:
            self.refresh()
            await self.connect_pty_sessions()
            session = self.get_session(user_id, project_dir)

        if session:
            # Auto-connect PTY if needed
            if isinstance(session, WindowsPtySession) and not session.is_alive:
                await session.connect()
                if not session.is_alive:
                    log.warning("PTY session %s not reachable, falling back", session.info.project)
                    session = None

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

    @staticmethod
    def find_sessions(project_dir: str, limit: int = 5) -> list[dict]:
        """Find recent session IDs from ~/.claude/projects/.

        Returns list of {id, mtime, source} dicts, newest first.
        """
        results: list[dict] = []
        seen_ids: set[str] = set()

        # Check Windows first (via WSL /mnt/c/), then native home
        candidates: list[Path] = []
        win_home = Path("/mnt/c/Users")
        if win_home.exists():
            for u in win_home.iterdir():
                p = u / ".claude" / "projects"
                if p.exists():
                    candidates.append(p)
        native = Path.home() / ".claude" / "projects"
        if native.exists() and native not in candidates:
            candidates.append(native)

        # Build encoded project dir name
        project_path = project_dir.replace("\\", "/")
        encoded = project_path.replace(":", "-").replace("/", "-").rstrip("-").replace("_", "-")

        # For WSL /mnt/d/ paths, also try Windows-style encoding
        encodings = [encoded]
        if project_path.startswith("/mnt/"):
            parts = project_path.split("/")
            if len(parts) >= 3:
                drive = parts[2].upper()
                rest = "-".join(parts[3:]).replace("_", "-")
                encodings.append(f"{drive}--{rest}".rstrip("-"))

        for claude_dir in candidates:
            if not claude_dir.exists():
                continue
            source = "windows" if "/mnt/c/" in str(claude_dir) else "wsl"
            # Try exact and alt encodings
            for enc in encodings:
                session_dir = claude_dir / enc
                if session_dir.exists():
                    for f in sorted(
                        session_dir.glob("*.jsonl"),
                        key=lambda f: f.stat().st_mtime,
                        reverse=True,
                    ):
                        sid = f.stem
                        if sid not in seen_ids:
                            seen_ids.add(sid)
                            results.append({
                                "id": sid,
                                "mtime": f.stat().st_mtime,
                                "source": source,
                            })
                        if len(results) >= limit:
                            return results
            # Fallback: partial name match
            project_name = os.path.basename(project_dir).lower()
            for d in claude_dir.iterdir():
                if d.is_dir() and project_name in d.name.lower():
                    for f in sorted(
                        d.glob("*.jsonl"),
                        key=lambda f: f.stat().st_mtime,
                        reverse=True,
                    ):
                        sid = f.stem
                        if sid not in seen_ids:
                            seen_ids.add(sid)
                            results.append({
                                "id": sid,
                                "mtime": f.stat().st_mtime,
                                "source": source,
                            })
                        if len(results) >= limit:
                            return results
        return results

    def _find_latest_session(self) -> str | None:
        """Find the most recent session ID."""
        sessions = self.find_sessions(self.project_dir, limit=1)
        return sessions[0]["id"] if sessions else None

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
                                # Send full accumulated text (consistent with tmux mode)
                                await stream_cb("".join(text_parts), False)

                elif isinstance(msg, ResultMessage):
                    if msg.session_id:
                        self._sdk_session_id = msg.session_id
                    if not text_parts and msg.result:
                        text_parts.append(msg.result)
                        if stream_cb:
                            await stream_cb("".join(text_parts), False)

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
