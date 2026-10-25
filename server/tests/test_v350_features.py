"""Unit tests for TunnelSats v3.5.0 backend features (Issue #186)."""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app as app_module
from app import app, apply_vpn_port
import nwc as nwc_module
from nwc import (
    NWC_WALLET_FILE,
    NwcError,
    _BECH32_CHARSET,
    _bech32_hrp_expand,
    _bech32_polymod,
    _build_nip47_request_event,
    _nostr_pubkey_from_secret,
    _parse_nip47_response_event,
    assemble_wg_config,
    compute_bandwidth_telemetry,
    derive_wg_public_key,
    evaluate_subscription_notices,
    generate_wg_keypair,
    load_nwc_wallet,
    maybe_nwc_auto_renew,
    parse_nwc_uri,
    save_nwc_wallet,
    validate_wg_public_key,
    verify_bolt11_invoice,
)


def _encode_int_5bit(val: int, num_words: int) -> list[int]:
    words = []
    for _ in range(num_words):
        words.append(val & 31)
        val >>= 5
    words.reverse()
    return words


def _make_bolt11(
    amount_hrp: str = "2500u",
    payment_hash_hex: str = "11" * 32,
    timestamp: int | None = None,
    expiry_seconds: int = 3600,
    hrp_prefix: str = "lnbc",
) -> str:
    """Construct a valid Bech32-checksummed BOLT11 invoice string for unit tests."""
    ts = int(timestamp if timestamp is not None else time.time())
    hrp = f"{hrp_prefix}{amount_hrp}"
    words = _encode_int_5bit(ts, 7)

    # Tag 1 ('p'): 52 words (260 bits, top 256 bits are payment_hash)
    ph_int = int(payment_hash_hex, 16) << 4
    ph_words = _encode_int_5bit(ph_int, 52)
    words.extend([1, (52 >> 5) & 31, 52 & 31] + ph_words)

    # Tag 6 ('x'): expiry_seconds
    exp_words: list[int] = []
    tmp_exp = expiry_seconds
    while tmp_exp > 0 or not exp_words:
        exp_words.append(tmp_exp & 31)
        tmp_exp >>= 5
    exp_words.reverse()
    words.extend([6, (len(exp_words) >> 5) & 31, len(exp_words) & 31] + exp_words)

    # Dummy 65-byte (104 5-bit words) signature
    words.extend([0] * 104)

    values = _bech32_hrp_expand(hrp) + words
    polymod = _bech32_polymod(values + [0, 0, 0, 0, 0, 0]) ^ 1
    checksum = [(polymod >> (5 * (5 - i))) & 31 for i in range(6)]
    data_str = "".join(_BECH32_CHARSET[w] for w in (words + checksum))
    return f"{hrp}1{data_str}"


VALID_SECRET = "22" * 32
VALID_WALLET_PRIV = "33" * 32
VALID_WALLET_PUB = _nostr_pubkey_from_secret(VALID_WALLET_PRIV)
VALID_NWC_URI = (
    f"nostr+walletconnect://{VALID_WALLET_PUB}"
    f"?relay=wss://relay.getalby.com/v1&secret={VALID_SECRET}&lud16=user@getalby.com"
)


@pytest.fixture
def data_dir(tmp_path):
    orig_dir = app_module.DATA_DIR
    app_module.DATA_DIR = str(tmp_path)
    app_module._SUBSCRIPTION_CACHE.clear()
    app_module._next_subscription_sync_time.clear()
    yield tmp_path
    app_module.DATA_DIR = orig_dir


@pytest.fixture
def client(data_dir):
    app.config["TESTING"] = True
    app.config["MANAGEMENT_BROWSER_SECURITY_ENABLED"] = False
    with app.test_client() as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# 1. Pure-Python X25519 Keygen & WireGuard Config Assembly
# ---------------------------------------------------------------------------

def test_generate_and_validate_wg_keypair_pure_python_fallback():
    with patch("nwc.subprocess.run", side_effect=FileNotFoundError("no wg binary")):
        priv, pub = generate_wg_keypair()
        assert validate_wg_public_key(priv) is True
        assert validate_wg_public_key(pub) is True
        assert priv != pub
        assert derive_wg_public_key(priv) == pub

    assert validate_wg_public_key("not-base64") is False
    assert validate_wg_public_key("AAAA") is False


def test_assemble_wg_config_from_structured_claim_response():
    with patch("nwc.subprocess.run", side_effect=FileNotFoundError("no wg binary")):
        priv, pub = generate_wg_keypair()
        _, srv_pub = generate_wg_keypair()
        _, other_pub = generate_wg_keypair()

    # 1. Canonical structured claim where peer.publicKey is client key and server.publicKey is server key
    claim_data = {
        "vpnPort": 36789,
        "expiresAt": "2027-01-15T00:00:00Z",
        "interface": {
            "address": "10.9.0.55",
            "dns": "8.8.8.8",
        },
        "peer": {
            "publicKey": pub,
            "presharedKey": priv,
            "allowedIPs": "0.0.0.0/0",
            "endpoint": "de2.tunnelsats.com:51820",
        },
        "server": {
            "publicKey": srv_pub,
            "domain": "de2.tunnelsats.com",
        },
    }
    cfg = assemble_wg_config(priv, claim_data, fallback_server_id="de2")
    assert cfg is not None
    assert f"PrivateKey = {priv}" in cfg
    assert "Address = 10.9.0.55/32" in cfg
    assert "DNS = 8.8.8.8" in cfg
    assert "# Port Forwarding: 36789" in cfg
    assert "# VPN Server Domain: de2.tunnelsats.com" in cfg
    assert f"# WG Public Key: {pub}" in cfg
    assert "# Subscription End: 2027-01-15T00:00:00Z" in cfg
    assert f"PublicKey = {srv_pub}" in cfg
    assert f"PublicKey = {pub}" not in cfg
    assert f"PresharedKey = {priv}" in cfg
    assert "Endpoint = de2.tunnelsats.com:51820" in cfg
    assert "PersistentKeepalive = 25" in cfg

    # 2. Rejects mismatched client peer.publicKey when server.publicKey is present
    bad_claim = dict(claim_data, peer={**claim_data["peer"], "publicKey": other_pub})
    assert assemble_wg_config(priv, bad_claim, fallback_server_id="de2") is None

    # 3. Rejects when peer.publicKey is client key and no server.publicKey is provided
    missing_srv_claim = {
        "interface": {"address": "10.9.0.55"},
        "peer": {"publicKey": pub, "endpoint": "de2.tunnelsats.com:51820"},
    }
    assert assemble_wg_config(priv, missing_srv_claim, fallback_server_id="de2") is None


# ---------------------------------------------------------------------------
# 2. Pure-Python BOLT11 Verifier
# ---------------------------------------------------------------------------

