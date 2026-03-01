#!/usr/bin/env bash
# 텔레그램 봇 자동 재시작 래퍼 (circuit breaker + 세션 연동)
#
# - 봇이 죽으면 자동 재시작
# - 60초 안에 5번 크래시 → crash loop → 대기 후 재시도
# - tmux 세션 종료 시 함께 종료
# - register-session.sh에 의해 자동 기동됨

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="${CT_BOT_LOG:-/tmp/claude-telegram.log}"
LOCK_FILE="/tmp/claude_telegram_bot.pid"
SESSION_DIR="/tmp/claude_sessions"

# circuit breaker 설정
CB_WINDOW=60
CB_THRESHOLD=5
RESTART_DELAY=5
RECOVERY_WAIT=120

# .env 로드
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/.env"
    set +a
fi

# ── 단일 인스턴스 보장 (PID 락) ──
if [ -f "$LOCK_FILE" ]; then
    old_pid=$(cat "$LOCK_FILE" 2>/dev/null)
    if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
        echo "$(date '+%F %T') [watchdog] 이미 실행 중 (PID $old_pid)" >> "$LOG_FILE"
        exit 0
    fi
    rm -f "$LOCK_FILE"
fi

# 기존 봇 프로세스 정리
pkill -f 'claude-telegram' 2>/dev/null || true
sleep 1

echo $$ > "$LOCK_FILE"

cleanup() {
    rm -f "$LOCK_FILE"
    pkill -P $$ 2>/dev/null || true
}
trap cleanup EXIT INT TERM

crash_times=()

log() {
    echo "$(date '+%F %T') [watchdog] $1" >> "$LOG_FILE"
}

send_telegram() {
    local msg="$1"
    local token="${CT_TELEGRAM_BOT_TOKEN:-}"
    local users="${CT_ALLOWED_USERS:-}"
    [ -z "$token" ] || [ -z "$users" ] && return
    for chat_id in ${users//,/ }; do
        curl -sf -X POST \
            "https://api.telegram.org/bot${token}/sendMessage" \
            -d chat_id="$chat_id" \
            -d text="$msg" > /dev/null 2>&1 || true
    done
}

count_recent_crashes() {
    local now cutoff count=0
    now=$(date +%s)
    cutoff=$((now - CB_WINDOW))
    local new_times=()
    for t in "${crash_times[@]}"; do
        if [ "$t" -ge "$cutoff" ]; then
            new_times+=("$t")
            count=$((count + 1))
        fi
    done
    crash_times=("${new_times[@]+"${new_times[@]}"}")
    echo "$count"
}

has_any_session() {
    # tmux sessions alive?
    tmux list-sessions &>/dev/null && return 0
    # PTY session files exist?
    [ -d "$SESSION_DIR" ] && [ -n "$(find "$SESSION_DIR" -name '*.json' -print -quit 2>/dev/null)" ] && return 0
    return 1
}

restart_count=0

while true; do
    if ! has_any_session; then
        log "세션 없음 (tmux/PTY) — 래퍼 종료"
        exit 0
    fi

    log "봇 시작 (restart #$restart_count)"
    start_time=$(date +%s)

    cd "$SCRIPT_DIR"
    uv run claude-telegram >> "$LOG_FILE" 2>&1 || true
    end_time=$(date +%s)
    uptime=$((end_time - start_time))

    if ! has_any_session; then
        log "세션 없음 (tmux/PTY) — 래퍼 종료"
        exit 0
    fi

    restart_count=$((restart_count + 1))

    if [ "$uptime" -ge 30 ]; then
        log "봇 종료(uptime=${uptime}s) — ${RESTART_DELAY}초 후 재시작"
        crash_times=()
        sleep "$RESTART_DELAY"
        continue
    fi

    crash_times+=("$end_time")
    recent=$(count_recent_crashes)
    log "봇 크래시(uptime=${uptime}s) — ${CB_WINDOW}초 내 ${recent}/${CB_THRESHOLD}회"

    if [ "$recent" -ge "$CB_THRESHOLD" ]; then
        log "CIRCUIT OPEN — ${RECOVERY_WAIT}초 대기"
        send_telegram "⚠️ 봇 crash loop (${CB_WINDOW}초 내 ${recent}회). ${RECOVERY_WAIT}초 후 재시도."
        sleep "$RECOVERY_WAIT"

        if ! has_any_session; then
            log "복구 대기 후 세션 없음 — 래퍼 종료"
            exit 0
        fi
        crash_times=()
        log "CIRCUIT CLOSED — 재시도"
    else
        sleep "$RESTART_DELAY"
    fi
done
