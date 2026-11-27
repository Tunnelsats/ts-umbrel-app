#!/bin/bash
# TunnelSats Unified Synchronization & Workflow
# Standardized RSync deployment for Umbrel 1.x
# Canonical compose lives under tunnelsats/ for app-store parity.

set -euo pipefail

GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Auto-load credentials from .env.local if present
if [ -f "${REPO_ROOT}/.env.local" ]; then
    set -a
    source "${REPO_ROOT}/.env.local"
    set +a
fi

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

check_sha256() {
    local expected="$1"
    local file="$2"
    if command -v sha256sum >/dev/null 2>&1; then
        printf "%s  %s\n" "$expected" "$file" | sha256sum -c - >/dev/null 2>&1
    elif command -v shasum >/dev/null 2>&1; then
        printf "%s  %s\n" "$expected" "$file" | shasum -a 256 -c - >/dev/null 2>&1
    else
        return 1
    fi
}

UMBREL_HOST="${UMBREL_HOST:-umbrel.local}"

usage() {
    echo "Usage: $0 [node|monorepo|vendor|version|promote]"
    echo ""
    echo "Commands:"
    echo "  node     Hot-patch the running tunnelsats container (docker cp + restart)"
    echo "  monorepo Push to remote repository"
    echo "  vendor   Update vendor assets"
    echo "  version VERSION --notes TEXT"
    echo "           Update all version locations and release notes"
    echo "  promote  Release promotion workflow"
    exit 1
}

