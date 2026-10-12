import os
import subprocess
import tempfile

import pytest


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
ENTRYPOINT_PATH = os.path.join(REPO_ROOT, "scripts", "entrypoint.sh")


def run_bash(script):
    with tempfile.TemporaryDirectory() as temp_dir:
        env = os.environ.copy()
        env["POLICY_TEST_STATE_FILE"] = os.path.join(temp_dir, "state")
        env["DOCKER_POLICY_STATE_FILE"] = os.path.join(
            temp_dir, "docker-policy-sources.json"
        )
        env["IPV6_POLICY_STATE_FILE"] = os.path.join(
            temp_dir, "ipv6-policy-sources.json"
        )
        env["K3S_POLICY_STATE_FILE"] = os.path.join(
            temp_dir, "k3s-policy-state.json"
        )
        env["DECOMMISSION_STATE_FILE"] = os.path.join(
            temp_dir, "decommissioned.json"
        )
        env["K3S_RULE_PROTOCOL"] = "200"
        return subprocess.run(
            ["bash", "-c", script, "policy-test", ENTRYPOINT_PATH],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )


@pytest.mark.parametrize(
    ("k3s_mode", "expected_host", "expected_port"),
    [
        ("false", "127.0.0.1", "9740"),
        ("true", "0.0.0.0", "9739"),
    ],
)
def test_dashboard_listener_defaults_are_safe_for_umbrel_and_compatible_with_k3s(
    k3s_mode, expected_host, expected_port
):
    result = run_bash(
        f'''
K3S_MODE="{k3s_mode}"
unset DASHBOARD_BIND_HOST DASHBOARD_BIND_PORT
source "$1"
[[ "${{DASHBOARD_BIND_HOST}}" == "{expected_host}" ]]
[[ "${{DASHBOARD_BIND_PORT}}" == "{expected_port}" ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_web_role_dispatches_before_any_privileged_dataplane_startup():
    source = open(ENTRYPOINT_PATH, encoding="utf-8").read()

    web_dispatch = source.index('if [[ "${TUNNELSATS_ROLE}" == "web" ]]')
    kill_switch_start = source.index("if ! ensure_reconcile_kill_switch;", web_dispatch)
    assert web_dispatch < kill_switch_start
    assert 'exec python3 /app/server/app.py' in source[web_dispatch:kill_switch_start]


def test_k3s_target_discovery_honors_persisted_node_type():
    result = run_bash(
        r'''
TUNNELSATS_META_PATH="${POLICY_TEST_STATE_FILE}.meta"
printf '%s\n' '{"nodeType":"cln"}' > "${TUNNELSATS_META_PATH}"
source "$1"

LND_K8S_SERVICE="lnd"
CLN_K8S_SERVICE="cln"
LND_K8S_NAMESPACE="default"
CLN_K8S_NAMESPACE="default"
LND_K8S_POD_SELECTOR="app=lnd"
CLN_K8S_POD_SELECTOR="app=cln"

resolve_svc_ip() { printf '%s\n' "10.43.0.10"; }
resolve_k3s_target_pod() { DOCKER_TARGET_IP="10.42.0.10"; return 0; }

detect_k3s_target
[[ "${TARGET_IMPL}" == "cln" ]]
[[ "${TARGET_CONTAINER_NAME}" == "cln" ]]

printf '%s\n' '{"nodeType":"lnd"}' > "${TUNNELSATS_META_PATH}"
detect_k3s_target
[[ "${TARGET_IMPL}" == "lnd" ]]
[[ "${TARGET_CONTAINER_NAME}" == "lnd" ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_daemon_role_owns_private_socket_directory_and_api_lifecycle():
    source = open(ENTRYPOINT_PATH, encoding="utf-8").read()

    assert 'install -d -m 0770 -o 0 -g "${TUNNELSATS_WEB_GID:-1000}" /run/tunnelsats' in source
    assert 'Starting private daemon API' in source
    assert 'chown -R "${TUNNELSATS_WEB_UID:-1000}:${TUNNELSATS_WEB_GID:-1000}" /data' in source


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


def test_startup_kill_switch_covers_docker_subnet_and_secure_candidates():
    result = run_bash(
        r'''
source "$1"

GUARDS=""
ensure_source_blackhole_rule() { GUARDS+="$1:$2"$'\n'; }

K3S_MODE="false"
SECURE_MODE="false"
ensure_reconcile_kill_switch
[[ "${GUARDS}" == $'10.9.9.0/25:32762\n' ]]

GUARDS=""
SECURE_MODE="true"
ensure_reconcile_kill_switch
[[ "${GUARDS}" == $'10.21.21.9:32762\n10.21.21.96:32762\n' ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_docker_endpoint_refresh_discovers_ipv6_address_gateway_and_mac():
    result = run_bash(
        r'''
source "$1"

K3S_MODE="false"
SECURE_MODE="false"
TARGET_CONTAINER_ID="target-id"
TARGET_CONTAINER_NAME="lnd"
DOCKER_POLICY_STATE_LOADED="true"
persist_managed_docker_policy_state() { :; }
docker_api() {
    printf '%s\n' '{"NetworkSettings":{"Networks":{"umbrel":{"IPAddress":"10.21.0.9","IPPrefixLen":16,"Gateway":"10.21.0.1","GwPriority":0,"GlobalIPv6Address":"2001:db8:21::9","GlobalIPv6PrefixLen":64,"IPv6Gateway":"2001:db8:21::1","MacAddress":"02:42:ac:11:00:09"},"docker-tunnelsats":{"IPAddress":"10.9.9.9","IPPrefixLen":25,"Gateway":"10.9.9.1","GwPriority":100,"GlobalIPv6Address":"","IPv6Gateway":"","MacAddress":"02:42:0a:09:09:09"}}}}'
}

refresh_docker_target_endpoints
[[ "${#TARGET_IPV4_ENDPOINTS[@]}" == "2" ]]
[[ "${#TARGET_IPV6_ADDRESSES[@]}" == "1" ]]
[[ "${TARGET_IPV6_ADDRESSES[0]}" == "2001:db8:21::9" ]]
[[ "${TARGET_IPV6_MACS[0]}" == "02:42:ac:11:00:09" ]]
[[ "${TARGET_HAS_IPV6_DEFAULT_ROUTE}" == "true" ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_ipv6_containment_is_idempotent_and_fails_verification_on_route_drift():
    result = run_bash(
        r'''
source "$1"

TARGET_IPV6_ADDRESSES=("2001:db8:42::7")
TARGET_IPV6_MACS=()
TARGET_HAS_IPV6_DEFAULT_ROUTE="true"
IPV6_POLICY_STATE_LOADED="true"
MANAGED_TARGET_IPV6_ADDRESSES=()
RULE_64=""
RULE_65=""
ROUTES=""
FILTER_INSTALLED="false"
ADD_COUNT=0
persist_managed_ipv6_policy_state() { :; }

ip() {
    case "$*" in
        "-6 rule show pref 32764") printf '%s\n' "${RULE_64}" ;;
        "-6 rule show pref 32765") printf '%s\n' "${RULE_65}" ;;
        "-6 rule add from 2001:db8:42::7 blackhole pref 32765")
            RULE_65=$'32765:\tfrom 2001:db8:42::7 blackhole'
            ADD_COUNT=$((ADD_COUNT + 1))
            ;;
        "-6 rule add from 2001:db8:42::7 table 51821 pref 32764")
            RULE_64=$'32764:\tfrom 2001:db8:42::7 lookup 51821'
            ADD_COUNT=$((ADD_COUNT + 1))
            ;;
        "-6 route show table 51821") printf '%s\n' "${ROUTES}" ;;
        "-6 route replace blackhole default metric 42760 table 51821")
            ROUTES="blackhole default metric 42760"
            ADD_COUNT=$((ADD_COUNT + 1))
            ;;
        *) return 1 ;;
    esac
}
ip6tables() {
    if [[ "$*" == "-S FORWARD" ]]; then
        if [ "${FILTER_INSTALLED}" = "true" ]; then
            printf '%s\n' '-A FORWARD -s 2001:db8:42::7/128 ! -d fe80::/10 -m comment --comment tunnelsats-ipv6-deny -j DROP'
        fi
        return 0
    fi
    if [[ "$1" == "-C" ]]; then
        [ "${FILTER_INSTALLED}" = "true" ]
        return
    fi
    if [[ "$1" == "-I" ]]; then
        FILTER_INSTALLED="true"
        ADD_COUNT=$((ADD_COUNT + 1))
        return 0
    fi
    return 1
}

ensure_ipv6_containment
ipv6_containment_is_synced
[[ "${ADD_COUNT}" == "4" ]]
ensure_ipv6_containment
ipv6_containment_is_synced
[[ "${ADD_COUNT}" == "4" ]]

ROUTES=""
if ipv6_containment_is_synced; then exit 1; fi
'''
    )

    assert result.returncode == 0, result.stderr


def test_ipv6_containment_refuses_to_claim_matching_unowned_policy_rule():
    result = run_bash(
        r'''
source "$1"

TARGET_IPV6_ADDRESSES=("2001:db8:42::7")
TARGET_IPV6_MACS=()
TARGET_HAS_IPV6_DEFAULT_ROUTE="true"
IPV6_POLICY_STATE_LOADED="true"
IPV6_POLICY_TABLE_MANAGED="false"
MANAGED_TARGET_IPV6_ADDRESSES=()
DELETE_COUNT=0
persist_managed_ipv6_policy_state() { :; }
ip6tables() {
    case "$1" in
        -C) return 0 ;;
        -S) return 0 ;;
        *) return 1 ;;
    esac
}
ip() {
    case "$*" in
        "-6 rule show pref 32765")
            printf '%s\n' $'32765:\tfrom 2001:db8:42::7 blackhole'
            ;;
        "-6 rule show pref 32764") return 0 ;;
        "-6 rule del"*) DELETE_COUNT=$((DELETE_COUNT + 1)); return 1 ;;
        "-6 route show table 51821") return 0 ;;
        "-6 route replace blackhole default metric 42760 table 51821") return 1 ;;
        *) return 1 ;;
    esac
}

if ensure_ipv6_containment; then exit 1; fi
[[ "${DELETE_COUNT}" == "0" ]]
[[ "${MANAGED_TARGET_IPV6_ADDRESSES[*]}" == "" ]]
[[ "${LAST_ERROR}" == *"unowned rule"* ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_rules_synced_reports_ipv4_and_ipv6_independently():
    result = run_bash(
        r'''
source "$1"
ipv4_rules_are_synced() { return 1; }
ipv6_containment_is_synced() { return 0; }
if rules_are_synced; then exit 1; fi
[[ "${IPV4_RULES_SYNCED}" == "false" ]]
[[ "${IPV6_RULES_SYNCED}" == "true" ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_write_state_reports_ipv6_policy_and_independent_family_status():
    result = run_bash(
        r'''
source "$1"
STATE_FILE="${POLICY_TEST_STATE_FILE}.json"
TARGET_IPV6_ADDRESSES=("2001:db8:42::7" "fd00:42::7")
TARGET_HAS_IPV6_DEFAULT_ROUTE="true"
RULES_SYNCED="false"
IPV4_RULES_SYNCED="false"
IPV6_RULES_SYNCED="true"

write_state
[[ "$(jq -r '.ipv6_policy' "${STATE_FILE}")" == "deny" ]]
[[ "$(jq -r '.ipv4_rules_synced' "${STATE_FILE}")" == "false" ]]
[[ "$(jq -r '.ipv6_rules_synced' "${STATE_FILE}")" == "true" ]]
[[ "$(jq -r '.target_ipv6_default_route' "${STATE_FILE}")" == "true" ]]
[[ "$(jq -r '.target_ipv6_addresses | join(",")' "${STATE_FILE}")" == "2001:db8:42::7,fd00:42::7" ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_docker_attach_sets_priority_above_competing_endpoint():
    result = run_bash(
        r'''
source "$1"

K3S_MODE="false"
SECURE_MODE="false"
TARGET_CONTAINER_ID="target-id"
TARGET_CONTAINER_NAME="lnd"
DOCKER_GW_PRIORITY_SUPPORTED="unknown"

docker_api() {
    local method="$1"
    local path="$2"
    local data="${3:-}"
    case "${method}:${path}" in
        "GET:/version")
            printf '%s\n' '{"ApiVersion":"1.48"}'
            ;;
        "GET:/containers/target-id/json")
            if [ -f "${POLICY_TEST_STATE_FILE}.connected" ]; then
                printf '%s\n' '{"NetworkSettings":{"Networks":{"umbrel":{"IPAddress":"10.21.0.9","IPPrefixLen":16,"Gateway":"10.21.0.1","GwPriority":250},"docker-tunnelsats":{"IPAddress":"10.9.9.9","IPPrefixLen":25,"Gateway":"10.9.9.1","GwPriority":251}}}}'
            else
                printf '%s\n' '{"NetworkSettings":{"Networks":{"umbrel":{"IPAddress":"10.21.0.9","IPPrefixLen":16,"Gateway":"10.21.0.1","GwPriority":250},"docker-tunnelsats":{"IPAddress":"10.9.9.9","IPPrefixLen":25,"Gateway":"10.9.9.1","GwPriority":0}}}}'
            fi
            ;;
        "POST:/networks/docker-tunnelsats/disconnect")
            printf '%s\n' "disconnect" >> "${POLICY_TEST_STATE_FILE}.events"
            ;;
        "POST:/networks/docker-tunnelsats/connect")
            printf '%s' "${data}" > "${POLICY_TEST_STATE_FILE}.payload"
            printf '%s\n' "connect" >> "${POLICY_TEST_STATE_FILE}.events"
            touch "${POLICY_TEST_STATE_FILE}.connected"
            ;;
        *) return 1 ;;
    esac
}

ensure_container_attached
[[ "$(jq -r '.EndpointConfig.GwPriority' "${POLICY_TEST_STATE_FILE}.payload")" == "251" ]]
[[ "$(jq -r '.EndpointConfig.IPAMConfig.IPv4Address' "${POLICY_TEST_STATE_FILE}.payload")" == "10.9.9.9" ]]
[[ "$(cat "${POLICY_TEST_STATE_FILE}.events")" == $'disconnect\nconnect' ]]
[[ "${#TARGET_IPV4_ENDPOINTS[@]}" == "2" ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_docker_attach_uses_legacy_payload_on_pre_148_engine():
    result = run_bash(
        r'''
source "$1"

K3S_MODE="false"
SECURE_MODE="false"
TARGET_CONTAINER_ID="target-id"
TARGET_CONTAINER_NAME="lnd"
DOCKER_GW_PRIORITY_SUPPORTED="unknown"

docker_api() {
    local method="$1"
    local path="$2"
    local data="${3:-}"
    case "${method}:${path}" in
        "GET:/version") printf '%s\n' '{"ApiVersion":"1.47"}' ;;
        "GET:/containers/target-id/json")
            if [ -f "${POLICY_TEST_STATE_FILE}.connected" ]; then
                printf '%s\n' '{"NetworkSettings":{"Networks":{"umbrel":{"IPAddress":"10.21.0.9","IPPrefixLen":16,"Gateway":"10.21.0.1"},"docker-tunnelsats":{"IPAddress":"10.9.9.9","IPPrefixLen":25,"Gateway":"10.9.9.1"}}}}'
            else
                printf '%s\n' '{"NetworkSettings":{"Networks":{"umbrel":{"IPAddress":"10.21.0.9","IPPrefixLen":16,"Gateway":"10.21.0.1"}}}}'
            fi
            ;;
        "POST:/networks/docker-tunnelsats/connect")
            printf '%s' "${data}" > "${POLICY_TEST_STATE_FILE}.payload"
            touch "${POLICY_TEST_STATE_FILE}.connected"
            ;;
        *) return 1 ;;
    esac
}

ensure_container_attached
[[ "$(jq 'has("GwPriority") or (.EndpointConfig | has("GwPriority"))' "${POLICY_TEST_STATE_FILE}.payload")" == "false" ]]
[[ "${DOCKER_GW_PRIORITY_SUPPORTED}" == "false" ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_docker_route_verification_requires_priority_source_and_gateway():
    result = run_bash(
        r'''
source "$1"

K3S_MODE="false"
SECURE_MODE="false"
TARGET_CONTAINER_ID="target-id"
TARGET_CONTAINER_NAME="lnd"
DOCKER_GW_PRIORITY_SUPPORTED="true"
ROUTE_MODE="safe"

refresh_docker_target_endpoints() {
    TARGET_IPV4_ENDPOINTS=(
        "10.9.9.9|10.9.9.0/25|10.9.9.1|1|101"
        "10.21.0.9|10.21.0.0/16|10.21.0.1|0|100"
    )
}
docker_exec_route_get() {
    printf '%s\n' "$1" >> "${POLICY_TEST_STATE_FILE}.probes"
    if [ "${ROUTE_MODE}" = "safe" ]; then
        printf '%s via 10.9.9.1 dev eth1 src 10.9.9.9\n' "$1"
    elif [ "${ROUTE_MODE}" = "wrong-source" ]; then
        printf '%s via 10.21.0.1 dev eth0 src 10.21.0.9\n' "$1"
    else
        printf '%s via 10.9.9.1 dev eth1 src 10.9.9.9\n' "$1"
    fi
}

verify_docker_target_routes
[[ "$(cat "${POLICY_TEST_STATE_FILE}.probes")" == $'1.1.1.1\n8.8.8.8' ]]

ROUTE_MODE="wrong-source"
if verify_docker_target_routes; then exit 1; fi
[[ "${LAST_ERROR}" == *"Unsafe route to 1.1.1.1"* ]]

TARGET_IPV4_ENDPOINTS=()
refresh_docker_target_endpoints() {
    TARGET_IPV4_ENDPOINTS=(
        "10.9.9.9|10.9.9.0/25|10.9.9.1|1|100"
        "10.21.0.9|10.21.0.0/16|10.21.0.1|0|100"
    )
}
if verify_docker_target_routes; then exit 1; fi
[[ "${LAST_ERROR}" == *"does not outrank"* ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_docker_source_policy_protects_every_assigned_ipv4():
    result = run_bash(
        r'''
source "$1"

K3S_MODE="false"
SECURE_MODE="false"
TARGET_CONTAINER_NAME="lnd"
RULES=""
ADD_COUNT=0

refresh_docker_target_endpoints() {
    TARGET_IPV4_ENDPOINTS=(
        "10.9.9.9|10.9.9.0/25|10.9.9.1|1|101"
        "10.21.0.9|10.21.0.0/16|10.21.0.1|0|100"
    )
}
ip() {
    if [[ "$*" == "rule show pref "* ]]; then
        local pref="${4}"
        printf '%s' "${RULES}" | grep "^${pref}:" || true
        return 0
    fi
    if [[ "$1 $2" == "rule add" ]]; then
        ADD_COUNT=$((ADD_COUNT + 1))
        case "$*" in
            "rule add from "*" blackhole pref 32765")
                RULES+="32765:"$'\t'"from $4 blackhole"$'\n'
                ;;
            "rule add from "*" to "*" table main pref 32500")
                RULES+="32500:"$'\t'"from $4 to $6 lookup main"$'\n'
                ;;
            "rule add from "*" table 51820 pref 32764")
                RULES+="32764:"$'\t'"from $4 lookup 51820"$'\n'
                ;;
            *) return 1 ;;
        esac
        return 0
    fi
    return 1
}

ensure_docker_source_policy_rules
[[ "${RULES}" == *$'from 10.9.9.9 to 10.9.9.0/25 lookup main'* ]]
[[ "${RULES}" == *$'from 10.21.0.9 to 10.21.0.0/16 lookup main'* ]]
[[ "${RULES}" == *$'from 10.9.9.9 lookup 51820'* ]]
[[ "${RULES}" == *$'from 10.21.0.9 lookup 51820'* ]]
[[ "${RULES}" == *$'from 10.9.9.9 blackhole'* ]]
[[ "${RULES}" == *$'from 10.21.0.9 blackhole'* ]]
docker_source_policy_rules_are_synced

FIRST_ADD_COUNT="${ADD_COUNT}"
ensure_docker_source_policy_rules
[[ "${ADD_COUNT}" == "${FIRST_ADD_COUNT}" ]]

RULES="${RULES//$'32764:\tfrom 10.21.0.9 lookup 51820\n'/}"
if docker_source_policy_rules_are_synced; then exit 1; fi
'''
    )

    assert result.returncode == 0, result.stderr


def test_stale_docker_source_policy_is_retired_after_network_reconnect():
    result = run_bash(
        r'''
source "$1"

TARGET_IPV4_ENDPOINTS=(
    "10.9.9.9|10.9.9.0/25|10.9.9.1|1|101"
    "10.21.0.10|10.21.0.0/16|10.21.0.1|0|100"
)
MANAGED_DOCKER_IPV4_ENDPOINTS=(
    "10.9.9.9|10.9.9.0/25|10.9.9.1|1|101"
    "10.21.0.9|10.21.0.0/16|10.21.0.1|0|100"
)
DELETED=""
ip() {
    if [[ "$1 $2" == "rule del" ]]; then
        DELETED+="$*"$'\n'
        return 0
    fi
    return 1
}

remove_stale_docker_source_policy_rules
[[ "${DOCKER_STALE_POLICY_CHANGED}" == "1" ]]
[[ "${DELETED}" == *"from 10.21.0.9 to 10.21.0.0/16 table main pref 32500"* ]]
[[ "${DELETED}" == *"from 10.21.0.9 table 51820 pref 32764"* ]]
[[ "${DELETED}" == *"from 10.21.0.9 blackhole pref 32762"* ]]
[[ "${DELETED}" == *"from 10.21.0.9 blackhole pref 32765"* ]]
[[ "${MANAGED_DOCKER_IPV4_ENDPOINTS[*]}" != *"10.21.0.9"* ]]
[[ "${MANAGED_DOCKER_IPV4_ENDPOINTS[*]}" == *"10.21.0.10"* ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_docker_policy_state_recovers_stale_sources_across_entrypoint_restart():
    result = run_bash(
        r'''
source "$1"

DOCKER_POLICY_STATE_LOADED="true"
MANAGED_DOCKER_IPV4_ENDPOINTS=(
    "10.9.9.9|10.9.9.0/25|10.9.9.1|1|101"
    "10.21.0.9|10.21.0.0/16|10.21.0.1|0|100"
)
persist_managed_docker_policy_state
[[ "$(stat -c '%a' "${DOCKER_POLICY_STATE_FILE}")" == "600" ]]

# Simulate a fresh entrypoint process after Docker reassigned the ordinary IP.
DOCKER_POLICY_STATE_LOADED="false"
MANAGED_DOCKER_IPV4_ENDPOINTS=()
load_managed_docker_policy_state
[[ "${MANAGED_DOCKER_IPV4_ENDPOINTS[*]}" == *"10.21.0.9"* ]]

TARGET_IPV4_ENDPOINTS=(
    "10.9.9.9|10.9.9.0/25|10.9.9.1|1|501"
    "10.21.0.10|10.21.0.0/16|10.21.0.1|0|500"
)
DELETED=""
ip() {
    if [[ "$1 $2" == "rule del" ]]; then
        DELETED+="$*"$'\n'
        return 0
    fi
    return 1
}
remove_stale_docker_source_policy_rules
[[ "${DELETED}" == *"from 10.21.0.9 table 51820 pref 32764"* ]]

# A second restart sees only the current ownership set.
DOCKER_POLICY_STATE_LOADED="false"
MANAGED_DOCKER_IPV4_ENDPOINTS=()
load_managed_docker_policy_state
[[ "${MANAGED_DOCKER_IPV4_ENDPOINTS[*]}" != *"10.21.0.9"* ]]
[[ "${MANAGED_DOCKER_IPV4_ENDPOINTS[*]}" == *"10.21.0.10"* ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_detection_failure_is_guarded_before_discovery_and_stays_fail_closed():
    result = run_bash(
        r'''
source "$1"

K3S_MODE="false"
SECURE_MODE="true"
EVENTS=""

ensure_source_blackhole_rule() { EVENTS+="guard:$1"$'\n'; }
detect_lightning_container() { EVENTS+="detect"$'\n'; return 1; }
cleanup_dataplane() { EVENTS+="cleanup:$1"$'\n'; }
write_state() { EVENTS+="state:${RULES_SYNCED}"$'\n'; }

if reconcile_once "test"; then exit 1; fi

[[ "${EVENTS}" == $'guard:10.21.21.9\nguard:10.21.21.96\ndetect\nguard:10.21.21.9\nguard:10.21.21.96\ncleanup:--keep-tunnel\nguard:10.21.21.9\nguard:10.21.21.96\nstate:false\n' ]]
'''
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "failed_stage",
    ["docker_network", "container_attach", "bridge", "wireguard", "policy", "nat"],
)
def test_each_reconciliation_failure_restores_the_kill_switch(failed_stage):
    result = run_bash(
        rf'''
source "$1"

K3S_MODE="false"
SECURE_MODE="true"
FAILED_STAGE="{failed_stage}"
GUARD_COUNT=0
CLEANUP_COUNT=0

ensure_source_blackhole_rule() {{ GUARD_COUNT=$((GUARD_COUNT + 1)); }}
detect_lightning_container() {{ DOCKER_TARGET_IP="10.21.21.9"; TARGET_CONTAINER_NAME="lnd"; return 0; }}
stage() {{ [[ "${{FAILED_STAGE}}" != "$1" ]]; }}
ensure_docker_network() {{ stage docker_network; }}
ensure_container_attached() {{ stage container_attach; }}
resolve_bridge_name() {{ stage bridge; }}
ensure_wg_up() {{ stage wireguard; }}
wireguard_handshake_is_fresh() {{ return 0; }}
ensure_policy_routing() {{ POLICY_CHANGED=0; stage policy; }}
ensure_nat_forward_rules() {{ NAT_CHANGED=0; stage nat; }}
rules_are_synced() {{ return 0; }}
cleanup_dataplane() {{ [[ "$1" == "--keep-tunnel" ]]; CLEANUP_COUNT=$((CLEANUP_COUNT + 1)); }}
write_state() {{ :; }}

if reconcile_once "test"; then exit 1; fi
[[ "${{CLEANUP_COUNT}}" == "1" ]]
[[ "${{GUARD_COUNT}}" == "6" ]]
[[ "${{RULES_SYNCED}}" == "false" ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_failed_precleanup_guard_still_cleans_partial_dataplane_and_retries_guard():
    result = run_bash(
        r'''
source "$1"

K3S_MODE="false"
SECURE_MODE="true"
GUARD_COUNT=0
CLEANUP_COUNT=0
STATE_ERROR=""

ensure_reconcile_kill_switch() {
    GUARD_COUNT=$((GUARD_COUNT + 1))
    if [ "${GUARD_COUNT}" = "1" ]; then
        LAST_ERROR="pre-cleanup guard failed"
        return 1
    fi
    return 0
}
cleanup_dataplane() {
    [[ "$1" == "--keep-tunnel" ]]
    CLEANUP_COUNT=$((CLEANUP_COUNT + 1))
}
write_state() { STATE_ERROR="${LAST_ERROR}"; }

LAST_ERROR="NAT staging failed"
if fail_reconcile_closed; then exit 1; fi

[[ "${CLEANUP_COUNT}" == "1" ]]
[[ "${GUARD_COUNT}" == "2" ]]
[[ "${STATE_ERROR}" == *"NAT staging failed"* ]]
[[ "${STATE_ERROR}" == *"pre-cleanup kill switch warning: pre-cleanup guard failed"* ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_stale_wireguard_handshake_never_activates_policy_routing():
    result = run_bash(
        r'''
source "$1"

K3S_MODE="false"
SECURE_MODE="true"
POLICY_COUNT=0
CLEANUP_COUNT=0

ensure_source_blackhole_rule() { :; }
detect_lightning_container() { DOCKER_TARGET_IP="10.21.21.9"; return 0; }
ensure_docker_network() { return 0; }
ensure_container_attached() { return 0; }
resolve_bridge_name() { return 0; }
ensure_wg_up() { return 0; }
wireguard_handshake_is_fresh() { LAST_ERROR="WireGuard handshake is stale"; return 1; }
ensure_policy_routing() { POLICY_COUNT=$((POLICY_COUNT + 1)); return 0; }
cleanup_dataplane() { CLEANUP_COUNT=$((CLEANUP_COUNT + 1)); }
write_state() { :; }

if reconcile_once "test"; then exit 1; fi
[[ "${POLICY_COUNT}" == "0" ]]
[[ "${CLEANUP_COUNT}" == "1" ]]
[[ "${LAST_ERROR}" == *"handshake is stale"* ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_unsynced_dataplane_is_requarantined_and_reported_as_failure():
    result = run_bash(
        r'''
source "$1"

K3S_MODE="false"
SECURE_MODE="true"
CLEANUP_COUNT=0
RELEASE_COUNT=0

ensure_source_blackhole_rule() { :; }
detect_lightning_container() { DOCKER_TARGET_IP="10.21.21.9"; return 0; }
ensure_docker_network() { return 0; }
ensure_container_attached() { return 0; }
resolve_bridge_name() { return 0; }
ensure_wg_up() { return 0; }
wireguard_handshake_is_fresh() { return 0; }
ensure_policy_routing() { POLICY_CHANGED=0; return 0; }
ensure_nat_forward_rules() { NAT_CHANGED=0; return 0; }
rules_are_synced() { return 1; }
remove_reconcile_kill_switch() { RELEASE_COUNT=$((RELEASE_COUNT + 1)); }
cleanup_dataplane() { CLEANUP_COUNT=$((CLEANUP_COUNT + 1)); }
write_state() { :; }

if reconcile_once "test"; then exit 1; fi
[[ "${RELEASE_COUNT}" == "0" ]]
[[ "${CLEANUP_COUNT}" == "1" ]]
[[ "${RULES_SYNCED}" == "false" ]]
[[ "${LAST_ERROR}" == "Dataplane rules are not fully synced" ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_guard_is_released_only_after_staged_and_active_verification():
    result = run_bash(
        r'''
source "$1"

K3S_MODE="false"
SECURE_MODE="true"
VERIFY_COUNT=0
RELEASE_COUNT=0
CLEANUP_COUNT=0

ensure_source_blackhole_rule() { :; }
detect_lightning_container() { DOCKER_TARGET_IP="10.21.21.9"; TARGET_CONTAINER_NAME="lnd"; return 0; }
ensure_docker_network() { return 0; }
ensure_container_attached() { return 0; }
resolve_bridge_name() { return 0; }
ensure_wg_up() { return 0; }
wireguard_handshake_is_fresh() { return 0; }
ensure_policy_routing() { POLICY_CHANGED=1; return 0; }
ensure_nat_forward_rules() { NAT_CHANGED=1; return 0; }
rules_are_synced() { VERIFY_COUNT=$((VERIFY_COUNT + 1)); return 0; }
remove_reconcile_kill_switch() { RELEASE_COUNT=$((RELEASE_COUNT + 1)); return 0; }
cleanup_dataplane() { CLEANUP_COUNT=$((CLEANUP_COUNT + 1)); }
write_state() { :; }

reconcile_once "test"
[[ "${VERIFY_COUNT}" == "2" ]]
[[ "${RELEASE_COUNT}" == "1" ]]
[[ "${CLEANUP_COUNT}" == "0" ]]
[[ "${RULES_SYNCED}" == "true" ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_wireguard_handshake_freshness_rejects_missing_and_stale_values():
    result = run_bash(
        r'''
source "$1"

HANDSHAKE=0
wg() { printf 'peer-key\t%s\n' "${HANDSHAKE}"; }
date() { printf '%s\n' 1000; }

if wireguard_handshake_is_fresh; then exit 1; fi
[[ "${LAST_ERROR}" == *"no completed handshake"* ]]
HANDSHAKE=819
if wireguard_handshake_is_fresh; then exit 1; fi
[[ "${LAST_ERROR}" == *"handshake is stale"* ]]
HANDSHAKE=820
wireguard_handshake_is_fresh
'''
    )

    assert result.returncode == 0, result.stderr


def test_runtime_wireguard_config_adds_keepalive_for_legacy_peer_idempotently():
    result = run_bash(
        r'''
source "$1"

CONFIG_FILE="${POLICY_TEST_STATE_FILE}.conf"
cat > "${CONFIG_FILE}" <<'EOF_CONFIG'
[Interface]
PrivateKey = client-key

[Peer]
PublicKey = server-key
AllowedIPs = 0.0.0.0/0
Endpoint = vpn.example.test:51820
EOF_CONFIG

ensure_runtime_wireguard_keepalive "${CONFIG_FILE}"
[[ "$(grep -c '^PersistentKeepalive = 25$' "${CONFIG_FILE}")" == "1" ]]

ensure_runtime_wireguard_keepalive "${CONFIG_FILE}"
[[ "$(grep -c '^PersistentKeepalive = 25$' "${CONFIG_FILE}")" == "1" ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_fail_closed_policy_prerequisites_require_both_blackholes_and_wg_default():
    result = run_bash(
        r'''
source "$1"

K3S_MODE="false"
SECURE_MODE="true"
DOCKER_TARGET_IP="10.21.21.9"
RULES=$'32765:\tfrom 10.21.21.9 blackhole\n'
ROUTES=$'default dev tunnelsatsv2 metric 2\nblackhole default metric 3\n'

ip() {
    if [[ "$*" == "rule show pref 32765" ]]; then printf '%s' "${RULES}"; return 0; fi
    if [[ "$*" == "route show table 51820" ]]; then printf '%s' "${ROUTES}"; return 0; fi
    return 1
}

fail_closed_policy_prerequisites_are_synced
RULES=""
if fail_closed_policy_prerequisites_are_synced; then exit 1; fi
RULES=$'32765:\tfrom 10.21.21.9 blackhole\n'
ROUTES=$'default dev tunnelsatsv2 metric 2\n'
if fail_closed_policy_prerequisites_are_synced; then exit 1; fi
ROUTES=$'blackhole default metric 3\n'
if fail_closed_policy_prerequisites_are_synced; then exit 1; fi
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

ensure_k3s_egress_guard_additive() { return 0; }
ensure_fallback_blackhole_rule() { return 0; }
ensure_ipv6_source_blackhole_rule() { return 0; }
resolve_svc_ip() {
    printf '%s\n' "10.43.0.10"
}
k8s_api() {
    printf '%s\n' '{"items":[{"metadata":{"name":"lnd-remote"},"spec":{"nodeName":"worker-b"},"status":{"phase":"Running","podIP":"10.42.2.9","conditions":[{"type":"Ready","status":"True"}]}},{"metadata":{"name":"lnd-unready"},"spec":{"nodeName":"worker-a"},"status":{"phase":"Running","podIP":"10.42.1.5","conditions":[{"type":"Ready","status":"False"}]}},{"metadata":{"name":"lnd-terminating","deletionTimestamp":"2026-07-31T08:00:00Z"},"spec":{"nodeName":"worker-a"},"status":{"phase":"Running","podIP":"10.42.1.6","conditions":[{"type":"Ready","status":"True"}]}},{"metadata":{"name":"lnd-local"},"spec":{"nodeName":"worker-a"},"status":{"phase":"Running","podIP":"10.42.1.7","podIPs":[{"ip":"10.42.1.7"},{"ip":"2001:db8:42::7"}],"conditions":[{"type":"Ready","status":"True"}]}}]}'
}

detect_k3s_target
[[ "${TARGET_IMPL}" == "lnd" ]]
[[ "${TARGET_CONTAINER_NAME}" == "lnd" ]]
[[ "${DOCKER_TARGET_IP}" == "10.42.1.7" ]]
[[ "${TARGET_IPV6_ADDRESSES[0]}" == "2001:db8:42::7" ]]
[[ "${TARGET_HAS_IPV6_DEFAULT_ROUTE}" == "true" ]]
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

ensure_k3s_egress_guard_additive() { return 0; }
ensure_fallback_blackhole_rule() { return 0; }
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


def test_k3s_wireguard_failure_blackholes_new_target_before_returning():
    result = run_bash(
        r'''
source "$1"

K3S_MODE="true"
SECURE_MODE="false"
BLACKHOLE_COUNT=0
POLICY_COUNT=0
STATE_ERROR=""
STATE_SYNCED=""

detect_lightning_container() {
    TARGET_CONTAINER_NAME="lnd"
    TARGET_IMPL="lnd"
    DOCKER_TARGET_IP="10.42.1.7"
    return 0
}
ensure_docker_network() { return 0; }
ensure_container_attached() { return 0; }
resolve_bridge_name() { return 0; }
ensure_fallback_blackhole_rule() {
    [[ "$1" == "10.42.1.7" ]]
    BLACKHOLE_COUNT=$((BLACKHOLE_COUNT + 1))
    BLACKHOLE_CHANGED="1"
    return 0
}
ensure_wg_up() {
    LAST_ERROR="Failed to bring up WireGuard"
    return 1
}
ensure_policy_routing() {
    POLICY_COUNT=$((POLICY_COUNT + 1))
    return 0
}
write_state() {
    STATE_ERROR="${LAST_ERROR}"
    STATE_SYNCED="${RULES_SYNCED}"
}

if reconcile_once "test"; then
    exit 1
fi

[[ "${BLACKHOLE_COUNT}" == "1" ]]
[[ "${POLICY_COUNT}" == "0" ]]
[[ "${STATE_SYNCED}" == "false" ]]
[[ "${STATE_ERROR}" == "Failed to bring up WireGuard" ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_k3s_blackhole_failure_keeps_emergency_egress_guard():
    result = run_bash(
        r'''
source "$1"

K3S_MODE="true"
SECURE_MODE="false"
GUARD_COUNT=0
GUARD_REMOVE_COUNT=0
WG_UP_COUNT=0
STATE_ERROR=""

detect_lightning_container() {
    TARGET_CONTAINER_NAME="lnd"
    TARGET_IMPL="lnd"
    DOCKER_TARGET_IP="10.42.1.7"
    return 0
}
ensure_k3s_egress_guard() {
    [[ "$1" == "10.42.1.7" ]]
    GUARD_COUNT=$((GUARD_COUNT + 1))
    return 0
}
remove_k3s_egress_guards() {
    GUARD_REMOVE_COUNT=$((GUARD_REMOVE_COUNT + 1))
    return 0
}
ensure_fallback_blackhole_rule() {
    LAST_ERROR="k3s: Failed to protect selected pod before WireGuard startup"
    return 1
}
ensure_wg_up() {
    WG_UP_COUNT=$((WG_UP_COUNT + 1))
    return 0
}
write_state() {
    STATE_ERROR="${LAST_ERROR}"
}

if reconcile_once "test"; then
    exit 1
fi

[[ "${GUARD_COUNT}" == "1" ]]
[[ "${GUARD_REMOVE_COUNT}" == "0" ]]
[[ "${WG_UP_COUNT}" == "0" ]]
[[ "${STATE_ERROR}" == *"Failed to protect selected pod"* ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_k3s_egress_guard_prunes_stale_recycled_ip_rules():
    result = run_bash(
        r'''
source "$1"

RULES=$'-A FORWARD -s 10.42.1.7/32 -m comment --comment tunnelsats-k3s-egress-guard -j DROP\n-A FORWARD -s 10.42.1.6/32 -m comment --comment tunnelsats-k3s-egress-guard -j DROP\n-A FORWARD -s 10.42.1.5/32 -m comment --comment foreign-guard -j DROP'
DELETED=""

iptables() {
    if [[ "$*" == "-S FORWARD" ]]; then
        printf '%s\n' "${RULES}"
        return 0
    fi
    if [[ "$*" == "-C FORWARD -s 10.42.1.7 -m comment --comment tunnelsats-k3s-egress-guard -j DROP" ]]; then
        return 0
    fi
    if [[ "$1 $2" == "-D FORWARD" ]]; then
        DELETED+="$*"$'\n'
        RULES="${RULES//$'-A FORWARD -s 10.42.1.6/32 -m comment --comment tunnelsats-k3s-egress-guard -j DROP\n'/}"
        return 0
    fi
    return 1
}

ensure_k3s_egress_guard "10.42.1.7"
[[ "${DELETED}" == *"-D FORWARD -s 10.42.1.6/32"* ]]
[[ "${DELETED}" != *"10.42.1.7"* ]]
[[ "${DELETED}" != *"foreign-guard"* ]]
[[ "${RULES}" == *"10.42.1.7/32"* ]]
[[ "${RULES}" == *"foreign-guard"* ]]
[[ "${RULES}" != *"10.42.1.6/32 -m comment --comment tunnelsats-k3s-egress-guard"* ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_k3s_isolation_failure_quarantines_target_pod_cidr():
    result = run_bash(
        r'''
source "$1"

K3S_MODE="true"
SECURE_MODE="false"
SUBNET_QUARANTINE_COUNT=0
STATE_ERROR=""

detect_lightning_container() {
    TARGET_CONTAINER_NAME="lnd"
    TARGET_IMPL="lnd"
    DOCKER_TARGET_IP="10.42.1.7"
    return 0
}
ensure_k3s_egress_guard() { return 1; }
ensure_fallback_blackhole_rule() {
    LAST_ERROR="k3s: Failed to protect selected pod before WireGuard startup"
    return 1
}
ensure_k3s_subnet_quarantine() {
    [[ "$1" == "10.42.1.7" ]]
    SUBNET_QUARANTINE_COUNT=$((SUBNET_QUARANTINE_COUNT + 1))
    K3S_QUARANTINE_CIDR="10.42.0.0/16"
    return 0
}
write_state() {
    STATE_ERROR="${LAST_ERROR}"
}

if reconcile_once "test"; then
    exit 1
fi

[[ "${SUBNET_QUARANTINE_COUNT}" == "1" ]]
[[ "${STATE_ERROR}" == *"quarantined pod CIDR"* ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_k3s_total_isolation_failure_deletes_target_pod():
    result = run_bash(
        r'''
source "$1"

K3S_MODE="true"
SECURE_MODE="false"
DELETE_COUNT=0
NETWORK_POLICY_COUNT=0
STATE_ERROR=""

detect_lightning_container() {
    TARGET_CONTAINER_NAME="lnd"
    TARGET_IMPL="lnd"
    DOCKER_TARGET_IP="10.42.1.7"
    K3S_TARGET_POD_NAMESPACE="lightning"
    K3S_TARGET_POD_NAME="lnd-0"
    return 0
}
ensure_k3s_egress_guard() { return 1; }
ensure_fallback_blackhole_rule() {
    LAST_ERROR="k3s: Failed to protect selected pod before WireGuard startup"
    return 1
}
ensure_k3s_subnet_quarantine() { return 1; }
ensure_k3s_emergency_network_policy() {
    NETWORK_POLICY_COUNT=$((NETWORK_POLICY_COUNT + 1))
    return 0
}
delete_k3s_target_pod() {
    [[ "${K3S_TARGET_POD_NAMESPACE}/${K3S_TARGET_POD_NAME}" == "lightning/lnd-0" ]]
    DELETE_COUNT=$((DELETE_COUNT + 1))
    return 0
}
write_state() {
    STATE_ERROR="${LAST_ERROR}"
}

if reconcile_once "test"; then
    exit 1
fi

[[ "${DELETE_COUNT}" == "1" ]]
[[ "${NETWORK_POLICY_COUNT}" == "1" ]]
[[ "${STATE_ERROR}" == *"deleted target pod lightning/lnd-0 to fail closed"* ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_k3s_total_isolation_failure_retries_until_target_is_contained():
    result = run_bash(
        r'''
source "$1"

K3S_MODE="true"
SECURE_MODE="false"
DELETE_COUNT=0
GUARD_COUNT=0
SLEEP_COUNT=0
STATE_ERROR=""

detect_lightning_container() {
    TARGET_CONTAINER_NAME="lnd"
    TARGET_IMPL="lnd"
    DOCKER_TARGET_IP="10.42.1.7"
    K3S_TARGET_POD_NAMESPACE="lightning"
    K3S_TARGET_POD_NAME="lnd-0"
    return 0
}
ensure_k3s_egress_guard() {
    GUARD_COUNT=$((GUARD_COUNT + 1))
    [[ "${GUARD_COUNT}" -ge 3 ]]
}
ensure_fallback_blackhole_rule() {
    LAST_ERROR="k3s: Failed to protect selected pod before WireGuard startup"
    return 1
}
ensure_k3s_subnet_quarantine() { return 1; }
ensure_k3s_emergency_network_policy() { return 1; }
delete_k3s_target_pod() {
    DELETE_COUNT=$((DELETE_COUNT + 1))
    return 1
}
sleep() { SLEEP_COUNT=$((SLEEP_COUNT + 1)); }
write_state() { STATE_ERROR="${LAST_ERROR}"; }

if reconcile_once "test"; then
    exit 1
fi

[[ "${GUARD_COUNT}" == "3" ]]
[[ "${DELETE_COUNT}" == "3" ]]
[[ "${SLEEP_COUNT}" == "0" ]]
[[ "${STATE_ERROR}" == *"egress guard installed after retry"* ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_k3s_deleted_target_waits_for_replacement_wide_isolation():
    result = run_bash(
        r'''
source "$1"

DOCKER_TARGET_IP="10.42.1.7"
K3S_TARGET_POD_NAMESPACE="lightning"
K3S_TARGET_POD_NAME="lnd-0"
DELETE_COUNT=0
GUARD_COUNT=0
QUARANTINE_COUNT=0

delete_k3s_target_pod() {
    DELETE_COUNT=$((DELETE_COUNT + 1))
    return 0
}
ensure_k3s_egress_guard() {
    GUARD_COUNT=$((GUARD_COUNT + 1))
    return 0
}
ensure_fallback_blackhole_rule() { return 0; }
ensure_k3s_subnet_quarantine() {
    QUARANTINE_COUNT=$((QUARANTINE_COUNT + 1))
    [[ "${QUARANTINE_COUNT}" -ge 2 ]]
}
ensure_k3s_emergency_network_policy() { return 1; }
write_state() { :; }

wait_for_k3s_emergency_isolation "initial isolation failure"
[[ "${DELETE_COUNT}" == "1" ]]
[[ "${GUARD_COUNT}" == "0" ]]
[[ "${QUARANTINE_COUNT}" == "2" ]]
[[ "${LAST_ERROR}" == *"pod CIDR quarantined after retry 2"* ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_k3s_emergency_network_policy_covers_replacement_pods_until_release():
    result = run_bash(
        r'''
source "$1"

K3S_TARGET_POD_NAMESPACE="lightning"
K3S_TARGET_POD_SELECTOR="app=lnd"
K3S_TARGET_POD_LABELS='{"app":"lnd","controller-revision-hash":"lnd-7f8d"}'
PAYLOAD_FILE="${POLICY_TEST_STATE_FILE}.network-policy"
DELETE_FILE="${POLICY_TEST_STATE_FILE}.network-policy-deleted"

k8s_api_write_status() {
    if [[ "$1" == "POST" ]]; then
        [[ "$2" == "/apis/networking.k8s.io/v1/namespaces/lightning/networkpolicies" ]]
        printf '%s' "$3" > "${PAYLOAD_FILE}"
        printf '%s' "201"
        return 0
    fi
    if [[ "$1" == "DELETE" ]]; then
        [[ "$2" == "/apis/networking.k8s.io/v1/namespaces/lightning/networkpolicies/tunnelsats-emergency-egress-deny" ]]
        [[ "$(printf '%s' "$3" | jq -r '.preconditions.uid')" == "owned-policy-uid" ]]
        touch "${DELETE_FILE}"
        printf '%s' "200"
        return 0
    fi
    if [[ "$1" == "GET" && -f "${DELETE_FILE}" ]]; then
        printf '%s' "404"
        return 0
    fi
    return 1
}
k8s_api() {
    if [[ -f "${DELETE_FILE}" ]]; then
        return 1
    fi
    jq '.metadata.uid = "owned-policy-uid"' "${PAYLOAD_FILE}"
}

ensure_k3s_emergency_network_policy
cat "${PAYLOAD_FILE}" | jq -e '
    .spec.podSelector.matchLabels.app == "lnd"
    and (.spec.podSelector.matchLabels | has("controller-revision-hash") | not)
    and .spec.policyTypes == ["Egress"]
    and .spec.egress == []
' >/dev/null

remove_k3s_emergency_network_policy
[[ -f "${DELETE_FILE}" ]]

# If DELETE returns 202 (pending finalizer) and the owned policy remains present,
# remove_k3s_emergency_network_policy must return non-zero rather than reporting success.
rm -f "${DELETE_FILE}"
K3S_NETPOL_DELETE_WAIT_ATTEMPTS=2
K3S_NETPOL_DELETE_WAIT_INTERVAL=0
k8s_api_write_status() {
    if [[ "$1" == "DELETE" ]]; then
        printf '%s' "202"
        return 0
    fi
    return 1
}
k8s_api() {
    jq '.metadata.uid = "owned-policy-uid"' "${PAYLOAD_FILE}"
}
if remove_k3s_emergency_network_policy; then
    exit 1
fi
[[ "${LAST_ERROR}" == *"still present after deletion"* ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_k3s_emergency_network_policy_release_preserves_foreign_fixed_name():
    result = run_bash(
        r'''
source "$1"

K3S_TARGET_POD_NAMESPACE="lightning"
K3S_TARGET_POD_SELECTOR="app=lnd"
DELETE_FILE="${POLICY_TEST_STATE_FILE}.foreign-policy-deleted"

k8s_api() {
    printf '%s' '{"metadata":{"name":"tunnelsats-emergency-egress-deny","uid":"foreign-uid","annotations":{"managed-by":"another-controller"}},"spec":{"podSelector":{"matchLabels":{"app":"other"}},"policyTypes":["Egress"],"egress":[]}}'
}
k8s_api_write_status() {
    if [[ "$1" == "DELETE" ]]; then
        touch "${DELETE_FILE}"
    fi
    printf '%s' "200"
}

remove_k3s_emergency_network_policy
[[ ! -f "${DELETE_FILE}" ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_k3s_emergency_network_policy_uid_conflict_requires_retry():
    result = run_bash(
        r'''
source "$1"

K3S_TARGET_POD_NAMESPACE="lightning"
K3S_TARGET_POD_SELECTOR="app=lnd"

k8s_api() {
    printf '%s' '{"metadata":{"name":"tunnelsats-emergency-egress-deny","uid":"owned-uid","annotations":{"tunnelsats.io/emergency-egress-deny":"true"}},"spec":{"podSelector":{"matchLabels":{"app":"lnd"}},"policyTypes":["Egress"],"egress":[]}}'
}
k8s_api_write_status() {
    [[ "$1" == "DELETE" ]]
    printf '%s' "409"
}

if remove_k3s_emergency_network_policy; then
    exit 1
fi
[[ "${LAST_ERROR}" == *"changed during deletion; retrying cleanup"* ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_k3s_guard_release_clears_stale_guards_after_revalidation():
    result = run_bash(
        r'''
source "$1"

DOCKER_TARGET_IP="10.42.1.7"
SUBNET_REMOVE_COUNT=0
GUARD_REMOVE_COUNT=0
SYNC_COUNT=0

remove_k3s_subnet_quarantine() {
    SUBNET_REMOVE_COUNT=$((SUBNET_REMOVE_COUNT + 1))
    return 0
}
rules_are_synced() {
    SYNC_COUNT=$((SYNC_COUNT + 1))
    return 0
}
remove_k3s_egress_guards() {
    GUARD_REMOVE_COUNT=$((GUARD_REMOVE_COUNT + 1))
    return 0
}

release_k3s_reconcile_guards
[[ "${SUBNET_REMOVE_COUNT}" == "1" ]]
[[ "${SYNC_COUNT}" == "1" ]]
[[ "${GUARD_REMOVE_COUNT}" == "1" ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_k3s_guard_release_retries_emergency_policy_cleanup():
    result = run_bash(
        r'''
source "$1"

DOCKER_TARGET_IP="10.42.1.7"
POLICY_REMOVE_COUNT=0
SLEEP_COUNT=0
STATE_ERROR=""

remove_k3s_subnet_quarantine() { return 0; }
rules_are_synced() { return 0; }
remove_k3s_egress_guards() { return 0; }
remove_k3s_emergency_network_policy() {
    POLICY_REMOVE_COUNT=$((POLICY_REMOVE_COUNT + 1))
    [[ "${POLICY_REMOVE_COUNT}" -ge 2 ]]
}
write_state() { STATE_ERROR="${LAST_ERROR}"; }
sleep() { SLEEP_COUNT=$((SLEEP_COUNT + 1)); }

release_k3s_reconcile_guards
[[ "${POLICY_REMOVE_COUNT}" == "2" ]]
[[ "${SLEEP_COUNT}" == "1" ]]
[[ "${STATE_ERROR}" == *"retrying emergency NetworkPolicy cleanup"* ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_k3s_guard_release_restores_quarantine_when_revalidation_fails():
    result = run_bash(
        r'''
source "$1"

DOCKER_TARGET_IP="10.42.1.7"
RESTORE_COUNT=0
GUARD_REMOVE_COUNT=0

remove_k3s_subnet_quarantine() { return 0; }
rules_are_synced() { return 1; }
ensure_k3s_subnet_quarantine() {
    [[ "$1" == "10.42.1.7" ]]
    RESTORE_COUNT=$((RESTORE_COUNT + 1))
    K3S_QUARANTINE_CIDR="10.42.0.0/16"
    return 0
}
remove_k3s_egress_guards() {
    GUARD_REMOVE_COUNT=$((GUARD_REMOVE_COUNT + 1))
    return 0
}

if release_k3s_reconcile_guards; then
    exit 1
fi
[[ "${RESTORE_COUNT}" == "1" ]]
[[ "${GUARD_REMOVE_COUNT}" == "0" ]]
[[ "${LAST_ERROR}" == *"failed after releasing emergency quarantine"* ]]
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
POLICY_ACTIVE=0
ensure_k3s_egress_guard_additive() { return 0; }
ensure_fallback_blackhole_rule() { return 0; }
k8s_api() {
    if [[ "$1" == "/apis/networking.k8s.io/v1/namespaces/lightning/networkpolicies/tunnelsats-emergency-egress-deny" ]]; then
        if [[ "${POLICY_ACTIVE}" == "1" ]]; then
            printf '%s\n' '{"metadata":{"name":"tunnelsats-emergency-egress-deny","annotations":{"tunnelsats.io/emergency-egress-deny":"true"}},"spec":{"podSelector":{"matchLabels":{"app":"lnd"}},"policyTypes":["Egress"],"egress":[]}}'
            return 0
        fi
        return 1
    fi
    printf '%s\n' '{"items":[{"metadata":{"name":"lnd-unready","labels":{"app":"lnd"}},"spec":{"nodeName":"worker-a"},"status":{"phase":"Running","podIP":"10.42.1.5","conditions":[{"type":"Ready","status":"False"}]}},{"metadata":{"name":"lnd-terminating","deletionTimestamp":"2026-07-31T08:00:00Z"},"spec":{"nodeName":"worker-a"},"status":{"phase":"Running","podIP":"10.42.1.6","conditions":[{"type":"Ready","status":"True"}]}}]}'
}

if resolve_k3s_target_pod "lnd" "lightning" "app=lnd"; then
    exit 1
fi
[[ "${LAST_ERROR}" == *"No Ready non-terminating LND pod is co-located"* ]]
[[ "${LAST_ERROR}" == *"TunnelSats node=worker-a"* ]]

POLICY_ACTIVE=1
LAST_ERROR=""
resolve_k3s_target_pod "lnd" "lightning" "app=lnd"
[[ "${DOCKER_TARGET_IP}" == "10.42.1.5" ]]
[[ "${K3S_TARGET_POD_NAME}" == "lnd-unready" ]]
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
[[ "${LAST_ERROR}" == "Invalid K3S_BYPASS_CIDRS: default route 0.0.0.0/0 is not allowed" ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_k3s_rule_protocol_is_persisted_and_avoids_shared_host_tags():
    result = run_bash(
        r'''
source "$1"

unset K3S_RULE_PROTOCOL
K3S_MODE="true"
K3S_RULE_PROTOCOL_FILE="${POLICY_TEST_STATE_FILE}.protocol"
ip() {
    if [[ "$*" == "-N rule show" ]]; then
        printf '%s\n' $'32764:\tfrom 10.88.0.5 lookup 51820 proto 200'
        return 0
    fi
    return 1
}

initialize_k3s_rule_protocol
[[ "${K3S_RULE_PROTOCOL}" =~ ^[0-9]+$ ]]
[[ "${K3S_RULE_PROTOCOL}" != "200" ]]
[[ "$(awk '{print $2}' "${K3S_RULE_PROTOCOL_FILE}")" == "${K3S_RULE_PROTOCOL}" ]]
[[ "$(awk '{print $3}' "${K3S_RULE_PROTOCOL_FILE}")" == "${K3S_RULE_PREF_BASE}" ]]
[[ "${K3S_RULE_PREF_BASE}" != "32500" ]]
[[ "${K3S_BLACKHOLE_RULE_PREF}" -lt 32766 ]]

FIRST_PROTOCOL="${K3S_RULE_PROTOCOL}"
FIRST_PREF_BASE="${K3S_RULE_PREF_BASE}"
unset K3S_RULE_PROTOCOL
unset K3S_RULE_PREF_BASE
initialize_k3s_rule_protocol
[[ "${K3S_RULE_PROTOCOL}" == "${FIRST_PROTOCOL}" ]]
[[ "${K3S_RULE_PREF_BASE}" == "${FIRST_PREF_BASE}" ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_k3s_rule_ownership_requires_boot_scoped_protocol_and_preference():
    result = run_bash(
        r'''
source "$1"

K3S_RULE_PROTOCOL="200"
K3S_BYPASS_RULE_PREF="32500"
K3S_QUARANTINE_RULE_PREF="32763"
K3S_TUNNEL_RULE_PREF="32764"
K3S_BLACKHOLE_RULE_PREF="32765"

k3s_policy_rule_is_owned $'32764:\tfrom 10.42.1.7 lookup 51820 proto 200'
if k3s_policy_rule_is_owned $'32766:\tfrom 10.42.9.9 lookup 999 proto 200'; then
    exit 1
fi
if k3s_policy_rule_is_owned $'32764:\tfrom 10.42.9.9 lookup 999 proto 201'; then
    exit 1
fi
'''
    )

    assert result.returncode == 0, result.stderr


def test_k3s_rule_owner_upgrade_retains_legacy_protocol_until_cleanup():
    result = run_bash(
        r'''
source "$1"

K3S_MODE="true"
K3S_RULE_PROTOCOL_FILE="${POLICY_TEST_STATE_FILE}.protocol-upgrade"
printf '%s 199\n' "$(cat /proc/sys/kernel/random/boot_id)" > "${K3S_RULE_PROTOCOL_FILE}"
unset K3S_RULE_PROTOCOL
unset K3S_RULE_PREF_BASE
ip() {
    if [[ "$*" == "-N rule show" ]]; then
        printf '%s\n' $'32765:\tfrom 10.42.0.0/16 blackhole proto 199'
        return 0
    fi
    if [[ "$*" == "rule show" ]]; then
        printf '%s\n' $'32765:\tfrom 10.42.0.0/16 blackhole proto 199'
        return 0
    fi
    return 1
}

initialize_k3s_rule_protocol
[[ "${K3S_RULE_PROTOCOL}" != "199" ]]
[[ "${K3S_LEGACY_RULE_PROTOCOL}" == "199" ]]
[[ "$(awk '{print $4}' "${K3S_RULE_PROTOCOL_FILE}")" == "199" ]]
[[ "${K3S_BLACKHOLE_RULE_PREF}" -lt 32766 ]]
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
    if [[ "$*" == "rule show pref 32764" ]]; then
        printf '%s' "${RULES}" | grep '^32764:' || true
        return 0
    fi
    if [[ "$*" == "rule show" ]]; then
        printf '%s' "${RULES}"
        return 0
    fi
    if [[ "$1 $2" == "rule add" ]]; then
        ADD_COUNT=$((ADD_COUNT + 1))
        case "$*" in
            "rule add from 10.42.1.7 to 10.42.0.0/16 table main protocol 200 pref 32500")
                RULES+=$'32500:\tfrom 10.42.1.7 to 10.42.0.0/16 lookup main proto 200\n'
                ;;
            "rule add from 10.42.1.7 to 10.43.0.0/16 table main protocol 200 pref 32500")
                RULES+=$'32500:\tfrom 10.42.1.7 to 10.43.0.0/16 lookup main proto 200\n'
                ;;
            "rule add from 10.42.1.7 table 51820 protocol 200 pref 32764")
                RULES+=$'32764:\tfrom 10.42.1.7 lookup 51820 proto 200\n'
                ;;
            "rule add from 10.42.1.7 blackhole protocol 200 pref 32765")
                RULES+=$'32765:\tfrom 10.42.1.7 blackhole proto 200\n'
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
    if [[ "$*" == "rule add from 10.42.1.7 blackhole protocol 200 pref 32765" ]]; then
        RULES=$'32765:\tfrom 10.42.1.7 blackhole proto 200\n'
        return 0
    fi
    return 0
}

if ensure_policy_routing; then
    exit 1
fi
[[ "${RULES}" == *"from 10.42.1.7 blackhole"* ]]
[[ "${LAST_ERROR}" == "Invalid K3S_BYPASS_CIDRS: default route 0.0.0.0/0 is not allowed" ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_k3s_policy_table_repair_suspends_source_rule_before_removing_default():
    result = run_bash(
        r'''
source "$1"

DOCKER_TARGET_IP="10.42.1.7"
WG_IFACE="tunnelsatsv2"
SOURCE_RULE=$'32764:\tfrom 10.42.1.7 lookup 51820 proto 200\n'
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
    if [[ "$*" == "rule show pref 32764" ]]; then
        printf '%s' "${SOURCE_RULE}"
        return 0
    fi
    if [[ "$*" == "rule del from 10.42.1.7 lookup 51820 proto 200 pref 32764" ]]; then
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
[[ "${DELETE_LOG}" == *"rule del from 10.42.1.7 lookup 51820 proto 200 pref 32764"* ]]
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
RULES=$'32500:\tfrom 10.42.1.7 to 10.42.0.0/16 lookup main proto 200\n32500:\tfrom 10.42.1.7 to 10.43.0.0/16 lookup main proto 200\n32764:\tfrom 10.42.1.7 lookup 51820 proto 200\n32765:\tfrom 10.42.1.7 blackhole proto 200\n'
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

RULES="${RULES//$'32764:\tfrom 10.42.1.7 lookup 51820 proto 200\n'/}"
if rules_are_synced; then
    exit 1
fi

RULES=$'32500:\tfrom 10.42.1.7 to 10.42.0.0/16 lookup main proto 200\n32500:\tfrom 10.42.1.7 to 10.43.0.0/16 lookup main proto 200\n30000:\tfrom 10.42.1.7 lookup 51820 proto 200\n32765:\tfrom 10.42.1.7 blackhole proto 200\n'
if rules_are_synced; then
    exit 1
fi

RULES=$'32500:\tfrom 10.42.1.7 to 10.42.0.0/16 lookup main proto 200\n32500:\tfrom 10.42.1.7 to 10.43.0.0/16 lookup main proto 200\n32500:\tfrom 10.42.1.7 to 192.168.0.0/16 lookup main proto 200\n32764:\tfrom 10.42.1.7 lookup 51820 proto 200\n32765:\tfrom 10.42.1.7 blackhole proto 200\n'
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
            $'32500:\tfrom 10.42.0.9 to 10.42.0.0/16 lookup main proto 200' \
            $'32500:\tfrom 10.42.0.9 to 10.99.0.0/16 lookup main proto 201' \
            $'32500:\tfrom 10.42.1.7 to 10.42.0.0/16 lookup main' \
            $'32500:\tfrom 10.99.0.5 to 10.99.0.0/16 lookup main'
        return 0
    fi
    if [[ "$*" == "rule show pref 32764" ]]; then
        printf '%s\n' \
            $'32764:\tfrom 10.42.0.9 lookup 51820 proto 200' \
            $'32764:\tfrom 10.88.0.5 lookup 51820 proto 201' \
            $'32764:\tfrom 10.42.0.9 lookup 51820 proto 201' \
            $'32764:\tfrom 10.42.1.7 lookup 51820' \
            $'32764:\tfrom 10.99.0.5 lookup 51820'
        return 0
    fi
    if [[ "$*" == "rule show pref 32765" ]]; then
        printf '%s\n' \
            $'32765:\tfrom 10.42.0.9 blackhole proto 200' \
            $'32765:\tfrom 10.42.0.9 blackhole proto 201' \
            $'32765:\tfrom 10.42.0.0/16 blackhole proto 200' \
            $'32765:\tfrom 10.42.1.7 blackhole' \
            $'32765:\tfrom 10.99.0.5 blackhole'
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
[[ "${DELETED}" != *"proto 201"* ]]
[[ "${DELETED}" != *"from 10.42.0.0/16"* ]]
[[ "${DELETED}" != *"from 10.42.1.7"* ]]
[[ "${DELETED}" != *"from 10.99.0.5"* ]]
[[ "${DELETED}" != *"from 10.88.0.5"* ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_legacy_fwmark_migration_requires_tagged_ownership_evidence():
    result = run_bash(
        r'''
source "$1"

OWNED=0
DELETED=""
ip() {
    if [[ "$*" == "rule show" ]]; then
        printf '%s\n' $'32764:\tfrom all fwmark 0xca6c lookup 51820'
        return 0
    fi
    if [[ "$1 $2" == "rule del" ]]; then
        DELETED+="$*"$'\n'
        return 0
    fi
    return 1
}
iptables() {
    if [[ "${OWNED}" == "1" && "$*" == "-t mangle -S PREROUTING" ]]; then
        printf '%s\n' "-A PREROUTING -m comment --comment tunnelsats-conn-restore"
    elif [[ "${OWNED}" == "1" && "$*" == "-t mangle -S FORWARD" ]]; then
        printf '%s\n' "-A FORWARD -m comment --comment tunnelsats-conn-save"
    fi
    return 0
}

if remove_legacy_k3s_fwmark_rules; then
    exit 1
fi
[[ -z "${DELETED}" ]]
[[ "${LAST_ERROR}" == *"Unowned legacy fwmark rule"* ]]

OWNED=1
LAST_ERROR=""
remove_legacy_k3s_fwmark_rules
[[ "${DELETED}" == *"fwmark 0xca6c lookup 51820"* ]]
[[ -z "${LAST_ERROR}" ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_k3s_subnet_quarantine_covers_replacement_ips_and_preserves_unowned_rules():
    result = run_bash(
        r'''
source "$1"

K3S_BYPASS_CIDRS="10.42.0.0/16,10.43.0.0/16"
RULES=$'32763:\tfrom 10.88.0.0/16 blackhole proto 201\n32765:\tfrom 10.42.0.0/16 blackhole proto 201\n32765:\tfrom 10.42.1.7 blackhole proto 200\n'
DELETED=""

ip() {
    if [[ "$*" == "rule show pref 32763" ]]; then
        printf '%s' "${RULES}" | grep '^32763:' || true
        return 0
    fi
    if [[ "$*" == "rule show pref 32765" ]]; then
        printf '%s' "${RULES}" | grep '^32765:' || true
        return 0
    fi
    if [[ "$*" == "rule add from 10.42.0.0/16 blackhole protocol 200 pref 32763" ]]; then
        RULES+=$'32763:\tfrom 10.42.0.0/16 blackhole proto 200\n'
        return 0
    fi
    if [[ "$1 $2" == "rule del" ]]; then
        DELETED+="$*"$'\n'
        RULES="${RULES//$'32763:\tfrom 10.42.0.0/16 blackhole proto 200\n'/}"
        return 0
    fi
    return 1
}

ensure_k3s_subnet_quarantine "10.42.1.7"
[[ "${K3S_QUARANTINE_CIDR}" == "10.42.0.0/16" ]]
[[ "${RULES}" == *"proto 200"* ]]

remove_k3s_subnet_quarantine
[[ "${DELETED}" == *"from 10.42.0.0/16 blackhole proto 200"* ]]
[[ "${DELETED}" != *"proto 201"* ]]
[[ "${RULES}" == *"proto 201"* ]]
[[ "${RULES}" == *"from 10.88.0.0/16 blackhole proto 201"* ]]
[[ "${RULES}" == *"from 10.42.1.7 blackhole proto 200"* ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_k3s_recovery_preserves_ambiguous_legacy_quarantine():
    result = run_bash(
        r'''
source "$1"

K3S_RULE_PROTOCOL="200"
K3S_RULE_PREF_BASE="10000"
K3S_BYPASS_RULE_PREF="10000"
K3S_QUARANTINE_RULE_PREF="10263"
K3S_TUNNEL_RULE_PREF="10264"
K3S_BLACKHOLE_RULE_PREF="10265"
K3S_LEGACY_RULE_PROTOCOL="199"
RULES=$'32765:\tfrom 10.42.0.0/16 blackhole proto 199\n32765:\tfrom 10.88.0.0/16 blackhole proto 198\n10265:\tfrom 10.42.1.7 blackhole proto 200\n'
DELETED=""
CLEARED=0

ip() {
    if [[ "$*" == "rule show pref 10263" ]]; then
        printf '%s' "${RULES}" | grep '^10263:' || true
        return 0
    fi
    if [[ "$*" == "rule show pref 32765" ]]; then
        printf '%s' "${RULES}" | grep '^32765:' || true
        return 0
    fi
    if [[ "$1 $2" == "rule del" ]]; then
        DELETED+="$*"$'\n'
        if [[ "$*" == *"proto 199 pref 32765"* ]]; then
            RULES="${RULES//$'32765:\tfrom 10.42.0.0/16 blackhole proto 199\n'/}"
        fi
        return 0
    fi
    return 1
}
clear_k3s_legacy_rule_protocol() {
    K3S_LEGACY_RULE_PROTOCOL=""
    CLEARED=1
}

if remove_k3s_subnet_quarantine; then
    exit 1
fi
[[ "${DELETED}" != *"from 10.42.0.0/16 blackhole proto 199 pref 32765"* ]]
[[ "${DELETED}" != *"proto 198"* ]]
[[ "${DELETED}" != *"from 10.42.1.7"* ]]
[[ "${RULES}" == *"proto 199"* ]]
[[ "${RULES}" == *"proto 198"* ]]
[[ "${RULES}" == *"from 10.42.1.7 blackhole proto 200"* ]]
[[ "${CLEARED}" == "0" ]]
[[ "${K3S_LEGACY_RULE_PROTOCOL}" == "199" ]]
[[ "${LAST_ERROR}" == *"ambiguous legacy pod-CIDR quarantine"* ]]
[[ "${LAST_ERROR}" == *"manual cleanup required"* ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_k3s_slash_32_quarantine_is_distinct_from_persistent_pod_fallback():
    result = run_bash(
        r'''
source "$1"

K3S_BYPASS_CIDRS="10.42.1.7/32"
RULES=$'32765:\tfrom 10.42.1.7 blackhole proto 200\n'
DELETED=""

ip() {
    if [[ "$*" == "rule show pref 32763" ]]; then
        printf '%s' "${RULES}" | grep '^32763:' || true
        return 0
    fi
    if [[ "$*" == "rule show pref 32765" ]]; then
        printf '%s' "${RULES}" | grep '^32765:' || true
        return 0
    fi
    if [[ "$*" == "rule add from 10.42.1.7/32 blackhole protocol 200 pref 32763" ]]; then
        RULES+=$'32763:\tfrom 10.42.1.7 blackhole proto 200\n'
        return 0
    fi
    if [[ "$1 $2" == "rule del" ]]; then
        DELETED+="$*"$'\n'
        RULES="${RULES//$'32763:\tfrom 10.42.1.7 blackhole proto 200\n'/}"
        return 0
    fi
    return 1
}

ensure_k3s_subnet_quarantine "10.42.1.7"
[[ "${K3S_QUARANTINE_CIDR}" == "10.42.1.7/32" ]]
[[ "${RULES}" == *"32763:"* ]]

remove_k3s_subnet_quarantine
[[ "${DELETED}" == *"from 10.42.1.7 blackhole proto 200"* ]]
[[ "${RULES}" == *"32765:"* ]]
[[ "${RULES}" != *"32763:"* ]]
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
RULES=$'32500:\tfrom 10.42.1.7 to 10.42.0.0/16 lookup main proto 200\n32500:\tfrom 10.99.0.5 to 10.99.0.0/16 lookup main proto 201\n32764:\tfrom 10.42.1.7 lookup 51820 proto 200\n32764:\tfrom 10.99.0.5 lookup 51820 proto 201\n32764:\tfrom all fwmark 0xca6c lookup 51820\n32765:\tfrom 10.42.1.7 blackhole proto 200\n32765:\tfrom 10.99.0.5 blackhole proto 201\n'

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
[[ "${DELETED}" != *"from 10.99.0.5"* ]]
[[ "${DELETED}" != *"fwmark 0xca6c"* ]]
[[ "${FLUSHED}" == "0" ]]

DELETED=""
cleanup_dataplane
[[ "${DELETED}" == *"from 10.42.1.7 blackhole"* ]]
[[ "${DELETED}" != *"from 10.99.0.5"* ]]
[[ "${DELETED}" != *"fwmark 0xca6c"* ]]
[[ "${FLUSHED}" == "1" ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_k3s_sigterm_cleanup_preserves_fail_closed_guards_while_removing_forwarding():
    result = run_bash(
        r'''
source "$1"

K3S_MODE="true"
SECURE_MODE="false"
DOCKER_TARGET_IP="10.42.1.7"
API_PID=""
DELETED_RULES=""
REMOVED_IPTABLES=""
EGRESS_GUARD_ENSURED=0
BLACKHOLE_ENSURED=0
NETPOL_ENSURED=0
IPV6_CLEANED=0
SUBNET_QUARANTINE_REMOVED=0
RULES=$'32500:\tfrom 10.42.1.7 to 10.42.0.0/16 lookup main proto 200\n32764:\tfrom 10.42.1.7 lookup 51820 proto 200\n32765:\tfrom 10.42.1.7 blackhole proto 200\n'

ensure_k3s_egress_guard() {
    if [[ "$1" == "10.42.1.7" ]]; then
        EGRESS_GUARD_ENSURED=1
        return 0
    fi
    return 1
}
ensure_fallback_blackhole_rule() {
    if [[ "$1" == "10.42.1.7" ]]; then
        BLACKHOLE_ENSURED=1
        return 0
    fi
    return 1
}
ensure_k3s_emergency_network_policy() {
    NETPOL_ENSURED=1
    return 0
}
remove_tagged_iptables_rules() {
    REMOVED_IPTABLES+="$1/$2/$3"$'\n'
}
cleanup_ipv6_containment() {
    IPV6_CLEANED=1
}
remove_k3s_subnet_quarantine() {
    SUBNET_QUARANTINE_REMOVED=1
}
ip() {
    if [[ "$*" == "rule show pref 32500" ]]; then
        printf '%s' "${RULES}" | grep '^32500:' || true
        return 0
    fi
    if [[ "$*" == "rule show pref 32763" ]]; then
        printf '%s' "${RULES}" | grep '^32763:' || true
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
    if [[ "$*" == "rule add from 10.42.0.0/16 blackhole protocol 200 pref 32763" ]]; then
        RULES+=$'32763:\tfrom 10.42.0.0/16 blackhole proto 200\n'
        return 0
    fi
    if [[ "$1 $2" == "rule del" ]]; then
        DELETED_RULES+="$*"$'\n'
        return 0
    fi
    return 0
}

( cleanup )
[[ "$?" == "0" ]]
# Running in subshell above verified exit 0; now inspect effects by overriding exit
exit() { return 0; }
cleanup

[[ "${EGRESS_GUARD_ENSURED}" == "1" ]]
[[ "${BLACKHOLE_ENSURED}" == "1" ]]
[[ "${NETPOL_ENSURED}" == "1" ]]
[[ "${RULES}" != *"32763:"* ]]
[[ "${DELETED_RULES}" == *"from 10.42.1.7 to 10.42.0.0/16 lookup main"* ]]
[[ "${DELETED_RULES}" == *"from 10.42.1.7 lookup 51820"* ]]
[[ "${DELETED_RULES}" != *"from 10.42.1.7 blackhole"* ]]
[[ "${REMOVED_IPTABLES}" == *"nat/PREROUTING/tunnelsats-dnat"* ]]
[[ "${REMOVED_IPTABLES}" == *"filter/FORWARD/tunnelsats-forward-in"* ]]
[[ "${REMOVED_IPTABLES}" == *"filter/FORWARD/tunnelsats-forward-out"* ]]
[[ "${REMOVED_IPTABLES}" != *"tunnelsats-k3s-egress-guard"* ]]
[[ "${IPV6_CLEANED}" == "0" ]]
[[ "${SUBNET_QUARANTINE_REMOVED}" == "0" ]]

# When no target IP is known on exit and emergency NetworkPolicy succeeds,
# routine shutdown must NOT install shared-subnet quarantine (avoiding blocking
# unrelated pods on the node).
K3S_BYPASS_CIDRS="10.43.0.0/16,10.42.0.0/16"
K3S_QUARANTINE_CIDR=""
RULES="${RULES//$'32763:\tfrom 10.42.0.0/16 blackhole proto 200\n'/}"
DOCKER_TARGET_IP=""
MANAGED_K3S_TARGET_IP=""
K3S_POLICY_STATE_LOADED="true"
NETPOL_ENSURED=0
cleanup
[[ "${RULES}" != *"32763:"* ]]
[[ "${NETPOL_ENSURED}" == "1" ]]

# When target IP is known (10.42.1.7) and per-pod guard/blackhole both fail,
# preserve_fail_closed_dataplane falls back to subnet quarantine for that target IP
# AND still applies the selector-scoped emergency NetworkPolicy.
DOCKER_TARGET_IP="10.42.1.7"
QUARANTINE_ENSURED=0
NETPOL_ENSURED=0
ensure_k3s_egress_guard() { return 1; }
ensure_fallback_blackhole_rule() { return 1; }
ensure_k3s_subnet_quarantine() {
    if [[ "$1" == "10.42.1.7" ]]; then
        QUARANTINE_ENSURED=1
        return 0
    fi
    return 1
}
cleanup
[[ "${QUARANTINE_ENSURED}" == "1" ]]
[[ "${NETPOL_ENSURED}" == "1" ]]

# When the Kubernetes API is unavailable during SIGTERM (emergency NetworkPolicy
# creation fails) even though per-pod guard/blackhole succeed, shutdown must
# install and retain pod-CIDR subnet quarantine, attempt delete_k3s_target_pod,
# AND exit non-zero.
K3S_SHUTDOWN_NETPOL_ATTEMPTS=2
K3S_SHUTDOWN_RETRY_INTERVAL=0
DOCKER_TARGET_IP="10.42.1.7"
QUARANTINE_ENSURED=0
SUBNET_QUARANTINE_REMOVED=0
NETPOL_ATTEMPTS=0
POD_DELETED=0
ensure_k3s_egress_guard() { return 0; }
ensure_fallback_blackhole_rule() { return 0; }
ensure_k3s_subnet_quarantine() {
    if [[ "$1" == "10.42.1.7" ]]; then
        QUARANTINE_ENSURED=1
        return 0
    fi
    return 1
}
ensure_k3s_emergency_network_policy() {
    NETPOL_ATTEMPTS=$((NETPOL_ATTEMPTS + 1))
    return 1
}
delete_k3s_target_pod() {
    POD_DELETED=1
    return 0
}
unset -f exit
set +e
( cleanup )
CLEANUP_RC=$?
set -e
[[ "${CLEANUP_RC}" == "1" ]]
exit() { return 0; }
QUARANTINE_ENSURED=0
SUBNET_QUARANTINE_REMOVED=0
NETPOL_ATTEMPTS=0
POD_DELETED=0
! preserve_fail_closed_dataplane
[[ "${QUARANTINE_ENSURED}" == "1" ]]
[[ "${SUBNET_QUARANTINE_REMOVED}" == "0" ]]
[[ "${NETPOL_ATTEMPTS}" == "2" ]]
[[ "${POD_DELETED}" == "1" ]]

# If every fail-closed control (guard, blackhole, subnet quarantine, and emergency
# NetworkPolicy) fails, cleanup_dataplane --keep-tunnel must NOT be called.
CLEANUP_DATAPLANE_CALLED=0
cleanup_dataplane() {
    CLEANUP_DATAPLANE_CALLED=1
}
ensure_k3s_egress_guard() { return 1; }
ensure_fallback_blackhole_rule() { return 1; }
ensure_k3s_subnet_quarantine() { return 1; }
ensure_k3s_emergency_network_policy() { return 1; }
! preserve_fail_closed_dataplane
[[ "${CLEANUP_DATAPLANE_CALLED}" == "0" ]]

# When the first emergency NetworkPolicy attempt fails transiently and the
# second attempt succeeds, temporary pod-CIDR quarantine is installed during the
# retry window and removed once the selector NetworkPolicy succeeds, before
# cleanup_dataplane --keep-tunnel runs.
ORDER_LOG=""
SUBNET_QUARANTINE_REMOVED=0
NETPOL_ATTEMPTS=0
ensure_k3s_egress_guard() { return 0; }
ensure_fallback_blackhole_rule() { return 0; }
ensure_k3s_subnet_quarantine() {
    ORDER_LOG+="quarantine;"
    return 0
}
remove_k3s_subnet_quarantine() {
    SUBNET_QUARANTINE_REMOVED=1
    ORDER_LOG+="unquarantine;"
    return 0
}
ensure_k3s_emergency_network_policy() {
    NETPOL_ATTEMPTS=$((NETPOL_ATTEMPTS + 1))
    ORDER_LOG+="netpol:${NETPOL_ATTEMPTS};"
    [[ "${NETPOL_ATTEMPTS}" -ge 2 ]]
}
cleanup_dataplane() {
    ORDER_LOG+="cleanup:${1:-full};"
    return 0
}
preserve_fail_closed_dataplane
[[ "${ORDER_LOG}" == "netpol:1;quarantine;netpol:2;unquarantine;cleanup:--keep-tunnel;" ]]
[[ "${SUBNET_QUARANTINE_REMOVED}" == "1" ]]
[[ "${NETPOL_ATTEMPTS}" == "2" ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_docker_and_secure_mode_sigterm_cleanup_preserve_fail_closed_blackholes():
    result = run_bash(
        r'''
source "$1"

API_PID=""
exit() { return 0; }
remove_tagged_iptables_rules() { :; }

# 1. Docker mode
K3S_MODE="false"
SECURE_MODE="false"
DOCKER_TARGET_IP="10.9.9.9"
DOCKER_POLICY_STATE_LOADED="true"
MANAGED_DOCKER_IPV4_ENDPOINTS=("172.18.0.5|172.18.0.0/16|172.18.0.1|0|0" "10.9.9.9|10.9.9.0/25|10.9.9.1|1|100")
KILL_SWITCH_CALLS=0
IPV6_CLEANED=0
DELETED_RULES=""

ensure_reconcile_kill_switch() {
    KILL_SWITCH_CALLS=$((KILL_SWITCH_CALLS + 1))
    return 0
}
cleanup_ipv6_containment() {
    IPV6_CLEANED=1
}
ip() {
    if [[ "$1 $2" == "rule del" ]]; then
        DELETED_RULES+="$*"$'\n'
        return 0
    fi
    return 0
}

cleanup
[[ "${KILL_SWITCH_CALLS}" -ge 1 ]]
[[ "${IPV6_CLEANED}" == "0" ]]
[[ "${DELETED_RULES}" == *"from 172.18.0.5 to 172.18.0.0/16 table main pref 32500"* ]]
[[ "${DELETED_RULES}" == *"from 172.18.0.5 table 51820 pref 32764"* ]]
[[ "${DELETED_RULES}" != *"blackhole"* ]]
[[ "${#MANAGED_DOCKER_IPV4_ENDPOINTS[@]}" == "2" ]]

# 2. Secure Mode
SECURE_MODE="true"
DOCKER_TARGET_IP="10.21.21.9"
KILL_SWITCH_CALLS=0
DELETED_RULES=""

cleanup
[[ "${KILL_SWITCH_CALLS}" -ge 1 ]]
[[ "${IPV6_CLEANED}" == "0" ]]
[[ "${DELETED_RULES}" == *"from 10.21.21.9 table 51820 pref 32764"* ]]
[[ "${DELETED_RULES}" != *"blackhole"* ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_restart_trigger_bounces_wireguard_and_reconciles_while_preserving_fail_closed_guards():
    result = run_bash(
        r'''
source "$1"

RESTART_TRIGGER="${POLICY_TEST_STATE_FILE}.restart"
printf 'trigger' > "${RESTART_TRIGGER}"
K3S_MODE="true"
SECURE_MODE="false"
DOCKER_TARGET_IP="10.42.1.7"
DECOMMISSIONED="true"
EVENTS=""

ensure_k3s_egress_guard() {
    EVENTS+="guard:$1;"
    return 0
}
ensure_fallback_blackhole_rule() {
    EVENTS+="blackhole:$1;"
    return 0
}
ensure_k3s_subnet_quarantine() {
    EVENTS+="quarantine:$1;"
    return 0
}
ensure_k3s_emergency_network_policy() {
    EVENTS+="netpol;"
    return 0
}
cleanup_dataplane() {
    EVENTS+="cleanup:${1:-full};"
    return 0
}
wg() {
    if [[ "$1 $2" == "show tunnelsatsv2" ]]; then
        return 0
    fi
    return 1
}
wg-quick() {
    EVENTS+="wg-quick:$1:$2;"
    return 0
}
reconcile_once() {
    EVENTS+="reconcile:$1;"
    return 0
}

handle_restart_trigger
[[ ! -f "${RESTART_TRIGGER}" ]]
[[ "${DECOMMISSIONED}" == "false" ]]
[[ "${EVENTS}" == "guard:10.42.1.7;blackhole:10.42.1.7;netpol;cleanup:--keep-tunnel;wg-quick:down:${WG_CONF_PATH};reconcile:restart;" ]]

# When emergency NetworkPolicy fails during restart (e.g. conflicting allow policy)
# but verified pod-CIDR quarantine is in place to protect replacement pods,
# handle_restart_trigger proceeds with WireGuard restart.
EVENTS=""
K3S_SHUTDOWN_NETPOL_ATTEMPTS=1
ensure_k3s_emergency_network_policy() {
    EVENTS+="netpol:fail;"
    return 1
}
delete_k3s_target_pod() {
    EVENTS+="delete;"
    return 0
}
handle_restart_trigger
[[ "${EVENTS}" == "guard:10.42.1.7;blackhole:10.42.1.7;netpol:fail;quarantine:10.42.1.7;cleanup:--keep-tunnel;wg-quick:down:${WG_CONF_PATH};reconcile:restart;" ]]

# If replacement-pod protection (both NetworkPolicy and pod-CIDR quarantine) fails
# during restart, handle_restart_trigger must return 1 and must NOT bring down WireGuard
# even when current-IP guards succeed.
EVENTS=""
ensure_k3s_subnet_quarantine() { return 1; }
if handle_restart_trigger; then
    exit 1
fi
[[ "${EVENTS}" != *"wg-quick:down"* ]]
[[ "${EVENTS}" != *"reconcile:restart"* ]]
'''
    )

    assert result.returncode == 0, result.stderr


def test_k3s_recreate_upgrade_persists_and_adopts_managed_target_state_across_restarts():
    result = run_bash(
        r'''
# Process 1: active k3s pod 10.42.1.7, exits on SIGTERM during Recreate upgrade.
(
    source "$1"
    K3S_MODE="true"
    SECURE_MODE="false"
    DOCKER_TARGET_IP="10.42.1.7"
    TARGET_IMPL="lnd"
    K3S_TARGET_POD_NAME="lnd-0"
    K3S_TARGET_POD_NAMESPACE="lightning"
    K3S_TARGET_POD_SELECTOR="app=lnd"
    ensure_k3s_egress_guard() { return 0; }
    ensure_fallback_blackhole_rule() { return 0; }
    ensure_k3s_emergency_network_policy() { return 0; }
    cleanup_dataplane() { return 0; }
    cleanup
)

[[ -f "${K3S_POLICY_STATE_FILE}" ]]
[[ "$(stat -c '%a' "${K3S_POLICY_STATE_FILE}")" == "600" ]]
jq -e '
    .version == 1
    and .target_ip == "10.42.1.7"
    and .target_impl == "lnd"
    and .pod_name == "lnd-0"
    and .pod_namespace == "lightning"
    and .pod_selector == "app=lnd"
    and .decommissioned == false
' "${K3S_POLICY_STATE_FILE}" >/dev/null

# Process 2: replacement TunnelSats pod starts on the same node before k8s API discovery runs.
(
    source "$1"
    K3S_MODE="true"
    SECURE_MODE="false"
    GUARDED_IP=""
    BLACKHOLED_IP=""
    ensure_k3s_egress_guard() {
        GUARDED_IP="$1"
        return 0
    }
    ensure_fallback_blackhole_rule() {
        BLACKHOLED_IP="$1"
        return 0
    }
    ensure_reconcile_kill_switch
    [[ "${DOCKER_TARGET_IP}" == "10.42.1.7" ]]
    [[ "${TARGET_IMPL}" == "lnd" ]]
    [[ "${K3S_TARGET_POD_NAME}" == "lnd-0" ]]
    [[ "${K3S_TARGET_POD_NAMESPACE}" == "lightning" ]]
    [[ "${K3S_TARGET_POD_SELECTOR}" == "app=lnd" ]]
    [[ "${GUARDED_IP}" == "10.42.1.7" ]]
    [[ "${BLACKHOLED_IP}" == "10.42.1.7" ]]

    # Switching from LND (lightning/app=lnd) to CLN (cln-ns/app=cln) must remove
    # LND's emergency NetworkPolicy before overwriting MANAGED_K3S_POD_NAMESPACE/SELECTOR.
    TUNNELSATS_K8S_NODE_NAME="node-1"
    REMOVED_TARGET=""
    k8s_api() {
        cat <<'JSON'
{"items":[{"metadata":{"name":"cln-0","labels":{"app":"cln"}},"spec":{"nodeName":"node-1"},"status":{"phase":"Running","podIP":"10.42.1.8","conditions":[{"type":"Ready","status":"True"}]}}]}
JSON
    }
    remove_k3s_emergency_network_policy() {
        REMOVED_TARGET="${K3S_TARGET_POD_NAMESPACE}:${K3S_TARGET_POD_SELECTOR}"
        return 1
    }
    if resolve_k3s_target_pod "cln" "cln-ns" "app=cln"; then
        exit 1
    fi
    [[ "${REMOVED_TARGET}" == "lightning:app=lnd" ]]
    [[ "${MANAGED_K3S_POD_NAMESPACE}" == "lightning" ]]
    [[ "${MANAGED_K3S_POD_SELECTOR}" == "app=lnd" ]]

    remove_k3s_emergency_network_policy() {
        REMOVED_TARGET="${K3S_TARGET_POD_NAMESPACE}:${K3S_TARGET_POD_SELECTOR}"
        return 0
    }
    resolve_k3s_target_pod "cln" "cln-ns" "app=cln"
    [[ "${REMOVED_TARGET}" == "lightning:app=lnd" ]]
    [[ "${MANAGED_K3S_POD_NAMESPACE}" == "cln-ns" ]]
    [[ "${MANAGED_K3S_POD_SELECTOR}" == "app=cln" ]]
)
'''
    )

    assert result.returncode == 0, result.stderr


def test_decommission_trigger_performs_full_teardown_and_skips_periodic_reconcile_until_manual():
    result = run_bash(
        r'''
source "$1"

STATE_FILE="${POLICY_TEST_STATE_FILE}.state.json"
DECOMMISSION_TRIGGER_DIR="${POLICY_TEST_STATE_FILE}.decomm_trigger.d"
DECOMMISSION_RESULT_DIR="${POLICY_TEST_STATE_FILE}.decomm_result.d"
RECONCILE_TRIGGER_DIR="${POLICY_TEST_STATE_FILE}.reconcile_trigger.d"
RECONCILE_RESULT_DIR="${POLICY_TEST_STATE_FILE}.reconcile_result.d"
RECONCILE_TRIGGER_LEGACY="${POLICY_TEST_STATE_FILE}.reconcile_trigger_legacy"
RECONCILE_RESULT_LEGACY="${POLICY_TEST_STATE_FILE}.reconcile_result_legacy.json"

K3S_MODE="true"
SECURE_MODE="false"
DOCKER_TARGET_IP="10.42.1.7"
TARGET_IMPL="lnd"
K3S_TARGET_POD_NAME="lnd-0"
K3S_TARGET_POD_NAMESPACE="lightning"
K3S_TARGET_POD_SELECTOR="app=lnd"
MANAGED_K3S_TARGET_IP="10.42.1.7"
MANAGED_K3S_TARGET_IMPL="lnd"
MANAGED_K3S_POD_NAME="lnd-0"
MANAGED_K3S_POD_NAMESPACE="lightning"
MANAGED_K3S_POD_SELECTOR="app=lnd"
K3S_POLICY_STATE_LOADED="true"
persist_managed_k3s_policy_state

CLEANUP_ARG="unset"
NETPOL_REMOVED=0
DETECT_CALLS=0

iptables() { return 0; }
ip6tables() { return 0; }
ip() { return 0; }
cleanup_dataplane() {
    CLEANUP_ARG="${1:-full}"
    return 0
}
detect_lightning_container() {
    DETECT_CALLS=$((DETECT_CALLS + 1))
    return 1
}

# 1. If emergency NetworkPolicy removal fails, decommission must fail closed,
# preserve ownership, and report decommissioned=false.
remove_k3s_emergency_network_policy() {
    LAST_ERROR="k3s: Failed to remove emergency deny-egress NetworkPolicy"
    return 1
}
ensure_decommission_dirs
printf 'req-decomm-fail\n' > "${DECOMMISSION_TRIGGER_DIR}/req-decomm-fail.trigger"
process_decommission_triggers

[[ "${DECOMMISSIONED}" == "false" ]]
[[ "${MANAGED_K3S_TARGET_IP}" == "10.42.1.7" ]]
jq -e '.request_id == "req-decomm-fail" and .decommissioned == false and .state.decommissioned == false and (.state.last_error | contains("NetworkPolicy"))' \
    "${DECOMMISSION_RESULT_DIR}/req-decomm-fail.json" >/dev/null

# 2. When all guards are removed and verified, decommission succeeds and
# discards any already-queued manual reconcile or restart triggers so an older
# waiting activation request cannot undo the restore.
remove_k3s_emergency_network_policy() {
    NETPOL_REMOVED=1
    return 0
}
ensure_reconcile_dirs
RESTART_TRIGGER="${POLICY_TEST_STATE_FILE}.restart"
printf 'restart\n' > "${RESTART_TRIGGER}"
printf 'req-legacy-stale\n' > "${RECONCILE_TRIGGER_LEGACY}"
printf 'req-manual-stale\n' > "${RECONCILE_TRIGGER_DIR}/req-manual-stale.trigger"
printf 'req-decomm-1\n' > "${DECOMMISSION_TRIGGER_DIR}/req-decomm-1.trigger"
process_decommission_triggers

[[ ! -f "${DECOMMISSION_TRIGGER_DIR}/req-decomm-1.trigger" ]]
[[ ! -f "${RESTART_TRIGGER}" ]]
[[ ! -f "${RECONCILE_TRIGGER_LEGACY}" ]]
[[ ! -f "${RECONCILE_TRIGGER_DIR}/req-manual-stale.trigger" ]]
[[ "${NETPOL_REMOVED}" == "1" ]]
[[ "${CLEANUP_ARG}" == "full" ]]
[[ "${DECOMMISSIONED}" == "true" ]]
[[ -z "${DOCKER_TARGET_IP}" ]]
[[ -z "${MANAGED_K3S_TARGET_IP}" ]]
[[ -f "${DECOMMISSION_STATE_FILE}" ]]

jq -e '.decommissioned == true' "${DECOMMISSION_STATE_FILE}" >/dev/null
jq -e '.decommissioned == true and .target_ip == ""' "${K3S_POLICY_STATE_FILE}" >/dev/null
jq -e '.decommissioned == true and .rules_synced == false' "${STATE_FILE}" >/dev/null
jq -e '.request_id == "req-decomm-1" and .decommissioned == true and .state.decommissioned == true' \
    "${DECOMMISSION_RESULT_DIR}/req-decomm-1.json" >/dev/null
jq -e '.request_id == "req-manual-stale" and .changed == false and .state.rules_synced == false and .state.decommissioned == true' \
    "${RECONCILE_RESULT_DIR}/req-manual-stale.json" >/dev/null
jq -e '.request_id == "req-legacy-stale" and .changed == false and .state.rules_synced == false and .state.decommissioned == true' \
    "${RECONCILE_RESULT_DIR}/req-legacy-stale.json" >/dev/null

# 3. Container restart in Docker/Secure Mode or k3s reboot must preserve disabled state
# before startup installs guards or reconciles.
(
    source "$1"
    K3S_MODE="false"
    SECURE_MODE="true"
    KILL_SWITCH_INSTALLED=0
    ensure_source_blackhole_rule() {
        KILL_SWITCH_INSTALLED=1
        return 0
    }
    ensure_reconcile_kill_switch
    [[ "${DECOMMISSIONED}" == "true" ]]
    [[ "${KILL_SWITCH_INSTALLED}" == "0" ]]
    reconcile_once "startup"
    [[ "${KILL_SWITCH_INSTALLED}" == "0" ]]
)

# 4. In Docker Standard Mode (K3S_MODE=false, SECURE_MODE=false), decommission_dataplane
# preserves DOCKER_TARGET_IP="10.9.9.9" so subsequent re-enable works without container restart.
# Also verify that if IPv6 FORWARD deletion fails early, saved IPv6 ownership is preserved on disk.
(
    source "$1"
    STATE_FILE="${POLICY_TEST_STATE_FILE}.docker_state.json"
    K3S_MODE="false"
    SECURE_MODE="false"
    DOCKER_TARGET_IP="10.9.9.9"
    IPV6_POLICY_STATE_LOADED="true"
    MANAGED_TARGET_IPV6_ADDRESSES=("2001:db8:42::7")
    IPV6_POLICY_TABLE_MANAGED="true"
    persist_managed_ipv6_policy_state
    iptables() { return 0; }
    ip6tables() {
        if [[ "$*" == "-S FORWARD" ]]; then
            printf '%s\n' "-A FORWARD -s 2001:db8:42::7/128 -m comment --comment ${IPV6_RULE_MARKER} -j DROP"
        fi
        return 0
    }
    ip() {
        if [[ "$*" == "-6 rule show pref ${IPV6_BLACKHOLE_RULE_PREF}" ]]; then
            printf '%s\n' "${IPV6_BLACKHOLE_RULE_PREF}: from 2001:db8:42::7 blackhole"
        fi
        return 0
    }
    if decommission_dataplane; then
        exit 1
    fi
    jq -e '.addresses == ["2001:db8:42::7"] and .table_managed == true' "${IPV6_POLICY_STATE_FILE}" >/dev/null

    ip6tables() { return 0; }
    ip() { return 0; }
    iptables() {
        if [[ "$*" == "-t nat -S POSTROUTING" ]]; then
            printf '%s\n' "-A POSTROUTING -m comment --comment tunnelsats-masq -j MASQUERADE"
        fi
        return 0
    }
    if decommission_dataplane; then
        exit 1
    fi
    [[ "${DECOMMISSIONED}" == "false" ]]

    iptables() { return 0; }
    decommission_dataplane
    [[ "${DECOMMISSIONED}" == "true" ]]
    [[ "${DOCKER_TARGET_IP}" == "10.9.9.9" ]]
    jq -e '.addresses == [] and .table_managed == false' "${IPV6_POLICY_STATE_FILE}" >/dev/null
)

# Periodic reconcile must be skipped while decommissioned.
reconcile_once "periodic"
[[ "${DETECT_CALLS}" == "0" ]]
[[ "${DECOMMISSIONED}" == "true" ]]

# Manual reconcile clears decommissioned state and resumes normal protection.
if reconcile_once "manual"; then
    exit 1
fi
[[ "${DETECT_CALLS}" == "1" ]]
[[ "${DECOMMISSIONED}" == "false" ]]
jq -e '.decommissioned == false' "${DECOMMISSION_STATE_FILE}" >/dev/null
jq -e '.decommissioned == false' "${K3S_POLICY_STATE_FILE}" >/dev/null
'''
    )

    assert result.returncode == 0, result.stderr


def test_docker_protected_route_script_PreservesCleanupTrapAndTempStateFiles() -> None:
    script_path = os.path.join(REPO_ROOT, "scripts", "test-docker-protected-route.sh")
    with open(script_path, encoding="utf-8") as fh:
        script = fh.read()
    assert "cleanup_test_env()" in script
    assert "trap cleanup_test_env EXIT" in script
    assert 'export DOCKER_POLICY_STATE_FILE="${DOCKER_POLICY_STATE_TMP}"' in script
    assert 'export IPV6_POLICY_STATE_FILE="${IPV6_POLICY_STATE_TMP}"' in script
    assert 'export DECOMMISSION_STATE_FILE="${DECOMMISSION_STATE_TMP}"' in script
    assert 'STATE_FILE="${STATE_FILE_TMP}"' in script

    with tempfile.TemporaryDirectory() as temp_dir:
        docker_log = os.path.join(temp_dir, "docker.log")
        docker_bin = os.path.join(temp_dir, "docker")
        with open(docker_bin, "w", encoding="utf-8") as fh:
            fh.write(
                f"""#!/usr/bin/env bash
echo "$*" >> "{docker_log}"
if [[ "$1" == "version" ]]; then
    echo "1.48"
    exit 0
fi
if [[ "$1" == "inspect" ]]; then
    exit 42
fi
exit 0
"""
            )
        os.chmod(docker_bin, 0o755)
        env = os.environ.copy()
        env["PATH"] = f"{temp_dir}:{env.get('PATH', '')}"
        res = subprocess.run(
            ["bash", script_path],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert res.returncode == 42
        assert "Permission denied" not in res.stderr
        assert "/data" not in res.stderr
        with open(docker_log, encoding="utf-8") as fh:
            calls = fh.read()
        assert "rm -f ts-route-lnd-" in calls
        assert "network rm ts-clear-" in calls
        assert "network rm ts-protected-" in calls


def test_k3s_emergency_network_policy_rejects_conflicting_egress_allow_policies() -> None:
    result = run_bash(
        r'''
source "$1"

K3S_MODE="true"
SECURE_MODE="false"
DOCKER_TARGET_IP="10.42.1.7"
K3S_TARGET_POD_NAMESPACE="lightning"
K3S_TARGET_POD_SELECTOR="app=lnd"
K3S_SHUTDOWN_NETPOL_ATTEMPTS=1
K3S_SHUTDOWN_RETRY_INTERVAL=0
PAYLOAD_FILE="${POLICY_TEST_STATE_FILE}.network-policy"

k8s_api_write_status() {
    if [[ "$1" == "POST" ]]; then
        printf '%s' "$3" > "${PAYLOAD_FILE}"
        printf '%s' "201"
        return 0
    fi
    return 1
}

# 1. Non-overlapping egress policy (app=other) and Ingress-only policy on app=lnd do NOT conflict.
k8s_api() {
    if [[ "$1" == "/apis/networking.k8s.io/v1/namespaces/lightning/networkpolicies/tunnelsats-emergency-egress-deny" ]]; then
        jq '.metadata.uid = "owned-policy-uid"' "${PAYLOAD_FILE}"
        return 0
    fi
    if [[ "$1" == "/apis/networking.k8s.io/v1/namespaces/lightning/networkpolicies" ]]; then
        jq -cn --slurpfile owned "${PAYLOAD_FILE}" '{
            items: [
                $owned[0],
                {
                    metadata: {name: "allow-other-egress"},
                    spec: {
                        podSelector: {matchLabels: {app: "other"}},
                        policyTypes: ["Egress"],
                        egress: [{to: [{ipBlock: {cidr: "0.0.0.0/0"}}]}]
                    }
                },
                {
                    metadata: {name: "allow-lnd-ingress"},
                    spec: {
                        podSelector: {matchLabels: {app: "lnd"}},
                        policyTypes: ["Ingress"],
                        ingress: [{}]
                    }
                },
                {
                    metadata: {name: "default-deny-egress-omitted"},
                    spec: {
                        podSelector: {},
                        policyTypes: ["Egress"]
                    }
                }
            ]
        }'
        return 0
    fi
    return 1
}

ensure_k3s_emergency_network_policy
k3s_emergency_network_policy_is_active "lightning" "app=lnd"

# 2. Conflicting egress-allowing policy (podSelector: {} or matchLabels: {app: lnd})
# causes ensure_k3s_emergency_network_policy to fail and preserve_fail_closed_dataplane
# to install and retain pod-CIDR subnet quarantine and return 1.
k8s_api() {
    if [[ "$1" == "/apis/networking.k8s.io/v1/namespaces/lightning/networkpolicies/tunnelsats-emergency-egress-deny" ]]; then
        jq '.metadata.uid = "owned-policy-uid"' "${PAYLOAD_FILE}"
        return 0
    fi
    if [[ "$1" == "/apis/networking.k8s.io/v1/namespaces/lightning/networkpolicies" ]]; then
        jq -cn --slurpfile owned "${PAYLOAD_FILE}" '{
            items: [
                $owned[0],
                {
                    metadata: {name: "allow-all-egress"},
                    spec: {
                        podSelector: {},
                        policyTypes: ["Egress"],
                        egress: [{to: [{ipBlock: {cidr: "0.0.0.0/0"}}]}]
                    }
                }
            ]
        }'
        return 0
    fi
    return 1
}

if ensure_k3s_emergency_network_policy; then
    exit 1
fi
if k3s_emergency_network_policy_is_active "lightning" "app=lnd"; then
    exit 1
fi

QUARANTINE_ENSURED=0
ensure_k3s_egress_guard() { return 0; }
ensure_fallback_blackhole_rule() { return 0; }
ensure_k3s_subnet_quarantine() {
    QUARANTINE_ENSURED=1
    return 0
}
delete_k3s_target_pod() { return 0; }
cleanup_dataplane() { return 0; }
if preserve_fail_closed_dataplane; then
    exit 1
fi
[[ "${QUARANTINE_ENSURED}" == "1" ]]

# 3. In restart mode, failed NetworkPolicy installs pod-CIDR quarantine and proceeds
# only when replacement pods are protected by that fallback quarantine.
QUARANTINE_ENSURED=0
preserve_fail_closed_dataplane "restart"
[[ "${QUARANTINE_ENSURED}" == "1" ]]

ensure_k3s_subnet_quarantine() { return 1; }
if preserve_fail_closed_dataplane "restart"; then
    exit 1
fi

# 4. Reactivation from decommissioned state aborts if persist_decommissioned_state fails.
DECOMMISSIONED="true"
persist_decommissioned_state() { return 1; }
if reconcile_once "manual"; then
    exit 1
fi
[[ "${DECOMMISSIONED}" == "true" ]]
if handle_restart_trigger; then
    exit 1
fi
[[ "${DECOMMISSIONED}" == "true" ]]
'''
    )
    assert result.returncode == 0, result.stderr


def test_k3s_dual_stack_restart_requires_ipv6_subnet_quarantine_when_netpol_fails() -> None:
    result = run_bash(
        r'''
source "$1"

K3S_MODE="true"
SECURE_MODE="false"
DOCKER_TARGET_IP="10.42.1.7"
K3S_TARGET_POD_NAMESPACE="lightning"
K3S_TARGET_POD_SELECTOR="app=lnd"
K3S_SHUTDOWN_NETPOL_ATTEMPTS=1
K3S_SHUTDOWN_RETRY_INTERVAL=0
K3S_BYPASS_CIDRS="10.42.0.0/16,10.43.0.0/16"
IPV6_POLICY_STATE_LOADED="true"
IPV6_POLICY_TABLE_MANAGED="true"
MANAGED_TARGET_IPV6_ADDRESSES=("2001:db8:42::7")
TARGET_IPV6_ADDRESSES=("2001:db8:42::7")
TARGET_HAS_IPV6_DEFAULT_ROUTE="true"

V4_RULES=""
V6_RULES=""
V6_FILTER_INSTALLED="false"
V4_DELETED=""
V6_DELETED=""

ensure_k3s_egress_guard() { return 0; }
ensure_fallback_blackhole_rule() { return 0; }
ensure_k3s_emergency_network_policy() { return 1; }
cleanup_dataplane() { return 0; }

ip() {
    case "$*" in
        "rule show pref 32763")
            printf '%s' "${V4_RULES}"
            return 0
            ;;
        "rule add from 10.42.0.0/16 blackhole protocol 200 pref 32763")
            V4_RULES=$'32763:\tfrom 10.42.0.0/16 blackhole proto 200\n'
            return 0
            ;;
        "rule del "*)
            V4_DELETED+="$*"$'\n'
            V4_RULES=""
            return 0
            ;;
        "-6 rule show pref 32763")
            printf '%s' "${V6_RULES}"
            return 0
            ;;
        "-6 rule add from 2001:db8:42::/64 blackhole protocol 200 pref 32763")
            V6_RULES=$'32763:\tfrom 2001:db8:42::/64 blackhole proto 200\n'
            return 0
            ;;
        "-6 rule del "*)
            V6_DELETED+="$*"$'\n'
            V6_RULES=""
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}
ip6tables() {
    if [[ "$*" == "-S FORWARD" ]]; then
        if [[ "${V6_FILTER_INSTALLED}" == "true" ]]; then
            printf '%s\n' "-A FORWARD -s 2001:db8:42::/64 ! -d fe80::/10 -m comment --comment ${K3S_IPV6_QUARANTINE_MARKER} -j DROP"
        fi
        printf '%s\n' "-A FORWARD -s 2001:db8:42::7/128 ! -d fe80::/10 -m comment --comment ${IPV6_RULE_MARKER} -j DROP"
        return 0
    fi
    if [[ "$1" == "-C" && "$*" == *"-s 2001:db8:42::/64"* ]]; then
        [[ "${V6_FILTER_INSTALLED}" == "true" ]]
        return
    fi
    if [[ "$1" == "-I" && "$*" == *"-s 2001:db8:42::/64"* ]]; then
        V6_FILTER_INSTALLED="true"
        return 0
    fi
    if [[ "$1" == "-D" && "$*" == *"-s 2001:db8:42::/64"* ]]; then
        V6_FILTER_INSTALLED="false"
        return 0
    fi
    return 1
}

# 1. Without a covering IPv6 pod CIDR (< /128) in K3S_IPV6_POD_CIDRS or local node routes,
# IPv4-only subnet quarantine is NOT sufficient to allow a restart when emergency NetworkPolicy
# fails in dual-stack k3s.
K3S_IPV6_POD_CIDRS=""
if preserve_fail_closed_dataplane "restart"; then
    exit 1
fi
[[ "${LAST_ERROR}" == *"Cannot determine IPv6 pod CIDR"* ]]

# A /128 IPv6 prefix covers only the old pod, not replacement pods, so it must also fail.
K3S_IPV6_POD_CIDRS="2001:db8:42::7/128"
if preserve_fail_closed_dataplane "restart"; then
    exit 1
fi

# 2. When the local node IPv6 routing table exposes the CNI pod subnet route (without needing
# Kubernetes Node API RBAC), ensure_k3s_ipv6_subnet_quarantine discovers and installs it even
# when K3S_IPV6_POD_CIDRS is empty or uses the standard default range.
K3S_IPV6_POD_CIDRS="2001:cafe:42::/56,fd00:10:42::/56"
ip() {
    case "$*" in
        "rule show pref 32763")
            printf '%s' "${V4_RULES}"
            return 0
            ;;
        "rule add from 10.42.0.0/16 blackhole protocol 200 pref 32763")
            V4_RULES=$'32763:\tfrom 10.42.0.0/16 blackhole proto 200\n'
            return 0
            ;;
        "rule del "*)
            V4_DELETED+="$*"$'\n'
            V4_RULES=""
            return 0
            ;;
        "-6 route show")
            printf '%s\n' "2001:db8:42::/64 dev cni0 proto kernel metric 256 pref medium"
            return 0
            ;;
        "-6 rule show pref 32763")
            printf '%s' "${V6_RULES}"
            return 0
            ;;
        "-6 rule add from 2001:db8:42::/64 blackhole protocol 200 pref 32763")
            V6_RULES=$'32763:\tfrom 2001:db8:42::/64 blackhole proto 200\n'
            return 0
            ;;
        "-6 rule del "*)
            V6_DELETED+="$*"$'\n'
            V6_RULES=""
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}
preserve_fail_closed_dataplane "restart"
[[ "${K3S_QUARANTINE_CIDR}" == "10.42.0.0/16" ]]
[[ "${K3S_IPV6_QUARANTINE_CIDR}" == "2001:db8:42::/64" ]]
[[ "${V4_RULES}" == *"from 10.42.0.0/16 blackhole proto 200"* ]]
[[ "${V6_RULES}" == *"from 2001:db8:42::/64 blackhole proto 200"* ]]
[[ "${V6_FILTER_INSTALLED}" == "true" ]]

# 3. Removing subnet quarantine removes both IPv4 and IPv6 pod-CIDR quarantine
# rules while preserving per-pod IPv6 containment rules.
remove_k3s_subnet_quarantine
[[ -z "${V4_RULES}" ]]
[[ -z "${V6_RULES}" ]]
[[ "${V6_FILTER_INSTALLED}" == "false" ]]
[[ -z "${K3S_IPV6_QUARANTINE_CIDR}" ]]
'''
    )
    assert result.returncode == 0, result.stderr


def test_k3s_shutdown_uses_managed_target_and_cold_pod_cidr_fallback_when_discovery_failed() -> None:
    result = run_bash(
        r'''
source "$1"

API_PID=""
exit() { return 0; }
K3S_MODE="true"
SECURE_MODE="false"
K3S_SHUTDOWN_NETPOL_ATTEMPTS=1
K3S_SHUTDOWN_RETRY_INTERVAL=0
K3S_BYPASS_CIDRS="10.43.0.0/16,10.42.0.0/16"

# (a) After an earlier successful reconcile, a failed detect_k3s_target clears
# DOCKER_TARGET_IP and K3S_TARGET_POD_NAME while K3S_POLICY_STATE_LOADED is
# already "true" and MANAGED_K3S_TARGET_IP is still set in memory. When SIGTERM
# arrives while the k8s API is still unavailable, cleanup must backfill from
# MANAGED_K3S_*, install per-pod guards for 10.42.1.7, and install pod-CIDR
# subnet quarantine.
K3S_POLICY_STATE_LOADED="true"
MANAGED_K3S_TARGET_IP="10.42.1.7"
MANAGED_K3S_TARGET_IMPL="lnd"
MANAGED_K3S_POD_NAME="lnd-0"
MANAGED_K3S_POD_NAMESPACE="lightning"
MANAGED_K3S_POD_SELECTOR="app=lnd"
DOCKER_TARGET_IP=""
K3S_TARGET_POD_NAME=""

GUARD_IP=""
BLACKHOLE_IP=""
DELETED_POD=""
V4_RULES=""

ensure_k3s_egress_guard() {
    GUARD_IP="$1"
    return 0
}
ensure_fallback_blackhole_rule() {
    BLACKHOLE_IP="$1"
    return 0
}
ensure_k3s_emergency_network_policy() {
    return 1
}
delete_k3s_target_pod() {
    DELETED_POD="${K3S_TARGET_POD_NAMESPACE}/${K3S_TARGET_POD_NAME}"
    return 0
}
cleanup_dataplane() {
    return 0
}
ip() {
    case "$*" in
        "rule show pref 32763")
            printf '%s' "${V4_RULES}"
            return 0
            ;;
        "rule add from 10.42.0.0/16 blackhole protocol 200 pref 32763")
            V4_RULES=$'32763:\tfrom 10.42.0.0/16 blackhole proto 200\n'
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

cleanup
[[ "${DOCKER_TARGET_IP}" == "10.42.1.7" ]]
[[ "${GUARD_IP}" == "10.42.1.7" ]]
[[ "${BLACKHOLE_IP}" == "10.42.1.7" ]]
[[ "${DELETED_POD}" == "lightning/lnd-0" ]]
[[ "${K3S_QUARANTINE_CIDR}" == "10.42.0.0/16" ]]
[[ "${V4_RULES}" == *"from 10.42.0.0/16 blackhole proto 200"* ]]

# (b) Cold shutdown where both DOCKER_TARGET_IP and MANAGED_K3S_TARGET_IP are
# empty and emergency NetworkPolicy fails: ensure_k3s_subnet_quarantine must
# still install the fallback 10.42.0.0/16 blackhole rule.
DOCKER_TARGET_IP=""
MANAGED_K3S_TARGET_IP=""
K3S_TARGET_POD_NAME=""
MANAGED_K3S_POD_NAME=""
K3S_QUARANTINE_CIDR=""
V4_RULES=""
GUARD_IP=""
BLACKHOLE_IP=""

cleanup
[[ -z "${GUARD_IP}" ]]
[[ -z "${BLACKHOLE_IP}" ]]
[[ "${K3S_QUARANTINE_CIDR}" == "10.42.0.0/16" ]]
[[ "${V4_RULES}" == *"from 10.42.0.0/16 blackhole proto 200"* ]]

# (c) Cold shutdown in dual-stack k3s before pod discovery succeeds (no saved IPv6
# state, empty TARGET_IPV6_ADDRESSES): both configured K3S_IPV6_POD_CIDRS and
# node CNI IPv6 pod routes trigger IPv6 pod-CIDR quarantine alongside IPv4.
IPV6_POLICY_STATE_LOADED="true"
IPV6_POLICY_TABLE_MANAGED="false"
TARGET_HAS_IPV6_DEFAULT_ROUTE="false"
TARGET_IPV6_ADDRESSES=()
MANAGED_TARGET_IPV6_ADDRESSES=()
MANAGED_K3S_TARGET_IPV6_ADDRESSES=()
K3S_IPV6_POD_CIDRS="2001:db8:42::/64"
K3S_QUARANTINE_CIDR=""
K3S_IPV6_QUARANTINE_CIDR=""
V4_RULES=""
V6_RULES=""
V6_FILTER_INSTALLED="false"

ip() {
    case "$*" in
        "rule show pref 32763")
            printf '%s' "${V4_RULES}"
            return 0
            ;;
        "rule add from 10.42.0.0/16 blackhole protocol 200 pref 32763")
            V4_RULES=$'32763:\tfrom 10.42.0.0/16 blackhole proto 200\n'
            return 0
            ;;
        "-6 rule show pref 32763")
            printf '%s' "${V6_RULES}"
            return 0
            ;;
        "-6 rule add from 2001:db8:42::/64 blackhole protocol 200 pref 32763")
            V6_RULES=$'32763:\tfrom 2001:db8:42::/64 blackhole proto 200\n'
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}
ip6tables() {
    if [[ "$1" == "-C" && "$*" == *"-s 2001:db8:42::/64"* ]]; then
        [[ "${V6_FILTER_INSTALLED}" == "true" ]]
        return
    fi
    if [[ "$1" == "-I" && "$*" == *"-s 2001:db8:42::/64"* ]]; then
        V6_FILTER_INSTALLED="true"
        return 0
    fi
    return 1
}

cleanup
[[ "${K3S_QUARANTINE_CIDR}" == "10.42.0.0/16" ]]
[[ "${K3S_IPV6_QUARANTINE_CIDR}" == "2001:db8:42::/64" ]]
[[ "${V6_RULES}" == *"from 2001:db8:42::/64 blackhole proto 200"* ]]
[[ "${V6_FILTER_INSTALLED}" == "true" ]]

# Even if K3S_IPV6_POD_CIDRS retains the shipped defaults while the cluster uses
# a custom local pod route on cni0, cold shutdown must quarantine the local pod route.
K3S_IPV6_POD_CIDRS="2001:cafe:42::/56,fd00:10:42::/56"
K3S_QUARANTINE_CIDR=""
K3S_IPV6_QUARANTINE_CIDR=""
V4_RULES=""
V6_RULES=""
V6_FILTER_INSTALLED="false"
ip() {
    case "$*" in
        "rule show pref 32763")
            printf '%s' "${V4_RULES}"
            return 0
            ;;
        "rule add from 10.42.0.0/16 blackhole protocol 200 pref 32763")
            V4_RULES=$'32763:\tfrom 10.42.0.0/16 blackhole proto 200\n'
            return 0
            ;;
        "-6 route show")
            printf '%s\n' \
                "2001:db8:1::/64 dev eth0 proto kernel metric 256 pref medium" \
                "2001:db8:42::/64 dev cni0 proto kernel metric 256 pref medium"
            return 0
            ;;
        "-6 rule show pref 32763")
            printf '%s' "${V6_RULES}"
            return 0
            ;;
        "-6 rule add from 2001:db8:42::/64 blackhole protocol 200 pref 32763")
            V6_RULES=$'32763:\tfrom 2001:db8:42::/64 blackhole proto 200\n'
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}
cleanup
[[ "${K3S_QUARANTINE_CIDR}" == "10.42.0.0/16" ]]
[[ "${K3S_IPV6_QUARANTINE_CIDR}" == "2001:db8:42::/64" ]]
[[ "${V6_RULES}" == *"from 2001:db8:42::/64 blackhole proto 200"* ]]
[[ "${V6_FILTER_INSTALLED}" == "true" ]]

# (d) Custom cluster where service CIDR (10.0.0.0/16) sorts before pod CIDR
# (10.244.0.0/16): CNI route discovery and KUBERNETES_SERVICE_HOST select
# 10.244.0.0/16, and when neither is available all candidate subnets are
# quarantined so 10.244.0.0/16 is never left unblocked.
K3S_BYPASS_CIDRS="10.0.0.0/16,10.244.0.0/16"
K3S_IPV6_POD_CIDRS=""
K3S_QUARANTINE_CIDR=""
K3S_IPV6_QUARANTINE_CIDR=""
V4_RULES=""
ip() {
    case "$*" in
        "rule show pref 32763")
            printf '%s' "${V4_RULES}"
            return 0
            ;;
        "-4 route show")
            printf '%s\n' "10.244.0.0/24 dev cni0 proto kernel scope link src 10.244.0.1"
            return 0
            ;;
        "rule add from 10.244.0.0/16 blackhole protocol 200 pref 32763")
            V4_RULES+=$'32763:\tfrom 10.244.0.0/16 blackhole proto 200\n'
            return 0
            ;;
        "rule add from 10.0.0.0/16 blackhole protocol 200 pref 32763")
            V4_RULES+=$'32763:\tfrom 10.0.0.0/16 blackhole proto 200\n'
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}
cleanup
[[ "${K3S_QUARANTINE_CIDR}" == "10.244.0.0/16" ]]
[[ "${V4_RULES}" == *"from 10.244.0.0/16 blackhole proto 200"* ]]
[[ "${V4_RULES}" != *"from 10.0.0.0/16"* ]]

# Without CNI routes or KUBERNETES_SERVICE_HOST, all candidate non-service
# bypass subnets are quarantined so 10.244.0.0/16 is still protected.
KUBERNETES_SERVICE_HOST=""
K3S_QUARANTINE_CIDR=""
V4_RULES=""
ip() {
    case "$*" in
        "rule show pref 32763")
            printf '%s' "${V4_RULES}"
            return 0
            ;;
        "rule add from 10.244.0.0/16 blackhole protocol 200 pref 32763")
            V4_RULES+=$'32763:\tfrom 10.244.0.0/16 blackhole proto 200\n'
            return 0
            ;;
        "rule add from 10.0.0.0/16 blackhole protocol 200 pref 32763")
            V4_RULES+=$'32763:\tfrom 10.0.0.0/16 blackhole proto 200\n'
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}
cleanup
[[ "${K3S_QUARANTINE_CIDR}" == "10.0.0.0/16,10.244.0.0/16" ]]
[[ "${V4_RULES}" == *"from 10.244.0.0/16 blackhole proto 200"* ]]

# If all bypass CIDRs are excluded as host LAN or service ranges, cold shutdown
# must fail closed without re-adding host LAN or service source blackholes.
K3S_BYPASS_CIDRS="192.168.1.0/24,10.43.0.0/16"
K3S_QUARANTINE_CIDR=""
V4_RULES=""
ip() {
    case "$*" in
        "-4 route show")
            printf '%s\n' "192.168.1.0/24 dev eth0 proto kernel scope link src 192.168.1.10"
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}
if ensure_k3s_subnet_quarantine ""; then
    exit 1
fi
[[ -z "${K3S_QUARANTINE_CIDR}" ]]
'''
    )
    assert result.returncode == 0, result.stderr


def test_k3s_configured_selector_metadata_retained_when_service_or_pod_discovery_fails():
    result = run_bash(
        r'''
TUNNELSATS_META_PATH="${POLICY_TEST_STATE_FILE}.meta"
printf '%s\n' '{"nodeType":"lnd"}' > "${TUNNELSATS_META_PATH}"
source "$1"

K3S_MODE="true"
LND_K8S_SERVICE="lnd"
LND_K8S_NAMESPACE="lightning"
LND_K8S_POD_SELECTOR="app.kubernetes.io/name=lnd"
PRE_GUARD_SELECTOR_CALLS=""
resolve_svc_ip() { return 1; }
pre_guard_colocated_k3s_candidate_pods_for_selector() {
    PRE_GUARD_SELECTOR_CALLS+="$1|$2;"
    return 0
}

if detect_k3s_target; then
    exit 1
fi
[[ "${K3S_TARGET_POD_NAMESPACE}" == "lightning" ]]
[[ "${K3S_TARGET_POD_SELECTOR}" == "app.kubernetes.io/name=lnd" ]]
[[ "${PRE_GUARD_SELECTOR_CALLS}" == *"lightning|app.kubernetes.io/name=lnd;"* ]]
[[ "${LAST_ERROR}" == *"No LND/CLN service resolved"* ]]

PAYLOAD_FILE="${POLICY_TEST_STATE_FILE}.np"
k8s_api_write_status() {
    printf '%s' "$3" > "${PAYLOAD_FILE}"
    printf '201'
}
k8s_api() {
    if [[ "$1" == *"/networkpolicies/tunnelsats-emergency-egress-deny" ]]; then
        cat "${PAYLOAD_FILE}"
        return 0
    fi
    if [[ "$1" == *"/networkpolicies" ]]; then
        printf '{"items":[]}'
        return 0
    fi
    return 1
}

ensure_k3s_emergency_network_policy
jq -e '.spec.podSelector.matchLabels["app.kubernetes.io/name"] == "lnd"' "${PAYLOAD_FILE}" >/dev/null

# Also retain namespace and selector when service resolves but pod lookup fails.
resolve_svc_ip() { printf '10.43.0.10\n'; return 0; }
resolve_k3s_target_pod() {
    LAST_ERROR="k3s: No Running LND pod found"
    return 1
}
if detect_k3s_target; then
    exit 1
fi
[[ "${K3S_TARGET_POD_NAMESPACE}" == "lightning" ]]
[[ "${K3S_TARGET_POD_SELECTOR}" == "app.kubernetes.io/name=lnd" ]]
'''
    )
    assert result.returncode == 0, result.stderr


def test_k3s_unready_colocated_replacement_pod_is_pre_guarded_before_ready():
    result = run_bash(
        r'''
source "$1"

K3S_MODE="true"
TUNNELSATS_K8S_NODE_NAME="worker-a"
PRE_GUARDED_IPS=""
BLACKHOLED_IPS=""
V6_FILTER_RULES=""
V6_BLACKHOLE_RULES=""

k8s_api() {
    printf '%s\n' '{
        "items": [
            {
                "metadata": {"name": "lnd-pending"},
                "spec": {"nodeName": "worker-a"},
                "status": {
                    "phase": "Pending",
                    "podIP": "10.42.1.8",
                    "podIPs": [{"ip": "10.42.1.8"}, {"ip": "2001:db8:42::8"}],
                    "conditions": [{"type": "Ready", "status": "False"}]
                }
            },
            {
                "metadata": {"name": "lnd-terminating", "deletionTimestamp": "2026-04-12T00:00:00Z"},
                "spec": {"nodeName": "worker-a"},
                "status": {
                    "phase": "Running",
                    "podIP": "10.42.1.7",
                    "podIPs": [{"ip": "10.42.1.7"}]
                }
            }
        ]
    }'
}
ensure_k3s_egress_guard_additive() {
    PRE_GUARDED_IPS+="$1,"
    return 0
}
ensure_fallback_blackhole_rule() {
    BLACKHOLED_IPS+="$1,"
    return 0
}
ip6tables() {
    if [[ "$1" == "-C" ]]; then
        return 1
    fi
    if [[ "$1" == "-I" ]]; then
        V6_FILTER_RULES+="$*"$'\n'
        return 0
    fi
    return 0
}
ip() {
    if [[ "$*" == "-6 rule show pref ${IPV6_BLACKHOLE_RULE_PREF}" ]]; then
        printf '%s' "${V6_BLACKHOLE_RULES}"
        return 0
    fi
    if [[ "$1 $2 $3" == "-6 rule add" ]]; then
        V6_BLACKHOLE_RULES+="${IPV6_BLACKHOLE_RULE_PREF}: from 2001:db8:42::8 blackhole"$'\n'
        return 0
    fi
    return 0
}

if resolve_k3s_target_pod "lnd" "lightning" "app=lnd"; then
    exit 1
fi
[[ "${K3S_POD_API_QUERY_OK}" == "true" ]]
[[ "${PRE_GUARDED_IPS}" == *"10.42.1.8,"* ]]
[[ "${PRE_GUARDED_IPS}" == *"10.42.1.7,"* ]]
[[ "${BLACKHOLED_IPS}" == *"10.42.1.8,"* ]]
[[ "${BLACKHOLED_IPS}" == *"10.42.1.7,"* ]]
[[ "${V6_FILTER_RULES}" == *"2001:db8:42::8"* ]]
[[ "${V6_BLACKHOLE_RULES}" == *"from 2001:db8:42::8 blackhole"* ]]
[[ ",${K3S_CANDIDATE_POD_IPS}," == *",10.42.1.8,"* ]]
[[ ",${K3S_CANDIDATE_POD_IPS}," == *",10.42.1.7,"* ]]
[[ ",${K3S_ALL_MATCHING_LOCAL_POD_IPS}," == *",10.42.1.8,"* ]]
[[ ",${K3S_ALL_MATCHING_LOCAL_POD_IPS}," == *",10.42.1.7,"* ]]

# Verify retire_stale_k3s_unmatched_ip_guards removes recycled IP 10.42.1.6
# (not in K3S_ALL_MATCHING_LOCAL_POD_IPS) while keeping 10.42.1.7 and 10.42.1.8.
FORWARD_RULES=$'-A FORWARD -s 10.42.1.6/32 -m comment --comment tunnelsats-k3s-egress-guard -j DROP\n-A FORWARD -s 10.42.1.7/32 -m comment --comment tunnelsats-k3s-egress-guard -j DROP\n-A FORWARD -s 10.42.1.8/32 -m comment --comment tunnelsats-k3s-egress-guard -j DROP\n'
RULE_32765=$'32765:\tfrom 10.42.1.6 blackhole proto 200\n32765:\tfrom 10.42.1.7 blackhole proto 200\n32765:\tfrom 10.42.1.8 blackhole proto 200\n'
iptables() {
    if [[ "$*" == "-S FORWARD" ]]; then
        printf '%s' "${FORWARD_RULES}"
        return 0
    fi
    if [[ "$1 $2" == "-D FORWARD" ]]; then
        if [[ "$*" == *"10.42.1.6"* ]]; then
            FORWARD_RULES="${FORWARD_RULES//$'-A FORWARD -s 10.42.1.6/32 -m comment --comment tunnelsats-k3s-egress-guard -j DROP\n'/}"
        fi
        if [[ "$*" == *"10.42.1.7"* ]]; then
            FORWARD_RULES="${FORWARD_RULES//$'-A FORWARD -s 10.42.1.7/32 -m comment --comment tunnelsats-k3s-egress-guard -j DROP\n'/}"
        fi
        return 0
    fi
    return 0
}
ip() {
    if [[ "$*" == "rule show pref 32765" ]]; then
        printf '%s' "${RULE_32765}"
        return 0
    fi
    if [[ "$1 $2" == "rule del" ]]; then
        if [[ "$*" == *"10.42.1.6"* ]]; then
            RULE_32765="${RULE_32765//$'32765:\tfrom 10.42.1.6 blackhole proto 200\n'/}"
        fi
        if [[ "$*" == *"10.42.1.7"* ]]; then
            RULE_32765="${RULE_32765//$'32765:\tfrom 10.42.1.7 blackhole proto 200\n'/}"
        fi
        return 0
    fi
    return 0
}

# When K3S_POD_API_QUERY_OK is false, nothing may be retired.
K3S_POD_API_QUERY_OK="false"
retire_stale_k3s_unmatched_ip_guards
[[ "${FORWARD_RULES}" == *"10.42.1.6/32"* ]]
[[ "${RULE_32765}" == *"from 10.42.1.6 blackhole"* ]]

# When K3S_POD_API_QUERY_OK is true, 10.42.1.6 is retired while 10.42.1.7 and 10.42.1.8 remain.
K3S_POD_API_QUERY_OK="true"
retire_stale_k3s_unmatched_ip_guards
[[ "${FORWARD_RULES}" != *"10.42.1.6/32"* ]]
[[ "${RULE_32765}" != *"from 10.42.1.6 blackhole"* ]]
[[ "${FORWARD_RULES}" == *"10.42.1.7/32"* ]]
[[ "${FORWARD_RULES}" == *"10.42.1.8/32"* ]]
[[ "${RULE_32765}" == *"from 10.42.1.7 blackhole"* ]]
[[ "${RULE_32765}" == *"from 10.42.1.8 blackhole"* ]]
'''
    )
    assert result.returncode == 0, result.stderr


def test_k3s_reconcile_quarantines_pod_cidr_and_network_policy_when_target_discovery_fails():
    result = run_bash(
        r'''
source "$1"

TEST_TMP="$(mktemp -d)"
trap 'rm -rf "${TEST_TMP}"' EXIT

K3S_MODE="true"
DECOMMISSION_STATE_FILE="${TEST_TMP}/decommissioned.json"
K3S_POLICY_STATE_FILE="${TEST_TMP}/k3s-policy-rules.json"
IPV6_POLICY_STATE_FILE="${TEST_TMP}/ipv6-policy-rules.json"
STATE_FILE="${TEST_TMP}/state.json"
RECONCILE_TRIGGER_DIR="${TEST_TMP}/triggers"
RECONCILE_RESULT_DIR="${TEST_TMP}/results"
TUNNELSATS_META_PATH="${TEST_TMP}/meta.json"
TUNNELSATS_K8S_NODE_NAME="worker-a"
LND_K8S_SERVICE="umbrel-lnd"
LND_K8S_NAMESPACE="lightning"
LND_K8S_POD_SELECTOR="app.kubernetes.io/name=lnd"
MANAGED_K3S_TARGET_IP="10.42.1.6"
K3S_POLICY_STATE_LOADED="true"

NETPOL_ENSURED=0
SUBNET_QUARANTINED=""
RETIRED_STALE=0
CLEANED_DATAPLANE=0

ensure_reconcile_kill_switch() { return 0; }
resolve_svc_ip() { printf '10.43.0.10\n'; }
k8s_api() {
    printf '%s\n' '{"items": []}'
}
ensure_k3s_emergency_network_policy() {
    NETPOL_ENSURED=$((NETPOL_ENSURED + 1))
    return 0
}
ensure_k3s_subnet_quarantine() {
    SUBNET_QUARANTINED="${1:-cidr}"
    K3S_QUARANTINE_CIDR="10.42.1.0/24"
    return 0
}
retire_stale_k3s_unmatched_ip_guards() {
    RETIRED_STALE=$((RETIRED_STALE + 1))
    return 0
}
cleanup_dataplane() {
    CLEANED_DATAPLANE=$((CLEANED_DATAPLANE + 1))
    return 0
}
write_state() { :; }

if reconcile_once "periodic"; then
    exit 1
fi
[[ "${NETPOL_ENSURED}" -ge 1 ]]
[[ -n "${SUBNET_QUARANTINED}" ]]
[[ "${RETIRED_STALE}" -ge 1 ]]
[[ "${CLEANED_DATAPLANE}" -ge 1 ]]
[[ "${K3S_TARGET_POD_NAMESPACE}" == "lightning" ]]
[[ "${K3S_TARGET_POD_SELECTOR}" == "app.kubernetes.io/name=lnd" ]]
[[ "${LAST_ERROR}" == *"No Running LND pod found"* ]]
'''
    )
    assert result.returncode == 0, result.stderr


def test_k3s_verify_target_routes_rejects_shadowing_rule_and_unsafe_route_get():
    result = run_bash(
        r'''
source "$1"

K3S_MODE="true"
SECURE_MODE="false"
DOCKER_TARGET_IP="10.42.1.8"
WG_IFACE="tunnelsatsv2"
K3S_BYPASS_CIDRS="10.42.0.0/16,10.43.0.0/16"
normalize_k3s_bypass_cidrs

RULE_LIST=$'0:\tfrom all lookup local\n32500:\tfrom 10.42.1.8 to 10.42.0.0/16 lookup main proto 200\n32500:\tfrom 10.42.1.8 to 10.43.0.0/16 lookup main proto 200\n32764:\tfrom 10.42.1.8 lookup 51820 proto 200\n32765:\tfrom 10.42.1.8 blackhole proto 200\n'
ROUTE_GET_OUT="1.1.1.1 dev tunnelsatsv2 table 51820 src 10.42.1.8 uid 0"

ip() {
    if [[ "$*" == "rule show" ]] || [[ "$*" == "-4 rule show" ]]; then
        printf '%s' "${RULE_LIST}"
        return 0
    fi
    if [[ "$1 $2 $3" == "-4 route get" ]] || [[ "$1 $2" == "route get" ]]; then
        printf '%s\n' "${ROUTE_GET_OUT}"
        return 0
    fi
    return 0
}

# Clean owned rules + WireGuard route get must pass.
verify_k3s_target_routes

# Unrelated rules (e.g. non-zero fwmark or LAN destination not covering public probes) must not block a healthy tunnel.
RULE_LIST=$'0:\tfrom all lookup local\n100:\tfrom all to 192.168.1.0/24 lookup main\n101:\tfrom all fwmark 0x4000/0x4000 lookup main\n32500:\tfrom 10.42.1.8 to 10.42.0.0/16 lookup main proto 200\n32764:\tfrom 10.42.1.8 lookup 51820 proto 200\n32765:\tfrom 10.42.1.8 blackhole proto 200\n'
verify_k3s_target_routes

# A port/protocol-qualified rule matching Lightning traffic at pref 100 must fail.
RULE_LIST=$'0:\tfrom all lookup local\n100:\tfrom 10.42.1.0/24 ipproto tcp dport 9735 lookup main\n32764:\tfrom 10.42.1.8 lookup 51820 proto 200\n32765:\tfrom 10.42.1.8 blackhole proto 200\n'
if verify_k3s_target_routes; then
    exit 1
fi
[[ "${LAST_ERROR}" == *"shadows target pod routing"* ]]

# An unowned higher-priority rule matching 10.42.1.0/24 at pref 100 must fail.
RULE_LIST=$'0:\tfrom all lookup local\n100:\tfrom 10.42.1.0/24 lookup main\n32764:\tfrom 10.42.1.8 lookup 51820 proto 200\n32765:\tfrom 10.42.1.8 blackhole proto 200\n'
if verify_k3s_target_routes; then
    exit 1
fi
[[ "${LAST_ERROR}" == *"shadows target pod routing"* ]]

# Inverted rule `not from 10.42.1.8 lookup main` (no extra selectors, to all)
# never matches target_ip 10.42.1.8 and must pass, whereas `not from all to 0.0.0.0/4`
# or `not from 10.42.1.8 fwmark 0x1` matches target traffic and must fail.
RULE_LIST=$'0:\tfrom all lookup local\n100:\tnot from 10.42.1.8 lookup main\n32764:\tfrom 10.42.1.8 lookup 51820 proto 200\n32765:\tfrom 10.42.1.8 blackhole proto 200\n'
verify_k3s_target_routes

RULE_LIST=$'0:\tfrom all lookup local\n100:\tnot from all to 0.0.0.0/4 lookup main\n32764:\tfrom 10.42.1.8 lookup 51820 proto 200\n32765:\tfrom 10.42.1.8 blackhole proto 200\n'
if verify_k3s_target_routes; then
    exit 1
fi
[[ "${LAST_ERROR}" == *"shadows target pod routing"* ]]

RULE_LIST=$'0:\tfrom all lookup local\n100:\tnot from 10.42.1.8 fwmark 0x1 lookup main\n32764:\tfrom 10.42.1.8 lookup 51820 proto 200\n32765:\tfrom 10.42.1.8 blackhole proto 200\n'
if verify_k3s_target_routes; then
    exit 1
fi
[[ "${LAST_ERROR}" == *"shadows target pod routing"* ]]

# Restore clean rule list, but make route get resolve via clear0 instead of tunnelsatsv2.
RULE_LIST=$'0:\tfrom all lookup local\n32764:\tfrom 10.42.1.8 lookup 51820 proto 200\n32765:\tfrom 10.42.1.8 blackhole proto 200\n'
ROUTE_GET_OUT="1.1.1.1 via 192.0.2.2 dev clear0 src 10.42.1.8 uid 0"
if verify_k3s_target_routes; then
    exit 1
fi
[[ "${LAST_ERROR}" == *"Unsafe k3s route"* ]]

# When owned pod-CIDR quarantine is active at pref 32763, kernel ip route get
# returns RTNETLINK answers: Invalid argument. Pre-release sync check must pass
# with K3S_ALLOW_ACTIVE_QUARANTINE_DURING_SYNC=true and fail once false.
RULE_LIST=$'0:\tfrom all lookup local\n32500:\tfrom 10.42.1.8 to 10.42.0.0/16 lookup main proto 200\n32763:\tfrom 10.42.0.0/16 blackhole proto 200\n32764:\tfrom 10.42.1.8 lookup 51820 proto 200\n32765:\tfrom 10.42.1.8 blackhole proto 200\n'
ROUTE_GET_OUT="RTNETLINK answers: Invalid argument"
K3S_ALLOW_ACTIVE_QUARANTINE_DURING_SYNC="true" verify_k3s_target_routes
if K3S_ALLOW_ACTIVE_QUARANTINE_DURING_SYNC="false" verify_k3s_target_routes; then
    exit 1
fi
[[ "${LAST_ERROR}" == *"Emergency quarantine rule"* ]]
'''
    )
    assert result.returncode == 0, result.stderr


def test_k3s_pod_watch_event_immediately_pre_guards_pod_and_writes_reconcile_trigger():
    result = run_bash(
        r'''
source "$1"

TEST_TMP="$(mktemp -d)"
trap 'rm -rf "${TEST_TMP}"' EXIT

K3S_MODE="true"
TUNNELSATS_K8S_NODE_NAME="worker-a"
LND_K8S_SERVICE="umbrel-lnd"
LND_K8S_NAMESPACE="lightning"
LND_K8S_POD_SELECTOR="app.kubernetes.io/name=lnd"
K3S_TARGET_POD_NAMESPACE="lightning"
K3S_TARGET_POD_SELECTOR="app.kubernetes.io/name=lnd"
RECONCILE_TRIGGER_DIR="${TEST_TMP}/triggers"
RECONCILE_RESULT_DIR="${TEST_TMP}/results"
K3S_POLICY_STATE_FILE="${TEST_TMP}/k3s-policy.json"
IPV6_POLICY_STATE_FILE="${TEST_TMP}/ipv6-policy.json"
WATCH_URL_FILE="${TEST_TMP}/watch_url"
PRE_GUARDED=""
BLACKHOLED=""

k8s_watch_api() {
    printf '%s' "$1" > "${WATCH_URL_FILE}"
    printf '%s\n' '{"type":"MODIFIED","object":{"metadata":{"name":"umbrel-lnd-0"},"spec":{"nodeName":"worker-a"},"status":{"phase":"Pending","podIP":"10.42.1.9","podIPs":[{"ip":"10.42.1.9"},{"ip":"2001:db8:42::9"}]}}}'
}
ensure_k3s_egress_guard_additive() {
    PRE_GUARDED+="$1,"
    return 0
}
ensure_fallback_blackhole_rule() {
    BLACKHOLED+="$1,"
    return 0
}
ensure_ipv6_source_blackhole_rule() {
    return 0
}
ip6tables() {
    return 0
}

watch_k3s_target_pods_once "lightning" "app.kubernetes.io/name=lnd"
[[ "${PRE_GUARDED}" == *"10.42.1.9,"* ]]
[[ "${BLACKHOLED}" == *"10.42.1.9,"* ]]
[[ "${K3S_WATCHED_CANDIDATE_POD_IPS}" == *"10.42.1.9"* ]]
[[ "${K3S_WATCHED_CANDIDATE_POD_IPV6_ADDRESSES}" == *"2001:db8:42::9"* ]]
[[ -f "${RECONCILE_TRIGGER_DIR}/k3s-pod-watch.trigger" ]]

# A successful full pod list during detect_k3s_target replaces stale watched
# candidates (10.42.1.9) with the fresh pod list (10.42.1.10), records
# resourceVersion, and a subsequent watch resumes from resourceVersion.
resolve_svc_ip() { printf '10.43.0.10\n'; }
k8s_api() {
    printf '%s\n' '{
        "metadata": {"resourceVersion": "420"},
        "items": [
            {
                "metadata": {"name": "umbrel-lnd-1", "resourceVersion": "419"},
                "spec": {"nodeName": "worker-a"},
                "status": {
                    "phase": "Running",
                    "podIP": "10.42.1.10",
                    "podIPs": [{"ip": "10.42.1.10"}, {"ip": "2001:db8:42::10"}],
                    "conditions": [{"type": "Ready", "status": "True"}]
                }
            }
        ]
    }'
}
detect_k3s_target
[[ "${K3S_WATCH_RESOURCE_VERSION}" == "420" ]]
[[ "${K3S_CANDIDATE_POD_IPS}" == "10.42.1.10" ]]
[[ "${K3S_ALL_MATCHING_LOCAL_POD_IPS}" == "10.42.1.10" ]]
[[ "${K3S_WATCHED_CANDIDATE_POD_IPS}" == "10.42.1.10" ]]
[[ "${K3S_CANDIDATE_POD_IPV6_ADDRESSES}" == "2001:db8:42::10" ]]
[[ "${K3S_WATCHED_CANDIDATE_POD_IPV6_ADDRESSES}" == "2001:db8:42::10" ]]

# Unchanged replay of the already-synced active pod updates resourceVersion
# without writing a reconcile trigger.
rm -f "${RECONCILE_TRIGGER_DIR}/k3s-pod-watch.trigger"
RULES_SYNCED="true"
k8s_watch_api() {
    printf '%s' "$1" > "${WATCH_URL_FILE}"
    printf '%s\n' '{"type":"ADDED","object":{"metadata":{"name":"umbrel-lnd-1","resourceVersion":"421"},"spec":{"nodeName":"worker-a"},"status":{"phase":"Running","podIP":"10.42.1.10","podIPs":[{"ip":"10.42.1.10"},{"ip":"2001:db8:42::10"}],"conditions":[{"type":"Ready","status":"True"}]}}}'
}
watch_k3s_target_pods_once "lightning" "app.kubernetes.io/name=lnd"
[[ "$(cat "${WATCH_URL_FILE}")" == *"resourceVersion=420"* ]]
[[ "${K3S_WATCH_RESOURCE_VERSION}" == "421" ]]
[[ ! -f "${RECONCILE_TRIGGER_DIR}/k3s-pod-watch.trigger" ]]

# Explicit DELETED watch event removes the deleted pod address immediately and triggers reconcile.
k8s_watch_api() {
    printf '%s' "$1" > "${WATCH_URL_FILE}"
    printf '%s\n' '{"type":"DELETED","object":{"metadata":{"name":"umbrel-lnd-1","resourceVersion":"422"},"spec":{"nodeName":"worker-a"},"status":{"phase":"Running","podIP":"10.42.1.10","podIPs":[{"ip":"10.42.1.10"},{"ip":"2001:db8:42::10"}]}}}'
}
watch_k3s_target_pods_once "lightning" "app.kubernetes.io/name=lnd"
[[ "$(cat "${WATCH_URL_FILE}")" == *"resourceVersion=421"* ]]
[[ "${K3S_WATCH_RESOURCE_VERSION}" == "422" ]]
[[ -f "${RECONCILE_TRIGGER_DIR}/k3s-pod-watch.trigger" ]]
[[ -z "${K3S_CANDIDATE_POD_IPS}" ]]
[[ -z "${K3S_ALL_MATCHING_LOCAL_POD_IPS}" ]]
[[ -z "${K3S_WATCHED_CANDIDATE_POD_IPS}" ]]
[[ -z "${K3S_CANDIDATE_POD_IPV6_ADDRESSES}" ]]
[[ -z "${K3S_WATCHED_CANDIDATE_POD_IPV6_ADDRESSES}" ]]

# Watch ERROR event (e.g. 410 Gone) resets K3S_WATCH_RESOURCE_VERSION and returns non-zero.
k8s_watch_api() {
    printf '%s\n' '{"type":"ERROR","object":{"kind":"Status","code":410}}'
}
if watch_k3s_target_pods_once "lightning" "app.kubernetes.io/name=lnd"; then
    exit 1
fi
[[ -z "${K3S_WATCH_RESOURCE_VERSION}" ]]
'''
    )
    assert result.returncode == 0, result.stderr


def test_k3s_failed_replacement_pod_pre_guard_stops_reconcile_and_retains_quarantine():
    result = run_bash(
        r'''
source "$1"

TEST_TMP="$(mktemp -d)"
trap 'rm -rf "${TEST_TMP}"' EXIT

K3S_MODE="true"
SECURE_MODE="false"
TUNNELSATS_K8S_NODE_NAME="worker-a"
LND_K8S_SERVICE="umbrel-lnd"
LND_K8S_NAMESPACE="lightning"
LND_K8S_POD_SELECTOR="app=lnd"
DECOMMISSION_STATE_FILE="${TEST_TMP}/decommissioned.json"
K3S_POLICY_STATE_FILE="${TEST_TMP}/k3s-policy.json"
IPV6_POLICY_STATE_FILE="${TEST_TMP}/ipv6-policy.json"
STATE_FILE="${TEST_TMP}/state.json"
QUARANTINED=0
NETPOL_COUNT=0
WG_UP_COUNT=0

resolve_svc_ip() { printf '10.43.0.10\n'; }
k8s_api() {
    printf '%s\n' '{
        "items": [
            {
                "metadata": {"name": "lnd-ready"},
                "spec": {"nodeName": "worker-a"},
                "status": {
                    "phase": "Running",
                    "podIP": "10.42.1.7",
                    "conditions": [{"type": "Ready", "status": "True"}]
                }
            },
            {
                "metadata": {"name": "lnd-replacement"},
                "spec": {"nodeName": "worker-a"},
                "status": {
                    "phase": "Pending",
                    "podIP": "10.42.1.8",
                    "conditions": [{"type": "Ready", "status": "False"}]
                }
            }
        ]
    }'
}
# Replacement pod 10.42.1.8 fails both iptables DROP and source blackhole.
ensure_k3s_egress_guard_additive() {
    [[ "$1" == "10.42.1.7" ]]
}
ensure_fallback_blackhole_rule() {
    [[ "$1" == "10.42.1.7" ]]
}
ensure_k3s_emergency_network_policy() {
    NETPOL_COUNT=$((NETPOL_COUNT + 1))
    return 0
}
ensure_k3s_subnet_quarantine() {
    QUARANTINED=$((QUARANTINED + 1))
    K3S_QUARANTINE_CIDR="10.42.0.0/16"
    return 0
}
cleanup_dataplane() { return 0; }
ensure_wg_up() {
    WG_UP_COUNT=$((WG_UP_COUNT + 1))
    return 0
}
write_state() { :; }

if reconcile_once "periodic"; then
    exit 1
fi
[[ "${WG_UP_COUNT}" == "0" ]]
[[ "${QUARANTINED}" -ge 1 ]]
[[ "${NETPOL_COUNT}" -ge 1 ]]
[[ "${K3S_CANDIDATE_PODS_GUARDED}" == "false" ]]
[[ "${LAST_ERROR}" == *"Failed to pre-guard co-located candidate pod 10.42.1.8"* ]]

# Also verify k3s_candidate_pod_guards_are_synced rejects missing per-pod guards
# for any non-selected candidate pod before shared quarantine can be released.
DOCKER_TARGET_IP="10.42.1.7"
K3S_CANDIDATE_POD_IPS="10.42.1.7,10.42.1.8"
K3S_CANDIDATE_PODS_GUARDED="true"
ip() { return 1; }
iptables() { return 1; }
if k3s_candidate_pod_guards_are_synced; then
    exit 1
fi
[[ "${LAST_ERROR}" == *"10.42.1.8 is missing per-pod isolation guard"* ]]
'''
    )
    assert result.returncode == 0, result.stderr


def test_k3s_pending_unassigned_replacement_and_pre_release_discovery_refresh():
    result = run_bash(
        r'''
source "$1"

TEST_TMP="$(mktemp -d)"
trap 'stop_k3s_pod_watcher; rm -rf "${TEST_TMP}"' EXIT

K3S_MODE="true"
SECURE_MODE="false"
TUNNELSATS_K8S_NODE_NAME="worker-a"
K3S_TARGET_POD_NAMESPACE="lightning"
K3S_TARGET_POD_SELECTOR="app=lnd"
DOCKER_TARGET_IP="10.42.1.7"
K3S_POD_API_QUERY_OK="true"
K3S_POLICY_STATE_FILE="${TEST_TMP}/k3s-policy.json"
IPV6_POLICY_STATE_FILE="${TEST_TMP}/ipv6-policy.json"
RECONCILE_TRIGGER_DIR="${TEST_TMP}/triggers"

EVENTS=""
ensure_k3s_egress_guard_additive() {
    EVENTS+="guard:$1;"
    return 0
}
ensure_fallback_blackhole_rule() {
    EVENTS+="blackhole:$1;"
    return 0
}
ensure_k3s_emergency_network_policy() {
    EVENTS+="netpol:ensure;"
    return 0
}
ensure_k3s_subnet_quarantine() {
    EVENTS+="quarantine:ensure;"
    K3S_QUARANTINE_CIDR="10.42.0.0/16"
    return 0
}
remove_k3s_subnet_quarantine() {
    EVENTS+="quarantine:remove;"
    return 0
}
remove_k3s_egress_guards() {
    EVENTS+="guards:remove;"
    return 0
}
remove_k3s_emergency_network_policy() {
    EVENTS+="netpol:remove;"
    return 0
}
rules_are_synced() {
    return 0
}

# 1. While replacement pod lnd-replacement has no assigned podIP yet,
# release_k3s_reconcile_guards must keep pre-discovery containment and refuse
# to remove subnet quarantine or emergency NetworkPolicy.
k8s_api() {
    printf '%s\n' '{
        "metadata": {"resourceVersion": "500"},
        "items": [
            {
                "metadata": {"name": "lnd-0"},
                "spec": {"nodeName": "worker-a"},
                "status": {
                    "phase": "Running",
                    "podIP": "10.42.1.7",
                    "conditions": [{"type": "Ready", "status": "True"}]
                }
            },
            {
                "metadata": {"name": "lnd-replacement"},
                "spec": {"nodeName": "worker-a"},
                "status": {
                    "phase": "Pending",
                    "podIP": ""
                }
            }
        ]
    }'
}
if release_k3s_reconcile_guards; then
    exit 1
fi
[[ "${K3S_UNSEEN_REPLACEMENT_PENDING}" == "true" ]]
[[ "${LAST_ERROR}" == *"pending IP assignment"* ]]
[[ "${EVENTS}" != *"quarantine:remove"* ]]
[[ "${EVENTS}" != *"netpol:remove"* ]]
[[ "${EVENTS}" == *"quarantine:ensure"* ]]

# 2. Once CNI assigns podIP 10.42.1.8 to lnd-replacement during guard release,
# refresh_k3s_discovery_before_guard_release pre-guards 10.42.1.8 BEFORE
# subnet quarantine and emergency NetworkPolicy are removed.
EVENTS=""
k8s_api() {
    printf '%s\n' '{
        "metadata": {"resourceVersion": "501"},
        "items": [
            {
                "metadata": {"name": "lnd-0"},
                "spec": {"nodeName": "worker-a"},
                "status": {
                    "phase": "Running",
                    "podIP": "10.42.1.7",
                    "conditions": [{"type": "Ready", "status": "True"}]
                }
            },
            {
                "metadata": {"name": "lnd-replacement"},
                "spec": {"nodeName": "worker-a"},
                "status": {
                    "phase": "Pending",
                    "podIP": "10.42.1.8",
                    "conditions": [{"type": "Ready", "status": "False"}]
                }
            }
        ]
    }'
}
release_k3s_reconcile_guards
[[ "${K3S_UNSEEN_REPLACEMENT_PENDING}" == "false" ]]
[[ ",${K3S_CANDIDATE_POD_IPS}," == *",10.42.1.8,"* ]]
[[ "${EVENTS}" == *"guard:10.42.1.8;blackhole:10.42.1.8;quarantine:remove;"* ]]
[[ "${EVENTS}" == *"netpol:remove;"* ]]
[[ "${EVENTS}" == *"guards:remove;" ]]

# 3. Background watcher starts, persists watch state across subshell boundary,
# refreshes promoted replacement pod state from disk without re-blocking the
# healthy promoted pod, and stops cleanly via stop_k3s_pod_watcher.
BG_GUARD_LOG="${TEST_TMP}/bg-guards.log"
: > "${BG_GUARD_LOG}"
ensure_k3s_egress_guard_additive() {
    printf '%s\n' "$1" >> "${BG_GUARD_LOG}"
    return 0
}
iptables() {
    if [[ "$1" == "-S" && "$2" == "FORWARD" ]]; then
        return 0
    fi
    return 0
}
ip() {
    if [[ "$1" == "rule" && "$2" == "show" && "${3:-}" == "pref" && "${4:-}" == "${K3S_TUNNEL_RULE_PREF}" ]]; then
        printf '%s\n' "${K3S_TUNNEL_RULE_PREF}: from 10.42.1.8 lookup 51820 proto ${K3S_RULE_PROTOCOL}"
        return 0
    fi
    if [[ "$1" == "route" && "$2" == "show" && "${3:-}" == "table" && "${4:-}" == "51820" ]]; then
        printf 'default dev %s metric 2\n' "${WG_IFACE}"
        return 0
    fi
    return 0
}
WATCH_EVENT_FILE="${TEST_TMP}/watch-event.json"
printf '%s\n' '{"type":"MODIFIED","object":{"metadata":{"name":"lnd-bg","resourceVersion":"555"},"spec":{"nodeName":"worker-a"},"status":{"phase":"Pending","podIP":"10.42.1.11","podIPs":[{"ip":"10.42.1.11"}]}}}' > "${WATCH_EVENT_FILE}"
k8s_watch_api() {
    cat "${WATCH_EVENT_FILE}"
    sleep 0.2
}
ensure_k3s_pod_watcher_running
[[ -n "${K3S_WATCHER_PID}" ]]
kill -0 "${K3S_WATCHER_PID}"
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    load_k3s_pod_watch_state
    if [[ "${K3S_WATCH_RESOURCE_VERSION}" == "555" ]]; then
        break
    fi
    sleep 0.1
done
[[ "${K3S_WATCH_RESOURCE_VERSION}" == "555" ]]
[[ ",${K3S_WATCHED_CANDIDATE_POD_IPS}," == *",10.42.1.11,"* ]]
grep -Fxq "10.42.1.11" "${BG_GUARD_LOG}"

# Simulate promotion of replacement pod lnd-replacement (10.42.1.8) in the
# parent process while the background watcher stays running.
MANAGED_K3S_TARGET_IP="10.42.1.8"
MANAGED_K3S_TARGET_IMPL="lnd"
MANAGED_K3S_POD_NAME="lnd-replacement"
MANAGED_K3S_POD_NAMESPACE="lightning"
MANAGED_K3S_POD_SELECTOR="app=lnd"
K3S_POLICY_STATE_LOADED="true"
persist_managed_k3s_policy_state
jq -n '{target_ip:"10.42.1.8", rules_synced:true}' > "${STATE_FILE}"
: > "${BG_GUARD_LOG}"
rm -f "${RECONCILE_TRIGGER_DIR}/k3s-pod-watch.trigger"
printf '%s\n' '{"type":"MODIFIED","object":{"metadata":{"name":"lnd-replacement","resourceVersion":"556"},"spec":{"nodeName":"worker-a"},"status":{"phase":"Running","podIP":"10.42.1.8","podIPs":[{"ip":"10.42.1.8"}],"conditions":[{"type":"Ready","status":"True"}]}}}' > "${WATCH_EVENT_FILE}"
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    load_k3s_pod_watch_state
    if [[ "${K3S_WATCH_RESOURCE_VERSION}" == "556" ]]; then
        break
    fi
    sleep 0.1
done
[[ "${K3S_WATCH_RESOURCE_VERSION}" == "556" ]]
! grep -Fxq "10.42.1.8" "${BG_GUARD_LOG}"
[[ ! -f "${RECONCILE_TRIGGER_DIR}/k3s-pod-watch.trigger" ]]

stop_k3s_pod_watcher
[[ -z "${K3S_WATCHER_PID}" ]]
'''
    )
    assert result.returncode == 0, result.stderr


def test_k3s_pod_watch_state_locking_and_error_propagation():
    result = run_bash(
        r'''
source "$1"

TEST_TMP="$(mktemp -d)"
trap 'rm -rf "${TEST_TMP}"' EXIT

K3S_POLICY_STATE_FILE="${TEST_TMP}/k3s-policy.json"
K3S_WATCH_SCOPE="lightning|app=lnd"
K3S_WATCH_RESOURCE_VERSION="700"
K3S_WATCHED_CANDIDATE_POD_IPS="10.42.1.8"
K3S_WATCHED_CANDIDATE_POD_IPV6_ADDRESSES="2001:db8:42::8"
K3S_UNSEEN_REPLACEMENT_PENDING="true"

# 1. persist_k3s_pod_watch_state must fail closed (non-zero) when the target
# path cannot be written, rather than silently returning 0.
K3S_WATCH_STATE_FILE="/dev/null/impossible/watch.json"
if persist_k3s_pod_watch_state; then
    exit 1
fi
[[ "${LAST_ERROR}" == *"watch state"* ]]

# 2. Concurrent parent and background subshell writers sharing $$ must use
# unique temp files and lock serialization without colliding on .$$.
K3S_WATCH_STATE_FILE="${TEST_TMP}/watch-state.json"
for idx in $(seq 1 15); do
    (
        K3S_WATCH_RESOURCE_VERSION="bg-${idx}"
        K3S_WATCHED_CANDIDATE_POD_IPS="10.42.1.${idx}"
        persist_k3s_pod_watch_state
    ) &
    K3S_WATCH_RESOURCE_VERSION="fg-${idx}"
    K3S_WATCHED_CANDIDATE_POD_IPS="10.42.2.${idx}"
    persist_k3s_pod_watch_state
done
wait

[[ ! -e "${K3S_WATCH_STATE_FILE}.$$" ]]
jq -e '.scope == "lightning|app=lnd" and (.resource_version | length > 0)' "${K3S_WATCH_STATE_FILE}" >/dev/null
'''
    )
    assert result.returncode == 0, result.stderr


def test_k3s_ipv6_containment_preserves_watcher_guarded_replacement_pod_ipv6():
    result = run_bash(
        r'''
source "$1"

TEST_TMP="$(mktemp -d)"
trap 'rm -rf "${TEST_TMP}"' EXIT

K3S_MODE="true"
SECURE_MODE="false"
TUNNELSATS_K8S_NODE_NAME="worker-a"
IPV6_POLICY_STATE_FILE="${TEST_TMP}/ipv6-policy.json"
K3S_POLICY_STATE_FILE="${TEST_TMP}/k3s-policy.json"
K3S_WATCH_STATE_FILE="${TEST_TMP}/watch-state.json"

# Parent took its pod snapshot with only 2001:db8:42::7
DOCKER_TARGET_IP="10.42.1.7"
TARGET_IPV6_ADDRESSES=("2001:db8:42::7")
TARGET_IPV6_MACS=()
TARGET_HAS_IPV6_DEFAULT_ROUTE="true"
K3S_CANDIDATE_POD_IPS="10.42.1.7"
K3S_CANDIDATE_POD_IPV6_ADDRESSES="2001:db8:42::7"

V6_FILTER_RULES=$'-A FORWARD -s 2001:db8:42::7/128 ! -d fe80::/10 -m comment --comment tunnelsats-ipv6-deny -j DROP\n-A FORWARD -s 2001:db8:42::8/128 ! -d fe80::/10 -m comment --comment tunnelsats-ipv6-deny -j DROP\n'
V6_BLACKHOLE_RULES=$'32765:\tfrom 2001:db8:42::7 blackhole\n32765:\tfrom 2001:db8:42::8 blackhole\n'
V6_POLICY_RULES=$'32764:\tfrom 2001:db8:42::7 lookup 51821\n'
V6_TABLE_ROUTES=$'blackhole default dev lo metric 42760\n'
DELETED_V6_FILTER=""
DELETED_V6_BLACKHOLE=""

ip6tables() {
    case "$1" in
        -S)
            printf '%s' "${V6_FILTER_RULES}"
            return 0
            ;;
        -C)
            [[ "${V6_FILTER_RULES}" == *" -s $3/128 "* || "${V6_FILTER_RULES}" == *" -s $3 "* ]]
            return
            ;;
        -I)
            V6_FILTER_RULES+="-A FORWARD -s $4/128 ! -d fe80::/10 -m comment --comment tunnelsats-ipv6-deny -j DROP"$'\n'
            return 0
            ;;
        -D)
            DELETED_V6_FILTER+="$*"$'\n'
            return 0
            ;;
    esac
    return 0
}
ip() {
    if [[ "$1" == "-6" && "$2" == "rule" && "$3" == "show" && "$4" == "pref" && "$5" == "32765" ]]; then
        printf '%s' "${V6_BLACKHOLE_RULES}"
        return 0
    fi
    if [[ "$1" == "-6" && "$2" == "rule" && "$3" == "show" && "$4" == "pref" && "$5" == "32764" ]]; then
        printf '%s' "${V6_POLICY_RULES}"
        return 0
    fi
    if [[ "$1" == "-6" && "$2" == "route" && "$3" == "show" && "$4" == "table" && "$5" == "51821" ]]; then
        printf '%s' "${V6_TABLE_ROUTES}"
        return 0
    fi
    if [[ "$1" == "-6" && "$2" == "rule" && "$3" == "add" && "$4" == "from" && "$6" == "table" ]]; then
        V6_POLICY_RULES+="32764:	from $5 lookup 51821"$'\n'
        return 0
    fi
    if [[ "$1" == "-6" && "$2" == "rule" && "$3" == "add" && "$4" == "from" && "$6" == "blackhole" ]]; then
        V6_BLACKHOLE_RULES+="32765:	from $5 blackhole"$'\n'
        return 0
    fi
    if [[ "$1" == "-6" && "$2" == "rule" && "$3" == "del" && "$4" == "from" && "$6" == "blackhole" ]]; then
        DELETED_V6_BLACKHOLE+="$5,"
        return 1
    fi
    return 0
}

# Simulate background watcher having guarded replacement pod 2001:db8:42::8
# after the parent took its snapshot.
IPV6_POLICY_STATE_LOADED="true"
IPV6_POLICY_TABLE_MANAGED="true"
MANAGED_TARGET_IPV6_ADDRESSES=("2001:db8:42::7" "2001:db8:42::8")
persist_managed_ipv6_policy_state

K3S_WATCH_SCOPE="lightning|app=lnd"
K3S_WATCH_RESOURCE_VERSION="600"
K3S_WATCHED_CANDIDATE_POD_IPS="10.42.1.7,10.42.1.8"
K3S_WATCHED_CANDIDATE_POD_IPV6_ADDRESSES="2001:db8:42::7,2001:db8:42::8"
K3S_UNSEEN_REPLACEMENT_PENDING="false"
persist_k3s_pod_watch_state

# Reset parent's in-memory watched state to simulate stale parent snapshot
K3S_WATCHED_CANDIDATE_POD_IPS="10.42.1.7"
K3S_WATCHED_CANDIDATE_POD_IPV6_ADDRESSES="2001:db8:42::7"
MANAGED_TARGET_IPV6_ADDRESSES=("2001:db8:42::7")

ensure_ipv6_containment
ipv6_containment_is_synced
[[ "${DELETED_V6_FILTER}" != *"2001:db8:42::8"* ]]
[[ "${DELETED_V6_BLACKHOLE}" != *"2001:db8:42::8"* ]]
[[ " ${TARGET_IPV6_ADDRESSES[*]} " == *" 2001:db8:42::8 "* ]]
[[ " ${MANAGED_TARGET_IPV6_ADDRESSES[*]} " == *" 2001:db8:42::8 "* ]]
'''
    )
    assert result.returncode == 0, result.stderr


def test_k3s_unknown_pod_containment_and_watch_failure_fail_closed():
    result = run_bash(
        r'''
source "$1"

TEST_TMP="$(mktemp -d)"
trap 'rm -rf "${TEST_TMP}"' EXIT

K3S_MODE="true"
SECURE_MODE="false"
K3S_ENFORCE_UNKNOWN_POD_CONTAINMENT="true"
TUNNELSATS_K8S_NODE_NAME="worker-a"
K3S_TARGET_POD_NAMESPACE="lightning"
K3S_TARGET_POD_SELECTOR="app=lnd"
DOCKER_TARGET_IP="10.42.1.7"
K3S_BYPASS_CIDRS="10.42.0.0/16,10.43.0.0/16"
K3S_IPV6_POD_CIDRS="2001:cafe:42::/56"
TARGET_IPV6_ADDRESSES=("2001:cafe:42::7")
TARGET_HAS_IPV6_DEFAULT_ROUTE="true"
WG_IFACE="tunnelsatsv2"
K3S_POLICY_STATE_FILE="${TEST_TMP}/k3s-policy.json"
IPV6_POLICY_STATE_FILE="${TEST_TMP}/ipv6-policy.json"
RECONCILE_TRIGGER_DIR="${TEST_TMP}/triggers"

RULES=""
V6_RULES=""
ROUTES=$'default dev tunnelsatsv2 metric 2\nblackhole default metric 3\n'
ip() {
    if [[ "${1:-}" == "-6" ]]; then
        if [[ "${2:-} ${3:-} ${4:-}" == "rule show pref" ]]; then
            printf '%s' "${V6_RULES}" | grep "^${5:-}:" || true
            return 0
        fi
        if [[ "${2:-} ${3:-}" == "rule show" ]]; then
            printf '%s' "${V6_RULES}"
            return 0
        fi
        if [[ "${2:-} ${3:-}" == "rule add" ]]; then
            case "$*" in
                "-6 rule add from 2001:cafe:42::50 table main protocol 200 pref 32500")
                    V6_RULES+=$'32500:\tfrom 2001:cafe:42::50 lookup main proto 200\n'
                    return 0
                    ;;
                "-6 rule add from 2001:cafe:42::/56 blackhole protocol 200 pref 32765")
                    V6_RULES+=$'32765:\tfrom 2001:cafe:42::/56 blackhole proto 200\n'
                    return 0
                    ;;
            esac
            return 1
        fi
        if [[ "${2:-} ${3:-}" == "rule del" ]]; then
            if [[ "$*" == *"from 2001:cafe:42::50 lookup main proto 200 pref 32500"* || "$*" == *"from 2001:cafe:42::50 table main protocol 200 pref 32500"* ]]; then
                V6_RULES="${V6_RULES//$'32500:\tfrom 2001:cafe:42::50 lookup main proto 200\n'/}"
            fi
            if [[ "$*" == *"from 2001:cafe:42::/56 blackhole proto 200 pref 32765"* ]]; then
                V6_RULES="${V6_RULES//$'32765:\tfrom 2001:cafe:42::/56 blackhole proto 200\n'/}"
            fi
            return 0
        fi
        if [[ "${2:-} ${3:-}" == "route show" ]]; then
            printf '2001:cafe:42::/56 dev cni0 metric 256\n'
            return 0
        fi
        return 0
    fi
    if [[ "${1:-} ${2:-} ${3:-}" == "rule show pref" ]]; then
        printf '%s' "${RULES}" | grep "^${4:-}:" || true
        return 0
    fi
    if [[ "${1:-} ${2:-}" == "rule show" ]]; then
        printf '%s' "${RULES}"
        return 0
    fi
    if [[ "${1:-} ${2:-}" == "rule add" ]]; then
        case "$*" in
            "rule add from 10.42.1.7 to 10.42.0.0/16 table main protocol 200 pref 32500")
                RULES+=$'32500:\tfrom 10.42.1.7 to 10.42.0.0/16 lookup main proto 200\n'
                ;;
            "rule add from 10.42.1.7 to 10.43.0.0/16 table main protocol 200 pref 32500")
                RULES+=$'32500:\tfrom 10.42.1.7 to 10.43.0.0/16 lookup main proto 200\n'
                ;;
            "rule add from 10.42.0.0/16 to 10.42.0.0/16 table main protocol 200 pref 32500")
                RULES+=$'32500:\tfrom 10.42.0.0/16 to 10.42.0.0/16 lookup main proto 200\n'
                ;;
            "rule add from 10.42.0.0/16 to 10.43.0.0/16 table main protocol 200 pref 32500")
                RULES+=$'32500:\tfrom 10.42.0.0/16 to 10.43.0.0/16 lookup main proto 200\n'
                ;;
            "rule add from 10.42.1.50 table main protocol 200 pref 32500")
                RULES+=$'32500:\tfrom 10.42.1.50 lookup main proto 200\n'
                ;;
            "rule add from 10.42.1.7 table 51820 protocol 200 pref 32764")
                RULES+=$'32764:\tfrom 10.42.1.7 lookup 51820 proto 200\n'
                ;;
            "rule add from 10.42.1.7 blackhole protocol 200 pref 32765")
                RULES+=$'32765:\tfrom 10.42.1.7 blackhole proto 200\n'
                ;;
            "rule add from 10.42.0.0/16 blackhole protocol 200 pref 32765")
                RULES+=$'32765:\tfrom 10.42.0.0/16 blackhole proto 200\n'
                ;;
            *)
                return 1
                ;;
        esac
        return 0
    fi
    if [[ "${1:-} ${2:-}" == "rule del" ]]; then
        return 0
    fi
    if [[ "${1:-} ${2:-}" == "route replace" || "$*" == "route del 10.9.0.0/24 table 51820" ]]; then
        return 0
    fi
    if [[ "$*" == "route show table 51820" ]]; then
        printf '%s' "${ROUTES}"
        return 0
    fi
    if [[ "$*" == "-4 addr show dev tunnelsatsv2" ]]; then
        printf '%s\n' "7: tunnelsatsv2    inet 10.9.0.2/24 scope global tunnelsatsv2"
        return 0
    fi
    return 0
}

