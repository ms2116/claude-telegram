"""
bridge-claude: Windows PTY wrapper for Claude Code remote control.

Spawns claude CLI inside a pywinpty pseudo-terminal, then:
  - Forwards local stdin → PTY (keyboard passthrough)
  - Forwards PTY output → local stdout (raw ANSI) + TCP clients (ANSI-stripped)
  - Accepts TCP input (JSON-Lines) → PTY (remote control)

TCP protocol (JSON-Lines, one JSON object per line):
  wrapper → client:  {"type":"output","data":"..."}
  wrapper → client:  {"type":"status","alive":true}
  client  → wrapper: {"type":"input","data":"hello\\n"}
"""
from __future__ import annotations

import argparse
import json
import msvcrt
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import BinaryIO

import re

import pyte

# DA (Device Attributes) response: ESC[?...c — filter from PTY output before display
DA_RE = re.compile(r"\x1b\[\?[0-9;]*c")
DEFAULT_PORT = 50001
WSL_SESSION_DIR = "/tmp/claude_sessions"
# Repo root: pty_wrapper.py → src/claude_telegram/ → src/ → repo
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PTY_COLS = 120
PTY_ROWS = 30
PTY_READ_INTERVAL = 0.05   # 50ms polling
SNAPSHOT_INTERVAL = 0.5     # screen snapshot broadcast interval


def _detect_bot_distro() -> str | None:
    """Find the currently running WSL distro."""
    try:
        r = subprocess.run(
            ["wsl", "-l", "--running", "-q"],
            capture_output=True, timeout=5,
        )
        if r.returncode != 0:
            return None
        text = r.stdout.decode("utf-16-le", errors="replace")
        for name in text.strip().split("\n"):
            name = name.strip()
            if name:
                sys.stderr.write(f"[bridge-claude] WSL distro: {name}\n")
                sys.stderr.flush()
                return name
    except Exception:
        pass
    return None


def _win_to_wsl_path(win_path: str) -> str:
    """Convert Windows path to WSL: D:\\foo\\bar → /mnt/d/foo/bar"""
    path = str(win_path).replace("\\", "/")
    if len(path) >= 2 and path[1] == ":":
        drive = path[0].lower()
        return f"/mnt/{drive}{path[2:]}"
    return path


# ── TCP client handler ──


