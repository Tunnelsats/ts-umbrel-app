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

> [!WARNING]
> Note that applying a new configuration via the **Install** tab or using the **Restore Node Networking** function will force a restart of your Lightning node container. This is required to ensure your node's networking information is correctly broadcast to the Lightning Network.

> [!WARNING]
> Note that applying a new configuration via the **Install** tab or using the **Restore Node Networking** function will force a restart of your Lightning node container. This is required to ensure your node's networking information is correctly broadcast to the Lightning Network.

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

### 6. How do I safely uninstall the TunnelSats App on Umbrel?

To prevent network routing issues with your Lightning node, you must perform a **two-step uninstall process**. 

Because of how Umbrel isolates applications, simply clicking "Uninstall" in the Umbrel App Store immediately kills the TunnelSats background processes. If this happens while your node is still actively routing through the VPN, your Lightning node (LND/CLN) will be left trying to broadcast a VPN IP address that no longer exists, resulting in lost connections and gossip failures.

Please follow these exact steps to safely remove TunnelSats:

#### Step 1: Disconnect within the TunnelSats App
First, we must safely detach the VPN from your Lightning node and return it to its default Tor/Clearnet state.
1. Open the **TunnelSats App** from your Umbrel dashboard.
2. Locate the **Node Routing Status** section.
3. Click the **Disable Routing (Uninstall)** button.
4. Wait for the UI to confirm that your node has been successfully restored to its default routing state.

#### Step 2: Uninstall via the Umbrel App Store
Once TunnelSats is disconnected from your node, you can safely remove the app itself.
1. Go to the **Umbrel App Store**.
2. Navigate to your installed apps and locate TunnelSats.
3. Click **Uninstall**.

> **Will I lose my subscription?**
> No. Uninstalling the app via the Umbrel App Store destroys the container, but Umbrel safely preserves your TunnelSats app data folder (including your active WireGuard subscription profile). If you reinstall the app in the future, your subscription will still be there.

---

### Troubleshooting: I uninstalled via the App Store first, and now my node is offline!

If you skipped Step 1, your LND or CLN configuration file still contains the TunnelSats `externalhosts` or `announce-addr` directives, but the VPN tunnel is dead. Your traffic is effectively being sent into a black hole.

**How to fix it:**
1. **The Easy Way:** Go back to the Umbrel App Store and **Reinstall** the TunnelSats app. Open it, ensure the VPN connects, and then follow the safe two-step process above.
2. **The Manual Way (Advanced):** Connect to your Umbrel via SSH and manually remove the orphaned TunnelSats lines from your node's configuration files:
   * **For LND:** Edit `~/umbrel/app-data/lightning/data/lnd/lnd.conf` and delete or comment out the `externalhosts=` line. Then restart LND.
   * **For CLN:** Edit `~/umbrel/app-data/core-lightning/data/lightningd/bitcoin/config` and delete or comment out the `announce-addr=` line. Then restart CLN.
