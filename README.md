# Tunnelsats - Umbrel App

This repository contains the containerized version of [Tunnelsats](https://tunnelsats.com/) explicitly designed for the new **umbrelOS 1.6+ immutable architecture**. 

With `umbreld` and Rugix operating the OS, host-level modifications (such as installing `wireguard-tools` natively or relying on `systemd` services) are destroyed upon reboot. Thus, this application wraps the WireGuard tunneling logic inside a self-contained Umbrel App that dynamically discovers and masks Lightning Node traffic.

## Architecture & How it Works

1. **Dockerized Environment**: The app runs a Debian container with `wireguard-tools`, `iproute2`, `procps`, and `nftables` compiled in.
2. **Network Interception**: By utilizing `network_mode: "host"` and elevated capabilities (`NET_ADMIN`, `NET_RAW`), the container modifies the host's `nftables` policy routing from within the container boundary.
3. **Dynamic IP Parsing**: LND and Core Lightning running on Umbrel do not have static IP addresses. To route their traffic out of the VPN interface (`tunnelsatsv2`), the `entrypoint.sh` script polls `/var/run/docker.sock` to detect the internal IPs of `lightning_lnd_1` and `lightning_core-lightning_1` as soon as they boot.
4. **The Killswitch**: The container strictly binds LND traffic to the `51820` routing table. If the VPN drops, the primary route falls back to a `blackhole` instead of leaking over the default physical interfaces.

## Important Note: The Reboot Race Condition

Because Umbrel spins up applications independently upon reboot, there can be a brief fraction of a second where LND completes its boot *before* Tunnelsats starts generating its routing rules.
Since we cannot deploy persistent `iptables`/`nftables` rules into the host OS (a limitation of Umbrel 1.6+ immutable design), **if Tunnelsats is delayed, LND might leak its true IP on standard outbound connections immediately upon boot.** 

### Mitigation
To heavily mitigate this, Node operators should strictly set their `externalip` configurations inside LND/CLN to their static Tunnelsats VPN IP. While brief outbound leakage is possible during reboot, the node will never aggressively *announce* its home IP to the broader gossip network.

## Testing Locally (TDD)

You can run offline mock tests of the application without needing Umbrel using Docker Compose.

1. Generate a mock `tunnelsats-dev.conf` inside the `data/` directory.
2. Spin up the test environment mock nodes:
    ```bash
    docker compose -f docker-compose.test.yml up -d
    ```
3. Run the Tunnelsats app:
    ```bash
    docker compose up --build tunnelsats
    ```
4. Check the logs for successful mock discovery of the LND container IPs:
    ```bash
    docker logs tunnelsats
    ```

## App Store Deployment
This repository is pre-configured with the required `umbrel-app.yml` manifest to adhere to the official [Umbrel App Store guidelines](https://github.com/getumbrel/umbrel-apps/blob/master/README.md).
