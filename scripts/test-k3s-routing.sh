#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "SKIP: k3s routing integration test requires root" >&2
    exit 0
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_SUFFIX="$$"
ROUTER_NS="tsr-${TEST_SUFFIX}"
POD_NS="tsp-${TEST_SUFFIX}"
VPN_NS="tsv-${TEST_SUFFIX}"
CLEAR_NS="tsc-${TEST_SUFFIX}"
POD_ROUTER_TMP="pr${TEST_SUFFIX}"
POD_TMP="p0${TEST_SUFFIX}"
VPN_ROUTER_TMP="wr${TEST_SUFFIX}"
VPN_TMP="w0${TEST_SUFFIX}"
CLEAR_ROUTER_TMP="cr${TEST_SUFFIX}"
CLEAR_TMP="c0${TEST_SUFFIX}"
IPV6_POLICY_STATE="/tmp/tunnelsats-ipv6-policy-${TEST_SUFFIX}.json"
K3S_POLICY_STATE="/tmp/tunnelsats-k3s-policy-${TEST_SUFFIX}.json"
K3S_PROTOCOL_STATE="/tmp/tunnelsats-k3s-protocol-${TEST_SUFFIX}"
DECOMMISSION_STATE="/tmp/tunnelsats-decommissioned-${TEST_SUFFIX}.json"
STATE_FILE_TMP="/tmp/tunnelsats-state-${TEST_SUFFIX}.json"

cleanup() {
    local namespace
    local link
    for link in \
        "${POD_ROUTER_TMP}" "${POD_TMP}" \
        "${VPN_ROUTER_TMP}" "${VPN_TMP}" \
        "${CLEAR_ROUTER_TMP}" "${CLEAR_TMP}"; do
        ip link del "${link}" >/dev/null 2>&1 || true
    done
    for namespace in "${POD_NS}" "${VPN_NS}" "${CLEAR_NS}" "${ROUTER_NS}"; do
        ip netns del "${namespace}" >/dev/null 2>&1 || true
    done
    rm -f \
        "${IPV6_POLICY_STATE}" "${IPV6_POLICY_STATE}.tmp."* \
        "${K3S_POLICY_STATE}" "${K3S_POLICY_STATE}.tmp."* \
        "${K3S_PROTOCOL_STATE}" "${K3S_PROTOCOL_STATE}.tmp."* \
        "${DECOMMISSION_STATE}" "${DECOMMISSION_STATE}.tmp."* \
        "${STATE_FILE_TMP}" "${STATE_FILE_TMP}.tmp."*
}
trap cleanup EXIT

for namespace in "${ROUTER_NS}" "${POD_NS}" "${VPN_NS}" "${CLEAR_NS}"; do
    ip netns add "${namespace}"
    ip -n "${namespace}" link set lo up
done

ip link add "${POD_ROUTER_TMP}" type veth peer name "${POD_TMP}"
ip link set "${POD_ROUTER_TMP}" netns "${ROUTER_NS}"
ip link set "${POD_TMP}" netns "${POD_NS}"
ip -n "${ROUTER_NS}" link set "${POD_ROUTER_TMP}" name pod-r
ip -n "${POD_NS}" link set "${POD_TMP}" name pod0
ip -n "${ROUTER_NS}" addr add 10.42.1.1/24 dev pod-r
ip -n "${POD_NS}" addr add 10.42.1.7/24 dev pod0
ip -n "${ROUTER_NS}" -6 addr add 2001:db8:42::1/64 dev pod-r nodad
ip -n "${POD_NS}" -6 addr add 2001:db8:42::7/64 dev pod0 nodad
ip -n "${ROUTER_NS}" link set pod-r up
ip -n "${POD_NS}" link set pod0 up
ip -n "${POD_NS}" route add default via 10.42.1.1
ip -n "${POD_NS}" -6 route add default via 2001:db8:42::1

