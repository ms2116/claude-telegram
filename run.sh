#!/usr/bin/env bash
# Circuit breaker wrapper for claude-telegram
# Restarts on crash with exponential backoff, sends Telegram alert on repeated failures.

set -euo pipefail

MAX_RETRIES=5
BACKOFF=5          # initial backoff seconds
MAX_BACKOFF=300    # max 5 minutes
ALERT_AFTER=3      # send Telegram alert after this many consecutive failures

failures=0
backoff=$BACKOFF

send_alert() {
    local msg="$1"
    if [[ -n "${CT_TELEGRAM_BOT_TOKEN:-}" && -n "${CT_ALERT_CHAT_ID:-}" ]]; then
        curl -sf -X POST \
            "https://api.telegram.org/bot${CT_TELEGRAM_BOT_TOKEN}/sendMessage" \
            -d chat_id="${CT_ALERT_CHAT_ID}" \
            -d text="${msg}" > /dev/null 2>&1 || true
    fi
}

while true; do
    echo "[$(date)] Starting claude-telegram (attempt $((failures + 1)))"

    if uv run claude-telegram; then
        # Clean exit (e.g., SIGTERM) — don't restart
        echo "[$(date)] Clean exit."
        break
    fi

    exit_code=$?
    failures=$((failures + 1))
    echo "[$(date)] Crashed with exit code $exit_code (failure #$failures)"

    if (( failures >= ALERT_AFTER )); then
        send_alert "⚠️ claude-telegram crashed $failures times (exit $exit_code). Backoff: ${backoff}s"
    fi

    if (( failures >= MAX_RETRIES )); then
        echo "[$(date)] Max retries ($MAX_RETRIES) reached. Giving up."
        send_alert "🛑 claude-telegram stopped after $MAX_RETRIES failures."
        exit 1
    fi

    echo "[$(date)] Restarting in ${backoff}s..."
    sleep $backoff
    backoff=$(( backoff * 2 ))
    (( backoff > MAX_BACKOFF )) && backoff=$MAX_BACKOFF
done
