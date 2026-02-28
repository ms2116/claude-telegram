"""SQLite storage — sessions, memories, cost tracking."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    project_dir TEXT NOT NULL,
    sdk_session_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    message_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    project_dir TEXT NOT NULL,
    summary TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS cost_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    project_dir TEXT NOT NULL,
    session_id INTEGER REFERENCES sessions(id),
    cost_usd REAL,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    duration_ms INTEGER DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, project_dir);
CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id, project_dir);
CREATE INDEX IF NOT EXISTS idx_cost_user ON cost_log(user_id);
"""


class Store:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    # --- Sessions ---

    async def create_session(
        self, user_id: int, project_dir: str, sdk_session_id: str = ""
    ) -> int:
        assert self._db
        cur = await self._db.execute(
            "INSERT INTO sessions (user_id, project_dir, sdk_session_id, started_at) VALUES (?, ?, ?, ?)",
            (user_id, project_dir, sdk_session_id, time.time()),
        )
        await self._db.commit()
        return cur.lastrowid  # type: ignore[return-value]

    async def end_session(self, session_id: int) -> None:
        assert self._db
        await self._db.execute(
            "UPDATE sessions SET ended_at = ? WHERE id = ?",
            (time.time(), session_id),
        )
        await self._db.commit()

    async def update_session(
        self, session_id: int, sdk_session_id: str = "", increment_messages: bool = False
    ) -> None:
        assert self._db
        updates = []
        params: list[Any] = []
        if sdk_session_id:
            updates.append("sdk_session_id = ?")
            params.append(sdk_session_id)
        if increment_messages:
            updates.append("message_count = message_count + 1")
        if updates:
            params.append(session_id)
            await self._db.execute(
                f"UPDATE sessions SET {', '.join(updates)} WHERE id = ?", params
            )
            await self._db.commit()

    async def get_active_session(
        self, user_id: int, project_dir: str
    ) -> dict[str, Any] | None:
        assert self._db
        cur = await self._db.execute(
            "SELECT * FROM sessions WHERE user_id = ? AND project_dir = ? AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
            (user_id, project_dir),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    # --- Memories ---

    async def save_memory(self, user_id: int, project_dir: str, summary: str) -> None:
        assert self._db
        await self._db.execute(
            "INSERT INTO memories (user_id, project_dir, summary, created_at) VALUES (?, ?, ?, ?)",
            (user_id, project_dir, summary, time.time()),
        )
        await self._db.commit()

    async def get_memories(
        self, user_id: int, project_dir: str, limit: int = 5
    ) -> list[str]:
        assert self._db
        cur = await self._db.execute(
            "SELECT summary FROM memories WHERE user_id = ? AND project_dir = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, project_dir, limit),
        )
        rows = await cur.fetchall()
        return [row["summary"] for row in rows]

    # --- Cost ---

    async def log_cost(
        self,
        user_id: int,
        project_dir: str,
        session_id: int | None = None,
        cost_usd: float | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        duration_ms: int = 0,
    ) -> None:
        assert self._db
        await self._db.execute(
            "INSERT INTO cost_log (user_id, project_dir, session_id, cost_usd, input_tokens, output_tokens, duration_ms, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, project_dir, session_id, cost_usd, input_tokens, output_tokens, duration_ms, time.time()),
        )
        await self._db.commit()

    async def get_total_cost(self, user_id: int, days: int = 30) -> float:
        assert self._db
        since = time.time() - days * 86400
        cur = await self._db.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) as total FROM cost_log WHERE user_id = ? AND created_at > ?",
            (user_id, since),
        )
        row = await cur.fetchone()
        return float(row["total"]) if row else 0.0

    async def get_cost_breakdown(self, user_id: int, days: int = 30) -> list[dict[str, Any]]:
        assert self._db
        since = time.time() - days * 86400
        cur = await self._db.execute(
            """SELECT project_dir,
                      COALESCE(SUM(cost_usd), 0) as total_cost,
                      SUM(input_tokens) as total_input,
                      SUM(output_tokens) as total_output,
                      COUNT(*) as queries
               FROM cost_log WHERE user_id = ? AND created_at > ?
               GROUP BY project_dir ORDER BY total_cost DESC""",
            (user_id, since),
        )
        return [dict(row) for row in await cur.fetchall()]