ip link add "${VPN_ROUTER_TMP}" type veth peer name "${VPN_TMP}"
ip link set "${VPN_ROUTER_TMP}" netns "${ROUTER_NS}"
ip link set "${VPN_TMP}" netns "${VPN_NS}"
ip -n "${ROUTER_NS}" link set "${VPN_ROUTER_TMP}" name tunnelsatsv2
ip -n "${VPN_NS}" link set "${VPN_TMP}" name vpn0
ip -n "${ROUTER_NS}" addr add 10.9.0.2/24 dev tunnelsatsv2
ip -n "${VPN_NS}" addr add 10.9.0.1/24 dev vpn0
ip -n "${VPN_NS}" addr add 198.51.100.2/32 dev lo
ip -n "${ROUTER_NS}" link set tunnelsatsv2 up
ip -n "${VPN_NS}" link set vpn0 up
ip -n "${VPN_NS}" route add 10.42.1.0/24 via 10.9.0.2
VPN_MAC="$(
    ip -n "${VPN_NS}" -o link show vpn0 \
        | awk '{for (i = 1; i <= NF; i++) if ($i == "link/ether") {print $(i + 1); exit}}'
)"
ip -n "${ROUTER_NS}" neigh replace 198.51.100.2 lladdr "${VPN_MAC}" dev tunnelsatsv2

ip link add "${CLEAR_ROUTER_TMP}" type veth peer name "${CLEAR_TMP}"
ip link set "${CLEAR_ROUTER_TMP}" netns "${ROUTER_NS}"
ip link set "${CLEAR_TMP}" netns "${CLEAR_NS}"
ip -n "${ROUTER_NS}" link set "${CLEAR_ROUTER_TMP}" name clear0
ip -n "${CLEAR_NS}" link set "${CLEAR_TMP}" name clear-peer
ip -n "${ROUTER_NS}" addr add 192.0.2.1/24 dev clear0
ip -n "${CLEAR_NS}" addr add 192.0.2.2/24 dev clear-peer
ip -n "${CLEAR_NS}" addr add 198.51.100.2/32 dev lo
ip -n "${ROUTER_NS}" -6 addr add 2001:db8:100::1/64 dev clear0 nodad
ip -n "${CLEAR_NS}" -6 addr add 2001:db8:100::2/64 dev clear-peer nodad
ip -n "${ROUTER_NS}" link set clear0 up
ip -n "${CLEAR_NS}" link set clear-peer up
ip -n "${CLEAR_NS}" route add 10.42.1.0/24 via 192.0.2.1
ip -n "${ROUTER_NS}" route add default via 192.0.2.2 dev clear0
ip -n "${CLEAR_NS}" -6 route add 2001:db8:42::/64 via 2001:db8:100::1
ip -n "${ROUTER_NS}" -6 route add default via 2001:db8:100::2 dev clear0

ip netns exec "${ROUTER_NS}" sysctl -q -w net.ipv4.ip_forward=1
ip netns exec "${ROUTER_NS}" sysctl -q -w net.ipv6.conf.all.forwarding=1
ip netns exec "${ROUTER_NS}" sysctl -q -w net.ipv6.conf.pod-r.forwarding=1
ip netns exec "${ROUTER_NS}" sysctl -q -w net.ipv6.conf.clear0.forwarding=1
ip netns exec "${ROUTER_NS}" sysctl -q -w net.ipv4.conf.all.rp_filter=0
ip netns exec "${ROUTER_NS}" sysctl -q -w net.ipv4.conf.pod-r.rp_filter=0
ip netns exec "${ROUTER_NS}" sysctl -q -w net.ipv4.conf.tunnelsatsv2.rp_filter=0
ip netns exec "${ROUTER_NS}" sysctl -q -w net.ipv4.conf.clear0.rp_filter=0

# Establish that ordinary routing would expose the clear path before policy
# routing is installed.
if ! ip netns exec "${POD_NS}" ping -c 1 -W 2 198.51.100.2 >/dev/null; then
    echo "FAIL: baseline clear-egress path is unreachable" >&2
    exit 1
