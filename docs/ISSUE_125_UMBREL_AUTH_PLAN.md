# Issue #125: Umbrel-Native Management Authentication Plan

## Status

- Scope: umbrelOS deployments only
- Related issue: <https://github.com/Tunnelsats/ts-umbrel-app/issues/125>
- Implementation status: planned
- k3s: explicitly out of scope for this change

## Objective

Protect the TunnelSats UI and local management API with the user's existing
Umbrel authentication session. Do not add an application-specific login,
Basic Authentication challenge, generated management password, or dependency
on `APP_PASSWORD`.

The implementation must prevent LAN, Tailscale, and ordinary container callers
from bypassing Umbrel authentication by connecting directly to the Flask
listener. Browser-based state changes must also be protected against CSRF.

## Background

The current application runs Flask on `0.0.0.0:9739` in the host network
namespace. The `/api/local` middleware treats membership in broad private
network ranges as authorization. As a result, callers on the LAN, Tailscale,
or a container network can reach sensitive management operations without
proving that they are the Umbrel user.

The reverted implementation did not use the Umbrel login session. It added
HTTP Basic Authentication backed by `APP_PASSWORD`, with a generated password
file as a fallback. `APP_PASSWORD` is a separate per-app credential, and the
manifest's `defaultUsername` and `defaultPassword` fields only expose app login
details to the user. They do not authenticate frontend requests automatically.
This created a second login boundary and caused the dashboard's API requests to
fail, locking users out of the UI.

Umbrel already supplies an authenticated `app_proxy`. It validates the
`UMBREL_PROXY_TOKEN` session cookie and redirects unauthenticated clients to the
normal Umbrel login. TunnelSats should use that proxy as its sole externally
reachable HTTP entry point.

## Target Architecture

```text
Authenticated browser
        |
        | http://umbrel.local:9739
        v
+---------------------------+
| Umbrel app_proxy          |
| - Umbrel session auth     |
| - external port 9739      |
| - host network namespace  |
+---------------------------+
        |
        | http://127.0.0.1:9740
        v
+---------------------------+
| TunnelSats Flask backend  |
| - loopback-only listener  |
| - CSRF/origin validation  |
| - management operations   |
+---------------------------+
```

Port `9739` remains the app's public Umbrel port. Port `9740` is an internal
implementation detail and must never listen on a non-loopback address in an
Umbrel deployment.

## Security Boundaries

The design deliberately uses two independent controls:

1. Umbrel `app_proxy` authenticates the user.
2. The loopback-only backend makes the authenticated proxy the exclusive
   network route to Flask.

The second control is essential. Adding `app_proxy` while leaving Flask on
`0.0.0.0:9739` would preserve a direct unauthenticated bypass. Headers such as
`X-Forwarded-For` must not be treated as proof that a request passed through the
proxy.

CSRF and origin checks form a separate browser security layer. Umbrel's session
cookie is `SameSite=Lax`, but same-site sibling applications and future browser
behavior make the cookie attribute insufficient as the only CSRF control for
high-impact mutations.

This plan does not attempt to defend against an attacker who already has root
access, arbitrary host-network process execution, or control of the Umbrel
authentication service. Those capabilities already cross the host trust
boundary.

## Implementation Plan

### 1. Add the Umbrel authenticated app proxy

Update `tunnelsats/docker-compose.yml` with an `app_proxy` service override:

```yaml
services:
  app_proxy:
    network_mode: "host"
    environment:
      APP_HOST: 127.0.0.1
      APP_PORT: 9740

  tunnelsats:
    network_mode: "host"
    environment:
      - SECURE_MODE=${SECURE_MODE:-false}
      - DASHBOARD_BIND_HOST=127.0.0.1
      - DASHBOARD_BIND_PORT=9740
```

Important requirements:

- Do not set `PROXY_AUTH_ADD=false`.
- Do not whitelist `/api/local`, static UI paths, or the root path.
- Protect the complete TunnelSats origin so the UI and its API share the same
  Umbrel-authenticated boundary.
- Keep `umbrel-app.yml` `port: 9739`; this is the port on which the injected
  Umbrel proxy listens.
- Keep `defaultUsername` and `defaultPassword` empty because TunnelSats no
  longer has a separate application login.
- Do not pass `APP_PASSWORD` into the TunnelSats container.

Umbrel injects the image, published proxy port, authentication secret, manifest
mount, and other base configuration for the `app_proxy` service. The TunnelSats
compose file should supply only the application target and the host-network
override required to reach a loopback backend.