# Hot-patch the running tunnelsats container with local code.
# This is the dev inner loop: rsync to staging → docker cp → restart.
# Does NOT touch app-stores. The community store pulls from GitHub automatically.
run_node() {
    log_info "Hot-patching tunnelsats on ${UMBREL_HOST}..."

    export SSHPASS="${UMBREL_PASSWORD:-}"
    SSH_PREFIX=""
    if [ -n "$SSHPASS" ]; then SSH_PREFIX="sshpass -e "; fi

    APP_ID="tunnelsats"
    UMBREL_APP_DATA="/home/umbrel/umbrel/app-data/${APP_ID}"
    UMBREL_COMPOSE="${UMBREL_APP_DATA}/docker-compose.yml"

    # 1. Verify app is installed
    log_info "Verifying ${APP_ID} is installed..."
    HAS_APP=$(${SSH_PREFIX}ssh -o StrictHostKeyChecking=accept-new umbrel@${UMBREL_HOST} \
        "test -d ${UMBREL_APP_DATA} && echo yes || echo no")
    if [ "$HAS_APP" = "no" ]; then
        log_error "tunnelsats not installed. Run: umbreld client apps.install.mutate --appId tunnelsats"
        exit 1
    fi

    # 2. Build Tailwind CSS bundle & stage local files on node
    log_info "Compiling Tailwind CSS bundle for hot-sync..."
    if [ ! -x "${REPO_ROOT}/node_modules/.bin/tailwindcss" ]; then
        (cd "${REPO_ROOT}" && npm ci --ignore-scripts)
    fi
    (cd "${REPO_ROOT}" && npm run build:css)
    chmod -R a+rX "${REPO_ROOT}/web/dist"

    log_info "Staging local source on node..."
    ${SSH_PREFIX}ssh -o StrictHostKeyChecking=accept-new umbrel@${UMBREL_HOST} "mkdir -p dev-patch"
    ${SSH_PREFIX}rsync -av --delete \
        -e "ssh -o StrictHostKeyChecking=accept-new" \
        --exclude=".git" --exclude="__pycache__" --exclude=".env" --exclude=".env.local" \
        --exclude="node_modules" --exclude="venv" --exclude=".venv" --exclude=".pytest_cache" \
        --exclude=".scratch" \
        "${REPO_ROOT}/" \
        umbrel@${UMBREL_HOST}:/home/umbrel/dev-patch/

    # 3. Recreate → Inject → Restart (the deploy.py pattern)
    log_info "Injecting (rm → up → cp → restart)..."
    SECURE_MODE_VAL="${SECURE_MODE:-false}"
    (
        echo "SSHPASS=\"${SSHPASS}\""
        echo "SECURE_MODE_VAL=\"${SECURE_MODE_VAL}\""
        cat << 'EOF'
set -euo pipefail
APP_ID="tunnelsats"
WEB_CONTAINER=""
DAEMON_CONTAINER=""
UMBREL_APP_DATA="/home/umbrel/umbrel/app-data/tunnelsats"
UMBREL_COMPOSE="${UMBREL_APP_DATA}/docker-compose.yml"

run_sudo() {
    if [ -n "${SSHPASS:-}" ]; then
        printf '%s\n' "${SSHPASS}" | sudo -S -p "" "$@"
    else
        sudo "$@"
    fi
}

# Keep the installed app definition aligned with the code being hot-patched.
# Only umbreld may restart this Compose model because it injects the complete
# authenticated app_proxy service (image, auth secret, and public port).
run_sudo cp /home/umbrel/dev-patch/tunnelsats/docker-compose.yml "${UMBREL_COMPOSE}"
run_sudo cp /home/umbrel/dev-patch/tunnelsats/umbrel-app.yml "${UMBREL_APP_DATA}/umbrel-app.yml"
TARGET_IMAGE=$(run_sudo grep "image: tunnelsats/ts-umbrel-app:" "${UMBREL_COMPOSE}" | head -n 1 | awk '{print $2}')
EXISTING_IMAGE=$(run_sudo docker images --format "{{.Repository}}:{{.Tag}}" | grep "^tunnelsats/ts-umbrel-app:" | head -n 1)
if [ -n "${EXISTING_IMAGE}" ] && [ -n "${TARGET_IMAGE}" ]; then
    run_sudo docker tag "${EXISTING_IMAGE}" "${TARGET_IMAGE}" || true
fi
if ! run_sudo grep -A 3 "app_proxy:" "${UMBREL_COMPOSE}" | run_sudo grep -q "image:"; then
    run_sudo sed -i '/app_proxy:/a \    image: getumbrel/app-proxy:1.7.0' "${UMBREL_COMPOSE}"
fi
if ! run_sudo grep -A 15 "app_proxy:" "${UMBREL_COMPOSE}" | run_sudo grep -q "ports:"; then
    run_sudo sed -i '/app_proxy:/a \    ports:\n      - "9739:9739"\n    volumes:\n      - '"${UMBREL_APP_DATA}"'/umbrel-app.yml:/extra/umbrel-app.yml:ro' "${UMBREL_COMPOSE}"
fi
if ! run_sudo grep -A 10 "app_proxy:" "${UMBREL_COMPOSE}" | run_sudo grep -q "UMBREL_AUTH_SECRET"; then
    run_sudo sed -i 's|APP_PORT: "9740"|APP_PORT: "9740"\n      AUTH_SERVICE_PORT: "2000"\n      PROXY_PORT: "9739"\n      UMBREL_AUTH_SECRET: DEADBEEF\n      JWT_SECRET: 10e3158d95b3fc002b2b8873f741488887c72e439bd248835cbb1f5fed63f31e\n      MANAGER_IP: tunnelsats-web\n      MANAGER_PORT: "9740"|' "${UMBREL_COMPOSE}"
fi
run_sudo sed -i "s|SECURE_MODE=\${SECURE_MODE:-false}|SECURE_MODE=${SECURE_MODE_VAL}|" "${UMBREL_COMPOSE}"
if [ "${SECURE_MODE_VAL}" = "true" ]; then
    if ! run_sudo grep -A 10 "tunnelsats-web:" "${UMBREL_COMPOSE}" | run_sudo grep -q "SECURE_MODE=true"; then
        run_sudo sed -i 's|MANAGEMENT_TRUSTED_PROXY_HOST=app_proxy|MANAGEMENT_TRUSTED_PROXY_HOST=app_proxy\n      - SECURE_MODE=true|' "${UMBREL_COMPOSE}"
    fi
    if ! run_sudo grep -A 10 "tunnelsats-daemon:" "${UMBREL_COMPOSE}" | run_sudo grep -q "SECURE_MODE=true"; then
        run_sudo sed -i 's|MANAGEMENT_BROWSER_SECURITY_ENABLED=false|MANAGEMENT_BROWSER_SECURITY_ENABLED=false\n      - SECURE_MODE=true|' "${UMBREL_COMPOSE}"
    fi
fi
if ! run_sudo grep -q "/home/umbrel/dev-patch/server" "${UMBREL_COMPOSE}"; then
    run_sudo sed -i 's|${APP_DATA_DIR}/runtime:/run/tunnelsats|${APP_DATA_DIR}/runtime:/run/tunnelsats\n      - /home/umbrel/dev-patch/scripts/entrypoint.sh:/app/scripts/entrypoint.sh:ro\n      - /home/umbrel/dev-patch/server:/app/server:ro\n      - /home/umbrel/dev-patch/web:/app/web:ro\n      - /home/umbrel/dev-patch/scripts:/app/scripts:ro\n      - /home/umbrel/dev-patch/tunnelsats/umbrel-app.yml:/app/umbrel-app.yml:ro|g' "${UMBREL_COMPOSE}"
fi
run_sudo umbreld client apps.restart.mutate --appId "${APP_ID}"
for i in $(seq 1 10); do
    WEB_CONTAINER="$(run_sudo docker ps -q --filter label=com.docker.compose.service=tunnelsats-web | head -n 1)"
    DAEMON_CONTAINER="$(run_sudo docker ps -q --filter label=com.docker.compose.service=tunnelsats-daemon | head -n 1)"
    if [ -z "${WEB_CONTAINER}" ] || [ -z "${DAEMON_CONTAINER}" ]; then
        sleep 1
        continue
    fi
    if [ "$(run_sudo docker inspect -f '{{.State.Running}}' "${WEB_CONTAINER}" 2>/dev/null)" = "true" ] && \
       [ "$(run_sudo docker inspect -f '{{.State.Running}}' "${DAEMON_CONTAINER}" 2>/dev/null)" = "true" ]; then
        break
    fi
    sleep 1
done
if [ "$(run_sudo docker inspect -f '{{.State.Running}}' "${WEB_CONTAINER}" 2>/dev/null)" != "true" ] || \
   [ "$(run_sudo docker inspect -f '{{.State.Running}}' "${DAEMON_CONTAINER}" 2>/dev/null)" != "true" ]; then
    echo "TunnelSats split services failed to restart through umbreld" >&2
    exit 1
fi
if ! run_sudo grep -q "/home/umbrel/dev-patch/server" "${UMBREL_COMPOSE}"; then
    run_sudo docker cp /home/umbrel/dev-patch/server/. "${WEB_CONTAINER}":/app/server/
    run_sudo docker cp /home/umbrel/dev-patch/server/. "${DAEMON_CONTAINER}":/app/server/
    run_sudo docker cp /home/umbrel/dev-patch/web/. "${WEB_CONTAINER}":/app/web/
    run_sudo docker cp /home/umbrel/dev-patch/scripts/. "${WEB_CONTAINER}":/app/scripts/
    run_sudo docker cp /home/umbrel/dev-patch/scripts/. "${DAEMON_CONTAINER}":/app/scripts/
    run_sudo docker cp /home/umbrel/dev-patch/tunnelsats/umbrel-app.yml "${WEB_CONTAINER}":/app/umbrel-app.yml
    run_sudo docker cp /home/umbrel/dev-patch/tunnelsats/umbrel-app.yml "${DAEMON_CONTAINER}":/app/umbrel-app.yml
    run_sudo docker exec --user 0 "${WEB_CONTAINER}" chmod +x /app/scripts/entrypoint.sh /app/scripts/sync.sh 2>/dev/null || true
    run_sudo docker exec --user 0 "${DAEMON_CONTAINER}" chmod +x /app/scripts/entrypoint.sh /app/scripts/sync.sh 2>/dev/null || true
fi
run_sudo docker restart "${WEB_CONTAINER}" "${DAEMON_CONTAINER}" >/dev/null
EOF
    ) | ${SSH_PREFIX}ssh -o StrictHostKeyChecking=accept-new -T umbrel@${UMBREL_HOST} bash

    log_info "Deploy successful! tunnelsats hot-patched on ${UMBREL_HOST}."
}

