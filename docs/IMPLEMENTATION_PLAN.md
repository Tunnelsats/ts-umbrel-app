# Issue #126 implementation plan: remove stale real-IP announcements

## Decision

Treat Lightning gossip announcements as part of the protected state, independently
from packet routing. TunnelSats must disable automatic or explicit clearnet
announcement settings, withdraw stale LND gossip addresses when it has direct
container access, and fail closed whenever the remaining state cannot be verified.

## Security invariants

- Never display `Protected` while an active LND `externalip`/`nat=true` or CLN
  non-TunnelSats `announce-addr`/`ip-discovery=true` conflict is present.
- Preserve explicitly retained Tor `.onion` announcements; remove other non-
  TunnelSats public announcements.
- Require an explicit privacy confirmation before changing user-owned address
  discovery or announcement settings.
- Save the original active settings only once in `backupConfig` and restore only
  that saved set. Repeated configuration and restore operations remain idempotent.
- In direct mode, do not mark LND gossip verified until `lncli getinfo` has been
  queried after restart, every conflicting live URI has been withdrawn with
  `peers updatenodeannouncement`, and a second query is clean.
- In secure or k3s modes where authenticated LND RPC is unavailable, keep the
  aggregate status in a warning state instead of trusting a manual confirmation.
- Explain that withdrawing an address updates current gossip but cannot erase
  historical snapshots held by third parties.

## Implementation

1. Add shared configuration parsing and atomic transformation helpers in
   `server/app.py`.
   - Audit active LND `externalip`, `externalhosts`, and `nat` values.
   - Audit active CLN `announce-addr` and `ip-discovery` values.
   - Classify TunnelSats and `.onion` endpoints separately from conflicting
     clearnet endpoints.
2. Harden direct-mode configuration.
   - Return a conflict response until the caller confirms address changes.
   - Persist original conflicting settings under `backupConfig.lnd` or
     `backupConfig.cln` before editing the node config.
   - Disable conflicting announcements/discovery, retain requested Tor entries,
     and install only the intended TunnelSats announcement.
   - Restart the selected node after the atomic config update.
3. Purge and verify LND gossip in direct mode.
   - Execute authenticated `lncli getinfo` inside the detected LND container.
   - Remove each live non-TunnelSats, non-retained-Tor URI with
     `lncli peers updatenodeannouncement --address_remove=...`.
   - Query again and persist an endpoint-bound verification result. Return an
     error and leave protection fail-closed if inspection, removal, or
     verification fails.
4. Make restore deliberate and idempotent.
   - Disable TunnelSats-managed announcement lines.
   - Restore exactly the settings captured in `backupConfig` and clear only the
     successfully restored backup entry.
   - Restart only detected node implementations whose config was processed.
5. Improve secure-mode guidance.
   - Tell LND users to remove/comment `externalip`, set `nat=false`, restart,
     withdraw each old address with the copyable Umbrel `docker exec lnd lncli`
     command, and verify live URIs.
   - Tell CLN users to remove non-TunnelSats `announce-addr` values and disable
     `ip-discovery` before restart.
   - Include the historical-gossip retention warning in setup and restore UI.
6. Gate local status.
   - Audit readable node configs on every `/api/local/status` request.
   - Override aggregate `rules_synced` and `last_error` when a config conflict
     exists, direct-mode LND gossip has not been verified for the current
     TunnelSats endpoint, or the deployment mode cannot perform that verification.
   - Return structured announcement-conflict and verification fields for UI and
     diagnostics while preserving existing dataplane fields.
7. Add backend and frontend regression coverage for multiple values, `nat=true`
   and `nat=1`, CLN discovery, confirmation, backup/restore, retained Tor,
   already-advertised real addresses, RPC failure, secure-mode instructions, and
   protected-status suppression.

## Review gates

1. Local review/fix cycle: inspect the complete branch diff for privacy leaks,
   backup corruption, partial failure behavior, restore safety, parsing gaps,
   command injection, status fail-open behavior, and missing tests; fix all
   findings and rerun the full relevant test suite until clean.
2. Greptile review/fix cycle: push the branch, open a pull request, request a
   Greptile review, resolve every actionable finding, and repeat review/checks
   until Greptile reports no findings.
