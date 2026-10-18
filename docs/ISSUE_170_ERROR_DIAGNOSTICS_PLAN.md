# Issue #170: Actionable Error Diagnostics in the Dashboard (UI first, CLI optional)

- Issue: [Tunnelsats/ts-umbrel-app#170](https://github.com/Tunnelsats/ts-umbrel-app/issues/170), assigned to `TrezorHannes`
- Branch: `feat/issue-170-announcement-diagnostics` (from `master` @ `9a68eb5`)
- Status: implemented in this branch (pytest and Jest green; see the PR for counts)

## 1. Problem

A customer saw **"Dataplane Blocked: Conflicting real-IP announcement setting detected in node configuration."** His LND announced `178.156.167.202:9735`. That IP is his TunnelSats server (`us3.tunnelsats.com`), but `9735` is LND's default port, not his forwarded port. The block was correct. The message was not: it reads like a home-IP leak, names no setting, and offers no fix. He is not comfortable with the CLI, and every remedy we document is CLI-only.

**Root problem:** the backend already computes exactly what is wrong (`announcement_conflicts`, the audited config line, the expected endpoint, the live-gossip leftovers), but the UI collapses it into one generic string. The vocabulary is wrong for the most confusing case, and the fix-then-validate loop needs SSH.

**End-user goal:** "Tell me which setting is wrong, why, what to change it to, and let me confirm it worked, without a terminal."

## 2. Current-state audit

### 2.1 Error surfaces today

| Situation | Secure Mode (official store default) | Standard mode | What the user sees | Gap |
|---|---|---|---|---|
| `externalip` / `nat=true` / foreign `externalhosts` in config | read-only config audit | config audit | "Dataplane Blocked" + "Conflicting real-IP announcement setting..." | No setting, line, class, or fix. Says "real-IP" for a TunnelSats IP. |
| TunnelSats endpoint missing from config | audit adds `missing-externalhosts=...` | n/a | Same generic string | Not told which line to add. |
| Live gossip still announces an old address | not observable (no `lncli`) | `lncli getinfo` + automatic `--address_remove` | "LND gossip announcements have not been verified..." | Remaining addresses are only persisted in metadata, never shown. |
| LND starting, syncing, or wallet locked | n/a | `lnd_initializing` | Same verification string | Reads like a privacy failure. The real action is "wait or unlock". |
| Config file missing/unreadable | `config-unavailable` | same | Conflict string | No hint. |
| New install, no subscription yet | daemon `No WireGuard config found in /data` | same | "Dataplane Blocked: No WireGuard config found in /data" | Should point to Subscribe / Import. |
| Lightning app not running | daemon `SecureMode: No Lightning node detected (probed ...)` | n/a | Raw technical string | Should say "start your Lightning app". |
| Daemon down (web returns 503) | `TunnelSats daemon is unavailable.` | same | "Tunnel Down" + "No Nodes Detected" | Misleading: the app cannot know either. |

Cross-cutting findings in `web/js/app.js` and `web/index.html`:

1. **Brittle string matching.** The Secure Mode branch checks `last_error` for `gossip|announcements`. The conflict string contains neither, so Secure Mode users (who hit this most) fall through to the generic red box.
2. **Unsafe rendering pattern.** That branch interpolates `last_error` into `innerHTML`.
3. **No validation loop.** Status is fetched on load, tab switch, and after actions only. After editing `lnd.conf` there is no "Re-check".
4. **Error styling silently missing.** `web/dist/tailwind.css` was last built in #45. 66 of 438 classes used by the UI are absent, including every red/yellow error and warning colour (`bg-red-950/40`, `text-red-300`, `border-red-800/70`, ...). The routing error box renders without its red styling.
5. **Secure Mode manual modal** lists generic placeholders (`externalip=`), uses the wrong container (`docker exec lnd`), and asks users to hand-edit `<your-old-ip>:9735`.
6. **Standard-mode confirmation modal** is titled "Real-IP announcement conflict detected" even for a TunnelSats IP on the wrong port.
7. **Configure failure toast** drops the `remaining_conflicts` the backend returns.

### 2.2 How the FAQ helps today

- **FAQ 8** (the only conflict troubleshooting) is `data-secure-mode="exclude"`. The official store runs Secure Mode by default, so the users who hit this most never see it.
- It is CLI-only (three SSH steps) and assumes `lncli` access.
- Container names are inconsistent (`lnd` in FAQ 6, the modal and the backend `gossip_command`; `lightning_lnd_1` in FAQ 8) and nothing explains how to look them up. FAQ 8 also uses `docker exec -it ... | grep`, which injects `\r` (a pattern `.agents/coding.md` warns about).
- No `grep` for the active announcement lines. No note that `--address_remove` takes `ip:port` without `pubkey@`, or that the config must be fixed first (otherwise the address returns on the next restart).
- No "TunnelSats IP on `:9735`" explanation.
- TunnelSats containers are not named for `docker logs`. The official-store promotion strips `container_name`, so store users run `tunnelsats_tunnelsats-daemon_1`, not `tunnelsats-daemon`. Commands must use the compose service or a lookup.
- Numbering collides ("6." twice) when both FAQ 6 and FAQ 8 are visible.
- `FAQ.md` has no conflict troubleshooting at all.

## 3. Goals and non-goals

**Goals**

1. Name the offending setting, where it was found (file and line, or live gossip), its class, and the expected endpoint.
2. Offer a fix path that needs no CLI in both modes: "Fix automatically" (standard) or an exact, pre-filled manual guide (Secure Mode). CLI stays an optional "advanced" layer.
3. Let users validate in the UI with **Re-check now**.
4. Make the FAQ visible in Secure Mode, consistent, and verifiable.
5. Replace UI string matching with stable error codes, and give friendly guidance for the most common non-announcement errors.

**Non-goals**

- No policy relaxation. The same conflicts still block (fail-closed); this is diagnostics only.
- No new privileges in Secure Mode (no macaroon reads, no Docker socket).
- No automatic polling. In standard mode every status call with unverified gossip runs `lncli` and auto-withdraws addresses; polling would hammer LND. Re-check is explicit.
- No dataplane or `entrypoint.sh` changes.

## 4. Design

### 4.1 Backend contract (additive only)

New `/api/local/status` fields. `last_error` and `announcement_conflicts` stay byte-for-byte compatible for FAQ `curl` users, scripts and tests.

| Field | Type | Purpose |
|---|---|---|
| `last_error_code` | string or null | Stable code for `last_error` (table below). |
| `announcement_diagnostics` | list | One structured entry per problem (schema below). |
| `announcement_help` | object | Mode-aware copy-paste helpers: config path, expected line, verify/lookup/grep/log commands. |

**Diagnostic entry**

| Key | Example | Notes |
|---|---|---|
| `impl` | `lnd` | `lnd` or `cln` (both configs are audited). |
| `kind` | `wrong-port` | Taxonomy below. |
| `source` | `config_file` | `config_file` or `live_gossip`. |
| `setting`, `value`, `line`, `line_number` | `externalip`, `178.156.167.202`, `externalip=178.156.167.202`, `12` | Exact active config line (1-based line number). |
| `address`, `port_defaulted` | `178.156.167.202:9735`, `true` | Effective announced endpoint. LND/CLN default to `9735` when no port is set. |
| `expected`, `expected_line` | `us3.tunnelsats.com:23217`, `externalhosts=us3.tunnelsats.com:23217` | What it should be. |
| `config_path` | `/home/umbrel/umbrel/app-data/lightning/data/lnd/lnd.conf` | Host path for Files app/SSH (null on k3s). |
| `withdraw_command` | `sudo docker exec lightning_lnd_1 lncli peers updatenodeannouncement --address_remove=178.156.167.202:9735` | LND clearnet addresses only; validated and shell-quoted. |
| `detail` | `lnd_initializing` | Machine reason for `config-unreadable` / `verification-pending` / unknown endpoint. |

**Kinds**

| Kind | Trigger | Example | Fix shown |
|---|---|---|---|
| `real-ip` | Host is not a TunnelSats address | `externalip=203.0.113.5` | Disable the line |
| `wrong-port` | Current TunnelSats server (name or resolved IP), port is not the forwarded port | `externalip=178.156.167.202` (announces `:9735`) | Replace with `expected_line` |
| `pinned-ip` | Correct server and port, but via LND `externalip` | `externalip=178.156.167.202:23217` | Use `externalhosts=` instead |
| `stale-endpoint` | Another `*.tunnelsats.com` endpoint (old subscription) | `externalhosts=de2.tunnelsats.com:35825` | Replace with `expected_line` |
| `auto-discovery` | `nat=true` (LND) / `ip-discovery=true` (CLN) | `nat=true` | Set to `false` |
| `missing-endpoint` | Expected endpoint not announced (config, or live gossip) | no `externalhosts` line | Add `expected_line` |
| `config-unreadable` | Config missing or unreadable while the node is detected | | Restart apps, check install |
| `verification-pending` | Live check could not run (LND starting/locked/not found) | `lnd_initializing` | Start/unlock LND, Re-check |

**Server attribution without new trust:** a host belongs to the current server if it equals `serverDomain`, equals the WireGuard endpoint host from metadata, or is one of the A records of `serverDomain`. Only the TunnelSats domain is ever resolved (never user hostnames). Resolution is lazy (only when an IP-literal conflict exists), bounded (1.5 s) and cached in memory. If DNS fails, the entry degrades to `real-ip`, whose copy stays honest ("not your TunnelSats endpoint").

**Mode-aware commands**

| Mode | LND exec prefix |
|---|---|
| Standard (Docker socket) | the real container from dataplane `target_container` when it matches the LND pattern, else `lightning_lnd_1` |
| Secure Mode | `lightning_lnd_1` / `core-lightning_lightningd_1` (umbrelOS `<app>_<service>_1`) |
| k3s | `kubectl -n <ns> exec $(kubectl -n <ns> get pod -l <selector> -o name \| head -n 1) --` |

TunnelSats logs use the compose service form, which works whether or not `container_name` was stripped: `sudo docker compose -f /home/umbrel/umbrel/app-data/tunnelsats/docker-compose.yml logs --tail 100 tunnelsats-daemon`.

**Error codes**

| `last_error_code` | Source |
|---|---|
| `target_switch_pending` | requested node differs from reconciled node |
| `announcement_conflict` | config audit found problems |
| `announcement_unverified` | live verification not passed |
| `k3s_dns_unprotected` | k3s DNS guard inactive |
| `no_wireguard_config` | daemon: `No WireGuard config found` |
| `no_lightning_node` | daemon: `SecureMode: No Lightning node detected` |
| `dataplane_error` | any other daemon error (raw text kept) |

**Related endpoint changes**

- Secure Mode `configure-node` manual response gains `diagnostics` (from the read-only audit) and pre-filled `gossip_commands`; `gossip_command` uses `lightning_lnd_1`.
- Standard-mode `409` conflict response gains `diagnostics` so the confirmation modal can label each line.
- `ANNOUNCEMENT_CONFLICT_ERROR` drops "real-IP"; `ANNOUNCEMENT_VERIFICATION_ERROR` says "Lightning node" instead of "LND" (it is also used for CLN). Tests compare against the constants.

### 4.2 Dashboard UX

```
Node Routing Status                                   [ACTION NEEDED]
Dataplane Blocked
+--------------------------------------------------------------------+
| Fix your node's announcement settings                              |
| TunnelSats keeps your node's clearnet traffic blocked until it     |
| announces only your TunnelSats endpoint.                           |
| Expected: us3.tunnelsats.com:23217                        [Copy]   |
|                                                                    |
| [TunnelSats server, wrong port]        lnd.conf, line 12 | config  |
|  externalip=178.156.167.202                                        |
|  Announces 178.156.167.202:9735. us3 is your TunnelSats server,    |
|  but 9735 (default port, no port set) is not your forwarded 23217. |
|  How to fix                                                        |
|   1. Open .../lnd.conf in the Umbrel Files app or over SSH [Copy]  |
|   2. Disable line 12: # externalip=178.156.167.202                 |
|   3. Make sure this line exists once: externalhosts=...    [Copy]  |
|   4. Save, restart the Lightning Node app, click Re-check now.     |
|                                                                    |
| [Re-check now] [Fix automatically | Show step-by-step fix] [FAQ]   |
| > Check over SSH (optional): lookup, grep, lncli getinfo, logs     |
+--------------------------------------------------------------------+
```

- **Primary action by mode:** standard mode reuses the existing configure flow (backup, confirmation, live withdrawal) as **Fix automatically**. Secure Mode opens the manual modal, now listing the user's actual offending lines and pre-filled withdrawal commands.
- **Re-check now** re-runs the status check (config audit in Secure Mode, live check in standard mode) with a busy state.
- **Badge:** "Action Needed" (yellow); "Verifying" when only `verification-pending` entries remain.
- **Other codes** get a short plain-language hint, Re-check and a FAQ link. A 503 renders "TunnelSats service unavailable" instead of "Tunnel Down / No Nodes Detected".
- **Safety:** every dynamic value is rendered with `textContent`. No `innerHTML` interpolation.
- **Styling:** semantic `ts-*` classes in `web/css/main.css` for the panel and error box, independent of the stale Tailwind build.
- **Mock scenarios** (`?mock=<scenario>` on localhost only) for visual review.

### 4.3 FAQ

- FAQ 8 becomes "Troubleshooting: Dataplane Blocked (announcement conflicts)" and is visible in both modes: label glossary, where-was-it-found, a no-SSH fix per mode, then optional SSH steps (container lookup, `grep` of active lines, `lncli getinfo`, `--address_remove=ip:port` without pubkey and only after the config fix, compose logs) and the "TunnelSats IP on `:9735`" case.
- FAQ 6 and the modal use `lightning_lnd_1`; no `-it` in piped commands.
- FAQ 3 gains the log commands. `FAQ.md` mirrors the troubleshooting section. Section IDs stay stable for existing deep links.

## 5. Challenging questions

- **If we did nothing?** Support load stays high, users misread a port typo as a leak, and some uninstall or edit the wrong line.
- **Simpler 80%?** Showing raw `announcement_conflicts` plus unhiding FAQ 8 is cheap but leaves users with `lnd:externalip=...` and no fix. The value is in the classification (wrong-port), the exact fix, and Re-check. The rest is additive and contained.
- **Assumptions:** umbrelOS names containers `<app-id>_<service>_1` (Umbrel packaging skill, store promotion strips `container_name`); LND/CLN default to port `9735`; `lncli peers updatenodeannouncement` exists (the daemon already uses it).
- **Graceful failure:** DNS failure degrades to `real-ip`; missing fields fall back to the raw `last_error`; an older backend still renders.

## 6. Blast radius

- **Backend:** `audit_node_announcement_config` (adds `conflict_entries`, existing keys unchanged), `local_status`, `configure_node`, new pure helpers. No persisted state; one in-memory DNS cache.
- **Frontend:** `fetchStatus`, routing card, manual and confirmation modals, `configureNode` error message, mock mode.
- **Docs:** `web/index.html` FAQ, `FAQ.md`, `docs/ISSUE_170_...`.
- **External:** additive JSON only.

## 7. Risks and edge cases

| Risk | Mitigation |
|---|---|
| XSS through config values | `textContent` only; regression test with a `<img onerror>` value. |
| Dangerous copy-paste commands from crafted config | Strict `host:port` validation and `shlex.quote`; no command when invalid. |
| Home IP displayed | Local authenticated dashboard; already in the API. A shareable "support report" waits for automatic redaction (Phase 2). |
| DNS latency in the status path | Lazy, bounded, cached, TunnelSats domain only. |
| LND and CLN both installed | Entries carry `impl`; both configs are audited (unchanged behaviour). |
| IPv6, empty or garbage values | Bracket-aware parsing; `address: null` and no command when unparseable. |
| Slow Re-check in standard mode (LND starting) | Busy state; no auto-poll. |

## 8. TDD cycles

1. **Classifier** (pytest): parsing, default port, every kind, DNS attribution, DNS failure.
2. **Config diagnostics builder** (pytest): line numbers, paths, expected lines, commands per mode, IPv6 quoting, invalid values.
3. **Status integration** (pytest): Secure Mode `config_file` vs standard `live_gossip`, `verification-pending`, error codes, compatibility of existing fields.
4. **configure-node payloads** (pytest): Secure Mode diagnostics and `gossip_commands`, 409 diagnostics.
5. **Dashboard** (Jest): panel per kind/source/mode, Re-check, Fix automatically, 503 state, XSS regression, other codes.
6. **FAQ contracts** (Jest/pytest): FAQ 8 visible in Secure Mode, no `docker exec lnd `, lookup command present, IDs stable.

## 9. Verification

- `pytest server/tests/` and `cd web && npm test` locally, then CI.
- Headless Chrome screenshots of mock scenarios (Secure wrong-port, standard live gossip, verifying, service unavailable).
- Greptile activation gate checked before any review run. Live-node validation is not possible from this workstation (no `.env.local`).

## 10. Rollback

Additive API and UI only, with no migrations or persisted state. Revert the PR.

## 11. Phase 2 (not in this PR)

1. Daemon-side error codes for the most common `LAST_ERROR` strings (there are about 290), replacing the backend prefix mapping.
2. Secure Mode read-only live-gossip check through LND REST `GET /v1/getinfo` with `readonly.macaroon` from the existing read-only mount. Needs a security review.
3. "Copy support report" with automatic redaction of real IPs, replacing the FAQ 3 `curl` for support.
4. Fix the Tailwind pipeline (v3 config versus v4 dependency) and add a CI guard for classes missing from the compiled CSS.
5. Optional gentle auto-recheck in Secure Mode only (the config audit is cheap), with backoff.
