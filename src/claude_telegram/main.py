"""Entrypoint — initialize components and run the bot."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

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


async def _run() -> None:
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

    # Initialize store
    store = Store(settings.get_db_path())
    await store.init()
    log.info("Database: %s", settings.get_db_path())

    # Initialize Claude manager
    claude = ClaudeManager(settings)

    # Build bot
    bot = Bot(settings, claude, store)
    app = bot.build_application()

    # Run with graceful shutdown
    try:
        await app.initialize()
        await app.start()
        log.info("Bot started. Polling for updates...")
        await app.updater.start_polling(drop_pending_updates=True)

        # Wait until stopped
        stop_event = asyncio.Event()

        def _signal_handler() -> None:
            log.info("Shutdown signal received")
            stop_event.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _signal_handler)
            except NotImplementedError:
                # Windows doesn't support add_signal_handler
                pass

        # On Windows, use keyboard interrupt
        try:
            await stop_event.wait()
        except KeyboardInterrupt:
            log.info("Keyboard interrupt")

    finally:
        log.info("Shutting down...")
        if app.updater and app.updater.running:
            await app.updater.stop()
        if app.running:
            await app.stop()
        await app.shutdown()
        await store.close()
        log.info("Goodbye.")


def main() -> None:
    """Entry point for `uv run claude-telegram`."""
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
