#!/bin/bash
set -e

echo "Starting Tunnelsats v3 (Umbrel App)..."

# Start internal UI server
echo "Starting internal dashboard web server on port 9739..."
python3 /app/server/app.py &
API_PID=$!

# Clean up trap
cleanup() {
    echo "Received SIGTERM. Shutting down Tunnelsats..."
    kill $API_PID 2>/dev/null || true
    exit 0
}

trap 'cleanup' SIGTERM

echo "Tunnelsats container running. UI available on port 9739."

while true; do
    if [ -f "/tmp/tunnelsats_restart_trigger" ]; then
        echo "Restart trigger detected! Triggering docker restart policy..."
        rm -f "/tmp/tunnelsats_restart_trigger"
        kill $API_PID 2>/dev/null || true
        exit 1
    fi
    sleep 5
done
