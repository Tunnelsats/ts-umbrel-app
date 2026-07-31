import os
import subprocess


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
ENTRYPOINT_PATH = os.path.join(REPO_ROOT, "scripts", "entrypoint.sh")


def run_bash(script):
    return subprocess.run(
        ["bash", "-c", script, "policy-test", ENTRYPOINT_PATH],
        check=False,
        capture_output=True,
        text=True,
    )


def test_fallback_blackhole_rule_removes_conflicts_and_is_idempotent():
    result = run_bash(
        r'''
source "$1"

RULES=$'32765:\tfrom 10.9.9.0/25 lookup main\n32765:\tfrom 10.9.9.0/25 blackhole'
DELETE_COUNT=0
ADD_COUNT=0

ip() {
    if [[ "$1 $2 $3 $4" == "rule show pref 32765" ]]; then
        printf '%s\n' "${RULES}"
        return 0
    fi
    if [[ "$1 $2" == "rule del" ]]; then
        DELETE_COUNT=$((DELETE_COUNT + 1))
        RULES=""
        return 0
    fi
    if [[ "$1 $2" == "rule add" ]]; then
        ADD_COUNT=$((ADD_COUNT + 1))
        RULES=$'32765:\tfrom 10.9.9.0/25 blackhole'
        return 0
    fi
    return 1
}

ensure_fallback_blackhole_rule "10.9.9.0/25" "test failure"
[[ "${BLACKHOLE_CHANGED}" == "1" ]]
[[ "${DELETE_COUNT}" == "2" ]]
[[ "${ADD_COUNT}" == "1" ]]
[[ "${RULES}" == $'32765:\tfrom 10.9.9.0/25 blackhole' ]]

ensure_fallback_blackhole_rule "10.9.9.0/25" "test failure"
[[ "${BLACKHOLE_CHANGED}" == "0" ]]
[[ "${DELETE_COUNT}" == "2" ]]
[[ "${ADD_COUNT}" == "1" ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_k3s_lnd_target_must_be_colocated():
    result = run_bash(
        r'''
source "$1"

K3S_MODE="true"
TUNNELSATS_K8S_NODE_NAME="worker-a"
LND_K8S_SERVICE="lnd"
LND_K8S_NAMESPACE="lightning"
LND_K8S_POD_SELECTOR="app=lnd"
CLN_K8S_SERVICE=""

resolve_svc_ip() {
    printf '%s\n' "10.43.0.10"
}
k8s_api() {
    printf '%s\n' '{"items":[{"metadata":{"name":"lnd-remote"},"spec":{"nodeName":"worker-b"},"status":{"phase":"Running","podIP":"10.42.2.9","conditions":[{"type":"Ready","status":"True"}]}},{"metadata":{"name":"lnd-unready"},"spec":{"nodeName":"worker-a"},"status":{"phase":"Running","podIP":"10.42.1.5","conditions":[{"type":"Ready","status":"False"}]}},{"metadata":{"name":"lnd-terminating","deletionTimestamp":"2026-07-31T08:00:00Z"},"spec":{"nodeName":"worker-a"},"status":{"phase":"Running","podIP":"10.42.1.6","conditions":[{"type":"Ready","status":"True"}]}},{"metadata":{"name":"lnd-local"},"spec":{"nodeName":"worker-a"},"status":{"phase":"Running","podIP":"10.42.1.7","conditions":[{"type":"Ready","status":"True"}]}}]}'
}

detect_k3s_target
[[ "${TARGET_IMPL}" == "lnd" ]]
[[ "${TARGET_CONTAINER_NAME}" == "lnd" ]]
[[ "${DOCKER_TARGET_IP}" == "10.42.1.7" ]]
[[ "${LN_TARGET_PORT}" == "9735" ]]
[[ -z "${LAST_ERROR}" ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_k3s_cln_target_must_be_colocated():
    result = run_bash(
        r'''
source "$1"

K3S_MODE="true"
TUNNELSATS_K8S_NODE_NAME="worker-b"
LND_K8S_SERVICE=""
CLN_K8S_SERVICE="cln"
CLN_K8S_NAMESPACE="lightning"
CLN_K8S_POD_SELECTOR="app=cln"

resolve_svc_ip() {
    printf '%s\n' "10.43.0.11"
}
k8s_api() {
    printf '%s\n' '{"items":[{"metadata":{"name":"cln-0"},"spec":{"nodeName":"worker-b"},"status":{"phase":"Running","podIP":"10.42.2.8","conditions":[{"type":"Ready","status":"True"}]}}]}'
}

detect_k3s_target
[[ "${TARGET_IMPL}" == "cln" ]]
[[ "${TARGET_CONTAINER_NAME}" == "cln" ]]
[[ "${DOCKER_TARGET_IP}" == "10.42.2.8" ]]
[[ "${LN_TARGET_PORT}" == "9736" ]]
[[ -z "${LAST_ERROR}" ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_k3s_cross_node_target_fails_closed_before_dataplane_activation():
    result = run_bash(
        r'''
source "$1"

K3S_MODE="true"
TUNNELSATS_K8S_NODE_NAME="worker-a"
LND_K8S_SERVICE="lnd"
LND_K8S_NAMESPACE="lightning"
LND_K8S_POD_SELECTOR="app=lnd"
CLN_K8S_SERVICE=""
CLEANUP_COUNT=0
WG_UP_COUNT=0
STATE_ERROR=""
STATE_SYNCED=""
STATE_TARGET_IP=""

resolve_svc_ip() {
    printf '%s\n' "10.43.0.10"
}
k8s_api() {
    printf '%s\n' '{"items":[{"metadata":{"name":"lnd-0"},"spec":{"nodeName":"worker-b"},"status":{"phase":"Running","podIP":"10.42.2.7"}}]}'
}
cleanup_dataplane() {
    [[ "${1:-}" == "--keep-tunnel" ]]
    CLEANUP_COUNT=$((CLEANUP_COUNT + 1))
}
ensure_wg_up() {
    WG_UP_COUNT=$((WG_UP_COUNT + 1))
}
write_state() {
    STATE_ERROR="${LAST_ERROR}"
    STATE_SYNCED="${RULES_SYNCED}"
    STATE_TARGET_IP="${DOCKER_TARGET_IP}"
}

if reconcile_once "test"; then
    exit 1
fi

[[ "${CLEANUP_COUNT}" == "1" ]]
[[ "${WG_UP_COUNT}" == "0" ]]
[[ "${STATE_SYNCED}" == "false" ]]
[[ -z "${STATE_TARGET_IP}" ]]
[[ "${STATE_ERROR}" == *"Pod co-location required"* ]]
[[ "${STATE_ERROR}" == *"TunnelSats node=worker-a"* ]]
[[ "${STATE_ERROR}" == *"LND pod=lightning/lnd-0 node=worker-b"* ]]
[[ "${STATE_ERROR}" == *"Check podAffinity in k3s/deployment.yaml or node scheduling labels."* ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_k3s_target_resolution_fails_when_node_metadata_is_unavailable():
    result = run_bash(
        r'''
source "$1"

TUNNELSATS_K8S_NODE_NAME="worker-a"
k8s_api() {
    printf '%s\n' '{"items":[{"metadata":{"name":"lnd-0"},"spec":{},"status":{"phase":"Running","podIP":"10.42.1.7"}}]}'
}

if resolve_k3s_target_pod "lnd" "lightning" "app=lnd"; then
    exit 1
fi
[[ "${LAST_ERROR}" == *"pod metadata incomplete"* ]]
[[ "${LAST_ERROR}" == *"node=missing"* ]]

TUNNELSATS_K8S_NODE_NAME=""
k8s_api() {
    printf '%s\n' '{"items":[{"metadata":{"name":"lnd-0"},"spec":{"nodeName":"worker-a"},"status":{"phase":"Running","podIP":"10.42.1.7"}}]}'
}

if resolve_k3s_target_pod "lnd" "lightning" "app=lnd"; then
    exit 1
fi
[[ "${LAST_ERROR}" == *"TunnelSats node name is unavailable"* ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_k3s_target_resolution_reports_api_and_selector_failures():
    result = run_bash(
        r'''
source "$1"

TUNNELSATS_K8S_NODE_NAME="worker-a"
k8s_api() {
    return 1
}

if resolve_k3s_target_pod "lnd" "lightning" "app=lnd"; then
    exit 1
fi
[[ "${LAST_ERROR}" == *"Failed to query LND pods"* ]]
[[ "${LAST_ERROR}" == *"namespace=lightning"* ]]
[[ "${LAST_ERROR}" == *"selector=app=lnd"* ]]

k8s_api() {
    printf '%s\n' '{"items":[]}'
}

if resolve_k3s_target_pod "lnd" "lightning" "app=lnd"; then
    exit 1
fi
[[ "${LAST_ERROR}" == *"No Running LND pod found"* ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_k3s_target_resolution_rejects_unready_or_terminating_local_pods():
    result = run_bash(
        r'''
source "$1"

TUNNELSATS_K8S_NODE_NAME="worker-a"
k8s_api() {
    printf '%s\n' '{"items":[{"metadata":{"name":"lnd-unready"},"spec":{"nodeName":"worker-a"},"status":{"phase":"Running","podIP":"10.42.1.5","conditions":[{"type":"Ready","status":"False"}]}},{"metadata":{"name":"lnd-terminating","deletionTimestamp":"2026-07-31T08:00:00Z"},"spec":{"nodeName":"worker-a"},"status":{"phase":"Running","podIP":"10.42.1.6","conditions":[{"type":"Ready","status":"True"}]}}]}'
}

if resolve_k3s_target_pod "lnd" "lightning" "app=lnd"; then
    exit 1
fi
[[ "${LAST_ERROR}" == *"No Ready non-terminating LND pod is co-located"* ]]
[[ "${LAST_ERROR}" == *"TunnelSats node=worker-a"* ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_k3s_bypass_cidrs_are_normalized_and_unsafe_values_are_rejected():
    result = run_bash(
        r'''
source "$1"

K3S_BYPASS_CIDRS="10.43.0.1/16, 10.42.0.0/16,10.43.0.0/16"
normalize_k3s_bypass_cidrs
[[ "${K3S_BYPASS_CIDRS_NORMALIZED}" == "10.42.0.0/16,10.43.0.0/16" ]]

K3S_BYPASS_CIDRS="10.42.0.0/16,not-a-cidr"
if normalize_k3s_bypass_cidrs; then
    exit 1
fi
[[ "${LAST_ERROR}" == *"Invalid K3S_BYPASS_CIDRS"* ]]

LAST_ERROR=""
K3S_BYPASS_CIDRS="0.0.0.0/0"
if normalize_k3s_bypass_cidrs; then
    exit 1
fi
[[ "${LAST_ERROR}" == *"must not contain 0.0.0.0/0"* ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_k3s_policy_routes_external_traffic_with_only_explicit_local_bypasses():
    result = run_bash(
        r'''
source "$1"

K3S_MODE="true"
DOCKER_TARGET_IP="10.42.1.7"
K3S_BYPASS_CIDRS="10.42.0.0/16,10.43.0.0/16"
WG_IFACE="tunnelsatsv2"
RULES=""
ROUTES=""
ADD_COUNT=0
DELETE_LOG=""

ip() {
    if [[ "$*" == "rule show pref 32500" ]]; then
        printf '%s' "${RULES}" | grep '^32500:' || true
        return 0
    fi
    if [[ "$*" == "rule show pref 32765" ]]; then
        printf '%s' "${RULES}" | grep '^32765:' || true
        return 0
    fi
    if [[ "$*" == "rule show" ]]; then
        printf '%s' "${RULES}"
        return 0
    fi
    if [[ "$1 $2" == "rule add" ]]; then
        ADD_COUNT=$((ADD_COUNT + 1))
        case "$*" in
            "rule add from 10.42.1.7 to 10.42.0.0/16 table main pref 32500")
                RULES+=$'32500:\tfrom 10.42.1.7 to 10.42.0.0/16 lookup main\n'
                ;;
            "rule add from 10.42.1.7 to 10.43.0.0/16 table main pref 32500")
                RULES+=$'32500:\tfrom 10.42.1.7 to 10.43.0.0/16 lookup main\n'
                ;;
            "rule add from 10.42.1.7 table 51820 pref 32764")
                RULES+=$'32764:\tfrom 10.42.1.7 lookup 51820\n'
                ;;
            "rule add from 10.42.1.7 blackhole pref 32765")
                RULES+=$'32765:\tfrom 10.42.1.7 blackhole\n'
                ;;
            *)
                return 1
                ;;
        esac
        return 0
    fi
    if [[ "$1 $2" == "rule del" ]]; then
        DELETE_LOG+="$*"$'\n'
        return 0
    fi
    if [[ "$1 $2" == "route replace" ]]; then
        ROUTES+="$*"$'\n'
        return 0
    fi
    if [[ "$*" == "route del 10.9.0.0/24 table 51820" ]]; then
        return 0
    fi
    if [[ "$*" == "-4 addr show dev tunnelsatsv2" ]]; then
        printf '%s\n' "7: tunnelsatsv2    inet 10.9.0.2/24 scope global tunnelsatsv2"
        return 0
    fi
    return 1
}

ensure_policy_routing
[[ "${POLICY_CHANGED}" == "1" ]]
[[ "${RULES}" == *$'32500:\tfrom 10.42.1.7 to 10.42.0.0/16 lookup main'* ]]
[[ "${RULES}" == *$'32500:\tfrom 10.42.1.7 to 10.43.0.0/16 lookup main'* ]]
[[ "${RULES}" == *$'32764:\tfrom 10.42.1.7 lookup 51820'* ]]
[[ "${RULES}" == *$'32765:\tfrom 10.42.1.7 blackhole'* ]]
[[ "${RULES}" != *"fwmark"* ]]
[[ "${ROUTES}" == *"route replace default dev tunnelsatsv2 metric 2 table 51820"* ]]
[[ "${ROUTES}" == *"route replace blackhole default metric 3 table 51820"* ]]

FIRST_ADD_COUNT="${ADD_COUNT}"
ensure_policy_routing
[[ "${POLICY_CHANGED}" == "0" ]]
[[ "${ADD_COUNT}" == "${FIRST_ADD_COUNT}" ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_k3s_policy_validation_failure_installs_blackhole_before_returning():
    result = run_bash(
        r'''
source "$1"

K3S_MODE="true"
DOCKER_TARGET_IP="10.42.1.7"
K3S_BYPASS_CIDRS="0.0.0.0/0"
RULES=""

ip() {
    if [[ "$*" == "rule show pref 32765" ]]; then
        printf '%s' "${RULES}"
        return 0
    fi
    if [[ "$*" == "rule add from 10.42.1.7 blackhole pref 32765" ]]; then
        RULES=$'32765:\tfrom 10.42.1.7 blackhole\n'
        return 0
    fi
    return 0
}

if ensure_policy_routing; then
    exit 1
fi
[[ "${RULES}" == *"from 10.42.1.7 blackhole"* ]]
[[ "${LAST_ERROR}" == *"must not contain 0.0.0.0/0"* ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_k3s_policy_table_repair_suspends_source_rule_before_removing_default():
    result = run_bash(
        r'''
source "$1"

DOCKER_TARGET_IP="10.42.1.7"
WG_IFACE="tunnelsatsv2"
SOURCE_RULE=$'32764:\tfrom 10.42.1.7 lookup 51820\n'
DELETE_LOG=""

ip() {
    if [[ "$*" == "route replace blackhole default metric 3 table 51820" ]] || \
       [[ "$*" == "route replace default dev tunnelsatsv2 metric 2 table 51820" ]]; then
        return 0
    fi
    if [[ "$*" == "route show table 51820" ]]; then
        printf '%s\n' \
            "default via 192.0.2.1 dev eth0 metric 1" \
            "default dev tunnelsatsv2 metric 2" \
            "blackhole default metric 3"
        return 0
    fi
    if [[ "$*" == "rule del from 10.42.1.7 table 51820 pref 32764" ]]; then
        DELETE_LOG+="$*"$'\n'
        SOURCE_RULE=""
        return 0
    fi
    if [[ "$*" == "rule show" ]]; then
        printf '%s' "${SOURCE_RULE}"
        return 0
    fi
    if [[ "$*" == "route del table 51820 default via 192.0.2.1 dev eth0 metric 1" ]]; then
        DELETE_LOG+="$*"$'\n'
        return 0
    fi
    return 1
}

ensure_k3s_policy_table_defaults
[[ "${K3S_TABLE_CHANGED}" == "1" ]]
[[ "${DELETE_LOG}" == *"rule del from 10.42.1.7 table 51820 pref 32764"* ]]
[[ "${DELETE_LOG}" == *"route del table 51820 default via 192.0.2.1 dev eth0 metric 1"* ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_k3s_rules_are_synced_requires_full_outbound_and_blackhole_routes():
    result = run_bash(
        r'''
source "$1"

K3S_MODE="true"
SECURE_MODE="false"
DOCKER_TARGET_IP="10.42.1.7"
K3S_BYPASS_CIDRS="10.42.0.0/16,10.43.0.0/16"
FORWARDING_PORT="19735"
LN_TARGET_PORT="9735"
WG_IFACE="tunnelsatsv2"
RULES=$'32500:\tfrom 10.42.1.7 to 10.42.0.0/16 lookup main\n32500:\tfrom 10.42.1.7 to 10.43.0.0/16 lookup main\n32764:\tfrom 10.42.1.7 lookup 51820\n32765:\tfrom 10.42.1.7 blackhole\n'
EXTRA_DEFAULT=0

ip() {
    if [[ "$*" == "rule show pref 32500" ]]; then
        printf '%s' "${RULES}" | grep '^32500:' || true
        return 0
    fi
    if [[ "$*" == "rule show pref 32765" ]]; then
        printf '%s' "${RULES}" | grep '^32765:' || true
        return 0
    fi
    if [[ "$*" == "rule show" ]]; then
        printf '%s' "${RULES}"
        return 0
    fi
    if [[ "$*" == "route show table 51820" ]]; then
        printf '%s\n' \
            "default dev tunnelsatsv2 metric 2" \
            "blackhole default metric 3" \
            "10.9.0.0/24 dev tunnelsatsv2"
        if [ "${EXTRA_DEFAULT}" = "1" ]; then
            printf '%s\n' "default via 192.0.2.1 dev eth0 metric 1"
        fi
        return 0
    fi
    return 1
}
iptables() {
    return 0
}

rules_are_synced

EXTRA_DEFAULT=1
if rules_are_synced; then
    exit 1
fi
EXTRA_DEFAULT=0

RULES="${RULES//$'32764:\tfrom 10.42.1.7 lookup 51820\n'/}"
if rules_are_synced; then
    exit 1
fi

RULES=$'32500:\tfrom 10.42.1.7 to 10.42.0.0/16 lookup main\n32500:\tfrom 10.42.1.7 to 10.43.0.0/16 lookup main\n30000:\tfrom 10.42.1.7 lookup 51820\n32765:\tfrom 10.42.1.7 blackhole\n'
if rules_are_synced; then
    exit 1
fi

RULES=$'32500:\tfrom 10.42.1.7 to 10.42.0.0/16 lookup main\n32500:\tfrom 10.42.1.7 to 10.43.0.0/16 lookup main\n32500:\tfrom 10.42.1.7 to 192.168.0.0/16 lookup main\n32764:\tfrom 10.42.1.7 lookup 51820\n32765:\tfrom 10.42.1.7 blackhole\n'
if rules_are_synced; then
    exit 1
fi
'''
    )

    assert result.returncode == 0, result.stderr


def test_k3s_nat_reconcile_removes_legacy_connmark_rules():
    result = run_bash(
        r'''
source "$1"

K3S_MODE="true"
SECURE_MODE="false"
DOCKER_TARGET_IP="10.42.1.7"
FORWARDING_PORT="19735"
LN_TARGET_PORT="9735"
WG_IFACE="tunnelsatsv2"
REMOVED=""

remove_tagged_iptables_rules() {
    REMOVED+="$1/$2/$3"$'\n'
}
iptables() {
    if [[ "$*" == "-t nat -S PREROUTING" ]]; then
        printf '%s\n' \
            "-A PREROUTING -i tunnelsatsv2 -p tcp --dport 19735 -m comment --comment tunnelsats-dnat -j DNAT --to-destination 10.42.1.7:9735" \
            "-A PREROUTING -i tunnelsatsv2 -p tcp --dport 9735 -m comment --comment tunnelsats-dnat -j DNAT --to-destination 10.42.1.7:9735"
        return 0
    fi
    if [[ "$*" == "-t nat -S POSTROUTING" ]]; then
        printf '%s\n' "-A POSTROUTING -s 10.42.1.7 -o tunnelsatsv2 -m comment --comment tunnelsats-masq -j MASQUERADE"
        return 0
    fi
    return 0
}

ensure_nat_forward_rules
[[ "${REMOVED}" == *"mangle/PREROUTING/tunnelsats-conn-restore"* ]]
[[ "${REMOVED}" == *"mangle/FORWARD/tunnelsats-conn-save"* ]]
[[ "${REMOVED}" == *"mangle/FORWARD/tunnelsats-wg-mark"* ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_stale_k3s_rules_are_removed_without_touching_current_source():
    result = run_bash(
        r'''
source "$1"

POLICY_CHANGED="0"
DELETED=""

ip() {
    if [[ "$*" == "rule show pref 32500" ]]; then
        printf '%s\n' \
            $'32500:\tfrom 10.42.0.9 to 10.42.0.0/16 lookup main' \
            $'32500:\tfrom 10.42.1.7 to 10.42.0.0/16 lookup main'
        return 0
    fi
    if [[ "$*" == "rule show pref 32764" ]]; then
        printf '%s\n' \
            $'32764:\tfrom 10.42.0.9 lookup 51820' \
            $'32764:\tfrom 10.42.1.7 lookup 51820'
        return 0
    fi
    if [[ "$*" == "rule show pref 32765" ]]; then
        printf '%s\n' \
            $'32765:\tfrom 10.42.0.9 blackhole' \
            $'32765:\tfrom 10.42.1.7 blackhole'
        return 0
    fi
    if [[ "$1 $2" == "rule del" ]]; then
        DELETED+="$*"$'\n'
        return 0
    fi
    return 0
}

remove_stale_k3s_policy_rules "10.42.1.7"
[[ "${POLICY_CHANGED}" == "1" ]]
[[ "${DELETED}" == *"from 10.42.0.9 to 10.42.0.0/16 lookup main"* ]]
[[ "${DELETED}" == *"from 10.42.0.9 lookup 51820"* ]]
[[ "${DELETED}" == *"from 10.42.0.9 blackhole"* ]]
[[ "${DELETED}" != *"from 10.42.1.7"* ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_k3s_cleanup_preserves_blackholes_only_when_tunnel_is_kept():
    result = run_bash(
        r'''
source "$1"

K3S_MODE="true"
SECURE_MODE="false"
DOCKER_TARGET_IP="10.42.1.7"
DELETED=""
FLUSHED=0
RULES=$'32500:\tfrom 10.42.1.7 to 10.42.0.0/16 lookup main\n32764:\tfrom 10.42.1.7 lookup 51820\n32765:\tfrom 10.42.1.7 blackhole\n'

remove_tagged_iptables_rules() {
    return 0
}
ip() {
    if [[ "$*" == "rule show pref 32500" ]]; then
        printf '%s' "${RULES}" | grep '^32500:' || true
        return 0
    fi
    if [[ "$*" == "rule show pref 32764" ]]; then
        printf '%s' "${RULES}" | grep '^32764:' || true
        return 0
    fi
    if [[ "$*" == "rule show pref 32765" ]]; then
        printf '%s' "${RULES}" | grep '^32765:' || true
        return 0
    fi
    if [[ "$1 $2" == "rule del" ]]; then
        DELETED+="$*"$'\n'
        return 0
    fi
    if [[ "$*" == "route flush table 51820" ]]; then
        FLUSHED=$((FLUSHED + 1))
        return 0
    fi
    return 0
}
wg() {
    return 1
}

cleanup_dataplane --keep-tunnel
[[ "${DELETED}" == *"from 10.42.1.7 to 10.42.0.0/16 lookup main"* ]]
[[ "${DELETED}" == *"from 10.42.1.7 lookup 51820"* ]]
[[ "${DELETED}" != *"from 10.42.1.7 blackhole"* ]]
[[ "${FLUSHED}" == "0" ]]

DELETED=""
cleanup_dataplane
[[ "${DELETED}" == *"from 10.42.1.7 blackhole"* ]]
[[ "${FLUSHED}" == "1" ]]
'''
    )

    assert result.returncode == 0, result.stderr
