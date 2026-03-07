# PR B: Import Config (Phase 3a) API Contract

> **Purpose**: This PR implements the "Import Config" feature, allowing users to paste a WireGuard config string, which is then parsed and saved by the backend. 
> **Assignment**: @blckbx 
> **Dependencies**: Branch ONLY from `master`. Do not touch `index.html` Dataplane cards (that's PR A).

## Scope
1. Build a "Paste Config" `<textarea>` and an "Import" button in the UI.
2. Add client-side pre-validation logic.
3. Hook the UI to `POST /api/local/upload-config`.
4. Display success/error results locally.

---

## API Contract (UI → Backend)

### `POST /api/local/upload-config`

**Request Body (JSON)**:
```json
{
  "config": "[Interface]\nPrivateKey = ...\n\n[Peer]\nPublicKey = ...\nAllowedIPs = 0.0.0.0/0\nEndpoint = de2.tunnelsats.com:51820"
}
```

**Response (Success - 200 OK)**:
```json
{
  "success": true,
  "message": "Configuration saved and parsed.",
  "meta": {
    "serverId": "de2",
    "wgPublicKey": "...",
    "expiresAt": "2026-04-05T10:30:00.000Z",
    "vpnPort": 35825
  }
}
```

**Response (Error - 400 Bad Request)**:
```json
{
  "success": false,
  "error": "Invalid WireGuard configuration format. Missing [Interface] or [Peer] block."
}
```

---

## Developer Notes
- The backend endpoint `POST /api/local/upload-config` is already stubbed in `server/app.py` returning `501 Not Implemented`. You need to replace the stub with the actual implementation (parsing the text, validating, deriving the public key via `wg pubkey`, and saving to `/data/tunnelsats.conf` and `/data/tunnelsats-meta.json`).
- Ensure you write Python and JS unit tests.
