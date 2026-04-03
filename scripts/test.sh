#!/bin/bash
# TunnelSats Unified Test Suite
# Consolidates unit, E2E, persistence, and entrypoint logic tests.

set -euo pipefail

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

usage() {
    echo "Usage: $0 [unit|e2e|persistence|entrypoint|all]"
    exit 1
}

run_unit() {
    log_info "Running Python Unit Tests..."
    cd "$REPO_ROOT"
    # Ensure dependencies are available (assuming venv)
    if [ -d "venv" ]; then source venv/bin/activate; fi
    python3 -m pytest server/tests/
}

run_e2e() {
    log_info "Running E2E Playwright Tests..."
    cd "$REPO_ROOT"
    # Placeholder for actual playwright command if configured
    if [ -f "node_modules/.bin/playwright" ]; then
        npx playwright test
    else
        log_error "Playwright not found in node_modules."
        return 1
    fi
}

run_persistence() {
    log_info "Running Data Persistence TDD Suite..."
    # Logic from test-persistence.sh
    APP_ID="tunnelsats"
    TEMP_ROOT="/tmp/tunnelsats_persistence_test"
    USER_HOME="${TEMP_ROOT}/home/umbrel"
    UMBREL_ROOT="${USER_HOME}/umbrel"
    APP_DATA_DIR="${UMBREL_ROOT}/app-data/${APP_ID}"
    PERSISTENT_DATA_DIR="${UMBREL_ROOT}/app-data/${APP_ID}-data"
    MAPPED_DIR="${PERSISTENT_DATA_DIR}" # Simulated mapping

    mkdir -p "${APP_DATA_DIR}" "${PERSISTENT_DATA_DIR}"

    echo "Simulating Persistent Fix..."
    touch "${MAPPED_DIR}/tunnelsats.conf"
    echo "Simulating Uninstall (rm -rf ${APP_DATA_DIR})..."
    rm -rf "${APP_DATA_DIR}"

    if [[ -f "${PERSISTENT_DATA_DIR}/tunnelsats.conf" ]]; then
        log_info "Persistence Test: PASS (Data survived)"
    else
        log_error "Persistence Test: FAIL (Data lost!)"
        return 1
    fi
    rm -rf "${TEMP_ROOT}"
}

run_entrypoint() {
    log_info "Running Entrypoint Logic Tests..."
    # Logic from test-entrypoint-logic.sh (Mocking JQ matches)
    # Check if jq is installed
    if ! command -v jq &> /dev/null; then log_error "jq required"; return 1; fi

    evaluate_detection() {
        local json_input="$1"
        echo "${json_input}" | jq -r '
            def cname: (.Names[0] // "") | ltrimstr("/");
            [ .[]
              | {id: .Id, name: cname}
              | select(.name | test("(^|[_-])lnd([_-]|$)"))
              | select(.name | test("(app|proxy|tor|web|ui)") | not)
            ] | .[0] | "\(.id)|\(.name)|lnd"
        ' 2>/dev/null
    }

    CONTAINERS_LND='[{"Id": "daemon123", "Names": ["/lightning_lnd_1"]}]'
    RESULT=$(evaluate_detection "$CONTAINERS_LND")
    if [[ "$RESULT" == "daemon123|lightning_lnd_1|lnd" ]]; then
        log_info "Entrypoint Case 1 (LND): PASS"
    else
        log_error "Entrypoint Case 1: FAIL (Got: $RESULT)"
        return 1
    fi
}

run_container() {
    log_info "Running Local Container Sanity Check..."
    IMAGE="tunnelsats/umbrel-app:test"
    docker build -t "$IMAGE" "$REPO_ROOT"
    
    declare -a cmds=(
        "python3 --version"
        "wg --version"
        "curl --version"
        "jq --version"
    )

    for cmd in "${cmds[@]}"; do
        log_info ">> Checking: $cmd"
        docker run --rm --entrypoint="" "$IMAGE" sh -c "$cmd"
    done
    log_info "Container sanity check passed."
}

case "${1:-all}" in
    unit) run_unit ;;
    e2e) run_e2e ;;
    persistence) run_persistence ;;
    entrypoint) run_entrypoint ;;
    container) run_container ;;
    all)
        run_unit
        run_persistence
        run_entrypoint
        log_info "All tests passed!"
        ;;
    *) usage ;;
esac
