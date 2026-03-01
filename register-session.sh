#!/usr/bin/env bash
# 세션 등록 — Claude Code 프로젝트를 텔레그램 봇에 자동 등록
#
# 사용법:
#   register-session.sh                    # 자동 감지
#   register-session.sh <project> <pane> <workdir>  # 수동
#
# SessionStart hook에서 자동 호출됨

set -euo pipefail

BOT_DIR="$(cd "$(dirname "$0")" && pwd)"
SESSION_DIR="/tmp/claude_sessions"
LOCK_FILE="/tmp/claude_telegram_bot.pid"

if [ $# -ge 3 ]; then
    PROJECT="$1"; PANE_ID="$2"; WORK_DIR="$3"
else
    WORK_DIR="${PWD}"
    PROJECT=$(basename "$(git -C "$WORK_DIR" rev-parse --show-toplevel 2>/dev/null || echo "$WORK_DIR")")
    PANE_ID="${TMUX_PANE:-}"
    if [ -z "$PANE_ID" ]; then
        # tmux 밖에서 호출 — 세션만 등록하지 않음
        exit 0
    fi
fi

# 세션 등록
mkdir -p "$SESSION_DIR"
cat > "$SESSION_DIR/$PROJECT.json" <<EOF
{"project":"$PROJECT","pane_id":"$PANE_ID","work_dir":"$WORK_DIR","registered_at":"$(date -Iseconds)"}
EOF
echo "세션 등록: $PROJECT (pane=$PANE_ID)"

# 봇이 안 떠있으면 자동 기동
bot_running=false
if [ -f "$LOCK_FILE" ]; then
    old_pid=$(cat "$LOCK_FILE" 2>/dev/null)
    if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
        bot_running=true
    fi
fi

if [ "$bot_running" = false ]; then
    echo "봇 기동 중..."
    nohup "$BOT_DIR/run.sh" > /dev/null 2>&1 &
    echo "봇 기동 완료"
fi
