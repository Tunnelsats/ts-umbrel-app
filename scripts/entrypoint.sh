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

API_PID=""
LAST_RECONCILE_EPOCH=0

TARGET_CONTAINER_ID=""
TARGET_CONTAINER_NAME=""
TARGET_IMPL=""
FORWARDING_PORT=""
BRIDGE_NAME=""
RULES_SYNCED="false"
LAST_ERROR=""

log() {
    local level="$1"
    shift
    printf '%s [%s] %s\n' "$(date -u +%FT%TZ)" "$level" "$*"
}

write_state() {
    jq -n \
        --arg dataplane_mode "docker-full-parity" \
        --arg target_container "${TARGET_CONTAINER_NAME:-}" \
        --arg target_ip "${DOCKER_TARGET_IP:-}" \
        --arg target_impl "${TARGET_IMPL:-}" \
        --arg forwarding_port "${FORWARDING_PORT:-}" \
        --argjson rules_synced "${RULES_SYNCED}" \
        --arg last_error "${LAST_ERROR:-}" \
        --arg docker_network_name "${DOCKER_NETWORK_NAME}" \
        --arg docker_network_subnet "${DOCKER_NETWORK_SUBNET}" \
        --arg bridge_name "${BRIDGE_NAME:-}" \
        --arg last_reconcile_at "$(date -u +%FT%TZ)" \
        '{
            dataplane_mode: $dataplane_mode,
            target_container: $target_container,
            target_ip: $target_ip,
            target_impl: $target_impl,
            forwarding_port: $forwarding_port,
            rules_synced: $rules_synced,
            last_reconcile_at: $last_reconcile_at,
            last_error: (if $last_error == "" then null else $last_error end),
            docker_network: {
                name: $docker_network_name,
                subnet: $docker_network_subnet,
                bridge: $bridge_name
            }
        }' > "${STATE_FILE}"
}

docker_api() {
    local method="$1"
    local path="$2"
    local data="${3:-}"

    if [ ! -S "${DOCKER_SOCK}" ]; then
        return 1
    fi

    if [ -n "${data}" ]; then
        curl -sS --fail --unix-socket "${DOCKER_SOCK}" -X "${method}" \
            -H "Content-Type: application/json" \
            -d "${data}" \
            "http://localhost${path}"
    else
        curl -sS --fail --unix-socket "${DOCKER_SOCK}" -X "${method}" \
            "http://localhost${path}"
    fi
}

docker_api_with_code() {
    local method="$1"
    local path="$2"
    local data="${3:-}"

    if [ -n "${data}" ]; then
        curl -sS --unix-socket "${DOCKER_SOCK}" -X "${method}" \
            -H "Content-Type: application/json" \
            -d "${data}" \
            -w "HTTPSTATUS:%{http_code}" \
            "http://localhost${path}"
    else
        curl -sS --unix-socket "${DOCKER_SOCK}" -X "${method}" \
            -w "HTTPSTATUS:%{http_code}" \
            "http://localhost${path}"
    fi
}

read_wg_config_path() {
    find /data -name "tunnelsats*.conf" -type f | head -n 1
}

extract_forwarding_port() {
    local cfg="$1"
    if [ -z "${cfg}" ] || [ ! -f "${cfg}" ]; then
        return 1
    fi

    local port
    port=$(grep -E '^#\s*(VPNPort|Port Forwarding)' "${cfg}" | head -n 1 | grep -oE '[0-9]{4,5}' | head -n 1 || true)
    if [ -z "${port}" ]; then
        return 1
    fi
    echo "${port}"
}

