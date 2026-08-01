#!/usr/bin/env bash
# shellcheck disable=SC2034
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_SUFFIX="$$"
CLEAR_NETWORK="ts-clear-${TEST_SUFFIX}"
PROTECTED_NETWORK="ts-protected-${TEST_SUFFIX}"
TARGET_CONTAINER="ts-route-lnd-${TEST_SUFFIX}"
CLEAR_SUBNET="10.121.120.0/24"
PROTECTED_SUBNET="10.121.121.0/24"
CLEAR_IP="10.121.120.9"
PROTECTED_IP="10.121.121.9"

cleanup() {
    docker rm -f "${TARGET_CONTAINER}" >/dev/null 2>&1 || true
    docker network rm "${CLEAR_NETWORK}" >/dev/null 2>&1 || true
    docker network rm "${PROTECTED_NETWORK}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

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

# shellcheck disable=SC1091
source "${REPO_ROOT}/scripts/entrypoint.sh"
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

echo "Docker protected default-route integration test passed"
