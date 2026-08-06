import os
import stat
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daemon_transport import DaemonUnavailable, UnixWSGIServer, request_over_unix_socket


def simple_app(environ, start_response):
    body = b'{"success":true}'
    start_response(
        "200 OK",
        [("Content-Type", "application/json"), ("Content-Length", str(len(body)))],
    )
    return [body]


@pytest.fixture
def unix_server(tmp_path):
    socket_path = tmp_path / "daemon.sock"
    server = UnixWSGIServer(str(socket_path), simple_app, socket_mode=0o660)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield socket_path
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_unix_transport_round_trip_and_socket_permissions(unix_server):
    response = request_over_unix_socket(
        str(unix_server), "GET", "/api/local/status", timeout=1
    )

    assert response.status == 200
    assert response.body == b'{"success":true}'
    assert stat.S_IMODE(os.stat(unix_server).st_mode) == 0o660


def test_unix_transport_fails_closed_when_daemon_is_missing(tmp_path):
    with pytest.raises(DaemonUnavailable):
        request_over_unix_socket(
            str(tmp_path / "missing.sock"), "GET", "/api/local/status", timeout=0.1
        )


def test_unix_transport_rejects_invalid_paths(unix_server):
    with pytest.raises(ValueError, match="management path"):
        request_over_unix_socket(str(unix_server), "GET", "/", timeout=1)


def test_unix_transport_rejects_oversized_request_body(unix_server):
    with pytest.raises(ValueError, match="too large"):
        request_over_unix_socket(
            str(unix_server),
            "POST",
            "/api/local/upload-config",
            body=b"x" * 1025,
            max_request_bytes=1024,
            timeout=1,
        )


def test_unix_transport_rejects_unsupported_method(unix_server):
    with pytest.raises(ValueError, match="unsupported"):
        request_over_unix_socket(
            str(unix_server), "DELETE", "/api/local/status", timeout=1
        )


def test_unix_server_refuses_to_replace_non_socket_path(tmp_path):
    socket_path = tmp_path / "daemon.sock"
    socket_path.write_text("do not replace", encoding="utf-8")

    with pytest.raises(RuntimeError, match="non-socket"):
        UnixWSGIServer(str(socket_path), simple_app)

    assert socket_path.read_text(encoding="utf-8") == "do not replace"


def test_unix_server_reclaims_stale_owned_socket_path(tmp_path):
    import socket

    socket_path = tmp_path / "daemon.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(socket_path))
    stale.close()

    server = UnixWSGIServer(str(socket_path), simple_app)
    try:
        assert stat.S_ISSOCK(os.stat(socket_path).st_mode)
    finally:
        server.server_close()

    assert not socket_path.exists()
