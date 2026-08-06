# Issue #143 implementation plan: split the Umbrel web service from the host daemon

## Goal

Make the promoted TunnelSats manifest pass the official Umbrel App Store
`app_proxy` linter without weakening dataplane isolation:

```text
app_proxy (Umbrel authentication, bridge network)
    -> tunnelsats-web (non-root Flask UI/API, bridge network)
    -> /run/tunnelsats/daemon.sock (private Unix domain socket)
    -> tunnelsats-daemon (root dataplane, host network)
```

`app_proxy` will target the Compose service name `tunnelsats-web`. Only
`tunnelsats-daemon` will retain host networking, Linux capabilities, the Docker
socket, Lightning configuration mounts, and responsibility for WireGuard,
iptables, routing, node restarts, and reconciliation.

## Design decisions and invariants

- Use one image with explicit `web`, `daemon`, and backward-compatible
  `combined` runtime roles. The Umbrel Compose file uses the split roles; the
  existing k3s deployment keeps the combined role unless it is deliberately
  migrated in a separate change.
- Use a Unix domain socket on a dedicated runtime volume. Do not open a daemon
  TCP port, publish a daemon port, or attach the daemon to the bridge network.
- Run the web process as a non-root UID/GID, drop all capabilities, enable
  `no-new-privileges`, and give it neither `/var/run/docker.sock` nor Lightning
  configuration mounts. Keep writable paths limited to the data/runtime paths
  that the web contract demonstrably requires; prefer daemon-mediated local
  persistence when practical.
- Treat the socket as a privilege boundary: restrictive ownership/mode, bounded
  request size, timeouts, strict JSON schemas, an explicit method allowlist, no
  caller-supplied shell commands or Docker paths, and sanitized errors.
- Preserve the existing external HTTP API and UI response contracts. If the
  daemon is unavailable, management operations fail closed with a stable `503`
  response; they must never report a successful restart, reconciliation, node
  configuration, or protected status.
- Trust forwarded browser headers only when the direct socket peer resolves to
  the configured `app_proxy` service. A random bridge peer with spoofed
  `X-Forwarded-*` headers must still be rejected. Preserve CSRF, Origin, Host,
  Fetch Metadata, CSP, and authenticated-proxy protections.
- Preserve config migration, Secure Mode behavior, Docker full-parity routing,
  k3s behavior, persistent metadata/config semantics, and all privacy
  fail-closed rules introduced by earlier fixes.

## TDD implementation sequence

### 1. RED: lock down the two-service manifest contract

Extend `server/tests/test_management_manifest.py` before changing Compose:

- Assert `APP_HOST: tunnelsats-web`, the existing internal web port, no empty
  `PROXY_AUTH_WHITELIST`, and no `network_mode: host` on the proxy target.
- Assert `tunnelsats-web` exists on the bridge network, runs non-root, exposes
  only the internal Flask port, drops capabilities, and has no Docker socket,
  host networking, Lightning mounts, or dataplane security options.
- Assert `tunnelsats-daemon` retains host networking and only the minimum current
  capabilities/mounts needed by the dataplane.
- Assert the web and daemon share only the named Unix-socket runtime volume (and
  any explicitly justified persistent-data volume), and that no daemon port is
  published.
- Add a regression fixture matching the official linter rules: `APP_HOST` must
  name a service in the generated Compose model and that service must not use
  host networking.

Expected RED result: the current monolithic manifest fails these assertions.

### 2. RED: define and secure the daemon IPC contract

Add focused tests for a daemon server/client boundary using a temporary Unix
socket:

- Successful round trips for the finite management operations required by the
  Flask routes (status, config/meta persistence and export as needed, restart,
  reconcile start/status, configure node, and restore node).
- Stable mapping of validation failures and daemon errors to HTTP status codes;
  unavailable, timed-out, malformed, truncated, oversized, or mismatched
  responses fail closed.
- Unknown methods, extra privileged arguments, invalid request IDs, arbitrary
  commands/paths, and concurrent request corruption are rejected.
- Socket creation is atomic, stale sockets are handled safely, permissions are
  restrictive, and shutdown removes only the owned socket.
- Sensitive WireGuard material and internal exception details are not logged or
  returned outside endpoints that intentionally export authenticated config.

Expected RED result: no daemon IPC implementation exists.

### 3. GREEN: extract the privileged runtime and add role dispatch

- Extract privileged/local-host operations from Flask route functions behind a
  small runtime interface. Keep validation and return values explicit so both
  direct and IPC adapters share one contract instead of duplicating behavior.
- Implement the daemon-side allowlisted Unix-socket server and the web-side
  client. Keep shell/Docker/WireGuard arguments constructed exclusively by the
  daemon implementation.
- Update `scripts/entrypoint.sh` to dispatch roles:
  - `web`: start only Flask with the IPC client;
  - `daemon`: initialize migration/dataplane state, serve the private socket,
    and run reconciliation without starting Flask;
  - `combined`: retain current k3s/local compatibility.
- Make startup, signal handling, stale-socket cleanup, and daemon-unavailable
  behavior deterministic. Do not let web startup or health imply dataplane
  readiness.

