#!/bin/bash
# TunnelSats Dataplane Verification Suite
# Professional diagnostic tool for verifying Lightning Hybrid Networking

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Config (Defaults for current node)
VPN_IP="157.180.94.206"
VPN_HOST="fi1.tunnelsats.com"
VPN_PORT="39486"

# Load metadata if available (Support Umbrel app-data or direct /app/data)
for meta_path in "/app/data/tunnelsats-meta.json" "/home/umbrel/umbrel/app-data/tunnelsats/data/tunnelsats-meta.json" "./tunnelsats-meta.json"; do
    if [ -f "$meta_path" ] && command -v jq >/dev/null 2>&1; then
        METADATA=$(cat "$meta_path")
        VPN_IP=$(echo "$METADATA" | jq -r '.vpn_ip // "157.180.94.206"')
        VPN_HOST=$(echo "$METADATA" | jq -r '.vpn_host // "fi1.tunnelsats.com"')
        VPN_PORT=$(echo "$METADATA" | jq -r '.vpn_port // "39486"')
        break
    fi
done

FAILED_TESTS=0

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
    echo -e "${YELLOW}Target: ${NC}${VPN_HOST} (${VPN_IP}) : ${VPN_PORT}"
    echo -e "----------------------------------------------------------------"
}

footer() {
    echo -e "----------------------------------------------------------------"
    echo -e "${YELLOW}Need help?${NC}"
    echo -e "  • FAQ:     ${CYAN}https://tunnelsats.com/faq${NC}"
    echo -e "  • Website: ${CYAN}https://tunnelsats.com${NC}"
    echo -e "${BLUE}================================================================${NC}"
}

check_result() {
    if [ $1 -eq 0 ]; then
        echo -e "${GREEN}PASS${NC} ($2)"
    else
        echo -e "${RED}FAIL${NC} ($2)"
        FAILED_TESTS=$((FAILED_TESTS + 1))
    fi
}

header

# 1. Outbound Test
echo -ne "${YELLOW}[1/3] Testing Outbound Tunnel Alignment...${NC} "
OUTBOUND=$(docker exec tunnelsats curl -sL --interface 10.9.9.1 --max-time 10 ifconfig.me 2>/dev/null || echo "TIMEOUT")
if [[ "$OUTBOUND" == "$VPN_IP" ]]; then
    check_result 0 "Verified via ${VPN_IP}"
else
    check_result 1 "Leak Detected or Timeout (Got: ${OUTBOUND:-NONE})"
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
if timeout 5s bash -c "true > /dev/tcp/${VPN_HOST}/${VPN_PORT}" 2>/dev/null; then
    check_result 0 "Connected to ${VPN_HOST}:${VPN_PORT}"
else
    check_result 1 "DNS Failure or Connection Refused"
fi

echo ""
footer

if [ $FAILED_TESTS -gt 0 ]; then
    exit 1
fi
exit 0
