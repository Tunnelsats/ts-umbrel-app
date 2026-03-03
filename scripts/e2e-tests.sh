#!/bin/bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:9739}"
COMPOSE_TEST_FILE="docker-compose.test.yml"

log() {
  printf '[e2e] %s\n' "$*"
}

wait_for_status() {
  local timeout="${1:-90}"
  local start
  start=$(date +%s)
  while true; do
    if curl -fsS "${BASE_URL}/api/local/status" >/dev/null 2>&1; then
      return 0
    fi
    if [ $(( $(date +%s) - start )) -ge "${timeout}" ]; then
      return 1
    fi
    sleep 2
  done
}

assert_json_field_nonempty() {
  local payload="$1"
  local field="$2"
  local value
  value=$(echo "${payload}" | jq -r "${field}")
  if [ -z "${value}" ] || [ "${value}" = "null" ]; then
    echo "Expected non-empty ${field} but got '${value}'"
    exit 1
  fi
}

setup_stack() {
  log "Starting test stack"
  docker compose -f "${COMPOSE_TEST_FILE}" up -d --build
  wait_for_status 120
}

teardown_stack() {
  log "Stopping test stack"
  docker compose -f "${COMPOSE_TEST_FILE}" down -v || true
}

scenario_happy_lnd() {
  log "Scenario: happy_lnd"
  local status
  status=$(curl -fsS "${BASE_URL}/api/local/status")

  assert_json_field_nonempty "${status}" '.dataplane_mode'
  assert_json_field_nonempty "${status}" '.docker_network.name'
  assert_json_field_nonempty "${status}" '.target_container'
  assert_json_field_nonempty "${status}" '.target_ip'
  assert_json_field_nonempty "${status}" '.forwarding_port'

  echo "${status}" | jq -e '.rules_synced == true or .rules_synced == false' >/dev/null
}

scenario_happy_cln() {
  log "Scenario: happy_cln"
  docker stop mock_lightning_lnd_1 >/dev/null
  curl -fsS -X POST "${BASE_URL}/api/local/reconcile" >/dev/null

  local status
  status=$(curl -fsS "${BASE_URL}/api/local/status")
  echo "${status}" | jq -e '.target_container | test("core-lightning|cln|lightningd")' >/dev/null

  docker start mock_lightning_lnd_1 >/dev/null
}

scenario_manual_reconcile() {
  log "Scenario: manual_reconcile"
  local result
  result=$(curl -fsS -X POST "${BASE_URL}/api/local/reconcile")

  echo "${result}" | jq -e '.success == true' >/dev/null
  assert_json_field_nonempty "${result}" '.request_id'
}

scenario_drift_restart() {
  log "Scenario: drift_restart"
  docker restart mock_lightning_lnd_1 >/dev/null
  sleep 3

  curl -fsS -X POST "${BASE_URL}/api/local/reconcile" >/dev/null
  local status
  status=$(curl -fsS "${BASE_URL}/api/local/status")
  echo "${status}" | jq -e '.target_container | length > 0' >/dev/null
}

scenario_inbound_rules_present() {
  log "Scenario: inbound_reachability (rule presence)"
  docker exec tunnelsats sh -lc 'iptables -t nat -S PREROUTING | grep -q tunnelsats-dnat'
  docker exec tunnelsats sh -lc 'iptables -S FORWARD | grep -q tunnelsats-forward-in'
  docker exec tunnelsats sh -lc 'iptables -S FORWARD | grep -q tunnelsats-forward-out'
}

scenario_missing_socket() {
  log "Scenario: missing_socket"
  docker run --rm --name tunnelsats-no-sock \
    --network host \
    --cap-add NET_ADMIN --cap-add NET_RAW \
    -v "$(pwd)/data:/data" \
    tunnelsats/umbrel-app:v3.0.0 sh -lc '\
      /app/scripts/entrypoint.sh & \
      pid=$!; \
      sleep 8; \
      curl -fsS http://127.0.0.1:9739/api/local/status > /tmp/status.json || true; \
      cat /tmp/status.json; \
      kill $pid || true'
}

scenario_missing_config() {
  log "Scenario: missing_config"
  local temp_data
  temp_data="$(mktemp -d)"

  docker run --rm --name tunnelsats-no-config \
    --network host \
    --cap-add NET_ADMIN --cap-add NET_RAW \
    -v "${temp_data}:/data" \
    -v /var/run/docker.sock:/var/run/docker.sock:ro \
    tunnelsats/umbrel-app:v3.0.0 sh -lc '\
      /app/scripts/entrypoint.sh & \
      pid=$!; \
      sleep 8; \
      status=$(curl -fsS http://127.0.0.1:9739/api/local/status || true); \
      echo "$status" | jq -e ".last_error | test(\"WireGuard config\")" >/dev/null; \
      kill $pid || true'

  rm -rf "${temp_data}"
}

scenario_shutdown_cleanup() {
  log "Scenario: shutdown_cleanup"
  docker stop tunnelsats >/dev/null
  sleep 2

  if command -v iptables >/dev/null 2>&1; then
    if iptables -t nat -S PREROUTING | grep -q "tunnelsats-dnat"; then
      echo "Expected no tunnelsats-dnat rule after shutdown"
      exit 1
    fi
  fi

  docker compose -f "${COMPOSE_TEST_FILE}" up -d tunnelsats >/dev/null
  wait_for_status 60
}

main() {
  trap teardown_stack EXIT

  setup_stack
  scenario_happy_lnd
  scenario_happy_cln
  scenario_manual_reconcile
  scenario_drift_restart
  scenario_inbound_rules_present
  scenario_missing_config

  # Run missing socket scenario outside compose to keep main stack intact.
  scenario_missing_socket
  scenario_shutdown_cleanup

  log "All scenarios completed"
}

main "$@"
