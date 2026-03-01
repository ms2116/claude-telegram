# claude-telegram

> Control Claude Code sessions from Telegram via tmux.

Send a message on Telegram, watch Claude think and execute tools in real-time, get notified when it's done. All without leaving your phone.

## How it works

```
Telegram  ──msg──>  Bot  ──send-keys──>  tmux pane (Claude Code)
                     |                         |
                    edit  <──capture-pane──────┘
                   (2s throttle, full text replace)
```

The bot attaches to tmux panes running Claude Code, pipes your messages in via `send-keys`, and streams back the output by polling `capture-pane` every second. No SDK, no API wrapper — just tmux.

## Features

- **Real-time streaming** — see tool calls (`Bash(...)`, `Read(...)`) and results as they happen
- **Auto session detection** — Claude Code hooks register/unregister sessions automatically
- **Multi-project** — `/projects` shows numbered list, `/1` `/2` to switch instantly
- **Completion alerts** — silent edits during work, sound notification on finish
- **Circuit breaker** — `run.sh` watchdog with crash detection (5 crashes / 60s)

## Commands

| Command | Action |
|---------|--------|
| `/projects` | List all projects (● active, ○ inactive) |
| `/1`, `/2`, ... | Switch project by number |
| `/project <name>` | Switch by name |
| `/new` | Start fresh conversation |
| `/stop` | Send Ctrl+C |
| `/esc` | Send Escape |
| `/yes` | Approve permission (y + Enter) |
| `/status` | Show sessions and state |

## Quick start

```bash
git clone https://github.com/ms2116/claude-telegram.git
cd claude-telegram
uv sync
cp .env.example .env   # edit: token, user ID, project dirs
uv run claude-telegram
```

## Configuration

Set in `.env` (prefix `CT_`):

| Variable | Required | Description |
|----------|----------|-------------|
| `CT_TELEGRAM_BOT_TOKEN` | Yes | Token from @BotFather |
| `CT_ALLOWED_USERS` | | Telegram user IDs, comma-separated |
| `CT_PROJECT_DIRS` | Yes | Project directories, comma-separated |
| `CT_PERMISSION_MODE` | | `acceptEdits` (default) / `default` / `bypassPermissions` |
| `CT_MODEL` | | Claude model override |
| `CT_MAX_TURNS` | | Max turns per query (0 = unlimited) |

## Auto-start with hooks

Register in `~/.claude/settings.json` to auto-manage sessions:

```json
{
  "hooks": {
    "SessionStart": [{
      "matcher": "",
      "hooks": [{"type": "command", "command": "bash /path/to/register-session.sh"}]
    }],
    "SessionEnd": [{
      "matcher": "",
      "hooks": [{"type": "command", "command": "bash /path/to/unregister-session.sh"}]
    }]
  }
}
```

When a Claude Code session starts, the hook writes a session file to `/tmp/claude_sessions/`. The bot's background watcher (30s interval) picks it up and sends a Telegram notification. On session end, same flow in reverse.

> **Note**: Must be `settings.json`, not `settings.local.json`. Both `"matcher": ""` and `bash` prefix are required.

## Production

```bash
bash run.sh   # PID lock + circuit breaker + auto-restart
```

## Architecture

```
src/claude_telegram/
├── config.py    # pydantic-settings, CT_ env vars
├── claude.py    # TmuxSession + ClaudeManager
├── bot.py       # Telegram handlers, streaming
├── store.py     # SQLite session logging
└── main.py      # Entrypoint, startup notification
```

5 files, ~800 lines. No over-engineering.

## License

MIT