def test_verify_bolt11_invoice_valid():
    ph = "ab" * 32
    inv = _make_bolt11(amount_hrp="250u", payment_hash_hex=ph, expiry_seconds=3600)
    res = verify_bolt11_invoice(inv, expected_payment_hash=ph, expected_amount_sats=25_000)
    assert res["valid"] is True
    assert res["amount_sats"] == 25_000
    assert res["payment_hash"] == ph


def test_verify_bolt11_invoice_rejects_bad_checksum():
    inv = _make_bolt11(amount_hrp="100u")
    tampered = inv[:-1] + ("q" if inv[-1] != "q" else "p")
    res = verify_bolt11_invoice(tampered)
    assert res["valid"] is False
    assert "checksum" in res["error"].lower()


def test_verify_bolt11_invoice_rejects_expired():
    past_ts = int(time.time()) - 7200
    inv = _make_bolt11(amount_hrp="100u", timestamp=past_ts, expiry_seconds=3600)
    res = verify_bolt11_invoice(inv, check_expiry=True)
    assert res["valid"] is False
    assert "expired" in res["error"].lower()


def test_verify_bolt11_invoice_rejects_wrong_payment_hash():
    inv = _make_bolt11(amount_hrp="100u", payment_hash_hex="11" * 32)
    res = verify_bolt11_invoice(inv, expected_payment_hash="22" * 32)
    assert res["valid"] is False
    assert "payment_hash" in res["error"]


def test_verify_bolt11_invoice_rejects_fractional_and_excessive_amounts():
    # 15p = 0.0015 sat (fractional sub-satoshi)
    frac_inv = _make_bolt11(amount_hrp="15p")
    res_frac = verify_bolt11_invoice(frac_inv)
    assert res_frac["valid"] is False
    assert "whole-satoshi" in res_frac["error"]

    # 10m = 1,000,000 sats (> 500,000 cap)
    huge_inv = _make_bolt11(amount_hrp="10m")
    res_huge = verify_bolt11_invoice(huge_inv, max_amount_sats=500_000)
    assert res_huge["valid"] is False
    assert "exceeds safety cap" in res_huge["error"]


# ---------------------------------------------------------------------------
# 3. Pure-Python NIP-47 NWC URI & Crypto
# ---------------------------------------------------------------------------

def test_parse_nwc_uri_valid_and_caps_at_three_relays():
    uri = (
        f"nostr+walletconnect://{VALID_WALLET_PUB}"
        "?relay=wss://r1.example.com"
        "&relay=wss://r2.example.com"
        "&relay=wss://r3.example.com"
        "&relay=wss://r4.example.com"
        f"&secret={VALID_SECRET}"
        "&lud16=alice@example.com"
    )
    parsed = parse_nwc_uri(uri)
    assert parsed["wallet_pubkey"] == VALID_WALLET_PUB
    assert parsed["secret"] == VALID_SECRET
    assert parsed["lud16"] == "alice@example.com"
    assert len(parsed["relays"]) == 3
    assert parsed["relay_host"] == "r1.example.com"


def test_parse_nwc_uri_rejects_invalid_scheme_pubkey_secret_and_ws_clearnet():
    with pytest.raises(ValueError, match="nostr\\+walletconnect://"):
        parse_nwc_uri("https://example.com")

    with pytest.raises(ValueError, match="64-character hex wallet pubkey"):
        parse_nwc_uri(f"nostr+walletconnect://badpubkey?relay=wss://r.example.com&secret={VALID_SECRET}")

    with pytest.raises(ValueError, match="secret"):
        parse_nwc_uri(f"nostr+walletconnect://{VALID_WALLET_PUB}?relay=wss://r.example.com&secret=short")

    with pytest.raises(ValueError, match="Plaintext ws://"):
        parse_nwc_uri(f"nostr+walletconnect://{VALID_WALLET_PUB}?relay=ws://r.example.com&secret={VALID_SECRET}")


def test_parse_nwc_uri_onion_relay_rules():
    onion_uri = (
        f"nostr+walletconnect://{VALID_WALLET_PUB}"
        f"?relay=ws://abcdef1234567890.onion:4848&secret={VALID_SECRET}"
    )
    # Allowed when route_via_tor is True or default
    parsed = parse_nwc_uri(onion_uri, route_via_tor=True)
    assert parsed["has_onion_relay"] is True
    assert parsed["relay_host"] == "abcdef1234567890.onion"

    # Rejected if route_via_tor is explicitly False
    with pytest.raises(ValueError, match="Tor"):
        parse_nwc_uri(onion_uri, route_via_tor=False)


def test_nip04_and_nip47_event_roundtrip():
    req_event, shared_key = _build_nip47_request_event(
        VALID_SECRET,
        VALID_WALLET_PUB,
        "pay_invoice",
        {"invoice": "lnbc100u1test"},
    )
    assert req_event["kind"] == 23194

    # Simulate wallet response event (kind 23195)
    from nwc import _nip04_encrypt, _schnorr_sign
    import hashlib

    resp_content = _nip04_encrypt(
        shared_key,
        json.dumps({"result_type": "pay_invoice", "result": {"preimage": "ff" * 32}}),
    )
    tags = [["p", req_event["pubkey"]], ["e", req_event["id"]]]
    ts = int(time.time())
    commitment = json.dumps([0, VALID_WALLET_PUB, ts, 23195, tags, resp_content], separators=(",", ":"))
    resp_id = hashlib.sha256(commitment.encode("utf-8")).digest()
    resp_event = {
        "id": resp_id.hex(),
        "pubkey": VALID_WALLET_PUB,
        "created_at": ts,
        "kind": 23195,
        "tags": tags,
        "content": resp_content,
        "sig": _schnorr_sign(VALID_WALLET_PRIV, resp_id),
    }
    parsed_body = _parse_nip47_response_event(resp_event, req_event["id"], VALID_WALLET_PUB, shared_key)
    assert parsed_body["result"]["preimage"] == "ff" * 32


# ---------------------------------------------------------------------------
# 4. NWC Endpoints & Auto-Renewal Engine
# ---------------------------------------------------------------------------

def test_nwc_connect_disconnect_and_file_mode_0600(client, data_dir):
    res = client.post(
        "/api/nwc/connect",
        json={"uri": VALID_NWC_URI, "autoRenewDuration": 3, "routeViaTor": False},
    )
    assert res.status_code == 200
    body = res.get_json()
    assert body["success"] is True
    assert body["nwcConnected"] is True
    assert body["relayHost"] == "relay.getalby.com"
    assert body["autoRenewDuration"] == 3

    wallet_path = os.path.join(data_dir, NWC_WALLET_FILE)
    assert os.path.exists(wallet_path)
    assert oct(os.stat(wallet_path).st_mode & 0o777) == "0o600"

    status_res = client.get("/api/local/status")
    status_data = status_res.get_json()
    assert status_data["nwc"]["connected"] is True
    assert status_data["nwc"]["relay_host"] == "relay.getalby.com"
    assert status_data["nwc"]["auto_renew_duration"] == 3

    disc_res = client.post("/api/nwc/disconnect", json={})
    assert disc_res.status_code == 200
    assert disc_res.get_json()["nwcConnected"] is False
    assert not os.path.exists(wallet_path)


