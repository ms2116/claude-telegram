"""Configuration via environment variables (CT_ prefix)."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "CT_"}

    # Telegram
    telegram_bot_token: str
    allowed_users: list[int] = []

    # Claude
    project_dirs: list[str] = []
    permission_mode: str = "acceptEdits"
    allowed_tools: list[str] = []
    model: str = ""
    max_turns: int = 0

    # Storage
    db_path: str = ""

    # Logging
    log_level: str = "INFO"

    @field_validator("allowed_users", mode="before")
    @classmethod
    def parse_int_list(cls, v: str | list) -> list[int]:
        if isinstance(v, str):
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        return v

    @field_validator("project_dirs", "allowed_tools", mode="before")
    @classmethod
    def parse_str_list(cls, v: str | list) -> list[str]:
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return v

    def get_db_path(self) -> Path:
        if self.db_path:
            return Path(self.db_path)
        base = Path(os.environ.get("HOME", Path.home())) / ".claude-telegram"
        base.mkdir(parents=True, exist_ok=True)
        return base / "store.db"

    def get_default_project(self) -> str | None:
        return self.project_dirs[0] if self.project_dirs else None
