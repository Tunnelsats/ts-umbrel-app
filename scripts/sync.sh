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
        echo "$expected  $file" | sha256sum --status -c -
    elif command -v shasum >/dev/null 2>&1; then
        echo "$expected  $file" | shasum -a 256 --status -c -
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
    echo "  version  Update version string"
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

    # 2. Stage local files on node (excluding build artifacts)
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
    REMOTE_CMD="docker rm -f ${APP_ID} 2>/dev/null || true && \
                APP_DATA_DIR=${UMBREL_APP_DATA} docker compose -f ${UMBREL_COMPOSE} up -d && \
                for i in \$(seq 1 10); do [ \"\$(docker inspect -f '{{.State.Running}}' ${APP_ID} 2>/dev/null)\" = \"true\" ] && break; sleep 1; done && \
                docker cp /home/umbrel/dev-patch/server/. ${APP_ID}:/app/server/ && \
                docker cp /home/umbrel/dev-patch/web/. ${APP_ID}:/app/web/ && \
                docker cp /home/umbrel/dev-patch/scripts/. ${APP_ID}:/app/scripts/ && \
                docker cp /home/umbrel/dev-patch/tunnelsats/umbrel-app.yml ${APP_ID}:/app/umbrel-app.yml && \
                docker exec ${APP_ID} chmod +x /app/scripts/entrypoint.sh /app/scripts/sync.sh 2>/dev/null; \
                docker restart ${APP_ID}"

    ${SSH_PREFIX}ssh -o StrictHostKeyChecking=accept-new umbrel@${UMBREL_HOST} "${REMOTE_CMD}"

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

            # Path traversal check: ensure local_path starts with "web/vendor/" and does not contain ".."
            if [[ "$local_path" != web/vendor/* ]] || [[ "$local_path" == *..* ]]; then
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
    if [ "$#" -lt 1 ]; then log_error "Version argument required"; return 1; fi
    NEW_VERSION="${1#v}"
    if [[ "$NEW_VERSION" =~ [^a-zA-Z0-9._-] ]]; then
        log_error "Invalid version string: $NEW_VERSION (only alphanumeric, '.', '_', '-' allowed)"
        return 1
    fi
    log_info "Updating version to ${NEW_VERSION}..."
    sed "s/version: .*/version: \"${NEW_VERSION}\"/" "${REPO_ROOT}/tunnelsats/umbrel-app.yml" > "${REPO_ROOT}/tunnelsats/umbrel-app.yml.tmp" && mv "${REPO_ROOT}/tunnelsats/umbrel-app.yml.tmp" "${REPO_ROOT}/tunnelsats/umbrel-app.yml"
    sed -E "s#(ts-umbrel-app:v?)[^@\" ]+(@sha256:[0-9a-f]{64})?#\1${NEW_VERSION}#" "${REPO_ROOT}/tunnelsats/docker-compose.yml" > "${REPO_ROOT}/tunnelsats/docker-compose.yml.tmp" && mv "${REPO_ROOT}/tunnelsats/docker-compose.yml.tmp" "${REPO_ROOT}/tunnelsats/docker-compose.yml"
    if [ -f "${REPO_ROOT}/web/index.html" ]; then
        sed -E "s/(id=\"app-version\"[^>]*>v?)[0-9]+\.[0-9]+\.[0-9]+[a-zA-Z0-9.-]*(<\/span>)/\1${NEW_VERSION}\2/" "${REPO_ROOT}/web/index.html" > "${REPO_ROOT}/web/index.html.tmp" && mv "${REPO_ROOT}/web/index.html.tmp" "${REPO_ROOT}/web/index.html"
    fi
}

run_promote() {
    log_info "Starting Release Promotion..."

    VERSION="${VERSION:-$(grep "^version: " "${REPO_ROOT}/tunnelsats/umbrel-app.yml" | sed -E 's/version: "?([^" ]+)"?.*/\1/' | head -1)}"
    if [ -z "$VERSION" ]; then log_error "Could not extract version"; return 1; fi
    log_info "Promoting version: ${VERSION}"

    IMAGE="tunnelsats/ts-umbrel-app:${VERSION}"
    log_info "Polling Docker Hub for $IMAGE multi-arch index digest..."
    DIGEST=$(docker buildx imagetools inspect "$IMAGE" | grep "Digest: " | head -1 | awk '{print $2}' || echo "")

    if [ -z "$DIGEST" ]; then
        log_error "Failed to retrieve digest from Docker Hub. Is the image published?"
        return 1
    fi
    log_info "Discovered Digest: ${DIGEST}"

    UMBREL_APPS_DIR="${UMBREL_APPS_DIR:-${REPO_ROOT}/../umbrel-apps}"
    if [ ! -d "$UMBREL_APPS_DIR" ]; then
        log_error "Umbrel Apps store repo not found at ${UMBREL_APPS_DIR}. Set UMBREL_APPS_DIR manually."
        return 1
    fi

    log_info "Synchronizing to official monorepo at ${UMBREL_APPS_DIR}..."
    rsync -av --delete --exclude=".gitkeep" "${REPO_ROOT}/tunnelsats/" "${UMBREL_APPS_DIR}/tunnelsats/"

    log_info "Pinning image digest in monorepo docker-compose.yml..."
    TARGET_COMPOSE="${UMBREL_APPS_DIR}/tunnelsats/docker-compose.yml"
    sed -E "s#(ts-umbrel-app:v?)[^@\" ]+(@sha256:[0-9a-f]{64})?#\1${VERSION#v}@${DIGEST}#" "${TARGET_COMPOSE}" > "${TARGET_COMPOSE}.tmp" && mv "${TARGET_COMPOSE}.tmp" "${TARGET_COMPOSE}"
    log_info "Monorepo docker-compose.yml pinned to ${VERSION#v}@${DIGEST}."

    TARGET_MANIFEST="${UMBREL_APPS_DIR}/tunnelsats/umbrel-app.yml"

    # Strip raw GitHub URLs for monorepo (assets are local)
    sed -E "s@https://raw.githubusercontent.com/Tunnelsats/ts-umbrel-app/(master|main)/tunnelsats/@@g" "${TARGET_MANIFEST}" > "${TARGET_MANIFEST}.tmp" && mv "${TARGET_MANIFEST}.tmp" "${TARGET_MANIFEST}"

    # Remove trailing period from tagline
    sed -E 's/^(tagline:.*)\.$/\1/' "${TARGET_MANIFEST}" > "${TARGET_MANIFEST}.tmp" && mv "${TARGET_MANIFEST}.tmp" "${TARGET_MANIFEST}"

    # Inject submitter and submission PR URL (if provided)
    if grep -qE "^submitter:" "${TARGET_MANIFEST}"; then
        log_info "submitter/submission already present, skipping injection."
    elif [ -n "${SUBMISSION_URL:-}" ]; then
        sed -E "s@^(website:.*)@\1\nsubmitter: Tunnelsats\nsubmission: ${SUBMISSION_URL}@" "${TARGET_MANIFEST}" > "${TARGET_MANIFEST}.tmp" && mv "${TARGET_MANIFEST}.tmp" "${TARGET_MANIFEST}"
    fi

    # Clear releaseNotes
    sed -e '/^releaseNotes:/,/^developer:/ { /^releaseNotes:/! { /^developer:/! d } }' \
        -e 's/^releaseNotes:.*/releaseNotes: ""/' "${TARGET_MANIFEST}" > "${TARGET_MANIFEST}.tmp" && mv "${TARGET_MANIFEST}.tmp" "${TARGET_MANIFEST}"

    # Clear icon and gallery for monorepo submission
    sed -e 's/^icon:.*/icon: ""/' \
        -e '/^gallery:/,/^path:/ { /^gallery:/! { /^path:/! d } }' \
        -e 's/^gallery:.*/gallery: []/' "${TARGET_MANIFEST}" > "${TARGET_MANIFEST}.tmp" && mv "${TARGET_MANIFEST}.tmp" "${TARGET_MANIFEST}"

    log_info "Promotion complete. Commit changes in ${UMBREL_APPS_DIR}."
}

if [ "$#" -lt 1 ]; then usage; fi

case "${1}" in
    node) run_node ;;
    monorepo) run_monorepo ;;
    vendor) shift; run_vendor "$@" ;;
    version) shift; run_version "$@" ;;
    promote) run_promote ;;
    *) usage ;;
esac
