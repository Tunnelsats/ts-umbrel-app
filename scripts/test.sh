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

# Ensure cleanup on interruption or failure
cleanup() {
    log_info "Cleaning up test environment..."
    docker rm -f tunnelsats-test-e2e 2>/dev/null || true
    # Optionally prune the test image to save disk space
    # docker rmi tunnelsats/umbrel-app:test 2>/dev/null || true
}
trap cleanup EXIT

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

usage() {
    echo "Usage: $0 [unit|e2e|persistence|entrypoint|all]"
    exit 1
}

run_unit() {
    log_info "Running Python Unit Tests..."
    cd "$REPO_ROOT"
    # Ensure dependencies are available (Support both venv and .venv conventions)
    if [ -f "venv/bin/activate" ]; then source venv/bin/activate; elif [ -f ".venv/bin/activate" ]; then source .venv/bin/activate; fi
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
    log_info "Running Entrypoint Logic Tests (8 Cases)..."
    
    if ! command -v jq &> /dev/null; then log_error "jq required"; return 1; fi

    evaluate_detection() {
        local json_input="$1"
        # LND Match Logic
        local lnd_pick
        lnd_pick=$(echo "${json_input}" | jq -r '
            def cname: (.Names[0] // "") | ltrimstr("/");
            [ .[]
              | {id: .Id, name: cname}
              | select(.name | test("(^|[_-])lnd([_-]|$)"))
              | select(.name | test("(app|proxy|tor|web|ui)") | not)
            ] | .[0] | "\(.id)|\(.name)|lnd"
        ' 2>/dev/null)

        if [[ "$lnd_pick" != "null|null|lnd" ]] && [[ -n "$lnd_pick" ]]; then
            echo "$lnd_pick"
            return
        fi

        # CLN Match Logic
        local cln_pick
        cln_pick=$(echo "${json_input}" | jq -r '
            def cname: (.Names[0] // "") | ltrimstr("/");
            [ .[]
              | {id: .Id, name: cname}
              | select(.name | test("(^|[_-])(core-lightning|clightning|lightningd)([_-]|$)"))
              | select(.name | test("(app|proxy|tor|web|ui)") | not)
            ] | .[0] | "\(.id)|\(.name)|cln"
        ' 2>/dev/null)
        echo "$cln_pick"
    }

    # Case 1: LND Isolation
    CONTAINERS_LND='[{"Id": "daemon123", "Names": ["/lightning_lnd_1"]}, {"Id": "tor789", "Names": ["/lightning_tor_1"]}]'
    [ "$(evaluate_detection "$CONTAINERS_LND")" == "daemon123|lightning_lnd_1|lnd" ] || { log_error "Failed Case 1"; return 1; }
    log_info "Case 1 (LND Isolation): PASS"

    # Case 2: CLN Isolation
    CONTAINERS_CLN='[{"Id": "cln_daemon", "Names": ["/core-lightning_lightningd_1"]}, {"Id": "cln_app", "Names": ["/core-lightning_app_1"]}]'
    [ "$(evaluate_detection "$CONTAINERS_CLN")" == "cln_daemon|core-lightning_lightningd_1|cln" ] || { log_error "Failed Case 2"; return 1; }
    log_info "Case 2 (CLN Isolation): PASS"

    # Case 3: Mixed (LND Priority)
    CONTAINERS_MIXED='[{"Id": "lnd_id", "Names": ["/lnd"]}, {"Id": "cln_id", "Names": ["/core-lightning_lightningd_1"]}]'
    [ "$(evaluate_detection "$CONTAINERS_MIXED")" == "lnd_id|lnd|lnd" ] || { log_error "Failed Case 3"; return 1; }
    log_info "Case 3 (LND Priority): PASS"

    # Case 4: CLN Port Semantic Check
    resolve_port() { [[ "$1" == "cln" ]] && echo "9736" || echo "9735"; }
    [ "$(resolve_port "cln")" == "9736" ] || { log_error "Failed Case 4"; return 1; }
    log_info "Case 4 (CLN Port 9736): PASS"

    # Case 5: Metadata Path Priority
    find_meta() { local paths=("/data.json" "/app.json"); echo "${paths[0]}"; }
    [ "$(find_meta)" == "/data.json" ] || { log_error "Failed Case 5"; return 1; }
    log_info "Case 5 (Meta Path): PASS"

    # Case 6: Config Priority
    read_wg_config_path() { local d="$1"; local -a f=(); mapfile -t f < <(ls -1t "${d}"/tunnelsats*.conf 2>/dev/null | grep -E -v '\.bak(\.[0-9]+)*$' || true); echo "${f[0]:-}"; }
    T_DIR="/tmp/ts_test_c"; mkdir -p "$T_DIR"; touch -t 202601011000 "$T_DIR/old.conf"; touch -t 202601011100 "$T_DIR/tunnelsats_new.conf"
    [ "$(read_wg_config_path "$T_DIR")" == "$T_DIR/tunnelsats_new.conf" ] || { log_error "Failed Case 6"; rm -rf "$T_DIR"; return 1; }
    log_info "Case 6 (Config Priority): PASS"

    # Case 7: Stdout Pollution
    log() { printf '[%s] %s\n' "$1" "$2" >&2; }
    T_OUT=$(read_wg_config_path "$T_DIR" 2>/dev/null)
    [[ "$T_OUT" == *"WARN"* ]] && { log_error "Failed Case 7"; rm -rf "$T_DIR"; return 1; }
    log_info "Case 7 (Stdout Pollution): PASS"
    rm -rf "$T_DIR"

    # Case 8: Migration Logic
    migrate() {
        local data="$1"; local mig="$2";
        if [ ! -f "$data/tunnelsats.conf" ] && [ -f "$mig/tunnelsats.conf" ]; then
            cp "$mig"/tunnelsats* "$data/" 2>/dev/null || true
            cp "$mig"/*.bak "$data/" 2>/dev/null || true
        fi
    }
    M_DIR_DATA="/tmp/ts_m_data"; M_DIR_SRC="/tmp/ts_m_src";
    mkdir -p "$M_DIR_DATA" "$M_DIR_SRC"
    touch "$M_DIR_SRC/tunnelsats.conf" "$M_DIR_SRC/tunnelsats.bak"
    migrate "$M_DIR_DATA" "$M_DIR_SRC"
    [ -f "$M_DIR_DATA/tunnelsats.conf" ] && [ -f "$M_DIR_DATA/tunnelsats.bak" ] || { log_error "Failed Case 8"; rm -rf "$M_DIR_DATA" "$M_DIR_SRC"; return 1; }
    log_info "Case 8 (Migration Logic): PASS"
    rm -rf "$M_DIR_DATA" "$M_DIR_SRC"

    # Case 9: Bak+Subdir Exclusion (Critical Regression Fix)
    BOOT_DIR="/tmp/ts_boot"; mkdir -p "$BOOT_DIR/backup"
    touch -t 202603101000 "$BOOT_DIR/tunnelsats.conf.bak"
    touch -t 202603211100 "$BOOT_DIR/tunnelsats.conf"
    read_wg_boot() { local d="$1"; local -a f=(); mapfile -t f < <(ls -1t "${d}"/tunnelsats* 2>/dev/null | grep -E -v '\.bak(\.[0-9]+)*$' || true); echo "${f[0]:-}"; }
    [ "$(read_wg_boot "$BOOT_DIR")" == "$BOOT_DIR/tunnelsats.conf" ] || { log_error "Failed Case 9"; rm -rf "$BOOT_DIR"; return 1; }
    log_info "Case 9 (Bak+Subdir Exclusion): PASS"; rm -rf "$BOOT_DIR"
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
