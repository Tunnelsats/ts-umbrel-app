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
    printf '%s\n' '{"items":[{"metadata":{"name":"lnd-0"},"spec":{"nodeName":"worker-a"},"status":{"phase":"Running","podIP":"10.42.1.7"}}]}'
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
    printf '%s\n' '{"items":[{"metadata":{"name":"cln-0"},"spec":{"nodeName":"worker-b"},"status":{"phase":"Running","podIP":"10.42.2.8"}}]}'
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
