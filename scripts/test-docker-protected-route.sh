#!/usr/bin/env bash
# shellcheck disable=SC2034
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_SUFFIX="$$"
CLEAR_NETWORK="ts-clear-${TEST_SUFFIX}"
PROTECTED_NETWORK="ts-protected-${TEST_SUFFIX}"
TARGET_CONTAINER="ts-route-lnd-${TEST_SUFFIX}"
HOLDER_CONTAINER="ts-route-cln-${TEST_SUFFIX}"
STOPPED_CONTAINER="ts-route-stopped-${TEST_SUFFIX}"
CLEAR_SUBNET="10.121.120.0/24"
PROTECTED_SUBNET="10.121.121.0/24"
CLEAR_IP="10.121.120.9"
PROTECTED_IP="10.121.121.9"
DOCKER_POLICY_STATE_TMP="/tmp/tunnelsats-docker-policy-${TEST_SUFFIX}.json"
IPV6_POLICY_STATE_TMP="/tmp/tunnelsats-ipv6-policy-${TEST_SUFFIX}.json"
DECOMMISSION_STATE_TMP="/tmp/tunnelsats-decommissioned-${TEST_SUFFIX}.json"
STATE_FILE_TMP="/tmp/tunnelsats-state-${TEST_SUFFIX}.json"

cleanup_test_env() {
    docker rm -f "${TARGET_CONTAINER}" >/dev/null 2>&1 || true
    docker rm -f "${HOLDER_CONTAINER}" >/dev/null 2>&1 || true
    docker rm -f "${STOPPED_CONTAINER}" >/dev/null 2>&1 || true
    docker network rm "${CLEAR_NETWORK}" >/dev/null 2>&1 || true
    docker network rm "${PROTECTED_NETWORK}" >/dev/null 2>&1 || true
    rm -f \
        "${DOCKER_POLICY_STATE_TMP}" "${DOCKER_POLICY_STATE_TMP}.tmp."* \
        "${IPV6_POLICY_STATE_TMP}" "${IPV6_POLICY_STATE_TMP}.tmp."* \
        "${DECOMMISSION_STATE_TMP}" "${DECOMMISSION_STATE_TMP}.tmp."* \
        "${STATE_FILE_TMP}" "${STATE_FILE_TMP}.tmp."*
}
trap cleanup_test_env EXIT

api_version="$(docker version --format '{{.Server.APIVersion}}')"
if ! python3 -c '
import sys
parts = tuple(int(part) for part in sys.argv[1].split("."))
raise SystemExit(0 if parts >= (1, 48) else 1)
' "${api_version}"; then
    echo "SKIP: Docker protected-route integration test requires Engine API 1.48+" >&2
    exit 0
fi

docker network create --subnet "${CLEAR_SUBNET}" "${CLEAR_NETWORK}" >/dev/null
docker network create --subnet "${PROTECTED_SUBNET}" "${PROTECTED_NETWORK}" >/dev/null
docker create \
    --name "${TARGET_CONTAINER}" \
    --network "name=${CLEAR_NETWORK},gw-priority=99" \
    --ip "${CLEAR_IP}" \
    alpine:latest sh -c 'while true; do sleep 3600; done' >/dev/null
docker start "${TARGET_CONTAINER}" >/dev/null

export DOCKER_POLICY_STATE_FILE="${DOCKER_POLICY_STATE_TMP}"
export IPV6_POLICY_STATE_FILE="${IPV6_POLICY_STATE_TMP}"
export DECOMMISSION_STATE_FILE="${DECOMMISSION_STATE_TMP}"
# shellcheck disable=SC1091
source "${REPO_ROOT}/scripts/entrypoint.sh"
STATE_FILE="${STATE_FILE_TMP}"
K3S_MODE="false"
SECURE_MODE="false"
DOCKER_NETWORK_NAME="${PROTECTED_NETWORK}"
DOCKER_NETWORK_SUBNET="${PROTECTED_SUBNET}"
DOCKER_TARGET_IP="${PROTECTED_IP}"
TARGET_CONTAINER_ID="$(docker inspect --format '{{.Id}}' "${TARGET_CONTAINER}")"
TARGET_CONTAINER_NAME="${TARGET_CONTAINER}"

ensure_container_attached
verify_docker_target_routes

