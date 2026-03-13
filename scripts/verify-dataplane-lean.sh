#!/bin/bash
# TunnelSats Dataplane Verification (Lean CI/CD Version)
# Optimized for automation, exit codes, and non-interactive environments

# Fail on any error in script logic
set -e

# Config
VPN_IP="157.180.94.206"
VPN_HOST="fi1.tunnelsats.com"
VPN_PORT="39486"

EXIT_CODE=0

echo "[CI] Starting TunnelSats Dataplane Check..."

# 1. Outbound Test
echo -n "[1/3] Outbound Tunnel Alignment: "
OUTBOUND=$(docker exec tunnelsats curl -sL --interface 10.9.9.1 --max-time 10 ifconfig.me 2>/dev/null || echo "TIMEOUT")
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
