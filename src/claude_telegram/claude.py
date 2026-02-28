"""Claude Agent SDK wrapper — session management with streaming and interrupt."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

if TYPE_CHECKING:
    from .config import Settings

log = logging.getLogger(__name__)

# Callback type: async fn(text_chunk, is_final)
StreamCallback = Callable[[str, bool], Coroutine[Any, Any, None]]


@dataclass
class SessionResult:
    text: str = ""
    cost_usd: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    num_turns: int = 0
    session_id: str = ""
    tools_used: list[str] = field(default_factory=list)


class ClaudeSession:
    """One session per (user, project) pair. Wraps ClaudeSDKClient for statefulness."""

    def __init__(
        self,
        project_dir: str,
        settings: "Settings",
        system_prompt: str = "",
    ) -> None:
        self.project_dir = project_dir
        self.settings = settings
        self.system_prompt = system_prompt
        self._client: ClaudeSDKClient | None = None
        self._running = False
        self._sdk_session_id: str | None = None

    def _build_options(self) -> ClaudeAgentOptions:
        opts: dict[str, Any] = {
            "cwd": self.project_dir,
            "permission_mode": self.settings.permission_mode,
            "env": {"CLAUDECODE": ""},  # allow running inside Claude Code session
        }
        tools = self.settings.get_allowed_tools()
        if tools:
            opts["allowed_tools"] = tools
        if self.settings.model:
            opts["model"] = self.settings.model
        if self.settings.max_turns > 0:
            opts["max_turns"] = self.settings.max_turns
        if self.system_prompt:
            opts["system_prompt"] = self.system_prompt
        return ClaudeAgentOptions(**opts)

    async def execute(
        self,
        prompt: str,
        stream_cb: StreamCallback | None = None,
    ) -> SessionResult:
        """Send prompt and stream response. Returns final result."""
        self._running = True
        result = SessionResult()
        text_parts: list[str] = []

        try:
            # Create new client for each query to avoid state issues
            opts = self._build_options()
            if self._sdk_session_id:
                opts.resume = self._sdk_session_id

            self._client = ClaudeSDKClient(options=opts)
            await self._client.connect()
            await self._client.query(prompt)

            async for msg in self._client.receive_response():
                log.debug("SDK message: type=%s %r", type(msg).__name__, msg)

                if isinstance(msg, SystemMessage):
                    if msg.subtype == "init" and hasattr(msg, "data"):
                        sid = msg.data.get("session_id")
                        if sid:
                            self._sdk_session_id = sid
                            result.session_id = sid

                elif isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        log.debug("Content block: type=%s %r", type(block).__name__, block)
                        if isinstance(block, TextBlock) and block.text:
                            text_parts.append(block.text)
                            if stream_cb:
                                await stream_cb(block.text, False)
                        elif isinstance(block, ToolUseBlock):
                            result.tools_used.append(block.name)

                elif isinstance(msg, ResultMessage):
                    result.cost_usd = msg.total_cost_usd
                    result.duration_ms = msg.duration_ms
                    result.num_turns = msg.num_turns
                    if msg.session_id:
                        self._sdk_session_id = msg.session_id
                        result.session_id = msg.session_id
                    if msg.usage:
                        result.input_tokens = msg.usage.get("input_tokens", 0)
                        result.output_tokens = msg.usage.get("output_tokens", 0)
                    # Fallback: use result text if no streaming text was captured
                    if not text_parts and msg.result:
                        text_parts.append(msg.result)
                        if stream_cb:
                            await stream_cb(msg.result, False)

            result.text = "".join(text_parts)
            if stream_cb:
                await stream_cb("", True)  # signal completion

        except Exception:
            log.exception("Claude SDK error in project %s", self.project_dir)
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
        """Interrupt the current query. Returns True if interrupted."""
        if self._client and self._running:
            try:
                await self._client.interrupt()
                self._running = False
                return True
            except Exception:
                log.exception("Failed to interrupt session")
        return False

    @property
    def is_running(self) -> bool:
        return self._running

    def reset(self) -> None:
        """Clear session state (for /new command)."""
        self._sdk_session_id = None


class ClaudeManager:
    """Manages ClaudeSessions per (user_id, project_dir)."""

    def __init__(self, settings: "Settings") -> None:
        self.settings = settings
        self._sessions: dict[str, ClaudeSession] = {}

    def _key(self, user_id: int, project_dir: str) -> str:
        return f"{user_id}:{project_dir}"

    def get_session(
        self,
        user_id: int,
        project_dir: str,
        system_prompt: str = "",
    ) -> ClaudeSession:
        key = self._key(user_id, project_dir)
        if key not in self._sessions:
            self._sessions[key] = ClaudeSession(
                project_dir=project_dir,
                settings=self.settings,
                system_prompt=system_prompt,
            )
        return self._sessions[key]

    def reset_session(self, user_id: int, project_dir: str) -> None:
        key = self._key(user_id, project_dir)
        session = self._sessions.get(key)
        if session:
            session.reset()

    async def interrupt_session(self, user_id: int, project_dir: str) -> bool:
        key = self._key(user_id, project_dir)
        session = self._sessions.get(key)
        if session:
            return await session.interrupt()
        return False

    def get_active_projects(self, user_id: int) -> list[str]:
        prefix = f"{user_id}:"
        return [
            k.split(":", 1)[1]
            for k, s in self._sessions.items()
            if k.startswith(prefix) and s.is_running
        ]

    async def execute_with_retry(
        self,
        user_id: int,
        project_dir: str,
        prompt: str,
        stream_cb: StreamCallback | None = None,
        system_prompt: str = "",
        max_retries: int = 3,
    ) -> SessionResult:
        """Execute with exponential backoff on transient errors."""
        session = self.get_session(user_id, project_dir, system_prompt)
        last_error: Exception | None = None

        for attempt in range(max_retries):
            try:
                return await session.execute(prompt, stream_cb)
            except Exception as e:
                last_error = e
                err_str = str(e).lower()
                # Only retry on transient errors
                transient = any(
                    kw in err_str
                    for kw in ("timeout", "connection", "rate", "503", "502", "429")
                )
                if not transient or attempt == max_retries - 1:
                    raise
                wait = 2**attempt
                log.warning(
                    "Transient error (attempt %d/%d), retrying in %ds: %s",
                    attempt + 1,
                    max_retries,
                    wait,
                    e,
                )
                await asyncio.sleep(wait)

        raise last_error  # unreachable but type-safe
