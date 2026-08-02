# Issue #127 implementation plan: fail-closed IPv6 denial

## Decision

Implement Option B from issue #127. The deployed TunnelSats WireGuard fleet
assigns only IPv4 client addresses and provides only IPv4 transit, so advertising
IPv6 tunnel support would require new customer configurations and a coordinated
backend/server rollout. Until that exists, the selected Lightning workload must
not have non-local IPv6 egress.

## Security invariants

- Keep the existing IPv4 policy-routing and WireGuard behavior unchanged.
- Treat IPv6 as a separately verified dataplane family with policy `deny`.
- Discover every non-link-local IPv6 address assigned to the selected Docker
  container or Kubernetes pod before reporting protection.
- Install two independent IPv6 controls for each discovered source: a policy
  rule into a table whose default is blackholed, followed by a source blackhole
  fallback, and a tagged `ip6tables` FORWARD drop for non-local egress.
- Preserve IPv6 loopback inside the workload and link-local traffic required for
  neighbor discovery; do not add any global/ULA bypass.
- Never set aggregate `rules_synced` to true unless both IPv4 routing and IPv6
  containment verify successfully. A global/ULA target address whose rules
  cannot be installed or verified remains unprotected and reports an error.

## Implementation

1. Extend target discovery in `scripts/entrypoint.sh`.
   - Parse IPv6 endpoint addresses and IPv6 gateways from Docker inspect data.
   - Parse the complete Kubernetes `status.podIPs` array while retaining the
     IPv4 pod IP for the existing dataplane.
   - Normalize and deduplicate non-loopback, non-link-local IPv6 addresses.
2. Add an idempotent IPv6 containment reconciler.
   - Seed a dedicated IPv6 policy table with a blackhole default.
   - Install per-source lookup and fallback-blackhole rules before the packet
     filter is considered ready.
   - Maintain a tagged `ip6tables` chain that returns link-local/ND traffic and
     drops all other forwarded IPv6 from current target addresses.
   - Remove stale rules only when they are provably owned by TunnelSats.
3. Split validation and status by address family.
   - Preserve `rules_synced` as the aggregate compatibility field.
   - Add `ipv4_rules_synced`, `ipv6_rules_synced`, `ipv6_policy`,
     `target_ipv6_addresses`, and `target_ipv6_default_route` to state and the
     local status API.
   - Require aggregate `rules_synced: true` before the dashboard renders the
     workload as Protected, even when no detailed error string is available.
   - Reconciliation evaluates both validators independently so diagnostics do
     not hide the state of one family behind a failure in the other.
4. Keep cleanup fail-closed during ordinary reconciliation failures and remove
   owned IPv6 state only during a full shutdown cleanup.
5. Add tests.
   - Unit-test Docker and k3s IPv6 discovery, idempotent containment, stale rule
     cleanup, independent status fields, and verification failure behavior.
   - Extend the root-only namespace integration test with a dual-stack pod and
     clear-net observer. Prove IPv4 traverses the VPN observer, IPv6 is denied,
     and removing a containment route cannot fall through to the ordinary IPv6
     default route.
   - Run shell syntax checks, backend tests, frontend tests, and the available
     root-only integration tests.
6. Document the explicit IPv6 product policy in `README.md` and `k3s/README.md`.

## Review gates

1. Local review/fix cycle: inspect the complete branch diff for security,
   ownership, cleanup, race, compatibility, and test gaps; fix every finding and
   rerun the relevant checks until clean.
2. Greptile review/fix cycle: push the branch, open a pull request, request a
   Greptile review, resolve every actionable finding, and repeat review/checks
   until Greptile has no findings left.
