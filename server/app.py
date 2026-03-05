import json
import os
import re
import subprocess
import uuid
from ipaddress import ip_address, ip_network

import requests
import logging
import yaml
from ipaddress import ip_address, ip_network
from flask import Flask, request, jsonify, send_from_directory, abort
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__, static_folder="../web", static_url_path="")
# Umbrel uses an app-proxy, so request.remote_addr will be 127.0.0.1 unless we use ProxyFix to parse X-Forwarded-For.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_host=1)

TUNNELSATS_API_URL = "https://tunnelsats.com/api/public/v1"
DATA_DIR = "/data"
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


# Allow local loopback and all standard private subnets (RFC 1918) for LAN access
ALLOWED_NETWORKS = [
    ip_network('127.0.0.0/8'),
    ip_network('10.0.0.0/8'),
    ip_network('172.16.0.0/12'),
    ip_network('192.168.0.0/16')
]

@app.before_request
def restrict_local_api():
    if request.path.startswith('/api/local/'):
        remote_addr = request.remote_addr
        if remote_addr:
            try:
                ip_obj = ip_address(remote_addr)
                if not any(ip_obj in net for net in ALLOWED_NETWORKS):
                    app.logger.warning(f"Unauthorized access attempt to {request.path} from {remote_addr}")
                    abort(403)
            except ValueError:
                abort(403)
        else:
            abort(403)

def client_is_allowed(remote_addr):
    if not remote_addr:
        return False

    try:
        remote_ip = ip_address(remote_addr)
    except ValueError:
        return False

    return any(remote_ip in subnet for subnet in ALLOWED_NETWORKS)


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
            manifest_raw = manifest_fp.read()
    except Exception:
        return "v3.0.0"

    if yaml:
        try:
            manifest = yaml.safe_load(manifest_raw) or {}
            return normalize_version(manifest.get("version", "3.0.0"))
        except Exception:
            pass

    match = re.search(r'^\s*version:\s*"?([^"\n]+)"?\s*$', manifest_raw, re.MULTILINE)
    if match:
        return normalize_version(match.group(1))

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
        return False

    try:
        with open(path, "r", encoding="utf-8") as conf_fp:
            lines = conf_fp.readlines()
    except Exception:
        return False

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
        try:
            with open(path, "w", encoding="utf-8") as conf_fp:
                conf_fp.writelines(updated_lines)
        except Exception:
            return False

    return True


@app.before_request
def restrict_to_local():
    if request.path.startswith("/api/local") and not client_is_allowed(request.remote_addr):
        return jsonify({"error": "Forbidden"}), 403


def get_active_vpn_info():
    port = None
    dns = "vpn.tunnelsats.com"
    if os.path.exists(DATA_DIR):
        for fname in os.listdir(DATA_DIR):
            if not fname.endswith(".conf"):
                continue
            with open(os.path.join(DATA_DIR, fname), "r", encoding="utf-8") as conf:
                for line in conf:
                    if "VPNPort" in line or "Port Forwarding" in line:
                        match = re.search(r"\b(\d{4,5})\b", line)
                        if match:
                            port = match.group(1)
                    if "Endpoint" in line:
                        match = re.search(r"Endpoint\s*=\s*([^:]+)", line)
                        if match and not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", match.group(1)):
                            dns = match.group(1).strip()
    return port, dns


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
            
        excluded_headers = ['content-encoding', 'content-length', 'transfer-encoding', 'connection']
        headers = [(name, value) for (name, value) in resp.headers.items()
                   if name.lower() not in excluded_headers]
                   
        return (resp.content, resp.status_code, headers)
    except requests.RequestException as e:
        return jsonify({"error": str(e)}), 500

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


