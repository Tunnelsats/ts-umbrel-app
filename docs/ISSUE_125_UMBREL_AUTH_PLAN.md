# Issue #125: Umbrel-Native Management Authentication Plan

## Status

- Scope: umbrelOS deployments with both `SECURE_MODE=false` and
  `SECURE_MODE=true`
- Related issue: <https://github.com/Tunnelsats/ts-umbrel-app/issues/125>
- Implementation status: implemented on `issue-125-umbrel-auth`; real-device
  umbrelOS validation remains pending
- Implementation method: test-driven development (red, green, refactor)
- k3s: explicitly out of scope for this change

## Implementation Record

- Baseline before feature tests: `224` server tests and `59` frontend tests
  passed.
- Cycle A red: `7` listener-contract tests failed because configurable binding
  did not exist; the focused listener suite then passed after implementation.
- Cycle B red: `32` browser-security tests failed before the centralized Host,
  Origin, CSRF, content-type, cache, and audit controls were added.
- Cycle C red: `3` frontend contract tests failed before CSRF bootstrap and the
  shared `managementFetch` wrapper were implemented.
- Route-inventory follow-up red: `2` server tests and `1` frontend test exposed
  the locally mutating subscription-status `GET`; it is now explicitly
  CSRF-protected.
- Cycle D red: `3` compose contract tests failed before the authenticated
  `app_proxy` and loopback backend wiring were added.
- Final local green gate: `279` server tests and `62` frontend tests pass. The
  compose overlay also renders successfully when merged with Umbrel's upstream
  `docker-compose.app_proxy.yml`.
- The two-mode real-device umbrelOS end-to-end matrix remains required before
  release, as described below.

## Objective

Protect the TunnelSats UI and local management API with the user's existing
Umbrel authentication session. Do not add an application-specific login,
Basic Authentication challenge, generated management password, or dependency
on `APP_PASSWORD`.

The implementation must prevent LAN, Tailscale, and ordinary container callers
from bypassing Umbrel authentication by connecting directly to the Flask
listener. Browser-based state changes must also be protected against CSRF.

The complete authentication boundary and browser protections must work
identically when Secure Mode is disabled and enabled. Secure Mode may continue
to alter which existing TunnelSats operations are available, but it must not
alter whether the UI or management API is authenticated, how Flask is exposed,
or whether CSRF and origin validation are enforced.

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

## Secure Mode Compatibility

This architecture applies unconditionally to both supported Umbrel runtime
modes:

| Concern | `SECURE_MODE=false` | `SECURE_MODE=true` |
| --- | --- | --- |
| External HTTP entry point | Authenticated `app_proxy` on `:9739` | Authenticated `app_proxy` on `:9739` |
| Flask listener | `127.0.0.1:9740` | `127.0.0.1:9740` |
| Umbrel session required | Yes | Yes |
| CSRF and Origin checks on mutations | Yes | Yes |
| Direct access to backend from LAN/Tailscale | Refused | Refused |
| Existing mode-specific operational restrictions | Preserved | Preserved |

The proxy and backend binding must not be conditional on `SECURE_MODE`. The
existing `SECURE_MODE` value continues to govern dataplane and node-management
behavior only. Authentication middleware must run before mode-specific route
logic so a mode-specific error can never be used to infer state without first
passing the Umbrel authentication boundary.

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
- Apply the proxy and loopback settings regardless of the value of
  `SECURE_MODE`; there must not be a legacy direct-listener branch for either
  mode.

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

Parameterize binding tests for `SECURE_MODE=false` and `SECURE_MODE=true` to
prove that both resolve to the same loopback address and internal port under
the Umbrel compose configuration.

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
Exercise every applicable mutation test in both Secure Mode states. Where
Secure Mode intentionally rejects an operation, first assert that missing or
invalid CSRF/origin inputs are rejected by the security layer, then assert the
existing mode-specific response for an authenticated, valid same-origin
request.

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

## Test-Driven Delivery Order

Implement this work test-first using a red-green-refactor cycle. Do not write
production code for a behavior until a focused test demonstrates that the
current implementation does not satisfy it.

### 1. Establish the baseline

Run and record the existing server, frontend, compose/manifest, and version-sync
test suites before adding new tests. Existing tests must be green. A pre-existing
failure must be understood and separated from this change before proceeding.

### 2. Add green characterization guardrails

Before changing behavior, add or confirm tests for properties the reverted code
already satisfies:

- no HTTP Basic Authentication challenge;
- no generated management-password file;
- no `APP_PASSWORD` dependency or non-empty manifest login credentials; and
- existing mode-specific behavior with `SECURE_MODE=false` and
  `SECURE_MODE=true`.

These are characterization tests, so they are expected to be green at the
baseline. They prevent the old authentication design or unintended Secure Mode
changes from returning. Do not misrepresent them as red tests.

### 3. Run focused red-green cycles

Implement one vertical contract at a time. For every cycle below:

1. Write the focused test first.
2. Run it and capture the expected failure. A test that passes immediately does
   not prove the new behavior and must be reviewed for an incorrect assertion
   or an already-satisfied contract.
3. Commit or otherwise preserve review evidence of the red test before changing
   production code.
