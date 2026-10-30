"""Native Umbrel home screen widget payload builders (Issue #81).

Formats `/api/local/status` telemetry into pixel-bounded payloads for Umbrel's
native `four-stats` (`id: status`) and `text-with-progress` (`id: overview`)
components across Desktop (270x150px, 81px inner cell width) and Mobile
(160x110px, 64px inner cell width).
"""

from datetime import datetime, timezone
from ipaddress import ip_address
import json
import math
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

from nwc import _parse_iso_dt, compute_bandwidth_telemetry, load_nwc_wallet


def _is_ip_literal(host: str) -> bool:
    if not host:
        return False
    cleaned = host.strip().strip("[]")
    try:
        ip_address(cleaned)
        return True
    except ValueError:
        return False


def format_widget_exit_node(
    status_dict: Dict[str, Any],
    server_geodata_fn: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
) -> str:
    """Return concise flag + short server identifier (e.g. '🇺🇸 us3' or '--')."""
    raw_domain = str(status_dict.get("server_domain") or "").strip().lower()
    if not raw_domain:
        return "--"
    if _is_ip_literal(raw_domain):
        return "Custom"

    short_id = raw_domain.split(".")[0].strip()
    if not short_id:
        return "--"

    flag = str(status_dict.get("flag") or "").strip()
    if not flag and server_geodata_fn is not None:
        geo = server_geodata_fn(short_id)
        if isinstance(geo, dict):
            flag = str(geo.get("flag") or "").strip()

    # Cap identifier length so `{flag} {short_id}` never exceeds 81px/64px cells.
    if len(short_id) > 8:
        short_id = short_id[:8]
    return f"{flag} {short_id}".strip() if flag else short_id


def classify_widget_shield(status_dict: Dict[str, Any]) -> Tuple[str, str, str, str]:
    """Return `(state_key, shield_title, badge_text, node_short)` with unified fail-closed semantics.

    Unified status vocabulary across Standard (`secure_mode=false`) and Official
    Store Secure Mode (`secure_mode=true`):
    - `🟢 Active`: VPN connected, target node routing configured, and all
      dataplane + announcement/config verification gates green.
    - `🟠 Conflict`: Announcement conflict or unverified clearnet/config state.
    - `🟠 Blocked`: Dataplane fail-closed gate active due to operational error.
    - `🟠 Setup`: WireGuard connected, waiting for Lightning node configuration.
    - `🟡 Syncing`: Target switch, wallet unlock, or initial dataplane reconciliation pending.
    - `🟡 Pending`: WireGuard handshake pending.
    - `🟡 Starting`: Daemon socket temporarily starting/restarting.
    - `🔴 Node Off`: WireGuard connected, protected Lightning node is stopped.
    - `🔴 Offline`: WireGuard tunnel disconnected or unconfigured.
    """
    target_impl = str(status_dict.get("target_impl") or "").strip().lower()
    lnd_detected = bool(status_dict.get("lnd_detected"))
    cln_detected = bool(status_dict.get("cln_detected"))
    lnd_routing = bool(status_dict.get("lnd_routing_active"))
    cln_routing = bool(status_dict.get("cln_routing_active"))
    configs_found = status_dict.get("configs_found") or []
    vpn_active = bool(status_dict.get("vpn_active"))

    if target_impl == "cln" and (cln_detected or cln_routing):
        node_short = "CLN"
        shield_title = "CLN Shield"
    elif target_impl == "lnd" and (lnd_detected or lnd_routing):
        node_short = "LND"
        shield_title = "LND Shield"
    elif target_impl in ("lnd", "cln") and (configs_found or vpn_active or status_dict.get("server_domain")):
        node_short = target_impl.upper()
        shield_title = f"{node_short} Shield"
    else:
        node_short = ""
        shield_title = "Tunnel"

    if status_dict.get("_degraded_daemon_unavailable"):
        return ("starting", shield_title, "🟡 Starting", node_short)

    active_detected = (
        cln_detected if target_impl == "cln" else (lnd_detected if target_impl == "lnd" else False)
    )
    active_routing = (
        cln_routing if target_impl == "cln" else (lnd_routing if target_impl == "lnd" else False)
    )
    rules_synced = bool(status_dict.get("rules_synced"))
    decommissioned = bool(status_dict.get("decommissioned"))
    conflicts = status_dict.get("announcement_conflicts") or []
    verification_required = bool(status_dict.get("announcement_verification_required"))
    announcement_verified = status_dict.get("announcement_verified")
    last_error_code = str(status_dict.get("last_error_code") or "").strip()
    diagnostics = status_dict.get("announcement_diagnostics")
    only_verification_pending = bool(
        isinstance(diagnostics, list)
        and diagnostics
        and not conflicts
        and all(
            isinstance(d, dict) and d.get("kind") == "verification-pending"
            for d in diagnostics
        )
    )

    if (
        vpn_active
        and active_detected
        and active_routing
        and rules_synced
        and not decommissioned
        and not conflicts
        and (not verification_required or announcement_verified is True)
    ):
        return ("active", shield_title, "🟢 Active", node_short)

    if only_verification_pending and vpn_active:
        return ("syncing", shield_title, "🟡 Syncing", node_short)

    if (vpn_active or active_routing) and (
        bool(conflicts) or (verification_required and announcement_verified is False)
    ):
        return ("conflict", shield_title, "🟠 Conflict", node_short)

    if vpn_active and status_dict.get("target_switch_pending"):
        return ("syncing", shield_title, "🟡 Syncing", node_short)

    if (
        vpn_active
        and not active_detected
        and (active_routing or last_error_code == "protected_node_not_running")
    ):
        return ("node_off", shield_title, "🔴 Node Off", node_short)

    if vpn_active and decommissioned:
        return ("setup", shield_title, "🟠 Setup", node_short)

    if vpn_active and active_routing and not rules_synced:
        if not status_dict.get("last_reconcile_at"):
            return ("syncing", shield_title, "🟡 Syncing", node_short)
        return ("blocked", shield_title, "🟠 Blocked", node_short)

    if vpn_active and not active_routing:
        return ("setup", shield_title, "🟠 Setup", node_short)

    wg_status = str(status_dict.get("wg_status") or "")
    if "Handshake" in wg_status or "Pending" in wg_status:
        return ("pending", shield_title, "🟡 Pending", node_short)

    return ("offline", shield_title, "🔴 Offline", node_short)