Run the IPC tests and the existing backend/entrypoint suites until GREEN.

### 4. RED/GREEN: preserve every Flask management contract through IPC

Before rewiring each route group, add route tests with a fake runtime plus at
least one real temporary-socket integration test:

- `/api/local/status` returns the existing schema and suppresses protected state
  when the daemon cannot verify it.
- upload/export, subscription claim/renew persistence, restart, reconcile
  polling, configure-node, restore-node, Docker/LND/CLN discovery, and gossip
  verification retain their existing status codes and side effects.
- Browser security accepts the exact bridge-network `app_proxy` peer while
  rejecting untrusted peers, spoofed forwarding headers, invalid Host/Origin,
  missing CSRF tokens, and cross-site mutations.
- Public subscription/server proxy routes and static UI delivery remain usable
  without daemon privileges where their behavior does not require the daemon.

Then route all host-sensitive work through the runtime interface. Remove direct
Docker, WireGuard, routing, Lightning-config, and trigger-file access from the
web role. Add source/manifest regression assertions so those privileges cannot
silently return to `tunnelsats-web`.

### 5. RED/GREEN: wire Compose, CI, hot-patching, and promotion

- Replace the monolithic `tunnelsats` block in
  `tunnelsats/docker-compose.yml` with `tunnelsats-web` and
  `tunnelsats-daemon`, configure the shared socket volume, and point
  `app_proxy` to the web service.
- Update `docker-compose.test.yml`, `docker-compose.ci.yml`, and the container
  smoke test to start both roles, wait for the web endpoint, exercise a daemon
  IPC-backed operation, and inspect both services on failure.
- Extend `server/tests/test_sync_script.py` first, then update
  `scripts/sync.sh node` to patch/restart the correct web and daemon containers.
  Preserve umbreld ownership of the injected `app_proxy` model.
- Add promotion tests around generated Compose output, then update
  `scripts/sync.sh promote` so digest pinning and official-store hardening apply
  to both image services without accidentally stripping or granting the wrong
  service privileges.
- Run the promoted manifest through the checked-out official
  `node .tools/lint-apps.mjs` tool when the sibling `umbrel-apps` checkout is
  available. The linter must report zero errors, including the two `APP_HOST`
  rules from issue #143.

### 6. RED/GREEN: migration and operational regression coverage

- Test upgrades from the current single-container deployment with existing
  `/data` configs, backups, metadata, restart-pending state, and read-only legacy
  migration sources. Existing secrets must not be lost or silently replaced.
- Test daemon restart while web remains running, web restart while daemon
  remains running, stale socket recovery, delayed daemon startup, concurrent UI
  polling/reconcile requests, and clean Compose shutdown/restart.
- Re-run Docker full-parity, Secure Mode, k3s, IPv4/IPv6 fail-closed,
  announcement/gossip, management-auth, CSP, and frontend suites.
- Update `README.md` and `DEVELOPING.md` for the two-container topology, role
  logs, health checks, hot-patching, and the new private diagnostics path. Remove
  obsolete advice that assumes a loopback Flask port in the privileged daemon.

## Verification gates

The implementation is not complete until all applicable checks are clean:

1. Python backend, security, IPC, manifest, promotion, and sync-script tests.
2. Shell syntax and entrypoint/dataplane tests.
3. Frontend tests and image/Compose smoke tests for both roles.
4. Promotion dry-run inspection and the official `umbrel-apps` linter with zero
   errors.
5. Manual diff checks confirming that `tunnelsats-web` cannot reach the host
   network, Docker socket, Lightning mounts, or privileged dataplane commands.

## Required review and fix cycles

### Local review cycle

After implementation and full verification, review the complete branch diff
against `origin/master`, with particular attention to privilege-boundary leaks,
RPC input validation, socket permissions/races, proxy trust, secret exposure,
fail-open status, upgrade safety, signal handling, Compose/promotion drift, and
missing regression tests. Fix every finding, rerun the relevant and full suites,
and repeat the local review until no findings remain.

### Greptile review cycle

Only after the local cycle is clean, commit and push the branch, open or update a
pull request, request Greptile review, and inspect all current Greptile comments
and checks. Fix every actionable finding, add regression tests, rerun all
affected verification, push the fixes, resolve/reply to addressed threads, and
request another Greptile pass. Repeat until Greptile reports no remaining issues
or findings and required checks are green.

## Completion criteria

- The official App Store linter reports zero errors for the promoted TunnelSats
  manifest, including neither an invalid `APP_HOST` target nor an empty
  `PROXY_AUTH_WHITELIST`.
- `app_proxy` routes only to bridge-networked `tunnelsats-web`.
- Only `tunnelsats-daemon` has host networking and dataplane privileges.
- The web-to-daemon boundary is private, allowlisted, tested, and fail-closed.
- Existing UI/API behavior, data, Secure Mode, Docker routing, and k3s behavior
  remain covered and passing.
- Both the local review cycle and the Greptile review cycle finish with no
  remaining findings.
