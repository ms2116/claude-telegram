# claude-telegram

Claude Code 세션을 텔레그램으로 제어하는 봇. tmux + Windows PTY 지원.

## 구조

`src/claude_telegram/` 6개 파일:
- `config.py` — pydantic-settings, `CT_` prefix 환경변수
- `claude.py` — `TmuxSession` (capture-pane 기반), `ClaudeManager`
- `pty_session.py` — `WindowsPtySession` (TCP 클라이언트, bridge-claude 연결)
- `pty_wrapper.py` — `bridge-claude` CLI (Windows PTY 래퍼 + TCP 서버)
- `store.py` — aiosqlite: 세션 로깅
- `bot.py` — 텔레그램 핸들러, 스트리밍 (2초 throttle edit, 완료 시 별도 알림)
- `main.py` — 엔트리포인트, `post_init`에서 기동 알림 + 명령어 등록

루트 스크립트:
- `run.sh` — circuit breaker 워치독 (PID lock, 5회/60초 크래시 감지)
- `register-session.sh` — SessionStart hook → 세션 등록 + 봇 자동 기동
- `unregister-session.sh` — SessionEnd hook → 세션 해제 + 마지막 세션이면 봇 종료

## 핵심 설계

- **tmux 기반**: tmux `send-keys`/`capture-pane`으로 Claude Code 직접 제어
- **Windows PTY**: `bridge-claude` TCP 래퍼로 Windows 네이티브 세션도 제어
- **스트리밍**: 매 1초 `capture_pane` (또는 PTY 버퍼) → `extract_response`로 응답 추출 → 전체 텍스트 교체 방식 (delta 아님)
- **`extract_response`**: `user_msg[:15]`로 짧게 검색 (한글 tmux 줄바꿈 대응), 앵커 폴백
- **`_is_spinner_line`**: `(` 위치로 tool call(`● Bash(cmd…)`) vs thinking(`✽ Thinking… (53s)`) 구분
- **완료 알림**: edit은 무음, 완료 시 "완료" 새 메시지 전송 (알림 소리)
- **세션 lifecycle**: SessionStart/SessionEnd hook → 세션 파일 생성/삭제 → 30초 watcher가 감지 → 텔레그램 알림
- **프로젝트 번호**: `/projects`에서 번호 목록 (● 활성 ○ 비활성), `/1` `/2`로 빠른 전환
- **hook 설정**: `~/.claude/settings.json`에 `"matcher": ""` + `"command": "bash ..."` 형식

## 세션 타입

### tmux 세션 (WSL)
```json
{"project":"my-app","pane_id":"%5","work_dir":"/home/user/my-app"}
```
- `register-session.sh`가 SessionStart hook에서 자동 생성
- `unregister-session.sh`가 SessionEnd hook에서 자동 삭제

### PTY 세션 (Windows)
```json
{"project":"my-app","type":"pty","host":"127.0.0.1","port":50001,"work_dir":"D:\\my-app"}
```
- `bridge-claude` 실행 시 자동 생성 (WSL `/tmp/claude_sessions/`에 `wsl -e`로 등록)
- `bridge-claude` 종료 시 자동 삭제
- `--no-register`로 자동 등록 비활성화 가능

## 실행

```bash
# 봇 (WSL)
cp .env.example .env  # 토큰, 유저ID, 프로젝트 경로 설정
uv run claude-telegram
# 또는 circuit breaker:
bash run.sh

# bridge-claude (Windows) — claude 대신 실행
bridge-claude                          # 기본 (포트 50001, 자동 등록)
bridge-claude --port 50002             # 다른 포트
bridge-claude --project my-app         # 프로젝트명 수동 지정
bridge-claude --no-register            # WSL 세션 등록 안 함
```

## 테스트

수동 테스트:
1. 텔레그램에서 메시지 전송 → Claude 응답 스트리밍
2. `/stop` → Ctrl+C 전송
3. `/esc` → Escape 전송
4. `/yes` → y + Enter 전송
5. `/project <name>` → 프로젝트 전환
6. `/projects` → 번호 목록 (● 활성 ○ 비활성)
7. `/1` `/2` → 번호로 프로젝트 전환
