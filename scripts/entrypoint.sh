#!/bin/bash
set -euo pipefail

APP_NAME="tunnelsats"
WG_IFACE="tunnelsatsv2"
WG_CONF_PATH="/etc/wireguard/${WG_IFACE}.conf"
DOCKER_SOCK="/var/run/docker.sock"
STATE_FILE="/tmp/tunnelsats_state.json"
RECONCILE_TRIGGER="/tmp/tunnelsats_reconcile_trigger"
RECONCILE_RESULT="/tmp/tunnelsats_reconcile_result.json"
RESTART_TRIGGER="/tmp/tunnelsats_restart_trigger"
DOCKER_NETWORK_NAME="docker-tunnelsats"
DOCKER_NETWORK_SUBNET="10.9.9.0/25"
DOCKER_TARGET_IP="10.9.9.9"
LN_TARGET_PORT="9735"
RECONCILE_INTERVAL=30

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

main_loop() {
    while true; do
        if [ -f "${RESTART_TRIGGER}" ]; then
            log INFO "restart trigger detected"
            rm -f "${RESTART_TRIGGER}"
            if [ -n "${API_PID}" ]; then
                kill "${API_PID}" >/dev/null 2>&1 || true
            fi
            cleanup_dataplane
            exit 1
        fi

        if [ -f "${RECONCILE_TRIGGER}" ]; then
            local req
            req=$(cat "${RECONCILE_TRIGGER}" 2>/dev/null || true)
            rm -f "${RECONCILE_TRIGGER}"
            reconcile_once "manual" "${req}" || true
        fi

        local now
        now=$(date +%s)
        if [ $((now - LAST_RECONCILE_EPOCH)) -ge ${RECONCILE_INTERVAL} ]; then
            reconcile_once "periodic" || true
        fi

        sleep 2
    done
}

trap cleanup SIGTERM SIGINT

echo "Starting Tunnelsats v3 (Umbrel App)..."
log INFO "Starting internal dashboard server on port 9739"
python3 /app/server/app.py &
API_PID=$!

reconcile_once "startup" || true

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
