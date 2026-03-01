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
            f"Claude Code 텔레그램 봇\n\n"
            f"현재 프로젝트: {current_name}\n"
            f"tmux 세션: {session_list}\n\n"
            f"/help 로 명령어 확인",
        )

    async def cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not self._is_allowed(update.effective_user.id):
            return
        await update.message.reply_text(  # type: ignore[union-attr]
            "메시지를 보내면 현재 프로젝트의 Claude에 전달됩니다\n\n"
            "/project <이름> — 프로젝트 전환\n"
            "/projects — 전체 프로젝트 목록\n"
            "/session [번호] — 이전 세션 선택\n"
            "/new — 새 대화 시작\n"
            "/stop — Ctrl+C (작업 중단)\n"
            "/esc — Escape 전송\n"
            "/yes — 권한 승인 (y + Enter)\n"
            "/status — 현재 상태 확인\n"
            "/refresh — tmux 세션 새로고침",
        )

    async def cmd_stop(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Ctrl+C 전송 — tmux: send-keys C-c, SDK: interrupt."""
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        project = self._get_project(user.id)
        if not project:
            await update.message.reply_text("활성 프로젝트가 없습니다.")  # type: ignore[union-attr]
            return
        # tmux: Ctrl+C 직접 전송
        session = self.claude.get_session(user.id, project)
        if session:
            try:
                import subprocess
                subprocess.run(["tmux", "send-keys", "-t", session.info.pane_id, "C-c"], timeout=5)
                await update.message.reply_text("Ctrl+C 전송됨 (작업 중단)")  # type: ignore[union-attr]
                return
            except Exception:
                pass
        # SDK: interrupt
        interrupted = await self.claude.interrupt_session(user.id, project)
        if interrupted:
            await update.message.reply_text("작업을 중단했습니다.")  # type: ignore[union-attr]
        else:
            await update.message.reply_text("실행 중인 작업이 없습니다.")  # type: ignore[union-attr]

    async def cmd_esc(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Escape 키 전송 (tmux 전용)."""
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        project = self._get_project(user.id)
        if not project:
            await update.message.reply_text("활성 프로젝트가 없습니다.")  # type: ignore[union-attr]
            return
        session = self.claude.get_session(user.id, project)
        if session:
            import subprocess
            subprocess.run(["tmux", "send-keys", "-t", session.info.pane_id, "Escape"], timeout=5)
            await update.message.reply_text("Escape 전송됨")  # type: ignore[union-attr]
        else:
            await update.message.reply_text("tmux 세션이 아닙니다.")  # type: ignore[union-attr]

    async def cmd_yes(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """권한 승인 — y + Enter 전송 (tmux 전용)."""
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        project = self._get_project(user.id)
        if not project:
            await update.message.reply_text("활성 프로젝트가 없습니다.")  # type: ignore[union-attr]
            return
        session = self.claude.get_session(user.id, project)
        if session:
            import subprocess
            subprocess.run(["tmux", "send-keys", "-t", session.info.pane_id, "y"], timeout=5)
            await asyncio.sleep(0.1)
            subprocess.run(["tmux", "send-keys", "-t", session.info.pane_id, "Enter"], timeout=5)
            await update.message.reply_text("승인(y) 전송됨")  # type: ignore[union-attr]
        else:
            await update.message.reply_text("tmux 세션이 아닙니다.")  # type: ignore[union-attr]

    async def cmd_new(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        project = self._get_project(user.id)
        if not project:
            await update.message.reply_text("활성 프로젝트가 없습니다.")  # type: ignore[union-attr]
            return

        # End current store session
        if user.id in self._user_store_sessions:
            await self.store.end_session(self._user_store_sessions[user.id])
            del self._user_store_sessions[user.id]

        # Tmux mode: send /new to Claude Code directly
        session = self.claude.get_session(user.id, project)
        if session:
            try:
                from .claude import send_to_tmux
                await send_to_tmux(session.info.pane_id, "/new")
                await update.message.reply_text("새 대화를 시작했습니다.")  # type: ignore[union-attr]
            except Exception:
                log.warning("Failed to send /new", exc_info=True)
                await update.message.reply_text("/new 전송 실패.")  # type: ignore[union-attr]
        else:
            # SDK mode: clear the SDK session so next message starts fresh
            self.claude.clear_sdk_session(project)
            await update.message.reply_text("세션 초기화 완료. 다음 메시지부터 새 대화입니다.")  # type: ignore[union-attr]

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
            await update.message.reply_text(f"현재: {current_name}\n사용법: /project <이름>")  # type: ignore[union-attr]
            return
        target = args[1].strip()
        # Match by tmux session name or work_dir
        self.claude.refresh()
        sessions = self.claude.get_all_sessions()
        for name, info in sessions.items():
            if target.lower() in (name.lower(), os.path.basename(info.work_dir).lower()):
                self._user_projects[user.id] = info.work_dir or name
                await update.message.reply_text(f"{name} (tmux)로 전환했습니다")  # type: ignore[union-attr]
                return
        # Partial match in tmux sessions
        for name, info in sessions.items():
            if target.lower() in name.lower():
                self._user_projects[user.id] = info.work_dir or name
                await update.message.reply_text(f"{name} (tmux)로 전환했습니다")  # type: ignore[union-attr]
                return
        # Fallback: match CT_PROJECT_DIRS (SDK mode)
        for d in self.settings.get_project_dirs():
            if target.lower() in (d.lower(), os.path.basename(d).lower()):
                self._user_projects[user.id] = d
                # Show available sessions for selection
                from .claude import SDKSession
                found = SDKSession.find_sessions(d, limit=5)
                lines = [f"{os.path.basename(d)} (SDK)로 전환했습니다"]
                if found:
                    lines.append("\n최근 세션:")
                    for i, s in enumerate(found):
                        import time as _time
                        ts = _time.strftime("%m/%d %H:%M", _time.localtime(s["mtime"]))
                        marker = " *" if i == 0 else ""
                        lines.append(f"  {i+1}. {s['id'][:8]}... ({ts}, {s['source']}){marker}")
                    lines.append(f"\n1번 세션으로 자동 연결. /session <번호>로 변경 가능")
                else:
                    lines.append("이전 세션 없음. 새 세션으로 시작합니다.")
                await update.message.reply_text("\n".join(lines))  # type: ignore[union-attr]
                return
        available = list(sessions.keys()) + [os.path.basename(d) for d in self.settings.get_project_dirs()]
        await update.message.reply_text(f"'{target}' 을(를) 찾을 수 없습니다\n목록: {available}")  # type: ignore[union-attr]

    def _build_project_list(self) -> list[tuple[int, str, str, bool]]:
        """Build numbered project list: (num, name, source, is_tmux).

        Active tmux sessions first, then SDK-only projects.
        Numbers are stable per session (rebuilt on each call).
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
        # SDK-only projects from env
        for d in self.settings.get_project_dirs():
            if d not in tmux_dirs:
                result.append((num, os.path.basename(d), d, False))
                num += 1
        return result

    def _switch_project(self, user_id: int, name: str, work_dir: str, is_tmux: bool) -> str:
        """Switch user's active project. Returns confirmation message."""
        self._user_projects[user_id] = work_dir or name
        mode = "tmux" if is_tmux else "sdk"
        return f"{name} ({mode})로 전환했습니다"

    async def cmd_projects(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        self.claude.refresh()
        current = self._get_project(user.id)
        current_base = os.path.basename(current.rstrip("/")) if current else ""
        projects = self._build_project_list()
        if not projects:
            await update.message.reply_text("세션/프로젝트가 없습니다.")  # type: ignore[union-attr]
            return
        lines = []
        for num, name, work_dir, is_tmux in projects:
            cur = " *" if name == current_base else ""
            dot = "●" if is_tmux else "○"
            lines.append(f"/{num} {dot} {name}{cur}")
        lines.append(f"\n● 활성  ○ 비활성\n번호로 전환: /1, /2, ...")
        await update.message.reply_text("\n".join(lines))  # type: ignore[union-attr]

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
                await update.message.reply_text(msg)  # type: ignore[union-attr]
                return
        await update.message.reply_text(f"/{num} — 없는 번호입니다. /projects로 확인하세요.")  # type: ignore[union-attr]

    async def cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        self.claude.refresh()
        sessions = self.claude.get_all_sessions()
        running = self.claude.get_active_projects(user.id)
        current = self._get_project(user.id)

        lines = [f"세션: {len(sessions)}개"]
        for name, info in sessions.items():
            is_current = current and (name == os.path.basename(current.rstrip("/")))
            is_running = info.project in running
            marker = " [현재]" if is_current else ""
            status = " (실행중)" if is_running else ""
            lines.append(f"  {name}{marker}{status} — {info.pane_id}")
        if not sessions:
            lines.append("  tmux 세션 없음")
        await update.message.reply_text("\n".join(lines))  # type: ignore[union-attr]

    async def cmd_session(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Select a specific session for the current SDK project."""
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        project = self._get_project(user.id)
        if not project:
            await update.message.reply_text("활성 프로젝트가 없습니다.")  # type: ignore[union-attr]
            return

        from .claude import SDKSession
        found = SDKSession.find_sessions(project, limit=5)
        if not found:
            await update.message.reply_text("이 프로젝트의 세션이 없습니다.")  # type: ignore[union-attr]
            return

        args = (update.message.text or "").split(maxsplit=1)  # type: ignore[union-attr]
        if len(args) < 2:
            # Show session list
            import time as _time
            lines = [f"{os.path.basename(project)} 세션 목록:"]
            for i, s in enumerate(found):
                ts = _time.strftime("%m/%d %H:%M", _time.localtime(s["mtime"]))
                lines.append(f"  {i+1}. {s['id'][:8]}... ({ts}, {s['source']})")
            lines.append(f"\n사용법: /session <번호>")
            await update.message.reply_text("\n".join(lines))  # type: ignore[union-attr]
            return

        try:
            idx = int(args[1].strip()) - 1
            if 0 <= idx < len(found):
                chosen = found[idx]
                # Clear existing SDK session and set the chosen one
                self.claude.clear_sdk_session(project)
                sdk = self.claude.get_or_create_sdk_session(project)
                sdk._sdk_session_id = chosen["id"]
                await update.message.reply_text(  # type: ignore[union-attr]
                    f"세션 설정: {chosen['id'][:8]}... ({chosen['source']})"
                )
            else:
                await update.message.reply_text(f"1~{len(found)} 사이 번호를 입력하세요")  # type: ignore[union-attr]
        except ValueError:
            await update.message.reply_text("사용법: /session <번호>")  # type: ignore[union-attr]

    async def cmd_refresh(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        self.claude.refresh()
        sessions = self.claude.get_all_sessions()
        names = list(sessions.keys())
        await update.message.reply_text(f"새로고침 완료: {names or '세션 없음'}")  # type: ignore[union-attr]

    # --- Message Handler ---

    async def handle_message(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        msg = update.message
        if not user or not msg or not self._is_allowed(user.id):
            return

        project = self._get_project(user.id)
        if not project:
            await msg.reply_text("프로젝트 미설정. .env에 CT_PROJECT_DIRS를 설정하세요.")
            return

        log.info("Message from %s → project %s", user.id, project)

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
        reply = await msg.reply_text("처리중...")

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
                system_prompt=system_prompt,
            )

            # Build final display text
            # Prefer streamed content (includes intermediate tool steps)
            # Fall back to result.text (final extract_response)
            streamed = current_text[0].strip()
            result_text = result.text.strip() if result.text else ""
            display_text = streamed if len(streamed) >= len(result_text) else result_text
            if not display_text:
                display_text = "(텍스트 응답 없음 — 도구 실행됨)"

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
            await msg.reply_text("완료")

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
        app.add_handler(CommandHandler("esc", self.cmd_esc))
        app.add_handler(CommandHandler("yes", self.cmd_yes))
        app.add_handler(CommandHandler("new", self.cmd_new))
        app.add_handler(CommandHandler("project", self.cmd_project))
        app.add_handler(CommandHandler("projects", self.cmd_projects))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("session", self.cmd_session))
        app.add_handler(CommandHandler("refresh", self.cmd_refresh))
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
