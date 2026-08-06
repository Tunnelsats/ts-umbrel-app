"""Private HTTP transport between the unprivileged web role and host daemon."""

from __future__ import annotations

import http.client
import os
import socket
import stat
from dataclasses import dataclass
from typing import Mapping, Optional
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer


DEFAULT_MAX_REQUEST_BYTES = 1024 * 1024
DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
DEFAULT_REQUEST_TIMEOUT_SECONDS = 60
ALLOWED_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "POST"})


class DaemonUnavailable(RuntimeError):
    """The private daemon socket could not complete a request."""


@dataclass(frozen=True)
class UnixHTTPResponse:
    status: int
    reason: str
    headers: tuple[tuple[str, str], ...]
    body: bytes


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.socket_path)
        except BaseException:
            sock.close()
            raise
        self.sock = sock


def _validate_management_path(path: str) -> None:
    if not isinstance(path, str) or not path.startswith("/api/local/"):
        raise ValueError("daemon transport requires an /api/local/ management path")
    if "\r" in path or "\n" in path or "#" in path:
        raise ValueError("invalid management path")


def request_over_unix_socket(
    socket_path: str,
    method: str,
    path: str,
    *,
    body: bytes = b"",
    headers: Optional[Mapping[str, str]] = None,
    timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> UnixHTTPResponse:
    """Send a bounded management request to the daemon's private Unix socket."""

    normalized_method = str(method or "").upper()
    if normalized_method not in ALLOWED_METHODS:
        raise ValueError("unsupported daemon request method")
    _validate_management_path(path)
    if not isinstance(body, bytes):
        raise TypeError("daemon request body must be bytes")
    if len(body) > max_request_bytes:
        raise ValueError("daemon request body is too large")
    if max_request_bytes <= 0 or max_response_bytes <= 0:
        raise ValueError("daemon transport size limits must be positive")
    if timeout <= 0:
        raise ValueError("daemon request timeout must be positive")

    safe_headers = {
        str(name): str(value)
        for name, value in (headers or {}).items()
        if str(name).lower() in {"content-type", "accept"}
    }
    safe_headers["Connection"] = "close"

    connection = _UnixHTTPConnection(socket_path, timeout)
    try:
        connection.request(
            normalized_method,
            path,
            body=body if body else None,
            headers=safe_headers,
        )
        raw_response = connection.getresponse()
        chunks = []
        total = 0
        while True:
            chunk = raw_response.read(min(65536, max_response_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_response_bytes:
                raise DaemonUnavailable("daemon response exceeded the size limit")
        return UnixHTTPResponse(
            status=raw_response.status,
            reason=raw_response.reason,
            headers=tuple(raw_response.getheaders()),
            body=b"".join(chunks),
        )
    except (OSError, http.client.HTTPException, TimeoutError) as exc:
        raise DaemonUnavailable("daemon is unavailable") from exc
    finally:
        connection.close()


class _UnixWSGIRequestHandler(WSGIRequestHandler):
    def address_string(self) -> str:
        return "local"

    def get_environ(self):
        original_address = self.client_address
        try:
            self.client_address = ("127.0.0.1", 0)
            return super().get_environ()
        finally:
            self.client_address = original_address


class UnixWSGIServer(WSGIServer):
    """A WSGI server bound only to an owned Unix-domain socket."""

    address_family = socket.AF_UNIX

    def __init__(
        self,
        socket_path: str,
        application,
        *,
        socket_mode: int = 0o660,
        socket_uid: Optional[int] = None,
        socket_gid: Optional[int] = None,
    ):
        self.socket_path = os.path.abspath(socket_path)
        self.socket_mode = socket_mode
        self.socket_uid = socket_uid
        self.socket_gid = socket_gid
        self._owned_socket_identity: Optional[tuple[int, int]] = None
        self._prepare_socket_path()
        super().__init__(self.socket_path, _UnixWSGIRequestHandler)
        self.set_app(application)
        socket_stat = os.stat(self.socket_path, follow_symlinks=False)
        self._owned_socket_identity = (socket_stat.st_dev, socket_stat.st_ino)
        try:
            os.chmod(self.socket_path, self.socket_mode)
            if self.socket_uid is not None or self.socket_gid is not None:
                os.chown(
                    self.socket_path,
                    self.socket_uid if self.socket_uid is not None else -1,
                    self.socket_gid if self.socket_gid is not None else -1,
                )
        except BaseException:
            self.server_close()
            raise

    def _prepare_socket_path(self) -> None:
        parent = os.path.dirname(self.socket_path)
        os.makedirs(parent, mode=0o750, exist_ok=True)
        try:
            existing = os.lstat(self.socket_path)
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(existing.st_mode):
            raise RuntimeError("refusing to replace a non-socket daemon path")

        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.1)
        try:
            probe.connect(self.socket_path)
        except OSError:
            os.unlink(self.socket_path)
        else:
            raise RuntimeError("daemon socket is already active")
        finally:
            probe.close()

    def server_bind(self) -> None:
        self.socket.bind(self.server_address)
        self.server_name = "localhost"
        self.server_port = 0
        self.setup_environ()

    def server_close(self) -> None:
        super().server_close()
        if self._owned_socket_identity is None:
            return
        try:
            socket_stat = os.lstat(self.socket_path)
        except FileNotFoundError:
            return
        if (
            stat.S_ISSOCK(socket_stat.st_mode)
            and (socket_stat.st_dev, socket_stat.st_ino) == self._owned_socket_identity
        ):
            os.unlink(self.socket_path)
