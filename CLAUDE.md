# claude-telegram

Claude Code 세션을 텔레그램으로 제어하는 봇. tmux 우선, SDK 폴백.

## 구조

`src/claude_telegram/` 5개 파일:
- `config.py` — pydantic-settings, `CT_` prefix 환경변수
- `claude.py` — `TmuxSession` (capture-pane 기반), `SDKSession` (resume 지원), `ClaudeManager`
- `store.py` — aiosqlite: 세션, 메모리
- `bot.py` — 텔레그램 핸들러, 스트리밍 (2초 throttle edit, 완료 시 별도 알림)
- `main.py` — 엔트리포인트, `post_init`에서 기동 알림 + 명령어 등록

루트 스크립트:
- `run.sh` — circuit breaker 워치독 (PID lock, 5회/60초 크래시 감지)
- `register-session.sh` — SessionStart hook → 세션 등록 + 봇 자동 기동
- `unregister-session.sh` — SessionEnd hook → 세션 해제 + 마지막 세션이면 봇 종료

## 핵심 설계

- **하이브리드**: tmux `send-keys`/`capture-pane` 우선 → SDK `resume` 폴백
- **스트리밍**: 매 1초 `capture_pane` → `extract_response`로 응답 추출 → 전체 텍스트 교체 방식 (delta 아님)
- **`extract_response`**: `user_msg[:15]`로 짧게 검색 (한글 tmux 줄바꿈 대응), 앵커 폴백
- **`_is_spinner_line`**: `(` 위치로 tool call(`● Bash(cmd…)`) vs thinking(`✽ Thinking… (53s)`) 구분
- **완료 알림**: edit은 무음, 완료 시 "완료" 새 메시지 전송 (알림 소리)
- **세션 lifecycle**: SessionStart/SessionEnd hook으로 자동 기동/종료

## 실행

```bash
cp .env.example .env  # 토큰, 유저ID, 프로젝트 경로 설정
uv run claude-telegram
# 또는 circuit breaker:
bash run.sh
```

## 테스트

수동 테스트:
1. 텔레그램에서 메시지 전송 → Claude 응답 스트리밍
2. `/stop` → Ctrl+C 전송
3. `/esc` → Escape 전송
4. `/yes` → y + Enter 전송
5. `/project <name>` → 프로젝트 전환
6. `/session` → 이전 세션 목록 및 선택