tunnel_priority="$(docker inspect --format "{{(index .NetworkSettings.Networks \"${PROTECTED_NETWORK}\").GwPriority}}" "${TARGET_CONTAINER}")"
clear_priority="$(docker inspect --format "{{(index .NetworkSettings.Networks \"${CLEAR_NETWORK}\").GwPriority}}" "${TARGET_CONTAINER}")"
if [ "${tunnel_priority}" -le "${clear_priority}" ]; then
    echo "FAIL: protected endpoint did not outrank the competing gateway" >&2
    exit 1
fi

# Removing the protected endpoint must immediately make live verification fail.
docker network disconnect "${PROTECTED_NETWORK}" "${TARGET_CONTAINER}"
if verify_docker_target_routes; then
    echo "FAIL: missing protected endpoint passed route verification" >&2
    exit 1
fi
ensure_container_attached
verify_docker_target_routes

# A newly reconnected ordinary endpoint can compete for the default route, but
# reconciliation must raise the protected priority and restore the invariant.
docker network disconnect "${CLEAR_NETWORK}" "${TARGET_CONTAINER}"
docker network connect --gw-priority 500 --ip "${CLEAR_IP}" "${CLEAR_NETWORK}" "${TARGET_CONTAINER}"
if verify_docker_target_routes; then
    echo "FAIL: competing ordinary gateway passed route verification" >&2
    exit 1
fi
ensure_container_attached
verify_docker_target_routes

docker restart "${TARGET_CONTAINER}" >/dev/null
ensure_container_attached
verify_docker_target_routes

protected_entry() {
    docker inspect --format '{{json .NetworkSettings.Networks}}' "$1" | jq -r --arg net "${PROTECTED_NETWORK}" '
        .[$net]
        | if . == null then "absent" else "present|\(.IPAddress // "")|\(.EndpointID // "")" end
    '
}

# Issue #173: another container holds the protected IP, as after a reconcile
# that protected the other Lightning implementation. Docker refuses the
# target's connect but keeps a protected-network entry without an EndpointID,
# and it cannot disconnect that entry either.
docker network disconnect "${PROTECTED_NETWORK}" "${TARGET_CONTAINER}"
docker run -d \
    --name "${HOLDER_CONTAINER}" \
    --network "${PROTECTED_NETWORK}" \
    --ip "${PROTECTED_IP}" \
    alpine:latest sh -c 'while true; do sleep 3600; done' >/dev/null
if docker network connect --ip "${PROTECTED_IP}" "${PROTECTED_NETWORK}" "${TARGET_CONTAINER}" >/dev/null 2>&1; then
    echo "FAIL: Docker attached a second container with the protected IP" >&2
    exit 1
fi
stale_entry="$(protected_entry "${TARGET_CONTAINER}")"
if [ "${stale_entry}" != "present||" ]; then
    echo "FAIL: expected a ${PROTECTED_NETWORK} entry without EndpointID after the refused connect, got: ${stale_entry}" >&2
    exit 1
fi
disconnect_error="$(docker network disconnect --force "${PROTECTED_NETWORK}" "${TARGET_CONTAINER}" 2>&1 || true)"
if [[ "${disconnect_error}" != *"is not connected"* ]]; then
    echo "FAIL: unexpected disconnect result for the stale entry: ${disconnect_error}" >&2
    exit 1
fi

# Reconciliation detaches the holder and attaches the target with the
# protected IP and a real endpoint.
ensure_container_attached
verify_docker_target_routes
holder_entry="$(protected_entry "${HOLDER_CONTAINER}")"
if [ "${holder_entry}" != "absent" ]; then
    echo "FAIL: the previous holder of the protected IP is still attached: ${holder_entry}" >&2
    exit 1
fi
target_entry="$(protected_entry "${TARGET_CONTAINER}")"
IFS='|' read -r target_state target_ip target_endpoint <<< "${target_entry}"
if [ "${target_state}" != "present" ] || [ "${target_ip}" != "${PROTECTED_IP}" ] || [ -z "${target_endpoint}" ]; then
    echo "FAIL: target was not reattached with ${PROTECTED_IP}: ${target_entry}" >&2
    exit 1
fi

# A stopped container that still has the protected network configured would
# claim the protected IP again when it starts. Reconciliation removes that
# configuration while the target stays attached.
docker create --name "${STOPPED_CONTAINER}" --network "${CLEAR_NETWORK}" \
    alpine:latest sh -c 'while true; do sleep 3600; done' >/dev/null
docker network connect --ip "${PROTECTED_IP}" "${PROTECTED_NETWORK}" "${STOPPED_CONTAINER}"
if [ "$(protected_entry "${STOPPED_CONTAINER}")" = "absent" ]; then
    echo "FAIL: the stopped container did not record the protected network" >&2
    exit 1
fi
ensure_container_attached
verify_docker_target_routes
stopped_entry="$(protected_entry "${STOPPED_CONTAINER}")"
if [ "${stopped_entry}" != "absent" ]; then
    echo "FAIL: the stopped container still has the protected network configured: ${stopped_entry}" >&2
    exit 1
fi
if [ "$(protected_entry "${TARGET_CONTAINER}")" != "${target_entry}" ]; then
    echo "FAIL: detaching the stopped container changed the target endpoint" >&2
    exit 1
fi

echo "Docker protected default-route integration test passed"
