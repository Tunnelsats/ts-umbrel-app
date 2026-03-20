import json
import os
import re
import subprocess
import uuid
from datetime import datetime, timezone
import time
from ipaddress import ip_address, ip_network
from typing import Dict, Any, List, Optional, Tuple, Iterable

import requests
import yaml
from flask import Flask, abort, jsonify, request, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

# Ensure verbose logging for container restarts is visible and unbuffered
import logging
import sys

app = Flask(__name__, static_folder="../web", static_url_path="")
# Umbrel uses a reverse proxy. Parse X-Forwarded-* headers before IP restrictions.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

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
DATA_DIR = "/data"
META_FILE = "tunnelsats-meta.json"
DOCKER_SOCK = "/var/run/docker.sock"
STATE_FILE = "/tmp/tunnelsats_state.json"
RECONCILE_TRIGGER_DIR = "/tmp/tunnelsats_reconcile_trigger.d"
RECONCILE_RESULT_DIR = "/tmp/tunnelsats_reconcile_result.d"
RECONCILE_RESULT_LEGACY = "/tmp/tunnelsats_reconcile_result.json"
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
APP_MANIFEST_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "umbrel-app.yml"))
LND_CONFIG_PATH = "/lightning-data/lnd/lnd.conf"
CLN_CONFIG_PATH = "/lightning-data/cln/config"
LND_RESTART_DELAY = 3  # Seconds to wait for middleware to generate umbrel-lnd.conf
LND_CONTAINER_PATTERN = r"^lightning[_-]lnd[_-]\d+$"
LND_MIDDLEWARE_PATTERN = r"^lightning[_-]app[_-]\d+$"
CLN_CONTAINER_PATTERN = r"(^|[_-])(core-lightning|clightning|lightningd)([_-]|$)"

ALLOWED_NETWORKS = (
    ip_network("127.0.0.0/8"),
    ip_network("10.0.0.0/8"),
    ip_network("172.16.0.0/12"),
    ip_network("192.168.0.0/16"),
    ip_network("::1/128"),
    ip_network("fc00::/7"),
    # NOTE: fe80::/10 (IPv6 link-local) is intentionally excluded.
)


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


def is_loopback_ip(remote_addr):
    if not remote_addr:
        return False
    try:
        return ip_address(remote_addr).is_loopback
    except ValueError:
        return False


def normalize_version(raw_version):
    version_text = str(raw_version or "").strip()
    if not version_text:
        return "v3.0.0"
    if version_text.startswith("v"):
        return version_text
    return f"v{version_text}"


def read_app_version():
    if not os.path.exists(APP_MANIFEST_PATH):
        return "v3.0.0"

    try:
        with open(APP_MANIFEST_PATH, "r", encoding="utf-8") as manifest_fp:
            manifest = yaml.safe_load(manifest_fp) or {}
            return normalize_version(manifest.get("version", "3.0.0"))
    except Exception:
        return "v3.0.0"


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
        line_str = str(line)
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
            line_str = str(line)
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
            updated_section.append(str(line))

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


def _set_restart_pending(meta_path, meta, key, is_pending):
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
    except (IOError, OSError) as exc:
        app.logger.warning(f"Failed to persist restart-pending state for {key}: {exc}")
        return False

    return True


def _has_required_wireguard_blocks(config_text):
    has_interface = bool(re.search(r"^\s*\[Interface\]\s*$", config_text, flags=re.IGNORECASE | re.MULTILINE))
    has_peer = bool(re.search(r"^\s*\[Peer\]\s*$", config_text, flags=re.IGNORECASE | re.MULTILINE))
    return has_interface and has_peer


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


def container_ip_by_match(pattern, containers=None):
    if containers is None:
        containers = docker_api("/containers/json?all=0")
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
        containers = docker_api("/containers/json?all=0")
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


def restart_container_by_pattern(pattern, is_lnd=False):
    # Specialized LND event chain to accommodate umbrelOS Node.js middleware
    if is_lnd:
        app.logger.info("Triggering sequential LND restart (middleware -> daemon)")
        
        # 1. Restart middleware (strictly lightning_app_1, excluding proxy)
        middleware_id = container_id_by_match(LND_MIDDLEWARE_PATTERN)
        if middleware_id:
            app.logger.info(f"Found LND middleware container (ID: {middleware_id[:12]}). Restarting...")
            res = docker_api_post(f"/containers/{middleware_id}/restart")
            if not res:
                app.logger.error("LND middleware restart failed. Aborting sequential restart.")
                return False
            app.logger.info("LND middleware restart successful.")
        else:
            app.logger.error("Failed to locate LND middleware container.")
            return False
        
        # 2. Wait for middleware to ingest lnd.conf and generate umbrel-lnd.conf
        app.logger.info(f"Waiting {LND_RESTART_DELAY} seconds for middleware configuration generation...")
        time.sleep(LND_RESTART_DELAY)
        
        # 3. Restart LND daemon
        daemon_id = container_id_by_match(LND_CONTAINER_PATTERN)
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
        "rules_synced": False,
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


