# TunnelSats Developer Guide (Umbrel 1.x)

This document explains the repository structure and workflow for the TunnelSats Umbrel application.

## Directory Structure

| Path | Purpose |
| :--- | :--- |
| `/` (Root) | Primary development workspace and source code (Server, Web, Scripts). |
| `tunnelsats/` | **Staging Area** for Umbrel Metadata (Manifests, Icons, Gallery). |
| `scripts/` | Tooling for vendor sync, node diagnostics, and persistence testing. |
| `umbrel-apps/tunnelsats/` | **External Monorepo Target** for official submissions. |

## Single Source of Truth & Version Control

- **Canonical Version Locations**: App versioning is maintained across 5 synchronized locations:
  1. `tunnelsats/umbrel-app.yml` (`version: "X.Y.Z"`)
  2. `tunnelsats/docker-compose.yml` (`image: tunnelsats/ts-umbrel-app:X.Y.Z`)
  3. `server/app.py` (`APP_VERSION = "vX.Y.Z"`)
  4. `web/index.html` (`id="app-version">vX.Y.Z</span>`)
  5. `k3s/deployment.yaml` (`image: tunnelsats/ts-umbrel-app:X.Y.Z`)
- **Docker Compose**: The canonical `docker-compose.yml` is located in `tunnelsats/docker-compose.yml`. The root `docker-compose.yml` is a symlink to `tunnelsats/docker-compose.yml` for local tooling compatibility.
- **Umbrel Runtime Split**: `tunnelsats-web` is the non-root bridge-networked
  Flask service targeted by `app_proxy`; `tunnelsats-daemon` is the only
  host-networked/capability-bearing service. They communicate through
  `${APP_DATA_DIR}/runtime/daemon.sock`. The k3s manifest continues to use the
  backward-compatible `combined` role.
- **Changelog & Release Notes**: `CHANGELOG.md` tracks human-readable version history. Manifest `releaseNotes:` in `umbrel-app.yml` is populated dynamically during version bumps.

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
# Hot-sync in Standard Mode:
./scripts/sync.sh node

# Hot-sync in Secure Mode (Official Umbrel App Store mode):
SECURE_MODE=true ./scripts/sync.sh node
```
This automatically mounts local source files into `tunnelsats-web` and `tunnelsats-daemon` containers and restarts them gracefully.

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
python3 scripts/bump_version.py 3.3.4 --notes "Harden policy routing fallback rules and automate version sync guardrails"
```

This single command atomically:
1. Updates the version string across all 5 canonical files.
2. Dynamically populates `releaseNotes:` in `tunnelsats/umbrel-app.yml`.
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
git commit -m "chore(release): bump version to v3.3.4"
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
We utilize an automated release promotion workflow (`scripts/sync.sh promote` -> `scripts/promote_official_store.py`) to maintain total parity between our local repository and the official `getumbrel/umbrel-apps` package (staged via the personal fork `TrezorHannes/umbrel-apps`).

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
- **Discovery & Digest Lookup**: Extracts the version from `tunnelsats/umbrel-app.yml` and fetches the published multi-arch `sha256` manifest index digest from Docker Hub (`docker buildx imagetools inspect` with Docker Hub v2 API fallback). Aborts without touching the target repo if lookup fails.
- **Structural Validation & Staging (`scripts/promote_official_store.py`)**: Parses source `tunnelsats/docker-compose.yml` and `tunnelsats/umbrel-app.yml` with PyYAML, fails closed on unexpected structural drift, renders Official Store manifests into a temporary staging directory, and verifies both generated files round-trip cleanly with `yaml.safe_load` before syncing.
- **Manifest Parity (`umbrel-app.yml`)**: Injects `storage.dataRoot: data`, preserves user-facing `releaseNotes` for post-launch updates, strips Community Store `icon:`, preserves existing official local `gallery:` entries from `${UMBREL_APPS_DIR}/tunnelsats/umbrel-app.yml` (falling back to `["1.webp", "2.webp", "3.webp", "4.webp"]`), preserves `widgets` verbatim, and places `submitter: Tunnelsats` and `submission: <SUBMISSION_URL>` at the bottom.
- **Two-Service Official Hardening (`docker-compose.yml`)**: Removes top-level `version:`, sets `app_proxy` to `APP_HOST: tunnelsats_tunnelsats-web_1` without `PROXY_AUTH_ADD`, digest-pins both service images, strips `container_name`, `NET_RAW`, `apparmor:unconfined`, `migration_source`, and `docker.sock`, hard-pins `SECURE_MODE=true`, mounts `lnd` and `cln` read-only, and writes 0-byte `data/.gitkeep` and `runtime/.gitkeep`.
- **Atomic Target Sync**: Only after validation succeeds does `sync.sh` run `rsync -av --delete` from the staging directory into `${UMBREL_APPS_DIR}/tunnelsats/`.

> [!TIP]
> **Pre-Push Hook**: A Git pre-push hook intercepts pushes to `master` and prompts the developer to execute `npm run promote` before changes are pushed upstream. Because `SUBMISSION_URL` now defaults to `https://github.com/getumbrel/umbrel-apps/pull/4919`, no extra environment variable is required unless overriding `UMBREL_APPS_DIR` or `SUBMISSION_URL`.

## Important Files

- `scripts/test.sh persistence`: Verifies that configuration data survives Umbrel 1.x uninstallation.
- `scripts/diagnose.sh`: Developer convenience wrapper for the bundled troubleshooting suite.
- `tunnelsats/scripts/verify.sh dataplane`: Automated health check for local/remote installations (must be executed with `sudo`).
- `umbrel-app.yml`: Main Umbrel app manifest (located in `tunnelsats/`).

> [!IMPORTANT]
> **Data Persistence**: TunnelSats maps its data volume to a peer directory (`../tunnelsats-data`) on Umbrel to prevent data loss when the app is uninstalled via the App Manager. Do not change this mapping without consulting the persistence documentation.
