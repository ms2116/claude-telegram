"""Entrypoint — initialize components and run the bot."""

from __future__ import annotations

import asyncio
import logging
import sys

from .bot import Bot
from .claude import ClaudeManager
from .config import Settings
from .store import Store

log = logging.getLogger("claude_telegram")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)


async def _init_store(settings: Settings) -> Store:
    store = Store(settings.get_db_path())
    await store.init()
    return store


def main() -> None:
    """Entry point for `uv run claude-telegram`."""
    # Load settings
    try:
        settings = Settings()  # type: ignore[call-arg]
    except Exception as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        print("Create a .env file or set CT_* environment variables.", file=sys.stderr)
        sys.exit(1)

    _setup_logging(settings.log_level)
    log.info("Starting claude-telegram bot")
    log.info("Projects: %s", settings.get_project_dirs())
    log.info("Allowed users: %s", settings.get_allowed_users() or "all")
    log.info("Permission mode: %s", settings.permission_mode)

    # Initialize store (need a quick event loop for async init)
    store = asyncio.run(_init_store(settings))
    log.info("Database: %s", settings.get_db_path())

    # Initialize Claude manager — load tmux sessions
    claude = ClaudeManager(settings)
    claude.load_sessions()
    sessions = claude.get_all_sessions()
    log.info("Tmux sessions: %s", list(sessions.keys()) or "none")

    # Build and run bot
    # run_polling handles its own event loop, signals, and graceful shutdown
    bot = Bot(settings, claude, store)
    app = bot.build_application()
    log.info("Starting polling...")
    app.run_polling(drop_pending_updates=True)
    log.info("Bot stopped.")


if __name__ == "__main__":
    main()
