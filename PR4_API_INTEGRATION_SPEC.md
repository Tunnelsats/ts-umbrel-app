# PR #4: API Integration Spec

> **Purpose**: This document defines the work needed to bridge the UI layer (from PR #2)
> with the dataplane layer (from PR #3). It should be picked up after both PRs are merged.

## Scope

Wire the frontend dashboard to the new dataplane API endpoints introduced by PR #3.
This PR does **not** include the purchase flow completion (that's a separate effort).

---

## API Contract (PR #3 Provides → PR #4 Consumes)

### `GET /api/local/status` (extended fields from PR #3)

```json
{
  "wg_status": "Connected",
  "wg_pubkey": "abc123...",
  "configs_found": ["tunnelsats-us1.conf"],
  "version": "v3.0.0",
  "dataplane_mode": "docker-full-parity",
  "target_container": "lightning_lnd_1",
  "target_ip": "10.9.9.9",
  "target_impl": "lnd",
  "forwarding_port": "12345",
  "rules_synced": true,
  "last_reconcile_at": "2026-03-04T20:00:00Z",
  "last_error": null,
  "docker_network": {
    "name": "docker-tunnelsats",
    "subnet": "10.9.9.0/25",
    "bridge": "br-abc123"
  }
}
```

### `POST /api/local/reconcile` → returns `202`

```json
{
  "success": true,
  "accepted": true,
  "request_id": "uuid-here",
  "status_url": "/api/local/reconcile/uuid-here"
}
```

### `GET /api/local/reconcile/<request_id>` → poll for result

```json
{ "success": true, "complete": true|false, "changed": true|false, "state": { ... } }
```

### `POST /api/local/configure-node` (from PR #3)
### `POST /api/local/restore-node` (from PR #3)

---

## Frontend Changes Needed

### 1. Dashboard — Dataplane Status Cards

Add new cards to the dashboard view (`index.html`) showing:
- **Target Container**: name + implementation (LND/CLN)
- **Forwarding Port**: the VPN port being DNAT'd to 9735
- **Rules Synced**: green/red indicator
- **Last Reconcile**: timestamp
- **Last Error**: if non-null, show in warning style

### 2. Dashboard — Reconcile Button

Add a "Reconcile Now" button that:
1. `POST /api/local/reconcile`
2. Show spinner + "Reconciling..."
3. Poll `GET /api/local/reconcile/<request_id>` every 1s
4. On `complete: true`, update all status fields and show result

### 3. `fetchStatus()` Updates

Extend `fetchStatus()` in `app.js` to read and render the new dataplane fields.

### 4. Configure Node Button

Re-introduce the "Configure Lightning Node" button in the Import/Install flow:
- Call `POST /api/local/configure-node`
- Display result (LND/CLN success/failure)

### 5. Restore Node (Uninstall Tab)

Re-introduce the "Restore Node Networking" button:
- Call `POST /api/local/restore-node`
- Show result, then trigger restart

### 6. Status Badge Enhancement

Update the sidebar status badge to reflect `rules_synced`:
- Green "Protected" when `wg_status === "Connected"` AND `rules_synced === true`
- Yellow "Partial" when connected but rules not synced
- Red "Disconnected" when WG is down

---

## Test Updates

### Jest (`app.test.js`)
- Update mock fetch response to include all new dataplane fields
- Add test for `reconcileTunnel()` function
- Add test for status badge logic

### Pytest (`test_app.py`)
- Add test for `/api/local/reconcile` POST → 202
- Add test for `/api/local/reconcile/<id>` GET polling
- Add test for `/api/local/configure-node` POST
- Add test for `/api/local/restore-node` POST

---

## Out of Scope
- Purchase flow completion (servers → invoice → QR → poll → claim)
- E2E testing on real hardware (separate effort)
- nftables migration (future consideration)
