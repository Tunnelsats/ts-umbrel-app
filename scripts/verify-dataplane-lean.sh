#!/bin/bash
# TunnelSats Dataplane Verification (Lean CI/CD Version)
# Optimized for automation, exit codes, and non-interactive environments

# Fail on any error in script logic
set -e

# Config
VPN_IP="REPLACE_WITH_VPN_IP"
VPN_HOST="REPLACE_WITH_VPN_HOST"
VPN_PORT="REPLACE_WITH_VPN_PORT"

# Load metadata if available
for meta_path in \
    "/data/tunnelsats-meta.json" \
    "/app/data/tunnelsats-meta.json" \
    "/home/umbrel/umbrel/app-data/tunnelsats/data/tunnelsats-meta.json" \
    "/umbrel/app-data/tunnelsats/data/tunnelsats-meta.json" \
    "./tunnelsats-meta.json"; do
    if [ -f "$meta_path" ] && command -v jq >/dev/null 2>&1; then
        METADATA="$(cat "$meta_path" 2>/dev/null)" || continue
        VPN_IP=$(echo "$METADATA" | jq -r '.vpn_ip // empty' | grep -m1 -oE '^[0-9.]+$' || echo "INVALID")
        VPN_HOST=$(echo "$METADATA" | jq -r '(.vpn_host // .serverDomain // empty)' | grep -m1 -oE '^[a-zA-Z0-9.-]+$' || echo "INVALID")
        VPN_PORT=$(echo "$METADATA" | jq -r '(.vpn_port // .vpnPort // empty)' | grep -m1 -oE '^[0-9]+$' || echo "INVALID")
        
        # Backward compatibility: derive IP from host if vpn_ip is missing.
        if [ "$VPN_IP" = "INVALID" ] && [ "$VPN_HOST" != "INVALID" ] && [ -n "$VPN_HOST" ]; then
            VPN_IP=$(getent hosts "$VPN_HOST" | awk '{ print $1 }' | head -n 1 || echo "INVALID")
        fi
        
        if [ "$VPN_IP" != "INVALID" ] && [ -n "$VPN_IP" ] && [ "$VPN_HOST" != "INVALID" ] && [ "$VPN_PORT" != "INVALID" ]; then
            break
        fi
    fi
done

if [[ "$VPN_IP" == "REPLACE_WITH_VPN_IP" || "$VPN_IP" == "INVALID" || -z "$VPN_IP" ]]; then
    echo "ERROR: No VPN configuration found (meta file missing and no manual config set)."
    exit 1
fi

EXIT_CODE=0

echo "[CI] Starting TunnelSats Dataplane Check..."

# 1. Outbound Test
echo -n "[1/3] Outbound Tunnel Alignment: "
if [[ -f "/.dockerenv" ]]; then
    OUTBOUND=$(curl -sL --interface 10.9.9.1 --max-time 10 ifconfig.me 2>/dev/null || echo "TIMEOUT")
else
    OUTBOUND=$(docker exec tunnelsats curl -sL --interface 10.9.9.1 --max-time 10 ifconfig.me 2>/dev/null || echo "TIMEOUT")
fi
if [[ "$OUTBOUND" == "$VPN_IP" ]]; then
    echo "OK"
else
    echo "FAILED (Got: $OUTBOUND)"
    EXIT_CODE=1
fi

# 2. Inbound IP Test
echo -n "[2/3] Inbound Port (IP):         "
if timeout 5s bash -c "true > /dev/tcp/${VPN_IP}/${VPN_PORT}" 2>/dev/null; then
    echo "OK"
else
    echo "FAILED"
    EXIT_CODE=1
fi

# 3. Inbound Hostname Test
echo -n "[3/3] Inbound Port (Hostname):   "
if timeout 5s bash -c "true > /dev/tcp/${VPN_HOST}/${VPN_PORT}" 2>/dev/null; then
    echo "OK"
else
    echo "FAILED"
    EXIT_CODE=1
fi

if [ $EXIT_CODE -ne 0 ]; then
    echo "FATAL: Dataplane verification failed!"
    exit 1
fi

echo "SUCCESS: Dataplane is healthy."
exit 0
