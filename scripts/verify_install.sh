#!/bin/bash
# Umbrel 1.x CLI Verification Script v2
set -e

PASSWORD="p9ZF3iPcjiKYOwutzNM6"
APP_ID="tunnelsats"
BASE_URL="http://localhost/trpc"

echo "--- Umbrel CLI Proof Workflow ---"

# 1. Login
echo "Logging in..."
TOKEN=$(curl -s -X POST "${BASE_URL}/user.login?batch=1" \
  -H 'Content-Type: application/json' \
  -d "{\"0\": {\"password\": \"${PASSWORD}\"}}" \
  | grep -oE '"data":"[^"]+"' | cut -d'"' -f4)

if [ -z "$TOKEN" ]; then
    echo "ERROR: Failed to acquire JWT."
    exit 1
fi
echo "JWT Acquired: ${TOKEN:0:10}..."

# 2. Trigger Install
echo "Triggering installation for '${APP_ID}'..."
INSTALL_RES=$(curl -s -X POST "${BASE_URL}/apps.install?batch=1" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H 'Content-Type: application/json' \
  -d "{\"0\": {\"appId\": \"${APP_ID}\"}}")

echo "Install response: ${INSTALL_RES}"

# 3. Poll State
echo "Polling installation state (Max 2m)..."
for i in {1..24}; do
    # Correctly escape the JSON input for GET
    INPUT="%7B%220%22%3A%7B%22appId%22%3A%22${APP_ID}%22%7D%7D"
    STATE=$(curl -s -G "${BASE_URL}/apps.state" \
      --data-urlencode "batch=1" \
      --data-urlencode "input={\"0\":{\"appId\":\"${APP_ID}\"}}" \
      -H "Authorization: Bearer ${TOKEN}" \
      | grep -oE '"state":"[^"]+"' | cut -d'"' -f4 || echo "unknown")
    
    echo "Current Status: ${STATE} ($((i*5))s)"
    
    if [ "$STATE" == "running" ] || [ "$STATE" == "installed" ]; then
        echo "SUCCESS: App is reported as ${STATE}."
        break
    fi
    
    if [ "$STATE" == "not-installed" ] && [ $i -gt 2 ]; then
         # Sometimes it takes a second to switch to 'installing'
         echo "App state stayed 'not-installed', checking if it already finished..."
    fi
    
    sleep 5
done

# 4. Final Logs Trace
echo ""
echo "--- Docker v3.1.0 Image Trace ---"
echo "p9ZF3iPcjiKYOwutzNM6" | sudo -S journalctl -u umbrel -n 100 | grep -Ei "pulling|downloading|image|v3.1.0" | tail -n 10

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
