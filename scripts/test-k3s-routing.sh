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
ip -n "${ROUTER_NS}" link set pod-r up
ip -n "${POD_NS}" link set pod0 up
ip -n "${POD_NS}" route add default via 10.42.1.1

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
ip -n "${ROUTER_NS}" link set clear0 up
ip -n "${CLEAR_NS}" link set clear-peer up
ip -n "${CLEAR_NS}" route add 10.42.1.0/24 via 192.0.2.1
ip -n "${ROUTER_NS}" route add default via 192.0.2.2 dev clear0

ip netns exec "${ROUTER_NS}" sysctl -q -w net.ipv4.ip_forward=1
ip netns exec "${ROUTER_NS}" sysctl -q -w net.ipv4.conf.all.rp_filter=0
ip netns exec "${ROUTER_NS}" sysctl -q -w net.ipv4.conf.pod-r.rp_filter=0
ip netns exec "${ROUTER_NS}" sysctl -q -w net.ipv4.conf.tunnelsatsv2.rp_filter=0
ip netns exec "${ROUTER_NS}" sysctl -q -w net.ipv4.conf.clear0.rp_filter=0

# Establish that ordinary routing would expose the clear path before policy
# routing is installed.
ip netns exec "${POD_NS}" ping -c 1 -W 2 198.51.100.2 >/dev/null

# Expansion in the quoted program belongs to the nested shell.
# shellcheck disable=SC2016
ip netns exec "${ROUTER_NS}" env \
    ENTRYPOINT_PATH="${REPO_ROOT}/scripts/entrypoint.sh" \
    bash -c '
        source "${ENTRYPOINT_PATH}"
        K3S_MODE="true"
        SECURE_MODE="false"
        DOCKER_TARGET_IP="10.42.1.7"
        K3S_BYPASS_CIDRS="10.42.0.0/16,10.43.0.0/16"
        WG_IFACE="tunnelsatsv2"
        ensure_policy_routing

        external_route="$(ip route get 198.51.100.2 from 10.42.1.7 iif pod-r)"
        [[ "${external_route}" == *"dev tunnelsatsv2"* ]]

        local_route="$(ip route get 10.42.1.1 from 10.42.1.7 iif pod-r)"
        [[ "${local_route}" == *"dev pod-r"* ]]
    '

# A new outbound connection now succeeds through the VPN-side namespace.
ip netns exec "${POD_NS}" ping -c 1 -W 2 198.51.100.2 >/dev/null

# A connection arriving from the VPN side also receives its reply through the
# same path, exercising the inbound-reply invariant without connmarks.
ip netns exec "${VPN_NS}" ping -I 198.51.100.2 -c 1 -W 2 10.42.1.7 >/dev/null

# Cluster-local dependencies remain available through the explicit bypass.
ip netns exec "${POD_NS}" ping -c 1 -W 2 10.42.1.1 >/dev/null

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

echo "k3s full-outbound routing integration test passed"