detect_lightning_container() {
    TARGET_CONTAINER_ID=""
    TARGET_CONTAINER_NAME=""
    TARGET_IMPL=""

    local containers
    containers=$(docker_api "GET" "/containers/json?all=0") || return 1

    local pick
    pick=$(echo "${containers}" | jq -r '
        def cname: (.Names[0] // "") | ltrimstr("/");
        [ .[]
          | {id: .Id, name: cname}
          | select(.name | test("lnd"))
        ]
        | if length > 0 then .[0] else empty end
        | "\(.id)|\(.name)|lnd"
    ')

    if [ -z "${pick}" ]; then
        pick=$(echo "${containers}" | jq -r '
            def cname: (.Names[0] // "") | ltrimstr("/");
            [ .[]
              | {id: .Id, name: cname}
              | select(.name | test("core-lightning|clightning|lightningd"))
            ]
            | if length > 0 then .[0] else empty end
            | "\(.id)|\(.name)|cln"
        ')
    fi

    if [ -z "${pick}" ]; then
        return 1
    fi

    TARGET_CONTAINER_ID="${pick%%|*}"
    local rest="${pick#*|}"
    TARGET_CONTAINER_NAME="${rest%%|*}"
    TARGET_IMPL="${rest##*|}"

    return 0
}

ensure_docker_network() {
    local response body code
    response=$(docker_api_with_code "GET" "/networks/${DOCKER_NETWORK_NAME}") || true
    body="${response%HTTPSTATUS:*}"
    code="${response##*HTTPSTATUS:}"

    if [ "${code}" = "404" ]; then
        log INFO "Creating docker network ${DOCKER_NETWORK_NAME} (${DOCKER_NETWORK_SUBNET})"
        docker_api "POST" "/networks/create" "$(jq -cn --arg name "${DOCKER_NETWORK_NAME}" --arg subnet "${DOCKER_NETWORK_SUBNET}" '{Name:$name, Driver:"bridge", IPAM:{Config:[{Subnet:$subnet}]}, Options:{"com.docker.network.driver.mtu":"1420"}}')" >/dev/null
        return 0
    fi

    if [ "${code}" != "200" ]; then
        LAST_ERROR="Unable to inspect docker network (${code})"
        return 1
    fi

    local current_subnet
    current_subnet=$(echo "${body}" | jq -r '.IPAM.Config[0].Subnet // empty')
    if [ -n "${current_subnet}" ] && [ "${current_subnet}" != "${DOCKER_NETWORK_SUBNET}" ]; then
        log WARN "docker-tunnelsats subnet is ${current_subnet}; expected ${DOCKER_NETWORK_SUBNET}"
    fi

    return 0
}

resolve_bridge_name() {
    local net
    net=$(docker_api "GET" "/networks/${DOCKER_NETWORK_NAME}") || return 1

    local bridge_id
    bridge_id=$(echo "${net}" | jq -r '.Id // empty' | cut -c1-12)
    if [ -z "${bridge_id}" ]; then
        return 1
    fi

    BRIDGE_NAME="br-${bridge_id}"
    return 0
}

ensure_container_attached() {
    local inspect
    inspect=$(docker_api "GET" "/containers/${TARGET_CONTAINER_ID}/json") || return 1

    local attached
    attached=$(echo "${inspect}" | jq -r --arg net "${DOCKER_NETWORK_NAME}" '.NetworkSettings.Networks[$net] != null')
    local current_ip
    current_ip=$(echo "${inspect}" | jq -r --arg net "${DOCKER_NETWORK_NAME}" '.NetworkSettings.Networks[$net].IPAddress // empty')

    if [ "${attached}" = "true" ] && [ "${current_ip}" = "${DOCKER_TARGET_IP}" ]; then
        return 0
    fi

    if [ "${attached}" = "true" ] && [ -n "${current_ip}" ] && [ "${current_ip}" != "${DOCKER_TARGET_IP}" ]; then
        log INFO "Disconnecting ${TARGET_CONTAINER_NAME} from ${DOCKER_NETWORK_NAME} (current IP: ${current_ip})"
        docker_api "POST" "/networks/${DOCKER_NETWORK_NAME}/disconnect" "$(jq -cn --arg c "${TARGET_CONTAINER_ID}" '{Container:$c, Force:true}')" >/dev/null || true
    fi

    log INFO "Connecting ${TARGET_CONTAINER_NAME} to ${DOCKER_NETWORK_NAME} (${DOCKER_TARGET_IP})"
    docker_api "POST" "/networks/${DOCKER_NETWORK_NAME}/connect" "$(jq -cn --arg c "${TARGET_CONTAINER_ID}" --arg ip "${DOCKER_TARGET_IP}" '{Container:$c, EndpointConfig:{IPAMConfig:{IPv4Address:$ip}}}')" >/dev/null
    return 0
}

ensure_wg_up() {
    local source_cfg
    source_cfg=$(read_wg_config_path)
    if [ -z "${source_cfg}" ]; then
        LAST_ERROR="No WireGuard config found in /data"
        return 1
    fi

    mkdir -p /etc/wireguard
    cp "${source_cfg}" "${WG_CONF_PATH}"

    FORWARDING_PORT="$(extract_forwarding_port "${source_cfg}" || true)"
    if [ -z "${FORWARDING_PORT}" ]; then
        LAST_ERROR="No forwarding port metadata found in config"
        return 1
    fi

    if ! wg show "${WG_IFACE}" >/dev/null 2>&1; then
        log INFO "Bringing up wireguard interface ${WG_IFACE}"
        wg-quick up "${WG_IFACE}" >/dev/null
    fi

    return 0
}

remove_tagged_iptables_rules() {
    local table="$1"
    local chain="$2"
    local marker="$3"

    local rules
    rules=$(iptables -t "${table}" -S "${chain}" | grep "${marker}" || true)
    if [ -z "${rules}" ]; then
        return 0
    fi

    while IFS= read -r rule; do
        [ -z "${rule}" ] && continue
        local del
        del=$(echo "${rule}" | sed -e 's/^-A /-D /' -e 's/^-I /-D /')
        iptables -t "${table}" ${del} >/dev/null 2>&1 || true
    done <<EOF_RULES
${rules}
EOF_RULES
}

ensure_policy_routing() {
    local changed=0

    if ! ip rule show | grep -q "from ${DOCKER_NETWORK_SUBNET} lookup 51820"; then
        ip rule add from "${DOCKER_NETWORK_SUBNET}" table 51820
        changed=1
    fi

    ip route replace default dev "${WG_IFACE}" metric 2 table 51820
    ip route replace blackhole default metric 3 table 51820
    ip route replace 10.9.0.0/24 dev "${WG_IFACE}" table 51820

    echo "${changed}"
}

ensure_nat_forward_rules() {
    remove_tagged_iptables_rules nat PREROUTING "tunnelsats-dnat"
    remove_tagged_iptables_rules filter FORWARD "tunnelsats-forward-in"
    remove_tagged_iptables_rules filter FORWARD "tunnelsats-forward-out"

    iptables -t nat -I PREROUTING -i "${WG_IFACE}" -p tcp --dport "${FORWARDING_PORT}" \
        -m comment --comment "tunnelsats-dnat" -j DNAT --to-destination "${DOCKER_TARGET_IP}:${LN_TARGET_PORT}"

    iptables -I FORWARD -i "${WG_IFACE}" -o "${BRIDGE_NAME}" \
        -m comment --comment "tunnelsats-forward-in" -j ACCEPT

    iptables -I FORWARD -i "${BRIDGE_NAME}" -o "${WG_IFACE}" \
        -m comment --comment "tunnelsats-forward-out" -j ACCEPT
}

rules_are_synced() {
    ip rule show | grep -q "from ${DOCKER_NETWORK_SUBNET} lookup 51820" || return 1
    iptables -t nat -C PREROUTING -i "${WG_IFACE}" -p tcp --dport "${FORWARDING_PORT}" \
        -m comment --comment "tunnelsats-dnat" -j DNAT --to-destination "${DOCKER_TARGET_IP}:${LN_TARGET_PORT}" >/dev/null 2>&1 || return 1
    iptables -C FORWARD -i "${WG_IFACE}" -o "${BRIDGE_NAME}" \
        -m comment --comment "tunnelsats-forward-in" -j ACCEPT >/dev/null 2>&1 || return 1
    iptables -C FORWARD -i "${BRIDGE_NAME}" -o "${WG_IFACE}" \
        -m comment --comment "tunnelsats-forward-out" -j ACCEPT >/dev/null 2>&1 || return 1
    return 0
}

cleanup_dataplane() {
    log INFO "Cleaning dataplane rules"
    remove_tagged_iptables_rules nat PREROUTING "tunnelsats-dnat"
    remove_tagged_iptables_rules filter FORWARD "tunnelsats-forward-in"
    remove_tagged_iptables_rules filter FORWARD "tunnelsats-forward-out"

    while ip rule show | grep -q "from ${DOCKER_NETWORK_SUBNET} lookup 51820"; do
        ip rule del from "${DOCKER_NETWORK_SUBNET}" table 51820 >/dev/null 2>&1 || break
    done

    ip route flush table 51820 >/dev/null 2>&1 || true

    if wg show "${WG_IFACE}" >/dev/null 2>&1; then
        wg-quick down "${WG_IFACE}" >/dev/null 2>&1 || true
    fi
}

write_reconcile_result() {
    local request_id="$1"
    local changed="$2"

    jq -n \
        --arg request_id "${request_id}" \
        --argjson changed "${changed}" \
        --slurpfile state "${STATE_FILE}" \
        '{request_id:$request_id, changed:$changed, state: ($state[0] // {})}' > "${RECONCILE_RESULT}"
}

reconcile_once() {
    local reason="$1"
    local request_id="${2:-}"
    local changed=0

    LAST_ERROR=""
    RULES_SYNCED="false"

    log INFO "reconcile_start reason=${reason}"

    if [ ! -S "${DOCKER_SOCK}" ]; then
        LAST_ERROR="Docker socket unavailable"
        write_state
        if [ -n "${request_id}" ]; then
            write_reconcile_result "${request_id}" false
        fi
        return 1
    fi

    if ! detect_lightning_container; then
        LAST_ERROR="No running LND/CLN container detected"
        write_state
        if [ -n "${request_id}" ]; then
            write_reconcile_result "${request_id}" false
        fi
        return 1
    fi

    if ! ensure_docker_network; then
        write_state
        if [ -n "${request_id}" ]; then
            write_reconcile_result "${request_id}" false
        fi
        return 1
    fi

    if ! ensure_container_attached; then
        LAST_ERROR="Failed to attach lightning container to docker-tunnelsats"
        write_state
        if [ -n "${request_id}" ]; then
            write_reconcile_result "${request_id}" false
        fi
        return 1
    fi

    if ! resolve_bridge_name; then
        LAST_ERROR="Failed to resolve docker bridge interface"
        write_state
        if [ -n "${request_id}" ]; then
            write_reconcile_result "${request_id}" false
        fi
        return 1
    fi

    if ! ensure_wg_up; then
        write_state
        if [ -n "${request_id}" ]; then
            write_reconcile_result "${request_id}" false
        fi
        return 1
    fi

    if [ "$(ensure_policy_routing)" = "1" ]; then
        changed=1
    fi

    ensure_nat_forward_rules
    changed=1

    if rules_are_synced; then
        RULES_SYNCED="true"
    else
        LAST_ERROR="Dataplane rules are not fully synced"
    fi

    write_state

    if [ -n "${request_id}" ]; then
        if [ "${changed}" -eq 1 ]; then
            write_reconcile_result "${request_id}" true
        else
            write_reconcile_result "${request_id}" false
        fi
    fi

    log INFO "reconcile_done reason=${reason} target=${TARGET_CONTAINER_NAME} port=${FORWARDING_PORT} synced=${RULES_SYNCED}"
    LAST_RECONCILE_EPOCH="$(date +%s)"

    return 0
}

cleanup() {
    log INFO "Received SIGTERM. Stopping ${APP_NAME}."
    cleanup_dataplane
    if [ -n "${API_PID}" ]; then
        kill "${API_PID}" >/dev/null 2>&1 || true
    fi
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
main_loop
