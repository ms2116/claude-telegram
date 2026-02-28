# claude-telegram

Lightweight Telegram bot for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) via the official `claude-agent-sdk`.

**5 files, ~1000 lines.** No tmux, no ANSI parsing, no subprocess hacks.

## Features

- **Streaming responses** — real-time message updates as Claude thinks
- **Task cancellation** — `/stop` interrupts via SDK (cross-platform)
- **Multi-project** — switch between projects with `/project`
- **Concurrent execution** — different projects run in parallel
- **Session memory** — `/new` summarizes & saves context for next session
- **Cost tracking** — per-project cost breakdown via `/status`
- **File upload** — send documents/images to Claude
- **Crash recovery** — `run.sh` circuit breaker with exponential backoff + Telegram alerts
- **Cross-platform** — Windows, WSL, Linux

## Quick Start

```bash
# Clone
git clone https://github.com/ms2116/claude-telegram.git
cd claude-telegram

# Install (requires uv)
uv sync

# Configure
cp .env.example .env
# Edit .env: set CT_TELEGRAM_BOT_TOKEN, CT_ALLOWED_USERS, CT_PROJECT_DIRS

# Run
uv run claude-telegram
```

## Configuration

All settings via environment variables (prefix `CT_`) or `.env` file:

| Variable | Required | Description |
|----------|----------|-------------|
| `CT_TELEGRAM_BOT_TOKEN` | Yes | Bot token from [@BotFather](https://t.me/BotFather) |
| `CT_ALLOWED_USERS` | No | Comma-separated Telegram user IDs (empty = allow all) |
| `CT_PROJECT_DIRS` | Yes | Comma-separated project directories |
| `CT_PERMISSION_MODE` | No | `acceptEdits` (default), `default`, `bypassPermissions` |
| `CT_ALLOWED_TOOLS` | No | Comma-separated tool names (empty = all) |
| `CT_MODEL` | No | Claude model override |
| `CT_MAX_TURNS` | No | Max turns per query (0 = unlimited) |
| `CT_DB_PATH` | No | SQLite path (default: `~/.claude-telegram/store.db`) |
| `CT_LOG_LEVEL` | No | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message |
| `/help` | Show all commands |
| `/stop` | Cancel running task |
| `/new` | New session (saves memory from current) |
| `/project <name>` | Switch active project |
| `/projects` | List configured projects |
| `/status` | Show active tasks & cost breakdown |

## Production

Use the circuit breaker wrapper:

```bash
# Optional: set alert chat ID for crash notifications
export CT_ALERT_CHAT_ID=your_chat_id

bash run.sh
```

## Architecture

```
src/claude_telegram/
├── config.py    # pydantic-settings, env prefix CT_
├── claude.py    # ClaudeSession + ClaudeManager (SDK wrapper)
├── bot.py       # Telegram handlers, streaming edits
├── store.py     # SQLite: sessions, memories, cost
└── main.py      # Entrypoint, graceful shutdown
```

## License

MIT
