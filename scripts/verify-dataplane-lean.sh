#!/bin/bash
# TunnelSats Dataplane Verification (Lean CI/CD Version)
# Optimized for automation, exit codes, and non-interactive environments

# Fail on any error in script logic
set -e

# Config (Search order for metadata)
META_PATHS=(
    "/home/umbrel/umbrel/app-data/tunnelsats/data/tunnelsats-meta.json"
    "/umbrel/app-data/tunnelsats/data/tunnelsats-meta.json"
    "/app/data/tunnelsats-meta.json"
    "/data/tunnelsats-meta.json"
    "./tunnelsats-meta.json"
)

VPN_IP=""
VPN_HOST=""
VPN_PORT=""

# Autodetect Container Names
TS_CONT=$(docker ps --format '{{.Names}}' | grep -E '^tunnelsats(_tunnelsats_1)?$' | head -n 1)
LND_CONT=$(docker ps --format '{{.Names}}' | grep -E 'lightning_lnd_1|lnd|clightning|core-lightning|cln|lightningd' | grep -vE 'app|proxy|tor|web|ui' | head -n 1)

# Load metadata efficiently (Gemini ID 3032511359)
for meta_path in "${META_PATHS[@]}"; do
    if [ -f "$meta_path" ] && command -v jq >/dev/null 2>&1; then
        METADATA=$(cat "$meta_path")
        # Extract all fields in one jq call to minimize subshells
        read -r V_IP V_HOST V_PORT < <(echo "$METADATA" | jq -r '[.vpn_ip // .vpnIP // "", .vpn_host // .serverDomain // "", .vpn_port // .vpnPort // ""] | @tsv')
        
        VPN_IP=$(echo "$V_IP" | grep -m1 -oE '^[0-9.]+$' || echo "")
        VPN_HOST=$(echo "$V_HOST" | grep -m1 -oE '^[a-zA-Z0-9.-]+$' || echo "")
        VPN_PORT=$(echo "$V_PORT" | grep -m1 -oE '^[0-9]+$' || echo "")
        
        if [ -n "$VPN_HOST" ] && [ -z "$VPN_IP" ]; then
            VPN_IP=$(getent hosts "$VPN_HOST" | awk '{ print $1 }' | head -n 1)
        fi
        [ -n "$VPN_IP" ] && [ -n "$VPN_PORT" ] && break
    fi
done

if [ -z "$VPN_IP" ]; then
    echo "ERROR: No active VPN configuration metadata found."
    exit 1
fi

EXIT_CODE=0

echo "[CI] Starting TunnelSats Dataplane Check..."
[ -n "$TS_CONT" ] && echo "[CI] Detected TunnelSats: $TS_CONT"
[ -n "$LND_CONT" ] && echo "[CI] Detected Lightning:  $LND_CONT"

# 1. Outbound Test (Lightning Implementation)
echo -n "[1/3] Outbound Tunnel Alignment: "
if [ -n "$LND_CONT" ]; then
    OUTBOUND=$(docker exec "$LND_CONT" curl -sL --max-time 10 ifconfig.me 2>/dev/null || echo "TIMEOUT")
    if [[ "$OUTBOUND" == "$VPN_IP" ]]; then
        echo "OK"
    else
        echo "FAILED (Got: $OUTBOUND)"
        EXIT_CODE=1
    fi
else
    echo "SKIPPED (No Lightning container detected)"
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
if [ -n "$VPN_HOST" ]; then
    if timeout 5s bash -c "true > /dev/tcp/${VPN_HOST}/${VPN_PORT}" 2>/dev/null; then
        echo "OK"
    else
        echo "FAILED"
        EXIT_CODE=1
    fi
else
    echo "SKIPPED (No hostname)"
fi

if [ $EXIT_CODE -ne 0 ]; then
    echo "FATAL: Dataplane verification failed!"
    exit 1
fi

echo "SUCCESS: Dataplane is healthy."
exit 0
