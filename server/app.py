import json
import os
import re
import subprocess
import uuid
from datetime import datetime, timezone
from ipaddress import ip_address, ip_network

import requests
import yaml
from flask import Flask, abort, jsonify, request, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

app = Flask(__name__, static_folder="../web", static_url_path="")
# Umbrel uses a reverse proxy. Parse X-Forwarded-* headers before IP restrictions.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

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
LND_TUNNELSATS_CONF_PATH = "/lightning-data/lnd/tunnelsats.conf"
CLN_CONFIG_PATH = "/lightning-data/cln/config"

ALLOWED_NETWORKS = (
    ip_network("127.0.0.0/8"),
    ip_network("10.0.0.0/8"),
    ip_network("172.16.0.0/12"),
    ip_network("192.168.0.0/16"),
)


def client_is_allowed(remote_addr):
    if not remote_addr:
        return False
    try:
        remote_ip = ip_address(remote_addr)
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
        try:
            file_mode = os.stat(path).st_mode & 0o777
        except (IOError, OSError) as exc:
            app.logger.warning(f"Error reading file mode for {path}: {exc}")

        tmp_path = os.path.join(os.path.dirname(path) or ".", f".{os.path.basename(path)}.tmp.{uuid.uuid4().hex}")
        try:
            with open(tmp_path, "w", encoding="utf-8") as conf_fp:
                conf_fp.writelines(updated_lines)
            if file_mode is not None:
                os.chmod(tmp_path, file_mode)
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


def _parse_config_comments(config_text):
    meta = {}
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
    os.replace(tmp_path, path)


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


def container_ip_by_match(pattern):
    containers = docker_api("/containers/json?all=0")
    if not containers:
        return ""

    for item in containers:
        names = item.get("Names", [])
        for name in names:
            clean = name.lstrip("/")
            if re.search(pattern, clean):
                networks = item.get("NetworkSettings", {}).get("Networks", {})
                for network_data in networks.values():
                    ip_addr = network_data.get("IPAddress")
                    if ip_addr:
                        return ip_addr
    return ""


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
        defaults.update({k: v for k, v in data.items() if k in defaults})
        if isinstance(data.get("docker_network"), dict):
            defaults["docker_network"].update(data["docker_network"])
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
    return proxy_request("GET", f"subscription/{paymentHash}")


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
    lnd_ip = container_ip_by_match(r"(^|[_-])lnd([_-]|$)")
    cln_ip = container_ip_by_match(r"(^|[_-])(core-lightning|clightning|lightningd)([_-]|$)")

    return jsonify(
        {
            "wg_status": wg_status,
            "wg_pubkey": wg_pubkey,
            "configs_found": configs,
            "version": read_app_version(),
            "lnd_ip": lnd_ip,
            "cln_ip": cln_ip,
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
    if "config" not in request.files and "config_text" not in request.form:
        return jsonify({"error": "No config provided"}), 400

    config_data = ""
    if "config" in request.files:
        uploaded = request.files["config"]
        if uploaded.filename == "":
            return jsonify({"error": "No selected file"}), 400
        config_data = uploaded.read().decode("utf-8")
    else:
        config_data = request.form.get("config_text", "")

    if "[Interface]" not in config_data or "[Peer]" not in config_data:
        return jsonify({"error": "Invalid WireGuard configuration format"}), 400

    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        backup_existing_wireguard_configs(excluded_files={"tunnelsats-imported.conf"})
        config_path = os.path.join(DATA_DIR, "tunnelsats-imported.conf")
        _write_file_secure(config_path, config_data)
        return jsonify({"success": True, "message": "Config imported successfully."})
    except Exception as exc:
        return jsonify({"error": f"Failed to save config: {str(exc)}"}), 500


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


@app.route("/api/local/restore-node", methods=["POST"])
def restore_node():
    lnd_processed, lnd_changed = comment_out_config_lines(
        LND_TUNNELSATS_CONF_PATH,
        (
            "externalhosts=",
            "tor.skip-proxy-for-clearnet-targets=",
        ),
    )
    cln_processed, cln_changed = comment_out_config_lines(
        CLN_CONFIG_PATH,
        (
            "bind-addr=",
            "announce-addr=",
            "always-use-proxy=",
        ),
    )

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
