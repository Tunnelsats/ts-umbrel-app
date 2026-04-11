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
- **Docker-Native Routing**: Unlike previous iterations, this app dynamically routes your LND or Core-Lightning traffic over the VPN purely using Docker bridge networking and `nftables` DNAT.
- **No Sudo Required**: You do not need to modify any host-level `umbrel/app-data` scripts or `docker-compose.yml` files!

---

## 📦 Installation via Community App Store

While we await review for the official Umbrel App Store - Appreciate the upvote [here](https://github.com/getumbrel/umbrel-apps/pull/4919) - you can securely install the app today on any umbrelOS version via our Community Store:

1. Open your Umbrel dashboard and go to the **App Store**.
2. Click the **three dots** in the top right corner and select **Community App Stores**.
3. Add our repository URL: `https://github.com/Tunnelsats/ts-umbrel-app`
4. Install the **TunnelSats** app from the newly added community store.

---

## 🛠 Architecture & Dataplane

Because Umbrel is immutable, host-level WireGuard services and persistent host networking rules are not reliable across upgrades/reboots. This app keeps the full dataplane inside the app container and reconciles drift continuously.

1. The container runs with `network_mode: "host"` and `NET_ADMIN`/`NET_RAW` to manage WireGuard, routing, and firewall state.
2. Runtime detects active Lightning containers (`lnd` or `core-lightning`) via the Docker API (`/var/run/docker.sock`).
3. Runtime ensures a deterministic bridge network (`docker-tunnelsats` under `10.9.9.0/25` with target IP `10.9.9.9`).
4. Runtime enforces dataplane parity:
   - Policy routing table `51820` with blackhole fallback.
   - Inbound DNAT from WireGuard forwarding port to `10.9.9.9:9735` (or dynamically selected ports).
   - FORWARD rules between the WireGuard interface and the docker bridge.
5. A periodic reconcile loop repairs drift after restarts or localized network changes.

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
- Run the available Inbound / Outbound Connection script:
```bash
sudo ~/umbrel/app-data/tunnelsats/scripts/verify.sh 
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
