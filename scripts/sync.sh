#!/bin/bash
# TunnelSats Unified Synchronization & Workflow
# Standardized RSync deployment for Umbrel 1.x
# NO EXPERIMENTS. NO SYMLINKS. Strictly following umbrel-apps standards.

set -euo pipefail

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

UMBREL_HOST="${UMBREL_HOST:-umbrel.local}"

usage() {
    echo "Usage: $0 [node|monorepo|vendor|version]"
    exit 1
}

run_node() {
    log_info "Synchronizing to ${UMBREL_HOST} (Standard Store Sync)..."
    
    # Destination hash discovery or override (Standard Umbrel Index)
    REPO_HASH="${REPO_HASH:-${UMBREL_REPO_HASH:-getumbrel-umbrel-apps-github-53f74447}}"
    
    # Handle credentials
    export SSHPASS="${UMBREL_PASSWORD:-}"
    SSH_PREFIX=""
    if [ -n "$SSHPASS" ]; then SSH_PREFIX="sshpass -e "; fi
    
    # 1. STANDARDIZED RSYNC: Sync only the tunnelsats package to the official app-stores directory
    # SSH host-key trust trade-off: using 'accept-new' to silently trust new hosts for developer convenience (Grep ID 3033189219)
    ${SSH_PREFIX}rsync -av --delete --exclude=".gitkeep" \
        -e "ssh -o StrictHostKeyChecking=accept-new" \
        "${REPO_ROOT}/tunnelsats" \
        umbrel@${UMBREL_HOST}:/home/umbrel/umbrel/app-stores/${REPO_HASH}/
    
    log_info "Synchronizing docker-compose.yml separately to match project root logic..."
    ${SSH_PREFIX}scp -o StrictHostKeyChecking=accept-new \
        "${REPO_ROOT}/docker-compose.yml" \
        umbrel@${UMBREL_HOST}:/home/umbrel/umbrel/app-stores/${REPO_HASH}/tunnelsats/docker-compose.yml

    log_info "Restarting tunnelsats via Umbrel manager..."
    ${SSH_PREFIX}ssh -o StrictHostKeyChecking=accept-new umbrel@${UMBREL_HOST} \
        "umbreld client apps.restart.mutate --appId tunnelsats" || log_error "Manager failed to restart tunnelsats (registry desync). Try manual reboot."
}

run_monorepo() {
    log_info "Pushing to remote repository..."
    git push
}

run_vendor() {
    log_info "Updating vendor assets..."
    # Placeholder for vendor logic
    echo "[INFO] Vendor check finished."
}

run_version() {
    if [ "$#" -lt 1 ]; then log_error "Version argument required"; return 1; fi
    NEW_VERSION="$1"
    log_info "Updating version to ${NEW_VERSION}..."
    sed -i "s/version: .*/version: \"${NEW_VERSION}\"/" "${REPO_ROOT}/tunnelsats/umbrel-app.yml"
    sed -i "s/ts-umbrel-app:v.*/ts-umbrel-app:v${NEW_VERSION}/" "${REPO_ROOT}/docker-compose.yml"
}

# Ensure argument exists
if [ "$#" -lt 1 ]; then usage; fi

COMMAND="$1"
shift

case "${COMMAND}" in
    node) run_node ;;
    monorepo) run_monorepo ;;
    vendor) run_vendor ;;
    version) run_version "$@" ;;
    *) usage ;;
esac
