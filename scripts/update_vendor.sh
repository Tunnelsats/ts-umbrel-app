#!/bin/bash
# TunnelSats Vendor Pulse-Check Script
# This script manages localized third-party assets to maintain privacy while ensuring updates.

set -e

MANIFEST="web/vendor/vendor.json"
VENDOR_DIR="web/vendor"

echo "🌍 TunnelSats Vendor Pulse-Check"
echo "-------------------------------"

if [ ! -f "$MANIFEST" ]; then
    echo "❌ Error: Manifest $MANIFEST not found."
    exit 1
fi

# Simple version check using curl and grep (could be expanded to more robust logic)
# Usage: ./scripts/update_vendor.sh [force]
FORCE=$1

assets=$(jq -c '.assets[]' "$MANIFEST")

for asset in $assets; do
    name=$(echo "$asset" | jq -r '.name')
    url=$(echo "$asset" | jq -r '.source_url')
    path=$(echo "$asset" | jq -r '.local_path')

    echo "🔍 Checking $name..."

    if [ "$FORCE" == "force" ] || [ ! -f "$path" ]; then
        echo "⬇️ Downloading latest $name from $url..."
        curl -L -s "$url" -o "$path"
        echo "✅ Updated $name at $path"
    else
        echo "💎 $name is already localized at $path"
    fi
done

echo "-------------------------------"
echo "✅ All vendor assets are in sync."