@patch("nwc.nwc_rpc_call")
def test_nwc_pay_endpoint_verifies_and_pays(mock_rpc, client, data_dir):
    save_nwc_wallet(str(data_dir), VALID_NWC_URI)
    ph = "aa" * 32
    inv = _make_bolt11(amount_hrp="100u", payment_hash_hex=ph)

    def rpc_side_effect(wallet, method, params=None, **kwargs):
        if method == "lookup_invoice":
            raise NwcError("Not found", code="NOT_FOUND")
        if method == "pay_invoice":
            return {"preimage": "99" * 32}
        return {}

    mock_rpc.side_effect = rpc_side_effect
    res = client.post(
        "/api/nwc/pay",
        json={"invoice": inv, "paymentHash": ph, "amountSats": 10_000},
    )
    assert res.status_code == 200
    data = res.get_json()
    assert data["success"] is True
    assert data["preimage"] == "99" * 32
    assert data["amount_sats"] == 10_000


@patch("nwc.nwc_rpc_call")
def test_maybe_nwc_auto_renew_success_and_backoff_to_fallback(mock_rpc, data_dir):
    save_nwc_wallet(str(data_dir), VALID_NWC_URI, auto_renew_duration=3)
    now = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
    ph = "cd" * 32
    inv = _make_bolt11(amount_hrp="300u", payment_hash_hex=ph, timestamp=int(now.timestamp()))

    meta = {
        "nwcConnected": True,
        "wgPublicKey": "L7vkSGz/ODjzBTmYo+gkJADq9GRF0NfxjOsFBNDVjQ4=",
        "serverId": "de2",
        "expiresAt": (now + timedelta(days=5)).isoformat(),
    }

    def create_renewal_fn(pubkey, server_id, duration):
        assert duration == 3
        return {"invoice": inv, "paymentHash": ph, "amountSats": 30_000}

    # 1. Simulate 3 consecutive failures (1h -> 6h -> 12h -> fallbackTaskRaised)
    mock_rpc.side_effect = NwcError("Wallet offline", code="INTERNAL")
    with patch("nwc.time.time", return_value=now.timestamp()):
        r1 = maybe_nwc_auto_renew(str(data_dir), meta, create_renewal_fn, now_dt=now)
        assert r1["success"] is False
        assert r1["attempts"] == 1
        assert r1["fallback_task_raised"] is False

        # During backoff window, should skip without attempting
        r_skip = maybe_nwc_auto_renew(
            str(data_dir), meta, create_renewal_fn, now_dt=now + timedelta(minutes=30)
        )
        assert r_skip["attempted"] is False
        assert r_skip["reason"] == "backoff_active"

        # Attempt 2 after 1h
        t2 = now + timedelta(hours=1, minutes=1)
        r2 = maybe_nwc_auto_renew(str(data_dir), meta, create_renewal_fn, now_dt=t2)
        assert r2["attempts"] == 2
        assert r2["fallback_task_raised"] is False

        # Attempt 3 after +6h raises fallbackTaskRaised
        t3 = t2 + timedelta(hours=6, minutes=1)
        r3 = maybe_nwc_auto_renew(str(data_dir), meta, create_renewal_fn, now_dt=t3)
        assert r3["attempts"] == 3
        assert r3["fallback_task_raised"] is True
        assert meta["nwcAutoRenewState"]["fallbackTaskRaised"] is True

    # 2. Reset state and verify successful auto-renewal payment
    meta["nwcAutoRenewState"] = {}
    mock_rpc.side_effect = lambda wallet, method, params=None, **kw: (
        {"remaining_budget": 100_000_000}
        if method == "get_budget"
        else ({"preimage": "ab" * 32} if method == "pay_invoice" else {})
    )
    with patch("nwc.time.time", return_value=now.timestamp()):
        r_ok = maybe_nwc_auto_renew(str(data_dir), meta, create_renewal_fn, now_dt=now)
        assert r_ok["success"] is True
        assert r_ok["amount_sats"] == 30_000
        assert meta["lastDuration"] == 3
        assert meta["lastAmountSats"] == 30_000
        assert "pendingRenewal" not in meta


# ---------------------------------------------------------------------------
# 5. Bandwidth Telemetry & Subscription Notices Engine
# ---------------------------------------------------------------------------

def test_compute_bandwidth_telemetry_pace_and_resets_remaining():
    # Oct 16, 2026 at 12:00 UTC is exactly 15.5 days into a 31-day month (50.0% elapsed)
    now = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    tel = compute_bandwidth_telemetry(
        {
            "bandwidthUsedGb": 60.0,
            "bandwidthLimitGb": 100.0,
            "resetsThisMonth": 0,
            "maxResetsPerMonth": 2,
            "status": "active",
            "lastSyncedAt": "2026-10-16T11:55:00Z",
        },
        now_dt=now,
    )
    assert tel["bandwidth_used_gb"] == 60.0
    assert tel["bandwidth_limit_gb"] == 100.0
    assert tel["used_pct"] == 60.0
    assert tel["resets_remaining_this_month"] == 2
    assert tel["next_free_reset_utc"] == "Nov 1 UTC"
    assert tel["pace_pct"] == 50.0
    assert tel["projected_gb"] == 120.0
    assert tel["over_pace"] is True


def test_compute_bandwidth_telemetry_utc_month_boundary_rollover():
    now = datetime(2026, 11, 2, 10, 0, 0, tzinfo=timezone.utc)
    tel = compute_bandwidth_telemetry(
        {
            "bandwidthUsedGb": 88.5,
            "bandwidthLimitGb": 100.0,
            "resetsThisMonth": 2,
            "maxResetsPerMonth": 2,
            "lastSyncedAt": "2026-10-31T23:50:00Z",
        },
        now_dt=now,
    )
    assert tel["month_rolled_over"] is True
    assert tel["bandwidth_used_gb"] == 0.0
    assert tel["bandwidth_resets_this_month"] == 0
    assert tel["resets_remaining_this_month"] == 2


