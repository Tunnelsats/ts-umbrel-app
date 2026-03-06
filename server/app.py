import os
import time
import subprocess
import requests
import logging
import yaml
import json
import stat
import re
from datetime import datetime, timezone
from ipaddress import ip_address, ip_network
from flask import Flask, request, jsonify, send_from_directory, abort
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__, static_folder="../web", static_url_path="")
# Umbrel uses an app-proxy, so request.remote_addr will be 127.0.0.1 unless we use ProxyFix to parse X-Forwarded-For.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_host=1)

TUNNELSATS_API_URL = "https://tunnelsats.com/api/public/v1"
DATA_DIR = "/data"
META_FILE = "tunnelsats-meta.json"

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

def get_active_vpn_info():
    port = None
    dns = "vpn.tunnelsats.com" 
    if os.path.exists(DATA_DIR):
        for f in os.listdir(DATA_DIR):
            if f.endswith(".conf"):
                with open(os.path.join(DATA_DIR, f), "r") as c:
                    for line in c:
                        if "VPNPort" in line or "Port Forwarding" in line:
                            import re
                            m = re.search(r'\b(\d{4,5})\b', line)
                            if m:
                                port = m.group(1)
                        if "Endpoint" in line:
                            import re
                            m = re.search(r'Endpoint\s*=\s*([^:]+)', line)
                            if m and not re.match(r'^\d{1,3}(\.\d{1,3}){3}$', m.group(1)):
                                dns = m.group(1).strip()
    return port, dns

# Proxy function to forward requests to the core Tunnelsats API
def proxy_request(method, endpoint, payload=None):
    url = f"{TUNNELSATS_API_URL}/{endpoint}"
    headers = {"Content-Type": "application/json"}
    try:
        if method == 'GET':
            resp = requests.get(url, headers=headers, timeout=10)
        elif method == 'POST':
            resp = requests.post(url, json=payload, headers=headers, timeout=10)
        else:
            return jsonify({"error": "Unsupported method"}), 405
            
        excluded_headers = ['content-encoding', 'content-length', 'transfer-encoding', 'connection']
        headers = [(name, value) for (name, value) in resp.headers.items()
                   if name.lower() not in excluded_headers]
                   
        return (resp.content, resp.status_code, headers)
    except requests.RequestException as e:
        return jsonify({"error": str(e)}), 500

@app.route("/")
def serve_index():
    return send_from_directory(app.static_folder, "index.html")

@app.route("/<path:path>")
def serve_static(path):
    return send_from_directory(app.static_folder, path)

# --- API PROXY ROUTES ---

@app.route("/api/servers", methods=["GET"])
def get_servers():
    return proxy_request('GET', 'servers')

@app.route("/api/subscription/create", methods=["POST"])
def create_subscription():
    return proxy_request('POST', 'subscription/create', request.json)

@app.route("/api/subscription/<paymentHash>", methods=["GET"])
def check_subscription(paymentHash):
    return proxy_request('GET', f'subscription/{paymentHash}')

def _parse_config_comments(config_text):
    """Extract metadata from WireGuard config comments and fields."""
    meta = {}
    for line in config_text.split('\n'):
        line = line.strip()
        if m := re.match(r'^#\s*Port Forwarding:\s*(\d+)', line):
            meta['vpnPort'] = int(m.group(1))
        elif m := re.match(r'^#\s*Server:\s*(.+)', line):
            meta['serverDomain'] = m.group(1).strip()
        elif m := re.match(r'^#\s*myPubKey:\s*(.+)', line):
            meta['wgPublicKey'] = m.group(1).strip()
        elif m := re.match(r'^#\s*Valid Until:\s*(.+)', line):
            meta['expiresAt'] = m.group(1).strip()
        elif m := re.match(r'^Endpoint\s*=\s*(.+)', line):
            endpoint_val = m.group(1).strip()
            meta['wgEndpoint'] = endpoint_val
            if ':' in endpoint_val:
                meta.setdefault('serverDomain', endpoint_val.rsplit(':', 1)[0])
        elif m := re.match(r'^PresharedKey\s*=\s*(.+)', line):
            meta['presharedKey'] = m.group(1).strip()
        elif m := re.match(r'^Address\s*=\s*(.+)', line):
            meta['peerAddress'] = m.group(1).strip()
    return meta