run_monorepo() {
    log_info "Pushing to remote repository..."
    git push
}

run_vendor() {
    log_info "Updating localized vendor assets..."
    local MANIFEST="${REPO_ROOT}/web/vendor/vendor.json"
    
    if ! command -v jq &> /dev/null; then log_error "jq is required for vendor sync."; return 1; fi
    if ! command -v curl &> /dev/null; then log_error "curl is required for vendor sync."; return 1; fi
    if ! command -v sha256sum &> /dev/null && ! command -v shasum &> /dev/null; then
        log_error "sha256sum or shasum is required for vendor sync."
        return 1
    fi
    if [ ! -f "$MANIFEST" ]; then log_error "Vendor manifest not found at $MANIFEST"; return 1; fi

    local FORCE="false"
    if [[ "${1:-}" == "force" ]]; then FORCE="true"; fi

    # Read assets from JSON
    if ! jq -e '.assets | arrays' "$MANIFEST" > /dev/null 2>&1; then
        log_error "Vendor manifest is missing or has an invalid 'assets' array: $MANIFEST"
        return 1
    fi
    local FAILED=0
    local PROCESSED=0
    local name url local_path full_path needs_download manifest_data JQ_FAILED=0 sha256
    if ! manifest_data=$(jq -r '.assets[] | [.name, .source_url, .local_path, .sha256] | map(. // "") | join("\u001f")' "$MANIFEST"); then
        log_error "Manifest parsing failed (jq exited with non-zero status)."
        FAILED=$((FAILED + 1))
        JQ_FAILED=1
    fi

    if [ -n "${manifest_data:-}" ]; then
        while IFS=$'\x1f' read -r name url local_path sha256; do
            PROCESSED=$((PROCESSED + 1))
            if [ -z "$name" ] || [ -z "$url" ] || [ -z "$local_path" ]; then
                log_error "Invalid or incomplete asset entry: name='$name', url='$url', path='$local_path'"
                FAILED=$((FAILED + 1))
                continue
            fi

            if [ -z "$sha256" ]; then
                log_error "Missing sha256 checksum for asset: ${name}"
                FAILED=$((FAILED + 1))
                continue
            fi

            # Path traversal check: ensure local_path starts with "web/vendor/" and does not contain directory traversal
            if [[ "$local_path" != web/vendor/* ]] || [[ "$local_path" == */../* ]] || [[ "$local_path" == ../* ]] || [[ "$local_path" == */.. ]]; then
                log_error "Path traversal or invalid local path detected: '$local_path'"
                FAILED=$((FAILED + 1))
                continue
            fi

            full_path="${REPO_ROOT}/${local_path}"

            # Ensure directory exists
            if ! mkdir -p "$(dirname "$full_path")"; then
                log_error "Failed to create directory for: ${full_path}"
                FAILED=$((FAILED + 1))
                continue
            fi

            # Check if download is required
            needs_download="false"
            if [ "$FORCE" = "true" ] || [ ! -f "$full_path" ]; then
                needs_download="true"
            elif ! check_sha256 "$sha256" "$full_path"; then
                # Checksum mismatch on existing file
                log_info "   ⚠️  Checksum mismatch for ${name}, will re-download."
                needs_download="true"
            elif [ ! -f "${full_path}.meta" ]; then
                # File exists and is valid, but meta is missing
                if ! echo "$url" > "${full_path}.meta"; then
                    log_error "  ❌  Failed to write .meta for ${name}"
                    FAILED=$((FAILED + 1))
                    continue
                fi
            elif [ "$(cat "${full_path}.meta" 2>/dev/null)" != "$url" ]; then
                # URL mismatch (asset version bumped locally)
                needs_download="true"
            fi

            if [ "$needs_download" = "true" ]; then
                log_info "   ⬇️  Downloading ${name} from remote sources..."
                if curl -L -s --fail --show-error --connect-timeout 10 --max-time 60 "$url" -o "${full_path}.tmp"; then
                    if ! check_sha256 "$sha256" "${full_path}.tmp"; then
                        log_error "  ❌  Checksum verification failed for ${name}"
                        rm -f "${full_path}.tmp"
                        FAILED=$((FAILED + 1))
                    elif mv "${full_path}.tmp" "$full_path"; then
                        if ! echo "$url" > "${full_path}.meta"; then
                            log_error "  ❌  Failed to write .meta for ${name}"
                            FAILED=$((FAILED + 1))
                            continue
                        fi
                        log_info "   ✅  Localized ${name} to ${local_path}"
                    else
                        rm -f "${full_path}.tmp"
                        log_error "  ❌  Failed to save downloaded ${name}"
                        FAILED=$((FAILED + 1))
                    fi
                else
                    rm -f "${full_path}.tmp"
                    log_error "  ❌  Failed to download ${name}"
                    FAILED=$((FAILED + 1))
                fi
            else
                log_info "   💎  ${name} is already localized."
            fi
        done <<< "$manifest_data"
    fi
    
    if [ "$PROCESSED" -eq 0 ] && [ "$JQ_FAILED" -eq 0 ]; then
        log_error "No vendor assets were processed. (Check manifest or jq parsing)"
        FAILED=$((FAILED + 1))
    fi

    if [ "$FAILED" -ne 0 ]; then
        log_error "Vendor asset check finished with ${FAILED} error(s)."
        return 1
    else
        log_info "Vendor asset check finished successfully."
    fi
}