def test_evaluate_subscription_notices_phases_and_nwc_suppression(data_dir):
    now = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)

    # 1. 5 days left without NWC -> renew_soon amber banner
    n_7d = evaluate_subscription_notices(
        str(data_dir),
        {"expiresAt": (now + timedelta(days=5)).isoformat(), "nwcConnected": False},
        now_dt=now,
    )
    assert n_7d["phase"] == "renew_soon"
    assert n_7d["suppressed_by_nwc"] is False
    assert any(b["id"] == "expiry_7d" and b["severity"] == "amber" for b in n_7d["banners"])

    # 2. 2 days left WITH healthy NWC -> suppressed_by_nwc is True, no manual expiry banner
    n_suppressed = evaluate_subscription_notices(
        str(data_dir),
        {
            "expiresAt": (now + timedelta(days=2)).isoformat(),
            "nwcConnected": True,
            "nwcAutoRenewState": {"budgetWarning": False, "fallbackTaskRaised": False},
        },
        now_dt=now,
    )
    assert n_suppressed["phase"] == "renew_now"
    assert n_suppressed["suppressed_by_nwc"] is True
    assert not any(b["kind"] == "expiry" for b in n_suppressed["banners"])

    # 3. 2 days left WITH NWC budgetWarning -> NOT suppressed, shows both expiry_3d and nwc_budget_warning
    n_unhealthy = evaluate_subscription_notices(
        str(data_dir),
        {
            "expiresAt": (now + timedelta(days=2)).isoformat(),
            "nwcConnected": True,
            "nwcAutoRenewState": {"budgetWarning": True, "lastError": "Low budget"},
        },
        now_dt=now,
    )
    assert n_unhealthy["suppressed_by_nwc"] is False
    banner_ids = {b["id"] for b in n_unhealthy["banners"]}
    assert "expiry_3d" in banner_ids
    assert "nwc_budget_warning" in banner_ids

    # 4. <24 hours left WITH healthy NWC -> NOT suppressed, shows expiry_24h red warning
    n_24h = evaluate_subscription_notices(
        str(data_dir),
        {
            "expiresAt": (now + timedelta(hours=12)).isoformat(),
            "nwcConnected": True,
            "nwcAutoRenewState": {"budgetWarning": False, "fallbackTaskRaised": False},
        },
        now_dt=now,
    )
    assert n_24h["phase"] == "renew_now"
    assert n_24h["suppressed_by_nwc"] is False
    assert any(b["id"] == "expiry_24h" and b["severity"] == "red" for b in n_24h["banners"])

    # 5. Expired + 92% bandwidth -> expired red + bandwidth_90 red
    n_exp = evaluate_subscription_notices(
        str(data_dir),
        {
            "expiresAt": (now - timedelta(hours=2)).isoformat(),
            "subscriptionTelemetry": {
                "bandwidthUsedGb": 92.0,
                "bandwidthLimitGb": 100.0,
                "lastSyncedAt": now.isoformat(),
            },
        },
        now_dt=now,
    )
    assert n_exp["phase"] == "expired"
    exp_ids = {b["id"] for b in n_exp["banners"]}
    assert "expiry_expired" in exp_ids
    assert "bandwidth_90" in exp_ids


# ---------------------------------------------------------------------------
# 6. Bandwidth Reset, Settlement, Dynamic Port Sync & Reachability
# ---------------------------------------------------------------------------

@patch("app.requests.post")
def test_bandwidth_reset_reuses_unexpired_pending_invoice_and_handles_429(mock_post, client, data_dir):
    meta_path = os.path.join(data_dir, app_module.META_FILE)
    future_exp = (datetime.now(timezone.utc) + timedelta(minutes=45)).isoformat()
    with open(meta_path, "w", encoding="utf-8") as fp:
        json.dump(
            {
                "wgPublicKey": "pubKeyReset123",
                "serverId": "de2",
                "pendingReset": {
                    "paymentHash": "resetHash111",
                    "invoice": "lnbc50u1cached",
                    "amountSats": 5000,
                    "expiresAt": future_exp,
                    "wgPublicKey": "pubKeyReset123",
                    "serverId": "de2",
                },
            },
            fp,
        )

    # 1. Reuses cached unexpired pendingReset without calling upstream
    res = client.post("/api/subscription/bandwidth-reset", json={})
    assert res.status_code == 200
    body = res.get_json()
    assert body["reused"] is True
    assert body["paymentHash"] == "resetHash111"
    mock_post.assert_not_called()

    # 2. When pendingReset is expired, calls upstream (test 200 and 429)
    past_exp = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    with open(meta_path, "w", encoding="utf-8") as fp:
        json.dump(
            {
                "wgPublicKey": "pubKeyReset123",
                "serverId": "de2",
                "pendingReset": {
                    "paymentHash": "oldHash",
                    "invoice": "lnbc50u1old",
                    "expiresAt": past_exp,
                    "wgPublicKey": "pubKeyReset123",
                },
            },
            fp,
        )

    mock_resp_200 = MagicMock()
    mock_resp_200.status_code = 200
    mock_resp_200.headers = {"Content-Type": "application/json"}
    mock_resp_200.content = json.dumps(
        {
            "paymentHash": "newResetHash222",
            "invoice": "lnbc50u1new",
            "amountSats": 5000,
            "expiresAt": future_exp,
            "resetsRemaining": 1,
        }
    ).encode("utf-8")
    mock_post.return_value = mock_resp_200

    res2 = client.post("/api/subscription/bandwidth-reset", json={})
    assert res2.status_code == 200
    assert json.loads(res2.data)["paymentHash"] == "newResetHash222"

    # Clear pendingReset and simulate 429 monthly limit reached
    with open(meta_path, "w", encoding="utf-8") as fp:
        json.dump({"wgPublicKey": "pubKeyReset123", "serverId": "de2"}, fp)

    mock_resp_429 = MagicMock()
    mock_resp_429.status_code = 429
    mock_resp_429.headers = {"Content-Type": "application/json"}
    mock_resp_429.content = b'{"error":"Maximum 2 bandwidth resets per month reached"}'
    mock_post.return_value = mock_resp_429

    res_429 = client.post("/api/subscription/bandwidth-reset", json={})
    assert res_429.status_code == 429


