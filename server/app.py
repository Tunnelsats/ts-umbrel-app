import json
import os
import re
import shlex
import socket
import subprocess
import uuid
import threading
import concurrent.futures
from datetime import datetime, timezone
import time
from ipaddress import ip_address, ip_network
from typing import Callable, Dict, Any, List, Optional, Tuple, Iterable
from urllib.parse import quote

import requests
import yaml
from flask import Flask, Response, abort, jsonify, request, send_from_directory, send_file
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

from security import ManagementSecurity
from daemon_transport import (
    ALLOWED_METHODS as DAEMON_ALLOWED_METHODS,
    DEFAULT_MAX_REQUEST_BYTES,
    DaemonUnavailable,
    UnixWSGIServer,
    request_over_unix_socket,
)

# Ensure verbose logging for container restarts is visible and unbuffered
import logging
import sys

app = Flask(__name__, static_folder="../web", static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = DEFAULT_MAX_REQUEST_BYTES
# Umbrel uses a reverse proxy. Parse X-Forwarded-* headers before IP restrictions.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

APP_VERSION = "v3.4.2"
APP_MANIFEST_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "umbrel-app.yml"))

class SecurityHeadersMiddleware:
    """
    WSGI middleware to ensure exactly one set of security headers is sent.
    This prevents 'Ghost' CSP policies (where browsers intersect multiple headers).
    """
    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        def custom_start_response(status, headers, exc_info=None):
            # Headers is a list of tuples (name, value)
            # 1. Strip any existing security headers to prevent duplication/intersection
            security_keys = {'content-security-policy', 'x-frame-options', 'x-content-type-options'}
            new_headers = [(n, v) for n, v in headers if n.lower() not in security_keys]

            # 2. Define our authoritative, hardened CSP
            # - 'unsafe-eval' & 'blob:': Required for Globe.gl/Three.js workers & shaders
            # - 'unsafe-inline': Required for Globe.gl layout style injection
            # - 'frame-ancestors self': Permits Umbrel dashboard framing
            csp = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-eval' blob:; "
                "script-src-elem 'self' blob:; "
                "style-src 'self' 'unsafe-inline'; "
                "font-src 'self'; "
                "img-src 'self' data: blob: https://*.tunnelsats.com; "
                "connect-src 'self' https://*.tunnelsats.com; "
                "worker-src 'self' blob:; "
                "frame-ancestors 'self'; "
                "object-src 'none';"
            )

            # 3. Append our single, canonical security headers
            new_headers.append(('Content-Security-Policy', csp))
            new_headers.append(('X-Frame-Options', 'SAMEORIGIN'))
            new_headers.append(('X-Content-Type-Options', 'nosniff'))

            return start_response(status, new_headers, exc_info)

        return self.app(environ, custom_start_response)

app.wsgi_app = SecurityHeadersMiddleware(app.wsgi_app)

# Ensure verbose logging for container restarts is visible and unbuffered
class TunnelsatsFormatter(logging.Formatter):
    def format(self, record):
        utc_dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
        return f"{utc_dt.strftime('%Y-%m-%dT%H:%M:%SZ')} [{record.levelname}] {record.getMessage()}"

handler = logging.StreamHandler(sys.stderr)
handler.setFormatter(TunnelsatsFormatter())
app.logger.handlers = [handler]
app.logger.setLevel(logging.INFO)

# Also ensure stdout is unbuffered for subprocess logs
os.environ["PYTHONUNBUFFERED"] = "1"

TUNNELSATS_API_URL = "https://tunnelsats.com/api/public/v1"
TUNNELSATS_ROLE = os.environ.get("TUNNELSATS_ROLE", "combined").strip().lower()
if TUNNELSATS_ROLE not in {"combined", "web", "daemon"}:
    raise ValueError("TUNNELSATS_ROLE must be combined, web, or daemon")
DAEMON_SOCKET_PATH = os.environ.get(
    "TUNNELSATS_DAEMON_SOCKET", "/run/tunnelsats/daemon.sock"
)
DATA_DIR = "/data"
META_FILE = "tunnelsats-meta.json"
DOCKER_SOCK = "/var/run/docker.sock"
STATE_FILE = "/tmp/tunnelsats_state.json"
RECONCILE_TRIGGER_DIR = "/tmp/tunnelsats_reconcile_trigger.d"
RECONCILE_RESULT_DIR = "/tmp/tunnelsats_reconcile_result.d"
RECONCILE_RESULT_LEGACY = "/tmp/tunnelsats_reconcile_result.json"
DECOMMISSION_TRIGGER_DIR = "/tmp/tunnelsats_decommission_trigger.d"
DECOMMISSION_RESULT_DIR = "/tmp/tunnelsats_decommission_result.d"
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
LND_CONFIG_PATH = "/lightning-data/lnd/lnd.conf"
CLN_CONFIG_PATH = "/lightning-data/cln/config"
LND_HOST_CONFIG_PATH = "/home/umbrel/umbrel/app-data/lightning/data/lnd/lnd.conf"
LND_PROBE_IP = "10.21.21.9"
LND_PROBE_PORT = 9735
CLN_PROBE_IP = "10.21.21.96"
CLN_PROBE_PORT = 9736
LND_RESTART_DELAY = 3  # Seconds to wait for middleware to generate umbrel-lnd.conf
LND_CONTAINER_PATTERN = r"(^|[_-])(lightning[_-]lnd|lnd)(?:[_-]?\d+)?$"
LND_MIDDLEWARE_PATTERN = r"(^|[_-])(lightning[_-]app|lnd[_-]app|lightning[_-]ui)(?:[_-]?\d+)?$"
CLN_CONTAINER_PATTERN = r"(^|[_-])(core-lightning|clightning|lightningd|cln)([_-]|\d|$)"
WG_INTERFACE_NAME = "tunnelsatsv2"
WG_HANDSHAKE_MAX_AGE_SECONDS = 180
ANNOUNCEMENT_CONFLICT_ERROR = (
    "Conflicting node announcement setting detected in node configuration."
)
ANNOUNCEMENT_VERIFICATION_ERROR = (
    "Lightning node gossip announcements have not been verified free of conflicting clearnet addresses."
)
TARGET_SWITCH_PENDING_ERROR = "Lightning target switch is pending dataplane reconciliation."
RECONCILE_WAIT_TIMEOUT_SECONDS = 60.0
RECONCILE_WAIT_POLL_SECONDS = 0.1
K8S_POD_TERMINATION_TIMEOUT_SECONDS = 30.0
DAEMON_RESTORE_TIMEOUT_SECONDS = (
    RECONCILE_WAIT_TIMEOUT_SECONDS + (2 * K8S_POD_TERMINATION_TIMEOUT_SECONDS) + 30.0
)

# k3s mode: set via env var K3S_MODE=true to use the Kubernetes API instead of the Docker socket
K3S_MODE = os.environ.get("K3S_MODE", "false").strip().lower() == "true"
SECURE_MODE = os.environ.get("SECURE_MODE", "false").strip().lower() == "true"
MANAGEMENT_SECURITY = ManagementSecurity()
MANAGEMENT_CSRF_HEADER = MANAGEMENT_SECURITY.csrf_header
MANAGEMENT_CSRF_REFRESH_HEADER = MANAGEMENT_SECURITY.csrf_refresh_header
MANAGEMENT_CSRF_TOKEN = MANAGEMENT_SECURITY.csrf_token
MANAGEMENT_JSON_PATHS = frozenset(
    {
        "/api/local/upload-config",
        "/api/local/configure-node",
        "/api/subscription/renew",
        "/api/subscription/claim",
    }
)
def dashboard_bind_config():
    default_host = "0.0.0.0" if (K3S_MODE or TUNNELSATS_ROLE == "web") else "127.0.0.1"
    default_port = "9739" if K3S_MODE else "9740"
    host = os.environ.get("DASHBOARD_BIND_HOST", default_host).strip()
    raw_port = os.environ.get("DASHBOARD_BIND_PORT", default_port).strip()
    if not host:
        raise ValueError("DASHBOARD_BIND_HOST must not be empty")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ValueError("DASHBOARD_BIND_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("DASHBOARD_BIND_PORT must be between 1 and 65535")
    return host, port

def check_tcp_port(ip, port):
    try:
        with socket.create_connection((ip, port), timeout=1.0):
            return True
    except (socket.timeout, OSError):
        return False

_PROBE_CACHE_TTL_SECONDS = 5
_probe_cache = {}
_probe_cache_lock = threading.Lock()
_in_flight_probes = {}
_in_flight_lock = threading.Lock()

def check_tcp_port_cached(ip, port):
    key = f"{ip}:{port}"
    with _probe_cache_lock:
        entry = _probe_cache.get(key)
        if entry is not None:
            ts, val = entry
            if time.time() - ts <= _PROBE_CACHE_TTL_SECONDS:
                return val

    initiator = False
    with _in_flight_lock:
        if key in _in_flight_probes:
            event = _in_flight_probes[key]
        else:
            # Re-check cache inside lock to prevent TOCTOU window
            with _probe_cache_lock:
                entry = _probe_cache.get(key)
                if entry is not None:
                    ts, val = entry
                    if time.time() - ts <= _PROBE_CACHE_TTL_SECONDS:
                        return val
            event = threading.Event()
            _in_flight_probes[key] = event
            initiator = True

    if not initiator:
        if event.wait(timeout=3.0):
            with _probe_cache_lock:
                entry = _probe_cache.get(key)
                if entry is not None:
                    return entry[1]
        # Fallback to direct check if timeout or cache is empty
        return check_tcp_port(ip, port)

    val = False
    try:
        val = check_tcp_port(ip, port)
        with _probe_cache_lock:
            _probe_cache[key] = (time.time(), val)
    finally:
        with _in_flight_lock:
            finished_event = _in_flight_probes.pop(key, None)
            if finished_event is not None:
                finished_event.set()

    return val

_cln_network_cache = None
_cln_network_cache_time = 0.0
_cln_network_lock = threading.Lock()

def detect_cln_network():
    """Detect the active CLN network by checking subdirectories in /lightning-data/cln/."""
    global _cln_network_cache, _cln_network_cache_time
    now = time.time()
    with _cln_network_lock:
        if _cln_network_cache is not None and now - _cln_network_cache_time < 60.0:
            return _cln_network_cache

    networks = ["bitcoin", "testnet", "signet", "regtest"]
    active_net = "bitcoin"
    latest_mtime = 0.0

    for net in networks:
        db_path = f"/lightning-data/cln/{net}/lightningd.sqlite3"
        if os.path.exists(db_path):
            try:
                mtime = os.path.getmtime(db_path)
                if mtime > latest_mtime:
                    latest_mtime = mtime
                    active_net = net
            except Exception:
                pass

    # If no sqlite database was found, fall back to checking config file presence using mtime recency
    if latest_mtime == 0.0:
        latest_config_mtime = 0.0
        for net in networks:
            config_path = f"/lightning-data/cln/{net}/config"
            try:
                mtime = os.path.getmtime(config_path)
                if mtime > latest_config_mtime:
                    latest_config_mtime = mtime
                    active_net = net
            except Exception:
                pass

    with _cln_network_lock:
        _cln_network_cache = active_net
        _cln_network_cache_time = now

    return active_net


def resolve_node_config(node_type):
    """Return (config_path_container, config_path_host) for LND or CLN."""
    if node_type == "lnd":
        return LND_CONFIG_PATH, LND_HOST_CONFIG_PATH
    elif node_type == "cln":
        net = detect_cln_network()
        container_path = f"/lightning-data/cln/{net}/config"
        # Test-only sentinel: when CLN_CONFIG_PATH is patched to a temp file by a test,
        # use that path as the container path so file-presence assertions resolve correctly.
        # NOTE: host_path is still derived from detect_cln_network(); patch detect_cln_network
        # too if the test needs a consistent (container_path, host_path) pair.
        if CLN_CONFIG_PATH != "/lightning-data/cln/config":
            container_path = CLN_CONFIG_PATH
        elif not os.path.exists(container_path) and os.path.exists("/lightning-data/cln/config"):
            container_path = "/lightning-data/cln/config"

        host_path = f"/home/umbrel/umbrel/app-data/core-lightning/data/lightningd/{net}/config"
        if container_path == "/lightning-data/cln/config":
            host_path = "/home/umbrel/umbrel/app-data/core-lightning/data/lightningd/config"
        return container_path, host_path
    return None, None

K8S_NAMESPACE = os.environ.get("K8S_NAMESPACE", "default")
LND_K8S_NAMESPACE = os.environ.get("LND_K8S_NAMESPACE", K8S_NAMESPACE)
CLN_K8S_NAMESPACE = os.environ.get("CLN_K8S_NAMESPACE", K8S_NAMESPACE)
LND_K8S_POD_SELECTOR = os.environ.get("LND_K8S_POD_SELECTOR", "app=lnd")
CLN_K8S_POD_SELECTOR = os.environ.get("CLN_K8S_POD_SELECTOR", "app=cln")
K8S_SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
K8S_SA_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"

# Shared HTTP session for k8s API calls — enables TLS keep-alive across helpers so
# the dashboard does not pay a fresh TCP+TLS handshake on every request.
_k8s_session = requests.Session()

# Tiny per-key TTL cache for k8s API responses. The dashboard's status endpoint is
# polled frequently from the UI; without caching this would translate 1:1 to k8s
# API calls and risk rate-limiting on busy clusters.
_K8S_CACHE_TTL_SECONDS = 5
_k8s_cache: Dict[str, Tuple[float, Any]] = {}
_k8s_cache_lock = threading.Lock()
_dns_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)


def _k8s_cache_get(key):
    with _k8s_cache_lock:
        entry = _k8s_cache.get(key)
        if entry is None:
            return None
        ts, value = entry
        if time.time() - ts > _K8S_CACHE_TTL_SECONDS:
            return None
        return value


def _k8s_cache_set(key, value):
    with _k8s_cache_lock:
        _k8s_cache[key] = (time.time(), value)

_metadata_lock = threading.Lock()

ALLOWED_NETWORKS = (
    ip_network("127.0.0.0/8"),
    ip_network("10.0.0.0/8"),
    ip_network("100.64.0.0/10"),  # RFC 6598 Shared Address Space (Tailscale/Headscale CGNAT)
    ip_network("172.16.0.0/12"),
    ip_network("192.168.0.0/16"),
    ip_network("::1/128"),
    ip_network("fc00::/7"),
    # NOTE: fe80::/10 (IPv6 link-local) is intentionally excluded.
)


NUREMBERG_DE = {'lat': 49.4521, 'lng': 11.0767, 'label': 'NUREMBERG, DE', 'flag': '🇩🇪'}
NEW_YORK_US = {'lat': 40.7128, 'lng': -74.0060, 'label': 'NEW YORK, US', 'flag': '🇺🇸'}

TUNNELSATS_SERVER_METADATA = {
    'au1': {'lat': -33.8688, 'lng': 151.2093, 'label': 'SYDNEY, AU', 'flag': '🇦🇺'},
    'br1': {'lat': -23.5505, 'lng': -46.6333, 'label': 'SAO PAULO, BR', 'flag': '🇧🇷'},
    'de1': {'lat': 50.1109, 'lng': 8.6821, 'label': 'FRANKFURT, DE', 'flag': '🇩🇪'},
    'de2': NUREMBERG_DE,
    'de3': NUREMBERG_DE,
    'fi1': {'lat': 60.1695, 'lng': 24.9354, 'label': 'HELSINKI, FI', 'flag': '🇫🇮'},
    'sg1': {'lat': 1.3521, 'lng': 103.8198, 'label': 'SINGAPORE, SG', 'flag': '🇸🇬'},
    'us1': NEW_YORK_US,
    'us2': {'lat': 45.5231, 'lng': -122.9898, 'label': 'HILLSBORO, US', 'flag': '🇺🇸'},
    'us3': {'lat': 39.0438, 'lng': -77.4875, 'label': 'ASHBURN, US', 'flag': '🇺🇸'},
    'za1': {'lat': -26.2041, 'lng': 28.0473, 'label': 'JOHANNESBURG, ZA', 'flag': '🇿🇦'},
    # Regional Defaults
    'de': NUREMBERG_DE,
    'us': NEW_YORK_US,
}


def client_is_allowed(remote_addr):
    if not remote_addr:
        return False
    try:
        remote_ip = ip_address(remote_addr)
        ipv4_mapped = getattr(remote_ip, "ipv4_mapped", None)
        if ipv4_mapped:
            remote_ip = ipv4_mapped
    except ValueError:
        return False
    return any(remote_ip in subnet for subnet in ALLOWED_NETWORKS)


def _get_direct_remote_addr():
    return MANAGEMENT_SECURITY.direct_remote_addr(request.environ)


def _management_peer_is_trusted(direct_remote_addr):
    trusted_host = os.environ.get("MANAGEMENT_TRUSTED_PROXY_HOST", "").strip()
    return MANAGEMENT_SECURITY.peer_is_trusted(direct_remote_addr, trusted_host)


def _management_host_is_allowed(raw_host):
    return MANAGEMENT_SECURITY.host_is_allowed(raw_host)


def _origin_matches_request(raw_origin):
    return MANAGEMENT_SECURITY.origin_matches(raw_origin, request.scheme, request.host)


def _management_browser_security_enabled():
    configured = app.config.get("MANAGEMENT_BROWSER_SECURITY_ENABLED")
    if configured is not None:
        if isinstance(configured, str):
            return configured.strip().lower() in ("true", "1", "yes", "on")
        return bool(configured)
    return os.environ.get("MANAGEMENT_BROWSER_SECURITY_ENABLED", "false").lower() == "true"


def _management_request_is_sensitive():
    return (
        request.path.startswith("/api/local")
        or request.path in ("/api/subscription/renew", "/api/subscription/claim")
        or request.endpoint == "check_subscription"
    )


def _management_request_changes_state():
    return request.method not in ("GET", "HEAD", "OPTIONS")


def _management_request_requires_csrf():
    # The subscription status route is a legacy GET that synchronizes paid
    # subscription data into local metadata. Treat it as a mutation without
    # changing its public method in this Umbrel-only security change.
    return _management_request_changes_state() or request.endpoint == "check_subscription"


def _management_security_error(reason):
    app.logger.warning(
        "Management request rejected: method=%s endpoint=%s remote=%s reason=%s",
        request.method,
        request.endpoint or "unknown",
        request.remote_addr or "unknown",
        reason,
    )
    response = jsonify({"success": False, "error": "Forbidden"})
    response.status_code = 403
    if reason == "csrf":
        response.headers[MANAGEMENT_CSRF_REFRESH_HEADER] = "required"
    return response


def normalize_version(raw_version):
    version_text = str(raw_version or "").strip()
    if not version_text:
        return APP_VERSION
    if version_text.startswith("v"):
        return version_text
    return f"v{version_text}"


def read_app_version():
    try:
        with open(APP_MANIFEST_PATH, "r", encoding="utf-8") as manifest_fp:
            manifest = yaml.safe_load(manifest_fp) or {}
            # Fallback to APP_VERSION if 'version' key is missing in manifest
            return normalize_version(manifest.get("version", APP_VERSION))
    except Exception:
        # Graceful fallback for all manifest-reading failures
        return APP_VERSION

def get_server_geodata(s_id):
    """Unified lookup for server coordinates, labels and flags."""
    if not s_id:
        return None

    # Matching Strategy: 
    # 1. Exact match (au1)
    # 2. Digit-stripped (us2 -> us)
    # 3. Hyphen-split component (eu-de -> de, us-east -> us)
    meta = TUNNELSATS_SERVER_METADATA.get(s_id)
    if not meta:
        prefix = re.sub(r'[0-9]', '', s_id)
        meta = TUNNELSATS_SERVER_METADATA.get(prefix)

    if not meta:
        parts = s_id.split('-')
        for p in parts:
            clean_p = re.sub(r'[0-9]', '', p)
            meta = TUNNELSATS_SERVER_METADATA.get(clean_p)
            if meta: break

    return meta


def backup_existing_wireguard_configs(excluded_files=None):
    if not os.path.exists(DATA_DIR):
        return

    excluded = set(excluded_files or ())
    for fname in os.listdir(DATA_DIR):
        if not fname.endswith(".conf") or fname in excluded:
            continue

        old_path = os.path.join(DATA_DIR, fname)
        if not os.path.isfile(old_path):
            continue

        backup_path = os.path.join(DATA_DIR, f"{fname}.bak")
        suffix = 1
        while os.path.exists(backup_path):
            backup_path = os.path.join(DATA_DIR, f"{fname}.bak.{suffix}")
            suffix += 1

        os.rename(old_path, backup_path)