@app.route("/")
def serve_index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/<path:path>")
def serve_static(path):
    return send_from_directory(app.static_folder, path)


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
    url = f"{TUNNELSATS_API_URL}/subscription/claim"
    try:
        resp = requests.post(url, json=request.json, headers={"Content-Type": "application/json"}, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if "wireguardConfig" in data and "server" in data:
                # Rename old configs to .bak (don't delete — user paid for these)
                server_id = data["server"].get("id", "unknown")
                if os.path.exists(DATA_DIR):
                    for f in os.listdir(DATA_DIR):
                        if f.endswith(".conf"):
                            try:
                                old_path = os.path.join(DATA_DIR, f)
                                os.rename(old_path, old_path + ".bak")
                            except: pass

                config_path = os.path.join(DATA_DIR, f"tunnelsats-{server_id}.conf")
                with open(config_path, "w") as f:
                    f.write(data["wireguardConfig"])

        # Filter hop-by-hop headers (same as proxy_request)
        excluded_headers = ['content-encoding', 'content-length', 'transfer-encoding', 'connection']
        headers = [(name, value) for (name, value) in resp.headers.items()
                   if name.lower() not in excluded_headers]
        return (resp.content, resp.status_code, headers)
    except requests.RequestException as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/subscription/renew", methods=["POST"])
def renew_subscription():
    return proxy_request("POST", "subscription/renew", request.json)


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

    # Get version from manifest
    version = "v3.0.0" # Default
    try:
        manifest_path = os.path.join(os.path.dirname(__file__), "..", "umbrel-app.yml")
        if os.path.exists(manifest_path):
            with open(manifest_path, 'r') as f:
                manifest = yaml.safe_load(f)
                version = f"v{manifest.get('version', '3.0.0')}"
    except Exception:
        pass

    return jsonify({
        "wg_status": wg_status,
        "wg_pubkey": wg_pubkey,
        "configs_found": configs,
        "version": version
    })

@app.route("/api/local/upload-config", methods=["POST"])
def upload_config():
    if "config" not in request.files and "config_text" not in request.form:
        return jsonify({"error": "No config provided"}), 400

    config_data = ""
    if "config" in request.files:
        file = request.files["config"]
        if file.filename == "":
            return jsonify({"error": "No selected file"}), 400
        config_data = file.read().decode("utf-8")
    else:
        config_data = request.form.get("config_text", "")

    if "[Interface]" not in config_data or "[Peer]" not in config_data:
        return jsonify({"error": "Invalid WireGuard configuration format"}), 400

    try:
        if not os.path.exists(DATA_DIR):
            os.makedirs(DATA_DIR)
        else:
            # Rename old configs to .bak (don't delete — user paid for these)
            for f in os.listdir(DATA_DIR):
                if f.endswith(".conf"):
                    try:
                        old_path = os.path.join(DATA_DIR, f)
                        os.rename(old_path, old_path + ".bak")
                    except: pass
            
        config_path = os.path.join(DATA_DIR, "tunnelsats-imported.conf")
        with open(config_path, "w", encoding="utf-8") as conf:
            conf.write(config_data)

        return jsonify({"success": True, "message": "Config imported successfully."})
    except Exception as exc:
        return jsonify({"error": f"Failed to save config: {str(exc)}"}), 500


@app.route("/api/local/restart", methods=["POST"])
def restart_tunnel():
    try:
        with open("/tmp/tunnelsats_restart_trigger", "w", encoding="utf-8") as trigger_fp:
            trigger_fp.write("trigger")
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
        return jsonify({"success": True, "complete": True, **result})

    legacy_result = read_legacy_reconcile_result()
    if isinstance(legacy_result, dict) and legacy_result.get("request_id") == request_id:
        return jsonify({"success": True, "complete": True, **legacy_result})

    return jsonify({"success": True, "complete": False, "request_id": request_id}), 202


# NOTE: configure-node and restore-node endpoints moved to PR #3 (dataplane layer).
# They will be re-introduced when the infra PR is merged.


@app.route("/api/local/restore-node", methods=["POST"])
def restore_node():
    lnd_success = comment_out_config_lines(
        LND_TUNNELSATS_CONF_PATH,
        (
            "externalhosts=",
            "tor.skip-proxy-for-clearnet-targets=",
        ),
    )
    cln_success = comment_out_config_lines(
        CLN_CONFIG_PATH,
        (
            "bind-addr=",
            "announce-addr=",
            "always-use-proxy=",
        ),
    )

    return jsonify({"lnd": lnd_success, "cln": cln_success})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9739)