fi
if ! ip netns exec "${POD_NS}" ping -6 -c 3 -W 2 2001:db8:100::2 >/dev/null; then
    echo "FAIL: baseline IPv6 clear-egress path is unreachable" >&2
    ip -n "${POD_NS}" -6 address show >&2
    ip -n "${POD_NS}" -6 route show table all >&2
    ip -n "${ROUTER_NS}" -6 address show >&2
    ip -n "${ROUTER_NS}" -6 route show table all >&2
    ip -n "${CLEAR_NS}" -6 address show >&2
    ip -n "${CLEAR_NS}" -6 route show table all >&2
    ip netns exec "${POD_NS}" ping -6 -c 1 -W 2 2001:db8:100::2 >&2 || true
    exit 1
fi

# Expansion in the quoted program belongs to the nested shell.
# shellcheck disable=SC2016
ip netns exec "${ROUTER_NS}" env \
    ENTRYPOINT_PATH="${REPO_ROOT}/scripts/entrypoint.sh" \
    IPV6_POLICY_STATE_FILE="${IPV6_POLICY_STATE}" \
    K3S_POLICY_STATE_FILE="${K3S_POLICY_STATE}" \
    K3S_RULE_PROTOCOL_FILE="${K3S_PROTOCOL_STATE}" \
    bash -c '
        source "${ENTRYPOINT_PATH}"
        K3S_MODE="true"
        SECURE_MODE="false"
        DOCKER_TARGET_IP="10.42.1.7"
        K3S_BYPASS_CIDRS="10.42.0.0/16,10.43.0.0/16"
        WG_IFACE="tunnelsatsv2"
        ensure_policy_routing
        TARGET_IPV6_ADDRESSES=("2001:db8:42::7")
        TARGET_IPV6_MACS=()
        TARGET_HAS_IPV6_DEFAULT_ROUTE="true"
        ensure_ipv6_containment
        ipv6_containment_is_synced
        verify_k3s_target_routes

        # A higher-priority unowned rule matching the pod CIDR must fail verification.
        ip rule add from 10.42.1.0/24 table main pref 100
        if verify_k3s_target_routes; then
            ip rule del from 10.42.1.0/24 table main pref 100 >/dev/null 2>&1 || true
            echo "FAIL: verify_k3s_target_routes accepted a shadowing rule at pref 100" >&2
            exit 1
        fi
        ip rule del from 10.42.1.0/24 table main pref 100
        verify_k3s_target_routes

        external_route="$(ip route get 198.51.100.2 from 10.42.1.7 iif pod-r)"
        if [[ "${external_route}" != *"dev tunnelsatsv2"* ]]; then
            echo "FAIL: external route did not select WireGuard: ${external_route}" >&2
            exit 1
        fi

        local_route="$(ip route get 10.42.1.1 from 10.42.1.7 iif pod-r)"
        if [[ "${local_route}" == *"dev tunnelsatsv2"* ]] || [[ "${local_route}" == *"dev clear0"* ]]; then
            echo "FAIL: local route selected external egress: ${local_route}" >&2
            exit 1
        fi
    '

# A new outbound connection now succeeds through the VPN-side namespace.
if ! ip netns exec "${POD_NS}" ping -c 1 -W 2 198.51.100.2 >/dev/null; then
    echo "FAIL: pod-initiated traffic did not traverse the VPN path" >&2
    exit 1
fi

# The same workload has an ordinary IPv6 default route, but Option B must
# deny it rather than exposing the clear observer to the workload's address.
if ip netns exec "${POD_NS}" ping -6 -c 1 -W 1 2001:db8:100::2 >/dev/null 2>&1; then
    echo "FAIL: IPv6 traffic escaped through the clear observer" >&2
    exit 1
fi

# A connection arriving from the VPN side also receives its reply through the
# same path, exercising the inbound-reply invariant without connmarks.
if ! ip netns exec "${VPN_NS}" ping -I 198.51.100.2 -c 1 -W 2 10.42.1.7 >/dev/null; then
    echo "FAIL: reply to VPN-originated traffic did not return through the VPN path" >&2
    exit 1
fi

# Cluster-local dependencies remain available through the explicit bypass.
if ! ip netns exec "${POD_NS}" ping -c 1 -W 2 10.42.1.1 >/dev/null; then
    echo "FAIL: cluster-local bypass is unreachable" >&2
    exit 1
