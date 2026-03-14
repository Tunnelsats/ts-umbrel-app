# TunnelSats v3 - Frequently Asked Questions

### 1. How do I manually add a WireGuard configuration?
The TunnelSats app automatically watches the `/data/` directory for configuration files. If you are using SSH or restoring a backup, place your `.conf` file into this folder:

`~/umbrel/app-data/tunnelsats/data/`

> [!NOTE]
> Files placed in the parent directory `~/umbrel/app-data/tunnelsats/` will be ignored.

### 2. Are manual configuration files picked up automatically?
Yes. The TunnelSats daemon handles dynamic reloading:
- **On App Startup:** The app reads the most recently modified `.conf` file in the `data/` folder and establishes the connection.
- **On Demand:** Pressing **"Reconcile"** in the UI instructs the daemon to instantly rescan the folder and apply your configuration without restarting the container.

### 3. How can I verify my connection from the command line?
The dashboard uses our internal **Dataplane API**. You can query this directly via SSH to debug your network state:

```bash
curl -s http://umbrel.lan:9739/api/local/status | jq
```

This returns a JSON payload containing the active WireGuard endpoint, internal routing metrics, and any failure logs (`last_error`). To check the live WireGuard handshake:

```bash
docker exec tunnelsats wg show
```

### 4. Can I tunnel both LND and Core-Lightning (CLN) simultaneously?
**No.** TunnelSats routes traffic for exactly **one** Lightning node at a time.
1. **Priority:** If both are running, TunnelSats will prioritize LND.
2. **Switching:** If you stop LND and start CLN, the daemon will automatically detect the change and reroute the tunnel to CLN.
3. **Warning:** Starting a second node while one is already tunneled may cause an IP conflict in Docker. Always stop one before starting the other.

### 5. Why does CLN use port 9736 internally?
When TunnelSats detects CLN, it handles two distinct ports:
- **Internal (9736):** This is the local bind port required by the Umbrel architecture. It is used for local communication inside the Docker network.
- **External (VPN Port):** This is the port assigned by your TunnelSats subscription (e.g., 39486). This port is gossiped to the network so other nodes can reach you.

TunnelSats handles this mapping automatically—you do not need to manually change your CLN `bind-addr`.
