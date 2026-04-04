#!/bin/bash
# TunnelSats Unified Synchronization & Workflow
# Standardized RSync deployment for Umbrel 1.x
# NO EXPERIMENTS. Canonical compose lives under tunnelsats/ for app-store parity.

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
    echo "Usage: $0 [node|monorepo|vendor|version|promote]"
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
    sed -E -i "s#(ts-umbrel-app:v)[^@\" ]+(@sha256:[0-9a-f]{64})?#\1${NEW_VERSION}#" "${REPO_ROOT}/tunnelsats/docker-compose.yml"
}

run_promote() {
    log_info "Starting Release Promotion..."
    
    # 1. Version Discovery
    VERSION=$(grep "version: " "${REPO_ROOT}/tunnelsats/umbrel-app.yml" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
    if [ -z "$VERSION" ]; then log_error "Could not extract version"; return 1; fi
    log_info "Promoting version: v${VERSION}"
    
    # 2. Polling Docker Hub for authoritative Digest Index
    IMAGE="tunnelsats/ts-umbrel-app:v${VERSION}"
    log_info "Polling Docker Hub for $IMAGE multi-arch index digest..."
    DIGEST=$(docker buildx imagetools inspect "$IMAGE" | grep "Digest: " | head -1 | awk '{print $2}' || echo "")
    
    if [ -z "$DIGEST" ]; then
        log_error "Failed to retrieve digest from Docker Hub. Is the image published?"
        return 1
    fi
    log_info "Discovered Digest: ${DIGEST}"
    
    # 3. Pin local Source of Truth
    sed -E -i "s#(ts-umbrel-app:v)[^@\" ]+(@sha256:[0-9a-f]{64})?#\1${VERSION}@${DIGEST}#" "${REPO_ROOT}/tunnelsats/docker-compose.yml"
    log_info "Local docker-compose.yml successfully pinned."
    
    # 4. Monorepo Injection & Path Realignment
    UMBREL_APPS_DIR="${UMBREL_APPS_DIR:-${REPO_ROOT}/../umbrel-apps}"
    if [ ! -d "$UMBREL_APPS_DIR" ]; then
        log_error "Umbrel Apps store repo not found at ${UMBREL_APPS_DIR}. Set UMBREL_APPS_DIR manually."
        return 1
    fi
    
    log_info "Synchronizing to official monorepo at ${UMBREL_APPS_DIR}..."
    rsync -av --delete --exclude=".gitkeep" "${REPO_ROOT}/tunnelsats/" "${UMBREL_APPS_DIR}/tunnelsats/"
    
    # 5. Hybrid Pathing Strip
    TARGET_MANIFEST="${UMBREL_APPS_DIR}/tunnelsats/umbrel-app.yml"
    sed -i "s|https://raw.githubusercontent.com/Tunnelsats/ts-umbrel-app/master/tunnelsats/||g" "${TARGET_MANIFEST}"
    log_info "Manifest URLs stripped for CDN compatibility."
    
    log_info "Promotion complete. You can now commit the changes in ${UMBREL_APPS_DIR}."
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
    promote) run_promote ;;
    *) usage ;;
esac
