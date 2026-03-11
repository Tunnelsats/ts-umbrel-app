# Tunnelsats Umbrel App - FAQ

### 1. How do I manually add a WireGuard configuration without using the UI?
The Tunnelsats app automatically watches the `~/umbrel/app-data/tunnelsats/data/` directory on your Umbrel hard drive for configuration files. 

If you prefer to load a configuration manually (e.g. via SSH or restoring a backup), simply place your `.conf` file directly into this folder:
`mv tunnelsats_eu-fi.conf ~/umbrel/app-data/tunnelsats/data/`

*(Note: Files placed in the parent directory `~/umbrel/app-data/tunnelsats/` will be ignored!)*

### 2. Are manual configuration files picked up automatically?
Yes! The Tunnelsats background daemon natively handles dynamic reloading:
- **On App Startup:** The app reads the most recently modified `.conf` file in the `data/` folder and immediately wires up the connection.
- **Via the Reconcile Button:** Pressing **Reconcile Now** in the user interface instructs the daemon to instantly rescan the folder and hot-hook your configuration without dropping the container connection.

### 3. How can I verify my connection state from the command line?
The UI dashboard relies on our native **Dataplane API**. You can query this API yourself via SSH to easily debug your network state.
Running the following command acts as the single source of truth for the container's health:
`curl -s http://umbrel.lan:9739/api/local/status | jq`

This instantly returns a comprehensive JSON payload containing:
- The active WireGuard endpoint & public key
- Internal IP routing metrics
- Currently matched `.conf` files
- Any explicit tunnel failure errors (`last_error`)

Additionally, if you want to verify the cryptographic WireGuard handshake itself, you can directly query the tunnel metrics using:
`docker exec tunnelsats wg show`

### 4. Can I tunnel both LND and CLN simultaneously?
No. Tunnelsats is designed to route traffic for exactly **one** distinct Lightning node (LND *or* CLN) at any given time.
If you attempt to run both node implementations in parallel on your Umbrel:
1. Tunnelsats will strictly prioritize LND. LND will be tunneled, and CLN will be ignored.
2. If you transition between nodes (e.g., stopping LND and starting CLN), the active node will successfully acquire the tunnel.
**Warning:** If you have tunneled CLN, and then subsequently click "Start" on LND via the Umbrel UI while CLN is still running, Docker will encounter an IP conflict (`Address already in use`) and LND will fail to boot. You must ensure only one lightning implementation is actively running before using Tunnelsats.

### 5. Why does Tunnelsats use two different ports for CLN?
When configuring CLN for hybrid mode, Tunnelsats injects two distinct port values that serve entirely different purposes:

- **`bind-addr=0.0.0.0:9736`** — This is CLN's internal daemon socket port (hardcoded by Umbrel as `APP_CORE_LIGHTNING_DAEMON_PORT=9736`). It tells CLN's `connectd` subprocess where to bind locally inside the container before it can open any outbound Tor connections. This port is **never visible** to the outside world.
- **`announce-addr=<vpn-server>:<vpnPort>`** — This is the clearnet address that gets gossiped to the Lightning Network, allowing remote nodes to reach you through the Tunnelsats VPN. The port here (e.g. `39486`) is the external VPN forwarding port assigned by Tunnelsats — completely separate from the internal `9736`.

Mixing these up (e.g. using the VPN port as `bind-addr`) would cause CLN to crash on boot, as there is no local process listening on the external VPN port.