@patch("app._trigger_lazy_subscription_sync")
@patch("app.requests.get")
def test_check_subscription_handles_202_processing_and_bandwidth_reset_paid(
    mock_get, mock_trigger_sync, client, data_dir
):
    meta_path = os.path.join(data_dir, app_module.META_FILE)
    with open(meta_path, "w", encoding="utf-8") as fp:
        json.dump(
            {
                "wgPublicKey": "pubKeyReset123",
                "serverId": "de2",
                "subscriptionTelemetry": {
                    "bandwidthUsedGb": 95.5,
                    "bandwidthLimitGb": 100.0,
                    "resetsThisMonth": 0,
                    "maxResetsPerMonth": 2,
                },
                "pendingReset": {
                    "paymentHash": "resetHash222",
                    "invoice": "lnbc50u1new",
                },
            },
            fp,
        )

    # 1. HTTP 202 processing
    resp_202 = MagicMock()
    resp_202.status_code = 202
    resp_202.headers = {"Content-Type": "application/json"}
    resp_202.json.return_value = {"status": "processing"}
    resp_202.content = b'{"status":"processing"}'
    mock_get.return_value = resp_202

    r_proc = client.get("/api/subscription/resetHash222")
    assert r_proc.status_code == 202

    # 2. HTTP 200 paid bandwidth_reset
    resp_paid = MagicMock()
    resp_paid.status_code = 200
    resp_paid.headers = {"Content-Type": "application/json"}
    resp_paid.json.return_value = {
        "status": "paid",
        "type": "bandwidth_reset",
        "bandwidthResetsRemaining": 1,
    }
    resp_paid.content = json.dumps(resp_paid.json.return_value).encode("utf-8")
    mock_get.return_value = resp_paid

    r_paid = client.get("/api/subscription/resetHash222")
    assert r_paid.status_code == 200

    with open(meta_path, "r", encoding="utf-8") as fp:
        updated_meta = json.load(fp)
    assert "pendingReset" not in updated_meta
    assert updated_meta["subscriptionTelemetry"]["bandwidthUsedGb"] == 0.0
    assert updated_meta["subscriptionTelemetry"]["resetsThisMonth"] == 1
    assert "resetHash222" in updated_meta.get("appliedResetHashes", [])
    mock_trigger_sync.assert_called_once_with("pubKeyReset123")

    # 3. Re-checking the same paid reset hash does NOT zero newer usage or increment resetsThisMonth again
    updated_meta["subscriptionTelemetry"]["bandwidthUsedGb"] = 14.5
    with open(meta_path, "w", encoding="utf-8") as fp:
        json.dump(updated_meta, fp)

    resp_paid_no_rem = MagicMock()
    resp_paid_no_rem.status_code = 200
    resp_paid_no_rem.headers = {"Content-Type": "application/json"}
    resp_paid_no_rem.json.return_value = {"status": "paid", "type": "bandwidth_reset"}
    resp_paid_no_rem.content = json.dumps(resp_paid_no_rem.json.return_value).encode("utf-8")
    mock_get.return_value = resp_paid_no_rem

    r_dup = client.get("/api/subscription/resetHash222")
    assert r_dup.status_code == 200
    with open(meta_path, "r", encoding="utf-8") as fp:
        after_dup = json.load(fp)
    assert after_dup["subscriptionTelemetry"]["bandwidthUsedGb"] == 14.5
    assert after_dup["subscriptionTelemetry"]["resetsThisMonth"] == 1


def test_apply_vpn_port_updates_meta_and_wireguard_conf(data_dir):
    meta_path = os.path.join(data_dir, app_module.META_FILE)
    conf_path = os.path.join(data_dir, "tunnelsats.conf")
    with open(meta_path, "w", encoding="utf-8") as fp:
        json.dump({"serverDomain": "de2.tunnelsats.com", "vpnPort": 31111}, fp)
    with open(conf_path, "w", encoding="utf-8") as fp:
        fp.write("# Port Forwarding: 31111\n[Interface]\nPrivateKey = x\n[Peer]\nPublicKey = y\n")

    assert apply_vpn_port(39999, data_dir=str(data_dir)) is True

    with open(meta_path, "r", encoding="utf-8") as fp:
        assert json.load(fp)["vpnPort"] == 39999
    with open(conf_path, "r", encoding="utf-8") as fp:
        assert "# Port Forwarding: 39999" in fp.read()

    assert apply_vpn_port(0, data_dir=str(data_dir)) is False
    assert apply_vpn_port(70000, data_dir=str(data_dir)) is False


@patch("app.requests.post")
def test_reachability_endpoint_builds_socket_and_calls_ping_test(mock_post, client, data_dir):
    meta_path = os.path.join(data_dir, app_module.META_FILE)
    with open(meta_path, "w", encoding="utf-8") as fp:
        json.dump({"serverDomain": "de2.tunnelsats.com", "vpnPort": 35825}, fp)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b'{"reachable": true, "latency_ms": 42}'
    mock_resp.json.return_value = {"reachable": True, "latency_ms": 42}
    mock_post.return_value = mock_resp

    node_pk = "02" + ("ab" * 32)
    res = client.post("/api/reachability", json={"pubkey": node_pk})
    assert res.status_code == 200
    body = res.get_json()
    assert body["success"] is True
    assert body["reachable"] is True
    assert body["latency_ms"] == 42
    assert body["socket"] == f"{node_pk}@de2.tunnelsats.com:35825"


@patch("app.requests.post")
def test_create_and_claim_subscription_with_local_keygen_and_structured_response(
    mock_post, client, data_dir
):
    # 1. Reject invalid referralCode
    bad_ref = client.post(
        "/api/subscription/create",
        json={"serverId": "de2", "duration": 1, "referralCode": "bad code with spaces!"},
    )
    assert bad_ref.status_code == 400

    # 2. Create subscription generates local X25519 keypair and stores pendingOrder
    mock_create_resp = MagicMock()
    mock_create_resp.status_code = 200
    mock_create_resp.headers = {"Content-Type": "application/json"}
    mock_create_resp.content = b'{"paymentHash":"orderHash999","invoice":"lnbc100u1order","amountSats":10000}'
    mock_post.return_value = mock_create_resp

    create_res = client.post(
        "/api/subscription/create",
        json={"serverId": "de2", "duration": 3, "referralCode": "TUNNEL2026"},
    )
    assert create_res.status_code == 200

    # Verify /api/local/meta and /api/local/status never leak wgPrivateKey
    meta_res = client.get("/api/local/meta").get_json()
    assert "wgPrivateKey" not in meta_res.get("pendingOrder", {})
    # Unpaid create must not overwrite lastDuration before claim
    assert "lastDuration" not in meta_res
    status_res = client.get("/api/local/status").get_json()
    assert "wgPrivateKey" not in (status_res.get("pending_order") or {})

    # 3. Claim subscription with structured response (no fullConfig) assembles config using pendingOrder key
    with patch("nwc.subprocess.run", side_effect=FileNotFoundError("no wg")):
        _, srv_pub = generate_wg_keypair()

    structured_claim = {
        "success": True,
        "vpnPort": 34444,
        "expiresAt": "2027-01-25T00:00:00Z",
        "interface": {"address": "10.9.0.77/32", "dns": "8.8.8.8"},
        "peer": {
            "publicKey": srv_pub,
            "endpoint": "de2.tunnelsats.com:51820",
            "allowedIPs": "0.0.0.0/0",
        },
    }
    mock_claim_resp = MagicMock()
    mock_claim_resp.status_code = 200
    mock_claim_resp.headers = {"Content-Type": "application/json"}
    mock_claim_resp.json.return_value = structured_claim
    mock_claim_resp.content = json.dumps(structured_claim).encode("utf-8")
    mock_post.return_value = mock_claim_resp

    claim_res = client.post("/api/subscription/claim", json={"paymentHash": "orderHash999"})
    assert claim_res.status_code == 200
    conf_path = os.path.join(data_dir, "tunnelsats.conf")
    assert os.path.exists(conf_path)
    with open(conf_path, "r", encoding="utf-8") as fp:
        conf_text = fp.read()
    assert "Address = 10.9.0.77/32" in conf_text
    assert f"PublicKey = {srv_pub}" in conf_text