def comment_out_config_lines(path, prefixes):
    if not os.path.exists(path):
        return False, False

    try:
        with open(path, "r", encoding="utf-8") as conf_fp:
            lines = conf_fp.readlines()
    except (IOError, OSError) as exc:
        app.logger.warning(f"Error reading {path} for restore: {exc}")
        return False, False

    changed = False
    updated_lines = []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("#"):
            updated_lines.append(line)
            continue

        if any(stripped.startswith(prefix) for prefix in prefixes):
            updated_lines.append(f"# {line}" if line.endswith("\n") else f"# {line}\n")
            changed = True
        else:
            updated_lines.append(line)

    if changed:
        file_mode = None
        file_uid = 1000
        file_gid = 1000
        try:
            st = os.stat(path)
            file_mode = st.st_mode & 0o777
            file_uid = st.st_uid
            file_gid = st.st_gid
        except (IOError, OSError) as exc:
            app.logger.warning(f"Error reading file stat for {path}: {exc}")

        tmp_path = os.path.join(os.path.dirname(path) or ".", f".{os.path.basename(path)}.tmp.{uuid.uuid4().hex}")
        try:
            with open(tmp_path, "w", encoding="utf-8") as conf_fp:
                conf_fp.writelines(updated_lines)
            if file_mode is not None:
                os.chmod(tmp_path, file_mode)
            try:
                os.chown(tmp_path, file_uid, file_gid)
            except OSError:
                pass
            os.replace(tmp_path, path)
        except (IOError, OSError) as exc:
            app.logger.warning(f"Error writing {path} for restore: {exc}")
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            return False, False

    return True, changed


def upsert_config_line(path: str, prefix: str, replacement_line: str) -> Tuple[bool, bool]:
    lines: List[str] = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as conf_fp:
                lines = conf_fp.readlines()
        except (IOError, OSError) as exc:
            app.logger.warning(f"Error reading {path} for configure: {exc}")
            return False, False

    changed = False
    found = False
    updated_lines = []
    normalized_line = f"{replacement_line}\n"

    for line in lines:
        line_str = line
        stripped = line_str.lstrip()
        candidate = stripped.removeprefix("#").lstrip()
        if candidate.startswith(prefix):
            if not found:
                if line != normalized_line:
                    changed = True
                updated_lines.append(normalized_line)
                found = True
            else:
                changed = True
            continue
        updated_lines.append(line)

    if not found:
        updated_lines.append(normalized_line)
        changed = True

    if changed:
        file_mode = None
        file_uid = 1000
        file_gid = 1000
        if os.path.exists(path):
            try:
                st = os.stat(path)
                file_mode = st.st_mode & 0o777
                file_uid = st.st_uid
                file_gid = st.st_gid
            except (IOError, OSError) as exc:
                app.logger.warning(f"Error reading file stat for {path}: {exc}")

        tmp_path = os.path.join(os.path.dirname(path) or ".", f".{os.path.basename(path)}.tmp.{uuid.uuid4().hex}")
        try:
            with open(tmp_path, "w", encoding="utf-8") as conf_fp:
                conf_fp.writelines(updated_lines)
            if file_mode is not None:
                os.chmod(tmp_path, file_mode)
            try:
                os.chown(tmp_path, file_uid, file_gid)
            except OSError:
                pass
            os.replace(tmp_path, path)
        except (IOError, OSError) as exc:
            app.logger.warning(f"Error writing {path} for configure: {exc}")
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            return False, False

    return True, changed


def upsert_config_lines(path: str, replacements: Iterable[Tuple[str, str]]) -> Tuple[bool, bool]:
    lines = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as conf_fp:
                lines = conf_fp.readlines()
        except (IOError, OSError) as exc:
            app.logger.warning(f"Error reading {path} for configure: {exc}")
            return False, False

    changed = False
    for prefix, replacement_line in replacements:
        found = False
        updated_lines = []
        normalized_line = f"{replacement_line}\n"

        for line in lines:
            line_str = str(line)
            stripped: str = line_str.lstrip()
            candidate: str = stripped.removeprefix("#").lstrip()
            if candidate.startswith(prefix):
                if not found:
                    if line != normalized_line:
                        changed = True
                    updated_lines.append(normalized_line)
                    found = True
                else:
                    changed = True
                continue
            updated_lines.append(line)

        if not found:
            updated_lines.append(normalized_line)
            changed = True

        lines = updated_lines

    if changed:
        file_mode = 0o600
        file_uid = 1000
        file_gid = 1000
        if os.path.exists(path):
            try:
                st = os.stat(path)
                file_mode = st.st_mode & 0o777
                file_uid = st.st_uid
                file_gid = st.st_gid
            except (IOError, OSError) as exc:
                app.logger.warning(f"Error reading file stat for {path}: {exc}")

        tmp_path = os.path.join(os.path.dirname(path) or ".", f".{os.path.basename(path)}.tmp.{uuid.uuid4().hex}")
        try:
            with open(tmp_path, "w", encoding="utf-8") as conf_fp:
                conf_fp.writelines(lines)
            try:
                os.chmod(tmp_path, file_mode)
            except OSError:
                pass
            try:
                os.chown(tmp_path, file_uid, file_gid)
            except OSError:
                pass
            os.replace(tmp_path, path)
            
            # Post-write verification
            try:
                with open(path, "r", encoding="utf-8") as verify_fp:
                    v_content = verify_fp.read()
                    for prefix, replacement_line in replacements:
                        if replacement_line not in v_content:
                            app.logger.error(f"Post-write verification failed: {replacement_line} missing in {path}")
                            return False, False
            except (IOError, OSError):
                app.logger.error(f"Post-write verification failed: Could not read {path}")
                return False, False
        except (IOError, OSError) as exc:
            app.logger.warning(f"Error writing {path} for configure: {exc}")
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            return False, False

    return True, changed


def upsert_config_line_in_section(path: str, section_header: str, prefix: str, replacement_line: str) -> Tuple[bool, bool]:
    lines: List[str] = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as conf_fp:
                lines = conf_fp.readlines()
        except (IOError, OSError) as exc:
            app.logger.warning(f"Error reading {path} for configure: {exc}")
            return False, False

    changed = False
    normalized_line = f"{replacement_line}\n"
    normalized_section = section_header.strip()
    section_match = normalized_section.lower()

    section_start = None
    section_end = len(lines)
    for idx, line in enumerate(lines):
        if line.strip().lower() != section_match:
            continue
        section_start = idx
        for next_idx in range(idx + 1, len(lines)):
            next_line = lines[next_idx].strip()
            if next_line.startswith("[") and next_line.endswith("]"):
                section_end = next_idx
                break
        break

    if section_start is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] = f"{lines[-1]}\n"
            changed = True
        if lines and lines[-1].strip():
            lines.append("\n")
        lines.append(f"{normalized_section}\n")
        lines.append(normalized_line)
        changed = True
        updated_lines = lines
    else:
        found = False
        updated_section: List[str] = []
        for line in lines[section_start + 1 : section_end]:
            line_str = line
            stripped: str = line_str.lstrip()
            candidate: str = stripped.removeprefix("#").lstrip()
            if candidate.startswith(prefix):
                if not found:
                    if line != normalized_line:
                        changed = True
                    updated_section.append(normalized_line)
                    found = True
                else:
                    changed = True
                continue
            updated_section.append(line)

        if not found:
            updated_section.append(normalized_line)
            changed = True
        
        # Explicit slicing with list cast for clarity
        head: List[str] = list(lines[: section_start + 1])
        tail: List[str] = list(lines[section_end:])
        updated_lines: List[str] = head + updated_section + tail

    if changed:
        file_mode = 0o600
        file_uid = 1000
        file_gid = 1000
        if os.path.exists(path):
            try:
                st = os.stat(path)
                file_mode = st.st_mode & 0o777
                file_uid = st.st_uid
                file_gid = st.st_gid
            except (IOError, OSError) as exc:
                app.logger.warning(f"Error reading file stat for {path}: {exc}")

        tmp_path = os.path.join(os.path.dirname(path) or ".", f".{os.path.basename(path)}.tmp.{uuid.uuid4().hex}")
        try:
            with open(tmp_path, "w", encoding="utf-8") as conf_fp:
                conf_fp.writelines(updated_lines)
            try:
                os.chmod(tmp_path, file_mode)
            except OSError:
                pass
            try:
                os.chown(tmp_path, file_uid, file_gid)
            except OSError:
                pass
            os.replace(tmp_path, path)
            
            # Post-write verification
            try:
                with open(path, "r", encoding="utf-8") as verify_fp:
                    v_content = verify_fp.read()
                    if replacement_line not in v_content:
                        app.logger.error(f"Post-write verification failed: {replacement_line} missing in {path}")
                        return False, False
            except (IOError, OSError):
                app.logger.error(f"Post-write verification failed: Could not read {path}")
                return False, False
        except (IOError, OSError) as exc:
            app.logger.warning(f"Error writing {path} for configure: {exc}")
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            return False, False

    return True, changed


def _parse_active_config_entries(lines: Iterable[str]) -> List[Dict[str, str]]:
    """Parse active key/value config lines without treating commented values as active."""
    entries: List[Dict[str, str]] = []
    for index, line in enumerate(lines):
        stripped = str(line).strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        match = re.match(r"^([A-Za-z0-9_.-]+)\s*=\s*(.*?)\s*$", stripped)
        if not match:
            continue
        entries.append({
            "index": str(index),
            "key": match.group(1).lower(),
            "value": match.group(2).strip(),
            "line": stripped,
        })
    return entries


def _address_host(address: str) -> str:
    endpoint = str(address or "").strip()
    if "@" in endpoint:
        endpoint = endpoint.rsplit("@", 1)[1]
    if endpoint.startswith("[") and "]" in endpoint:
        return endpoint[1:endpoint.index("]")].lower().rstrip(".")
    host, separator, _port = endpoint.rpartition(":")
    if separator and host:
        return host.lower().rstrip(".")
    return endpoint.lower().rstrip(".")


def _is_onion_address(address: str) -> bool:
    return _address_host(address).endswith(".onion")


def _is_expected_tunnelsats_address(address: str, dns: str, port: int) -> bool:
    endpoint = str(address or "").strip()
    if "@" in endpoint:
        endpoint = endpoint.rsplit("@", 1)[1]
    endpoint_lower = endpoint.lower().rstrip(".")
    expected_domain = f"{dns}:{port}".lower().rstrip(".")
    if endpoint_lower == expected_domain:
        return True

    try:
        if ":" in endpoint:
            host, host_port_str = endpoint.rsplit(":", 1)
            if int(host_port_str) == int(port):
                _, _, ip_list = socket.gethostbyname_ex(dns)
                if host in ip_list:
                    return True
    except Exception:
        pass

    return False


def _config_bool(value: str) -> Optional[bool]:
    token = re.split(r"\s*[#;]\s*|\s+", str(value or "").strip(), maxsplit=1)[0].lower()
    if token in ("true", "1", "yes", "on"):
        return True
    if token in ("false", "0", "no", "off"):
        return False
    return None


def _node_announcement_keys(node_type: str) -> Tuple[str, ...]:
    if node_type == "lnd":
        return ("externalip", "externalhosts", "nat")
    return ("announce-addr", "ip-discovery", "bind-addr", "always-use-proxy")


def audit_node_announcement_config(
    node_type: str,
    path: str,
    dns: str,
    port: int,
) -> Dict[str, Any]:
    """Return active announcement settings and privacy conflicts for one node config.

    ``conflicts`` keeps the historic ``key=value`` strings. ``conflict_entries`` carries
    the parsed entries (including the zero-based line index) behind each conflict so
    diagnostics can point users at the exact line to change.
    """
    try:
        with open(path, "r", encoding="utf-8") as conf_fp:
            lines = conf_fp.readlines()
    except FileNotFoundError:
        return {"readable": False, "settings": [], "conflicts": [], "conflict_entries": []}
    except (IOError, OSError) as exc:
        app.logger.warning(f"Failed to audit {node_type.upper()} announcement config {path}: {exc}")
        return {
            "readable": False,
            "settings": [],
            "conflicts": ["config-unreadable"],
            "conflict_entries": [],
        }

    keys = set(_node_announcement_keys(node_type))
    entries = [entry for entry in _parse_active_config_entries(lines) if entry["key"] in keys]
    conflicts: List[str] = []
    conflict_entries: List[Dict[str, str]] = []
    for entry in entries:
        key = entry["key"]
        value = entry["value"]
        if node_type == "lnd":
            is_conflict = (
                (key == "externalip" and not _is_onion_address(value))
                or (
                    key == "externalhosts"
                    and not (_is_onion_address(value) or _is_expected_tunnelsats_address(value, dns, port))
                )
                or (key == "nat" and _config_bool(value) is True)
            )
        else:
            is_conflict = (
                key == "announce-addr"
                and not (_is_onion_address(value) or _is_expected_tunnelsats_address(value, dns, port))
            ) or (key == "ip-discovery" and _config_bool(value) is True)
        if is_conflict:
            conflicts.append(f"{key}={value}")
            conflict_entries.append(entry)

    has_tunnelsats = False
    if dns and port:
        target_key = "externalhosts" if node_type == "lnd" else "announce-addr"
        has_tunnelsats = any(
            _is_expected_tunnelsats_address(entry["value"], dns, port)
            for entry in entries
            if entry.get("key") == target_key
        )

    return {
        "readable": True,
        "settings": entries,
        "conflicts": conflicts,
        "conflict_entries": conflict_entries,
        "has_expected_tunnelsats": has_tunnelsats,
    }


# --- Announcement diagnostics (issue #170) ----------------------------------------------
# Diagnostics never change the fail-closed policy above. They explain every conflict the
# config audit or the live gossip check found, so the dashboard can show users exactly
# which setting is wrong, why, what to change it to, and how to verify the result.

DEFAULT_LIGHTNING_PORT = 9735
UMBREL_LND_CONTAINER = "lightning_lnd_1"
UMBREL_CLN_CONTAINER = "core-lightning_lightningd_1"
UMBREL_TUNNELSATS_COMPOSE = "/home/umbrel/umbrel/app-data/tunnelsats/docker-compose.yml"
TUNNELSATS_DOMAIN_SUFFIX = ".tunnelsats.com"
DOCKER_CONTAINER_LOOKUP_COMMAND = (
    "sudo docker ps --format '{{.Names}}' | grep -iE 'lnd|lightning|tunnelsats'"
)
# Withdrawing a pinned TunnelSats address would drop a valid route, so it is excluded.
ANNOUNCEMENT_WITHDRAWABLE_KINDS = frozenset({"real-ip", "wrong-port", "stale-endpoint"})
_ANNOUNCED_HOSTNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]*$")
_ANNOUNCED_PORT_PATTERN = re.compile(r"^[0-9]{1,5}$")
_CONTAINER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

STATUS_ERROR_TARGET_SWITCH_PENDING = "target_switch_pending"
STATUS_ERROR_ANNOUNCEMENT_CONFLICT = "announcement_conflict"
STATUS_ERROR_ANNOUNCEMENT_UNVERIFIED = "announcement_unverified"
STATUS_ERROR_K3S_DNS_UNPROTECTED = "k3s_dns_unprotected"
STATUS_ERROR_NO_WIREGUARD_CONFIG = "no_wireguard_config"
STATUS_ERROR_NO_LIGHTNING_NODE = "no_lightning_node"
STATUS_ERROR_DATAPLANE = "dataplane_error"
_DATAPLANE_ERROR_CODE_PREFIXES = (
    ("No WireGuard config found", STATUS_ERROR_NO_WIREGUARD_CONFIG),
    ("SecureMode: No Lightning node detected", STATUS_ERROR_NO_LIGHTNING_NODE),
)

_SERVER_IP_CACHE_TTL_SECONDS = 60.0
_SERVER_IP_NEGATIVE_CACHE_TTL_SECONDS = 15.0
_SERVER_IP_RESOLVE_TIMEOUT_SECONDS = 1.5
_server_ip_cache: Dict[str, Tuple[float, Tuple[str, ...]]] = {}
_server_ip_cache_lock = threading.Lock()
# Dedicated worker so a hanging resolver can never starve the status probe pool.
_diagnostics_dns_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="tunnelsats-diagnostics-dns"
)


def dataplane_error_code(error: Any) -> Optional[str]:
    """Map a daemon error string to a stable code the dashboard can explain."""
    text = str(error or "").strip()
    if not text:
        return None
    for prefix, code in _DATAPLANE_ERROR_CODE_PREFIXES:
        if text.startswith(prefix):
            return code
    return STATUS_ERROR_DATAPLANE


def _normalize_host(host: Any) -> str:
    return str(host or "").strip().lower().rstrip(".")


def _ip_literal(host: Any) -> Optional[str]:
    """Return the canonical form of an IP literal, or None for hostnames.

    Scoped IPv6 literals (``fe80::1%eth0``) are rejected: ``ipaddress`` accepts any scope
    string, and Lightning announcements never carry one.
    """
    text = _normalize_host(host).strip("[]")
    if "%" in text:
        return None
    try:
        return str(ip_address(text))
    except ValueError:
        return None


def _format_endpoint(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def split_announced_endpoint(
    value: Any, default_port: int = DEFAULT_LIGHTNING_PORT
) -> Optional[Tuple[str, int, bool]]:
    """Parse an announced address into ``(host, port, port_defaulted)``.

    LND and CLN announce ``DEFAULT_LIGHTNING_PORT`` when an address has no port. Anything
    that is not a plain ``host[:port]`` returns None, so callers never build commands from
    unexpected input.
    """
    raw = str(value or "").strip()
    if "@" in raw:
        raw = raw.rsplit("@", 1)[1].strip()
    if not raw or any(char.isspace() for char in raw):
        return None

    port_text = ""
    if raw.startswith("["):
        end = raw.find("]")
        if end < 0:
            return None
        literal = _ip_literal(raw[1:end])
        remainder = raw[end + 1:]
        if literal is None or ":" not in literal:
            return None
        if remainder:
            if not remainder.startswith(":") or len(remainder) == 1:
                return None
            port_text = remainder[1:]
        host = literal
    elif raw.count(":") > 1:
        literal = _ip_literal(raw)
        if literal is None:
            return None
        host = literal
    else:
        host_part, separator, port_text = raw.partition(":")
        if separator and not port_text:
            return None
        host = _normalize_host(host_part)
        if not _ANNOUNCED_HOSTNAME_PATTERN.fullmatch(host):
            return None

    if port_text:
        # str.isdigit() also accepts characters such as "²" that int() rejects.
        if not _ANNOUNCED_PORT_PATTERN.fullmatch(port_text):
            return None
        port = int(port_text)
        port_defaulted = False
    else:
        port = int(default_port)
        port_defaulted = True
    if not 0 < port <= 65535:
        return None
    return host, port, port_defaulted


def _tunnelsats_server_ips(dns: str) -> Tuple[str, ...]:
    """Resolve the TunnelSats server's addresses with a bounded wait and a short cache."""
    host = _normalize_host(dns)
    if not host:
        return ()
    now = time.time()
    with _server_ip_cache_lock:
        cached = _server_ip_cache.get(host)
    if cached is not None:
        stamp, cached_ips = cached
        ttl = _SERVER_IP_CACHE_TTL_SECONDS if cached_ips else _SERVER_IP_NEGATIVE_CACHE_TTL_SECONDS
        if 0 <= now - stamp < ttl:
            return cached_ips

    ips: Tuple[str, ...] = ()
    try:
        future = _diagnostics_dns_executor.submit(socket.gethostbyname_ex, host)
        _name, _aliases, addresses = future.result(timeout=_SERVER_IP_RESOLVE_TIMEOUT_SECONDS)
        ips = tuple(sorted({literal for literal in map(_ip_literal, addresses) if literal}))
    except Exception as exc:
        app.logger.info(f"Could not resolve TunnelSats server {host} for diagnostics: {exc}")
    with _server_ip_cache_lock:
        _server_ip_cache[host] = (now, ips)
    return ips


def _server_host_matcher(dns: str, extra_hosts: Iterable[str] = ()) -> Callable[[str], bool]:
    """Return a predicate telling whether a host is the current TunnelSats server.

    A host matches when it is the server domain, a known endpoint host (for example the
    WireGuard endpoint from the subscription metadata), or an IP literal found in the
    server domain's DNS records. Only the TunnelSats domain is ever resolved, lazily and
    at most once per matcher; user-controlled hostnames are never looked up.
    """
    server_name = _normalize_host(dns)
    known = {server_name} if server_name else set()
    for extra in extra_hosts or ():
        normalized = _ip_literal(extra) or _normalize_host(extra)
        if normalized:
            known.add(normalized)
    resolved: List[Tuple[str, ...]] = []

    def is_server_host(host: str) -> bool:
        literal = _ip_literal(host)
        candidate = literal or _normalize_host(host)
        if not candidate:
            return False
        if candidate in known:
            return True
        if literal is None or not server_name:
            return False
        if not resolved:
            resolved.append(_tunnelsats_server_ips(server_name))
        return literal in resolved[0]

    return is_server_host


def classify_announced_address(
    value: Any,
    dns: str,
    port: Any,
    is_server_host: Optional[Callable[[str], bool]] = None,
) -> Tuple[str, Optional[str], bool]:
    """Return ``(kind, effective_address, port_defaulted)`` for one announced address."""
    parsed = split_announced_endpoint(value)
    if parsed is None:
        return "real-ip", None, False
    host, announced_port, port_defaulted = parsed
    address = _format_endpoint(host, announced_port)
    matcher = is_server_host or _server_host_matcher(dns)
    if _normalize_host(dns) and matcher(host):
        try:
            expected_port = int(port or 0)
        except (TypeError, ValueError):
            expected_port = 0
        if expected_port > 0 and announced_port != expected_port:
            return "wrong-port", address, port_defaulted
        return "pinned-ip", address, port_defaulted
    if host.endswith(TUNNELSATS_DOMAIN_SUFFIX):
        return "stale-endpoint", address, port_defaulted
    return "real-ip", address, port_defaulted


def _withdraw_command(cli_prefix: str, address: Optional[str]) -> Optional[str]:
    """Build a copy-paste LND gossip withdrawal for a validated ``host:port`` only."""
    if not cli_prefix or not address:
        return None
    parsed = split_announced_endpoint(address)
    if parsed is None or parsed[2]:
        return None
    endpoint = _format_endpoint(parsed[0], parsed[1])
    return f"{cli_prefix} lncli peers updatenodeannouncement {shlex.quote(f'--address_remove={endpoint}')}"


def _node_cli_prefix(node_type: str, target_container: str = "") -> str:
    """Shell prefix that runs a Lightning CLI inside the node container for this mode."""
    is_cln = node_type == "cln"
    if K3S_MODE:
        namespace = shlex.quote(CLN_K8S_NAMESPACE if is_cln else LND_K8S_NAMESPACE)
        selector = shlex.quote(CLN_K8S_POD_SELECTOR if is_cln else LND_K8S_POD_SELECTOR)
        return (
            f"kubectl -n {namespace} exec "
            f"$(kubectl -n {namespace} get pod -l {selector} -o name | head -n 1) --"
        )
    name = UMBREL_CLN_CONTAINER if is_cln else UMBREL_LND_CONTAINER
    candidate = str(target_container or "").strip()
    pattern = CLN_CONTAINER_PATTERN if is_cln else LND_CONTAINER_PATTERN
    # Without the Docker socket (Secure Mode) the umbrelOS default is the best we know.
    if not SECURE_MODE and _CONTAINER_NAME_PATTERN.fullmatch(candidate) and re.search(pattern, candidate):
        name = candidate
    return f"sudo docker exec {name}"


def _expected_announcement(node_type: str, dns: str, port: Any) -> Tuple[Optional[str], Optional[str]]:
    server = str(dns or "").strip()
    try:
        expected_port = int(port or 0)
    except (TypeError, ValueError):
        expected_port = 0
    if not server or expected_port <= 0:
        return None, None
    expected = f"{server}:{expected_port}"
    key = "announce-addr" if node_type == "cln" else "externalhosts"
    return expected, f"{key}={expected}"


def build_announcement_help(
    node_type: str, dns: str, port: Any, target_container: str = ""
) -> Dict[str, Any]:
    """Mode-aware, copy-paste helpers for optional SSH verification."""
    impl = "cln" if node_type == "cln" else "lnd"
    cli = "lightning-cli" if impl == "cln" else "lncli"
    expected, expected_line = _expected_announcement(impl, dns, port)
    if K3S_MODE:
        mode = "k3s"
        config_path = None
        namespace = shlex.quote(CLN_K8S_NAMESPACE if impl == "cln" else LND_K8S_NAMESPACE)
        selector = shlex.quote(CLN_K8S_POD_SELECTOR if impl == "cln" else LND_K8S_POD_SELECTOR)
        lookup_command = f"kubectl -n {namespace} get pods -l {selector} -o wide"
        grep_command = None
        logs_commands = [f"kubectl -n {shlex.quote(K8S_NAMESPACE)} logs -l app=tunnelsats --tail 100"]
    else:
        mode = "secure" if SECURE_MODE else "standard"
        config_path = resolve_node_config(impl)[1]
        lookup_command = DOCKER_CONTAINER_LOOKUP_COMMAND
        keys = "|".join(_node_announcement_keys(impl))
        grep_command = (
            f"sudo grep -nE '^[[:space:]]*({keys})[[:space:]]*=' {shlex.quote(config_path)}"
            if config_path else None
        )
        # Compose service names work whether or not the store stripped container_name.
        logs_commands = [
            f"sudo docker compose -f {UMBREL_TUNNELSATS_COMPOSE} logs --tail 100 {service}"
            for service in ("tunnelsats-daemon", "tunnelsats-web")
        ]
    return {
        "impl": impl,
        "mode": mode,
        "config_path": config_path,
        "expected": expected,
        "expected_line": expected_line,
        "verify_command": f"{_node_cli_prefix(impl, target_container)} {cli} getinfo",
        "container_lookup_command": lookup_command,
        "config_grep_command": grep_command,
        "logs_commands": logs_commands,
    }


def _announcement_diagnostic(node_type: str, kind: str, source: str, **fields: Any) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "impl": node_type,
        "kind": kind,
        "source": source,
        "setting": None,
        "value": None,
        "line": None,
        "line_number": None,
        "address": None,
        "port_defaulted": False,
        "expected": None,
        "expected_line": None,
        "config_path": None,
        "withdraw_command": None,
        "detail": None,
    }
    unknown = set(fields) - set(entry)
    if unknown:
        raise ValueError(f"Unknown announcement diagnostic fields: {sorted(unknown)}")
    entry.update(fields)
    return entry


