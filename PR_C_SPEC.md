# PR C: Install Config (Phase 3b) API Contract

> **Purpose**: This PR implements the "Install Config" feature, allowing users to configure or restore their LND/CLN node networking to use the WireGuard tunnel.
> **Assignment**: @blckbx 
> **Dependencies**: Branch ONLY from `master`. Do not touch `index.html` Dataplane cards (that's PR A).

## Scope
1. Implement Node selection UI logic.
2. Hook "Configure Node" UI button to `POST /api/local/configure-node`.
3. Hook "Restore Node" UI button to `POST /api/local/restore-node`.
4. Add JS unit tests for these interactions.

---

## API Contract (UI → Backend)

### `POST /api/local/configure-node`
This endpoint tells the backend to modify `lnd.conf` or CLN config to utilize the VPN via port forwarding, using the target implementation specified by the user.

**Request Body (JSON)**:
```json
{
  "nodeType": "lnd"  // or "cln"
}
```

**Response (Success - 200 OK)**:
```json
{
  "success": true,
  "lnd": true,
  "cln": false,
  "port": 35825,
  "dns": "de2.tunnelsats.com"
}
```

**Response (Error - 400/500)**:
```json
{
  "success": false,
  "error": "Failed to modify LND config or CLN is not currently supported."
}
```

### `POST /api/local/restore-node` (Already in Master)
This endpoint disables the VPN modifications in the node configurations, safe-guarding the node's clearnet connectivity if the VPN is removed.

**Request Body**: `None`

**Response (Success - 200 OK)**:
```json
{
  "lnd": true,
  "cln": true,
  "lnd_changed": true,
  "cln_changed": false
}
```

---

## Developer Notes
- `POST /api/local/restore-node` is already implemented and working in backend.
- `POST /api/local/configure-node` is stubbed in `server/app.py` returning `501 Not Implemented`. You need to replace this stub with the logic to read `tunnelsats-meta.json` for `vpnPort` and `serverDomain`, open `/lightning-data/lnd/tunnelsats.conf`, and inject `externalhosts=<domain>:<port>`. 
- Ensure you write Python and JS unit tests before marking ready for review.
