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

from telegram import LinkPreviewOptions, Update
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
# Disable link previews globally
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


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

    # --- Command Handlers ---

    async def _reply_html(self, update: Update, text: str) -> None:
        """Send HTML-formatted reply."""
        await update.message.reply_text(  # type: ignore[union-attr]
            text, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW,
        )

    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        sessions = self.claude.get_all_sessions()
        current = self._get_project(user.id)
        current_name = _escape(os.path.basename(current)) if current else "—"
        session_names = [_escape(n) for n in sessions.keys()]
        session_str = ", ".join(session_names) if session_names else "없음"
        await self._reply_html(update,
            f"<b>Claude Code Telegram</b>\n\n"
            f"  📂  현재 프로젝트  <b>{current_name}</b>\n"
            f"  📡  활성 세션  <code>{session_str}</code>\n\n"
            f"  /help 로 명령어 확인",
        )

    async def cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not self._is_allowed(update.effective_user.id):
            return
        await self._reply_html(update,
            "<b>명령어</b>\n\n"
            "<b>프로젝트</b>\n"
            "  /projects  전체 목록\n"
            "  /1 /2 …  번호로 전환\n"
            "  /project &lt;이름&gt;  이름으로 전환\n\n"
            "<b>대화</b>\n"
            "  /new  새 대화 시작\n"
            "  /stop  작업 중단\n"
            "  /esc  Escape 전송\n"
            "  /yes  권한 승인\n\n"
            "<b>상태</b>\n"
            "  /status  세션 상태 확인\n\n"
            "<i>메시지를 보내면 현재 프로젝트의 Claude에 전달됩니다</i>",
        )

    async def cmd_stop(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Ctrl+C 전송 — tmux send-keys C-c."""
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        project = self._get_project(user.id)
        if not project:
            await self._reply_html(update, "⚠️ 활성 프로젝트가 없습니다")
            return
        session = self.claude.get_session(user.id, project)
        if session:
            try:
                import subprocess
                subprocess.run(["tmux", "send-keys", "-t", session.info.pane_id, "C-c"], timeout=5)
                await self._reply_html(update, "⏹ <b>작업 중단</b>")
                return
            except Exception:
                pass
        interrupted = await self.claude.interrupt_session(user.id, project)
        if interrupted:
            await self._reply_html(update, "⏹ <b>작업 중단</b>")
        else:
            await self._reply_html(update, "⚠️ 실행 중인 작업이 없습니다")

    async def cmd_esc(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Escape 키 전송 (tmux 전용)."""
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        project = self._get_project(user.id)
        if not project:
            await self._reply_html(update, "⚠️ 활성 프로젝트가 없습니다")
            return
        session = self.claude.get_session(user.id, project)
        if session:
            import subprocess
            subprocess.run(["tmux", "send-keys", "-t", session.info.pane_id, "Escape"], timeout=5)
            await self._reply_html(update, "⎋ <b>Escape 전송</b>")
        else:
            await self._reply_html(update, "⚠️ tmux 세션이 없습니다")

    async def cmd_yes(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """권한 승인 — y + Enter 전송 (tmux 전용)."""
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        project = self._get_project(user.id)
        if not project:
            await self._reply_html(update, "⚠️ 활성 프로젝트가 없습니다")
            return
        session = self.claude.get_session(user.id, project)
        if session:
            import subprocess
            subprocess.run(["tmux", "send-keys", "-t", session.info.pane_id, "y"], timeout=5)
            await asyncio.sleep(0.1)
            subprocess.run(["tmux", "send-keys", "-t", session.info.pane_id, "Enter"], timeout=5)
            await self._reply_html(update, "✅ <b>승인 전송</b>")
        else:
            await self._reply_html(update, "⚠️ tmux 세션이 없습니다")

    async def cmd_new(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        project = self._get_project(user.id)
        if not project:
            await self._reply_html(update, "⚠️ 활성 프로젝트가 없습니다")
            return

        session = self.claude.get_session(user.id, project)
        if session:
            try:
                from .claude import send_to_tmux
                await send_to_tmux(session.info.pane_id, "/new")
                await self._reply_html(update, "🔄 <b>새 대화 시작</b>")
            except Exception:
                log.warning("Failed to send /new", exc_info=True)
                await self._reply_html(update, "❌ /new 전송 실패")
        else:
            await self._reply_html(update, "⚠️ tmux 세션이 없습니다")

    async def cmd_project(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        args = (update.message.text or "").split(maxsplit=1)  # type: ignore[union-attr]
        if len(args) < 2:
            current = self._get_project(user.id)
            current_name = "—"
            if current:
                for name, info in self.claude.get_all_sessions().items():
                    if info.work_dir == current or name == current:
                        current_name = name
                        break
                else:
                    current_name = os.path.basename(current)
            await self._reply_html(update,
                f"📂 현재  <b>{_escape(current_name)}</b>\n\n"
                f"<i>/project &lt;이름&gt; 으로 전환</i>")
            return
        target = args[1].strip()
        self.claude.refresh()
        sessions = self.claude.get_all_sessions()
        for name, info in sessions.items():
            if target.lower() in (name.lower(), os.path.basename(info.work_dir).lower()):
                self._user_projects[user.id] = info.work_dir or name
                await self._reply_html(update, f"📂 <b>{_escape(name)}</b> 으로 전환")
                return
        for name, info in sessions.items():
            if target.lower() in name.lower():
                self._user_projects[user.id] = info.work_dir or name
                await self._reply_html(update, f"📂 <b>{_escape(name)}</b> 으로 전환")
                return
        names = [_escape(n) for n in sessions.keys()]
        await self._reply_html(update,
            f"⚠️ <code>{_escape(target)}</code> 을(를) 찾을 수 없습니다\n\n"
            f"활성 세션: {', '.join(names) or '없음'}")

    def _build_project_list(self) -> list[tuple[int, str, str, bool]]:
        """Build numbered project list: (num, name, work_dir, is_tmux).

        Active tmux sessions first, then inactive env projects.
        """
        result: list[tuple[int, str, str, bool]] = []
        tmux_sessions = self.claude.get_all_sessions()
        tmux_dirs: set[str] = set()
        num = 1
        for name, info in tmux_sessions.items():
            if name.startswith("sdk:"):
                continue
            result.append((num, name, info.work_dir, True))
            tmux_dirs.add(info.work_dir)
            num += 1
        # Inactive projects from env (no tmux session)
        for d in self.settings.get_project_dirs():
            if d not in tmux_dirs:
                result.append((num, os.path.basename(d), d, False))
                num += 1
        return result

    def _switch_project(self, user_id: int, name: str, work_dir: str, is_tmux: bool) -> str:
        """Switch user's active project. Returns HTML message.

        Blocks switching to inactive (no tmux session) projects.
        """
        if not is_tmux:
            return (f"⚠️ <b>{_escape(name)}</b> — 비활성 세션\n\n"
                    f"<i>tmux에서 Claude Code를 먼저 실행하세요</i>")
        self._user_projects[user_id] = work_dir or name
        return f"📂 <b>{_escape(name)}</b> 으로 전환"

    async def cmd_projects(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        self.claude.refresh()
        current = self._get_project(user.id)
        current_base = os.path.basename(current.rstrip("/")) if current else ""
        projects = self._build_project_list()
        if not projects:
            await self._reply_html(update, "⚠️ 등록된 프로젝트가 없습니다")
            return
        lines = ["<b>프로젝트 목록</b>\n"]
        for num, name, work_dir, is_tmux in projects:
            dot = "●" if is_tmux else "◦"
            cur = "  ◀" if name == current_base else ""
            lines.append(f"  /{num}  {dot}  {_escape(name)}{cur}")
        lines.append(f"\n<i>● 활성  ◦ 비활성  ◀ 현재</i>")
        await self._reply_html(update, "\n".join(lines))

    async def cmd_switch_by_number(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /1, /2, ... commands to switch project by number."""
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        text = (update.message.text or "").strip()  # type: ignore[union-attr]
        try:
            num = int(text.lstrip("/"))
        except ValueError:
            return
        self.claude.refresh()
        projects = self._build_project_list()
        for pnum, name, work_dir, is_tmux in projects:
            if pnum == num:
                msg = self._switch_project(user.id, name, work_dir, is_tmux)
                await self._reply_html(update, msg)
                return
        await self._reply_html(update, f"⚠️ /{num} — 없는 번호입니다\n/projects 로 확인하세요")

    async def cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        self.claude.refresh()
        sessions = self.claude.get_all_sessions()
        running = self.claude.get_active_projects(user.id)
        current = self._get_project(user.id)

        lines = [f"<b>세션 상태</b>  —  {len(sessions)}개\n"]
        for name, info in sessions.items():
            is_current = current and (name == os.path.basename(current.rstrip("/")))
            is_running = info.project in running
            cur = "  ◀" if is_current else ""
            dot = "▶" if is_running else "●"
            lines.append(f"  {dot}  <b>{_escape(name)}</b>  "
                         f"<code>{_escape(info.pane_id)}</code>{cur}")
        if not sessions:
            lines.append("  <i>활성 세션 없음</i>")
        lines.append(f"\n<i>● 대기  ▶ 실행중  ◀ 현재</i>")
        await self._reply_html(update, "\n".join(lines))

    # --- Message Handler ---

    async def handle_message(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        msg = update.message
        if not user or not msg or not self._is_allowed(user.id):
            return

        project = self._get_project(user.id)
        if not project:
            await msg.reply_text(
                "⚠️ 프로젝트 미설정\n\n<i>.env에 CT_PROJECT_DIRS를 설정하세요</i>",
                parse_mode=ParseMode.HTML)
            return

        log.info("Message from %s → project %s", user.id, project)

        # Build prompt from text + files
        prompt = await self._build_prompt(msg, ctx)
        if not prompt:
            return

        # Send typing indicator and placeholder message
        await ctx.bot.send_chat_action(chat_id=msg.chat_id, action=ChatAction.TYPING)
        reply = await msg.reply_text("⏳")

        # Stream callback — receives full text each time, replaces display
        current_text = [""]  # mutable holder for latest full text
        last_edit = 0.0
        edit_lock = asyncio.Lock()

        async def stream_cb(full_text: str, is_final: bool) -> None:
            nonlocal last_edit
            if full_text:
                current_text[0] = full_text

            now = time.monotonic()
            should_edit = is_final or (now - last_edit >= EDIT_THROTTLE)
            if not should_edit or not current_text[0]:
                return

            async with edit_lock:
                if not current_text[0].strip():
                    return
                display = _truncate(current_text[0])
                try:
                    await reply.edit_text(display, link_preview_options=NO_PREVIEW)
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
            )

            # Build final display text
            # Prefer streamed content (includes intermediate tool steps)
            # Fall back to result.text (final extract_response)
            streamed = current_text[0].strip()
            result_text = result.text.strip() if result.text else ""
            display_text = streamed if len(streamed) >= len(result_text) else result_text
            if not display_text:
                display_text = "⚙️ 도구 실행 완료 (텍스트 응답 없음)"

            # Send final message (edit = silent)
            if display_text:
                if len(display_text) > TG_MAX_LEN - 100:
                    try:
                        await reply.delete()
                    except Exception:
                        pass
                    parts = _split_message(display_text)
                    for part in parts:
                        await msg.reply_text(part, link_preview_options=NO_PREVIEW,
                                             disable_notification=True)
                else:
                    try:
                        await reply.edit_text(display_text, link_preview_options=NO_PREVIEW)
                    except Exception:
                        pass

            # Completion notification (new message = triggers sound)
            await msg.reply_text("✅ 완료")

        except Exception as e:
            log.exception("Error processing message")
            try:
                await reply.edit_text(
                    f"❌ <b>오류</b>\n\n<code>{_escape(str(e)[:500])}</code>",
                    parse_mode=ParseMode.HTML)
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
        app.add_handler(CommandHandler("esc", self.cmd_esc))
        app.add_handler(CommandHandler("yes", self.cmd_yes))
        app.add_handler(CommandHandler("new", self.cmd_new))
        app.add_handler(CommandHandler("project", self.cmd_project))
        app.add_handler(CommandHandler("projects", self.cmd_projects))
        app.add_handler(CommandHandler("status", self.cmd_status))
        # Number shortcuts: /1, /2, ... /20 for quick project switch
        for n in range(1, 21):
            app.add_handler(CommandHandler(str(n), self.cmd_switch_by_number))
        # Messages (text, documents, photos)
        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND | filters.Document.ALL | filters.PHOTO,
                self.handle_message,
            )
        )
        return app
