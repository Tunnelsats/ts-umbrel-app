# TunnelSats Developer Guide (Umbrel 1.x)

This document explains the repository structure and workflow for the TunnelSats Umbrel application.

## Directory Structure

| Path | Purpose |
| :--- | :--- |
| `/` (Root) | Primary development workspace and source code (Server, Web, Scripts). |
| `tunnelsats/` | **Staging Area** for Umbrel Metadata (Manifests, Icons, Gallery). |
| `scripts/` | Tooling for vendor sync, node diagnostics, and persistence testing. |
| `umbrel-apps/tunnelsats/` | **External Monorepo Target** for official submissions. |

## Single Source of Truth

*   **Docker Compose**: The canonical `docker-compose.yml` is located in `tunnelsats/docker-compose.yml`.
*   **Root Convenience Link**: The root `docker-compose.yml` is a symlink to `tunnelsats/docker-compose.yml` for local tooling compatibility.

## Synchronization Workflow

### 1. Verification (Local/Remote)
Always verify your changes on a live Umbrel node before submitting to the monorepo:
```bash
# Sync local dev to Umbrel node and restart
dev@env:~/ts-umbrel-app$ rsync -av --delete tunnelsats/ umbrel@umbrel.local:~/umbrel/app-data/tunnelsats/
```

### 2. Multi-Repo Release Automation (`promote`)
We utilize an automated release promotion workflow to maintain total parity between our local repository and the official `umbrel-apps` GitHub fork.

When a new version is ready:
1. Ensure `tunnelsats/umbrel-app.yml` contains the correct new `version: "x.y.z"`.
2. Ensure the Docker image is built and pushed to Docker Hub (`tunnelsats/ts-umbrel-app:vX.Y.Z`).
3. Run the automation by specifying the `SUBMISSION_URL` environment variable:
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