def _write_file_secure(path, content):
    """Write content to a file atomically with chmod 600 (no TOCTOU window)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(content)


@app.route("/api/subscription/claim", methods=["POST"])
def claim_subscription():
    # If the claim was successful, we also want to intercept the config and save it
    url = f"{TUNNELSATS_API_URL}/subscription/claim"
    try:
        resp = requests.post(url, json=request.json, headers={"Content-Type": "application/json"}, timeout=10)
        if resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError:
                data = {}
            if "fullConfig" in data:
                # Ensure DATA_DIR exists
                if not os.path.exists(DATA_DIR):
                    os.makedirs(DATA_DIR)

                # Rename old configs to .bak (don't delete — user paid for these)
                for f_name in os.listdir(DATA_DIR):
                    if f_name.endswith(".conf"):
                        try:
                            old_path = os.path.join(DATA_DIR, f_name)
                            os.rename(old_path, old_path + ".bak")
                        except Exception as e:
                            app.logger.warning(f"Failed to rename old config {f_name}: {e}")

                # Extract serverId from subscription or fallback
                server_id = "unknown"
                if "subscription" in data:
                    server_id = secure_filename(data["subscription"].get("serverId", "unknown")) or "unknown"

                # Write config file (chmod 600)
                full_config = data["fullConfig"]
                config_path = os.path.join(DATA_DIR, f"tunnelsats-{server_id}.conf")
                _write_file_secure(config_path, full_config)

                # Parse config comments to extract metadata
                parsed = _parse_config_comments(full_config)

                # Build metadata from API response + parsed config
                payment_hash = (request.json or {}).get("paymentHash", "")
                meta = {
                    "serverId": server_id,
                    "paymentHash": payment_hash,
                    "wgPublicKey": parsed.get("wgPublicKey", ""),
                    "peerAddress": data.get("peer", {}).get("address", parsed.get("peerAddress", "")),
                    "presharedKey": data.get("peer", {}).get("presharedKey", parsed.get("presharedKey", "")),
                    "vpnPort": parsed.get("vpnPort", 0),
                    "serverDomain": parsed.get("serverDomain", ""),
                    "wgEndpoint": data.get("server", {}).get("endpoint", parsed.get("wgEndpoint", "")),
                    "claimedAt": datetime.now(timezone.utc).isoformat(),
                    "expiresAt": data.get("subscription", {}).get("expiresAt", parsed.get("expiresAt", ""))
                }
                meta_path = os.path.join(DATA_DIR, META_FILE)
                _write_file_secure(meta_path, json.dumps(meta, indent=2))

        # Filter hop-by-hop headers (same as proxy_request)
        excluded_headers = ['content-encoding', 'content-length', 'transfer-encoding', 'connection']
        headers = [(name, value) for (name, value) in resp.headers.items()
                   if name.lower() not in excluded_headers]
        return (resp.content, resp.status_code, headers)
    except requests.RequestException as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/subscription/renew", methods=["POST"])
def renew_subscription():
    return proxy_request('POST', 'subscription/renew', request.json)

# --- LOCAL APP ROUTES ---

@app.route("/api/local/status", methods=["GET"])
def local_status():
    # Detect if WireGuard is running
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

    # Check for config files in /data
    configs = []
    if os.path.exists(DATA_DIR):
        for f in os.listdir(DATA_DIR):
            if f.endswith(".conf"):
                configs.append(f)

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
        
    # Write to /data securely
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
        with open(config_path, "w") as f:
            f.write(config_data)
            
        return jsonify({"success": True, "message": "Config imported successfully."})
    except Exception as e:
        return jsonify({"error": f"Failed to save config: {str(e)}"}), 500

@app.route("/api/local/restart", methods=["POST"])
def restart_tunnel():
    try:
        with open("/tmp/tunnelsats_restart_trigger", "w") as f:
            f.write("trigger")
        return jsonify({"success": True, "message": "Restarting tunnel..."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/local/meta", methods=["GET"])
def get_metadata():
    """Return stored subscription metadata, or empty object if none."""
    meta_data = {}
    meta_path = os.path.join(DATA_DIR, META_FILE)
    if os.path.exists(meta_path):
        try:
            with open(meta_path) as f:
                meta_data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            app.logger.error(f"Error reading metadata file {meta_path}: {e}")
    return jsonify(meta_data)

# NOTE: configure-node and restore-node endpoints moved to PR #3 (dataplane layer).
# They will be re-introduced when the infra PR is merged.

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9739)
