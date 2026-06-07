#!/bin/bash
set -euo pipefail

APP_NAME="tunnelsats"
WG_IFACE="tunnelsatsv2"
WG_CONF_PATH="/etc/wireguard/${WG_IFACE}.conf"
DOCKER_SOCK="/var/run/docker.sock"
STATE_FILE="/tmp/tunnelsats_state.json"
RECONCILE_TRIGGER_DIR="/tmp/tunnelsats_reconcile_trigger.d"
RECONCILE_RESULT_DIR="/tmp/tunnelsats_reconcile_result.d"
RECONCILE_TRIGGER_LEGACY="/tmp/tunnelsats_reconcile_trigger"
RECONCILE_RESULT_LEGACY="/tmp/tunnelsats_reconcile_result.json"
RESTART_TRIGGER="/tmp/tunnelsats_restart_trigger"
DOCKER_NETWORK_NAME="docker-tunnelsats"
DOCKER_NETWORK_SUBNET="10.9.9.0/25"
DOCKER_TARGET_IP="10.9.9.9"
LN_TARGET_PORT="9735" # Default to LND, will be updated in detect_lightning_container
RECONCILE_INTERVAL=30

# k3s mode: set K3S_MODE=true to bypass Docker networking and use Kubernetes Services instead.
# These are explicitly exported so the Python dashboard (launched as a child process below)
# always sees them, even if a future caller passes them as plain shell vars instead of env.
export K3S_MODE="${K3S_MODE:-false}"
export LND_K8S_SERVICE="${LND_K8S_SERVICE:-}"
export CLN_K8S_SERVICE="${CLN_K8S_SERVICE:-}"
export K8S_NAMESPACE="${K8S_NAMESPACE:-default}"
# Namespace where LND/CLN live — defaults to the same namespace as tunnelsats,
# but must be set explicitly when they run in a different namespace.
export LND_K8S_NAMESPACE="${LND_K8S_NAMESPACE:-${K8S_NAMESPACE}}"
export CLN_K8S_NAMESPACE="${CLN_K8S_NAMESPACE:-${K8S_NAMESPACE}}"
export LND_K8S_POD_SELECTOR="${LND_K8S_POD_SELECTOR:-app=lnd}"
export CLN_K8S_POD_SELECTOR="${CLN_K8S_POD_SELECTOR:-app=cln}"
K8S_SA_TOKEN_PATH="/var/run/secrets/kubernetes.io/serviceaccount/token"
K8S_SA_CA_PATH="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
K8S_API_URL="https://kubernetes.default.svc"

API_PID=""
LAST_RECONCILE_EPOCH=0

TARGET_CONTAINER_ID=""
TARGET_CONTAINER_NAME=""
TARGET_IMPL=""
FORWARDING_PORT=""
BRIDGE_NAME=""
RULES_SYNCED="false"
LAST_ERROR=""
POLICY_CHANGED="0"
NAT_CHANGED="0"

log() {
    local level="$1"
    shift
    printf '%s [%s] %s\n' "$(date -u +%FT%TZ)" "$level" "$*" >&2
}

if [[ "${K3S_MODE}" == "true" ]] && [ ! -f "${K8S_SA_TOKEN_PATH}" ]; then
    log WARN "K3S_MODE is enabled but Kubernetes ServiceAccount token is missing. Falling back to Docker mode."
    export K3S_MODE="false"
fi

is_valid_request_id() {
    local request_id="$1"
    [[ "${request_id}" =~ ^[A-Za-z0-9_-]{1,128}$ ]]
}

ensure_reconcile_dirs() {
    mkdir -p "${RECONCILE_TRIGGER_DIR}" "${RECONCILE_RESULT_DIR}"
}

reconcile_result_path() {
    local request_id="$1"
    printf '%s/%s.json' "${RECONCILE_RESULT_DIR}" "${request_id}"
}

write_state() {
    local tmp
    tmp="$(mktemp "${STATE_FILE}.tmp.XXXXXX")"

    local dataplane_mode
    dataplane_mode="$([[ "${K3S_MODE}" == "true" ]] && echo "k3s" || echo "docker-full-parity")"

    if jq -n \
        --arg dataplane_mode "${dataplane_mode}" \
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
        }' > "${tmp}"; then
        mv -f "${tmp}" "${STATE_FILE}"
    else
        rm -f "${tmp}"
        return 1
    fi
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
        curl -sS --noproxy "*" --unix-socket "${DOCKER_SOCK}" -X "${method}" \
            -H "Content-Type: application/json" \
            -d "${data}" \
            -w "HTTPSTATUS:%{http_code}" \
            "http://localhost${path}"
    else
        curl -sS --noproxy "*" --unix-socket "${DOCKER_SOCK}" -X "${method}" \
            -w "HTTPSTATUS:%{http_code}" \
            "http://localhost${path}"
    fi
}

k8s_api() {
    local path="$1"
    local token
    token=$(cat "${K8S_SA_TOKEN_PATH}" 2>/dev/null) || { log WARN "k8s: Cannot read service account token"; return 1; }
    curl -sf --connect-timeout 5 --max-time 10 --cacert "${K8S_SA_CA_PATH}" \
        -H "Authorization: Bearer ${token}" \
        "${K8S_API_URL}${path}"
}

