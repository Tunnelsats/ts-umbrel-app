# TunnelSats v3 - Frequently Asked Questions

### 1. How do I manually add a WireGuard configuration?
The TunnelSats app automatically watches the `/data/` directory for configuration files. If you are using SSH or restoring a backup, place your `.conf` file into this folder:

`~/umbrel/app-data/tunnelsats-data/`

> [!NOTE]
> Files placed in the application directory `~/umbrel/app-data/tunnelsats/` will be ignored.

### 2. Are manual configuration files picked up automatically?
Yes. The TunnelSats daemon handles dynamic reloading:
- **On App Startup:** The app reads the most recently modified `.conf` file in the `tunnelsats-data/` folder and establishes the connection.
- **On Demand:** Pressing **"Reconcile"** in the UI instructs the daemon to instantly rescan the folder and apply your configuration without restarting the container.

> [!WARNING]
> Note that applying a new configuration via the **Install** tab or using the **Restore Node Networking** function will force a restart of your Lightning node container. This is required to ensure your node's networking information is correctly broadcast to the Lightning Network.

### 3. How can I verify my connection from the command line?
The dashboard uses an Umbrel-authenticated **Dataplane API**. From an SSH
session on the Umbrel host, query the non-root web service inside its container
to debug your network state. The privileged daemon has no TCP listener:

```bash
sudo docker compose -f /home/umbrel/umbrel/app-data/tunnelsats/docker-compose.yml \
  exec -T tunnelsats-web curl -s http://127.0.0.1:9740/api/local/status | jq
```

Port `9739` is the user-facing authenticated Umbrel proxy and is not a public
LAN API. State-changing operations must be performed through the authenticated
dashboard. The container-local status request returns a JSON payload containing the
active WireGuard endpoint, internal routing metrics, and any failure logs
(`last_error`). To check the live WireGuard handshake:

```bash
sudo docker compose -f /home/umbrel/umbrel/app-data/tunnelsats/docker-compose.yml \
  exec -T tunnelsats-daemon wg show
```

You rarely need this: the dashboard shows the same checks in plain language with a
**Re-check now** button. The status JSON also contains `last_error_code` and, for
announcement problems, `announcement_diagnostics` with the exact setting, line and fix.
To find the container names on your node and read the TunnelSats logs (useful for a
support request):