def _has_real_bandwidth_telemetry(status_dict: Dict[str, Any]) -> bool:
    tel = status_dict.get("subscription_telemetry")
    if isinstance(tel, dict):
        if tel.get("last_synced_at"):
            return True
        sub_status = str(tel.get("subscription_status") or "unknown").lower()
        if sub_status not in ("", "unknown"):
            return True
        try:
            if float(tel.get("bandwidth_used_gb") or 0.0) > 0.0:
                return True
        except (TypeError, ValueError):
            pass
    if status_dict.get("last_synced_at"):
        return True
    try:
        return float(status_dict.get("bandwidth_used_gb") or 0.0) > 0.0
    except (TypeError, ValueError):
        return False


def _format_bandwidth(status_dict: Dict[str, Any]) -> Tuple[str, str, str, float]:
    """Return `(cell_text, cell_subtext, progress_label, progress_float)`."""
    if not _has_real_bandwidth_telemetry(status_dict):
        has_sub = bool(
            (status_dict.get("configs_found") or [])
            or status_dict.get("expires_at")
            or status_dict.get("server_domain")
        )
        label = (
            "Syncing bandwidth telemetry..."
            if has_sub
            else "Open TunnelSats to get started"
        )
        return ("--", "", label, 0.0)

    tel = status_dict.get("subscription_telemetry")
    if not isinstance(tel, dict):
        tel = {}

    try:
        used_gb = max(
            0.0,
            float(
                status_dict.get(
                    "bandwidth_used_gb", tel.get("bandwidth_used_gb", 0.0)
                )
                or 0.0
            ),
        )
    except (TypeError, ValueError):
        used_gb = 0.0

    try:
        limit_gb = float(
            status_dict.get(
                "bandwidth_limit_gb", tel.get("bandwidth_limit_gb", 100.0)
            )
            or 100.0
        )
    except (TypeError, ValueError):
        limit_gb = 100.0
    if limit_gb <= 0.0:
        limit_gb = 100.0

    used_str = f"{used_gb:.1f}" if used_gb < 100.0 else f"{int(round(used_gb))}"
    limit_str = (
        f"{int(limit_gb)}"
        if math.isclose(limit_gb, round(limit_gb))
        else f"{limit_gb:.1f}"
    )
    raw_ratio = used_gb / limit_gb
    pct_int = max(0, int(round(raw_ratio * 100.0)))
    progress_float = max(0.0, min(1.0, round(raw_ratio, 4)))

    return (
        used_str,
        "GB",
        f"{used_str} / {limit_str} GB ({pct_int}%)",
        progress_float,
    )


