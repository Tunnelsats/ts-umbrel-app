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
- **Changelog & Release Notes**: `CHANGELOG.md` tracks human-readable version history. Manifest `releaseNotes:` in `umbrel-app.yml` is populated dynamically during version bumps.

---

## Release & Versioning Workflow (Step-by-Step Sequence)

To ensure 100% version parity, prevent silent CI release suppression, and maintain clean release notes, follow this exact sequence when preparing a release:

### 1. Feature / Hotfix Development & Testing
Make your code changes on a feature/fix branch and run local test suites:
```bash
# Python Backend & Version Sync Guardrails
pytest server/tests/

# Frontend Web UI Tests
cd web && npm test
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

### 5. Multi-Repo Release Automation (`promote`)
We utilize an automated release promotion workflow to maintain total parity between our local repository and the official `umbrel-apps` GitHub fork.

When a new version is merged to `master`:
```bash
SUBMISSION_URL="https://github.com/getumbrel/umbrel-apps/pull/<PR_NUMBER>" npm run promote
```
> [!IMPORTANT]
> The `SUBMISSION_URL` environment variable is required for production promotions to ensure proper provenance and metadata in the app store. Without it, the promotion script will exit with an error.

#### Previewing Changes (Dry-Run)
You can run a dry-run to preview the files and changes that would be generated without writing anything to the actual monorepo target. In dry-run mode, `SUBMISSION_URL` is optional and will default to a placeholder (`https://github.com/getumbrel/umbrel-apps/pull/CHANGE_ME`) if unset:
```bash
# Via npm script
npm run promote -- --dry-run

# Or run the script directly
./scripts/sync.sh promote --dry-run
```

**The `promote` automation executes the following sequence:**
- **Validation**: Enforces that `SUBMISSION_URL` is provided (or defaults to a placeholder in `--dry-run` mode).
- **Discovery**: Extracts the version from `umbrel-app.yml`.
- **SHA256 Pinning**: Polls Docker Hub to fetch the official multi-arch digest index and pins it directly into `tunnelsats/docker-compose.yml`, ensuring production immutability.
- **Monorepo Synchronization**: Recursively forces synchronization (rsync) of the local `tunnelsats/` folder into the target `umbrel-apps` structure.
- **Metadata Injection**: Independently checks for and injects the `submitter: Tunnelsats` and `submission: <SUBMISSION_URL>` metadata fields into `umbrel-app.yml`.
- **Hybrid Stripping**: Surgically strips our development absolute GitHub URLs (icons, gallery) from the target `umbrel-app.yml` to maintain Umbrel CDN-first submission protocol compliance.

> [!TIP]
> **Pre-Push Hook**: A Git pre-push hook intercepts pushes to `master` and prompts the developer to execute this promotion layer automatically before changes are pushed upstream. Since promotion requires `SUBMISSION_URL`, ensure it is set in your environment if you choose to trigger promotion during `git push` (e.g., `SUBMISSION_URL="https://github.com/..." git push`).

## Important Files

- `scripts/test.sh persistence`: Verifies that configuration data survives Umbrel 1.x uninstallation.
- `scripts/diagnose.sh`: Developer convenience wrapper for the bundled troubleshooting suite.
- `tunnelsats/scripts/verify.sh dataplane`: Automated health check for local/remote installations (must be executed with `sudo`).
- `umbrel-app.yml`: Main Umbrel app manifest (located in `tunnelsats/`).

> [!IMPORTANT]
> **Data Persistence**: TunnelSats maps its data volume to a peer directory (`../tunnelsats-data`) on Umbrel to prevent data loss when the app is uninstalled via the App Manager. Do not change this mapping without consulting the persistence documentation.
