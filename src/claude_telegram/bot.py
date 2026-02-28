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
        return not self.settings.allowed_users or user_id in self.settings.allowed_users

    def _get_project(self, user_id: int) -> str | None:
        if user_id in self._user_projects:
            return self._user_projects[user_id]
        default = self.settings.get_default_project()
        if default:
            self._user_projects[user_id] = default
        return default

    async def _ensure_store_session(self, user_id: int, project_dir: str) -> int:
        if user_id in self._user_store_sessions:
            return self._user_store_sessions[user_id]
        # Check for existing active session
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
        project = self._get_project(user.id)
        proj_info = f"\nActive project: `{project}`" if project else "\nNo project configured. Set CT_PROJECT_DIRS."
        await update.message.reply_text(  # type: ignore[union-attr]
            f"Claude Code Telegram Bot\n"
            f"Send any message to interact with Claude.{proj_info}\n\n"
            f"Commands:\n"
            f"/help — Show commands\n"
            f"/stop — Cancel running task\n"
            f"/new — New session (saves memory)\n"
            f"/project <path> — Switch project\n"
            f"/projects — List configured projects\n"
            f"/status — Show active sessions & cost",
        )

    async def cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not self._is_allowed(update.effective_user.id):
            return
        await update.message.reply_text(  # type: ignore[union-attr]
            "/start — Welcome message\n"
            "/help — This help\n"
            "/stop — Cancel current Claude task\n"
            "/new — Start new session (saves memory from current)\n"
            "/project <name|path> — Switch active project\n"
            "/projects — List configured projects\n"
            "/status — Active sessions, cost summary\n\n"
            "Send any text to chat with Claude.\n"
            "Send files/images to include them in your message.",
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

        # Save a summary memory via Claude
        session = self.claude.get_session(user.id, project)
        if session._sdk_session_id:
            try:
                result = await session.execute(
                    "Summarize our conversation in 2-3 sentences for future context. "
                    "Focus on what was accomplished and any important decisions."
                )
                if result.text:
                    await self.store.save_memory(user.id, project, result.text)
            except Exception:
                log.warning("Failed to generate session summary", exc_info=True)

        # Reset the Claude session
        self.claude.reset_session(user.id, project)
        await update.message.reply_text(  # type: ignore[union-attr]
            "New session started. Previous context saved to memory."
        )

    async def cmd_project(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        args = (update.message.text or "").split(maxsplit=1)  # type: ignore[union-attr]
        if len(args) < 2:
            current = self._get_project(user.id) or "none"
            await update.message.reply_text(f"Current project: `{current}`\nUsage: /project <name|path>")  # type: ignore[union-attr]
            return
        target = args[1].strip()
        # Match by name (basename) or full path
        for d in self.settings.project_dirs:
            if target == d or target == os.path.basename(d):
                self._user_projects[user.id] = d
                await update.message.reply_text(f"Switched to: `{d}`")  # type: ignore[union-attr]
                return
        # Accept arbitrary path if it exists
        if os.path.isdir(target):
            self._user_projects[user.id] = target
            await update.message.reply_text(f"Switched to: `{target}`")  # type: ignore[union-attr]
            return
        await update.message.reply_text(f"Project not found: {target}")  # type: ignore[union-attr]

    async def cmd_projects(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        if not self.settings.project_dirs:
            await update.message.reply_text("No projects configured. Set CT_PROJECT_DIRS.")  # type: ignore[union-attr]
            return
        current = self._get_project(user.id)
        lines = []
        for d in self.settings.project_dirs:
            marker = " (active)" if d == current else ""
            lines.append(f"  {os.path.basename(d)} — {d}{marker}")
        await update.message.reply_text("Projects:\n" + "\n".join(lines))  # type: ignore[union-attr]

    async def cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        active = self.claude.get_active_projects(user.id)
        active_str = ", ".join(os.path.basename(p) for p in active) if active else "none"
        total_cost = await self.store.get_total_cost(user.id)
        breakdown = await self.store.get_cost_breakdown(user.id)

        lines = [
            f"Active tasks: {active_str}",
            f"Cost (30d): ${total_cost:.4f}",
        ]
        if breakdown:
            lines.append("\nPer project:")
            for row in breakdown:
                name = os.path.basename(row["project_dir"])
                lines.append(f"  {name}: ${row['total_cost']:.4f} ({row['queries']} queries)")
        await update.message.reply_text("\n".join(lines))  # type: ignore[union-attr]

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

            # Final message — send full text, possibly split
            if result.text:
                full_text = result.text.strip()
                # If text was already shown via streaming and is short enough, it's already there
                if len(full_text) > TG_MAX_LEN - 100:
                    # Delete streaming message and send split messages
                    try:
                        await reply.delete()
                    except Exception:
                        pass
                    for part in _split_message(full_text):
                        await msg.reply_text(part)
                else:
                    # Final edit to ensure complete text
                    try:
                        await reply.edit_text(full_text)
                    except Exception:
                        pass
            elif not accumulated:
                await reply.edit_text("(no text response — tools were used)")

            # Append cost info
            cost_line = ""
            if result.cost_usd is not None:
                cost_line = f"\n\n💲 ${result.cost_usd:.4f}"
                if result.tools_used:
                    unique_tools = sorted(set(result.tools_used))
                    cost_line += f" | Tools: {', '.join(unique_tools)}"

            if cost_line:
                try:
                    current = reply.text or "".join(accumulated)
                    await reply.edit_text(_truncate(current + cost_line))
                except Exception:
                    pass

            # Log cost
            await self.store.log_cost(
                user_id=user.id,
                project_dir=project,
                session_id=store_sid,
                cost_usd=result.cost_usd,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                duration_ms=result.duration_ms,
            )
            await self.store.update_session(
                store_sid,
                sdk_session_id=result.session_id,
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
        # Messages (text, documents, photos)
        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND | filters.Document.ALL | filters.PHOTO,
                self.handle_message,
            )
        )
        return app
