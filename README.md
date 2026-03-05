# Tunnelsats - Umbrel App

This repository contains the containerized version of [Tunnelsats](https://tunnelsats.com/) explicitly designed for the new **umbrelOS 1.6+ immutable architecture**. 

With `umbreld` and Rugix operating the OS, host-level modifications (such as installing `wireguard-tools` natively or relying on `systemd` services) are destroyed upon reboot. Thus, this application wraps the WireGuard tunneling logic inside a self-contained Umbrel App that dynamically discovers and masks Lightning Node traffic.

## Architecture & How it Works

1. **Dockerized Environment**: The app runs a Debian container with `wireguard-tools`, `iproute2`, `iptables`, `nftables`, `curl`, `jq`, and Flask.
2. **Host Network Control**: With `network_mode: "host"` and capabilities (`NET_ADMIN`, `NET_RAW`), the container can apply host-visible routing and firewall policy without host-installed systemd units.
3. **Docker API Driven Discovery**: The runtime talks directly to `/var/run/docker.sock` to discover active Lightning containers (`lnd` or `core-lightning`) and reconcile dataplane state.
4. **Deterministic Overlay Network**: The app ensures `docker-tunnelsats` (`10.9.9.0/25`) exists and keeps the active Lightning container attached with deterministic IP `10.9.9.9`.
5. **Full Dataplane Parity**:
   - Policy routing (`table 51820`) with blackhole fallback kill switch.
   - Inbound DNAT from WireGuard forwarded VPN port to `10.9.9.9:9735`.
   - Forward-chain allows between WG interface and the Docker bridge.
6. **Auto Reconciliation**: A periodic reconcile loop (30s) and manual reconcile endpoint continuously repair drift (container restart, IP/network changes, stale rules).

### Runtime API Surface

- `GET /api/local/status` now includes dataplane metadata:
  - `dataplane_mode`, `target_container`, `target_ip`, `docker_network`, `forwarding_port`
  - `rules_synced`, `last_reconcile_at`, `last_error`
- `POST /api/local/reconcile` triggers immediate dataplane reconciliation and returns `202 Accepted` with a `request_id`.
- `GET /api/local/reconcile/<request_id>` returns reconcile completion and result payload for that request.

## Important Note: The Reboot Race Condition

Because Umbrel spins up applications independently upon reboot, there can be a brief fraction of a second where LND completes its boot *before* Tunnelsats starts generating its routing rules.
Since we cannot deploy persistent `iptables`/`nftables` rules into the host OS (a limitation of Umbrel 1.6+ immutable design), **if Tunnelsats is delayed, LND might leak its true IP on standard outbound connections immediately upon boot.** 

### Mitigation
To heavily mitigate this, Node operators should strictly set their `externalip` configurations inside LND/CLN to their static Tunnelsats VPN IP. While brief outbound leakage is possible during reboot, the node will never aggressively *announce* its home IP to the broader gossip network.

## Testing Locally (TDD)

Run backend unit tests first:

```bash
./scripts/run-unit-tests.sh -v
```

Then run offline mock tests of the application without needing Umbrel using Docker Compose.

1. Generate a mock `tunnelsats-dev.conf` inside the `data/` directory.
2. Spin up the test environment mock nodes:
    ```bash
    docker compose -f docker-compose.test.yml up -d
    ```
3. Run the Tunnelsats app:
    ```bash
    docker compose up --build tunnelsats
    ```
4. Check logs for successful reconcile and rule sync:
    ```bash
    docker logs tunnelsats
    ```
5. Run the e2e scenario harness:
    ```bash
    ./scripts/e2e-tests.sh
    ```

### E2E Scenarios Covered

- `happy_lnd`: dataplane metadata present and reconcile state exposed.
- `cln_fallback`: stopping the LND mock causes reconcile to target CLN.
- `manual_reconcile`: `POST /api/local/reconcile` returns `202` + request id, then status polling returns completion.
- `drift_restart`: Lightning container restart is healed by reconcile.
- `inbound_reachability`: DNAT/FORWARD rules are present for inbound path.
- `missing_socket`: app degrades gracefully when Docker socket is absent.

## Operational Troubleshooting

- If `rules_synced` is `false`, check `last_error` in `GET /api/local/status`.
- Use the dashboard **Reconcile dataplane now** button or call:
  ```bash
  curl -X POST http://127.0.0.1:9739/api/local/reconcile
  ```
- For full rule refresh, use:
  ```bash
  curl -X POST http://127.0.0.1:9739/api/local/restart
  ```

## App Store Deployment
This repository is pre-configured with the required `umbrel-app.yml` manifest to adhere to the official [Umbrel App Store guidelines](https://github.com/getumbrel/umbrel-apps/blob/master/README.md).