def reconcile_trigger_path(request_id):
    return os.path.join(RECONCILE_TRIGGER_DIR, f"{request_id}.trigger")


def reconcile_result_path(request_id):
    return os.path.join(RECONCILE_RESULT_DIR, f"{request_id}.json")


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


@app.before_request
def restrict_local_api():
    if request.path.startswith("/api/local") or request.path == "/api/subscription/renew":
        proxyfix_orig = request.environ.get("werkzeug.proxy_fix.orig", {}) or {}
        direct_remote_addr = proxyfix_orig.get("REMOTE_ADDR") or request.remote_addr

        # Trust X-Forwarded-For only when the immediate peer is loopback (local reverse proxy).
        # Direct clients can otherwise spoof forwarded headers to bypass local-network checks.
        if is_loopback_ip(direct_remote_addr):
            effective_remote_addr = request.remote_addr
        else:
            effective_remote_addr = direct_remote_addr

        if not client_is_allowed(effective_remote_addr):
            abort(403)


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
    return send_from_directory(app.static_folder, "index.html")


@app.route("/<path:path>")
def serve_static(path):
    return send_from_directory(app.static_folder, path)


# --- API PROXY ROUTES ---

@app.route("/api/servers", methods=["GET"])
def get_servers():
    return proxy_request("GET", "servers")


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


def _update_local_metadata(subscription_data: Dict[str, Any], payment_hash: Optional[str] = None) -> bool:
    """
    Update tunnelsats-meta.json with latest subscription data (e.g. after renewal).
    Only updates fields that are present in subscription_data.
    """
    meta_path = os.path.join(DATA_DIR, META_FILE)
    if not os.path.exists(meta_path):
        app.logger.warning(f"Metadata file not found at {meta_path}; skipping sync.")
        return False

    if not isinstance(subscription_data, dict):
        app.logger.warning("Metadata sync skipped: subscription payload is not a JSON object.")
        return False

    try:
        with open(meta_path, "r", encoding="utf-8") as fp:
            meta = json.load(fp)
    except (IOError, json.JSONDecodeError) as exc:
        app.logger.warning(f"Failed to read metadata for sync: {exc}")
        return False

    if not isinstance(meta, dict):
        app.logger.warning("Metadata sync skipped: metadata file is not a JSON object.")
        return False

    changed = False
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


@app.route("/api/subscription/claim", methods=["POST"])
def claim_subscription():
    # If the claim was successful, intercept and persist WireGuard config + metadata.
    url = f"{TUNNELSATS_API_URL}/subscription/claim"
    try:
        resp = requests.post(url, json=request.json, headers={"Content-Type": "application/json"}, timeout=10)
        if resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError:
                data = {}

            full_config = data.get("fullConfig") or data.get("wireguardConfig")
            if full_config:
                os.makedirs(DATA_DIR, exist_ok=True)
                backup_existing_wireguard_configs()

                subscription_data = data.get("subscription", {}) if isinstance(data.get("subscription"), dict) else {}
                server_data = data.get("server", {}) if isinstance(data.get("server"), dict) else {}
                peer_data = data.get("peer", {}) if isinstance(data.get("peer"), dict) else {}
                server_id = secure_filename(subscription_data.get("serverId") or server_data.get("id") or "unknown")
                server_id = server_id or "unknown"

                config_path = os.path.join(DATA_DIR, f"tunnelsats-{server_id}.conf")
                _write_file_secure(config_path, full_config)

                parsed = _parse_config_comments(full_config)
                payment_hash = (request.json or {}).get("paymentHash", "")
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
                    "expiresAt": subscription_data.get("expiresAt", parsed.get("expiresAt", "")),
                }
                meta_path = os.path.join(DATA_DIR, META_FILE)
                _write_file_secure(meta_path, json.dumps(meta, indent=2))

        excluded_headers = ["content-encoding", "content-length", "transfer-encoding", "connection"]
        filtered_headers = [
            (name, value) for (name, value) in resp.headers.items() if name.lower() not in excluded_headers
        ]
        return (resp.content, resp.status_code, filtered_headers)
    except requests.RequestException as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/subscription/renew", methods=["POST"])
