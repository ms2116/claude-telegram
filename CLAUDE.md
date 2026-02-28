# claude-telegram

Telegram bot that bridges to Claude Code via `claude-agent-sdk`.

## Architecture

5-file flat structure in `src/claude_telegram/`:
- `config.py` — pydantic-settings, env prefix `CT_`
- `claude.py` — `ClaudeSession` (per user+project), `ClaudeManager`
- `store.py` — aiosqlite: sessions, memories, cost_log
- `bot.py` — Telegram handlers, streaming with throttled edits
- `main.py` — entrypoint, graceful shutdown

## Key Design Decisions

- **ClaudeSDKClient** per query (not persistent) — avoids stale connection issues, `resume` param for session continuity
- **Streaming via message edits** — 2s throttle to avoid Telegram rate limits
- **Cross-session memory** — `/new` triggers Claude to summarize, stored in SQLite, injected as system_prompt
- **Concurrent updates** — `concurrent_updates=True` on Telegram app, each project gets its own session
- **Circuit breaker** — `run.sh` with exponential backoff + Telegram alert

## Running

```bash
cp .env.example .env  # fill in values
uv run claude-telegram
# or with circuit breaker:
bash run.sh
```

## Testing

The bot has no unit tests yet. Manual testing:
1. `/start` → welcome message
2. Send text → Claude response streams in
3. `/stop` → cancels running task
4. `/new` → saves memory, resets session
5. `/project <name>` → switches project
6. `/status` → shows cost breakdown