# Percent-encode a string for safe use inside a URL query parameter. Used for
# labelSelector values which may contain '=', ',', '!', spaces, etc.
urlencode() {
    local s="$1" i c out=""
    for (( i=0; i<${#s}; i++ )); do
        c="${s:i:1}"
        case "${c}" in
            [a-zA-Z0-9._~-]) out+="${c}" ;;
            *) out+=$(printf '%%%02X' "'${c}") ;;
        esac
    done
    printf '%s' "${out}"
}

read_wg_config_path() {
    local -a files=()
    # Use ls -1t for flat (non-recursive), time-ordered discovery.
    # grep -v '.bak' explicitly excludes any rotation artifacts (*.conf.bak, *.conf.bak.1, etc.)
    # that server/app.py leaves in the same /data/ directory.
    mapfile -t files < <(ls -1t /data/tunnelsats*.conf 2>/dev/null | grep -E -v '\.bak(\.[0-9]+)*$' || true)
    if [ "${#files[@]}" -gt 1 ]; then
        log WARN "Multiple tunnelsats*.conf files found, using most recent: ${files[0]}"
    fi
    echo "${files[0]:-}"
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
    if [ "${port}" -lt 1 ] || [ "${port}" -gt 65535 ]; then
        return 1
    fi
    echo "${port}"
}

# Resolve a k8s Service to its ClusterIP. Tries the FQDN first, then the short name.
# Distinguishes the common "name not found" case (getent rc=2) from real resolver
# failures (rc=1/3/other) so the logs make it clear whether DNS is broken or the
# Service simply does not exist yet.
resolve_svc_ip() {
    local fqdn="$1" name="$2"
    local out rc

    out=$(getent hosts "${fqdn}" 2>&1)
    rc=$?
    if [ "${rc}" -eq 0 ]; then
        echo "${out}" | awk '{print $1}' | head -n1
        return 0
    elif [ "${rc}" -ne 2 ]; then
        log WARN "k3s: getent failed for ${fqdn} (rc=${rc}): ${out}"
    fi

    out=$(getent hosts "${name}" 2>&1)
    rc=$?
    if [ "${rc}" -eq 0 ]; then
        echo "${out}" | awk '{print $1}' | head -n1
        return 0
    elif [ "${rc}" -ne 2 ]; then
        log WARN "k3s: getent failed for ${name} (rc=${rc}): ${out}"
    fi

    return 1
}

detect_k3s_target() {
    TARGET_CONTAINER_ID=""
    TARGET_CONTAINER_NAME=""
    TARGET_IMPL=""

    local svc_name svc_fqdn svc_ip

    if [ -n "${LND_K8S_SERVICE}" ]; then
        svc_name="${LND_K8S_SERVICE}"
        svc_fqdn="${svc_name}.${LND_K8S_NAMESPACE}.svc.cluster.local"
        svc_ip=$(resolve_svc_ip "${svc_fqdn}" "${svc_name}" || true)
        if [ -n "${svc_ip}" ]; then
            TARGET_IMPL="lnd"
            TARGET_CONTAINER_NAME="${svc_name}"
            LN_TARGET_PORT="9735"
            log INFO "k3s: Detected LND service ${svc_fqdn} at ClusterIP ${svc_ip}"
            # Use the actual pod IP for DNAT and policy routing to avoid asymmetric
            # routing caused by kube-proxy's double NAT through the ClusterIP.
            local pod_ip encoded_selector
            encoded_selector=$(urlencode "${LND_K8S_POD_SELECTOR}")
            pod_ip=$(k8s_api "/api/v1/namespaces/${LND_K8S_NAMESPACE}/pods?labelSelector=${encoded_selector}" 2>/dev/null \
                | jq -r '.items[] | select(.status.phase == "Running") | .status.podIP' 2>/dev/null \
                | head -n1 || true)
            if [ -n "${pod_ip}" ]; then
                DOCKER_TARGET_IP="${pod_ip}"
                log INFO "k3s: Using LND pod IP ${pod_ip} for direct routing (bypasses kube-proxy)"
            else
                LAST_ERROR="k3s: Could not resolve LND pod IP"
                log ERROR "k3s: Could not resolve LND pod IP, direct routing is required for WireGuard CONNMARK"
                return 1
            fi
            return 0
        fi
        log WARN "k3s: Could not resolve LND service ${svc_fqdn}"
    fi

    if [ -n "${CLN_K8S_SERVICE}" ]; then
        svc_name="${CLN_K8S_SERVICE}"
        svc_fqdn="${svc_name}.${CLN_K8S_NAMESPACE}.svc.cluster.local"
        svc_ip=$(resolve_svc_ip "${svc_fqdn}" "${svc_name}" || true)
        if [ -n "${svc_ip}" ]; then
            TARGET_IMPL="cln"
            TARGET_CONTAINER_NAME="${svc_name}"
            LN_TARGET_PORT="9736"
            log INFO "k3s: Detected CLN service ${svc_fqdn} at ClusterIP ${svc_ip}"
            local pod_ip encoded_selector
            encoded_selector=$(urlencode "${CLN_K8S_POD_SELECTOR}")
            pod_ip=$(k8s_api "/api/v1/namespaces/${CLN_K8S_NAMESPACE}/pods?labelSelector=${encoded_selector}" 2>/dev/null \
                | jq -r '.items[] | select(.status.phase == "Running") | .status.podIP' 2>/dev/null \
                | head -n1 || true)
            if [ -n "${pod_ip}" ]; then
                DOCKER_TARGET_IP="${pod_ip}"
                log INFO "k3s: Using CLN pod IP ${pod_ip} for direct routing (bypasses kube-proxy)"
            else
                LAST_ERROR="k3s: Could not resolve CLN pod IP"
                log ERROR "k3s: Could not resolve CLN pod IP, direct routing is required for WireGuard CONNMARK"
                return 1
            fi
            return 0
        fi
        log WARN "k3s: Could not resolve CLN service ${svc_fqdn}"
    fi

    LAST_ERROR="k3s: No LND/CLN service resolved (LND_K8S_SERVICE=${LND_K8S_SERVICE:-}, CLN_K8S_SERVICE=${CLN_K8S_SERVICE:-})"
    return 1
}

detect_lightning_container() {
    TARGET_CONTAINER_ID=""
    TARGET_CONTAINER_NAME=""
    TARGET_IMPL=""

    if [[ "${K3S_MODE}" == "true" ]]; then
        detect_k3s_target || return 1
        return 0
    fi

    local containers
    containers=$(docker_api "GET" "/containers/json?all=0") || return 1

    local pick
    pick=$(echo "${containers}" | jq -r '
        def cname: (.Names[0] // "") | ltrimstr("/");
        [ .[]
          | {id: .Id, name: cname}
          | select(.name | test("(^|[_-])lnd([_-]|$)"))
          | select(.name | test("(app|proxy|tor|web|ui)") | not)
        ]
        | if length > 0 then .[0] else empty end
        | "\(.id)|\(.name)|lnd"
    ')

    if [ -z "${pick}" ]; then
        pick=$(echo "${containers}" | jq -r '
            def cname: (.Names[0] // "") | ltrimstr("/");
            [ .[]
              | {id: .Id, name: cname}
              | select(.name | test("(^|[_-])(core-lightning|clightning|lightningd)([_-]|$)"))
              | select(.name | test("(app|proxy|tor|web|ui)") | not)
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

    if [ "${TARGET_IMPL}" = "cln" ]; then
        LN_TARGET_PORT="9736"
    else
        LN_TARGET_PORT="9735"
    fi

    return 0
}

ensure_docker_network() {
    [[ "${K3S_MODE}" == "true" ]] && return 0
    local response body code
    response=$(docker_api_with_code "GET" "/networks/${DOCKER_NETWORK_NAME}") || true
    body="${response%HTTPSTATUS:*}"
    code="${response##*HTTPSTATUS:}"

    if [ "${code}" = "404" ]; then
        log INFO "Creating docker network ${DOCKER_NETWORK_NAME} (${DOCKER_NETWORK_SUBNET})"
        if ! docker_api "POST" "/networks/create" "$(jq -cn --arg name "${DOCKER_NETWORK_NAME}" --arg subnet "${DOCKER_NETWORK_SUBNET}" '{Name:$name, Driver:"bridge", IPAM:{Config:[{Subnet:$subnet}]}, Options:{"com.docker.network.driver.mtu":"1420"}}')" >/dev/null; then
            LAST_ERROR="Failed to create docker network ${DOCKER_NETWORK_NAME}"
            return 1
        fi
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
    if [[ "${K3S_MODE}" == "true" ]]; then
        BRIDGE_NAME=""
        return 0
    fi
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
    [[ "${K3S_MODE}" == "true" ]] && return 0
    local inspect
    inspect=$(docker_api "GET" "/containers/${TARGET_CONTAINER_ID}/json") || return 1

    local attached
    attached=$(echo "${inspect}" | jq -r --arg net "${DOCKER_NETWORK_NAME}" '.NetworkSettings.Networks[$net] != null')
    local current_ip
    current_ip=$(echo "${inspect}" | jq -r --arg net "${DOCKER_NETWORK_NAME}" '.NetworkSettings.Networks[$net].IPAddress // empty')

    if [ "${attached}" = "true" ] && [ "${current_ip}" = "${DOCKER_TARGET_IP}" ]; then
        return 0
    fi

    if [ "${attached}" = "true" ] && [ "${current_ip}" != "${DOCKER_TARGET_IP}" ]; then
        log INFO "Disconnecting ${TARGET_CONTAINER_NAME} from ${DOCKER_NETWORK_NAME} (force clean for IP: ${current_ip:-NONE})"
        docker_api "POST" "/networks/${DOCKER_NETWORK_NAME}/disconnect" "$(jq -cn --arg c "${TARGET_CONTAINER_ID}" '{Container:$c, Force:true}')" >/dev/null || true
    fi

    log INFO "Connecting ${TARGET_CONTAINER_NAME} to ${DOCKER_NETWORK_NAME} (${DOCKER_TARGET_IP})"
    if ! docker_api "POST" "/networks/${DOCKER_NETWORK_NAME}/connect" "$(jq -cn --arg c "${TARGET_CONTAINER_ID}" --arg ip "${DOCKER_TARGET_IP}" '{Container:$c, EndpointConfig:{IPAMConfig:{IPv4Address:$ip}}}')" >/dev/null; then
        LAST_ERROR="Failed to connect ${TARGET_CONTAINER_NAME} to ${DOCKER_NETWORK_NAME}"
        return 1
    fi
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

    # Ensure WireGuard doesn't aggressively hijack the host routing table via AllowedIPs=0.0.0.0/0
    sed -i '/^\s*Table\s*=/Id' "${WG_CONF_PATH}"
    sed -i '/^\[Interface\]/a Table = off' "${WG_CONF_PATH}"

    FORWARDING_PORT="$(extract_forwarding_port "${source_cfg}" || true)"
    if [ -z "${FORWARDING_PORT}" ]; then
        LAST_ERROR="No forwarding port metadata found in config"
        return 1
    fi

    if wg show "${WG_IFACE}" >/dev/null 2>&1; then
        local stripped_cfg
        stripped_cfg="$(mktemp)"
        if ! wg-quick strip "${WG_CONF_PATH}" > "${stripped_cfg}" 2>/dev/null; then
            rm -f "${stripped_cfg}"
            LAST_ERROR="Failed to prepare WireGuard sync config for ${WG_IFACE}"
            return 1
        fi

        log INFO "WireGuard interface ${WG_IFACE} exists; syncing config"
        if ! wg syncconf "${WG_IFACE}" "${stripped_cfg}" >/dev/null 2>&1; then
            log WARN "syncconf failed for ${WG_IFACE}; recreating interface"
            wg-quick down "${WG_IFACE}" >/dev/null 2>&1 || true
            if ! wg-quick up "${WG_IFACE}" >/dev/null 2>&1; then
                rm -f "${stripped_cfg}"
                LAST_ERROR="Failed to reconfigure WireGuard interface ${WG_IFACE}"
                return 1
            fi
        fi
        rm -f "${stripped_cfg}"
    else
        log INFO "Bringing up wireguard interface ${WG_IFACE}"
        if ! wg-quick up "${WG_IFACE}" >/dev/null 2>&1; then
            LAST_ERROR="Failed to bring up WireGuard interface ${WG_IFACE}"
            return 1
        fi
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
    POLICY_CHANGED="0"
    
    if [[ "${K3S_MODE}" == "true" ]]; then
        # k3s mode: fwmark-based routing so ONLY replies to WireGuard-originated connections
        # are sent back through the tunnel. IP-source rules (from <pod-ip> lookup 51820) route
        # ALL traffic from the LND pod through WG, which breaks thunderhub/lndg and causes
        # chacha20poly1305 auth failures on LND's own outbound P2P connections.
        # The mangle rules added in ensure_nat_forward_rules() mark incoming WG packets in
        # FORWARD (after DNAT) and save the mark to conntrack; CONNMARK --restore-mark in
        # PREROUTING then tags reply packets from LND so they take the fwmark routing path.

        # Remove ALL IP-source rules at our reserved priorities — not just for the
        # current pod IP. LND pod IP can change across restarts; if the old tunnelsats
        # pod was SIGKILL'd, cleanup never ran and stale rules for the old IP remain.
        #
        # Only touch rules that look like ours — i.e. rules that route to table 51820 or
        # table main. This avoids accidentally wiping unrelated rules that some other
        # operator on the host might have parked at these prefs.
        local _pref _line _spec
        for _pref in 32500 32763 32764; do
            while IFS= read -r _line; do
                [[ -z "${_line}" ]] && continue
                _spec=$(echo "${_line}" | sed 's/^[0-9]*:[[:space:]]*//')
                [[ -z "${_spec}" ]] && continue
                local -a _spec_arr
                read -r -a _spec_arr <<< "${_spec}"
                ip rule del "${_spec_arr[@]}" >/dev/null 2>&1 || true
            done < <(ip rule show pref "${_pref}" 2>/dev/null | grep -v "fwmark" | grep -E "lookup (51820|main)" || true)
        done

        # Single fwmark rule: only packets carrying fwmark 51820 go through table 51820.
        if ! ip rule show | grep -qE "fwmark 0x[cC][aA]6[cC].*lookup 51820"; then
            if ! ip rule add fwmark 51820 table 51820 pref 32764 >/dev/null 2>&1; then
                if ! ip rule show pref 32764 | grep -q "fwmark"; then
                    LAST_ERROR="k3s: Failed to add fwmark policy routing rule"
                    return 1
                fi
            fi
            changed=1
        fi
    else
        # Docker mode: subnet-based rules for the bridge network.
        # Priority 32500: Local-to-Local bypass.
        # Keep bridge internal traffic out of the VPN table 51820 to prevent "No route to host" errors.
        if ! ip rule show | grep -qE "from ${DOCKER_NETWORK_SUBNET//./\\.}[[:space:]]+to[[:space:]]+${DOCKER_NETWORK_SUBNET//./\\.}[[:space:]]+lookup[[:space:]]+main"; then
            if ! ip rule add from "${DOCKER_NETWORK_SUBNET}" to "${DOCKER_NETWORK_SUBNET}" table main pref 32500 >/dev/null 2>&1; then
                if ! ip rule show pref 32500 | grep -q "from ${DOCKER_NETWORK_SUBNET}"; then
                    LAST_ERROR="Failed to add local-to-local bypass rule for ${DOCKER_NETWORK_SUBNET}"
                    return 1
                fi
            fi
            changed=1
        fi

        if ! ip rule show | grep -qE "^[0-9]+:[[:space:]]+from[[:space:]]+${DOCKER_NETWORK_SUBNET//./\\.}[[:space:]]+lookup[[:space:]]+51820[[:space:]]*$"; then
            if ! ip rule add from "${DOCKER_NETWORK_SUBNET}" table 51820 pref 32764 >/dev/null 2>&1; then
                if ! ip rule show pref 32764 | grep -q "from ${DOCKER_NETWORK_SUBNET}"; then
                    LAST_ERROR="Failed to add policy routing rule for subnet ${DOCKER_NETWORK_SUBNET}"
                    return 1
                fi
            fi
            changed=1
        fi

        # Ensure the tunnelsats bridge gateway itself (10.9.9.1) is also routed through the tunnel
        # to prevent outbound leaks from this container during diagnostics (e.g. curl ifconfig.me)
        local bridge_gw
        bridge_gw="${DOCKER_NETWORK_SUBNET%.*}.1"
        if ! ip rule show | grep -qE "from ${bridge_gw//./\\.}[[:space:]]+lookup[[:space:]]+51820"; then
            if ! ip rule add from "${bridge_gw}" table 51820 pref 32763 >/dev/null 2>&1; then
                if ! ip rule show pref 32763 | grep -q "from ${bridge_gw}"; then
                    LAST_ERROR="Failed to add policy routing rule for bridge gateway ${bridge_gw}"
                    return 1
                fi
            fi
            changed=1
        fi
    fi

    if ! ip route replace default dev "${WG_IFACE}" metric 2 table 51820 >/dev/null 2>&1; then
        LAST_ERROR="Failed to set policy route default via ${WG_IFACE}"
        return 1
    fi

    if ! ip route replace blackhole default metric 3 table 51820 >/dev/null 2>&1; then
        LAST_ERROR="Failed to set policy route blackhole fallback"
        return 1
    fi

    # Remove legacy hardcoded route from older releases (if present).
    ip route del 10.9.0.0/24 table 51820 >/dev/null 2>&1 || true

    local wg_cidrs
    wg_cidrs="$(ip -4 addr show dev "${WG_IFACE}" | awk '/inet / {print $2}' || true)"
    if [ -z "${wg_cidrs}" ]; then
        LAST_ERROR="Failed to discover WireGuard interface addresses on ${WG_IFACE}"
        return 1
    fi

    # Mask the addresses to proper network CIDRs using python3 (e.g. 10.9.0.2/24 -> 10.9.0.0/24, or 10.9.0.100/32 -> 10.9.0.100/32)
    wg_cidrs="$(python3 -c '
import sys, ipaddress
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try:
        print(ipaddress.IPv4Network(line, strict=False))
    except (ValueError, TypeError):
        pass
' <<< "${wg_cidrs}")"

    while IFS= read -r cidr; do
        [ -n "${cidr}" ] || continue
        if ! ip route replace "${cidr}" dev "${WG_IFACE}" table 51820 >/dev/null 2>&1; then
            LAST_ERROR="Failed to set policy route for WireGuard network ${cidr}"
            return 1
        fi
    done <<EOF_WG_CIDRS
${wg_cidrs}
EOF_WG_CIDRS

    POLICY_CHANGED="${changed}"
    return 0
}

ensure_nat_forward_rules() {
    local changed=0
    NAT_CHANGED="0"
    local dnat_count
    local forward_in_count
    local forward_out_count
    local primary_dnat_missing=0
    local fallback_dnat_missing=0

    # We match the config-defined VPNPort on the tunnel interface to catch these packets.
    local internal_match_port="${FORWARDING_PORT}"

    if ! iptables -t nat -C PREROUTING -i "${WG_IFACE}" -p tcp --dport "${internal_match_port}" \
        -m comment --comment "tunnelsats-dnat" -j DNAT --to-destination "${DOCKER_TARGET_IP}:${LN_TARGET_PORT}" 2>/dev/null; then
        primary_dnat_missing=1
    fi

    if [ "${internal_match_port}" != "9735" ] && \
       ! iptables -t nat -C PREROUTING -i "${WG_IFACE}" -p tcp --dport 9735 \
        -m comment --comment "tunnelsats-dnat" -j DNAT --to-destination "${DOCKER_TARGET_IP}:${LN_TARGET_PORT}" 2>/dev/null; then
        fallback_dnat_missing=1
    fi
    
    dnat_count=$(iptables -t nat -S PREROUTING | grep -c "tunnelsats-dnat" || true)
    if [ "${dnat_count}" -lt 1 ] || [ "${primary_dnat_missing}" -eq 1 ]; then
        log INFO "Syncing DNAT rules"
        remove_tagged_iptables_rules nat PREROUTING "tunnelsats-dnat"
        if ! iptables -t nat -I PREROUTING 1 -i "${WG_IFACE}" -p tcp --dport "${internal_match_port}" \
            -m comment --comment "tunnelsats-dnat" -j DNAT --to-destination "${DOCKER_TARGET_IP}:${LN_TARGET_PORT}"; then
            LAST_ERROR="Failed to add primary DNAT rule for port ${internal_match_port}"
            return 1
        fi
        # Add fallback DNAT for port 9735 in case the VPN server translates before the tunnel
        if [ "${internal_match_port}" != "9735" ]; then
            if ! iptables -t nat -I PREROUTING 2 -i "${WG_IFACE}" -p tcp --dport 9735 \
                -m comment --comment "tunnelsats-dnat" -j DNAT --to-destination "${DOCKER_TARGET_IP}:${LN_TARGET_PORT}"; then
                LAST_ERROR="Failed to add fallback DNAT rule for port 9735"
                return 1
            fi
        fi
        changed=1
    elif [ "${internal_match_port}" != "9735" ] && [ "${fallback_dnat_missing}" -eq 1 ]; then
        log INFO "Adding fallback DNAT rule for port 9735"
        if ! iptables -t nat -I PREROUTING 2 -i "${WG_IFACE}" -p tcp --dport 9735 \
            -m comment --comment "tunnelsats-dnat" -j DNAT --to-destination "${DOCKER_TARGET_IP}:${LN_TARGET_PORT}"; then
            LAST_ERROR="Failed to add fallback DNAT rule for port 9735"
            return 1
        else
            changed=1
        fi
    fi

    if [[ "${K3S_MODE}" == "true" ]]; then
        # k3s mode: scope FORWARD rules to the target pod IP rather than accepting all
        # established connections / all WG-ingress traffic. This keeps tunnelsats off the
        # path for traffic it should not touch and minimizes blast radius.
        if ! iptables -C FORWARD -i "${WG_IFACE}" -d "${DOCKER_TARGET_IP}" \
            -m comment --comment "tunnelsats-forward-in" -j ACCEPT 2>/dev/null; then
            log INFO "Syncing FORWARD inbound rules (k3s)"
            remove_tagged_iptables_rules filter FORWARD "tunnelsats-forward-in"
            if ! iptables -I FORWARD 1 -i "${WG_IFACE}" -d "${DOCKER_TARGET_IP}" \
                -m comment --comment "tunnelsats-forward-in" -j ACCEPT; then
                LAST_ERROR="k3s: Failed to add FORWARD inbound rule"
                return 1
            fi
            changed=1
        fi

        if ! iptables -C FORWARD -s "${DOCKER_TARGET_IP}" -o "${WG_IFACE}" \
            -m comment --comment "tunnelsats-forward-out" -j ACCEPT 2>/dev/null; then
            log INFO "Syncing FORWARD outbound rules (k3s)"
            remove_tagged_iptables_rules filter FORWARD "tunnelsats-forward-out"
            if ! iptables -I FORWARD 2 -s "${DOCKER_TARGET_IP}" -o "${WG_IFACE}" \
                -m comment --comment "tunnelsats-forward-out" -j ACCEPT; then
                LAST_ERROR="k3s: Failed to add FORWARD outbound rule"
                return 1
            fi
            changed=1
        fi

        # Mangle rules for conntrack fwmark routing.
        if ! iptables -t mangle -C PREROUTING ! -i "${WG_IFACE}" -s "${DOCKER_TARGET_IP}" \
            -m comment --comment "tunnelsats-conn-restore" -j CONNMARK --restore-mark --mask 0xca6c 2>/dev/null; then
            log INFO "Syncing mangle CONNMARK restore rule (k3s)"
            remove_tagged_iptables_rules mangle PREROUTING "tunnelsats-conn-restore"
            if ! iptables -t mangle -A PREROUTING ! -i "${WG_IFACE}" -s "${DOCKER_TARGET_IP}" \
                -m comment --comment "tunnelsats-conn-restore" -j CONNMARK --restore-mark --mask 0xca6c; then
                LAST_ERROR="k3s: Failed to add CONNMARK restore-mark rule"
                return 1
            fi
            changed=1
        fi

        # Remove legacy wg-mark rule (MARK --set-mark) if it exists from a previous deployment.
        remove_tagged_iptables_rules mangle FORWARD "tunnelsats-wg-mark"

        if ! iptables -t mangle -C FORWARD -i "${WG_IFACE}" -d "${DOCKER_TARGET_IP}" \
            -m comment --comment "tunnelsats-conn-save" -j CONNMARK --set-mark 0xca6c/0xca6c 2>/dev/null; then
            log INFO "Syncing mangle CONNMARK set-mark rule (k3s)"
            remove_tagged_iptables_rules mangle FORWARD "tunnelsats-conn-save"
            if ! iptables -t mangle -A FORWARD -i "${WG_IFACE}" -d "${DOCKER_TARGET_IP}" \
                -m comment --comment "tunnelsats-conn-save" -j CONNMARK --set-mark 0xca6c/0xca6c; then
                LAST_ERROR="k3s: Failed to add CONNMARK set-mark rule"
                return 1
            fi
            changed=1
        fi
    else
        # Docker mode: bridge-interface FORWARD rules.
        if ! iptables -C FORWARD -i "${WG_IFACE}" -o "${BRIDGE_NAME}" \
            -m comment --comment "tunnelsats-forward-in" -j ACCEPT 2>/dev/null; then
            log INFO "Syncing FORWARD inbound rules"
            remove_tagged_iptables_rules filter FORWARD "tunnelsats-forward-in"
            if ! iptables -I FORWARD 1 -i "${WG_IFACE}" -o "${BRIDGE_NAME}" \
                -m comment --comment "tunnelsats-forward-in" -j ACCEPT; then
                LAST_ERROR="Failed to add FORWARD inbound rule"
                return 1
            fi
            changed=1
        fi

        if ! iptables -C FORWARD -i "${BRIDGE_NAME}" -o "${WG_IFACE}" \
            -m comment --comment "tunnelsats-forward-out" -j ACCEPT 2>/dev/null; then
            log INFO "Syncing FORWARD outbound rules"
            remove_tagged_iptables_rules filter FORWARD "tunnelsats-forward-out"
            if ! iptables -I FORWARD 2 -i "${BRIDGE_NAME}" -o "${WG_IFACE}" \
                -m comment --comment "tunnelsats-forward-out" -j ACCEPT; then
                LAST_ERROR="Failed to add FORWARD outbound rule"
                return 1
            fi
            changed=1
        fi
    fi

    NAT_CHANGED="${changed}"

    # Verify MASQUERADE positioning to ensure deterministic routing priority (Grep ID 3033104618)
    # Check if the exact rule exists at position 1 (first entry in POSTROUTING)
    if [[ "${K3S_MODE}" == "true" ]]; then
        local masq_src="${DOCKER_TARGET_IP}"
    else
        local masq_src="${DOCKER_NETWORK_SUBNET}"
    fi
    if ! iptables -t nat -S POSTROUTING 1 | grep -F "tunnelsats-masq" | grep -F -- "-s ${masq_src}" | grep -F -- "-o ${WG_IFACE}" | grep -qF -- "-j MASQUERADE"; then
        log INFO "Rule rotation: TunnelSats MASQUERADE is not at position 1. Re-positioning for ${WG_IFACE}..."

        # Deterministic cleanup before re-insertion at position 1 (Grep ID 3033104618)
        remove_tagged_iptables_rules nat POSTROUTING "tunnelsats-masq"

        if ! iptables -t nat -I POSTROUTING 1 -s "${masq_src}" -o "${WG_IFACE}" -m comment --comment "tunnelsats-masq" -j MASQUERADE; then
            LAST_ERROR="Failed to add/re-position MASQUERADE rule for ${WG_IFACE}"
            return 1
        fi
        NAT_CHANGED="1"
    else
        log INFO "TunnelSats MASQUERADE rule confirmed at position 1 (Optimal)."
    fi

    return 0
}

rules_are_synced() {
    # We match the config-defined VPNPort on the tunnel interface to catch these packets.
    local internal_match_port="${FORWARDING_PORT}"

    if [[ "${K3S_MODE}" == "true" ]]; then
        # 1. fwmark policy routing rule
        if ! ip rule show | grep -qE "fwmark 0x[cC][aA]6[cC].*lookup 51820"; then
            log WARN "rules_are_synced: k3s fwmark rule FAIL"
            return 1
        fi

        # 1b. mangle CONNMARK restore rule
        if ! iptables -t mangle -C PREROUTING ! -i "${WG_IFACE}" -s "${DOCKER_TARGET_IP}" \
            -m comment --comment "tunnelsats-conn-restore" -j CONNMARK --restore-mark --mask 0xca6c 2>/dev/null; then
            log WARN "rules_are_synced: k3s mangle conn-restore FAIL (missing or wrong form)"
            return 1
        fi

        # 1c. mangle CONNMARK set rule
        if ! iptables -t mangle -C FORWARD -i "${WG_IFACE}" -d "${DOCKER_TARGET_IP}" \
            -m comment --comment "tunnelsats-conn-save" -j CONNMARK --set-mark 0xca6c/0xca6c 2>/dev/null; then
            log WARN "rules_are_synced: k3s mangle conn-save FAIL (missing or wrong form)"
            return 1
        fi

        # 2. NAT PREROUTING check (DNAT)
        if ! iptables -t nat -C PREROUTING -i "${WG_IFACE}" -p tcp --dport "${internal_match_port}" \
            -m comment --comment "tunnelsats-dnat" -j DNAT --to-destination "${DOCKER_TARGET_IP}:${LN_TARGET_PORT}" 2>/dev/null; then
            log WARN "rules_are_synced: NAT rule FAIL"
            return 1
        fi

        # 2b. NAT PREROUTING fallback check for translated 9735 traffic (k3s)
        if [ "${internal_match_port}" != "9735" ] && ! iptables -t nat -C PREROUTING -i "${WG_IFACE}" -p tcp --dport 9735 \
            -m comment --comment "tunnelsats-dnat" -j DNAT --to-destination "${DOCKER_TARGET_IP}:${LN_TARGET_PORT}" 2>/dev/null; then
            log WARN "rules_are_synced: NAT fallback rule FAIL"
            return 1
        fi

        # 3. FORWARD Inbound check (scoped to target pod IP)
        if ! iptables -C FORWARD -i "${WG_IFACE}" -d "${DOCKER_TARGET_IP}" \
            -m comment --comment "tunnelsats-forward-in" -j ACCEPT 2>/dev/null; then
            log WARN "rules_are_synced: k3s FORWARD in FAIL"
            return 1
        fi

        # 4. FORWARD Outbound check (scoped to target pod IP)
        if ! iptables -C FORWARD -s "${DOCKER_TARGET_IP}" -o "${WG_IFACE}" \
            -m comment --comment "tunnelsats-forward-out" -j ACCEPT 2>/dev/null; then
            log WARN "rules_are_synced: k3s FORWARD out FAIL"
            return 1
        fi

        # 5. MASQUERADE check
        if ! iptables -t nat -C POSTROUTING -s "${DOCKER_TARGET_IP}" -o "${WG_IFACE}" \
            -m comment --comment "tunnelsats-masq" -j MASQUERADE 2>/dev/null; then
            log WARN "rules_are_synced: MASQUERADE rule FAIL"
            return 1
        fi

        return 0
    fi

    # Docker mode checks

    # 1. IP Rule check (Subnet routing)
    if ! ip rule show | grep -F "from ${DOCKER_NETWORK_SUBNET}" | grep -q "lookup 51820"; then
        log WARN "rules_are_synced: IP Subnet rule FAIL"
        return 1
    fi

    # 1b. IP Rule check (Bypass bridge)
    if ! ip rule show pref 32500 | grep -q "lookup main"; then
        log WARN "rules_are_synced: IP Bypass rule FAIL"
        return 1
    fi

    # 1c. IP Rule check (Bridge gateway tunnel rule)
    local bridge_gw
    bridge_gw="${DOCKER_NETWORK_SUBNET%.*}.1"
    if ! ip rule show pref 32763 | grep -q "from ${bridge_gw}"; then
        log WARN "rules_are_synced: IP Bridge-GW rule FAIL"
        return 1
    fi

    # 2. NAT PREROUTING check (DNAT)
    if ! iptables -t nat -C PREROUTING -i "${WG_IFACE}" -p tcp --dport "${internal_match_port}" \
        -m comment --comment "tunnelsats-dnat" -j DNAT --to-destination "${DOCKER_TARGET_IP}:${LN_TARGET_PORT}" 2>/dev/null; then
         log WARN "rules_are_synced: NAT rule FAIL"
         return 1
    fi

    # 2b. NAT PREROUTING fallback check for translated 9735 traffic
    if [ "${internal_match_port}" != "9735" ] && ! iptables -t nat -C PREROUTING -i "${WG_IFACE}" -p tcp --dport 9735 \
        -m comment --comment "tunnelsats-dnat" -j DNAT --to-destination "${DOCKER_TARGET_IP}:${LN_TARGET_PORT}" 2>/dev/null; then
        log WARN "rules_are_synced: NAT fallback rule FAIL"
        return 1
    fi

    # 3. FORWARD Inbound check
    if ! iptables -C FORWARD -i "${WG_IFACE}" -o "${BRIDGE_NAME}" \
        -m comment --comment "tunnelsats-forward-in" -j ACCEPT 2>/dev/null; then
        log WARN "rules_are_synced: FORWARD in FAIL"
        return 1
    fi

    # 4. FORWARD Outbound check
    if ! iptables -C FORWARD -i "${BRIDGE_NAME}" -o "${WG_IFACE}" \
        -m comment --comment "tunnelsats-forward-out" -j ACCEPT 2>/dev/null; then
        log WARN "rules_are_synced: FORWARD out FAIL"
        return 1
    fi

    # 5. MASQUERADE check
    if ! iptables -t nat -C POSTROUTING -s "${DOCKER_NETWORK_SUBNET}" -o "${WG_IFACE}" \
        -m comment --comment "tunnelsats-masq" -j MASQUERADE 2>/dev/null; then
        log WARN "rules_are_synced: MASQUERADE rule FAIL"
        return 1
    fi

    return 0
}

cleanup_dataplane() {
    log INFO "Cleaning dataplane rules"
    remove_tagged_iptables_rules nat PREROUTING "tunnelsats-dnat"
    remove_tagged_iptables_rules nat POSTROUTING "tunnelsats-masq"
    remove_tagged_iptables_rules filter FORWARD "tunnelsats-forward-in"
    remove_tagged_iptables_rules filter FORWARD "tunnelsats-forward-out"

    local max_attempts=10
    local attempt=0

    if [[ "${K3S_MODE}" == "true" ]]; then
        ip rule del fwmark 51820 table 51820 pref 32764 >/dev/null 2>&1 || true
        # Also remove any legacy IP-source rules from older deployments.
        ip rule del from "${DOCKER_TARGET_IP}" to "${DOCKER_TARGET_IP}" table main pref 32500 >/dev/null 2>&1 || true
        ip rule del from "${DOCKER_TARGET_IP}" table 51820 pref 32764 >/dev/null 2>&1 || true
        remove_tagged_iptables_rules mangle PREROUTING "tunnelsats-conn-restore"
        remove_tagged_iptables_rules mangle FORWARD "tunnelsats-wg-mark"
        remove_tagged_iptables_rules mangle FORWARD "tunnelsats-conn-save"
    else
        # Remove local bypass rule (pref 32500)
        ip rule del from "${DOCKER_NETWORK_SUBNET}" to "${DOCKER_NETWORK_SUBNET}" table main pref 32500 >/dev/null 2>&1 || true

        # Remove bridge gateway tunnel rule (pref 32763)
        local bridge_gw
        bridge_gw="${DOCKER_NETWORK_SUBNET%.*}.1"
        ip rule del from "${bridge_gw}" table 51820 pref 32763 >/dev/null 2>&1 || true

        while ip rule show | grep -qE "^[0-9]+:[[:space:]]+from[[:space:]]+${DOCKER_NETWORK_SUBNET//./\\.}[[:space:]]+lookup[[:space:]]+51820[[:space:]]*$" && [ ${attempt} -lt ${max_attempts} ]; do
            ip rule del from "${DOCKER_NETWORK_SUBNET}" table 51820 >/dev/null 2>&1 || break
            attempt=$((attempt + 1))
        done
    fi

    ip route flush table 51820 >/dev/null 2>&1 || true

    if wg show "${WG_IFACE}" >/dev/null 2>&1; then
        wg-quick down "${WG_IFACE}" >/dev/null 2>&1 || true
    fi
}

write_reconcile_result() {
    local request_id="$1"
    local changed="$2"
    local result_path
    local tmp_path
    local state_json="{}"

    if ! is_valid_request_id "${request_id}"; then
        log WARN "Skipping reconcile result write for invalid request_id: ${request_id}"
        return 0
    fi

    ensure_reconcile_dirs
    result_path="$(reconcile_result_path "${request_id}")"
    tmp_path="$(mktemp "${RECONCILE_RESULT_DIR}/.${request_id}.tmp.XXXXXX")"

    if [ -f "${STATE_FILE}" ]; then
        state_json="$(cat "${STATE_FILE}" 2>/dev/null || echo "{}")"
        if ! echo "${state_json}" | jq -e . >/dev/null 2>&1; then
            state_json="{}"
        fi
    fi

    if ! jq -n \
        --arg request_id "${request_id}" \
        --argjson changed "${changed}" \
        --argjson state "${state_json}" \
        '{request_id:$request_id, changed:$changed, state: $state}' > "${tmp_path}"; then
        rm -f "${tmp_path}"
        return 1
    fi

    mv -f "${tmp_path}" "${result_path}"
    cp -f "${result_path}" "${RECONCILE_RESULT_LEGACY}" || true
}

reconcile_once() {
    local reason="$1"
    local request_id="${2:-}"
    local changed=0
    local policy_changed="0"
    local nat_changed="0"

    LAST_ERROR=""
    RULES_SYNCED="false"

    log INFO "reconcile_start reason=${reason}"

    if [[ "${K3S_MODE}" != "true" ]] && [ ! -S "${DOCKER_SOCK}" ]; then
        LAST_ERROR="Docker socket unavailable"
        write_state
        if [ -n "${request_id}" ]; then
            write_reconcile_result "${request_id}" false
        fi
        return 1
    fi

    if ! detect_lightning_container; then
        LAST_ERROR="${LAST_ERROR:-No running LND/CLN container detected}"
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

    if ! ensure_policy_routing; then
        write_state
        if [ -n "${request_id}" ]; then
            write_reconcile_result "${request_id}" false
        fi
        return 1
    fi
    policy_changed="${POLICY_CHANGED}"

    if [ "${policy_changed}" = "1" ]; then
        changed=1
    fi

    if ! ensure_nat_forward_rules; then
        write_state
        if [ -n "${request_id}" ]; then
            write_reconcile_result "${request_id}" false
        fi
        return 1
    fi
    nat_changed="${NAT_CHANGED}"

    if [ "${nat_changed}" = "1" ]; then
        changed=1
    fi

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
    if [ -n "${API_PID}" ]; then
        kill "${API_PID}" >/dev/null 2>&1 || true
    fi
    cleanup_dataplane
    exit 0
}

main_loop() {
    while true; do
        ensure_reconcile_dirs

        if [ -f "${RESTART_TRIGGER}" ]; then
            log INFO "restart trigger detected"
            rm -f "${RESTART_TRIGGER}"
            if [ -n "${API_PID}" ]; then
                kill "${API_PID}" >/dev/null 2>&1 || true
            fi
            cleanup_dataplane
            exit 1
        fi

        if [ -f "${RECONCILE_TRIGGER_LEGACY}" ]; then
            local legacy_req
            legacy_req=$(cat "${RECONCILE_TRIGGER_LEGACY}" 2>/dev/null || true)
            rm -f "${RECONCILE_TRIGGER_LEGACY}"
            if is_valid_request_id "${legacy_req}"; then
                reconcile_once "manual" "${legacy_req}" || true
            else
                log WARN "Ignoring legacy reconcile trigger with invalid request id"
            fi
        fi

        local trigger_path
        local req
        while IFS= read -r trigger_path; do
            [ -n "${trigger_path}" ] || continue
            req="$(basename "${trigger_path}" .trigger)"
            rm -f "${trigger_path}"
            if is_valid_request_id "${req}"; then
                reconcile_once "manual" "${req}" || true
            else
                log WARN "Ignoring reconcile trigger with invalid request id: ${req}"
            fi
        done < <(find "${RECONCILE_TRIGGER_DIR}" -maxdepth 1 -type f -name '*.trigger' | sort)

        local now
        now=$(date +%s)
        if [ $((now - LAST_RECONCILE_EPOCH)) -ge ${RECONCILE_INTERVAL} ]; then
            reconcile_once "periodic" || true
        fi

        sleep 2
    done
}

trap cleanup SIGTERM SIGINT

echo "Starting Tunnelsats v3 (mode: $([[ "${K3S_MODE}" == "true" ]] && echo "k3s" || echo "umbrel"))..."
log INFO "Starting internal dashboard server on port 9739"
python3 /app/server/app.py &
API_PID=$!

# Zero-Loss Migration: Safeguard existing users moving to persistent data mounts (Grep ID 3033104615)
if ! ls /data/tunnelsats*.conf >/dev/null 2>&1 && ls /migration_source/tunnelsats*.conf >/dev/null 2>&1; then
    log INFO "Legacy configuration detected in migration_source. Promoting to persistent /data mount..."

    migrated_configs=0
    while IFS= read -r -d '' legacy_cfg; do
        if cp -pn "${legacy_cfg}" /data/ 2>/dev/null; then
            migrated_configs=$((migrated_configs + 1))
        fi
    done < <(find /migration_source -maxdepth 1 -type f -name 'tunnelsats*.conf' -print0 2>/dev/null || true)

    while IFS= read -r -d '' legacy_bak; do
        cp -pn "${legacy_bak}" /data/ 2>/dev/null || true
    done < <(find /migration_source -maxdepth 1 -type f -name 'tunnelsats*.bak*' -print0 2>/dev/null || true)

    log INFO "Migration complete. Promoted ${migrated_configs} config file(s) to persistent storage."
fi

ensure_reconcile_dirs

reconcile_once "startup" || true

echo "Tunnelsats container running. UI available on port 9739."
main_loop
