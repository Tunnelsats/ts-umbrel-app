# Issue #125 implementation plan: authenticate the management API

## Decision

Protect the management plane in the application itself so direct access to the
host-network listener cannot bypass authentication. Umbrel deployments use the
high-entropy per-app `APP_PASSWORD`; other deployments may provide an explicit
credential or let TunnelSats create a persistent random credential in `/data`.
HTTP source-address filtering remains defense in depth, never authorization.

## Security invariants

- No `/api/local` response, subscription renewal/claim, or dashboard document is
  available without a valid management credential.
- Every state-changing management request requires a server-issued CSRF token
  in both a same-site cookie and a custom request header.
- Browser mutations require a non-null `Origin` whose authority matches the
  validated request `Host`; both must resolve through an explicit deployment
  allowlist.
- Forwarded client, host, and scheme values are trusted only from explicitly
  trusted reverse proxies. Direct callers cannot spoof `X-Forwarded-*` headers.
- Missing, unreadable, or weak management credentials fail closed. Generated
  credentials are random, persisted with mode `0600`, and never written to logs.
- Security audit logs identify accepted administrative actions and fixed reject
  reasons without recording authorization headers, CSRF values, request bodies,
  WireGuard material, or other secrets.

## Implementation

1. Add centralized management security middleware in `server/app.py`.
   - Resolve the direct peer and forwarded values safely around `ProxyFix`.
   - Keep the existing network allowlist as the first, secondary restriction.
   - Validate `Host`, HTTP Basic credentials, CSRF, and `Origin` in that order.
   - Return generic JSON errors, an appropriate Basic challenge, and no-store
     response headers for protected API responses.
2. Add credential and CSRF lifecycle support.
   - Prefer `MANAGEMENT_PASSWORD`, then Umbrel `APP_PASSWORD`.
   - Generate a persistent high-entropy password when neither is configured.
   - Expose an authenticated session bootstrap endpoint that returns the
     process-scoped CSRF token and sets a strict same-site cookie.
3. Update the browser client.
   - Route management requests through a shared fetch helper.
   - Lazily bootstrap CSRF state and attach the token header to every mutation.
   - Preserve existing confirmation flows and surface authentication failures
     without leaking backend details.
4. Harden deployment defaults and operational guidance.
   - Pass Umbrel's per-app password plus its device hostnames to the container.
   - Configure k3s with an operator-supplied Secret option, safe internal Host
     defaults, and a default-deny ingress policy that only admits explicitly
     labelled management clients.
   - Document credential retrieval/rotation, additional allowed hosts, reverse
     proxy trust, CSRF-aware CLI usage, and host-firewall recommendations.
5. Add regression and security coverage.
   - Cover valid, invalid, missing, weak, and generated credentials.
   - Cover CSRF cookie/header binding, missing and cross-origin requests, Host
     rejection, direct forwarded-header spoofing, and trusted proxy behavior.
   - Demonstrate that an ordinary k3s pod source cannot read or mutate the API
     without authentication, while an authenticated intended client can.
   - Update frontend and manifest tests for the authenticated request flow and
     deployment controls.

## Review gates

1. Local review/fix cycle: inspect the complete branch diff for authentication
   bypasses, proxy trust mistakes, Host/Origin parsing gaps, CSRF bypasses,
   fail-open configuration, secret disclosure, deployment lockouts, and missing
   tests; fix every finding and rerun all relevant suites until clean.
2. Greptile review/fix cycle: push the branch, open a pull request, request
   `@greptileai review`, resolve every actionable inline or summary finding, and
   repeat review/checks until Greptile reports no remaining findings.
