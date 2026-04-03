#!/bin/bash
# Umbrel 1.x CLI Verification Script v2
set -e

# Load password from .env.local if it exists
SCRIPT_DIR=$(dirname "$(readlink -f "$0")")
PARENT_DIR=$(dirname "$SCRIPT_DIR")

# Gemini ID 3032511352: Respect existing environment variables before sourcing fallback
if [ -z "${UMBREL_PASSWORD}" ] && [ -f "$PARENT_DIR/.env.local" ]; then
    set -a
    . "$PARENT_DIR/.env.local"
    set +a
fi

PASSWORD="${UMBREL_PASSWORD}"
APP_ID="tunnelsats"
BASE_URL="http://localhost/trpc"

if [ -z "$PASSWORD" ]; then
    echo "ERROR: UMBREL_PASSWORD not set in environment or .env.local"
    exit 1
fi

echo "--- Umbrel CLI Proof Workflow ---"

# 1. Login
# Gemini ID 3032453931: Use jq -Arg to escapce the password safely
echo "Logging in..."
JSON_LOGIN=$(jq -nc --arg pw "$PASSWORD" '{"0": {"password": $pw}}')
# Gemini ID 3032511355: Add explicit timeout to curl calls
TOKEN=$(curl --max-time 15 -s -X POST "${BASE_URL}/user.login?batch=1" \
  -H 'Content-Type: application/json' \
  -d "$JSON_LOGIN" \
  | jq -r '.[0].result.data')

if [ -z "$TOKEN" ] || [ "$TOKEN" == "null" ]; then
    echo "ERROR: Failed to acquire JWT."
    exit 1
fi
echo "JWT Acquired: ${TOKEN:0:10}..."

# 2. Trigger Install
# ID 3032453931: Use jq to build install payload
echo "Triggering installation for '${APP_ID}'..."
JSON_INSTALL=$(jq -nc --arg id "$APP_ID" '{"0": {"appId": $id}}')
INSTALL_RES=$(curl --max-time 15 -s -X POST "${BASE_URL}/apps.install?batch=1" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H 'Content-Type: application/json' \
  -d "$JSON_INSTALL")

echo "Install response: ${INSTALL_RES}"

# 3. Poll State
echo "Polling installation state (Max 2m)..."
JSON_INPUT=$(jq -nc --arg id "$APP_ID" '{"0": {"appId": $id}}')
for i in {1..24}; do
    STATE=$(curl --max-time 15 -s -G "${BASE_URL}/apps.state" \
      --data-urlencode "batch=1" \
      --data-urlencode "input=$JSON_INPUT" \
      -H "Authorization: Bearer ${TOKEN}" \
      | jq -r '.[0].result.data.state // "unknown"')
    
    echo "Current Status: ${STATE} ($((i*5))s)"
    
    if [ "$STATE" == "running" ] || [ "$STATE" == "installed" ]; then
        echo "SUCCESS: App is reported as ${STATE}."
        break
    fi
    
    if [ "$STATE" == "not-installed" ] && [ $i -gt 2 ]; then
         echo "App state stayed 'not-installed', checking if it already finished..."
    fi
    
    sleep 5
done

# 4. Final Logs Trace
echo ""
echo "--- Docker Image Trace ---"
# Greptile ID 3032444870: Use heredoc for sudo -S to avoid piped visibility
sudo -S journalctl -u umbrel -n 100 <<< "${PASSWORD}" | grep -Ei "pulling|downloading|image" | tail -n 10

# 5. Docker Inspection
echo ""
echo "--- Container Metadata ---"
CONTAINER_ID=$(docker ps -a | grep tunnelsats | awk '{print $1}' | head -n 1)
if [ -z "$CONTAINER_ID" ]; then
    echo "ERROR: No container found for ${APP_ID}."
    docker ps -a | head -n 5
else
    docker inspect "$CONTAINER_ID" | grep -Ei "Image\": \"|v3.1.0" | head -n 10
    echo "Container Name: $(docker inspect $CONTAINER_ID | grep Name | head -n 1)"
fi

echo "--- Proof Workflow Complete ---"
