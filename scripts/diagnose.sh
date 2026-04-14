#!/bin/bash
# TunnelSats Node Diagnostic Utility (Development Wrapper)
# Proxies to the bundled troubleshooting script in tunnelsats/scripts/

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TARGET_SCRIPT="${REPO_ROOT}/tunnelsats/scripts/verify.sh"

if [ ! -f "$TARGET_SCRIPT" ]; then
    echo "[ERROR] Verification script not found at $TARGET_SCRIPT"
    exit 1
fi

chmod +x "$TARGET_SCRIPT"

# Relay all arguments
exec bash "$TARGET_SCRIPT" "$@"