def _format_expiry(
    status_dict: Dict[str, Any],
    shield_state: str,
    now_dt: Optional[datetime] = None,
) -> Tuple[str, str, str, str, str]:
    """Return `(cell_title, cell_text, cell_subtext, hero_text, hero_subtext)`."""
    now = now_dt.astimezone(timezone.utc) if now_dt else datetime.now(timezone.utc)
    nwc_info = status_dict.get("nwc") if isinstance(status_dict.get("nwc"), dict) else {}
    nwc_connected = bool(nwc_info.get("connected"))
    nwc_healthy = bool(nwc_connected and nwc_info.get("healthy"))

    expiry_dt = _parse_iso_dt(status_dict.get("expires_at"))
    if expiry_dt is not None:
        sec_rem = (expiry_dt - now).total_seconds()
        if sec_rem <= 0:
            return ("Expires ⚠️", "Expired", "", "Expired", "· ⚠️ Renew")

        if sec_rem < 82800:  # < 23 hours -> display hours remaining
            hours_left = max(1, min(23, int(math.ceil(sec_rem / 3600.0))))
            unit = "hr" if hours_left == 1 else "hrs"
            if nwc_healthy:
                return (
                    "Expires ⚡",
                    str(hours_left),
                    unit,
                    f"{hours_left} {unit}",
                    "left · ⚡ Auto",
                )
            return (
                "Expires ⚠️",
                str(hours_left),
                unit,
                f"{hours_left} {unit}",
                "left · ⚠️ NWC" if nwc_connected else "left · ⚠️ Renew",
            )

        days_left = max(1, int(round(sec_rem / 86400.0)))
        unit = "day" if days_left == 1 else "days"
        if nwc_healthy:
            return (
                "Expires ⚡",
                str(days_left),
                unit,
                f"{days_left} {unit}",
                "left · ⚡ Auto",
            )
        if nwc_connected and not nwc_healthy:
            return (
                "Expires ⚠️",
                str(days_left),
                unit,
                f"{days_left} {unit}",
                "left · ⚠️ NWC",
            )
        if sec_rem <= 3 * 86400:
            return (
                "Expires ⚠️",
                str(days_left),
                unit,
                f"{days_left} {unit}",
                "left · ⚠️ Renew",
            )
        return (
            "Expires",
            str(days_left),
            unit,
            f"{days_left} {unit}",
            "remaining",
        )

    has_config = bool(
        (status_dict.get("configs_found") or []) or status_dict.get("vpn_active")
    )
    if has_config:
        title = "Expires ⚡" if nwc_healthy else ("Expires ⚠️" if nwc_connected else "Expires")
        hero_map = {
            "active": "Active",
            "node_off": "Node Off",
            "setup": "Finish Setup",
            "syncing": "Syncing",
            "pending": "Connecting",
            "starting": "Starting",
            "offline": "Offline",
        }
        hero_text = hero_map.get(shield_state, "Configured")
        hero_sub = "· ⚡ Auto" if nwc_healthy else ("· ⚠️ NWC" if nwc_connected else "")
        return (title, "--", "", hero_text, hero_sub)

    return ("Plan", "No Sub", "", "No Sub", "· Setup")