fi

# Simulate an unready co-located replacement pod (10.42.1.8 / 2001:db8:42::8).
# Pre-guarding must block clearnet egress for the replacement pod before Ready=True
# while preserving WireGuard routing for the active pod (10.42.1.7).
ip -n "${POD_NS}" addr add 10.42.1.8/24 dev pod0
ip -n "${POD_NS}" -6 addr add 2001:db8:42::8/64 dev pod0 nodad
# shellcheck disable=SC2016
ip netns exec "${ROUTER_NS}" env \
    ENTRYPOINT_PATH="${REPO_ROOT}/scripts/entrypoint.sh" \
    IPV6_POLICY_STATE_FILE="${IPV6_POLICY_STATE}" \
    K3S_POLICY_STATE_FILE="${K3S_POLICY_STATE}" \
    K3S_RULE_PROTOCOL_FILE="${K3S_PROTOCOL_STATE}" \
    bash -c '
        source "${ENTRYPOINT_PATH}"
        K3S_MODE="true"
        SECURE_MODE="false"
        TUNNELSATS_K8S_NODE_NAME="worker-a"
        DOCKER_TARGET_IP="10.42.1.7"
        WG_IFACE="tunnelsatsv2"
        pod_list='\''{"items":[{"metadata":{"name":"lnd-0"},"spec":{"nodeName":"worker-a"},"status":{"phase":"Running","podIP":"10.42.1.7","podIPs":[{"ip":"10.42.1.7"},{"ip":"2001:db8:42::7"}],"conditions":[{"type":"Ready","status":"True"}]}},{"metadata":{"name":"lnd-replacement"},"spec":{"nodeName":"worker-a"},"status":{"phase":"Running","podIP":"10.42.1.8","podIPs":[{"ip":"10.42.1.8"},{"ip":"2001:db8:42::8"}],"conditions":[{"type":"Ready","status":"False"}]}}]}'\''
        pre_guard_colocated_k3s_candidate_pods "${pod_list}"
    '
if ip netns exec "${POD_NS}" ping -I 10.42.1.8 -c 1 -W 1 198.51.100.2 >/dev/null 2>&1; then
    echo "FAIL: unready replacement pod IP 10.42.1.8 escaped to clearnet" >&2
    exit 1
fi
if ip netns exec "${POD_NS}" ping -6 -I 2001:db8:42::8 -c 1 -W 1 2001:db8:100::2 >/dev/null 2>&1; then
    echo "FAIL: unready replacement pod IPv6 2001:db8:42::8 escaped to clearnet" >&2
    exit 1
fi
if ! ip netns exec "${POD_NS}" ping -I 10.42.1.7 -c 1 -W 2 198.51.100.2 >/dev/null; then
    echo "FAIL: active pod IP 10.42.1.7 lost VPN routing while replacement was pre-guarded" >&2
    exit 1
fi

# Simulate pre-discovery containment when target discovery fails and a new
# replacement pod IP (10.42.1.9) appears on the shared pod CIDR before discovery.
ip -n "${POD_NS}" addr add 10.42.1.9/24 dev pod0
# shellcheck disable=SC2016
ip netns exec "${ROUTER_NS}" env \
    ENTRYPOINT_PATH="${REPO_ROOT}/scripts/entrypoint.sh" \
    IPV6_POLICY_STATE_FILE="${IPV6_POLICY_STATE}" \
    K3S_POLICY_STATE_FILE="${K3S_POLICY_STATE}" \
    K3S_RULE_PROTOCOL_FILE="${K3S_PROTOCOL_STATE}" \
    bash -c '
        source "${ENTRYPOINT_PATH}"
        K3S_MODE="true"
        SECURE_MODE="false"
        K3S_BYPASS_CIDRS="10.42.0.0/16,10.43.0.0/16"
        LND_K8S_SERVICE="lnd"
        LND_K8S_NAMESPACE="lightning"
        LND_K8S_POD_SELECTOR="app=lnd"
        ensure_k3s_emergency_network_policy() { return 0; }
        ensure_k3s_pre_discovery_containment "10.42.1.7"
    '
