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
export SECURE_MODE="${SECURE_MODE:-false}"
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

API_PID=""
LAST_RECONCILE_EPOCH=0

TARGET_CONTAINER_ID=""
TARGET_CONTAINER_NAME=""
TARGET_IMPL=""
FORWARDING_PORT=""
BRIDGE_NAME=""
K3S_TARGET_POD_NAME=""
K3S_TARGET_POD_NAMESPACE=""
K3S_TARGET_POD_SELECTOR=""
RULES_SYNCED="false"
LAST_ERROR=""
POLICY_CHANGED="0"
NAT_CHANGED="0"
K3S_BYPASS_CIDRS_NORMALIZED=""

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
    if [[ "${K3S_MODE}" == "true" ]]; then
        dataplane_mode="k3s"
    elif [[ "${SECURE_MODE}" == "true" ]]; then
        dataplane_mode="secure-mode"
    else
        dataplane_mode="docker-full-parity"
    fi

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
        --arg k3s_bypass_cidrs "${K3S_BYPASS_CIDRS_NORMALIZED:-}" \
        --arg last_reconcile_at "$(date -u +%FT%TZ)" \
        '{
            dataplane_mode: $dataplane_mode,
            target_container: $target_container,
            target_ip: $target_ip,
            target_impl: $target_impl,
            forwarding_port: $forwarding_port,
            rules_synced: $rules_synced,
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

    # Returning control to the ordinary reconcile loop while every isolation
    # mechanism is absent would leave the Lightning workload on clear-net
    # egress for an entire interval. Stay in a fail-stop retry loop until one
    # independent control is verified or the unsafe pod is deleted.
    while true; do
        attempt=$((attempt + 1))
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
        if ensure_k3s_subnet_quarantine "${DOCKER_TARGET_IP}"; then
            LAST_ERROR="${isolation_error}; pod CIDR quarantined after retry ${attempt}"
            return 0
        fi
        if ensure_k3s_emergency_network_policy; then
            LAST_ERROR="${isolation_error}; emergency NetworkPolicy installed after retry ${attempt}"
            return 0
        fi
        if delete_k3s_target_pod; then
            LAST_ERROR="${isolation_error}; target pod deleted after retry ${attempt}"
            return 0
        fi

        LAST_ERROR="${isolation_error}; all emergency isolation controls unavailable; fail-stop retry ${attempt}"
        write_state
        sleep "${K3S_ISOLATION_RETRY_INTERVAL}"
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
        200|202|404) return 0 ;;
        409)
            log WARN "k3s: Emergency NetworkPolicy changed before deletion; preserving replacement"
            return 0
            ;;
        *)
            LAST_ERROR="k3s: Failed to remove emergency deny-egress NetworkPolicy"
            return 1
            ;;
    esac
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

resolve_k3s_target_pod() {
    local impl="$1"
    local namespace="$2"
    local selector="$3"
    local encoded_selector pod_list running_pods pod
    local pod_name pod_ip pod_node

    encoded_selector=$(urlencode "${selector}")
    if ! pod_list=$(k8s_api "/api/v1/namespaces/${namespace}/pods?labelSelector=${encoded_selector}"); then
        LAST_ERROR="k3s: Failed to query ${impl^^} pods (namespace=${namespace}, selector=${selector})"
        log ERROR "${LAST_ERROR}"
        return 1
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
    pod_ip=$(printf '%s' "${pod}" | jq -r '.status.podIP // empty')
    pod_node=$(printf '%s' "${pod}" | jq -r '.spec.nodeName // empty')

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

    DOCKER_TARGET_IP="${pod_ip}"
    K3S_TARGET_POD_NAMESPACE="${namespace}"
    K3S_TARGET_POD_NAME="${pod_name}"
    K3S_TARGET_POD_SELECTOR="${selector}"
    log INFO "k3s: Using co-located ${impl^^} pod ${namespace}/${pod_name} on node ${pod_node} at ${pod_ip}"
    return 0
}

