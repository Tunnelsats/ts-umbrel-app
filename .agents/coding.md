# Coding Insights & Technical Patterns: TunnelSats Umbrel App

## Docker Isolation & WireGuard Dataplane Realities

### 1. The Perils of `wg-quick` within Docker Isolation
- **The Problem**: When `wg-quick down <interface>` runs during container teardown, if the link state was already dropped or crashed, `wg-quick` exits fatally early. This leaves behind:
  - Orphaned `iptables` rules that hijack host DNS (`AllowedIPs = 0.0.0.0/0` -> pref 32764).
  - Orphaned `nftables` tables (`wg-quick-<interface>`) that silently drop all decrypted WireGuard return packets *before* they reach Linux `PREROUTING`.
- **The Pattern**:
  - Defensively strip `Table=` and enforce `Table = off` inside the parsed config `[Interface]` block to protect the Docker host.
  - Mathematically isolate drop filters by verifying via `tcpdump` vs `iptables -vnL`.

### 2. Docker Process Ownership & File Permissions
- **The Problem**: When the internal Python daemon writes files inside the container, they inherit `root:root` ownership. When mounted onto umbrelOS host disks, this locks out the `umbrel` (UID 1000) user.
- **The Pattern**:
  - Always explicitly enforce `os.chown(path, 1000, 1000)` and `os.chmod(path, 0o600)` on any dynamically generated file in shared volumes.
  - Always initialize variables (e.g. `file_mode = None`) prior to running `os.stat` checks to prevent `NameError` on fallback blocks.

### 3. IPRoute2 vs Subnet Mask Parsing (CIDR)
- **The Problem**: Kernel `iproute2` rejects adding `scope link` routes if the host bits of the IP address are not strictly zeroed out. Trying to add `10.9.0.2/24` directly crashes `ip route replace`.
- **The Pattern**:
  ```python
  import ipaddress
  network_address = str(ipaddress.IPv4Network(cidr_string, strict=False).network_address)
  ```
  Using `ipaddress.IPv4Network(..., strict=False)` mathematically guarantees clean zeroed host bits.

---

## umbrelOS Container & Runtime Patterns

### 1. Sequential LND Restart Chain
- **The Problem**: Writing to `umbrel-lnd.conf` is futile because it is an ephemeral file overwritten on boot by `lightning_app_1`.
- **The Pattern**:
  1. Write desired changes to the source of truth: `lnd.conf`.
  2. Restart middleware: `docker restart $(docker ps -q --filter "name=(^|[_-])(lightning[_-]app|lnd[_-]app|lightning[_-]ui)(?:[_-]?\d+)?$")`.
  3. Sleep for 3 seconds to allow `umbrel-lnd.conf` generation.
  4. Restart daemon: `docker restart $(docker ps -q --filter "name=^lightning[_-]lnd[_-]\d+$")`.

### 2. Python Unbuffered Logging in Docker
- **The Problem**: Python aggressively buffers `stdout`/`stderr` when run non-interactively in containers, delaying critical diagnostic logs.
- **The Pattern**:
  - Set `PYTHONUNBUFFERED=1` in the container environment.
  - Attach a `StreamHandler(sys.stderr)` to the logger.
  - Ensure Flask/App instances are fully initialized *before* attaching custom log handlers to avoid startup `NameErrors`.

### 3. Atomic Configuration Swapping
- **The Pattern**:
  1. Write the configuration to a temporary file (`config.conf.tmp.<uuid>`).
  2. Call `os.replace(tmp_file, target_file)`.
  3. Read the target file back from disk and verify expected byte sequences before triggering any downstream daemon restarts.

### 4. Capturing Output from Container Exec
- **The Problem**: When running commands via `docker exec -it` or `podman exec -it`, pseudo-TTY (`-t`) allocation translates `\n` to `\r\n`, adding invisible carriage returns that break shell string comparisons.
- **The Pattern**: Omit `-t` (use `-i` only) and pipe to `tr -d '\r'`:
  ```bash
  docker exec -i <container> <command> | tr -d '\r'
  ```

### 5. Dual-Store Container Naming & Project-Aware Discovery
- **The Problem**: Both Community (`tunnelsats-community`) and Official (`tunnelsats`) define Compose services `tunnelsats-web` and `tunnelsats-daemon`. Hardcoding bare `container_name: tunnelsats-web` in `tunnelsats-community/docker-compose.yml` breaks umbrelOS `lan-ingress` target resolution and hijacks Official Store proxy traffic.
- **The Pattern**:
  - Omit `container_name` in Umbrel compose files so umbrelOS injects `tunnelsats-community_tunnelsats-web_1` / `tunnelsats-community_tunnelsats-daemon_1` (or `tunnelsats_tunnelsats-*_1` for Official).
  - Never hardcode bare `tunnelsats-web` or `tunnelsats-daemon` in `docker cp`/`docker exec`/`docker restart` commands. Resolve containers by Compose project + service label:
    ```bash
    APP_ID="${APP_ID:-tunnelsats-community}"
    WEB_CID=$(docker ps -q --filter "label=com.docker.compose.project=${APP_ID}" --filter "label=com.docker.compose.service=tunnelsats-web" | head -n 1)
    DAEMON_CID=$(docker ps -q --filter "label=com.docker.compose.project=${APP_ID}" --filter "label=com.docker.compose.service=tunnelsats-daemon" | head -n 1)
    ```

---

## Deployment & Development Sync

### Standard Umbrel Synchronization Workflow
Use the project's maintained synchronization workflow (`scripts/sync.sh node`), which stages source via rsync, safely injects code into the split-container architecture (`tunnelsats-daemon` and `tunnelsats-web`), and securely passes credentials via environment variables without leaking them into process arguments:

```bash
# Export password in environment (avoids argument exposure in process lists)
export UMBREL_PASSWORD="your_node_password"
export UMBREL_HOST="umbrel.local"

# Run automated hot-patching workflow (defaults to tunnelsats-community; set APP_ID=tunnelsats for Official)
./scripts/sync.sh node
```

For manual emergency fallback without the script (using project-aware container lookup instead of hardcoded container names):
```bash
export SSHPASS="$UMBREL_PASSWORD"
export APP_ID="${APP_ID:-tunnelsats-community}"

# 1. Compile Tailwind CSS bundle and sync source directory to Umbrel host
npm ci --ignore-scripts && npm run build:css
sshpass -e rsync -avz --exclude "node_modules" --exclude ".venv" ./ "umbrel@$UMBREL_HOST:dev-patch/"

# 2. Resolve target containers by Compose project + service labels, copy updated files, and restart
sshpass -e ssh "umbrel@$UMBREL_HOST" "APP_ID='$APP_ID' bash -s" <<'EOF'
set -euo pipefail
WEB_CID=$(sudo docker ps -q --filter "label=com.docker.compose.project=${APP_ID}" --filter "label=com.docker.compose.service=tunnelsats-web" | head -n 1)
DAEMON_CID=$(sudo docker ps -q --filter "label=com.docker.compose.project=${APP_ID}" --filter "label=com.docker.compose.service=tunnelsats-daemon" | head -n 1)
sudo docker cp /home/umbrel/dev-patch/server/. "${DAEMON_CID}:/app/server/"
sudo docker cp /home/umbrel/dev-patch/server/. "${WEB_CID}:/app/server/"
sudo docker cp /home/umbrel/dev-patch/web/. "${WEB_CID}:/app/web/"
sudo docker restart "${WEB_CID}" "${DAEMON_CID}"
EOF
```