if ip netns exec "${POD_NS}" ping -I 10.42.1.9 -c 1 -W 1 198.51.100.2 >/dev/null 2>&1; then
    echo "FAIL: unseen replacement pod IP 10.42.1.9 escaped during pre-discovery containment" >&2
    exit 1
fi
ip -n "${POD_NS}" addr del 10.42.1.9/24 dev pod0
ip -n "${POD_NS}" addr del 10.42.1.8/24 dev pod0
ip -n "${POD_NS}" -6 addr del 2001:db8:42::8/64 dev pod0
ip -n "${POD_NS}" addr add 10.42.1.50/24 dev pod0
ip -n "${POD_NS}" -6 addr add 2001:db8:42::50/64 dev pod0 nodad
# Clean up the pre-guarded candidate rules for 10.42.1.8 / 2001:db8:42::8 and
# restore clean 10.42.1.7 state for the subsequent kill-switch and SIGTERM tests.
# shellcheck disable=SC2016
ip netns exec "${ROUTER_NS}" env \
    ENTRYPOINT_PATH="${REPO_ROOT}/scripts/entrypoint.sh" \
    IPV6_POLICY_STATE_FILE="${IPV6_POLICY_STATE}" \
    K3S_POLICY_STATE_FILE="${K3S_POLICY_STATE}" \
    K3S_RULE_PROTOCOL_FILE="${K3S_PROTOCOL_STATE}" \
    bash -c '
        source "${ENTRYPOINT_PATH}"
        K3S_MODE="true"
        SECURE_MODE="false"
        DOCKER_TARGET_IP="10.42.1.7"
        WG_IFACE="tunnelsatsv2"
        K3S_ALLOW_ACTIVE_QUARANTINE_DURING_SYNC="true" verify_k3s_target_routes
        remove_k3s_subnet_quarantine
        verify_k3s_target_routes
        K3S_POD_API_QUERY_OK="true"
        K3S_ALL_MATCHING_LOCAL_POD_IPS="10.42.1.7"
        K3S_CANDIDATE_POD_IPS="10.42.1.7"
        retire_stale_k3s_unmatched_ip_guards
        remove_k3s_egress_guards
        TARGET_IPV6_ADDRESSES=("2001:db8:42::7")
        TARGET_IPV6_MACS=()
        TARGET_HAS_IPV6_DEFAULT_ROUTE="true"
        ensure_ipv6_containment
        K3S_NON_TARGET_POD_IPS="10.42.1.50"
        K3S_NON_TARGET_POD_IPV6_ADDRESSES="2001:db8:42::50"
        ensure_k3s_unknown_pod_containment "10.42.1.7"
    '

# Even after emergency pod-CIDR quarantine (32763) and egress guards are released,
# a fast replacement pod IP (10.42.1.11 / 2001:db8:42::11) that finishes CNI setup
# before its IP appears in the Kubernetes watch stream must remain blocked from
# clearnet egress by the persistent kernel pod-CIDR fallback blackhole (32765)
# while retaining cluster-local bypass access (32500), known non-target pod
# outbound access (10.42.1.50 / 2001:db8:42::50), and active pod VPN routing.
ip -n "${POD_NS}" addr add 10.42.1.11/24 dev pod0
ip -n "${POD_NS}" -6 addr add 2001:db8:42::11/64 dev pod0 nodad
if ip netns exec "${POD_NS}" ping -I 10.42.1.11 -c 1 -W 1 198.51.100.2 >/dev/null 2>&1; then
    echo "FAIL: unseen post-CNI replacement pod IP 10.42.1.11 escaped to clearnet" >&2
    exit 1
fi
if ip netns exec "${POD_NS}" ping -6 -I 2001:db8:42::11 -c 1 -W 1 2001:db8:100::2 >/dev/null 2>&1; then
    echo "FAIL: unseen post-CNI replacement pod IPv6 2001:db8:42::11 escaped to clearnet" >&2
    exit 1