Before implementation is merged, validate on a real umbrelOS 1.x installation
that the injected proxy compose configuration accepts `network_mode: host` and
successfully reaches `127.0.0.1:9740`. Inspect the fully rendered Compose model
as part of this validation. Published ports may be reported as redundant in
host-network mode; the proxy process itself must still bind to the manifest
port.

### 2. Move the Flask backend to a configurable loopback listener

Replace the hard-coded `app.run(host="0.0.0.0", port=9739)` call with validated
environment configuration:

- `DASHBOARD_BIND_HOST`
- `DASHBOARD_BIND_PORT`

For the Umbrel compose deployment, set these explicitly to `127.0.0.1` and
`9740`. Reject invalid port values at startup with a clear error. Log the bind
address and port without logging credentials or tokens.

Preserve the existing defaults for deployment types not changed by this work so
that this Umbrel-only change does not silently alter k3s behavior. Do not edit
the k3s manifests in this implementation.

Update entrypoint log messages so they distinguish the internal backend
listener from the user-facing Umbrel URL.

### 3. Remove application-specific authentication

Do not restore any of the reverted password implementation. In particular, the
new implementation must not contain:

- `MANAGEMENT_PASSWORD` or `MANAGEMENT_USERNAME`
- `APP_PASSWORD` authentication
- `/data/management-password`
- HTTP Basic Authentication challenges
- browser-side `Authorization` header construction
- app-specific login recovery documentation

Flask does not need to validate the Umbrel token itself. The app proxy owns that
responsibility and strips its authentication cookie before forwarding. Flask
trusts the authenticated boundary because only the loopback proxy can reach its
listener.

### 4. Add CSRF and same-origin protection for mutations

Create a small management security layer that is independent of authentication.

For every state-changing management request:

- Require an `Origin` header.
- Canonicalize and compare the origin's scheme, host, and effective port with
  the proxied request origin.
- Reject malformed, missing, `null`, or cross-origin values with a generic
  `403 Forbidden` response.
- Require a high-entropy CSRF token in a custom header such as
  `X-TunnelSats-CSRF-Token`.
- Compare tokens with a constant-time comparison.
- Reject requests with a form-compatible content type where the endpoint
  expects JSON.
- Optionally reject `Sec-Fetch-Site: cross-site` as defense in depth, but do not
  use it instead of the Origin and token checks.

Add an authenticated bootstrap endpoint, for example `GET /api/local/session`,
that returns the CSRF token with `Cache-Control: no-store`. Because this endpoint
is reachable only through the Umbrel proxy, an unauthenticated or cross-origin
site cannot read the token. The frontend should hold the token in memory and
send it only on unsafe methods through a single `managementFetch` wrapper.

At minimum, apply this protection to:

- `POST /api/local/upload-config`
- `POST /api/local/restart`
- `POST /api/local/reconcile`
- `POST /api/local/configure-node`
- `POST /api/local/restore-node`
- any subscription route that changes or consumes locally held subscription
  state

Inventory every Flask route during implementation rather than relying only on
the list above. New management mutations should be protected by default.

### 5. Validate forwarded host and proxy behavior

Continue using `ProxyFix` only for the exact single proxy hop. Security checks
must distinguish the direct socket peer from forwarded client information.

For Umbrel requests:

- The direct peer must be loopback.
- The forwarded host must be syntactically valid.
- The request `Origin` must exactly match the effective proxied origin for
  mutations.
- Known device names from `DEVICE_HOSTNAME`, `DEVICE_DOMAIN_NAME`, and
  `APP_HIDDEN_SERVICE` should be accepted.
- IP-literal hosts should be accepted only when they belong to a local host
  interface, allowing normal access through the Umbrel device's LAN or
  Tailscale address without a static installation-specific list.
- Provide an explicit extra-host configuration for installations using an
  additional reverse proxy or custom DNS name.

Host validation must be tested with `.local`, direct IPv4, direct IPv6, Tor, and
any supported Tailscale access pattern. It must not reintroduce the UI lockout
by assuming every installation uses `umbrel.local`.

The existing private-source network allowlist may remain temporarily as a
defense-in-depth restriction and for compatibility, but it must no longer be
described or tested as the authorization mechanism. Authentication comes from
Umbrel; bypass prevention comes from the loopback listener.

### 6. Add safe response behavior and audit logging

For management responses:

- Add `Cache-Control: no-store` and `Pragma: no-cache` where sensitive state or
  tokens are returned.
- Return generic `403` responses for origin, CSRF, or host failures.
- Log accepted mutations and rejected requests with timestamp, method,
  endpoint, and a non-secret reason code.
- Never log cookies, authorization material, CSRF tokens, WireGuard private
  keys, uploaded configuration bodies, or subscription secrets.

