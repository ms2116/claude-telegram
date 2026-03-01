"""TCP JSON-Lines protocol tests for pty_wrapper (no pywinpty needed)."""
import json
import socket
import threading
import time

from claude_telegram.pty_wrapper import JsonLinesClient, strip_ansi


def recv_json(sock: socket.socket, timeout: float = 2.0) -> dict:
    """Read one JSON-Lines message from socket."""
    sock.settimeout(timeout)
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("socket closed")
        buf += chunk
    line, _ = buf.split(b"\n", 1)
    return json.loads(line.decode("utf-8"))


def test_strip_ansi():
    """ANSI escape sequences are removed."""
    raw = "\x1b[31mHello\x1b[0m World\x1b]0;title\x07"
    assert strip_ansi(raw) == "Hello World"


def test_strip_ansi_empty():
    assert strip_ansi("") == ""
    assert strip_ansi("plain text") == "plain text"


def test_json_lines_client_send():
    """JsonLinesClient can send JSON to a connected socket."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.listen(1)

    # Connect
    client_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client_sock.connect(("127.0.0.1", port))
    conn, _ = srv.accept()

    jlc = JsonLinesClient(conn, ("127.0.0.1", port))

    # Send a message
    jlc.send_json({"type": "output", "data": "hello"})

    # Receive on client side
    msg = recv_json(client_sock)
    assert msg == {"type": "output", "data": "hello"}

    jlc.close()
    client_sock.close()
    srv.close()


def test_json_lines_client_recv():
    """JsonLinesClient can receive JSON-Lines from a connected socket."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.listen(1)

    sender_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sender_sock.connect(("127.0.0.1", port))
    conn, addr = srv.accept()

    jlc = JsonLinesClient(conn, addr)

    # Send two messages from the sender
    sender_sock.sendall(b'{"type":"input","data":"hi"}\n')
    sender_sock.sendall(b'{"type":"input","data":"bye"}\n')
    sender_sock.close()  # EOF triggers recv_lines to stop

    messages = list(jlc.recv_lines())
    assert len(messages) == 2
    assert messages[0] == {"type": "input", "data": "hi"}
    assert messages[1] == {"type": "input", "data": "bye"}

    jlc.close()
    srv.close()


def test_roundtrip_bidirectional():
    """Full bidirectional roundtrip: server sends output, client sends input."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.listen(1)

    client_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client_sock.connect(("127.0.0.1", port))
    conn, addr = srv.accept()

    server_client = JsonLinesClient(conn, addr)

    # Server → Client: status greeting
    server_client.send_json({"type": "status", "alive": True})
    msg = recv_json(client_sock)
    assert msg == {"type": "status", "alive": True}

    # Server → Client: output
    server_client.send_json({"type": "output", "data": "Welcome to Claude"})
    msg = recv_json(client_sock)
    assert msg["type"] == "output"
    assert "Claude" in msg["data"]

    # Client → Server: input
    client_sock.sendall(b'{"type":"input","data":"hello\\n"}\n')

    received = []
    def collect():
        for m in server_client.recv_lines():
            received.append(m)
            break  # just get one
    t = threading.Thread(target=collect)
    t.start()
    t.join(timeout=2)

    assert len(received) == 1
    assert received[0] == {"type": "input", "data": "hello\n"}

    server_client.close()
    client_sock.close()
    srv.close()


def test_client_disconnect_graceful():
    """Server-side JsonLinesClient handles client disconnect without error."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.listen(1)

    client_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client_sock.connect(("127.0.0.1", port))
    conn, addr = srv.accept()

    jlc = JsonLinesClient(conn, addr)

    # Close client immediately
    client_sock.close()

    # recv_lines should return empty (no crash)
    messages = list(jlc.recv_lines())
    assert messages == []
    assert not jlc.alive

    jlc.close()
    srv.close()


def test_broadcast_removes_dead_clients():
    """Broadcast skips and removes dead clients without error."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.listen(2)

    # Two clients connect
    sock1 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock1.connect(("127.0.0.1", port))
    conn1, addr1 = srv.accept()

    sock2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock2.connect(("127.0.0.1", port))
    conn2, addr2 = srv.accept()

    jlc1 = JsonLinesClient(conn1, addr1)
    jlc2 = JsonLinesClient(conn2, addr2)

    # Kill client 1
    sock1.close()
    jlc1.alive = False

    # Build a mini wrapper-like broadcast
    clients = [jlc1, jlc2]
    dead = []
    for c in clients:
        c.send_json({"type": "output", "data": "test"})
        if not c.alive:
            dead.append(c)
    for c in dead:
        clients.remove(c)

    assert len(clients) == 1
    assert clients[0] is jlc2

    # Client 2 should have received the message
    msg = recv_json(sock2)
    assert msg["data"] == "test"

    jlc2.close()
    sock2.close()
    srv.close()


if __name__ == "__main__":
    test_strip_ansi()
    test_strip_ansi_empty()
    test_json_lines_client_send()
    test_json_lines_client_recv()
    test_roundtrip_bidirectional()
    test_client_disconnect_graceful()
    test_broadcast_removes_dead_clients()
    print("All tests passed!")
