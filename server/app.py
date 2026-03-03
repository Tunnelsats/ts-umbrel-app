import json
import os
import re
import subprocess
import uuid

import requests
from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__, static_folder="../web", static_url_path="")

TUNNELSATS_API_URL = "https://tunnelsats.com/api/public/v1"
DATA_DIR = "/data"
DOCKER_SOCK = "/var/run/docker.sock"
STATE_FILE = "/tmp/tunnelsats_state.json"
RECONCILE_TRIGGER = "/tmp/tunnelsats_reconcile_trigger"
RECONCILE_RESULT = "/tmp/tunnelsats_reconcile_result.json"


def safe_config_path_for_server(server_id):
    raw_id = str(server_id or "unknown")
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", raw_id).strip("_")
    if not safe_id:
        safe_id = "unknown"

    data_dir_abs = os.path.abspath(DATA_DIR)
    config_path = os.path.abspath(os.path.join(data_dir_abs, f"tunnelsats-{safe_id}.conf"))

    if os.path.commonpath([data_dir_abs, config_path]) != data_dir_abs:
        raise ValueError("Resolved config path escapes data directory")

    return config_path


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

        return resp.content, resp.status_code, resp.headers.items()
    except requests.RequestException as exc:
        return jsonify({"error": str(exc)}), 500


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
    except Exception:
        return None


def container_ip_by_match(pattern):
    containers = docker_api("/containers/json?all=1")
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


def read_reconcile_result():
    if not os.path.exists(RECONCILE_RESULT):
        return None

    try:
        with open(RECONCILE_RESULT, "r", encoding="utf-8") as result_fp:
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
                server = data.get("server")
                server_id = server.get("id", "unknown") if isinstance(server, dict) else "unknown"
                os.makedirs(DATA_DIR, exist_ok=True)
                config_path = safe_config_path_for_server(server_id)
                with open(config_path, "w", encoding="utf-8") as conf:
                    conf.write(data["wireguardConfig"])
        return resp.content, resp.status_code, resp.headers.items()
    except requests.RequestException as exc:
        return jsonify({"error": str(exc)}), 500
    except Exception as exc:
        return jsonify({"error": f"Failed to persist WireGuard config: {str(exc)}"}), 500


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

    lnd_ip = container_ip_by_match(r"lnd")
    cln_ip = container_ip_by_match(r"core-lightning|clightning|lightningd")
    dataplane = read_dataplane_state()

    return jsonify(
        {
            "wg_status": wg_status,
            "wg_pubkey": wg_pubkey,
            "configs_found": configs,
            "lnd_ip": lnd_ip,
            "cln_ip": cln_ip,
            "dataplane_mode": dataplane["dataplane_mode"],
            "target_container": dataplane["target_container"],
            "target_ip": dataplane["target_ip"],
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
        if os.path.exists(RECONCILE_RESULT):
            os.remove(RECONCILE_RESULT)
        with open(RECONCILE_TRIGGER, "w", encoding="utf-8") as trigger_fp:
            trigger_fp.write(request_id)
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
    result = read_reconcile_result()
    if isinstance(result, dict) and result.get("request_id") == request_id:
        return jsonify({"success": True, "complete": True, **result})

    return jsonify({"success": True, "complete": False, "request_id": request_id}), 202


@app.route("/api/local/configure-node", methods=["POST"])
def configure_node():
    port, dns = get_active_vpn_info()
    if not port:
        return jsonify({"error": "No VPN forwarding port found in config."}), 400

    lnd_success = False
    lnd_path = "/lightning-data/lnd/tunnelsats.conf"
    if os.path.exists("/lightning-data/lnd"):
        try:
            with open(lnd_path, "w", encoding="utf-8") as conf:
                conf.write(
                    f"[Application Options]\nexternalhosts={dns}:{port}\n\n[Tor]\n"
                    "tor.streamisolation=false\n"
                    "tor.skip-proxy-for-clearnet-targets=true\n"
                )
            lnd_success = True
        except Exception:
            pass

    cln_success = False
    cln_path = "/lightning-data/cln/config"
    if os.path.exists(cln_path):
        try:
            with open(cln_path, "r", encoding="utf-8") as conf:
                lines = conf.readlines()

            new_lines = []
            for line in lines:
                if not line.startswith("bind-addr=") and not line.startswith("announce-addr=") and not line.startswith("always-use-proxy="):
                    new_lines.append(line)

            new_lines.append("bind-addr=0.0.0.0:9735\n")
            new_lines.append(f"announce-addr={dns}:{port}\n")
            new_lines.append("always-use-proxy=false\n")

            with open(cln_path, "w", encoding="utf-8") as conf:
                conf.writelines(new_lines)
            cln_success = True
        except Exception:
            pass

    return jsonify({"lnd": lnd_success, "cln": cln_success, "port": port, "dns": dns})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9739)
