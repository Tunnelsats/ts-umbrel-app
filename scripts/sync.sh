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

# Harden: Configurable hostname check (Grep ID 3032889217)
UMBREL_HOST="${UMBREL_HOST:-umbrel.local}"

usage() {
    echo "Usage: $0 [node|monorepo|vendor|version]"
    exit 1
}

run_node() {
    log_info "Synchronizing to umbrel.local..."
    # Replacement for deploy.py using rsync
    export SSHPASS="${UMBREL_PASSWORD:-}"
    if [ -z "$SSHPASS" ] && [ -z "${UMBREL_NO_PASSWORD:-}" ]; then log_error "UMBREL_PASSWORD missing"; return 1; fi

    # Destination hash discovery or override (Grep ID 3033313918)
    REPO_HASH="${REPO_HASH:-${UMBREL_REPO_HASH:-getumbrel-umbrel-apps-github-53f74447}}"
    
    # Use passwordless SSH if explicitly allowed or password missing
    SSH_PREFIX=""
    if [ -n "$SSHPASS" ]; then SSH_PREFIX="sshpass -e "; fi

    # Sync app-stores cache
    ${SSH_PREFIX}rsync -av --delete -e "ssh -o StrictHostKeyChecking=accept-new" "${REPO_ROOT}/tunnelsats/" umbrel@${UMBREL_HOST}:/home/umbrel/umbrel/app-stores/${REPO_HASH}/tunnelsats/
    
    # Sync active app-data
    ${SSH_PREFIX}rsync -av -e "ssh -o StrictHostKeyChecking=accept-new" "${REPO_ROOT}/docker-compose.yml" umbrel@${UMBREL_HOST}:/home/umbrel/umbrel/app-data/tunnelsats/docker-compose.yml
    
    # Optional: Sync src/server/web if needed for live-patching
    log_info "Restarting tunnelsats..."
    ${SSH_PREFIX}ssh -o StrictHostKeyChecking=accept-new umbrel@${UMBREL_HOST} "umbreld client apps.restart.mutate --appId tunnelsats"
}

run_node_install() {
    log_info "Triggering remote TunnelSats installation via tRPC..."
    PASSWORD="${UMBREL_PASSWORD:-}"
    if [ -z "$PASSWORD" ]; then log_error "UMBREL_PASSWORD missing"; return 1; fi
    
    # Login & Acquire Token
    JSON_LOGIN=$(jq -nc --arg pw "$PASSWORD" '{"0": {"password": $pw}}')
    TOKEN=$(curl --max-time 15 --connect-timeout 5 -s -X POST "http://${UMBREL_HOST}/trpc/user.login?batch=1" \
          -H 'Content-Type: application/json' -d "$JSON_LOGIN" | jq -r '.[0].result.data')
    
    if [ -z "$TOKEN" ] || [ "$TOKEN" == "null" ]; then log_error "Failed to acquire JWT"; return 1; fi

    # Trigger Install
    curl --max-time 30 --connect-timeout 10 -s -X POST "http://${UMBREL_HOST}/trpc/apps.install?batch=1" \
         -H 'Content-Type: application/json' \
         -H "Cookie: umbrel_auth_token=${TOKEN}" \
         -d '{"0":{"appId":"tunnelsats"}}'
    log_info "Install triggered successfully."
}

run_monorepo() {
    log_info "Synchronizing staging area to Community Monorepo..."
    SOURCE_DIR="${REPO_ROOT}/tunnelsats"
    # Portability: Allow user to override monorepo target path (Grep ID 3032833386)
    TARGET_DIR="${UMBREL_APPS_REPO:-/mnt/development/umbrel-apps}/tunnelsats"

    if [[ ! -d "${TARGET_DIR}" ]]; then log_error "Target monorepo not found at ${TARGET_DIR}"; return 1; fi

    # Decouple: Only sync metadata if explicitly requested to avoid overwriting production pinning (Grep ID 3033189212)
    if [[ "$*" == *"--meta"* ]]; then
        log_info "Syncing metadata (docker-compose & manifest)..."
        cp "${SOURCE_DIR}/umbrel-app.yml" "${TARGET_DIR}/"
        cp -L "${REPO_ROOT}/docker-compose.yml" "${TARGET_DIR}/"
    else
        log_info "Skipping metadata sync (Production-safe mode). Use '--meta' to force overwrite."
    fi

    # Always sync core assets
    cp "${SOURCE_DIR}/icon.svg" "${TARGET_DIR}/"
    mkdir -p "${TARGET_DIR}/gallery"
    cp "${SOURCE_DIR}/gallery/"*.png "${TARGET_DIR}/gallery/" 2>/dev/null || true
    log_info "Monorepo staging complete."
}

run_vendor() {
    log_info "Updating third-party vendor assets..."
    MANIFEST="${REPO_ROOT}/web/vendor/vendor.json"
    if [ ! -f "$MANIFEST" ]; then log_error "Vendor manifest missing"; return 1; fi

    FORCE=false
    # Restore 'force' argument (Grep ID 3032537592)
    if [[ "$*" == *"force"* ]]; then FORCE=true; fi

    assets=$(jq -c '.assets[]' "$MANIFEST")
    echo "$assets" | while IFS= read -r asset; do
        [ -z "$asset" ] && continue
        name=$(echo "$asset" | jq -r '.name')
        url=$(echo "$asset" | jq -r '.source_url')
        path="${REPO_ROOT}/$(echo "$asset" | jq -r '.local_path')"
        
        if [ "$FORCE" = true ] || [ ! -f "$path" ]; then
            log_info "Downloading $name..."
            # Ensure target directory exists (Grep ID 3032889234)
            mkdir -p "$(dirname "$path")"
            # Add network timeouts to prevent hanging (Grep ID 3033104620)
            curl --max-time 15 --connect-timeout 5 -L -s --fail "$url" -o "$path"
            log_info "✅ Updated $name at $path"
        else
            log_info "💎 $name is already localized at $path (use 'force' to refresh)"
        fi
    done
}

run_version() {
    log_info "Validating Version Parity..."
    MANIFEST_PATH="${REPO_ROOT}/tunnelsats/umbrel-app.yml"
    # Harden parsing: Anchor to start, allow indentation, more robust quote handling (Grep ID 3032889238)
    VERSION=$(grep -E '^\s*version:' "$MANIFEST_PATH" | sed -E 's/^\s*version:[[:space:]]*//' | tr -d '"' | tr -d "'" | awk '{print $1}')
    echo "Current Version: $VERSION"
}

case "${1:-node}" in
    node) run_node ;;
    node-install) run_node_install ;;
    monorepo) shift; run_monorepo "$@" ;;
    vendor) shift; run_vendor "$@" ;;
    version) run_version ;;
    *) usage ;;
esac
