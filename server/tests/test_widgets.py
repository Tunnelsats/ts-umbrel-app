"""Integration tests for Native Umbrel Widgets (Issue #81)."""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app as app_module
from app import app
from nwc import _nostr_pubkey_from_secret, save_nwc_wallet


VALID_SECRET = "22" * 32
VALID_WALLET_PRIV = "33" * 32
VALID_WALLET_PUB = _nostr_pubkey_from_secret(VALID_WALLET_PRIV)
VALID_NWC_URI = (
    f"nostr+walletconnect://{VALID_WALLET_PUB}"
    f"?relay=wss://relay.getalby.com/v1&secret={VALID_SECRET}"
)


@pytest.fixture
def env_setup(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    lnd_conf = tmp_path / "lnd.conf"
    cln_conf = tmp_path / "cln_config"

    monkeypatch.setattr(app_module, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(app_module, "LND_CONFIG_PATH", str(lnd_conf))
    monkeypatch.setattr(app_module, "CLN_CONFIG_PATH", str(cln_conf))
    app_module._SUBSCRIPTION_CACHE.clear()
    app_module._next_subscription_sync_time.clear()
    if hasattr(app_module, "_invalidate_widget_status_cache"):
        app_module._invalidate_widget_status_cache()

    return {
        "data_dir": data_dir,
        "lnd_conf": lnd_conf,
        "cln_conf": cln_conf,
    }


@pytest.fixture
def client(env_setup):
    app.config["TESTING"] = True
    app.config["MANAGEMENT_BROWSER_SECURITY_ENABLED"] = False
    with app.test_client() as test_client:
        yield test_client


def _seed_healthy_state(env_setup, *, node_type="lnd", nwc=True, days=28, used_gb=14.2, limit_gb=90.0, server="us3.tunnelsats.com"):
    data_dir = env_setup["data_dir"]
    (data_dir / "tunnelsatsv2.conf").write_text("[Interface]\nPrivateKey = AAAA\n", encoding="utf-8")

    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(days=days)).isoformat()
    meta = {
        "serverDomain": server,
        "vpnPort": 36789,
        "expiresAt": expires_at,
        "nodeType": node_type,
        "nwcConnected": bool(nwc),
        "lndAnnouncementVerification": {
            "endpoint": f"{server}:36789",
            "verified": True,
            "checkedAt": now.isoformat(),
        },
        "clnAnnouncementVerification": {
            "endpoint": f"{server}:36789",
            "verified": True,
            "checkedAt": now.isoformat(),
        },
        "subscriptionTelemetry": {
            "bandwidthUsedGb": used_gb,
            "bandwidthLimitGb": limit_gb,
            "status": "active",
            "lastSyncedAt": now.isoformat(),
        },
    }
    (data_dir / "tunnelsats-meta.json").write_text(json.dumps(meta), encoding="utf-8")
    if nwc:
        save_nwc_wallet(str(data_dir), VALID_NWC_URI, auto_renew_duration="match", route_via_tor=False)

    if node_type == "lnd":
        env_setup["lnd_conf"].write_text(f"externalhosts={server}:36789\n", encoding="utf-8")
    else:
        env_setup["cln_conf"].write_text(f"announce-addr={server}:36789\n", encoding="utf-8")


def test_widgets_healthy_active_with_nwc_standard_and_secure_modes(client, env_setup, monkeypatch):
    _seed_healthy_state(env_setup, node_type="lnd", nwc=True, days=28, used_gb=14.2, limit_gb=90.0)

    dataplane_ok = {
        "dataplane_mode": "docker",
        "target_container": "lightning_lnd_1",
        "target_ip": "10.21.21.9",
        "target_impl": "lnd",
        "docker_network": "umbrel_main_network",
        "forwarding_port": 36789,
        "rules_synced": True,
        "ipv4_rules_synced": True,
        "ipv6_rules_synced": True,
        "dns_protected": True,
        "decommissioned": False,
        "last_reconcile_at": datetime.now(timezone.utc).isoformat(),
        "last_error": None,
    }

    for secure_mode in (False, True):
        monkeypatch.setattr(app_module, "SECURE_MODE", secure_mode)
        with (
            patch.object(app_module, "_get_wireguard_state", return_value=("Connected", "pub123")),
            patch.object(app_module, "_trigger_lazy_subscription_sync"),
            patch.object(app_module, "read_dataplane_state", return_value=dataplane_ok),
            patch.object(app_module, "lnd_exists", return_value=True),
            patch.object(app_module, "cln_exists", return_value=False),
            patch.object(
                app_module,
                "list_containers",
                return_value=[{"Id": "abc", "Names": ["/lightning_lnd_1"], "NetworkSettings": {"Networks": {"main": {"IPAddress": "10.21.21.9"}}}}],
            ),
        ):
            status_res = client.get("/api/local/widgets/status")
            assert status_res.status_code == 200
            status_body = status_res.get_json()
            assert status_body["type"] == "four-stats"
            assert status_body["refresh"] == "15s"
            assert status_body["link"] == ""
            assert len(status_body["items"]) == 4
            assert status_body["items"][0] == {"title": "LND Shield", "text": "🟢 Active", "subtext": ""}
            assert status_body["items"][1] == {"title": "Exit Node", "text": "🇺🇸 us3", "subtext": ""}
            assert status_body["items"][2] == {"title": "Data Used", "text": "14.2", "subtext": "GB"}
            assert status_body["items"][3] == {"title": "Expires ⚡", "text": "28", "subtext": "days"}

            # Verify anti-cramming bound: every cell value+subtext stays concise (<= 12 chars)
            for cell in status_body["items"]:
                rendered = f"{cell['text']} {cell['subtext']}".strip()
                assert len(rendered) <= 12, f"Cell too wide for 81px box: {rendered!r}"

            overview_res = client.get("/api/local/widgets/overview")
            assert overview_res.status_code == 200
            overview_body = overview_res.get_json()
            assert overview_body["type"] == "text-with-progress"
            assert overview_body["refresh"] == "15s"
            assert overview_body["link"] == ""
            assert overview_body["title"] == "🟢 Active · LND · 🇺🇸 us3"
            assert overview_body["text"] == "28 days"
            assert overview_body["subtext"] == "left · ⚡ Auto"
            assert overview_body["progressLabel"] == "14.2 / 90 GB (16%)"
            assert overview_body["progress"] == pytest.approx(0.1578, abs=0.01)


def test_manifest_declares_both_umbrel_widgets_and_survives_promotion(tmp_path):
    import shutil
    import subprocess
    from pathlib import Path
    import yaml

    repo_root = Path(__file__).resolve().parents[2]
    manifest_path = repo_root / "tunnelsats" / "umbrel-app.yml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))

    assert isinstance(manifest.get("widgets"), list), "Expected `widgets` list in umbrel-app.yml"
    by_id = {w["id"]: w for w in manifest["widgets"]}
    assert set(by_id.keys()) == {"status", "overview"}

    status_w = by_id["status"]
    assert status_w["type"] == "four-stats"
    assert status_w["refresh"] == "15s"
    assert status_w["endpoint"] == "tunnelsats-web:9740/api/local/widgets/status"
    assert status_w["link"] == ""
    assert status_w["example"]["type"] == "four-stats"
    assert len(status_w["example"]["items"]) == 4

    overview_w = by_id["overview"]
    assert overview_w["type"] == "text-with-progress"
    assert overview_w["refresh"] == "15s"
    assert overview_w["endpoint"] == "tunnelsats-web:9740/api/local/widgets/overview"
    assert overview_w["link"] == ""
    assert overview_w["example"]["type"] == "text-with-progress"
    assert 0.0 <= float(overview_w["example"]["progress"]) <= 1.0

    # Verify sync.sh promote preserves widgets identically for the Official Umbrel App Store
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    shutil.copy2(repo_root / "scripts" / "sync.sh", project / "scripts" / "sync.sh")
    shutil.copytree(repo_root / "tunnelsats", project / "tunnelsats")

    store = tmp_path / "umbrel-apps"
    store.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        "#!/bin/sh\nprintf '%s\\n' 'Name: test' "
        "'Digest: sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "UMBREL_APPS_DIR": str(store),
            "SUBMISSION_URL": "https://github.com/getumbrel/umbrel-apps/pull/4919",
        }
    )
    res = subprocess.run(
        ["bash", str(project / "scripts" / "sync.sh"), "promote"],
        cwd=project,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    promoted = yaml.safe_load((store / "tunnelsats" / "umbrel-app.yml").read_text(encoding="utf-8"))
    assert promoted["widgets"] == manifest["widgets"]


def test_widgets_cln_manual_and_expiry_countdown_phases(client, env_setup, monkeypatch):
    monkeypatch.setattr(app_module, "SECURE_MODE", True)
    dataplane_cln = {
        "dataplane_mode": "docker",
        "target_container": "core-lightning_lightningd_1",
        "target_ip": "10.21.21.96",
        "target_impl": "cln",
        "docker_network": "umbrel_main_network",
        "forwarding_port": 36789,
        "rules_synced": True,
        "ipv4_rules_synced": True,
        "ipv6_rules_synced": True,
        "dns_protected": True,
        "decommissioned": False,
        "last_reconcile_at": datetime.now(timezone.utc).isoformat(),
        "last_error": None,
    }

    with (
        patch.object(app_module, "_get_wireguard_state", return_value=("Connected", "pub123")),
        patch.object(app_module, "_trigger_lazy_subscription_sync"),
        patch.object(app_module, "read_dataplane_state", return_value=dataplane_cln),
        patch.object(app_module, "lnd_exists", return_value=False),
        patch.object(app_module, "cln_exists", return_value=True),
    ):
        # 1. Healthy manual CLN subscription (12 days, Switzerland ch1)
        _seed_healthy_state(
            env_setup, node_type="cln", nwc=False, days=12, used_gb=61.5, limit_gb=90.0, server="ch1.tunnelsats.com"
        )
        s_body = client.get("/api/local/widgets/status").get_json()
        assert s_body["items"][0] == {"title": "CLN Shield", "text": "🟢 Active", "subtext": ""}
        assert s_body["items"][1] == {"title": "Exit Node", "text": "🇨🇭 ch1", "subtext": ""}
        assert s_body["items"][2] == {"title": "Data Used", "text": "61.5", "subtext": "GB"}
        assert s_body["items"][3] == {"title": "Expires", "text": "12", "subtext": "days"}

        o_body = client.get("/api/local/widgets/overview").get_json()
        assert o_body["title"] == "🟢 Active · CLN · 🇨🇭 ch1"
        assert o_body["text"] == "12 days"
        assert o_body["subtext"] == "remaining"
        assert o_body["progressLabel"] == "61.5 / 90 GB (68%)"

        # 2. Expiring soon (2 days remaining, manual) -> warning indicator
        _seed_healthy_state(
            env_setup, node_type="cln", nwc=False, days=2, used_gb=88.4, limit_gb=90.0, server="ch1.tunnelsats.com"
        )
        s_soon = client.get("/api/local/widgets/status").get_json()
        assert s_soon["items"][3] == {"title": "Expires ⚠️", "text": "2", "subtext": "days"}
        o_soon = client.get("/api/local/widgets/overview").get_json()
        assert o_soon["text"] == "2 days"
        assert o_soon["subtext"] == "left · ⚠️ Renew"

        # 3. Sub-day hours remaining (6 hours) and Expired (-2 hours)
        meta_path = env_setup["data_dir"] / "tunnelsats-meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["expiresAt"] = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=40)).isoformat()
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        s_hrs = client.get("/api/local/widgets/status").get_json()
        assert s_hrs["items"][3] == {"title": "Expires ⚠️", "text": "6", "subtext": "hrs"}

        meta["expiresAt"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        s_exp = client.get("/api/local/widgets/status").get_json()
        assert s_exp["items"][3] == {"title": "Expires ⚠️", "text": "Expired", "subtext": ""}
        o_exp = client.get("/api/local/widgets/overview").get_json()
        assert o_exp["text"] == "Expired"
        assert o_exp["subtext"] == "· ⚠️ Renew"

        # 4. NWC connected but unhealthy (budgetWarning) must NOT claim ⚡ Auto
        _seed_healthy_state(
            env_setup, node_type="cln", nwc=True, days=5, used_gb=10.0, limit_gb=90.0, server="ch1.tunnelsats.com"
        )
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["nwcAutoRenewState"] = {"budgetWarning": True}
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        s_nwc_warn = client.get("/api/local/widgets/status").get_json()
        assert s_nwc_warn["items"][3] == {"title": "Expires ⚠️", "text": "5", "subtext": "days"}
        o_nwc_warn = client.get("/api/local/widgets/overview").get_json()
        assert o_nwc_warn["subtext"] == "left · ⚠️ NWC"


def test_widgets_fail_closed_conflict_blocked_setup_and_fresh_install(client, env_setup, monkeypatch):
    monkeypatch.setattr(app_module, "SECURE_MODE", True)

    # 1. Fresh unconfigured install (no .conf, no meta)
    dataplane_idle = {
        "dataplane_mode": "docker",
        "target_container": None,
        "target_ip": None,
        "target_impl": None,
        "docker_network": None,
        "forwarding_port": None,
        "rules_synced": False,
        "dns_protected": False,
        "decommissioned": False,
        "last_reconcile_at": None,
        "last_error": None,
    }
    with (
        patch.object(app_module, "_get_wireguard_state", return_value=("Not Configured", None)),
        patch.object(app_module, "read_dataplane_state", return_value=dataplane_idle),
        patch.object(app_module, "lnd_exists", return_value=False),
        patch.object(app_module, "cln_exists", return_value=False),
    ):
        s_fresh = client.get("/api/local/widgets/status").get_json()
        assert s_fresh["items"] == [
            {"title": "Tunnel", "text": "🔴 Offline", "subtext": ""},
            {"title": "Exit Node", "text": "--", "subtext": ""},
            {"title": "Data Used", "text": "--", "subtext": ""},
            {"title": "Plan", "text": "No Sub", "subtext": ""},
        ]
        o_fresh = client.get("/api/local/widgets/overview").get_json()
        assert o_fresh["title"] == "🔴 Offline · TunnelSats"
        assert o_fresh["text"] == "No Sub"
        assert o_fresh["progressLabel"] == "Open app to get started"
        assert len(o_fresh["progressLabel"]) <= 24
        assert o_fresh["progress"] == 0.0

    # 2. Secure Mode announcement conflict (clearnet externalip in lnd.conf) -> 🟠 Conflict
    _seed_healthy_state(
        env_setup, node_type="lnd", nwc=True, days=20, used_gb=12.0, limit_gb=90.0, server="de2.tunnelsats.com"
    )
    env_setup["lnd_conf"].write_text(
        "externalhosts=de2.tunnelsats.com:36789\nexternalip=203.0.113.42:9735\n",
        encoding="utf-8",
    )
    dataplane_ok = {
        "dataplane_mode": "docker",
        "target_container": "lightning_lnd_1",
        "target_ip": "10.21.21.9",
        "target_impl": "lnd",
        "docker_network": "umbrel_main_network",
        "forwarding_port": 36789,
        "rules_synced": True,
        "dns_protected": True,
        "decommissioned": False,
        "last_reconcile_at": datetime.now(timezone.utc).isoformat(),
        "last_error": None,
    }
    with (
        patch.object(app_module, "_get_wireguard_state", return_value=("Connected", "pub123")),
        patch.object(app_module, "_trigger_lazy_subscription_sync"),
        patch.object(app_module, "read_dataplane_state", return_value=dataplane_ok),
        patch.object(app_module, "lnd_exists", return_value=True),
        patch.object(app_module, "cln_exists", return_value=False),
    ):
        s_conf = client.get("/api/local/widgets/status").get_json()
        assert s_conf["items"] == [
            {"title": "LND Shield", "text": "🟠 Conflict", "subtext": ""},
            {"title": "Exit Node", "text": "🇩🇪 de2", "subtext": ""},
            {"title": "Routing", "text": "Blocked", "subtext": ""},
            {"title": "Action", "text": "Fix Config", "subtext": ""},
        ]
        o_conf = client.get("/api/local/widgets/overview").get_json()
        assert o_conf["title"] == "🟠 Conflict · LND · 🇩🇪 de2"
        assert o_conf["text"] == "Fix Config"
        assert o_conf["subtext"] == "· Blocked"
        assert o_conf["progressLabel"] == "Clearnet leak prevented"
        assert len(o_conf["progressLabel"]) <= 24
        assert o_conf["progress"] == 0.0

    # 3. Tunnel up, but LND externalhosts not configured yet -> 🟠 Setup (never false 🟢 Active)
    env_setup["lnd_conf"].write_text("# no externalhosts yet\n", encoding="utf-8")
    with (
        patch.object(app_module, "_get_wireguard_state", return_value=("Connected", "pub123")),
        patch.object(app_module, "_trigger_lazy_subscription_sync"),
        patch.object(app_module, "read_dataplane_state", return_value=dataplane_ok),
        patch.object(app_module, "lnd_exists", return_value=True),
        patch.object(app_module, "cln_exists", return_value=False),
    ):
        s_setup = client.get("/api/local/widgets/status").get_json()
        assert s_setup["items"][0] == {"title": "LND Shield", "text": "🟠 Setup", "subtext": ""}

    # 4. Dataplane error (rules_synced=False, last_error set) -> 🟠 Blocked
    env_setup["lnd_conf"].write_text("externalhosts=de2.tunnelsats.com:36789\n", encoding="utf-8")
    dataplane_err = {**dataplane_ok, "rules_synced": False, "last_error": "iptables rule sync failed"}
    with (
        patch.object(app_module, "_get_wireguard_state", return_value=("Connected", "pub123")),
        patch.object(app_module, "_trigger_lazy_subscription_sync"),
        patch.object(app_module, "read_dataplane_state", return_value=dataplane_err),
        patch.object(app_module, "lnd_exists", return_value=True),
        patch.object(app_module, "cln_exists", return_value=False),
    ):
        s_blk = client.get("/api/local/widgets/status").get_json()
        assert s_blk["items"][0] == {"title": "LND Shield", "text": "🟠 Blocked", "subtext": ""}
        assert s_blk["items"][2] == {"title": "Routing", "text": "Blocked", "subtext": ""}
        assert s_blk["items"][3] == {"title": "Action", "text": "Check App", "subtext": ""}
        o_blk = client.get("/api/local/widgets/overview").get_json()
        assert o_blk["progressLabel"] == "Fail-closed block active"
        assert len(o_blk["progressLabel"]) <= 24

    # 5. Configured subscription before first telemetry sync -> concise <= 24-char label on 160px mobile
    meta_path = env_setup["data_dir"] / "tunnelsats-meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.pop("subscriptionTelemetry", None)
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    with (
        patch.object(app_module, "_get_wireguard_state", return_value=("Connected", "pub123")),
        patch.object(app_module, "_trigger_lazy_subscription_sync"),
        patch.object(app_module, "read_dataplane_state", return_value=dataplane_ok),
        patch.object(app_module, "lnd_exists", return_value=True),
        patch.object(app_module, "cln_exists", return_value=False),
    ):
        o_syncing = client.get("/api/local/widgets/overview").get_json()
        assert o_syncing["progressLabel"] == "Syncing bandwidth..."
        assert len(o_syncing["progressLabel"]) <= 24


def test_split_runtime_web_role_serves_degraded_widget_when_daemon_unavailable(client, env_setup, monkeypatch):
    _seed_healthy_state(env_setup, node_type="lnd", nwc=True, days=28, used_gb=14.2, limit_gb=90.0)
    monkeypatch.setattr(app_module, "TUNNELSATS_ROLE", "web")

    with patch.object(
        app_module,
        "request_over_unix_socket",
        side_effect=app_module.DaemonUnavailable("socket not ready"),
    ):
        # Non-widget privileged status still fails closed with 503
        raw_status = client.get("/api/local/status")
        assert raw_status.status_code == 503

        # Widget endpoints degrade gracefully with 200 OK and 5s fast retry
        w_status = client.get("/api/local/widgets/status")
        assert w_status.status_code == 200
        s_json = w_status.get_json()
        assert s_json["refresh"] == "5s"
        assert s_json["items"][0] == {"title": "LND Shield", "text": "🟡 Starting", "subtext": ""}
        assert s_json["items"][1] == {"title": "Exit Node", "text": "🇺🇸 us3", "subtext": ""}
        assert s_json["items"][2] == {"title": "Data Used", "text": "14.2", "subtext": "GB"}
        assert s_json["items"][3] == {"title": "Expires ⚡", "text": "28", "subtext": "days"}

        w_overview = client.get("/api/local/widgets/overview")
        assert w_overview.status_code == 200
        o_json = w_overview.get_json()
        assert o_json["refresh"] == "5s"
        assert o_json["title"] == "🟡 Starting · LND · 🇺🇸 us3"
        assert o_json["text"] == "28 days"
        assert o_json["progressLabel"] == "14.2 / 90 GB (16%)"


def test_umbreld_bridge_gateway_peer_trusted_while_adjacent_container_rejected(env_setup, monkeypatch):
    _seed_healthy_state(env_setup, node_type="lnd", nwc=True, days=28, used_gb=14.2, limit_gb=90.0)
    monkeypatch.setattr(app_module, "SECURE_MODE", True)

    dataplane_ok = {
        "dataplane_mode": "docker",
        "target_container": "lightning_lnd_1",
        "target_ip": "10.21.21.9",
        "target_impl": "lnd",
        "docker_network": "umbrel_main_network",
        "forwarding_port": 36789,
        "rules_synced": True,
        "dns_protected": True,
        "decommissioned": False,
        "last_reconcile_at": datetime.now(timezone.utc).isoformat(),
        "last_error": None,
    }

    app.config["TESTING"] = True
    app.config["MANAGEMENT_BROWSER_SECURITY_ENABLED"] = True
    try:
        with (
            app.test_client() as sec_client,
            patch.object(app_module.MANAGEMENT_SECURITY, "default_gateway_ip", return_value="10.21.0.1"),
            patch.object(app_module, "_get_wireguard_state", return_value=("Connected", "pub123")),
            patch.object(app_module, "_trigger_lazy_subscription_sync"),
            patch.object(app_module, "read_dataplane_state", return_value=dataplane_ok),
            patch.object(app_module, "lnd_exists", return_value=True),
            patch.object(app_module, "cln_exists", return_value=False),
        ):
            # Host umbreld polling http://10.21.0.5:9740/api/local/widgets/status from gateway 10.21.0.1
            ok_res = sec_client.get(
                "/api/local/widgets/status",
                headers={"Host": "10.21.0.5:9740"},
                environ_base={"REMOTE_ADDR": "10.21.0.1"},
            )
            assert ok_res.status_code == 200
            assert ok_res.get_json()["type"] == "four-stats"

            # Adjacent untrusted container on the bridge network (10.21.0.99) is rejected
            bad_res = sec_client.get(
                "/api/local/widgets/status",
                headers={"Host": "10.21.0.5:9740"},
                environ_base={"REMOTE_ADDR": "10.21.0.99"},
            )
            assert bad_res.status_code == 403
    finally:
        app.config["MANAGEMENT_BROWSER_SECURITY_ENABLED"] = False


def test_widget_status_ttl_cache_deduplicates_polls_and_invalidates_on_file_change(client, env_setup, monkeypatch):
    _seed_healthy_state(env_setup, node_type="lnd", nwc=True, days=28, used_gb=14.2, limit_gb=90.0)
    monkeypatch.setattr(app_module, "SECURE_MODE", True)
    app.config["WIDGET_STATUS_CACHE_TTL"] = 5.0
    app_module._invalidate_widget_status_cache()

    dataplane_ok = {
        "dataplane_mode": "docker",
        "target_container": "lightning_lnd_1",
        "target_ip": "10.21.21.9",
        "target_impl": "lnd",
        "docker_network": "umbrel_main_network",
        "forwarding_port": 36789,
        "rules_synced": True,
        "dns_protected": True,
        "decommissioned": False,
        "last_reconcile_at": datetime.now(timezone.utc).isoformat(),
        "last_error": None,
    }

    try:
        with (
            patch.object(app_module, "_get_wireguard_state", return_value=("Connected", "pub123")) as mock_wg,
            patch.object(app_module, "_trigger_lazy_subscription_sync"),
            patch.object(app_module, "read_dataplane_state", return_value=dataplane_ok),
            patch.object(app_module, "lnd_exists", return_value=True),
            patch.object(app_module, "cln_exists", return_value=False),
        ):
            r1 = client.get("/api/local/widgets/status")
            r2 = client.get("/api/local/widgets/overview")
            assert r1.status_code == 200
            assert r2.status_code == 200
            assert mock_wg.call_count == 1  # Second widget poll hit the 5s cache

            # Updating tunnelsats-meta.json immediately invalidates the cache via mtime_ns/size signature
            meta_path = env_setup["data_dir"] / "tunnelsats-meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["subscriptionTelemetry"]["bandwidthUsedGb"] = 25.6
            meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

            r3 = client.get("/api/local/widgets/status")
            assert r3.get_json()["items"][2]["text"] == "25.6"
            assert mock_wg.call_count == 2
    finally:
        app.config.pop("WIDGET_STATUS_CACHE_TTL", None)
        app_module._invalidate_widget_status_cache()


def test_widget_status_cache_skips_caching_when_metadata_mutates_mid_build(client, env_setup, monkeypatch):
    _seed_healthy_state(env_setup, node_type="lnd", nwc=True, days=28, used_gb=14.2, limit_gb=90.0)
    monkeypatch.setattr(app_module, "SECURE_MODE", True)
    app.config["WIDGET_STATUS_CACHE_TTL"] = 5.0
    app_module._invalidate_widget_status_cache()

    dataplane_ok = {
        "dataplane_mode": "docker",
        "target_container": "lightning_lnd_1",
        "target_ip": "10.21.21.9",
        "target_impl": "lnd",
        "docker_network": "umbrel_main_network",
        "forwarding_port": 36789,
        "rules_synced": True,
        "dns_protected": True,
        "decommissioned": False,
        "last_reconcile_at": datetime.now(timezone.utc).isoformat(),
        "last_error": None,
    }

    orig_build = app_module._build_local_status_dict
    mutated_once = {"done": False}

    def _build_with_concurrent_meta_write():
        result = orig_build()
        if not mutated_once["done"]:
            mutated_once["done"] = True
            meta_path = env_setup["data_dir"] / "tunnelsats-meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["subscriptionTelemetry"]["bandwidthUsedGb"] = 41.8
            meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return result

    try:
        with (
            patch.object(app_module, "_get_wireguard_state", return_value=("Connected", "pub123")),
            patch.object(app_module, "_trigger_lazy_subscription_sync"),
            patch.object(app_module, "read_dataplane_state", return_value=dataplane_ok),
            patch.object(app_module, "lnd_exists", return_value=True),
            patch.object(app_module, "cln_exists", return_value=False),
            patch.object(app_module, "_build_local_status_dict", side_effect=_build_with_concurrent_meta_write),
        ):
            first = client.get("/api/local/widgets/status").get_json()
            assert first["items"][2]["text"] == "14.2"

            # Next poll MUST NOT pair the old 14.2 result with the post-mutation signature
            second = client.get("/api/local/widgets/status").get_json()
            assert second["items"][2]["text"] == "41.8"
    finally:
        app.config.pop("WIDGET_STATUS_CACHE_TTL", None)
        app_module._invalidate_widget_status_cache()


def test_widgets_stopped_target_node_never_reports_active(client, env_setup, monkeypatch):
    """Regression for Greptile P1: if LND stops while rules_synced and lnd.conf remain, never report Active."""
    _seed_healthy_state(env_setup, node_type="lnd", nwc=True, days=28, used_gb=14.2, limit_gb=90.0)
    monkeypatch.setattr(app_module, "SECURE_MODE", False)

    dataplane_stale_synced = {
        "dataplane_mode": "docker",
        "target_container": "lightning_lnd_1",
        "target_ip": "10.21.21.9",
        "target_impl": "lnd",
        "docker_network": "umbrel_main_network",
        "forwarding_port": 36789,
        "rules_synced": True,
        "ipv4_rules_synced": True,
        "ipv6_rules_synced": True,
        "dns_protected": True,
        "decommissioned": False,
        "last_reconcile_at": datetime.now(timezone.utc).isoformat(),
        "last_error": None,
    }

    # 1. Pre-reconcile window: LND stopped, rules_synced still True, lnd.conf intact
    with (
        patch.object(app_module, "_get_wireguard_state", return_value=("Connected", "pub123")),
        patch.object(app_module, "_trigger_lazy_subscription_sync"),
        patch.object(app_module, "read_dataplane_state", return_value=dataplane_stale_synced),
        patch.object(app_module, "lnd_exists", return_value=False),
        patch.object(app_module, "cln_exists", return_value=False),
        patch.object(app_module, "list_containers", return_value=[]),
    ):
        s_body = client.get("/api/local/widgets/status").get_json()
        assert s_body["items"][0] == {"title": "LND Shield", "text": "🔴 Node Off", "subtext": ""}

        o_body = client.get("/api/local/widgets/overview").get_json()
        assert o_body["title"] == "🔴 Node Off · LND · 🇺🇸 us3"

    # 2. Unrelated CLN container running while protected LND is stopped -> still 🔴 Node Off (never 🟢 Active)
    env_setup["cln_conf"].write_text("network=bitcoin\n", encoding="utf-8")
    cln_only_containers = [
        {"Id": "cln1", "Names": ["/core-lightning_lightningd_1"], "NetworkSettings": {"Networks": {"main": {"IPAddress": "10.21.21.96"}}}}
    ]
    with (
        patch.object(app_module, "_get_wireguard_state", return_value=("Connected", "pub123")),
        patch.object(app_module, "_trigger_lazy_subscription_sync"),
        patch.object(app_module, "read_dataplane_state", return_value=dataplane_stale_synced),
        patch.object(app_module, "lnd_exists", return_value=False),
        patch.object(app_module, "cln_exists", return_value=True),
        patch.object(app_module, "list_containers", return_value=cln_only_containers),
    ):
        s_cln_side = client.get("/api/local/widgets/status").get_json()
        assert s_cln_side["items"][0] == {"title": "LND Shield", "text": "🔴 Node Off", "subtext": ""}

    # 3. Post-reconcile window: rules_synced=False with Waiting for the protected node to start
    dataplane_waiting = {
        **dataplane_stale_synced,
        "rules_synced": False,
        "last_error": "Waiting for the protected node to start: LND (lightning_lnd_1)",
    }
    env_setup["lnd_conf"].unlink()
    with (
        patch.object(app_module, "_get_wireguard_state", return_value=("Connected", "pub123")),
        patch.object(app_module, "_trigger_lazy_subscription_sync"),
        patch.object(app_module, "read_dataplane_state", return_value=dataplane_waiting),
        patch.object(app_module, "lnd_exists", return_value=False),
        patch.object(app_module, "cln_exists", return_value=True),
        patch.object(app_module, "list_containers", return_value=cln_only_containers),
    ):
        s_waiting = client.get("/api/local/widgets/status").get_json()
        assert s_waiting["items"][0] == {"title": "LND Shield", "text": "🔴 Node Off", "subtext": ""}


def test_widget_status_cache_singleflights_concurrent_cold_polls(env_setup, monkeypatch):
    """Regression for Greptile P2: simultaneous cold widget polls share one _build_local_status_dict() call."""
    import concurrent.futures
    import threading

    _seed_healthy_state(env_setup, node_type="lnd", nwc=True, days=28, used_gb=14.2, limit_gb=90.0)
    monkeypatch.setattr(app_module, "SECURE_MODE", True)
    app.config["TESTING"] = True
    app.config["MANAGEMENT_BROWSER_SECURITY_ENABLED"] = False
    app.config["WIDGET_STATUS_CACHE_TTL"] = 5.0
    app_module._invalidate_widget_status_cache()

    dataplane_ok = {
        "dataplane_mode": "docker",
        "target_container": "lightning_lnd_1",
        "target_ip": "10.21.21.9",
        "target_impl": "lnd",
        "docker_network": "umbrel_main_network",
        "forwarding_port": 36789,
        "rules_synced": True,
        "dns_protected": True,
        "decommissioned": False,
        "last_reconcile_at": datetime.now(timezone.utc).isoformat(),
        "last_error": None,
    }

    orig_build = app_module._build_local_status_dict
    build_calls = {"count": 0}
    count_lock = threading.Lock()
    entered_build = threading.Event()
    release_build = threading.Event()

    def _slow_build():
        with count_lock:
            build_calls["count"] += 1
        entered_build.set()
        release_build.wait(timeout=2.0)
        return orig_build()

    def _poll(path: str):
        with app.test_client() as local_client:
            return local_client.get(path).get_json()

    try:
        with (
            patch.object(app_module, "_get_wireguard_state", return_value=("Connected", "pub123")),
            patch.object(app_module, "_trigger_lazy_subscription_sync"),
            patch.object(app_module, "read_dataplane_state", return_value=dataplane_ok),
            patch.object(app_module, "lnd_exists", return_value=True),
            patch.object(app_module, "cln_exists", return_value=False),
            patch.object(app_module, "_build_local_status_dict", side_effect=_slow_build),
        ):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                f1 = pool.submit(_poll, "/api/local/widgets/status")
                assert entered_build.wait(timeout=2.0)
                f2 = pool.submit(_poll, "/api/local/widgets/overview")
                # Give second worker a moment to reach _get_widget_status_dict() while first build is in flight
                import time as _t
                _t.sleep(0.05)
                release_build.set()
                res1 = f1.result(timeout=3.0)
                res2 = f2.result(timeout=3.0)

            assert res1["type"] == "four-stats"
            assert res2["type"] == "text-with-progress"
            assert build_calls["count"] == 1
    finally:
        release_build.set()
        app.config.pop("WIDGET_STATUS_CACHE_TTL", None)
        app_module._invalidate_widget_status_cache()