def renew_subscription():
    payload = dict(request.json or {})
    meta_path = os.path.join(DATA_DIR, META_FILE)
    if os.path.exists(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as fp:
                meta = json.load(fp)
                if not payload.get("serverId") and "serverId" in meta:
                    payload["serverId"] = meta["serverId"]
                if not payload.get("wgPublicKey") and "wgPublicKey" in meta:
                    payload["wgPublicKey"] = meta["wgPublicKey"]
        except (IOError, json.JSONDecodeError) as exc:
            app.logger.warning(f"Failed to read metadata for renew autofill: {exc}")

    return proxy_request("POST", "subscription/renew", payload)


# --- LOCAL APP ROUTES ---

@app.route("/api/local/status", methods=["GET"])
def local_status():
    app.logger.debug("Action Request: Fetching local status")
    wg_status = "Disconnected"
    wg_pubkey = ""
    try:
        output = subprocess.check_output(["wg", "show", "tunnelsatsv2"], stderr=subprocess.STDOUT).decode("utf-8")
        if "interface: tunnelsatsv2" in output:
            wg_status = "Connected"
            for line in output.split("\n"):
                if line.strip().startswith("public key:"):
                    wg_pubkey = line.split(":", 1)[1].strip()
    except Exception:
        pass

    configs = []
    if os.path.exists(DATA_DIR):
        for fname in os.listdir(DATA_DIR):
            if fname.endswith(".conf"):
                configs.append(fname)

    dataplane = read_dataplane_state()
    containers = docker_api("/containers/json?all=0") or []
    lnd_ip = container_ip_by_match(LND_CONTAINER_PATTERN, containers=containers)
    cln_ip = container_ip_by_match(CLN_CONTAINER_PATTERN, containers=containers)

    # Granular state detection
    vpn_active = (wg_status == "Connected")
    lnd_detected = bool(container_ids_by_match(LND_CONTAINER_PATTERN, containers=containers))
    cln_detected = bool(container_ids_by_match(CLN_CONTAINER_PATTERN, containers=containers))

    lnd_routing_active = False
    if os.path.exists(LND_CONFIG_PATH):
        try:
            with open(LND_CONFIG_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    if line.lstrip().startswith("externalhosts="):
                        lnd_routing_active = True
                        break
        except (IOError, OSError) as exc:
            app.logger.warning(f"Failed to read LND config for routing detection: {exc}")

    cln_routing_active = False
    if os.path.exists(CLN_CONFIG_PATH):
        try:
            with open(CLN_CONFIG_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    if line.lstrip().startswith("announce-addr="):
                        cln_routing_active = True
                        break
        except (IOError, OSError) as exc:
            app.logger.warning(f"Failed to read CLN config for routing detection: {exc}")

    # Dynamic Internal IP Recovery
    vpn_internal_ip = ""
    try:
        # Source of Truth: the live interface state. 
        # check=False in subprocess.run is more robust than check_output if "ip" is missing.
        res = subprocess.run(["ip", "-4", "addr", "show", "dev", "tunnelsatsv2"], 
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
    meta_path = os.path.join(DATA_DIR, META_FILE)
    if os.path.exists(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as fp:
                meta = json.load(fp)
                server_domain = meta.get("serverDomain", "")
                expires_at = meta.get("expiresAt", "")
                vpn_port = meta.get("vpnPort", "")
        except (IOError, OSError, json.JSONDecodeError) as exc:
            app.logger.warning(f"Failed to read or parse metadata file {meta_path}: {exc}")
            pass

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
            "expires_at": expires_at,
            "vpn_port": vpn_port,
            "dataplane_mode": dataplane["dataplane_mode"],
            "target_container": dataplane["target_container"],
            "target_ip": dataplane["target_ip"],
            "target_impl": dataplane["target_impl"],
            "docker_network": dataplane["docker_network"],
            "forwarding_port": dataplane["forwarding_port"],
            "rules_synced": dataplane["rules_synced"],
            "last_reconcile_at": dataplane["last_reconcile_at"],
            "last_error": dataplane["last_error"],
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

    config_text = _ensure_peer_persistent_keepalive(config_text, keepalive=25)

    parsed = _parse_config_comments(config_text)
    server_domain = parsed.get("serverDomain", "")
    server_id = _server_id_from_domain(server_domain)
    expires_at = parsed.get("expiresAt", "")
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
    }

    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        backup_existing_wireguard_configs()
        _write_file_secure(os.path.join(DATA_DIR, "tunnelsats.conf"), config_text)
        _write_file_secure(os.path.join(DATA_DIR, META_FILE), json.dumps(meta, indent=2))
    except (IOError, OSError) as exc:
        app.logger.error(f"Unable to persist uploaded config: {exc}")
        return jsonify({"success": False, "error": "Failed to save configuration on disk."}), 500

    return jsonify(
        {
            "success": True,
            "message": "Configuration saved and parsed.",
            "meta": {
                "serverId": server_id,
                "wgPublicKey": wg_public_key,
                "expiresAt": expires_at,
                "vpnPort": vpn_port,
            },
        }
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
    if os.path.exists(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as fp:
                meta_data = json.load(fp)
                meta_data.pop("presharedKey", None)
                meta_data.pop("paymentHash", None)
        except (json.JSONDecodeError, IOError) as exc:
            app.logger.error(f"Error reading metadata file {meta_path}: {exc}")
    return jsonify(meta_data)


@app.route("/api/local/configure-node", methods=["POST"])
def configure_node():
    app.logger.info("Action Request: Configuring Node")
    payload = request.get_json(silent=True) or {}
    node_type = str(payload.get("nodeType", "")).strip().lower()

    if node_type not in ("lnd", "cln"):
        return jsonify({"success": False, "error": "Invalid nodeType. Use 'lnd' or 'cln'."}), 400

    meta_path = os.path.join(DATA_DIR, META_FILE)
    if not os.path.exists(meta_path):
        return jsonify({"success": False, "error": "Missing tunnelsats metadata file."}), 400

    try:
        with open(meta_path, "r", encoding="utf-8") as fp:
            meta = json.load(fp)
    except (IOError, OSError, json.JSONDecodeError):
        return jsonify({"success": False, "error": "Unable to read tunnelsats metadata file."}), 500

    dns = str(meta.get("serverDomain", "")).strip()
    try:
        port = int(meta.get("vpnPort", 0))
    except (TypeError, ValueError):
        port = 0

    if not dns or port <= 0:
        return jsonify({"success": False, "error": "Metadata is missing vpnPort or serverDomain."}), 400

    lnd_pending_key = "lndRestartPending"
    cln_pending_key = "clnRestartPending"

    if node_type == "lnd":
        if not container_ids_by_match(LND_CONTAINER_PATTERN):
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

        lnd_processed, lnd_changed = upsert_config_line_in_section(
            LND_CONFIG_PATH,
            "[Application Options]",
            "externalhosts=",
            f"externalhosts={dns}:{port}",
        )
        if not lnd_processed:
            return jsonify({"success": False, "error": "Failed to modify LND config."}), 500
        if not restart_container_by_pattern(LND_CONTAINER_PATTERN, is_lnd=True):
            _set_restart_pending(meta_path, meta, lnd_pending_key, True)
            return jsonify({"success": False, "error": "Failed to restart LND container."}), 500
        _set_restart_pending(meta_path, meta, lnd_pending_key, False)

        return jsonify(
            {
                "success": True,
                "lnd": True,
                "cln": False,
                "lnd_changed": lnd_changed,
                "port": port,
                "dns": dns,
            }
        )

    # CLN target
    if not container_ids_by_match(CLN_CONTAINER_PATTERN):
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

    cln_steps = (
        ("bind-addr=", "bind-addr=0.0.0.0:9736"),
        ("announce-addr=", f"announce-addr={dns}:{port}"),
        ("always-use-proxy=", "always-use-proxy=false"),
    )
    cln_processed, cln_changed = upsert_config_lines(CLN_CONFIG_PATH, cln_steps)
    if not cln_processed:
        return jsonify({"success": False, "error": "Failed to modify CLN config."}), 500

    if not restart_container_by_pattern(CLN_CONTAINER_PATTERN):
        _set_restart_pending(meta_path, meta, cln_pending_key, True)
        return jsonify({"success": False, "error": "Failed to restart CLN container."}), 500
    _set_restart_pending(meta_path, meta, cln_pending_key, False)

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
    lnd_processed, lnd_changed, cln_processed, cln_changed = False, False, False, False
    errors = []
    lnd_detected = bool(container_ids_by_match(LND_CONTAINER_PATTERN))
    cln_detected = bool(container_ids_by_match(CLN_CONTAINER_PATTERN))

    lnd_processed, lnd_changed = comment_out_config_lines(
        LND_CONFIG_PATH,
        (
            "externalhosts=",
            "tor.skip-proxy-for-clearnet-targets=",
        ),
    )
    if lnd_processed and lnd_detected:
        if not restart_container_by_pattern(LND_CONTAINER_PATTERN, is_lnd=True):
            errors.append("Failed to restart LND container.")
    elif lnd_processed and not lnd_detected:
        app.logger.info("LND config restored, but no running LND container detected. Skipping restart.")

    cln_processed, cln_changed = comment_out_config_lines(
        CLN_CONFIG_PATH,
        (
            "bind-addr=",
            "announce-addr=",
            "always-use-proxy=",
        ),
    )
    if cln_processed and cln_detected:
        if not restart_container_by_pattern(CLN_CONTAINER_PATTERN):
            errors.append("Failed to restart CLN container.")
    elif cln_processed and not cln_detected:
        app.logger.info("CLN config restored, but no running CLN container detected. Skipping restart.")

    if errors:
        return jsonify({"success": False, "error": " ".join(errors)}), 500

    return jsonify(
        {
            "lnd": lnd_processed,
            "cln": cln_processed,
            "lnd_changed": lnd_changed,
            "cln_changed": cln_changed,
        }
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9739)
