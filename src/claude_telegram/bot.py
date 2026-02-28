"""Telegram bot handlers — commands and message processing."""

from __future__ import annotations

import asyncio
import html
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

if TYPE_CHECKING:
    from .claude import ClaudeManager
    from .config import Settings
    from .store import Store

log = logging.getLogger(__name__)

# Telegram message length limit
TG_MAX_LEN = 4096
# Minimum interval between message edits (seconds)
EDIT_THROTTLE = 2.0


def _truncate(text: str, limit: int = TG_MAX_LEN - 100) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n... (truncated)"


def _escape(text: str) -> str:
    return html.escape(text)


def _split_message(text: str, limit: int = TG_MAX_LEN - 100) -> list[str]:
    """Split long text into multiple messages."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    while text:
        if len(text) <= limit:
            parts.append(text)
            break
        # Find a good split point
        split_at = text.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = limit
        parts.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return parts


class Bot:
    def __init__(
        self,
        settings: "Settings",
        claude: "ClaudeManager",
        store: "Store",
    ) -> None:
        self.settings = settings
        self.claude = claude
        self.store = store
        # user_id -> active project_dir
        self._user_projects: dict[int, str] = {}
        # user_id -> active store session id
        self._user_store_sessions: dict[int, int] = {}

    def _is_allowed(self, user_id: int) -> bool:
        allowed = self.settings.get_allowed_users()
        return not allowed or user_id in allowed

    def _get_project(self, user_id: int) -> str | None:
        if user_id in self._user_projects:
            return self._user_projects[user_id]
        # Default to first available tmux session
        sessions = self.claude.get_all_sessions()
        if sessions:
            first = next(iter(sessions.values()))
            self._user_projects[user_id] = first.work_dir or first.project
            return self._user_projects[user_id]
        return None

    async def _ensure_store_session(self, user_id: int, project_dir: str) -> int:
        if user_id in self._user_store_sessions:
            return self._user_store_sessions[user_id]
        existing = await self.store.get_active_session(user_id, project_dir)
        if existing:
            self._user_store_sessions[user_id] = existing["id"]
            return existing["id"]
        sid = await self.store.create_session(user_id, project_dir)
        self._user_store_sessions[user_id] = sid
        return sid

    # --- Command Handlers ---

    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        sessions = self.claude.get_all_sessions()
        session_list = ", ".join(sessions.keys()) if sessions else "none"
        current = self._get_project(user.id)
        current_name = os.path.basename(current) if current else "none"
        await update.message.reply_text(  # type: ignore[union-attr]
            f"Claude Code Telegram Bot\n\n"
            f"Active: {current_name}\n"
            f"Sessions: {session_list}\n\n"
            f"/help — Commands\n"
            f"/projects — Live tmux sessions\n"
            f"/project <name> — Switch session\n"
            f"/stop — Cancel (Ctrl+C)\n"
            f"/new — New conversation\n"
            f"/refresh — Reload sessions",
        )

    async def cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not self._is_allowed(update.effective_user.id):
            return
        await update.message.reply_text(  # type: ignore[union-attr]
            "Messages → active tmux Claude session\n\n"
            "/projects — Live tmux sessions\n"
            "/project <name> — Switch session\n"
            "/stop — Send Ctrl+C\n"
            "/new — Send /new to Claude\n"
            "/refresh — Reload tmux sessions\n"
            "/status — Running tasks",
        )

    async def cmd_stop(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        project = self._get_project(user.id)
        if not project:
            await update.message.reply_text("No active project.")  # type: ignore[union-attr]
            return
        interrupted = await self.claude.interrupt_session(user.id, project)
        if interrupted:
            await update.message.reply_text("Task cancelled.")  # type: ignore[union-attr]
        else:
            await update.message.reply_text("No running task to cancel.")  # type: ignore[union-attr]

    async def cmd_new(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        project = self._get_project(user.id)
        if not project:
            await update.message.reply_text("No active project.")  # type: ignore[union-attr]
            return

        # End current store session
        if user.id in self._user_store_sessions:
            await self.store.end_session(self._user_store_sessions[user.id])
            del self._user_store_sessions[user.id]

        # In tmux mode, /new sends /new to Claude Code directly
        session = self.claude.get_session(user.id, project)
        if session:
            try:
                from .claude import send_to_tmux
                await send_to_tmux(session.info.pane_id, "/new")
                await update.message.reply_text("Sent /new to Claude session.")  # type: ignore[union-attr]
            except Exception:
                log.warning("Failed to send /new", exc_info=True)
                await update.message.reply_text("Failed to send /new.")  # type: ignore[union-attr]
        else:
            await update.message.reply_text("No tmux session found for this project.")  # type: ignore[union-attr]

    async def cmd_project(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        args = (update.message.text or "").split(maxsplit=1)  # type: ignore[union-attr]
        if len(args) < 2:
            current = self._get_project(user.id)
            # Show tmux session name if possible
            current_name = "none"
            if current:
                for name, info in self.claude.get_all_sessions().items():
                    if info.work_dir == current or name == current:
                        current_name = name
                        break
                else:
                    current_name = os.path.basename(current)
            await update.message.reply_text(f"Current: {current_name}\nUsage: /project <name>")  # type: ignore[union-attr]
            return
        target = args[1].strip()
        # Match by tmux session name or work_dir
        self.claude.refresh()
        sessions = self.claude.get_all_sessions()
        for name, info in sessions.items():
            if target.lower() in (name.lower(), os.path.basename(info.work_dir).lower()):
                self._user_projects[user.id] = info.work_dir or name
                await update.message.reply_text(f"Switched to: {name} ({info.work_dir})")  # type: ignore[union-attr]
                return
        # Partial match in tmux sessions
        for name, info in sessions.items():
            if target.lower() in name.lower():
                self._user_projects[user.id] = info.work_dir or name
                await update.message.reply_text(f"Switched to: {name} ({info.work_dir})")  # type: ignore[union-attr]
                return
        # Fallback: match CT_PROJECT_DIRS (SDK mode)
        for d in self.settings.get_project_dirs():
            if target.lower() in (d.lower(), os.path.basename(d).lower()):
                self._user_projects[user.id] = d
                await update.message.reply_text(f"Switched to: {os.path.basename(d)} (SDK mode)")  # type: ignore[union-attr]
                return
        available = list(sessions.keys()) + [os.path.basename(d) for d in self.settings.get_project_dirs()]
        await update.message.reply_text(f"Not found: {target}\nAvailable: {available}")  # type: ignore[union-attr]

    async def cmd_projects(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        self.claude.refresh()
        tmux_sessions = self.claude.get_all_sessions()
        current = self._get_project(user.id)
        current_base = os.path.basename(current.rstrip("/")) if current else ""
        lines = []
        # Tmux sessions
        if tmux_sessions:
            lines.append("tmux:")
            for name, info in tmux_sessions.items():
                marker = " *" if name == current_base else ""
                lines.append(f"  {name}{marker} — {info.pane_id}")
        # SDK projects (from CT_PROJECT_DIRS, excluding those already in tmux)
        tmux_dirs = {info.work_dir for info in tmux_sessions.values()}
        sdk_dirs = [d for d in self.settings.get_project_dirs() if d not in tmux_dirs]
        if sdk_dirs:
            lines.append("sdk:")
            for d in sdk_dirs:
                name = os.path.basename(d)
                marker = " *" if name == current_base else ""
                lines.append(f"  {name}{marker} — {d}")
        if not lines:
            lines.append("No sessions or projects configured.")
        await update.message.reply_text("\n".join(lines))  # type: ignore[union-attr]

    async def cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        self.claude.refresh()
        sessions = self.claude.get_all_sessions()
        running = self.claude.get_active_projects(user.id)
        current = self._get_project(user.id)

        lines = [f"Sessions: {len(sessions)}"]
        for name, info in sessions.items():
            is_current = current and (name == os.path.basename(current.rstrip("/")))
            is_running = info.project in running
            marker = " [active]" if is_current else ""
            status = " (running)" if is_running else ""
            lines.append(f"  {name}{marker}{status} — {info.pane_id}")
        if not sessions:
            lines.append("  No tmux sessions found")
        await update.message.reply_text("\n".join(lines))  # type: ignore[union-attr]

    async def cmd_refresh(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        self.claude.refresh()
        sessions = self.claude.get_all_sessions()
        names = list(sessions.keys())
        await update.message.reply_text(f"Reloaded: {names or 'no sessions found'}")  # type: ignore[union-attr]

    # --- Message Handler ---

    async def handle_message(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        msg = update.message
        if not user or not msg or not self._is_allowed(user.id):
            return

        project = self._get_project(user.id)
        if not project:
            await msg.reply_text("No project configured. Set CT_PROJECT_DIRS in .env")
            return

        # Build prompt from text + files
        prompt = await self._build_prompt(msg, ctx)
        if not prompt:
            return

        # Get memories for system prompt
        memories = await self.store.get_memories(user.id, project)
        system_prompt = ""
        if memories:
            mem_text = "\n".join(f"- {m}" for m in memories)
            system_prompt = f"Previous session context:\n{mem_text}"

        # Ensure store session
        store_sid = await self._ensure_store_session(user.id, project)

        # Send typing indicator and placeholder message
        await ctx.bot.send_chat_action(chat_id=msg.chat_id, action=ChatAction.TYPING)
        reply = await msg.reply_text("Thinking...")

        # Stream callback — edit the reply message with accumulated text
        accumulated: list[str] = []
        last_edit = 0.0
        edit_lock = asyncio.Lock()

        async def stream_cb(chunk: str, is_final: bool) -> None:
            nonlocal last_edit
            if chunk:
                accumulated.append(chunk)

            now = time.monotonic()
            should_edit = is_final or (now - last_edit >= EDIT_THROTTLE)
            if not should_edit or not accumulated:
                return

            async with edit_lock:
                full_text = "".join(accumulated)
                if not full_text.strip():
                    return
                display = _truncate(full_text)
                try:
                    await reply.edit_text(display)
                    last_edit = time.monotonic()
                except Exception:
                    pass  # Telegram rate limit or message unchanged

        # Execute
        try:
            result = await self.claude.execute_with_retry(
                user_id=user.id,
                project_dir=project,
                prompt=prompt,
                stream_cb=stream_cb,
                system_prompt=system_prompt,
            )

            # Build final display text
            display_text = result.text.strip() if result.text else ""
            if not display_text and not accumulated:
                display_text = "(no text response — tools were used)"

            # Send final message
            if display_text:
                if len(display_text) > TG_MAX_LEN - 100:
                    try:
                        await reply.delete()
                    except Exception:
                        pass
                    for part in _split_message(display_text):
                        await msg.reply_text(part)
                else:
                    try:
                        await reply.edit_text(display_text)
                    except Exception:
                        pass

            # Log usage
            await self.store.update_session(
                store_sid,
                increment_messages=True,
            )

        except Exception as e:
            log.exception("Error processing message")
            try:
                await reply.edit_text(f"Error: {_escape(str(e)[:500])}")
            except Exception:
                pass

    async def _build_prompt(self, msg, ctx: ContextTypes.DEFAULT_TYPE) -> str:
        """Build prompt from message text and any attached files."""
        parts: list[str] = []

        # Text
        if msg.text:
            parts.append(msg.text)
        elif msg.caption:
            parts.append(msg.caption)

        # Document
        if msg.document:
            try:
                file = await ctx.bot.get_file(msg.document.file_id)
                suffix = Path(msg.document.file_name or "file").suffix
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                    await file.download_to_drive(tmp.name)
                    content = Path(tmp.name).read_text(errors="replace")
                    parts.append(f"\n--- File: {msg.document.file_name} ---\n{content}")
                    os.unlink(tmp.name)
            except Exception:
                log.warning("Failed to download document", exc_info=True)

        # Photo — mention that an image was sent (SDK handles vision if model supports it)
        if msg.photo:
            try:
                photo = msg.photo[-1]  # highest resolution
                file = await ctx.bot.get_file(photo.file_id)
                with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                    await file.download_to_drive(tmp.name)
                    parts.append(f"\n[Image attached: {tmp.name}]")
                    # Note: tmp not unlinked — Claude may need to read it
            except Exception:
                log.warning("Failed to download photo", exc_info=True)

        return "\n".join(parts)

    def build_application(self) -> Application:
        """Build and return the Telegram Application."""
        app = (
            Application.builder()
            .token(self.settings.telegram_bot_token)
            .concurrent_updates(True)
            .build()
        )
        # Commands
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("help", self.cmd_help))
        app.add_handler(CommandHandler("stop", self.cmd_stop))
        app.add_handler(CommandHandler("new", self.cmd_new))
        app.add_handler(CommandHandler("project", self.cmd_project))
        app.add_handler(CommandHandler("projects", self.cmd_projects))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("refresh", self.cmd_refresh))
        # Messages (text, documents, photos)
        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND | filters.Document.ALL | filters.PHOTO,
                self.handle_message,
            )
        )
        return app
