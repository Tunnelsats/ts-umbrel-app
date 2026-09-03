# Issue 155: umbrelOS 2.0 Management Proxy Compatibility Plan

## Problem

On umbrelOS 2.0, the host-native `umbreld` AppGateway connects to the
bridge-networked `tunnelsats-web` service from the container's default gateway
address. The management boundary currently trusts only loopback peers and the
DNS-resolved `MANAGEMENT_TRUSTED_PROXY_HOST`. Because umbrelOS 2.0 removes the
runtime `app_proxy` container, that hostname does not resolve and otherwise
healthy dashboard requests are rejected with HTTP 403 as `direct-peer`.

The fix must preserve the legacy `app_proxy` path used by umbrelOS 1.6.x,
continue rejecting unrelated bridge peers and forged forwarding headers, and
leave the Umbrel App Store proxy declaration intact.

## Scope

- Add fail-closed, cached IPv4 default-gateway discovery to
  `server/security.py`.
- Centralize loopback, gateway, and configured-host peer trust in
  `ManagementSecurity`.
- Apply the existing allowed-client-network policy to the effective forwarded
  client after, and only after, the immediate peer has been authenticated.
- Cover the pure security primitives and Flask request boundary with focused
  tests.
- Preserve `tunnelsats/docker-compose.yml`, the frontend, and all dataplane
  behavior.

## TDD Cycles

### Cycle 1: Default gateway discovery

**Red**

Add focused tests in `server/tests/test_security.py` proving that discovery:

- parses `/proc/net/route` and decodes the little-endian IPv4 gateway;
- accepts only default routes with an all-zero destination and mask and both
  the `UP` and `GATEWAY` flags;
- selects the valid route with the lowest metric when several defaults exist;
- ignores malformed rows, invalid fields, and unusable gateway values;
- returns `None` when the route file is missing or unreadable; and
- caches the result so management polling does not repeatedly read procfs.

**Green**

Implement a lazy cached `ManagementSecurity.default_gateway_ip()` primitive.
It returns one normalized IPv4 address or `None`; parsing and I/O failures must
never broaden peer trust.

### Cycle 2: Multi-source immediate-peer trust

**Red**

Add unit tests proving that:

- loopback remains trusted;
- an exact default-gateway match is trusted;
- the legacy configured proxy remains trusted when its resolved address differs
  from the gateway;
- an unresolvable configured proxy never raises an exception or prevents an
  already-established gateway match; and
- malformed, non-gateway, and non-proxy peers fail closed.

**Green**

Add `ManagementSecurity.peer_is_trusted()` with this evaluation order:

1. Normalize the direct socket peer and reject invalid input.
2. Accept loopback.
3. Accept an exact match with the discovered default gateway.
4. Fall back to exact addresses resolved for the configured proxy hostname.
5. Catch gateway, DNS, and address parsing errors and return `False`.

Gateway evaluation precedes DNS so umbrelOS 2.0 does not perform a known-failing
`app_proxy` lookup for every dashboard poll.

### Cycle 3: Flask boundary and forwarded-client authorization

**Red**

Add integration tests in `server/tests/test_app.py` proving that:

- `/api/local/session` succeeds through the host gateway;
- `/api/local/status` succeeds through the gateway and reaches the web-to-daemon
  transport;
- private IPv4, IPv6 ULA, and Tailscale forwarded clients are accepted;
- public or malformed forwarded clients are rejected with HTTP 403;
- an unrelated bridge peer cannot gain access by injecting
  `X-Forwarded-For`;
- the legacy DNS-resolved `app_proxy` path remains accepted; and
- a gateway-proxied mutation still requires matching Origin and CSRF values.

**Green**

Delegate immediate-peer evaluation from `server/app.py` to
`ManagementSecurity`. Only after that succeeds, use ProxyFix's
`request.remote_addr` as the effective client and apply `client_is_allowed()`.
Keep Host, Origin, CSRF, fetch-site, and JSON content-type checks unchanged, and
preserve generic HTTP 403 responses.

### Cycle 4: Refactor while green

- Make `ManagementSecurity` the single owner of peer-trust decisions.
- Evaluate immediate-peer trust once per request and validate Host syntax
  separately.
- Update stale loopback-only comments and names.
- Keep trust comparisons address-exact; never trust a Docker bridge subnet.
- Keep `app_proxy`, `APP_HOST`, `APP_PORT`, and
  `MANAGEMENT_TRUSTED_PROXY_HOST=app_proxy` unchanged.

## Verification

Run focused tests after each red and green step:

```bash
.venv-tests/bin/pytest -q server/tests/test_security.py
.venv-tests/bin/pytest -q server/tests/test_app.py -k "management or proxy or forwarded"
```

Then run the complete local regression set:

```bash
.venv-tests/bin/pytest -q server/tests/
npm test --prefix web
docker compose config
```

Finally validate the release candidate on both runtime generations:

1. On umbrelOS 1.6.x, confirm the direct peer resolves to `app_proxy` and that
   session, status, and one CSRF-protected mutation succeed.
2. On umbrelOS 2.0+, confirm `app_proxy` DNS may be absent, the direct peer is
   the container gateway, and the same requests succeed.
3. From an unrelated bridge container, confirm direct requests and forged
   forwarding headers receive HTTP 403 and never reach the daemon.
4. Confirm the dashboard reports the actual tunnel and node-routing state.

## Acceptance Criteria

- Management requests proxied by the umbrelOS 2.0 host gateway succeed.
- Legacy containerized `app_proxy` requests continue to succeed.
- Forwarded clients are authorized against the existing local, ULA, and
  Tailscale networks only after the direct peer is trusted.
- Arbitrary bridge peers and public or malformed forwarded clients receive
  HTTP 403.
- Gateway discovery fails closed on malformed data and non-Linux systems.
- The official Umbrel proxy and manifest contracts remain unchanged.
- All backend, frontend, Compose, and applicable store-linter checks pass.
