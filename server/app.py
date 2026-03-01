import os
import time
import subprocess
import requests
from flask import Flask, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename

app = Flask(__name__, static_folder="../web", static_url_path="")

TUNNELSATS_API_URL = "https://tunnelsats.com/api/public/v1"
DATA_DIR = "/data"

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
            
        return (resp.content, resp.status_code, resp.headers.items())
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

@app.route("/api/subscription/claim", methods=["POST"])
def claim_subscription():
    # If the claim was successful, we also want to intercept the config and save it
    url = f"{TUNNELSATS_API_URL}/subscription/claim"
    try:
        resp = requests.post(url, json=request.json, headers={"Content-Type": "application/json"}, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if "wireguardConfig" in data and "server" in data:
                # Save config
                server_id = data["server"].get("id", "unknown")
                config_path = os.path.join(DATA_DIR, f"tunnelsats-{server_id}.conf")
                with open(config_path, "w") as f:
                    f.write(data["wireguardConfig"])
        return (resp.content, resp.status_code, resp.headers.items())
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
    wg_ip = ""
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

    # Check for LND and CLN IPs via our entrypoint logs or docker 
    lnd_ip = ""
    cln_ip = ""
    try:
        # We can read the ip file we might dump from entrypoint to /tmp or just exec docker inspect if socket available
        if os.path.exists("/var/run/docker.sock"):
            lnd_out = subprocess.check_output(["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", "lightning_lnd_1"], stderr=subprocess.DEVNULL)
            lnd_ip = lnd_out.decode().strip()
            cln_out = subprocess.check_output(["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", "lightning_core-lightning_1"], stderr=subprocess.DEVNULL)
            cln_ip = cln_out.decode().strip()
    except Exception:
        pass

    return jsonify({
        "wg_status": wg_status,
        "wg_pubkey": wg_pubkey,
        "configs_found": configs,
        "lnd_ip": lnd_ip,
        "cln_ip": cln_ip
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

@app.route("/api/local/configure-node", methods=["POST"])
def configure_node():
    port, dns = get_active_vpn_info()
    if not port:
        return jsonify({"error": "No VPN forwarding port found in config."}), 400
        
    lnd_success = False
    lnd_path = "/lightning-data/lnd/tunnelsats.conf"
    if os.path.exists("/lightning-data/lnd"):
        try:
            with open(lnd_path, "w") as f:
                f.write(f"[Application Options]\nexternalhosts={dns}:{port}\n\n[Tor]\ntor.streamisolation=false\ntor.skip-proxy-for-clearnet-targets=true\n")
            lnd_success = True
        except Exception:
            pass

    cln_success = False
    cln_path = "/lightning-data/cln/config"
    if os.path.exists(cln_path):
        try:
            with open(cln_path, "r") as f:
                lines = f.readlines()
            
            new_lines = []
            for line in lines:
                if not line.startswith("bind-addr=") and not line.startswith("announce-addr=") and not line.startswith("always-use-proxy="):
                    new_lines.append(line)
                    
            new_lines.append(f"bind-addr=0.0.0.0:9735\n")
            new_lines.append(f"announce-addr={dns}:{port}\n")
            new_lines.append(f"always-use-proxy=false\n")
            
            with open(cln_path, "w") as f:
                f.writelines(new_lines)
            cln_success = True
        except Exception:
            pass

    return jsonify({"lnd": lnd_success, "cln": cln_success, "port": port, "dns": dns})

@app.route("/api/local/restore-node", methods=["POST"])
def restore_node():
    lnd_success = False
    lnd_path = "/lightning-data/lnd/tunnelsats.conf"
    if os.path.exists(lnd_path):
        try:
            os.remove(lnd_path)
            lnd_success = True
        except Exception:
            pass

    cln_success = False
    cln_path = "/lightning-data/cln/config"
    if os.path.exists(cln_path):
        try:
            with open(cln_path, "r") as f:
                lines = f.readlines()
            
            new_lines = []
            for line in lines:
                if not line.startswith("bind-addr=") and not line.startswith("announce-addr=") and not line.startswith("always-use-proxy="):
                    new_lines.append(line)
                    
            with open(cln_path, "w") as f:
                f.writelines(new_lines)
            cln_success = True
        except Exception:
            pass

    configs_cleaned = False
    if os.path.exists(DATA_DIR):
        try:
            for f in os.listdir(DATA_DIR):
                if f.endswith(".conf"):
                    os.remove(os.path.join(DATA_DIR, f))
            configs_cleaned = True
        except Exception:
            pass

    return jsonify({"lnd": lnd_success, "cln": cln_success, "configs_cleaned": configs_cleaned})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9739)
