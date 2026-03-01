<div align="center">

# claude-telegram

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Telegram Bot](https://img.shields.io/badge/Telegram-Bot-26A5E4?logo=telegram&logoColor=white)](https://core.telegram.org/bots)
[![Claude Code](https://img.shields.io/badge/Claude-Code-D97757?logo=anthropic&logoColor=white)](https://claude.ai)

**텔레그램에서 Claude Code를 실시간으로 제어하는 봇**

메시지 하나로 Claude가 코드를 작성하고, 도구를 실행하고, 결과를 돌려주는 과정을<br/>
텔레그램에서 실시간으로 확인하세요.

</div>

<br/>

## 작동 방식

두 가지 모드를 지원합니다:

**WSL/Linux (tmux 모드)**
```mermaid
sequenceDiagram
    participant T as 📱 Telegram
    participant B as 🤖 Bot
    participant X as 🖥️ tmux (Claude Code)

    T->>B: 메시지 전송
    B->>X: send-keys (입력 전달)
    loop 매 1초 폴링
        X-->>B: capture-pane (출력 캡처)
        B-->>T: edit_message (2초 쓰로틀)
    end
    B->>T: ✅ 완료 알림
```

**Windows (bridge-claude 모드)**
```mermaid
sequenceDiagram
    participant T as 📱 Telegram
    participant B as 🤖 Bot (WSL)
    participant W as 🌉 bridge-claude (Windows)
    participant C as 🖥️ Claude Code

    W->>C: pywinpty PTY 스폰
    T->>B: 메시지 전송
    B->>W: TCP JSON-Lines
    W->>C: PTY 입력 전달
    loop 0.5초 스냅샷
        C-->>W: PTY 출력
        W->>W: pyte 가상 터미널 렌더링
        W-->>B: 화면 스냅샷
        B-->>T: edit_message
    end
    B->>T: ✅ 완료 알림
```

## 주요 기능

| | 기능 | 설명 |
|:---:|------|------|
| **⚡** | **실시간 스트리밍** | `Bash(...)`, `Read(...)` 등 도구 실행 과정을 텔레그램에서 실시간 확인 |
| **🔄** | **자동 세션 감지** | Claude Code hook으로 세션 시작/종료 자동 감지 + 텔레그램 알림 |
| **📂** | **다중 프로젝트** | `/projects`로 번호 목록 확인, `/1` `/2`로 즉시 전환 |
| **🔔** | **완료 알림** | 작업 중 편집은 무음, 완료 시 알림음과 함께 새 메시지 전송 |
| **🛡️** | **자동 복구** | circuit breaker 워치독 (5회 크래시/60초 감지 시 자동 재시작) |

## 명령어

| 명령어 | 설명 |
|--------|------|
| `/projects` | 프로젝트 목록 (● 활성 ○ 비활성) |
| `/1`, `/2`, ... | 번호로 프로젝트 전환 |
| `/project <이름>` | 이름으로 프로젝트 전환 |
| `/new` | 새 대화 시작 |
| `/stop` | Ctrl+C — 작업 중단 |
| `/esc` | Escape 전송 |
| `/yes` | 권한 승인 (y + Enter) |
| `/status` | 세션 상태 확인 |

## 빠른 시작

### WSL/Linux (tmux 모드)

```bash
# 1. 클론 + 설정
git clone https://github.com/ms2116/claude-telegram.git
cd claude-telegram
cp .env.example .env   # 토큰, 유저 ID, 프로젝트 경로 설정

# 2. Claude Code 훅 등록 (한 번만)
# ~/.claude/settings.json 에 아래 내용 추가:
cat <<'EOF'
{
  "hooks": {
    "SessionStart": [{
      "matcher": "",
      "hooks": [{"type": "command", "command": "bash /path/to/claude-telegram/register-session.sh"}]
    }],
    "SessionEnd": [{
      "matcher": "",
      "hooks": [{"type": "command", "command": "bash /path/to/claude-telegram/unregister-session.sh"}]
    }]
  }
}
EOF

# 3. tmux에서 Claude Code 실행하면 봇이 자동 기동됩니다
tmux new -s myproject
claude --dangerously-skip-permissions
# → 봇 자동 시작 → 텔레그램에 알림
```

### Windows (bridge-claude 모드)

```bash
# 1. 클론 + 설정 (WSL에서)
git clone https://github.com/ms2116/claude-telegram.git
cd claude-telegram
cp .env.example .env   # 토큰, 유저 ID, 프로젝트 경로 설정

# 2. Windows에서 bridge-claude 설치 (한 번만)
uv tool install .

# 3. 프로젝트 디렉토리에서 실행
cd D:\your\project
bridge-claude --dangerously-skip-permissions
# → WSL 세션 자동등록 + 봇 자동 기동 + Claude Code 시작
```

> bridge-claude는 Claude Code를 PTY로 감싸서, 터미널에서 직접 사용하면서 동시에 텔레그램으로 원격 제어할 수 있게 합니다.

## 설정

`.env` 파일에서 `CT_` 접두사로 설정합니다.

| 변수 | 필수 | 설명 |
|------|:----:|------|
| `CT_TELEGRAM_BOT_TOKEN` | ✅ | [@BotFather](https://t.me/BotFather)에서 발급받은 토큰 |
| `CT_PROJECT_DIRS` | ✅ | 프로젝트 디렉토리 (쉼표 구분) |
| `CT_ALLOWED_USERS` | | 허용할 텔레그램 유저 ID (쉼표 구분) |
| `CT_PERMISSION_MODE` | | `acceptEdits` / `default` / `bypassPermissions` |
| `CT_MODEL` | | Claude 모델 지정 |
| `CT_MAX_TURNS` | | 쿼리당 최대 턴 (0 = 무제한) |

## 자동 세션 관리

| 모드 | 봇 기동 | 세션 등록 | 봇 종료 |
|------|---------|----------|---------|
| **WSL tmux** | SessionStart 훅 → `register-session.sh` → `run.sh` | 훅이 `/tmp/claude_sessions/`에 JSON 생성 | SessionEnd 훅 → 마지막 세션이면 봇 종료 |
| **Windows PTY** | `bridge-claude` → `_ensure_bot_running()` | bridge-claude가 WSL에 JSON 직접 생성 | bridge-claude 종료 → 세션 해제 → 봇 자동 종료 |

### WSL 훅 설정

`~/.claude/settings.json`에 등록합니다 (한 번만):

```json
{
  "hooks": {
    "SessionStart": [{
      "matcher": "",
      "hooks": [{"type": "command", "command": "bash /path/to/claude-telegram/register-session.sh"}]
    }],
    "SessionEnd": [{
      "matcher": "",
      "hooks": [{"type": "command", "command": "bash /path/to/claude-telegram/unregister-session.sh"}]
    }]
  }
}
```

> [!IMPORTANT]
> `settings.local.json`이 아닌 **`settings.json`** 에 등록해야 합니다.<br/>
> `"matcher": ""`와 `bash` 명시가 필수입니다.<br/>
> `/path/to/`는 실제 클론 경로로 교체하세요.

### Windows bridge-claude

별도 훅 설정 불필요. `bridge-claude` 실행 시 자동으로:
1. WSL distro 감지 → `/tmp/claude_sessions/`에 세션 등록
2. 봇이 안 돌고 있으면 자동 기동
3. 종료 시 세션 해제

## 프로덕션

```bash
bash run.sh   # PID 잠금 + circuit breaker + 자동 재시작
```

## 구조

```
src/claude_telegram/
├── config.py        # 환경변수 (pydantic-settings, CT_ prefix)
├── claude.py        # TmuxSession + ClaudeManager + SDK 폴백
├── pty_wrapper.py   # bridge-claude: pywinpty + pyte PTY 래퍼 (Windows)
├── pty_session.py   # WindowsPtySession: TCP 클라이언트 (봇↔bridge-claude)
├── bot.py           # 텔레그램 핸들러, 스트리밍
├── store.py         # SQLite 세션 로깅
└── main.py          # 엔트리포인트, 기동 알림
```

## 라이선스

MIT