k8s_api() {
    printf '%s\n' '{
        "items": [
            {
                "metadata": {"name": "lnd-0", "namespace": "lightning", "labels": {"app": "lnd"}},
                "spec": {"nodeName": "worker-a"},
                "status": {
                    "phase": "Running",
                    "podIP": "10.42.1.7",
                    "podIPs": [{"ip": "10.42.1.7"}, {"ip": "2001:cafe:42::7"}]
                }
            },
            {
                "metadata": {"name": "lnd-replacement", "namespace": "lightning", "labels": {"app": "lnd"}},
                "spec": {"nodeName": "worker-a"},
                "status": {
                    "phase": "Running",
                    "podIP": "10.42.1.8",
                    "podIPs": [{"ip": "10.42.1.8"}, {"ip": "2001:cafe:42::8"}]
                }
            },
            {
                "metadata": {"name": "bitcoind-0", "namespace": "lightning", "labels": {"app": "bitcoind"}},
                "spec": {"nodeName": "worker-a"},
                "status": {
                    "phase": "Running",
                    "podIP": "10.42.1.50",
                    "podIPs": [{"ip": "10.42.1.50"}, {"ip": "2001:cafe:42::50"}]
                }
            }
        ]
    }'
    return 0
}

# 1. ensure_policy_routing installs pod-CIDR cluster-local bypasses at 32500,
# normal table-main outbound rules at 32500 for known non-target pods on the
# same node (10.42.1.50 / 2001:cafe:42::50), and pod-CIDR fallback blackholes
# at 32765 (IPv4 and IPv6) so any brand-new replacement pod IP is blocked from
# clearnet egress in the kernel even before its podIP appears in the watch stream.
ensure_policy_routing
[[ "${RULES}" == *$'32500:\tfrom 10.42.0.0/16 to 10.42.0.0/16 lookup main proto 200'* ]]
[[ "${RULES}" == *$'32500:\tfrom 10.42.0.0/16 to 10.43.0.0/16 lookup main proto 200'* ]]
[[ "${RULES}" == *$'32500:\tfrom 10.42.1.50 lookup main proto 200'* ]]
[[ "${RULES}" != *$'32500:\tfrom 10.42.1.7 lookup main'* ]]
[[ "${RULES}" != *$'32500:\tfrom 10.42.1.8 lookup main'* ]]
[[ "${RULES}" == *$'32765:\tfrom 10.42.0.0/16 blackhole proto 200'* ]]
[[ "${V6_RULES}" == *$'32500:\tfrom 2001:cafe:42::50 lookup main proto 200'* ]]
[[ "${V6_RULES}" != *$'32500:\tfrom 2001:cafe:42::7 lookup main'* ]]
[[ "${V6_RULES}" != *$'32500:\tfrom 2001:cafe:42::8 lookup main'* ]]
[[ "${V6_RULES}" == *$'32765:\tfrom 2001:cafe:42::/56 blackhole proto 200'* ]]
verify_k3s_target_routes