@patch("nwc.nwc_rpc_call")
def test_pay_invoice_via_nwc_prevents_double_payment_when_lookup_settled(mock_rpc, data_dir):
    from nwc import pay_invoice_via_nwc

    save_nwc_wallet(str(data_dir), VALID_NWC_URI)
    ph = "ef" * 32
    inv = _make_bolt11(amount_hrp="150u", payment_hash_hex=ph)

    def rpc_side_effect(wallet, method, params=None, **kwargs):
        if method == "lookup_invoice":
            return {"state": "settled", "preimage": "77" * 32}
        raise AssertionError("pay_invoice must not be called when lookup_invoice reports settled")

    mock_rpc.side_effect = rpc_side_effect
    res = pay_invoice_via_nwc(str(data_dir), inv, expected_payment_hash=ph, expected_amount_sats=15_000)
    assert res["success"] is True
    assert res["already_paid"] is True
    assert res["preimage"] == "77" * 32


@patch("app.requests.post")
def test_claim_subscription_returns_202_when_upstream_processing(mock_post, client):
    mock_resp = MagicMock()
    mock_resp.status_code = 202
    mock_resp.headers = {"Content-Type": "application/json"}
    mock_resp.content = b'{"status":"processing"}'
    mock_post.return_value = mock_resp

    res = client.post("/api/subscription/claim", json={"paymentHash": "procHash123"})
    assert res.status_code == 202
    assert json.loads(res.data)["status"] == "processing"


def test_evaluate_subscription_notices_70pct_amber_and_100pct_exhausted(data_dir):
    now = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
    n_70 = evaluate_subscription_notices(
        str(data_dir),
        {
            "expiresAt": (now + timedelta(days=20)).isoformat(),
            "subscriptionTelemetry": {
                "bandwidthUsedGb": 75.0,
                "bandwidthLimitGb": 100.0,
                "lastSyncedAt": now.isoformat(),
            },
        },
        now_dt=now,
    )
    assert n_70["phase"] == "on_track"
    assert any(b["id"] == "bandwidth_70" and b["severity"] == "amber" for b in n_70["banners"])

    n_100 = evaluate_subscription_notices(
        str(data_dir),
        {
            "expiresAt": (now + timedelta(days=20)).isoformat(),
            "subscriptionTelemetry": {
                "bandwidthUsedGb": 100.0,
                "bandwidthLimitGb": 100.0,
                "lastSyncedAt": now.isoformat(),
            },
        },
        now_dt=now,
    )
    assert any(b["id"] == "bandwidth_100" and b["severity"] == "red" for b in n_100["banners"])


def test_update_local_metadata_syncs_telemetry_and_clears_matching_pending_renewal(data_dir):
    from app import _update_local_metadata

    meta_path = os.path.join(data_dir, app_module.META_FILE)
    with open(meta_path, "w", encoding="utf-8") as fp:
        json.dump(
            {
                "wgPublicKey": "pubKeySync1",
                "expiresAt": "2026-11-01T00:00:00Z",
                "pendingRenewal": {
                    "paymentHash": "renewHash777",
                    "duration": 6,
                    "amountSats": 45000,
                },
            },
            fp,
        )

    ok = _update_local_metadata(
        {
            "newExpiry": "2027-05-01T00:00:00Z",
            "bandwidth_used_gb": 12.34,
            "bandwidth_limit_gb": 100.0,
            "bandwidth_resets_this_month": 1,
            "max_resets_per_month": 2,
            "status": "active",
        },
        payment_hash="renewHash777",
        wg_pubkey="pubKeySync1",
    )
    assert ok is True
    with open(meta_path, "r", encoding="utf-8") as fp:
        meta = json.load(fp)
    assert meta["expiresAt"] == "2027-05-01T00:00:00Z"
    assert "pendingRenewal" not in meta
    assert meta["lastDuration"] == 6
    assert meta["lastAmountSats"] == 45000
    assert meta["subscriptionTelemetry"]["bandwidthUsedGb"] == 12.34
    assert meta["subscriptionTelemetry"]["resetsThisMonth"] == 1


def test_update_local_metadata_status_only_preserves_last_synced_at(data_dir):
    from app import _update_local_metadata

    meta_path = os.path.join(data_dir, app_module.META_FILE)
    prior_sync = "2026-09-30T23:50:00+00:00"
    with open(meta_path, "w", encoding="utf-8") as fp:
        json.dump(
            {
                "wgPublicKey": "pubKeySync2",
                "expiresAt": "2026-10-15T00:00:00Z",
                "subscriptionTelemetry": {
                    "bandwidthUsedGb": 84.0,
                    "bandwidthLimitGb": 100.0,
                    "resetsThisMonth": 1,
                    "maxResetsPerMonth": 2,
                    "status": "active",
                    "lastSyncedAt": prior_sync,
                },
            },
            fp,
        )

    ok = _update_local_metadata(
        {"newExpiry": "2026-11-15T00:00:00Z", "status": "active"},
        wg_pubkey="pubKeySync2",
    )
    assert ok is True
    with open(meta_path, "r", encoding="utf-8") as fp:
        meta = json.load(fp)
    assert meta["subscriptionTelemetry"]["lastSyncedAt"] == prior_sync
    # Month rollover still triggers because lastSyncedAt was not falsely bumped to October
    tel = compute_bandwidth_telemetry(
        meta["subscriptionTelemetry"],
        now_dt=datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc),
    )
    assert tel["month_rolled_over"] is True
    assert tel["bandwidth_used_gb"] == 0.0


