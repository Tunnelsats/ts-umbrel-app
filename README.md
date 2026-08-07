  <img src="https://raw.githubusercontent.com/Tunnelsats/tunnelsats/ffb4732328045922dc90eb5580654077e8d3f246/images/brand/logos/ts_logo_rectangle.svg" alt="TunnelSats Logo" width="400"/>

<br/>

<div align="center">
  <img src="https://img.shields.io/github/actions/workflow/status/Tunnelsats/ts-umbrel-app/docker-release.yml?branch=master&label=Docker%20Build&style=flat-square" alt="Build Status"/>
  <img src="https://img.shields.io/github/license/Tunnelsats/ts-umbrel-app?style=flat-square&color=blue" alt="License"/>
  <a href="https://tunnelsats.com/join-telegram"><img src="https://img.shields.io/badge/Telegram-Join%20Community-blue?style=flat-square&logo=telegram" alt="Telegram"/></a>
</div>

<br/>

# TunnelSats for Umbrel

This repository contains the containerized version of [TunnelSats](https://tunnelsats.com/) optimized for [umbrelOS](https://github.com/getumbrel/umbrel) (fully compatible with both current and future versions). It is currently under review for the official native Umbrel App Store ☂️. 

## ⚡ What it Solves
Running a Lightning Network node over Tor ensures privacy but introduces high latency and routing reliability issues. Conversely, running on Clearnet exposes your home IP address. 

TunnelSats provides a hybrid solution: **Privacy-preserving clearnet connectivity**. 
By establishing a secure WireGuard tunnel to one of our global servers, your node's lightning traffic is routed through our IP address. Your home IP remains hidden, while you benefit from the speed and reliability of the Clearnet.

## 🚀 Features
- **Buy & Renew In-App**: Purchase WireGuard subscriptions using Lightning right from the Umbrel dashboard.
- **Secure Mode by Default**: The bundled Umbrel compose runs without the Docker socket and uses TCP probing plus manual node configuration guidance.
- **No Sudo Required**: You do not need to modify any host-level `umbrel/app-data` scripts or `docker-compose.yml` files!

---

## 📦 Installation via Community App Store

While we await review for the official Umbrel App Store - Appreciate the upvote [here](https://github.com/getumbrel/umbrel-apps/pull/4919) - you can install the app today on any umbrelOS version via our Community Store:

1. Open your Umbrel dashboard and go to the **App Store**.
2. Click the **three dots** in the top right corner and select **Community App Stores**.
3. Add our repository URL: `https://github.com/Tunnelsats/ts-umbrel-app`
4. Install the **TunnelSats** app from the newly added community store.

---

## 🔒 Secure Mode (Official App Store Sandbox)

To comply with Umbrel's strict security guidelines, the bundled Umbrel compose runs in **Secure Mode** by default (with `SECURE_MODE=true` set in the environment).

### What changes in Secure Mode?
1. **No Docker Socket Access**: The privileged daemon does not mount `/var/run/docker.sock`, preventing it from inspecting or mutating other containers on the host.
2. **Reduced Container Privileges**: The daemon keeps `NET_ADMIN` for WireGuard and routing, but no longer adds `NET_RAW` or `apparmor:unconfined`.
3. **Dynamic Probing**: Instead of using the Docker API, the app detects LND or Core-Lightning nodes by dynamically probing their default TCP ports (`10.21.21.9:9735` and `10.21.21.96:9736`) and checking read-only configuration file paths.
4. **Manual Node Configuration**: The app cannot automatically edit your node's configuration files or restart the containers. Instead, the UI provides copy-to-clipboard blocks and step-by-step instructions so you can easily copy and paste the parameters yourself.

### Swapping Modes
By default, the bundled compose runs with Secure Mode enabled (`SECURE_MODE=true`). The older automated Docker dataplane remains available for development or controlled self-hosted installs, but it is no longer the default.

You can manually force either behavior by editing `tunnelsats/docker-compose.yml` or setting the environment variable:
- **Force Secure Mode**: `SECURE_MODE=true`
- **Force Automated Mode**: `SECURE_MODE=false` (requires restoring the Docker socket mount and accepting the broader privilege model)

---

## ☸️ Running on k3s / Kubernetes

Besides Umbrel, TunnelSats can run in `k3s` mode alongside an LND/CLN node in a
Kubernetes cluster. The manifests live in [`k3s/`](k3s/) and are applied with
`kubectl apply -k k3s/` after setting the namespace in
`k3s/kustomization.yaml`.

See **[`k3s/README.md`](k3s/README.md)** for the full guide. Pay special
attention to the **namespace/RBAC**, **cluster bypass CIDRs**, and **PVC mount
path** configuration. k3s mode routes both inbound replies and new outbound
Lightning connections through WireGuard, with a blackhole fallback when the
tunnel route is unavailable.

---

## 🛠 Architecture & Dataplane

Because Umbrel is immutable, host-level WireGuard services and persistent host networking rules are not reliable across upgrades/reboots. The Umbrel app therefore uses two isolated services:

- `app_proxy` routes authenticated browser traffic to the non-root,
  bridge-networked `tunnelsats-web` service.
- `tunnelsats-web` sends allowlisted management requests over the private
  `/run/tunnelsats/daemon.sock` Unix socket.
- Only the host-networked `tunnelsats-daemon` owns WireGuard, routing,
  firewall, Docker, and Lightning-node privileges.

The daemon reconciles dataplane drift continuously. It has no bridge-network
attachment or published management port; daemon failures make the web API
return `503` instead of reporting protection optimistically.

### IPv6 policy: fail-closed denial

TunnelSats VPN servers currently provide IPv4 transit only. TunnelSats therefore
does **not** route IPv6 over WireGuard: it discovers non-link-local IPv6 addresses
on the selected Lightning workload and blocks their egress with both IPv6 policy
blackholes and tagged `ip6tables` rules. Loopback remains internal to the
workload and link-local traffic remains available for neighbor discovery; global
and unique-local IPv6 egress is denied.

The local status API reports `ipv4_rules_synced` and `ipv6_rules_synced`
independently, with `rules_synced` remaining the aggregate compatibility field.
`ipv6_policy` is `deny`; `target_ipv6_addresses` and
`target_ipv6_default_route` expose the detected IPv6 context. TunnelSats never
reports aggregate protection when IPv6 is present but containment cannot be
installed and verified.

### Secure Mode dataplane
1. Only `tunnelsats-daemon` runs with `network_mode: "host"` and `NET_ADMIN` to manage WireGuard, routing, and firewall state.
2. Runtime detects active Lightning nodes through TCP probes on Umbrel's default service IPs and read-only config path checks.
3. The UI returns manual LND/CLN configuration and restore instructions instead of modifying Lightning config files directly.
4. Runtime enforces dataplane parity:
   - Policy routing table `51820` with blackhole fallback.
   - Inbound DNAT from WireGuard forwarding port to the detected local Lightning node port.
   - FORWARD rules between the WireGuard interface and local Lightning service.
5. A periodic reconcile loop repairs drift after restarts or localized network changes.

### Legacy automated Docker dataplane
When `SECURE_MODE=false` and the Docker socket is explicitly mounted, the runtime can use Docker APIs to attach the active Lightning container to a deterministic bridge network (`docker-tunnelsats` under `10.9.9.0/25` with target IP `10.9.9.9`) and automatically configure routing:
   - Policy routing table `51820` with blackhole fallback.
   - Inbound DNAT from WireGuard forwarding port to `10.9.9.9:9735` (or dynamically selected ports).
   - FORWARD rules between the WireGuard interface and the docker bridge.
   - A `GwPriority` higher than every competing endpoint on Docker Engine API 1.48+.
   - Per-source policy and fallback rules for every IPv4 assigned to the Lightning container, with only each directly connected subnet bypassing WireGuard.
   - Live route checks to multiple public IPv4 destinations from inside the Lightning container on every reconciliation. Protection is never reported when the selected source is not `10.9.9.9` or the selected gateway is not `docker-tunnelsats`.

Older Docker Engines do not support `GwPriority`; they use the compatible attach payload but must still pass the same live route checks before the reconciliation kill switch is released.

### k3s dataplane

The k3s deployment selects a ready, co-located Lightning pod and routes every
non-cluster destination sourced by that pod through policy table `51820`.
Explicit `K3S_BYPASS_CIDRS` entries keep required pod, service, Bitcoin, and Tor
traffic on the cluster's main table. A source blackhole prevents ordinary
egress fallback if WireGuard routing disappears.

---

## 💬 Support & Links
- **Website**: [tunnelsats.com](https://tunnelsats.com)
- **FAQ**: [tunnelsats.com/faq](https://tunnelsats.com/faq)
- **Support**: Join our [Telegram](https://tunnelsats.com/join-telegram) community.

---

## 💻 Developer Guide & Local Testing

If you are a developer looking to contribute or run tests locally, follow these steps.

### Local Node Hot-Syncing (`sync.sh node`)
Configure your node connection in `.env.local` (**gitignored, never commit passwords or secrets**):
```env
UMBREL_HOST=umbrel.local
UMBREL_PASSWORD=your_umbrel_password
```
Then stage local source code directly onto your dev node without building full Docker images:
```bash
# Hot-sync in Standard mode
./scripts/sync.sh node

# Hot-sync in Secure Mode (Official Umbrel App Store mode)
SECURE_MODE=true ./scripts/sync.sh node
```

### Unified Test Suite
The workspace uses a single **Source of Truth (SOT)** for backend tests, E2E dataplane scenarios, and entrypoint verification.
```bash
./scripts/test.sh
```
*Note: This script automatically detects and sets up the correct environment for unit and integration testing.*

### Frontend UI Tests
```bash
cd web && npm test
```

### Troubleshooting & API
- Run the available Inbound / Outbound Connection script:
```bash
# Official Store (or remote run)
wget -q https://raw.githubusercontent.com/Tunnelsats/tunnelsats/59a866dcfa2a7d9ef344963a3fa630fac0e7568a/scripts/verify-umbrel.sh -O verify-umbrel.sh
sudo bash verify-umbrel.sh

# Community Store (bundled on host)
sudo ~/umbrel/app-data/tunnelsats/scripts/verify.sh 

# Developer-facing (local repo root)
sudo bash scripts/diagnose.sh
```

```text
=== TunnelSats Dataplane Verification ===
Target: us3.tunnelsats.com (178.156.167.202) : 12345
----------------------------------------------------------------
[0/3] Discovering Home IP...                    PASS (123.456.789.101)
[1/3] Testing Outbound Tunnel Alignment...      PASS (Verified via 178.156.167.202)
[2/3] Testing Inbound Port (via IP)...          PASS (Connected to 178.156.167.202:12345)
[3/3] Testing Inbound Port (via Hostname)...    PASS (Connected to us3.tunnelsats.com:12345)
----------------------------------------------------------------
```
- Check the authenticated dashboard first to view the current
  `dataplane_mode`, `wg_status`, and any reconciliation error.
- For SSH-only diagnostics, query the bridge-networked web service from inside
  its container. The privileged daemon has no TCP listener. For example:
  ```bash
  sudo docker compose -f \
    /home/umbrel/umbrel/app-data/tunnelsats/docker-compose.yml \
    exec -T tunnelsats-web \
    curl -s http://127.0.0.1:9740/api/local/status | jq
  ```
- Inspect role-specific logs with `sudo docker compose -f
  /home/umbrel/umbrel/app-data/tunnelsats/docker-compose.yml logs
  tunnelsats-web tunnelsats-daemon`.
- If `rules_synced` is `false`, inspect `ipv4_rules_synced`,
  `ipv6_rules_synced`, and `last_error` in the JSON response.
- Trigger reconciliation and restart actions from the authenticated dashboard.
  Browser mutations require Umbrel authentication plus same-origin CSRF
  validation and are intentionally not exposed as unauthenticated LAN `curl`
  commands.