fi
if ! ip netns exec "${POD_NS}" ping -I 10.42.1.50 -c 1 -W 2 198.51.100.2 >/dev/null; then
    echo "FAIL: known non-target pod IP 10.42.1.50 lost outbound Internet access" >&2
    exit 1
fi
if ! ip netns exec "${POD_NS}" ping -6 -I 2001:db8:42::50 -c 1 -W 2 2001:db8:100::2 >/dev/null; then
    echo "FAIL: known non-target pod IPv6 2001:db8:42::50 lost outbound Internet access" >&2
    exit 1
fi
# Simulate a replacement Lightning pod reusing the terminated non-target pod's
# IP (10.42.1.50 / 2001:db8:42::50) on a new CNI veth with a new MAC address
# before the Kubernetes watch sees it. Because the 32500 exemption requires the
# original pod's MAC-bound fwmark (0xca6d), the reused IP must be blocked by 32765.
ORIG_POD0_MAC="$(ip -n "${POD_NS}" link show dev pod0 | awk '/link\/ether/ {print $2; exit}')"
ip -n "${POD_NS}" link set dev pod0 address 02:42:0a:2a:01:fe
if ip netns exec "${POD_NS}" ping -I 10.42.1.50 -c 1 -W 1 198.51.100.2 >/dev/null 2>&1; then
    echo "FAIL: replacement pod reusing non-target pod IP 10.42.1.50 with new MAC escaped to clearnet" >&2
    exit 1
fi
if ip netns exec "${POD_NS}" ping -6 -I 2001:db8:42::50 -c 1 -W 1 2001:db8:100::2 >/dev/null 2>&1; then
    echo "FAIL: replacement pod reusing non-target pod IPv6 2001:db8:42::50 with new MAC escaped to clearnet" >&2
    exit 1
fi
ip -n "${POD_NS}" link set dev pod0 address "${ORIG_POD0_MAC}"
ip -n "${ROUTER_NS}" neigh replace 10.42.1.50 lladdr "${ORIG_POD0_MAC}" dev pod-r nud reachable
ip -n "${ROUTER_NS}" -6 neigh replace 2001:db8:42::50 lladdr "${ORIG_POD0_MAC}" dev pod-r nud reachable
if ! ip netns exec "${POD_NS}" ping -I 10.42.1.11 -c 1 -W 2 10.42.1.1 >/dev/null; then
    echo "FAIL: unseen replacement pod IP 10.42.1.11 lost cluster-local bypass" >&2
    exit 1
fi
if ! ip netns exec "${POD_NS}" ping -I 10.42.1.7 -c 1 -W 2 198.51.100.2 >/dev/null; then
    echo "FAIL: active pod IP 10.42.1.7 lost VPN routing under unknown-pod containment" >&2
    exit 1
fi
ip -n "${POD_NS}" addr del 10.42.1.50/24 dev pod0
ip -n "${POD_NS}" -6 addr del 2001:db8:42::50/64 dev pod0
ip -n "${POD_NS}" addr del 10.42.1.11/24 dev pod0
ip -n "${POD_NS}" -6 addr del 2001:db8:42::11/64 dev pod0

# Removing the WireGuard default leaves the table blackhole in place. External
# traffic must fail instead of falling back to the clear interface.
ip -n "${ROUTER_NS}" route del default dev tunnelsatsv2 metric 2 table 51820
if ip netns exec "${POD_NS}" ping -c 1 -W 1 198.51.100.2 >/dev/null 2>&1; then
    echo "FAIL: external traffic escaped after the WireGuard route was removed" >&2
    exit 1
fi

kill_route="$(
    ip netns exec "${ROUTER_NS}" \
        ip route get 198.51.100.2 from 10.42.1.7 iif pod-r 2>&1 || true
)"
if [[ "${kill_route}" == *"dev clear0"* ]]; then
    echo "FAIL: kill-switch route fell through to ordinary egress" >&2
    exit 1
fi