run_version() {
    python3 "${REPO_ROOT}/scripts/bump_version.py" "$@"
}

run_promote() {
    local dry_run="${1:-false}"
    log_info "Starting Release Promotion..."
    if [ "${dry_run}" = "true" ]; then
        log_info "[DRY RUN] Previewing changes without writing to target..."
    fi

    VERSION="${VERSION:-$(grep "^version: " "${REPO_ROOT}/tunnelsats/umbrel-app.yml" | sed -E 's/version: "?([^" ]+)"?.*/\1/' | head -1)}"
    if [ -z "$VERSION" ]; then log_error "Could not extract version"; return 1; fi
    log_info "Promoting version: ${VERSION}"

    IMAGE="tunnelsats/ts-umbrel-app:${VERSION}"
    log_info "Polling Docker Hub for $IMAGE multi-arch index digest..."
    DIGEST=""
    if command -v docker >/dev/null 2>&1; then
        DIGEST=$(docker buildx imagetools inspect "$IMAGE" 2>/dev/null | grep "Digest: " | head -1 | awk '{print $2}' || echo "")
    fi
    if [ -z "$DIGEST" ] && command -v curl >/dev/null 2>&1 && command -v jq >/dev/null 2>&1; then
        DIGEST=$(curl -fsSL --connect-timeout 10 --max-time 30 "https://hub.docker.com/v2/repositories/tunnelsats/ts-umbrel-app/tags/${VERSION#v}" 2>/dev/null | jq -r '.digest // empty' || echo "")
    fi
    if ! [[ "${DIGEST:-}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
        DIGEST=""
    fi

    if [ -z "$DIGEST" ]; then
        log_error "Failed to retrieve digest from Docker Hub. Is the image published?"
        return 1
    fi
    log_info "Discovered Digest: ${DIGEST}"

    UMBREL_APPS_DIR="${UMBREL_APPS_DIR:-${REPO_ROOT}/../umbrel-apps}"
    if [ ! -d "$UMBREL_APPS_DIR" ] && [ "${dry_run}" = "false" ]; then
        log_error "Umbrel Apps store repo not found at ${UMBREL_APPS_DIR}. Set UMBREL_APPS_DIR manually."
        return 1
    fi

    local sub_url="${SUBMISSION_URL:-https://github.com/getumbrel/umbrel-apps/pull/4919}"
    local existing_manifest="${UMBREL_APPS_DIR}/tunnelsats/umbrel-app.yml"
    local stage_dir
    stage_dir="$(mktemp -d)"

    log_info "Generating official-store manifests via scripts/promote_official_store.py..."
    local promote_status=0
    if [ -f "${existing_manifest}" ]; then
        python3 "${REPO_ROOT}/scripts/promote_official_store.py" \
            --source-dir "${REPO_ROOT}/tunnelsats" \
            --target-dir "${stage_dir}" \
            --version "${VERSION#v}" \
            --digest "${DIGEST}" \
            --submission-url "${sub_url}" \
            --existing-manifest "${existing_manifest}" || promote_status=$?
    else
        python3 "${REPO_ROOT}/scripts/promote_official_store.py" \
            --source-dir "${REPO_ROOT}/tunnelsats" \
            --target-dir "${stage_dir}" \
            --version "${VERSION#v}" \
            --digest "${DIGEST}" \
            --submission-url "${sub_url}" || promote_status=$?
    fi

    if [ "${promote_status}" -ne 0 ]; then
        rm -rf "${stage_dir}"
        log_error "Official-store promotion failed; target repository was not modified."
        return "${promote_status}"
    fi

    if [ "${dry_run}" = "true" ]; then
        log_info "Monorepo files generated in temporary dry-run directory: ${stage_dir}."
        if [ -d "${UMBREL_APPS_DIR}/tunnelsats" ]; then
            log_info "Comparing against existing files at ${UMBREL_APPS_DIR}/tunnelsats..."
            diff -urN "${UMBREL_APPS_DIR}/tunnelsats" "${stage_dir}" || true
        else
            log_info "Target directory ${UMBREL_APPS_DIR}/tunnelsats does not exist yet. Showing preview of generated files:"
            log_info "--- docker-compose.yml ---"
            cat "${stage_dir}/docker-compose.yml"
            log_info "--- umbrel-app.yml ---"
            cat "${stage_dir}/umbrel-app.yml"
        fi
        rm -rf "${stage_dir}"
        log_info "[DRY RUN] Complete."
    else
        log_info "Synchronizing to official monorepo at ${UMBREL_APPS_DIR}..."
        local sync_status=0
        mkdir -p "${UMBREL_APPS_DIR}/tunnelsats" || sync_status=$?
        if [ "${sync_status}" -eq 0 ]; then
            rsync -av --delete "${stage_dir}/" "${UMBREL_APPS_DIR}/tunnelsats/" || sync_status=$?
        fi
        rm -rf "${stage_dir}"
        if [ "${sync_status}" -ne 0 ]; then
            log_error "Failed to synchronize staged files to ${UMBREL_APPS_DIR}/tunnelsats."
            return "${sync_status}"
        fi
        log_info "Promotion complete. Commit changes in ${UMBREL_APPS_DIR}."
    fi
}

if [ "$#" -lt 1 ]; then usage; fi

case "${1}" in
    node) run_node ;;
    monorepo) run_monorepo ;;
    vendor) shift; run_vendor "$@" ;;
    version) shift; run_version "$@" ;;
    promote)
        shift
        dry_run=false
        if [ "${1:-}" = "--dry-run" ]; then
            dry_run=true
        fi
        run_promote "${dry_run}"
        ;;
    *) usage ;;
esac