detect_k3s_target() {
    TARGET_CONTAINER_ID=""
    TARGET_CONTAINER_NAME=""
    TARGET_IMPL=""
    DOCKER_TARGET_IP=""
    K3S_TARGET_POD_NAME=""
    K3S_TARGET_POD_NAMESPACE=""
    K3S_TARGET_POD_SELECTOR=""

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
            if ! resolve_k3s_target_pod "lnd" "${LND_K8S_NAMESPACE}" "${LND_K8S_POD_SELECTOR}"; then
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
            if ! resolve_k3s_target_pod "cln" "${CLN_K8S_NAMESPACE}" "${CLN_K8S_POD_SELECTOR}"; then
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
        if [ -f "/data/tunnelsats-meta.json" ]; then
            meta_node=$(jq -r '.nodeType // empty' /data/tunnelsats-meta.json 2>/dev/null | tr '[:upper:]' '[:lower:]')
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
            return 0
        elif [ "${cln_ok}" -eq 1 ]; then
            TARGET_IMPL="cln"
            TARGET_CONTAINER_NAME="lightning_cln_1"
            LN_TARGET_PORT="9736"
            DOCKER_TARGET_IP="10.21.21.96"
            log INFO "SecureMode: Detected CLN node at ${DOCKER_TARGET_IP}"
            return 0
        fi
        LAST_ERROR="SecureMode: No Lightning node detected (probed 10.21.21.9 and 10.21.21.96)"
        return 1
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

ensure_container_attached() {
    [[ "${K3S_MODE}" == "true" ]] && return 0
    [[ "${SECURE_MODE}" == "true" ]] && return 0
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

ensure_k3s_egress_guard() {
    local source_ip="$1"
    local marker="tunnelsats-k3s-egress-guard"

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
    local current_source="$1"
    local marker="tunnelsats-k3s-egress-guard"
    local rules
    local rule
    local source
    local index
    local -a parts

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
        if [ "${source%/32}" = "${current_source}" ]; then
            continue
        fi
        parts[0]="-D"
        iptables "${parts[@]}" >/dev/null 2>&1 || return 1
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
        [ "${source%/32}" = "${current_source}" ] || return 1
    done <<< "${rules}"
    return 0
}

remove_k3s_egress_guards() {
    local marker="tunnelsats-k3s-egress-guard"

    remove_tagged_iptables_rules filter FORWARD "${marker}"
    if iptables -S FORWARD 2>/dev/null | grep -Fq -- "${marker}"; then
        LAST_ERROR="k3s: Failed to remove temporary egress guard"
        return 1
    fi
    return 0
}

ensure_k3s_subnet_quarantine() {
    local source_ip="$1"
    local pod_cidr
    local escaped_cidr
    local exact_pattern
    local quarantine_rule

    if ! normalize_k3s_bypass_cidrs; then
        return 1
    fi
    pod_cidr="$(
        SOURCE_IP="${source_ip}" \
        BYPASS_CIDRS="${K3S_BYPASS_CIDRS_NORMALIZED}" \
        python3 -c '
import ipaddress
import os

source = ipaddress.ip_address(os.environ["SOURCE_IP"])
matches = [
    ipaddress.ip_network(value)
    for value in os.environ["BYPASS_CIDRS"].split(",")
    if value and source in ipaddress.ip_network(value)
]
print(max(matches, key=lambda network: network.prefixlen) if matches else "")
'
    )"
    if [ -z "${pod_cidr}" ]; then
        LAST_ERROR="k3s: Cannot determine pod CIDR for emergency quarantine"
        return 1
    fi
    K3S_QUARANTINE_CIDR="${pod_cidr}"

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
        return 0
    fi
    if ! ip rule add from "${pod_cidr}" blackhole protocol "${K3S_RULE_PROTOCOL}" pref "${K3S_QUARANTINE_RULE_PREF}" >/dev/null 2>&1; then
        LAST_ERROR="k3s: Failed to install emergency pod-CIDR quarantine"
        return 1
    fi
    quarantine_rule="$(ip rule show pref "${K3S_QUARANTINE_RULE_PREF}" 2>/dev/null | grep -E "${exact_pattern}" | head -n 1 || true)"
    if [ -z "${quarantine_rule}" ]; then
        LAST_ERROR="k3s: Emergency pod-CIDR quarantine was not installed"
        return 1
    fi
    return 0
}

remove_k3s_subnet_quarantine() {
    local rule_line
    local rule_spec
    local rule_source
    local quarantine_pref
    local owned_quarantine

    # Also remove pre-upgrade CIDR-wide quarantines from pref 32765. A /32 at
    # that legacy preference is the persistent per-pod fallback and must stay.
    for quarantine_pref in "${K3S_QUARANTINE_RULE_PREF}" 32765; do
        while IFS= read -r rule_line; do
            [ -n "${rule_line}" ] || continue
            rule_spec="${rule_line#*:}"
            [[ "${rule_spec}" == *"blackhole"* ]] || continue
            owned_quarantine=false
            if k3s_policy_rule_is_owned "${rule_line}"; then
                owned_quarantine=true
            elif [ "${quarantine_pref}" = "32765" ] && \
                 [ -n "${K3S_LEGACY_RULE_PROTOCOL}" ] && \
                 [[ "${rule_spec}" =~ (^|[[:space:]])proto(col)?[[:space:]]+${K3S_LEGACY_RULE_PROTOCOL}([[:space:]]|$) ]]; then
                owned_quarantine=true
            fi
            [ "${owned_quarantine}" = true ] || continue
            rule_source="$(awk '{for (i = 1; i <= NF; i++) if ($i == "from") {print $(i + 1); exit}}' <<< "${rule_spec}")"
            if [ "${quarantine_pref}" = "32765" ]; then
                [[ "${rule_source}" == */* ]] || continue
                [[ "${rule_source}" != */32 ]] || continue
            else
                [ -n "${rule_source}" ] || continue
            fi
            delete_policy_rule_line "${rule_line}"
        done < <(ip rule show pref "${quarantine_pref}" 2>/dev/null || true)
    done

    for quarantine_pref in "${K3S_QUARANTINE_RULE_PREF}" 32765; do
        while IFS= read -r rule_line; do
            [ -n "${rule_line}" ] || continue
            rule_spec="${rule_line#*:}"
            [[ "${rule_spec}" == *"blackhole"* ]] || continue
            owned_quarantine=false
            if k3s_policy_rule_is_owned "${rule_line}"; then
                owned_quarantine=true
            elif [ "${quarantine_pref}" = "32765" ] && \
                 [ -n "${K3S_LEGACY_RULE_PROTOCOL}" ] && \
                 [[ "${rule_spec}" =~ (^|[[:space:]])proto(col)?[[:space:]]+${K3S_LEGACY_RULE_PROTOCOL}([[:space:]]|$) ]]; then
                owned_quarantine=true
            fi
            [ "${owned_quarantine}" = true ] || continue
            rule_source="$(awk '{for (i = 1; i <= NF; i++) if ($i == "from") {print $(i + 1); exit}}' <<< "${rule_spec}")"
            if [ "${quarantine_pref}" = "32765" ]; then
                [[ "${rule_source}" == */* ]] || continue
                [[ "${rule_source}" != */32 ]] || continue
            fi
            LAST_ERROR="k3s: Failed to remove emergency pod-CIDR quarantine"
            return 1
        done < <(ip rule show pref "${quarantine_pref}" 2>/dev/null || true)
    done
    if ! clear_k3s_legacy_rule_protocol; then
        LAST_ERROR="k3s: Failed to retire legacy quarantine ownership marker"
        return 1
    fi
    return 0
}

release_k3s_reconcile_guards() {
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

    # Remove every tagged guard, including stale IPs from failed prior
    # reconciliations, only after the unguarded routing policy is verified.
    if ! remove_k3s_egress_guards; then
        return 1
    fi
    # A successfully recovered dataplane must not be left behind an emergency
    # deny policy merely because the API was temporarily unavailable. Keep the
    # workload fail-closed while retrying, and only report recovery once the
    # policy is gone (or a foreign replacement has been safely preserved).
    while ! remove_k3s_emergency_network_policy; do
        LAST_ERROR="k3s: Dataplane recovered; retrying emergency NetworkPolicy cleanup"
        write_state
        sleep "${K3S_ISOLATION_RETRY_INTERVAL}"
    done
    return 0
}

get_target_subnet() {
    local target_ip="${1:-${DOCKER_TARGET_IP:-}}"
    if [ -z "${target_ip}" ]; then
        echo "10.21.0.0/16"
        return
    fi
    local route_info dev_iface subnet
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

            rule_source="$(awk '{for (i = 1; i <= NF; i++) if ($i == "from") {print $(i + 1); exit}}' <<< "${rule_spec}")"
            if [ "${pref}" = "${K3S_BLACKHOLE_RULE_PREF}" ] && [[ "${rule_source}" == */* ]] && [[ "${rule_source}" != */32 ]]; then
                # CIDR-wide emergency quarantine must survive ordinary stale
                # per-pod cleanup; only explicit full cleanup may remove it.
                continue
            fi
            if [ -n "${rule_source}" ] && \
               [ "${rule_source}" != "${current_source}" ] && \
               k3s_policy_rule_is_owned "${rule_line}"; then
                delete_policy_rule_line "${rule_line}"
                POLICY_CHANGED="1"
            fi
        done < <(ip rule show pref "${pref}" 2>/dev/null || true)
    done
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

rules_are_synced() {
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

cleanup_k3s_policy_rules() {
    local keep_blackholes="$1"
    local pref
    local rule_line
    local rule_spec

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
                [ "${keep_blackholes}" = "false" ] || continue
                [[ "${rule_spec}" == *"blackhole"* ]] || continue
                [[ "${rule_spec}" == *" from "* || "${rule_spec}" == from\ * ]] || continue
            fi

            k3s_policy_rule_is_owned "${rule_line}" || continue

            delete_policy_rule_line "${rule_line}"
        done < <(ip rule show pref "${pref}" 2>/dev/null || true)
    done
}

cleanup_dataplane() {
    local keep_tunnel=false
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
                ip rule del from "${cleanup_ip}" blackhole pref 32765 >/dev/null 2>&1 || true
            fi
        done

        if [[ "${SECURE_MODE}" != "true" ]]; then
            # Remove local bypass rule (pref 32500)
            ip rule del from "${DOCKER_NETWORK_SUBNET}" to "${DOCKER_NETWORK_SUBNET}" table main pref 32500 >/dev/null 2>&1 || true

            # Remove bridge gateway tunnel rule (pref 32763)
            local bridge_gw
            bridge_gw="${DOCKER_NETWORK_SUBNET%.*}.1"
            ip rule del from "${bridge_gw}" table 51820 pref 32763 >/dev/null 2>&1 || true

            if [ "${keep_tunnel}" = false ]; then
                ip rule del from "${DOCKER_NETWORK_SUBNET}" blackhole pref 32765 >/dev/null 2>&1 || true
            fi

            while ip rule show | grep -qE "^[0-9]+:[[:space:]]+from[[:space:]]+${DOCKER_NETWORK_SUBNET//./\\.}[[:space:]]+lookup[[:space:]]+51820[[:space:]]*$" && [ ${attempt} -lt ${max_attempts} ]; do
                ip rule del from "${DOCKER_NETWORK_SUBNET}" table 51820 >/dev/null 2>&1 || break
                attempt=$((attempt + 1))
            done
        fi
    fi

    if [ "${keep_tunnel}" = false ]; then
        ip route flush table 51820 >/dev/null 2>&1 || true

        if wg show "${WG_IFACE}" >/dev/null 2>&1; then
            wg-quick down "${WG_IFACE}" >/dev/null 2>&1 || true
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

reconcile_once() {
    local reason="$1"
    local request_id="${2:-}"
    local changed=0
    local policy_changed="0"
    local nat_changed="0"
    local k3s_guard_active=false

    LAST_ERROR=""
    RULES_SYNCED="false"

    log INFO "reconcile_start reason=${reason}"

    if [[ "${K3S_MODE}" != "true" ]] && [[ "${SECURE_MODE}" != "true" ]] && [ ! -S "${DOCKER_SOCK}" ]; then
        LAST_ERROR="Docker socket unavailable"
        write_state
        if [ -n "${request_id}" ]; then
            write_reconcile_result "${request_id}" false
        fi
        return 1
    fi

    if ! detect_lightning_container; then
        LAST_ERROR="${LAST_ERROR:-No running LND/CLN container detected}"
        cleanup_dataplane "--keep-tunnel"
        write_state
        if [ -n "${request_id}" ]; then
            write_reconcile_result "${request_id}" false
        fi
        return 1
    fi

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
        if [[ "${K3S_MODE}" == "true" ]] && ! release_k3s_reconcile_guards; then
            RULES_SYNCED="false"
        else
            RULES_SYNCED="true"
        fi
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

if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
    return 0
fi

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