Read-only endpoints that expose private configuration or metadata remain behind
the Umbrel proxy even though they do not require a CSRF token.

### 7. Update user and operator documentation

Update documentation that currently demonstrates unauthenticated calls such as:

```text
curl http://umbrel.local:9739/api/local/status
```

The management API is no longer intended to be an unauthenticated LAN API.
Document browser access through Umbrel as the supported management path. Do not
instruct users to retrieve a generated password or configure Basic Auth.

If programmatic management access is required later, design a separate,
revocable API-token feature with narrowly scoped permissions. It is not part of
this change and must not weaken the browser authentication path.

## Delivery Order

The proxy and backend listener changes must be delivered atomically in one app
release:

1. Add configurable Flask binding and CSRF/origin middleware.
2. Update the frontend to bootstrap and send CSRF tokens.
3. Add the authenticated host-network `app_proxy` targeting loopback port 9740.
4. Set the Umbrel backend bind address to `127.0.0.1:9740`.
5. Update tests and documentation.
6. Validate the rendered compose configuration and run the umbrelOS end-to-end
   checks before publishing the image and manifest version together.

Do not publish an intermediate state. If Flask and the proxy both bind port
9739, startup will fail. If Flask moves to 9740 before the proxy is present, the
UI will be unavailable.

## Test Plan

### Unit and application tests

- Safe methods do not require a CSRF token.
- Every management mutation rejects a missing token.
- Every management mutation rejects an invalid token.
- Every management mutation rejects missing, malformed, `null`, and
  cross-origin `Origin` values.
- Valid same-origin requests with a valid token succeed.
- Form-compatible requests are rejected for JSON-only mutation endpoints.
- Forwarded headers are trusted only for one expected proxy hop.
- Invalid Host values are rejected without reflecting their value.
- Sensitive responses are marked `no-store`.
- Audit logs contain reason codes but no secrets.
- The CSRF bootstrap response is not cached.
- Existing UI workflows use the common management fetch wrapper.

### Compose and manifest tests

- `app_proxy` is present.
- `APP_HOST` is `127.0.0.1` and `APP_PORT` is `9740`.
- Proxy authentication is not disabled.
- No management path is whitelisted.
- The TunnelSats backend binds to `127.0.0.1:9740` in the Umbrel compose file.
- The manifest still exposes port `9739`.
- Manifest default username and password remain empty.
- No password environment variable is passed to TunnelSats.
- Version-sync and promotion tests include the updated compose structure.

### umbrelOS end-to-end tests

Run these checks on a supported umbrelOS 1.x installation:

1. Opening TunnelSats from an authenticated Umbrel dashboard loads without a
   second login prompt.
2. Visiting `http://umbrel.local:9739` while logged out redirects to Umbrel
   authentication.
3. An unauthenticated request to `:9739/api/local/status` cannot read state.
4. An unauthenticated request cannot invoke any management mutation.
5. The host's LAN and Tailscale addresses cannot connect directly to port 9740.
6. The proxy can reach the loopback backend and all existing UI workflows work.
7. A cross-origin form POST cannot trigger restart, restore, configure,
   reconcile, or upload behavior.
8. A cross-origin fetch cannot obtain a CSRF token or invoke a mutation.
9. Config upload, restart, reconciliation, node configuration, node restoration,
   subscription renewal, and config export work after normal Umbrel login.
10. Restarting or upgrading TunnelSats does not create a password file or ask
    for application credentials.
11. Access through supported `.local`, IP-literal, Tailscale, and Tor entry
    points is either functional after Umbrel login or fails closed with a clear,
    documented configuration path.

## Acceptance Criteria

- TunnelSats uses the existing Umbrel login session and never asks for a second
  password.
- `app_proxy` is the only externally reachable HTTP service for TunnelSats.
- Flask listens only on loopback in the Umbrel deployment.
- Direct LAN, Tailscale, and ordinary bridged-container access cannot bypass
  Umbrel authentication.
- All management mutations require a valid same-origin request and CSRF token.
- The complete dashboard remains functional after a normal Umbrel login.
- No generated management credential or Basic Authentication code remains.
- Tests cover authentication routing, direct-port isolation, CSRF, Origin, Host,
  proxy forwarding, and existing UI workflows.
- No k3s manifest or behavior is intentionally changed by this work.

## Out of Scope

- k3s authentication, ingress, Service, NetworkPolicy, or host firewall changes
- a standalone TunnelSats user database or login page
- password synchronization with Umbrel
- programmatic API tokens
- remote third-party API access to `/api/local`
- reauthentication for individual high-impact actions

Reauthentication or high-assurance confirmations can be considered separately
after Umbrel-native authentication and direct-port isolation are in place.
