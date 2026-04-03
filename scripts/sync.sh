#!/bin/bash
# TunnelSats Unified Synchronization & Workflow
# Consolidates node deployment, monorepo submission, vendor updates, and versioning.

set -euo pipefail

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

usage() {
    echo "Usage: $0 [node|monorepo|vendor|version]"
    exit 1
}

run_node() {
    log_info "Synchronizing to umbrel.local..."
    # Replacement for deploy.py using rsync
    export SSHPASS="${UMBREL_PASSWORD:-}"
    if [ -z "$SSHPASS" ]; then log_error "UMBREL_PASSWORD missing"; return 1; fi

    # Sync app-stores cache
    sshpass -e rsync -av --delete "${REPO_ROOT}/tunnelsats/" umbrel@umbrel.local:/home/umbrel/umbrel/app-stores/getumbrel-umbrel-apps-github-53f74447/tunnelsats/
    
    # Sync active app-data
    sshpass -e rsync -av "${REPO_ROOT}/docker-compose.yml" umbrel@umbrel.local:/home/umbrel/umbrel/app-data/tunnelsats/docker-compose.yml
    
    # Optional: Sync src/server/web if needed for live-patching
    log_info "Restarting tunnelsats..."
    sshpass -e ssh -o StrictHostKeyChecking=no umbrel@umbrel.local "umbreld client apps.restart.mutate --appId tunnelsats"
}

run_node_install() {
    log_info "Triggering remote TunnelSats installation via tRPC..."
    PASSWORD="${UMBREL_PASSWORD:-}"
    if [ -z "$PASSWORD" ]; then log_error "UMBREL_PASSWORD missing"; return 1; fi
    
    # Login & Acquire Token
    JSON_LOGIN=$(jq -nc --arg pw "$PASSWORD" '{"0": {"password": $pw}}')
    TOKEN=$(curl -s -X POST "http://umbrel.local/trpc/user.login?batch=1" \
          -H 'Content-Type: application/json' -d "$JSON_LOGIN" | jq -r '.[0].result.data')
    
    if [ -z "$TOKEN" ] || [ "$TOKEN" == "null" ]; then log_error "Failed to acquire JWT"; return 1; fi

    # Trigger Install
    JSON_INSTALL=$(jq -nc --arg id "tunnelsats" '{"0": {"appId": $id}}')
    curl -s -X POST "http://umbrel.local/trpc/apps.install?batch=1" \
         -H "Authorization: Bearer ${TOKEN}" \
         -H 'Content-Type: application/json' -d "$JSON_INSTALL"
    log_info "Install triggered successfully."
}

run_monorepo() {
    log_info "Synchronizing staging area to Community Monorepo..."
    SOURCE_DIR="${REPO_ROOT}/tunnelsats"
    TARGET_DIR="/mnt/development/umbrel-apps/tunnelsats"

    if [[ ! -d "${TARGET_DIR}" ]]; then log_error "Target monorepo not found"; return 1; fi

    cp "${SOURCE_DIR}/umbrel-app.yml" "${TARGET_DIR}/"
    cp "${SOURCE_DIR}/icon.svg" "${TARGET_DIR}/"
    cp -L "${REPO_ROOT}/docker-compose.yml" "${TARGET_DIR}/"
    
    mkdir -p "${TARGET_DIR}/gallery"
    cp "${SOURCE_DIR}/gallery-"*.png "${TARGET_DIR}/gallery/" 2>/dev/null || true
    log_info "Monorepo staging complete."
}

run_vendor() {
    log_info "Updating third-party vendor assets..."
    MANIFEST="${REPO_ROOT}/web/vendor/vendor.json"
    if [ ! -f "$MANIFEST" ]; then log_error "Vendor manifest missing"; return 1; fi

    assets=$(jq -c '.assets[]' "$MANIFEST")
    echo "$assets" | while IFS= read -r asset; do
        [ -z "$asset" ] && continue
        name=$(echo "$asset" | jq -r '.name')
        url=$(echo "$asset" | jq -r '.source_url')
        path="${REPO_ROOT}/$(echo "$asset" | jq -r '.local_path')"
        
        if [ ! -f "$path" ]; then
            log_info "Downloading $name..."
            curl -L -s --fail "$url" -o "$path"
        fi
    done
}

run_version() {
    log_info "Validating Version Parity..."
    MANIFEST_PATH="${REPO_ROOT}/tunnelsats/umbrel-app.yml"
    VERSION=$(grep 'version:' "$MANIFEST_PATH" | tr -d '"' | awk '{print $2}')
    echo "Current Version: $VERSION"
    # Logic from sync-version.sh...
}

case "${1:-node}" in
    node) run_node ;;
    node-install) run_node_install ;;
    monorepo) run_monorepo ;;
    vendor) run_vendor ;;
    version) run_version ;;
    *) usage ;;
esac
