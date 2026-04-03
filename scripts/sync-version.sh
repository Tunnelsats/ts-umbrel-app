#!/bin/bash
# TunnelSats Version Synchronization & Validation Tool
# Prevents "Hold-back" and 404 deployment failures.
set -e

REPO="Tunnelsats/ts-umbrel-app"
DOCKER_ORG="tunnelsats"
IMAGE_NAME="ts-umbrel-app"
MANIFEST_PATH="tunnelsats/umbrel-app.yml"
COMPOSE_PATH="tunnelsats/docker-compose.yml"

echo "--- TunnelSats Sync-Version Check ---"

# 1. Directory Structure Consistency
SUBDIR=$(ls -d tunnelsats 2>/dev/null || echo "")
if [ -z "$SUBDIR" ]; then
    echo "❌ ERROR: 'tunnelsats/' subdirectory not found. Umbrel 1.x indexing will fail."
    exit 1
fi

# 2. Manifest ID vs Directory
MANIFEST_ID=$(grep 'id:' "$MANIFEST_PATH" | awk '{print $2}')
if [ "$MANIFEST_ID" != "tunnelsats" ]; then
    echo "❌ ERROR: Manifest 'id: ${MANIFEST_ID}' does not match folder 'tunnelsats'. 
       Umbrel 1.x will fail to locate template file paths."
    exit 1
fi
echo "✅ Directory/ID Consistency: PASS"

# 3. Version Parity (Manifest)
VERSION=$(grep 'version:' "$MANIFEST_PATH" | tr -d '"' | awk '{print $2}')
echo "🔍 Current Release Version: ${VERSION}"

# 4. Docker Hub Integrity Check
# We check if the tag exists on Docker Hub before allowing a PR submission
TAG="v${VERSION}"
echo "🔍 Validating Docker Hub tag: ${DOCKER_ORG}/${IMAGE_NAME}:${TAG}..."

# tRPC/API check for Docker Hub
HTTP_CODE=$(curl -s -L -o /dev/null -w "%{http_code}" "https://hub.docker.com/v2/repositories/${DOCKER_ORG}/${IMAGE_NAME}/tags/${TAG}")

if [ "$HTTP_CODE" != "200" ]; then
    # Fallback check for the old repository name
    HTTP_CODE_OLD=$(curl -s -L -o /dev/null -w "%{http_code}" "https://hub.docker.com/v2/repositories/${DOCKER_ORG}/umbrel-app/tags/${TAG}")
    
    if [ "$HTTP_CODE_OLD" != "200" ]; then
        echo "❌ ERROR: Tag '${TAG}' NOT FOUND on Docker Hub! (Check ${IMAGE_NAME} and umbrel-app)"
        echo "   Please push the image before finalizing the PR."
        exit 1
    else
        echo "⚠️  WARNING: Tag found on legacy repo 'umbrel-app'. Update docker-compose.yml namespace?"
    fi
fi
echo "✅ Docker Hub Parity: PASS"

# 5. Image/Compose Consistency
COMPOSE_IMAGE_TAG=$(grep 'image:' "$COMPOSE_PATH" | grep -o ':[^ ]*' | tr -d ':')
if [ "$COMPOSE_IMAGE_TAG" != "$TAG" ] && [ "$COMPOSE_IMAGE_TAG" != "latest" ] && [ "$COMPOSE_IMAGE_TAG" != "master" ]; then
    echo "❌ ERROR: Compose image tag '${COMPOSE_IMAGE_TAG}' differs from manifest version '${TAG}'."
    exit 1
fi
echo "✅ Compose Parity: PASS"

echo "--- 🏁 All Release Checks Passed! ---"
