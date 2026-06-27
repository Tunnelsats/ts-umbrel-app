#!/bin/bash
# TunnelSats Node Diagnostic Utility (Development Wrapper)
# Proxies to the bundled troubleshooting script in tunnelsats/scripts/
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
    echo "[ERROR] This diagnostic script must be run as root. Please run with sudo: sudo bash $0" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TARGET_SCRIPT="${REPO_ROOT}/tunnelsats/scripts/verify.sh"

if [ ! -f "$TARGET_SCRIPT" ]; then
    echo "[ERROR] Verification script not found at $TARGET_SCRIPT" >&2
    exit 1
fi

# Relay all arguments
exec bash "$TARGET_SCRIPT" "$@"