def _announced_host_is_ip(address: Optional[str]) -> bool:
    parsed = split_announced_endpoint(address) if address else None
    return bool(parsed and _ip_literal(parsed[0]))


def build_config_announcement_diagnostics(
    node_type: str,
    audit: Dict[str, Any],
    dns: str,
    port: Any,
    *,
    detected: bool = False,
    config_path: Optional[str] = None,
    cli_prefix: str = "",
    is_server_host: Optional[Callable[[str], bool]] = None,
    check_missing_endpoint: bool = False,
) -> List[Dict[str, Any]]:
    """Explain each config-file conflict (and optionally a missing endpoint) for one node."""
    expected, expected_line = _expected_announcement(node_type, dns, port)
    common: Dict[str, Any] = {
        "expected": expected,
        "expected_line": expected_line,
        "config_path": config_path,
    }
    audit = audit if isinstance(audit, dict) else {}
    if not audit.get("readable"):
        if "config-unreadable" in (audit.get("conflicts") or []):
            return [_announcement_diagnostic(node_type, "config-unreadable", "config_file", detail="unreadable", **common)]
        if detected:
            return [_announcement_diagnostic(node_type, "config-unreadable", "config_file", detail="missing", **common)]
        return []

    matcher = is_server_host or _server_host_matcher(dns)
    diagnostics: List[Dict[str, Any]] = []
    for entry in audit.get("conflict_entries") or []:
        key = str(entry.get("key", ""))
        value = str(entry.get("value", ""))
        try:
            line_number: Optional[int] = int(entry.get("index")) + 1
        except (TypeError, ValueError):
            line_number = None
        fields: Dict[str, Any] = dict(
            common, setting=key, value=value, line=entry.get("line"), line_number=line_number
        )
        if key in ("nat", "ip-discovery"):
            diagnostics.append(_announcement_diagnostic(node_type, "auto-discovery", "config_file", **fields))
            continue
        kind, address, port_defaulted = classify_announced_address(value, dns, port, matcher)
        withdraw_command = None
        # LND announces configured hostnames as resolved IPs, so only IP literals map
        # one-to-one onto a live gossip entry that can be withdrawn exactly.
        if node_type == "lnd" and kind in ANNOUNCEMENT_WITHDRAWABLE_KINDS and _announced_host_is_ip(address):
            withdraw_command = _withdraw_command(cli_prefix, address)
        diagnostics.append(_announcement_diagnostic(
            node_type, kind, "config_file",
            address=address, port_defaulted=port_defaulted, withdraw_command=withdraw_command, **fields,
        ))

    if check_missing_endpoint and not audit.get("has_expected_tunnelsats"):
        diagnostics.append(_announcement_diagnostic(
            node_type, "missing-endpoint", "config_file",
            setting="announce-addr" if node_type == "cln" else "externalhosts",
            detail=None if expected else "endpoint_unknown",
            **common,
        ))
    return diagnostics


def build_live_announcement_diagnostics(
    node_type: str,
    remaining: Iterable[Any],
    dns: str,
    port: Any,
    *,
    config_path: Optional[str] = None,
    cli_prefix: str = "",
    is_server_host: Optional[Callable[[str], bool]] = None,
) -> List[Dict[str, Any]]:
    """Explain what the live gossip check could not clean up or verify."""
    expected, expected_line = _expected_announcement(node_type, dns, port)
    common: Dict[str, Any] = {
        "expected": expected,
        "expected_line": expected_line,
        "config_path": config_path,
    }
    pending_states = {f"{node_type}_initializing", f"{node_type}_not_found"}
    matcher = is_server_host
    diagnostics: List[Dict[str, Any]] = []
    for item in remaining or []:
        text = str(item or "").strip()
        if not text or text == "secure_mode_manual_required":
            continue
        if text in pending_states:
            diagnostics.append(_announcement_diagnostic(
                node_type, "verification-pending", "live_gossip", detail=text, **common
            ))
            continue
        if text.startswith("missing expected announcement"):
            diagnostics.append(_announcement_diagnostic(
                node_type, "missing-endpoint", "live_gossip",
                detail=None if expected else "endpoint_unknown", **common,
            ))
            continue
        if matcher is None:
            matcher = _server_host_matcher(dns)
        kind, address, port_defaulted = classify_announced_address(text, dns, port, matcher)
        withdraw_command = (
            _withdraw_command(cli_prefix, address)
            if node_type == "lnd" and kind in ANNOUNCEMENT_WITHDRAWABLE_KINDS
            else None
        )
        diagnostics.append(_announcement_diagnostic(
            node_type, kind, "live_gossip",
            value=text, address=address, port_defaulted=port_defaulted,
            withdraw_command=withdraw_command, **common,
        ))
    return diagnostics


