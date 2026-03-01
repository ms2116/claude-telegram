#!/usr/bin/env bash
# 세션 해제 — 텔레그램 봇에서 프로젝트 세션 제거
#
# 사용법:
#   unregister-session.sh              # 자동 감지
#   unregister-session.sh <project>    # 수동
#
# SessionEnd hook에서 자동 호출됨

SESSION_DIR="/tmp/claude_sessions"

if [ $# -ge 1 ]; then
    PROJECT="$1"
else
    PROJECT=$(basename "$(git -C "$PWD" rev-parse --show-toplevel 2>/dev/null || echo "$PWD")")
fi

rm -f "$SESSION_DIR/$PROJECT.json"
echo "세션 해제: $PROJECT"

# 남은 세션이 없으면 봇 종료
remaining=$(find "$SESSION_DIR" -name '*.json' 2>/dev/null | wc -l)
if [ "$remaining" -eq 0 ]; then
    echo "마지막 세션 해제 — 봇 종료"
    pkill -f 'claude-telegram' 2>/dev/null || true
    rm -f /tmp/claude_telegram_bot.pid
fi
