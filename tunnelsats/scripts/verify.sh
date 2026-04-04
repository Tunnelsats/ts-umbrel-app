#!/bin/bash
# TunnelSats Unified Verification & Diagnostics
# Consolidates installation proofing and dataplane connectivity tests.

set -euo pipefail

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

LEAN=false
if [[ "$*" == *"--lean"* ]]; then LEAN=true; fi

log_info() { if [ "$LEAN" = false ]; then echo -e "${GREEN}[INFO]${NC} $1"; fi; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

usage() {
    echo "Usage: $0 [node|dataplane] [--lean]"
    exit 1
}

# Config for dataplane
META_PATHS=(
    "/data/tunnelsats-meta.json"
    "/home/umbrel/umbrel/app-data/tunnelsats-data/tunnelsats-meta.json"
    "/home/umbrel/umbrel/app-data/tunnelsats/data/tunnelsats-meta.json"
)

run_node_check() {
    log_info "Verifying Umbrel App State..."
    # Simplified login/state check logic from verify_install.sh
    if ! command -v docker &> /dev/null; then log_error "Docker not found"; return 1; fi
    
    CONTAINER_ID=$(docker ps -aqf "name=tunnelsats" | head -n 1)
    if [ -z "$CONTAINER_ID" ]; then
        log_error "TunnelSats container not found."
        return 1
    fi
    
    STATE=$(docker inspect -f '{{.State.Status}}' "$CONTAINER_ID")
    log_info "Container State: $STATE"
    if [ "$STATE" != "running" ]; then return 1; fi
}

run_dataplane() {
    if [ "$LEAN" = false ]; then
        echo -e "${BLUE}=== TunnelSats Dataplane Verification ===${NC}"
    fi

    # Metadata discovery
    VPN_IP=""
    for p in "${META_PATHS[@]}"; do
        if [ -f "$p" ] && command -v jq &> /dev/null; then
            RAW_VAL=$(jq -r '(.vpn_ip // .vpnIP // .wgEndpoint // empty)' "$p" 2>/dev/null || true)
            if [ -n "$RAW_VAL" ]; then
                DOMAIN="${RAW_VAL%%:*}"
                if [[ "$DOMAIN" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
                    VPN_IP="$DOMAIN"
                else
                    VPN_IP=$(getent hosts "$DOMAIN" | awk '{ print $1 }' | head -n 1 || true)
                fi
            fi
            [ -n "$VPN_IP" ] && break
        fi
    done

    if [ -z "$VPN_IP" ]; then
        log_error "No active VPN metadata found."
        return 1
    fi

    # Outbound Tunnel Verification (Independent of lightning node presence)
    # Use TunnelSats container for curl (guaranteed presence) via the VPN interface
    OUTBOUND=$(docker exec tunnelsats curl -sL --interface tunnelsatsv2 --max-time 10 ifconfig.me 2>/dev/null || echo "TIMEOUT")
    if [[ "$OUTBOUND" == "$VPN_IP" ]]; then
        echo -e "Outbound Tunnel: ${GREEN}PASS${NC} (Verified via $VPN_IP)"
    else
        echo -e "Outbound Tunnel: ${RED}FAIL${NC} (Leak/Timeout: $OUTBOUND)"
        return 1
    fi

    if [ "$LEAN" = false ]; then echo "Verification Complete."; fi
}

case "${1:-dataplane}" in
    node) run_node_check ;;
    dataplane) run_dataplane ;;
    *) usage ;;
esac