```bash
sudo docker ps --format '{{.Names}}' | grep -iE 'lnd|lightning|tunnelsats'
sudo docker compose -f /home/umbrel/umbrel/app-data/tunnelsats/docker-compose.yml \
  logs --tail 100 tunnelsats-daemon tunnelsats-web
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

TunnelSats handles this mapping automatically - you do not need to manually change your CLN `bind-addr`.

### 6. How do I safely uninstall the TunnelSats App on Umbrel?

To prevent network routing issues with your Lightning node, you must perform a **two-step uninstall process**.

By design, normal `SIGTERM` shutdowns, container or pod restarts, `Recreate` upgrades, and `k3s` replacement-pod rollouts (including pre-ready and pre-discovery intervals) preserve fail-closed kernel policy routing, `iptables`/`ip6tables` egress guards, IPv6 containment, `k3s` split-horizon DNS interception/guards (`dns_protected`), and `k3s` emergency `NetworkPolicy` / pod-CIDR quarantine on the host/node so your Lightning node cannot leak traffic or external DNS lookups over clearnet while the TunnelSats daemon or Lightning pod is restarting. Because stopping the container intentionally leaves fail-closed protection active, clicking "Uninstall" in the Umbrel App Store without first running **Restore Node Networking** (`POST /api/local/restore-node`) will leave your Lightning node (LND/CLN) blocked by the host/node fail-closed kill switch and still configured to advertise your VPN address.

Please follow these exact steps to safely remove TunnelSats:

#### Step 1: Restore Node Networking within the TunnelSats App
First, we must safely detach the VPN from your Lightning node, restore its configuration, and decommission the host/node fail-closed guards.
1. Open the **TunnelSats App** from your Umbrel dashboard.
2. Locate the **Node Routing Status** section.
3. Click the **Disable Routing (Uninstall)** / **Restore Node Networking** button (which calls `POST /api/local/restore-node`).
4. Wait for the UI to confirm that your node configuration has been restored, your Lightning node has restarted, and the host/node fail-closed dataplane rules have been decommissioned.

#### Step 2: Uninstall via the Umbrel App Store
Once TunnelSats has decommissioned its fail-closed guards and disconnected from your node, you can safely remove the app itself.
1. Go to the **Umbrel App Store**.
2. Navigate to your installed apps and locate TunnelSats.
3. Click **Uninstall**.

> **Will I lose my subscription?**
> No. Uninstalling the app via the Umbrel App Store destroys the container, but Umbrel safely preserves your TunnelSats app data folder (including your active WireGuard subscription profile). If you reinstall the app in the future, your subscription will still be there.

---

### Troubleshooting: I uninstalled via the App Store first, and now my node is offline!

If you skipped Step 1, TunnelSats's fail-closed host/node routing and firewall guards remain active (as designed for crash/restart protection), and your LND or CLN configuration file still contains the TunnelSats `externalhosts` or `announce-addr` directives while the VPN tunnel is gone.

**How to fix it:**
1. **The Easy Way:** Go back to the Umbrel App Store and **Reinstall** the TunnelSats app. Open it, and then run **Disable Routing (Uninstall)** / **Restore Node Networking** in Step 1 above before uninstalling.
2. **The Manual Way (Advanced):** Connect to your Umbrel via SSH, reboot or flush the residual `tunnelsats` routing/firewall rules after removing the orphaned TunnelSats lines from your node's configuration files:
   * **For LND:** Edit `~/umbrel/app-data/lightning/data/lnd/lnd.conf` and delete or comment out the `externalhosts=` line. Then restart LND.
   * **For CLN:** Edit `~/umbrel/app-data/core-lightning/data/lightningd/bitcoin/config` and delete or comment out the `announce-addr=` line. Then restart CLN.

---

### Troubleshooting: "Dataplane Blocked" because of an announcement conflict

TunnelSats keeps your node's clearnet traffic blocked (fail-closed) until your node
announces **only** your TunnelSats endpoint: the VPN server and forwarded port shown on
the dashboard. Start on the **dashboard**. The Node Routing card lists every setting
TunnelSats found, where it found it, why it is a problem, and how to fix it. You usually
do not need SSH.

**What the labels mean**

| Label | Meaning | Fix |
|---|---|---|
| Non-TunnelSats address | The setting announces an address that is not your TunnelSats endpoint, usually your home or ISP IP or a leftover from an earlier setup. | Disable the line. |
| TunnelSats server, wrong port | The address is your TunnelSats server, but on another port. Example: `externalip=178.156.167.202` announces `178.156.167.202:9735`. `9735` is the Lightning default (used when no port is set), not your forwarded **VPN External Port**. | Use `externalhosts=<server>:<forwarded-port>`. |
| TunnelSats address pinned by IP | Right server and port, but set as a fixed address (for example `externalip=`). | Use the hostname form `externalhosts=<server>:<port>`. |
| Old TunnelSats endpoint | An endpoint from a previous subscription or server. | Replace it with your current endpoint. |
| Automatic IP discovery enabled | `nat=true` (LND) or `ip-discovery=true` (Core Lightning) announces your public home IP automatically. | Set it to `false`. |
| TunnelSats endpoint not announced | The TunnelSats line is missing. | Add the line shown on the dashboard. |
| Node config file not readable / Waiting for your Lightning node | TunnelSats cannot check yet. | Make sure the Lightning app is installed, running, synced and unlocked, then re-check. |

**Where was it found?** *Config file* means TunnelSats read `lnd.conf` (or the Core
Lightning `config`). This check runs in every mode, and it is the only check in Secure
Mode, where TunnelSats can read the file but cannot change it or ask your node directly.
*Live gossip* means TunnelSats asked your node what it announces right now (standard mode
only). For LND it also withdraws old addresses automatically on every check, without a
restart; Core Lightning only changes its announcement after a restart.

**Fix it without SSH**

1. Standard mode: click **Fix automatically** on the dashboard. TunnelSats backs up and
   disables the conflicting lines in your config file and withdraws old addresses from
   live gossip right away. LND keeps running (Core Lightning is restarted). Restart LND
   when convenient so it also loads the saved file, especially after `nat=true` was disabled.
2. Secure Mode: click **Show step-by-step fix**. It lists the exact lines from your config
   file. Edit the file in the Umbrel **Files** app, save it, and restart the Lightning Node
   (or Core Lightning) app.
3. Click **Re-check now** on the dashboard to confirm.

**Optional: check over SSH**

```bash
# 1. Find the container names (umbrelOS: lightning_lnd_1, core-lightning_lightningd_1)
sudo docker ps --format '{{.Names}}' | grep -iE 'lnd|lightning|tunnelsats'

# 2. Show the active announcement lines (lines starting with # are ignored)
sudo grep -nE '^[[:space:]]*(externalip|externalhosts|nat)[[:space:]]*=' \
  ~/umbrel/app-data/lightning/data/lnd/lnd.conf

# 3. Show what LND announces right now (each "uris" entry is pubkey@address)
sudo docker exec lightning_lnd_1 lncli getinfo

# 4. Withdraw a stale address from live gossip (LND only, after fixing the config file)
sudo docker exec lightning_lnd_1 lncli peers updatenodeannouncement --address_remove=203.0.113.5:9735

# 5. TunnelSats logs for a support request
sudo docker compose -f /home/umbrel/umbrel/app-data/tunnelsats/docker-compose.yml \
  logs --tail 100 tunnelsats-daemon
```

Pass only `ip:port` to `--address_remove`, without the `pubkey@` prefix. Fix the config
file first, otherwise the address returns on the next restart. For Core Lightning, search
for `announce-addr|ip-discovery` in
`~/umbrel/app-data/core-lightning/data/lightningd/bitcoin/config`.

Public explorers can keep showing an old address for a while, and historical snapshots
cannot be erased. What counts is what your node announces now: the dashboard check and
`lncli getinfo`.
