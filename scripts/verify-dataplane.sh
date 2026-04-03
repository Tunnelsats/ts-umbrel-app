#!/bin/bash
# TunnelSats Dataplane Verification Suite (Umbrel-Edition)
# Professional diagnostic tool for verifying Lightning Hybrid Networking

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

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

# Load metadata
for meta_path in "${META_PATHS[@]}"; do
    if [ -f "$meta_path" ] && command -v jq >/dev/null 2>&1; then
        METADATA=$(cat "$meta_path")
        VPN_IP=$(echo "$METADATA" | jq -r '(.vpn_ip // .vpnIP // empty)' | grep -m1 -oE '^[0-9.]+$' || echo "")
        VPN_HOST=$(echo "$METADATA" | jq -r '(.vpn_host // .serverDomain // empty)' | grep -m1 -oE '^[a-zA-Z0-9.-]+$' || echo "")
        VPN_PORT=$(echo "$METADATA" | jq -r '(.vpn_port // .vpnPort // empty)' | grep -m1 -oE '^[0-9]+$' || echo "")
        
        if [ -n "$VPN_HOST" ] && [ -z "$VPN_IP" ]; then
            VPN_IP=$(getent hosts "$VPN_HOST" | awk '{ print $1 }' | head -n 1)
        fi
        [ -n "$VPN_IP" ] && [ -n "$VPN_PORT" ] && break
    fi
done

if [ -z "$VPN_IP" ]; then
    echo -e "${RED}ERROR: No active VPN configuration metadata found.${NC}"
    exit 1
fi

header() {
    clear
    echo -e "${BLUE}================================================================${NC}"
    echo -e "${BLUE}  _______                      _  _____       _                 ${NC}"
    echo -e "${BLUE} |__   __|                    | |/ ____|     | |                ${NC}"
    echo -e "${BLUE}    | |_   _ _ __  _ __   ___ | | (___   __ _| |_ ___           ${NC}"
    echo -e "${BLUE}    | | | | | '_ \| '_ \ / _ \| |\___ \ / _' | __/ __|          ${NC}"
    echo -e "${BLUE}    | | |_| | | | | | | |  __/| |____) | (_| | |_\__ \          ${NC}"
    echo -e "${BLUE}    |_|\__,_|_| |_|_| |_|\___||_|_____/ \__,_|\__|___/          ${NC}"
    echo -e "${BLUE}                                                                ${NC}"
    echo -e "${CYAN}             TunnelSats Hybrid Data-Plane Verification          ${NC}"
    echo -e "${BLUE}================================================================${NC}"
    echo -e "${YELLOW}Target: ${NC}${VPN_HOST:-UNKNOWN} (${VPN_IP}) : ${VPN_PORT}"
    echo -e "${YELLOW}Detected TunnelSats: ${NC}${TS_CONT:-MISSING}"
    echo -e "${YELLOW}Detected Lightning:  ${NC}${LND_CONT:-MISSING}"
    echo -e "----------------------------------------------------------------"
}

footer() {
    echo -e "----------------------------------------------------------------"
    echo -e "${YELLOW}Need help?${NC}"
    echo -e "  • FAQ:     ${CYAN}https://tunnelsats.com/faq${NC}"
    echo -e "  • Website: ${CYAN}https://tunnelsats.com${NC}"
    echo -e "${BLUE}================================================================${NC}"
}

FAILED_TESTS=0

check_result() {
    if [ $1 -eq 0 ]; then
        echo -e "${GREEN}PASS${NC} ($2)"
    else
        echo -e "${RED}FAIL${NC} ($2)"
        FAILED_TESTS=$((FAILED_TESTS + 1))
    fi
}

header

# 1. Outbound Test (LND)
echo -ne "${YELLOW}[1/3] Testing LND Outbound Tunnel...        ${NC} "
if [ -n "$LND_CONT" ]; then
    OUTBOUND=$(docker exec "$LND_CONT" curl -sL --max-time 10 ifconfig.me 2>/dev/null || echo "TIMEOUT")
    if [[ "$OUTBOUND" == "$VPN_IP" ]]; then
        check_result 0 "Verified via ${VPN_IP}"
    else
        check_result 1 "Leak Detected or Timeout (Got: ${OUTBOUND:-NONE})"
    fi
else
    check_result 1 "LND container not found"
fi

# 2. Inbound IP Test
echo -ne "${YELLOW}[2/3] Testing Inbound Port (via IP)...       ${NC} "
if timeout 5s bash -c "true > /dev/tcp/${VPN_IP}/${VPN_PORT}" 2>/dev/null; then
    check_result 0 "Connected to ${VPN_IP}:${VPN_PORT}"
else
    check_result 1 "Connection Refused/Timeout"
fi

# 3. Inbound Hostname Test
echo -ne "${YELLOW}[3/3] Testing Inbound Port (via Hostname)... ${NC} "
if [ -n "$VPN_HOST" ]; then
    if timeout 5s bash -c "true > /dev/tcp/${VPN_HOST}/${VPN_PORT}" 2>/dev/null; then
        check_result 0 "Connected to ${VPN_HOST}:${VPN_PORT}"
    else
        check_result 1 "DNS Failure or Connection Refused"
    fi
else
    check_result 1 "No VPN hostname in metadata"
fi

echo ""
footer

if [ "${FAILED_TESTS:-0}" -gt 0 ]; then
    exit 1
fi
