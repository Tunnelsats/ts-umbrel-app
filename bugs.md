# Tunnelsats Phase 3b: Bug Tracker

This document tracks known bugs discovered during testing for inclusion in future PRs.

## Open Bugs

### 1. Reconcile Policy Route Failure
**Description:** Clicking Reconcile causes an error `Failed to discover WireGuard interface addresses on tunnelsatsv2` or `Failed to set policy route for WireGuard network 10.9.0.2/24`.
**Root Cause:** Production WireGuard configs often use `/32` addresses (e.g. `10.9.0.100/32`) which do not create subnet-level `.scope link` routes in the kernel. When `ip addr show` is used instead, it mathematically returns a "dirty" host CIDR (e.g. `10.9.0.2/24`). `ip route replace` rigidly rejects this because the host bits aren't zeroed out.
**Fix:** Embed a native `python3 -c` script inside `entrypoint.sh` using the `ipaddress.IPv4Network(..., strict=False)` module. This mathematically calculates and zeroes out the host bits of any arbitrary interface address before passing it to `ip route replace`, preventing the crash on both `/24` and `/32` architectures.

### 2. Multi-Config Resolution Order
**Description:** Alphabetical sorting of `/data/tunnelsats*.conf` can cause dummy configs (`tunnelsats-dev.conf`) to be prioritized over valid user configs (like `tunnelsats_eu-fi.conf`).
**Root Cause:** The `find ... | sort` command sorts ascii characters, putting `-dev` (0x2D) ahead of `_eu` (0x5F). 
**Fix:** Change the config locator logic in `tunnelsats.sh` and `entrypoint.sh` to sort by newest-modified (`ls -1t`) so newly imported files are always prioritized.

### 3. Uploaded Configs locked as Root
**Description:** Configuration files imported via the UI are written to `/data` as `root:root` with `-rw-------` permissions, breaking native SSH administration.
**Root Cause:** Python's `open("w")` generates files natively bound to the Container's runtime user (`root`).
**Fix:** Explicitly inject `os.chmod(..., 0o600)` and `os.chown(..., 1000, 1000)` during `_write_file_secure()` and `upsert_config_line_in_section` so the files elegantly degrade to the `umbrel:umbrel` host user with safe, restricted permissions (`-rw-------`).

### 4. LND Config Injection and Reconcile Timeout
**Description:** During configuration injection, clicking Reconcile silently times out after 5 seconds with a 500 API Error.
**Root Cause:** The `docker restart lnd` API call inherently blocks until LND successfully spins down and restarts. If the Umbrel disk is bogged down (e.g., syncing Bitcoin Core), LND takes significantly longer than Python's hardcoded 5-second HTTP timeout limit to fully restart, causing `docker_api_post` to loudly crash. 
**Fix:** Expand the HTTP `timeout=` parameter in `docker_api_post()` from `5` to `30` seconds.

### 5. Config Import DNS Hijack (Table Attribute) 
**Description:** Importing a user `WireGuard` config causes the entire Umbrel node to temporarily lose DNS/Internet access (` Temporary failure in name resolution `).
**Root Cause:** Standard configs contain `AllowedIPs = 0.0.0.0/0`. When passed blindly to `wg-quick`, it hijacks the entire host's default routing table (including DNS sockets).
**Fix:** Ensure `entrypoint.sh` aggressively strips any existing `Table=` definitions and injects `Table = off` into the parsed file immediately before bringing `wg-quick` up.

### 6. Ghost Nftables Drop Filter (wg-quick Crash Residue)
**Description:** After a manual or crashed WireGuard restart on a specific interface name, `curl ifconfig.me` routes perfectly outbound but all return packets are silently dropped prior to Netfilter `PREROUTING`.
**Root Cause:** When `wg-quick down` fails to cleanly execute (e.g. if the interface is deleted early), it orphans a permanent `nftables` table in the host kernel (e.g., `table ip wg-quick-tunnelsats` or `wg-quick-tunnelsatsv2`). This table contains a `raw PREROUTING` drop hook that aggressively destroys any return packets destined for the VPN IP (`10.9.x.x`) that do not securely match the exact historical interface string (`iifname != "tunnelsats"`).
**Fix:** Provide troubleshooting instructions indicating that rogue `nftables` tables must be manually purged via `nft delete table ip wg-quick-<interface_name>` from the host network namespace if `curl` continuously times out (Exit Code 28) despite a successful handshake.
