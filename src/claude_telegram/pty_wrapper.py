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
import re
import socket
import sys
import threading
import time
from typing import BinaryIO

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b\(B")
DEFAULT_PORT = 50001
PTY_COLS = 120
PTY_ROWS = 30
PTY_READ_INTERVAL = 0.05  # 50ms polling


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


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
    def __init__(self, port: int = DEFAULT_PORT, cmd: str = "claude.cmd"):
        self.port = port
        self.cmd = cmd
        self._clients: list[JsonLinesClient] = []
        self._clients_lock = threading.Lock()
        self._running = True
        self._pty = None

    def start(self):
        from winpty import PTY  # type: ignore[import-untyped]

        self._pty = PTY(PTY_COLS, PTY_ROWS)
        self._pty.spawn(self.cmd.encode())

        threads = [
            threading.Thread(target=self._stdin_thread, daemon=True),
            threading.Thread(target=self._pty_read_thread, daemon=True),
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

    # ── stdin → PTY ──

    def _stdin_thread(self):
        """Forward local stdin to the PTY."""
        try:
            stdin: BinaryIO = sys.stdin.buffer
            while self._running:
                data = stdin.read(1)
                if not data:
                    break
                if self._pty and self._pty.isalive():
                    self._pty.write(data)
        except (OSError, EOFError):
            pass

    # ── PTY → stdout + TCP broadcast ──

    def _pty_read_thread(self):
        """Poll PTY output, write to stdout and broadcast to TCP clients."""
        while self._running:
            if not self._pty or not self._pty.isalive():
                break
            try:
                data: bytes = self._pty.read()
            except (OSError, EOFError):
                break
            if not data:
                time.sleep(PTY_READ_INTERVAL)
                continue

            # Raw output to local terminal (preserve ANSI)
            try:
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
            except OSError:
                pass

            # ANSI-stripped output to TCP clients
            try:
                text = data.decode("utf-8", errors="replace")
            except Exception:
                text = data.decode("latin-1")
            clean = strip_ansi(text)
            if clean:
                self._broadcast({"type": "output", "data": clean})

            time.sleep(PTY_READ_INTERVAL)

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
        self._server_sock.bind(("127.0.0.1", self.port))
        self._server_sock.listen(4)
        self._server_sock.settimeout(1.0)

        sys.stderr.write(f"[bridge-claude] TCP listening on 127.0.0.1:{self.port}\n")
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
                    self._pty.write(data.encode("utf-8"))
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
        "--cmd", default="claude.cmd",
        help="Command to spawn in PTY (default: claude.cmd)",
    )
    args = parser.parse_args()

    wrapper = PtyWrapper(port=args.port, cmd=args.cmd)
    wrapper.start()


if __name__ == "__main__":
    main()