def test_nwc_connect_rejects_unreachable_tor_proxy_and_accepts_reachable(client, data_dir):
    onion_uri = (
        f"nostr+walletconnect://{VALID_WALLET_PUB}"
        f"?relay=ws://abcdef1234567890.onion:4848&secret={VALID_SECRET}"
    )
    with patch("nwc.is_tor_socks_proxy_reachable", return_value=False):
        r_fail_tor = client.post(
            "/api/nwc/connect",
            json={"uri": VALID_NWC_URI, "routeViaTor": True},
        )
        assert r_fail_tor.status_code == 400
        assert "Tor SOCKS5 proxy" in r_fail_tor.get_json()["error"]

        r_fail_onion = client.post(
            "/api/nwc/connect",
            json={"uri": onion_uri},
        )
        assert r_fail_onion.status_code == 400
        assert "Tor SOCKS5 proxy" in r_fail_onion.get_json()["error"]

    with patch("nwc.is_tor_socks_proxy_reachable", return_value=True):
        r_ok = client.post(
            "/api/nwc/connect",
            json={"uri": onion_uri, "routeViaTor": True},
        )
        assert r_ok.status_code == 200
        assert r_ok.get_json()["routeViaTor"] is True


@patch("nwc.nwc_rpc_call")
def test_maybe_nwc_auto_renew_ignores_mismatched_pending_duration_and_persists_before_pay(
    mock_rpc, data_dir
):
    save_nwc_wallet(str(data_dir), VALID_NWC_URI, auto_renew_duration=3)
    now = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
    ph_1m = "11" * 32
    ph_3m = "33" * 32
    inv_1m = _make_bolt11(amount_hrp="100u", payment_hash_hex=ph_1m, timestamp=int(now.timestamp()))
    inv_3m = _make_bolt11(amount_hrp="300u", payment_hash_hex=ph_3m, timestamp=int(now.timestamp()))

    wg_pub = "L7vkSGz/ODjzBTmYo+gkJADq9GRF0NfxjOsFBNDVjQ4="
    meta = {
        "nwcConnected": True,
        "wgPublicKey": wg_pub,
        "serverId": "de2",
        "expiresAt": (now + timedelta(days=5)).isoformat(),
        "pendingRenewal": {
            "paymentHash": ph_1m,
            "invoice": inv_1m,
            "amountSats": 10_000,
            "serverId": "de2",
            "wgPublicKey": wg_pub,
            "duration": 1,
            "createdAt": now.isoformat(),
        },
    }

    persisted_snapshots = []

    def persist_fn(m):
        persisted_snapshots.append(json.loads(json.dumps(m)))

    def create_renewal_fn(pubkey, server_id, duration):
        assert duration == 3
        return {"invoice": inv_3m, "paymentHash": ph_3m, "amountSats": 30_000}

    def rpc_side_effect(wallet, method, params=None, **kw):
        if method == "pay_invoice":
            # Verify pendingRenewal for 3m was persisted BEFORE pay_invoice was called
            assert len(persisted_snapshots) >= 1
            assert persisted_snapshots[-1]["pendingRenewal"]["paymentHash"] == ph_3m
            assert persisted_snapshots[-1]["pendingRenewal"]["duration"] == 3
            return {"preimage": "ab" * 32}
        return {}

    mock_rpc.side_effect = rpc_side_effect
    with patch("nwc.time.time", return_value=now.timestamp()):
        res = maybe_nwc_auto_renew(
            str(data_dir),
            meta,
            create_renewal_fn,
            now_dt=now,
            persist_meta_fn=persist_fn,
        )
    assert res["success"] is True
    assert res["payment_hash"] == ph_3m
    assert res["amount_sats"] == 30_000
    assert meta["lastDuration"] == 3


def test_evaluate_subscription_notices_does_not_suppress_when_nwc_has_last_error(data_dir):
    now = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
    n_err = evaluate_subscription_notices(
        str(data_dir),
        {
            "expiresAt": (now + timedelta(days=2)).isoformat(),
            "nwcConnected": True,
            "nwcAutoRenewState": {
                "attempts": 1,
                "lastError": "NWC relay connection timed out",
                "budgetWarning": False,
                "fallbackTaskRaised": False,
            },
        },
        now_dt=now,
    )
    assert n_err["suppressed_by_nwc"] is False
    assert any(b["id"] == "expiry_3d" for b in n_err["banners"])


def test_apply_vpn_port_skips_when_expected_pubkey_mismatches(data_dir):
    meta_path = os.path.join(data_dir, app_module.META_FILE)
    conf_path = os.path.join(data_dir, "tunnelsats.conf")
    with open(meta_path, "w", encoding="utf-8") as fp:
        json.dump(
            {
                "wgPublicKey": "newPubKey999",
                "serverDomain": "de2.tunnelsats.com",
                "vpnPort": 31111,
            },
            fp,
        )
    with open(conf_path, "w", encoding="utf-8") as fp:
        fp.write("# Port Forwarding: 31111\n[Interface]\nPrivateKey = x\n[Peer]\nPublicKey = y\n")

    assert apply_vpn_port(39999, data_dir=str(data_dir), expected_pubkey="oldPubKey111") is False
    with open(meta_path, "r", encoding="utf-8") as fp:
        assert json.load(fp)["vpnPort"] == 31111


@patch("app.requests.post")
def test_create_subscription_reuses_identical_pending_order_and_preserves_multi_order_keys(
    mock_post, client, data_dir
):
    resp_a = MagicMock()
    resp_a.status_code = 200
    resp_a.headers = {"Content-Type": "application/json"}
    resp_a.content = b'{"paymentHash":"hashOrderA","invoice":"lnbc100u1a","amountSats":10000}'

    resp_b = MagicMock()
    resp_b.status_code = 200
    resp_b.headers = {"Content-Type": "application/json"}
    resp_b.content = b'{"paymentHash":"hashOrderB","invoice":"lnbc300u1b","amountSats":30000}'

    mock_post.side_effect = [resp_a, resp_b]

    # 1. Create Order A (de2, 1 month)
    r1 = client.post("/api/subscription/create", json={"serverId": "de2", "duration": 1})
    assert r1.status_code == 200
    assert json.loads(r1.data)["paymentHash"] == "hashOrderA"

    # 2. Repeat identical create request -> reuses Order A without calling upstream
    r1_reuse = client.post("/api/subscription/create", json={"serverId": "de2", "duration": 1})
    assert r1_reuse.status_code == 200
    r1_reuse_json = r1_reuse.get_json()
    assert r1_reuse_json["reused"] is True
    assert r1_reuse_json["paymentHash"] == "hashOrderA"
    assert mock_post.call_count == 1

    # 3. Create Order B with different duration (3 months) -> creates new invoice, preserves Order A in pendingOrders
    r2 = client.post("/api/subscription/create", json={"serverId": "de2", "duration": 3})
    assert r2.status_code == 200
    assert json.loads(r2.data)["paymentHash"] == "hashOrderB"

    # 4. Claim Order A (even though Order B was created later) -> succeeds using Order A's private key
    with patch("nwc.subprocess.run", side_effect=FileNotFoundError("no wg")):
        _, srv_pub = generate_wg_keypair()

    meta_path = os.path.join(data_dir, app_module.META_FILE)
    with open(meta_path, "r", encoding="utf-8") as fp:
        saved_meta = json.load(fp)
    order_a_pub = saved_meta["pendingOrders"]["hashOrderA"]["wgPublicKey"]
    order_a_priv = saved_meta["pendingOrders"]["hashOrderA"]["wgPrivateKey"]

    claim_payload = {
        "success": True,
        "vpnPort": 35555,
        "expiresAt": "2027-02-01T00:00:00Z",
        "interface": {"address": "10.9.0.88/32", "dns": "8.8.8.8"},
        "peer": {"publicKey": order_a_pub, "allowedIPs": "0.0.0.0/0"},
        "server": {"publicKey": srv_pub, "endpoint": "de2.tunnelsats.com:51820"},
    }
    resp_claim = MagicMock()
    resp_claim.status_code = 200
    resp_claim.headers = {"Content-Type": "application/json"}
    resp_claim.json.return_value = claim_payload
    resp_claim.content = json.dumps(claim_payload).encode("utf-8")
    mock_post.side_effect = None
    mock_post.return_value = resp_claim

    r_claim = client.post("/api/subscription/claim", json={"paymentHash": "hashOrderA"})
    assert r_claim.status_code == 200
    with open(os.path.join(data_dir, "tunnelsats.conf"), "r", encoding="utf-8") as fp:
        conf_text = fp.read()
    assert f"PrivateKey = {order_a_priv}" in conf_text
    assert f"PublicKey = {srv_pub}" in conf_text