# Even if the dedicated IPv6 policy-table route drifts away, the independent
# source blackhole rule must prevent fallback to the ordinary IPv6 default.
ip -n "${ROUTER_NS}" -6 route del blackhole default metric 42760 table 51821
if ip netns exec "${POD_NS}" ping -6 -c 1 -W 1 2001:db8:100::2 >/dev/null 2>&1; then
    echo "FAIL: IPv6 traffic escaped after the policy-table blackhole was removed" >&2
    exit 1
fi
ipv6_kill_route="$(
    ip netns exec "${ROUTER_NS}" \
        ip -6 route get 2001:db8:100::2 from 2001:db8:42::7 iif pod-r 2>&1 || true
)"
if [[ "${ipv6_kill_route}" == *"dev clear0"* ]]; then
    echo "FAIL: IPv6 kill switch fell through to ordinary egress" >&2
    exit 1
fi

# SIGTERM shutdown/restart/upgrade preservation must keep the pod fail-closed
# even after active tunnel/bypass rules are removed.
# shellcheck disable=SC2016
ip netns exec "${ROUTER_NS}" env \
    ENTRYPOINT_PATH="${REPO_ROOT}/scripts/entrypoint.sh" \
    IPV6_POLICY_STATE_FILE="${IPV6_POLICY_STATE}" \
    K3S_POLICY_STATE_FILE="${K3S_POLICY_STATE}" \
    K3S_RULE_PROTOCOL_FILE="${K3S_PROTOCOL_STATE}" \
    DECOMMISSION_STATE_FILE="${DECOMMISSION_STATE}" \
    bash -c '
        source "${ENTRYPOINT_PATH}"
        STATE_FILE="'"${STATE_FILE_TMP}"'"
        K3S_MODE="true"
        SECURE_MODE="false"
        DOCKER_TARGET_IP="10.42.1.7"
        TARGET_IMPL="lnd"
        K3S_TARGET_POD_NAME="lnd-0"
        K3S_TARGET_POD_NAMESPACE="lightning"
        K3S_TARGET_POD_SELECTOR="app=lnd"
        ensure_k3s_emergency_network_policy() { return 0; }
        preserve_fail_closed_dataplane
        if ip rule show pref "${K3S_QUARANTINE_RULE_PREF}" | grep -Fq "from 10.42.0.0/16"; then
            echo "FAIL: routine SIGTERM preservation installed shared-subnet quarantine" >&2
            exit 1
        fi
    '
if ip netns exec "${POD_NS}" ping -c 1 -W 1 198.51.100.2 >/dev/null 2>&1; then
    echo "FAIL: IPv4 traffic escaped during fail-closed SIGTERM preservation" >&2
    exit 1
fi
if ip netns exec "${POD_NS}" ping -6 -c 1 -W 1 2001:db8:100::2 >/dev/null 2>&1; then
    echo "FAIL: IPv6 traffic escaped during fail-closed SIGTERM preservation" >&2
    exit 1
fi
ip -n "${POD_NS}" addr add 10.42.1.50/24 dev pod0
ip -n "${POD_NS}" -6 addr add 2001:db8:42::50/64 dev pod0 nodad
if ! ip netns exec "${POD_NS}" ping -I 10.42.1.50 -c 1 -W 2 198.51.100.2 >/dev/null; then
    echo "FAIL: non-target pod IP 10.42.1.50 lost Internet access during SIGTERM preservation" >&2
    exit 1
fi
if ! ip netns exec "${POD_NS}" ping -6 -I 2001:db8:42::50 -c 1 -W 2 2001:db8:100::2 >/dev/null; then
    echo "FAIL: non-target pod IPv6 2001:db8:42::50 lost Internet access during SIGTERM preservation" >&2
    exit 1
fi
ip -n "${POD_NS}" addr del 10.42.1.50/24 dev pod0
ip -n "${POD_NS}" -6 addr del 2001:db8:42::50/64 dev pod0

