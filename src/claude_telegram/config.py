"""Configuration via environment variables (CT_ prefix)."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "CT_", "env_file": ".env", "env_file_encoding": "utf-8"}

    # Telegram
    telegram_bot_token: str
    allowed_users: str = ""  # comma-separated user IDs

    # Claude
    project_dirs: str = ""  # comma-separated paths
    permission_mode: str = "acceptEdits"
    allowed_tools: str = ""  # comma-separated tool names
    model: str = ""
    max_turns: int = 0

    # Storage
    db_path: str = ""

    # Logging
    log_level: str = "INFO"

    def get_allowed_users(self) -> list[int]:
        if not self.allowed_users:
            return []
        return [int(x.strip()) for x in self.allowed_users.split(",") if x.strip()]

    def get_project_dirs(self) -> list[str]:
        if not self.project_dirs:
            return []
        return [x.strip() for x in self.project_dirs.split(",") if x.strip()]

    def get_allowed_tools(self) -> list[str]:
        if not self.allowed_tools:
            return []
        return [x.strip() for x in self.allowed_tools.split(",") if x.strip()]

    def get_db_path(self) -> Path:
        if self.db_path:
            return Path(self.db_path)
        base = Path(os.environ.get("HOME", str(Path.home()))) / ".claude-telegram"
        base.mkdir(parents=True, exist_ok=True)
        return base / "store.db"

    def get_default_project(self) -> str | None:
        dirs = self.get_project_dirs()
        return dirs[0] if dirs else None
