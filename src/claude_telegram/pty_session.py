"""WindowsPtySession — TCP client for bridge-claude PTY wrapper."""

from __future__ import annotations

import asyncio
import json
import logging

from .claude import (
    MIN_WAIT,
    POLL_INTERVAL,
    TIMEOUT,
    SessionInfo,
    SessionResult,
    StreamCallback,
    extract_response,
    is_claude_idle,
)

log = logging.getLogger(__name__)

MAX_BUFFER_LINES = 2000


class WindowsPtySession:
    """TCP client that talks to bridge-claude (PTY wrapper).

    Provides the same interface as TmuxSession: execute(), interrupt(),
    is_running, plus send_key() for raw key injection.
    """

    def __init__(self, info: SessionInfo, host: str, port: int) -> None:
        self.info = info
        self.host = host
        self.port = port
        self._running = False
        self._interrupted = False
        self._alive = False
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._recv_task: asyncio.Task | None = None
        self._pane_buffer: list[str] = []
        self._buf_lock = asyncio.Lock()

    async def connect(self) -> None:
        """Open TCP connection and start background receiver."""
        try:
            self._reader, self._writer = await asyncio.open_connection(
                self.host, self.port,
            )
        except OSError as e:
            log.warning("PTY connect failed %s:%d — %s", self.host, self.port, e)
            return

        # Read greeting: {"type":"status","alive":true}
        try:
            raw = await asyncio.wait_for(self._reader.readline(), timeout=5)
            msg = json.loads(raw.decode())
            if msg.get("type") == "status" and msg.get("alive"):
                self._alive = True
                log.info("PTY connected: %s:%d (%s)", self.host, self.port, self.info.project)
            else:
                log.warning("PTY unexpected greeting: %s", msg)
                return
        except Exception as e:
            log.warning("PTY greeting failed: %s", e)
            return

        self._recv_task = asyncio.create_task(self._receiver_loop())

    async def _receiver_loop(self) -> None:
        """Background task: read JSON-Lines output, accumulate into buffer."""
        assert self._reader is not None
        while self._alive:
            try:
                raw = await self._reader.readline()
                if not raw:
                    break
                msg = json.loads(raw.decode())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            except Exception:
                break

            if msg.get("type") == "output":
                data = msg.get("data", "")
                if data:
                    async with self._buf_lock:
                        # Append new lines to buffer
                        new_lines = data.split("\n")
                        self._pane_buffer.extend(new_lines)
                        # Trim to max
                        if len(self._pane_buffer) > MAX_BUFFER_LINES:
                            self._pane_buffer = self._pane_buffer[-MAX_BUFFER_LINES:]
            elif msg.get("type") == "status":
                if not msg.get("alive"):
                    break

        self._alive = False
        log.info("PTY receiver ended: %s", self.info.project)

    async def _get_buffer_snapshot(self) -> str:
        """Get current buffer as a single string (like capture_pane)."""
        async with self._buf_lock:
            return "\n".join(self._pane_buffer)

    async def _send_json(self, obj: dict) -> None:
        """Send a JSON-Lines message to bridge-claude."""
        if not self._writer or not self._alive:
            return
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        try:
            self._writer.write(line.encode("utf-8"))
            await self._writer.drain()
        except OSError:
            self._alive = False

    async def send_key(self, data: str) -> None:
        """Send raw key data to the PTY (e.g. '\\x03' for Ctrl+C)."""
        await self._send_json({"type": "input", "data": data})

    async def execute(
        self,
        prompt: str,
        stream_cb: StreamCallback | None = None,
    ) -> SessionResult:
        self._running = True
        self._interrupted = False
        result = SessionResult(session_name=self.info.project)

        try:
            before = await self._get_buffer_snapshot()

            # Send prompt as input
            await self._send_json({"type": "input", "data": prompt + "\n"})
            log.info("PTY sent to %s: %s", self.info.project, prompt[:80])

            await asyncio.sleep(MIN_WAIT)
            elapsed = MIN_WAIT
            last_streamed = ""

            while elapsed < TIMEOUT and not self._interrupted:
                await asyncio.sleep(POLL_INTERVAL)
                elapsed += POLL_INTERVAL

                current = await self._get_buffer_snapshot()
                response_so_far = extract_response(before, current, prompt)
                if response_so_far and response_so_far != last_streamed:
                    if stream_cb:
                        await stream_cb(response_so_far, False)
                    last_streamed = response_so_far

                if current != before and is_claude_idle(current):
                    log.info("PTY response complete (%ds)", elapsed)
                    break

            if elapsed >= TIMEOUT:
                log.warning("PTY timeout after %ds", TIMEOUT)

            final = await self._get_buffer_snapshot()
            result.text = extract_response(before, final, prompt)

            if stream_cb:
                await stream_cb("", True)

        except Exception:
            log.exception("Error in PTY session %s", self.info.project)
            raise
        finally:
            self._running = False

        return result

    async def interrupt(self) -> bool:
        if self._running:
            self._interrupted = True
            await self.send_key("\x03")
            log.info("PTY sent Ctrl+C to %s", self.info.project)
            self._running = False
            return True
        return False

    async def disconnect(self) -> None:
        self._alive = False
        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
        self._reader = None

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_alive(self) -> bool:
        return self._alive
