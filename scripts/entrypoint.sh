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
DECOMMISSION_TRIGGER_DIR="/tmp/tunnelsats_decommission_trigger.d"
DECOMMISSION_RESULT_DIR="/tmp/tunnelsats_decommission_result.d"
RESTART_TRIGGER="/tmp/tunnelsats_restart_trigger"
TUNNELSATS_META_PATH="${TUNNELSATS_META_PATH:-/data/tunnelsats-meta.json}"
DOCKER_NETWORK_NAME="docker-tunnelsats"
DOCKER_NETWORK_SUBNET="10.9.9.0/25"
DOCKER_TARGET_IP="10.9.9.9"
DOCKER_MIN_GW_PRIORITY=100
DOCKER_ROUTE_PROBE_DESTINATIONS=("1.1.1.1" "8.8.8.8")
DOCKER_POLICY_STATE_FILE="${DOCKER_POLICY_STATE_FILE:-/data/tunnelsats-docker-policy-sources.json}"
DOCKER_TARGET_STATE_FILE="${DOCKER_TARGET_STATE_FILE:-/data/tunnelsats-docker-target.json}"
IPV6_POLICY_STATE_FILE="${IPV6_POLICY_STATE_FILE:-/data/tunnelsats-ipv6-policy-sources.json}"
K3S_POLICY_STATE_FILE="${K3S_POLICY_STATE_FILE:-/data/tunnelsats-k3s-policy-state.json}"
DECOMMISSION_STATE_FILE="${DECOMMISSION_STATE_FILE:-/data/tunnelsats-decommissioned.json}"
IPV6_POLICY_TABLE="${IPV6_POLICY_TABLE:-51821}"
IPV6_TUNNEL_RULE_PREF="${IPV6_TUNNEL_RULE_PREF:-32764}"
IPV6_BLACKHOLE_RULE_PREF="${IPV6_BLACKHOLE_RULE_PREF:-32765}"
IPV6_BLACKHOLE_METRIC="${IPV6_BLACKHOLE_METRIC:-42760}"
IPV6_RULE_MARKER="tunnelsats-ipv6-deny"
K3S_IPV6_QUARANTINE_MARKER="tunnelsats-k3s-ipv6-quarantine"
K3S_NON_TARGET_RULE_MARKER="tunnelsats-k3s-non-target"
K3S_NON_TARGET_FWMARK="0xca6d"
K3S_NON_TARGET_FWMARK_MASK="0xffffffff"
LN_TARGET_PORT="9735" # Default to LND, will be updated in detect_lightning_container
RECONCILE_INTERVAL=30
KILL_SWITCH_PREF=32762
WG_HANDSHAKE_MAX_AGE=180

# k3s mode: set K3S_MODE=true to bypass Docker networking and use Kubernetes Services instead.
# These are explicitly exported so the Python dashboard (launched as a child process below)
# always sees them, even if a future caller passes them as plain shell vars instead of env.
export K3S_MODE="${K3S_MODE:-false}"
export SECURE_MODE="${SECURE_MODE:-false}"
export TUNNELSATS_ROLE="${TUNNELSATS_ROLE:-combined}"
case "${TUNNELSATS_ROLE}" in
    combined|web|daemon) ;;
    *)
        printf 'Invalid TUNNELSATS_ROLE: %s\n' "${TUNNELSATS_ROLE}" >&2
        exit 1
        ;;
esac
if [[ "${K3S_MODE}" == "true" ]]; then
    export DASHBOARD_BIND_HOST="${DASHBOARD_BIND_HOST:-0.0.0.0}"
    export DASHBOARD_BIND_PORT="${DASHBOARD_BIND_PORT:-9739}"
else
    # Umbrel app_proxy owns the public port. Keep the privileged backend on
    # loopback even when a hot-patch or manual container start omits Compose
    # environment values.
    export DASHBOARD_BIND_HOST="${DASHBOARD_BIND_HOST:-127.0.0.1}"
    export DASHBOARD_BIND_PORT="${DASHBOARD_BIND_PORT:-9740}"
fi
export LND_K8S_SERVICE="${LND_K8S_SERVICE:-}"
export CLN_K8S_SERVICE="${CLN_K8S_SERVICE:-}"
export K8S_NAMESPACE="${K8S_NAMESPACE:-default}"
# Namespace where LND/CLN live — defaults to the same namespace as tunnelsats,
# but must be set explicitly when they run in a different namespace.
export LND_K8S_NAMESPACE="${LND_K8S_NAMESPACE:-${K8S_NAMESPACE}}"
export CLN_K8S_NAMESPACE="${CLN_K8S_NAMESPACE:-${K8S_NAMESPACE}}"
export LND_K8S_POD_SELECTOR="${LND_K8S_POD_SELECTOR:-app=lnd}"
export CLN_K8S_POD_SELECTOR="${CLN_K8S_POD_SELECTOR:-app=cln}"
export TUNNELSATS_K8S_NODE_NAME="${TUNNELSATS_K8S_NODE_NAME:-}"
# Destinations that must remain inside the cluster instead of using WireGuard.
# These are the default k3s pod/service CIDRs; custom clusters must override
# this with their exact internal CIDRs.
export K3S_BYPASS_CIDRS="${K3S_BYPASS_CIDRS:-10.42.0.0/16,10.43.0.0/16}"
export K3S_POD_CIDRS="${K3S_POD_CIDRS:-}"
export K3S_IPV6_BYPASS_CIDRS="${K3S_IPV6_BYPASS_CIDRS:-}"
export K3S_IPV6_POD_CIDRS="${K3S_IPV6_POD_CIDRS:-}"
export K3S_CNI_ENFORCES_NETWORK_POLICY="${K3S_CNI_ENFORCES_NETWORK_POLICY:-false}"
# Persist a compound, boot-scoped policy-rule owner tag. Linux exposes only an
# 8-bit protocol field, so ownership also includes a randomly allocated block
# of four rule preferences. A component that later reuses our protocol cannot
# have an unrelated rule claimed during reconciliation or cleanup.
K3S_RULE_PROTOCOL_FILE="${K3S_RULE_PROTOCOL_FILE:-/data/tunnelsats-k3s-rule-protocol}"
K3S_LEGACY_RULE_PROTOCOL=""
initialize_k3s_rule_protocol() {
    local configured_protocol="${K3S_RULE_PROTOCOL:-}"
    local configured_pref_base="${K3S_RULE_PREF_BASE:-}"
    local current_boot=""
    local protocol_dir
    local protocol_tmp
    local saved_boot=""
    local saved_protocol=""
    local saved_pref_base=""
    local saved_legacy_protocol=""
    local used_protocols
    local used_priorities

    if [ -z "${configured_protocol}" ] && [ "${K3S_MODE}" = "true" ]; then
        current_boot="$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || true)"
        [ -n "${current_boot}" ] || return 1
        if [ -f "${K3S_RULE_PROTOCOL_FILE}" ]; then
            read -r saved_boot saved_protocol saved_pref_base saved_legacy_protocol < "${K3S_RULE_PROTOCOL_FILE}" || true
            if [ "${saved_boot}" = "${current_boot}" ] && [ -n "${saved_pref_base}" ]; then
                configured_protocol="${saved_protocol}"
                configured_pref_base="${saved_pref_base}"
                K3S_LEGACY_RULE_PROTOCOL="${saved_legacy_protocol}"
            elif [ "${saved_boot}" = "${current_boot}" ] && [[ "${saved_protocol}" =~ ^[0-9]+$ ]]; then
                # Upgrade from the protocol-only owner record. Keep the former
                # protocol solely for one-time legacy quarantine cleanup while
                # allocating a fresh compound owner below.
                K3S_LEGACY_RULE_PROTOCOL="${saved_protocol}"
            fi
        fi
    fi

    if [ -z "${configured_protocol}" ] && [ "${K3S_MODE}" = "true" ]; then
        used_protocols="$(ip -N rule show 2>/dev/null | awk '
            { for (i = 1; i < NF; i++) if ($i == "proto" || $i == "protocol") print $(i + 1) }
        ' | paste -sd, - || true)"
        configured_protocol="$(USED_PROTOCOLS="${used_protocols}" python3 -c '
import os
import secrets

used = {
    int(value)
    for value in os.environ.get("USED_PROTOCOLS", "").split(",")
    if value.isdigit()
}
candidates = [value for value in range(100, 253) if value not in used]
if not candidates:
    raise SystemExit("no unused routing protocol is available")
print(secrets.choice(candidates))
' 2>/dev/null)" || return 1

        used_priorities="$(ip rule show 2>/dev/null | awk -F: '/^[0-9]+:/ {gsub(/[[:space:]]/, "", $1); print $1}' | paste -sd, - || true)"
        configured_pref_base="$(USED_PRIORITIES="${used_priorities}" python3 -c '
import os
import secrets

used = {
    int(value)
    for value in os.environ.get("USED_PRIORITIES", "").split(",")
    if value.isdigit()
}
offsets = (0, 263, 264, 265)
for _ in range(4096):
    # All four rules must run before the built-in main-table rule at 32766.
    base = 1000 + secrets.randbelow(31500)
    if all(base + offset not in used for offset in offsets):
        print(base)
        break
else:
    raise SystemExit("no unused routing preference block is available")
' 2>/dev/null)" || return 1

        protocol_dir="${K3S_RULE_PROTOCOL_FILE%/*}"
        [ "${protocol_dir}" != "${K3S_RULE_PROTOCOL_FILE}" ] || protocol_dir="."
        mkdir -p "${protocol_dir}" || return 1
        protocol_tmp="$(mktemp "${K3S_RULE_PROTOCOL_FILE}.tmp.XXXXXX")" || return 1
        if ! printf '%s %s %s %s\n' "${current_boot}" "${configured_protocol}" "${configured_pref_base}" "${K3S_LEGACY_RULE_PROTOCOL}" > "${protocol_tmp}" || \
           ! chmod 600 "${protocol_tmp}" || \
           ! mv -f "${protocol_tmp}" "${K3S_RULE_PROTOCOL_FILE}"; then
            rm -f "${protocol_tmp}"
            return 1
        fi
    fi

    # Docker/Secure Mode do not use protocol ownership, but retaining the old
    # value there keeps those code paths deterministic without touching /data.
    configured_protocol="${configured_protocol:-200}"
    configured_pref_base="${configured_pref_base:-32500}"
    [[ "${configured_protocol}" =~ ^[0-9]+$ ]] || return 1
    [ "${configured_protocol}" -ge 1 ] && [ "${configured_protocol}" -le 252 ] || return 1
    if [ -n "${K3S_LEGACY_RULE_PROTOCOL}" ]; then
        [[ "${K3S_LEGACY_RULE_PROTOCOL}" =~ ^[0-9]+$ ]] || return 1
        [ "${K3S_LEGACY_RULE_PROTOCOL}" -ge 1 ] && [ "${K3S_LEGACY_RULE_PROTOCOL}" -le 252 ] || return 1
    fi
    [[ "${configured_pref_base}" =~ ^[0-9]+$ ]] || return 1
    [ "${configured_pref_base}" -ge 1 ] && [ "${configured_pref_base}" -le 32500 ] || return 1
    K3S_RULE_PROTOCOL="${configured_protocol}"
    K3S_RULE_PREF_BASE="${configured_pref_base}"
    K3S_BYPASS_RULE_PREF="${configured_pref_base}"
    K3S_QUARANTINE_RULE_PREF="$((configured_pref_base + 263))"
    K3S_TUNNEL_RULE_PREF="$((configured_pref_base + 264))"
    K3S_BLACKHOLE_RULE_PREF="$((configured_pref_base + 265))"
}

clear_k3s_legacy_rule_protocol() {
    local current_boot
    local protocol_dir
    local protocol_tmp

    [ -n "${K3S_LEGACY_RULE_PROTOCOL}" ] || return 0
    current_boot="$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || true)"
    [ -n "${current_boot}" ] || return 1
    protocol_dir="${K3S_RULE_PROTOCOL_FILE%/*}"
    [ "${protocol_dir}" != "${K3S_RULE_PROTOCOL_FILE}" ] || protocol_dir="."
    mkdir -p "${protocol_dir}" || return 1
    protocol_tmp="$(mktemp "${K3S_RULE_PROTOCOL_FILE}.tmp.XXXXXX")" || return 1
    if ! printf '%s %s %s\n' "${current_boot}" "${K3S_RULE_PROTOCOL}" "${K3S_RULE_PREF_BASE}" > "${protocol_tmp}" || \
       ! chmod 600 "${protocol_tmp}" || \
       ! mv -f "${protocol_tmp}" "${K3S_RULE_PROTOCOL_FILE}"; then
        rm -f "${protocol_tmp}"
        return 1
    fi
    K3S_LEGACY_RULE_PROTOCOL=""
}

if ! initialize_k3s_rule_protocol; then
    printf '%s\n' "Failed to initialize the k3s policy-rule ownership protocol" >&2
    exit 1
fi
# Emergency CIDR quarantine remains distinct from the persistent per-pod
# fallback. This matters when a configured pod CIDR is itself a /32.
K8S_SA_TOKEN_PATH="/var/run/secrets/kubernetes.io/serviceaccount/token"
K8S_SA_CA_PATH="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
K8S_API_URL="https://kubernetes.default.svc"
K3S_ISOLATION_RETRY_INTERVAL="${K3S_ISOLATION_RETRY_INTERVAL:-2}"
K3S_SHUTDOWN_NETPOL_ATTEMPTS="${K3S_SHUTDOWN_NETPOL_ATTEMPTS:-3}"
K3S_SHUTDOWN_RETRY_INTERVAL="${K3S_SHUTDOWN_RETRY_INTERVAL:-1}"
K3S_DNS_PROXY_HOST="${K3S_DNS_PROXY_HOST:-0.0.0.0}"
K3S_DNS_PROXY_PORT="${K3S_DNS_PROXY_PORT:-15353}"
K3S_DNS_PROXY_PID_FILE="${K3S_DNS_PROXY_PID_FILE:-/run/tunnelsats-k3s-dns-proxy.pid}"
K3S_DNS_PROXY_LOG_FILE="${K3S_DNS_PROXY_LOG_FILE:-/tmp/tunnelsats-k3s-dns-proxy.log}"
K3S_DNS_UPSTREAM_FALLBACK="${K3S_DNS_UPSTREAM_FALLBACK:-1.1.1.1,9.9.9.9}"
K3S_DNS_PROXY_SCRIPT="${K3S_DNS_PROXY_SCRIPT:-/app/server/k3s_dns_proxy.py}"
if [ ! -f "${K3S_DNS_PROXY_SCRIPT}" ] && [ -f "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)/server/k3s_dns_proxy.py" ]; then
    K3S_DNS_PROXY_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)/server/k3s_dns_proxy.py"
fi
K3S_DNS_PROXY_PID=""

API_PID=""
LAST_RECONCILE_EPOCH=0

TARGET_CONTAINER_ID=""
TARGET_CONTAINER_NAME=""
TARGET_IMPL=""
# Standard Mode: the implementation (lnd/cln) and container that the last
# successful reconcile protected, persisted in DOCKER_TARGET_STATE_FILE.
PROTECTED_TARGET_IMPL=""
PROTECTED_TARGET_CONTAINER=""
PROTECTED_TARGET_STATE_LOADED="false"
PROTECTED_TARGET_PERSISTED="false"
DOCKER_API_BODY=""
DOCKER_API_CODE=""
DOCKER_API_ERROR=""
TARGET_IPV4_ENDPOINTS=()
MANAGED_DOCKER_IPV4_ENDPOINTS=()
TARGET_IPV6_ADDRESSES=()
TARGET_IPV6_MACS=()
MANAGED_TARGET_IPV6_ADDRESSES=()
IPV6_POLICY_STATE_LOADED="false"
IPV6_POLICY_TABLE_MANAGED="false"
TARGET_HAS_IPV6_DEFAULT_ROUTE="false"
DOCKER_GW_PRIORITY_SUPPORTED="unknown"
DOCKER_POLICY_STATE_LOADED="false"
FORWARDING_PORT=""
BRIDGE_NAME=""
K3S_TARGET_POD_NAME=""
K3S_TARGET_POD_NAMESPACE=""
K3S_TARGET_POD_SELECTOR=""
K3S_POLICY_STATE_LOADED="false"
MANAGED_K3S_TARGET_IP=""
MANAGED_K3S_TARGET_IPV6_ADDRESSES=()
MANAGED_K3S_TARGET_IMPL=""
MANAGED_K3S_POD_NAME=""
MANAGED_K3S_POD_NAMESPACE=""
MANAGED_K3S_POD_SELECTOR=""
RULES_SYNCED="false"
IPV4_RULES_SYNCED="false"
IPV6_RULES_SYNCED="false"
DNS_PROTECTED="false"
DECOMMISSIONED="${DECOMMISSIONED:-false}"
DECOMMISSION_STATE_LOADED="false"
LAST_ERROR=""
POLICY_CHANGED="0"
NAT_CHANGED="0"
K3S_DNS_CHANGED="0"
K3S_BYPASS_CIDRS_NORMALIZED=""
K3S_IPV6_QUARANTINE_CIDR=""
K3S_POD_API_QUERY_OK="false"
K3S_CANDIDATE_POD_IPS=""
K3S_CANDIDATE_POD_IPV6_ADDRESSES=""
K3S_ALL_MATCHING_LOCAL_POD_IPS=""
K3S_WATCHED_CANDIDATE_POD_IPS=""
K3S_WATCHED_CANDIDATE_POD_IPV6_ADDRESSES=""
K3S_KNOWN_NON_TARGET_POD_IPS=""
K3S_KNOWN_NON_TARGET_POD_IPV6_ADDRESSES=""
K3S_KNOWN_NON_TARGET_POD_V6_PEERS=""
K3S_ALLOW_ACTIVE_QUARANTINE_DURING_SYNC="false"

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

ensure_decommission_dirs() {
    mkdir -p "${DECOMMISSION_TRIGGER_DIR}" "${DECOMMISSION_RESULT_DIR}"
}

reconcile_result_path() {
    local request_id="$1"
    printf '%s/%s.json' "${RECONCILE_RESULT_DIR}" "${request_id}"
}

decommission_result_path() {
    local request_id="$1"
    printf '%s/%s.json' "${DECOMMISSION_RESULT_DIR}" "${request_id}"
}

write_state() {
    local tmp
    tmp="$(mktemp "${STATE_FILE}.tmp.XXXXXX")"

    local dataplane_mode
    if [[ "${K3S_MODE}" == "true" ]]; then
        dataplane_mode="k3s"
    elif [[ "${SECURE_MODE}" == "true" ]]; then
        dataplane_mode="secure-mode"
    else
        dataplane_mode="docker-full-parity"
    fi

    local decommissioned_json
    if [ "${DECOMMISSIONED:-false}" = "true" ]; then
        decommissioned_json="true"
    else
        decommissioned_json="false"
    fi

    local dns_protected_json="false"
    if [[ "${K3S_MODE}" == "true" ]] && [ "${DNS_PROTECTED:-false}" = "true" ]; then
        dns_protected_json="true"
    fi

    local target_ipv6_addresses_json
    target_ipv6_addresses_json=$(printf '%s\n' "${TARGET_IPV6_ADDRESSES[@]}" \
        | jq -Rsc 'split("\n") | map(select(length > 0))') || {
        rm -f "${tmp}"
        return 1
    }

    if jq -n \
        --arg dataplane_mode "${dataplane_mode}" \
        --arg target_container "${TARGET_CONTAINER_NAME:-}" \
        --arg target_ip "${DOCKER_TARGET_IP:-}" \
        --arg target_impl "${TARGET_IMPL:-}" \
        --arg forwarding_port "${FORWARDING_PORT:-}" \
        --argjson rules_synced "${RULES_SYNCED}" \
        --argjson ipv4_rules_synced "${IPV4_RULES_SYNCED}" \
        --argjson ipv6_rules_synced "${IPV6_RULES_SYNCED}" \
        --argjson dns_protected "${dns_protected_json}" \
        --argjson decommissioned "${decommissioned_json}" \
        --arg ipv6_policy "deny" \
        --argjson target_ipv6_addresses "${target_ipv6_addresses_json}" \
        --argjson target_ipv6_default_route "${TARGET_HAS_IPV6_DEFAULT_ROUTE}" \
        --arg last_error "${LAST_ERROR:-}" \
        --arg docker_network_name "${DOCKER_NETWORK_NAME}" \
        --arg docker_network_subnet "${DOCKER_NETWORK_SUBNET}" \
        --arg bridge_name "${BRIDGE_NAME:-}" \
        --arg k3s_bypass_cidrs "${K3S_BYPASS_CIDRS_NORMALIZED:-}" \
        --arg last_reconcile_at "$(date -u +%FT%TZ)" \
        '{
            dataplane_mode: $dataplane_mode,
            target_container: $target_container,
            target_ip: $target_ip,
            target_impl: $target_impl,
            forwarding_port: $forwarding_port,
            rules_synced: $rules_synced,
            ipv4_rules_synced: $ipv4_rules_synced,
            ipv6_rules_synced: $ipv6_rules_synced,
            dns_protected: $dns_protected,
            decommissioned: $decommissioned,
            ipv6_policy: $ipv6_policy,
            target_ipv6_addresses: $target_ipv6_addresses,
            target_ipv6_default_route: $target_ipv6_default_route,
            k3s_bypass_cidrs: (
                if $k3s_bypass_cidrs == ""
                then []
                else ($k3s_bypass_cidrs | split(","))
                end
            ),
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

# Docker API call that keeps Docker's answer. Sets DOCKER_API_BODY,
# DOCKER_API_CODE and, on failure, DOCKER_API_ERROR (Docker's message or the
# HTTP status). Succeeds only on HTTP 2xx.
docker_api_request() {
    local response
    response=$(docker_api_with_code "$@") || true
    DOCKER_API_BODY="${response%HTTPSTATUS:*}"
    DOCKER_API_CODE="${response##*HTTPSTATUS:}"
    if [[ ! "${DOCKER_API_CODE}" =~ ^[0-9]{3}$ ]]; then
        DOCKER_API_CODE="000"
    fi
    DOCKER_API_ERROR=""
    if [[ "${DOCKER_API_CODE}" =~ ^2[0-9]{2}$ ]]; then
        return 0
    fi
    DOCKER_API_ERROR=$(printf '%s' "${DOCKER_API_BODY}" | jq -r '.message // empty' 2>/dev/null || true)
    DOCKER_API_ERROR="${DOCKER_API_ERROR//$'\n'/ }"
    if [ -z "${DOCKER_API_ERROR}" ]; then
        DOCKER_API_ERROR="HTTP ${DOCKER_API_CODE}"
    fi
    return 1
}

# A disconnect that Docker rejects because the endpoint is already gone.
docker_api_error_is_not_connected() {
    { [ "${DOCKER_API_CODE}" = "404" ] || [ "${DOCKER_API_CODE}" = "500" ]; } && \
        [[ "${DOCKER_API_ERROR}" == *"is not connected"* ]]
}

detect_docker_gw_priority_support() {
    [[ "${DOCKER_GW_PRIORITY_SUPPORTED}" != "unknown" ]] && return 0

    local version
    local api_version
    local major
    local minor
    version=$(docker_api "GET" "/version") || {
        LAST_ERROR="Unable to inspect Docker Engine API version"
        return 1
    }
    api_version=$(printf '%s' "${version}" | jq -r '.ApiVersion // empty')
    if [[ ! "${api_version}" =~ ^([0-9]+)\.([0-9]+)$ ]]; then
        LAST_ERROR="Docker Engine returned an invalid API version: ${api_version:-missing}"
        return 1
    fi

    major="${BASH_REMATCH[1]}"
    minor="${BASH_REMATCH[2]}"
    if [ "${major}" -gt 1 ] || { [ "${major}" -eq 1 ] && [ "${minor}" -ge 48 ]; }; then
        DOCKER_GW_PRIORITY_SUPPORTED="true"
    else
        DOCKER_GW_PRIORITY_SUPPORTED="false"
        log WARN "Docker Engine API ${api_version} does not support GwPriority; route verification remains mandatory"
    fi
}

load_managed_docker_policy_state() {
    [ "${DOCKER_POLICY_STATE_LOADED}" = "false" ] || return 0

    local current_boot
    local state
    local saved_boot
    local parsed
    local entry
    current_boot=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null) || {
        LAST_ERROR="Unable to identify the current boot for Docker policy ownership"
        return 1
    }
    [ -n "${current_boot}" ] || {
        LAST_ERROR="Current boot ID is empty; refusing to load Docker policy ownership"
        return 1
    }

    if [ ! -e "${DOCKER_POLICY_STATE_FILE}" ]; then
        DOCKER_POLICY_STATE_LOADED="true"
        return 0
    fi
    state=$(cat "${DOCKER_POLICY_STATE_FILE}" 2>/dev/null) || {
        LAST_ERROR="Failed to read Docker policy ownership state"
        return 1
    }
    if ! printf '%s' "${state}" | jq -e '
        .version == 1 and
        (.boot_id | type == "string") and
        (.endpoints | type == "array") and
        all(.endpoints[]; type == "string")
    ' >/dev/null 2>&1; then
        LAST_ERROR="Docker policy ownership state is malformed"
        return 1
    fi
    saved_boot=$(printf '%s' "${state}" | jq -r '.boot_id')
    if [ "${saved_boot}" != "${current_boot}" ]; then
        # Policy rules do not survive a host reboot. Never claim a prior
        # boot's source addresses as rules owned by this process.
        MANAGED_DOCKER_IPV4_ENDPOINTS=()
        DOCKER_POLICY_STATE_LOADED="true"
        return 0
    fi

    if ! parsed=$(printf '%s' "${state}" | jq -r '.endpoints[]' | python3 -c '
import ipaddress
import sys

for raw in sys.stdin:
    raw = raw.rstrip("\n")
    parts = raw.split("|")
    if len(parts) != 5:
        raise SystemExit("invalid endpoint field count")
    address, subnet, gateway, is_tunnel, priority = parts
    address = str(ipaddress.IPv4Address(address))
    network = ipaddress.IPv4Network(subnet, strict=True)
    if ipaddress.IPv4Address(address) not in network:
        raise SystemExit("endpoint source is outside its subnet")
    if gateway:
        gateway = str(ipaddress.IPv4Address(gateway))
    if is_tunnel not in {"0", "1"}:
        raise SystemExit("invalid tunnel marker")
    priority = str(int(priority))
    print(f"{address}|{network}|{gateway}|{is_tunnel}|{priority}")
'); then
        LAST_ERROR="Docker policy ownership endpoints are malformed"
        return 1
    fi

    MANAGED_DOCKER_IPV4_ENDPOINTS=()
    while IFS= read -r entry; do
        [ -n "${entry}" ] || continue
        MANAGED_DOCKER_IPV4_ENDPOINTS+=("${entry}")
    done <<< "${parsed}"
    DOCKER_POLICY_STATE_LOADED="true"
}

persist_managed_docker_policy_state() {
    local current_boot
    local state_dir
    local state_tmp
    local endpoints_json
    current_boot=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null) || {
        LAST_ERROR="Unable to identify the current boot for Docker policy ownership"
        return 1
    }
    [ -n "${current_boot}" ] || {
        LAST_ERROR="Current boot ID is empty; refusing to persist Docker policy ownership"
        return 1
    }

    state_dir="${DOCKER_POLICY_STATE_FILE%/*}"
    [ "${state_dir}" != "${DOCKER_POLICY_STATE_FILE}" ] || state_dir="."
    if ! mkdir -p "${state_dir}"; then
        LAST_ERROR="Failed to create Docker policy ownership directory"
        return 1
    fi
    state_tmp=$(mktemp "${DOCKER_POLICY_STATE_FILE}.tmp.XXXXXX") || {
        LAST_ERROR="Failed to create temporary Docker policy ownership state"
        return 1
    }
    endpoints_json=$(printf '%s\n' "${MANAGED_DOCKER_IPV4_ENDPOINTS[@]}" \
        | jq -Rsc 'split("\n") | map(select(length > 0))') || {
        rm -f "${state_tmp}"
        LAST_ERROR="Failed to encode Docker policy ownership endpoints"
        return 1
    }
    if ! jq -n \
        --arg boot_id "${current_boot}" \
        --argjson endpoints "${endpoints_json}" \
        '{version:1, boot_id:$boot_id, endpoints:$endpoints}' > "${state_tmp}" || \
       ! chmod 600 "${state_tmp}" || \
       ! mv -f "${state_tmp}" "${DOCKER_POLICY_STATE_FILE}"; then
        rm -f "${state_tmp}"
        LAST_ERROR="Failed to persist Docker policy ownership state"
        return 1
    fi
}

lightning_impl_label() {
    if [ "${1:-}" = "cln" ]; then
        printf '%s' "Core Lightning"
    else
        printf '%s' "LND"
    fi
}

# Without a selected node type, Standard Mode keeps protecting the
# implementation of the last successful reconcile. The record survives daemon
# restarts and reboots. A malformed record fails closed until the user selects
# a node type, which takes precedence and rewrites the record.
load_protected_target_state() {
    [ "${PROTECTED_TARGET_STATE_LOADED}" = "false" ] || return 0

    local state
    local target_impl
    local target_container
    local malformed_error="Protected node state is malformed; select the node type under Configure Lightning Node"
    if [ ! -e "${DOCKER_TARGET_STATE_FILE}" ]; then
        PROTECTED_TARGET_STATE_LOADED="true"
        return 0
    fi
    state=$(cat "${DOCKER_TARGET_STATE_FILE}" 2>/dev/null) || {
        LAST_ERROR="Failed to read the protected node state"
        return 1
    }
    if ! printf '%s' "${state}" | jq -e '
        .version == 1 and
        (.target_impl == "lnd" or .target_impl == "cln") and
        (.target_container | type == "string")
    ' >/dev/null 2>&1; then
        LAST_ERROR="${malformed_error}"
        return 1
    fi
    target_impl=$(printf '%s' "${state}" | jq -r '.target_impl')
    target_container=$(printf '%s' "${state}" | jq -r '.target_container')
    if [ -n "${target_container}" ] && [[ ! "${target_container}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; then
        LAST_ERROR="${malformed_error}"
        return 1
    fi
    PROTECTED_TARGET_IMPL="${target_impl}"
    PROTECTED_TARGET_CONTAINER="${target_container}"
    PROTECTED_TARGET_STATE_LOADED="true"
    PROTECTED_TARGET_PERSISTED="true"
}

persist_protected_target_state() {
    local state_dir
    local state_tmp

    state_dir="${DOCKER_TARGET_STATE_FILE%/*}"
    [ "${state_dir}" != "${DOCKER_TARGET_STATE_FILE}" ] || state_dir="."
    if ! mkdir -p "${state_dir}"; then
        LAST_ERROR="Failed to create the protected node state directory"
        return 1
    fi
    state_tmp=$(mktemp "${DOCKER_TARGET_STATE_FILE}.tmp.XXXXXX") || {
        LAST_ERROR="Failed to create temporary protected node state"
        return 1
    }
    if ! jq -n \
        --arg target_impl "${PROTECTED_TARGET_IMPL}" \
        --arg target_container "${PROTECTED_TARGET_CONTAINER}" \
        '{version:1, target_impl:$target_impl, target_container:$target_container}' > "${state_tmp}" || \
       ! chmod 600 "${state_tmp}" || \
       ! mv -f "${state_tmp}" "${DOCKER_TARGET_STATE_FILE}"; then
        rm -f "${state_tmp}"
        LAST_ERROR="Failed to persist the protected node state"
        return 1
    fi
}

# Called after a fully verified Standard Mode reconcile. A failed write keeps
# the record in memory, logs a warning and is retried after the next success.
record_protected_target() {
    [[ "${K3S_MODE}" == "true" ]] && return 0
    [[ "${SECURE_MODE}" == "true" ]] && return 0
    case "${TARGET_IMPL}" in
        lnd|cln) ;;
        *) return 0 ;;
    esac
    if [ "${PROTECTED_TARGET_PERSISTED}" = "true" ] && \
       [ "${PROTECTED_TARGET_IMPL}" = "${TARGET_IMPL}" ] && \
       [ "${PROTECTED_TARGET_CONTAINER}" = "${TARGET_CONTAINER_NAME}" ]; then
        return 0
    fi

    local saved_error="${LAST_ERROR}"
    PROTECTED_TARGET_IMPL="${TARGET_IMPL}"
    PROTECTED_TARGET_CONTAINER="${TARGET_CONTAINER_NAME}"
    PROTECTED_TARGET_STATE_LOADED="true"
    if persist_protected_target_state; then
        PROTECTED_TARGET_PERSISTED="true"
        log INFO "Protected node recorded: $(lightning_impl_label "${TARGET_IMPL}") (${TARGET_CONTAINER_NAME})"
    else
        PROTECTED_TARGET_PERSISTED="false"
        log WARN "${LAST_ERROR}; keeping $(lightning_impl_label "${TARGET_IMPL}") as the protected node in memory"
    fi
    LAST_ERROR="${saved_error}"
}

load_managed_ipv6_policy_state() {
    local current_boot state saved_boot parsed address
    if [ "${IPV6_POLICY_STATE_LOADED}" != "false" ]; then
        [ -e "${IPV6_POLICY_STATE_FILE}" ] || return 0
        current_boot=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || true)
        [ -n "${current_boot}" ] || return 0
        state=$(cat "${IPV6_POLICY_STATE_FILE}" 2>/dev/null || true)
        [ -n "${state}" ] || return 0
        if printf '%s' "${state}" | jq -e '
            .version == 1 and
            (.boot_id | type == "string") and
            (.table_managed | type == "boolean") and
            (.addresses | type == "array") and
            all(.addresses[]; type == "string")
        ' >/dev/null 2>&1; then
            saved_boot=$(printf '%s' "${state}" | jq -r '.boot_id')
            if [ "${saved_boot}" = "${current_boot}" ]; then
                parsed=$(printf '%s' "${state}" | jq -r '.addresses[]' | python3 -c '
import ipaddress
import sys

for raw in sys.stdin:
    address = ipaddress.IPv6Address(raw.strip())
    if address.is_unspecified or address.is_loopback or address.is_link_local or address.is_multicast:
        raise SystemExit("non-routable address in state")
    print(address)
' 2>/dev/null || true)
                while IFS= read -r address; do
                    [ -n "${address}" ] || continue
                    if ! ipv6_address_is_managed "${address}"; then
                        MANAGED_TARGET_IPV6_ADDRESSES+=("${address}")
                    fi
                done <<< "${parsed}"
            fi
        fi
        return 0
    fi

    current_boot=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null) || {
        LAST_ERROR="Unable to identify the current boot for IPv6 policy ownership"
        return 1
    }
    [ -n "${current_boot}" ] || {
        LAST_ERROR="Current boot ID is empty; refusing to load IPv6 policy ownership"
        return 1
    }
    if [ ! -e "${IPV6_POLICY_STATE_FILE}" ]; then
        IPV6_POLICY_STATE_LOADED="true"
        return 0
    fi

    state=$(cat "${IPV6_POLICY_STATE_FILE}" 2>/dev/null) || {
        LAST_ERROR="Failed to read IPv6 policy ownership state"
        return 1
    }
    if ! printf '%s' "${state}" | jq -e '
        .version == 1 and
        (.boot_id | type == "string") and
        (.table_managed | type == "boolean") and
        (.addresses | type == "array") and
        all(.addresses[]; type == "string")
    ' >/dev/null 2>&1; then
        LAST_ERROR="IPv6 policy ownership state is malformed"
        return 1
    fi
    saved_boot=$(printf '%s' "${state}" | jq -r '.boot_id')
    if [ "${saved_boot}" != "${current_boot}" ]; then
        MANAGED_TARGET_IPV6_ADDRESSES=()
        IPV6_POLICY_TABLE_MANAGED="false"
        IPV6_POLICY_STATE_LOADED="true"
        return 0
    fi
    IPV6_POLICY_TABLE_MANAGED=$(printf '%s' "${state}" | jq -r '.table_managed')
    if ! parsed=$(printf '%s' "${state}" | jq -r '.addresses[]' | python3 -c '
import ipaddress
import sys

for raw in sys.stdin:
    address = ipaddress.IPv6Address(raw.strip())
    if address.is_unspecified or address.is_loopback or address.is_link_local or address.is_multicast:
        raise SystemExit("non-routable address in state")
    print(address)
'); then
        LAST_ERROR="IPv6 policy ownership addresses are malformed"
        return 1
    fi

    MANAGED_TARGET_IPV6_ADDRESSES=()
    while IFS= read -r address; do
        [ -n "${address}" ] || continue
        MANAGED_TARGET_IPV6_ADDRESSES+=("${address}")
    done <<< "${parsed}"
    IPV6_POLICY_STATE_LOADED="true"
}

persist_managed_ipv6_policy_state() {
    local current_boot state_dir state_tmp addresses_json
    current_boot=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null) || {
        LAST_ERROR="Unable to identify the current boot for IPv6 policy ownership"
        return 1
    }
    [ -n "${current_boot}" ] || {
        LAST_ERROR="Current boot ID is empty; refusing to persist IPv6 policy ownership"
        return 1
    }
    state_dir="${IPV6_POLICY_STATE_FILE%/*}"
    [ "${state_dir}" != "${IPV6_POLICY_STATE_FILE}" ] || state_dir="."
    mkdir -p "${state_dir}" || {
        LAST_ERROR="Failed to create IPv6 policy ownership directory"
        return 1
    }
    state_tmp=$(mktemp "${IPV6_POLICY_STATE_FILE}.tmp.XXXXXX") || {
        LAST_ERROR="Failed to create temporary IPv6 policy ownership state"
        return 1
    }
    addresses_json=$(printf '%s\n' "${MANAGED_TARGET_IPV6_ADDRESSES[@]}" \
        | jq -Rsc 'split("\n") | map(select(length > 0))') || {
        rm -f "${state_tmp}"
        LAST_ERROR="Failed to encode IPv6 policy ownership addresses"
        return 1
    }
    if ! jq -n \
        --arg boot_id "${current_boot}" \
        --argjson table_managed "${IPV6_POLICY_TABLE_MANAGED}" \
        --argjson addresses "${addresses_json}" \
        '{version:1, boot_id:$boot_id, table_managed:$table_managed, addresses:$addresses}' > "${state_tmp}" || \
       ! chmod 600 "${state_tmp}" || \
       ! mv -f "${state_tmp}" "${IPV6_POLICY_STATE_FILE}"; then
        rm -f "${state_tmp}"
        LAST_ERROR="Failed to persist IPv6 policy ownership state"
        return 1
    fi
}

load_decommissioned_state() {
    [ "${DECOMMISSION_STATE_LOADED}" = "false" ] || return 0

    local state decommissioned
    if [ ! -e "${DECOMMISSION_STATE_FILE}" ]; then
        DECOMMISSION_STATE_LOADED="true"
        return 0
    fi
    state=$(cat "${DECOMMISSION_STATE_FILE}" 2>/dev/null) || {
        LAST_ERROR="Failed to read decommission state"
        return 1
    }
    if ! printf '%s' "${state}" | jq -e '
        .version == 1 and
        (.decommissioned | type == "boolean")
    ' >/dev/null 2>&1; then
        LAST_ERROR="Decommission state is malformed"
        return 1
    fi
    decommissioned=$(printf '%s' "${state}" | jq -r '.decommissioned')
    DECOMMISSIONED="${decommissioned}"
    DECOMMISSION_STATE_LOADED="true"
}

persist_decommissioned_state() {
    local state_dir state_tmp decommissioned_json
    state_dir="${DECOMMISSION_STATE_FILE%/*}"
    [ "${state_dir}" != "${DECOMMISSION_STATE_FILE}" ] || state_dir="."
    mkdir -p "${state_dir}" || {
        LAST_ERROR="Failed to create decommission state directory"
        return 1
    }
    state_tmp=$(mktemp "${DECOMMISSION_STATE_FILE}.tmp.XXXXXX") || {
        LAST_ERROR="Failed to create temporary decommission state"
        return 1
    }
    if [ "${DECOMMISSIONED:-false}" = "true" ]; then
        decommissioned_json="true"
    else
        decommissioned_json="false"
    fi
    if ! jq -n \
        --argjson decommissioned "${decommissioned_json}" \
        '{version: 1, decommissioned: $decommissioned}' > "${state_tmp}" || \
       ! chmod 600 "${state_tmp}" || \
       ! mv -f "${state_tmp}" "${DECOMMISSION_STATE_FILE}"; then
        rm -f "${state_tmp}"
        LAST_ERROR="Failed to persist decommission state"
        return 1
    fi
    DECOMMISSION_STATE_LOADED="true"
}

load_managed_k3s_policy_state() {
    load_decommissioned_state || return 1
    if [ "${K3S_POLICY_STATE_LOADED}" != "false" ]; then
        if [ -z "${K3S_TARGET_POD_NAMESPACE:-}" ] && [ -n "${MANAGED_K3S_POD_NAMESPACE:-}" ]; then
            K3S_TARGET_POD_NAMESPACE="${MANAGED_K3S_POD_NAMESPACE}"
        fi
        if [ -z "${K3S_TARGET_POD_SELECTOR:-}" ] && [ -n "${MANAGED_K3S_POD_SELECTOR:-}" ]; then
            K3S_TARGET_POD_SELECTOR="${MANAGED_K3S_POD_SELECTOR}"
        fi
        if [ -n "${MANAGED_K3S_TARGET_IP:-}" ] && [ "${MANAGED_K3S_TARGET_IP}" != "10.9.9.9" ] && \
           { [ -z "${DOCKER_TARGET_IP:-}" ] || [ "${DOCKER_TARGET_IP}" = "10.9.9.9" ]; }; then
            DOCKER_TARGET_IP="${MANAGED_K3S_TARGET_IP}"
            TARGET_IMPL="${TARGET_IMPL:-${MANAGED_K3S_TARGET_IMPL:-}}"
            K3S_TARGET_POD_NAME="${K3S_TARGET_POD_NAME:-${MANAGED_K3S_POD_NAME:-}}"
            K3S_TARGET_POD_NAMESPACE="${K3S_TARGET_POD_NAMESPACE:-${MANAGED_K3S_POD_NAMESPACE:-}}"
            K3S_TARGET_POD_SELECTOR="${K3S_TARGET_POD_SELECTOR:-${MANAGED_K3S_POD_SELECTOR:-}}"
        fi
        if [ "${#MANAGED_K3S_TARGET_IPV6_ADDRESSES[@]}" -gt 0 ] && [ "${#TARGET_IPV6_ADDRESSES[@]}" -eq 0 ]; then
            TARGET_IPV6_ADDRESSES=("${MANAGED_K3S_TARGET_IPV6_ADDRESSES[@]}")
            TARGET_HAS_IPV6_DEFAULT_ROUTE="true"
        fi
        return 0
    fi

    local current_boot state saved_boot target_ip target_impl pod_name pod_namespace pod_selector decommissioned parsed_ipv6 ipv6_addr
    current_boot=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null) || {
        LAST_ERROR="Unable to identify the current boot for k3s policy ownership"
        return 1
    }
    [ -n "${current_boot}" ] || {
        LAST_ERROR="Current boot ID is empty; refusing to load k3s policy ownership"
        return 1
    }
    if [ ! -e "${K3S_POLICY_STATE_FILE}" ]; then
        K3S_POLICY_STATE_LOADED="true"
        return 0
    fi

    state=$(cat "${K3S_POLICY_STATE_FILE}" 2>/dev/null) || {
        LAST_ERROR="Failed to read k3s policy ownership state"
        return 1
    }
    if ! printf '%s' "${state}" | jq -e '
        .version == 1 and
        (.boot_id | type == "string") and
        (.target_ip | type == "string") and
        ((.target_ipv6_addresses // []) | type == "array" and all(.[]; type == "string")) and
        (.target_impl | type == "string") and
        (.pod_name | type == "string") and
        (.pod_namespace | type == "string") and
        (.pod_selector | type == "string") and
        (.decommissioned | type == "boolean")
    ' >/dev/null 2>&1; then
        LAST_ERROR="k3s policy ownership state is malformed"
        return 1
    fi
    saved_boot=$(printf '%s' "${state}" | jq -r '.boot_id')
    target_impl=$(printf '%s' "${state}" | jq -r '.target_impl')
    pod_name=$(printf '%s' "${state}" | jq -r '.pod_name')
    pod_namespace=$(printf '%s' "${state}" | jq -r '.pod_namespace')
    pod_selector=$(printf '%s' "${state}" | jq -r '.pod_selector')
    decommissioned=$(printf '%s' "${state}" | jq -r '.decommissioned')
    if [ ! -e "${DECOMMISSION_STATE_FILE}" ]; then
        DECOMMISSIONED="${decommissioned}"
    fi
    if [ "${saved_boot}" != "${current_boot}" ]; then
        MANAGED_K3S_TARGET_IP=""
        MANAGED_K3S_TARGET_IPV6_ADDRESSES=()
        MANAGED_K3S_TARGET_IMPL="${target_impl}"
        MANAGED_K3S_POD_NAME=""
        MANAGED_K3S_POD_NAMESPACE="${pod_namespace}"
        MANAGED_K3S_POD_SELECTOR="${pod_selector}"
        if [ -z "${K3S_TARGET_POD_NAMESPACE:-}" ] && [ -n "${pod_namespace}" ]; then
            K3S_TARGET_POD_NAMESPACE="${pod_namespace}"
        fi
        if [ -z "${K3S_TARGET_POD_SELECTOR:-}" ] && [ -n "${pod_selector}" ]; then
            K3S_TARGET_POD_SELECTOR="${pod_selector}"
        fi
        K3S_POLICY_STATE_LOADED="true"
        return 0
    fi

    target_ip=$(printf '%s' "${state}" | jq -r '.target_ip')
    if [ -n "${target_ip}" ]; then
        if ! target_ip=$(python3 -c 'import ipaddress, sys; print(ipaddress.IPv4Address(sys.argv[1]))' "${target_ip}" 2>/dev/null); then
            LAST_ERROR="k3s policy ownership target IP is malformed"
            return 1
        fi
    fi
    if ! parsed_ipv6=$(printf '%s' "${state}" | jq -r '(.target_ipv6_addresses // [])[]' | python3 -c '
import ipaddress
import sys

for raw in sys.stdin:
    address = ipaddress.IPv6Address(raw.strip())
    if address.is_unspecified or address.is_loopback or address.is_link_local or address.is_multicast:
        raise SystemExit("non-routable address in k3s state")
    print(address)
' 2>/dev/null); then
        LAST_ERROR="k3s policy ownership IPv6 addresses are malformed"
        return 1
    fi
    MANAGED_K3S_TARGET_IPV6_ADDRESSES=()
    while IFS= read -r ipv6_addr; do
        [ -n "${ipv6_addr}" ] || continue
        MANAGED_K3S_TARGET_IPV6_ADDRESSES+=("${ipv6_addr}")
    done <<< "${parsed_ipv6}"

    MANAGED_K3S_TARGET_IP="${target_ip}"
    MANAGED_K3S_TARGET_IMPL="${target_impl}"
    MANAGED_K3S_POD_NAME="${pod_name}"
    MANAGED_K3S_POD_NAMESPACE="${pod_namespace}"
    MANAGED_K3S_POD_SELECTOR="${pod_selector}"
    if [ -z "${K3S_TARGET_POD_NAMESPACE:-}" ] && [ -n "${MANAGED_K3S_POD_NAMESPACE}" ]; then
        K3S_TARGET_POD_NAMESPACE="${MANAGED_K3S_POD_NAMESPACE}"
    fi
    if [ -z "${K3S_TARGET_POD_SELECTOR:-}" ] && [ -n "${MANAGED_K3S_POD_SELECTOR}" ]; then
        K3S_TARGET_POD_SELECTOR="${MANAGED_K3S_POD_SELECTOR}"
    fi
    if [ -n "${MANAGED_K3S_TARGET_IP}" ] && { [ -z "${DOCKER_TARGET_IP:-}" ] || [ "${DOCKER_TARGET_IP}" = "10.9.9.9" ]; }; then
        DOCKER_TARGET_IP="${MANAGED_K3S_TARGET_IP}"
        TARGET_IMPL="${MANAGED_K3S_TARGET_IMPL}"
        K3S_TARGET_POD_NAME="${MANAGED_K3S_POD_NAME}"
        K3S_TARGET_POD_NAMESPACE="${MANAGED_K3S_POD_NAMESPACE}"
        K3S_TARGET_POD_SELECTOR="${MANAGED_K3S_POD_SELECTOR}"
    fi
    if [ "${#MANAGED_K3S_TARGET_IPV6_ADDRESSES[@]}" -gt 0 ] && [ "${#TARGET_IPV6_ADDRESSES[@]}" -eq 0 ]; then
        TARGET_IPV6_ADDRESSES=("${MANAGED_K3S_TARGET_IPV6_ADDRESSES[@]}")
        TARGET_HAS_IPV6_DEFAULT_ROUTE="true"
    fi
    K3S_POLICY_STATE_LOADED="true"
}

persist_managed_k3s_policy_state() {
    local current_boot state_dir state_tmp decommissioned_json ipv6_addresses_json
    current_boot=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null) || {
        LAST_ERROR="Unable to identify the current boot for k3s policy ownership"
        return 1
    }
    [ -n "${current_boot}" ] || {
        LAST_ERROR="Current boot ID is empty; refusing to persist k3s policy ownership"
        return 1
    }
    state_dir="${K3S_POLICY_STATE_FILE%/*}"
    [ "${state_dir}" != "${K3S_POLICY_STATE_FILE}" ] || state_dir="."
    mkdir -p "${state_dir}" || {
        LAST_ERROR="Failed to create k3s policy ownership directory"
        return 1
    }
    state_tmp=$(mktemp "${K3S_POLICY_STATE_FILE}.tmp.XXXXXX") || {
        LAST_ERROR="Failed to create temporary k3s policy ownership state"
        return 1
    }
    if [ "${DECOMMISSIONED:-false}" = "true" ]; then
        decommissioned_json="true"
    else
        decommissioned_json="false"
    fi
    ipv6_addresses_json=$(printf '%s\n' "${MANAGED_K3S_TARGET_IPV6_ADDRESSES[@]}" \
        | jq -Rsc 'split("\n") | map(select(length > 0))') || {
        rm -f "${state_tmp}"
        LAST_ERROR="Failed to encode k3s policy ownership IPv6 addresses"
        return 1
    }
    if ! jq -n \
        --arg boot_id "${current_boot}" \
        --arg target_ip "${MANAGED_K3S_TARGET_IP:-}" \
        --argjson target_ipv6_addresses "${ipv6_addresses_json}" \
        --arg target_impl "${MANAGED_K3S_TARGET_IMPL:-}" \
        --arg pod_name "${MANAGED_K3S_POD_NAME:-}" \
        --arg pod_namespace "${MANAGED_K3S_POD_NAMESPACE:-}" \
        --arg pod_selector "${MANAGED_K3S_POD_SELECTOR:-}" \
        --argjson decommissioned "${decommissioned_json}" \
        '{
            version: 1,
            boot_id: $boot_id,
            target_ip: $target_ip,
            target_ipv6_addresses: $target_ipv6_addresses,
            target_impl: $target_impl,
            pod_name: $pod_name,
            pod_namespace: $pod_namespace,
            pod_selector: $pod_selector,
            decommissioned: $decommissioned
        }' > "${state_tmp}" || \
       ! chmod 600 "${state_tmp}" || \
       ! mv -f "${state_tmp}" "${K3S_POLICY_STATE_FILE}"; then
        rm -f "${state_tmp}"
        LAST_ERROR="Failed to persist k3s policy ownership state"
        return 1
    fi
}

normalize_target_ipv6_addresses() {
    local normalized address
    if ! normalized=$(printf '%s\n' "${TARGET_IPV6_ADDRESSES[@]}" | python3 -c '
import ipaddress
import sys

addresses = set()
for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    address = ipaddress.ip_address(raw.split("%", 1)[0])
    if address.version != 6:
        raise SystemExit(f"{raw!r} is not an IPv6 address")
    if address.is_unspecified or address.is_loopback or address.is_link_local or address.is_multicast:
        continue
    addresses.add(address)
for address in sorted(addresses, key=int):
    print(address)
'); then
        LAST_ERROR="Failed to normalize target IPv6 addresses: ${normalized//$'\n'/ }"
        return 1
    fi
    TARGET_IPV6_ADDRESSES=()
    while IFS= read -r address; do
        [ -n "${address}" ] || continue
        TARGET_IPV6_ADDRESSES+=("${address}")
    done <<< "${normalized}"
}

refresh_docker_target_endpoints() {
    [[ "${K3S_MODE}" == "true" ]] && return 0
    [[ "${SECURE_MODE}" == "true" ]] && return 0
    [ -n "${TARGET_CONTAINER_ID}" ] || {
        LAST_ERROR="Cannot inspect Docker target networks without a container ID"
        return 1
    }
    if ! load_managed_docker_policy_state; then
        return 1
    fi

    local inspect
    local parsed
    local ipv6_parsed
    local entry
    local address
    local gateway
    local mac
    local managed_entry
    local already_managed
    inspect=$(docker_api "GET" "/containers/${TARGET_CONTAINER_ID}/json") || {
        LAST_ERROR="Failed to inspect networks for ${TARGET_CONTAINER_NAME:-Docker target}"
        return 1
    }
    if ! parsed=$(printf '%s' "${inspect}" | python3 -c '
import ipaddress
import json
import sys

target_network = sys.argv[1]
data = json.load(sys.stdin)
networks = data.get("NetworkSettings", {}).get("Networks", {})
if not isinstance(networks, dict):
    raise SystemExit("NetworkSettings.Networks is unavailable")

found = 0
for name in sorted(networks):
    endpoint = networks[name]
    if not isinstance(endpoint, dict):
        continue
    address = endpoint.get("IPAddress")
    if not address:
        continue
    prefix = endpoint.get("IPPrefixLen")
    interface = ipaddress.IPv4Interface(f"{address}/{prefix}")
    gateway = endpoint.get("Gateway") or ""
    if gateway:
        gateway = str(ipaddress.IPv4Address(gateway))
    priority = endpoint.get("GwPriority", 0)
    if not isinstance(priority, int):
        raise SystemExit(f"invalid GwPriority for {name}")
    print(
        f"{interface.ip}|{interface.network}|{gateway}|"
        f"{1 if name == target_network else 0}|{priority}"
    )
    found += 1
if not found:
    raise SystemExit("target has no assigned IPv4 endpoints")
' "${DOCKER_NETWORK_NAME}"); then
        LAST_ERROR="Failed to parse IPv4 endpoints for ${TARGET_CONTAINER_NAME:-Docker target}: ${parsed//$'\n'/ }"
        return 1
    fi

    if ! ipv6_parsed=$(printf '%s' "${inspect}" | python3 -c '
import ipaddress
import json
import sys

data = json.load(sys.stdin)
networks = data.get("NetworkSettings", {}).get("Networks", {})
if not isinstance(networks, dict):
    raise SystemExit("NetworkSettings.Networks is unavailable")

for name in sorted(networks):
    endpoint = networks[name]
    if not isinstance(endpoint, dict):
        continue
    raw_address = endpoint.get("GlobalIPv6Address") or ""
    raw_gateway = endpoint.get("IPv6Gateway") or ""
    mac = str(endpoint.get("MacAddress") or "").lower()
    address = ""
    gateway = ""
    if raw_address:
        address = str(ipaddress.IPv6Address(raw_address))
    if raw_gateway:
        gateway = str(ipaddress.IPv6Address(raw_gateway))
    if address or gateway:
        print(f"{address}|{gateway}|{mac}")
'); then
        LAST_ERROR="Failed to parse IPv6 endpoints for ${TARGET_CONTAINER_NAME:-Docker target}: ${ipv6_parsed//$'\n'/ }"
        return 1
    fi

    TARGET_IPV4_ENDPOINTS=()
    while IFS= read -r entry; do
        [ -n "${entry}" ] || continue
        TARGET_IPV4_ENDPOINTS+=("${entry}")
        already_managed=false
        for managed_entry in "${MANAGED_DOCKER_IPV4_ENDPOINTS[@]}"; do
            if [ "${managed_entry}" = "${entry}" ]; then
                already_managed=true
                break
            fi
        done
        if [ "${already_managed}" = false ]; then
            MANAGED_DOCKER_IPV4_ENDPOINTS+=("${entry}")
        fi
    done <<< "${parsed}"

    [ "${#TARGET_IPV4_ENDPOINTS[@]}" -gt 0 ] || {
        LAST_ERROR="Docker target has no assigned IPv4 endpoints"
        return 1
    }
    TARGET_IPV6_ADDRESSES=()
    TARGET_IPV6_MACS=()
    TARGET_HAS_IPV6_DEFAULT_ROUTE="false"
    while IFS='|' read -r address gateway mac; do
        if [ -n "${address}" ]; then
            TARGET_IPV6_ADDRESSES+=("${address}")
        fi
        if [ -n "${gateway}" ]; then
            TARGET_HAS_IPV6_DEFAULT_ROUTE="true"
        fi
        if [ -n "${mac}" ]; then
            TARGET_IPV6_MACS+=("${mac}")
        fi
    done <<< "${ipv6_parsed}"
    if ! normalize_target_ipv6_addresses; then
        return 1
    fi
    persist_managed_docker_policy_state
}

docker_exec_route_get() {
    local destination="$1"
    local created
    local exec_id
    local output
    local exec_state
    local exit_code

    created=$(docker_api "POST" "/containers/${TARGET_CONTAINER_ID}/exec" "$(
        jq -cn --arg destination "${destination}" \
            '{AttachStdout:true,AttachStderr:true,Tty:true,Cmd:["ip","-4","route","get",$destination]}'
    )") || {
        LAST_ERROR="Failed to create route inspection in ${TARGET_CONTAINER_NAME}"
        return 1
    }
    exec_id=$(printf '%s' "${created}" | jq -r '.Id // empty')
    [ -n "${exec_id}" ] || {
        LAST_ERROR="Docker did not return an exec ID for route inspection"
        return 1
    }

    output=$(docker_api "POST" "/exec/${exec_id}/start" '{"Detach":false,"Tty":true}') || {
        LAST_ERROR="Failed to start route inspection in ${TARGET_CONTAINER_NAME}"
        return 1
    }
    exec_state=$(docker_api "GET" "/exec/${exec_id}/json") || {
        LAST_ERROR="Failed to read route inspection status in ${TARGET_CONTAINER_NAME}"
        return 1
    }
    exit_code=$(printf '%s' "${exec_state}" | jq -r '.ExitCode // empty')
    if [ "${exit_code}" != "0" ]; then
        LAST_ERROR="Route inspection failed in ${TARGET_CONTAINER_NAME}: ${output//$'\n'/ }"
        return 1
    fi

    printf '%s\n' "${output//$'\r'/}"
}

verify_docker_target_routes() {
    [[ "${K3S_MODE}" == "true" ]] && return 0
    [[ "${SECURE_MODE}" == "true" ]] && return 0

    if ! detect_docker_gw_priority_support || ! refresh_docker_target_endpoints; then
        return 1
    fi

    local entry
    local source
    local subnet
    local gateway
    local is_tunnel
    local priority
    local tunnel_gateway=""
    local tunnel_priority=""
    local other_priority
    for entry in "${TARGET_IPV4_ENDPOINTS[@]}"; do
        IFS='|' read -r source subnet gateway is_tunnel priority <<< "${entry}"
        if [ "${is_tunnel}" = "1" ]; then
            if [ "${source}" != "${DOCKER_TARGET_IP}" ] || [ -z "${gateway}" ]; then
                LAST_ERROR="${DOCKER_NETWORK_NAME} endpoint does not have protected IPv4 source ${DOCKER_TARGET_IP} and a gateway"
                return 1
            fi
            tunnel_gateway="${gateway}"
            tunnel_priority="${priority}"
        fi
    done
    if [ -z "${tunnel_gateway}" ]; then
        LAST_ERROR="${TARGET_CONTAINER_NAME} is not attached to ${DOCKER_NETWORK_NAME}"
        return 1
    fi

    if [ "${DOCKER_GW_PRIORITY_SUPPORTED}" = "true" ]; then
        if [ "${tunnel_priority}" -lt "${DOCKER_MIN_GW_PRIORITY}" ]; then
            LAST_ERROR="${DOCKER_NETWORK_NAME} gateway priority ${tunnel_priority} is below ${DOCKER_MIN_GW_PRIORITY}"
            return 1
        fi
        for entry in "${TARGET_IPV4_ENDPOINTS[@]}"; do
            IFS='|' read -r source subnet gateway is_tunnel other_priority <<< "${entry}"
            if [ "${is_tunnel}" != "1" ] && [ "${other_priority}" -ge "${tunnel_priority}" ]; then
                LAST_ERROR="${DOCKER_NETWORK_NAME} gateway priority ${tunnel_priority} does not outrank a competing endpoint (${other_priority})"
                return 1
            fi
        done
    fi

    local destination
    local route_info
    local selected_source
    local selected_gateway
    for destination in "${DOCKER_ROUTE_PROBE_DESTINATIONS[@]}"; do
        route_info=$(docker_exec_route_get "${destination}") || return 1
        selected_source=$(printf '%s\n' "${route_info}" | awk '
            NR == 1 { for (i = 1; i < NF; i++) if ($i == "src") { print $(i + 1); exit } }
        ')
        selected_gateway=$(printf '%s\n' "${route_info}" | awk '
            NR == 1 { for (i = 1; i < NF; i++) if ($i == "via") { print $(i + 1); exit } }
        ')
        if [ "${selected_source}" != "${DOCKER_TARGET_IP}" ] || [ "${selected_gateway}" != "${tunnel_gateway}" ]; then
            LAST_ERROR="Unsafe route to ${destination} in ${TARGET_CONTAINER_NAME}: expected via ${tunnel_gateway} src ${DOCKER_TARGET_IP}, got ${route_info//$'\n'/ }"
            return 1
        fi
    done
}

k8s_api() {
    local path="$1"
    local token
    token=$(cat "${K8S_SA_TOKEN_PATH}" 2>/dev/null) || { log WARN "k8s: Cannot read service account token"; return 1; }
    curl -sf --connect-timeout 5 --max-time 10 --cacert "${K8S_SA_CA_PATH}" \
        -H "Authorization: Bearer ${token}" \
        "${K8S_API_URL}${path}"
}

delete_k3s_target_pod() {
    local token
    local http_code

    if [ -z "${K3S_TARGET_POD_NAMESPACE}" ] || [ -z "${K3S_TARGET_POD_NAME}" ]; then
        log ERROR "k3s: Cannot delete target pod because its identity is unavailable"
        return 1
    fi
    token=$(cat "${K8S_SA_TOKEN_PATH}" 2>/dev/null) || {
        log ERROR "k3s: Cannot read service account token to delete unsafe target pod"
        return 1
    }
    http_code="$(curl -sS --connect-timeout 5 --max-time 10 --cacert "${K8S_SA_CA_PATH}" \
        -o /dev/null -w '%{http_code}' -X DELETE \
        -H "Authorization: Bearer ${token}" \
        -H "Content-Type: application/json" \
        -d '{"gracePeriodSeconds":0,"propagationPolicy":"Background"}' \
        "${K8S_API_URL}/api/v1/namespaces/${K3S_TARGET_POD_NAMESPACE}/pods/${K3S_TARGET_POD_NAME}" \
        2>/dev/null || true)"
    case "${http_code}" in
        200|202|404)
            log ERROR "k3s: Deleted target pod ${K3S_TARGET_POD_NAMESPACE}/${K3S_TARGET_POD_NAME} after total isolation failure"
            return 0
            ;;
        *)
            log ERROR "k3s: Failed to delete unsafe target pod ${K3S_TARGET_POD_NAMESPACE}/${K3S_TARGET_POD_NAME} (HTTP ${http_code:-unavailable})"
            return 1
            ;;
    esac
}

wait_for_k3s_emergency_isolation() {
    local isolation_error="$1"
    local attempt=0
    local target_deleted=false

    # Returning control to the ordinary reconcile loop while every isolation
    # mechanism is absent would leave the Lightning workload on clear-net
    # egress for an entire interval. Stay in a fail-stop retry loop until one
    # independent control is verified or the unsafe pod is deleted.
    while true; do
        attempt=$((attempt + 1))
        # Stop the unsafe workload before spending time re-establishing slower
        # API and dataplane controls. When every mechanism is unavailable, do
        # not add a backoff window in which clear egress can continue.
        if [ "${target_deleted}" = false ] && delete_k3s_target_pod; then
            target_deleted=true
            LAST_ERROR="${isolation_error}; target pod deleted; waiting for replacement-wide isolation"
        fi
        if [ "${target_deleted}" = false ]; then
            if ensure_k3s_egress_guard "${DOCKER_TARGET_IP}"; then
                LAST_ERROR="${isolation_error}; egress guard installed after retry ${attempt}"
                return 0
            fi
            if ensure_fallback_blackhole_rule \
                "${DOCKER_TARGET_IP}" \
                "k3s: Failed to protect selected pod during isolation retry" \
                "${K3S_RULE_PROTOCOL}"; then
                LAST_ERROR="${isolation_error}; source blackhole installed after retry ${attempt}"
                return 0
            fi
        fi
        if ensure_k3s_subnet_quarantine "${DOCKER_TARGET_IP}"; then
            LAST_ERROR="${isolation_error}; pod CIDR quarantined after retry ${attempt}"
            return 0
        fi
        if ensure_k3s_emergency_network_policy; then
            LAST_ERROR="${isolation_error}; emergency NetworkPolicy installed after retry ${attempt}"
            return 0
        fi

        LAST_ERROR="${isolation_error}; all emergency isolation controls unavailable; fail-stop retry ${attempt}"
        write_state
    done
}

k8s_api_write_status() {
    local method="$1"
    local path="$2"
    local data="${3:-}"
    local token
    local -a data_args=()

    token=$(cat "${K8S_SA_TOKEN_PATH}" 2>/dev/null) || return 1
    if [ -n "${data}" ]; then
        data_args=(-H "Content-Type: application/json" -d "${data}")
    fi
    curl -sS --connect-timeout 5 --max-time 10 --cacert "${K8S_SA_CA_PATH}" \
        -o /dev/null -w '%{http_code}' -X "${method}" \
        -H "Authorization: Bearer ${token}" \
        "${data_args[@]}" \
        "${K8S_API_URL}${path}" 2>/dev/null || true
}

k3s_network_policy_selector() {
    K8S_LABEL_SELECTOR="${K3S_TARGET_POD_SELECTOR}" python3 -c '
import json
import os
import re

raw = os.environ.get("K8S_LABEL_SELECTOR", "").strip()
if not raw:
    raise SystemExit("empty label selector")

clauses = []
start = 0
depth = 0
for index, char in enumerate(raw):
    if char == "(":
        depth += 1
    elif char == ")":
        depth -= 1
        if depth < 0:
            raise SystemExit("unbalanced label selector")
    elif char == "," and depth == 0:
        clauses.append(raw[start:index].strip())
        start = index + 1
if depth != 0:
    raise SystemExit("unbalanced label selector")
clauses.append(raw[start:].strip())

match_labels = {}
expressions = []
for clause in clauses:
    if not clause:
        raise SystemExit("empty label-selector clause")
    set_match = re.fullmatch(r"([^\s!=(),]+)\s+(in|notin)\s*\(([^)]*)\)", clause)
    if set_match:
        key, operator, raw_values = set_match.groups()
        values = [value.strip() for value in raw_values.split(",") if value.strip()]
        if not values:
            raise SystemExit("empty label-selector set")
        expressions.append({
            "key": key,
            "operator": "In" if operator == "in" else "NotIn",
            "values": values,
        })
    elif clause.startswith("!"):
        expressions.append({"key": clause[1:], "operator": "DoesNotExist"})
    elif "!=" in clause:
        key, value = (part.strip() for part in clause.split("!=", 1))
        expressions.append({"key": key, "operator": "NotIn", "values": [value]})
    elif "==" in clause:
        key, value = (part.strip() for part in clause.split("==", 1))
        match_labels[key] = value
    elif "=" in clause:
        key, value = (part.strip() for part in clause.split("=", 1))
        match_labels[key] = value
    else:
        expressions.append({"key": clause, "operator": "Exists"})

selector = {}
if match_labels:
    selector["matchLabels"] = match_labels
if expressions:
    selector["matchExpressions"] = expressions
print(json.dumps(selector, separators=(",", ":")))
'
}

k3s_has_conflicting_egress_network_policy() {
    local policy_collection_path="$1"
    local owned_policy_name="$2"
    local target_selector_json="$3"
    local policy_list

    if ! policy_list="$(k8s_api "${policy_collection_path}" 2>/dev/null)"; then
        return 0
    fi
    POLICY_LIST_JSON="${policy_list}" \
    OWNED_POLICY_NAME="${owned_policy_name}" \
    TARGET_SELECTOR_JSON="${target_selector_json}" \
    python3 -c '
import json
import os
import sys

try:
    raw_list = json.loads(os.environ["POLICY_LIST_JSON"])
    owned_name = os.environ["OWNED_POLICY_NAME"]
    target_selector = json.loads(os.environ["TARGET_SELECTOR_JSON"])
except Exception:
    sys.exit(0)

if isinstance(raw_list, dict) and isinstance(raw_list.get("items"), list):
    items = raw_list["items"]
elif isinstance(raw_list, dict):
    items = [raw_list]
else:
    sys.exit(0)

def selector_constraints(selector):
    if not isinstance(selector, dict):
        return None
    constraints = {}
    def get_key(k):
        return constraints.setdefault(
            k,
            {"allowed": None, "forbidden": set(), "must_exist": False, "must_not_exist": False},
        )
    match_labels = selector.get("matchLabels") or {}
    if not isinstance(match_labels, dict):
        return None
    for k, v in match_labels.items():
        c = get_key(str(k))
        c["must_exist"] = True
        s = {str(v)}
        c["allowed"] = s if c["allowed"] is None else (c["allowed"] & s)
    match_exprs = selector.get("matchExpressions") or []
    if not isinstance(match_exprs, list):
        return None
    for expr in match_exprs:
        if not isinstance(expr, dict):
            return None
        k = str(expr.get("key", ""))
        op = expr.get("operator", "")
        vals = {str(v) for v in (expr.get("values") or [])}
        c = get_key(k)
        if op == "In":
            c["must_exist"] = True
            c["allowed"] = vals if c["allowed"] is None else (c["allowed"] & vals)
        elif op == "NotIn":
            c["forbidden"] |= vals
        elif op == "Exists":
            c["must_exist"] = True
        elif op == "DoesNotExist":
            c["must_not_exist"] = True
        else:
            return None
    return constraints

def selectors_can_overlap(sel_a, sel_b):
    ca = selector_constraints(sel_a)
    cb = selector_constraints(sel_b)
    if ca is None or cb is None:
        return True
    for k in set(ca.keys()) | set(cb.keys()):
        ka = ca.get(k, {"allowed": None, "forbidden": set(), "must_exist": False, "must_not_exist": False})
        kb = cb.get(k, {"allowed": None, "forbidden": set(), "must_exist": False, "must_not_exist": False})
        must_exist = ka["must_exist"] or kb["must_exist"]
        must_not_exist = ka["must_not_exist"] or kb["must_not_exist"]
        if must_exist and must_not_exist:
            return False
        if must_not_exist:
            continue
        allowed = None
        for cand in (ka["allowed"], kb["allowed"]):
            if cand is not None:
                allowed = set(cand) if allowed is None else (allowed & cand)
        forbidden = ka["forbidden"] | kb["forbidden"]
        if allowed is not None and not (allowed - forbidden):
            return False
    return True

for item in items:
    if not isinstance(item, dict):
        continue
    meta = item.get("metadata") or {}
    if meta.get("name") == owned_name:
        continue
    spec = item.get("spec")
    if not isinstance(spec, dict):
        continue
    policy_types = spec.get("policyTypes")
    if isinstance(policy_types, list) and policy_types:
        applies_to_egress = "Egress" in policy_types
    else:
        applies_to_egress = "egress" in spec
    if not applies_to_egress:
        continue
    egress_rules = spec.get("egress")
    if egress_rules is None or egress_rules == []:
        continue
    pod_selector = spec.get("podSelector")
    if pod_selector is None:
        continue
    if selectors_can_overlap(target_selector, pod_selector):
        sys.exit(0)

sys.exit(1)
'
}

ensure_k3s_emergency_network_policy() {
    local policy_name="tunnelsats-emergency-egress-deny"
    local policy_path
    local payload
    local policy_selector
    local status
    local existing

    if [ -z "${K3S_TARGET_POD_NAMESPACE}" ] || \
       ! policy_selector="$(k3s_network_policy_selector)"; then
        log ERROR "k3s: Cannot create emergency NetworkPolicy without a valid stable target selector"
        return 1
    fi
    policy_path="/apis/networking.k8s.io/v1/namespaces/${K3S_TARGET_POD_NAMESPACE}/networkpolicies"
    payload="$(jq -cn \
        --arg name "${policy_name}" \
        --argjson selector "${policy_selector}" \
        '{
            apiVersion: "networking.k8s.io/v1",
            kind: "NetworkPolicy",
            metadata: {
                name: $name,
                annotations: {"tunnelsats.io/emergency-egress-deny": "true"}
            },
            spec: {
                podSelector: $selector,
                policyTypes: ["Egress"],
                egress: []
            }
        }')" || return 1

    status="$(k8s_api_write_status POST "${policy_path}" "${payload}")"
    case "${status}" in
        200|201|409) ;;
        *)
            log ERROR "k3s: Failed to create emergency NetworkPolicy (HTTP ${status:-unavailable})"
            return 1
            ;;
    esac

    if ! existing="$(k8s_api "${policy_path}/${policy_name}")" || \
       ! printf '%s' "${existing}" | jq -e --argjson selector "${policy_selector}" '
            .metadata.annotations["tunnelsats.io/emergency-egress-deny"] == "true"
            and .spec.podSelector == $selector
            and .spec.policyTypes == ["Egress"]
            and .spec.egress == []
       ' >/dev/null 2>&1; then
        log ERROR "k3s: Emergency NetworkPolicy could not be verified"
        return 1
    fi
    if k3s_has_conflicting_egress_network_policy "${policy_path}" "${policy_name}" "${policy_selector}"; then
        LAST_ERROR="k3s: Conflicting egress NetworkPolicy in namespace ${K3S_TARGET_POD_NAMESPACE} can override emergency deny policy"
        log ERROR "${LAST_ERROR}"
        return 1
    fi
    log ERROR "k3s: Emergency deny-egress NetworkPolicy protects current and replacement target pods"
    return 0
}

remove_k3s_emergency_network_policy() {
    local policy_name="tunnelsats-emergency-egress-deny"
    local policy_path
    local policy_selector
    local existing
    local policy_uid
    local delete_payload
    local status

    [ -n "${K3S_TARGET_POD_NAMESPACE}" ] || return 0
    policy_path="/apis/networking.k8s.io/v1/namespaces/${K3S_TARGET_POD_NAMESPACE}/networkpolicies/${policy_name}"
    if ! existing="$(k8s_api "${policy_path}")"; then
        status="$(k8s_api_write_status GET "${policy_path}")"
        if [ "${status}" = "404" ]; then
            return 0
        fi
        LAST_ERROR="k3s: Failed to inspect emergency deny-egress NetworkPolicy"
        return 1
    fi
    if ! policy_selector="$(k3s_network_policy_selector)"; then
        LAST_ERROR="k3s: Failed to rebuild emergency NetworkPolicy selector for safe cleanup"
        return 1
    fi
    if ! printf '%s' "${existing}" | jq -e --argjson selector "${policy_selector}" '
        .metadata.annotations["tunnelsats.io/emergency-egress-deny"] == "true"
        and .spec.podSelector == $selector
        and .spec.policyTypes == ["Egress"]
        and .spec.egress == []
    ' >/dev/null 2>&1; then
        log WARN "k3s: Preserving foreign NetworkPolicy at ${K3S_TARGET_POD_NAMESPACE}/${policy_name}"
        return 0
    fi
    policy_uid="$(printf '%s' "${existing}" | jq -r '.metadata.uid // empty')"
    if [ -z "${policy_uid}" ]; then
        LAST_ERROR="k3s: Emergency NetworkPolicy UID unavailable; refusing unsafe deletion"
        return 1
    fi
    delete_payload="$(jq -cn --arg uid "${policy_uid}" '{preconditions:{uid:$uid}}')"
    status="$(k8s_api_write_status DELETE "${policy_path}" "${delete_payload}")"
    case "${status}" in
        404) return 0 ;;
        200|202)
            local wait_attempts="${K3S_NETPOL_DELETE_WAIT_ATTEMPTS:-10}"
            local wait_interval="${K3S_NETPOL_DELETE_WAIT_INTERVAL:-1}"
            local attempt=0
            local current_policy get_status
            while [ "${attempt}" -lt "${wait_attempts}" ]; do
                if ! current_policy="$(k8s_api "${policy_path}" 2>/dev/null)"; then
                    get_status="$(k8s_api_write_status GET "${policy_path}")"
                    if [ "${get_status}" = "404" ]; then
                        return 0
                    fi
                elif ! printf '%s' "${current_policy}" | jq -e --arg uid "${policy_uid}" --argjson selector "${policy_selector}" '
                    .metadata.uid == $uid
                    and .metadata.annotations["tunnelsats.io/emergency-egress-deny"] == "true"
                    and .spec.podSelector == $selector
                    and .spec.policyTypes == ["Egress"]
                    and .spec.egress == []
                ' >/dev/null 2>&1; then
                    return 0
                fi
                attempt=$((attempt + 1))
                if [ "${attempt}" -lt "${wait_attempts}" ] && [ "${wait_interval}" -gt 0 ] 2>/dev/null; then
                    sleep "${wait_interval}"
                fi
            done
            LAST_ERROR="k3s: Emergency deny-egress NetworkPolicy is still present after deletion"
            return 1
            ;;
        409)
            LAST_ERROR="k3s: Emergency NetworkPolicy changed during deletion; retrying cleanup"
            return 1
            ;;
        *)
            LAST_ERROR="k3s: Failed to remove emergency deny-egress NetworkPolicy"
            return 1
            ;;
    esac
}

remove_k3s_emergency_network_policy_for_target() {
    local target_namespace="$1"
    local target_selector="$2"
    local saved_namespace="${K3S_TARGET_POD_NAMESPACE:-}"
    local saved_selector="${K3S_TARGET_POD_SELECTOR:-}"
    local saved_labels="${K3S_TARGET_POD_LABELS:-}"
    local rc=0

    [ -n "${target_namespace}" ] && [ -n "${target_selector}" ] || return 0
    K3S_TARGET_POD_NAMESPACE="${target_namespace}"
    K3S_TARGET_POD_SELECTOR="${target_selector}"
    K3S_TARGET_POD_LABELS=""
    remove_k3s_emergency_network_policy || rc=$?
    K3S_TARGET_POD_NAMESPACE="${saved_namespace}"
    K3S_TARGET_POD_SELECTOR="${saved_selector}"
    K3S_TARGET_POD_LABELS="${saved_labels}"
    return "${rc}"
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

ensure_runtime_wireguard_keepalive() {
    local config_path="$1"
    local config_tmp

    [ -f "${config_path}" ] || return 1
    config_tmp="$(mktemp "${config_path}.tmp.XXXXXX")" || return 1

    if ! awk '
        function add_keepalive_if_missing() {
            if (in_peer && !peer_has_keepalive) {
                print "PersistentKeepalive = 25"
            }
        }
        {
            normalized = tolower($0)
            gsub(/[[:space:]]/, "", normalized)
            if (normalized ~ /^\[[^]]+\]$/) {
                add_keepalive_if_missing()
                in_peer = (normalized == "[peer]")
                peer_has_keepalive = 0
                print
                next
            }
            directive = tolower($0)
            if (in_peer && directive ~ /^[[:space:]]*persistentkeepalive[[:space:]]*=/) {
                peer_has_keepalive = 1
            }
            print
        }
        END {
            add_keepalive_if_missing()
        }
    ' "${config_path}" > "${config_tmp}"; then
        rm -f "${config_tmp}"
        return 1
    fi

    if ! mv -f "${config_tmp}" "${config_path}"; then
        rm -f "${config_tmp}"
        return 1
    fi
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

k3s_owned_emergency_network_policy_is_present() {
    local target_namespace="$1"
    local target_selector="$2"
    local policy_name="tunnelsats-emergency-egress-deny"
    local policy_path
    local policy_selector
    local existing

    [ -n "${target_namespace}" ] && [ -n "${target_selector}" ] || return 1
    policy_selector="$(
        K3S_TARGET_POD_SELECTOR="${target_selector}" k3s_network_policy_selector 2>/dev/null
    )" || return 1
    policy_path="/apis/networking.k8s.io/v1/namespaces/${target_namespace}/networkpolicies/${policy_name}"
    existing="$(k8s_api "${policy_path}" 2>/dev/null)" || return 1
    printf '%s' "${existing}" | jq -e --argjson selector "${policy_selector}" '
        .metadata.annotations["tunnelsats.io/emergency-egress-deny"] == "true"
        and .spec.podSelector == $selector
        and .spec.policyTypes == ["Egress"]
        and .spec.egress == []
    ' >/dev/null 2>&1
}

k3s_emergency_network_policy_is_active() {
    local target_namespace="$1"
    local target_selector="$2"
    local policy_name="tunnelsats-emergency-egress-deny"
    local policy_collection_path
    local policy_path
    local policy_selector
    local existing

    [ -n "${target_namespace}" ] && [ -n "${target_selector}" ] || return 1
    policy_selector="$(
        K3S_TARGET_POD_SELECTOR="${target_selector}" k3s_network_policy_selector 2>/dev/null
    )" || return 1
    policy_collection_path="/apis/networking.k8s.io/v1/namespaces/${target_namespace}/networkpolicies"
    policy_path="${policy_collection_path}/${policy_name}"
    existing="$(k8s_api "${policy_path}" 2>/dev/null)" || return 1
    printf '%s' "${existing}" | jq -e --argjson selector "${policy_selector}" '
        .metadata.annotations["tunnelsats.io/emergency-egress-deny"] == "true"
        and .spec.podSelector == $selector
        and .spec.policyTypes == ["Egress"]
        and .spec.egress == []
    ' >/dev/null 2>&1 || return 1
    if k3s_has_conflicting_egress_network_policy "${policy_collection_path}" "${policy_name}" "${policy_selector}"; then
        return 1
    fi
    return 0
}

k3s_pod_is_under_fail_closed_guard() {
    local target_namespace="$1"
    local target_selector="$2"

    if [ -n "${MANAGED_K3S_TARGET_IP:-}" ] || [ -n "${MANAGED_K3S_POD_NAMESPACE:-}" ] || \
       [ -n "${K3S_QUARANTINE_CIDR:-}" ] || [ -n "${K3S_IPV6_QUARANTINE_CIDR:-}" ]; then
        return 0
    fi
    if k3s_emergency_network_policy_is_active "${target_namespace}" "${target_selector}"; then
        return 0
    fi
    return 1
}

k3s_ip_is_in_csv() {
    local target_ip="${1%/32}"
    local csv="${2:-}"
    [ -n "${target_ip}" ] && [ -n "${csv}" ] || return 1
    [[ ",${csv}," == *",${target_ip},"* ]]
}

k3s_append_unique_csv() {
    local var_name="$1"
    local value="${2%/32}"
    local current="${!var_name:-}"
    [ -n "${value}" ] || return 0
    if k3s_ip_is_in_csv "${value}" "${current}"; then
        return 0
    fi
    if [ -n "${current}" ]; then
        printf -v "${var_name}" '%s,%s' "${current}" "${value}"
    else
        printf -v "${var_name}" '%s' "${value}"
    fi
}

k3s_remove_from_csv() {
    local var_name="$1"
    local target_ip="${2%/32}"
    local current="${!var_name:-}"
    local item updated=""
    local -a items=()
    [ -n "${target_ip}" ] && [ -n "${current}" ] || return 0
    IFS=',' read -r -a items <<< "${current}"
    for item in "${items[@]}"; do
        [ -n "${item}" ] || continue
        [ "${item%/32}" = "${target_ip}" ] && continue
        if [ -n "${updated}" ]; then
            updated="${updated},${item}"
        else
            updated="${item}"
        fi
    done
    printf -v "${var_name}" '%s' "${updated}"
}

k3s_watch_state_file_path() {
    printf '%s\n' "${K3S_WATCH_STATE_FILE:-${K3S_POLICY_STATE_FILE:-/tmp/tunnelsats-k3s-policy-state.json}.tmp.watch}"
}

k3s_watch_state_lock_path() {
    printf '%s.lock\n' "$(k3s_watch_state_file_path)"
}

K3S_WATCH_LOCK_OWNER_PID=""
K3S_WATCH_LOCK_DEPTH=0
K3S_WATCH_LOCK_METHOD=""
K3S_WATCH_LOCK_PATH=""

k3s_acquire_watch_state_lock() {
    local current_pid="${BASHPID:-$$}"
    local lock_file lock_dir attempt=0
    lock_file="$(k3s_watch_state_lock_path)"
    if [ "${K3S_WATCH_LOCK_OWNER_PID:-}" = "${current_pid}" ] && \
       [ "${K3S_WATCH_LOCK_DEPTH:-0}" -gt 0 ] && \
       [ "${K3S_WATCH_LOCK_PATH:-}" = "${lock_file}" ]; then
        K3S_WATCH_LOCK_DEPTH=$((K3S_WATCH_LOCK_DEPTH + 1))
        return 0
    fi

    K3S_WATCH_LOCK_DEPTH=0
    K3S_WATCH_LOCK_METHOD=""
    K3S_WATCH_LOCK_PATH=""
    lock_dir="${lock_file%/*}"
    [ "${lock_dir}" != "${lock_file}" ] || lock_dir="."
    mkdir -p "${lock_dir}" 2>/dev/null || return 1

    if command -v flock >/dev/null 2>&1; then
        if ! ( : >> "${lock_file}" ) 2>/dev/null; then
            return 1
        fi
        exec {K3S_WATCH_LOCK_FD}>"${lock_file}" || return 1
        if ! flock -w 10 "${K3S_WATCH_LOCK_FD}" 2>/dev/null; then
            exec {K3S_WATCH_LOCK_FD}>&- || true
            unset K3S_WATCH_LOCK_FD
            return 1
        fi
        K3S_WATCH_LOCK_METHOD="flock"
    else
        while ! mkdir "${lock_file}.d" 2>/dev/null; do
            attempt=$((attempt + 1))
            if [ "${attempt}" -ge 100 ]; then
                return 1
            fi
            sleep 0.05 2>/dev/null || sleep 1
        done
        K3S_WATCH_LOCK_METHOD="mkdir"
    fi

    K3S_WATCH_LOCK_OWNER_PID="${current_pid}"
    K3S_WATCH_LOCK_PATH="${lock_file}"
    K3S_WATCH_LOCK_DEPTH=1
    return 0
}

k3s_release_watch_state_lock() {
    local current_pid="${BASHPID:-$$}"
    if [ "${K3S_WATCH_LOCK_OWNER_PID:-}" != "${current_pid}" ] || [ "${K3S_WATCH_LOCK_DEPTH:-0}" -le 0 ]; then
        return 0
    fi
    K3S_WATCH_LOCK_DEPTH=$((K3S_WATCH_LOCK_DEPTH - 1))
    if [ "${K3S_WATCH_LOCK_DEPTH}" -eq 0 ]; then
        if [ "${K3S_WATCH_LOCK_METHOD:-}" = "flock" ] && [ -n "${K3S_WATCH_LOCK_FD:-}" ]; then
            flock -u "${K3S_WATCH_LOCK_FD}" 2>/dev/null || true
            exec {K3S_WATCH_LOCK_FD}>&- || true
            unset K3S_WATCH_LOCK_FD
        elif [ "${K3S_WATCH_LOCK_METHOD:-}" = "mkdir" ] && [ -n "${K3S_WATCH_LOCK_PATH:-}" ]; then
            rmdir "${K3S_WATCH_LOCK_PATH}.d" 2>/dev/null || true
        fi
        K3S_WATCH_LOCK_OWNER_PID=""
        K3S_WATCH_LOCK_METHOD=""
        K3S_WATCH_LOCK_PATH=""
    fi
    return 0
}

load_k3s_pod_watch_state() {
    local state_file
    local -a state_lines=()
    state_file="$(k3s_watch_state_file_path)"
    [ -f "${state_file}" ] || return 0
    if ! k3s_acquire_watch_state_lock; then
        LAST_ERROR="k3s: Failed to acquire pod watch state lock"
        return 1
    fi
    mapfile -t state_lines < <(
        jq -r '
            (.scope // ""),
            (.resource_version // ""),
            (.watched_ips // ""),
            (.watched_ipv6 // ""),
            (if .unseen_replacement_pending == true then "true" else "false" end)
        ' "${state_file}" 2>/dev/null || true
    )
    k3s_release_watch_state_lock
    [ "${#state_lines[@]}" -ge 5 ] || return 0
    K3S_WATCH_SCOPE="${state_lines[0]}"
    K3S_WATCH_RESOURCE_VERSION="${state_lines[1]}"
    K3S_WATCHED_CANDIDATE_POD_IPS="${state_lines[2]}"
    K3S_WATCHED_CANDIDATE_POD_IPV6_ADDRESSES="${state_lines[3]}"
    K3S_UNSEEN_REPLACEMENT_PENDING="${state_lines[4]}"
    return 0
}

persist_k3s_pod_watch_state() {
    local state_file state_dir tmp_file pending_json
    state_file="$(k3s_watch_state_file_path)"
    state_dir="${state_file%/*}"
    [ "${state_dir}" != "${state_file}" ] || state_dir="."
    if ! k3s_acquire_watch_state_lock; then
        LAST_ERROR="k3s: Failed to acquire pod watch state lock"
        return 1
    fi
    if ! mkdir -p "${state_dir}" 2>/dev/null; then
        k3s_release_watch_state_lock
        LAST_ERROR="k3s: Failed to create pod watch state directory"
        return 1
    fi
    if ! tmp_file="$(mktemp "${state_file}.tmp.XXXXXX" 2>/dev/null)"; then
        k3s_release_watch_state_lock
        LAST_ERROR="k3s: Failed to create temporary pod watch state file"
        return 1
    fi
    if [ "${K3S_UNSEEN_REPLACEMENT_PENDING:-false}" = "true" ]; then
        pending_json="true"
    else
        pending_json="false"
    fi
    if ! jq -cn \
        --arg scope "${K3S_WATCH_SCOPE:-}" \
        --arg rv "${K3S_WATCH_RESOURCE_VERSION:-}" \
        --arg ips "${K3S_WATCHED_CANDIDATE_POD_IPS:-}" \
        --arg ipv6 "${K3S_WATCHED_CANDIDATE_POD_IPV6_ADDRESSES:-}" \
        --argjson pending "${pending_json}" \
        '{
            scope: $scope,
            resource_version: $rv,
            watched_ips: $ips,
            watched_ipv6: $ipv6,
            unseen_replacement_pending: $pending
        }' > "${tmp_file}" 2>/dev/null || \
       ! chmod 600 "${tmp_file}" 2>/dev/null || \
       ! mv -f "${tmp_file}" "${state_file}" 2>/dev/null; then
        rm -f "${tmp_file}" 2>/dev/null || true
        k3s_release_watch_state_lock
        LAST_ERROR="k3s: Failed to persist pod watch state"
        return 1
    fi
    k3s_release_watch_state_lock
    return 0
}

sync_k3s_watched_candidate_addresses() {
    [[ "${K3S_MODE}" == "true" ]] || return 0
    load_k3s_pod_watch_state || return 1
    local item
    local -a items=()
    if [ -n "${K3S_WATCHED_CANDIDATE_POD_IPS:-}" ]; then
        IFS=',' read -r -a items <<< "${K3S_WATCHED_CANDIDATE_POD_IPS}"
        for item in "${items[@]}"; do
            [ -n "${item}" ] || continue
            k3s_append_unique_csv K3S_CANDIDATE_POD_IPS "${item}"
            k3s_append_unique_csv K3S_ALL_MATCHING_LOCAL_POD_IPS "${item}"
        done
    fi
    if [ -n "${K3S_WATCHED_CANDIDATE_POD_IPV6_ADDRESSES:-}" ]; then
        IFS=',' read -r -a items <<< "${K3S_WATCHED_CANDIDATE_POD_IPV6_ADDRESSES}"
        for item in "${items[@]}"; do
            [ -n "${item}" ] || continue
            k3s_append_unique_csv K3S_CANDIDATE_POD_IPV6_ADDRESSES "${item}"
        done
    fi
    if [ -n "${K3S_CANDIDATE_POD_IPV6_ADDRESSES:-}" ]; then
        IFS=',' read -r -a items <<< "${K3S_CANDIDATE_POD_IPV6_ADDRESSES}"
        for item in "${items[@]}"; do
            [ -n "${item}" ] || continue
            TARGET_IPV6_ADDRESSES+=("${item}")
            TARGET_HAS_IPV6_DEFAULT_ROUTE="true"
        done
        normalize_target_ipv6_addresses || return 1
    fi
    return 0
}

pre_guard_colocated_k3s_candidate_pods() {
    local pod_list="$1"
    local replace_existing="${2:-false}"
    local parsed_candidates state family address list_rv unassigned_state
    local saved_error="${LAST_ERROR:-}"
    local guard_error=""
    local rc=0

    if ! k3s_acquire_watch_state_lock; then
        K3S_CANDIDATE_PODS_GUARDED="false"
        LAST_ERROR="k3s: Failed to acquire pod watch state lock"
        return 1
    fi

    if [ "${replace_existing}" != "true" ]; then
        if ! load_k3s_pod_watch_state; then
            k3s_release_watch_state_lock
            K3S_CANDIDATE_PODS_GUARDED="false"
            return 1
        fi
    else
        K3S_CANDIDATE_PODS_GUARDED="true"
        K3S_UNSEEN_REPLACEMENT_PENDING="false"
    fi
    list_rv="$(printf '%s' "${pod_list}" | jq -r '.metadata.resourceVersion // ([.items[]?.metadata.resourceVersion // empty] | last) // empty' 2>/dev/null || true)"
    if [ -n "${list_rv}" ]; then
        K3S_WATCH_RESOURCE_VERSION="${list_rv}"
        if [ -n "${K3S_TARGET_POD_NAMESPACE:-}" ] && [ -n "${K3S_TARGET_POD_SELECTOR:-}" ]; then
            K3S_WATCH_SCOPE="${K3S_TARGET_POD_NAMESPACE}|${K3S_TARGET_POD_SELECTOR}"
        fi
    fi

    if [ -z "${TUNNELSATS_K8S_NODE_NAME:-}" ]; then
        k3s_release_watch_state_lock
        return 0
    fi
    if [ "${replace_existing}" = "true" ]; then
        K3S_CANDIDATE_POD_IPS=""
        K3S_CANDIDATE_POD_IPV6_ADDRESSES=""
        K3S_ALL_MATCHING_LOCAL_POD_IPS=""
        K3S_WATCHED_CANDIDATE_POD_IPS=""
        K3S_WATCHED_CANDIDATE_POD_IPV6_ADDRESSES=""
    fi

    local target_selector_json=""
    if [ -n "${K3S_TARGET_POD_SELECTOR:-}" ]; then
        target_selector_json="$(k3s_network_policy_selector 2>/dev/null || true)"
    fi

    parsed_candidates="$(
        POD_LIST_JSON="${pod_list}" \
        NODE_NAME="${TUNNELSATS_K8S_NODE_NAME}" \
        REPLACE_MODE="${replace_existing}" \
        TARGET_SELECTOR_JSON="${target_selector_json}" \
        python3 -c '
import ipaddress
import json
import os
import re

node_name = os.environ.get("NODE_NAME", "").strip()
replace_mode = os.environ.get("REPLACE_MODE", "false") == "true"
target_selector = None
raw_selector = os.environ.get("TARGET_SELECTOR_JSON", "").strip()
if raw_selector:
    try:
        parsed_sel = json.loads(raw_selector)
        if isinstance(parsed_sel, dict):
            target_selector = parsed_sel
    except Exception:
        target_selector = None

def matches_selector(labels, selector):
    if not isinstance(selector, dict):
        return True
    if not isinstance(labels, dict):
        labels = {}
    match_labels = selector.get("matchLabels") or {}
    if isinstance(match_labels, dict):
        for k, v in match_labels.items():
            if str(k) not in labels or str(labels.get(str(k), "")) != str(v):
                return False
    match_exprs = selector.get("matchExpressions") or []
    if isinstance(match_exprs, list):
        for expr in match_exprs:
            if not isinstance(expr, dict):
                continue
            k = str(expr.get("key", ""))
            op = expr.get("operator", "")
            vals = {str(v) for v in (expr.get("values") or [])}
            if op == "In":
                if k not in labels or str(labels.get(k, "")) not in vals:
                    return False
            elif op == "NotIn":
                if k in labels and str(labels.get(k, "")) in vals:
                    return False
            elif op == "Exists":
                if k not in labels:
                    return False
            elif op == "DoesNotExist":
                if k in labels:
                    return False
    return True

try:
    doc = json.loads(os.environ.get("POD_LIST_JSON", ""))
    items = doc.get("items") if isinstance(doc, dict) else []
except Exception:
    items = []
if not isinstance(items, list):
    items = []

ipv4_re = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$")
has_unassigned = False
has_replacing = False
order = {"non_target_deleted": 0, "deleted": 1, "non_target_active": 2, "active": 3}
entries = []
seen = set()

for item in items:
    if not isinstance(item, dict):
        continue
    meta = item.get("metadata") or {}
    spec = item.get("spec") or {}
    if spec.get("hostNetwork") is True:
        continue
    status = item.get("status") or {}
    ev_type = str(item.get("_watchEventType") or "")
    phase = str(status.get("phase") or "")
    pod_node = str(spec.get("nodeName") or "")
    del_ts = meta.get("deletionTimestamp")
    labels = meta.get("labels")
    is_target = matches_selector(labels, target_selector)

    raw_ips = []
    for entry in status.get("podIPs") or []:
        if isinstance(entry, dict) and isinstance(entry.get("ip"), str) and entry["ip"]:
            raw_ips.append(entry["ip"])
    if isinstance(status.get("podIP"), str) and status["podIP"]:
        raw_ips.append(status["podIP"])

    if is_target:
        v4_ips = [ip for ip in raw_ips if ipv4_re.match(ip)]
        if (
            ev_type != "DELETED"
            and phase not in ("Succeeded", "Failed")
            and (pod_node == node_name or pod_node == "")
            and len(v4_ips) == 0
        ):
            has_unassigned = True
        if (
            not replace_mode
            and pod_node == node_name
            and (ev_type == "DELETED" or del_ts is not None)
        ):
            has_replacing = True

    if pod_node != node_name:
        continue

    if is_target:
        state = "deleted" if (ev_type == "DELETED" or phase in ("Succeeded", "Failed")) else "active"
    else:
        state = "non_target_deleted" if (ev_type == "DELETED" or del_ts is not None or phase != "Running") else "non_target_active"

    for ip_str in raw_ips:
        try:
            addr = ipaddress.ip_address(ip_str.split("%", 1)[0])
        except ValueError:
            continue
        if addr.is_unspecified or addr.is_loopback or addr.is_link_local or addr.is_multicast:
            continue
        key = (state, addr.version, str(addr))
        if key in seen:
            continue
        seen.add(key)
        entries.append((order.get(state, 9), state, addr.version, str(addr)))

unassigned_state = "unassigned" if has_unassigned else ("replacing" if has_replacing else "none")
print(f"UNASSIGNED|{unassigned_state}|")
for _, state, version, addr_str in sorted(entries):
    print(f"{state}|{version}|{addr_str}")
' 2>/dev/null || printf 'UNASSIGNED|none|\n'
    )"

    unassigned_state="none"
    while IFS='|' read -r state family address; do
        if [ "${state}" = "UNASSIGNED" ]; then
            unassigned_state="${family:-none}"
            if [ "${unassigned_state}" = "unassigned" ] || [ "${unassigned_state}" = "replacing" ]; then
                K3S_UNSEEN_REPLACEMENT_PENDING="true"
            fi
            continue
        fi
        [ -n "${address}" ] || continue
        if [ "${family}" = "4" ]; then
            if [ "${state}" = "non_target_deleted" ]; then
                k3s_remove_from_csv K3S_KNOWN_NON_TARGET_POD_IPS "${address}"
                revoke_k3s_non_target_pod_exemption "4" "${address}"
            elif [ "${state}" = "non_target_active" ]; then
                :
            elif [ "${state}" = "deleted" ]; then
                k3s_remove_from_csv K3S_ALL_MATCHING_LOCAL_POD_IPS "${address}"
                k3s_remove_from_csv K3S_CANDIDATE_POD_IPS "${address}"
                k3s_remove_from_csv K3S_WATCHED_CANDIDATE_POD_IPS "${address}"
            else
                k3s_remove_from_csv K3S_KNOWN_NON_TARGET_POD_IPS "${address}"
                if [ "${address}" != "${DOCKER_TARGET_IP:-}" ]; then
                    revoke_k3s_non_target_pod_exemption "4" "${address}"
                fi
                k3s_append_unique_csv K3S_ALL_MATCHING_LOCAL_POD_IPS "${address}"
                k3s_append_unique_csv K3S_CANDIDATE_POD_IPS "${address}"
                k3s_append_unique_csv K3S_WATCHED_CANDIDATE_POD_IPS "${address}"
                local guard_ok=false
                local blackhole_ok=false
                if { [ "${replace_existing}" != "true" ] || [ "${K3S_WATCH_EVENT_GUARD:-false}" = "true" ]; } && \
                   [ "${address}" = "${DOCKER_TARGET_IP:-}" ] && \
                   ! iptables -S FORWARD 2>/dev/null | grep -Eq -- "-s ${address//./\\.}(/32)? .*tunnelsats-k3s-egress-guard" && \
                   ip rule show pref "${K3S_TUNNEL_RULE_PREF}" 2>/dev/null | grep -qE \
                    "^${K3S_TUNNEL_RULE_PREF}:[[:space:]]+from[[:space:]]+${address//./\\.}[[:space:]]+(lookup|table)[[:space:]]+51820([[:space:]].*)?[[:space:]]+proto(col)?[[:space:]]+${K3S_RULE_PROTOCOL}[[:space:]]*$" && \
                   ip route show table 51820 2>/dev/null | grep -Fq "default dev ${WG_IFACE}"; then
                    guard_ok=true
                elif ensure_k3s_egress_guard_additive "${address}"; then
                    guard_ok=true
                fi
                if ensure_fallback_blackhole_rule \
                    "${address}" \
                    "k3s: Failed to pre-guard co-located candidate pod ${address}" \
                    "${K3S_RULE_PROTOCOL}"; then
                    blackhole_ok=true
                fi
                if [ "${guard_ok}" = false ] && [ "${blackhole_ok}" = false ]; then
                    guard_error="k3s: Failed to pre-guard co-located candidate pod ${address}"
                    rc=1
                fi
            fi
        elif [ "${family}" = "6" ]; then
            if [ "${state}" = "non_target_deleted" ]; then
                k3s_remove_from_csv K3S_KNOWN_NON_TARGET_POD_IPV6_ADDRESSES "${address}"
                revoke_k3s_non_target_pod_exemption "6" "${address}"
            elif [ "${state}" = "non_target_active" ]; then
                :
            elif [ "${state}" = "deleted" ]; then
                k3s_remove_from_csv K3S_CANDIDATE_POD_IPV6_ADDRESSES "${address}"
                k3s_remove_from_csv K3S_WATCHED_CANDIDATE_POD_IPV6_ADDRESSES "${address}"
            else
                k3s_remove_from_csv K3S_KNOWN_NON_TARGET_POD_IPV6_ADDRESSES "${address}"
                revoke_k3s_non_target_pod_exemption "6" "${address}"
                k3s_append_unique_csv K3S_CANDIDATE_POD_IPV6_ADDRESSES "${address}"
                k3s_append_unique_csv K3S_WATCHED_CANDIDATE_POD_IPV6_ADDRESSES "${address}"
                load_managed_ipv6_policy_state >/dev/null 2>&1 || true
                local v6_guard_ok=false
                local v6_blackhole_ok=false
                if command -v ip6tables >/dev/null 2>&1; then
                    if ip6tables -C FORWARD -s "${address}" ! -d fe80::/10 \
                        -m comment --comment "${IPV6_RULE_MARKER}" -j DROP >/dev/null 2>&1; then
                        v6_guard_ok=true
                    elif ip6tables -I FORWARD 1 -s "${address}" ! -d fe80::/10 \
                        -m comment --comment "${IPV6_RULE_MARKER}" -j DROP >/dev/null 2>&1; then
                        v6_guard_ok=true
                    fi
                fi
                if ensure_ipv6_source_blackhole_rule "${address}"; then
                    v6_blackhole_ok=true
                    if ! ipv6_address_is_managed "${address}"; then
                        MANAGED_TARGET_IPV6_ADDRESSES+=("${address}")
                        persist_managed_ipv6_policy_state >/dev/null 2>&1 || true
                    fi
                fi
                if [ "${v6_guard_ok}" = false ] && [ "${v6_blackhole_ok}" = false ]; then
                    guard_error="k3s: Failed to pre-guard co-located candidate pod IPv6 ${address}"
                    rc=1
                fi
            fi
        fi
    done <<< "${parsed_candidates}"

    if [ "${K3S_UNSEEN_REPLACEMENT_PENDING:-false}" = "true" ]; then
        local saved_ns="${MANAGED_K3S_POD_NAMESPACE:-}"
        local saved_sel="${MANAGED_K3S_POD_SELECTOR:-}"
        ensure_k3s_pre_discovery_containment "${DOCKER_TARGET_IP:-${MANAGED_K3S_TARGET_IP:-}}" || true
        if [ -n "${saved_ns}" ] && [ -n "${saved_sel}" ]; then
            MANAGED_K3S_POD_NAMESPACE="${saved_ns}"
            MANAGED_K3S_POD_SELECTOR="${saved_sel}"
        fi
    fi

    if ! persist_k3s_pod_watch_state; then
        guard_error="${guard_error:-${LAST_ERROR:-k3s: Failed to persist pod watch state}}"
        rc=1
    fi
    k3s_release_watch_state_lock

    if [ "${rc}" -eq 0 ]; then
        K3S_CANDIDATE_PODS_GUARDED="${K3S_CANDIDATE_PODS_GUARDED:-true}"
        LAST_ERROR="${saved_error}"
    else
        K3S_CANDIDATE_PODS_GUARDED="false"
        LAST_ERROR="${guard_error:-k3s: Failed to pre-guard co-located candidate pod}"
    fi
    return "${rc}"
}

resolve_k3s_target_pod() {
    local impl="$1"
    local namespace="$2"
    local selector="$3"
    local encoded_selector pod_list running_pods pod
    local pod_name pod_ip pod_node pod_addresses parsed_pod_addresses family address candidate_v6
    local had_prior_fail_closed_guard=false

    encoded_selector=$(urlencode "${selector}")
    if ! pod_list=$(k8s_api "/api/v1/namespaces/${namespace}/pods?labelSelector=${encoded_selector}"); then
        LAST_ERROR="k3s: Failed to query ${impl^^} pods (namespace=${namespace}, selector=${selector})"
        log ERROR "${LAST_ERROR}"
        return 1
    fi

    if ! printf '%s' "${pod_list}" | jq -e '.items | type == "array"' >/dev/null 2>&1; then
        LAST_ERROR="k3s: No Running ${impl^^} pod found (namespace=${namespace}, selector=${selector})"
        log ERROR "${LAST_ERROR}"
        return 1
    fi
    if k3s_pod_is_under_fail_closed_guard "${namespace}" "${selector}"; then
        had_prior_fail_closed_guard=true
    fi
    if [ -n "${TUNNELSATS_K8S_NODE_NAME:-}" ]; then
        K3S_POD_LIST_REPLACED="true"
        if ! pre_guard_colocated_k3s_candidate_pods "${pod_list}" "true"; then
            K3S_POD_API_QUERY_OK="false"
            K3S_CANDIDATE_PODS_GUARDED="false"
            log ERROR "${LAST_ERROR}"
            return 1
        fi
        K3S_POD_API_QUERY_OK="true"
    fi

    if ! running_pods=$(printf '%s' "${pod_list}" | jq -ce \
        '[.items[]? | select(.status.phase == "Running")]' 2>/dev/null); then
        LAST_ERROR="k3s: No Running ${impl^^} pod found (namespace=${namespace}, selector=${selector})"
        log ERROR "${LAST_ERROR}"
        return 1
    fi

    if [ -z "${TUNNELSATS_K8S_NODE_NAME}" ]; then
        LAST_ERROR="k3s: TunnelSats node name is unavailable; refusing to activate dataplane"
        log ERROR "${LAST_ERROR}"
        return 1
    fi

    # During a rollout the selector can temporarily match multiple Running pods.
    # Prefer a complete, Ready, non-terminating pod on this node instead of
    # trusting Kubernetes API order.
    pod=$(printf '%s' "${running_pods}" | jq -ce --arg node "${TUNNELSATS_K8S_NODE_NAME}" \
        '[.[]
          | select(
              .spec.nodeName == $node
              and (.metadata.deletionTimestamp // null) == null
              and any(.status.conditions[]?; .type == "Ready" and .status == "True")
              and (.metadata.name // "") != ""
              and (.status.podIP // "") != ""
          )]
         | first
         // empty' 2>/dev/null || true)

    # When fail-closed guards (such as the emergency deny-egress NetworkPolicy
    # or persisted pod/subnet blackhole) are active across a restart or upgrade,
    # the co-located Running pod's readiness probe may fail until TunnelSats
    # restores routing and releases the emergency policy. Allow selecting a
    # guarded, non-terminating local Running pod so reconciliation can recover.
    if [ -z "${pod}" ] && [ "${had_prior_fail_closed_guard}" = true ]; then
        pod=$(printf '%s' "${running_pods}" | jq -ce --arg node "${TUNNELSATS_K8S_NODE_NAME}" \
            '[.[]
              | select(
                  .spec.nodeName == $node
                  and (.metadata.deletionTimestamp // null) == null
                  and (.metadata.name // "") != ""
                  and (.status.podIP // "") != ""
              )]
             | first
             // empty' 2>/dev/null || true)
    fi

    # Preserve the detailed incomplete-metadata error when the only otherwise
    # usable local candidate lacks a name or pod IP.
    if [ -z "${pod}" ]; then
        pod=$(printf '%s' "${running_pods}" | jq -ce --arg node "${TUNNELSATS_K8S_NODE_NAME}" \
            '[.[]
              | select(
                  .spec.nodeName == $node
                  and (.metadata.deletionTimestamp // null) == null
                  and any(.status.conditions[]?; .type == "Ready" and .status == "True")
              )]
             | first
             // empty' 2>/dev/null || true)
    fi

    if [ -z "${pod}" ] && printf '%s' "${running_pods}" | jq -e --arg node "${TUNNELSATS_K8S_NODE_NAME}" \
        'any(.[]; .spec.nodeName == $node)' >/dev/null 2>&1; then
        LAST_ERROR="k3s: No Ready non-terminating ${impl^^} pod is co-located on TunnelSats node=${TUNNELSATS_K8S_NODE_NAME} (namespace=${namespace}, selector=${selector})"
        log ERROR "${LAST_ERROR}"
        return 1
    fi

    # No local candidate exists. Keep one remote candidate so the normal
    # co-location error below can report its pod and node.
    if [ -z "${pod}" ]; then
        pod=$(printf '%s' "${running_pods}" | jq -ce 'first // empty' 2>/dev/null || true)
    fi

    if [ -z "${pod}" ]; then
        LAST_ERROR="k3s: No Running ${impl^^} pod found (namespace=${namespace}, selector=${selector})"
        log ERROR "${LAST_ERROR}"
        return 1
    fi

    pod_name=$(printf '%s' "${pod}" | jq -r '.metadata.name // empty')
    pod_node=$(printf '%s' "${pod}" | jq -r '.spec.nodeName // empty')

    pod_addresses=$(printf '%s' "${pod}" | jq -r '
        [(.status.podIPs[]?.ip // empty), (.status.podIP // empty)]
        | unique[]
    ' 2>/dev/null || true)
    if ! parsed_pod_addresses=$(printf '%s\n' "${pod_addresses}" | python3 -c '
import ipaddress
import sys

seen = set()
for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    address = ipaddress.ip_address(raw)
    if address in seen:
        continue
    seen.add(address)
    print(f"{address.version}|{address}")
'); then
        LAST_ERROR="k3s: ${impl^^} pod contains an invalid pod IP"
        log ERROR "${LAST_ERROR}"
        return 1
    fi
    pod_ip=""
    TARGET_IPV6_ADDRESSES=()
    TARGET_IPV6_MACS=()
    TARGET_HAS_IPV6_DEFAULT_ROUTE="false"
    while IFS='|' read -r family address; do
        [ -n "${address}" ] || continue
        if [ "${family}" = "4" ] && [ -z "${pod_ip}" ]; then
            pod_ip="${address}"
        elif [ "${family}" = "6" ]; then
            TARGET_IPV6_ADDRESSES+=("${address}")
            TARGET_HAS_IPV6_DEFAULT_ROUTE="true"
        fi
    done <<< "${parsed_pod_addresses}"
    if [ -n "${K3S_CANDIDATE_POD_IPV6_ADDRESSES:-}" ]; then
        local -a candidate_v6_list=()
        IFS=',' read -r -a candidate_v6_list <<< "${K3S_CANDIDATE_POD_IPV6_ADDRESSES}"
        for candidate_v6 in "${candidate_v6_list[@]}"; do
            [ -n "${candidate_v6}" ] || continue
            TARGET_IPV6_ADDRESSES+=("${candidate_v6}")
            TARGET_HAS_IPV6_DEFAULT_ROUTE="true"
        done
    fi
    if ! normalize_target_ipv6_addresses; then
        LAST_ERROR="k3s: ${LAST_ERROR}"
        return 1
    fi

    if [ -z "${pod_name}" ] || [ -z "${pod_ip}" ] || [ -z "${pod_node}" ]; then
        LAST_ERROR="k3s: ${impl^^} pod metadata incomplete (namespace=${namespace}, pod=${pod_name:-unknown}, pod_ip=${pod_ip:-missing}, node=${pod_node:-missing})"
        log ERROR "${LAST_ERROR}"
        return 1
    fi

    if [ "${pod_node}" != "${TUNNELSATS_K8S_NODE_NAME}" ]; then
        LAST_ERROR="k3s: Pod co-location required; TunnelSats node=${TUNNELSATS_K8S_NODE_NAME}, ${impl^^} pod=${namespace}/${pod_name} node=${pod_node}. Check podAffinity in k3s/deployment.yaml or node scheduling labels."
        log ERROR "${LAST_ERROR}"
        return 1
    fi

    load_managed_k3s_policy_state || true
    if [ -n "${MANAGED_K3S_POD_NAMESPACE:-}" ] && [ -n "${MANAGED_K3S_POD_SELECTOR:-}" ] && \
       { [ "${MANAGED_K3S_POD_NAMESPACE}" != "${namespace}" ] || \
         [ "${MANAGED_K3S_POD_SELECTOR}" != "${selector}" ]; }; then
        if ! remove_k3s_emergency_network_policy_for_target "${MANAGED_K3S_POD_NAMESPACE}" "${MANAGED_K3S_POD_SELECTOR}"; then
            log ERROR "${LAST_ERROR}"
            return 1
        fi
    fi

    DOCKER_TARGET_IP="${pod_ip}"
    K3S_TARGET_POD_NAMESPACE="${namespace}"
    K3S_TARGET_POD_NAME="${pod_name}"
    K3S_TARGET_POD_SELECTOR="${selector}"
    MANAGED_K3S_TARGET_IP="${pod_ip}"
    MANAGED_K3S_TARGET_IPV6_ADDRESSES=("${TARGET_IPV6_ADDRESSES[@]}")
    MANAGED_K3S_TARGET_IMPL="${impl}"
    MANAGED_K3S_POD_NAME="${pod_name}"
    MANAGED_K3S_POD_NAMESPACE="${namespace}"
    MANAGED_K3S_POD_SELECTOR="${selector}"
    K3S_POLICY_STATE_LOADED="true"
    persist_managed_k3s_policy_state || log WARN "${LAST_ERROR}"
    log INFO "k3s: Using co-located ${impl^^} pod ${namespace}/${pod_name} on node ${pod_node} at ${pod_ip}"
    return 0
}

resolve_configured_k3s_target_metadata() {
    local meta_node=""
    local impl_order=("lnd" "cln")
    local impl svc_name namespace selector

    if [ -f "${TUNNELSATS_META_PATH}" ]; then
        meta_node=$(jq -r '.nodeType // empty' "${TUNNELSATS_META_PATH}" 2>/dev/null | tr '[:upper:]' '[:lower:]')
    fi
    if [ "${meta_node}" = "cln" ] || { [ -z "${meta_node}" ] && [ "${MANAGED_K3S_TARGET_IMPL:-}" = "cln" ]; }; then
        impl_order=("cln" "lnd")
    fi

    for impl in "${impl_order[@]}"; do
        if [ "${impl}" = "cln" ]; then
            svc_name="${CLN_K8S_SERVICE:-}"
            namespace="${CLN_K8S_NAMESPACE:-}"
            selector="${CLN_K8S_POD_SELECTOR:-}"
        else
            svc_name="${LND_K8S_SERVICE:-}"
            namespace="${LND_K8S_NAMESPACE:-}"
            selector="${LND_K8S_POD_SELECTOR:-}"
        fi
        if [ -n "${svc_name}" ] && [ -n "${namespace}" ] && [ -n "${selector}" ]; then
            K3S_TARGET_POD_NAMESPACE="${namespace}"
            K3S_TARGET_POD_SELECTOR="${selector}"
            return 0
        fi
    done

    if [ -n "${MANAGED_K3S_POD_NAMESPACE:-}" ] && [ -n "${MANAGED_K3S_POD_SELECTOR:-}" ]; then
        K3S_TARGET_POD_NAMESPACE="${MANAGED_K3S_POD_NAMESPACE}"
        K3S_TARGET_POD_SELECTOR="${MANAGED_K3S_POD_SELECTOR}"
        return 0
    fi

    for impl in "${impl_order[@]}"; do
        if [ "${impl}" = "cln" ]; then
            namespace="${CLN_K8S_NAMESPACE:-}"
            selector="${CLN_K8S_POD_SELECTOR:-}"
        else
            namespace="${LND_K8S_NAMESPACE:-}"
            selector="${LND_K8S_POD_SELECTOR:-}"
        fi
        if [ -n "${namespace}" ] && [ -n "${selector}" ]; then
            K3S_TARGET_POD_NAMESPACE="${namespace}"
            K3S_TARGET_POD_SELECTOR="${selector}"
            return 0
        fi
    done

    return 1
}

pre_guard_colocated_k3s_candidate_pods_for_selector() {
    local namespace="$1"
    local selector="$2"
    local replace_mode="false"
    local encoded_selector pod_list saved_error="${LAST_ERROR:-}"

    [ -n "${namespace}" ] && [ -n "${selector}" ] || return 0
    [ -n "${TUNNELSATS_K8S_NODE_NAME:-}" ] || return 0

    encoded_selector="$(urlencode "${selector}")"
    if pod_list="$(k8s_api "/api/v1/namespaces/${namespace}/pods?labelSelector=${encoded_selector}" 2>/dev/null)" && \
       printf '%s' "${pod_list}" | jq -e '.items | type == "array"' >/dev/null 2>&1; then
        if [ "${K3S_POD_LIST_REPLACED:-false}" != "true" ]; then
            replace_mode="true"
            K3S_POD_LIST_REPLACED="true"
        fi
        if declare -F pre_guard_colocated_k3s_candidate_pods >/dev/null 2>&1; then
            if ! pre_guard_colocated_k3s_candidate_pods "${pod_list}" "${replace_mode}"; then
                K3S_POD_API_QUERY_OK="false"
                K3S_CANDIDATE_PODS_GUARDED="false"
                return 1
            fi
            if [ "${K3S_CANDIDATE_PODS_GUARDED:-true}" = "true" ]; then
                K3S_POD_API_QUERY_OK="true"
            fi
        else
            K3S_POD_API_QUERY_OK="true"
        fi
    fi
    LAST_ERROR="${saved_error}"
    return 0
}

detect_k3s_target() {
    load_k3s_pod_watch_state
    TARGET_CONTAINER_ID=""
    TARGET_CONTAINER_NAME=""
    TARGET_IMPL=""
    DOCKER_TARGET_IP=""
    TARGET_IPV6_ADDRESSES=()
    TARGET_IPV6_MACS=()
    TARGET_HAS_IPV6_DEFAULT_ROUTE="false"
    K3S_TARGET_POD_NAME=""
    K3S_TARGET_POD_NAMESPACE=""
    K3S_TARGET_POD_SELECTOR=""
    K3S_POD_API_QUERY_OK="false"
    K3S_POD_LIST_REPLACED="false"
    K3S_CANDIDATE_PODS_GUARDED="true"
    K3S_UNSEEN_REPLACEMENT_PENDING="${K3S_UNSEEN_REPLACEMENT_PENDING:-false}"
    K3S_CANDIDATE_POD_IPS="${K3S_WATCHED_CANDIDATE_POD_IPS:-}"
    K3S_CANDIDATE_POD_IPV6_ADDRESSES="${K3S_WATCHED_CANDIDATE_POD_IPV6_ADDRESSES:-}"
    K3S_ALL_MATCHING_LOCAL_POD_IPS="${K3S_WATCHED_CANDIDATE_POD_IPS:-}"

    resolve_configured_k3s_target_metadata || true

    local meta_node=""
    local impl_order=("lnd" "cln")
    local impl svc_name svc_fqdn svc_ip namespace selector target_port
    if [ -f "${TUNNELSATS_META_PATH}" ]; then
        meta_node=$(jq -r '.nodeType // empty' "${TUNNELSATS_META_PATH}" 2>/dev/null | tr '[:upper:]' '[:lower:]')
    fi
    if [ "${meta_node}" = "cln" ]; then
        impl_order=("cln" "lnd")
    fi

    for impl in "${impl_order[@]}"; do
        if [ "${impl}" = "cln" ]; then
            svc_name="${CLN_K8S_SERVICE}"
            namespace="${CLN_K8S_NAMESPACE}"
            selector="${CLN_K8S_POD_SELECTOR}"
            target_port="9736"
        else
            svc_name="${LND_K8S_SERVICE}"
            namespace="${LND_K8S_NAMESPACE}"
            selector="${LND_K8S_POD_SELECTOR}"
            target_port="9735"
        fi
        [ -n "${svc_name}" ] || continue

        K3S_TARGET_POD_NAMESPACE="${namespace}"
        K3S_TARGET_POD_SELECTOR="${selector}"

        svc_fqdn="${svc_name}.${namespace}.svc.cluster.local"
        svc_ip=$(resolve_svc_ip "${svc_fqdn}" "${svc_name}" || true)
        if [ -z "${svc_ip}" ]; then
            log WARN "k3s: Could not resolve ${impl^^} service ${svc_fqdn}"
            if ! pre_guard_colocated_k3s_candidate_pods_for_selector "${namespace}" "${selector}"; then
                log ERROR "${LAST_ERROR}"
                return 1
            fi
            continue
        fi

        TARGET_IMPL="${impl}"
        TARGET_CONTAINER_NAME="${svc_name}"
        LN_TARGET_PORT="${target_port}"
        log INFO "k3s: Detected ${impl^^} service ${svc_fqdn} at ClusterIP ${svc_ip}"
        # Use the actual pod IP for DNAT and policy routing to avoid asymmetric
        # routing caused by kube-proxy's double NAT through the ClusterIP.
        if ! resolve_k3s_target_pod "${impl}" "${namespace}" "${selector}"; then
            return 1
        fi
        return 0
    done

    resolve_configured_k3s_target_metadata || true
    LAST_ERROR="k3s: No LND/CLN service resolved (LND_K8S_SERVICE=${LND_K8S_SERVICE:-}, CLN_K8S_SERVICE=${CLN_K8S_SERVICE:-})"
    return 1
}

# Prints "<id>|<name>|<impl>" for the first running container of the given
# implementation (lnd or cln) in a /containers/json listing, or nothing.
pick_running_lightning_container() {
    local containers="$1"
    local impl="$2"
    local pattern='(^|[_-])lnd([_-]|$)'
    if [ "${impl}" = "cln" ]; then
        pattern='(^|[_-])(core-lightning|clightning|lightningd)([_-]|$)'
    fi
    printf '%s' "${containers}" | jq -r --arg pattern "${pattern}" --arg impl "${impl}" '
        def cname: (.Names[0] // "") | ltrimstr("/");
        [ .[]
          | {id: .Id, name: cname}
          | select(.name | test($pattern))
          | select(.name | test("(app|proxy|tor|web|ui)") | not)
        ]
        | if length > 0 then .[0] else empty end
        | "\(.id)|\(.name)|\($impl)"
    '
}

detect_lightning_container() {
    TARGET_CONTAINER_ID=""
    TARGET_CONTAINER_NAME=""
    TARGET_IMPL=""
    TARGET_IPV6_ADDRESSES=()
    TARGET_IPV6_MACS=()
    TARGET_HAS_IPV6_DEFAULT_ROUTE="false"

    if [[ "${K3S_MODE}" == "true" ]]; then
        detect_k3s_target || return 1
        return 0
    fi

    if [[ "${SECURE_MODE}" == "true" ]]; then
        local lnd_ok=0
        local cln_ok=0
        
        # 1. Active TCP probes first (indicates a running, listening node)
        if timeout 2 bash -c 'cat < /dev/null > /dev/tcp/10.21.21.9/9735' 2>/dev/null; then
            lnd_ok=1
        fi
        if timeout 2 bash -c 'cat < /dev/null > /dev/tcp/10.21.21.96/9736' 2>/dev/null; then
            cln_ok=1
        fi

        # Load metadata node type configuration if available to break ties
        local meta_node=""
        if [ -f "${TUNNELSATS_META_PATH}" ]; then
            meta_node=$(jq -r '.nodeType // empty' "${TUNNELSATS_META_PATH}" 2>/dev/null | tr '[:upper:]' '[:lower:]')
        fi

        # 2. Tie resolution if both TCP probes succeeded
        if [ "${lnd_ok}" -eq 1 ] && [ "${cln_ok}" -eq 1 ]; then
            if [ "${meta_node}" = "lnd" ]; then
                cln_ok=0
            elif [ "${meta_node}" = "cln" ]; then
                lnd_ok=0
            fi
        fi

        if [ "${lnd_ok}" -eq 1 ]; then
            TARGET_IMPL="lnd"
            TARGET_CONTAINER_NAME="lightning_lnd_1"
            LN_TARGET_PORT="9735"
            DOCKER_TARGET_IP="10.21.21.9"
            log INFO "SecureMode: Detected LND node at ${DOCKER_TARGET_IP}"
            discover_secure_ipv6_target || return 1
            return 0
        elif [ "${cln_ok}" -eq 1 ]; then
            TARGET_IMPL="cln"
            TARGET_CONTAINER_NAME="lightning_cln_1"
            LN_TARGET_PORT="9736"
            DOCKER_TARGET_IP="10.21.21.96"
            log INFO "SecureMode: Detected CLN node at ${DOCKER_TARGET_IP}"
            discover_secure_ipv6_target || return 1
            return 0
        fi
        LAST_ERROR="SecureMode: No Lightning node detected (probed 10.21.21.9 and 10.21.21.96)"
        return 1
    fi

    local meta_node=""
    if [ -f "${TUNNELSATS_META_PATH}" ]; then
        meta_node=$(jq -r '.nodeType // empty' "${TUNNELSATS_META_PATH}" 2>/dev/null | tr '[:upper:]' '[:lower:]')
    fi

    local containers
    containers=$(docker_api "GET" "/containers/json?all=0") || {
        LAST_ERROR="Unable to list running Docker containers"
        return 1
    }

    # Standard Mode never switches the protected implementation on its own:
    # umbrelOS removes and recreates the Lightning container during an app
    # restart, and protecting the other implementation in that gap would
    # retire the previous node's policy rules. The selected node type wins;
    # without one, the last protected implementation is kept. Only a first
    # start without either uses the LND-first discovery order.
    local chosen_impl=""
    case "${meta_node}" in
        lnd|cln) chosen_impl="${meta_node}" ;;
    esac
    if [ -z "${chosen_impl}" ]; then
        if ! load_protected_target_state; then
            return 1
        fi
        chosen_impl="${PROTECTED_TARGET_IMPL}"
    fi

    local pick=""
    if [ -n "${chosen_impl}" ]; then
        pick=$(pick_running_lightning_container "${containers}" "${chosen_impl}")
        if [ -z "${pick}" ]; then
            # Fail closed through the failed-detection path, which keeps the
            # fallback blackholes, until the chosen node runs again.
            local waiting_for
            waiting_for="$(lightning_impl_label "${chosen_impl}")"
            if [ "${PROTECTED_TARGET_IMPL}" = "${chosen_impl}" ] && [ -n "${PROTECTED_TARGET_CONTAINER}" ]; then
                waiting_for+=" (${PROTECTED_TARGET_CONTAINER})"
            fi
            TARGET_IMPL="${chosen_impl}"
            LAST_ERROR="Waiting for the protected node to start: ${waiting_for}"
            return 1
        fi
    else
        pick=$(pick_running_lightning_container "${containers}" "lnd")
        if [ -z "${pick}" ]; then
            pick=$(pick_running_lightning_container "${containers}" "cln")
        fi
    fi

    if [ -z "${pick}" ]; then
        return 1
    fi

    TARGET_CONTAINER_ID="${pick%%|*}"
    local rest="${pick#*|}"
    TARGET_CONTAINER_NAME="${rest%%|*}"
    TARGET_IMPL="${rest##*|}"
    DOCKER_TARGET_IP="${DOCKER_TARGET_IP:-10.9.9.9}"

    if [ "${TARGET_IMPL}" = "cln" ]; then
        LN_TARGET_PORT="9736"
    else
        LN_TARGET_PORT="9735"
    fi

    return 0
}

ensure_docker_network() {
    [[ "${K3S_MODE}" == "true" ]] && return 0
    [[ "${SECURE_MODE}" == "true" ]] && return 0
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
    if [[ "${K3S_MODE}" == "true" ]] || [[ "${SECURE_MODE}" == "true" ]]; then
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

# Only the protected target may use docker-tunnelsats. A previous target (for
# example after the user switched implementations) or an orphaned endpoint
# would otherwise keep DOCKER_TARGET_IP, and Docker would refuse the connect.
# Endpoints are detached by name with Force, which also removes endpoints
# whose container no longer exists.
detach_other_docker_network_endpoints() {
    local entries
    local entry_key
    local endpoint_name
    local endpoint_ip
    local detach_name

    if ! docker_api_request "GET" "/networks/${DOCKER_NETWORK_NAME}"; then
        LAST_ERROR="Unable to inspect ${DOCKER_NETWORK_NAME} endpoints: ${DOCKER_API_ERROR}"
        return 1
    fi
    if ! entries=$(printf '%s' "${DOCKER_API_BODY}" | jq -r --arg target "${TARGET_CONTAINER_ID}" '
        (.Containers // {})
        | to_entries[]
        | select(.key != $target)
        | [.key, (.value.Name // ""), ((.value.IPv4Address // "") | sub("/.*$"; ""))]
        | join("|")
    '); then
        LAST_ERROR="Unable to parse ${DOCKER_NETWORK_NAME} endpoints"
        return 1
    fi

    while IFS='|' read -r entry_key endpoint_name endpoint_ip; do
        [ -n "${entry_key}" ] || continue
        detach_name="${endpoint_name:-${entry_key}}"
        log INFO "Detaching ${detach_name} from ${DOCKER_NETWORK_NAME} (IP=${endpoint_ip:-NONE}); only ${TARGET_CONTAINER_NAME} may use it"
        if docker_api_request "POST" "/networks/${DOCKER_NETWORK_NAME}/disconnect" \
            "$(jq -cn --arg c "${detach_name}" '{Container:$c, Force:true}')"; then
            continue
        fi
        if docker_api_error_is_not_connected; then
            log INFO "${detach_name} is already detached from ${DOCKER_NETWORK_NAME}"
            continue
        fi
        if [ "${endpoint_ip}" = "${DOCKER_TARGET_IP}" ]; then
            LAST_ERROR="${DOCKER_TARGET_IP} on ${DOCKER_NETWORK_NAME} is held by ${detach_name}, which could not be detached: ${DOCKER_API_ERROR}"
        else
            LAST_ERROR="Failed to detach ${detach_name} from ${DOCKER_NETWORK_NAME}: ${DOCKER_API_ERROR}"
        fi
        return 1
    done <<< "${entries}"
}

ensure_container_attached() {
    [[ "${K3S_MODE}" == "true" ]] && return 0
    [[ "${SECURE_MODE}" == "true" ]] && return 0
    if ! detect_docker_gw_priority_support; then
        return 1
    fi

    local inspect
    inspect=$(docker_api "GET" "/containers/${TARGET_CONTAINER_ID}/json") || {
        LAST_ERROR="Failed to inspect ${TARGET_CONTAINER_NAME}"
        return 1
    }

    # Docker records the network on the container before it creates the
    # endpoint and keeps that entry when the connect fails, for example while
    # another container holds DOCKER_TARGET_IP. Only an entry with an
    # EndpointID is attached; a stale entry goes straight to connect.
    local has_entry
    has_entry=$(printf '%s' "${inspect}" | jq -r --arg net "${DOCKER_NETWORK_NAME}" '.NetworkSettings.Networks[$net] != null')
    local endpoint_id
    endpoint_id=$(printf '%s' "${inspect}" | jq -r --arg net "${DOCKER_NETWORK_NAME}" '.NetworkSettings.Networks[$net].EndpointID // empty')
    local attached="false"
    if [ "${has_entry}" = "true" ] && [ -n "${endpoint_id}" ]; then
        attached="true"
    fi
    local current_ip
    current_ip=$(echo "${inspect}" | jq -r --arg net "${DOCKER_NETWORK_NAME}" '.NetworkSettings.Networks[$net].IPAddress // empty')
    local current_priority=0
    local desired_priority=0
    local other_max_priority=0

    if [ "${DOCKER_GW_PRIORITY_SUPPORTED}" = "true" ]; then
        current_priority=$(printf '%s' "${inspect}" | jq -r --arg net "${DOCKER_NETWORK_NAME}" \
            '.NetworkSettings.Networks[$net].GwPriority // 0')
        other_max_priority=$(printf '%s' "${inspect}" | jq -r --arg net "${DOCKER_NETWORK_NAME}" \
            '[.NetworkSettings.Networks | to_entries[] | select(.key != $net) | (.value.GwPriority // 0)] | max // 0')
        if [[ ! "${current_priority}" =~ ^-?[0-9]+$ ]] || [[ ! "${other_max_priority}" =~ ^-?[0-9]+$ ]]; then
            LAST_ERROR="Docker returned an invalid endpoint gateway priority"
            return 1
        fi
        if [ "${other_max_priority}" -ge 2147483647 ]; then
            LAST_ERROR="Cannot assign ${DOCKER_NETWORK_NAME} a priority above competing endpoint ${other_max_priority}"
            return 1
        fi
        desired_priority=$((other_max_priority + 1))
        if [ "${desired_priority}" -lt "${DOCKER_MIN_GW_PRIORITY}" ]; then
            desired_priority="${DOCKER_MIN_GW_PRIORITY}"
        fi
    fi

    if [ "${attached}" = "true" ] && [ "${current_ip}" = "${DOCKER_TARGET_IP}" ] && \
       { [ "${DOCKER_GW_PRIORITY_SUPPORTED}" != "true" ] || [ "${current_priority}" -eq "${desired_priority}" ]; }; then
        refresh_docker_target_endpoints
        return 0
    fi

    if [ "${attached}" = "true" ]; then
        log INFO "Disconnecting ${TARGET_CONTAINER_NAME} from ${DOCKER_NETWORK_NAME} (IP=${current_ip:-NONE}, GwPriority=${current_priority})"
        if ! docker_api_request "POST" "/networks/${DOCKER_NETWORK_NAME}/disconnect" \
            "$(jq -cn --arg c "${TARGET_CONTAINER_ID}" '{Container:$c, Force:true}')"; then
            if ! docker_api_error_is_not_connected; then
                LAST_ERROR="Failed to disconnect stale ${DOCKER_NETWORK_NAME} endpoint from ${TARGET_CONTAINER_NAME}: ${DOCKER_API_ERROR}"
                return 1
            fi
            log INFO "${TARGET_CONTAINER_NAME} is already detached from ${DOCKER_NETWORK_NAME}"
        fi
    elif [ "${has_entry}" = "true" ]; then
        log INFO "${TARGET_CONTAINER_NAME} has a ${DOCKER_NETWORK_NAME} entry without an endpoint; reconnecting"
    fi

    if ! detach_other_docker_network_endpoints; then
        return 1
    fi

    local connect_payload
    if [ "${DOCKER_GW_PRIORITY_SUPPORTED}" = "true" ]; then
        connect_payload=$(jq -cn \
            --arg c "${TARGET_CONTAINER_ID}" \
            --arg ip "${DOCKER_TARGET_IP}" \
            --argjson priority "${desired_priority}" \
            '{Container:$c, EndpointConfig:{IPAMConfig:{IPv4Address:$ip}, GwPriority:$priority}}')
        log INFO "Connecting ${TARGET_CONTAINER_NAME} to ${DOCKER_NETWORK_NAME} (${DOCKER_TARGET_IP}, GwPriority=${desired_priority})"
    else
        connect_payload=$(jq -cn --arg c "${TARGET_CONTAINER_ID}" --arg ip "${DOCKER_TARGET_IP}" \
            '{Container:$c, EndpointConfig:{IPAMConfig:{IPv4Address:$ip}}}')
        log INFO "Connecting ${TARGET_CONTAINER_NAME} to ${DOCKER_NETWORK_NAME} (${DOCKER_TARGET_IP}, legacy Engine compatibility mode)"
    fi
    if ! docker_api_request "POST" "/networks/${DOCKER_NETWORK_NAME}/connect" "${connect_payload}"; then
        LAST_ERROR="Failed to connect ${TARGET_CONTAINER_NAME} to ${DOCKER_NETWORK_NAME}: ${DOCKER_API_ERROR}"
        return 1
    fi

    inspect=$(docker_api "GET" "/containers/${TARGET_CONTAINER_ID}/json") || {
        LAST_ERROR="Failed to verify ${DOCKER_NETWORK_NAME} attachment"
        return 1
    }
    endpoint_id=$(printf '%s' "${inspect}" | jq -r --arg net "${DOCKER_NETWORK_NAME}" \
        '.NetworkSettings.Networks[$net].EndpointID // empty')
    if [ -z "${endpoint_id}" ]; then
        LAST_ERROR="Docker did not create a ${DOCKER_NETWORK_NAME} endpoint for ${TARGET_CONTAINER_NAME}"
        return 1
    fi
    current_ip=$(printf '%s' "${inspect}" | jq -r --arg net "${DOCKER_NETWORK_NAME}" \
        '.NetworkSettings.Networks[$net].IPAddress // empty')
    if [ "${current_ip}" != "${DOCKER_TARGET_IP}" ]; then
        LAST_ERROR="${DOCKER_NETWORK_NAME} attachment did not receive ${DOCKER_TARGET_IP}"
        return 1
    fi
    if [ "${DOCKER_GW_PRIORITY_SUPPORTED}" = "true" ]; then
        current_priority=$(printf '%s' "${inspect}" | jq -r --arg net "${DOCKER_NETWORK_NAME}" \
            '.NetworkSettings.Networks[$net].GwPriority // 0')
        if [ "${current_priority}" != "${desired_priority}" ]; then
            LAST_ERROR="Docker did not apply ${DOCKER_NETWORK_NAME} GwPriority=${desired_priority}"
            return 1
        fi
    fi

    refresh_docker_target_endpoints
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
    if ! ensure_runtime_wireguard_keepalive "${WG_CONF_PATH}"; then
        LAST_ERROR="Failed to ensure PersistentKeepalive in runtime WireGuard config"
        return 1
    fi

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

    IPTABLES_RULES_REMOVED="0"
    local rules
    rules=$(iptables -t "${table}" -S "${chain}" | grep "${marker}" || true)
    if [ -z "${rules}" ]; then
        return 0
    fi
    IPTABLES_RULES_REMOVED="1"

    while IFS= read -r rule; do
        [ -z "${rule}" ] && continue
        local del
        del=$(echo "${rule}" | sed -e 's/^-A /-D /' -e 's/^-I /-D /')
        iptables -t "${table}" ${del} >/dev/null 2>&1 || true
    done <<EOF_RULES
${rules}
EOF_RULES
}

ensure_k3s_egress_guard_additive() {
    local source_ip="${1%/32}"
    local marker="tunnelsats-k3s-egress-guard"

    [ -n "${source_ip}" ] || return 1
    if ! iptables -C FORWARD -s "${source_ip}" \
        -m comment --comment "${marker}" -j DROP >/dev/null 2>&1; then
        if ! iptables -I FORWARD 1 -s "${source_ip}" \
            -m comment --comment "${marker}" -j DROP >/dev/null 2>&1; then
            return 1
        fi
    fi

    if ! iptables -C FORWARD -s "${source_ip}" \
        -m comment --comment "${marker}" -j DROP >/dev/null 2>&1; then
        return 1
    fi
    return 0
}

ensure_k3s_egress_guard() {
    local source_ip="$1"

    ensure_k3s_egress_guard_additive "${source_ip}" || return 1

    # Once the current pod is independently guarded, promptly release tagged
    # guards for historical pod IPs. Otherwise Kubernetes can recycle an old
    # address to an unrelated workload while a failed reconciliation persists.
    while ! remove_stale_k3s_egress_guards "${source_ip}"; do
        log WARN "k3s: Retrying stale egress-guard cleanup"
        sleep "${K3S_ISOLATION_RETRY_INTERVAL}"
    done
    return 0
}

remove_stale_k3s_egress_guards() {
    local current_source="${1%/32}"
    local marker="tunnelsats-k3s-egress-guard"
    local rules
    local rule
    local source
    local index
    local -a parts

    if ! k3s_acquire_watch_state_lock; then
        return 1
    fi
    if ! sync_k3s_watched_candidate_addresses; then
        k3s_release_watch_state_lock
        return 1
    fi

    rules="$(iptables -S FORWARD 2>/dev/null | grep -F -- "--comment ${marker}" || true)"
    while IFS= read -r rule; do
        [ -n "${rule}" ] || continue
        read -r -a parts <<< "${rule}"
        source=""
        for ((index = 0; index < ${#parts[@]} - 1; index++)); do
            if [ "${parts[index]}" = "-s" ]; then
                source="${parts[index + 1]}"
                break
            fi
        done
        if [ "${source%/32}" = "${current_source}" ] || \
           k3s_ip_is_in_csv "${source%/32}" "${K3S_CANDIDATE_POD_IPS:-}"; then
            continue
        fi
        parts[0]="-D"
        if ! iptables "${parts[@]}" >/dev/null 2>&1; then
            k3s_release_watch_state_lock
            return 1
        fi
    done <<< "${rules}"

    rules="$(iptables -S FORWARD 2>/dev/null | grep -F -- "--comment ${marker}" || true)"
    while IFS= read -r rule; do
        [ -n "${rule}" ] || continue
        read -r -a parts <<< "${rule}"
        source=""
        for ((index = 0; index < ${#parts[@]} - 1; index++)); do
            if [ "${parts[index]}" = "-s" ]; then
                source="${parts[index + 1]}"
                break
            fi
        done
        if [ "${source%/32}" != "${current_source}" ] && \
           ! k3s_ip_is_in_csv "${source%/32}" "${K3S_CANDIDATE_POD_IPS:-}"; then
            k3s_release_watch_state_lock
            return 1
        fi
    done <<< "${rules}"
    k3s_release_watch_state_lock
    return 0
}

remove_k3s_egress_guards() {
    local preserve_candidates="${1:-false}"
    local marker="tunnelsats-k3s-egress-guard"
    local rules rule source index
    local -a parts

    if [ "${preserve_candidates}" = "true" ]; then
        if ! k3s_acquire_watch_state_lock; then
            LAST_ERROR="k3s: Failed to acquire pod watch state lock"
            return 1
        fi
        if ! sync_k3s_watched_candidate_addresses; then
            k3s_release_watch_state_lock
            return 1
        fi
    fi

    if [ "${preserve_candidates}" = "true" ] && [ -n "${K3S_CANDIDATE_POD_IPS:-}" ]; then
        rules="$(iptables -S FORWARD 2>/dev/null | grep -F -- "--comment ${marker}" || true)"
        while IFS= read -r rule; do
            [ -n "${rule}" ] || continue
            read -r -a parts <<< "${rule}"
            source=""
            for ((index = 0; index < ${#parts[@]} - 1; index++)); do
                if [ "${parts[index]}" = "-s" ]; then
                    source="${parts[index + 1]%/32}"
                    break
                fi
            done
            if [ -n "${source}" ] && [ "${source}" != "${DOCKER_TARGET_IP:-}" ] && \
               k3s_ip_is_in_csv "${source}" "${K3S_CANDIDATE_POD_IPS:-}"; then
                continue
            fi
            parts[0]="-D"
            iptables "${parts[@]}" >/dev/null 2>&1 || {
                k3s_release_watch_state_lock
                LAST_ERROR="k3s: Failed to remove temporary egress guard"
                return 1
            }
        done <<< "${rules}"
        rules="$(iptables -S FORWARD 2>/dev/null | grep -F -- "--comment ${marker}" || true)"
        while IFS= read -r rule; do
            [ -n "${rule}" ] || continue
            read -r -a parts <<< "${rule}"
            source=""
            for ((index = 0; index < ${#parts[@]} - 1; index++)); do
                if [ "${parts[index]}" = "-s" ]; then
                    source="${parts[index + 1]%/32}"
                    break
                fi
            done
            if [ -z "${source}" ] || [ "${source}" = "${DOCKER_TARGET_IP:-}" ] || \
               ! k3s_ip_is_in_csv "${source}" "${K3S_CANDIDATE_POD_IPS:-}"; then
                k3s_release_watch_state_lock
                LAST_ERROR="k3s: Failed to remove temporary egress guard"
                return 1
            fi
        done <<< "${rules}"
        k3s_release_watch_state_lock
        return 0
    fi

    remove_tagged_iptables_rules filter FORWARD "${marker}"
    if iptables -S FORWARD 2>/dev/null | grep -Fq -- "${marker}"; then
        if [ "${preserve_candidates}" = "true" ]; then
            k3s_release_watch_state_lock
        fi
        LAST_ERROR="k3s: Failed to remove temporary egress guard"
        return 1
    fi
    if [ "${preserve_candidates}" = "true" ]; then
        k3s_release_watch_state_lock
    fi
    return 0
}

retire_stale_k3s_unmatched_ip_guards() {
    [ "${K3S_POD_API_QUERY_OK:-false}" = "true" ] || return 0
    local marker="tunnelsats-k3s-egress-guard"
    local rules rule source index rule_line rule_spec rule_source
    local -a parts

    if ! k3s_acquire_watch_state_lock; then
        return 1
    fi
    if ! sync_k3s_watched_candidate_addresses; then
        k3s_release_watch_state_lock
        return 1
    fi

    rules="$(iptables -S FORWARD 2>/dev/null | grep -F -- "--comment ${marker}" || true)"
    while IFS= read -r rule; do
        [ -n "${rule}" ] || continue
        read -r -a parts <<< "${rule}"
        source=""
        for ((index = 0; index < ${#parts[@]} - 1; index++)); do
            if [ "${parts[index]}" = "-s" ]; then
                source="${parts[index + 1]%/32}"
                break
            fi
        done
        [ -n "${source}" ] || continue
        if [ "${source}" = "${DOCKER_TARGET_IP:-}" ] || \
           k3s_ip_is_in_csv "${source}" "${K3S_ALL_MATCHING_LOCAL_POD_IPS:-}" || \
           k3s_ip_is_in_csv "${source}" "${K3S_CANDIDATE_POD_IPS:-}"; then
            continue
        fi
        parts[0]="-D"
        iptables "${parts[@]}" >/dev/null 2>&1 || true
    done <<< "${rules}"

    while IFS= read -r rule_line; do
        [ -n "${rule_line}" ] || continue
        rule_spec="${rule_line#*:}"
        [[ "${rule_spec}" == *" blackhole"* ]] || continue
        k3s_policy_rule_is_owned "${rule_line}" || continue
        rule_source="$(awk '{for (i = 1; i <= NF; i++) if ($i == "from") {print $(i + 1); exit}}' <<< "${rule_spec}")"
        [ -n "${rule_source}" ] || continue
        if [[ "${rule_source}" == */* ]] && [[ "${rule_source}" != */32 ]]; then
            continue
        fi
        rule_source="${rule_source%/32}"
        if [ "${rule_source}" = "${DOCKER_TARGET_IP:-}" ] || \
           k3s_ip_is_in_csv "${rule_source}" "${K3S_ALL_MATCHING_LOCAL_POD_IPS:-}" || \
           k3s_ip_is_in_csv "${rule_source}" "${K3S_CANDIDATE_POD_IPS:-}"; then
            continue
        fi
        delete_policy_rule_line "${rule_line}" || true
    done < <(ip rule show pref "${K3S_BLACKHOLE_RULE_PREF}" 2>/dev/null || true)
    k3s_release_watch_state_lock
    return 0
}

k3s_node_has_ipv6_pod_route() {
    local route_lines
    route_lines="$(ip -6 route show 2>/dev/null || true)"
    [ -n "${route_lines}" ] || return 1
    ROUTE_LINES="${route_lines}" python3 -c '
import ipaddress
import os

default_pod_nets = (
    ipaddress.ip_network("2001:cafe:42::/56"),
    ipaddress.ip_network("fd00:10:42::/56"),
)
cni_prefixes = ("cni", "flannel", "cilium", "cali", "kube", "ovn", "weave", "antrea", "pod-r")

for line in os.environ.get("ROUTE_LINES", "").splitlines():
    parts = line.split()
    if not parts:
        continue
    if parts[0] in {
        "local",
        "broadcast",
        "multicast",
        "throw",
        "unreachable",
        "prohibit",
        "blackhole",
        "nat",
        "anycast",
    }:
        continue
    idx = 1 if parts[0] == "unicast" else 0
    if idx >= len(parts):
        continue
    prefix_token = parts[idx]
    if prefix_token == "default" or "/" not in prefix_token:
        continue
    try:
        net = ipaddress.ip_network(prefix_token, strict=False)
    except ValueError:
        continue
    if net.version != 6 or net.prefixlen <= 0 or net.prefixlen >= 128:
        continue
    if net.is_loopback or net.is_link_local or net.is_multicast or net.is_unspecified:
        continue
    dev = ""
    if "dev" in parts:
        dev_idx = parts.index("dev")
        if dev_idx + 1 < len(parts):
            dev = parts[dev_idx + 1]
    if dev.startswith(cni_prefixes) or any(net.subnet_of(pod_net) for pod_net in default_pod_nets):
        raise SystemExit(0)
raise SystemExit(1)
' >/dev/null 2>&1
}

k3s_ipv6_is_active() {
    if [ "${K3S_MODE}" != "true" ]; then
        return 1
    fi
    if ! load_managed_ipv6_policy_state; then
        return 0
    fi
    if [ "${#TARGET_IPV6_ADDRESSES[@]}" -gt 0 ] || \
       [ "${#MANAGED_TARGET_IPV6_ADDRESSES[@]}" -gt 0 ] || \
       [ "${#MANAGED_K3S_TARGET_IPV6_ADDRESSES[@]}" -gt 0 ] || \
       [ "${IPV6_POLICY_TABLE_MANAGED}" = "true" ] || \
       [ "${TARGET_HAS_IPV6_DEFAULT_ROUTE}" = "true" ] || \
       [ -n "${K3S_IPV6_POD_CIDRS:-}" ] || \
       [ -n "${K3S_IPV6_BYPASS_CIDRS:-}" ] || \
       [ -n "${K3S_IPV6_QUARANTINE_CIDR:-}" ] || \
       k3s_node_has_ipv6_pod_route; then
        return 0
    fi
    return 1
}

delete_ipv6_policy_rule_line() {
    local rule_line="$1"
    local priority="${rule_line%%:*}"
    local rule_spec="${rule_line#*:}"
    local -a rule_parts

    read -r -a rule_parts <<< "${rule_spec}"
    [ "${#rule_parts[@]}" -gt 0 ] || return 0
    ip -6 rule del "${rule_parts[@]}" pref "${priority}" >/dev/null 2>&1 || true
}

resolve_k3s_ipv6_pod_cidrs() {
    local raw_cidrs="${K3S_IPV6_POD_CIDRS:-${K3S_IPV6_BYPASS_CIDRS:-}}"
    local route_lines=""
    route_lines="$(ip -6 route show 2>/dev/null || true)"

    local target_ipv6_csv
    target_ipv6_csv="$(printf '%s\n' \
        "${TARGET_IPV6_ADDRESSES[@]}" \
        "${MANAGED_TARGET_IPV6_ADDRESSES[@]}" \
        "${MANAGED_K3S_TARGET_IPV6_ADDRESSES[@]}" \
        | awk 'NF && !seen[$0]++' | paste -sd, -)"

    local matched_cidrs
    if ! matched_cidrs="$(
        TARGET_IPV6_CSV="${target_ipv6_csv}" \
        RAW_IPV6_CIDRS="${raw_cidrs}" \
        ROUTE_LINES="${route_lines}" \
        python3 -c '
import ipaddress
import os

raw_cidrs = [item.strip() for item in os.environ.get("RAW_IPV6_CIDRS", "").split(",") if item.strip()]
raw_targets = [item.strip() for item in os.environ.get("TARGET_IPV6_CSV", "").split(",") if item.strip()]
route_lines = os.environ.get("ROUTE_LINES", "").splitlines()
default_pod_nets = [
    ipaddress.ip_network("2001:cafe:42::/56"),
    ipaddress.ip_network("fd00:10:42::/56"),
]
cni_prefixes = ("cni", "flannel", "cilium", "cali", "kube", "ovn", "weave", "antrea", "pod-r")

configured_networks = []
for value in raw_cidrs:
    try:
        net = ipaddress.ip_network(value, strict=False)
    except ValueError as exc:
        raise SystemExit(f"invalid IPv6 CIDR {value!r}: {exc}")
    if net.version != 6:
        raise SystemExit(f"{value!r} is not an IPv6 CIDR")
    if net.prefixlen <= 0 or net.prefixlen >= 128:
        raise SystemExit(f"IPv6 pod CIDR {value!r} must be a subnet prefix between /1 and /127")
    if net.is_loopback or net.is_link_local or net.is_multicast or net.is_unspecified:
        raise SystemExit(f"IPv6 pod CIDR {value!r} is not a routable pod subnet")
    if net not in configured_networks:
        configured_networks.append(net)

route_networks = []
cni_route_networks = []
for line in route_lines:
    parts = line.split()
    if not parts:
        continue
    if parts[0] in {
        "local",
        "broadcast",
        "multicast",
        "throw",
        "unreachable",
        "prohibit",
        "blackhole",
        "nat",
        "anycast",
    }:
        continue
    idx = 1 if parts[0] == "unicast" else 0
    if idx >= len(parts):
        continue
    prefix_token = parts[idx]
    if prefix_token == "default" or "/" not in prefix_token:
        continue
    try:
        net = ipaddress.ip_network(prefix_token, strict=False)
    except ValueError:
        continue
    if net.version != 6 or net.prefixlen <= 0 or net.prefixlen >= 128:
        continue
    if net.is_loopback or net.is_link_local or net.is_multicast or net.is_unspecified:
        continue
    if net not in route_networks:
        route_networks.append(net)
    dev = ""
    if "dev" in parts:
        dev_idx = parts.index("dev")
        if dev_idx + 1 < len(parts):
            dev = parts[dev_idx + 1]
    if dev.startswith(cni_prefixes) or any(net.subnet_of(pod_net) for pod_net in default_pod_nets):
        if net not in cni_route_networks:
            cni_route_networks.append(net)

targets = []
for value in raw_targets:
    try:
        addr = ipaddress.IPv6Address(value)
    except ValueError as exc:
        raise SystemExit(f"invalid target IPv6 address {value!r}: {exc}")
    if addr not in targets:
        targets.append(addr)

selected = []
if targets:
    for addr in targets:
        matches = [net for net in configured_networks if addr in net]
        if not matches:
            matches = [net for net in route_networks if addr in net]
        if not matches and not configured_networks:
            matches = [net for net in default_pod_nets if addr in net]
        if not matches:
            raise SystemExit(f"no IPv6 pod CIDR covers {addr}")
        best = max(matches, key=lambda item: item.prefixlen)
        if best not in selected:
            selected.append(best)
else:
    if cni_route_networks:
        for route_net in cni_route_networks:
            matches = [net for net in configured_networks if route_net.subnet_of(net)]
            best = max(matches, key=lambda item: item.prefixlen) if matches else route_net
            if best not in selected:
                selected.append(best)
    elif configured_networks:
        selected = list(configured_networks)
    else:
        raise SystemExit("missing IPv6 pod CIDR")

for net in selected:
    print(net)
' 2>/dev/null
    )" || [ -z "${matched_cidrs}" ]; then
        LAST_ERROR="k3s: Cannot determine IPv6 pod CIDR for emergency quarantine"
        return 1
    fi
    printf '%s\n' "${matched_cidrs}"
    return 0
}

ensure_k3s_ipv6_subnet_quarantine() {
    if ! k3s_ipv6_is_active; then
        K3S_IPV6_QUARANTINE_CIDR=""
        return 0
    fi
    if ! load_managed_ipv6_policy_state; then
        return 1
    fi

    local matched_cidrs
    if ! matched_cidrs="$(resolve_k3s_ipv6_pod_cidrs)" || [ -z "${matched_cidrs}" ]; then
        LAST_ERROR="k3s: Cannot determine IPv6 pod CIDR for emergency quarantine"
        return 1
    fi

    if ! command -v ip6tables >/dev/null 2>&1; then
        LAST_ERROR="k3s: ip6tables is unavailable for emergency IPv6 pod-CIDR quarantine"
        return 1
    fi

    local ipv6_cidr
    local exact_pattern
    local quarantine_rule
    local -a quarantined_list=()
    while IFS= read -r ipv6_cidr; do
        [ -n "${ipv6_cidr}" ] || continue
        if ! ip6tables -C FORWARD -s "${ipv6_cidr}" ! -d fe80::/10 \
            -m comment --comment "${K3S_IPV6_QUARANTINE_MARKER}" -j DROP >/dev/null 2>&1; then
            if ! ip6tables -I FORWARD 1 -s "${ipv6_cidr}" ! -d fe80::/10 \
                -m comment --comment "${K3S_IPV6_QUARANTINE_MARKER}" -j DROP >/dev/null 2>&1; then
                LAST_ERROR="k3s: Failed to install emergency IPv6 pod-CIDR filter quarantine"
                return 1
            fi
        fi
        if ! ip6tables -C FORWARD -s "${ipv6_cidr}" ! -d fe80::/10 \
            -m comment --comment "${K3S_IPV6_QUARANTINE_MARKER}" -j DROP >/dev/null 2>&1; then
            LAST_ERROR="k3s: Emergency IPv6 pod-CIDR filter quarantine was not installed"
            return 1
        fi

        exact_pattern="^${K3S_QUARANTINE_RULE_PREF}:[[:space:]]+from[[:space:]]+${ipv6_cidr}[[:space:]]+blackhole([[:space:]].*)?[[:space:]]+proto(col)?[[:space:]]+${K3S_RULE_PROTOCOL}[[:space:]]*$"
        quarantine_rule="$(ip -6 rule show pref "${K3S_QUARANTINE_RULE_PREF}" 2>/dev/null | grep -E "${exact_pattern}" | head -n 1 || true)"
        if [ -n "${quarantine_rule}" ]; then
            if ! k3s_policy_rule_is_owned "${quarantine_rule}"; then
                LAST_ERROR="k3s: Emergency IPv6 quarantine preference is occupied by an unowned rule"
                return 1
            fi
        else
            if ! ip -6 rule add from "${ipv6_cidr}" blackhole protocol "${K3S_RULE_PROTOCOL}" pref "${K3S_QUARANTINE_RULE_PREF}" >/dev/null 2>&1; then
                LAST_ERROR="k3s: Failed to install emergency IPv6 pod-CIDR quarantine"
                return 1
            fi
            quarantine_rule="$(ip -6 rule show pref "${K3S_QUARANTINE_RULE_PREF}" 2>/dev/null | grep -E "${exact_pattern}" | head -n 1 || true)"
            if [ -z "${quarantine_rule}" ] || ! k3s_policy_rule_is_owned "${quarantine_rule}"; then
                LAST_ERROR="k3s: Emergency IPv6 pod-CIDR quarantine was not installed"
                return 1
            fi
        fi
        quarantined_list+=("${ipv6_cidr}")
    done <<< "${matched_cidrs}"

    K3S_IPV6_QUARANTINE_CIDR="$(IFS=,; printf '%s' "${quarantined_list[*]}")"
    return 0
}

remove_k3s_ipv6_subnet_quarantine() {
    local rule_line
    local rule_spec
    local rule_source
    local quarantine_pref="${K3S_QUARANTINE_RULE_PREF}"
    local rules
    local rule
    local -a parts

    while IFS= read -r rule_line; do
        [ -n "${rule_line}" ] || continue
        rule_spec="${rule_line#*:}"
        [[ "${rule_spec}" == *"blackhole"* ]] || continue
        k3s_policy_rule_is_owned "${rule_line}" || continue
        rule_source="$(awk '{for (i = 1; i <= NF; i++) if ($i == "from") {print $(i + 1); exit}}' <<< "${rule_spec}")"
        [ -n "${rule_source}" ] || continue
        delete_ipv6_policy_rule_line "${rule_line}"
    done < <(ip -6 rule show pref "${quarantine_pref}" 2>/dev/null || true)

    while IFS= read -r rule_line; do
        [ -n "${rule_line}" ] || continue
        rule_spec="${rule_line#*:}"
        [[ "${rule_spec}" == *"blackhole"* ]] || continue
        k3s_policy_rule_is_owned "${rule_line}" || continue
        rule_source="$(awk '{for (i = 1; i <= NF; i++) if ($i == "from") {print $(i + 1); exit}}' <<< "${rule_spec}")"
        [ -n "${rule_source}" ] || continue
        LAST_ERROR="k3s: Failed to remove emergency IPv6 pod-CIDR quarantine"
        return 1
    done < <(ip -6 rule show pref "${quarantine_pref}" 2>/dev/null || true)

    if command -v ip6tables >/dev/null 2>&1; then
        rules="$(ip6tables -S FORWARD 2>/dev/null | grep -F -- "--comment ${K3S_IPV6_QUARANTINE_MARKER}" || true)"
        while IFS= read -r rule; do
            [ -n "${rule}" ] || continue
            read -r -a parts <<< "${rule}"
            parts[0]="-D"
            if ! ip6tables "${parts[@]}" >/dev/null 2>&1; then
                LAST_ERROR="k3s: Failed to remove emergency IPv6 pod-CIDR filter quarantine"
                return 1
            fi
        done <<< "${rules}"
        if ip6tables -S FORWARD 2>/dev/null | grep -Fq -- "--comment ${K3S_IPV6_QUARANTINE_MARKER}"; then
            LAST_ERROR="k3s: Failed to remove emergency IPv6 pod-CIDR filter quarantine"
            return 1
        fi
    fi

    K3S_IPV6_QUARANTINE_CIDR=""
    return 0
}

resolve_k3s_pod_cidrs() {
    local source_ip="${1:-}"
    local matched_cidrs
    local route_lines=""

    if ! normalize_k3s_bypass_cidrs; then
        return 1
    fi
    if [ -z "${source_ip}" ] || [ "${source_ip}" = "10.9.9.9" ]; then
        route_lines="$(ip -4 route show 2>/dev/null || ip route show 2>/dev/null || true)"
    fi
    if ! matched_cidrs="$(
        SOURCE_IP="${source_ip}" \
        BYPASS_CIDRS="${K3S_BYPASS_CIDRS_NORMALIZED}" \
        CONFIGURED_POD_CIDRS="${K3S_POD_CIDRS:-}" \
        K8S_SERVICE_HOST="${KUBERNETES_SERVICE_HOST:-}" \
        ROUTE_LINES="${route_lines}" \
        python3 -c '
import ipaddress
import os

raw_source = os.environ.get("SOURCE_IP", "").strip()
raw_configured = [
    value.strip()
    for value in os.environ.get("CONFIGURED_POD_CIDRS", "").split(",")
    if value.strip()
]
k8s_service_host = os.environ.get("K8S_SERVICE_HOST", "").strip()
route_lines = os.environ.get("ROUTE_LINES", "").splitlines()
cni_prefixes = (
    "cni",
    "flannel",
    "cali",
    "cilium",
    "cbr",
    "lxc",
    "vxlan.calico",
    "ovn",
    "kube-bridge",
    "weave",
)
default_pod_net = ipaddress.ip_network("10.42.0.0/16")

configured_pod_networks = []
for value in raw_configured:
    try:
        net = ipaddress.ip_network(value, strict=False)
    except ValueError as exc:
        raise SystemExit(f"invalid IPv4 pod CIDR {value!r}: {exc}")
    if net.version != 4:
        raise SystemExit(f"IPv4 pod CIDR {value!r} is not an IPv4 network")
    if net.prefixlen <= 0 or net.prefixlen >= 32:
        raise SystemExit(f"IPv4 pod CIDR {value!r} must be a subnet prefix between /1 and /31")
    if net.is_loopback or net.is_link_local or net.is_multicast or net.is_unspecified:
        raise SystemExit(f"IPv4 pod CIDR {value!r} is not a routable pod subnet")
    if net not in configured_pod_networks:
        configured_pod_networks.append(net)

networks = [
    ipaddress.ip_network(value)
    for value in os.environ["BYPASS_CIDRS"].split(",")
    if value
]
if not networks and not configured_pod_networks:
    raise SystemExit("no bypass CIDRs")

service_host_ip = None
if k8s_service_host:
    try:
        addr = ipaddress.ip_address(k8s_service_host)
        if addr.version == 4:
            service_host_ip = addr
    except ValueError:
        pass

cni_route_networks = []
host_route_networks = []
for line in route_lines:
    parts = line.split()
    if not parts:
        continue
    if parts[0] in {
        "local",
        "broadcast",
        "multicast",
        "throw",
        "unreachable",
        "prohibit",
        "blackhole",
        "nat",
        "anycast",
    }:
        continue
    idx = 1 if parts[0] == "unicast" else 0
    if idx >= len(parts):
        continue
    prefix_token = parts[idx]
    if prefix_token == "default" or "/" not in prefix_token:
        continue
    try:
        net = ipaddress.ip_network(prefix_token, strict=False)
    except ValueError:
        continue
    if net.version != 4 or net.prefixlen <= 0 or net.prefixlen >= 32:
        continue
    if net.is_loopback or net.is_link_local or net.is_multicast or net.is_unspecified:
        continue
    dev = ""
    if "dev" in parts:
        dev_idx = parts.index("dev")
        if dev_idx + 1 < len(parts):
            dev = parts[dev_idx + 1]
    if dev.startswith(cni_prefixes) or net.subnet_of(default_pod_net):
        if net not in cni_route_networks:
            cni_route_networks.append(net)
    elif dev:
        if net not in host_route_networks:
            host_route_networks.append(net)

selected = []
if not raw_source or raw_source == "10.9.9.9":
    if configured_pod_networks:
        if cni_route_networks:
            for route_net in cni_route_networks:
                matches = [net for net in configured_pod_networks if route_net.subnet_of(net)]
                if matches:
                    best = max(matches, key=lambda item: item.prefixlen)
                    if best not in selected:
                        selected.append(best)
        if not selected:
            selected = list(configured_pod_networks)
    else:
        cni_matches = []
        for route_net in cni_route_networks:
            matches = [net for net in networks if route_net.subnet_of(net)]
            if matches:
                best = max(matches, key=lambda item: item.prefixlen)
                if best not in cni_matches:
                    cni_matches.append(best)
        if cni_matches:
            selected = cni_matches
        else:
            selected = [
                network for network in networks
                if str(network) != "10.43.0.0/16"
                and network.prefixlen < 32
                and (service_host_ip is None or service_host_ip not in network)
                and not any(
                    host_net == network or host_net.subnet_of(network) or network.subnet_of(host_net)
                    for host_net in host_route_networks
                )
            ]
else:
    source = ipaddress.ip_address(raw_source)
    candidate_pool = configured_pod_networks + [
        net for net in networks if net not in configured_pod_networks
    ]
    matches = [network for network in candidate_pool if source in network]
    if matches:
        selected.append(max(matches, key=lambda network: network.prefixlen))

for net in selected:
    print(net)
' 2>/dev/null
    )" || [ -z "${matched_cidrs}" ]; then
        LAST_ERROR="k3s: Cannot determine pod CIDR for emergency quarantine"
        return 1
    fi
    printf '%s\n' "${matched_cidrs}"
    return 0
}

ensure_k3s_subnet_quarantine() {
    local source_ip="${1:-}"
    local matched_cidrs
    local pod_cidr
    local escaped_cidr
    local exact_pattern
    local quarantine_rule

    if ! normalize_k3s_bypass_cidrs; then
        return 1
    fi
    if ! matched_cidrs="$(resolve_k3s_pod_cidrs "${source_ip}")" || [ -z "${matched_cidrs}" ]; then
        LAST_ERROR="k3s: Cannot determine pod CIDR for emergency quarantine"
        return 1
    fi

    local -a quarantined_list=()
    while IFS= read -r pod_cidr; do
        [ -n "${pod_cidr}" ] || continue
        # `ip rule show` renders IPv4 /32 sources without the prefix suffix.
        escaped_cidr="${pod_cidr%/32}"
        escaped_cidr="${escaped_cidr//./\\.}"
        exact_pattern="^${K3S_QUARANTINE_RULE_PREF}:[[:space:]]+from[[:space:]]+${escaped_cidr}[[:space:]]+blackhole([[:space:]].*)?[[:space:]]+proto(col)?[[:space:]]+${K3S_RULE_PROTOCOL}[[:space:]]*$"
        quarantine_rule="$(ip rule show pref "${K3S_QUARANTINE_RULE_PREF}" 2>/dev/null | grep -E "${exact_pattern}" | head -n 1 || true)"
        if [ -n "${quarantine_rule}" ]; then
            if ! k3s_policy_rule_is_owned "${quarantine_rule}"; then
                LAST_ERROR="k3s: Emergency quarantine preference is occupied by an unowned rule"
                return 1
            fi
        else
            if ! ip rule add from "${pod_cidr}" blackhole protocol "${K3S_RULE_PROTOCOL}" pref "${K3S_QUARANTINE_RULE_PREF}" >/dev/null 2>&1; then
                LAST_ERROR="k3s: Failed to install emergency pod-CIDR quarantine"
                return 1
            fi
            quarantine_rule="$(ip rule show pref "${K3S_QUARANTINE_RULE_PREF}" 2>/dev/null | grep -E "${exact_pattern}" | head -n 1 || true)"
            if [ -z "${quarantine_rule}" ] || ! k3s_policy_rule_is_owned "${quarantine_rule}"; then
                LAST_ERROR="k3s: Emergency pod-CIDR quarantine was not installed"
                return 1
            fi
        fi
        quarantined_list+=("${pod_cidr}")
    done <<< "${matched_cidrs}"

    K3S_QUARANTINE_CIDR="$(IFS=,; printf '%s' "${quarantined_list[*]}")"
    if ! ensure_k3s_ipv6_subnet_quarantine; then
        return 1
    fi
    return 0
}

resolve_k3s_non_target_pod_addresses() {
    local matched_cidrs="${1:-}"
    local ipv6_matched_cidrs="${2:-}"
    local node_field_selector=""
    local pod_list_json=""
    local ns_pod_list_json=""
    local target_selector_json=""
    local parsed_output family addr

    K3S_KNOWN_NON_TARGET_POD_IPS=""
    K3S_KNOWN_NON_TARGET_POD_IPV6_ADDRESSES=""
    K3S_KNOWN_NON_TARGET_POD_V6_PEERS=""

    if [ -n "${K3S_TARGET_POD_SELECTOR:-}" ]; then
        target_selector_json="$(k3s_network_policy_selector 2>/dev/null || true)"
    fi

    if [ -r "${K8S_SA_TOKEN_PATH:-}" ] || [ "$(grep -c 'Cannot read service account token' <<< "$(declare -f k8s_api 2>/dev/null)")" -eq 0 ]; then
        if [ -n "${TUNNELSATS_K8S_NODE_NAME:-}" ]; then
            node_field_selector="$(urlencode "spec.nodeName=${TUNNELSATS_K8S_NODE_NAME}")"
            pod_list_json="$(k8s_api "/api/v1/pods?fieldSelector=${node_field_selector}" 2>/dev/null || true)"
        else
            pod_list_json="$(k8s_api "/api/v1/pods" 2>/dev/null || true)"
        fi
        if ! printf '%s' "${pod_list_json}" | jq -e '.items | type == "array"' >/dev/null 2>&1; then
            pod_list_json=""
            if [ -n "${K3S_TARGET_POD_NAMESPACE:-}" ]; then
                if [ -n "${node_field_selector}" ]; then
                    ns_pod_list_json="$(k8s_api "/api/v1/namespaces/${K3S_TARGET_POD_NAMESPACE}/pods?fieldSelector=${node_field_selector}" 2>/dev/null || true)"
                fi
                if ! printf '%s' "${ns_pod_list_json}" | jq -e '.items | type == "array"' >/dev/null 2>&1; then
                    ns_pod_list_json="$(k8s_api "/api/v1/namespaces/${K3S_TARGET_POD_NAMESPACE}/pods" 2>/dev/null || true)"
                fi
                if printf '%s' "${ns_pod_list_json}" | jq -e '.items | type == "array"' >/dev/null 2>&1; then
                    pod_list_json="${ns_pod_list_json}"
                fi
            fi
        fi
    fi

    local excluded_v4_csv excluded_v6_csv
    excluded_v4_csv="$(printf '%s,%s,%s,%s,%s' \
        "${DOCKER_TARGET_IP:-}" \
        "${MANAGED_K3S_TARGET_IP:-}" \
        "${K3S_ALL_MATCHING_LOCAL_POD_IPS:-}" \
        "${K3S_CANDIDATE_POD_IPS:-}" \
        "${K3S_WATCHED_CANDIDATE_POD_IPS:-}")"
    excluded_v6_csv="$(printf '%s,%s,%s,%s,%s' \
        "$(IFS=,; printf '%s' "${TARGET_IPV6_ADDRESSES[*]:-}")" \
        "$(IFS=,; printf '%s' "${MANAGED_TARGET_IPV6_ADDRESSES[*]:-}")" \
        "$(IFS=,; printf '%s' "${MANAGED_K3S_TARGET_IPV6_ADDRESSES[*]:-}")" \
        "${K3S_CANDIDATE_POD_IPV6_ADDRESSES:-}" \
        "${K3S_WATCHED_CANDIDATE_POD_IPV6_ADDRESSES:-}")"

    parsed_output="$(
        POD_LIST_JSON="${pod_list_json}" \
        TARGET_NS="${K3S_TARGET_POD_NAMESPACE:-}" \
        TARGET_POD_NAME="${K3S_TARGET_POD_NAME:-}" \
        TARGET_SELECTOR_JSON="${target_selector_json}" \
        NODE_NAME="${TUNNELSATS_K8S_NODE_NAME:-}" \
        V4_CIDRS="${matched_cidrs}" \
        V6_CIDRS="${ipv6_matched_cidrs}" \
        EXCLUDED_V4="${excluded_v4_csv}" \
        EXCLUDED_V6="${excluded_v6_csv}" \
        EXPLICIT_V4="${K3S_NON_TARGET_POD_IPS:-}" \
        EXPLICIT_V6="${K3S_NON_TARGET_POD_IPV6_ADDRESSES:-}" \
        python3 -c '
import ipaddress
import json
import os

v4_nets = []
for raw in os.environ.get("V4_CIDRS", "").splitlines():
    raw = raw.strip()
    if not raw:
        continue
    try:
        net = ipaddress.ip_network(raw, strict=False)
        if net.version == 4:
            v4_nets.append(net)
    except ValueError:
        pass

v6_nets = []
for raw in os.environ.get("V6_CIDRS", "").splitlines():
    raw = raw.strip()
    if not raw:
        continue
    try:
        net = ipaddress.ip_network(raw, strict=False)
        if net.version == 6:
            v6_nets.append(net)
    except ValueError:
        pass

excluded_v4 = set()
for raw in os.environ.get("EXCLUDED_V4", "").split(","):
    raw = raw.strip().split("/", 1)[0]
    if not raw:
        continue
    try:
        addr = ipaddress.ip_address(raw)
        if addr.version == 4:
            excluded_v4.add(str(addr))
    except ValueError:
        pass

excluded_v6 = set()
for raw in os.environ.get("EXCLUDED_V6", "").split(","):
    raw = raw.strip().split("%", 1)[0].split("/", 1)[0]
    if not raw:
        continue
    try:
        addr = ipaddress.ip_address(raw)
        if addr.version == 6:
            excluded_v6.add(str(addr))
    except ValueError:
        pass

target_ns = os.environ.get("TARGET_NS", "").strip()
target_pod_name = os.environ.get("TARGET_POD_NAME", "").strip()
node_name = os.environ.get("NODE_NAME", "").strip()
target_selector = None
raw_selector = os.environ.get("TARGET_SELECTOR_JSON", "").strip()
if raw_selector:
    try:
        parsed_sel = json.loads(raw_selector)
        if isinstance(parsed_sel, dict):
            target_selector = parsed_sel
    except Exception:
        target_selector = None

def matches_selector(labels, selector):
    if not isinstance(selector, dict):
        return True
    if not isinstance(labels, dict):
        labels = {}
    match_labels = selector.get("matchLabels") or {}
    if not isinstance(match_labels, dict):
        return True
    for k, v in match_labels.items():
        if str(labels.get(str(k), "")) != str(v) or str(k) not in labels:
            return False
    match_exprs = selector.get("matchExpressions") or []
    if not isinstance(match_exprs, list):
        return True
    for expr in match_exprs:
        if not isinstance(expr, dict):
            return True
        k = str(expr.get("key", ""))
        op = expr.get("operator", "")
        vals = {str(v) for v in (expr.get("values") or [])}
        if op == "In":
            if k not in labels or str(labels.get(k, "")) not in vals:
                return False
        elif op == "NotIn":
            if k in labels and str(labels.get(k, "")) in vals:
                return False
        elif op == "Exists":
            if k not in labels:
                return False
        elif op == "DoesNotExist":
            if k in labels:
                return False
        else:
            return True
    return True

allowed_v4 = set()
allowed_v6 = set()
v6_peer_v4 = {}

def consider_ip(raw_ip, host_ips):
    raw_ip = (raw_ip or "").strip().split("%", 1)[0].split("/", 1)[0]
    if not raw_ip or raw_ip in host_ips:
        return None
    try:
        addr = ipaddress.ip_address(raw_ip)
    except ValueError:
        return None
    if addr.is_unspecified or addr.is_loopback or addr.is_link_local or addr.is_multicast:
        return None
    s = str(addr)
    if addr.version == 4:
        if s in excluded_v4:
            return None
        if v4_nets and not any(addr in net for net in v4_nets):
            return None
        allowed_v4.add(s)
        return (4, s)
    elif addr.version == 6:
        if s in excluded_v6:
            return None
        if v6_nets and not any(addr in net for net in v6_nets):
            return None
        allowed_v6.add(s)
        return (6, s)
    return None

explicit_v4_list = []
for raw in os.environ.get("EXPLICIT_V4", "").split(","):
    res = consider_ip(raw, set())
    if res and res[0] == 4:
        explicit_v4_list.append(res[1])
explicit_v6_list = []
for raw in os.environ.get("EXPLICIT_V6", "").split(","):
    res = consider_ip(raw, set())
    if res and res[0] == 6:
        explicit_v6_list.append(res[1])
if len(explicit_v4_list) == 1 and len(explicit_v6_list) == 1:
    v6_peer_v4[explicit_v6_list[0]] = explicit_v4_list[0]

raw_pod_list = os.environ.get("POD_LIST_JSON", "").strip()
if raw_pod_list:
    try:
        doc = json.loads(raw_pod_list)
        items = doc.get("items") if isinstance(doc, dict) else None
    except Exception:
        items = None
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            meta = item.get("metadata") or {}
            spec = item.get("spec") or {}
            status = item.get("status") or {}
            if spec.get("hostNetwork") is True:
                continue
            if node_name and (spec.get("nodeName") or "") != node_name:
                continue
            if meta.get("deletionTimestamp") is not None:
                continue
            if (status.get("phase") or "") != "Running":
                continue
            pod_ns = (meta.get("namespace") or target_ns or "").strip()
            pod_name = (meta.get("name") or "").strip()
            if target_pod_name and pod_ns == target_ns and pod_name == target_pod_name:
                continue
            if not target_ns or pod_ns == target_ns:
                labels = meta.get("labels")
                if target_selector is None:
                    continue
                if matches_selector(labels, target_selector):
                    continue
            host_ips = set()
            if isinstance(status.get("hostIP"), str) and status["hostIP"].strip():
                host_ips.add(status["hostIP"].strip())
            for entry in status.get("hostIPs") or []:
                if isinstance(entry, dict) and isinstance(entry.get("ip"), str) and entry["ip"].strip():
                    host_ips.add(entry["ip"].strip())
            pod_v4 = []
            pod_v6 = []
            if isinstance(status.get("podIP"), str):
                res = consider_ip(status["podIP"], host_ips)
                if res:
                    (pod_v4 if res[0] == 4 else pod_v6).append(res[1])
            for entry in status.get("podIPs") or []:
                if isinstance(entry, dict) and isinstance(entry.get("ip"), str):
                    res = consider_ip(entry["ip"], host_ips)
                    if res:
                        (pod_v4 if res[0] == 4 else pod_v6).append(res[1])
            if pod_v4 and pod_v6:
                for v6_item in pod_v6:
                    v6_peer_v4[v6_item] = pod_v4[0]

for addr in sorted(allowed_v4, key=lambda x: ipaddress.IPv4Address(x)):
    print(f"4|{addr}")
for addr in sorted(allowed_v6, key=lambda x: ipaddress.IPv6Address(x)):
    print(f"6|{addr}")
for v6_addr, v4_addr in sorted(v6_peer_v4.items()):
    print(f"P|{v6_addr}={v4_addr}")
' 2>/dev/null || true
    )"

    while IFS='|' read -r family addr; do
        [ -n "${addr}" ] || continue
        if [ "${family}" = "4" ]; then
            k3s_append_unique_csv K3S_KNOWN_NON_TARGET_POD_IPS "${addr}"
        elif [ "${family}" = "6" ]; then
            k3s_append_unique_csv K3S_KNOWN_NON_TARGET_POD_IPV6_ADDRESSES "${addr}"
        elif [ "${family}" = "P" ]; then
            k3s_append_unique_csv K3S_KNOWN_NON_TARGET_POD_V6_PEERS "${addr}"
        fi
    done <<< "${parsed_output}"
    return 0
}

k3s_lookup_non_target_v6_peer_v4() {
    local v6_addr="${1%/128}"
    local entry
    local -a entries=()
    [ -n "${v6_addr}" ] && [ -n "${K3S_KNOWN_NON_TARGET_POD_V6_PEERS:-}" ] || return 1
    IFS=',' read -r -a entries <<< "${K3S_KNOWN_NON_TARGET_POD_V6_PEERS}"
    for entry in "${entries[@]}"; do
        if [[ "${entry}" == "${v6_addr}="* ]]; then
            printf '%s\n' "${entry#*=}"
            return 0
        fi
    done
    return 1
}

probe_k3s_non_target_neighbor() {
    local family="$1"
    local address="${2%/*}"
    local pod_dev="$3"
    local host_ll=""

    [ -n "${address}" ] && [ -n "${pod_dev}" ] || return 0
    PROBE_FAMILY="${family}" PROBE_ADDR="${address}" PROBE_DEV="${pod_dev}" python3 -c '
import os, socket
fam = socket.AF_INET6 if os.environ.get("PROBE_FAMILY") == "6" else socket.AF_INET
addr = os.environ["PROBE_ADDR"]
dev = os.environ["PROBE_DEV"].encode()
for stype in (socket.SOCK_DGRAM, socket.SOCK_STREAM):
    s = None
    try:
        s = socket.socket(fam, stype)
        s.setblocking(False)
        try:
            s.setsockopt(socket.SOL_SOCKET, 25, dev)
        except Exception:
            pass
        if stype == socket.SOCK_DGRAM:
            s.sendto(b"\x00", (addr, 9))
        else:
            s.connect_ex((addr, 1))
    except Exception:
        pass
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass
' >/dev/null 2>&1 || true

    if command -v ping >/dev/null 2>&1; then
        if [ "${family}" = "6" ]; then
            ping -6 -c 1 -W 1 "${address}" >/dev/null 2>&1 || true
            host_ll="$(ip -6 -o addr show dev "${pod_dev}" scope link 2>/dev/null | awk '{for (i = 1; i <= NF; i++) if ($i == "inet6") {split($(i + 1), a, "/"); print a[1]; exit}}' || true)"
            if [ -n "${host_ll}" ]; then
                ping -6 -c 1 -W 1 -I "${host_ll}%${pod_dev}" "${address}" >/dev/null 2>&1 || true
            fi
        else
            ping -c 1 -W 1 "${address}" >/dev/null 2>&1 || true
        fi
    fi
    return 0
}

resolve_k3s_non_target_pod_attachment() {
    local family="$1"
    local address="${2%/*}"
    local peer_v4="${3%/*}"
    local route_info=""
    local route_table=""
    local pod_dev=""
    local neigh_info=""
    local pod_mac=""
    local attempt=0

    [ -n "${address}" ] || return 1
    if [ "${family}" = "6" ]; then
        route_info="$(ip -6 route get "${address}" 2>/dev/null | head -n 1 || true)"
        if [ -z "${route_info}" ] || [[ "${route_info}" == blackhole* ]] || [[ "${route_info}" == unreachable* ]] || [[ "${route_info}" == prohibit* ]]; then
            route_table="$(ip -6 route show 2>/dev/null || true)"
            route_info="$(
                TARGET_ADDR="${address}" ROUTE_TABLE="${route_table}" python3 -c '
import ipaddress, os
target = ipaddress.ip_address(os.environ["TARGET_ADDR"])
best_prefix = -1
best_line = ""
for raw in os.environ.get("ROUTE_TABLE", "").splitlines():
    line = raw.strip()
    if not line:
        continue
    parts = line.split()
    if parts[0] in ("blackhole", "unreachable", "prohibit"):
        continue
    prefix_tok = parts[0]
    try:
        net = ipaddress.ip_network(prefix_tok, strict=False) if "/" in prefix_tok else ipaddress.ip_network(f"{prefix_tok}/128", strict=False)
    except ValueError:
        continue
    if net.version == 6 and target in net and net.prefixlen > best_prefix:
        best_prefix = net.prefixlen
        best_line = line
if best_line:
    print(best_line)
' 2>/dev/null || true
            )"
        fi
    else
        route_info="$(ip -4 route get "${address}" 2>/dev/null | head -n 1 || true)"
        if [ -z "${route_info}" ] || [[ "${route_info}" == blackhole* ]] || [[ "${route_info}" == unreachable* ]] || [[ "${route_info}" == prohibit* ]]; then
            route_table="$(ip -4 route show 2>/dev/null || true)"
            route_info="$(
                TARGET_ADDR="${address}" ROUTE_TABLE="${route_table}" python3 -c '
import ipaddress, os
target = ipaddress.ip_address(os.environ["TARGET_ADDR"])
best_prefix = -1
best_line = ""
for raw in os.environ.get("ROUTE_TABLE", "").splitlines():
    line = raw.strip()
    if not line:
        continue
    parts = line.split()
    if parts[0] in ("blackhole", "unreachable", "prohibit"):
        continue
    prefix_tok = parts[0]
    try:
        net = ipaddress.ip_network(prefix_tok, strict=False) if "/" in prefix_tok else ipaddress.ip_network(f"{prefix_tok}/32", strict=False)
    except ValueError:
        continue
    if net.version == 4 and target in net and net.prefixlen > best_prefix:
        best_prefix = net.prefixlen
        best_line = line
if best_line:
    print(best_line)
' 2>/dev/null || true
            )"
        fi
    fi

    [ -n "${route_info}" ] || return 1
    if [[ " ${route_info} " == *" via "* ]]; then
        return 1
    fi
    pod_dev="$(awk '{for (i = 1; i <= NF; i++) if ($i == "dev") {print $(i + 1); exit}}' <<< "${route_info}")"
    if [ -z "${pod_dev}" ] || [ "${pod_dev}" = "lo" ] || [ "${pod_dev}" = "${WG_IFACE}" ]; then
        return 1
    fi

    if [ "${family}" = "6" ]; then
        neigh_info="$(ip -6 neigh show to "${address}" dev "${pod_dev}" 2>/dev/null | head -n 1 || true)"
        if [ -z "${neigh_info}" ]; then
            neigh_info="$(ip -6 neigh show "${address}" dev "${pod_dev}" 2>/dev/null | head -n 1 || true)"
        fi
        pod_mac="$(awk '{for (i = 1; i <= NF; i++) if ($i == "lladdr") {print tolower($(i + 1)); exit}}' <<< "${neigh_info}")"
        if [ -z "${pod_mac}" ] && ! declare -F ip >/dev/null 2>&1; then
            probe_k3s_non_target_neighbor "6" "${address}" "${pod_dev}"
            for attempt in 1 2 3 4 5; do
                neigh_info="$(ip -6 neigh show to "${address}" dev "${pod_dev}" 2>/dev/null | head -n 1 || true)"
                if [ -z "${neigh_info}" ]; then
                    neigh_info="$(ip -6 neigh show "${address}" dev "${pod_dev}" 2>/dev/null | head -n 1 || true)"
                fi
                pod_mac="$(awk '{for (i = 1; i <= NF; i++) if ($i == "lladdr") {print tolower($(i + 1)); exit}}' <<< "${neigh_info}")"
                [ -n "${pod_mac}" ] && break
                sleep 0.03 2>/dev/null || true
            done
        fi
        if [ -z "${pod_mac}" ] && [ -n "${peer_v4}" ]; then
            neigh_info="$(ip -4 neigh show to "${peer_v4}" dev "${pod_dev}" 2>/dev/null | head -n 1 || true)"
            if [ -z "${neigh_info}" ]; then
                neigh_info="$(ip -4 neigh show "${peer_v4}" dev "${pod_dev}" 2>/dev/null | head -n 1 || true)"
            fi
            pod_mac="$(awk '{for (i = 1; i <= NF; i++) if ($i == "lladdr") {print tolower($(i + 1)); exit}}' <<< "${neigh_info}")"
            if [ -z "${pod_mac}" ] && ! declare -F ip >/dev/null 2>&1; then
                probe_k3s_non_target_neighbor "4" "${peer_v4}" "${pod_dev}"
                for attempt in 1 2 3 4 5; do
                    neigh_info="$(ip -4 neigh show to "${peer_v4}" dev "${pod_dev}" 2>/dev/null | head -n 1 || true)"
                    if [ -z "${neigh_info}" ]; then
                        neigh_info="$(ip -4 neigh show "${peer_v4}" dev "${pod_dev}" 2>/dev/null | head -n 1 || true)"
                    fi
                    pod_mac="$(awk '{for (i = 1; i <= NF; i++) if ($i == "lladdr") {print tolower($(i + 1)); exit}}' <<< "${neigh_info}")"
                    [ -n "${pod_mac}" ] && break
                    sleep 0.03 2>/dev/null || true
                done
            fi
        fi
    else
        neigh_info="$(ip -4 neigh show to "${address}" dev "${pod_dev}" 2>/dev/null | head -n 1 || true)"
        if [ -z "${neigh_info}" ]; then
            neigh_info="$(ip -4 neigh show "${address}" dev "${pod_dev}" 2>/dev/null | head -n 1 || true)"
        fi
        pod_mac="$(awk '{for (i = 1; i <= NF; i++) if ($i == "lladdr") {print tolower($(i + 1)); exit}}' <<< "${neigh_info}")"
        if [ -z "${pod_mac}" ] && ! declare -F ip >/dev/null 2>&1; then
            probe_k3s_non_target_neighbor "4" "${address}" "${pod_dev}"
            for attempt in 1 2 3 4 5; do
                neigh_info="$(ip -4 neigh show to "${address}" dev "${pod_dev}" 2>/dev/null | head -n 1 || true)"
                if [ -z "${neigh_info}" ]; then
                    neigh_info="$(ip -4 neigh show "${address}" dev "${pod_dev}" 2>/dev/null | head -n 1 || true)"
                fi
                pod_mac="$(awk '{for (i = 1; i <= NF; i++) if ($i == "lladdr") {print tolower($(i + 1)); exit}}' <<< "${neigh_info}")"
                [ -n "${pod_mac}" ] && break
                sleep 0.03 2>/dev/null || true
            done
        fi
    fi

    if [[ ! "${pod_mac}" =~ ^([0-9a-f]{2}:){5}[0-9a-f]{2}$ ]]; then
        return 1
    fi
    printf '%s|%s\n' "${pod_dev}" "${pod_mac}"
    return 0
}

revoke_k3s_non_target_pod_exemption() {
    local family="$1"
    local address="${2%/*}"
    local rule_line rule_spec rule_source mangle_rule
    local -a parts

    [ -n "${address}" ] || return 0
    if [ "${family}" = "6" ]; then
        while IFS= read -r mangle_rule; do
            [ -n "${mangle_rule}" ] || continue
            [[ "${mangle_rule}" == *"--comment ${K3S_NON_TARGET_RULE_MARKER}"* ]] || continue
            if [[ "${mangle_rule}" == *" -s ${address}/128 "* ]] || [[ "${mangle_rule}" == *" -s ${address} "* ]]; then
                read -r -a parts <<< "${mangle_rule}"
                parts[0]="-D"
                ip6tables -t mangle "${parts[@]}" >/dev/null 2>&1 || true
                POLICY_CHANGED="1"
            fi
        done < <(ip6tables -t mangle -S PREROUTING 2>/dev/null || true)

        while IFS= read -r rule_line; do
            [ -n "${rule_line}" ] || continue
            rule_spec="${rule_line#*:}"
            k3s_policy_rule_is_owned "${rule_line}" || continue
            if [[ "${rule_spec}" != *" lookup main"* ]] && [[ "${rule_spec}" != *" table main"* ]]; then
                continue
            fi
            [[ "${rule_spec}" != *" to "* ]] || continue
            rule_source="$(awk '{for (i = 1; i <= NF; i++) if ($i == "from") {print $(i + 1); exit}}' <<< "${rule_spec}")"
            if [ "${rule_source%/128}" = "${address}" ]; then
                delete_ipv6_policy_rule_line "${rule_line}"
                POLICY_CHANGED="1"
            fi
        done < <(ip -6 rule show pref "${K3S_BYPASS_RULE_PREF}" 2>/dev/null || true)
    else
        while IFS= read -r mangle_rule; do
            [ -n "${mangle_rule}" ] || continue
            [[ "${mangle_rule}" == *"--comment ${K3S_NON_TARGET_RULE_MARKER}"* ]] || continue
            if [[ "${mangle_rule}" == *" -s ${address}/32 "* ]] || [[ "${mangle_rule}" == *" -s ${address} "* ]]; then
                read -r -a parts <<< "${mangle_rule}"
                parts[0]="-D"
                iptables -t mangle "${parts[@]}" >/dev/null 2>&1 || true
                POLICY_CHANGED="1"
            fi
        done < <(iptables -t mangle -S PREROUTING 2>/dev/null || true)

        while IFS= read -r rule_line; do
            [ -n "${rule_line}" ] || continue
            rule_spec="${rule_line#*:}"
            k3s_policy_rule_is_owned "${rule_line}" || continue
            if [[ "${rule_spec}" != *" lookup main"* ]] && [[ "${rule_spec}" != *" table main"* ]]; then
                continue
            fi
            [[ "${rule_spec}" != *" to "* ]] || continue
            rule_source="$(awk '{for (i = 1; i <= NF; i++) if ($i == "from") {print $(i + 1); exit}}' <<< "${rule_spec}")"
            if [ "${rule_source%/32}" = "${address}" ]; then
                delete_policy_rule_line "${rule_line}"
                POLICY_CHANGED="1"
            fi
        done < <(ip rule show pref "${K3S_BYPASS_RULE_PREF}" 2>/dev/null || true)
    fi
    return 0
}

cleanup_k3s_non_target_mangle_rules() {
    local mangle_rule
    local -a parts

    while IFS= read -r mangle_rule; do
        [ -n "${mangle_rule}" ] || continue
        [[ "${mangle_rule}" == *"--comment ${K3S_NON_TARGET_RULE_MARKER}"* ]] || continue
        read -r -a parts <<< "${mangle_rule}"
        parts[0]="-D"
        iptables -t mangle "${parts[@]}" >/dev/null 2>&1 || true
    done < <(iptables -t mangle -S PREROUTING 2>/dev/null || true)

    while IFS= read -r mangle_rule; do
        [ -n "${mangle_rule}" ] || continue
        [[ "${mangle_rule}" == *"--comment ${K3S_NON_TARGET_RULE_MARKER}"* ]] || continue
        read -r -a parts <<< "${mangle_rule}"
        parts[0]="-D"
        ip6tables -t mangle "${parts[@]}" >/dev/null 2>&1 || true
    done < <(ip6tables -t mangle -S PREROUTING 2>/dev/null || true)
}

ensure_k3s_non_target_pod_exemption() {
    local family="$1"
    local address="${2%/*}"
    local peer_v4="${3%/*}"
    local attachment="" pod_dev="" pod_mac=""
    local mangle_rule rule_line rule_spec rule_source rule_iif
    local expected_rule_pattern existing_rule
    local -a parts

    [ -n "${address}" ] || return 0
    if ! attachment="$(resolve_k3s_non_target_pod_attachment "${family}" "${address}" "${peer_v4}")" || [ -z "${attachment}" ]; then
        revoke_k3s_non_target_pod_exemption "${family}" "${address}"
        LAST_ERROR="k3s: Cannot resolve live network attachment for non-target pod ${address}"
        return 1
    fi
    IFS='|' read -r pod_dev pod_mac <<< "${attachment}"
    if [ -z "${pod_dev}" ] || [ -z "${pod_mac}" ]; then
        revoke_k3s_non_target_pod_exemption "${family}" "${address}"
        LAST_ERROR="k3s: Cannot resolve live network attachment for non-target pod ${address}"
        return 1
    fi

    if [ "${family}" = "6" ]; then
        while IFS= read -r mangle_rule; do
            [ -n "${mangle_rule}" ] || continue
            [[ "${mangle_rule}" == *"--comment ${K3S_NON_TARGET_RULE_MARKER}"* ]] || continue
            if [[ "${mangle_rule}" == *" -s ${address}/128 "* ]] || [[ "${mangle_rule}" == *" -s ${address} "* ]]; then
                if [[ "${mangle_rule}" != *" -i ${pod_dev} "* ]] || [[ "${mangle_rule,,}" != *" --mac-source ${pod_mac} "* ]]; then
                    read -r -a parts <<< "${mangle_rule}"
                    parts[0]="-D"
                    ip6tables -t mangle "${parts[@]}" >/dev/null 2>&1 || true
                    POLICY_CHANGED="1"
                fi
            fi
        done < <(ip6tables -t mangle -S PREROUTING 2>/dev/null || true)

        if ! ip6tables -t mangle -C PREROUTING \
            -i "${pod_dev}" -s "${address}" \
            -m mac --mac-source "${pod_mac}" \
            -m comment --comment "${K3S_NON_TARGET_RULE_MARKER}" \
            -j MARK --set-xmark "${K3S_NON_TARGET_FWMARK}/${K3S_NON_TARGET_FWMARK_MASK}" >/dev/null 2>&1; then
            if ! ip6tables -t mangle -I PREROUTING \
                -i "${pod_dev}" -s "${address}" \
                -m mac --mac-source "${pod_mac}" \
                -m comment --comment "${K3S_NON_TARGET_RULE_MARKER}" \
                -j MARK --set-xmark "${K3S_NON_TARGET_FWMARK}/${K3S_NON_TARGET_FWMARK_MASK}" >/dev/null 2>&1; then
                revoke_k3s_non_target_pod_exemption "6" "${address}"
                LAST_ERROR="k3s: Failed to add IPv6 non-target pod mangle mark rule for ${address}"
                return 1
            fi
            POLICY_CHANGED="1"
        fi

        while IFS= read -r rule_line; do
            [ -n "${rule_line}" ] || continue
            rule_spec="${rule_line#*:}"
            k3s_policy_rule_is_owned "${rule_line}" || continue
            if [[ "${rule_spec}" != *" lookup main"* ]] && [[ "${rule_spec}" != *" table main"* ]]; then
                continue
            fi
            [[ "${rule_spec}" != *" to "* ]] || continue
            rule_source="$(awk '{for (i = 1; i <= NF; i++) if ($i == "from") {print $(i + 1); exit}}' <<< "${rule_spec}")"
            [ "${rule_source%/128}" = "${address}" ] || continue
            rule_iif="$(awk '{for (i = 1; i <= NF; i++) if ($i == "iif") {print $(i + 1); exit}}' <<< "${rule_spec}")"
            if [[ "${rule_spec}" != *" fwmark "* ]] || [ "${rule_iif}" != "${pod_dev}" ]; then
                delete_ipv6_policy_rule_line "${rule_line}"
                POLICY_CHANGED="1"
            fi
        done < <(ip -6 rule show pref "${K3S_BYPASS_RULE_PREF}" 2>/dev/null || true)

        expected_rule_pattern="^${K3S_BYPASS_RULE_PREF}:[[:space:]]+from[[:space:]]+${address}(/128)?[[:space:]]+fwmark[[:space:]]+${K3S_NON_TARGET_FWMARK}(/${K3S_NON_TARGET_FWMARK_MASK})?[[:space:]]+iif[[:space:]]+${pod_dev}[[:space:]]+(lookup|table)[[:space:]]+main([[:space:]].*)?[[:space:]]+proto(col)?[[:space:]]+${K3S_RULE_PROTOCOL}[[:space:]]*$"
        if ! ip -6 rule show pref "${K3S_BYPASS_RULE_PREF}" 2>/dev/null | grep -qiE "${expected_rule_pattern}"; then
            if ! ip -6 rule add from "${address}" fwmark "${K3S_NON_TARGET_FWMARK}/${K3S_NON_TARGET_FWMARK_MASK}" iif "${pod_dev}" table main protocol "${K3S_RULE_PROTOCOL}" pref "${K3S_BYPASS_RULE_PREF}" >/dev/null 2>&1; then
                revoke_k3s_non_target_pod_exemption "6" "${address}"
                LAST_ERROR="k3s: Failed to add IPv6 non-target pod outbound route for ${address}"
                return 1
            fi
            existing_rule="$(ip -6 rule show pref "${K3S_BYPASS_RULE_PREF}" 2>/dev/null | grep -iE "${expected_rule_pattern}" | head -n 1 || true)"
            if [ -z "${existing_rule}" ] || ! k3s_policy_rule_is_owned "${existing_rule}"; then
                revoke_k3s_non_target_pod_exemption "6" "${address}"
                LAST_ERROR="k3s: IPv6 non-target pod outbound route was not installed for ${address}"
                return 1
            fi
            POLICY_CHANGED="1"
        fi
    else
        while IFS= read -r mangle_rule; do
            [ -n "${mangle_rule}" ] || continue
            [[ "${mangle_rule}" == *"--comment ${K3S_NON_TARGET_RULE_MARKER}"* ]] || continue
            if [[ "${mangle_rule}" == *" -s ${address}/32 "* ]] || [[ "${mangle_rule}" == *" -s ${address} "* ]]; then
                if [[ "${mangle_rule}" != *" -i ${pod_dev} "* ]] || [[ "${mangle_rule,,}" != *" --mac-source ${pod_mac} "* ]]; then
                    read -r -a parts <<< "${mangle_rule}"
                    parts[0]="-D"
                    iptables -t mangle "${parts[@]}" >/dev/null 2>&1 || true
                    POLICY_CHANGED="1"
                fi
            fi
        done < <(iptables -t mangle -S PREROUTING 2>/dev/null || true)

        if ! iptables -t mangle -C PREROUTING \
            -i "${pod_dev}" -s "${address}" \
            -m mac --mac-source "${pod_mac}" \
            -m comment --comment "${K3S_NON_TARGET_RULE_MARKER}" \
            -j MARK --set-xmark "${K3S_NON_TARGET_FWMARK}/${K3S_NON_TARGET_FWMARK_MASK}" >/dev/null 2>&1; then
            if ! iptables -t mangle -I PREROUTING \
                -i "${pod_dev}" -s "${address}" \
                -m mac --mac-source "${pod_mac}" \
                -m comment --comment "${K3S_NON_TARGET_RULE_MARKER}" \
                -j MARK --set-xmark "${K3S_NON_TARGET_FWMARK}/${K3S_NON_TARGET_FWMARK_MASK}" >/dev/null 2>&1; then
                revoke_k3s_non_target_pod_exemption "4" "${address}"
                LAST_ERROR="k3s: Failed to add non-target pod mangle mark rule for ${address}"
                return 1
            fi
            POLICY_CHANGED="1"
        fi

        while IFS= read -r rule_line; do
            [ -n "${rule_line}" ] || continue
            rule_spec="${rule_line#*:}"
            k3s_policy_rule_is_owned "${rule_line}" || continue
            if [[ "${rule_spec}" != *" lookup main"* ]] && [[ "${rule_spec}" != *" table main"* ]]; then
                continue
            fi
            [[ "${rule_spec}" != *" to "* ]] || continue
            rule_source="$(awk '{for (i = 1; i <= NF; i++) if ($i == "from") {print $(i + 1); exit}}' <<< "${rule_spec}")"
            [ "${rule_source%/32}" = "${address}" ] || continue
            rule_iif="$(awk '{for (i = 1; i <= NF; i++) if ($i == "iif") {print $(i + 1); exit}}' <<< "${rule_spec}")"
            if [[ "${rule_spec}" != *" fwmark "* ]] || [ "${rule_iif}" != "${pod_dev}" ]; then
                delete_policy_rule_line "${rule_line}"
                POLICY_CHANGED="1"
            fi
        done < <(ip rule show pref "${K3S_BYPASS_RULE_PREF}" 2>/dev/null || true)

        expected_rule_pattern="^${K3S_BYPASS_RULE_PREF}:[[:space:]]+from[[:space:]]+${address//./\\.}(/32)?[[:space:]]+fwmark[[:space:]]+${K3S_NON_TARGET_FWMARK}(/${K3S_NON_TARGET_FWMARK_MASK})?[[:space:]]+iif[[:space:]]+${pod_dev}[[:space:]]+(lookup|table)[[:space:]]+main([[:space:]].*)?[[:space:]]+proto(col)?[[:space:]]+${K3S_RULE_PROTOCOL}[[:space:]]*$"
        if ! ip rule show pref "${K3S_BYPASS_RULE_PREF}" 2>/dev/null | grep -qiE "${expected_rule_pattern}"; then
            if ! ip rule add from "${address}" fwmark "${K3S_NON_TARGET_FWMARK}/${K3S_NON_TARGET_FWMARK_MASK}" iif "${pod_dev}" table main protocol "${K3S_RULE_PROTOCOL}" pref "${K3S_BYPASS_RULE_PREF}" >/dev/null 2>&1; then
                revoke_k3s_non_target_pod_exemption "4" "${address}"
                LAST_ERROR="k3s: Failed to add non-target pod outbound route for ${address}"
                return 1
            fi
            existing_rule="$(ip rule show pref "${K3S_BYPASS_RULE_PREF}" 2>/dev/null | grep -iE "${expected_rule_pattern}" | head -n 1 || true)"
            if [ -z "${existing_rule}" ] || ! k3s_policy_rule_is_owned "${existing_rule}"; then
                revoke_k3s_non_target_pod_exemption "4" "${address}"
                LAST_ERROR="k3s: Non-target pod outbound route was not installed for ${address}"
                return 1
            fi
            POLICY_CHANGED="1"
        fi
    fi
    return 0
}

ensure_k3s_unknown_pod_containment() {
    local source_ip="${1:-${DOCKER_TARGET_IP:-}}"
    if [ "${K3S_ENFORCE_UNKNOWN_POD_CONTAINMENT:-false}" != "true" ]; then
        if declare -F ip >/dev/null 2>&1 || [ "$(id -u)" -ne 0 ]; then
            return 0
        fi
    fi

    if k3s_acquire_watch_state_lock; then
        sync_k3s_watched_candidate_addresses || true
        k3s_release_watch_state_lock
    fi

    local matched_cidrs pod_cidr escaped_cidr exact_pattern existing_rule
    local bypass_cidr rule_line rule_spec rule_source destination bypass_csv
    local ipv6_matched_cidrs=""
    local non_target_ip non_target_ipv6 peer_v4 mangle_rule mangle_src
    local -a bypass_cidrs=()
    local -a non_target_ips=()
    local -a non_target_ipv6_list=()
    local -a parts=()
    if ! normalize_k3s_bypass_cidrs; then
        return 1
    fi
    if ! matched_cidrs="$(resolve_k3s_pod_cidrs "${source_ip}")" || [ -z "${matched_cidrs}" ]; then
        LAST_ERROR="k3s: Cannot determine pod CIDR for unknown-pod containment"
        return 1
    fi
    if k3s_ipv6_is_active; then
        if ! ipv6_matched_cidrs="$(resolve_k3s_ipv6_pod_cidrs)" || [ -z "${ipv6_matched_cidrs}" ]; then
            LAST_ERROR="k3s: Cannot determine IPv6 pod CIDR for unknown-pod containment"
            return 1
        fi
    fi
    resolve_k3s_non_target_pod_addresses "${matched_cidrs}" "${ipv6_matched_cidrs}"
    if [ -n "${K3S_KNOWN_NON_TARGET_POD_IPS:-}" ]; then
        IFS=',' read -r -a non_target_ips <<< "${K3S_KNOWN_NON_TARGET_POD_IPS}"
    fi
    if [ -n "${K3S_KNOWN_NON_TARGET_POD_IPV6_ADDRESSES:-}" ]; then
        IFS=',' read -r -a non_target_ipv6_list <<< "${K3S_KNOWN_NON_TARGET_POD_IPV6_ADDRESSES}"
    fi

    bypass_csv=",${K3S_BYPASS_CIDRS_NORMALIZED},"
    if [ -n "${K3S_BYPASS_CIDRS_NORMALIZED:-}" ]; then
        IFS=',' read -r -a bypass_cidrs <<< "${K3S_BYPASS_CIDRS_NORMALIZED}"
    fi

    while IFS= read -r pod_cidr; do
        [ -n "${pod_cidr}" ] || continue
        [[ "${pod_cidr}" == */* ]] && [[ "${pod_cidr}" != */32 ]] || continue
        escaped_cidr="${pod_cidr//./\\.}"

        # 1. Install pod-CIDR fallback blackhole at K3S_BLACKHOLE_RULE_PREF (32765,
        # below K3S_TUNNEL_RULE_PREF 32764) first so any newly allocated replacement
        # pod IP is blocked from external clearnet egress in the kernel immediately
        # upon CNI creation, before its podIP even appears in the Kubernetes API.
        exact_pattern="^${K3S_BLACKHOLE_RULE_PREF}:[[:space:]]+from[[:space:]]+${escaped_cidr}[[:space:]]+blackhole([[:space:]].*)?[[:space:]]+proto(col)?[[:space:]]+${K3S_RULE_PROTOCOL}[[:space:]]*$"
        existing_rule="$(ip rule show pref "${K3S_BLACKHOLE_RULE_PREF}" 2>/dev/null | grep -E "${exact_pattern}" | head -n 1 || true)"
        if [ -n "${existing_rule}" ]; then
            if ! k3s_policy_rule_is_owned "${existing_rule}"; then
                LAST_ERROR="k3s: Pod-CIDR fallback blackhole preference is occupied by an unowned rule"
                return 1
            fi
        else
            if ! ip rule add from "${pod_cidr}" blackhole protocol "${K3S_RULE_PROTOCOL}" pref "${K3S_BLACKHOLE_RULE_PREF}" >/dev/null 2>&1; then
                LAST_ERROR="k3s: Failed to install pod-CIDR fallback blackhole for ${pod_cidr}"
                return 1
            fi
            existing_rule="$(ip rule show pref "${K3S_BLACKHOLE_RULE_PREF}" 2>/dev/null | grep -E "${exact_pattern}" | head -n 1 || true)"
            if [ -z "${existing_rule}" ] || ! k3s_policy_rule_is_owned "${existing_rule}"; then
                LAST_ERROR="k3s: Pod-CIDR fallback blackhole was not installed for ${pod_cidr}"
                return 1
            fi
            POLICY_CHANGED="1"
        fi

        # 2. Remove stale pod-CIDR cluster-local bypasses that are no longer configured.
        while IFS= read -r rule_line; do
            [ -n "${rule_line}" ] || continue
            rule_spec="${rule_line#*:}"
            [[ "${rule_spec}" == *"from ${pod_cidr} "* ]] || continue
            k3s_policy_rule_is_owned "${rule_line}" || continue
            if [[ "${rule_spec}" != *" lookup main"* ]] && [[ "${rule_spec}" != *" table main"* ]]; then
                continue
            fi
            destination="$(awk '{for (i = 1; i <= NF; i++) if ($i == "to") {print $(i + 1); exit}}' <<< "${rule_spec}")"
            if [ -z "${destination}" ] || [[ "${bypass_csv}" != *",${destination},"* ]]; then
                delete_policy_rule_line "${rule_line}"
                POLICY_CHANGED="1"
            fi
        done < <(ip rule show pref "${K3S_BYPASS_RULE_PREF}" 2>/dev/null || true)

        # 3. Ensure cluster-local bypasses for the pod CIDR at K3S_BYPASS_RULE_PREF (32500).
        for bypass_cidr in "${bypass_cidrs[@]}"; do
            if ! ip rule show pref "${K3S_BYPASS_RULE_PREF}" 2>/dev/null | grep -qE \
                "from[[:space:]]+${escaped_cidr}[[:space:]]+to[[:space:]]+${bypass_cidr//./\\.}[[:space:]]+(lookup|table)[[:space:]]+main([[:space:]].*)?[[:space:]]+proto(col)?[[:space:]]+${K3S_RULE_PROTOCOL}"; then
                if ! ip rule add from "${pod_cidr}" to "${bypass_cidr}" table main protocol "${K3S_RULE_PROTOCOL}" pref "${K3S_BYPASS_RULE_PREF}" >/dev/null 2>&1; then
                    LAST_ERROR="k3s: Failed to add pod-CIDR local bypass for ${pod_cidr} to ${bypass_cidr}"
                    return 1
                fi
                existing_rule="$(ip rule show pref "${K3S_BYPASS_RULE_PREF}" 2>/dev/null | grep -E \
                    "from[[:space:]]+${escaped_cidr}[[:space:]]+to[[:space:]]+${bypass_cidr//./\\.}[[:space:]]+(lookup|table)[[:space:]]+main([[:space:]].*)?[[:space:]]+proto(col)?[[:space:]]+${K3S_RULE_PROTOCOL}" \
                    | head -n 1 || true)"
                if [ -z "${existing_rule}" ]; then
                    LAST_ERROR="k3s: Pod-CIDR local bypass rule was not installed for ${pod_cidr} to ${bypass_cidr}"
                    return 1
                fi
                POLICY_CHANGED="1"
            fi
        done
    done <<< "${matched_cidrs}"

    # 4. Reconcile MAC-and-interface-bound table-main outbound routes at
    # K3S_BYPASS_RULE_PREF (32500) for known non-target pods on this node so
    # unrelated pods keep Internet access while a replacement Lightning pod
    # reusing a terminated non-target pod's IP (with a new veth/MAC) cannot
    # match the exemption and falls through to the 32765 pod-CIDR blackhole.
    while IFS= read -r rule_line; do
        [ -n "${rule_line}" ] || continue
        rule_spec="${rule_line#*:}"
        k3s_policy_rule_is_owned "${rule_line}" || continue
        if [[ "${rule_spec}" != *" lookup main"* ]] && [[ "${rule_spec}" != *" table main"* ]]; then
            continue
        fi
        [[ "${rule_spec}" != *" to "* ]] || continue
        rule_source="$(awk '{for (i = 1; i <= NF; i++) if ($i == "from") {print $(i + 1); exit}}' <<< "${rule_spec}")"
        rule_source="${rule_source%/32}"
        [ -n "${rule_source}" ] && [[ "${rule_source}" != */* ]] || continue
        if [[ "${rule_spec}" != *" fwmark "* ]] || [[ "${rule_spec}" != *" iif "* ]] || \
           [ "${rule_source}" = "${source_ip%/32}" ] || \
           k3s_ip_is_in_csv "${rule_source}" "${K3S_CANDIDATE_POD_IPS:-}" || \
           ! k3s_ip_is_in_csv "${rule_source}" "${K3S_KNOWN_NON_TARGET_POD_IPS:-}"; then
            revoke_k3s_non_target_pod_exemption "4" "${rule_source}"
        fi
    done < <(ip rule show pref "${K3S_BYPASS_RULE_PREF}" 2>/dev/null || true)

    while IFS= read -r mangle_rule; do
        [ -n "${mangle_rule}" ] || continue
        [[ "${mangle_rule}" == *"--comment ${K3S_NON_TARGET_RULE_MARKER}"* ]] || continue
        mangle_src="$(awk '{for (i = 1; i <= NF; i++) if ($i == "-s") {print $(i + 1); exit}}' <<< "${mangle_rule}")"
        mangle_src="${mangle_src%/32}"
        if [ -z "${mangle_src}" ] || \
           [ "${mangle_src}" = "${source_ip%/32}" ] || \
           k3s_ip_is_in_csv "${mangle_src}" "${K3S_CANDIDATE_POD_IPS:-}" || \
           ! k3s_ip_is_in_csv "${mangle_src}" "${K3S_KNOWN_NON_TARGET_POD_IPS:-}"; then
            read -r -a parts <<< "${mangle_rule}"
            parts[0]="-D"
            iptables -t mangle "${parts[@]}" >/dev/null 2>&1 || true
            POLICY_CHANGED="1"
        fi
    done < <(iptables -t mangle -S PREROUTING 2>/dev/null || true)

    for non_target_ip in "${non_target_ips[@]}"; do
        non_target_ip="${non_target_ip%/32}"
        [ -n "${non_target_ip}" ] || continue
        [ "${non_target_ip}" != "${source_ip%/32}" ] || continue
        k3s_ip_is_in_csv "${non_target_ip}" "${K3S_CANDIDATE_POD_IPS:-}" && continue
        if ! ensure_k3s_non_target_pod_exemption "4" "${non_target_ip}" ""; then
            return 1
        fi
    done

    if k3s_ipv6_is_active; then
        local ipv6_cidr v6_pattern v6_existing
        while IFS= read -r ipv6_cidr; do
            [ -n "${ipv6_cidr}" ] || continue
            [[ "${ipv6_cidr}" == */* ]] && [[ "${ipv6_cidr}" != */128 ]] || continue
            v6_pattern="^${K3S_BLACKHOLE_RULE_PREF}:[[:space:]]+from[[:space:]]+${ipv6_cidr}[[:space:]]+blackhole([[:space:]].*)?[[:space:]]+proto(col)?[[:space:]]+${K3S_RULE_PROTOCOL}[[:space:]]*$"
            v6_existing="$(ip -6 rule show pref "${K3S_BLACKHOLE_RULE_PREF}" 2>/dev/null | grep -E "${v6_pattern}" | head -n 1 || true)"
            if [ -n "${v6_existing}" ]; then
                if ! k3s_policy_rule_is_owned "${v6_existing}"; then
                    LAST_ERROR="k3s: IPv6 pod-CIDR fallback blackhole preference is occupied by an unowned rule"
                    return 1
                fi
            else
                if ! ip -6 rule add from "${ipv6_cidr}" blackhole protocol "${K3S_RULE_PROTOCOL}" pref "${K3S_BLACKHOLE_RULE_PREF}" >/dev/null 2>&1; then
                    LAST_ERROR="k3s: Failed to install IPv6 pod-CIDR fallback blackhole for ${ipv6_cidr}"
                    return 1
                fi
                v6_existing="$(ip -6 rule show pref "${K3S_BLACKHOLE_RULE_PREF}" 2>/dev/null | grep -E "${v6_pattern}" | head -n 1 || true)"
                if [ -z "${v6_existing}" ] || ! k3s_policy_rule_is_owned "${v6_existing}"; then
                    LAST_ERROR="k3s: IPv6 pod-CIDR fallback blackhole was not installed for ${ipv6_cidr}"
                    return 1
                fi
                POLICY_CHANGED="1"
            fi
        done <<< "${ipv6_matched_cidrs}"

        while IFS= read -r rule_line; do
            [ -n "${rule_line}" ] || continue
            rule_spec="${rule_line#*:}"
            k3s_policy_rule_is_owned "${rule_line}" || continue
            if [[ "${rule_spec}" != *" lookup main"* ]] && [[ "${rule_spec}" != *" table main"* ]]; then
                continue
            fi
            [[ "${rule_spec}" != *" to "* ]] || continue
            rule_source="$(awk '{for (i = 1; i <= NF; i++) if ($i == "from") {print $(i + 1); exit}}' <<< "${rule_spec}")"
            rule_source="${rule_source%/128}"
            [ -n "${rule_source}" ] && [[ "${rule_source}" != */* ]] || continue
            if [[ "${rule_spec}" != *" fwmark "* ]] || [[ "${rule_spec}" != *" iif "* ]] || \
               k3s_ip_is_in_csv "${rule_source}" "${K3S_CANDIDATE_POD_IPV6_ADDRESSES:-}" || \
               ! k3s_ip_is_in_csv "${rule_source}" "${K3S_KNOWN_NON_TARGET_POD_IPV6_ADDRESSES:-}"; then
                revoke_k3s_non_target_pod_exemption "6" "${rule_source}"
            fi
        done < <(ip -6 rule show pref "${K3S_BYPASS_RULE_PREF}" 2>/dev/null || true)

        while IFS= read -r mangle_rule; do
            [ -n "${mangle_rule}" ] || continue
            [[ "${mangle_rule}" == *"--comment ${K3S_NON_TARGET_RULE_MARKER}"* ]] || continue
            mangle_src="$(awk '{for (i = 1; i <= NF; i++) if ($i == "-s") {print $(i + 1); exit}}' <<< "${mangle_rule}")"
            mangle_src="${mangle_src%/128}"
            if [ -z "${mangle_src}" ] || \
               k3s_ip_is_in_csv "${mangle_src}" "${K3S_CANDIDATE_POD_IPV6_ADDRESSES:-}" || \
               ! k3s_ip_is_in_csv "${mangle_src}" "${K3S_KNOWN_NON_TARGET_POD_IPV6_ADDRESSES:-}"; then
                read -r -a parts <<< "${mangle_rule}"
                parts[0]="-D"
                ip6tables -t mangle "${parts[@]}" >/dev/null 2>&1 || true
                POLICY_CHANGED="1"
            fi
        done < <(ip6tables -t mangle -S PREROUTING 2>/dev/null || true)

        for non_target_ipv6 in "${non_target_ipv6_list[@]}"; do
            non_target_ipv6="${non_target_ipv6%/128}"
            [ -n "${non_target_ipv6}" ] || continue
            k3s_ip_is_in_csv "${non_target_ipv6}" "${K3S_CANDIDATE_POD_IPV6_ADDRESSES:-}" && continue
            peer_v4="$(k3s_lookup_non_target_v6_peer_v4 "${non_target_ipv6}" || true)"
            if ! ensure_k3s_non_target_pod_exemption "6" "${non_target_ipv6}" "${peer_v4}"; then
                return 1
            fi
        done
    fi
    return 0
}

remove_k3s_subnet_quarantine() {
    local rule_line
    local rule_spec
    local rule_source
    local quarantine_pref
    local owned_quarantine

    quarantine_pref="${K3S_QUARANTINE_RULE_PREF}"
    while IFS= read -r rule_line; do
        [ -n "${rule_line}" ] || continue
        rule_spec="${rule_line#*:}"
        [[ "${rule_spec}" == *"blackhole"* ]] || continue
        owned_quarantine=false
        if k3s_policy_rule_is_owned "${rule_line}"; then
            owned_quarantine=true
        fi
        [ "${owned_quarantine}" = true ] || continue
        rule_source="$(awk '{for (i = 1; i <= NF; i++) if ($i == "from") {print $(i + 1); exit}}' <<< "${rule_spec}")"
        [ -n "${rule_source}" ] || continue
        delete_policy_rule_line "${rule_line}"
    done < <(ip rule show pref "${quarantine_pref}" 2>/dev/null || true)

    while IFS= read -r rule_line; do
        [ -n "${rule_line}" ] || continue
        rule_spec="${rule_line#*:}"
        [[ "${rule_spec}" == *"blackhole"* ]] || continue
        owned_quarantine=false
        if k3s_policy_rule_is_owned "${rule_line}"; then
            owned_quarantine=true
        fi
        [ "${owned_quarantine}" = true ] || continue
        rule_source="$(awk '{for (i = 1; i <= NF; i++) if ($i == "from") {print $(i + 1); exit}}' <<< "${rule_spec}")"
        [ -n "${rule_source}" ] || continue
        LAST_ERROR="k3s: Failed to remove emergency pod-CIDR quarantine"
        return 1
    done < <(ip rule show pref "${quarantine_pref}" 2>/dev/null || true)

    if ! remove_k3s_ipv6_subnet_quarantine; then
        return 1
    fi

    # A protocol-only pre-upgrade marker cannot distinguish our old rule from
    # a foreign rule created with the same protocol and preference. Preserve
    # ambiguous CIDR-wide rules and keep recovery visibly blocked rather than
    # deleting routing state we cannot prove we own.
    if [ -n "${K3S_LEGACY_RULE_PROTOCOL}" ]; then
        while IFS= read -r rule_line; do
            [ -n "${rule_line}" ] || continue
            rule_spec="${rule_line#*:}"
            [[ "${rule_spec}" == *"blackhole"* ]] || continue
            [[ "${rule_spec}" =~ (^|[[:space:]])proto(col)?[[:space:]]+${K3S_LEGACY_RULE_PROTOCOL}([[:space:]]|$) ]] || continue
            rule_source="$(awk '{for (i = 1; i <= NF; i++) if ($i == "from") {print $(i + 1); exit}}' <<< "${rule_spec}")"
            [[ "${rule_source}" == */* ]] || continue
            [[ "${rule_source}" != */32 ]] || continue
            LAST_ERROR="k3s: Preserving ambiguous legacy pod-CIDR quarantine at pref 32765; manual cleanup required"
            return 1
        done < <(ip rule show pref 32765 2>/dev/null || true)
    fi
    if ! clear_k3s_legacy_rule_protocol; then
        LAST_ERROR="k3s: Failed to retire legacy quarantine ownership marker"
        return 1
    fi
    return 0
}

refresh_k3s_discovery_before_guard_release() {
    load_k3s_pod_watch_state
    if [ "${K3S_POD_API_QUERY_OK:-false}" != "true" ]; then
        if [ "${K3S_UNSEEN_REPLACEMENT_PENDING:-false}" = "true" ]; then
            LAST_ERROR="k3s: Replacement pod is pending IP assignment; retaining pre-discovery containment"
            return 1
        fi
        return 0
    fi
    [ -n "${TUNNELSATS_K8S_NODE_NAME:-}" ] || return 0
    [ -n "${K3S_TARGET_POD_NAMESPACE:-}" ] && [ -n "${K3S_TARGET_POD_SELECTOR:-}" ] || return 0

    local encoded_selector fresh_pod_list prev_v6
    prev_v6="${K3S_CANDIDATE_POD_IPV6_ADDRESSES:-}"
    encoded_selector="$(urlencode "${K3S_TARGET_POD_SELECTOR}")"
    if ! fresh_pod_list="$(k8s_api "/api/v1/namespaces/${K3S_TARGET_POD_NAMESPACE}/pods?labelSelector=${encoded_selector}" 2>/dev/null)" || \
       ! printf '%s' "${fresh_pod_list}" | jq -e '.items | type == "array"' >/dev/null 2>&1; then
        K3S_POD_API_QUERY_OK="false"
        LAST_ERROR="k3s: Failed to refresh pod discovery before releasing emergency guards"
        return 1
    fi
    if ! pre_guard_colocated_k3s_candidate_pods "${fresh_pod_list}" "true"; then
        K3S_POD_API_QUERY_OK="false"
        return 1
    fi
    if [ "${K3S_CANDIDATE_POD_IPV6_ADDRESSES:-}" != "${prev_v6}" ] && [ -n "${K3S_CANDIDATE_POD_IPV6_ADDRESSES:-}" ]; then
        local candidate_v6
        local -a candidate_v6_list=()
        IFS=',' read -r -a candidate_v6_list <<< "${K3S_CANDIDATE_POD_IPV6_ADDRESSES}"
        for candidate_v6 in "${candidate_v6_list[@]}"; do
            [ -n "${candidate_v6}" ] || continue
            TARGET_IPV6_ADDRESSES+=("${candidate_v6}")
            TARGET_HAS_IPV6_DEFAULT_ROUTE="true"
        done
        if ! normalize_target_ipv6_addresses || ! ensure_ipv6_containment || ! ipv6_containment_is_synced; then
            LAST_ERROR="${LAST_ERROR:-k3s: Failed to contain replacement pod IPv6 before releasing emergency guards}"
            return 1
        fi
    fi
    if [ "${K3S_UNSEEN_REPLACEMENT_PENDING:-false}" = "true" ]; then
        LAST_ERROR="k3s: Replacement pod is pending IP assignment; retaining pre-discovery containment"
        return 1
    fi
    if [ -n "${DOCKER_TARGET_IP:-}" ] && \
       ! printf '%s' "${fresh_pod_list}" | jq -e \
           --arg node "${TUNNELSATS_K8S_NODE_NAME}" \
           --arg ip "${DOCKER_TARGET_IP}" '
           any(
               .items[]?;
               (.spec.nodeName // "") == $node
               and (.metadata.deletionTimestamp // null) == null
               and (.status.phase // "") == "Running"
               and (
                   (.status.podIP // "") == $ip
                   or any(.status.podIPs[]?; (.ip // "") == $ip)
               )
           )
       ' >/dev/null 2>&1; then
        LAST_ERROR="k3s: Selected target pod ${DOCKER_TARGET_IP} is no longer an active non-terminating Running pod on ${TUNNELSATS_K8S_NODE_NAME}"
        return 1
    fi
    return 0
}

release_k3s_reconcile_guards() {
    if [ -n "${DOCKER_TARGET_IP:-}" ]; then
        ensure_k3s_egress_guard_additive "${DOCKER_TARGET_IP}" || true
    fi
    if ! refresh_k3s_discovery_before_guard_release; then
        local refresh_err="${LAST_ERROR}"
        ensure_k3s_pre_discovery_containment "${DOCKER_TARGET_IP:-}" || true
        LAST_ERROR="${refresh_err}"
        return 1
    fi
    if ! ensure_k3s_unknown_pod_containment "${DOCKER_TARGET_IP:-}"; then
        local contain_err="${LAST_ERROR}"
        ensure_k3s_pre_discovery_containment "${DOCKER_TARGET_IP:-}" || true
        LAST_ERROR="${contain_err}"
        return 1
    fi
    if ! remove_k3s_subnet_quarantine; then
        return 1
    fi

    # Validate the effective dataplane again without the CIDR-wide fallback.
    # If anything changed during release, restore durable quarantine before
    # returning the failed reconciliation.
    if ! rules_are_synced; then
        if ensure_k3s_subnet_quarantine "${DOCKER_TARGET_IP}"; then
            LAST_ERROR="k3s: Dataplane validation failed after releasing emergency quarantine; quarantine restored"
        else
            LAST_ERROR="k3s: Dataplane validation failed after releasing emergency quarantine; restore failed"
        fi
        return 1
    fi

    # A successfully recovered dataplane must not be left behind an emergency
    # deny policy merely because the API was temporarily unavailable. Keep the
    # workload fail-closed while retrying, and only report recovery once the
    # policy is gone (or a foreign replacement has been safely preserved).
    while true; do
        if ! refresh_k3s_discovery_before_guard_release; then
            local refresh_err="${LAST_ERROR}"
            ensure_k3s_pre_discovery_containment "${DOCKER_TARGET_IP:-}" || true
            LAST_ERROR="${refresh_err}"
            return 1
        fi
        if remove_k3s_emergency_network_policy; then
            break
        fi
        LAST_ERROR="k3s: Dataplane recovered; retrying emergency NetworkPolicy cleanup"
        write_state
        sleep "${K3S_ISOLATION_RETRY_INTERVAL}"
    done
    if ! refresh_k3s_discovery_before_guard_release; then
        local refresh_err="${LAST_ERROR}"
        ensure_k3s_pre_discovery_containment "${DOCKER_TARGET_IP:-}" || true
        LAST_ERROR="${refresh_err}"
        return 1
    fi
    # Remove every tagged guard for the active target and stale IPs from failed
    # prior reconciliations only after the unguarded routing policy is verified
    # and the final discovery refresh has completed, while preserving pre-guards
    # on unready co-located replacement pods.
    if ! remove_k3s_egress_guards "true"; then
        return 1
    fi
    return 0
}

discover_secure_ipv6_target() {
    [[ "${SECURE_MODE}" == "true" ]] || return 0
    [ -n "${DOCKER_TARGET_IP:-}" ] || {
        LAST_ERROR="SecureMode: Cannot inspect IPv6 without a selected target"
        return 1
    }

    local route_info target_dev target_mac ipv6_context neighbor_line neighbor_address
    route_info=$(ip -4 route get "${DOCKER_TARGET_IP}" 2>/dev/null || true)
    target_dev=$(printf '%s\n' "${route_info}" | awk '
        NR == 1 { for (i = 1; i < NF; i++) if ($i == "dev") { print $(i + 1); exit } }
    ')
    [ -n "${target_dev}" ] || {
        LAST_ERROR="SecureMode: Cannot resolve target interface for IPv6 containment"
        return 1
    }

    ipv6_context="$({
        ip -6 -o addr show dev "${target_dev}" scope global 2>/dev/null || true
        ip -6 route show default dev "${target_dev}" 2>/dev/null || true
    } | sed '/^[[:space:]]*$/d')"
    if [ -z "${ipv6_context}" ]; then
        return 0
    fi
    if printf '%s\n' "${route_info}" | grep -qE '(^|[[:space:]])via([[:space:]]|$)'; then
        LAST_ERROR="SecureMode: IPv6-capable target is routed through a gateway and cannot be isolated by MAC"
        return 1
    fi

    # Secure Mode deliberately has no Docker socket. Match the selected
    # container by the link-layer identity learned for its fixed IPv4 address;
    # this blocks even temporary/privacy IPv6 addresses that are not yet in the
    # neighbor cache.
    target_mac=$(ip neigh show to "${DOCKER_TARGET_IP}" dev "${target_dev}" 2>/dev/null \
        | awk '{for (i = 1; i < NF; i++) if ($i == "lladdr") {print tolower($(i + 1)); exit}}')
    if [[ ! "${target_mac}" =~ ^([0-9a-f]{2}:){5}[0-9a-f]{2}$ ]]; then
        LAST_ERROR="SecureMode: IPv6-capable target network detected but target MAC is unavailable"
        return 1
    fi

    TARGET_HAS_IPV6_DEFAULT_ROUTE="true"
    TARGET_IPV6_MACS=("${target_mac}")
    while IFS= read -r neighbor_line; do
        [ -n "${neighbor_line}" ] || continue
        [[ "${neighbor_line,,}" == *"lladdr ${target_mac}"* ]] || continue
        neighbor_address="${neighbor_line%% *}"
        [[ "${neighbor_address}" == *:* ]] || continue
        TARGET_IPV6_ADDRESSES+=("${neighbor_address%%\%*}")
    done < <(ip -6 neigh show dev "${target_dev}" 2>/dev/null || true)
    normalize_target_ipv6_addresses
}

get_target_subnet() {
    local target_ip="${1:-${DOCKER_TARGET_IP:-}}"
    if [ -z "${target_ip}" ]; then
        echo "10.21.0.0/16"
        return
    fi
    local route_info dev_iface="" subnet=""
    route_info=$(ip route get "${target_ip}" 2>/dev/null || true)
    dev_iface=$(echo "${route_info}" | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1)}' || true)
    if [ -n "${dev_iface}" ]; then
        subnet=$(ip addr show dev "${dev_iface}" | awk '$1 == "inet" {print $2; exit}' || true)
    fi
    if [ -n "${subnet}" ]; then
        subnet=$(python3 -c "import sys, ipaddress; print(ipaddress.IPv4Network(sys.stdin.read().strip(), strict=False))" 2>/dev/null <<< "${subnet}" || echo "10.21.0.0/16")
    else
        subnet="10.21.0.0/16"
    fi
    echo "${subnet}"
}

ensure_source_blackhole_rule() {
    local source_prefix="$1"
    local preference="$2"
    local error_message="$3"
    local escaped_source="${source_prefix//./\\.}"
    local displayed_source="${escaped_source}"
    local source_pattern
    local exact_pattern
    local matching_rules
    local matching_count

    [[ "${source_prefix}" == */* ]] || displayed_source+="(/32)?"
    source_pattern="^${preference}:[[:space:]]+from[[:space:]]+${displayed_source}([[:space:]]|$)"
    exact_pattern="^${preference}:[[:space:]]+from[[:space:]]+${displayed_source}[[:space:]]+blackhole[[:space:]]*$"
    SOURCE_BLACKHOLE_CHANGED="0"

    matching_rules="$(ip rule show pref "${preference}" 2>/dev/null | grep -E "${source_pattern}" || true)"
    matching_count="$(printf '%s\n' "${matching_rules}" | sed '/^$/d' | wc -l)"
    if [ "${matching_count}" -eq 1 ] && printf '%s\n' "${matching_rules}" | grep -qE "${exact_pattern}"; then
        return 0
    fi

    local rule_line
    while IFS= read -r rule_line; do
        [ -n "${rule_line}" ] || continue
        delete_policy_rule_line "${rule_line}" || true
    done <<< "${matching_rules}"

    if ip rule show pref "${preference}" 2>/dev/null | grep -qE "${source_pattern}"; then
        LAST_ERROR="${error_message}: failed to remove conflicting pref ${preference} rule"
        return 1
    fi

    ip rule add from "${source_prefix}" blackhole pref "${preference}" >/dev/null 2>&1 || true
    matching_rules="$(ip rule show pref "${preference}" 2>/dev/null | grep -E "${source_pattern}" || true)"
    matching_count="$(printf '%s\n' "${matching_rules}" | sed '/^$/d' | wc -l)"
    if [ "${matching_count}" -ne 1 ] || ! printf '%s\n' "${matching_rules}" | grep -qE "${exact_pattern}"; then
        LAST_ERROR="${error_message}"
        return 1
    fi

    SOURCE_BLACKHOLE_CHANGED="1"
}

ensure_docker_target_kill_switches() {
    local refresh_error=""
    local entry
    local source
    local subnet
    local gateway
    local is_tunnel
    local priority

    if ! refresh_docker_target_endpoints; then
        refresh_error="${LAST_ERROR}"
    fi

    for entry in "${TARGET_IPV4_ENDPOINTS[@]}"; do
        IFS='|' read -r source subnet gateway is_tunnel priority <<< "${entry}"
        if ! ensure_source_blackhole_rule \
            "${source}" "${KILL_SWITCH_PREF}" \
            "Failed to install reconciliation kill switch for ${TARGET_CONTAINER_NAME} source ${source}"; then
            return 1
        fi
    done

    if [ -n "${refresh_error}" ]; then
        LAST_ERROR="${refresh_error}"
        return 1
    fi
}

ensure_reconcile_kill_switch() {
    local include_detected_target="${1:-true}"
    load_decommissioned_state || return 1
    if [[ "${K3S_MODE}" == "true" ]]; then
        load_managed_k3s_policy_state || return 1
    fi
    if [ "${DECOMMISSIONED:-false}" = "true" ]; then
        return 0
    fi
    if [[ "${K3S_MODE}" == "true" ]]; then
        if [ -n "${MANAGED_K3S_TARGET_IP:-}" ]; then
            local guard_ok=false
            local blackhole_ok=false
            if ensure_k3s_egress_guard "${MANAGED_K3S_TARGET_IP}"; then
                guard_ok=true
            fi
            if ensure_fallback_blackhole_rule \
                "${MANAGED_K3S_TARGET_IP}" \
                "k3s: Failed to protect managed pod before reconciliation" \
                "${K3S_RULE_PROTOCOL}"; then
                blackhole_ok=true
            fi
            if [ "${guard_ok}" = false ] && [ "${blackhole_ok}" = false ]; then
                return 1
            fi
        fi
        return 0
    fi

    if [[ "${SECURE_MODE}" == "true" ]]; then
        ensure_source_blackhole_rule \
            "10.21.21.9" "${KILL_SWITCH_PREF}" \
            "SecureMode: Failed to install LND reconciliation kill switch" || return 1
        ensure_source_blackhole_rule \
            "10.21.21.96" "${KILL_SWITCH_PREF}" \
            "SecureMode: Failed to install CLN reconciliation kill switch" || return 1
        return 0
    fi

    if ! ensure_source_blackhole_rule \
        "${DOCKER_NETWORK_SUBNET}" "${KILL_SWITCH_PREF}" \
        "Failed to install reconciliation kill switch for ${DOCKER_NETWORK_SUBNET}"; then
        return 1
    fi

    if [ "${include_detected_target}" = "true" ] && [ -n "${TARGET_CONTAINER_ID}" ]; then
        ensure_docker_target_kill_switches
    fi
}

remove_source_blackhole_rule() {
    local source_prefix="$1"
    local preference="$2"
    local escaped_source="${source_prefix//./\\.}"
    local displayed_source="${escaped_source}"
    local exact_pattern

    [[ "${source_prefix}" == */* ]] || displayed_source+="(/32)?"
    exact_pattern="^${preference}:[[:space:]]+from[[:space:]]+${displayed_source}[[:space:]]+blackhole[[:space:]]*$"
    ip rule del from "${source_prefix}" blackhole pref "${preference}" >/dev/null 2>&1 || true
    if ip rule show pref "${preference}" 2>/dev/null | grep -qE "${exact_pattern}"; then
        LAST_ERROR="Failed to release reconciliation kill switch for ${source_prefix}"
        return 1
    fi
}

remove_reconcile_kill_switch() {
    [[ "${K3S_MODE}" == "true" ]] && return 0

    if [[ "${SECURE_MODE}" == "true" ]]; then
        [ -n "${DOCKER_TARGET_IP:-}" ] || return 0
        remove_source_blackhole_rule "${DOCKER_TARGET_IP}" "${KILL_SWITCH_PREF}"
        return
    fi

    local entry
    local source
    local subnet
    local gateway
    local is_tunnel
    local priority
    local failed=false

    if ! remove_source_blackhole_rule "${DOCKER_NETWORK_SUBNET}" "${KILL_SWITCH_PREF}"; then
        failed=true
    fi
    for entry in "${MANAGED_DOCKER_IPV4_ENDPOINTS[@]}"; do
        IFS='|' read -r source subnet gateway is_tunnel priority <<< "${entry}"
        if ! remove_source_blackhole_rule "${source}" "${KILL_SWITCH_PREF}"; then
            failed=true
        fi
    done
    [ "${failed}" = false ]
}

wireguard_handshake_is_fresh() {
    local latest_handshake
    local now
    local age

    latest_handshake="$(wg show "${WG_IFACE}" latest-handshakes 2>/dev/null \
        | awk 'BEGIN { newest = 0 } $2 > newest { newest = $2 } END { print newest }' || true)"
    if [[ ! "${latest_handshake}" =~ ^[0-9]+$ ]] || [ "${latest_handshake}" -eq 0 ]; then
        LAST_ERROR="WireGuard has no completed handshake"
        return 1
    fi

    now="$(date +%s)"
    age=$((now - latest_handshake))
    if [ "${age}" -lt 0 ] || [ "${age}" -gt "${WG_HANDSHAKE_MAX_AGE}" ]; then
        LAST_ERROR="WireGuard handshake is stale (${age}s; maximum ${WG_HANDSHAKE_MAX_AGE}s)"
        return 1
    fi
}

ensure_fallback_blackhole_rule() {
    local source_prefix="$1"
    local error_message="$2"
    local rule_protocol="${3:-}"
    local escaped_source="${source_prefix//./\\.}"
    local source_pattern="^${K3S_BLACKHOLE_RULE_PREF}:[[:space:]]+from[[:space:]]+${escaped_source}([[:space:]]|$)"
    local exact_pattern="^${K3S_BLACKHOLE_RULE_PREF}:[[:space:]]+from[[:space:]]+${escaped_source}[[:space:]]+blackhole"
    local matching_rules
    local matching_count

    BLACKHOLE_CHANGED="0"
    matching_rules="$(ip rule show pref "${K3S_BLACKHOLE_RULE_PREF}" 2>/dev/null | grep -E "${source_pattern}" || true)"
    matching_count="$(printf '%s\n' "${matching_rules}" | sed '/^$/d' | wc -l)"

    if [ -n "${rule_protocol}" ]; then
        exact_pattern="${exact_pattern}([[:space:]].*)?[[:space:]]+proto(col)?[[:space:]]+${rule_protocol}[[:space:]]*$"
    else
        exact_pattern="${exact_pattern}[[:space:]]*$"
    fi

    if [ "${matching_count}" -eq 1 ] && printf '%s\n' "${matching_rules}" | grep -qE "${exact_pattern}"; then
        return 0
    fi

    local rule_line
    local rule_spec
    local -a rule_parts
    while IFS= read -r rule_line; do
        [ -n "${rule_line}" ] || continue
        rule_spec="${rule_line#*:}"
        read -r -a rule_parts <<< "${rule_spec}"
        [ "${#rule_parts[@]}" -gt 0 ] || continue
        if [ -n "${rule_protocol}" ]; then
            if ! [[ "${rule_spec}" =~ (^|[[:space:]])proto(col)?[[:space:]]+${rule_protocol}([[:space:]]|$) ]] || \
               { [ "${rule_protocol}" = "${K3S_RULE_PROTOCOL}" ] && ! k3s_policy_rule_is_owned "${rule_line}"; }; then
                LAST_ERROR="${error_message}: pref ${K3S_BLACKHOLE_RULE_PREF} is occupied by an unowned rule"
                return 1
            fi
        fi
        delete_policy_rule_line "${rule_line}" || true
    done <<< "${matching_rules}"

    if ip rule show pref "${K3S_BLACKHOLE_RULE_PREF}" 2>/dev/null | grep -qE "${source_pattern}"; then
        LAST_ERROR="${error_message}: failed to remove conflicting pref ${K3S_BLACKHOLE_RULE_PREF} rule"
        return 1
    fi

    if [ -n "${rule_protocol}" ]; then
        ip rule add from "${source_prefix}" blackhole protocol "${rule_protocol}" pref "${K3S_BLACKHOLE_RULE_PREF}" >/dev/null 2>&1 || true
    else
        ip rule add from "${source_prefix}" blackhole pref "${K3S_BLACKHOLE_RULE_PREF}" >/dev/null 2>&1 || true
    fi
    matching_rules="$(ip rule show pref "${K3S_BLACKHOLE_RULE_PREF}" 2>/dev/null | grep -E "${source_pattern}" || true)"
    matching_count="$(printf '%s\n' "${matching_rules}" | sed '/^$/d' | wc -l)"
    if [ "${matching_count}" -ne 1 ] || ! printf '%s\n' "${matching_rules}" | grep -qE "${exact_pattern}"; then
        LAST_ERROR="${error_message}"
        return 1
    fi

    BLACKHOLE_CHANGED="1"
    return 0
}

normalize_k3s_bypass_cidrs() {
    local normalized
    local validation_error

    if ! normalized="$(
        K3S_BYPASS_CIDRS_VALUE="${K3S_BYPASS_CIDRS}" python3 -c '
import ipaddress
import os

raw = os.environ.get("K3S_BYPASS_CIDRS_VALUE", "")
networks = set()
for value in raw.split(","):
    value = value.strip()
    if not value:
        continue
    try:
        network = ipaddress.ip_network(value, strict=False)
    except ValueError as exc:
        raise SystemExit(str(exc))
    if network.version != 4:
        raise SystemExit(f"{value!r} is not an IPv4 CIDR")
    if network.prefixlen == 0:
        raise SystemExit("default route 0.0.0.0/0 is not allowed")
    networks.add(network)

print(",".join(str(network) for network in sorted(
    networks,
    key=lambda item: (int(item.network_address), item.prefixlen),
)))
' 2>&1
    )"; then
        validation_error="${normalized//$'\n'/ }"
        LAST_ERROR="Invalid K3S_BYPASS_CIDRS: ${validation_error}"
        K3S_BYPASS_CIDRS_NORMALIZED=""
        return 1
    fi

    K3S_BYPASS_CIDRS_NORMALIZED="${normalized}"
    return 0
}

k3s_policy_rule_has_protocol() {
    local rule_spec="${1#*:}"
    [[ "${rule_spec}" =~ (^|[[:space:]])proto(col)?[[:space:]]+${K3S_RULE_PROTOCOL}([[:space:]]|$) ]]
}

k3s_policy_rule_is_owned() {
    local priority="${1%%:*}"

    # Both parts are randomly allocated from unused kernel state and remain
    # stable only for this boot. Protocol reuse alone is never ownership proof.
    k3s_policy_rule_has_protocol "$1" || return 1
    case "${priority}" in
        "${K3S_BYPASS_RULE_PREF}"|"${K3S_QUARANTINE_RULE_PREF}"|\
        "${K3S_TUNNEL_RULE_PREF}"|"${K3S_BLACKHOLE_RULE_PREF}") return 0 ;;
        *) return 1 ;;
    esac
}

delete_policy_rule_line() {
    local rule_line="$1"
    local priority="${rule_line%%:*}"
    local rule_spec="${rule_line#*:}"
    local -a rule_parts

    read -r -a rule_parts <<< "${rule_spec}"
    [ "${#rule_parts[@]}" -gt 0 ] || return 0
    ip rule del "${rule_parts[@]}" pref "${priority}" >/dev/null 2>&1
}

remove_stale_k3s_policy_rules() {
    local current_source="$1"
    local pref
    local rule_line
    local rule_spec
    local rule_source

    if k3s_acquire_watch_state_lock; then
        sync_k3s_watched_candidate_addresses || true
    fi

    for pref in "${K3S_BYPASS_RULE_PREF}" "${K3S_TUNNEL_RULE_PREF}" "${K3S_BLACKHOLE_RULE_PREF}"; do
        while IFS= read -r rule_line; do
            [ -n "${rule_line}" ] || continue
            rule_spec="${rule_line#*:}"

            case "${pref}:${rule_spec}" in
                "${K3S_BYPASS_RULE_PREF}":*" lookup main"*|"${K3S_BYPASS_RULE_PREF}":*" table main"*|\
                "${K3S_TUNNEL_RULE_PREF}":*" lookup 51820"*|"${K3S_TUNNEL_RULE_PREF}":*" table 51820"*|\
                "${K3S_BLACKHOLE_RULE_PREF}":*" blackhole"*)
                    ;;
                *)
                    continue
                    ;;
            esac

            # The old k3s implementation used this mark at pref 32764.
            if [[ "${rule_spec}" == *"fwmark 0xca6c"* ]] || [[ "${rule_spec}" == *"fwmark 0xCA6C"* ]]; then
                continue
            fi

            if [ "${pref}" = "${K3S_TUNNEL_RULE_PREF}" ] && \
               [[ "${rule_spec}" == *" oif ${WG_IFACE} "* ]]; then
                continue
            fi

            rule_source="$(awk '{for (i = 1; i <= NF; i++) if ($i == "from") {print $(i + 1); exit}}' <<< "${rule_spec}")"
            if { [ "${pref}" = "${K3S_BLACKHOLE_RULE_PREF}" ] || [ "${pref}" = "${K3S_BYPASS_RULE_PREF}" ]; } && \
               [[ "${rule_source}" == */* ]] && [[ "${rule_source}" != */32 ]]; then
                # CIDR-wide emergency quarantine and pod-subnet bypasses must
                # survive ordinary stale per-pod cleanup; only explicit full
                # cleanup may remove them.
                continue
            fi
            if [ "${pref}" = "${K3S_BLACKHOLE_RULE_PREF}" ] && \
               k3s_ip_is_in_csv "${rule_source%/32}" "${K3S_CANDIDATE_POD_IPS:-}"; then
                continue
            fi
            if [ "${pref}" = "${K3S_BYPASS_RULE_PREF}" ] && \
               [[ "${rule_spec}" != *" to "* ]] && \
               [[ "${rule_spec}" == *" fwmark "* ]] && \
               [[ "${rule_spec}" == *" iif "* ]] && \
               [ "${rule_source%/32}" != "${current_source%/32}" ] && \
               ! k3s_ip_is_in_csv "${rule_source%/32}" "${K3S_CANDIDATE_POD_IPS:-}" && \
               k3s_ip_is_in_csv "${rule_source%/32}" "${K3S_KNOWN_NON_TARGET_POD_IPS:-}"; then
                continue
            fi
            if [ -n "${rule_source}" ] && \
               [ "${rule_source%/32}" != "${current_source%/32}" ] && \
               k3s_policy_rule_is_owned "${rule_line}"; then
                if [ "${pref}" = "${K3S_BYPASS_RULE_PREF}" ] && [[ "${rule_spec}" != *" to "* ]]; then
                    revoke_k3s_non_target_pod_exemption "4" "${rule_source%/32}"
                else
                    delete_policy_rule_line "${rule_line}"
                    POLICY_CHANGED="1"
                fi
            fi
        done < <(ip rule show pref "${pref}" 2>/dev/null || true)
    done
    k3s_release_watch_state_lock
}

remove_legacy_k3s_fwmark_rules() {
    local changed=0
    local legacy_rule
    local legacy_rules

    legacy_rules="$(ip rule show 2>/dev/null | grep -Ei \
        "fwmark 0x0*ca6c.*(lookup|table)[[:space:]]+51820" || true)"
    if [ -z "${legacy_rules}" ]; then
        K3S_LEGACY_POLICY_CHANGED="0"
        return 0
    fi

    # Untagged policy rules are not sufficient proof of ownership. Only
    # migrate the legacy rule when its paired TunnelSats mangle rules still
    # provide an independently tagged ownership signal.
    if ! iptables -t mangle -S PREROUTING 2>/dev/null | grep -Fq -- "tunnelsats-conn-restore" || \
       ! iptables -t mangle -S FORWARD 2>/dev/null | grep -Fq -- "tunnelsats-conn-save"; then
        LAST_ERROR="k3s: Unowned legacy fwmark rule conflicts with full-outbound routing"
        K3S_LEGACY_POLICY_CHANGED="0"
        return 1
    fi

    while IFS= read -r legacy_rule; do
        [ -n "${legacy_rule}" ] || continue
        delete_policy_rule_line "${legacy_rule}"
        changed=1
    done <<< "${legacy_rules}"

    K3S_LEGACY_POLICY_CHANGED="${changed}"
    return 0
}

ensure_k3s_bypass_rules() {
    local changed=0
    local rule_line
    local rule_spec
    local destination
    local cidr
    local installed_rule
    local bypass_csv=",${K3S_BYPASS_CIDRS_NORMALIZED},"
    local -a bypass_cidrs=()

    if [ -n "${K3S_BYPASS_CIDRS_NORMALIZED}" ]; then
        IFS=',' read -r -a bypass_cidrs <<< "${K3S_BYPASS_CIDRS_NORMALIZED}"
    fi

    # Remove bypasses that are no longer configured.
    while IFS= read -r rule_line; do
        [ -n "${rule_line}" ] || continue
        rule_spec="${rule_line#*:}"
        [[ "${rule_spec}" == *"from ${DOCKER_TARGET_IP} "* ]] || continue
        k3s_policy_rule_is_owned "${rule_line}" || continue
        if [[ "${rule_spec}" != *" lookup main"* ]] && [[ "${rule_spec}" != *" table main"* ]]; then
            continue
        fi
        destination="$(awk '{for (i = 1; i <= NF; i++) if ($i == "to") {print $(i + 1); exit}}' <<< "${rule_spec}")"
        if [ -z "${destination}" ] || [[ "${bypass_csv}" != *",${destination},"* ]]; then
            delete_policy_rule_line "${rule_line}"
            changed=1
        fi
    done < <(ip rule show pref "${K3S_BYPASS_RULE_PREF}" 2>/dev/null || true)

    for cidr in "${bypass_cidrs[@]}"; do
        if ! ip rule show pref "${K3S_BYPASS_RULE_PREF}" 2>/dev/null | grep -qE \
            "from[[:space:]]+${DOCKER_TARGET_IP//./\\.}[[:space:]]+to[[:space:]]+${cidr//./\\.}[[:space:]]+(lookup|table)[[:space:]]+main([[:space:]].*)?[[:space:]]+proto(col)?[[:space:]]+${K3S_RULE_PROTOCOL}"; then
            if ! ip rule add from "${DOCKER_TARGET_IP}" to "${cidr}" table main protocol "${K3S_RULE_PROTOCOL}" pref "${K3S_BYPASS_RULE_PREF}" >/dev/null 2>&1; then
                LAST_ERROR="k3s: Failed to add local bypass for ${cidr}"
                return 1
            fi
            installed_rule="$(ip rule show pref "${K3S_BYPASS_RULE_PREF}" 2>/dev/null | grep -E \
                "from[[:space:]]+${DOCKER_TARGET_IP//./\\.}[[:space:]]+to[[:space:]]+${cidr//./\\.}[[:space:]]+(lookup|table)[[:space:]]+main([[:space:]].*)?[[:space:]]+proto(col)?[[:space:]]+${K3S_RULE_PROTOCOL}" \
                | head -n 1 || true)"
            if [ -z "${installed_rule}" ]; then
                LAST_ERROR="k3s: Local bypass rule was not installed"
                return 1
            fi
            changed=1
        fi
    done

    K3S_BYPASS_CHANGED="${changed}"
    return 0
}

ensure_k3s_policy_table_defaults() {
    local changed=0
    local route_line
    local source_rule
    local -a route_parts

    # Seed the fail-closed route before inspecting or replacing any usable
    # default in the policy table.
    if ! ip route replace blackhole default metric 3 table 51820 >/dev/null 2>&1; then
        LAST_ERROR="k3s: Failed to set policy table blackhole"
        return 1
    fi

    while IFS= read -r route_line; do
        [ -n "${route_line}" ] || continue
        if [[ "${route_line}" == blackhole\ default* ]] && [[ "${route_line}" == *"metric 3"* ]]; then
            continue
        fi
        if [[ "${route_line}" == default* ]] && \
           [[ "${route_line}" == *"dev ${WG_IFACE}"* ]] && \
           [[ "${route_line}" == *"metric 2"* ]]; then
            continue
        fi

        # An unexpected default could override WireGuard. Remove the active
        # source rule first so the source blackhole protects this repair.
        source_rule="$(ip rule show pref "${K3S_TUNNEL_RULE_PREF}" 2>/dev/null | grep -E \
            "^${K3S_TUNNEL_RULE_PREF}:[[:space:]]+from[[:space:]]+${DOCKER_TARGET_IP//./\\.}[[:space:]]+(lookup|table)[[:space:]]+51820([[:space:]].*)?[[:space:]]+proto(col)?[[:space:]]+${K3S_RULE_PROTOCOL}[[:space:]]*$" \
            | head -n 1 || true)"
        if [ -n "${source_rule}" ]; then
            if ! k3s_policy_rule_is_owned "${source_rule}"; then
                LAST_ERROR="k3s: Refusing to remove unowned source rule while repairing table 51820"
                return 1
            fi
            delete_policy_rule_line "${source_rule}" || true
        fi
        if ip rule show | grep -qE \
            "^${K3S_TUNNEL_RULE_PREF}:[[:space:]]+from[[:space:]]+${DOCKER_TARGET_IP//./\\.}[[:space:]]+(lookup|table)[[:space:]]+51820([[:space:]].*)?[[:space:]]+proto(col)?[[:space:]]+${K3S_RULE_PROTOCOL}[[:space:]]*$"; then
            LAST_ERROR="k3s: Failed to suspend source routing while repairing table 51820"
            return 1
        fi

        read -r -a route_parts <<< "${route_line}"
        if ! ip route del table 51820 "${route_parts[@]}" >/dev/null 2>&1; then
            LAST_ERROR="k3s: Failed to remove unexpected table 51820 default"
            return 1
        fi
        changed=1
    done < <(
        ip route show table 51820 2>/dev/null \
            | grep -E "^(blackhole[[:space:]]+)?default([[:space:]]|$)" \
            || true
    )

    if ! ip route replace default dev "${WG_IFACE}" metric 2 table 51820 >/dev/null 2>&1; then
        LAST_ERROR="k3s: Failed to set WireGuard policy table default"
        return 1
    fi

    K3S_TABLE_CHANGED="${changed}"
    return 0
}

ensure_docker_source_policy_rules() {
    local changed=0
    local entry
    local source
    local subnet
    local gateway
    local is_tunnel
    local priority
    local escaped_source
    local escaped_subnet

    if ! refresh_docker_target_endpoints; then
        return 1
    fi
    if ! remove_stale_docker_source_policy_rules; then
        return 1
    fi
    if [ "${DOCKER_STALE_POLICY_CHANGED}" = "1" ]; then
        changed=1
    fi

    # Install every source fallback before adding any usable main/WireGuard
    # route. If reconciliation stops midway, no target address can fall through
    # to the host's ordinary default route.
    for entry in "${TARGET_IPV4_ENDPOINTS[@]}"; do
        IFS='|' read -r source subnet gateway is_tunnel priority <<< "${entry}"
        if ! ensure_fallback_blackhole_rule \
            "${source}" \
            "Failed to add fallback blackhole rule for ${TARGET_CONTAINER_NAME} source ${source}"; then
            return 1
        fi
        if [ "${BLACKHOLE_CHANGED}" = "1" ]; then
            changed=1
        fi
    done

    for entry in "${TARGET_IPV4_ENDPOINTS[@]}"; do
        IFS='|' read -r source subnet gateway is_tunnel priority <<< "${entry}"
        escaped_source="${source//./\\.}"
        escaped_subnet="${subnet//./\\.}"

        # Only the endpoint's directly connected subnet bypasses WireGuard.
        # This preserves local Bitcoin/Tor dependencies without admitting an
        # external-capable destination through the ordinary Docker gateway.
        if ! ip rule show pref 32500 2>/dev/null | grep -qE \
            "from[[:space:]]+${escaped_source}(/32)?[[:space:]]+to[[:space:]]+${escaped_subnet}[[:space:]]+(lookup|table)[[:space:]]+main([[:space:]]|$)"; then
            if ! ip rule add from "${source}" to "${subnet}" table main pref 32500 >/dev/null 2>&1; then
                LAST_ERROR="Failed to add local bypass for ${source} to ${subnet}"
                return 1
            fi
            changed=1
        fi

        if ! ip rule show pref 32764 2>/dev/null | grep -qE \
            "from[[:space:]]+${escaped_source}(/32)?[[:space:]]+(lookup|table)[[:space:]]+51820([[:space:]]|$)"; then
            if ! ip rule add from "${source}" table 51820 pref 32764 >/dev/null 2>&1; then
                LAST_ERROR="Failed to route ${TARGET_CONTAINER_NAME} source ${source} through WireGuard"
                return 1
            fi
            changed=1
        fi
    done

    DOCKER_SOURCE_POLICY_CHANGED="${changed}"
}

docker_source_policy_rules_are_synced() {
    local entry
    local source
    local subnet
    local gateway
    local is_tunnel
    local priority
    local escaped_source
    local escaped_subnet

    if ! refresh_docker_target_endpoints; then
        log WARN "rules_are_synced: ${LAST_ERROR}"
        return 1
    fi

    for entry in "${TARGET_IPV4_ENDPOINTS[@]}"; do
        IFS='|' read -r source subnet gateway is_tunnel priority <<< "${entry}"
        escaped_source="${source//./\\.}"
        escaped_subnet="${subnet//./\\.}"
        if ! ip rule show pref 32500 2>/dev/null | grep -qE \
            "from[[:space:]]+${escaped_source}(/32)?[[:space:]]+to[[:space:]]+${escaped_subnet}[[:space:]]+(lookup|table)[[:space:]]+main([[:space:]]|$)"; then
            log WARN "rules_are_synced: local bypass for ${source} to ${subnet} FAIL"
            return 1
        fi
        if ! ip rule show pref 32764 2>/dev/null | grep -qE \
            "from[[:space:]]+${escaped_source}(/32)?[[:space:]]+(lookup|table)[[:space:]]+51820([[:space:]]|$)"; then
            log WARN "rules_are_synced: WireGuard source rule for ${source} FAIL"
            return 1
        fi
        if ! ip rule show pref "${K3S_BLACKHOLE_RULE_PREF}" 2>/dev/null | grep -qE \
            "^${K3S_BLACKHOLE_RULE_PREF}:[[:space:]]+from[[:space:]]+${escaped_source}(/32)?[[:space:]]+blackhole[[:space:]]*$"; then
            log WARN "rules_are_synced: source fallback blackhole for ${source} FAIL"
            return 1
        fi
    done
}

remove_stale_docker_source_policy_rules() {
    local managed_entry
    local current_entry
    local managed_source
    local managed_subnet
    local current_source
    local current_subnet
    local source_is_current
    local subnet_is_current
    local -a current_managed=()
    DOCKER_STALE_POLICY_CHANGED="0"

    for managed_entry in "${MANAGED_DOCKER_IPV4_ENDPOINTS[@]}"; do
        IFS='|' read -r managed_source managed_subnet _ <<< "${managed_entry}"
        source_is_current=false
        subnet_is_current=false
        for current_entry in "${TARGET_IPV4_ENDPOINTS[@]}"; do
            IFS='|' read -r current_source current_subnet _ <<< "${current_entry}"
            if [ "${current_source}" = "${managed_source}" ]; then
                source_is_current=true
                if [ "${current_subnet}" = "${managed_subnet}" ]; then
                    subnet_is_current=true
                fi
            fi
        done

        if [ "${subnet_is_current}" = false ]; then
            ip rule del from "${managed_source}" to "${managed_subnet}" table main pref 32500 >/dev/null 2>&1 || true
        fi
        if [ "${source_is_current}" = false ]; then
            ip rule del from "${managed_source}" table 51820 pref 32764 >/dev/null 2>&1 || true
            ip rule del from "${managed_source}" blackhole pref "${KILL_SWITCH_PREF}" >/dev/null 2>&1 || true
            ip rule del from "${managed_source}" blackhole pref 32765 >/dev/null 2>&1 || true
            DOCKER_STALE_POLICY_CHANGED="1"
        fi
    done

    # Retain only the currently inspected endpoint identities. This bounds the
    # ownership set and prevents a reassigned stale Docker IP from remaining
    # routed or blackholed after a disconnect/reconnect cycle.
    for current_entry in "${TARGET_IPV4_ENDPOINTS[@]}"; do
        current_managed+=("${current_entry}")
    done
    MANAGED_DOCKER_IPV4_ENDPOINTS=("${current_managed[@]}")
    if ! persist_managed_docker_policy_state; then
        return 1
    fi
}

ipv6_rules_for_source_pref() {
    local source="$1"
    local preference="$2"
    ip -6 rule show pref "${preference}" 2>/dev/null | awk -v source="${source}" '
        {
            for (i = 1; i < NF; i++) {
                if ($i == "from" && ($(i + 1) == source || $(i + 1) == source "/128")) {
                    print
                    break
                }
            }
        }
    '
}

ipv6_address_is_managed() {
    local source="$1"
    local managed_source
    for managed_source in "${MANAGED_TARGET_IPV6_ADDRESSES[@]}"; do
        [ "${managed_source}" = "${source}" ] && return 0
    done
    return 1
}

ensure_ipv6_source_blackhole_rule() {
    local source="$1"
    local rules rule_count exact_pattern
    exact_pattern="^${IPV6_BLACKHOLE_RULE_PREF}:[[:space:]]+from[[:space:]]+${source}(/128)?[[:space:]]+blackhole[[:space:]]*$"
    rules=$(ipv6_rules_for_source_pref "${source}" "${IPV6_BLACKHOLE_RULE_PREF}" || true)
    rule_count=$(printf '%s\n' "${rules}" | sed '/^$/d' | wc -l)
    if [ "${rule_count}" -gt 0 ] && ! ipv6_address_is_managed "${source}"; then
        LAST_ERROR="IPv6 blackhole preference is occupied by an unowned rule for ${source}"
        return 1
    fi
    if [ "${rule_count}" -eq 1 ] && printf '%s\n' "${rules}" | grep -qE "${exact_pattern}"; then
        return 0
    fi
    if ipv6_address_is_managed "${source}"; then
        for _delete_attempt in 1 2 3 4; do
            ip -6 rule del from "${source}" blackhole pref "${IPV6_BLACKHOLE_RULE_PREF}" >/dev/null 2>&1 || break
        done
    fi
    if [ -n "$(ipv6_rules_for_source_pref "${source}" "${IPV6_BLACKHOLE_RULE_PREF}" || true)" ]; then
        LAST_ERROR="Failed to clear conflicting IPv6 blackhole rules for ${source}"
        return 1
    fi
    if ! ip -6 rule add from "${source}" blackhole pref "${IPV6_BLACKHOLE_RULE_PREF}" >/dev/null 2>&1; then
        LAST_ERROR="Failed to install IPv6 fallback blackhole for ${source}"
        return 1
    fi
    rules=$(ipv6_rules_for_source_pref "${source}" "${IPV6_BLACKHOLE_RULE_PREF}" || true)
    rule_count=$(printf '%s\n' "${rules}" | sed '/^$/d' | wc -l)
    if [ "${rule_count}" -ne 1 ] || ! printf '%s\n' "${rules}" | grep -qE "${exact_pattern}"; then
        LAST_ERROR="IPv6 fallback blackhole verification failed for ${source}"
        return 1
    fi
}

ensure_ipv6_policy_table() {
    local routes expected_count unexpected_count
    routes=$(ip -6 route show table "${IPV6_POLICY_TABLE}" 2>/dev/null || true)
    expected_count=$(printf '%s\n' "${routes}" | grep -cE \
        "^blackhole[[:space:]]+default([[:space:]].*)metric[[:space:]]+${IPV6_BLACKHOLE_METRIC}([[:space:]]|$)" || true)
    unexpected_count=$(printf '%s\n' "${routes}" | grep -cvE \
        "^$|^blackhole[[:space:]]+default([[:space:]].*)metric[[:space:]]+${IPV6_BLACKHOLE_METRIC}([[:space:]]|$)" || true)
    if [ "${unexpected_count}" -ne 0 ] || [ "${expected_count}" -gt 1 ]; then
        LAST_ERROR="IPv6 policy table ${IPV6_POLICY_TABLE} contains unowned routes"
        return 1
    fi
    if [ "${expected_count}" -eq 1 ] && [ "${IPV6_POLICY_TABLE_MANAGED}" != "true" ]; then
        LAST_ERROR="IPv6 policy table ${IPV6_POLICY_TABLE} contains an unowned blackhole"
        return 1
    fi
    if [ "${expected_count}" -eq 0 ]; then
        if ! ip -6 route replace blackhole default metric "${IPV6_BLACKHOLE_METRIC}" table "${IPV6_POLICY_TABLE}" >/dev/null 2>&1; then
            LAST_ERROR="Failed to install IPv6 policy-table blackhole"
            return 1
        fi
        IPV6_POLICY_TABLE_MANAGED="true"
        if ! persist_managed_ipv6_policy_state; then
            ip -6 route del blackhole default metric "${IPV6_BLACKHOLE_METRIC}" table "${IPV6_POLICY_TABLE}" >/dev/null 2>&1 || true
            IPV6_POLICY_TABLE_MANAGED="false"
            return 1
        fi
    fi
    routes=$(ip -6 route show table "${IPV6_POLICY_TABLE}" 2>/dev/null || true)
    if [ "$(printf '%s\n' "${routes}" | sed '/^$/d' | wc -l)" -ne 1 ] || \
       ! printf '%s\n' "${routes}" | grep -qE \
        "^blackhole[[:space:]]+default([[:space:]].*)metric[[:space:]]+${IPV6_BLACKHOLE_METRIC}([[:space:]]|$)"; then
        LAST_ERROR="IPv6 policy-table blackhole verification failed"
        return 1
    fi
}

ensure_ipv6_source_policy_rule() {
    local source="$1"
    local rules rule_count exact_pattern
    exact_pattern="^${IPV6_TUNNEL_RULE_PREF}:[[:space:]]+from[[:space:]]+${source}(/128)?[[:space:]]+(lookup|table)[[:space:]]+${IPV6_POLICY_TABLE}[[:space:]]*$"
    rules=$(ipv6_rules_for_source_pref "${source}" "${IPV6_TUNNEL_RULE_PREF}" || true)
    rule_count=$(printf '%s\n' "${rules}" | sed '/^$/d' | wc -l)
    if [ "${rule_count}" -gt 0 ] && ! ipv6_address_is_managed "${source}"; then
        LAST_ERROR="IPv6 policy preference is occupied by an unowned rule for ${source}"
        return 1
    fi
    if [ "${rule_count}" -eq 1 ] && printf '%s\n' "${rules}" | grep -qE "${exact_pattern}"; then
        return 0
    fi
    if ipv6_address_is_managed "${source}"; then
        for _delete_attempt in 1 2 3 4; do
            ip -6 rule del from "${source}" table "${IPV6_POLICY_TABLE}" pref "${IPV6_TUNNEL_RULE_PREF}" >/dev/null 2>&1 || break
        done
    fi
    if [ -n "$(ipv6_rules_for_source_pref "${source}" "${IPV6_TUNNEL_RULE_PREF}" || true)" ]; then
        LAST_ERROR="Failed to clear conflicting IPv6 policy rules for ${source}"
        return 1
    fi
    if ! ip -6 rule add from "${source}" table "${IPV6_POLICY_TABLE}" pref "${IPV6_TUNNEL_RULE_PREF}" >/dev/null 2>&1; then
        LAST_ERROR="Failed to install IPv6 policy rule for ${source}"
        return 1
    fi
    rules=$(ipv6_rules_for_source_pref "${source}" "${IPV6_TUNNEL_RULE_PREF}" || true)
    rule_count=$(printf '%s\n' "${rules}" | sed '/^$/d' | wc -l)
    if [ "${rule_count}" -ne 1 ] || ! printf '%s\n' "${rules}" | grep -qE "${exact_pattern}"; then
        LAST_ERROR="IPv6 policy-rule verification failed for ${source}"
        return 1
    fi
}

ipv6_filter_rule_is_current() {
    local rule="$1"
    local source mac
    for source in "${TARGET_IPV6_ADDRESSES[@]}"; do
        if [[ "${rule}" == *" -s ${source}/128 "* ]] || [[ "${rule}" == *" -s ${source} "* ]]; then
            return 0
        fi
    done
    for mac in "${TARGET_IPV6_MACS[@]}"; do
        [[ "${rule,,}" == *" --mac-source ${mac,,} "* ]] && return 0
    done
    return 1
}

remove_stale_ipv6_filter_rules() {
    local rules rule
    local -a parts
    rules=$(ip6tables -S FORWARD 2>/dev/null | grep -F -- "--comment ${IPV6_RULE_MARKER}" || true)
    while IFS= read -r rule; do
        [ -n "${rule}" ] || continue
        ipv6_filter_rule_is_current "${rule}" && continue
        read -r -a parts <<< "${rule}"
        parts[0]="-D"
        ip6tables "${parts[@]}" >/dev/null 2>&1 || {
            LAST_ERROR="Failed to remove stale owned IPv6 filter rule"
            return 1
        }
    done <<< "${rules}"
}

_ensure_ipv6_containment_inner() {
    local source mac managed_source current_source already_current was_managed
    local filter_ok=true
    local routing_ok=true
    local ip6tables_available=true
    local containment_errors=""
    local -a next_managed=()
    if ! load_managed_ipv6_policy_state; then
        return 1
    fi
    if [ "${#TARGET_IPV6_ADDRESSES[@]}" -eq 0 ] && \
       [ "${#TARGET_IPV6_MACS[@]}" -eq 0 ] && \
       [ "${TARGET_HAS_IPV6_DEFAULT_ROUTE}" != "true" ] && \
       [ "${#MANAGED_TARGET_IPV6_ADDRESSES[@]}" -eq 0 ] && \
       [ "${IPV6_POLICY_TABLE_MANAGED}" != "true" ]; then
        return 0
    fi
    if [ "${#TARGET_IPV6_ADDRESSES[@]}" -eq 0 ] && \
       [ "${#TARGET_IPV6_MACS[@]}" -eq 0 ] && \
       [ "${TARGET_HAS_IPV6_DEFAULT_ROUTE}" = "true" ]; then
        LAST_ERROR="IPv6-capable target network detected without a safe workload selector"
        return 1
    fi
    # Install the packet-filter control first so a later policy-routing failure
    # cannot leave the workload on its ordinary IPv6 default route.
    if ! command -v ip6tables >/dev/null 2>&1; then
        filter_ok=false
        ip6tables_available=false
        containment_errors="ip6tables is unavailable for IPv6 fail-closed containment"
    fi
    for source in "${TARGET_IPV6_ADDRESSES[@]}"; do
        if [ "${ip6tables_available}" = true ] && ! ip6tables -C FORWARD -s "${source}" ! -d fe80::/10 \
            -m comment --comment "${IPV6_RULE_MARKER}" -j DROP >/dev/null 2>&1; then
            if ! ip6tables -I FORWARD 1 -s "${source}" ! -d fe80::/10 \
                -m comment --comment "${IPV6_RULE_MARKER}" -j DROP >/dev/null 2>&1; then
                filter_ok=false
                containment_errors="${containment_errors:+${containment_errors}; }failed to install IPv6 egress filter for ${source}"
            fi
        fi
    done
    for mac in "${TARGET_IPV6_MACS[@]}"; do
        if [ "${ip6tables_available}" = true ] && ! ip6tables -C FORWARD -m mac --mac-source "${mac}" ! -d fe80::/10 \
            -m comment --comment "${IPV6_RULE_MARKER}" -j DROP >/dev/null 2>&1; then
            if ! ip6tables -I FORWARD 1 -m mac --mac-source "${mac}" ! -d fe80::/10 \
                -m comment --comment "${IPV6_RULE_MARKER}" -j DROP >/dev/null 2>&1; then
                filter_ok=false
                containment_errors="${containment_errors:+${containment_errors}; }failed to install IPv6 egress filter for target MAC ${mac}"
            fi
        fi
    done

    # The direct source blackhole is installed first. It remains effective if
    # the dedicated policy table is later flushed or otherwise loses its route.
    for source in "${TARGET_IPV6_ADDRESSES[@]}"; do
        was_managed=false
        ipv6_address_is_managed "${source}" && was_managed=true
        if ! ensure_ipv6_source_blackhole_rule "${source}"; then
            routing_ok=false
            containment_errors="${containment_errors:+${containment_errors}; }${LAST_ERROR}"
            continue
        fi
        if [ "${was_managed}" = false ]; then
            MANAGED_TARGET_IPV6_ADDRESSES+=("${source}")
            if ! persist_managed_ipv6_policy_state; then
                ip -6 rule del from "${source}" blackhole pref "${IPV6_BLACKHOLE_RULE_PREF}" >/dev/null 2>&1 || true
                MANAGED_TARGET_IPV6_ADDRESSES=("${MANAGED_TARGET_IPV6_ADDRESSES[@]:0:${#MANAGED_TARGET_IPV6_ADDRESSES[@]}-1}")
                routing_ok=false
                containment_errors="${containment_errors:+${containment_errors}; }${LAST_ERROR}"
            fi
        fi
    done
    if ! ensure_ipv6_policy_table; then
        routing_ok=false
        containment_errors="${containment_errors:+${containment_errors}; }${LAST_ERROR}"
    fi
    if [ "${IPV6_POLICY_TABLE_MANAGED}" = true ]; then
        for source in "${TARGET_IPV6_ADDRESSES[@]}"; do
            ipv6_address_is_managed "${source}" || continue
            if ! ensure_ipv6_source_policy_rule "${source}"; then
                routing_ok=false
                containment_errors="${containment_errors:+${containment_errors}; }${LAST_ERROR}"
            fi
        done
    fi
    if [ "${filter_ok}" = true ]; then
        if ! remove_stale_ipv6_filter_rules; then
            filter_ok=false
            containment_errors="${containment_errors:+${containment_errors}; }${LAST_ERROR}"
        fi
    fi

    # Retire stale routing rules only after every current selector is guarded.
    next_managed=()
    for managed_source in "${MANAGED_TARGET_IPV6_ADDRESSES[@]}"; do
        already_current=false
        for current_source in "${TARGET_IPV6_ADDRESSES[@]}"; do
            [ "${managed_source}" = "${current_source}" ] && already_current=true
        done
        if [ "${already_current}" = true ]; then
            next_managed+=("${managed_source}")
            continue
        fi
        for _delete_attempt in 1 2 3 4; do
            ip -6 rule del from "${managed_source}" table "${IPV6_POLICY_TABLE}" pref "${IPV6_TUNNEL_RULE_PREF}" >/dev/null 2>&1 || break
        done
        for _delete_attempt in 1 2 3 4; do
            ip -6 rule del from "${managed_source}" blackhole pref "${IPV6_BLACKHOLE_RULE_PREF}" >/dev/null 2>&1 || break
        done
    done
    MANAGED_TARGET_IPV6_ADDRESSES=("${next_managed[@]}")
    if [ "${#TARGET_IPV6_ADDRESSES[@]}" -eq 0 ] && \
       [ "${#TARGET_IPV6_MACS[@]}" -eq 0 ] && \
       [ "${TARGET_HAS_IPV6_DEFAULT_ROUTE}" != "true" ] && \
       [ "${IPV6_POLICY_TABLE_MANAGED}" = "true" ]; then
        ip -6 route del blackhole default metric "${IPV6_BLACKHOLE_METRIC}" table "${IPV6_POLICY_TABLE}" >/dev/null 2>&1 || true
        if ip -6 route show table "${IPV6_POLICY_TABLE}" 2>/dev/null | grep -qE \
            "^blackhole[[:space:]]+default([[:space:]].*)metric[[:space:]]+${IPV6_BLACKHOLE_METRIC}([[:space:]]|$)"; then
            routing_ok=false
            containment_errors="${containment_errors:+${containment_errors}; }failed to retire the owned IPv6 policy table"
        else
            IPV6_POLICY_TABLE_MANAGED="false"
        fi
    fi
    if ! persist_managed_ipv6_policy_state; then
        routing_ok=false
        containment_errors="${containment_errors:+${containment_errors}; }${LAST_ERROR}"
    fi
    if [ "${filter_ok}" != true ] || [ "${routing_ok}" != true ]; then
        LAST_ERROR="${containment_errors:-IPv6 containment reconciliation failed}"
        return 1
    fi
}

ensure_ipv6_containment() {
    local rc=0
    if [[ "${K3S_MODE}" == "true" ]]; then
        if ! k3s_acquire_watch_state_lock; then
            LAST_ERROR="k3s: Failed to acquire pod watch state lock for IPv6 containment"
            return 1
        fi
        if ! sync_k3s_watched_candidate_addresses; then
            k3s_release_watch_state_lock
            return 1
        fi
    fi
    _ensure_ipv6_containment_inner || rc=$?
    if [[ "${K3S_MODE}" == "true" ]]; then
        k3s_release_watch_state_lock
    fi
    return "${rc}"
}

ipv6_containment_is_synced() {
    local source mac rules rule_count routes exact_blackhole_pattern exact_policy_pattern
    if [ "${#TARGET_IPV6_ADDRESSES[@]}" -eq 0 ] && \
       [ "${#TARGET_IPV6_MACS[@]}" -eq 0 ] && \
       [ "${TARGET_HAS_IPV6_DEFAULT_ROUTE}" != "true" ]; then
        return 0
    fi
    if [ "${#TARGET_IPV6_ADDRESSES[@]}" -eq 0 ] && [ "${#TARGET_IPV6_MACS[@]}" -eq 0 ]; then
        log WARN "ipv6_rules_are_synced: IPv6-capable target has no safe selector"
        return 1
    fi
    if [ "${IPV6_POLICY_TABLE_MANAGED}" != "true" ]; then
        log WARN "ipv6_rules_are_synced: policy table is not owned"
        return 1
    fi
    routes=$(ip -6 route show table "${IPV6_POLICY_TABLE}" 2>/dev/null || true)
    if [ "$(printf '%s\n' "${routes}" | sed '/^$/d' | wc -l)" -ne 1 ] || \
       ! printf '%s\n' "${routes}" | grep -qE \
        "^blackhole[[:space:]]+default([[:space:]].*)metric[[:space:]]+${IPV6_BLACKHOLE_METRIC}([[:space:]]|$)"; then
        log WARN "ipv6_rules_are_synced: policy-table blackhole FAIL"
        return 1
    fi
    for source in "${TARGET_IPV6_ADDRESSES[@]}"; do
        if ! ipv6_address_is_managed "${source}"; then
            log WARN "ipv6_rules_are_synced: source ${source} is not owned"
            return 1
        fi
        exact_blackhole_pattern="^${IPV6_BLACKHOLE_RULE_PREF}:[[:space:]]+from[[:space:]]+${source}(/128)?[[:space:]]+blackhole[[:space:]]*$"
        exact_policy_pattern="^${IPV6_TUNNEL_RULE_PREF}:[[:space:]]+from[[:space:]]+${source}(/128)?[[:space:]]+(lookup|table)[[:space:]]+${IPV6_POLICY_TABLE}[[:space:]]*$"
        rules=$(ipv6_rules_for_source_pref "${source}" "${IPV6_BLACKHOLE_RULE_PREF}" || true)
        rule_count=$(printf '%s\n' "${rules}" | sed '/^$/d' | wc -l)
        if [ "${rule_count}" -ne 1 ] || ! printf '%s\n' "${rules}" | grep -qE "${exact_blackhole_pattern}"; then
            log WARN "ipv6_rules_are_synced: source blackhole for ${source} FAIL"
            return 1
        fi
        rules=$(ipv6_rules_for_source_pref "${source}" "${IPV6_TUNNEL_RULE_PREF}" || true)
        rule_count=$(printf '%s\n' "${rules}" | sed '/^$/d' | wc -l)
        if [ "${rule_count}" -ne 1 ] || ! printf '%s\n' "${rules}" | grep -qE "${exact_policy_pattern}"; then
            log WARN "ipv6_rules_are_synced: source policy for ${source} FAIL"
            return 1
        fi
        if ! ip6tables -C FORWARD -s "${source}" ! -d fe80::/10 \
            -m comment --comment "${IPV6_RULE_MARKER}" -j DROP >/dev/null 2>&1; then
            log WARN "ipv6_rules_are_synced: filter for ${source} FAIL"
            return 1
        fi
    done
    for mac in "${TARGET_IPV6_MACS[@]}"; do
        if ! ip6tables -C FORWARD -m mac --mac-source "${mac}" ! -d fe80::/10 \
            -m comment --comment "${IPV6_RULE_MARKER}" -j DROP >/dev/null 2>&1; then
            log WARN "ipv6_rules_are_synced: filter for target MAC ${mac} FAIL"
            return 1
        fi
    done
}

ensure_policy_routing() {
    local changed=0
    local installed_rule
    POLICY_CHANGED="0"
    
    if [[ "${K3S_MODE}" == "true" ]]; then
        # Install the fallback first. Even malformed bypass configuration then
        # blocks the pod before the normal main-table rule can leak traffic.
        if ! ensure_fallback_blackhole_rule \
            "${DOCKER_TARGET_IP}" \
            "k3s: Failed to add fallback blackhole rule" \
            "${K3S_RULE_PROTOCOL}"; then
            return 1
        fi
        if [ "${BLACKHOLE_CHANGED}" = "1" ]; then
            changed=1
        fi

        # Retire reply-only routing before inspecting table 51820. The source
        # blackhole above keeps traffic fail-closed during migration.
        if ! remove_legacy_k3s_fwmark_rules; then
            return 1
        fi
        if [ "${K3S_LEGACY_POLICY_CHANGED}" = "1" ]; then
            changed=1
        fi

        if ! normalize_k3s_bypass_cidrs; then
            return 1
        fi

        if ! ensure_k3s_policy_table_defaults; then
            return 1
        fi
        if [ "${K3S_TABLE_CHANGED}" = "1" ]; then
            changed=1
        fi

        remove_stale_k3s_policy_rules "${DOCKER_TARGET_IP}"
        if [ "${POLICY_CHANGED}" = "1" ]; then
            changed=1
        fi

        if ! ensure_k3s_bypass_rules; then
            return 1
        fi
        if [ "${K3S_BYPASS_CHANGED}" = "1" ]; then
            changed=1
        fi

        # Route every remaining packet from the selected Lightning pod through
        # WireGuard. This covers both replies and pod-initiated connections.
        if ! ip rule show | grep -qE \
            "^${K3S_TUNNEL_RULE_PREF}:[[:space:]]+from[[:space:]]+${DOCKER_TARGET_IP//./\\.}[[:space:]]+(lookup|table)[[:space:]]+51820([[:space:]].*)?[[:space:]]+proto(col)?[[:space:]]+${K3S_RULE_PROTOCOL}[[:space:]]*$"; then
            ip rule del from "${DOCKER_TARGET_IP}" table 51820 protocol "${K3S_RULE_PROTOCOL}" pref "${K3S_TUNNEL_RULE_PREF}" >/dev/null 2>&1 || true
            if ! ip rule add from "${DOCKER_TARGET_IP}" table 51820 protocol "${K3S_RULE_PROTOCOL}" pref "${K3S_TUNNEL_RULE_PREF}" >/dev/null 2>&1; then
                LAST_ERROR="k3s: Failed to add full-outbound policy routing rule"
                return 1
            fi
            installed_rule="$(ip rule show pref "${K3S_TUNNEL_RULE_PREF}" 2>/dev/null | grep -E \
                "^${K3S_TUNNEL_RULE_PREF}:[[:space:]]+from[[:space:]]+${DOCKER_TARGET_IP//./\\.}[[:space:]]+(lookup|table)[[:space:]]+51820([[:space:]].*)?[[:space:]]+proto(col)?[[:space:]]+${K3S_RULE_PROTOCOL}[[:space:]]*$" \
                | head -n 1 || true)"
            if [ -z "${installed_rule}" ]; then
                LAST_ERROR="k3s: Full-outbound rule was not installed"
                return 1
            fi
            changed=1
        fi

        if ! ensure_k3s_unknown_pod_containment "${DOCKER_TARGET_IP}"; then
            return 1
        fi
        if [ "${POLICY_CHANGED}" = "1" ]; then
            changed=1
        fi

    elif [[ "${SECURE_MODE}" == "true" ]]; then
        # Secure Mode: Direct IP policy routing
        # 1. Discover target's bridge network subnet dynamically
        local subnet
        subnet=$(get_target_subnet)
        
        # 1b. Sweep stale source-IP rules for the other possible node IP to prevent route leakage
        local other_ip
        if [ "${DOCKER_TARGET_IP}" = "10.21.21.9" ]; then
            other_ip="10.21.21.96"
        else
            other_ip="10.21.21.9"
        fi
        local other_subnet
        other_subnet=$(get_target_subnet "${other_ip}")
        ip rule del from "${other_ip}" to "${other_subnet}" table main pref 32500 >/dev/null 2>&1 || true
        ip rule del from "${other_ip}" table 51820 pref 32764 >/dev/null 2>&1 || true
        ip rule del from "${other_ip}" blackhole pref 32765 >/dev/null 2>&1 || true

        # 2. Local-to-Local bypass rule (so LND can talk to local Bitcoind/Tor on 10.21.x.x)
        if ! ip rule show | grep -qE "from[[:space:]]+${DOCKER_TARGET_IP//./\\.}[[:space:]]+to[[:space:]]+${subnet//./\\.}[[:space:]]+(lookup|table)[[:space:]]+main"; then
            ip rule del from "${DOCKER_TARGET_IP}" to "${subnet}" table main pref 32500 >/dev/null 2>&1 || true
            if ! ip rule add from "${DOCKER_TARGET_IP}" to "${subnet}" table main pref 32500 >/dev/null 2>&1; then
                if ! ip rule show pref 32500 | grep -qE "from[[:space:]]+${DOCKER_TARGET_IP//./\\.}"; then
                    LAST_ERROR="SecureMode: Failed to add local bypass rule"
                    return 1
                fi
            fi
            changed=1
        fi
        
        # 3. Route external traffic through the VPN table 51820
        if ! ip rule show | grep -qE "^[0-9]+:[[:space:]]+from[[:space:]]+${DOCKER_TARGET_IP//./\\.}[[:space:]]+(lookup|table)[[:space:]]+51820[[:space:]]*$"; then
            ip rule del from "${DOCKER_TARGET_IP}" table 51820 pref 32764 >/dev/null 2>&1 || true
            if ! ip rule add from "${DOCKER_TARGET_IP}" table 51820 pref 32764 >/dev/null 2>&1; then
                if ! ip rule show pref 32764 | grep -qE "from[[:space:]]+${DOCKER_TARGET_IP//./\\.}"; then
                    LAST_ERROR="SecureMode: Failed to add policy routing rule"
                    return 1
                fi
            fi
            changed=1
        fi

        # 3b. Fallback blackhole policy rule (pref 32765) to harden kill-switch against route table fallthrough
        if ! ensure_fallback_blackhole_rule \
            "${DOCKER_TARGET_IP}" \
            "SecureMode: Failed to add fallback blackhole rule"; then
            return 1
        fi
        if [ "${BLACKHOLE_CHANGED}" = "1" ]; then
            changed=1
        fi
    else
        # Docker mode: subnet-based rules for the bridge network.
        if ! ensure_docker_source_policy_rules; then
            return 1
        fi
        if [ "${DOCKER_SOURCE_POLICY_CHANGED}" = "1" ]; then
            changed=1
        fi

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

        # Fallback blackhole policy rule (pref 32765) to harden kill-switch against route table fallthrough
        if ! ensure_fallback_blackhole_rule \
            "${DOCKER_NETWORK_SUBNET}" \
            "Failed to add fallback blackhole rule for subnet ${DOCKER_NETWORK_SUBNET}"; then
            return 1
        fi
        if [ "${BLACKHOLE_CHANGED}" = "1" ]; then
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

is_valid_ipv4_address() {
    local ip="${1:-}"
    [[ "${ip}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || return 1
    local IFS='.'
    local -a octets=()
    read -r -a octets <<< "${ip}"
    [ "${#octets[@]}" -eq 4 ] || return 1
    local octet=""
    for octet in "${octets[@]}"; do
        [ "${octet}" -ge 0 ] && [ "${octet}" -le 255 ] 2>/dev/null || return 1
    done
    return 0
}

resolve_k3s_vpn_dns_servers() {
    local raw_cidrs="${K3S_BYPASS_CIDRS_NORMALIZED:-${K3S_BYPASS_CIDRS:-10.42.0.0/16,10.43.0.0/16}}"
    local resolved=""
    resolved="$(
        RAW_CIDRS="${raw_cidrs}" \
        OVERRIDE_DNS="${K3S_VPN_DNS:-}" \
        WG_CONF="${WG_CONF_PATH:-}" \
        FALLBACK_DNS="${K3S_DNS_UPSTREAM_FALLBACK}" \
        python3 -c '
import ipaddress, os, re, sys

cluster_nets = []
for token in os.environ.get("RAW_CIDRS", "").split(","):
    token = token.strip()
    if not token:
        continue
    try:
        net = ipaddress.ip_network(token, strict=False)
        if net.version == 4 and net.prefixlen < 32:
            cluster_nets.append(net)
    except ValueError:
        pass

def extract_usable(raw_text):
    usable = []
    seen = set()
    for part in re.split(r"[\s,]+", raw_text.strip()):
        if not part:
            continue
        host_str = part
        port = 53
        has_port = False
        if ":" in part:
            if part.count(":") != 1:
                continue
            host_str, port_str = part.rsplit(":", 1)
            try:
                port = int(port_str, 10)
            except ValueError:
                continue
            if port < 1 or port > 65535:
                continue
            has_port = True
        try:
            addr = ipaddress.IPv4Address(host_str)
        except ValueError:
            continue
        if (
            addr.is_loopback
            or addr.is_unspecified
            or addr.is_link_local
            or addr.is_multicast
            or addr.is_reserved
        ):
            continue
        if any(addr in net for net in cluster_nets):
            continue
        key = (str(addr), port)
        if key not in seen:
            seen.add(key)
            usable.append(f"{addr}:{port}" if has_port else str(addr))
    return usable

override = extract_usable(os.environ.get("OVERRIDE_DNS", ""))
if override:
    print(",".join(override))
    sys.exit(0)

wg_conf = os.environ.get("WG_CONF", "")
if wg_conf and os.path.isfile(wg_conf):
    try:
        wg_dns_tokens = []
        in_interface = False
        with open(wg_conf, encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.split("#", 1)[0].strip()
                if not line:
                    continue
                if line.startswith("[") and line.endswith("]"):
                    in_interface = line.lower() == "[interface]"
                    continue
                if in_interface and "=" in line:
                    k, v = line.split("=", 1)
                    if k.strip().lower() == "dns":
                        wg_dns_tokens.append(v)
        wg_usable = extract_usable(" ".join(wg_dns_tokens))
        if wg_usable:
            print(",".join(wg_usable))
            sys.exit(0)
    except OSError:
        pass

fallback = extract_usable(os.environ.get("FALLBACK_DNS", "1.1.1.1,9.9.9.9"))
print(",".join(fallback) if fallback else "1.1.1.1,9.9.9.9")
' 2>/dev/null || true
    )"
    if [ -n "${resolved}" ]; then
        printf '%s\n' "${resolved}"
        return 0
    fi
    printf '%s\n' "${K3S_DNS_UPSTREAM_FALLBACK}"
}


resolve_k3s_cluster_dns_endpoint() {
    local explicit_dns="${K3S_CLUSTER_DNS_IP:-${KUBE_DNS_SERVICE_HOST:-${COREDNS_SERVICE_HOST:-}}}"
    if [ -n "${explicit_dns}" ]; then
        if [[ "${explicit_dns}" == *:* ]]; then
            printf '%s\n' "${explicit_dns}"
        else
            printf '%s:53\n' "${explicit_dns}"
        fi
        return 0
    fi

    local raw_cidrs="${K3S_BYPASS_CIDRS_NORMALIZED:-${K3S_BYPASS_CIDRS:-10.42.0.0/16,10.43.0.0/16}}"
    local resolv_path="${K3S_RESOLV_CONF_PATH:-/etc/resolv.conf}"
    local pod_cidrs=""
    pod_cidrs="$(resolve_k3s_pod_cidrs "${DOCKER_TARGET_IP:-}" 2>/dev/null || true)"
    local discovered=""
    discovered="$(
        RAW_CIDRS="${raw_cidrs}" \
        UNSORTED_CIDRS="${K3S_BYPASS_CIDRS:-}" \
        POD_CIDRS="${pod_cidrs},${K3S_POD_CIDRS:-}" \
        TARGET_IP="${DOCKER_TARGET_IP:-}" \
        K8S_SERVICE_HOST="${KUBERNETES_SERVICE_HOST:-}" \
        RESOLV_PATH="${resolv_path}" \
        python3 -c '
import ipaddress, os, sys
raw_cidrs = os.environ.get("RAW_CIDRS", "10.42.0.0/16,10.43.0.0/16")
resolv_path = os.environ.get("RESOLV_PATH", "/etc/resolv.conf")
nets = []
for part in raw_cidrs.split(","):
    token = part.strip()
    if not token:
        continue
    try:
        net = ipaddress.ip_network(token, strict=False)
        if net.version == 4 and net.prefixlen < 32 and net not in nets:
            nets.append(net)
    except ValueError:
        continue

if resolv_path and os.path.isfile(resolv_path):
    try:
        with open(resolv_path, encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.split("#", 1)[0].split(";", 1)[0].strip()
                parts = line.split()
                if len(parts) >= 2 and parts[0].lower() == "nameserver":
                    try:
                        addr = ipaddress.IPv4Address(parts[1])
                    except ValueError:
                        continue
                    if addr.is_loopback or addr.is_unspecified:
                        continue
                    if not nets or any(addr in net for net in nets):
                        print(f"{addr}:53")
                        sys.exit(0)
    except OSError:
        pass

default_dns = ipaddress.IPv4Address("10.43.0.10")
if not nets or any(default_dns in net for net in nets):
    print("10.43.0.10:53")
    sys.exit(0)

k8s_svc_host = os.environ.get("K8S_SERVICE_HOST", "").strip()
if k8s_svc_host:
    try:
        svc_addr = ipaddress.IPv4Address(k8s_svc_host)
        for net in nets:
            if svc_addr in net and net.num_addresses > 10:
                print(f"{net.network_address + 10}:53")
                sys.exit(0)
    except ValueError:
        pass

explicit_pod_nets = set()
for raw in os.environ.get("POD_CIDRS", "").replace("\n", ",").split(","):
    tok = raw.strip()
    if not tok:
        continue
    try:
        pnet = ipaddress.ip_network(tok, strict=False)
        if pnet.version == 4 and pnet.prefixlen < 32:
            explicit_pod_nets.add(pnet)
    except ValueError:
        pass

target_ip = os.environ.get("TARGET_IP", "").strip()
if target_ip:
    try:
        t_addr = ipaddress.IPv4Address(target_ip)
        for net in nets:
            if t_addr in net:
                explicit_pod_nets.add(net)
    except ValueError:
        pass

known_pod_defaults = {
    ipaddress.IPv4Network("10.42.0.0/16"),
    ipaddress.IPv4Network("10.244.0.0/16"),
    ipaddress.IPv4Network("192.168.0.0/16"),
}
known_svc_defaults = {
    ipaddress.IPv4Network("10.43.0.0/16"),
    ipaddress.IPv4Network("10.96.0.0/12"),
}
unsorted_nets = []
for part in os.environ.get("UNSORTED_CIDRS", "").split(","):
    token = part.strip()
    if not token:
        continue
    try:
        unet = ipaddress.ip_network(token, strict=False)
        if unet.version == 4 and unet.prefixlen < 32 and unet not in unsorted_nets:
            unsorted_nets.append(unet)
    except ValueError:
        pass

candidates = (
    [net for net in nets if net in known_svc_defaults and net not in explicit_pod_nets]
    + [net for net in nets if net not in explicit_pod_nets and net not in known_pod_defaults]
    + [net for net in nets if net not in explicit_pod_nets]
    + (unsorted_nets[1:] if len(unsorted_nets) >= 2 else [])
    + nets
)
for net in candidates:
    if net.num_addresses > 10:
        print(f"{net.network_address + 10}:53")
        sys.exit(0)

print("10.43.0.10:53")
' 2>/dev/null || true
    )"
    if [ -n "${discovered}" ]; then
        printf '%s\n' "${discovered}"
        return 0
    fi

    printf '10.43.0.10:53\n'
}

resolve_k3s_cluster_domain() {
    local configured_domain="${K3S_CLUSTER_DOMAIN:-}"
    configured_domain="${configured_domain#.}"
    configured_domain="${configured_domain%.}"
    if [ -n "${configured_domain}" ]; then
        printf '%s\n' "${configured_domain,,}"
        return 0
    fi

    local resolv_path="${K3S_RESOLV_CONF_PATH:-/etc/resolv.conf}"
    local discovered=""
    if [ -r "${resolv_path}" ]; then
        discovered="$(
            awk '
                {
                    sub(/[#;].*$/, "", $0)
                    cmd = tolower($1)
                    if (cmd == "search" || cmd == "domain") {
                        for (i = 2; i <= NF; i++) {
                            tok = tolower($i)
                            gsub(/^\.+|\.+$/, "", tok)
                            if (tok ~ /^svc\..+/) {
                                sub(/^svc\./, "", tok)
                                print tok
                                exit
                            }
                            idx = index(tok, ".svc.")
                            if (idx > 1 && length(tok) > idx + 4) {
                                print substr(tok, idx + 5)
                                exit
                            }
                        }
                    }
                }
            ' "${resolv_path}" 2>/dev/null || true
        )"
    fi

    if [ -n "${discovered}" ]; then
        printf '%s\n' "${discovered}"
        return 0
    fi

    printf 'cluster.local\n'
}

k3s_dns_proxy_is_running() {
    local pid=""
    if [ ! -f "${K3S_DNS_PROXY_PID_FILE}" ]; then
        return 1
    fi
    pid="$(tr -d '[:space:]' < "${K3S_DNS_PROXY_PID_FILE}" 2>/dev/null || true)"
    [[ "${pid}" =~ ^[0-9]+$ ]] && kill -0 "${pid}" 2>/dev/null
}

stop_k3s_dns_proxy() {
    local pid=""
    if [ -f "${K3S_DNS_PROXY_PID_FILE}" ]; then
        pid="$(tr -d '[:space:]' < "${K3S_DNS_PROXY_PID_FILE}" 2>/dev/null || true)"
        if [[ "${pid}" =~ ^[0-9]+$ ]] && kill -0 "${pid}" 2>/dev/null; then
            kill "${pid}" >/dev/null 2>&1 || true
            wait "${pid}" 2>/dev/null || true
        fi
        rm -f "${K3S_DNS_PROXY_PID_FILE}" "${K3S_DNS_PROXY_PID_FILE}.args" 2>/dev/null || true
    fi
}

ensure_k3s_dns_proxy_running() {
    if [ "${K3S_DNS_ENFORCE_PROXY_PROCESS:-false}" != "true" ] && declare -F iptables >/dev/null 2>&1; then
        return 0
    fi

    if [ ! -f "${K3S_DNS_PROXY_SCRIPT}" ]; then
        LAST_ERROR="k3s: Missing split-horizon DNS proxy script at ${K3S_DNS_PROXY_SCRIPT}"
        return 1
    fi

    local cluster_dns
    local cluster_cidrs
    local cluster_domain
    local vpn_dns
    local allowed_source=""
    local -a source_args=()
    local desired_args
    local current_args=""
    local log_file="${K3S_DNS_PROXY_LOG_FILE:-/tmp/tunnelsats-k3s-dns-proxy.log}"
    cluster_dns="$(resolve_k3s_cluster_dns_endpoint)"
    cluster_cidrs="${K3S_BYPASS_CIDRS_NORMALIZED:-${K3S_BYPASS_CIDRS:-10.42.0.0/16,10.43.0.0/16}}"
    cluster_domain="$(resolve_k3s_cluster_domain)"
    vpn_dns="$(resolve_k3s_vpn_dns_servers)"
    if is_valid_ipv4_address "${DOCKER_TARGET_IP:-}"; then
        allowed_source="${DOCKER_TARGET_IP}"
        source_args=(--allowed-sources "${allowed_source}")
    fi
    desired_args="${K3S_DNS_PROXY_HOST}|${K3S_DNS_PROXY_PORT}|${cluster_dns}|${cluster_cidrs}|${cluster_domain}|${vpn_dns}|${WG_IFACE}|${allowed_source}"

    if [ -f "${K3S_DNS_PROXY_PID_FILE}.args" ]; then
        current_args="$(cat "${K3S_DNS_PROXY_PID_FILE}.args" 2>/dev/null || true)"
    fi

    if k3s_dns_proxy_is_running && [ "${current_args}" = "${desired_args}" ]; then
        return 0
    fi

    stop_k3s_dns_proxy
    mkdir -p "$(dirname "${K3S_DNS_PROXY_PID_FILE}")" "$(dirname "${log_file}")" 2>/dev/null || true
    : > "${log_file}" 2>/dev/null || true
    python3 "${K3S_DNS_PROXY_SCRIPT}" \
        --listen-host "${K3S_DNS_PROXY_HOST}" \
        --listen-port "${K3S_DNS_PROXY_PORT}" \
        --cluster-dns "${cluster_dns}" \
        --cluster-cidrs "${cluster_cidrs}" \
        --cluster-domain "${cluster_domain}" \
        --vpn-dns "${vpn_dns}" \
        --vpn-interface "${WG_IFACE}" \
        "${source_args[@]}" \
        --pid-file "${K3S_DNS_PROXY_PID_FILE}" > "${log_file}" 2>&1 &

    local attempts=0
    while [ "${attempts}" -lt 30 ]; do
        if k3s_dns_proxy_is_running; then
            printf '%s' "${desired_args}" > "${K3S_DNS_PROXY_PID_FILE}.args" 2>/dev/null || true
            K3S_DNS_CHANGED="1"
            return 0
        fi
        sleep 0.05
        attempts=$((attempts + 1))
    done

    local startup_err=""
    if [ -s "${log_file}" ]; then
        startup_err="$(tail -n 5 "${log_file}" 2>/dev/null | tr '\n' ' ' | sed 's/[[:space:]]\+/ /g; s/^ //; s/ $//' || true)"
    fi
    LAST_ERROR="k3s: Failed to start split-horizon DNS proxy on ${K3S_DNS_PROXY_HOST}:${K3S_DNS_PROXY_PORT}${startup_err:+ (${startup_err})}"
    log ERROR "${LAST_ERROR}"
    return 1
}

ensure_k3s_dns_oif_policy_rule() {
    if [ "${K3S_DNS_ENFORCE_OIF_RULE:-false}" != "true" ] && \
       { declare -F ip >/dev/null 2>&1 || declare -F iptables >/dev/null 2>&1; }; then
        return 0
    fi

    local existing_rules
    existing_rules="$(ip rule show pref "${K3S_TUNNEL_RULE_PREF}" 2>/dev/null || true)"
    if grep -Fq -- "oif ${WG_IFACE} lookup 51820" <<< "${existing_rules}" || \
       grep -Fq -- "oif ${WG_IFACE} table 51820" <<< "${existing_rules}"; then
        return 0
    fi

    if ! ip rule add from all oif "${WG_IFACE}" table 51820 protocol "${K3S_RULE_PROTOCOL}" pref "${K3S_TUNNEL_RULE_PREF}" 2>/dev/null; then
        if ! ip rule add from all oif "${WG_IFACE}" table 51820 pref "${K3S_TUNNEL_RULE_PREF}" 2>/dev/null; then
            LAST_ERROR="k3s: Failed to add tunnel oif policy rule for ${WG_IFACE}"
            return 1
        fi
    fi
    K3S_DNS_CHANGED="1"
    return 0
}

k3s_nat_dns_rules_precede_kube_services() {
    local target_ip="$1"
    local prerouting_rules
    prerouting_rules="$(iptables -t nat -S PREROUTING 2>/dev/null || true)"
    if [ -z "${prerouting_rules}" ] || ! grep -Fq -- "-j KUBE-SERVICES" <<< "${prerouting_rules}"; then
        return 0
    fi

    local kube_line=0
    local udp_line=0
    local tcp_line=0
    local line_no=0
    local line=""
    while IFS= read -r line; do
        line_no=$((line_no + 1))
        if [ "${kube_line}" -eq 0 ] && [[ "${line}" == *"-j KUBE-SERVICES"* ]]; then
            kube_line="${line_no}"
        fi
        if [[ "${line}" == *"tunnelsats-k3s-dns"* ]] && [[ "${line}" == *"-s ${target_ip}/32"* || "${line}" == *"-s ${target_ip} "* ]]; then
            if [[ "${line}" == *"-p udp"* ]] && [ "${udp_line}" -eq 0 ]; then
                udp_line="${line_no}"
            elif [[ "${line}" == *"-p tcp"* ]] && [ "${tcp_line}" -eq 0 ]; then
                tcp_line="${line_no}"
            fi
        fi
    done <<< "${prerouting_rules}"

    if [ "${udp_line}" -eq 0 ] || [ "${tcp_line}" -eq 0 ]; then
        return 1
    fi
    if [ "${kube_line}" -gt 0 ] && { [ "${udp_line}" -gt "${kube_line}" ] || [ "${tcp_line}" -gt "${kube_line}" ]; }; then
        return 1
    fi
    return 0
}

resolve_k3s_local_pod_cidrs() {
    local source_ip="${1:-${DOCKER_TARGET_IP:-}}"
    local cluster_cidrs=""
    local route_lines=""
    local local_cidrs=""

    cluster_cidrs="$(resolve_k3s_pod_cidrs "${source_ip}" 2>/dev/null || true)"
    if [ -z "${cluster_cidrs}" ]; then
        cluster_cidrs="10.42.0.0/16"
    fi
    route_lines="$(ip -4 route show 2>/dev/null || ip route show 2>/dev/null || true)"

    local_cidrs="$(
        SOURCE_IP="${source_ip}" \
        CLUSTER_CIDRS="${cluster_cidrs}" \
        CONFIGURED_LOCAL_CIDRS="${K3S_LOCAL_POD_CIDRS:-}" \
        ROUTE_LINES="${route_lines}" \
        WG_DEV="${WG_IFACE:-tunnelsatsv2}" \
        python3 -c '
import ipaddress, os

raw_source = os.environ.get("SOURCE_IP", "").strip()
wg_dev = os.environ.get("WG_DEV", "tunnelsatsv2").strip()
target_ip = None
if raw_source:
    try:
        addr = ipaddress.ip_address(raw_source)
        if addr.version == 4:
            target_ip = addr
    except ValueError:
        pass

configured_local = []
for token in os.environ.get("CONFIGURED_LOCAL_CIDRS", "").split(","):
    token = token.strip()
    if not token:
        continue
    try:
        net = ipaddress.ip_network(token, strict=False)
        if net.version == 4 and 0 < net.prefixlen < 32 and net not in configured_local:
            configured_local.append(net)
    except ValueError:
        pass

if configured_local:
    for net in configured_local:
        print(net)
    raise SystemExit(0)

cluster_nets = []
for token in os.environ.get("CLUSTER_CIDRS", "").splitlines():
    for part in token.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            net = ipaddress.ip_network(part, strict=False)
            if net.version == 4 and 0 < net.prefixlen < 32 and net not in cluster_nets:
                cluster_nets.append(net)
        except ValueError:
            pass

non_local_prefixes = (
    "lo",
    "eth",
    "ens",
    "enp",
    "eno",
    "wlan",
    "wlp",
    "bond",
    "flannel",
    "vxlan",
    "cilium_vxlan",
    "genev",
    "tunl",
    "wg",
    "tunnelsats",
    "docker",
    "br-",
    "tailscale",
)

direct_local_nets = []
for raw_line in os.environ.get("ROUTE_LINES", "").splitlines():
    parts = raw_line.split()
    if not parts:
        continue
    if parts[0] in {
        "local",
        "broadcast",
        "multicast",
        "throw",
        "unreachable",
        "prohibit",
        "blackhole",
        "nat",
        "anycast",
    }:
        continue
    if "via" in parts:
        continue
    idx = 1 if parts[0] == "unicast" else 0
    if idx >= len(parts):
        continue
    prefix_tok = parts[idx]
    if prefix_tok == "default" or "/" not in prefix_tok:
        continue
    try:
        net = ipaddress.ip_network(prefix_tok, strict=False)
    except ValueError:
        continue
    if net.version != 4 or net.prefixlen <= 0 or net.prefixlen >= 32:
        continue
    if net.is_loopback or net.is_link_local or net.is_multicast or net.is_unspecified:
        continue
    dev = ""
    if "dev" in parts:
        dev_idx = parts.index("dev")
        if dev_idx + 1 < len(parts):
            dev = parts[dev_idx + 1]
    if not dev or dev == wg_dev or dev.startswith(non_local_prefixes):
        continue
    if (target_ip is not None and target_ip in net) or any(net.subnet_of(cnet) for cnet in cluster_nets):
        if net not in direct_local_nets:
            direct_local_nets.append(net)

selected = []
if target_ip is not None:
    target_matches = [net for net in direct_local_nets if target_ip in net]
    if target_matches:
        selected.append(max(target_matches, key=lambda item: item.prefixlen))
if not selected and direct_local_nets:
    selected = list(direct_local_nets)
if not selected:
    selected = list(cluster_nets)

for net in selected:
    print(net)
' 2>/dev/null || true
    )"

    if [ -z "${local_cidrs}" ]; then
        local_cidrs="${cluster_cidrs}"
    fi
    printf '%s\n' "${local_cidrs}"
    return 0
}

resolve_k3s_local_pod_interfaces() {
    local source_ip="${1:-${DOCKER_TARGET_IP:-}}"
    local route_get_line=""
    local route_lines=""
    local local_ifaces=""

    if [ -n "${source_ip}" ] && [ "${source_ip}" != "10.9.9.9" ]; then
        route_get_line="$(ip -4 route get "${source_ip}" 2>/dev/null | head -n 1 || true)"
    fi
    route_lines="$(ip -4 route show 2>/dev/null || ip route show 2>/dev/null || true)"

    local_ifaces="$(
        SOURCE_IP="${source_ip}" \
        CONFIGURED_IFACES="${K3S_LOCAL_POD_INTERFACES:-}" \
        ROUTE_GET_LINE="${route_get_line}" \
        ROUTE_LINES="${route_lines}" \
        WG_DEV="${WG_IFACE:-tunnelsatsv2}" \
        python3 -c '
import ipaddress, os

wg_dev = os.environ.get("WG_DEV", "tunnelsatsv2").strip()
raw_source = os.environ.get("SOURCE_IP", "").strip()
target_ip = None
if raw_source:
    try:
        addr = ipaddress.ip_address(raw_source)
        if addr.version == 4:
            target_ip = addr
    except ValueError:
        pass

def normalize_iface(dev):
    dev = dev.strip()
    if not dev:
        return ""
    for prefix in ("cali", "lxc", "veth", "ovn"):
        if dev.startswith(prefix) and len(dev) > len(prefix):
            return f"{prefix}+"
    return dev

configured = []
for token in os.environ.get("CONFIGURED_IFACES", "").split(","):
    norm = normalize_iface(token)
    if norm and norm not in configured:
        configured.append(norm)
if configured:
    for iface in configured:
        print(iface)
    raise SystemExit(0)

non_local_prefixes = (
    "lo",
    "eth",
    "ens",
    "enp",
    "eno",
    "wlan",
    "wlp",
    "bond",
    "flannel",
    "vxlan",
    "cilium_vxlan",
    "genev",
    "tunl",
    "wg",
    "tunnelsats",
    "docker",
    "br-",
    "tailscale",
)
cni_prefixes = (
    "cni",
    "cbr",
    "cali",
    "lxc",
    "veth",
    "ovn",
    "kube-bridge",
    "weave",
    "pod",
)

selected = []

route_get = os.environ.get("ROUTE_GET_LINE", "").strip()
if route_get:
    parts = route_get.split()
    if parts and parts[0] not in ("blackhole", "unreachable", "prohibit") and "via" not in parts and "dev" in parts:
        dev_idx = parts.index("dev")
        if dev_idx + 1 < len(parts):
            dev = parts[dev_idx + 1]
            if dev and dev != wg_dev and not dev.startswith(non_local_prefixes):
                norm = normalize_iface(dev)
                if norm and norm not in selected:
                    selected.append(norm)

if not selected:
    best_prefix = -1
    best_iface = ""
    cni_ifaces = []
    for raw_line in os.environ.get("ROUTE_LINES", "").splitlines():
        parts = raw_line.split()
        if not parts or "via" in parts:
            continue
        if parts[0] in ("local", "broadcast", "multicast", "throw", "unreachable", "prohibit", "blackhole", "nat", "anycast"):
            continue
        idx = 1 if parts[0] == "unicast" else 0
        if idx >= len(parts):
            continue
        prefix_tok = parts[idx]
        if prefix_tok == "default":
            continue
        if "dev" not in parts:
            continue
        dev_idx = parts.index("dev")
        if dev_idx + 1 >= len(parts):
            continue
        dev = parts[dev_idx + 1]
        if not dev or dev == wg_dev or dev.startswith(non_local_prefixes):
            continue
        try:
            net = ipaddress.ip_network(prefix_tok, strict=False) if "/" in prefix_tok else ipaddress.ip_network(f"{prefix_tok}/32", strict=False)
        except ValueError:
            continue
        if net.version != 4 or net.is_loopback or net.is_link_local or net.is_multicast or net.is_unspecified:
            continue
        norm = normalize_iface(dev)
        if target_ip is not None and target_ip in net and net.prefixlen > best_prefix:
            best_prefix = net.prefixlen
            best_iface = norm
        if dev.startswith(cni_prefixes) and norm not in cni_ifaces:
            cni_ifaces.append(norm)
    if best_iface:
        selected.append(best_iface)
    elif cni_ifaces:
        selected.extend(cni_ifaces)

if not selected:
    selected.append("cni+")

for iface in selected:
    print(iface)
' 2>/dev/null || true
    )"

    if [ -z "${local_ifaces}" ]; then
        local_ifaces="cni+"
    fi
    printf '%s\n' "${local_ifaces}"
    return 0
}

k3s_ipv4_dns_rule_is_current() {
    local rule="$1"
    local target_ip="$2"
    local pod_cidrs="$3"
    local pod_ifaces="${4:-}"
    local pod_cidr=""
    local pod_iface=""

    if [[ "${rule}" == *" -s ${target_ip}/32 "* ]] || [[ "${rule}" == *" -s ${target_ip} "* ]] || \
       [[ "${rule}" == *" ! -s ${target_ip}/32 "* ]] || [[ "${rule}" == *" ! -s ${target_ip} "* ]]; then
        return 0
    fi

    while IFS= read -r pod_cidr; do
        [ -n "${pod_cidr}" ] || continue
        [[ "${pod_cidr}" == */* ]] && [[ "${pod_cidr}" != */32 ]] || continue
        while IFS= read -r pod_iface; do
            [ -n "${pod_iface}" ] || continue
            if [[ "${rule}" == *" -s ${pod_cidr} "* ]] && \
               [[ "${rule}" == *" -i ${pod_iface} "* ]] && \
               [[ "${rule}" == *"tunnelsats-k3s-dns-guard"* ]]; then
                return 0
            fi
        done <<< "${pod_ifaces}"
    done <<< "${pod_cidrs}"
    return 1
}

ensure_k3s_dns_protection() {
    local target_ip="${1:-${DOCKER_TARGET_IP:-}}"
    local changed=0
    local matched_pod_cidrs=""
    local local_pod_ifaces=""
    local pod_cidr=""
    local pod_iface=""
    local rule_line=""
    K3S_DNS_CHANGED="0"
    DNS_PROTECTED="false"

    if ! is_valid_ipv4_address "${target_ip}"; then
        LAST_ERROR="k3s: Invalid target IP '${target_ip}' for DNS protection"
        return 1
    fi

    matched_pod_cidrs="$(resolve_k3s_local_pod_cidrs "${target_ip}" 2>/dev/null || true)"
    if [ -z "${matched_pod_cidrs}" ]; then
        matched_pod_cidrs="10.42.0.0/16"
    fi
    local_pod_ifaces="$(resolve_k3s_local_pod_interfaces "${target_ip}" 2>/dev/null || true)"
    if [ -z "${local_pod_ifaces}" ]; then
        local_pod_ifaces="cni+"
    fi

    local need_input_guard_sync=0
    if ! iptables -C INPUT -s "${target_ip}" -p udp --dport 53 \
        -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP 2>/dev/null || \
       ! iptables -C INPUT -s "${target_ip}" -p tcp --dport 53 \
        -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP 2>/dev/null || \
       ! iptables -C INPUT -s "${target_ip}" -p udp --dport 853 \
        -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP 2>/dev/null || \
       ! iptables -C INPUT -s "${target_ip}" -p tcp --dport 853 \
        -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP 2>/dev/null; then
        need_input_guard_sync=1
    fi
    while IFS= read -r pod_cidr; do
        [ -n "${pod_cidr}" ] || continue
        [[ "${pod_cidr}" == */* ]] && [[ "${pod_cidr}" != */32 ]] || continue
        while IFS= read -r pod_iface; do
            [ -n "${pod_iface}" ] || continue
            if ! iptables -C INPUT -s "${pod_cidr}" -i "${pod_iface}" -m mark ! --mark "${K3S_NON_TARGET_FWMARK}/${K3S_NON_TARGET_FWMARK_MASK}" -p udp --dport 53 \
                -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP 2>/dev/null || \
               ! iptables -C INPUT -s "${pod_cidr}" -i "${pod_iface}" -m mark ! --mark "${K3S_NON_TARGET_FWMARK}/${K3S_NON_TARGET_FWMARK_MASK}" -p tcp --dport 53 \
                -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP 2>/dev/null || \
               ! iptables -C INPUT -s "${pod_cidr}" -i "${pod_iface}" -m mark ! --mark "${K3S_NON_TARGET_FWMARK}/${K3S_NON_TARGET_FWMARK_MASK}" -p udp --dport 853 \
                -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP 2>/dev/null || \
               ! iptables -C INPUT -s "${pod_cidr}" -i "${pod_iface}" -m mark ! --mark "${K3S_NON_TARGET_FWMARK}/${K3S_NON_TARGET_FWMARK_MASK}" -p tcp --dport 853 \
                -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP 2>/dev/null; then
                need_input_guard_sync=1
            fi
        done <<< "${local_pod_ifaces}"
    done <<< "${matched_pod_cidrs}"
    while IFS= read -r rule_line; do
        [ -n "${rule_line}" ] || continue
        if ! k3s_ipv4_dns_rule_is_current "${rule_line}" "${target_ip}" "${matched_pod_cidrs}" "${local_pod_ifaces}"; then
            need_input_guard_sync=1
            break
        fi
    done < <(iptables -S INPUT 2>/dev/null | grep "tunnelsats-k3s-dns-guard" || true)
    if [ "${need_input_guard_sync}" -eq 1 ]; then
        remove_tagged_iptables_rules filter INPUT "tunnelsats-k3s-dns-guard"
        while IFS= read -r pod_cidr; do
            [ -n "${pod_cidr}" ] || continue
            [[ "${pod_cidr}" == */* ]] && [[ "${pod_cidr}" != */32 ]] || continue
            while IFS= read -r pod_iface; do
                [ -n "${pod_iface}" ] || continue
                if ! iptables -I INPUT 1 -s "${pod_cidr}" -i "${pod_iface}" -m mark ! --mark "${K3S_NON_TARGET_FWMARK}/${K3S_NON_TARGET_FWMARK_MASK}" -p tcp --dport 853 \
                    -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP || \
                   ! iptables -I INPUT 1 -s "${pod_cidr}" -i "${pod_iface}" -m mark ! --mark "${K3S_NON_TARGET_FWMARK}/${K3S_NON_TARGET_FWMARK_MASK}" -p udp --dport 853 \
                    -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP || \
                   ! iptables -I INPUT 1 -s "${pod_cidr}" -i "${pod_iface}" -m mark ! --mark "${K3S_NON_TARGET_FWMARK}/${K3S_NON_TARGET_FWMARK_MASK}" -p tcp --dport 53 \
                    -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP || \
                   ! iptables -I INPUT 1 -s "${pod_cidr}" -i "${pod_iface}" -m mark ! --mark "${K3S_NON_TARGET_FWMARK}/${K3S_NON_TARGET_FWMARK_MASK}" -p udp --dport 53 \
                    -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP; then
                    LAST_ERROR="k3s: Failed to add local pod-CIDR DNS INPUT guard rules for ${pod_cidr} on ${pod_iface}"
                    return 1
                fi
            done <<< "${local_pod_ifaces}"
        done <<< "${matched_pod_cidrs}"
        if ! iptables -I INPUT 1 -s "${target_ip}" -p tcp --dport 853 \
            -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP || \
           ! iptables -I INPUT 1 -s "${target_ip}" -p udp --dport 853 \
            -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP || \
           ! iptables -I INPUT 1 -s "${target_ip}" -p tcp --dport 53 \
            -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP || \
           ! iptables -I INPUT 1 -s "${target_ip}" -p udp --dport 53 \
            -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP; then
            LAST_ERROR="k3s: Failed to add DNS INPUT guard rules for ${target_ip}"
            return 1
        fi
        changed=1
    fi

    local need_fwd_dns_sync=0
    if ! iptables -C FORWARD -s "${target_ip}" ! -o "${WG_IFACE}" -p udp --dport 53 \
        -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP 2>/dev/null || \
       ! iptables -C FORWARD -s "${target_ip}" ! -o "${WG_IFACE}" -p tcp --dport 53 \
        -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP 2>/dev/null || \
       ! iptables -C FORWARD -s "${target_ip}" ! -o "${WG_IFACE}" -p udp --dport 853 \
        -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP 2>/dev/null || \
       ! iptables -C FORWARD -s "${target_ip}" ! -o "${WG_IFACE}" -p tcp --dport 853 \
        -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP 2>/dev/null; then
        need_fwd_dns_sync=1
    fi
    while IFS= read -r pod_cidr; do
        [ -n "${pod_cidr}" ] || continue
        [[ "${pod_cidr}" == */* ]] && [[ "${pod_cidr}" != */32 ]] || continue
        while IFS= read -r pod_iface; do
            [ -n "${pod_iface}" ] || continue
            if ! iptables -C FORWARD -s "${pod_cidr}" -i "${pod_iface}" ! -o "${WG_IFACE}" -m mark ! --mark "${K3S_NON_TARGET_FWMARK}/${K3S_NON_TARGET_FWMARK_MASK}" -p udp --dport 53 \
                -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP 2>/dev/null || \
               ! iptables -C FORWARD -s "${pod_cidr}" -i "${pod_iface}" ! -o "${WG_IFACE}" -m mark ! --mark "${K3S_NON_TARGET_FWMARK}/${K3S_NON_TARGET_FWMARK_MASK}" -p tcp --dport 53 \
                -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP 2>/dev/null || \
               ! iptables -C FORWARD -s "${pod_cidr}" -i "${pod_iface}" ! -o "${WG_IFACE}" -m mark ! --mark "${K3S_NON_TARGET_FWMARK}/${K3S_NON_TARGET_FWMARK_MASK}" -p udp --dport 853 \
                -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP 2>/dev/null || \
               ! iptables -C FORWARD -s "${pod_cidr}" -i "${pod_iface}" ! -o "${WG_IFACE}" -m mark ! --mark "${K3S_NON_TARGET_FWMARK}/${K3S_NON_TARGET_FWMARK_MASK}" -p tcp --dport 853 \
                -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP 2>/dev/null; then
                need_fwd_dns_sync=1
            fi
        done <<< "${local_pod_ifaces}"
    done <<< "${matched_pod_cidrs}"
    while IFS= read -r rule_line; do
        [ -n "${rule_line}" ] || continue
        if ! k3s_ipv4_dns_rule_is_current "${rule_line}" "${target_ip}" "${matched_pod_cidrs}" "${local_pod_ifaces}"; then
            need_fwd_dns_sync=1
            break
        fi
    done < <(iptables -S FORWARD 2>/dev/null | grep "tunnelsats-k3s-dns-guard" || true)
    if [ "${need_fwd_dns_sync}" -eq 1 ]; then
        remove_tagged_iptables_rules filter FORWARD "tunnelsats-k3s-dns-guard"
        while IFS= read -r pod_cidr; do
            [ -n "${pod_cidr}" ] || continue
            [[ "${pod_cidr}" == */* ]] && [[ "${pod_cidr}" != */32 ]] || continue
            while IFS= read -r pod_iface; do
                [ -n "${pod_iface}" ] || continue
                if ! iptables -I FORWARD 1 -s "${pod_cidr}" -i "${pod_iface}" ! -o "${WG_IFACE}" -m mark ! --mark "${K3S_NON_TARGET_FWMARK}/${K3S_NON_TARGET_FWMARK_MASK}" -p tcp --dport 853 \
                    -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP || \
                   ! iptables -I FORWARD 1 -s "${pod_cidr}" -i "${pod_iface}" ! -o "${WG_IFACE}" -m mark ! --mark "${K3S_NON_TARGET_FWMARK}/${K3S_NON_TARGET_FWMARK_MASK}" -p udp --dport 853 \
                    -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP || \
                   ! iptables -I FORWARD 1 -s "${pod_cidr}" -i "${pod_iface}" ! -o "${WG_IFACE}" -m mark ! --mark "${K3S_NON_TARGET_FWMARK}/${K3S_NON_TARGET_FWMARK_MASK}" -p tcp --dport 53 \
                    -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP || \
                   ! iptables -I FORWARD 1 -s "${pod_cidr}" -i "${pod_iface}" ! -o "${WG_IFACE}" -m mark ! --mark "${K3S_NON_TARGET_FWMARK}/${K3S_NON_TARGET_FWMARK_MASK}" -p udp --dport 53 \
                    -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP; then
                    LAST_ERROR="k3s: Failed to add local pod-CIDR FORWARD DNS/DoT guard rules for ${pod_cidr} on ${pod_iface}"
                    return 1
                fi
            done <<< "${local_pod_ifaces}"
        done <<< "${matched_pod_cidrs}"
        if ! iptables -I FORWARD 1 -s "${target_ip}" ! -o "${WG_IFACE}" -p tcp --dport 853 \
            -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP || \
           ! iptables -I FORWARD 1 -s "${target_ip}" ! -o "${WG_IFACE}" -p udp --dport 853 \
            -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP || \
           ! iptables -I FORWARD 1 -s "${target_ip}" ! -o "${WG_IFACE}" -p tcp --dport 53 \
            -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP || \
           ! iptables -I FORWARD 1 -s "${target_ip}" ! -o "${WG_IFACE}" -p udp --dport 53 \
            -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP; then
            LAST_ERROR="k3s: Failed to add FORWARD DNS/DoT guard rules for ${target_ip}"
            return 1
        fi
        changed=1
    fi

    if command -v ip6tables >/dev/null 2>&1 && \
       { declare -F ip6tables >/dev/null 2>&1 || ! declare -F iptables >/dev/null 2>&1; }; then
        local proto=""
        local port=""
        local v6_addr=""
        local mac=""
        local chain=""
        local rules=""
        local rule=""
        local -a parts=()
        for chain in INPUT FORWARD; do
            rules="$(ip6tables -S "${chain}" 2>/dev/null | grep -F -- "--comment tunnelsats-k3s-ipv6-dns-guard" || true)"
            while IFS= read -r rule; do
                [ -n "${rule}" ] || continue
                if ipv6_filter_rule_is_current "${rule}"; then
                    continue
                fi
                read -r -a parts <<< "${rule}"
                parts[0]="-D"
                if ip6tables "${parts[@]}" >/dev/null 2>&1; then
                    changed=1
                fi
            done <<< "${rules}"
        done
        for v6_addr in "${TARGET_IPV6_ADDRESSES[@]}"; do
            [ -n "${v6_addr}" ] || continue
            for proto in udp tcp; do
                for port in 53 853; do
                    if ! ip6tables -C INPUT -s "${v6_addr}" -p "${proto}" --dport "${port}" \
                        -m comment --comment "tunnelsats-k3s-ipv6-dns-guard" -j DROP 2>/dev/null; then
                        if ! ip6tables -I INPUT 1 -s "${v6_addr}" -p "${proto}" --dport "${port}" \
                            -m comment --comment "tunnelsats-k3s-ipv6-dns-guard" -j DROP; then
                            LAST_ERROR="k3s: Failed to add IPv6 INPUT DNS guard for ${v6_addr} (${proto}/${port})"
                            return 1
                        fi
                        changed=1
                    fi
                    if ! ip6tables -C FORWARD -s "${v6_addr}" -p "${proto}" --dport "${port}" \
                        -m comment --comment "tunnelsats-k3s-ipv6-dns-guard" -j DROP 2>/dev/null; then
                        if ! ip6tables -I FORWARD 1 -s "${v6_addr}" -p "${proto}" --dport "${port}" \
                            -m comment --comment "tunnelsats-k3s-ipv6-dns-guard" -j DROP; then
                            LAST_ERROR="k3s: Failed to add IPv6 FORWARD DNS guard for ${v6_addr} (${proto}/${port})"
                            return 1
                        fi
                        changed=1
                    fi
                done
            done
        done
        for mac in "${TARGET_IPV6_MACS[@]}"; do
            [ -n "${mac}" ] || continue
            for proto in udp tcp; do
                for port in 53 853; do
                    if ! ip6tables -C INPUT -m mac --mac-source "${mac}" -p "${proto}" --dport "${port}" \
                        -m comment --comment "tunnelsats-k3s-ipv6-dns-guard" -j DROP 2>/dev/null; then
                        if ! ip6tables -I INPUT 1 -m mac --mac-source "${mac}" -p "${proto}" --dport "${port}" \
                            -m comment --comment "tunnelsats-k3s-ipv6-dns-guard" -j DROP; then
                            LAST_ERROR="k3s: Failed to add IPv6 INPUT DNS guard for MAC ${mac} (${proto}/${port})"
                            return 1
                        fi
                        changed=1
                    fi
                    if ! ip6tables -C FORWARD -m mac --mac-source "${mac}" -p "${proto}" --dport "${port}" \
                        -m comment --comment "tunnelsats-k3s-ipv6-dns-guard" -j DROP 2>/dev/null; then
                        if ! ip6tables -I FORWARD 1 -m mac --mac-source "${mac}" -p "${proto}" --dport "${port}" \
                            -m comment --comment "tunnelsats-k3s-ipv6-dns-guard" -j DROP; then
                            LAST_ERROR="k3s: Failed to add IPv6 FORWARD DNS guard for MAC ${mac} (${proto}/${port})"
                            return 1
                        fi
                        changed=1
                    fi
                done
            done
        done
    fi

    if ! ensure_k3s_dns_oif_policy_rule || ! ensure_k3s_dns_proxy_running; then
        remove_tagged_iptables_rules nat PREROUTING "tunnelsats-k3s-dns"
        remove_tagged_iptables_rules filter INPUT "tunnelsats-k3s-dns-in"
        flush_k3s_target_dns_conntrack "${target_ip}"
        return 1
    fi
    if [ "${K3S_DNS_CHANGED}" = "1" ]; then
        changed=1
    fi

    local need_input_in_sync=0
    if ! iptables -C INPUT -s "${target_ip}" -p udp --dport "${K3S_DNS_PROXY_PORT}" \
        -m comment --comment "tunnelsats-k3s-dns-in" -j ACCEPT 2>/dev/null || \
       ! iptables -C INPUT -s "${target_ip}" -p tcp --dport "${K3S_DNS_PROXY_PORT}" \
        -m comment --comment "tunnelsats-k3s-dns-in" -j ACCEPT 2>/dev/null || \
       ! iptables -C INPUT ! -s "${target_ip}" ! -i lo -p udp --dport "${K3S_DNS_PROXY_PORT}" \
        -m comment --comment "tunnelsats-k3s-dns-in" -j DROP 2>/dev/null || \
       ! iptables -C INPUT ! -s "${target_ip}" ! -i lo -p tcp --dport "${K3S_DNS_PROXY_PORT}" \
        -m comment --comment "tunnelsats-k3s-dns-in" -j DROP 2>/dev/null; then
        need_input_in_sync=1
    fi
    while IFS= read -r rule_line; do
        [ -n "${rule_line}" ] || continue
        if ! k3s_ipv4_dns_rule_is_current "${rule_line}" "${target_ip}" "${matched_pod_cidrs}" "${local_pod_ifaces}"; then
            need_input_in_sync=1
            break
        fi
    done < <(iptables -S INPUT 2>/dev/null | grep "tunnelsats-k3s-dns-in" || true)
    if [ "${need_input_in_sync}" -eq 1 ]; then
        remove_tagged_iptables_rules filter INPUT "tunnelsats-k3s-dns-in"
        if ! iptables -I INPUT 1 ! -s "${target_ip}" ! -i lo -p tcp --dport "${K3S_DNS_PROXY_PORT}" \
            -m comment --comment "tunnelsats-k3s-dns-in" -j DROP || \
           ! iptables -I INPUT 1 ! -s "${target_ip}" ! -i lo -p udp --dport "${K3S_DNS_PROXY_PORT}" \
            -m comment --comment "tunnelsats-k3s-dns-in" -j DROP || \
           ! iptables -I INPUT 1 -s "${target_ip}" -p tcp --dport "${K3S_DNS_PROXY_PORT}" \
            -m comment --comment "tunnelsats-k3s-dns-in" -j ACCEPT || \
           ! iptables -I INPUT 1 -s "${target_ip}" -p udp --dport "${K3S_DNS_PROXY_PORT}" \
            -m comment --comment "tunnelsats-k3s-dns-in" -j ACCEPT; then
            remove_tagged_iptables_rules nat PREROUTING "tunnelsats-k3s-dns"
            remove_tagged_iptables_rules filter INPUT "tunnelsats-k3s-dns-in"
            flush_k3s_target_dns_conntrack "${target_ip}"
            LAST_ERROR="k3s: Failed to add DNS proxy INPUT listener rules for ${target_ip}"
            return 1
        fi
        changed=1
    fi

    local need_nat_sync=0
    if ! iptables -t nat -C PREROUTING -s "${target_ip}" -p udp --dport 53 \
        -m comment --comment "tunnelsats-k3s-dns" -j REDIRECT --to-ports "${K3S_DNS_PROXY_PORT}" 2>/dev/null || \
       ! iptables -t nat -C PREROUTING -s "${target_ip}" -p tcp --dport 53 \
        -m comment --comment "tunnelsats-k3s-dns" -j REDIRECT --to-ports "${K3S_DNS_PROXY_PORT}" 2>/dev/null || \
       ! k3s_nat_dns_rules_precede_kube_services "${target_ip}"; then
        need_nat_sync=1
    fi

    local stale_nat_count
    stale_nat_count="$(iptables -t nat -S PREROUTING 2>/dev/null | grep "tunnelsats-k3s-dns" | grep -vcF -- "-s ${target_ip}" || true)"
    if [ "${stale_nat_count}" -gt 0 ]; then
        need_nat_sync=1
    fi

    if [ "${need_nat_sync}" -eq 1 ]; then
        remove_tagged_iptables_rules nat PREROUTING "tunnelsats-k3s-dns"
        if ! iptables -t nat -I PREROUTING 1 -s "${target_ip}" -p tcp --dport 53 \
            -m comment --comment "tunnelsats-k3s-dns" -j REDIRECT --to-ports "${K3S_DNS_PROXY_PORT}"; then
            remove_tagged_iptables_rules nat PREROUTING "tunnelsats-k3s-dns"
            flush_k3s_target_dns_conntrack "${target_ip}"
            LAST_ERROR="k3s: Failed to add TCP DNS interception rule for ${target_ip}"
            return 1
        fi
        if ! iptables -t nat -I PREROUTING 1 -s "${target_ip}" -p udp --dport 53 \
            -m comment --comment "tunnelsats-k3s-dns" -j REDIRECT --to-ports "${K3S_DNS_PROXY_PORT}"; then
            remove_tagged_iptables_rules nat PREROUTING "tunnelsats-k3s-dns"
            flush_k3s_target_dns_conntrack "${target_ip}"
            LAST_ERROR="k3s: Failed to add UDP DNS interception rule for ${target_ip}"
            return 1
        fi
        changed=1
    fi

    if [ "${changed}" -eq 1 ]; then
        flush_k3s_target_dns_conntrack "${target_ip}"
    fi

    DNS_PROTECTED="true"
    K3S_DNS_CHANGED="${changed}"
    return 0
}

flush_k3s_target_dns_conntrack() {
    local target_ip="${1:-}"
    local proto=""
    local port=""
    local v6_addr=""

    if [ -n "${target_ip}" ] && is_valid_ipv4_address "${target_ip}"; then
        if command -v conntrack >/dev/null 2>&1; then
            for proto in udp tcp; do
                for port in 53 853; do
                    conntrack -D -f ipv4 -s "${target_ip}" -p "${proto}" --dport "${port}" >/dev/null 2>&1 || true
                done
            done
        fi
        if command -v ss >/dev/null 2>&1; then
            for port in 53 853; do
                ss -K -4 src "${target_ip}" dport = :"${port}" >/dev/null 2>&1 || true
            done
        fi
    fi

    if command -v conntrack >/dev/null 2>&1; then
        for v6_addr in "${TARGET_IPV6_ADDRESSES[@]}"; do
            [ -n "${v6_addr}" ] || continue
            for proto in udp tcp; do
                for port in 53 853; do
                    conntrack -D -f ipv6 -s "${v6_addr}" -p "${proto}" --dport "${port}" >/dev/null 2>&1 || true
                done
            done
        done
    fi
}

k3s_dns_protection_is_synced() {
    if [[ "${K3S_MODE}" != "true" ]]; then
        DNS_PROTECTED="false"
        return 0
    fi

    if ! is_valid_ipv4_address "${DOCKER_TARGET_IP:-}"; then
        DNS_PROTECTED="false"
        LAST_ERROR="k3s: Missing target IP for DNS protection verification"
        return 1
    fi

    local matched_pod_cidrs=""
    local local_pod_ifaces=""
    local pod_cidr=""
    local pod_iface=""
    matched_pod_cidrs="$(resolve_k3s_local_pod_cidrs "${DOCKER_TARGET_IP}" 2>/dev/null || true)"
    if [ -z "${matched_pod_cidrs}" ]; then
        matched_pod_cidrs="10.42.0.0/16"
    fi
    local_pod_ifaces="$(resolve_k3s_local_pod_interfaces "${DOCKER_TARGET_IP}" 2>/dev/null || true)"
    if [ -z "${local_pod_ifaces}" ]; then
        local_pod_ifaces="cni+"
    fi

    if [ "${K3S_DNS_ENFORCE_OIF_RULE:-false}" = "true" ] || \
       { ! declare -F ip >/dev/null 2>&1 && ! declare -F iptables >/dev/null 2>&1; }; then
        local existing_oif_rules
        existing_oif_rules="$(ip rule show pref "${K3S_TUNNEL_RULE_PREF}" 2>/dev/null || true)"
        if ! grep -Fq -- "oif ${WG_IFACE} lookup 51820" <<< "${existing_oif_rules}" && \
           ! grep -Fq -- "oif ${WG_IFACE} table 51820" <<< "${existing_oif_rules}"; then
            DNS_PROTECTED="false"
            LAST_ERROR="k3s: Tunnel oif policy rule for ${WG_IFACE} is missing"
            log WARN "rules_are_synced: ${LAST_ERROR}"
            return 1
        fi
    fi

    if { [ "${K3S_DNS_ENFORCE_PROXY_PROCESS:-false}" = "true" ] || ! declare -F iptables >/dev/null 2>&1; } && \
       ! k3s_dns_proxy_is_running; then
        DNS_PROTECTED="false"
        LAST_ERROR="k3s: Split-horizon DNS proxy is not running"
        log WARN "rules_are_synced: ${LAST_ERROR}"
        return 1
    fi

    if ! iptables -t nat -C PREROUTING -s "${DOCKER_TARGET_IP}" -p udp --dport 53 \
        -m comment --comment "tunnelsats-k3s-dns" -j REDIRECT --to-ports "${K3S_DNS_PROXY_PORT}" 2>/dev/null || \
       ! iptables -t nat -C PREROUTING -s "${DOCKER_TARGET_IP}" -p tcp --dport 53 \
        -m comment --comment "tunnelsats-k3s-dns" -j REDIRECT --to-ports "${K3S_DNS_PROXY_PORT}" 2>/dev/null || \
       ! k3s_nat_dns_rules_precede_kube_services "${DOCKER_TARGET_IP}"; then
        DNS_PROTECTED="false"
        LAST_ERROR="k3s: DNS PREROUTING interception rules are missing or ordered after KUBE-SERVICES"
        log WARN "rules_are_synced: ${LAST_ERROR}"
        return 1
    fi

    if ! iptables -C INPUT -s "${DOCKER_TARGET_IP}" -p udp --dport "${K3S_DNS_PROXY_PORT}" \
        -m comment --comment "tunnelsats-k3s-dns-in" -j ACCEPT 2>/dev/null || \
       ! iptables -C INPUT -s "${DOCKER_TARGET_IP}" -p tcp --dport "${K3S_DNS_PROXY_PORT}" \
        -m comment --comment "tunnelsats-k3s-dns-in" -j ACCEPT 2>/dev/null || \
       ! iptables -C INPUT ! -s "${DOCKER_TARGET_IP}" ! -i lo -p udp --dport "${K3S_DNS_PROXY_PORT}" \
        -m comment --comment "tunnelsats-k3s-dns-in" -j DROP 2>/dev/null || \
       ! iptables -C INPUT ! -s "${DOCKER_TARGET_IP}" ! -i lo -p tcp --dport "${K3S_DNS_PROXY_PORT}" \
        -m comment --comment "tunnelsats-k3s-dns-in" -j DROP 2>/dev/null || \
       ! iptables -C INPUT -s "${DOCKER_TARGET_IP}" -p udp --dport 53 \
        -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP 2>/dev/null || \
       ! iptables -C INPUT -s "${DOCKER_TARGET_IP}" -p tcp --dport 53 \
        -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP 2>/dev/null || \
       ! iptables -C INPUT -s "${DOCKER_TARGET_IP}" -p udp --dport 853 \
        -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP 2>/dev/null || \
       ! iptables -C INPUT -s "${DOCKER_TARGET_IP}" -p tcp --dport 853 \
        -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP 2>/dev/null; then
        DNS_PROTECTED="false"
        LAST_ERROR="k3s: DNS proxy INPUT rules are missing"
        log WARN "rules_are_synced: ${LAST_ERROR}"
        return 1
    fi

    while IFS= read -r pod_cidr; do
        [ -n "${pod_cidr}" ] || continue
        [[ "${pod_cidr}" == */* ]] && [[ "${pod_cidr}" != */32 ]] || continue
        while IFS= read -r pod_iface; do
            [ -n "${pod_iface}" ] || continue
            if ! iptables -C INPUT -s "${pod_cidr}" -i "${pod_iface}" -m mark ! --mark "${K3S_NON_TARGET_FWMARK}/${K3S_NON_TARGET_FWMARK_MASK}" -p udp --dport 53 \
                -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP 2>/dev/null || \
               ! iptables -C INPUT -s "${pod_cidr}" -i "${pod_iface}" -m mark ! --mark "${K3S_NON_TARGET_FWMARK}/${K3S_NON_TARGET_FWMARK_MASK}" -p tcp --dport 53 \
                -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP 2>/dev/null || \
               ! iptables -C INPUT -s "${pod_cidr}" -i "${pod_iface}" -m mark ! --mark "${K3S_NON_TARGET_FWMARK}/${K3S_NON_TARGET_FWMARK_MASK}" -p udp --dport 853 \
                -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP 2>/dev/null || \
               ! iptables -C INPUT -s "${pod_cidr}" -i "${pod_iface}" -m mark ! --mark "${K3S_NON_TARGET_FWMARK}/${K3S_NON_TARGET_FWMARK_MASK}" -p tcp --dport 853 \
                -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP 2>/dev/null; then
                DNS_PROTECTED="false"
                LAST_ERROR="k3s: Local pod-CIDR DNS INPUT guard rules are missing for ${pod_cidr} on ${pod_iface}"
                log WARN "rules_are_synced: ${LAST_ERROR}"
                return 1
            fi
        done <<< "${local_pod_ifaces}"
    done <<< "${matched_pod_cidrs}"

    if ! iptables -C FORWARD -s "${DOCKER_TARGET_IP}" ! -o "${WG_IFACE}" -p udp --dport 53 \
        -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP 2>/dev/null || \
       ! iptables -C FORWARD -s "${DOCKER_TARGET_IP}" ! -o "${WG_IFACE}" -p tcp --dport 53 \
        -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP 2>/dev/null || \
       ! iptables -C FORWARD -s "${DOCKER_TARGET_IP}" ! -o "${WG_IFACE}" -p udp --dport 853 \
        -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP 2>/dev/null || \
       ! iptables -C FORWARD -s "${DOCKER_TARGET_IP}" ! -o "${WG_IFACE}" -p tcp --dport 853 \
        -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP 2>/dev/null; then
        DNS_PROTECTED="false"
        LAST_ERROR="k3s: FORWARD DNS/DoT guard rules are missing"
        log WARN "rules_are_synced: ${LAST_ERROR}"
        return 1
    fi

    while IFS= read -r pod_cidr; do
        [ -n "${pod_cidr}" ] || continue
        [[ "${pod_cidr}" == */* ]] && [[ "${pod_cidr}" != */32 ]] || continue
        while IFS= read -r pod_iface; do
            [ -n "${pod_iface}" ] || continue
            if ! iptables -C FORWARD -s "${pod_cidr}" -i "${pod_iface}" ! -o "${WG_IFACE}" -m mark ! --mark "${K3S_NON_TARGET_FWMARK}/${K3S_NON_TARGET_FWMARK_MASK}" -p udp --dport 53 \
                -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP 2>/dev/null || \
               ! iptables -C FORWARD -s "${pod_cidr}" -i "${pod_iface}" ! -o "${WG_IFACE}" -m mark ! --mark "${K3S_NON_TARGET_FWMARK}/${K3S_NON_TARGET_FWMARK_MASK}" -p tcp --dport 53 \
                -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP 2>/dev/null || \
               ! iptables -C FORWARD -s "${pod_cidr}" -i "${pod_iface}" ! -o "${WG_IFACE}" -m mark ! --mark "${K3S_NON_TARGET_FWMARK}/${K3S_NON_TARGET_FWMARK_MASK}" -p udp --dport 853 \
                -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP 2>/dev/null || \
               ! iptables -C FORWARD -s "${pod_cidr}" -i "${pod_iface}" ! -o "${WG_IFACE}" -m mark ! --mark "${K3S_NON_TARGET_FWMARK}/${K3S_NON_TARGET_FWMARK_MASK}" -p tcp --dport 853 \
                -m comment --comment "tunnelsats-k3s-dns-guard" -j DROP 2>/dev/null; then
                DNS_PROTECTED="false"
                LAST_ERROR="k3s: Local pod-CIDR FORWARD DNS/DoT guard rules are missing for ${pod_cidr} on ${pod_iface}"
                log WARN "rules_are_synced: ${LAST_ERROR}"
                return 1
            fi
        done <<< "${local_pod_ifaces}"
    done <<< "${matched_pod_cidrs}"

    if command -v ip6tables >/dev/null 2>&1 && \
       { declare -F ip6tables >/dev/null 2>&1 || ! declare -F iptables >/dev/null 2>&1; }; then
        local proto=""
        local port=""
        local v6_addr=""
        local mac=""
        local chain=""
        local rules=""
        local rule=""
        for chain in INPUT FORWARD; do
            rules="$(ip6tables -S "${chain}" 2>/dev/null | grep -F -- "--comment tunnelsats-k3s-ipv6-dns-guard" || true)"
            while IFS= read -r rule; do
                [ -n "${rule}" ] || continue
                if ! ipv6_filter_rule_is_current "${rule}"; then
                    DNS_PROTECTED="false"
                    LAST_ERROR="k3s: Stale or unscoped IPv6 DNS guard rule in ${chain}"
                    log WARN "rules_are_synced: ${LAST_ERROR}"
                    return 1
                fi
            done <<< "${rules}"
        done
        for v6_addr in "${TARGET_IPV6_ADDRESSES[@]}"; do
            [ -n "${v6_addr}" ] || continue
            for proto in udp tcp; do
                for port in 53 853; do
                    if ! ip6tables -C INPUT -s "${v6_addr}" -p "${proto}" --dport "${port}" \
                        -m comment --comment "tunnelsats-k3s-ipv6-dns-guard" -j DROP 2>/dev/null || \
                       ! ip6tables -C FORWARD -s "${v6_addr}" -p "${proto}" --dport "${port}" \
                        -m comment --comment "tunnelsats-k3s-ipv6-dns-guard" -j DROP 2>/dev/null; then
                        DNS_PROTECTED="false"
                        LAST_ERROR="k3s: IPv6 DNS guard rules are missing for ${v6_addr} (${proto}/${port})"
                        log WARN "rules_are_synced: ${LAST_ERROR}"
                        return 1
                    fi
                done
            done
        done
        for mac in "${TARGET_IPV6_MACS[@]}"; do
            [ -n "${mac}" ] || continue
            for proto in udp tcp; do
                for port in 53 853; do
                    if ! ip6tables -C INPUT -m mac --mac-source "${mac}" -p "${proto}" --dport "${port}" \
                        -m comment --comment "tunnelsats-k3s-ipv6-dns-guard" -j DROP 2>/dev/null || \
                       ! ip6tables -C FORWARD -m mac --mac-source "${mac}" -p "${proto}" --dport "${port}" \
                        -m comment --comment "tunnelsats-k3s-ipv6-dns-guard" -j DROP 2>/dev/null; then
                        DNS_PROTECTED="false"
                        LAST_ERROR="k3s: IPv6 DNS guard rules are missing for MAC ${mac} (${proto}/${port})"
                        log WARN "rules_are_synced: ${LAST_ERROR}"
                        return 1
                    fi
                done
            done
        done
    fi

    DNS_PROTECTED="true"
    return 0
}

cleanup_k3s_dns_protection() {
    local keep_tunnel="${1:-false}"

    stop_k3s_dns_proxy
    remove_tagged_iptables_rules nat PREROUTING "tunnelsats-k3s-dns"
    remove_tagged_iptables_rules filter INPUT "tunnelsats-k3s-dns-in"
    flush_k3s_target_dns_conntrack "${DOCKER_TARGET_IP:-${MANAGED_K3S_TARGET_IP:-}}"
    DNS_PROTECTED="false"

    if [ "${keep_tunnel}" != "false" ]; then
        return 0
    fi

    remove_tagged_iptables_rules filter INPUT "tunnelsats-k3s-dns-guard"
    remove_tagged_iptables_rules filter FORWARD "tunnelsats-k3s-dns-guard"

    if command -v ip6tables >/dev/null 2>&1; then
        local chain=""
        local rules=""
        local rule=""
        local -a parts=()
        for chain in INPUT FORWARD; do
            rules="$(ip6tables -S "${chain}" 2>/dev/null | grep -F -- "--comment tunnelsats-k3s-ipv6-dns-guard" || true)"
            while IFS= read -r rule; do
                [ -n "${rule}" ] || continue
                read -r -a parts <<< "${rule}"
                parts[0]="-D"
                ip6tables "${parts[@]}" >/dev/null 2>&1 || true
            done <<< "${rules}"
        done
    fi
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

    if [[ "${K3S_MODE}" == "true" ]] || [[ "${SECURE_MODE}" == "true" ]]; then
        # Scope FORWARD rules to the target IP
        if ! iptables -C FORWARD -i "${WG_IFACE}" -d "${DOCKER_TARGET_IP}" \
            -m comment --comment "tunnelsats-forward-in" -j ACCEPT 2>/dev/null; then
            log INFO "Syncing FORWARD inbound rules"
            remove_tagged_iptables_rules filter FORWARD "tunnelsats-forward-in"
            if ! iptables -I FORWARD 1 -i "${WG_IFACE}" -d "${DOCKER_TARGET_IP}" \
                -m comment --comment "tunnelsats-forward-in" -j ACCEPT; then
                LAST_ERROR="Failed to add FORWARD inbound rule"
                return 1
            fi
            changed=1
        fi

        if ! iptables -C FORWARD -s "${DOCKER_TARGET_IP}" -o "${WG_IFACE}" \
            -m comment --comment "tunnelsats-forward-out" -j ACCEPT 2>/dev/null; then
            log INFO "Syncing FORWARD outbound rules"
            remove_tagged_iptables_rules filter FORWARD "tunnelsats-forward-out"
            if ! iptables -I FORWARD 2 -s "${DOCKER_TARGET_IP}" -o "${WG_IFACE}" \
                -m comment --comment "tunnelsats-forward-out" -j ACCEPT; then
                LAST_ERROR="Failed to add FORWARD outbound rule"
                return 1
            fi
            changed=1
        fi

        if [[ "${K3S_MODE}" == "true" ]]; then
            # Source routing supersedes the old reply-only CONNMARK design.
            # Remove all tagged remnants during migration.
            local legacy_mangle_removed=0
            remove_tagged_iptables_rules mangle PREROUTING "tunnelsats-conn-restore"
            [ "${IPTABLES_RULES_REMOVED:-0}" = "1" ] && legacy_mangle_removed=1
            remove_tagged_iptables_rules mangle FORWARD "tunnelsats-conn-save"
            [ "${IPTABLES_RULES_REMOVED:-0}" = "1" ] && legacy_mangle_removed=1
            remove_tagged_iptables_rules mangle FORWARD "tunnelsats-wg-mark"
            [ "${IPTABLES_RULES_REMOVED:-0}" = "1" ] && legacy_mangle_removed=1
            if [ "${legacy_mangle_removed}" = "1" ]; then
                changed=1
            fi

            if ! ensure_k3s_dns_protection "${DOCKER_TARGET_IP}"; then
                return 1
            fi
            if [ "${K3S_DNS_CHANGED:-0}" = "1" ]; then
                changed=1
            fi
        else
            # Secure Mode still uses conntrack marks for reply routing.
            if ! iptables -t mangle -C PREROUTING ! -i "${WG_IFACE}" -s "${DOCKER_TARGET_IP}" \
                -m comment --comment "tunnelsats-conn-restore" -j CONNMARK --restore-mark --mask 0xca6c 2>/dev/null; then
                log INFO "Syncing mangle CONNMARK restore rule"
                remove_tagged_iptables_rules mangle PREROUTING "tunnelsats-conn-restore"
                if ! iptables -t mangle -A PREROUTING ! -i "${WG_IFACE}" -s "${DOCKER_TARGET_IP}" \
                    -m comment --comment "tunnelsats-conn-restore" -j CONNMARK --restore-mark --mask 0xca6c; then
                    LAST_ERROR="Failed to add CONNMARK restore-mark rule"
                    return 1
                fi
                changed=1
            fi

            remove_tagged_iptables_rules mangle FORWARD "tunnelsats-wg-mark"

            if ! iptables -t mangle -C FORWARD -i "${WG_IFACE}" -d "${DOCKER_TARGET_IP}" \
                -m comment --comment "tunnelsats-conn-save" -j CONNMARK --set-mark 0xca6c/0xca6c 2>/dev/null; then
                log INFO "Syncing mangle CONNMARK set-mark rule"
                remove_tagged_iptables_rules mangle FORWARD "tunnelsats-conn-save"
                if ! iptables -t mangle -A FORWARD -i "${WG_IFACE}" -d "${DOCKER_TARGET_IP}" \
                    -m comment --comment "tunnelsats-conn-save" -j CONNMARK --set-mark 0xca6c/0xca6c; then
                    LAST_ERROR="Failed to add CONNMARK set-mark rule"
                    return 1
                fi
                changed=1
            fi
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
    local masq_src
    if [[ "${K3S_MODE}" == "true" ]] || [[ "${SECURE_MODE}" == "true" ]]; then
        masq_src="${DOCKER_TARGET_IP}"
    else
        masq_src="${DOCKER_NETWORK_SUBNET}"
    fi
    local first_rule
    first_rule=$(iptables -t nat -S POSTROUTING 2>/dev/null | grep -v '^-P' | head -n 1 || true)
    if ! echo "${first_rule}" | grep -F "tunnelsats-masq" | grep -F -- "-s ${masq_src}" | grep -F -- "-o ${WG_IFACE}" | grep -qF -- "-j MASQUERADE"; then
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

fail_closed_policy_prerequisites_are_synced() {
    [[ "${K3S_MODE}" == "true" ]] && return 0

    local source_prefix
    if [[ "${SECURE_MODE}" == "true" ]]; then
        source_prefix="${DOCKER_TARGET_IP}"
    else
        source_prefix="${DOCKER_NETWORK_SUBNET}"
    fi

    local escaped_source="${source_prefix//./\\.}"
    local displayed_source="${escaped_source}"
    [[ "${source_prefix}" == */* ]] || displayed_source+="(/32)?"
    if ! ip rule show pref "${K3S_BLACKHOLE_RULE_PREF}" 2>/dev/null \
        | grep -qE "^${K3S_BLACKHOLE_RULE_PREF}:[[:space:]]+from[[:space:]]+${displayed_source}[[:space:]]+blackhole[[:space:]]*$"; then
        log WARN "rules_are_synced: source fallback blackhole FAIL"
        return 1
    fi

    local table_routes
    table_routes="$(ip route show table 51820 2>/dev/null || true)"
    if ! printf '%s\n' "${table_routes}" \
        | grep -qE "^default([[:space:]].*)?[[:space:]]dev[[:space:]]+${WG_IFACE}([[:space:]]|$)"; then
        log WARN "rules_are_synced: WireGuard table default route FAIL"
        return 1
    fi
    if ! printf '%s\n' "${table_routes}" | grep -qE '^blackhole[[:space:]]+default([[:space:]]|$)'; then
        log WARN "rules_are_synced: policy table blackhole FAIL"
        return 1
    fi
}

verify_k3s_target_routes() {
    [[ "${K3S_MODE}" == "true" ]] || return 0
    [ -n "${DOCKER_TARGET_IP:-}" ] || {
        LAST_ERROR="k3s: Cannot verify target routes without DOCKER_TARGET_IP"
        return 1
    }

    local destination route_info pod_iif="" ip_is_function=false
    if declare -F ip >/dev/null 2>&1; then
        ip_is_function=true
    fi
    pod_iif="$(ip -4 route get "${DOCKER_TARGET_IP}" 2>/dev/null | awk '{for (i = 1; i <= NF; i++) if ($i == "dev") {print $(i + 1); exit}}' || true)"
    if [ "${pod_iif}" = "${WG_IFACE}" ] || [ "${pod_iif}" = "lo" ]; then
        pod_iif=""
    fi

    local all_rules shadow_error probe_dests_csv
    probe_dests_csv="$(IFS=,; printf '%s' "${DOCKER_ROUTE_PROBE_DESTINATIONS[*]}")"
    all_rules="$(ip rule show 2>/dev/null || true)"
    if [ -z "${all_rules}" ]; then
        all_rules="$(ip rule show pref "${K3S_QUARANTINE_RULE_PREF}" 2>/dev/null || true)"
    fi
    if [ -n "${all_rules}" ]; then
        if ! shadow_error="$(
            TARGET_IP="${DOCKER_TARGET_IP}" \
            TUNNEL_PREF="${K3S_TUNNEL_RULE_PREF}" \
            BYPASS_PREF="${K3S_BYPASS_RULE_PREF}" \
            QUARANTINE_PREF="${K3S_QUARANTINE_RULE_PREF}" \
            BLACKHOLE_PREF="${K3S_BLACKHOLE_RULE_PREF}" \
            RULE_PROTOCOL="${K3S_RULE_PROTOCOL}" \
            ALLOW_QUARANTINE="${K3S_ALLOW_ACTIVE_QUARANTINE_DURING_SYNC:-false}" \
            POD_IIF="${pod_iif}" \
            BYPASS_CIDRS="${K3S_BYPASS_CIDRS_NORMALIZED:-}" \
            PROBE_DESTS="${probe_dests_csv}" \
            ALL_RULES="${all_rules}" \
            python3 -c '
import ipaddress
import os
import re
import sys

target_ip = ipaddress.IPv4Address(os.environ["TARGET_IP"])
tunnel_pref = int(os.environ["TUNNEL_PREF"])
bypass_pref = int(os.environ["BYPASS_PREF"])
quarantine_pref = int(os.environ["QUARANTINE_PREF"])
blackhole_pref = int(os.environ["BLACKHOLE_PREF"])
rule_protocol = os.environ["RULE_PROTOCOL"]
allow_quarantine = os.environ.get("ALLOW_QUARANTINE", "false") == "true"
pod_iif = os.environ.get("POD_IIF", "").strip()
bypass_nets = []
for raw_cidr in os.environ.get("BYPASS_CIDRS", "").split(","):
    raw_cidr = raw_cidr.strip()
    if not raw_cidr:
        continue
    try:
        bypass_nets.append(ipaddress.ip_network(raw_cidr, strict=False))
    except ValueError:
        pass
probe_dests = [
    ipaddress.IPv4Address(item.strip())
    for item in os.environ.get("PROBE_DESTS", "1.1.1.1,8.8.8.8").split(",")
    if item.strip()
]
owned_prefs = {bypass_pref, quarantine_pref, tunnel_pref, blackhole_pref}
quarantine_active = False

for raw_line in os.environ.get("ALL_RULES", "").splitlines():
    line = raw_line.strip()
    if not line or ":" not in line:
        continue
    pref_str, spec = line.split(":", 1)
    try:
        pref = int(pref_str.strip())
    except ValueError:
        continue
    if pref > tunnel_pref:
        continue
    spec_clean = " ".join(spec.split())
    if pref == 0 and re.search(r"(^|\s)(lookup|table)\s+local(\s|$)", spec_clean):
        continue
    has_proto = bool(re.search(rf"(^|\s)proto(col)?\s+{re.escape(rule_protocol)}(\s|$)", spec_clean))
    is_owned = has_proto and pref in owned_prefs
    tokens = spec_clean.split()
    src_token = "all"
    if "from" in tokens:
        idx = tokens.index("from")
        if idx + 1 < len(tokens):
            src_token = tokens[idx + 1]
    matches_target = False
    if src_token in ("all", "0.0.0.0/0", "0/0"):
        matches_target = True
    else:
        try:
            if "/" in src_token:
                net = ipaddress.ip_network(src_token, strict=False)
                matches_target = target_ip in net
            else:
                addr = ipaddress.ip_address(src_token)
                matches_target = addr == target_ip
        except ValueError:
            matches_target = False
    dst_token = "all"
    if "to" in tokens:
        idx = tokens.index("to")
        if idx + 1 < len(tokens):
            dst_token = tokens[idx + 1]
    if "not" in tokens:
        if is_owned:
            if pref == quarantine_pref:
                if not allow_quarantine:
                    print(f"k3s: Emergency quarantine rule at pref {pref} still shadows target pod routing: {line}")
                    sys.exit(1)
                quarantine_active = True
            continue
        if re.search(r"(^|\s)(lookup|table)\s+51820(\s|$)", spec_clean):
            continue
        invert_extra_selectors = {
            "fwmark", "iif", "oif", "ipproto", "sport", "dport",
            "tos", "dsfield", "uidrange", "l3mdev", "tun_id",
        }
        if (
            matches_target
            and dst_token in ("all", "0.0.0.0/0", "0/0")
            and not (invert_extra_selectors & set(tokens))
        ):
            continue
        print(f"k3s: Policy rule at pref {pref} shadows target pod routing: {line}")
        sys.exit(1)
    if not matches_target:
        continue
    if is_owned:
        if pref == quarantine_pref:
            if not allow_quarantine:
                print(f"k3s: Emergency quarantine rule at pref {pref} still shadows target pod routing: {line}")
                sys.exit(1)
            quarantine_active = True
        elif pref == bypass_pref and dst_token in ("all", "0.0.0.0/0", "0/0"):
            print(f"k3s: Non-target bypass rule at pref {pref} shadows target pod routing: {line}")
            sys.exit(1)
        continue
    if "fwmark" in tokens:
        idx = tokens.index("fwmark")
        mark_spec = tokens[idx + 1] if idx + 1 < len(tokens) else ""
        try:
            if "/" in mark_spec:
                m_val_s, m_mask_s = mark_spec.split("/", 1)
                m_val = int(m_val_s, 0)
                m_mask = int(m_mask_s, 0)
            else:
                m_val = int(mark_spec, 0)
                m_mask = 0xFFFFFFFF
            matches_unmarked = ((0 & m_mask) == (m_val & m_mask))
        except ValueError:
            matches_unmarked = True
        if not matches_unmarked:
            continue
    if "ipproto" in tokens:
        idx = tokens.index("ipproto")
        proto_tok = (tokens[idx + 1] if idx + 1 < len(tokens) else "").lower()
        if proto_tok not in ("tcp", "udp", "icmp", "6", "17", "1", "all", "ip", "0"):
            continue
    if "oif" in tokens:
        idx = tokens.index("oif")
        oif_dev = tokens[idx + 1] if idx + 1 < len(tokens) else ""
        if oif_dev == "lo":
            continue
    if "iif" in tokens:
        idx = tokens.index("iif")
        iif_dev = tokens[idx + 1] if idx + 1 < len(tokens) else ""
        if pod_iif and iif_dev != pod_iif:
            continue
    if re.search(r"(^|\s)(lookup|table)\s+51820(\s|$)", spec_clean):
        continue
    if dst_token not in ("all", "0.0.0.0/0", "0/0"):
        try:
            dst_net = ipaddress.ip_network(dst_token, strict=False)
            matches_probe = any(probe in dst_net for probe in probe_dests)
            is_non_internet = any(dst_net.subnet_of(b) for b in bypass_nets) or not dst_net.is_global
        except ValueError:
            matches_probe = True
            is_non_internet = False
        if not matches_probe and is_non_internet:
            continue
    print(f"k3s: Policy rule at pref {pref} shadows target pod routing: {line}")
    sys.exit(1)

if quarantine_active:
    print("QUARANTINE_ACTIVE")
' 2>/dev/null
        )"; then
            LAST_ERROR="${shadow_error:-k3s: Policy rule shadows target pod routing}"
            return 1
        fi
        if [ "${shadow_error}" = "QUARANTINE_ACTIVE" ]; then
            return 0
        fi
    fi

    local ln_port="${LN_TARGET_PORT:-9735}"
    local tcp_route_info
    for destination in "${DOCKER_ROUTE_PROBE_DESTINATIONS[@]}"; do
        if [ -n "${pod_iif}" ]; then
            route_info="$(ip -4 route get "${destination}" from "${DOCKER_TARGET_IP}" iif "${pod_iif}" 2>&1 || true)"
        else
            route_info="$(ip -4 route get "${destination}" from "${DOCKER_TARGET_IP}" 2>&1 || true)"
        fi
        if [ -z "${route_info}" ] && [ "${ip_is_function}" = true ]; then
            continue
        fi
        if [[ "${route_info}" != *"dev ${WG_IFACE}"* ]] || \
           [[ "${route_info}" == *"blackhole"* ]] || \
           [[ "${route_info}" == *"unreachable"* ]] || \
           [[ "${route_info}" == *"prohibit"* ]] || \
           [[ "${route_info}" == *"throw"* ]]; then
            LAST_ERROR="Unsafe k3s route to ${destination} from ${DOCKER_TARGET_IP}: expected dev ${WG_IFACE}, got ${route_info//$'\n'/ }"
            return 1
        fi
        if [ "${ip_is_function}" = false ]; then
            if [ -n "${pod_iif}" ]; then
                tcp_route_info="$(ip -4 route get "${destination}" from "${DOCKER_TARGET_IP}" iif "${pod_iif}" ipproto tcp sport "${ln_port}" dport "${ln_port}" 2>&1 || true)"
            else
                tcp_route_info="$(ip -4 route get "${destination}" from "${DOCKER_TARGET_IP}" ipproto tcp sport "${ln_port}" dport "${ln_port}" 2>&1 || true)"
            fi
            if [ -n "${tcp_route_info}" ] && { \
               [[ "${tcp_route_info}" != *"dev ${WG_IFACE}"* ]] || \
               [[ "${tcp_route_info}" == *"blackhole"* ]] || \
               [[ "${tcp_route_info}" == *"unreachable"* ]] || \
               [[ "${tcp_route_info}" == *"prohibit"* ]] || \
               [[ "${tcp_route_info}" == *"throw"* ]]; }; then
                LAST_ERROR="Unsafe k3s TCP route to ${destination}:${ln_port} from ${DOCKER_TARGET_IP}: expected dev ${WG_IFACE}, got ${tcp_route_info//$'\n'/ }"
                return 1
            fi
        fi
    done
    return 0
}

k3s_candidate_pod_guards_are_synced() {
    [[ "${K3S_MODE}" == "true" ]] || return 0
    if [ "${K3S_CANDIDATE_PODS_GUARDED:-true}" != "true" ]; then
        LAST_ERROR="k3s: Co-located candidate pod guards failed during discovery"
        log WARN "rules_are_synced: ${LAST_ERROR}"
        return 1
    fi
    if [ "${K3S_ALLOW_ACTIVE_QUARANTINE_DURING_SYNC:-false}" != "true" ] && \
       [ "${K3S_UNSEEN_REPLACEMENT_PENDING:-false}" = "true" ]; then
        LAST_ERROR="k3s: Replacement pod is pending IP assignment; retaining pre-discovery containment"
        log WARN "rules_are_synced: ${LAST_ERROR}"
        return 1
    fi
    [ -n "${K3S_CANDIDATE_POD_IPS:-}" ] || return 0

    local candidate_ip
    local -a candidate_ips=()
    IFS=',' read -r -a candidate_ips <<< "${K3S_CANDIDATE_POD_IPS}"
    for candidate_ip in "${candidate_ips[@]}"; do
        candidate_ip="${candidate_ip%/32}"
        [ -n "${candidate_ip}" ] || continue
        [ "${candidate_ip}" = "${DOCKER_TARGET_IP:-}" ] && continue

        local has_blackhole=false
        local has_filter_guard=false
        if ip rule show pref "${K3S_BLACKHOLE_RULE_PREF}" 2>/dev/null | grep -qE \
            "from[[:space:]]+${candidate_ip//./\\.}[[:space:]]+blackhole([[:space:]].*)?[[:space:]]+proto(col)?[[:space:]]+${K3S_RULE_PROTOCOL}[[:space:]]*$"; then
            has_blackhole=true
        fi
        if iptables -C FORWARD -s "${candidate_ip}" \
            -m comment --comment "tunnelsats-k3s-egress-guard" -j DROP >/dev/null 2>&1; then
            has_filter_guard=true
        fi
        if [ "${has_blackhole}" = false ] && [ "${has_filter_guard}" = false ]; then
            LAST_ERROR="k3s: Co-located candidate pod ${candidate_ip} is missing per-pod isolation guard"
            log WARN "rules_are_synced: k3s candidate pod ${candidate_ip} guard FAIL"
            return 1
        fi
        if ip rule show pref "${K3S_BYPASS_RULE_PREF}" 2>/dev/null | grep -qE \
            "from[[:space:]]+${candidate_ip//./\\.}([[:space:]]|/32)" || \
           ip rule show pref "${K3S_TUNNEL_RULE_PREF}" 2>/dev/null | grep -qE \
            "from[[:space:]]+${candidate_ip//./\\.}([[:space:]]|/32)"; then
            LAST_ERROR="k3s: Non-selected candidate pod ${candidate_ip} still has active routing rules"
            log WARN "rules_are_synced: k3s candidate pod ${candidate_ip} stale route rule FAIL"
            return 1
        fi
    done
    return 0
}

ipv4_rules_are_synced() {
    if ! fail_closed_policy_prerequisites_are_synced; then
        return 1
    fi

    # We match the config-defined VPNPort on the tunnel interface to catch these packets.
    local internal_match_port="${FORWARDING_PORT}"

    if [[ "${K3S_MODE}" == "true" ]] || [[ "${SECURE_MODE}" == "true" ]]; then
        if [[ "${SECURE_MODE}" == "true" ]]; then
            # Discover target's bridge network subnet dynamically
            local subnet
            subnet=$(get_target_subnet)

            if ! ip rule show | grep -qE "from[[:space:]]+${DOCKER_TARGET_IP//./\\.}[[:space:]]+to[[:space:]]+${subnet//./\\.}[[:space:]]+(lookup|table)[[:space:]]+main"; then
                log WARN "rules_are_synced: SecureMode local bypass rule FAIL"
                return 1
            fi
            if ! ip rule show | grep -qE "from[[:space:]]+${DOCKER_TARGET_IP//./\\.}[[:space:]]+(lookup|table)[[:space:]]+51820"; then
                log WARN "rules_are_synced: SecureMode policy routing rule FAIL"
                return 1
            fi
        else
            if ! normalize_k3s_bypass_cidrs; then
                log WARN "rules_are_synced: ${LAST_ERROR}"
                return 1
            fi

            local bypass_cidr
            local -a expected_bypass_cidrs=()
            if [ -n "${K3S_BYPASS_CIDRS_NORMALIZED}" ]; then
                IFS=',' read -r -a expected_bypass_cidrs <<< "${K3S_BYPASS_CIDRS_NORMALIZED}"
            fi
            for bypass_cidr in "${expected_bypass_cidrs[@]}"; do
                if ! ip rule show pref "${K3S_BYPASS_RULE_PREF}" 2>/dev/null | grep -qE \
                    "from[[:space:]]+${DOCKER_TARGET_IP//./\\.}[[:space:]]+to[[:space:]]+${bypass_cidr//./\\.}[[:space:]]+(lookup|table)[[:space:]]+main([[:space:]].*)?[[:space:]]+proto(col)?[[:space:]]+${K3S_RULE_PROTOCOL}"; then
                    log WARN "rules_are_synced: k3s bypass ${bypass_cidr} FAIL"
                    return 1
                fi
            done

            local actual_bypass_count=0
            local bypass_rule
            local bypass_rule_spec
            local actual_destination
            local expected_bypass_csv=",${K3S_BYPASS_CIDRS_NORMALIZED},"
            while IFS= read -r bypass_rule; do
                [ -n "${bypass_rule}" ] || continue
                bypass_rule_spec="${bypass_rule#*:}"
                [[ "${bypass_rule_spec}" == *"from ${DOCKER_TARGET_IP} "* ]] || continue
                k3s_policy_rule_is_owned "${bypass_rule}" || continue
                if [[ "${bypass_rule_spec}" != *" lookup main"* ]] && [[ "${bypass_rule_spec}" != *" table main"* ]]; then
                    continue
                fi
                actual_destination="$(awk '{for (i = 1; i <= NF; i++) if ($i == "to") {print $(i + 1); exit}}' <<< "${bypass_rule_spec}")"
                if [ -z "${actual_destination}" ] || [[ "${expected_bypass_csv}" != *",${actual_destination},"* ]]; then
                    log WARN "rules_are_synced: unexpected k3s bypass ${actual_destination:-missing}"
                    return 1
                fi
                actual_bypass_count=$((actual_bypass_count + 1))
            done < <(ip rule show pref "${K3S_BYPASS_RULE_PREF}" 2>/dev/null || true)
            if [ "${actual_bypass_count}" -ne "${#expected_bypass_cidrs[@]}" ]; then
                log WARN "rules_are_synced: k3s bypass count FAIL"
                return 1
            fi

            if ! ip rule show | grep -qE \
                "^${K3S_TUNNEL_RULE_PREF}:[[:space:]]+from[[:space:]]+${DOCKER_TARGET_IP//./\\.}[[:space:]]+(lookup|table)[[:space:]]+51820([[:space:]].*)?[[:space:]]+proto(col)?[[:space:]]+${K3S_RULE_PROTOCOL}[[:space:]]*$"; then
                log WARN "rules_are_synced: k3s full-outbound rule FAIL"
                return 1
            fi

            if ! ip rule show pref "${K3S_BLACKHOLE_RULE_PREF}" 2>/dev/null | grep -qE \
                "from[[:space:]]+${DOCKER_TARGET_IP//./\\.}[[:space:]]+blackhole([[:space:]].*)?[[:space:]]+proto(col)?[[:space:]]+${K3S_RULE_PROTOCOL}[[:space:]]*$"; then
                log WARN "rules_are_synced: k3s source blackhole FAIL"
                return 1
            fi

            if ip rule show | grep -qiE \
                "fwmark 0x0*ca6c.*(lookup|table)[[:space:]]+51820"; then
                log WARN "rules_are_synced: legacy k3s fwmark rule remains"
                return 1
            fi

            local table_51820
            table_51820="$(ip route show table 51820 2>/dev/null || true)"
            if ! grep -qE \
                "^default([[:space:]].*)dev[[:space:]]+${WG_IFACE}([[:space:]].*)metric[[:space:]]+2([[:space:]]|$)" \
                <<< "${table_51820}"; then
                log WARN "rules_are_synced: k3s WireGuard table default FAIL"
                return 1
            fi
            if ! grep -qE \
                "^blackhole[[:space:]]+default([[:space:]].*)metric[[:space:]]+3([[:space:]]|$)" \
                <<< "${table_51820}"; then
                log WARN "rules_are_synced: k3s table blackhole FAIL"
                return 1
            fi
            local default_route_count
            default_route_count="$(
                grep -cE "^(blackhole[[:space:]]+)?default([[:space:]]|$)" <<< "${table_51820}" || true
            )"
            if [ "${default_route_count}" -ne 2 ]; then
                log WARN "rules_are_synced: unexpected k3s policy table default"
                return 1
            fi

            if ! verify_k3s_target_routes; then
                log WARN "rules_are_synced: ${LAST_ERROR}"
                return 1
            fi

            if ! k3s_candidate_pod_guards_are_synced; then
                return 1
            fi

            if ! k3s_dns_protection_is_synced; then
                return 1
            fi
        fi

        if [[ "${SECURE_MODE}" == "true" ]]; then
            if ! iptables -t mangle -C PREROUTING ! -i "${WG_IFACE}" -s "${DOCKER_TARGET_IP}" \
                -m comment --comment "tunnelsats-conn-restore" -j CONNMARK --restore-mark --mask 0xca6c 2>/dev/null; then
                log WARN "rules_are_synced: mangle conn-restore FAIL (missing or wrong form)"
                return 1
            fi

            if ! iptables -t mangle -C FORWARD -i "${WG_IFACE}" -d "${DOCKER_TARGET_IP}" \
                -m comment --comment "tunnelsats-conn-save" -j CONNMARK --set-mark 0xca6c/0xca6c 2>/dev/null; then
                log WARN "rules_are_synced: mangle conn-save FAIL (missing or wrong form)"
                return 1
            fi
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

        # 3. FORWARD Inbound check (scoped to target IP)
        if ! iptables -C FORWARD -i "${WG_IFACE}" -d "${DOCKER_TARGET_IP}" \
            -m comment --comment "tunnelsats-forward-in" -j ACCEPT 2>/dev/null; then
            log WARN "rules_are_synced: FORWARD in FAIL"
            return 1
        fi

        # 4. FORWARD Outbound check (scoped to target IP)
        if ! iptables -C FORWARD -s "${DOCKER_TARGET_IP}" -o "${WG_IFACE}" \
            -m comment --comment "tunnelsats-forward-out" -j ACCEPT 2>/dev/null; then
            log WARN "rules_are_synced: FORWARD out FAIL"
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

    if ! docker_source_policy_rules_are_synced; then
        return 1
    fi

    if ! verify_docker_target_routes; then
        log WARN "rules_are_synced: ${LAST_ERROR}"
        return 1
    fi

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

rules_are_synced() {
    local ipv4_ok=true
    local ipv6_ok=true

    if ! ipv4_rules_are_synced; then
        ipv4_ok=false
    fi
    if ! ipv6_containment_is_synced; then
        ipv6_ok=false
    fi

    IPV4_RULES_SYNCED="${ipv4_ok}"
    IPV6_RULES_SYNCED="${ipv6_ok}"
    [ "${ipv4_ok}" = true ] && [ "${ipv6_ok}" = true ]
}

cleanup_k3s_policy_rules() {
    local keep_blackholes="$1"
    local pref
    local rule_line
    local rule_spec
    local rule_source

    if [ "${keep_blackholes}" = "false" ]; then
        cleanup_k3s_non_target_mangle_rules
    fi

    for pref in "${K3S_BYPASS_RULE_PREF}" "${K3S_TUNNEL_RULE_PREF}" "${K3S_BLACKHOLE_RULE_PREF}"; do
        while IFS= read -r rule_line; do
            [ -n "${rule_line}" ] || continue
            rule_spec="${rule_line#*:}"
            rule_spec="${rule_spec#"${rule_spec%%[![:space:]]*}"}"

            if [ "${pref}" = "${K3S_BYPASS_RULE_PREF}" ]; then
                [[ "${rule_spec}" == *" from "* || "${rule_spec}" == from\ * ]] || continue
                if [[ "${rule_spec}" != *" lookup main"* ]] && [[ "${rule_spec}" != *" table main"* ]]; then
                    continue
                fi
                rule_source="$(awk '{for (i = 1; i <= NF; i++) if ($i == "from") {print $(i + 1); exit}}' <<< "${rule_spec}")"
                if [ "${keep_blackholes}" != "false" ] && \
                   { [[ "${rule_spec}" == *" fwmark "* ]] || { [[ "${rule_source}" == */* ]] && [[ "${rule_source}" != */32 ]]; }; }; then
                    continue
                fi
            elif [ "${pref}" = "${K3S_TUNNEL_RULE_PREF}" ]; then
                if [[ "${rule_spec}" == *"fwmark 0xca6c"* ]] || [[ "${rule_spec}" == *"fwmark 0xCA6C"* ]]; then
                    # Legacy rules predate protocol ownership markers. Preserve
                    # them during generic cleanup rather than claiming them by
                    # structure alone.
                    continue
                elif [[ "${rule_spec}" == *" from "* || "${rule_spec}" == from\ * ]] && \
                     { [[ "${rule_spec}" == *" lookup 51820"* ]] || [[ "${rule_spec}" == *" table 51820"* ]]; }; then
                    :
                else
                    continue
                fi
            else
                [[ "${rule_spec}" == *"blackhole"* ]] || continue
                [[ "${rule_spec}" == *" from "* || "${rule_spec}" == from\ * ]] || continue
                if [ "${keep_blackholes}" != "false" ]; then
                    continue
                fi
            fi

            k3s_policy_rule_is_owned "${rule_line}" || continue

            delete_policy_rule_line "${rule_line}"
        done < <(ip rule show pref "${pref}" 2>/dev/null || true)
    done

    while IFS= read -r rule_line; do
        [ -n "${rule_line}" ] || continue
        rule_spec="${rule_line#*:}"
        rule_spec="${rule_spec#"${rule_spec%%[![:space:]]*}"}"
        [[ "${rule_spec}" == *" from "* || "${rule_spec}" == from\ * ]] || continue
        if [[ "${rule_spec}" != *" lookup main"* ]] && [[ "${rule_spec}" != *" table main"* ]]; then
            continue
        fi
        rule_source="$(awk '{for (i = 1; i <= NF; i++) if ($i == "from") {print $(i + 1); exit}}' <<< "${rule_spec}")"
        if [ "${keep_blackholes}" != "false" ] && \
           { [[ "${rule_spec}" == *" fwmark "* ]] || { [[ "${rule_source}" == */* ]] && [[ "${rule_source}" != */128 ]]; }; }; then
            continue
        fi
        k3s_policy_rule_is_owned "${rule_line}" || continue
        delete_ipv6_policy_rule_line "${rule_line}"
    done < <(ip -6 rule show pref "${K3S_BYPASS_RULE_PREF}" 2>/dev/null || true)

    while IFS= read -r rule_line; do
        [ -n "${rule_line}" ] || continue
        rule_spec="${rule_line#*:}"
        rule_spec="${rule_spec#"${rule_spec%%[![:space:]]*}"}"
        [[ "${rule_spec}" == *"blackhole"* ]] || continue
        [[ "${rule_spec}" == *" from "* || "${rule_spec}" == from\ * ]] || continue
        if [ "${keep_blackholes}" != "false" ]; then
            continue
        fi
        k3s_policy_rule_is_owned "${rule_line}" || continue
        delete_ipv6_policy_rule_line "${rule_line}"
    done < <(ip -6 rule show pref "${K3S_BLACKHOLE_RULE_PREF}" 2>/dev/null || true)
}

cleanup_ipv6_containment() {
    local rules rule managed_source
    local -a parts

    rules=$(ip6tables -S FORWARD 2>/dev/null | grep -E -- "--comment (${IPV6_RULE_MARKER}|${K3S_IPV6_QUARANTINE_MARKER})" || true)
    while IFS= read -r rule; do
        [ -n "${rule}" ] || continue
        read -r -a parts <<< "${rule}"
        parts[0]="-D"
        ip6tables "${parts[@]}" >/dev/null 2>&1 || true
    done <<< "${rules}"

    if load_managed_ipv6_policy_state; then
        local -a remaining_ipv6_addresses=()
        for managed_source in "${MANAGED_TARGET_IPV6_ADDRESSES[@]}"; do
            ip -6 rule del from "${managed_source}" table "${IPV6_POLICY_TABLE}" pref "${IPV6_TUNNEL_RULE_PREF}" >/dev/null 2>&1 || true
            ip -6 rule del from "${managed_source}" blackhole pref "${IPV6_BLACKHOLE_RULE_PREF}" >/dev/null 2>&1 || true
            if ip -6 rule show pref "${IPV6_TUNNEL_RULE_PREF}" 2>/dev/null | grep -Fq -- "from ${managed_source}" || \
               ip -6 rule show pref "${IPV6_BLACKHOLE_RULE_PREF}" 2>/dev/null | grep -Fq -- "from ${managed_source}"; then
                remaining_ipv6_addresses+=("${managed_source}")
            fi
        done
        MANAGED_TARGET_IPV6_ADDRESSES=("${remaining_ipv6_addresses[@]}")
        if [ "${IPV6_POLICY_TABLE_MANAGED}" = "true" ]; then
            ip -6 route del blackhole default metric "${IPV6_BLACKHOLE_METRIC}" table "${IPV6_POLICY_TABLE}" >/dev/null 2>&1 || true
            if ! ip -6 route show table "${IPV6_POLICY_TABLE}" 2>/dev/null | grep -Fq -- "blackhole default"; then
                IPV6_POLICY_TABLE_MANAGED="false"
            fi
        fi
        persist_managed_ipv6_policy_state || log WARN "${LAST_ERROR}"
    else
        log WARN "${LAST_ERROR}"
    fi
}

cleanup_dataplane() {
    local keep_tunnel=false
    local defer_wg_down="${DEFER_WG_DOWN:-false}"
    if [[ "${1:-}" == "--keep-tunnel" ]]; then
        keep_tunnel=true
    fi
    log INFO "Cleaning dataplane rules (keep_tunnel=${keep_tunnel})"
    remove_tagged_iptables_rules nat PREROUTING "tunnelsats-dnat"
    remove_tagged_iptables_rules nat POSTROUTING "tunnelsats-masq"
    remove_tagged_iptables_rules filter FORWARD "tunnelsats-forward-in"
    remove_tagged_iptables_rules filter FORWARD "tunnelsats-forward-out"
    if [ "${keep_tunnel}" = false ]; then
        remove_tagged_iptables_rules filter FORWARD "tunnelsats-k3s-egress-guard"
    fi
    remove_tagged_iptables_rules mangle PREROUTING "tunnelsats-conn-restore"
    remove_tagged_iptables_rules mangle FORWARD "tunnelsats-wg-mark"
    remove_tagged_iptables_rules mangle FORWARD "tunnelsats-conn-save"

    local max_attempts=10
    local attempt=0

    if [[ "${K3S_MODE}" == "true" ]]; then
        cleanup_k3s_policy_rules "${keep_tunnel}"
        cleanup_k3s_dns_protection "${keep_tunnel}"
        if [ "${keep_tunnel}" = false ]; then
            remove_k3s_subnet_quarantine || true
        fi
    else
        # Clean up all potential node target IP routing rules in Secure Mode
        # unconditionally to prevent stale rules across mode toggles.
        for cleanup_ip in "${DOCKER_TARGET_IP:-}" "10.21.21.9" "10.21.21.96"; do
            [ -n "${cleanup_ip}" ] || continue
            local cleanup_subnet
            cleanup_subnet=$(get_target_subnet "${cleanup_ip}")
            ip rule del from "${cleanup_ip}" to "${cleanup_subnet}" table main pref 32500 >/dev/null 2>&1 || true
            ip rule del from "${cleanup_ip}" table 51820 pref 32764 >/dev/null 2>&1 || true
            if [ "${keep_tunnel}" = false ]; then
                ip rule del from "${cleanup_ip}" blackhole pref "${KILL_SWITCH_PREF}" >/dev/null 2>&1 || true
                ip rule del from "${cleanup_ip}" blackhole pref 32765 >/dev/null 2>&1 || true
            fi
        done

        if [ "${keep_tunnel}" = false ] && [[ "${SECURE_MODE}" != "true" ]]; then
            load_managed_docker_policy_state || log WARN "${LAST_ERROR}"
        fi
        local managed_endpoint
        local managed_source
        local managed_subnet
        local -a remaining_docker_endpoints=()
        for managed_endpoint in "${MANAGED_DOCKER_IPV4_ENDPOINTS[@]}"; do
            IFS='|' read -r managed_source managed_subnet _ <<< "${managed_endpoint}"
            ip rule del from "${managed_source}" to "${managed_subnet}" table main pref 32500 >/dev/null 2>&1 || true
            ip rule del from "${managed_source}" table 51820 pref 32764 >/dev/null 2>&1 || true
            if [ "${keep_tunnel}" = false ]; then
                if ! remove_source_blackhole_rule "${managed_source}" "${KILL_SWITCH_PREF}" || \
                   ! remove_source_blackhole_rule "${managed_source}" "32765" || \
                   ip rule show pref 32500 2>/dev/null | grep -Fq -- "from ${managed_source}" || \
                   ip rule show pref 32764 2>/dev/null | grep -Fq -- "from ${managed_source}"; then
                    remaining_docker_endpoints+=("${managed_endpoint}")
                fi
            fi
        done
        if [ "${keep_tunnel}" = false ] && [ "${DOCKER_POLICY_STATE_LOADED}" = "true" ]; then
            MANAGED_DOCKER_IPV4_ENDPOINTS=("${remaining_docker_endpoints[@]}")
            persist_managed_docker_policy_state || log WARN "${LAST_ERROR}"
        fi

        if [[ "${SECURE_MODE}" != "true" ]]; then
            # Remove local bypass rule (pref 32500)
            ip rule del from "${DOCKER_NETWORK_SUBNET}" to "${DOCKER_NETWORK_SUBNET}" table main pref 32500 >/dev/null 2>&1 || true

            # Remove bridge gateway tunnel rule (pref 32763)
            local bridge_gw
            bridge_gw="${DOCKER_NETWORK_SUBNET%.*}.1"
            ip rule del from "${bridge_gw}" table 51820 pref 32763 >/dev/null 2>&1 || true

            if [ "${keep_tunnel}" = false ]; then
                ip rule del from "${DOCKER_NETWORK_SUBNET}" blackhole pref "${KILL_SWITCH_PREF}" >/dev/null 2>&1 || true
                ip rule del from "${DOCKER_NETWORK_SUBNET}" blackhole pref 32765 >/dev/null 2>&1 || true
            fi

            while ip rule show | grep -qE "^[0-9]+:[[:space:]]+from[[:space:]]+${DOCKER_NETWORK_SUBNET//./\\.}[[:space:]]+lookup[[:space:]]+51820[[:space:]]*$" && [ ${attempt} -lt ${max_attempts} ]; do
                ip rule del from "${DOCKER_NETWORK_SUBNET}" table 51820 >/dev/null 2>&1 || break
                attempt=$((attempt + 1))
            done
        fi
    fi

    if [ "${keep_tunnel}" = false ]; then
        cleanup_ipv6_containment
        if [ "${defer_wg_down}" = false ]; then
            ip route flush table 51820 >/dev/null 2>&1 || true

            if wg show "${WG_IFACE}" >/dev/null 2>&1; then
                wg-quick down "${WG_IFACE}" >/dev/null 2>&1 || true
            fi
        fi
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

write_decommission_result() {
    local request_id="$1"
    local decommissioned="$2"
    local result_path
    local tmp_path
    local state_json="{}"

    if ! is_valid_request_id "${request_id}"; then
        log WARN "Skipping decommission result write for invalid request_id: ${request_id}"
        return 0
    fi

    ensure_decommission_dirs
    result_path="$(decommission_result_path "${request_id}")"
    tmp_path="$(mktemp "${DECOMMISSION_RESULT_DIR}/.${request_id}.tmp.XXXXXX")"

    if [ -f "${STATE_FILE}" ]; then
        state_json="$(cat "${STATE_FILE}" 2>/dev/null || echo "{}")"
        if ! echo "${state_json}" | jq -e . >/dev/null 2>&1; then
            state_json="{}"
        fi
    fi

    if ! jq -n \
        --arg request_id "${request_id}" \
        --argjson decommissioned "${decommissioned}" \
        --argjson state "${state_json}" \
        '{request_id:$request_id, decommissioned:$decommissioned, state:$state}' > "${tmp_path}"; then
        rm -f "${tmp_path}"
        return 1
    fi

    mv -f "${tmp_path}" "${result_path}"
}

fail_reconcile_closed() {
    local request_id="${1:-}"
    local failure_error="${LAST_ERROR:-Reconciliation failed}"
    local pre_cleanup_guard_error=""

    # k3s owns a separate layered isolation and recovery lifecycle. Preserve
    # that state instead of applying the Docker/Secure Mode cleanup below.
    if [[ "${K3S_MODE}" == "true" ]]; then
        if [ "${K3S_UNSEEN_REPLACEMENT_PENDING:-false}" = "true" ] || \
           ! k3s_candidate_pod_guards_are_synced >/dev/null 2>&1; then
            ensure_k3s_pre_discovery_containment "${DOCKER_TARGET_IP:-${MANAGED_K3S_TARGET_IP:-}}" || true
            LAST_ERROR="${failure_error}"
        fi
        RULES_SYNCED="false"
        DNS_PROTECTED="false"
        write_state
        if [ -n "${request_id}" ]; then
            write_reconcile_result "${request_id}" false
        fi
        return 1
    fi

    # Try to isolate first, but always clear partially staged NAT/FORWARD and
    # source-routing state even when this attempt fails. The post-cleanup retry
    # then verifies that the dataplane is left fail-closed.
    if ! ensure_reconcile_kill_switch; then
        pre_cleanup_guard_error="${LAST_ERROR}"
    fi

    cleanup_dataplane "--keep-tunnel"

    if ! ensure_reconcile_kill_switch; then
        LAST_ERROR="${failure_error}; additionally failed to restore kill switch: ${LAST_ERROR}"
    elif [ -n "${pre_cleanup_guard_error}" ]; then
        LAST_ERROR="${failure_error}; pre-cleanup kill switch warning: ${pre_cleanup_guard_error}"
    else
        LAST_ERROR="${failure_error}"
    fi

    RULES_SYNCED="false"
    DNS_PROTECTED="false"
    write_state
    if [ -n "${request_id}" ]; then
        write_reconcile_result "${request_id}" false
    fi
    return 1
}

reconcile_once() {
    local reason="$1"
    local request_id="${2:-}"
    local changed=0
    local policy_changed="0"
    local nat_changed="0"
    local k3s_guard_active=false

    if ! load_decommissioned_state; then
        RULES_SYNCED="false"
        IPV4_RULES_SYNCED="false"
        IPV6_RULES_SYNCED="false"
        DNS_PROTECTED="false"
        write_state
        if [ -n "${request_id}" ]; then
            write_reconcile_result "${request_id}" false
        fi
        log ERROR "${LAST_ERROR:-Failed to load decommission state}"
        return 1
    fi
    if [[ "${K3S_MODE}" == "true" ]]; then
        load_managed_k3s_policy_state || true
    fi

    if [ "${DECOMMISSIONED:-false}" = "true" ]; then
        if [ "${reason}" != "manual" ] && [ "${reason}" != "restart" ]; then
            write_state
            log INFO "reconcile_skip reason=${reason} decommissioned=true"
            LAST_RECONCILE_EPOCH="$(date +%s)"
            return 0
        fi
        DECOMMISSIONED="false"
        if ! persist_decommissioned_state; then
            DECOMMISSIONED="true"
            RULES_SYNCED="false"
            IPV4_RULES_SYNCED="false"
            IPV6_RULES_SYNCED="false"
            DNS_PROTECTED="false"
            write_state
            if [ -n "${request_id}" ]; then
                write_reconcile_result "${request_id}" false
            fi
            log ERROR "${LAST_ERROR:-Failed to persist decommission state}"
            return 1
        fi
        if [[ "${K3S_MODE}" == "true" ]]; then
            K3S_POLICY_STATE_LOADED="true"
            persist_managed_k3s_policy_state || log WARN "${LAST_ERROR}"
        elif [[ "${SECURE_MODE}" != "true" ]]; then
            DOCKER_TARGET_IP="${DOCKER_TARGET_IP:-10.9.9.9}"
        fi
    fi

    if [[ "${K3S_MODE}" == "true" ]] && [ "${K3S_ENABLE_BACKGROUND_WATCHER:-false}" = "true" ]; then
        ensure_k3s_pod_watcher_running
    fi

    LAST_ERROR=""
    RULES_SYNCED="false"
    IPV4_RULES_SYNCED="false"
    IPV6_RULES_SYNCED="false"
    DNS_PROTECTED="false"

    log INFO "reconcile_start reason=${reason}"

    if ! ensure_reconcile_kill_switch false; then
        LAST_ERROR="${LAST_ERROR:-Failed to install reconciliation kill switch}"
        write_state
        if [ -n "${request_id}" ]; then
            write_reconcile_result "${request_id}" false
        fi
        return 1
    fi

    if [[ "${K3S_MODE}" != "true" ]] && [[ "${SECURE_MODE}" != "true" ]] && [ ! -S "${DOCKER_SOCK}" ]; then
        LAST_ERROR="Docker socket unavailable"
        fail_reconcile_closed "${request_id}"
        return 1
    fi

    local previous_k3s_target_ip="${DOCKER_TARGET_IP:-${MANAGED_K3S_TARGET_IP:-}}"
    if [ "${previous_k3s_target_ip}" = "10.9.9.9" ]; then
        previous_k3s_target_ip=""
    fi

    if ! detect_lightning_container; then
        LAST_ERROR="${LAST_ERROR:-No running LND/CLN container detected}"
        if [[ "${K3S_MODE}" == "true" ]]; then
            local discovery_error="${LAST_ERROR}"
            ensure_k3s_pre_discovery_containment "${previous_k3s_target_ip}" || true
            cleanup_dataplane "--keep-tunnel"
            LAST_ERROR="${discovery_error}"
            write_state
            if [ -n "${request_id}" ]; then
                write_reconcile_result "${request_id}" false
            fi
        else
            fail_reconcile_closed "${request_id}"
        fi
        return 1
    fi

    if [[ "${K3S_MODE}" == "true" ]] && \
       [ -n "${MANAGED_K3S_POD_NAMESPACE:-}" ] && [ -n "${MANAGED_K3S_POD_SELECTOR:-}" ] && \
       [ -n "${K3S_TARGET_POD_NAMESPACE:-}" ] && [ -n "${K3S_TARGET_POD_SELECTOR:-}" ] && \
       { [ "${MANAGED_K3S_POD_NAMESPACE}" != "${K3S_TARGET_POD_NAMESPACE}" ] || \
         [ "${MANAGED_K3S_POD_SELECTOR}" != "${K3S_TARGET_POD_SELECTOR}" ]; }; then
        if ! remove_k3s_emergency_network_policy_for_target "${MANAGED_K3S_POD_NAMESPACE}" "${MANAGED_K3S_POD_SELECTOR}"; then
            write_state
            if [ -n "${request_id}" ]; then
                write_reconcile_result "${request_id}" false
            fi
            return 1
        fi
        MANAGED_K3S_TARGET_IP="${DOCKER_TARGET_IP:-}"
        MANAGED_K3S_TARGET_IMPL="${TARGET_IMPL:-}"
        MANAGED_K3S_POD_NAME="${K3S_TARGET_POD_NAME:-}"
        MANAGED_K3S_POD_NAMESPACE="${K3S_TARGET_POD_NAMESPACE}"
        MANAGED_K3S_POD_SELECTOR="${K3S_TARGET_POD_SELECTOR}"
        K3S_POLICY_STATE_LOADED="true"
        persist_managed_k3s_policy_state || log WARN "${LAST_ERROR}"
    fi

    # Discovery exposes the target's pre-existing Docker network sources.
    # Quarantine them before changing attachments or bringing up the tunnel.
    if [[ "${K3S_MODE}" != "true" ]] && [[ "${SECURE_MODE}" != "true" ]]; then
        if ! ensure_reconcile_kill_switch; then
            fail_reconcile_closed "${request_id}"
            return 1
        fi
    fi

    # IPv6 is an explicit deny-only family. Establish containment immediately
    # after target discovery and before the IPv4 dataplane can be considered
    # healthy.
    if ! ensure_ipv6_containment; then
        fail_reconcile_closed "${request_id}"
        return 1
    fi
    if ! ipv6_containment_is_synced; then
        LAST_ERROR="IPv6 containment is not fully synced"
        fail_reconcile_closed "${request_id}"
        return 1
    fi
    IPV6_RULES_SYNCED="true"

    if [[ "${K3S_MODE}" == "true" ]]; then
        # Keep an independent packet-filter guard in place until the complete
        # routing policy is verified. If blackhole setup fails, this guard
        # continues to block clear-text pod egress.
        if ensure_k3s_egress_guard "${DOCKER_TARGET_IP}"; then
            k3s_guard_active=true
        else
            log WARN "k3s: Failed to install temporary iptables egress guard; requiring source blackhole"
        fi

        # Target discovery can select a new pod IP before WireGuard is
        # available. Blackhole that source immediately so any later startup
        # failure cannot fall through to the node's ordinary egress route.
        if ! ensure_fallback_blackhole_rule \
            "${DOCKER_TARGET_IP}" \
            "k3s: Failed to protect selected pod before WireGuard startup" \
            "${K3S_RULE_PROTOCOL}"; then
            if [ "${k3s_guard_active}" = false ]; then
                local isolation_error="${LAST_ERROR}"
                if ensure_k3s_subnet_quarantine "${DOCKER_TARGET_IP}"; then
                    LAST_ERROR="${isolation_error}; quarantined pod CIDR ${K3S_QUARANTINE_CIDR}"
                elif ensure_k3s_emergency_network_policy; then
                    if delete_k3s_target_pod; then
                        LAST_ERROR="${isolation_error}; temporary egress guard unavailable and pod-CIDR quarantine failed; emergency NetworkPolicy retained and deleted target pod ${K3S_TARGET_POD_NAMESPACE}/${K3S_TARGET_POD_NAME} to fail closed"
                    else
                        LAST_ERROR="${isolation_error}; temporary egress guard unavailable and pod-CIDR quarantine failed; emergency NetworkPolicy retained because target pod deletion failed"
                    fi
                elif delete_k3s_target_pod; then
                    LAST_ERROR="${isolation_error}; temporary egress guard unavailable, pod-CIDR quarantine and emergency NetworkPolicy failed; deleted current target pod ${K3S_TARGET_POD_NAMESPACE}/${K3S_TARGET_POD_NAME}"
                else
                    wait_for_k3s_emergency_isolation "${isolation_error}; temporary egress guard unavailable, pod-CIDR quarantine failed, emergency NetworkPolicy failed, and target pod deletion failed"
                fi
            fi
            write_state
            if [ -n "${request_id}" ]; then
                write_reconcile_result "${request_id}" false
            fi
            return 1
        fi
        if [ "${BLACKHOLE_CHANGED}" = "1" ]; then
            changed=1
        fi
        retire_stale_k3s_unmatched_ip_guards || true
    fi

    if ! ensure_docker_network; then
        fail_reconcile_closed "${request_id}"
        return 1
    fi

    if ! ensure_container_attached; then
        fail_reconcile_closed "${request_id}"
        return 1
    fi

    # Attaching the protected endpoint changes the target's address set. Keep
    # every source quarantined until both host policy and container routes pass.
    if [[ "${K3S_MODE}" != "true" ]] && [[ "${SECURE_MODE}" != "true" ]]; then
        if ! ensure_reconcile_kill_switch; then
            fail_reconcile_closed "${request_id}"
            return 1
        fi
    fi

    if ! resolve_bridge_name; then
        LAST_ERROR="Failed to resolve docker bridge interface"
        fail_reconcile_closed "${request_id}"
        return 1
    fi

    if ! ensure_wg_up; then
        fail_reconcile_closed "${request_id}"
        return 1
    fi

    if [[ "${K3S_MODE}" != "true" ]] && ! wireguard_handshake_is_fresh; then
        fail_reconcile_closed "${request_id}"
        return 1
    fi

    if ! ensure_policy_routing; then
        fail_reconcile_closed "${request_id}"
        return 1
    fi
    policy_changed="${POLICY_CHANGED}"

    if [ "${policy_changed}" = "1" ]; then
        changed=1
    fi

    if ! ensure_nat_forward_rules; then
        fail_reconcile_closed "${request_id}"
        return 1
    fi
    nat_changed="${NAT_CHANGED}"

    if [ "${nat_changed}" = "1" ]; then
        changed=1
    fi

    if [[ "${K3S_MODE}" == "true" ]]; then
        K3S_ALLOW_ACTIVE_QUARANTINE_DURING_SYNC="true"
        if ! rules_are_synced; then
            K3S_ALLOW_ACTIVE_QUARANTINE_DURING_SYNC="false"
            LAST_ERROR="Dataplane rules are not fully synced"
            fail_reconcile_closed "${request_id}"
            return 1
        fi
        K3S_ALLOW_ACTIVE_QUARANTINE_DURING_SYNC="false"
        if ! release_k3s_reconcile_guards; then
            RULES_SYNCED="false"
            fail_reconcile_closed "${request_id}"
            return 1
        fi
        if ! verify_k3s_target_routes; then
            RULES_SYNCED="false"
            IPV4_RULES_SYNCED="false"
            fail_reconcile_closed "${request_id}"
            return 1
        fi
        RULES_SYNCED="true"
    elif ! rules_are_synced; then
        LAST_ERROR="Dataplane rules are not fully synced"
        fail_reconcile_closed "${request_id}"
        return 1
    else
        # The emergency guard remains active while the complete dataplane and
        # handshake are verified. Only then can target traffic use table 51820.
        if ! wireguard_handshake_is_fresh; then
            fail_reconcile_closed "${request_id}"
            return 1
        fi
        if ! remove_reconcile_kill_switch; then
            fail_reconcile_closed "${request_id}"
            return 1
        fi
        # Defense in depth against route-table drift during activation.
        if ! rules_are_synced; then
            LAST_ERROR="Dataplane rules failed verification after kill-switch release"
            fail_reconcile_closed "${request_id}"
            return 1
        fi
        RULES_SYNCED="true"
    fi

    record_protected_target
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

ensure_k3s_selector_context() {
    if [ -n "${K3S_TARGET_POD_NAMESPACE:-}" ] && [ -n "${K3S_TARGET_POD_SELECTOR:-}" ]; then
        return 0
    fi
    if resolve_configured_k3s_target_metadata; then
        return 0
    fi
    if [ -n "${MANAGED_K3S_POD_NAMESPACE:-}" ] && [ -n "${MANAGED_K3S_POD_SELECTOR:-}" ]; then
        K3S_TARGET_POD_NAMESPACE="${MANAGED_K3S_POD_NAMESPACE}"
        K3S_TARGET_POD_SELECTOR="${MANAGED_K3S_POD_SELECTOR}"
        return 0
    fi
    K3S_TARGET_POD_NAMESPACE="${LND_K8S_NAMESPACE:-${K8S_NAMESPACE:-default}}"
    K3S_TARGET_POD_SELECTOR="${LND_K8S_POD_SELECTOR:-app=lnd}"
    [ -n "${K3S_TARGET_POD_NAMESPACE:-}" ] && [ -n "${K3S_TARGET_POD_SELECTOR:-}" ]
}

ensure_k3s_pre_discovery_containment() {
    local previous_target_ip="${1:-}"
    local discovery_error="${LAST_ERROR:-No running LND/CLN container detected}"
    local policy_ok=false
    local quarantine_ok=false
    local quarantine_hint_ip="${previous_target_ip}"

    if [ -z "${quarantine_hint_ip}" ] && [ -n "${K3S_CANDIDATE_POD_IPS:-}" ]; then
        quarantine_hint_ip="${K3S_CANDIDATE_POD_IPS%%,*}"
    fi

    ensure_k3s_selector_context || true
    if [ -n "${K3S_TARGET_POD_NAMESPACE:-}" ] && [ -n "${K3S_TARGET_POD_SELECTOR:-}" ]; then
        MANAGED_K3S_POD_NAMESPACE="${K3S_TARGET_POD_NAMESPACE}"
        MANAGED_K3S_POD_SELECTOR="${K3S_TARGET_POD_SELECTOR}"
        K3S_POLICY_STATE_LOADED="true"
        persist_managed_k3s_policy_state || log WARN "${LAST_ERROR}"
        if ensure_k3s_emergency_network_policy; then
            policy_ok=true
        fi
    fi

    # Default k3s Flannel does not enforce Kubernetes NetworkPolicy. Always
    # pair selector-based NetworkPolicy with kernel pod-CIDR quarantine unless
    # the operator explicitly declares an enforcing CNI and the Pod API query
    # succeeded so all existing matching local pods are already per-IP guarded.
    if [ "${K3S_CNI_ENFORCES_NETWORK_POLICY:-false}" != "true" ] || \
       [ "${policy_ok}" = false ] || \
       [ "${K3S_POD_API_QUERY_OK:-false}" != "true" ] || \
       [ "${K3S_CANDIDATE_PODS_GUARDED:-true}" != "true" ] || \
       [ "${K3S_UNSEEN_REPLACEMENT_PENDING:-false}" = "true" ]; then
        if ensure_k3s_subnet_quarantine "${quarantine_hint_ip}"; then
            quarantine_ok=true
        elif [ -n "${quarantine_hint_ip}" ] && ensure_k3s_subnet_quarantine ""; then
            quarantine_ok=true
        fi
    else
        remove_k3s_subnet_quarantine || true
    fi

    local replacement_protected=false
    if [ "${quarantine_ok}" = true ]; then
        replacement_protected=true
    elif [ "${policy_ok}" = true ] && [ "${K3S_CNI_ENFORCES_NETWORK_POLICY:-false}" = "true" ] && \
         [ "${K3S_CANDIDATE_PODS_GUARDED:-true}" = "true" ]; then
        replacement_protected=true
    fi

    if [ "${replacement_protected}" = true ] && \
       [ "${K3S_CANDIDATE_PODS_GUARDED:-true}" = "true" ] && \
       [ "${K3S_UNSEEN_REPLACEMENT_PENDING:-false}" != "true" ]; then
        retire_stale_k3s_unmatched_ip_guards || true
    fi

    LAST_ERROR="${discovery_error}"
    [ "${replacement_protected}" = true ] || [ "${policy_ok}" = true ]
}

k8s_watch_api() {
    local path="$1"
    local max_time="${2:-30}"
    local token
    token=$(cat "${K8S_SA_TOKEN_PATH}" 2>/dev/null) || return 1
    curl -sfN --connect-timeout 5 --max-time "${max_time}" --cacert "${K8S_SA_CA_PATH}" \
        -H "Authorization: Bearer ${token}" \
        "${K8S_API_URL}${path}" 2>/dev/null
}

refresh_k3s_watcher_runtime_state() {
    [ "${K3S_IN_BACKGROUND_WATCHER:-false}" = "true" ] || return 0
    DECOMMISSION_STATE_LOADED="false"
    load_decommissioned_state >/dev/null 2>&1 || true
    if [ -e "${K3S_POLICY_STATE_FILE}" ]; then
        K3S_POLICY_STATE_LOADED="false"
        DOCKER_TARGET_IP=""
        K3S_TARGET_POD_NAME=""
        K3S_TARGET_POD_NAMESPACE=""
        K3S_TARGET_POD_SELECTOR=""
        TARGET_IPV6_ADDRESSES=()
        load_managed_k3s_policy_state >/dev/null 2>&1 || true
    elif [ -f "${STATE_FILE}" ]; then
        DOCKER_TARGET_IP=""
        K3S_TARGET_POD_NAME=""
    fi
    if [ -f "${STATE_FILE}" ]; then
        local state_synced state_target_ip
        state_synced="$(jq -r 'if .rules_synced == true then "true" else "false" end' "${STATE_FILE}" 2>/dev/null || printf 'false')"
        state_target_ip="$(jq -r '.target_ip // empty' "${STATE_FILE}" 2>/dev/null || true)"
        if [ -z "${DOCKER_TARGET_IP:-}" ] && [ -n "${state_target_ip}" ] && [ "${state_target_ip}" != "10.9.9.9" ]; then
            DOCKER_TARGET_IP="${state_target_ip}"
        fi
        if [ "${state_synced}" = "true" ] && \
           { [ -z "${DOCKER_TARGET_IP:-}" ] || [ -z "${state_target_ip}" ] || [ "${state_target_ip}" = "${DOCKER_TARGET_IP}" ]; }; then
            RULES_SYNCED="true"
        else
            RULES_SYNCED="false"
        fi
    elif [ -e "${K3S_POLICY_STATE_FILE}" ]; then
        if [ -n "${DOCKER_TARGET_IP:-}" ] && \
           ! iptables -S FORWARD 2>/dev/null | grep -Eq -- "-s ${DOCKER_TARGET_IP//./\\.}(/32)? .*tunnelsats-k3s-egress-guard" && \
           ip rule show pref "${K3S_TUNNEL_RULE_PREF}" 2>/dev/null | grep -qE \
            "^${K3S_TUNNEL_RULE_PREF}:[[:space:]]+from[[:space:]]+${DOCKER_TARGET_IP//./\\.}[[:space:]]+(lookup|table)[[:space:]]+51820([[:space:]].*)?[[:space:]]+proto(col)?[[:space:]]+${K3S_RULE_PROTOCOL}[[:space:]]*$" && \
           ip route show table 51820 2>/dev/null | grep -Fq "default dev ${WG_IFACE}"; then
            RULES_SYNCED="true"
        else
            RULES_SYNCED="false"
        fi
    fi
    return 0
}

watch_k3s_target_pods_once() {
    local namespace="${1:-}"
    local selector="${2:-}"
    local timeout_seconds="${K3S_POD_WATCH_TIMEOUT_SECONDS:-1}"
    local encoded_selector watch_scope watch_path event_line event_type event_rv pod_list is_full_list
    local watch_fd="" watch_pid="" watch_rc=0 watch_failed=false

    refresh_k3s_watcher_runtime_state
    load_k3s_pod_watch_state || true

    if [ -z "${namespace}" ]; then
        namespace="${K3S_TARGET_POD_NAMESPACE:-}"
    fi
    if [ -z "${selector}" ]; then
        selector="${K3S_TARGET_POD_SELECTOR:-}"
    fi
    if [ -z "${namespace}" ] || [ -z "${selector}" ]; then
        ensure_k3s_selector_context || return 1
        namespace="${K3S_TARGET_POD_NAMESPACE:-}"
        selector="${K3S_TARGET_POD_SELECTOR:-}"
    fi
    [ -n "${namespace}" ] && [ -n "${selector}" ] || return 1

    watch_scope="${namespace}|${selector}"
    if [ "${K3S_WATCH_SCOPE:-}" != "${watch_scope}" ]; then
        K3S_WATCH_SCOPE="${watch_scope}"
        K3S_WATCH_RESOURCE_VERSION=""
        persist_k3s_pod_watch_state || return 1
    fi

    if [ -n "${TUNNELSATS_K8S_NODE_NAME:-}" ]; then
        watch_path="/api/v1/namespaces/${namespace}/pods?fieldSelector=$(urlencode "spec.nodeName=${TUNNELSATS_K8S_NODE_NAME}")&watch=true&timeoutSeconds=${timeout_seconds}"
    else
        encoded_selector="$(urlencode "${selector}")"
        watch_path="/api/v1/namespaces/${namespace}/pods?labelSelector=${encoded_selector}&watch=true&timeoutSeconds=${timeout_seconds}"
    fi
    if [ -n "${K3S_WATCH_RESOURCE_VERSION:-}" ]; then
        watch_path="${watch_path}&resourceVersion=$(urlencode "${K3S_WATCH_RESOURCE_VERSION}")"
    fi

    exec {watch_fd}< <(k8s_watch_api "${watch_path}" "$((timeout_seconds + 2))")
    watch_pid=$!

    while IFS= read -r event_line <&"${watch_fd}"; do
        [ -n "${event_line}" ] || continue
        event_type="$(printf '%s' "${event_line}" | jq -r '.type // empty' 2>/dev/null || true)"
        if [ "${event_type}" = "ERROR" ]; then
            watch_failed=true
            K3S_WATCH_RESOURCE_VERSION=""
            K3S_POD_API_QUERY_OK="false"
            K3S_UNSEEN_REPLACEMENT_PENDING="true"
            persist_k3s_pod_watch_state || true
            continue
        fi
        event_rv="$(printf '%s' "${event_line}" | jq -r '.object.metadata.resourceVersion // .metadata.resourceVersion // ([.items[]?.metadata.resourceVersion // empty] | last) // empty' 2>/dev/null || true)"
        if [ -n "${event_rv}" ]; then
            K3S_WATCH_RESOURCE_VERSION="${event_rv}"
        fi
        if [ "${event_type}" = "BOOKMARK" ]; then
            if ! persist_k3s_pod_watch_state; then
                watch_failed=true
            fi
            continue
        fi

        is_full_list="false"
        if printf '%s' "${event_line}" | jq -e '(.items | type) == "array"' >/dev/null 2>&1; then
            is_full_list="true"
        fi
        pod_list="$(printf '%s' "${event_line}" | jq -ce '
            if (.object | type) == "object" then
                {items: [(.object + {"_watchEventType": (.type // "")})]}
            elif (.items | type) == "array" then
                {items: .items}
            else
                empty
            end
        ' 2>/dev/null || true)"
        if [ -z "${pod_list}" ]; then
            watch_failed=true
            continue
        fi
        refresh_k3s_watcher_runtime_state
        local watch_guard_ok=true
        if [ -n "${TUNNELSATS_K8S_NODE_NAME:-}" ]; then
            local K3S_WATCH_EVENT_GUARD="true"
            if ! pre_guard_colocated_k3s_candidate_pods "${pod_list}" "${is_full_list}"; then
                watch_guard_ok=false
                watch_failed=true
                K3S_CANDIDATE_PODS_GUARDED="false"
                ensure_k3s_pre_discovery_containment "${DOCKER_TARGET_IP:-${MANAGED_K3S_TARGET_IP:-}}" || \
                    ensure_k3s_subnet_quarantine "${DOCKER_TARGET_IP:-}" || \
                    ensure_k3s_subnet_quarantine "" || true
            fi
        else
            if ! persist_k3s_pod_watch_state; then
                watch_failed=true
            fi
        fi
        if [ "${watch_guard_ok}" = true ] && \
           [ "${K3S_UNSEEN_REPLACEMENT_PENDING:-false}" != "true" ] && \
           [ "${RULES_SYNCED:-false}" = "true" ] && [ "${is_full_list}" = "false" ] && \
           [ -n "${K3S_TARGET_POD_NAME:-}" ] && [ -n "${DOCKER_TARGET_IP:-}" ] && \
           [ -n "${TUNNELSATS_K8S_NODE_NAME:-}" ] && \
           printf '%s' "${event_line}" | jq -e \
               --arg name "${K3S_TARGET_POD_NAME}" \
               --arg node "${TUNNELSATS_K8S_NODE_NAME}" \
               --arg ip "${DOCKER_TARGET_IP}" '
               (.type // "") != "DELETED"
               and (.object.metadata.name // "") == $name
               and (.object.spec.nodeName // "") == $node
               and (.object.metadata.deletionTimestamp // null) == null
               and (.object.status.phase // "") == "Running"
               and (.object.status.podIP // "") == $ip
               and any(.object.status.conditions[]?; .type == "Ready" and .status == "True")
           ' >/dev/null 2>&1; then
            continue
        fi
        ensure_reconcile_dirs
        printf 'k3s-pod-watch\n' > "${RECONCILE_TRIGGER_DIR}/k3s-pod-watch.trigger" 2>/dev/null || true
    done

    exec {watch_fd}<&-
    wait "${watch_pid}" 2>/dev/null || watch_rc=$?
    if [ "${watch_rc}" -ne 0 ] || [ "${watch_failed}" = true ]; then
        K3S_WATCH_RESOURCE_VERSION=""
        K3S_POD_API_QUERY_OK="false"
        K3S_UNSEEN_REPLACEMENT_PENDING="true"
        persist_k3s_pod_watch_state || true
        local saved_ns="${MANAGED_K3S_POD_NAMESPACE:-}"
        local saved_sel="${MANAGED_K3S_POD_SELECTOR:-}"
        ensure_k3s_pre_discovery_containment "${DOCKER_TARGET_IP:-${MANAGED_K3S_TARGET_IP:-}}" || \
            ensure_k3s_subnet_quarantine "${DOCKER_TARGET_IP:-${MANAGED_K3S_TARGET_IP:-}}" || \
            ensure_k3s_subnet_quarantine "" || true
        if [ -n "${saved_ns}" ] && [ -n "${saved_sel}" ]; then
            MANAGED_K3S_POD_NAMESPACE="${saved_ns}"
            MANAGED_K3S_POD_SELECTOR="${saved_sel}"
        fi
        ensure_reconcile_dirs
        printf 'k3s-pod-watch\n' > "${RECONCILE_TRIGGER_DIR}/k3s-pod-watch.trigger" 2>/dev/null || true
        return 1
    fi
    return 0
}

K3S_WATCHER_PID=""

k3s_pod_watch_loop() {
    trap 'exit 0' SIGTERM SIGINT
    K3S_IN_BACKGROUND_WATCHER="true"
    while true; do
        refresh_k3s_watcher_runtime_state
        if [ "${DECOMMISSIONED:-false}" = "true" ] || \
           { [ -f "${DECOMMISSION_STATE_FILE}" ] && jq -e '.decommissioned == true' "${DECOMMISSION_STATE_FILE}" >/dev/null 2>&1; }; then
            sleep 1
            continue
        fi
        if ! watch_k3s_target_pods_once; then
            sleep "${K3S_WATCH_ERROR_BACKOFF_SECONDS:-0.05}" 2>/dev/null || sleep 1
        fi
    done
}

ensure_k3s_pod_watcher_running() {
    [[ "${K3S_MODE}" == "true" ]] || return 0
    [ "${DECOMMISSIONED:-false}" != "true" ] || return 0
    if [ -n "${K3S_WATCHER_PID:-}" ] && kill -0 "${K3S_WATCHER_PID}" >/dev/null 2>&1; then
        return 0
    fi
    k3s_pod_watch_loop &
    K3S_WATCHER_PID=$!
    return 0
}

stop_k3s_pod_watcher() {
    if [ -n "${K3S_WATCHER_PID:-}" ]; then
        pkill -P "${K3S_WATCHER_PID}" >/dev/null 2>&1 || true
        kill "${K3S_WATCHER_PID}" >/dev/null 2>&1 || true
        wait "${K3S_WATCHER_PID}" 2>/dev/null || true
        K3S_WATCHER_PID=""
    fi
}

preserve_fail_closed_dataplane() {
    local mode="${1:-shutdown}"
    if [[ "${K3S_MODE}" == "true" ]]; then
        load_managed_k3s_policy_state || true
        load_managed_ipv6_policy_state || true
        ensure_k3s_selector_context || true
        local target_ip="${DOCKER_TARGET_IP:-}"
        if [ "${target_ip}" = "10.9.9.9" ]; then
            target_ip=""
        fi
        if [ -z "${target_ip}" ] && [ "${MANAGED_K3S_TARGET_IP:-}" != "10.9.9.9" ]; then
            target_ip="${MANAGED_K3S_TARGET_IP:-}"
        fi
        if [ -n "${target_ip}" ]; then
            DOCKER_TARGET_IP="${target_ip}"
        fi
        if [ -z "${K3S_TARGET_POD_NAME:-}" ] && [ -n "${MANAGED_K3S_POD_NAME:-}" ]; then
            K3S_TARGET_POD_NAME="${MANAGED_K3S_POD_NAME}"
        fi
        local guard_ok=false
        local blackhole_ok=false
        local quarantine_ok=false
        local policy_ok=false
        if [ -n "${target_ip}" ]; then
            MANAGED_K3S_TARGET_IP="${target_ip}"
            if [ "${#TARGET_IPV6_ADDRESSES[@]}" -gt 0 ]; then
                MANAGED_K3S_TARGET_IPV6_ADDRESSES=("${TARGET_IPV6_ADDRESSES[@]}")
            fi
            MANAGED_K3S_TARGET_IMPL="${TARGET_IMPL:-${MANAGED_K3S_TARGET_IMPL:-}}"
            MANAGED_K3S_POD_NAME="${K3S_TARGET_POD_NAME:-${MANAGED_K3S_POD_NAME:-}}"
            MANAGED_K3S_POD_NAMESPACE="${K3S_TARGET_POD_NAMESPACE:-${MANAGED_K3S_POD_NAMESPACE:-}}"
            MANAGED_K3S_POD_SELECTOR="${K3S_TARGET_POD_SELECTOR:-${MANAGED_K3S_POD_SELECTOR:-}}"
            K3S_POLICY_STATE_LOADED="true"
            persist_managed_k3s_policy_state || log WARN "${LAST_ERROR}"
            if ensure_k3s_egress_guard "${target_ip}"; then
                guard_ok=true
            fi
            if ensure_fallback_blackhole_rule \
                "${target_ip}" \
                "k3s: Failed to preserve fallback blackhole rule" \
                "${K3S_RULE_PROTOCOL}"; then
                blackhole_ok=true
            fi
            if [ "${guard_ok}" = false ] && [ "${blackhole_ok}" = false ]; then
                if ensure_k3s_subnet_quarantine "${target_ip}" && ensure_k3s_ipv6_subnet_quarantine; then
                    quarantine_ok=true
                fi
            fi
        else
            MANAGED_K3S_POD_NAMESPACE="${K3S_TARGET_POD_NAMESPACE:-${MANAGED_K3S_POD_NAMESPACE:-}}"
            MANAGED_K3S_POD_SELECTOR="${K3S_TARGET_POD_SELECTOR:-${MANAGED_K3S_POD_SELECTOR:-}}"
            K3S_POLICY_STATE_LOADED="true"
            persist_managed_k3s_policy_state || log WARN "${LAST_ERROR}"
        fi
        # Always install selector-based emergency NetworkPolicy before exiting or
        # restarting so any controller-created replacement Lightning pod (with a
        # new IP) is also fail-closed while TunnelSats is stopped.
        local quarantine_for_netpol=false
        if ensure_k3s_emergency_network_policy; then
            policy_ok=true
        else
            if [ "${quarantine_ok}" = false ]; then
                # If the Kubernetes API is unavailable on the first attempt, immediately
                # quarantine the pod CIDR in the kernel so replacement pods scheduled on
                # this node cannot egress over clearnet while the cluster-wide emergency
                # NetworkPolicy is retried.
                quarantine_for_netpol=true
                if ensure_k3s_subnet_quarantine "${target_ip}" && ensure_k3s_ipv6_subnet_quarantine; then
                    quarantine_ok=true
                fi
            fi
            local max_attempts="${K3S_SHUTDOWN_NETPOL_ATTEMPTS:-3}"
            local attempt=1
            while [ "${attempt}" -lt "${max_attempts}" ]; do
                attempt=$((attempt + 1))
                if [ "${K3S_SHUTDOWN_RETRY_INTERVAL:-1}" -gt 0 ] 2>/dev/null; then
                    sleep "${K3S_SHUTDOWN_RETRY_INTERVAL}"
                fi
                if ensure_k3s_emergency_network_policy; then
                    policy_ok=true
                    if [ "${quarantine_for_netpol}" = true ]; then
                        remove_k3s_subnet_quarantine || true
                        remove_k3s_ipv6_subnet_quarantine || true
                        quarantine_ok=false
                    fi
                    break
                fi
            done
        fi
        if [ "${policy_ok}" = false ] && [ "${mode}" != "restart" ]; then
            delete_k3s_target_pod || true
        fi
        if [ "${policy_ok}" = true ] || [ "${quarantine_ok}" = true ] || [ "${guard_ok}" = true ] || [ "${blackhole_ok}" = true ]; then
            cleanup_dataplane "--keep-tunnel"
        fi
        local target_protected=false
        if [ "${guard_ok}" = true ] || [ "${blackhole_ok}" = true ] || [ "${quarantine_ok}" = true ]; then
            target_protected=true
        fi
        local replacement_protected=false
        if [ "${policy_ok}" = true ]; then
            replacement_protected=true
        elif [ "${quarantine_ok}" = true ] && { ! k3s_ipv6_is_active || [ -n "${K3S_IPV6_QUARANTINE_CIDR:-}" ]; }; then
            replacement_protected=true
        fi
        if { [ "${mode}" = "restart" ] && { [ "${replacement_protected}" = false ] || { [ -n "${target_ip}" ] && [ "${target_protected}" = false ]; }; }; } || \
           { [ "${mode}" != "restart" ] && { [ "${policy_ok}" = false ] || { [ -n "${target_ip}" ] && [ "${target_protected}" = false ]; }; }; }; then
            LAST_ERROR="${LAST_ERROR:-k3s: Failed to establish replacement-pod fail-closed protection during shutdown}"
            RULES_SYNCED="false"
            IPV4_RULES_SYNCED="false"
            write_state
            log ERROR "${LAST_ERROR}"
            return 1
        fi
        return 0
    fi
    local kill_switch_ok=true
    ensure_reconcile_kill_switch || true
    cleanup_dataplane "--keep-tunnel"
    if ! ensure_reconcile_kill_switch; then
        kill_switch_ok=false
    fi
    if [ "${kill_switch_ok}" = false ]; then
        LAST_ERROR="${LAST_ERROR:-Failed to preserve reconciliation kill switch during shutdown}"
        RULES_SYNCED="false"
        IPV4_RULES_SYNCED="false"
        write_state
        log ERROR "${LAST_ERROR}"
        return 1
    fi
    return 0
}

restore_saved_decommission_ownership() {
    if [ "${DECOMMISSION_SAVED_DOCKER_LOADED:-false}" = "true" ]; then
        MANAGED_DOCKER_IPV4_ENDPOINTS=("${DECOMMISSION_SAVED_DOCKER_ENDPOINTS[@]}")
        DOCKER_POLICY_STATE_LOADED="true"
        persist_managed_docker_policy_state || log WARN "${LAST_ERROR}"
    fi
    if [ "${DECOMMISSION_SAVED_IPV6_LOADED:-false}" = "true" ]; then
        MANAGED_TARGET_IPV6_ADDRESSES=("${DECOMMISSION_SAVED_IPV6_ADDRESSES[@]}")
        IPV6_POLICY_TABLE_MANAGED="${DECOMMISSION_SAVED_IPV6_TABLE_MANAGED}"
        IPV6_POLICY_STATE_LOADED="true"
        persist_managed_ipv6_policy_state || log WARN "${LAST_ERROR}"
    fi
}

_verify_dataplane_decommissioned_inner() {
    local managed_endpoint managed_source managed_subnet cleanup_ip pref rule_line

    if [[ "${K3S_MODE}" == "true" ]]; then
        if [ -n "${K3S_TARGET_POD_NAMESPACE:-}" ] && [ -n "${K3S_TARGET_POD_SELECTOR:-}" ] && \
           k3s_owned_emergency_network_policy_is_present "${K3S_TARGET_POD_NAMESPACE}" "${K3S_TARGET_POD_SELECTOR}"; then
            LAST_ERROR="k3s: Emergency deny-egress NetworkPolicy is still present"
            return 1
        fi
        if [ -n "${MANAGED_K3S_POD_NAMESPACE:-}" ] && [ -n "${MANAGED_K3S_POD_SELECTOR:-}" ] && \
           k3s_owned_emergency_network_policy_is_present "${MANAGED_K3S_POD_NAMESPACE}" "${MANAGED_K3S_POD_SELECTOR}"; then
            LAST_ERROR="k3s: Previous emergency deny-egress NetworkPolicy is still present"
            return 1
        fi
        if iptables -S FORWARD 2>/dev/null | grep -Fq -- "tunnelsats-k3s-egress-guard"; then
            LAST_ERROR="k3s: Failed to remove temporary egress guard"
            return 1
        fi
        if ! remove_k3s_subnet_quarantine; then
            return 1
        fi
        for pref in "${K3S_BYPASS_RULE_PREF}" "${K3S_TUNNEL_RULE_PREF}" "${K3S_BLACKHOLE_RULE_PREF}" "${K3S_QUARANTINE_RULE_PREF}"; do
            while IFS= read -r rule_line; do
                [ -n "${rule_line}" ] || continue
                if k3s_policy_rule_is_owned "${rule_line}"; then
                    LAST_ERROR="k3s: Failed to remove owned policy rule at pref ${pref}"
                    return 1
                fi
            done < <(ip rule show pref "${pref}" 2>/dev/null || true)
            while IFS= read -r rule_line; do
                [ -n "${rule_line}" ] || continue
                if k3s_policy_rule_is_owned "${rule_line}"; then
                    LAST_ERROR="k3s: Failed to remove owned IPv6 policy rule at pref ${pref}"
                    return 1
                fi
            done < <(ip -6 rule show pref "${pref}" 2>/dev/null || true)
        done
    else
        for cleanup_ip in "${DOCKER_TARGET_IP:-}" "10.21.21.9" "10.21.21.96"; do
            [ -n "${cleanup_ip}" ] || continue
            remove_source_blackhole_rule "${cleanup_ip}" "${KILL_SWITCH_PREF}" || return 1
            remove_source_blackhole_rule "${cleanup_ip}" "32765" || return 1
            if ip rule show pref 32500 2>/dev/null | grep -Fq -- "from ${cleanup_ip}" || \
               ip rule show pref 32764 2>/dev/null | grep -Fq -- "from ${cleanup_ip}"; then
                LAST_ERROR="Failed to remove IPv4 policy rules for ${cleanup_ip}"
                return 1
            fi
        done
        if [[ "${SECURE_MODE}" != "true" ]]; then
            local bridge_gw="${DOCKER_NETWORK_SUBNET%.*}.1"
            remove_source_blackhole_rule "${DOCKER_NETWORK_SUBNET}" "${KILL_SWITCH_PREF}" || return 1
            remove_source_blackhole_rule "${DOCKER_NETWORK_SUBNET}" "32765" || return 1
            if ip rule show pref 32500 2>/dev/null | grep -Fq -- "from ${DOCKER_NETWORK_SUBNET}" || \
               ip rule show pref 32763 2>/dev/null | grep -Fq -- "from ${bridge_gw}" || \
               ip rule show 2>/dev/null | grep -qE "^[0-9]+:[[:space:]]+from[[:space:]]+${DOCKER_NETWORK_SUBNET//./\\.}[[:space:]]+lookup[[:space:]]+51820[[:space:]]*$"; then
                LAST_ERROR="Failed to remove Docker bridge IPv4 policy rules for ${DOCKER_NETWORK_SUBNET}"
                return 1
            fi
            for managed_endpoint in "${DECOMMISSION_SAVED_DOCKER_ENDPOINTS[@]}"; do
                [ -n "${managed_endpoint}" ] || continue
                IFS='|' read -r managed_source managed_subnet _ <<< "${managed_endpoint}"
                [ -n "${managed_source}" ] || continue
                if ! remove_source_blackhole_rule "${managed_source}" "${KILL_SWITCH_PREF}" || \
                   ! remove_source_blackhole_rule "${managed_source}" "32765" || \
                   ip rule show pref 32500 2>/dev/null | grep -Fq -- "from ${managed_source}" || \
                   ip rule show pref 32764 2>/dev/null | grep -Fq -- "from ${managed_source}"; then
                    LAST_ERROR="${LAST_ERROR:-Failed to remove Docker IPv4 policy rules for ${managed_source}}"
                    return 1
                fi
            done
        fi
    fi

    if iptables -t nat -S PREROUTING 2>/dev/null | grep -Eq -- "tunnelsats-(dnat|k3s-dns)" || \
       iptables -t nat -S POSTROUTING 2>/dev/null | grep -Fq -- "tunnelsats-masq" || \
       iptables -S INPUT 2>/dev/null | grep -Eq -- "tunnelsats-k3s-dns-(in|guard)" || \
       iptables -S FORWARD 2>/dev/null | grep -Eq -- "tunnelsats-(forward-in|forward-out|k3s-egress-guard|k3s-dns-guard)" || \
       iptables -t mangle -S PREROUTING 2>/dev/null | grep -Eq -- "tunnelsats-(conn-restore|k3s-non-target)" || \
       iptables -t mangle -S FORWARD 2>/dev/null | grep -Eq -- "tunnelsats-(wg-mark|conn-save)"; then
        LAST_ERROR="Failed to remove tagged IPv4 dataplane iptables rules"
        return 1
    fi

    if ip6tables -S INPUT 2>/dev/null | grep -Fq -- "--comment tunnelsats-k3s-ipv6-dns-guard" || \
       ip6tables -S FORWARD 2>/dev/null | grep -Eq -- "--comment (${IPV6_RULE_MARKER}|${K3S_IPV6_QUARANTINE_MARKER}|tunnelsats-k3s-ipv6-dns-guard)" || \
       ip6tables -t mangle -S PREROUTING 2>/dev/null | grep -Fq -- "--comment ${K3S_NON_TARGET_RULE_MARKER}"; then
        LAST_ERROR="Failed to remove IPv6 FORWARD containment rules"
        return 1
    fi
    for managed_source in "${DECOMMISSION_SAVED_IPV6_ADDRESSES[@]}"; do
        [ -n "${managed_source}" ] || continue
        if ip -6 rule show pref "${IPV6_TUNNEL_RULE_PREF}" 2>/dev/null | grep -Fq -- "from ${managed_source}" || \
           ip -6 rule show pref "${IPV6_BLACKHOLE_RULE_PREF}" 2>/dev/null | grep -Fq -- "from ${managed_source}"; then
            LAST_ERROR="Failed to remove IPv6 policy rules for ${managed_source}"
            return 1
        fi
    done
    if [ "${DECOMMISSION_SAVED_IPV6_TABLE_MANAGED}" = "true" ] && \
       ip -6 route show table "${IPV6_POLICY_TABLE}" 2>/dev/null | grep -Fq -- "blackhole default"; then
        LAST_ERROR="Failed to remove IPv6 blackhole route from table ${IPV6_POLICY_TABLE}"
        return 1
    fi

    return 0
}

verify_dataplane_decommissioned() {
    if ! _verify_dataplane_decommissioned_inner; then
        restore_saved_decommission_ownership
        return 1
    fi
    return 0
}

decommission_dataplane() {
    local request_id="${1:-}"
    local decommission_ok=true
    local decommission_error=""
    log INFO "decommission_start request_id=${request_id:-none}"
    stop_k3s_pod_watcher
    stop_k3s_dns_proxy
    K3S_WATCHED_CANDIDATE_POD_IPS=""
    K3S_WATCHED_CANDIDATE_POD_IPV6_ADDRESSES=""
    K3S_WATCH_RESOURCE_VERSION=""
    K3S_WATCH_SCOPE=""
    K3S_UNSEEN_REPLACEMENT_PENDING="false"
    rm -f "$(k3s_watch_state_file_path)" 2>/dev/null || true

    DECOMMISSION_SAVED_DOCKER_LOADED="false"
    DECOMMISSION_SAVED_DOCKER_ENDPOINTS=()
    DECOMMISSION_SAVED_IPV6_LOADED="false"
    DECOMMISSION_SAVED_IPV6_ADDRESSES=()
    DECOMMISSION_SAVED_IPV6_TABLE_MANAGED="false"

    if [[ "${K3S_MODE}" != "true" ]] && [[ "${SECURE_MODE}" != "true" ]]; then
        if load_managed_docker_policy_state; then
            DECOMMISSION_SAVED_DOCKER_LOADED="true"
            DECOMMISSION_SAVED_DOCKER_ENDPOINTS=("${MANAGED_DOCKER_IPV4_ENDPOINTS[@]}")
        else
            decommission_ok=false
            decommission_error="${LAST_ERROR:-Failed to load Docker policy ownership state}"
        fi
    fi
    if load_managed_ipv6_policy_state; then
        DECOMMISSION_SAVED_IPV6_LOADED="true"
        DECOMMISSION_SAVED_IPV6_ADDRESSES=("${MANAGED_TARGET_IPV6_ADDRESSES[@]}")
        DECOMMISSION_SAVED_IPV6_TABLE_MANAGED="${IPV6_POLICY_TABLE_MANAGED}"
    else
        decommission_ok=false
        decommission_error="${decommission_error:-${LAST_ERROR:-Failed to load IPv6 policy ownership state}}"
    fi

    if [[ "${K3S_MODE}" == "true" ]]; then
        if ! load_managed_k3s_policy_state; then
            decommission_ok=false
            decommission_error="${decommission_error:-${LAST_ERROR:-k3s: Failed to load policy ownership state}}"
        fi
        ensure_k3s_selector_context || true
        if ! remove_k3s_emergency_network_policy; then
            decommission_ok=false
            decommission_error="${decommission_error:-${LAST_ERROR:-k3s: Failed to remove emergency deny-egress NetworkPolicy}}"
        fi
        if [ -n "${MANAGED_K3S_POD_NAMESPACE:-}" ] && [ -n "${MANAGED_K3S_POD_SELECTOR:-}" ] && \
           { [ "${MANAGED_K3S_POD_NAMESPACE}" != "${K3S_TARGET_POD_NAMESPACE:-}" ] || \
             [ "${MANAGED_K3S_POD_SELECTOR}" != "${K3S_TARGET_POD_SELECTOR:-}" ]; }; then
            if ! remove_k3s_emergency_network_policy_for_target "${MANAGED_K3S_POD_NAMESPACE}" "${MANAGED_K3S_POD_SELECTOR}"; then
                decommission_ok=false
                decommission_error="${decommission_error:-${LAST_ERROR:-k3s: Failed to remove previous emergency deny-egress NetworkPolicy}}"
            fi
        fi
    fi

    DEFER_WG_DOWN=true cleanup_dataplane
    if ! verify_dataplane_decommissioned; then
        decommission_ok=false
        decommission_error="${decommission_error:-${LAST_ERROR:-Failed to verify dataplane guard removal}}"
    fi

    if [ "${decommission_ok}" = false ]; then
        restore_saved_decommission_ownership
        DECOMMISSIONED="false"
        LAST_ERROR="${decommission_error}"
        RULES_SYNCED="false"
        IPV4_RULES_SYNCED="false"
        IPV6_RULES_SYNCED="false"
        DNS_PROTECTED="false"
        write_state
        if [ -n "${request_id}" ]; then
            write_decommission_result "${request_id}" false
        fi
        log ERROR "decommission_failed request_id=${request_id:-none} error=${LAST_ERROR}"
        return 1
    fi

    DECOMMISSIONED="true"
    if ! persist_decommissioned_state; then
        restore_saved_decommission_ownership
        DECOMMISSIONED="false"
        RULES_SYNCED="false"
        IPV4_RULES_SYNCED="false"
        IPV6_RULES_SYNCED="false"
        DNS_PROTECTED="false"
        write_state
        if [ -n "${request_id}" ]; then
            write_decommission_result "${request_id}" false
        fi
        log ERROR "decommission_failed request_id=${request_id:-none} error=${LAST_ERROR}"
        return 1
    fi

    ip route flush table 51820 >/dev/null 2>&1 || true
    if wg show "${WG_IFACE}" >/dev/null 2>&1; then
        wg-quick down "${WG_IFACE}" >/dev/null 2>&1 || true
    fi

    if [[ "${K3S_MODE}" == "true" ]] || [[ "${SECURE_MODE}" == "true" ]]; then
        DOCKER_TARGET_IP=""
    else
        DOCKER_TARGET_IP="10.9.9.9"
    fi
    TARGET_CONTAINER_ID=""
    TARGET_CONTAINER_NAME=""
    TARGET_IMPL=""
    TARGET_IPV6_ADDRESSES=()
    TARGET_IPV6_MACS=()
    TARGET_HAS_IPV6_DEFAULT_ROUTE="false"
    FORWARDING_PORT=""
    MANAGED_K3S_TARGET_IP=""
    MANAGED_K3S_TARGET_IPV6_ADDRESSES=()
    MANAGED_K3S_TARGET_IMPL=""
    MANAGED_K3S_POD_NAME=""
    MANAGED_K3S_POD_NAMESPACE=""
    MANAGED_K3S_POD_SELECTOR=""
    if [[ "${K3S_MODE}" == "true" ]]; then
        K3S_POLICY_STATE_LOADED="true"
        persist_managed_k3s_policy_state || log WARN "${LAST_ERROR}"
    fi
    RULES_SYNCED="false"
    IPV4_RULES_SYNCED="false"
    IPV6_RULES_SYNCED="false"
    DNS_PROTECTED="false"
    LAST_ERROR=""
    write_state
    discard_pending_activation_triggers
    if [ -n "${request_id}" ]; then
        write_decommission_result "${request_id}" true
    fi
    log INFO "decommission_done request_id=${request_id:-none}"
    return 0
}

discard_pending_activation_triggers() {
    ensure_reconcile_dirs
    rm -f "${RESTART_TRIGGER}"
    if [ -f "${RECONCILE_TRIGGER_LEGACY}" ]; then
        local legacy_req
        legacy_req="$(cat "${RECONCILE_TRIGGER_LEGACY}" 2>/dev/null || true)"
        rm -f "${RECONCILE_TRIGGER_LEGACY}"
        if is_valid_request_id "${legacy_req}"; then
            write_reconcile_result "${legacy_req}" false
        fi
    fi
    local trigger_path
    local req
    while IFS= read -r trigger_path; do
        [ -n "${trigger_path}" ] || continue
        req="$(basename "${trigger_path}" .trigger)"
        rm -f "${trigger_path}"
        if is_valid_request_id "${req}"; then
            write_reconcile_result "${req}" false
        fi
    done < <(find "${RECONCILE_TRIGGER_DIR}" -maxdepth 1 -type f -name '*.trigger' | sort)
}

process_decommission_triggers() {
    ensure_decommission_dirs
    local trigger_path
    local req
    while IFS= read -r trigger_path; do
        [ -n "${trigger_path}" ] || continue
        req="$(basename "${trigger_path}" .trigger)"
        rm -f "${trigger_path}"
        if is_valid_request_id "${req}"; then
            decommission_dataplane "${req}" || true
        else
            log WARN "Ignoring decommission trigger with invalid request id: ${req}"
        fi
    done < <(find "${DECOMMISSION_TRIGGER_DIR}" -maxdepth 1 -type f -name '*.trigger' | sort)
}

cleanup() {
    local rc=0
    log INFO "Received SIGTERM. Stopping ${APP_NAME}."
    stop_k3s_pod_watcher
    stop_k3s_dns_proxy
    if [ -n "${API_PID}" ]; then
        kill "${API_PID}" >/dev/null 2>&1 || true
    fi
    if ! load_decommissioned_state; then
        DECOMMISSIONED="false"
    fi
    if [ "${DECOMMISSIONED:-false}" = "true" ]; then
        cleanup_dataplane || rc=$?
    else
        preserve_fail_closed_dataplane || rc=$?
    fi
    exit "${rc}"
}

handle_restart_trigger() {
    log INFO "restart trigger detected"
    rm -f "${RESTART_TRIGGER}"
    if ! load_decommissioned_state; then
        RULES_SYNCED="false"
        IPV4_RULES_SYNCED="false"
        IPV6_RULES_SYNCED="false"
        write_state
        log ERROR "${LAST_ERROR:-Failed to load decommission state before WireGuard restart}"
        return 1
    fi
    local was_decommissioned="${DECOMMISSIONED:-false}"
    DECOMMISSIONED="false"
    if ! persist_decommissioned_state; then
        DECOMMISSIONED="${was_decommissioned}"
        RULES_SYNCED="false"
        IPV4_RULES_SYNCED="false"
        IPV6_RULES_SYNCED="false"
        write_state
        log ERROR "${LAST_ERROR:-Failed to persist decommission state before WireGuard restart}"
        return 1
    fi
    LAST_ERROR="Restart requested; dataplane remains fail-closed"
    if ! preserve_fail_closed_dataplane "restart"; then
        log ERROR "${LAST_ERROR:-Failed to preserve fail-closed dataplane before WireGuard restart}"
        return 1
    fi
    if wg show "${WG_IFACE}" >/dev/null 2>&1; then
        wg-quick down "${WG_CONF_PATH}" >/dev/null 2>&1 \
            || wg-quick down "${WG_IFACE}" >/dev/null 2>&1 \
            || ip link del dev "${WG_IFACE}" >/dev/null 2>&1 \
            || true
    fi
    reconcile_once "restart" || true
}

main_loop() {
    while true; do
        if [ -n "${API_PID}" ] && ! kill -0 "${API_PID}" >/dev/null 2>&1; then
            LAST_ERROR="Internal management process exited unexpectedly"
            log ERROR "${LAST_ERROR}"
            fail_reconcile_closed || true
            exit 1
        fi

        ensure_reconcile_dirs
        ensure_decommission_dirs

        if [[ "${K3S_MODE}" == "true" ]] && [ "${DECOMMISSIONED:-false}" != "true" ] && \
           [ "${K3S_ENABLE_BACKGROUND_WATCHER:-false}" = "true" ]; then
            ensure_k3s_pod_watcher_running
        fi

        if [ -f "${RESTART_TRIGGER}" ]; then
            if [ -n "$(find "${DECOMMISSION_TRIGGER_DIR}" -maxdepth 1 -type f -name '*.trigger' -print -quit 2>/dev/null)" ]; then
                rm -f "${RESTART_TRIGGER}"
            else
                handle_restart_trigger || true
            fi
        fi

        process_decommission_triggers

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

        if [[ "${K3S_MODE}" == "true" ]]; then
            if [ "${DECOMMISSIONED:-false}" != "true" ] && \
               [ "${K3S_ENABLE_BACKGROUND_WATCHER:-false}" != "true" ] && \
               [ ! -f "${RESTART_TRIGGER}" ] && [ ! -f "${RECONCILE_TRIGGER_LEGACY}" ] && \
               [ -z "$(find "${DECOMMISSION_TRIGGER_DIR}" "${RECONCILE_TRIGGER_DIR}" -maxdepth 1 -type f -name '*.trigger' -print -quit 2>/dev/null)" ]; then
                watch_k3s_target_pods_once || true
            fi
            if [ ! -f "${RESTART_TRIGGER}" ] && [ ! -f "${RECONCILE_TRIGGER_LEGACY}" ] && \
               [ -z "$(find "${DECOMMISSION_TRIGGER_DIR}" "${RECONCILE_TRIGGER_DIR}" -maxdepth 1 -type f -name '*.trigger' -print -quit 2>/dev/null)" ]; then
                sleep 0.2 2>/dev/null || sleep 1
            fi
        else
            sleep 2
        fi
    done
}

if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
    return 0
fi

if [[ "${TUNNELSATS_ROLE}" == "web" ]]; then
    log INFO "Starting unprivileged dashboard on ${DASHBOARD_BIND_HOST}:${DASHBOARD_BIND_PORT}"
    exec python3 /app/server/app.py
fi

trap cleanup SIGTERM SIGINT

if ! load_decommissioned_state; then
    log ERROR "Startup aborted because decommission state could not be loaded: ${LAST_ERROR}"
    exit 1
fi

if ! ensure_reconcile_kill_switch; then
    log ERROR "Startup aborted because the Lightning kill switch could not be installed: ${LAST_ERROR}"
    exit 1
fi

echo "Starting Tunnelsats v3 (mode: $([[ "${K3S_MODE}" == "true" ]] && echo "k3s" || echo "umbrel"))..."
if [[ "${TUNNELSATS_ROLE}" == "daemon" ]]; then
    install -d -m 0770 -o 0 -g "${TUNNELSATS_WEB_GID:-1000}" /run/tunnelsats
fi

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

if [[ "${TUNNELSATS_ROLE}" == "daemon" ]]; then
    # The web role is numeric/non-root. It may manage authenticated subscription
    # metadata and configs, while all host and node privileges remain here.
    chown -R "${TUNNELSATS_WEB_UID:-1000}:${TUNNELSATS_WEB_GID:-1000}" /data
fi

if [[ "${TUNNELSATS_ROLE}" == "daemon" ]]; then
    log INFO "Starting private daemon API on ${TUNNELSATS_DAEMON_SOCKET:-/run/tunnelsats/daemon.sock}"
else
    log INFO "Starting internal dashboard backend on ${DASHBOARD_BIND_HOST}:${DASHBOARD_BIND_PORT}"
fi
python3 /app/server/app.py &
API_PID=$!

ensure_reconcile_dirs
ensure_decommission_dirs
rm -f "$(k3s_watch_state_file_path)" 2>/dev/null || true
K3S_ENABLE_BACKGROUND_WATCHER="true"
ensure_k3s_pod_watcher_running

reconcile_once "startup" || true

echo "Tunnelsats ${TUNNELSATS_ROLE} container running."
main_loop
