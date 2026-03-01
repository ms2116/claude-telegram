# claude-telegram

Claude Code 세션을 텔레그램으로 제어하는 봇.

tmux 세션에 직접 연결하여 중간 도구 실행 과정까지 실시간으로 확인 가능. SDK 폴백으로 tmux 없는 프로젝트도 지원.

## 주요 기능

- **실시간 스트리밍** — Claude의 도구 실행 과정(`● Bash(...)`, `⎿ 결과`)을 텔레그램에서 실시간 확인
- **하이브리드 연결** — tmux `capture-pane` 우선, SDK `resume` 폴백
- **자동 세션 감지** — SessionStart/End hook으로 세션 자동 등록/해제 + 텔레그램 알림
- **다중 프로젝트** — `/projects`로 번호 목록 확인, `/1` `/2`로 빠른 전환, 활성(●)/비활성(○) 표시
- **세션 이어하기** — `/session`으로 이전 세션 선택 및 resume
- **완료 알림** — 작업 완료 시 별도 알림 메시지 (소리)
- **링크 프리뷰 비활성화** — URL 포함 응답에서 프리뷰 노이즈 제거
- **자동 재시작** — `run.sh` circuit breaker (5회 크래시/60초 감지, 자동 복구)

## 명령어

| 명령어 | 설명 |
|--------|------|
| `/project <이름>` | 프로젝트 전환 |
| `/projects` | 번호 목록 (● 활성 ○ 비활성) |
| `/1`, `/2`, ... | 번호로 프로젝트 전환 |
| `/session [번호]` | 이전 세션 선택 |
| `/new` | 새 대화 시작 |
| `/stop` | Ctrl+C — 작업 중단 |
| `/esc` | Escape 전송 |
| `/yes` | 권한 승인 (y + Enter) |
| `/status` | 현재 상태 확인 |
| `/refresh` | tmux 세션 새로고침 |

## 설치

```bash
git clone https://github.com/ms2116/claude-telegram.git
cd claude-telegram
uv sync
cp .env.example .env  # 토큰, 유저ID, 프로젝트 경로 설정
```

## 설정 (.env)

| 변수 | 필수 | 설명 |
|------|------|------|
| `CT_TELEGRAM_BOT_TOKEN` | O | @BotFather에서 발급받은 토큰 |
| `CT_ALLOWED_USERS` | - | 허용할 텔레그램 유저 ID (쉼표 구분) |
| `CT_PROJECT_DIRS` | O | 프로젝트 디렉토리 목록 (쉼표 구분) |
| `CT_PERMISSION_MODE` | - | `acceptEdits` (기본), `default`, `bypassPermissions` |
| `CT_MODEL` | - | Claude 모델 지정 |
| `CT_MAX_TURNS` | - | 쿼리당 최대 턴 (0 = 무제한) |

## 실행

```bash
# 직접 실행
uv run claude-telegram

# 자동 재시작 (프로덕션)
bash run.sh
```

## 자동 기동 (SessionStart hook)

`~/.claude/settings.json`에 hook 등록하면 Claude Code 세션 시작/종료 시 봇 자동 관리:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "",
        "hooks": [{"type": "command", "command": "bash /path/to/register-session.sh"}]
      }
    ],
    "SessionEnd": [
      {
        "matcher": "",
        "hooks": [{"type": "command", "command": "bash /path/to/unregister-session.sh"}]
      }
    ]
  }
}
```

> **주의**: `settings.local.json`이 아닌 `settings.json`에 등록해야 함. `"matcher": ""`와 `bash` 명시 필수.

## 구조

```
src/claude_telegram/
├── config.py    # 환경변수 설정 (CT_ prefix)
├── claude.py    # TmuxSession + SDKSession + ClaudeManager
├── bot.py       # 텔레그램 핸들러, 스트리밍
├── store.py     # SQLite: 세션, 메모리
└── main.py      # 엔트리포인트
```

## 라이선스

MIT