class JsonLinesClient:
    """Wraps a connected TCP socket for JSON-Lines I/O."""

    def __init__(self, sock: socket.socket, addr: tuple):
        self.sock = sock
        self.addr = addr
        self._lock = threading.Lock()
        self.alive = True

    def send_json(self, obj: dict) -> None:
        if not self.alive:
            return
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        try:
            with self._lock:
                self.sock.sendall(line.encode("utf-8"))
        except OSError:
            self.alive = False

    def recv_lines(self):
        """Yield JSON objects from the socket until disconnect."""
        buf = b""
        while self.alive:
            try:
                chunk = self.sock.recv(4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
        self.alive = False

    def close(self):
        self.alive = False
        try:
            self.sock.close()
        except OSError:
            pass


# ── Main wrapper ──


class PtyWrapper:
    def __init__(
        self,
        port: int = DEFAULT_PORT,
        cmd: str = "claude.cmd",
        project: str | None = None,
        no_register: bool = False,
        wsl_distro: str | None = None,
    ):
        self.port = port
        self.cmd = cmd
        self.project = project or os.path.basename(os.getcwd())
        self.no_register = no_register
        self.wsl_distro = wsl_distro
        self._clients: list[JsonLinesClient] = []
        self._clients_lock = threading.Lock()
        self._running = True
        self._pty = None
        self._registered = False
        # pyte virtual terminal (initialized in start() with actual terminal size)
        self._screen: pyte.Screen | None = None
        self._stream: pyte.Stream | None = None
        self._screen_lock = threading.Lock()
        self._last_snapshot = ""

    @staticmethod
    def _detect_terminal_size() -> tuple[int, int]:
        """Detect actual terminal size, fallback to defaults."""
        try:
            size = os.get_terminal_size()
            return size.columns, size.lines
        except OSError:
            return PTY_COLS, PTY_ROWS

    def start(self):
        from winpty import PTY  # type: ignore[import-untyped]

        # Detect actual terminal size
        cols, rows = self._detect_terminal_size()
        self._screen = pyte.Screen(cols, rows)
        self._stream = pyte.Stream(self._screen)

        self._pty = PTY(cols, rows)
        self._pty.spawn(self.cmd)

        if not self.no_register:
            self._register_session()
            self._ensure_bot_running()

        threads = [
            threading.Thread(target=self._stdin_thread, daemon=True),
            threading.Thread(target=self._pty_read_thread, daemon=True),
            threading.Thread(target=self._snapshot_thread, daemon=True),
            threading.Thread(target=self._tcp_server_thread, daemon=True),
        ]
        for t in threads:
            t.start()

        # Wait for PTY process to exit
        try:
            while self._running and self._pty.isalive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self._shutdown()

    def _shutdown(self):
        self._running = False
        if self._registered:
            self._unregister_session()
        with self._clients_lock:
            for c in self._clients:
                c.close()
            self._clients.clear()
        # Close the server socket to unblock accept()
        if hasattr(self, "_server_sock"):
            try:
                self._server_sock.close()
            except OSError:
                pass

    # ── Session registration (via WSL) ──

    def _wsl_bash(self, script: str, **kwargs) -> subprocess.CompletedProcess:
        """Run a bash script inside WSL (avoids Windows path conversion)."""
        cmd = ["wsl"]
        if self.wsl_distro:
            cmd += ["-d", self.wsl_distro]
        cmd += ["--", "bash", "-c", script]
        return subprocess.run(cmd, **kwargs)

    def _get_wsl_host_ip(self) -> str:
        """Get the Windows host IP as seen from WSL (for WSL2 networking)."""
        try:
            r = self._wsl_bash(
                "ip route show default | head -1 | cut -d' ' -f3",
                capture_output=True, timeout=5,
            )
            if r.returncode == 0:
                ip = r.stdout.decode().strip()
                if ip:
                    sys.stderr.write(f"[bridge-claude] WSL host IP: {ip}\n")
                    sys.stderr.flush()
                    return ip
        except Exception:
            pass
        return "127.0.0.1"

    def _register_session(self) -> None:
        """Write session JSON to WSL /tmp/claude_sessions/ via wsl command."""
        host_ip = self._get_wsl_host_ip()
        data = json.dumps({
            "project": self.project,
            "type": "pty",
            "host": host_ip,
            "port": self.port,
            "work_dir": os.getcwd(),
            "registered_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        })
        try:
            r = self._wsl_bash(
                f"mkdir -p {WSL_SESSION_DIR} && cat > '{WSL_SESSION_DIR}/{self.project}.json'",
                input=data.encode(), timeout=10,
            )
            if r.returncode == 0:
                self._registered = True
                sys.stderr.write(f"[bridge-claude] Session registered: {self.project}\n")
            else:
                sys.stderr.write(f"[bridge-claude] Session register failed (rc={r.returncode})\n")
            sys.stderr.flush()
        except FileNotFoundError:
            sys.stderr.write("[bridge-claude] WSL not found — session not registered\n")
            sys.stderr.flush()
        except Exception as e:
            sys.stderr.write(f"[bridge-claude] Session register failed: {e}\n")
            sys.stderr.flush()

    def _unregister_session(self) -> None:
        """Remove session JSON from WSL."""
        try:
            self._wsl_bash(
                f"rm -f '{WSL_SESSION_DIR}/{self.project}.json'",
                timeout=10,
            )
            sys.stderr.write(f"[bridge-claude] Session unregistered: {self.project}\n")
            sys.stderr.flush()
        except Exception:
            pass

    def _ensure_bot_running(self) -> None:
        """Start the telegram bot on WSL if not already running."""
        try:
            r = self._wsl_bash(
                "pgrep -f claude-telegram > /dev/null 2>&1",
                capture_output=True, timeout=5,
            )
            if r.returncode == 0:
                sys.stderr.write("[bridge-claude] Bot already running\n")
                sys.stderr.flush()
                return
        except Exception:
            pass

        wsl_bot_dir = _win_to_wsl_path(str(REPO_ROOT))
        try:
            cmd = ["wsl"]
            if self.wsl_distro:
                cmd += ["-d", self.wsl_distro]
            cmd += ["--", "bash", "-c",
                    f"cd '{wsl_bot_dir}' && nohup bash run.sh > /dev/null 2>&1 &"]
            subprocess.Popen(cmd)
            sys.stderr.write(f"[bridge-claude] Bot started: {wsl_bot_dir}/run.sh\n")
            sys.stderr.flush()
        except Exception as e:
            sys.stderr.write(f"[bridge-claude] Bot start failed: {e}\n")
            sys.stderr.flush()

    # ── stdin → PTY ──

    def _stdin_thread(self):
        """Forward local stdin to the PTY using raw key input (msvcrt)."""
        try:
            while self._running:
                if not msvcrt.kbhit():
                    time.sleep(0.01)
                    continue
                # msvcrt.getwch() returns one char without echo, raw mode
                ch = msvcrt.getwch()
                if not ch:
                    continue
                # Special keys: getwch returns '\x00' or '\xe0' prefix
                if ch in ("\x00", "\xe0"):
                    ch2 = msvcrt.getwch()
                    # Map common special keys to ANSI sequences
                    key_map = {
                        "H": "\x1b[A",  # Up
                        "P": "\x1b[B",  # Down
                        "M": "\x1b[C",  # Right
                        "K": "\x1b[D",  # Left
                        "G": "\x1b[H",  # Home
                        "O": "\x1b[F",  # End
                        "R": "\x1b[2~", # Insert
                        "S": "\x1b[3~", # Delete
                        "I": "\x1b[5~", # PageUp
                        "Q": "\x1b[6~", # PageDown
                    }
                    seq = key_map.get(ch2, "")
                    if seq and self._pty and self._pty.isalive():
                        self._pty.write(seq)
                else:
                    if self._pty and self._pty.isalive():
                        self._pty.write(ch)
        except (OSError, EOFError):
            pass

    # ── PTY → stdout + pyte screen ──

    def _pty_read_thread(self):
        """Poll PTY output, write to stdout and feed into pyte virtual terminal."""
        while self._running:
            if not self._pty or not self._pty.isalive():
                break
            try:
                text: str = self._pty.read()
            except (OSError, EOFError):
                break
            if not text:
                time.sleep(PTY_READ_INTERVAL)
                continue

            # Raw output to local terminal (filter DA response, preserve other ANSI)
            try:
                display = DA_RE.sub("", text)
                if display:
                    sys.stdout.write(display)
                    sys.stdout.flush()
            except OSError:
                pass

            # Feed into pyte virtual terminal
            with self._screen_lock:
                self._stream.feed(text)

            time.sleep(PTY_READ_INTERVAL)

    # ── Screen snapshot → TCP broadcast ──

    def _snapshot_thread(self):
        """Periodically capture pyte screen and broadcast to TCP clients."""
        while self._running:
            time.sleep(SNAPSHOT_INTERVAL)

            with self._screen_lock:
                lines = []
                for row in range(self._screen.lines):
                    line = self._screen.buffer[row]
                    chars = []
                    for col in range(self._screen.columns):
                        chars.append(line[col].data)
                    lines.append("".join(chars).rstrip())
                snapshot = "\n".join(lines).rstrip()

            if snapshot != self._last_snapshot:
                self._last_snapshot = snapshot
                self._broadcast({"type": "output", "data": snapshot})

    def _broadcast(self, obj: dict):
        with self._clients_lock:
            dead = []
            for c in self._clients:
                c.send_json(obj)
                if not c.alive:
                    dead.append(c)
            for c in dead:
                self._clients.remove(c)

    # ── TCP server ──

    def _tcp_server_thread(self):
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind(("0.0.0.0", self.port))
        self._server_sock.listen(4)
        self._server_sock.settimeout(1.0)

        sys.stderr.write(f"[bridge-claude] TCP listening on 0.0.0.0:{self.port}\n")
        sys.stderr.flush()

        while self._running:
            try:
                conn, addr = self._server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            client = JsonLinesClient(conn, addr)
            with self._clients_lock:
                self._clients.append(client)

            # Send status greeting
            client.send_json({"type": "status", "alive": True})

            # Spawn a thread to read commands from this client
            threading.Thread(
                target=self._client_recv_thread,
                args=(client,),
                daemon=True,
            ).start()

            sys.stderr.write(f"[bridge-claude] Client connected: {addr}\n")
            sys.stderr.flush()

    def _client_recv_thread(self, client: JsonLinesClient):
        """Read JSON-Lines from a TCP client, forward input to PTY."""
        for msg in client.recv_lines():
            if not self._running:
                break
            msg_type = msg.get("type")
            if msg_type == "input":
                data = msg.get("data", "")
                if data and self._pty and self._pty.isalive():
                    # Split text and Enter (\r) to give TUI time to process
                    if len(data) > 1 and data.endswith(("\r", "\n")):
                        text = data.rstrip("\r\n")
                        self._pty.write(text)
                        time.sleep(0.1)
                        self._pty.write("\r")
                    else:
                        self._pty.write(data)
        client.close()
        with self._clients_lock:
            if client in self._clients:
                self._clients.remove(client)


def main():
    parser = argparse.ArgumentParser(
        prog="bridge-claude",
        description="PTY wrapper for Claude Code with TCP remote control",
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help=f"TCP listen port (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--cmd", default="claude.exe",
        help="Command to spawn in PTY (default: claude.exe)",
    )
    parser.add_argument(
        "--project", default=None,
        help="Project name for session registration (default: cwd basename)",
    )
    parser.add_argument(
        "--no-register", action="store_true",
        help="Disable automatic WSL session registration",
    )
    parser.add_argument(
        "--wsl-distro", default=None,
        help="WSL distro name for session registration (auto-detected if omitted)",
    )
    args = parser.parse_args()

    # Auto-detect WSL distro: find one running claude-telegram
    wsl_distro = args.wsl_distro
    if not wsl_distro and not args.no_register:
        wsl_distro = _detect_bot_distro()

    wrapper = PtyWrapper(
        port=args.port,
        cmd=args.cmd,
        project=args.project,
        no_register=args.no_register,
        wsl_distro=wsl_distro,
    )
    wrapper.start()


if __name__ == "__main__":
    main()