# 2. When k8s_watch_api fails (non-zero exit) or emits an ERROR event,
# watch_k3s_target_pods_once must NOT swallow the failure: it must reset
# K3S_WATCH_RESOURCE_VERSION, mark K3S_UNSEEN_REPLACEMENT_PENDING="true",
# restore pre-discovery containment, write a reconcile trigger, and return non-zero.
CONTAINMENT_CALLS=0
ensure_k3s_pre_discovery_containment() {
    CONTAINMENT_CALLS=$((CONTAINMENT_CALLS + 1))
    return 0
}
K3S_WATCH_SCOPE="lightning|app=lnd"
K3S_WATCH_RESOURCE_VERSION="777"
K3S_UNSEEN_REPLACEMENT_PENDING="false"
persist_k3s_pod_watch_state

k8s_watch_api() {
    return 22
}
if watch_k3s_target_pods_once "lightning" "app=lnd"; then
    exit 1
fi
load_k3s_pod_watch_state
[[ -z "${K3S_WATCH_RESOURCE_VERSION}" ]]
[[ "${K3S_UNSEEN_REPLACEMENT_PENDING}" == "true" ]]
[[ "${CONTAINMENT_CALLS}" -ge 1 ]]
[[ -f "${RECONCILE_TRIGGER_DIR}/k3s-pod-watch.trigger" ]]