4. Add only enough production code to satisfy that contract.
5. Run the focused test to green, followed by all affected existing tests.
6. Parameterize the cycle over both Secure Mode states wherever runtime behavior
   is involved.

Use these cycles in dependency order:

#### Cycle A: backend listener

- Red: server tests require validated configurable bind settings and prove that
  explicit `127.0.0.1:9740` settings are honored with Secure Mode disabled and
  enabled.
- Green: add the bind configuration and update the server/entrypoint startup
  behavior without changing non-Umbrel deployment defaults.

#### Cycle B: server-side browser security

- Red: tests require CSRF, Origin, Host, cache-control, generic errors, and safe
  audit logging for the management surface in both Secure Mode states.
- Green: add centralized management-route classification, security middleware,
  and the session/CSRF bootstrap endpoint.

#### Cycle C: frontend management requests

- Red: frontend tests require CSRF bootstrap and a common CSRF-aware management
  request wrapper for every unsafe call.
- Green: implement the wrapper and migrate the existing calls without changing
  their mode-specific UI behavior.

#### Cycle D: Umbrel proxy and exclusive network path

- Red: compose and manifest tests require the authenticated host-network
  `app_proxy`, the loopback target, empty app credentials, no authentication
  whitelist, and identical proxy configuration for both Secure Mode values.
- Green: add the proxy override and the explicit Umbrel backend bind settings.

Do not weaken assertions merely to obtain a green result. A discovered design
change should first be reflected in this plan and in a deliberately failing
test.

After Cycle D, add or run the integrated regression contract against the
rendered Compose model. It should be green because the lower-level red-green
cycles established the behavior. If it reveals a missing contract, preserve
that failure and run an additional focused red-green cycle before continuing.

### 4. Refactor while green

Once the contract tests pass, remove duplication, centralize management-route
classification and frontend request handling, and improve names or structure.
Run the complete affected suite after every refactor. Security middleware must
fail closed if configuration or token initialization fails.

### 5. Validate the integrated Umbrel release

Render and inspect the final Umbrel Compose model, then run the umbrelOS
end-to-end matrix with Secure Mode disabled and enabled. Publish the proxy,
backend listener, frontend, tests, image, and manifest version atomically in one
app release.

Do not publish an intermediate state. If Flask and the proxy both bind port
9739, startup will fail. If Flask moves to 9740 before the proxy is present, the
UI will be unavailable.

## Test Plan

### Unit and application tests

- Parameterize security and bind tests over `SECURE_MODE=false` and
  `SECURE_MODE=true` unless a test is specifically about mode-dependent
  operational behavior.
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
- Authenticated requests continue to receive the pre-existing mode-appropriate
  result after the shared security layer accepts them.

### Compose and manifest tests

- `app_proxy` is present.
- `APP_HOST` is `127.0.0.1` and `APP_PORT` is `9740`.
- Proxy authentication is not disabled.
- No management path is whitelisted.
- The TunnelSats backend binds to `127.0.0.1:9740` in the Umbrel compose file.
- The manifest still exposes port `9739`.
- Manifest default username and password remain empty.
- No password environment variable is passed to TunnelSats.
- Proxy and loopback configuration do not vary with `SECURE_MODE`.
- Version-sync and promotion tests include the updated compose structure.

### umbrelOS end-to-end tests

Run the complete set of checks below twice on a supported umbrelOS 1.x
installation: once with `SECURE_MODE=false` and once with `SECURE_MODE=true`.
Do not treat success in one mode as evidence for the other.

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
12. Switching between Secure Mode states and restarting the app preserves the
    authenticated proxy path and never exposes the backend listener.
13. Existing mode-specific UI controls and permitted/forbidden operations retain
    their prior semantics after valid authentication and CSRF validation.

## Acceptance Criteria

- TunnelSats uses the existing Umbrel login session and never asks for a second
  password.
- `app_proxy` is the only externally reachable HTTP service for TunnelSats.
- Flask listens only on loopback in the Umbrel deployment.
- Direct LAN, Tailscale, and ordinary bridged-container access cannot bypass
  Umbrel authentication.
- All management mutations require a valid same-origin request and CSRF token.
- The complete dashboard remains functional after a normal Umbrel login.
- Every authentication, direct-port isolation, CSRF, and origin requirement
  passes with both `SECURE_MODE=false` and `SECURE_MODE=true`.
- Existing mode-specific application behavior remains unchanged after a request
  passes the shared security layer.
- No generated management credential or Basic Authentication code remains.
- Tests cover authentication routing, direct-port isolation, CSRF, Origin, Host,
  proxy forwarding, and existing UI workflows.
- No k3s manifest or behavior is intentionally changed by this work.
- The implementation history or review evidence demonstrates the expected red
  failures before the corresponding production changes turn them green.

## Out of Scope

- k3s authentication, ingress, Service, NetworkPolicy, or host firewall changes
- a standalone TunnelSats user database or login page
- password synchronization with Umbrel
- programmatic API tokens
- remote third-party API access to `/api/local`
- reauthentication for individual high-impact actions

Reauthentication or high-assurance confirmations can be considered separately
after Umbrel-native authentication and direct-port isolation are in place.
