# TunnelSats Developer Guide (Umbrel 1.x)

This document explains the repository structure and workflow for the TunnelSats Umbrel application.

## Directory Structure

| Path | Purpose |
| :--- | :--- |
| `/` (Root) | Primary development workspace and source code (Server, Web, Scripts). |
| `tunnelsats/` | **Staging Area** for Umbrel Metadata (Manifests, Icons, Gallery). |
| `scripts/` | Tooling for verification, persistence testing, and synchronization. |
| `umbrel-apps/tunnelsats/` | **External Monorepo Target** for official submissions. |

## Single Source of Truth

*   **Docker Compose**: The canonical `docker-compose.yml` is located in the **ROOT** of the repository. 
*   **Symlink**: To maintain Umbrel's directory structure requirements, `tunnelsats/docker-compose.yml` is a **Symbolic Link** to the root file. **Editing either file updates both.**

## Synchronization Workflow

### 1. Verification (Local/Remote)
Always verify your changes on a live Umbrel node before submitting to the monorepo:
```bash
# Sync local dev to Umbrel node and restart
umbrel@umbrel:~/umbrel/app-data/tunnelsats$ rsync -av --delete tunnelsats/ umbrel@umbrel.local:~/umbrel/app-data/tunnelsats/
```

### 2. Monorepo Sync
Once validated, use the sync script to mirror the staging state to the official community monorepo:
```bash
./scripts/sync-monorepo.sh
```

## Important Files

- `scripts/test-persistence.sh`: Verifies that configuration data survives Umbrel 1.x uninstallation.
- `scripts/verify_install.sh`: Automated health check for local/remote installations.
- `umbrel-app.yml`: Main Umbrel app manifest (located in `tunnelsats/`).

> [!IMPORTANT]
> **Data Persistence**: TunnelSats maps its data volume to a peer directory (`../tunnelsats-data`) on Umbrel to prevent data loss when the app is uninstalled via the App Manager. Do not change this mapping without consulting the persistence documentation.