rm -f "${RECONCILE_TRIGGER_DIR}/k3s-pod-watch.trigger"
CONTAINMENT_CALLS=0
K3S_WATCH_RESOURCE_VERSION="778"
K3S_UNSEEN_REPLACEMENT_PENDING="false"
persist_k3s_pod_watch_state
k8s_watch_api() {
    printf '%s\n' '{"type":"ERROR","object":{"kind":"Status","code":410}}'
    return 0
}
if watch_k3s_target_pods_once "lightning" "app=lnd"; then
    exit 1
fi
load_k3s_pod_watch_state
[[ -z "${K3S_WATCH_RESOURCE_VERSION}" ]]
[[ "${K3S_UNSEEN_REPLACEMENT_PENDING}" == "true" ]]
[[ "${CONTAINMENT_CALLS}" -ge 1 ]]
[[ -f "${RECONCILE_TRIGGER_DIR}/k3s-pod-watch.trigger" ]]

# 3. cleanup_k3s_policy_rules "false" must remove tab-indented IPv6 pod-CIDR
# blackhole rules at pref 32765 and non-target pod IPv6 bypass rules at 32500.
cleanup_k3s_policy_rules "false"
[[ -z "${V6_RULES}" ]]
'''
    )
    assert result.returncode == 0, result.stderr

