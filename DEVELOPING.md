# TunnelSats Developer Guide (umbrelOS)

This document explains the repository structure, dual-store architecture, and release workflow for the TunnelSats Umbrel application.

## Directory Structure

| Path | Purpose |
| :--- | :--- |
| `/` (Root) | Primary development workspace and source code (`server/`, `web/`, `scripts/`, `k3s/`). |
| `tunnelsats-community/` | **Canonical Community Store Package** (`id: tunnelsats-community`, manifests, icons, gallery, `scripts/verify.sh`) and source of truth for Official Store promotion. |
| `tunnelsats/` | **Temporary Conflict-Guard Stub** (`id: tunnelsats`). Kept only until Official Store `v3.6.3` merges into `getumbrel/umbrel-apps` (see [Temporary Conflict-Guard Stub](#temporary-tunnelsats-conflict-guard-stub)). |
| `scripts/` | Tooling for node hot-sync, official store promotion, version bumping, diagnostics, and persistence testing. |
| `../umbrel-apps/tunnelsats/` | **External Monorepo Target** for Official Umbrel App Store submissions (`getumbrel/umbrel-apps`). |

---

## Dual-Store Architecture (`tunnelsats-community` vs Official `tunnelsats`)

TunnelSats is distributed through two Umbrel app stores using the same multi-arch Docker image (`tunnelsats/ts-umbrel-app:<version>`), configured via separate manifests and compose files:

| Property | Community App Store (`tunnelsats-community/`) | Official Umbrel App Store (`getumbrel/umbrel-apps/tunnelsats/`) |
| :--- | :--- | :--- |
| **App ID (`id`)** | `tunnelsats-community` | `tunnelsats` |
| **Display Name (`name`)** | `TunnelSats (Community)` | `TunnelSats` |
| **Manifest UI Port (`port`)** | `9741` | `9739` |
| **Internal Web Port (`APP_PORT`)** | `9740` | `9740` |
| **Proxy Target (`APP_HOST`)** | `tunnelsats-community_tunnelsats-web_1` | `tunnelsats_tunnelsats-web_1` |
| **Container Names** | Omitted (Umbrel injects `tunnelsats-community_<service>_1`) | Omitted (Umbrel injects `tunnelsats_<service>_1`) |
| **Dataplane Mode (`SECURE_MODE`)** | `false` (Automatic/Standard Mode; Docker socket RO, LND/CLN RW) | `true` (Sandboxed Secure Mode; no Docker socket, LND/CLN RO) |
| **Data Volume** | `${APP_DATA_DIR}/../tunnelsats-data:/data` (persists across uninstall) | `${APP_DATA_DIR}/data:/data` (self-contained per-app storage) |
| **App ID Env (`UMBREL_APP_ID`)** | `tunnelsats-community` | Omitted (defaults to `tunnelsats`) |
| **Widget Endpoints** | `tunnelsats-web:9740/api/local/widgets/{status,overview}` | `tunnelsats-web:9740/api/local/widgets/{status,overview}` |

> [!NOTE]
> `umbrel-app-store.yml` keeps `id: tunnelsats` so existing Community Store URLs do not break. umbrelOS requires Community Store app IDs to start with `<store-id>` (`app.id.startsWith(meta.id)`), which `tunnelsats-community` satisfies cleanly.

### umbrelOS 2.x Runtime Rules & Invariants

Understanding three umbrelOS 2.x (`umbreld`) rules prevents store shadowing, dead UI routes, and cross-app proxy hijacking:

1. **`resolvedApps()` Duplicate-ID Shadowing (`app-store.ts`)**:
   - umbrelOS resolves duplicate app IDs using first-repository-wins order, where the Official Store (`umbrel-app-store`) is permanently index `0`.
   - When the same `id` exists in two stores, the backend resolves updates exclusively to the Official Store and the UI places the ID into `ambiguousAppIds`, hard-blocking both Install and Update with `"This app ID conflicts with another app store"`.
   - Giving the Community Store package its own `id: tunnelsats-community` eliminates store shadowing and catalog collisions permanently.
2. **`lan-ingress` Port-Conflict Drop Rule (`lan-ingress.ts`)**:
   - Store validation does not block two apps from declaring the same manifest `port`, but at runtime `removeConflictingAppRoutes()` silently drops the second running app's entire LAN ingress route and `ensureAppLoopbackServer()` fails to bind the same loopback port twice.
   - Community uses `port: 9741` while Official owns `port: 9739` so both packages can coexist in catalogs and installs without dropping LAN ingress. (Internal `APP_PORT: "9740"` binds per-container on the bridge network and does not conflict.)
3. **`container_name` Proxy-Hijack Trap (`app.ts`, `lan-ingress.ts`) — NEVER Re-Add Hardcoded `container_name`**:
   - umbrelOS `patchComposeFile()` only injects `container_name: ${appId}_${serviceName}_1` when `container_name` is omitted from `docker-compose.yml`.
   - At proxy time, `resolveAppTarget()` calls `docker.getContainer(host).inspect()` **before** filtering by `com.docker.compose.project=${appId}`. If Community hardcoded `container_name: tunnelsats-web`, an Official `tunnelsats` gateway resolving `tunnelsats-web` would connect directly to the Community container and proxy Official traffic into Community!
   - Both manifests therefore **omit `container_name`** and target their project-prefixed web container (`APP_HOST: tunnelsats-community_tunnelsats-web_1` vs `tunnelsats_tunnelsats-web_1`). Widget manifests keep the Compose service key (`tunnelsats-web:9740/...`), because `umbreld` resolves widget hosts per app via `compose.services['tunnelsats-web'].container_name`.

> [!WARNING]
> Although ID, port, and container separation prevents store and UI collisions, **only one active TunnelSats VPN daemon can control host networking at a time** (both share `tunnelsatsv2`, routing table `51820`, firewall rules, and Lightning node configuration).

### Promotion Mapping (`tunnelsats-community/` -> `getumbrel/umbrel-apps/tunnelsats/`)

`scripts/sync.sh promote` (and `npm run promote`) invokes `scripts/promote_official_store.py` to render the Official Store package directly from `tunnelsats-community/`:
- **Source**: `tunnelsats-community/umbrel-app.yml` + `tunnelsats-community/docker-compose.yml` (validates `id == "tunnelsats-community"`).
- **Target**: `${UMBREL_APPS_DIR}/tunnelsats/` with `id: tunnelsats`, `name: TunnelSats`, `port: 9739`, `APP_HOST: tunnelsats_tunnelsats-web_1`, `APP_PORT: "9740"`, `SECURE_MODE=true`, and `${APP_DATA_DIR}/data:/data`.
- **Sanitization**: Strips `UMBREL_APP_ID`, `container_name`, `/var/run/docker.sock`, `NET_RAW`, `apparmor:unconfined`, `migration_source`, and Community-only migration copy from the Official description, pins multi-arch `sha256` image digests, and injects `storage.dataRoot: data`.
- **Release Notes Guard**: Front-loads a concise 3-line notice in Official `releaseNotes` warning legacy Community Store users not to click Official Update (umbrelOS's Updates card displays the first 3 lines next to the Update button). For future Official promotions after the transition window, pass `--no-community-notice` to `scripts/promote_official_store.py` to omit this front-loaded notice.

### Temporary `tunnelsats/` Conflict-Guard Stub

This repository temporarily retains a minimal `tunnelsats/` conflict-guard stub alongside `tunnelsats-community/` until Official Store **`v3.6.3`** (which ships both the 3-line `releaseNotes` warning and the in-app cross-grade recovery banner) is merged into `getumbrel/umbrel-apps`:
- **Why it stays temporarily**: umbrelOS checks `availableVersion !== installedVersion` (not semver `>`). Removing `tunnelsats/` from `Tunnelsats/ts-umbrel-app` while Official Store is still on `3.6.0` would immediately clear `ambiguousAppIds` and show an **Update** badge to Community `3.6.2`/`3.6.1`/`3.5.x` users—silently cross-grading and downgrading them to Official `3.6.0` (`SECURE_MODE=true`, empty `/data`, no recovery banner). Keeping `tunnelsats/` in the Community repo intentionally keeps legacy `tunnelsats` locked in `ambiguousAppIds` until Official `v3.6.3` is live.
- **Exact cleanup step once Official `v3.6.3` is merged into `getumbrel/umbrel-apps`**:
  1. Confirm `getumbrel/umbrel-apps` `master` serves `tunnelsats` version `3.6.3` (with the 3-line warning and in-app recovery banner).
  2. Delete the temporary stub directory (`git rm -r tunnelsats/`) and remove any stub-only `.gitignore` entry or stub-preservation test assertions in `ts-umbrel-app`.
  3. Run `pytest server/tests/`, open a cleanup PR, and merge to `master`.

---

## Single Source of Truth & Version Control

- **Canonical Version Locations**: App versioning is maintained across 5 synchronized locations:
  1. `tunnelsats-community/umbrel-app.yml` (`version: "X.Y.Z"`)
  2. `tunnelsats-community/docker-compose.yml` (`image: tunnelsats/ts-umbrel-app:X.Y.Z`)
  3. `server/app.py` (`APP_VERSION = "vX.Y.Z"`)
  4. `web/index.html` (`id="app-version">vX.Y.Z</span>`)
  5. `k3s/deployment.yaml` (`image: tunnelsats/ts-umbrel-app:X.Y.Z`)
- **Docker Compose**: The canonical `docker-compose.yml` is located in `tunnelsats-community/docker-compose.yml`. The root `docker-compose.yml` is a symlink to `tunnelsats-community/docker-compose.yml` for local tooling and CI compatibility.
- **Umbrel Runtime Split**: `tunnelsats-web` is the non-root bridge-networked
  Flask service targeted by `app_proxy`; `tunnelsats-daemon` is the only
  host-networked/capability-bearing service. They communicate through
  `${APP_DATA_DIR}/runtime/daemon.sock`. The k3s manifest continues to use the
  backward-compatible `combined` role.
- **Changelog & Release Notes**: `CHANGELOG.md` tracks human-readable version history. Manifest `releaseNotes:` in `tunnelsats-community/umbrel-app.yml` is populated dynamically during version bumps.

---

## Local Development & Hot-Syncing (`sync.sh node`)

To test code changes directly on a physical or virtual Umbrel node without building Docker images:

### 1. Configure Credentials (`.env.local`)
Create `.env.local` in the root of the repository (**never commit passwords or credentials — `.env.local` is gitignored**):
```env
UMBREL_HOST=umbrel.local
UMBREL_PASSWORD=your_umbrel_password
```

### 2. Hot-Patch Code to Node
Run the hot-sync tool to stage your local `server/`, `web/`, and `scripts/` changes directly to the running Umbrel containers:
```bash
# Hot-sync Community edition (tunnelsats-community, Standard Mode):
./scripts/sync.sh node

# Hot-sync Official edition (tunnelsats, Secure Mode):
APP_ID=tunnelsats SECURE_MODE=true ./scripts/sync.sh node
```
This automatically mounts local source files into the target app's `tunnelsats-web` and `tunnelsats-daemon` containers and restarts them gracefully.

---

## Release & Versioning Workflow (Step-by-Step Sequence)

To ensure 100% version parity, prevent silent CI release suppression, and maintain clean release notes, follow this exact sequence when preparing a release:

### 1. Feature / Hotfix Development & Testing
Make your code changes on a feature/fix branch and run local test suites:
```bash
# Python Backend & Version Sync Guardrails
pytest server/tests/

# Frontend Web UI Tests (compile Tailwind CSS at repo root first)
npm ci && npm run build:css
cd web && npm ci && npm test

# Exact official-store manifest rules (from a sibling umbrel-apps checkout)
node ../umbrel-apps/.tools/lint-apps.mjs tunnelsats \
  --root ../umbrel-apps
```

### 2. Atomic Version Bump & Release Notes Population
**Never edit version strings manually across the 5 files.** Use the transactional CLI bumper:
```bash
python3 scripts/bump_version.py <X.Y.Z> --notes "<Single-line summary of changes>"
```
*Example:*
```bash
python3 scripts/bump_version.py 3.6.3 --notes "Introduce tunnelsats-community app ID and cross-grade recovery banner"
```

This single command atomically:
1. Updates the version string across all 5 canonical files.
2. Dynamically populates `releaseNotes:` in `tunnelsats-community/umbrel-app.yml`.
3. Appends or updates the version entry in `CHANGELOG.md`.
4. Performs staged writes with automatic rollback if any file update fails.

### 3. Verify Version Parity
Run the automated version sync guardrail test to guarantee 100% parity:
```bash
pytest server/tests/test_version_sync.py
```

### 4. Commit, PR, & CI Release Automation
Commit the synchronized files and open a PR to `master`:
```bash
git add -A
git commit -m "chore(release): bump version to v3.6.3"
git push origin <feature-branch>
```
Upon merging to `master`:
- GitHub Actions automatically verifies version parity via `test_version_sync.py`.
- If `version` is new, CI builds `tunnelsats/ts-umbrel-app:X.Y.Z` on Docker Hub and creates GitHub Release `vX.Y.Z`.

Dependabot PRs:
- Minor and patch updates for npm (root and `web/`) and GitHub Actions are approved and auto-merged by `.github/workflows/dependabot-auto-merge.yml` once the required checks pass. They are exempt from the TunnelSentinel review: the workflow marks the required `TunnelSentinel / Code Review` check as "Not reviewed".
- These merges do not build or publish images. Users only receive dependency updates with the next versioned release.
- Major updates, Docker base image updates and Dependabot PRs with commits from anyone else still need a TunnelSentinel review.
- The workflow runs on `pull_request`, not `pull_request_target`: from November 2, 2026 GitHub blocks `pull_request_target` in public repositories unless an Actions event policy allows it. On `pull_request`, Dependabot runs get a `GITHUB_TOKEN` that is read-only except for the scopes in the workflow's `permissions:` block, and only Dependabot secrets. `server/tests/test_github_workflows.py` fails if any workflow uses `pull_request_target`.
- An open Dependabot PR gets no new run of the workflow until Dependabot pushes to it again (for example a PR opened before the workflow changed). Comment `@dependabot rebase` on it to trigger one.

### 5. Multi-Repo Release Automation (`promote`)
We utilize an automated release promotion workflow (`scripts/sync.sh promote` -> `scripts/promote_official_store.py`) to maintain total parity between our canonical `tunnelsats-community/` package and the official `getumbrel/umbrel-apps/tunnelsats/` package (staged via the personal fork `TrezorHannes/umbrel-apps`).

When a new version is merged to `master` and its multi-arch Docker image is published on Docker Hub:
```bash
# Uses default SUBMISSION_URL (https://github.com/getumbrel/umbrel-apps/pull/4919)
# and defaults UMBREL_APPS_DIR to ../umbrel-apps (your TrezorHannes/umbrel-apps checkout)
npm run promote

# Override target checkout or submission PR URL if needed:
UMBREL_APPS_DIR=../umbrel-apps SUBMISSION_URL="https://github.com/getumbrel/umbrel-apps/pull/4919" npm run promote
```
> [!NOTE]
> `SUBMISSION_URL` defaults to `https://github.com/getumbrel/umbrel-apps/pull/4919` (the original TunnelSats submission PR) and remains overridable via environment variable. After running `npm run promote`, commit the updated `tunnelsats/` files on a fresh branch in `TrezorHannes/umbrel-apps`, run `node .tools/lint-apps.mjs tunnelsats --root .`, and open an update PR against `getumbrel/umbrel-apps`.

#### Previewing Changes (Dry-Run)
You can run a dry-run to preview the unified diff against your local `umbrel-apps` checkout without writing anything to the target repository:
```bash
# Via npm script
npm run promote -- --dry-run

# Or run the script directly
./scripts/sync.sh promote --dry-run
```

**The `promote` automation executes the following sequence:**
- **Discovery & Digest Lookup**: Extracts the version from `tunnelsats-community/umbrel-app.yml` and fetches the published multi-arch `sha256` manifest index digest from Docker Hub (`docker buildx imagetools inspect` with Docker Hub v2 API fallback). Aborts without touching the target repo if lookup fails.
- **Structural Validation & Staging (`scripts/promote_official_store.py`)**: Parses source `tunnelsats-community/docker-compose.yml` and `tunnelsats-community/umbrel-app.yml` with PyYAML, fails closed on unexpected structural drift, renders Official Store manifests (`id: tunnelsats`, `name: TunnelSats`, `port: 9739`) into a temporary staging directory, and verifies both generated files round-trip cleanly with `yaml.safe_load` before syncing.
- **Manifest Parity (`umbrel-app.yml`)**: Maps `id: tunnelsats-community` -> `id: tunnelsats`, `name: TunnelSats (Community)` -> `name: TunnelSats`, `port: 9741` -> `port: 9739`, injects `storage.dataRoot: data`, preserves user-facing `releaseNotes` (with the front-loaded 3-line Community-store guard notice by default; opt out via `--no-community-notice` on `scripts/promote_official_store.py` once the transition window closes), strips Community Store `icon:` and Community-only migration copy, preserves existing official local `gallery:` entries from `${UMBREL_APPS_DIR}/tunnelsats/umbrel-app.yml` (falling back to `["1.webp", "2.webp", "3.webp", "4.webp"]`), preserves `widgets` verbatim, and places `submitter: Tunnelsats` and `submission: <SUBMISSION_URL>` at the bottom.
- **Two-Service Official Hardening (`docker-compose.yml`)**: Removes top-level `version:`, sets `app_proxy` to `APP_HOST: tunnelsats_tunnelsats-web_1` without `PROXY_AUTH_ADD`, digest-pins both service images, strips `UMBREL_APP_ID`, `container_name`, `NET_RAW`, `apparmor:unconfined`, `migration_source`, and `docker.sock`, hard-pins `SECURE_MODE=true`, rewrites `/data` to `${APP_DATA_DIR}/data:/data`, mounts `lnd` and `cln` read-only, and writes 0-byte `data/.gitkeep` and `runtime/.gitkeep`.
- **Atomic Target Sync**: Only after validation succeeds does `sync.sh` run `rsync -av --delete` from the staging directory into `${UMBREL_APPS_DIR}/tunnelsats/`.

> [!TIP]
> **Pre-Push Hook**: A Git pre-push hook intercepts pushes to `master` and prompts the developer to execute `npm run promote` before changes are pushed upstream. Because `SUBMISSION_URL` now defaults to `https://github.com/getumbrel/umbrel-apps/pull/4919`, no extra environment variable is required unless overriding `UMBREL_APPS_DIR` or `SUBMISSION_URL`.

## Important Files

- `scripts/test.sh persistence`: Verifies that configuration data in `app-data/tunnelsats-data` survives Community Store uninstallation.
- `scripts/diagnose.sh`: Developer convenience wrapper for the bundled troubleshooting suite.
- `tunnelsats-community/scripts/verify.sh dataplane`: Automated health check for local/remote installations (must be executed with `sudo`).
- `tunnelsats-community/umbrel-app.yml`: Canonical Community Store Umbrel app manifest.

> [!IMPORTANT]
> **Data Persistence**: The Community Store edition (`tunnelsats-community`) maps its data volume to a sibling directory (`${APP_DATA_DIR}/../tunnelsats-data:/data`) so configuration survives uninstallation and legacy `tunnelsats` -> `tunnelsats-community` migration. The Official Store edition (`tunnelsats`) uses Umbrel-managed per-app storage (`${APP_DATA_DIR}/data:/data`). Do not change either mapping without consulting the dual-store persistence rules above.
