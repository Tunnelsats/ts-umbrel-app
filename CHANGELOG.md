# Changelog

## [3.3.4]

- Hardened dataplane policy routing and fallback blackhole rules for target node network isolation. Integrated automated version sync guardrails.

## [3.3.7]

- Revert management authentication to restore UI compatibility and fix DNS IP resolution in node announcements

## [3.3.8]

- Secure Mode: Backup WireGuard Config view, native file download, modal UX fixes, and gossip verification via read-only lnd.conf audit

## [3.4.0]

- Secure management API with native Umbrel authentication and optimize browser favicon for high-resolution display

## [3.4.1]

- UI status badge indicator distinguishing config_file vs live_gossip verification source

## [3.4.2]

- Fix ingress reverse proxy authentication on umbrelOS 2.0+ host gateway while preserving full umbrelOS 1.6.x backward compatibility

## [3.4.3]

- Add actionable announcement conflict diagnostics, keep the protected node across app restarts while recovering stale Docker endpoints, and harden k3s routing and fail-closed startup

## [3.4.4]

- Harden container CA trust store by distrusting compromised eMudhra (emSign) root CAs and enforcing system CA bundle validation

## [3.5.0]

- Add NWC auto-renewal engine, 3D server globe, subscription timeline ring, Diagnostics & Logs drawer, and 20% faster test suite

## [3.6.0]

- Add native Umbrel home screen widgets (2x2 Shield Grid and Subscription & Bandwidth Overview) with unified fail-closed status badges
