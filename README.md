# Tunnelsats - Umbrel App

This repository contains the containerized version of [Tunnelsats](https://tunnelsats.com/) for **umbrelOS 1.6+**.

Because Umbrel is immutable, host-level WireGuard services and persistent host networking rules are not reliable across upgrades/reboots. This app keeps the full dataplane inside the app container and reconciles drift continuously.

## Docker-Native Architecture

1. The container runs with `network_mode: "host"` and `NET_ADMIN`/`NET_RAW` to manage WireGuard, routing and firewall state.
2. Runtime detects active Lightning containers (`lnd` or `core-lightning`) via Docker API (`/var/run/docker.sock`).
3. Runtime ensures a deterministic bridge network:
   - Network: `docker-tunnelsats`
   - Subnet: `10.9.9.0/25`
   - Target node IP: `10.9.9.9`
4. Runtime enforces dataplane parity:
   - Policy routing table `51820` with blackhole fallback
   - Inbound DNAT from WireGuard forwarding port to `10.9.9.9:9735`
   - FORWARD rules between WireGuard interface and docker bridge
5. A periodic reconcile loop (default: 30s) plus manual reconcile API repairs drift after restarts/network changes.

## Runtime API

`GET /api/local/status` includes baseline status and dataplane metadata:
- `wg_status`, `wg_pubkey`, `configs_found`, `version`
- `dataplane_mode`, `target_container`, `target_impl`, `target_ip`
- `docker_network`, `forwarding_port`, `rules_synced`, `last_reconcile_at`, `last_error`

`POST /api/local/reconcile`:
- Triggers immediate reconcile
- Returns `202 Accepted` with `request_id` and status URL

`GET /api/local/reconcile/<request_id>`:
- Returns `202` while pending
- Returns completed reconcile result once available

`POST /api/local/restore-node`:
- Comments out TunnelSats-injected LND/CLN lines in mounted config files

## Local Test Workflow

Backend unit tests:

```bash
./scripts/run-unit-tests.sh -v
```

Frontend tests:

```bash
cd web && npm test
```

End-to-end dataplane scenarios:

```bash
./scripts/e2e-tests.sh
```

### E2E Scenarios

- `happy_lnd`
- `happy_cln`
- `manual_reconcile`
- `drift_restart`
- `inbound_reachability`
- `missing_socket`
- `missing_config`
- `shutdown_cleanup`

## Troubleshooting

- Check `GET /api/local/status` first.
- If `rules_synced` is `false`, inspect `last_error`.
- Trigger immediate repair:

```bash
curl -X POST http://127.0.0.1:9739/api/local/reconcile
```

- Force full restart path:

```bash
curl -X POST http://127.0.0.1:9739/api/local/restart
```

## App Store Deployment

This repository contains `umbrel-app.yml` for Umbrel app store compatibility.