def build_widget_status_payload(
    status_dict: Dict[str, Any],
    *,
    refresh: str = "15s",
    now_dt: Optional[datetime] = None,
    server_geodata_fn: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """Build Umbrel `four-stats` widget JSON (`id: status`)."""
    state_key, shield_title, badge_text, _ = classify_widget_shield(status_dict)
    exit_node = format_widget_exit_node(status_dict, server_geodata_fn=server_geodata_fn)

    if state_key in ("conflict", "blocked"):
        action_text = "Fix Config" if state_key == "conflict" else "Check App"
        items: List[Dict[str, str]] = [
            {"title": shield_title, "text": badge_text, "subtext": ""},
            {"title": "Exit Node", "text": exit_node, "subtext": ""},
            {"title": "Routing", "text": "Blocked", "subtext": ""},
            {"title": "Action", "text": action_text, "subtext": ""},
        ]
    else:
        bw_text, bw_sub, _, _ = _format_bandwidth(status_dict)
        exp_title, exp_text, exp_sub, _, _ = _format_expiry(
            status_dict, state_key, now_dt=now_dt
        )
        items = [
            {"title": shield_title, "text": badge_text, "subtext": ""},
            {"title": "Exit Node", "text": exit_node, "subtext": ""},
            {"title": "Data Used", "text": bw_text, "subtext": bw_sub},
            {"title": exp_title, "text": exp_text, "subtext": exp_sub},
        ]

    return {
        "type": "four-stats",
        "refresh": str(refresh),
        "link": "",
        "items": items,
    }


def build_widget_overview_payload(
    status_dict: Dict[str, Any],
    *,
    refresh: str = "15s",
    now_dt: Optional[datetime] = None,
    server_geodata_fn: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """Build Umbrel `text-with-progress` widget JSON (`id: overview`)."""
    state_key, _, badge_text, node_short = classify_widget_shield(status_dict)
    exit_node = format_widget_exit_node(status_dict, server_geodata_fn=server_geodata_fn)

    header_parts = [badge_text]
    if node_short:
        header_parts.append(node_short)
    if exit_node != "--":
        header_parts.append(exit_node)
    if len(header_parts) == 1:
        header_parts.append("TunnelSats")
    header_title = " · ".join(header_parts)

    if state_key in ("conflict", "blocked"):
        hero_text = "Fix Config" if state_key == "conflict" else "Check App"
        progress_label = (
            "Clearnet gossip leak prevented"
            if state_key == "conflict"
            else "Dataplane fail-closed active"
        )
        return {
            "type": "text-with-progress",
            "refresh": str(refresh),
            "link": "",
            "title": header_title,
            "text": hero_text,
            "subtext": "· Blocked",
            "progressLabel": progress_label,
            "progress": 0.0,
        }

    _, _, progress_label, progress_float = _format_bandwidth(status_dict)
    _, _, _, hero_text, hero_subtext = _format_expiry(
        status_dict, state_key, now_dt=now_dt
    )
    return {
        "type": "text-with-progress",
        "refresh": str(refresh),
        "link": "",
        "title": header_title,
        "text": hero_text,
        "subtext": hero_subtext,
        "progressLabel": progress_label,
        "progress": progress_float,
    }


def build_degraded_status_dict(
    data_dir: str,
    *,
    meta_file: str = "tunnelsats-meta.json",
    metadata_lock: Any = None,
    server_geodata_fn: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """Build a safe fallback status dict from `/data` when `tunnelsats-daemon` is starting."""
    configs: List[str] = []
    if os.path.isdir(data_dir):
        try:
            configs = [f for f in os.listdir(data_dir) if f.endswith(".conf")]
        except OSError:
            configs = []

    meta_data: Dict[str, Any] = {}
    meta_path = os.path.join(data_dir, meta_file)

    def _read_meta() -> Dict[str, Any]:
        try:
            with open(meta_path, "r", encoding="utf-8") as fp:
                loaded = json.load(fp)
                return loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    if metadata_lock is not None:
        with metadata_lock:
            meta_data = _read_meta()
    else:
        meta_data = _read_meta()

    server_domain = str(meta_data.get("serverDomain") or "").strip()
    expires_at = str(meta_data.get("expiresAt") or "").strip()
    requested_node = str(meta_data.get("nodeType") or "").strip().lower()
    target_impl = requested_node if requested_node in ("lnd", "cln") else ""

    flag = None
    if server_domain and server_geodata_fn is not None:
        s_id = server_domain.split(".")[0]
        geo = server_geodata_fn(s_id)
        if isinstance(geo, dict):
            flag = geo.get("flag")

    telemetry_src = (
        meta_data.get("subscriptionTelemetry")
        if isinstance(meta_data.get("subscriptionTelemetry"), dict)
        else meta_data
    )
    telemetry = compute_bandwidth_telemetry(telemetry_src)

    wallet_on_disk = load_nwc_wallet(data_dir)
    nwc_connected = bool(meta_data.get("nwcConnected") and wallet_on_disk)
    nwc_state_dict = (
        meta_data.get("nwcAutoRenewState")
        if isinstance(meta_data.get("nwcAutoRenewState"), dict)
        else {}
    )
    nwc_healthy = bool(
        nwc_connected
        and not nwc_state_dict.get("budgetWarning")
        and not nwc_state_dict.get("fallbackTaskRaised")
        and not nwc_state_dict.get("restoreReconnectNeeded")
        and not nwc_state_dict.get("lastError")
    )

    return {
        "_degraded_daemon_unavailable": True,
        "wg_status": "Starting",
        "vpn_active": False,
        "configs_found": configs,
        "server_domain": server_domain,
        "flag": flag,
        "expires_at": expires_at,
        "target_impl": target_impl,
        "lnd_detected": target_impl == "lnd",
        "cln_detected": target_impl == "cln",
        "lnd_routing_active": False,
        "cln_routing_active": False,
        "rules_synced": False,
        "decommissioned": False,
        "announcement_conflicts": [],
        "announcement_verification_required": False,
        "announcement_verified": None,
        "subscription_telemetry": telemetry,
        "bandwidth_used_gb": telemetry["bandwidth_used_gb"],
        "bandwidth_limit_gb": telemetry["bandwidth_limit_gb"],
        "last_synced_at": telemetry["last_synced_at"],
        "nwc": {
            "connected": nwc_connected,
            "healthy": nwc_healthy,
        },
    }