# When emergency NetworkPolicy creation fails during SIGTERM, preserve_fail_closed_dataplane
# must install and retain the pod-CIDR subnet quarantine and return non-zero.
# shellcheck disable=SC2016
ip netns exec "${ROUTER_NS}" env \
    ENTRYPOINT_PATH="${REPO_ROOT}/scripts/entrypoint.sh" \
    IPV6_POLICY_STATE_FILE="${IPV6_POLICY_STATE}" \
    K3S_POLICY_STATE_FILE="${K3S_POLICY_STATE}" \
    K3S_RULE_PROTOCOL_FILE="${K3S_PROTOCOL_STATE}" \
    DECOMMISSION_STATE_FILE="${DECOMMISSION_STATE}" \
    bash -c '
        source "${ENTRYPOINT_PATH}"
        STATE_FILE="'"${STATE_FILE_TMP}"'"
        K3S_MODE="true"
        SECURE_MODE="false"
        DOCKER_TARGET_IP="10.42.1.7"
        TARGET_IMPL="lnd"
        K3S_TARGET_POD_NAME="lnd-0"
        K3S_TARGET_POD_NAMESPACE="lightning"
        K3S_TARGET_POD_SELECTOR="app=lnd"
        K3S_SHUTDOWN_NETPOL_ATTEMPTS=1
        ensure_k3s_emergency_network_policy() { return 1; }
        delete_k3s_target_pod() { return 0; }
        if preserve_fail_closed_dataplane; then
            echo "FAIL: preserve_fail_closed_dataplane succeeded when emergency NetworkPolicy failed" >&2
            exit 1
        fi
        if ! ip rule show pref "${K3S_QUARANTINE_RULE_PREF}" | grep -Fq "from 10.42.0.0/16"; then
            echo "FAIL: failed NetworkPolicy SIGTERM did not retain shared-subnet quarantine" >&2
            exit 1
        fi
        if ! ip -6 rule show pref "${K3S_QUARANTINE_RULE_PREF}" | grep -Fq "from 2001:db8:42::/64"; then
            echo "FAIL: failed NetworkPolicy SIGTERM did not retain IPv6 shared-subnet quarantine" >&2
            exit 1
        fi
    '
if ip netns exec "${POD_NS}" ping -c 1 -W 1 198.51.100.2 >/dev/null 2>&1; then
    echo "FAIL: IPv4 traffic escaped during failed-policy SIGTERM quarantine" >&2
    exit 1
fi
if ip netns exec "${POD_NS}" ping -6 -c 1 -W 1 2001:db8:100::2 >/dev/null 2>&1; then
    echo "FAIL: IPv6 traffic escaped during failed-policy SIGTERM quarantine" >&2
    exit 1
fi

# Explicit decommission (Restore Node Networking) removes all fail-closed guards
# and restores baseline clear-net routing. This standalone netns harness has no
# Kubernetes API server, so stub the k8s API NetworkPolicy call (covered by unit tests).
# shellcheck disable=SC2016
ip netns exec "${ROUTER_NS}" env \
    ENTRYPOINT_PATH="${REPO_ROOT}/scripts/entrypoint.sh" \
    IPV6_POLICY_STATE_FILE="${IPV6_POLICY_STATE}" \
    K3S_POLICY_STATE_FILE="${K3S_POLICY_STATE}" \
    K3S_RULE_PROTOCOL_FILE="${K3S_PROTOCOL_STATE}" \
    DECOMMISSION_STATE_FILE="${DECOMMISSION_STATE}" \
    bash -c '
        source "${ENTRYPOINT_PATH}"
        STATE_FILE="'"${STATE_FILE_TMP}"'"
        K3S_MODE="true"
        SECURE_MODE="false"
        remove_k3s_emergency_network_policy() { return 0; }
        decommission_dataplane
    '
if ! ip netns exec "${POD_NS}" ping -c 1 -W 2 198.51.100.2 >/dev/null; then
    echo "FAIL: baseline IPv4 routing was not restored after explicit decommission" >&2
    exit 1
fi
if ! ip netns exec "${POD_NS}" ping -6 -c 3 -W 2 2001:db8:100::2 >/dev/null; then
    echo "FAIL: baseline IPv6 routing was not restored after explicit decommission" >&2
    exit 1
fi

echo "k3s dual-stack fail-closed routing integration test passed"