def _diagnostics_or_empty(build: Callable[[], List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Diagnostics explain the fail-closed verdict; failing to explain must never break it."""
    try:
        return build()
    except Exception:
        app.logger.exception("Failed to build announcement diagnostics; continuing without them")
        return []


def _announcement_help_or_empty(node_type: str, dns: str, port: Any, target_container: str = "") -> Dict[str, Any]:
    try:
        return build_announcement_help(node_type, dns, port, target_container)
    except Exception:
        app.logger.exception("Failed to build announcement help; continuing without it")
        return {}


def _confirmation_diagnostics(
    node_type: str, audit: Dict[str, Any], dns: str, port: Any, meta: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Diagnostics shown before standard mode backs up and disables conflicting lines."""
    return _diagnostics_or_empty(lambda: build_config_announcement_diagnostics(
        node_type, audit, dns, port,
        config_path=None if K3S_MODE else resolve_node_config(node_type)[1],
        cli_prefix=_node_cli_prefix(node_type, str(read_dataplane_state().get("target_container") or "")),
        is_server_host=_server_host_matcher(
            dns, extra_hosts=[_address_host(str(meta.get("wgEndpoint") or ""))]
        ),
    ))


def _write_config_lines_atomic(path: str, lines: List[str]) -> bool:
    file_mode = 0o600
    file_uid = 1000
    file_gid = 1000
    if os.path.exists(path):
        try:
            stat_result = os.stat(path)
            file_mode = stat_result.st_mode & 0o777
            file_uid = stat_result.st_uid
            file_gid = stat_result.st_gid
        except (IOError, OSError) as exc:
            app.logger.warning(f"Failed to read config ownership for {path}: {exc}")

    parent_dir = os.path.dirname(path) or "."
    tmp_path = os.path.join(parent_dir, f".{os.path.basename(path)}.tmp.{uuid.uuid4().hex}")
    try:
        os.makedirs(parent_dir, exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as conf_fp:
            conf_fp.writelines(lines)
        os.chmod(tmp_path, file_mode)
        try:
            os.chown(tmp_path, file_uid, file_gid)
        except OSError:
            pass
        os.replace(tmp_path, path)
        return True
    except (IOError, OSError) as exc:
        app.logger.warning(f"Failed to atomically update node config {path}: {exc}")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        return False


def _ensure_lnd_application_lines(lines: List[str], desired: List[str]) -> List[str]:
    section_start = None
    section_end = len(lines)
    for index, line in enumerate(lines):
        if line.strip().lower() != "[application options]":
            continue
        section_start = index
        for next_index in range(index + 1, len(lines)):
            candidate = lines[next_index].strip()
            if candidate.startswith("[") and candidate.endswith("]"):
                section_end = next_index
                break
        break
    if section_start is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        if lines and lines[-1].strip():
            lines.append("\n")
        lines.extend(["[Application Options]\n", *[f"{line}\n" for line in desired]])
        return lines
    if section_end > 0 and not lines[section_end - 1].endswith("\n"):
        lines[section_end - 1] += "\n"
    lines[section_end:section_end] = [f"{line}\n" for line in desired]
    return lines


def apply_node_announcement_protection(
    node_type: str,
    path: str,
    dns: str,
    port: int,
    retain_tor: bool,
) -> Tuple[bool, bool, List[str]]:
    """Disable conflicting settings and install the TunnelSats announcement atomically."""
    try:
        with open(path, "r", encoding="utf-8") as conf_fp:
            original_lines = conf_fp.readlines()
    except FileNotFoundError:
        original_lines = []
    except (IOError, OSError) as exc:
        app.logger.warning(f"Failed to read {node_type.upper()} config for protection: {exc}")
        return False, False, []

    entries = _parse_active_config_entries(original_lines)
    relevant_keys = set(_node_announcement_keys(node_type))
    backup_lines = [entry["line"] for entry in entries if entry["key"] in relevant_keys]
    expected = f"{dns}:{port}"
    kept: set = set()
    updated_lines: List[str] = []

    for index, line in enumerate(original_lines):
        entry = next((item for item in entries if int(item["index"]) == index), None)
        if entry is None or entry["key"] not in relevant_keys:
            updated_lines.append(line)
            continue

        key = entry["key"]
        value = entry["value"]
        keep = False
        mark_kept = True
        if node_type == "lnd":
            if key == "externalhosts" and _is_expected_tunnelsats_address(value, dns, port):
                keep = "externalhosts" not in kept
            elif key == "externalip" and retain_tor and _is_onion_address(value):
                keep = True
                mark_kept = False
            elif key == "externalhosts" and retain_tor and _is_onion_address(value):
                keep = True
                mark_kept = False
            elif key == "nat" and _config_bool(value) is False:
                keep = "nat" not in kept
        else:
            if key == "announce-addr" and _is_expected_tunnelsats_address(value, dns, port):
                keep = "announce-addr" not in kept
            elif key == "announce-addr" and retain_tor and _is_onion_address(value):
                keep = True
                mark_kept = False
            elif key == "ip-discovery" and _config_bool(value) is False:
                keep = "ip-discovery" not in kept
            elif key == "bind-addr" and value == "0.0.0.0:9736":
                keep = "bind-addr" not in kept
            elif key == "always-use-proxy" and value.lower() == "false":
                keep = "always-use-proxy" not in kept

        if keep:
            if mark_kept:
                kept.add(key)
            updated_lines.append(line)
        else:
            newline = "\n" if line.endswith("\n") else ""
            updated_lines.append(f"# TunnelSats disabled: {entry['line']}{newline}")

    desired: List[str] = []
    if node_type == "lnd":
        if "externalhosts" not in kept:
            desired.append(f"externalhosts={expected}")
        if "nat" not in kept:
            desired.append("nat=false")
        updated_lines = _ensure_lnd_application_lines(updated_lines, desired)
    else:
        if "announce-addr" not in kept:
            desired.append(f"announce-addr={expected}")
        if "ip-discovery" not in kept:
            desired.append("ip-discovery=false")
        if "bind-addr" not in kept:
            desired.append("bind-addr=0.0.0.0:9736")
        if "always-use-proxy" not in kept:
            desired.append("always-use-proxy=false")
        if updated_lines and not updated_lines[-1].endswith("\n"):
            updated_lines[-1] += "\n"
        updated_lines.extend(f"{line}\n" for line in desired)

    changed = updated_lines != original_lines
    if changed and not _write_config_lines_atomic(path, updated_lines):
        return False, False, backup_lines
    return True, changed, backup_lines


def restore_node_announcement_config(
    node_type: str,
    path: str,
    backup_lines: List[str],
    dns: str,
    port: int,
) -> Tuple[bool, bool]:
    """Remove managed values and reactivate exactly the settings captured before setup."""
    if not os.path.exists(path):
        return False, False
    try:
        with open(path, "r", encoding="utf-8") as conf_fp:
            original_lines = conf_fp.readlines()
    except (IOError, OSError) as exc:
        app.logger.warning(f"Failed to read {node_type.upper()} config for restore: {exc}")
        return False, False

    expected_counts: Dict[str, int] = {}
    for saved_line in backup_lines:
        expected_counts[saved_line] = expected_counts.get(saved_line, 0) + 1
    restored_counts: Dict[str, int] = {}
    expected_endpoint = f"{dns}:{port}"
    updated_lines: List[str] = []

    for line in original_lines:
        stripped = line.strip()
        tagged = re.match(r"^#\s*TunnelSats disabled:\s*(.*?)\s*$", stripped)
        if tagged:
            saved = tagged.group(1)
            if restored_counts.get(saved, 0) < expected_counts.get(saved, 0):
                updated_lines.append(f"{saved}\n")
                restored_counts[saved] = restored_counts.get(saved, 0) + 1
            else:
                updated_lines.append(line)
            continue

        parsed = _parse_active_config_entries([line])
        if not parsed:
            updated_lines.append(line)
            continue
        entry = parsed[0]
        saved = entry["line"]
        if restored_counts.get(saved, 0) < expected_counts.get(saved, 0):
            updated_lines.append(line)
            restored_counts[saved] = restored_counts.get(saved, 0) + 1
            continue

        managed = False
        if node_type == "lnd":
            managed = (
                entry["key"] == "externalhosts"
                and _is_expected_tunnelsats_address(entry["value"], dns, port)
            ) or (entry["key"] == "nat" and _config_bool(entry["value"]) is False)
        else:
            managed = (
                entry["key"] == "announce-addr"
                and entry["value"].lower() == expected_endpoint.lower()
            ) or (
                entry["key"] == "ip-discovery" and _config_bool(entry["value"]) is False
            ) or (
                entry["key"] == "bind-addr" and entry["value"] == "0.0.0.0:9736"
            ) or (
                entry["key"] == "always-use-proxy" and entry["value"].lower() == "false"
            )
        if managed:
            newline = "\n" if line.endswith("\n") else ""
            updated_lines.append(f"# TunnelSats disabled managed: {entry['line']}{newline}")
        else:
            updated_lines.append(line)

    for saved_line, count in expected_counts.items():
        missing = count - restored_counts.get(saved_line, 0)
        updated_lines.extend(f"{saved_line}\n" for _ in range(max(0, missing)))

    changed = updated_lines != original_lines
    if changed and not _write_config_lines_atomic(path, updated_lines):
        return False, False
    return True, changed


def _parse_config_comments(config_text):
    meta: Dict[str, Any] = {}
    for line in config_text.split("\n"):
        line = line.strip()
        if match := re.match(r"^#\s*Port Forwarding:\s*(\d+)", line):
            meta["vpnPort"] = int(match.group(1))
        elif match := re.match(r"^#\s*VPNPort\s*(?:[:=]\s*|\s+)(\d+)", line):
            meta["vpnPort"] = int(match.group(1))
        elif match := re.match(r"^#\s*Server:\s*(.+)", line):
            meta["serverDomain"] = match.group(1).strip()
        elif match := re.match(r"^#\s*myPubKey:\s*(.+)", line):
            meta["wgPublicKey"] = match.group(1).strip()
        elif match := re.match(r"^#\s*Valid Until:\s*(.+)", line):
            meta["expiresAt"] = match.group(1).strip()
        elif match := re.match(r"^Endpoint\s*=\s*(.+)", line):
            endpoint_val = match.group(1).strip()
            meta["wgEndpoint"] = endpoint_val
            if ":" in endpoint_val:
                meta.setdefault("serverDomain", endpoint_val.rsplit(":", 1)[0])
        elif match := re.match(r"^PresharedKey\s*=\s*(.+)", line):
            meta["presharedKey"] = match.group(1).strip()
        elif match := re.match(r"^Address\s*=\s*(.+)", line):
            meta["peerAddress"] = match.group(1).strip()
    return meta


def _write_file_secure(path, content):
    parent_dir = os.path.dirname(path) or "."
    os.makedirs(parent_dir, exist_ok=True)
    tmp_path = os.path.join(parent_dir, f".{os.path.basename(path)}.tmp.{uuid.uuid4().hex}")
    with open(tmp_path, "w", encoding="utf-8") as fp:
        fp.write(content)
    os.chmod(tmp_path, 0o600)
    try:
        os.chown(tmp_path, 1000, 1000)
    except OSError:
        pass
    os.replace(tmp_path, path)


def _persist_tunnelsats_config_and_meta(config_text: str, meta: dict) -> bool:
    """Saves the WG config and metadata to disk using atomic writes and automatic .bak rotation."""
    with _metadata_lock:
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            backup_existing_wireguard_configs()
            conf_path = os.path.join(DATA_DIR, "tunnelsats.conf")
            meta_path = os.path.join(DATA_DIR, META_FILE)
            _write_file_secure(conf_path, config_text)
            _write_file_secure(meta_path, json.dumps(meta, indent=2))
            return True
        except (IOError, OSError) as exc:
            app.logger.error(f"Failed to persist tunnelsats.conf and metadata: {exc}")
            return False


def _set_restart_pending(meta_path, _ignored_meta, key, is_pending):
    with _metadata_lock:
        if not os.path.exists(meta_path):
            return False
        try:
            with open(meta_path, "r", encoding="utf-8") as fp:
                meta = json.load(fp)
        except (IOError, OSError, json.JSONDecodeError):
            return False

        if not isinstance(meta, dict):
            return False

        has_key = key in meta
        current_pending = bool(meta.get(key))

        if is_pending:
            if has_key and current_pending:
                return True
            meta[key] = True
        else:
            if not has_key:
                return True
            meta.pop(key, None)

        try:
            _write_file_secure(meta_path, json.dumps(meta, indent=2))
            return True
        except (IOError, OSError) as exc:
            app.logger.warning(f"Failed to persist restart-pending state for {key}: {exc}")
            return False


def _update_announcement_metadata(
    meta_path: str,
    node_type: str,
    backup_lines: Optional[List[str]] = None,
    verification: Optional[Dict[str, Any]] = None,
    clear_backup: bool = False,
) -> bool:
    with _metadata_lock:
        try:
            with open(meta_path, "r", encoding="utf-8") as meta_fp:
                meta = json.load(meta_fp)
        except (IOError, OSError, json.JSONDecodeError):
            return False
        if not isinstance(meta, dict):
            return False

        backup_config = meta.get("backupConfig")
        if not isinstance(backup_config, dict):
            backup_config = {}
        if backup_lines is not None and node_type not in backup_config:
            backup_config[node_type] = {
                "lines": list(backup_lines),
                "capturedAt": datetime.now(timezone.utc).isoformat(),
            }
        if clear_backup:
            backup_config.pop(node_type, None)
        if backup_config:
            meta["backupConfig"] = backup_config
        else:
            meta.pop("backupConfig", None)

        if node_type in ("lnd", "cln"):
            key = f"{node_type}AnnouncementVerification"
            if verification is None:
                meta.pop(key, None)
            else:
                meta[key] = verification
        try:
            _write_file_secure(meta_path, json.dumps(meta, indent=2))
            return True
        except (IOError, OSError) as exc:
            app.logger.warning(f"Failed to persist announcement metadata: {exc}")
            return False


def _is_metadata_writable(meta_path: str) -> bool:
    try:
        if not os.path.exists(meta_path):
            return False
        with open(meta_path, "r+", encoding="utf-8") as fp:
            pass
        return True
    except (IOError, OSError):
        return False


def _persist_node_type(meta_path: str, node_type: Optional[str]) -> bool:
    """Persist the selected nodeType ('lnd' or 'cln') to metadata upon successful configuration."""
    with _metadata_lock:
        try:
            with open(meta_path, "r", encoding="utf-8") as fp:
                meta = json.load(fp)
            if not isinstance(meta, dict):
                return False
            normalized = str(node_type).strip().lower() if node_type else ""
            current = str(meta.get("nodeType", "")).strip().lower()
            if current != normalized:
                if normalized:
                    meta["nodeType"] = normalized
                else:
                    meta.pop("nodeType", None)
                _write_file_secure(meta_path, json.dumps(meta, indent=2))
            return True
        except (IOError, OSError, json.JSONDecodeError) as exc:
            app.logger.error(f"Failed to persist nodeType {node_type} to metadata: {exc}")
            return False


def _backup_lines_from_meta(meta: Dict[str, Any], node_type: str) -> Optional[List[str]]:
    backup_config = meta.get("backupConfig")
    if not isinstance(backup_config, dict):
        return None
    node_backup = backup_config.get(node_type)
    if not isinstance(node_backup, dict):
        return None
    lines = node_backup.get("lines")
    if not isinstance(lines, list) or not all(isinstance(line, str) for line in lines):
        return None
    return lines


def _has_required_wireguard_blocks(config_text):
    has_interface = bool(re.search(r"^\s*\[Interface\]\s*$", config_text, flags=re.IGNORECASE | re.MULTILINE))
    has_peer = bool(re.search(r"^\s*\[Peer\]\s*$", config_text, flags=re.IGNORECASE | re.MULTILINE))
    return has_interface and has_peer


WIREGUARD_SECTION_NAMES = {"interface": "Interface", "peer": "Peer"}
WIREGUARD_ALLOWED_KEYS = {
    "interface": {
        "address": "Address",
        "dns": "DNS",
        "listenport": "ListenPort",
        "mtu": "MTU",
        "privatekey": "PrivateKey",
    },
    "peer": {
        "allowedips": "AllowedIPs",
        "endpoint": "Endpoint",
        "persistentkeepalive": "PersistentKeepalive",
        "presharedkey": "PresharedKey",
        "publickey": "PublicKey",
    },
}
WIREGUARD_OUTPUT_ORDER = {
    "interface": ("PrivateKey", "Address", "DNS", "ListenPort", "MTU"),
    "peer": ("PublicKey", "PresharedKey", "AllowedIPs", "Endpoint", "PersistentKeepalive"),
}
WIREGUARD_EXEC_DIRECTIVES = {"preup", "postup", "predown", "postdown"}


def _sanitize_wireguard_config(config_text: str) -> Tuple[Optional[str], Optional[str]]:
    comments: List[str] = []
    sections: Dict[str, Dict[str, str]] = {}
    current_section: Optional[str] = None

    for line_number, raw_line in enumerate(config_text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("#") or line.startswith(";"):
            comments.append(line)
            continue

        section_match = re.match(r"^\[([A-Za-z][A-Za-z0-9_-]*)\]$", line)
        if section_match:
            section = section_match.group(1).lower()
            if section not in WIREGUARD_SECTION_NAMES:
                return None, f"Unsupported WireGuard section [{section_match.group(1)}]."
            if section in sections:
                return None, f"Duplicate WireGuard section [{WIREGUARD_SECTION_NAMES[section]}]."
            sections[section] = {}
            current_section = section
            continue

        directive_match = re.match(r"^([A-Za-z][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if not directive_match:
            return None, f"Invalid WireGuard directive on line {line_number}."
        if current_section is None:
            return None, f"WireGuard directive appears before a section on line {line_number}."

        key_raw = directive_match.group(1)
        key = key_raw.lower()
        if key in WIREGUARD_EXEC_DIRECTIVES:
            return None, f"Unsafe WireGuard directive {key_raw} is not allowed."
        canonical_key = WIREGUARD_ALLOWED_KEYS[current_section].get(key)
        if canonical_key is None:
            return None, f"Unsupported WireGuard directive {key_raw} in [{WIREGUARD_SECTION_NAMES[current_section]}]."
        if canonical_key in sections[current_section]:
            return None, f"Duplicate WireGuard directive {canonical_key} in [{WIREGUARD_SECTION_NAMES[current_section]}]."

        value = directive_match.group(2).strip()
        if not value:
            return None, f"WireGuard directive {canonical_key} cannot be empty."
        sections[current_section][canonical_key] = value

    if "interface" not in sections or "peer" not in sections:
        return None, "Invalid WireGuard configuration format. Missing [Interface] or [Peer] block."
    if not sections["interface"].get("PrivateKey"):
        return None, "Invalid WireGuard configuration format. Missing Interface PrivateKey."
    if not sections["peer"].get("PublicKey"):
        return None, "Invalid WireGuard configuration format. Missing Peer PublicKey."

    output_lines: List[str] = []
    output_lines.extend(comments)
    if output_lines:
        output_lines.append("")

    for section in ("interface", "peer"):
        output_lines.append(f"[{WIREGUARD_SECTION_NAMES[section]}]")
        for key in WIREGUARD_OUTPUT_ORDER[section]:
            value = sections[section].get(key)
            if value is not None:
                output_lines.append(f"{key} = {value}")
        if section != "peer":
            output_lines.append("")

    return "\n".join(output_lines) + "\n", None


def _extract_interface_private_key(config_text):
    in_interface = False
    for raw_line in config_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        section_match = re.match(r"^\s*\[([^\]]+)\]\s*$", line)
        if section_match:
            in_interface = section_match.group(1).strip().lower() == "interface"
            continue

        if in_interface:
            private_key_match = re.match(r"^PrivateKey\s*=\s*(.+)$", line, flags=re.IGNORECASE)
            if private_key_match:
                return private_key_match.group(1).strip()

    return ""


def _ensure_peer_persistent_keepalive(config_text, keepalive=25):
    lines = config_text.splitlines()
    if not lines:
        return config_text

    updated_lines: List[str] = []
    in_peer = False
    peer_has_keepalive = False

    for line in lines:
        stripped = line.strip()
        section_match = re.match(r"^\[([^\]]+)\]\s*$", stripped)
        if section_match:
            if in_peer and not peer_has_keepalive:
                updated_lines.append(f"PersistentKeepalive = {keepalive}")

            in_peer = section_match.group(1).strip().lower() == "peer"
            peer_has_keepalive = False
            updated_lines.append(line)
            continue

        if in_peer and re.match(r"^PersistentKeepalive\s*=", stripped, flags=re.IGNORECASE):
            peer_has_keepalive = True

        updated_lines.append(line)

    if in_peer and not peer_has_keepalive:
        updated_lines.append(f"PersistentKeepalive = {keepalive}")

    normalized = "\n".join(updated_lines)
    if config_text.endswith("\n"):
        normalized += "\n"
    return normalized


def _derive_wg_public_key(private_key):
    private_key = str(private_key or "").strip()
    if not private_key or len(private_key) > 1024:
        return ""

    try:
        result = subprocess.run(
            ["wg", "pubkey"],
            input=private_key,
            text=True,
            capture_output=True,
            check=True,
            timeout=5,
        )
        return result.stdout.strip()
    except (subprocess.SubprocessError, OSError) as exc:
        app.logger.warning(f"Failed to derive WireGuard public key: {exc}")
        return ""


def _get_wireguard_state(interface_name: str = WG_INTERFACE_NAME) -> Tuple[str, str]:
    wg_status = "Disconnected"
    wg_pubkey = ""

    try:
        output = subprocess.check_output(
            ["wg", "show", interface_name],
            stderr=subprocess.STDOUT,
            timeout=5,
        ).decode("utf-8")
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        app.logger.debug(f"WireGuard state check info (expected failure): {exc}")
        return wg_status, wg_pubkey
    except Exception as exc:
        app.logger.warning(f"Unexpected error running 'wg show {interface_name}': {exc}")
        return wg_status, wg_pubkey

    if f"interface: {interface_name}" not in output:
        return wg_status, wg_pubkey

    for line in output.splitlines():
        if line.strip().startswith("public key:"):
            wg_pubkey = line.split(":", 1)[1].strip()
            break

    try:
        handshakes_output = subprocess.check_output(
            ["wg", "show", interface_name, "latest-handshakes"],
            stderr=subprocess.STDOUT,
            timeout=5,
        ).decode("utf-8")
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        app.logger.debug(f"WireGuard latest-handshakes info (expected failure): {exc}")
        return wg_status, wg_pubkey
    except Exception as exc:
        app.logger.warning(f"Unexpected error reading WireGuard latest-handshakes: {exc}")
        return wg_status, wg_pubkey

    now_epoch = int(time.time())
    for line in handshakes_output.splitlines():
        parts = line.strip().split()
        if len(parts) < 2:
            continue

        try:
            latest_handshake_epoch = int(parts[-1])
        except ValueError:
            continue

        if latest_handshake_epoch <= 0:
            continue

        if 0 <= (now_epoch - latest_handshake_epoch) <= WG_HANDSHAKE_MAX_AGE_SECONDS:
            wg_status = "Connected"
            break

    return wg_status, wg_pubkey


def _server_id_from_domain(server_domain):
    server_domain = str(server_domain or "").strip()
    if not server_domain:
        return "unknown"

    server_id = secure_filename(server_domain.split(".", 1)[0])
    return server_id or "unknown"


def _port_from_endpoint(endpoint):
    endpoint = str(endpoint or "").strip()
    if not endpoint:
        return 0

    try:
        candidate = endpoint.rsplit(":", 1)[1]
    except IndexError:
        return 0

    if candidate.isdigit():
        return int(candidate)
    return 0


def docker_api(path):
    if not os.path.exists(DOCKER_SOCK):
        return None
    try:
        out = subprocess.check_output(
            ["curl", "-sS", "--fail", "--unix-socket", DOCKER_SOCK, f"http://localhost{path}"],
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return json.loads(out.decode("utf-8"))
    except (subprocess.SubprocessError, json.JSONDecodeError, FileNotFoundError, TimeoutError, OSError) as exc:
        app.logger.warning(f"Docker API call failed for path {path}: {exc}")
        return None


def docker_api_post(path):
    if not os.path.exists(DOCKER_SOCK):
        return False
    try:
        subprocess.check_output(
            ["curl", "-sS", "--fail", "-X", "POST", "--unix-socket", DOCKER_SOCK, f"http://localhost{path}"],
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        return True
    except (subprocess.SubprocessError, FileNotFoundError, TimeoutError, OSError) as exc:
        app.logger.warning(f"Docker API POST failed for path {path}: {exc}")
        return False


def docker_exec(container_id: str, command: List[str]) -> Tuple[bool, str]:
    """Execute a fixed argv inside a container through the mounted Docker socket."""
    if not os.path.exists(DOCKER_SOCK) or not re.fullmatch(r"[A-Za-z0-9_.-]+", container_id or ""):
        return False, ""
    create_payload = json.dumps({
        "AttachStdout": True,
        "AttachStderr": True,
        "Tty": True,
        "Cmd": [str(part) for part in command],
    })
    try:
        create_result = subprocess.run(
            [
                "curl", "-sS", "--fail", "-X", "POST", "--unix-socket", DOCKER_SOCK,
                "-H", "Content-Type: application/json", "--data-binary", create_payload,
                f"http://localhost/containers/{container_id}/exec",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if create_result.returncode != 0:
            return False, create_result.stderr.strip()
        exec_id = str(json.loads(create_result.stdout).get("Id", ""))
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", exec_id):
            return False, ""

        start_result = subprocess.run(
            [
                "curl", "-sS", "--fail", "-X", "POST", "--unix-socket", DOCKER_SOCK,
                "-H", "Content-Type: application/json", "--data-binary",
                json.dumps({"Detach": False, "Tty": True}),
                f"http://localhost/exec/{exec_id}/start",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        inspect_result = subprocess.run(
            [
                "curl", "-sS", "--fail", "--unix-socket", DOCKER_SOCK,
                f"http://localhost/exec/{exec_id}/json",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if start_result.returncode != 0 or inspect_result.returncode != 0:
            return False, (start_result.stdout + start_result.stderr).strip()
        inspect_payload = json.loads(inspect_result.stdout)
        return inspect_payload.get("ExitCode") == 0, start_result.stdout.strip()
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
        app.logger.warning(f"Docker exec failed for container {container_id[:12]}: {exc}")
        return False, ""


def _parse_lnd_getinfo_uris(output: str) -> Optional[List[str]]:
    raw = str(output or "").replace("\r", "")
    start = raw.find("{")
    if start < 0:
        return None
    try:
        payload, _end = json.JSONDecoder().raw_decode(raw[start:])
    except json.JSONDecodeError:
        return None
    uris = payload.get("uris")
    if not isinstance(uris, list) or not all(isinstance(uri, str) for uri in uris):
        return None
    addresses = []
    for uri in uris:
        endpoint = uri.rsplit("@", 1)[-1].strip()
        if endpoint and endpoint not in addresses:
            addresses.append(endpoint)
    return addresses


def _query_lnd_addresses(container_id: str, attempts: int = 5) -> Optional[List[str]]:
    """Allow LND a short readiness window after its required restart."""
    for attempt in range(max(1, attempts)):
        ok, output = docker_exec(container_id, ["lncli", "getinfo"])
        addresses = _parse_lnd_getinfo_uris(output) if ok else None
        if addresses is not None:
            return addresses
        if attempt + 1 < attempts:
            time.sleep(2)
    return None


def clean_and_verify_lnd_announcements(
    dns: str,
    port: int,
    retain_tor: bool,
) -> Tuple[bool, List[str], List[str]]:
    """Withdraw live non-TunnelSats clearnet LND URIs, add missing TunnelSats URI live, then verify."""
    if SECURE_MODE or K3S_MODE:
        return False, [], ["secure_mode_manual_required"]

    container_id = container_id_by_match(LND_CONTAINER_PATTERN)
    if not container_id:
        return False, [], ["lnd_not_found"]

    addresses = _query_lnd_addresses(container_id)
    if addresses is None:
        return False, [], ["lnd_initializing"]

    conflicts = [
        address for address in addresses
        if not _is_expected_tunnelsats_address(address, dns, port)
        and not (retain_tor and _is_onion_address(address))
    ]
    removed: List[str] = []
    for address in conflicts:
        removed_ok, _ = docker_exec(
            container_id,
            ["lncli", "peers", "updatenodeannouncement", f"--address_remove={address}"],
        )
        if not removed_ok:
            return False, removed, conflicts
        removed.append(address)

    # If expected TunnelSats endpoint is not yet in live gossip, add it live via RPC
    has_tunnelsats = any(
        _is_expected_tunnelsats_address(address, dns, port)
        for address in addresses
    )
    if not has_tunnelsats:
        docker_exec(
            container_id,
            ["lncli", "peers", "updatenodeannouncement", f"--address_add={dns}:{port}"],
        )

    verified_addresses = _query_lnd_addresses(container_id)
    if verified_addresses is None:
        return False, removed, ["lnd_initializing"]

    remaining = [
        address for address in verified_addresses
        if not _is_expected_tunnelsats_address(address, dns, port)
        and not (retain_tor and _is_onion_address(address))
    ]
    if not any(
        _is_expected_tunnelsats_address(address, dns, port)
        for address in verified_addresses
    ):
        remaining.append(f"missing expected announcement {dns}:{port}")
    return not remaining, removed, remaining
def _parse_cln_getinfo_addresses(raw: str) -> Optional[List[str]]:
    """Parse output from CLN `lightning-cli getinfo` into a list of address:port strings."""
    if not raw or not isinstance(raw, str):
        return None
    start = raw.find("{")
    if start < 0:
        return None
    try:
        payload, _end = json.JSONDecoder().raw_decode(raw[start:])
    except json.JSONDecodeError:
        return None
    addr_list = payload.get("address")
    if not isinstance(addr_list, list):
        return None
    addresses = []
    for item in addr_list:
        if isinstance(item, dict):
            addr = str(item.get("address", "")).strip()
            port = item.get("port")
            if addr and port:
                # CLN lists IPv6 hosts without brackets. Bracket them (as LND URIs do) so the
                # port stays unambiguous: "2001:db8::1:9735" is itself a valid IPv6 address.
                endpoint = _format_endpoint(addr.strip("[]"), port)
                if endpoint not in addresses:
                    addresses.append(endpoint)
    return addresses


def _query_cln_addresses(container_id: str, attempts: int = 5) -> Optional[List[str]]:
    """Allow CLN a short readiness window after its required restart."""
    for attempt in range(max(1, attempts)):
        ok, output = docker_exec(container_id, ["lightning-cli", "getinfo"])
        addresses = _parse_cln_getinfo_addresses(output) if ok else None
        if addresses is not None:
            return addresses
        if attempt + 1 < attempts:
            time.sleep(2)
    return None


def clean_and_verify_cln_announcements(
    dns: str,
    port: int,
    retain_tor: bool,
) -> Tuple[bool, List[str], List[str]]:
    """Verify live CLN announcements via lightning-cli getinfo RPC in non-secure mode."""
    if SECURE_MODE or K3S_MODE:
        return False, [], ["secure_mode_manual_required"]

    container_id = container_id_by_match(CLN_CONTAINER_PATTERN)
    if not container_id:
        return False, [], ["cln_not_found"]

    addresses = _query_cln_addresses(container_id)
    if addresses is None:
        return False, [], ["cln_initializing"]

    conflicts = [
        address for address in addresses
        if not _is_expected_tunnelsats_address(address, dns, port)
        and not (retain_tor and _is_onion_address(address))
    ]
    if not any(
        _is_expected_tunnelsats_address(address, dns, port)
        for address in addresses
    ):
        conflicts.append(f"missing expected announcement {dns}:{port}")
    return not conflicts, [], conflicts


def _k8s_list_pods_in_namespace(ns, token):
    """Single-namespace fetch helper used by k8s_list_pods()."""
    url = f"https://kubernetes.default.svc/api/v1/namespaces/{ns}/pods"
    resp = _k8s_session.get(url, headers={"Authorization": f"Bearer {token}"}, verify=K8S_SA_CA_PATH, timeout=5)
    resp.raise_for_status()
    out = []
    for pod in resp.json().get("items", []):
        meta = pod.get("metadata", {})
        status = pod.get("status", {})
        if status.get("phase") != "Running":
            continue
        pod_name = meta.get("name", "")
        pod_ip = status.get("podIP", "")
        out.append({
            "Names": [f"/{pod_name}"],
            "Id": meta.get("uid", pod_name),
            "NetworkSettings": {"Networks": {"k8s": {"IPAddress": pod_ip}}},
        })
    return out


def k8s_list_pods():
    """Fetch running pods across the tunnelsats namespace and LND/CLN namespaces.

    Unifies pods from K8S_NAMESPACE, LND_K8S_NAMESPACE and CLN_K8S_NAMESPACE so that
    name/pattern matching used by the dashboard (container_ip_by_match etc.) sees
    pods even when LND or CLN are deployed in a different namespace. Responses are
    cached for _K8S_CACHE_TTL_SECONDS to keep the dashboard polling cheap.
    """
    cached = _k8s_cache_get("list_pods")
    if cached is not None:
        return cached
    try:
        with open(K8S_SA_TOKEN_PATH) as f:
            token = f.read().strip()
        namespaces = {K8S_NAMESPACE, LND_K8S_NAMESPACE, CLN_K8S_NAMESPACE}
        containers = []
        for ns in namespaces:
            try:
                containers.extend(_k8s_list_pods_in_namespace(ns, token))
            except Exception as exc:
                app.logger.warning(f"k8s pod listing failed for ns={ns}: {exc}")
        _k8s_cache_set("list_pods", containers)
        return containers
    except Exception as exc:
        app.logger.warning(f"k8s pod listing failed: {exc}")
        return None


_k8s_deleted_pod_uids: Dict[Tuple[str, str], str] = {}


def _k8s_lookup_pod_name(
    label_selector, namespace=None, use_cache: bool = True
) -> Tuple[bool, Optional[str]]:
    """Return (lookup_ok, pod_name) for the first Running pod matching label_selector."""
    if not label_selector:
        return True, None
    ns = namespace or K8S_NAMESPACE
    cache_key = f"pod_name:{ns}:{label_selector}"
    if use_cache:
        cached = _k8s_cache_get(cache_key)
        if cached is not None:
            # Cache the negative result too (cached=="") so we don't re-hit the API
            # in tight loops when a pod is genuinely missing.
            return True, (cached or None)
    try:
        with open(K8S_SA_TOKEN_PATH) as f:
            token = f.read().strip()
        encoded_selector = quote(label_selector, safe="")
        url = f"https://kubernetes.default.svc/api/v1/namespaces/{ns}/pods?labelSelector={encoded_selector}"
        resp = _k8s_session.get(url, headers={"Authorization": f"Bearer {token}"}, verify=K8S_SA_CA_PATH, timeout=5)
        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, dict) or not isinstance(body.get("items", []), list):
            return False, None
        has_indeterminate_pod = False
        for pod in body.get("items", []):
            if not isinstance(pod, dict):
                continue
            phase = pod.get("status", {}).get("phase")
            if phase == "Running":
                meta = pod.get("metadata", {})
                name = meta["name"]
                uid = str(meta.get("uid", "") or "").strip()
                _k8s_cache_set(cache_key, name)
                if uid:
                    _k8s_cache_set(f"pod_uid:{ns}:{name}", uid)
                return True, name
            if phase not in ("Succeeded", "Failed"):
                has_indeterminate_pod = True
        if has_indeterminate_pod:
            return False, None
        _k8s_cache_set(cache_key, "")
        return True, None
    except Exception as exc:
        app.logger.warning(f"k8s pod lookup failed (selector={label_selector}, ns={ns}): {exc}")
        return False, None


def k8s_get_pod_name(label_selector, namespace=None):
    """Return the name of the first Running pod matching label_selector."""
    _, pod_name = _k8s_lookup_pod_name(label_selector, namespace=namespace, use_cache=True)
    return pod_name


_DEFAULT_K8S_GET_POD_NAME = k8s_get_pod_name



def k8s_delete_pod(pod_name, namespace=None):
    """Delete a pod by name; the owning Deployment will recreate it."""
    if not pod_name:
        return False
    ns = namespace or K8S_NAMESPACE
    try:
        with open(K8S_SA_TOKEN_PATH) as f:
            token = f.read().strip()
        url = f"https://kubernetes.default.svc/api/v1/namespaces/{ns}/pods/{pod_name}"
        resp = _k8s_session.delete(url, headers={"Authorization": f"Bearer {token}"}, verify=K8S_SA_CA_PATH, timeout=10)
        if resp.status_code in (200, 202):
            try:
                body = resp.json()
                if isinstance(body, dict):
                    deleted_uid = str(body.get("metadata", {}).get("uid", "") or "").strip()
                    if deleted_uid:
                        with _k8s_cache_lock:
                            _k8s_deleted_pod_uids[(ns, pod_name)] = deleted_uid
            except Exception:
                pass
        # Invalidate the cache so the next list/lookup sees the post-delete state
        # (the Deployment will spin up a new pod with a different name+IP).
        with _k8s_cache_lock:
            _k8s_cache.clear()
        return resp.status_code in (200, 202, 404)
    except Exception as exc:
        app.logger.warning(f"k8s pod deletion failed ({pod_name}, ns={ns}): {exc}")
        return False


def k8s_wait_for_pod_termination(pod_name, namespace=None, pod_uid=None, timeout=30.0):
    """Wait until the deleted pod has stopped (404, terminal phase, or replaced by a new UID)."""
    if not pod_name:
        return False
    ns = namespace or K8S_NAMESPACE
    if not pod_uid:
        with _k8s_cache_lock:
            pod_uid = _k8s_deleted_pod_uids.get((ns, pod_name))
    url = f"https://kubernetes.default.svc/api/v1/namespaces/{ns}/pods/{pod_name}"
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        try:
            with open(K8S_SA_TOKEN_PATH) as f:
                token = f.read().strip()
            resp = _k8s_session.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                verify=K8S_SA_CA_PATH,
                timeout=5,
            )
            if resp.status_code == 404:
                with _k8s_cache_lock:
                    _k8s_cache.clear()
                    _k8s_deleted_pod_uids.pop((ns, pod_name), None)
                return True
            if resp.status_code == 200:
                body = resp.json()
                meta = body.get("metadata", {}) if isinstance(body, dict) else {}
                status = body.get("status", {}) if isinstance(body, dict) else {}
                phase = status.get("phase")
                current_uid = str(meta.get("uid", "") or "").strip()
                deletion_ts = meta.get("deletionTimestamp")
                if phase in ("Succeeded", "Failed"):
                    with _k8s_cache_lock:
                        _k8s_cache.clear()
                        _k8s_deleted_pod_uids.pop((ns, pod_name), None)
                    return True
                if pod_uid and current_uid and current_uid != pod_uid and not deletion_ts:
                    with _k8s_cache_lock:
                        _k8s_cache.clear()
                        _k8s_deleted_pod_uids.pop((ns, pod_name), None)
                    return True
        except Exception as exc:
            app.logger.warning(f"k8s pod termination check failed ({pod_name}, ns={ns}): {exc}")
        if time.monotonic() >= deadline:
            break
        time.sleep(0.5)
    app.logger.error(f"k8s: Timed out waiting for pod {pod_name} in ns={ns} to terminate")
    return False


def list_containers():
    """Return the running container/pod list for the active mode."""
    if K3S_MODE:
        return k8s_list_pods()
    if SECURE_MODE:
        containers = []
        # Note: Falling back to config file existence handles a "possibly installed but down"
        # heuristic when the active TCP port probe fails, allowing users to still trigger manual
        # configuration/restore flows for stopped containers.
        if check_tcp_port_cached(LND_PROBE_IP, LND_PROBE_PORT) or os.path.exists(LND_CONFIG_PATH):
            containers.append({
                "Names": ["/lightning_lnd_1"],
                "Id": "lnd_probe_id",
                "NetworkSettings": {"Networks": {"umbrel_main_network": {"IPAddress": LND_PROBE_IP}}}
            })
        cln_container_config, _ = resolve_node_config("cln")
        if check_tcp_port_cached(CLN_PROBE_IP, CLN_PROBE_PORT) or (cln_container_config and os.path.exists(cln_container_config)):
            containers.append({
                "Names": ["/lightning_cln_1"],
                "Id": "cln_probe_id",
                "NetworkSettings": {"Networks": {"umbrel_main_network": {"IPAddress": CLN_PROBE_IP}}}
            })
        return containers
    return docker_api("/containers/json?all=0")


def lnd_exists():
    """Return True if an LND container/pod is reachable in the current mode."""
    if K3S_MODE:
        return bool(k8s_get_pod_name(LND_K8S_POD_SELECTOR, namespace=LND_K8S_NAMESPACE))
    if SECURE_MODE:
        return check_tcp_port_cached(LND_PROBE_IP, LND_PROBE_PORT)
    return bool(container_ids_by_match(LND_CONTAINER_PATTERN))


def cln_exists():
    """Return True if a CLN container/pod is reachable in the current mode."""
    if K3S_MODE:
        return bool(k8s_get_pod_name(CLN_K8S_POD_SELECTOR, namespace=CLN_K8S_NAMESPACE))
    if SECURE_MODE:
        return check_tcp_port_cached(CLN_PROBE_IP, CLN_PROBE_PORT)
    return bool(container_ids_by_match(CLN_CONTAINER_PATTERN))


def container_ip_by_match(pattern, containers=None):
    if containers is None:
        containers = list_containers()
    if not containers:
        return ""

    for item in containers:
        if not isinstance(item, dict):
            continue
        names = item.get("Names")
        if not isinstance(names, list):
            continue
        for name in names:
            name_str = str(name)
            clean = name_str.lstrip("/")
            if re.search(pattern, clean):
                network_settings = item.get("NetworkSettings")
                if isinstance(network_settings, dict):
                    networks = network_settings.get("Networks")
                    if isinstance(networks, dict):
                        for network_data in networks.values():
                            ip: str = ""
                            if isinstance(network_data, dict):
                                ip = str(network_data.get("IPAddress", ""))
                            if ip:
                                return ip
    return ""


def container_ids_by_match(pattern, containers=None):
    if containers is None:
        containers = list_containers()
    if not containers:
        return []

    ids = []
    for item in containers:
        names = item.get("Names", [])
        for name in names:
            clean = name.lstrip("/")
            if re.search(pattern, clean):
                ids.append(item.get("Id", ""))
                break
    return ids


def container_id_by_match(pattern):
    ids = container_ids_by_match(pattern)
    return ids[0] if ids else ""


_DEFAULT_LND_EXISTS = lnd_exists
_DEFAULT_CLN_EXISTS = cln_exists
_DEFAULT_CONTAINER_IDS_BY_MATCH = container_ids_by_match
_DEFAULT_CONTAINER_ID_BY_MATCH = container_id_by_match


def detect_node_running_state(node_type: str) -> Tuple[bool, bool]:
    """Return (lookup_ok, is_running) for 'lnd' or 'cln'."""
    is_lnd = node_type == "lnd"
    exists_fn = lnd_exists if is_lnd else cln_exists
    default_exists_fn = _DEFAULT_LND_EXISTS if is_lnd else _DEFAULT_CLN_EXISTS
    if exists_fn is not default_exists_fn:
        try:
            res = exists_fn()
        except Exception as exc:
            app.logger.warning(f"{node_type.upper()} running-state check failed: {exc}")
            return False, False
        if res is None:
            return False, False
        return True, bool(res)

    if SECURE_MODE:
        probe_ip = LND_PROBE_IP if is_lnd else CLN_PROBE_IP
        probe_port = LND_PROBE_PORT if is_lnd else CLN_PROBE_PORT
        return True, bool(check_tcp_port_cached(probe_ip, probe_port))

    if K3S_MODE:
        selector = LND_K8S_POD_SELECTOR if is_lnd else CLN_K8S_POD_SELECTOR
        ns = LND_K8S_NAMESPACE if is_lnd else CLN_K8S_NAMESPACE
        if k8s_get_pod_name is not _DEFAULT_K8S_GET_POD_NAME:
            try:
                pod_name = k8s_get_pod_name(selector, namespace=ns)
            except Exception as exc:
                app.logger.warning(f"k8s pod lookup failed (selector={selector}, ns={ns}): {exc}")
                return False, False
            return True, bool(pod_name)
        lookup_ok, pod_name = _k8s_lookup_pod_name(selector, namespace=ns, use_cache=False)
        return lookup_ok, bool(pod_name)

    pattern = LND_CONTAINER_PATTERN if is_lnd else CLN_CONTAINER_PATTERN
    if container_ids_by_match is not _DEFAULT_CONTAINER_IDS_BY_MATCH:
        try:
            ids = container_ids_by_match(pattern)
        except Exception as exc:
            app.logger.warning(f"Docker container lookup failed for {node_type}: {exc}")
            return False, False
        if ids is None:
            return False, False
        return True, bool(ids)

    containers = list_containers()
    if not isinstance(containers, list):
        return False, False
    return True, bool(container_ids_by_match(pattern, containers=containers))


def restart_container_by_pattern(pattern, is_lnd=False):
    if K3S_MODE:
        selector = LND_K8S_POD_SELECTOR if is_lnd else CLN_K8S_POD_SELECTOR
        ns = LND_K8S_NAMESPACE if is_lnd else CLN_K8S_NAMESPACE
        app.logger.info(f"k3s: Triggering pod restart (selector={selector}, ns={ns})")
        pod_name = k8s_get_pod_name(selector, namespace=ns)
        if not pod_name:
            app.logger.error(f"k3s: No Running pod found for selector {selector} in ns={ns}")
            return False
        pod_uid = _k8s_cache_get(f"pod_uid:{ns}:{pod_name}")
        app.logger.info(f"k3s: Deleting pod {pod_name} (Deployment will recreate it)")
        result = k8s_delete_pod(pod_name, namespace=ns)
        app.logger.info(f"k3s: Pod deletion {'successful' if result else 'failed'}")
        if not result:
            return False
        if not k8s_wait_for_pod_termination(pod_name, namespace=ns, pod_uid=pod_uid):
            return False
        return True

    # Specialized LND event chain to accommodate umbrelOS Node.js middleware
    if is_lnd:
        app.logger.info("Triggering LND container restart sequence")
        preloaded_daemon_id: Optional[str] = None
        if (
            container_id_by_match is _DEFAULT_CONTAINER_ID_BY_MATCH
            and container_ids_by_match is _DEFAULT_CONTAINER_IDS_BY_MATCH
        ):
            containers = list_containers()
            if not isinstance(containers, list):
                app.logger.error("Failed to list Docker containers for LND restart sequence.")
                return False
            middleware_ids = container_ids_by_match(LND_MIDDLEWARE_PATTERN, containers=containers)
            middleware_id = middleware_ids[0] if middleware_ids else ""
            daemon_ids = container_ids_by_match(LND_CONTAINER_PATTERN, containers=containers)
            preloaded_daemon_id = daemon_ids[0] if daemon_ids else ""
        else:
            middleware_id = container_id_by_match(LND_MIDDLEWARE_PATTERN)

        # 1. Restart middleware if present (excluding proxy)
        if middleware_id:
            app.logger.info(f"Found LND middleware container (ID: {middleware_id[:12]}). Restarting...")
            res = docker_api_post(f"/containers/{middleware_id}/restart")
            if not res:
                app.logger.error("LND middleware restart failed. Aborting restart sequence.")
                return False
            else:
                app.logger.info("LND middleware restart successful.")
                app.logger.info(f"Waiting {LND_RESTART_DELAY} seconds for middleware configuration generation...")
                time.sleep(LND_RESTART_DELAY)
        else:
            app.logger.info("No LND middleware container found; proceeding directly to daemon restart.")
        
        # 2. Restart LND daemon
        daemon_id = (
            preloaded_daemon_id
            if preloaded_daemon_id is not None and not middleware_id
            else container_id_by_match(LND_CONTAINER_PATTERN)
        )
        if daemon_id:
            app.logger.info(f"Found LND daemon container (ID: {daemon_id[:12]}). Restarting...")
            res = docker_api_post(f"/containers/{daemon_id}/restart")
            app.logger.info(f"LND daemon restart {'successful' if res else 'failed'}.")
            return res
        else:
            app.logger.error("Failed to locate LND daemon container.")
            return False

    # General restart logic for other patterns (e.g. CLN)
    container_ids = container_ids_by_match(pattern)
    if not container_ids:
        app.logger.warning(f"No containers found matching pattern: {pattern}")
        return False
    
    success = True
    for cid in container_ids:
        app.logger.info(f"Restarting container matching '{pattern}' (ID: {cid[:12]})...")
        res = docker_api_post(f"/containers/{cid}/restart")
        if res:
            app.logger.info(f"Restart successful for container {cid[:12]}")
        else:
            app.logger.error(f"Restart failed for container {cid[:12]}")
            success = False
            
    return success


def read_dataplane_state():
    defaults = {
        "dataplane_mode": "docker-full-parity",
        "target_container": "",
        "target_ip": "",
        "target_impl": "",
        "forwarding_port": "",
        "k3s_bypass_cidrs": [],
        "rules_synced": False,
        "ipv4_rules_synced": False,
        "ipv6_rules_synced": False,
        "dns_protected": False,
        "decommissioned": False,
        "ipv6_policy": "deny",
        "target_ipv6_addresses": [],
        "target_ipv6_default_route": False,
        "last_reconcile_at": "",
        "last_error": None,
        "docker_network": {
            "name": "docker-tunnelsats",
            "subnet": "10.9.9.0/25",
            "bridge": "",
        },
    }

    if not os.path.exists(STATE_FILE):
        return defaults

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as state_fp:
            data = json.load(state_fp)
            if isinstance(data, dict):
                # Type-safe update of defaults
                for k, v in data.items():
                    if k in defaults:
                        defaults[k] = v  # type: ignore (dynamic dict update)
                
                docker_net = data.get("docker_network")
                target_net = defaults.get("docker_network")
                if isinstance(docker_net, dict) and isinstance(target_net, dict):
                    target_net.update(docker_net)
    except Exception:
        pass

    return defaults


def sanitize_request_id(raw_request_id):
    request_id = str(raw_request_id or "").strip()
    if REQUEST_ID_PATTERN.fullmatch(request_id):
        return request_id
    return None


def ensure_reconcile_dirs():
    os.makedirs(RECONCILE_TRIGGER_DIR, exist_ok=True)
    os.makedirs(RECONCILE_RESULT_DIR, exist_ok=True)


def ensure_decommission_dirs():
    os.makedirs(DECOMMISSION_TRIGGER_DIR, exist_ok=True)
    os.makedirs(DECOMMISSION_RESULT_DIR, exist_ok=True)


def reconcile_trigger_path(request_id):
    return os.path.join(RECONCILE_TRIGGER_DIR, f"{request_id}.trigger")


def reconcile_result_path(request_id):
    return os.path.join(RECONCILE_RESULT_DIR, f"{request_id}.json")


def decommission_trigger_path(request_id):
    return os.path.join(DECOMMISSION_TRIGGER_DIR, f"{request_id}.trigger")


def decommission_result_path(request_id):
    return os.path.join(DECOMMISSION_RESULT_DIR, f"{request_id}.json")


def atomic_write_text(path, payload):
    tmp_path = f"{path}.tmp.{uuid.uuid4().hex}"
    with open(tmp_path, "w", encoding="utf-8") as out_fp:
        out_fp.write(payload)
    os.replace(tmp_path, path)


def read_reconcile_result(request_id):
    result_path = reconcile_result_path(request_id)
    if not os.path.exists(result_path):
        return None

    try:
        with open(result_path, "r", encoding="utf-8") as result_fp:
            return json.load(result_fp)
    except Exception:
        return None


def read_decommission_result(request_id):
    result_path = decommission_result_path(request_id)
    if not os.path.exists(result_path):
        return None

    try:
        with open(result_path, "r", encoding="utf-8") as result_fp:
            return json.load(result_fp)
    except Exception:
        return None


def read_legacy_reconcile_result():
    if not os.path.exists(RECONCILE_RESULT_LEGACY):
        return None

    try:
        with open(RECONCILE_RESULT_LEGACY, "r", encoding="utf-8") as result_fp:
            return json.load(result_fp)
    except Exception:
        return None


def reconcile_result_success(result):
    if not isinstance(result, dict):
        return False
    state = result.get("state", {})
    if isinstance(state, dict):
        return bool(state.get("rules_synced", False))
    return False


def decommission_result_success(result):
    if not isinstance(result, dict):
        return False
    if result.get("decommissioned") is not True:
        return False
    state = result.get("state", {})
    if isinstance(state, dict):
        return bool(state.get("decommissioned", False))
    return False


def reconcile_target_and_wait(
    expected_target: Optional[str],
    timeout: float = RECONCILE_WAIT_TIMEOUT_SECONDS,
) -> Tuple[bool, Dict[str, Any]]:
    """Trigger reconciliation and wait until the dataplane confirms the expected target."""
    request_id = str(uuid.uuid4())
    try:
        ensure_reconcile_dirs()
        atomic_write_text(reconcile_trigger_path(request_id), f"{request_id}\n")
    except Exception as exc:
        return False, {"error": f"Unable to trigger reconcile: {exc}"}

    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        result = read_reconcile_result(request_id)
        if isinstance(result, dict) and result.get("request_id") == request_id:
            state = result.get("state")
            if not isinstance(state, dict):
                return False, {"error": "Reconcile returned an invalid dataplane state."}
            target_matches = not expected_target or state.get("target_impl") == expected_target
            success = reconcile_result_success(result) and target_matches
            if success:
                return True, result
            error = state.get("last_error") or (
                f"Reconciled target {state.get('target_impl') or 'none'} does not match "
                f"requested target {expected_target}."
            )
            return False, {**result, "error": str(error)}
        time.sleep(RECONCILE_WAIT_POLL_SECONDS)

    return False, {"error": "Timed out waiting for dataplane reconciliation."}


def decommission_dataplane_and_wait(
    timeout: float = RECONCILE_WAIT_TIMEOUT_SECONDS,
) -> Tuple[bool, Dict[str, Any]]:
    """Trigger dataplane decommission and wait until the daemon confirms full teardown."""
    request_id = str(uuid.uuid4())
    try:
        ensure_decommission_dirs()
        atomic_write_text(decommission_trigger_path(request_id), f"{request_id}\n")
    except Exception as exc:
        return False, {"error": f"Unable to trigger dataplane decommission: {exc}"}

    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        result = read_decommission_result(request_id)
        if isinstance(result, dict) and result.get("request_id") == request_id:
            state = result.get("state")
            if not isinstance(state, dict):
                return False, {"error": "Decommission returned an invalid dataplane state."}
            if decommission_result_success(result):
                return True, result
            error = state.get("last_error") or "Dataplane decommission failed."
            return False, {**result, "error": str(error)}
        time.sleep(RECONCILE_WAIT_POLL_SECONDS)

    return False, {"error": "Timed out waiting for dataplane decommission."}


@app.before_request
def restrict_local_api():
    if TUNNELSATS_ROLE == "daemon":
        # The daemon is reachable only through its permissioned Unix socket.
        # Browser-facing authentication, proxy validation, and CSRF enforcement
        # happen in the web role before a request is forwarded here.
        return None

    if _management_browser_security_enabled() and _management_request_is_sensitive():
        direct_remote_addr = _get_direct_remote_addr()
        if not _management_peer_is_trusted(direct_remote_addr):
            return _management_security_error("direct-peer")
        # ProxyFix exposes the forwarded client as request.remote_addr. Use it
        # only after authenticating the original socket peer above.
        if not client_is_allowed(request.remote_addr):
            return _management_security_error("client-ip")
        if not _management_host_is_allowed(request.host):
            return _management_security_error("host")
        changes_state = _management_request_changes_state()
        requires_csrf = _management_request_requires_csrf()
        if changes_state:
            if request.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
                return _management_security_error("fetch-site")
            if not _origin_matches_request(request.headers.get("Origin")):
                return _management_security_error("origin")
        if requires_csrf:
            supplied_token = request.headers.get(MANAGEMENT_CSRF_HEADER, "")
            if not MANAGEMENT_SECURITY.csrf_matches(supplied_token):
                return _management_security_error("csrf")
            if changes_state and request.path in MANAGEMENT_JSON_PATHS and not request.is_json:
                return _management_security_error("content-type")
            app.logger.info(
                "Management mutation accepted: method=%s endpoint=%s remote=%s reason=authorized",
                request.method,
                request.endpoint or "unknown",
                request.remote_addr or "unknown",
            )
        return None

    if request.path.startswith("/api/local") or request.path == "/api/subscription/renew":
        direct_remote_addr = _get_direct_remote_addr()

        # Trust X-Forwarded-For only after authenticating the immediate proxy peer.
        # Direct clients can otherwise spoof forwarded headers to bypass local-network checks.
        if _management_peer_is_trusted(direct_remote_addr):
            effective_remote_addr = request.remote_addr
        else:
            effective_remote_addr = direct_remote_addr

        if not client_is_allowed(effective_remote_addr):
            abort(403)


@app.after_request
def protect_management_response(response):
    if _management_browser_security_enabled() and _management_request_is_sensitive():
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    return response


# Proxy function to forward requests to the core Tunnelsats API
def proxy_request(method, endpoint, payload=None):
    url = f"{TUNNELSATS_API_URL}/{endpoint}"
    headers = {"Content-Type": "application/json"}
    try:
        if method == "GET":
            resp = requests.get(url, headers=headers, timeout=10)
        elif method == "POST":
            resp = requests.post(url, json=payload, headers=headers, timeout=10)
        else:
            return jsonify({"error": "Unsupported method"}), 405

        excluded_headers = ["content-encoding", "content-length", "transfer-encoding", "connection"]
        filtered_headers = [
            (name, value) for (name, value) in resp.headers.items() if name.lower() not in excluded_headers
        ]
        return (resp.content, resp.status_code, filtered_headers)
    except requests.RequestException as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/")
def serve_index():
    static_folder = app.static_folder or "../web"
    return send_from_directory(static_folder, "index.html")


@app.route("/<path:path>")
def serve_static(path):
    static_folder = app.static_folder or "../web"
    return send_from_directory(static_folder, path)


@app.errorhandler(404)
def handle_404(e):
    if request.path.startswith("/api/"):
        return jsonify({"success": False, "error": "API endpoint not found"}), 404
    return e


@app.errorhandler(405)
def handle_405(e):
    if request.path.startswith("/api/"):
        return jsonify({"success": False, "error": "Method not allowed"}), 405
    return jsonify({"success": False, "error": "Method not allowed"}), 405


@app.errorhandler(500)
def handle_500(e):
    app.logger.error(f"Unhandled 500 error on {request.path}: {e}")
    return jsonify({"success": False, "error": "Internal server error"}), 500


@app.errorhandler(Exception)
def handle_exception(e):
    if isinstance(e, HTTPException):
        if request.path.startswith("/api/"):
            return jsonify({"success": False, "error": e.name}), e.code or 500
        return e
    app.logger.error(f"Unhandled exception on {request.path}: {e}", exc_info=True)
    return jsonify({"success": False, "error": "Internal server error"}), 500


# --- API PROXY ROUTES ---

# Short-lived server-side cache for subscription status checks to avoid redundant
# 10s API timeouts on retries while maintaining authoritative Source of Truth (SOT).
_SUBSCRIPTION_CACHE: Dict[str, Tuple[float, Optional[Dict[str, Any]]]] = {}
SUBSCRIPTION_CACHE_TTL = 600  # 10 minutes

def _status_info_is_disabled_or_expired(info: Dict[str, Any]) -> bool:
    """True when subscription state indicates an expired/disabled entitlement."""
    if not isinstance(info, dict):
        return False
    status = str(info.get("status", "")).lower()
    return status == "disabled" or _is_timestamp_expired(info.get("expiry"))

def _fetch_subscription_status_cached(wg_public_key: str) -> Optional[Dict[str, Any]]:
    """Fetch subscription status with a short-lived internal cache."""
    now = time.time()
    if wg_public_key in _SUBSCRIPTION_CACHE:
        cache_time, info = _SUBSCRIPTION_CACHE[wg_public_key]
        if now - cache_time < SUBSCRIPTION_CACHE_TTL:
            # Never reuse volatile disabled/expired status entries from cache.
            if info and _status_info_is_disabled_or_expired(info):
                _SUBSCRIPTION_CACHE.pop(wg_public_key, None)
            else:
                return info

    info = _fetch_subscription_status(wg_public_key)
    if info and not _status_info_is_disabled_or_expired(info):
        _SUBSCRIPTION_CACHE[wg_public_key] = (now, info)
    elif not info:
        # Negative Caching: Cache failures for 1 minute to avoid repeated 10s timeouts on retries.
        # We store this by setting the cache timestamp to be close to expiration (1 minute left).
        _SUBSCRIPTION_CACHE[wg_public_key] = (now - SUBSCRIPTION_CACHE_TTL + 60, None)
    else:
        _SUBSCRIPTION_CACHE.pop(wg_public_key, None)
    return info

def _is_timestamp_expired(timestamp_str: Optional[str]) -> bool:
    """Checks if a given ISO timestamp string is in the past."""
    if not timestamp_str:
        return False
    try:
        # Normalize 'Z' to offset-aware format
        expiry_dt = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        if expiry_dt.tzinfo is None:
            expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)
        return expiry_dt < datetime.now(timezone.utc)
    except (ValueError, TypeError) as e:
        app.logger.debug(f"Failed to parse timestamp {timestamp_str}: {e}")
        return False

def _fetch_subscription_status(wg_public_key: str) -> Optional[Dict[str, Any]]:
    """Fetch subscription status from the TunnelSats Public API."""
    try:
        url = f"{TUNNELSATS_API_URL}/subscription/status"
        res = requests.post(url, json={"wgPublicKey": wg_public_key}, timeout=10)
        if res.status_code == 200:
            data = res.json()
            if isinstance(data, dict):
                return data
    except requests.RequestException as e:
        app.logger.error(f"Failed to fetch subscription status for {wg_public_key}: {e}")
    return None


_DAEMON_RESPONSE_HEADERS = frozenset(
    {
        "cache-control",
        "content-disposition",
        "content-type",
        "expires",
        "pragma",
        "x-content-type-options",
    }
)


@app.before_request
def dispatch_split_runtime_request():
    if TUNNELSATS_ROLE == "daemon":
        if not request.path.startswith("/api/local/"):
            abort(404)
        return None

    if (
        TUNNELSATS_ROLE != "web"
        or not request.path.startswith("/api/local/")
        or request.path == "/api/local/session"
    ):
        return None

    if request.method not in DAEMON_ALLOWED_METHODS:
        return jsonify({"success": False, "error": "Method not allowed."}), 405

    body = request.get_data(cache=True)
    daemon_headers = {}
    for header_name in ("Content-Type", "Accept"):
        value = request.headers.get(header_name)
        if value:
            daemon_headers[header_name] = value
    path = request.full_path[:-1] if request.full_path.endswith("?") else request.full_path
    request_kwargs: Dict[str, Any] = {
        "body": body,
        "headers": daemon_headers,
    }
    if request.path in ("/api/local/restore-node", "/api/local/configure-node"):
        request_kwargs["timeout"] = DAEMON_RESTORE_TIMEOUT_SECONDS
    try:
        daemon_response = request_over_unix_socket(
            DAEMON_SOCKET_PATH,
            request.method,
            path,
            **request_kwargs,
        )
    except (DaemonUnavailable, ValueError, TypeError):
        app.logger.warning(
            "Privileged management request failed closed: method=%s path=%s",
            request.method,
            request.path,
        )
        return jsonify(
            {
                "success": False,
                "error": "TunnelSats daemon is unavailable.",
            }
        ), 503

    response_headers = [
        (name, value)
        for name, value in daemon_response.headers
        if name.lower() in _DAEMON_RESPONSE_HEADERS
    ]
    return Response(
        daemon_response.body,
        status=daemon_response.status,
        headers=response_headers,
    )


_subscription_sync_lock = threading.Lock()
_next_subscription_sync_time: Dict[str, float] = {}

SYNC_INTERVAL_SECONDS = 86400  # 24 hours
RETRY_INTERVAL_SECONDS = 3600  # 1 hour
EVICTION_INTERVAL_SECONDS = 172800  # 48 hours

def _trigger_lazy_subscription_sync(wg_pubkey: str):
    """
    Triggers an asynchronous sync of the subscription status from the public API
    if the next allowed sync time has passed.
    """
    if not wg_pubkey or wg_pubkey == "Not available":
        return

    now = time.time()
    with _subscription_sync_lock:
        stale_keys = [
            k for k, t in _next_subscription_sync_time.items()
            if now - t > EVICTION_INTERVAL_SECONDS
        ]
        for k in stale_keys:
            _next_subscription_sync_time.pop(k, None)

        next_sync = _next_subscription_sync_time.get(wg_pubkey, 0.0)
        if now < next_sync:
            return
        # Prevent concurrent duplicate threads by immediately pushing the next sync time
        _next_subscription_sync_time[wg_pubkey] = now + SYNC_INTERVAL_SECONDS

    def _sync_worker(pubkey):
        app.logger.info(f"Starting lazy background subscription status sync for pubkey: {pubkey}")
        try:
            # Fetch directly from public API bypassing cache to get absolute truth
            status_info = _fetch_subscription_status(pubkey)
            if status_info and isinstance(status_info, dict):
                expiry = status_info.get("expiry")
                if expiry:
                    success = _update_local_metadata({"expiresAt": expiry}, wg_pubkey=pubkey)
                    if success:
                        app.logger.info(f"Successfully synced subscription expiry to {expiry} via background sync")
                    else:
                        app.logger.warning("Background subscription sync: Failed to update local metadata")
                        with _subscription_sync_lock:
                            _next_subscription_sync_time[pubkey] = time.time() + RETRY_INTERVAL_SECONDS
                else:
                    app.logger.warning("Background subscription sync: API response missing 'expiry' field")
                    with _subscription_sync_lock:
                        _next_subscription_sync_time[pubkey] = time.time() + RETRY_INTERVAL_SECONDS
            else:
                app.logger.warning("Background subscription sync: Failed to fetch status from public API")
                with _subscription_sync_lock:
                    _next_subscription_sync_time[pubkey] = time.time() + RETRY_INTERVAL_SECONDS
        except Exception as e:
            app.logger.error(f"Error in background subscription sync worker: {e}")
            with _subscription_sync_lock:
                _next_subscription_sync_time[pubkey] = time.time() + RETRY_INTERVAL_SECONDS

    thread = threading.Thread(
        target=_sync_worker,
        args=(wg_pubkey,),
        name=f"sync_worker_{wg_pubkey}",
        daemon=True
    )
    thread.start()


@app.route("/api/servers", methods=["GET"])
def get_servers():
    proxy_res = proxy_request("GET", "servers")
    if len(proxy_res) == 2:
        return proxy_res  # Error JSON and status code
    content, status_code, headers = proxy_res
    if status_code == 200:
        try:
            data = json.loads(content)
            servers = data.get("servers", []) if isinstance(data, dict) else data
            
            # Enrich with coordinates and labels using unified helper
            for s in servers:
                s_id = s.get("id", "")
                meta = get_server_geodata(s_id)

                if meta:
                    s["lat"] = meta["lat"]
                    s["lng"] = meta["lng"]
                    s["label"] = meta["label"]
                    if "flag" not in s:
                        s["flag"] = meta["flag"]
            
            return jsonify(data), status_code, headers
        except Exception as e:
            app.logger.warning(f"Failed to enrich servers: {e}")
    return content, status_code, headers


@app.route("/api/subscription/create", methods=["POST"])
def create_subscription():
    return proxy_request("POST", "subscription/create", request.json)


@app.route("/api/subscription/<paymentHash>", methods=["GET"])
def check_subscription(paymentHash):
    # Proxy the check request to the core API
    url = f"{TUNNELSATS_API_URL}/subscription/{paymentHash}"
    try:
        resp = requests.get(url, headers={"Content-Type": "application/json"}, timeout=10)
        if resp.status_code == 200:
            try:
                data = resp.json()
                # If the subscription is paid, ensure our local metadata is in sync.
                if not isinstance(data, dict):
                    app.logger.warning("Unexpected subscription response shape: expected JSON object.")
                elif data.get("status") == "paid":
                    # Support both standard 'subscription' object and top-level renewal fields
                    new_expiry = data.get("newExpiry")
                    sub_data = data.get("subscription")
                    
                    if isinstance(sub_data, dict):
                        sync_payload = dict(sub_data)
                        # Renewal APIs may include top-level newExpiry even when subscription exists.
                        if new_expiry:
                            sync_payload["newExpiry"] = new_expiry
                        _update_local_metadata(sync_payload, payment_hash=paymentHash)
                    elif new_expiry:
                        # Direct renewal response format
                        _update_local_metadata({"newExpiry": new_expiry}, payment_hash=paymentHash)
            except ValueError as exc:
                app.logger.warning(f"Failed to parse subscription data or update metadata: {exc}")
            except Exception as exc:
                app.logger.error(f"Metadata sync failed after subscription check: {exc}")

        excluded_headers = ["content-encoding", "content-length", "transfer-encoding", "connection"]
        filtered_headers = [
            (name, value) for (name, value) in resp.headers.items() if name.lower() not in excluded_headers
        ]
        return (resp.content, resp.status_code, filtered_headers)
    except requests.RequestException as exc:
        return jsonify({"error": str(exc)}), 500


def _update_local_metadata(
    subscription_data: Dict[str, Any],
    payment_hash: Optional[str] = None,
    wg_pubkey: Optional[str] = None
) -> bool:
    """
    Update tunnelsats-meta.json with latest subscription data (e.g. after renewal).
    Only updates fields that are present in subscription_data.
    """
    if not isinstance(subscription_data, dict):
        app.logger.warning("Metadata sync skipped: subscription payload is not a JSON object.")
        return False

    meta_path = os.path.join(DATA_DIR, META_FILE)
    with _metadata_lock:
        try:
            with open(meta_path, "r", encoding="utf-8") as fp:
                meta = json.load(fp)
        except FileNotFoundError:
            app.logger.warning(f"Metadata file not found at {meta_path}; skipping sync.")
            return False
        except (IOError, json.JSONDecodeError) as exc:
            app.logger.warning(f"Failed to read metadata for sync: {exc}")
            return False

        if not isinstance(meta, dict):
            app.logger.warning("Metadata sync skipped: metadata file is not a JSON object.")
            return False

        active_pubkey = meta.get("wgPublicKey")
        if wg_pubkey and active_pubkey and active_pubkey != wg_pubkey:
            app.logger.warning(
                f"Metadata sync skipped: active wgPublicKey '{active_pubkey}' "
                f"does not match expected '{wg_pubkey}'."
            )
            return False

        changed = False
        if wg_pubkey and not active_pubkey:
            meta["wgPublicKey"] = wg_pubkey
            changed = True
        # Prefer renewal-specific newExpiry; fall back to expiresAt for standard subscription payloads.
        _new_expiry = subscription_data.get("newExpiry")
        new_expiry = _new_expiry if _new_expiry is not None else subscription_data.get("expiresAt")
        
        if new_expiry and meta.get("expiresAt") != new_expiry:
            meta["expiresAt"] = new_expiry
            changed = True

        if payment_hash and meta.get("paymentHash") != payment_hash:
            meta["paymentHash"] = payment_hash
            changed = True

        if changed:
            try:
                _write_file_secure(meta_path, json.dumps(meta, indent=2))
                return True
            except (IOError, OSError) as exc:
                app.logger.error(f"Failed to write synchronized metadata: {exc}")
                return False
        
        return True


@app.route("/api/subscription/claim", methods=["POST"])
def claim_subscription():
    # If the claim was successful, intercept and persist WireGuard config + metadata.
    url = f"{TUNNELSATS_API_URL}/subscription/claim"
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        payload = {}
    
    # Hide sensitive logs
    app.logger.info("Initiating subscription claim upstream")
    
    try:
        resp = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=10)
        app.logger.info(f"Upstream claim responded with HTTP {resp.status_code}, len: {len(resp.content)}")
        
        if resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError as exc:
                app.logger.error("Upstream claim returned invalid JSON")
                return jsonify({"success": False, "error": "Invalid upstream payload (not JSON)"}), 400

            if not isinstance(data, dict):
                app.logger.error(f"Upstream claim returned non-object JSON payload: {type(data).__name__}")
                return jsonify({"success": False, "error": "Invalid upstream payload (expected JSON object)"}), 400

            # If the upstream actively rejected the claim with a 200 OK (semantic failure)
            if data.get("success") is False or data.get("status") == "error":
                msg = data.get("message", "Subscription already claimed or invalid payload.")
                app.logger.error(f"Upstream claim semantic failure: {msg}")
                return jsonify({"success": False, "error": msg}), 400

            full_config = data.get("config") or data.get("fullConfig") or data.get("wireguardConfig")
            if not full_config:
                app.logger.error(f"Upstream claim response omitted fullConfig. Received keys: {list(data.keys())}")
                return jsonify({"success": False, "error": "Invalid upstream payload: Missing WireGuard configuration"}), 400

            if not _has_required_wireguard_blocks(full_config):
                app.logger.error("Upstream claim returned a config that is missing [Interface] or [Peer] blocks.")
                return jsonify({"success": False, "error": "Invalid upstream payload: WireGuard config missing [Interface] or [Peer] block"}), 400

            sanitized_full_config, sanitize_error = _sanitize_wireguard_config(full_config)
            if sanitize_error:
                app.logger.error(f"Upstream claim returned unsafe WireGuard config: {sanitize_error}")
                return jsonify({"success": False, "error": "Invalid upstream payload: Unsafe WireGuard configuration"}), 400
            full_config = _ensure_peer_persistent_keepalive(sanitized_full_config or "", keepalive=25)

            sub = data.get("subscription")
            subscription_data = sub if isinstance(sub, dict) else {}
            srv = data.get("server")
            server_data = srv if isinstance(srv, dict) else {}
            peer = data.get("peer")
            peer_data = peer if isinstance(peer, dict) else {}
            
            server_id = secure_filename(subscription_data.get("serverId") or server_data.get("id") or "unknown") or "unknown"

            parsed = _parse_config_comments(full_config)
            payment_hash = payload.get("paymentHash", "")
            meta = {
                "serverId": server_id,
                "paymentHash": payment_hash,
                "wgPublicKey": parsed.get("wgPublicKey", ""),
                "peerAddress": peer_data.get("address", parsed.get("peerAddress", "")),
                "presharedKey": peer_data.get("presharedKey", parsed.get("presharedKey", "")),
                "vpnPort": parsed.get("vpnPort", 0),
                "serverDomain": parsed.get("serverDomain", ""),
                "wgEndpoint": server_data.get("endpoint", parsed.get("wgEndpoint", "")),
                "claimedAt": datetime.now(timezone.utc).isoformat(),
                "expiresAt": data.get("subscriptionEnd") or subscription_data.get("expiresAt", parsed.get("expiresAt", "")),
            }

            if _persist_tunnelsats_config_and_meta(full_config, meta):
                app.logger.info("Successfully persisted claimed configuration and metadata.")
            else:
                return jsonify({"success": False, "error": "Failed to write configuration to local disk."}), 500

        excluded_headers = ["content-encoding", "content-length", "transfer-encoding", "connection"]
        filtered_headers = [
            (name, value) for (name, value) in resp.headers.items() if name.lower() not in excluded_headers
        ]
        return (resp.content, resp.status_code, filtered_headers)
    except requests.RequestException as exc:
        app.logger.error(f"Network error during upstream claim: {exc}")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/subscription/renew", methods=["POST"])
def renew_subscription():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        payload = {}
    meta_path = os.path.join(DATA_DIR, META_FILE)
    with _metadata_lock:
        try:
            with open(meta_path, "r", encoding="utf-8") as fp:
                meta = json.load(fp)
                if isinstance(meta, dict):
                    if not payload.get("serverId") and "serverId" in meta:
                        payload["serverId"] = meta["serverId"]
                    if not payload.get("wgPublicKey") and "wgPublicKey" in meta:
                        payload["wgPublicKey"] = meta["wgPublicKey"]
        except FileNotFoundError:
            pass
        except (IOError, json.JSONDecodeError) as exc:
            app.logger.warning(f"Failed to read metadata for renew autofill: {exc}")

    app.logger.info("Initiating renewal upstream...")
    res = proxy_request("POST", "subscription/renew", payload)
    app.logger.info("Upstream renewal completed.")
    return res


# --- LOCAL APP ROUTES ---

@app.route("/api/local/session", methods=["GET"])
def local_session():
    return jsonify(
        {
            "authenticated": True,
            "csrf_token": MANAGEMENT_CSRF_TOKEN,
        }
    )


@app.route("/api/local/status", methods=["GET"])
def local_status():
    app.logger.debug("Action Request: Fetching local status")
    wg_status, wg_pubkey = _get_wireguard_state()
    if wg_pubkey:
        _trigger_lazy_subscription_sync(wg_pubkey)

    configs = []
    if os.path.exists(DATA_DIR):
        for fname in os.listdir(DATA_DIR):
            if fname.endswith(".conf"):
                configs.append(fname)

    dataplane = read_dataplane_state()

    if K3S_MODE:
        import socket
        def _resolve_svc(svc_env, ns):
            name = os.environ.get(svc_env, "")
            if not name:
                return ""
            cache_key = f"svc_resolve:{name}:{ns}"
            cached = _k8s_cache_get(cache_key)
            if cached is not None:
                return cached
            resolved = ""
            for fqdn in (f"{name}.{ns}.svc.cluster.local", name):
                try:
                    future = _dns_executor.submit(socket.getaddrinfo, fqdn, None, socket.AF_INET)
                    addr_info = future.result(timeout=1.0)
                    if addr_info:
                        resolved = addr_info[0][4][0]
                        break
                except Exception:
                    pass
            _k8s_cache_set(cache_key, resolved)
            return resolved
        lnd_ip = _resolve_svc("LND_K8S_SERVICE", LND_K8S_NAMESPACE)
        cln_ip = _resolve_svc("CLN_K8S_SERVICE", CLN_K8S_NAMESPACE)
        future_lnd = _dns_executor.submit(lnd_exists)
        future_cln = _dns_executor.submit(cln_exists)
        lnd_detected = future_lnd.result()
        cln_detected = future_cln.result()
    elif SECURE_MODE:
        future_lnd = _dns_executor.submit(lnd_exists)
        future_cln = _dns_executor.submit(cln_exists)
        lnd_detected = future_lnd.result()
        cln_detected = future_cln.result()
        lnd_ip = LND_PROBE_IP if lnd_detected else ""
        cln_ip = CLN_PROBE_IP if cln_detected else ""
    else:
        containers = list_containers() or []
        lnd_ip = container_ip_by_match(LND_CONTAINER_PATTERN, containers=containers)
        cln_ip = container_ip_by_match(CLN_CONTAINER_PATTERN, containers=containers)
        lnd_detected = bool(container_ids_by_match(LND_CONTAINER_PATTERN, containers=containers))
        cln_detected = bool(container_ids_by_match(CLN_CONTAINER_PATTERN, containers=containers))

    # Granular state detection
    vpn_active = (wg_status == "Connected")

    lnd_routing_active = False
    try:
        with open(LND_CONFIG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                cleaned = line.strip()
                if cleaned.startswith("#"):
                    continue
                if re.match(r"^externalhosts\s*=\s*", cleaned):
                    lnd_routing_active = True
                    break
    except FileNotFoundError:
        pass
    except (IOError, OSError) as exc:
        app.logger.warning(f"Failed to read LND config for routing detection: {exc}")

    cln_routing_active = False
    cln_container_config, _ = resolve_node_config("cln")
    if cln_container_config:
        try:
            with open(cln_container_config, "r", encoding="utf-8") as f:
                for line in f:
                    cleaned = line.strip()
                    if cleaned.startswith("#"):
                        continue
                    if re.match(r"^announce-addr\s*=\s*", cleaned):
                        cln_routing_active = True
                        break
        except FileNotFoundError:
            pass
        except (IOError, OSError) as exc:
            app.logger.warning(f"Failed to read CLN config for routing detection: {exc}")

    # Dynamic Internal IP Recovery
    vpn_internal_ip = ""
    try:
        # Source of Truth: the live interface state. 
        # check=False in subprocess.run is more robust than check_output if "ip" is missing.
        res = subprocess.run(["ip", "-4", "addr", "show", "dev", WG_INTERFACE_NAME], 
                             capture_output=True, text=True, timeout=2)
        if res.returncode == 0:
            if match := re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", res.stdout):
                vpn_internal_ip = match.group(1)
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        # Handle cases where "ip" is missing or dev doesn't exist gracefully
        pass

    server_domain = ""
    expires_at = ""
    vpn_port = ""
    meta_data: Dict[str, Any] = {}
    meta_path = os.path.join(DATA_DIR, META_FILE)
    with _metadata_lock:
        try:
            with open(meta_path, "r", encoding="utf-8") as fp:
                meta = json.load(fp)
                if isinstance(meta, dict):
                    meta_data = meta
                    server_domain = meta.get("serverDomain", "")
                    expires_at = meta.get("expiresAt", "")
                    vpn_port = meta.get("vpnPort", "")
        except FileNotFoundError:
            pass
        except (IOError, OSError, json.JSONDecodeError) as exc:
            app.logger.warning(f"Failed to read or parse metadata file {meta_path}: {exc}")

    try:
        expected_port = int(vpn_port or 0)
    except (TypeError, ValueError):
        expected_port = 0
    announcement_conflicts: List[str] = []
    lnd_audit: Dict[str, Any] = {"readable": False, "settings": [], "conflicts": [], "has_expected_tunnelsats": False}
    cln_audit: Dict[str, Any] = {"readable": False, "settings": [], "conflicts": [], "has_expected_tunnelsats": False}
    lnd_audited = False
    cln_audited = False
    if lnd_detected or os.path.exists(LND_CONFIG_PATH):
        lnd_audited = True
        lnd_audit = audit_node_announcement_config(
            "lnd", LND_CONFIG_PATH, str(server_domain), expected_port
        )
        announcement_conflicts.extend(f"lnd:{item}" for item in lnd_audit["conflicts"])
        if lnd_detected and not lnd_audit["readable"] and not lnd_audit["conflicts"]:
            announcement_conflicts.append("lnd:config-unavailable")
    if cln_detected or (cln_container_config and os.path.exists(cln_container_config)):
        cln_audited = True
        cln_audit = audit_node_announcement_config(
            "cln", cln_container_config, str(server_domain), expected_port
        )
        announcement_conflicts.extend(f"cln:{item}" for item in cln_audit["conflicts"])
        if cln_detected and not cln_audit["readable"] and not cln_audit["conflicts"]:
            announcement_conflicts.append("cln:config-unavailable")

    requested_node_type = str(meta_data.get("nodeType", "")).strip().lower()
    dataplane_target = str(dataplane.get("target_impl", "")).strip().lower()
    requested_target_detected = (
        (requested_node_type == "cln" and cln_detected)
        or (requested_node_type == "lnd" and lnd_detected)
    )
    if dataplane_target in ("lnd", "cln"):
        active_target_impl = dataplane_target
    elif requested_target_detected:
        active_target_impl = requested_node_type
    else:
        active_target_impl = "cln" if (cln_detected and not lnd_detected) else ("lnd" if lnd_detected else (dataplane_target or "lnd"))

    requested_target_impl = requested_node_type if requested_node_type in ("lnd", "cln") else None
    target_switch_pending = bool(
        requested_target_impl
        and requested_target_impl != dataplane_target
    )

    active_routing_active = cln_routing_active if active_target_impl == "cln" else lnd_routing_active
    active_detected = cln_detected if active_target_impl == "cln" else lnd_detected

    # Diagnostics context: which container to address in copy-paste commands and how to
    # recognise the TunnelSats server (domain, WireGuard endpoint host, or its DNS records).
    target_container = str(dataplane.get("target_container") or "")
    server_host_matcher = _server_host_matcher(
        str(server_domain), extra_hosts=[_address_host(str(meta_data.get("wgEndpoint") or ""))]
    )

    def _diagnostics_config_path(impl: str) -> Optional[str]:
        return None if K3S_MODE else resolve_node_config(impl)[1]

    live_announcement_diagnostics: List[Dict[str, Any]] = []
    verification_required = bool(active_detected and active_routing_active)
    verification = meta_data.get(f"{active_target_impl}AnnouncementVerification")
    expected_endpoint = f"{server_domain}:{expected_port}"
    announcement_verified: Optional[bool] = None
    verification_source: Optional[str] = None
    if verification_required:
        if SECURE_MODE or K3S_MODE:
            prefix = f"{active_target_impl}:"
            active_config_conflicts = [c for c in announcement_conflicts if c.startswith(prefix)]
            active_audit = cln_audit if active_target_impl == "cln" else lnd_audit
            has_ts_endpoint = bool(active_audit.get("has_expected_tunnelsats"))
            if not has_ts_endpoint and server_domain and expected_port:
                key_name = "externalhosts" if active_target_impl == "lnd" else "announce-addr"
                announcement_conflicts.append(f"{active_target_impl}:missing-{key_name}={expected_endpoint}")
            announcement_verified = len(active_config_conflicts) == 0 and has_ts_endpoint
            verification_source = "config_file"
        else:
            announcement_verified = bool(
                isinstance(verification, dict)
                and verification.get("verified") is True
                and verification.get("endpoint") == expected_endpoint
            )
            if not announcement_verified and server_domain:
                if active_target_impl == "cln":
                    v_ok, v_rem, v_conf = clean_and_verify_cln_announcements(str(server_domain), expected_port, True)
                    if "cln_initializing" not in v_conf and "cln_not_found" not in v_conf:
                        new_verification = {
                            "endpoint": expected_endpoint,
                            "verified": v_ok,
                            "checkedAt": datetime.now(timezone.utc).isoformat(),
                            "removed": v_rem,
                            "remainingConflicts": v_conf,
                        }
                        _update_announcement_metadata(meta_path, "cln", verification=new_verification)
                        announcement_verified = v_ok
                else:
                    v_ok, v_rem, v_conf = clean_and_verify_lnd_announcements(str(server_domain), expected_port, True)
                    if "lnd_initializing" not in v_conf and "lnd_not_found" not in v_conf:
                        new_verification = {
                            "endpoint": expected_endpoint,
                            "verified": v_ok,
                            "checkedAt": datetime.now(timezone.utc).isoformat(),
                            "removed": v_rem,
                            "remainingConflicts": v_conf,
                        }
                        _update_announcement_metadata(meta_path, "lnd", verification=new_verification)
                        announcement_verified = v_ok
                if not v_ok:
                    live_announcement_diagnostics = _diagnostics_or_empty(lambda: build_live_announcement_diagnostics(
                        active_target_impl, v_conf, str(server_domain), expected_port,
                        config_path=_diagnostics_config_path(active_target_impl),
                        cli_prefix=_node_cli_prefix(active_target_impl, target_container),
                        is_server_host=server_host_matcher,
                    ))
            elif not announcement_verified:
                live_announcement_diagnostics = _diagnostics_or_empty(lambda: [_announcement_diagnostic(
                    active_target_impl, "missing-endpoint", "live_gossip",
                    detail="endpoint_unknown",
                    config_path=_diagnostics_config_path(active_target_impl),
                )])
            verification_source = "live_gossip"

    def _build_config_diagnostics() -> List[Dict[str, Any]]:
        collected: List[Dict[str, Any]] = []
        for impl, audited, audit, detected in (
            ("lnd", lnd_audited, lnd_audit, lnd_detected),
            ("cln", cln_audited, cln_audit, cln_detected),
        ):
            if not audited:
                continue
            collected.extend(build_config_announcement_diagnostics(
                impl, audit, str(server_domain), expected_port,
                detected=detected,
                config_path=_diagnostics_config_path(impl),
                cli_prefix=_node_cli_prefix(impl, target_container),
                is_server_host=server_host_matcher,
                check_missing_endpoint=bool(
                    (SECURE_MODE or K3S_MODE) and verification_required and impl == active_target_impl
                ),
            ))
        return collected

    announcement_diagnostics = _diagnostics_or_empty(_build_config_diagnostics)
    announcement_diagnostics.extend(live_announcement_diagnostics)

    dns_protected = bool(dataplane.get("dns_protected", False))
    effective_rules_synced = bool(dataplane["rules_synced"])
    effective_error = dataplane["last_error"]
    last_error_code: Optional[str] = None
    if target_switch_pending:
        effective_rules_synced = False
        effective_error = TARGET_SWITCH_PENDING_ERROR
        last_error_code = STATUS_ERROR_TARGET_SWITCH_PENDING
    elif announcement_conflicts:
        effective_rules_synced = False
        effective_error = ANNOUNCEMENT_CONFLICT_ERROR
        last_error_code = STATUS_ERROR_ANNOUNCEMENT_CONFLICT
    elif verification_required and not announcement_verified:
        effective_rules_synced = False
        effective_error = ANNOUNCEMENT_VERIFICATION_ERROR
        last_error_code = STATUS_ERROR_ANNOUNCEMENT_UNVERIFIED
    elif (K3S_MODE or dataplane.get("dataplane_mode") == "k3s") and effective_rules_synced and not dns_protected:
        effective_rules_synced = False
        if not effective_error:
            effective_error = "k3s: External DNS protection is not active"
            last_error_code = STATUS_ERROR_K3S_DNS_UNPROTECTED
    if last_error_code is None:
        last_error_code = dataplane_error_code(effective_error)

    # Enrich with location metadata using unified helper
    lat, lng, label, flag = None, None, None, None
    if server_domain:
        s_id = server_domain.split(".")[0]
        meta = get_server_geodata(s_id)
        
        if meta:
            lat = meta["lat"]
            lng = meta["lng"]
            label = meta["label"]
            flag = meta["flag"]

    return jsonify(
        {
            "wg_status": wg_status,
            "wg_pubkey": wg_pubkey,
            "vpn_internal_ip": vpn_internal_ip,
            "configs_found": configs,
            "version": read_app_version(),
            "lnd_ip": lnd_ip,
            "cln_ip": cln_ip,
            "vpn_active": vpn_active,
            "lnd_detected": lnd_detected,
            "cln_detected": cln_detected,
            "lnd_routing_active": lnd_routing_active,
            "cln_routing_active": cln_routing_active,
            "server_domain": server_domain,
            "lat": lat,
            "lng": lng,
            "label": label,
            "flag": flag,
            "expires_at": expires_at,
            "vpn_port": vpn_port,
            "dataplane_mode": dataplane["dataplane_mode"],
            "secure_mode": SECURE_MODE,
            "target_container": dataplane["target_container"],
            "target_ip": dataplane["target_ip"],
            "target_impl": active_target_impl,
            "requested_target_impl": requested_target_impl,
            "target_switch_pending": target_switch_pending,
            "announcement_verified": announcement_verified,
            "announcement_verification_source": verification_source,
            "docker_network": dataplane["docker_network"],
            "forwarding_port": dataplane["forwarding_port"],
            "k3s_bypass_cidrs": dataplane.get("k3s_bypass_cidrs", []),
            "rules_synced": effective_rules_synced,
            "ipv4_rules_synced": dataplane.get("ipv4_rules_synced", False),
            "ipv6_rules_synced": dataplane.get("ipv6_rules_synced", False),
            "dns_protected": dns_protected,
            "decommissioned": dataplane.get("decommissioned", False),
            "ipv6_policy": dataplane.get("ipv6_policy", "deny"),
            "target_ipv6_addresses": dataplane.get("target_ipv6_addresses", []),
            "target_ipv6_default_route": dataplane.get("target_ipv6_default_route", False),
            "last_reconcile_at": dataplane["last_reconcile_at"],
            "last_error": effective_error,
            "last_error_code": last_error_code,
            "announcement_conflicts": announcement_conflicts,
            "announcement_diagnostics": announcement_diagnostics,
            "announcement_help": _announcement_help_or_empty(
                active_target_impl, str(server_domain), expected_port, target_container
            ),
            "announcement_verification_required": verification_required,
        }
    )


@app.route("/api/local/upload-config", methods=["POST"])
def upload_config():
    payload = request.get_json(silent=True) or {}
    config_text = payload.get("config")

    # Backward compatibility for older UI calls sending multipart form data.
    if not isinstance(config_text, str) and request.form:
        config_text = request.form.get("config_text")

    config_text = str(config_text or "")
    if not config_text.strip():
        return jsonify({"success": False, "error": "Missing WireGuard configuration text."}), 400

    if not _has_required_wireguard_blocks(config_text):
        return (
            jsonify(
                {
                    "success": False,
                    "error": "Invalid WireGuard configuration format. Missing [Interface] or [Peer] block.",
                }
            ),
            400,
        )

    sanitized_config_text, sanitize_error = _sanitize_wireguard_config(config_text)
    if sanitize_error:
        return jsonify({"success": False, "error": sanitize_error}), 400
    config_text = sanitized_config_text or ""

    private_key = _extract_interface_private_key(config_text)
    if not private_key:
        return (
            jsonify(
                {
                    "success": False,
                    "error": "Invalid WireGuard configuration format. Missing Interface PrivateKey.",
                }
            ),
            400,
        )

    wg_public_key = _derive_wg_public_key(private_key)
    if not wg_public_key:
        return jsonify({"success": False, "error": "Unable to derive public key from provided PrivateKey."}), 400

    # Only an explicit JSON boolean true counts as confirmation.
    confirm = payload.get("confirm") is True
    # Always attempt authoritative status check via cache.
    # This prevents redundant 10s waits on confirmation retries while keeping SOT in the backend.
    status_info = _fetch_subscription_status_cached(wg_public_key)
    is_expired = False
    if status_info:
        # Check if the API explicitly says disabled or if expiration is past
        if status_info.get("status") == "disabled" or _is_timestamp_expired(status_info.get("expiry")):
            is_expired = True

    # If authoritative check fails or is missing, fall back to parsing comments (less reliable but safe)
    parsed = _parse_config_comments(config_text)
    if not is_expired and status_info is None:
        if _is_timestamp_expired(parsed.get("expiresAt")):
            is_expired = True

    server_domain = (status_info.get("server_domain") if status_info else None) or parsed.get("serverDomain", "")
    server_id = _server_id_from_domain(server_domain)
    expires_at = (status_info.get("expiry") if status_info else None) or parsed.get("expiresAt", "")
    vpn_port = parsed.get("vpnPort", 0)
    if not vpn_port:
        vpn_port = _port_from_endpoint(parsed.get("wgEndpoint", ""))

    meta = {
        "serverId": server_id,
        "serverDomain": server_domain,
        "wgEndpoint": parsed.get("wgEndpoint", ""),
        "wgPublicKey": wg_public_key,
        "expiresAt": expires_at,
        "vpnPort": vpn_port,
        "importedAt": datetime.now(timezone.utc).isoformat(),
        "isExpired": is_expired,
    }

    # If it is expired and NOT confirmed, stop here and return a warning
    if is_expired and not confirm:
        return jsonify(
            {
                "success": True,
                "warning": "Expired",
                "is_expired": True,
                "meta": {
                    "serverId": server_id,
                    "serverDomain": server_domain,
                    "wgPublicKey": wg_public_key,
                    "expiresAt": expires_at,
                    "vpnPort": vpn_port,
                },
            }
        )

    config_text = _ensure_peer_persistent_keepalive(config_text, keepalive=25)

    if not _persist_tunnelsats_config_and_meta(config_text, meta):
        return jsonify({"success": False, "error": "Failed to save configuration on disk."}), 500

    return jsonify(
        {
            "success": True,
            "message": "Configuration saved and parsed.",
            "is_expired": is_expired,
            "meta": {
                "serverId": server_id,
                "wgPublicKey": wg_public_key,
                "expiresAt": expires_at,
                "vpnPort": vpn_port,
            },
        }
    )


@app.route("/api/local/export-config", methods=["GET"])
def export_config():
    """Download active WireGuard config file (tunnelsats.conf)."""
    if not os.path.exists(DATA_DIR):
        return jsonify({"success": False, "error": "No configuration directory found."}), 404
    conf_path = os.path.join(DATA_DIR, "tunnelsats.conf")
    if not os.path.exists(conf_path):
        # Filter to tunnelsats-prefixed profiles only — matching runtime discovery;
        # non-TunnelSats configs (e.g. mullvad.conf) must never be exported as a backup.
        confs = [
            f for f in os.listdir(DATA_DIR)
            if f.startswith("tunnelsats") and f.endswith(".conf") and not f.endswith(".bak")
        ]
        if not confs:
            return jsonify({"success": False, "error": "No active WireGuard configuration file found."}), 404
        # Pick the most recently modified profile when tunnelsats.conf is absent
        # to avoid returning stale credentials from an older subscription.
        confs.sort(key=lambda f: os.path.getmtime(os.path.join(DATA_DIR, f)), reverse=True)
        conf_path = os.path.join(DATA_DIR, confs[0])

    return send_file(
        conf_path,
        as_attachment=True,
        download_name="tunnelsats.conf",
        mimetype="text/plain"
    )


@app.route("/api/local/restart", methods=["POST"])
def restart_tunnel():
    try:
        with open("/tmp/tunnelsats_restart_trigger", "w", encoding="utf-8") as fp:
            fp.write("trigger")
        return jsonify({"success": True, "message": "Restarting tunnel..."})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/local/reconcile", methods=["POST"])
def reconcile_tunnel():
    request_id = str(uuid.uuid4())
    try:
        ensure_reconcile_dirs()
        atomic_write_text(reconcile_trigger_path(request_id), f"{request_id}\n")
    except Exception as exc:
        return jsonify({"error": f"Unable to trigger reconcile: {str(exc)}"}), 500

    return (
        jsonify(
            {
                "success": True,
                "accepted": True,
                "request_id": request_id,
                "status_url": f"/api/local/reconcile/{request_id}",
            }
        ),
        202,
    )


@app.route("/api/local/reconcile/<request_id>", methods=["GET"])
def reconcile_status(request_id):
    request_id = sanitize_request_id(request_id)
    if not request_id:
        return jsonify({"error": "Invalid request_id"}), 400

    result = read_reconcile_result(request_id)
    if isinstance(result, dict) and result.get("request_id") == request_id:
        return jsonify({"success": reconcile_result_success(result), "complete": True, **result})

    legacy_result = read_legacy_reconcile_result()
    if isinstance(legacy_result, dict) and legacy_result.get("request_id") == request_id:
        return jsonify({"success": reconcile_result_success(legacy_result), "complete": True, **legacy_result})

    return jsonify({"success": True, "complete": False, "request_id": request_id}), 202


@app.route("/api/local/meta", methods=["GET"])
def get_metadata():
    meta_data = {}
    meta_path = os.path.join(DATA_DIR, META_FILE)
    with _metadata_lock:
        try:
            with open(meta_path, "r", encoding="utf-8") as fp:
                meta_data = json.load(fp)
                if isinstance(meta_data, dict):
                    meta_data.pop("presharedKey", None)
                    meta_data.pop("paymentHash", None)
                else:
                    meta_data = {}
        except FileNotFoundError:
            pass
        except (json.JSONDecodeError, IOError) as exc:
            app.logger.error(f"Error reading metadata file {meta_path}: {exc}")
    return jsonify(meta_data)


@app.route("/api/local/configure-node", methods=["POST"])
def configure_node():
    app.logger.info("Action Request: Configuring Node")
    payload = request.get_json(silent=True) or {}
    node_type = str(payload.get("nodeType", "")).strip().lower()
    confirmed = payload.get("confirmAddressChanges") is True
    retain_tor = payload.get("retainTorAnnouncements", True) is not False

    if node_type not in ("lnd", "cln"):
        return jsonify({"success": False, "error": "Invalid nodeType. Use 'lnd' or 'cln'."}), 400

    meta_path = os.path.join(DATA_DIR, META_FILE)
    with _metadata_lock:
        try:
            with open(meta_path, "r", encoding="utf-8") as fp:
                meta = json.load(fp)
            if not isinstance(meta, dict):
                return jsonify({"success": False, "error": "Invalid tunnelsats metadata format."}), 400
        except FileNotFoundError:
            return jsonify({"success": False, "error": "Missing tunnelsats metadata file."}), 400
        except (IOError, OSError, json.JSONDecodeError):
            return jsonify({"success": False, "error": "Unable to read tunnelsats metadata file."}), 500

        dns = str(meta.get("serverDomain", "")).strip()
        try:
            port = int(meta.get("vpnPort", 0))
        except (TypeError, ValueError):
            port = 0

        if not dns or port <= 0:
            return jsonify({"success": False, "error": "Metadata is missing vpnPort or serverDomain."}), 400

        previous_node_type = str(meta.get("nodeType", "")).strip().lower()

        if not _is_metadata_writable(meta_path):
            return jsonify({"success": False, "error": "Unable to write to metadata file."}), 500

    def _persist_and_reconcile_target(selected_node_type: str):
        if not _persist_node_type(meta_path, selected_node_type):
            return False, "Failed to persist nodeType to metadata."

        reconciled, result = reconcile_target_and_wait(selected_node_type)
        if reconciled:
            return True, ""

        rollback_target = previous_node_type if previous_node_type in ("lnd", "cln") else None
        rollback_persisted = _persist_node_type(meta_path, rollback_target)
        detail = str(result.get("error") or "Dataplane reconciliation failed.")
        if not rollback_persisted:
            detail = f"{detail} Failed to restore the previous nodeType."
        elif rollback_target != selected_node_type:
            rollback_reconciled, rollback_result = reconcile_target_and_wait(rollback_target)
            if not rollback_reconciled:
                rollback_error = str(
                    rollback_result.get("error") or "Previous target reconciliation failed."
                )
                detail = f"{detail} Failed to reconcile the previous target: {rollback_error}"
        return False, detail

    if SECURE_MODE:
        confirm_manual_setup = (
            payload.get("confirmManualConfigure") is True
            or payload.get("confirmManualSetup") is True
        )
        if confirm_manual_setup:
            target_ready, target_error = _persist_and_reconcile_target(node_type)
            if not target_ready:
                return jsonify({
                    "success": False,
                    "error": target_error,
                    "manual_mode": True,
                    "requires_confirmation": True,
                    "reconciled": False,
                    "node_type": node_type,
                    "port": port,
                    "dns": dns,
                }), 500
            return jsonify({
                "success": True,
                "manual_mode": True,
                "requires_confirmation": False,
                "reconciled": True,
                "node_type": node_type,
                "port": port,
                "dns": dns,
            })

        if not _persist_node_type(meta_path, node_type):
            return jsonify({"success": False, "error": "Failed to persist nodeType to metadata."}), 500
        container_config_path, config_path = resolve_node_config(node_type)
        cli_prefix = _node_cli_prefix(node_type)
        # Read-only audit: Secure Mode can read the node config but never writes it.
        diagnostics = _diagnostics_or_empty(lambda: build_config_announcement_diagnostics(
            node_type,
            audit_node_announcement_config(node_type, container_config_path, dns, port),
            dns,
            port,
            detected=True,
            config_path=config_path,
            cli_prefix=cli_prefix,
            is_server_host=_server_host_matcher(
                dns, extra_hosts=[_address_host(str(meta.get("wgEndpoint") or ""))]
            ),
            check_missing_endpoint=True,
        ))
        gossip_commands = [entry["withdraw_command"] for entry in diagnostics if entry.get("withdraw_command")]
        if node_type == "lnd":
            config_lines = [
                f"externalhosts={dns}:{port}",
                "nat=false",
            ]
            cleanup_lines = ["externalip=", "nat=true", "nat=1"]
            gossip_command = (
                f"{cli_prefix} lncli peers updatenodeannouncement "
                "--address_remove=<your-old-ip>:9735"
            )
        else:
            config_lines = [
                "bind-addr=0.0.0.0:9736",
                f"announce-addr={dns}:{port}",
                "ip-discovery=false",
                "always-use-proxy=false",
            ]
            cleanup_lines = ["announce-addr=<non-TunnelSats-address>", "ip-discovery=true"]
            gossip_command = ""

        return jsonify({
            "success": True,
            "manual_mode": True,
            "requires_confirmation": True,
            "reconciled": False,
            "node_type": node_type,
            "config_path": config_path,
            "config_lines": config_lines,
            "cleanup_lines": cleanup_lines,
            "gossip_command": gossip_command,
            "gossip_commands": gossip_commands,
            "diagnostics": diagnostics,
            "retain_tor_supported": True,
            "historical_warning": (
                "Withdrawing an address updates live Lightning gossip, but third-party "
                "explorers and historical collectors may retain addresses published earlier."
            ),
            "port": port,
            "dns": dns
        })

    lnd_pending_key = "lndRestartPending"
    cln_pending_key = "clnRestartPending"

    if node_type == "lnd":
        if not lnd_exists():
            app.logger.warning("LND container not found. Skipping configuration.")
            return jsonify({
                "success": False, 
                "error": "LND container not found, skipping.",
                "lnd": False, 
                "cln": False, 
                "lnd_changed": False, 
                "port": port, 
                "dns": dns
            }), 422

        audit = audit_node_announcement_config("lnd", LND_CONFIG_PATH, dns, port)
        if audit["conflicts"] and not confirmed:
            return jsonify({
                "success": False,
                "requires_confirmation": True,
                "node_type": "lnd",
                "conflicts": audit["conflicts"],
                "diagnostics": _confirmation_diagnostics("lnd", audit, dns, port, meta),
                "retain_tor_supported": True,
                "error": ANNOUNCEMENT_CONFLICT_ERROR,
            }), 409
        original_settings = [entry["line"] for entry in audit["settings"]]
        if not _update_announcement_metadata(
            meta_path, "lnd", backup_lines=original_settings, verification=None
        ):
            return jsonify({"success": False, "error": "Failed to back up LND announcement settings."}), 500
        lnd_processed, lnd_changed, _ = apply_node_announcement_protection(
            "lnd", LND_CONFIG_PATH, dns, port, retain_tor
        )
        if not lnd_processed:
            return jsonify({"success": False, "error": "Failed to modify LND config."}), 500
        _set_restart_pending(meta_path, meta, lnd_pending_key, False)

        verified, removed, remaining = clean_and_verify_lnd_announcements(dns, port, retain_tor)
        verification = {
            "endpoint": f"{dns}:{port}",
            "verified": verified,
            "checkedAt": datetime.now(timezone.utc).isoformat(),
            "removed": removed,
            "remainingConflicts": remaining,
        }
        if not _update_announcement_metadata(meta_path, "lnd", verification=verification):
            return jsonify({"success": False, "error": "Failed to save LND announcement verification."}), 500
        if not verified:
            return jsonify({
                "success": False,
                "error": ANNOUNCEMENT_VERIFICATION_ERROR,
                "removed_addresses": removed,
                "remaining_conflicts": remaining,
            }), 500

        # Commit the desired target only after node configuration succeeds, then
        # wait for the dataplane to confirm that the same target is protected.
        target_ready, target_error = _persist_and_reconcile_target("lnd")
        if not target_ready:
            return jsonify({"success": False, "error": target_error}), 500
        return jsonify(
            {
                "success": True,
                "lnd": True,
                "cln": False,
                "lnd_changed": lnd_changed,
                "announcement_verified": True,
                "removed_addresses": removed,
                "port": port,
                "dns": dns,
            }
        )

    # CLN target
    if not cln_exists():
        app.logger.warning("CLN container not found. Skipping configuration.")
        return jsonify({
            "success": False, 
            "error": "CLN container not found, skipping.",
            "lnd": False, 
            "cln": False, 
            "cln_changed": False, 
            "port": port, 
            "dns": dns
        }), 422

    cln_container_config, _ = resolve_node_config("cln")
    audit = audit_node_announcement_config("cln", cln_container_config, dns, port)
    if audit["conflicts"] and not confirmed:
        return jsonify({
            "success": False,
            "requires_confirmation": True,
            "node_type": "cln",
            "conflicts": audit["conflicts"],
            "diagnostics": _confirmation_diagnostics("cln", audit, dns, port, meta),
            "retain_tor_supported": True,
            "error": ANNOUNCEMENT_CONFLICT_ERROR,
        }), 409
    original_settings = [entry["line"] for entry in audit["settings"]]
    if not _update_announcement_metadata(meta_path, "cln", backup_lines=original_settings):
        return jsonify({"success": False, "error": "Failed to back up CLN announcement settings."}), 500
    cln_processed, cln_changed, _ = apply_node_announcement_protection(
        "cln", cln_container_config, dns, port, retain_tor
    )
    if not cln_processed:
        return jsonify({"success": False, "error": "Failed to modify CLN config."}), 500

    if not restart_container_by_pattern(CLN_CONTAINER_PATTERN):
        _set_restart_pending(meta_path, meta, cln_pending_key, True)
        return jsonify({"success": False, "error": "Failed to restart CLN container."}), 500
    _set_restart_pending(meta_path, meta, cln_pending_key, False)

    # Commit the desired target only after node configuration succeeds, then
    # wait for the dataplane to confirm that the same target is protected.
    target_ready, target_error = _persist_and_reconcile_target("cln")
    if not target_ready:
        return jsonify({"success": False, "error": target_error}), 500
    return jsonify(
        {
            "success": True,
            "lnd": False,
            "cln": True,
            "cln_changed": cln_changed,
            "port": port,
            "dns": dns,
        }
    )

@app.route("/api/local/restore-node", methods=["POST"])
def restore_node():
    app.logger.info("Action Request: Restoring networking to default")
    if SECURE_MODE:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            payload = {}
        confirmed = bool(
            payload.get("confirmManualRestore")
            or payload.get("confirmed")
        )
        targets = []
        lnd_detected = lnd_exists() or os.path.exists(LND_CONFIG_PATH)
        cln_container_config, _ = resolve_node_config("cln")
        cln_detected = cln_exists() or (cln_container_config and os.path.exists(cln_container_config))
        if lnd_detected:
            _, lnd_path = resolve_node_config("lnd")
            targets.append({
                "node_type": "lnd",
                "config_path": lnd_path,
                "config_lines": [
                    "externalhosts=",
                    "nat=false",
                ],
                "restore_notes": [
                    "Only re-enable old externalip or nat settings you deliberately want to publish.",
                    "Previously published IP addresses may remain in third-party historical snapshots.",
                ],
            })
        if cln_detected:
            _, cln_path = resolve_node_config("cln")
            targets.append({
                "node_type": "cln",
                "config_path": cln_path,
                "config_lines": [
                    "bind-addr=",
                    "announce-addr=",
                    "ip-discovery=false",
                    "always-use-proxy=",
                ],
                "restore_notes": [
                    "Only re-enable old announce-addr or ip-discovery settings you deliberately want to publish."
                ],
            })
        if not confirmed:
            return jsonify({
                "success": True,
                "manual_mode": True,
                "restore": True,
                "requires_confirmation": True,
                "decommissioned": False,
                "targets": targets
            })
        decommissioned, decomm_result = decommission_dataplane_and_wait()
        if not decommissioned:
            return jsonify({
                "success": False,
                "error": str(decomm_result.get("error") or "Failed to decommission dataplane."),
            }), 500
        return jsonify({
            "success": True,
            "manual_mode": True,
            "restore": True,
            "requires_confirmation": False,
            "decommissioned": True,
            "targets": targets
        })

    lnd_processed, lnd_changed, cln_processed, cln_changed = False, False, False, False
    errors = []

    meta_path = os.path.join(DATA_DIR, META_FILE)
    meta: Dict[str, Any] = {}
    try:
        with _metadata_lock:
            with open(meta_path, "r", encoding="utf-8") as meta_fp:
                loaded_meta = json.load(meta_fp)
                if isinstance(loaded_meta, dict):
                    meta = loaded_meta
    except (FileNotFoundError, IOError, OSError, json.JSONDecodeError):
        pass
    dns = str(meta.get("serverDomain", "")).strip()
    try:
        port = int(meta.get("vpnPort", 0))
    except (TypeError, ValueError):
        port = 0

    lnd_config_exists = os.path.exists(LND_CONFIG_PATH)
    lnd_backup = _backup_lines_from_meta(meta, "lnd")
    cln_container_config, _ = resolve_node_config("cln")
    cln_config_exists = bool(cln_container_config and os.path.exists(cln_container_config))
    cln_backup = _backup_lines_from_meta(meta, "cln")

    meta_target = str(meta.get("nodeType") or "").strip().lower()
    dataplane_target = str(read_dataplane_state().get("target_impl") or "").strip().lower()
    lnd_service_configured = bool(K3S_MODE and os.getenv("LND_K8S_SERVICE", "").strip())
    cln_service_configured = bool(K3S_MODE and os.getenv("CLN_K8S_SERVICE", "").strip())

    lnd_relevant = bool(
        lnd_config_exists
        or lnd_backup is not None
        or meta_target == "lnd"
        or dataplane_target == "lnd"
        or lnd_service_configured
        or lnd_exists is not _DEFAULT_LND_EXISTS
    )
    cln_relevant = bool(
        cln_config_exists
        or cln_backup is not None
        or meta_target == "cln"
        or dataplane_target == "cln"
        or cln_service_configured
        or cln_exists is not _DEFAULT_CLN_EXISTS
    )
    if not lnd_relevant and not cln_relevant:
        lnd_relevant = True
        cln_relevant = True

    if lnd_relevant:
        lnd_lookup_ok, lnd_detected = detect_node_running_state("lnd")
        if not lnd_lookup_ok:
            errors.append("Failed to determine LND running state.")
    else:
        lnd_lookup_ok, lnd_detected = True, False

    if cln_relevant:
        cln_lookup_ok, cln_detected = detect_node_running_state("cln")
        if not cln_lookup_ok:
            errors.append("Failed to determine CLN running state.")
    else:
        cln_lookup_ok, cln_detected = True, False

    if errors:
        return jsonify({"success": False, "error": " ".join(errors)}), 500

    if lnd_backup is None:
        lnd_processed, lnd_changed = comment_out_config_lines(
            LND_CONFIG_PATH,
            ("externalhosts=", "tor.skip-proxy-for-clearnet-targets="),
        )
    else:
        lnd_processed, lnd_changed = restore_node_announcement_config(
            "lnd", LND_CONFIG_PATH, lnd_backup, dns, port
        )
    if (lnd_detected or lnd_config_exists or lnd_backup is not None) and not lnd_processed:
        errors.append("Failed to restore LND configuration.")
    elif lnd_processed and lnd_detected:
        if not restart_container_by_pattern(LND_CONTAINER_PATTERN, is_lnd=True):
            errors.append("Failed to restart LND container.")
    elif lnd_processed and not lnd_detected:
        app.logger.info("LND config restored, but no running LND container detected. Skipping restart.")

    if cln_backup is None:
        cln_processed, cln_changed = comment_out_config_lines(
            cln_container_config,
            ("bind-addr=", "announce-addr=", "always-use-proxy="),
        )
    else:
        cln_processed, cln_changed = restore_node_announcement_config(
            "cln", cln_container_config, cln_backup, dns, port
        )
    if (cln_detected or cln_config_exists or cln_backup is not None) and not cln_processed:
        errors.append("Failed to restore CLN configuration.")
    elif cln_processed and cln_detected:
        if not restart_container_by_pattern(CLN_CONTAINER_PATTERN):
            errors.append("Failed to restart CLN container.")
    elif cln_processed and not cln_detected:
        app.logger.info("CLN config restored, but no running CLN container detected. Skipping restart.")

    if errors:
        return jsonify({"success": False, "error": " ".join(errors)}), 500

    decommissioned, decomm_result = decommission_dataplane_and_wait()
    if not decommissioned:
        return jsonify({
            "success": False,
            "error": str(decomm_result.get("error") or "Failed to decommission dataplane."),
        }), 500

    if meta:
        if lnd_processed:
            _update_announcement_metadata(meta_path, "lnd", clear_backup=True, verification=None)
        if cln_processed:
            _update_announcement_metadata(meta_path, "cln", clear_backup=True)

    return jsonify(
        {
            "lnd": lnd_processed,
            "cln": cln_processed,
            "lnd_changed": lnd_changed,
            "cln_changed": cln_changed,
            "decommissioned": True,
        }
    )


if __name__ == "__main__":
    if TUNNELSATS_ROLE == "daemon":
        socket_uid = int(os.environ.get("TUNNELSATS_WEB_UID", "1000"))
        socket_gid = int(os.environ.get("TUNNELSATS_WEB_GID", "1000"))
        app.logger.info("Starting private daemon API on %s", DAEMON_SOCKET_PATH)
        daemon_server = UnixWSGIServer(
            DAEMON_SOCKET_PATH,
            app,
            socket_mode=0o660,
            socket_uid=socket_uid,
            socket_gid=socket_gid,
        )
        try:
            daemon_server.serve_forever()
        finally:
            daemon_server.server_close()
        raise SystemExit(0)

    dashboard_host, dashboard_port = dashboard_bind_config()
    app.logger.info(
        "Starting internal dashboard backend on %s:%d",
        dashboard_host,
        dashboard_port,
    )
    app.run(host=dashboard_host, port=dashboard_port)