def test_subscription_create_requires_management_csrf_when_security_enabled(data_dir):
    app.config["TESTING"] = True
    app.config["MANAGEMENT_BROWSER_SECURITY_ENABLED"] = True
    try:
        with app.test_client() as sec_client:
            res = sec_client.post(
                "/api/subscription/create",
                json={"serverId": "de2", "duration": 1},
            )
            assert res.status_code == 403
    finally:
        app.config["MANAGEMENT_BROWSER_SECURITY_ENABLED"] = False


def test_umbrel_user_agent_header_sent_on_all_outbound_public_api_requests(client, data_dir):
    expected_version = app_module.read_app_version().lstrip("v")
    expected_ua = f"TunnelSats-Umbrel/{expected_version}"
    assert app_module.get_user_agent() == expected_ua
    assert app_module._upstream_headers() == {
        "Content-Type": "application/json",
        "User-Agent": expected_ua,
    }

    with patch.object(app_module, "read_app_version", return_value="invalid version!"):
        assert app_module.get_user_agent() == "TunnelSats-Umbrel/unknown"

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"Content-Type": "application/json"}
    mock_resp.content = b'{"status":"active"}'
    mock_resp.json.return_value = {"status": "active"}

    with patch("app.requests.post", return_value=mock_resp) as mock_post, patch(
        "app.requests.get", return_value=mock_resp
    ) as mock_get:
        app_module.proxy_request("GET", "servers")
        assert mock_get.call_args.kwargs["headers"]["User-Agent"] == expected_ua

        app_module.proxy_request("POST", "subscription/create", {"serverId": "de2", "duration": 1})
        assert mock_post.call_args.kwargs["headers"]["User-Agent"] == expected_ua

        app_module._fetch_subscription_status("pubKey123")
        assert mock_post.call_args.kwargs["headers"]["User-Agent"] == expected_ua

        app_module._create_renewal_invoice_upstream("pubKey123", "de2", 3)
        assert mock_post.call_args.kwargs["headers"]["User-Agent"] == expected_ua

        mock_get.reset_mock()
        r_check = client.get("/api/subscription/hash123")
        assert r_check.status_code == 200
        mock_get.assert_called_once()
        assert mock_get.call_args.kwargs["headers"]["User-Agent"] == expected_ua

        mock_post.reset_mock()
        client.post(
            "/api/subscription/claim",
            json={"paymentHash": "hash123", "wgPublicKey": "pubKey123"},
        )
        mock_post.assert_called_once()
        assert mock_post.call_args.kwargs["headers"]["User-Agent"] == expected_ua

        mock_post.reset_mock()
        response = client.post(
            "/api/reachability",
            json={"socket": f"{'02' + 'a' * 64}@de2.tunnelsats.com:31234"},
        )
        assert response.status_code == 200
        mock_post.assert_called_once()
        assert mock_post.call_args.args[0] == f"{app_module.TUNNELSATS_API_URL}/ping/test"
        assert mock_post.call_args.kwargs["headers"]["User-Agent"] == expected_ua


@patch("app.proxy_request")
def test_get_servers_enriches_all_public_api_offered_servers_with_distinct_geodata(mock_proxy, client):
    upstream_servers = [
        {"id": "us-east", "country": "USA", "city": "Ashburn", "flag": "🇺🇸", "status": "online"},
        {"id": "us-west", "country": "USA", "city": "Hillsboro", "flag": "🇺🇸", "status": "online"},
        {"id": "eu-de", "country": "Germany", "city": "Nuremberg", "flag": "🇩🇪", "status": "online"},
        {"id": "eu-ch", "country": "Switzerland", "city": "Geneva", "flag": "🇨🇭", "status": "online"},
        {"id": "asia-sg", "country": "Singapore", "city": "Singapore", "flag": "🇸🇬", "status": "online"},
        {"id": "sa-br", "country": "Brazil", "city": "Sao Paulo", "flag": "🇧🇷", "status": "online"},
        {"id": "oc-au", "country": "Australia", "city": "Sydney", "flag": "🇦🇺", "status": "online"},
    ]
    mock_proxy.return_value = (
        json.dumps({"servers": upstream_servers}).encode("utf-8"),
        200,
        [("Content-Type", "application/json")],
    )

    res = client.get("/api/servers")
    assert res.status_code == 200
    servers = {s["id"]: s for s in res.get_json()["servers"]}

    assert servers["us-east"]["label"] == "ASHBURN, US"
    assert servers["us-east"]["lat"] == 39.0438
    assert servers["us-west"]["label"] == "HILLSBORO, US"
    assert servers["us-west"]["lat"] == 45.5231
    assert servers["eu-de"]["label"] == "NUREMBERG, DE"
    assert servers["eu-de"]["lat"] == 49.4521
    assert servers["eu-ch"]["label"] == "GENEVA, CH"
    assert servers["eu-ch"]["lat"] == 46.2044
    assert servers["asia-sg"]["label"] == "SINGAPORE, SG"
    assert servers["asia-sg"]["lat"] == 1.3521
    assert servers["sa-br"]["label"] == "SAO PAULO, BR"
    assert servers["sa-br"]["lat"] == -23.5505
    assert servers["oc-au"]["label"] == "SYDNEY, AU"
    assert servers["oc-au"]["lat"] == -33.8688




