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
1. **No Docker Socket Access**: The container does not mount `/var/run/docker.sock`, preventing it from inspecting or mutating other containers on the host.
2. **Reduced Container Privileges**: The compose file keeps `NET_ADMIN` for WireGuard and routing, but no longer adds `NET_RAW` or `apparmor:unconfined`.
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
`kubectl apply -k k3s/ --namespace=<your-namespace>`.

See **[`k3s/README.md`](k3s/README.md)** for the full guide. Pay special
attention to the **namespace/RBAC** and **PVC mount path** configuration — those
are the two settings most likely to trip up a first install (a misconfigured
namespace causes `403 Forbidden` pod lookups; a wrong mount path causes
*"Failed to modify LND config"*).

---

## 🛠 Architecture & Dataplane

Because Umbrel is immutable, host-level WireGuard services and persistent host networking rules are not reliable across upgrades/reboots. This app keeps the full dataplane inside the app container and reconciles drift continuously.

### Secure Mode dataplane
1. The container runs with `network_mode: "host"` and `NET_ADMIN` to manage WireGuard, routing, and firewall state.
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

---

## 💬 Support & Links
- **Website**: [tunnelsats.com](https://tunnelsats.com)
- **FAQ**: [tunnelsats.com/faq](https://tunnelsats.com/faq)
- **Support**: Join our [Telegram](https://tunnelsats.com/join-telegram) community.

---

## 💻 Developer Guide & Local Testing

If you are a developer looking to contribute or run tests locally, follow these steps.

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
- Run the available Inbound / Outbound Connection script (bundled or developer wrapper):
```bash
# User-facing (on Umbrel host)
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
- Check `GET /api/local/status` first to view the current `dataplane_mode` and `wg_status`.
- If `rules_synced` is `false`, inspect `last_error` in the JSON response.
- **Trigger immediate Dataplane repair:**
  ```bash
  curl -X POST http://127.0.0.1:9739/api/local/reconcile
  ```
- **Force full app-level restart:**
  ```bash
  curl -X POST http://127.0.0.1:9739/api/local/restart
  ```
