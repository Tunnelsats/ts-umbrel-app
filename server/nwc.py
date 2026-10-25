"""Pure-Python NWC (NIP-47), X25519, BOLT11, and Subscription Telemetry Engine for TunnelSats."""

import base64
import calendar
import hashlib
import ipaddress
import json
import math
import os
import re
import socket
import ssl
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from urllib.parse import parse_qsl, quote, urlparse

NWC_WALLET_FILE = "nwc-wallet.json"
SUBSCRIPTION_NOTICES_FILE = "subscription-notices.json"
NWC_MAX_RELAYS = 3
NWC_AUTO_RENEW_DEADLINE_SECONDS = 180
NWC_RENEW_WINDOW_DAYS = 7
NWC_MAX_AUTO_RENEW_SATS = 500_000
NWC_BACKOFF_HOURS = (1, 6, 12)
NWC_MAX_ATTEMPTS = 3

REFERRAL_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_WG_KEY_B64_RE = re.compile(r"^[A-Za-z0-9+/]{43}=$")
_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_BOLT11_AMOUNT_RE = re.compile(
    r"^ln(?:bcrt|bc|tbs|tb|sb)(?:(\d+)([munp])?)?1[0-9a-z]{7,}$",
    re.IGNORECASE,
)
_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_MAP = {c: i for i, c in enumerate(_BECH32_CHARSET)}

TOR_SOCKS_HOST = os.environ.get("TOR_PROXY_IP", os.environ.get("TOR_SOCKS_HOST", "127.0.0.1"))
TOR_SOCKS_PORT = int(os.environ.get("TOR_PROXY_PORT", os.environ.get("TOR_SOCKS_PORT", "9050")) or 9050)


class NwcError(Exception):
    """Error raised during NIP-47 Nostr Wallet Connect operations."""

    def __init__(self, message: str, code: str = "INTERNAL", permanent: bool = False):
        super().__init__(message)
        self.code = code
        self.permanent = permanent


# ---------------------------------------------------------------------------
# 1. Pure-Python RFC 7748 X25519 Keygen & WireGuard Config Assembly
# ---------------------------------------------------------------------------

def _x25519_scalarmult_base(scalar_bytes: bytes) -> bytes:
    """RFC 7748 X25519 scalar multiplication on base point u = 9 (pure Python)."""
    if len(scalar_bytes) != 32:
        raise ValueError("X25519 private key must be exactly 32 bytes")
    p = (1 << 255) - 19
    a24 = 121665
    k_bytes = bytearray(scalar_bytes)
    k_bytes[0] &= 248
    k_bytes[31] &= 127
    k_bytes[31] |= 64
    k = int.from_bytes(k_bytes, "little")

    x_1 = 9
    x_2 = 1
    z_2 = 0
    x_3 = 9
    z_3 = 1
    swap = 0

    for t in range(254, -1, -1):
        k_t = (k >> t) & 1
        swap ^= k_t
        if swap:
            x_2, x_3 = x_3, x_2
            z_2, z_3 = z_3, z_2
        swap = k_t

        a = (x_2 + z_2) % p
        aa = (a * a) % p
        b = (x_2 - z_2) % p
        bb = (b * b) % p
        e = (aa - bb) % p
        c = (x_3 + z_3) % p
        d = (x_3 - z_3) % p
        da = (d * a) % p
        cb = (c * b) % p
        x_3 = pow(da + cb, 2, p)
        z_3 = (x_1 * pow(da - cb, 2, p)) % p
        x_2 = (aa * bb) % p
        z_2 = (e * (aa + a24 * e)) % p

    if swap:
        x_2, x_3 = x_3, x_2
        z_2, z_3 = z_3, z_2

    res = (x_2 * pow(z_2, p - 2, p)) % p
    return res.to_bytes(32, "little")


def validate_wg_public_key(pubkey: str) -> bool:
    """Validate that a string is a 44-char base64 WireGuard key decoding to 32 bytes."""
    if not isinstance(pubkey, str):
        return False
    cleaned = pubkey.strip()
    if len(cleaned) != 44 or not _WG_KEY_B64_RE.fullmatch(cleaned):
        return False
    try:
        raw = base64.b64decode(cleaned, validate=True)
    except Exception:
        return False
    return len(raw) == 32


def derive_wg_public_key(private_key: str) -> str:
    """Derive a WireGuard X25519 public key from a base64 private key."""
    if not validate_wg_public_key(private_key):
        return ""
    cleaned = private_key.strip()
    try:
        res = subprocess.run(
            ["wg", "pubkey"],
            input=cleaned,
            text=True,
            capture_output=True,
            check=True,
            timeout=5,
        )
        out = res.stdout.strip()
        if validate_wg_public_key(out):
            return out
    except (subprocess.SubprocessError, OSError):
        pass

    try:
        raw_priv = base64.b64decode(cleaned, validate=True)
        raw_pub = _x25519_scalarmult_base(raw_priv)
        return base64.b64encode(raw_pub).decode("ascii")
    except Exception:
        return ""


def generate_wg_keypair() -> Tuple[str, str]:
    """Generate a WireGuard X25519 (private_key, public_key) pair."""
    try:
        gen_res = subprocess.run(
            ["wg", "genkey"],
            text=True,
            capture_output=True,
            check=True,
            timeout=5,
        )
        priv_b64 = gen_res.stdout.strip()
        pub_res = subprocess.run(
            ["wg", "pubkey"],
            input=priv_b64,
            text=True,
            capture_output=True,
            check=True,
            timeout=5,
        )
        pub_b64 = pub_res.stdout.strip()
        if validate_wg_public_key(priv_b64) and validate_wg_public_key(pub_b64):
            return priv_b64, pub_b64
    except (subprocess.SubprocessError, OSError):
        pass

    raw_priv = bytearray(os.urandom(32))
    raw_priv[0] &= 248
    raw_priv[31] &= 127
    raw_priv[31] |= 64
    priv_bytes = bytes(raw_priv)
    pub_bytes = _x25519_scalarmult_base(priv_bytes)
    return (
        base64.b64encode(priv_bytes).decode("ascii"),
        base64.b64encode(pub_bytes).decode("ascii"),
    )


def assemble_wg_config(
    private_key: str,
    data: Dict[str, Any],
    fallback_server_id: str = "",
) -> Optional[str]:
    """Assemble a complete WireGuard config from structured claim fields and local private key."""
    if not isinstance(private_key, str) or not private_key.strip():
        return None
    if not isinstance(data, dict):
        return None

    priv_clean = private_key.strip()
    peer = data.get("peer") if isinstance(data.get("peer"), dict) else {}
    interface = data.get("interface") if isinstance(data.get("interface"), dict) else {}
    server = data.get("server") if isinstance(data.get("server"), dict) else {}
    subscription = data.get("subscription") if isinstance(data.get("subscription"), dict) else {}

    address = (
        interface.get("address")
        or interface.get("ip")
        or peer.get("address")
        or data.get("vpnInternalIp")
        or data.get("internalIp")
        or data.get("assignedIp")
        or data.get("address")
        or data.get("clientIp")
    )
    if not address or not isinstance(address, str):
        return None
    address = address.strip()
    if "/" not in address:
        address = f"{address}/32"

    dns_raw = interface.get("dns") or data.get("dns") or "8.8.8.8"
    if isinstance(dns_raw, list):
        dns = ", ".join(str(x).strip() for x in dns_raw if str(x).strip()) or "8.8.8.8"
    else:
        dns = str(dns_raw).strip() or "8.8.8.8"

    server_pubkey = (
        peer.get("publicKey")
        or peer.get("serverPublicKey")
        or server.get("publicKey")
        or server.get("wgPublicKey")
        or data.get("serverPublicKey")
        or data.get("peerPublicKey")
        or data.get("publicKey")
    )
    if not server_pubkey or not isinstance(server_pubkey, str):
        return None
    server_pubkey = server_pubkey.strip()

    preshared_key = (
        peer.get("presharedKey")
        or interface.get("presharedKey")
        or data.get("wgPresharedKey")
        or data.get("presharedKey")
        or ""
    )
    preshared_key = str(preshared_key).strip() if preshared_key else ""

    allowed_ips_raw = (
        peer.get("allowedIPs")
        or peer.get("allowedIps")
        or server.get("allowedIPs")
        or data.get("allowedIPs")
        or "0.0.0.0/0"
    )
    if isinstance(allowed_ips_raw, list):
        allowed_ips = ", ".join(str(x).strip() for x in allowed_ips_raw if str(x).strip()) or "0.0.0.0/0"
    else:
        allowed_ips = str(allowed_ips_raw).strip() or "0.0.0.0/0"

    endpoint = (
        peer.get("endpoint")
        or server.get("endpoint")
        or data.get("endpoint")
        or ""
    )
    endpoint = str(endpoint).strip() if endpoint else ""

    server_domain = (
        server.get("domain")
        or server.get("host")
        or data.get("serverDomain")
        or data.get("vpnServerDomain")
        or ""
    )
    server_domain = str(server_domain).strip() if server_domain else ""
    if not server_domain and endpoint and ":" in endpoint:
        server_domain = endpoint.rsplit(":", 1)[0].strip()
    if not server_domain and fallback_server_id:
        clean_id = str(fallback_server_id).strip()
        if clean_id:
            server_domain = clean_id if "." in clean_id else f"{clean_id}.tunnelsats.com"

    endpoint_port = (
        server.get("wireguardPort")
        or server.get("port")
        or data.get("endpointPort")
        or 51820
    )
    if not endpoint and server_domain:
        endpoint = f"{server_domain}:{endpoint_port}"
    if not endpoint:
        return None

    vpn_port = (
        data.get("vpnPort")
        or data.get("portForwarding")
        or peer.get("vpnPort")
        or peer.get("portForwarding")
        or interface.get("vpnPort")
        or server.get("vpnPort")
        or subscription.get("vpnPort")
    )
    expires_at = (
        data.get("expiresAt")
        or data.get("subscriptionEnd")
        or subscription.get("expiresAt")
        or peer.get("expiresAt")
        or ""
    )
    client_pubkey = (
        data.get("wgPublicKey")
        or interface.get("publicKey")
        or peer.get("clientPublicKey")
        or ""
    )
    if not client_pubkey and validate_wg_public_key(priv_clean):
        client_pubkey = derive_wg_public_key(priv_clean)

    lines = [
        "[Interface]",
        f"PrivateKey = {priv_clean}",
        f"Address = {address}",
        f"DNS = {dns}",
    ]
    if vpn_port is not None and str(vpn_port).strip() != "":
        lines.append(f"# Port Forwarding: {vpn_port}")
    if server_domain:
        lines.append(f"# VPN Server Domain: {server_domain}")
    if client_pubkey:
        lines.append(f"# WG Public Key: {client_pubkey}")
    if expires_at:
        lines.append(f"# Subscription End: {expires_at}")

    lines.append("")
    lines.append("[Peer]")
    lines.append(f"PublicKey = {server_pubkey}")
    if preshared_key:
        lines.append(f"PresharedKey = {preshared_key}")
    lines.append(f"AllowedIPs = {allowed_ips}")
    lines.append(f"Endpoint = {endpoint}")
    lines.append("PersistentKeepalive = 25")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 2. Pure-Python BOLT11 Pre-Payment Verifier
# ---------------------------------------------------------------------------

def _bech32_polymod(values: List[int]) -> int:
    gen = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for v in values:
        b = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ v
        for i in range(5):
            if (b >> i) & 1:
                chk ^= gen[i]
    return chk


def _bech32_hrp_expand(hrp: str) -> List[int]:
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _bech32_verify_checksum(hrp: str, data_words: List[int]) -> bool:
    return _bech32_polymod(_bech32_hrp_expand(hrp) + data_words) == 1


def _bolt11_amount_sats(invoice: str) -> Optional[int]:
    """Parse whole-satoshi amount from BOLT11 HRP, returning None if missing or fractional."""
    if not isinstance(invoice, str):
        return None
    m = _BOLT11_AMOUNT_RE.match(invoice.strip())
    if not m or not m.group(1):
        return None
    value = int(m.group(1))
    unit = (m.group(2) or "").lower()
    if unit == "":
        return value * 100_000_000
    if unit == "m":
        return value * 100_000
    if unit == "u":
        return value * 100
    if unit == "n":
        return value // 10 if value % 10 == 0 else None
    if unit == "p":
        return value // 10_000 if value % 10_000 == 0 else None
    return None


def verify_bolt11_invoice(
    invoice: str,
    expected_payment_hash: Optional[str] = None,
    expected_amount_sats: Optional[int] = None,
    max_amount_sats: int = NWC_MAX_AUTO_RENEW_SATS,
    check_expiry: bool = True,
) -> Dict[str, Any]:
    """Cryptographically and structurally verify a BOLT11 invoice before NWC payment."""
    if not isinstance(invoice, str) or not invoice.strip():
        return {"valid": False, "error": "Missing BOLT11 invoice"}

    s = invoice.strip().lower()
    if not s.startswith("lnbc"):
        return {"valid": False, "error": "BOLT11 invoice is not for Bitcoin mainnet (lnbc)"}

    pos = s.rfind("1")
    if pos < 4 or pos + 7 > len(s):
        return {"valid": False, "error": "Malformed BOLT11 Bech32 separator"}

    hrp = s[:pos]
    data_part = s[pos + 1 :]
    try:
        data_words = [_BECH32_MAP[c] for c in data_part]
    except KeyError:
        return {"valid": False, "error": "Invalid Bech32 character in BOLT11 invoice"}

    if not _bech32_verify_checksum(hrp, data_words):
        return {"valid": False, "error": "BOLT11 Bech32 checksum verification failed"}

    words = data_words[:-6]
    if len(words) < 7 + 104:
        return {"valid": False, "error": "BOLT11 payload is too short"}

    amount_sats = _bolt11_amount_sats(s)
    if amount_sats is None or amount_sats <= 0:
        return {"valid": False, "error": "BOLT11 invoice is missing a positive whole-satoshi amount"}

    if max_amount_sats > 0 and amount_sats > max_amount_sats:
        return {
            "valid": False,
            "error": f"BOLT11 invoice amount ({amount_sats} sats) exceeds safety cap ({max_amount_sats} sats)",
        }

    if expected_amount_sats is not None:
        try:
            exp_amt = int(expected_amount_sats)
        except (TypeError, ValueError):
            return {"valid": False, "error": "Invalid expected_amount_sats"}
        if amount_sats != exp_amt:
            return {
                "valid": False,
                "error": f"BOLT11 invoice amount ({amount_sats} sats) does not match expected ({exp_amt} sats)",
            }

    timestamp = 0
    for w in words[:7]:
        timestamp = (timestamp << 5) | w

    tagged = words[7:-104]
    idx = 0
    payment_hash_hex: Optional[str] = None
    expiry_seconds = 3600

    while idx + 3 <= len(tagged):
        tag_type = tagged[idx]
        data_len = (tagged[idx + 1] << 5) | tagged[idx + 2]
        idx += 3
        if idx + data_len > len(tagged):
            return {"valid": False, "error": "Truncated tagged field in BOLT11 invoice"}
        field_words = tagged[idx : idx + data_len]
        idx += data_len

        if tag_type == 1 and data_len == 52 and payment_hash_hex is None:
            acc = 0
            for w in field_words:
                acc = (acc << 5) | w
            acc >>= 4
            payment_hash_hex = acc.to_bytes(32, "big").hex()
        elif tag_type == 6 and data_len > 0:
            exp_val = 0
            for w in field_words:
                exp_val = (exp_val << 5) | w
            expiry_seconds = exp_val

    if not payment_hash_hex:
        return {"valid": False, "error": "BOLT11 invoice is missing payment_hash (p tag)"}

    if expected_payment_hash is not None and str(expected_payment_hash).strip():
        exp_hash = str(expected_payment_hash).strip().lower()
        if payment_hash_hex.lower() != exp_hash:
            return {
                "valid": False,
                "error": "BOLT11 invoice payment_hash does not match expected paymentHash",
            }

    if check_expiry:
        now_ts = int(time.time())
        if now_ts >= timestamp + expiry_seconds:
            return {"valid": False, "error": "BOLT11 invoice has expired"}

    return {
        "valid": True,
        "amount_sats": amount_sats,
        "payment_hash": payment_hash_hex,
        "timestamp": timestamp,
        "expiry_seconds": expiry_seconds,
    }


# ---------------------------------------------------------------------------
# 3. Pure-Python secp256k1 / BIP-340 Schnorr & NIP-04 AES-256-CBC
# ---------------------------------------------------------------------------

_SECP_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_SECP_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_SECP_G = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
)


def _secp_point_add(
    p1: Optional[Tuple[int, int]], p2: Optional[Tuple[int, int]]
) -> Optional[Tuple[int, int]]:
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and y1 != y2:
        return None
    if x1 == x2:
        lam = (3 * x1 * x1 * pow(2 * y1, _SECP_P - 2, _SECP_P)) % _SECP_P
    else:
        lam = ((y2 - y1) * pow(x2 - x1, _SECP_P - 2, _SECP_P)) % _SECP_P
    x3 = (lam * lam - x1 - x2) % _SECP_P
    y3 = (lam * (x1 - x3) - y1) % _SECP_P
    return (x3, y3)


def _secp_point_mul(k: int, pt: Tuple[int, int] = _SECP_G) -> Optional[Tuple[int, int]]:
    k = k % _SECP_N
    if k == 0:
        return None
    res = None
    addend: Optional[Tuple[int, int]] = pt
    while k:
        if k & 1:
            res = _secp_point_add(res, addend)
        addend = _secp_point_add(addend, addend)
        k >>= 1
    return res


def _secp_lift_x(x: int) -> Optional[Tuple[int, int]]:
    if x <= 0 or x >= _SECP_P:
        return None
    y_sq = (pow(x, 3, _SECP_P) + 7) % _SECP_P
    y = pow(y_sq, (_SECP_P + 1) // 4, _SECP_P)
    if pow(y, 2, _SECP_P) != y_sq:
        return None
    return (x, y if (y & 1) == 0 else _SECP_P - y)


def _tagged_hash(tag: str, msg: bytes) -> bytes:
    tag_hash = hashlib.sha256(tag.encode("utf-8")).digest()
    return hashlib.sha256(tag_hash + tag_hash + msg).digest()


def _nostr_pubkey_from_secret(secret_hex: str) -> str:
    d0 = int(secret_hex, 16)
    if not (1 <= d0 < _SECP_N):
        raise ValueError("Invalid Nostr secret key scalar")
    pt = _secp_point_mul(d0)
    if pt is None:
        raise ValueError("Point at infinity")
    return pt[0].to_bytes(32, "big").hex()


def _schnorr_sign(secret_hex: str, msg32: bytes, aux_rand: Optional[bytes] = None) -> str:
    if len(msg32) != 32:
        raise ValueError("BIP-340 message must be 32 bytes")
    d0 = int(secret_hex, 16)
    if not (1 <= d0 < _SECP_N):
        raise ValueError("Invalid secret scalar")
    pt = _secp_point_mul(d0)
    if pt is None:
        raise ValueError("Invalid public point")
    px, py = pt
    d = d0 if (py & 1) == 0 else (_SECP_N - d0)
    aux = aux_rand if (aux_rand and len(aux_rand) == 32) else os.urandom(32)
    t = d ^ int.from_bytes(_tagged_hash("BIP0340/aux", aux), "big")
    px_bytes = px.to_bytes(32, "big")
    rand = _tagged_hash("BIP0340/nonce", t.to_bytes(32, "big") + px_bytes + msg32)
    k0 = int.from_bytes(rand, "big") % _SECP_N
    if k0 == 0:
        raise ValueError("Zero nonce")
    r_pt = _secp_point_mul(k0)
    if r_pt is None:
        raise ValueError("Invalid nonce point")
    rx, ry = r_pt
    k = k0 if (ry & 1) == 0 else (_SECP_N - k0)
    rx_bytes = rx.to_bytes(32, "big")
    e = int.from_bytes(_tagged_hash("BIP0340/challenge", rx_bytes + px_bytes + msg32), "big") % _SECP_N
    sig_s = (k + e * d) % _SECP_N
    return (rx_bytes + sig_s.to_bytes(32, "big")).hex()


def _schnorr_verify(pubkey_hex: str, msg32: bytes, sig_hex: str) -> bool:
    try:
        sig = bytes.fromhex(sig_hex)
        if len(sig) != 64 or len(msg32) != 32:
            return False
        pt = _secp_lift_x(int(pubkey_hex, 16))
        if pt is None:
            return False
        r = int.from_bytes(sig[:32], "big")
        s = int.from_bytes(sig[32:], "big")
        if r >= _SECP_P or s >= _SECP_N:
            return False
        e = int.from_bytes(
            _tagged_hash("BIP0340/challenge", sig[:32] + pt[0].to_bytes(32, "big") + msg32),
            "big",
        ) % _SECP_N
        r_pt = _secp_point_add(_secp_point_mul(s), _secp_point_mul(_SECP_N - e, pt))
        if r_pt is None or (r_pt[1] & 1) != 0 or r_pt[0] != r:
            return False
        return True
    except Exception:
        return False


def _nip04_shared_secret(secret_hex: str, peer_pubkey_hex: str) -> bytes:
    d = int(secret_hex, 16)
    if not (1 <= d < _SECP_N):
        raise ValueError("Invalid client secret scalar")
    peer_pt = _secp_lift_x(int(peer_pubkey_hex, 16))
    if peer_pt is None:
        raise ValueError("Peer pubkey is not a valid secp256k1 point")
    shared_pt = _secp_point_mul(d, peer_pt)
    if shared_pt is None:
        raise ValueError("Invalid ECDH shared point")
    return shared_pt[0].to_bytes(32, "big")


# Pure-Python AES-256-CBC implementation (zero external C/CLI dependencies)
_AES_SBOX = (
    0x63, 0x7C, 0x77, 0x7B, 0xF2, 0x6B, 0x6F, 0xC5, 0x30, 0x01, 0x67, 0x2B, 0xFE, 0xD7, 0xAB, 0x76,
    0xCA, 0x82, 0xC9, 0x7D, 0xFA, 0x59, 0x47, 0xF0, 0xAD, 0xD4, 0xA2, 0xAF, 0x9C, 0xA4, 0x72, 0xC0,
    0xB7, 0xFD, 0x93, 0x26, 0x36, 0x3F, 0xF7, 0xCC, 0x34, 0xA5, 0xE5, 0xF1, 0x71, 0xD8, 0x31, 0x15,
    0x04, 0xC7, 0x23, 0xC3, 0x18, 0x96, 0x05, 0x9A, 0x07, 0x12, 0x80, 0xE2, 0xEB, 0x27, 0xB2, 0x75,
    0x09, 0x83, 0x2C, 0x1A, 0x1B, 0x6E, 0x5A, 0xA0, 0x52, 0x3B, 0xD6, 0xB3, 0x29, 0xE3, 0x2F, 0x84,
    0x53, 0xD1, 0x00, 0xED, 0x20, 0xFC, 0xB1, 0x5B, 0x6A, 0xCB, 0xBE, 0x39, 0x4A, 0x4C, 0x58, 0xCF,
    0xD0, 0xEF, 0xAA, 0xFB, 0x43, 0x4D, 0x33, 0x85, 0x45, 0xF9, 0x02, 0x7F, 0x50, 0x3C, 0x9F, 0xA8,
    0x51, 0xA3, 0x40, 0x8F, 0x92, 0x9D, 0x38, 0xF5, 0xBC, 0xB6, 0xDA, 0x21, 0x10, 0xFF, 0xF3, 0xD2,
    0xCD, 0x0C, 0x13, 0xEC, 0x5F, 0x97, 0x44, 0x17, 0xC4, 0xA7, 0x7E, 0x3D, 0x64, 0x5D, 0x19, 0x73,
    0x60, 0x81, 0x4F, 0xDC, 0x22, 0x2A, 0x90, 0x88, 0x46, 0xEE, 0xB8, 0x14, 0xDE, 0x5E, 0x0B, 0xDB,
    0xE0, 0x32, 0x3A, 0x0A, 0x49, 0x06, 0x24, 0x5C, 0xC2, 0xD3, 0xAC, 0x62, 0x91, 0x95, 0xE4, 0x79,
    0xE7, 0xC8, 0x37, 0x6D, 0x8D, 0xD5, 0x4E, 0xA9, 0x6C, 0x56, 0xF4, 0xEA, 0x65, 0x7A, 0xAE, 0x08,
    0xBA, 0x78, 0x25, 0x2E, 0x1C, 0xA6, 0xB4, 0xC6, 0xE8, 0xDD, 0x74, 0x1F, 0x4B, 0xBD, 0x8B, 0x8A,
    0x70, 0x3E, 0xB5, 0x66, 0x48, 0x03, 0xF6, 0x0E, 0x61, 0x35, 0x57, 0xB9, 0x86, 0xC1, 0x1D, 0x9E,
    0xE1, 0xF8, 0x98, 0x11, 0x69, 0xD9, 0x8E, 0x94, 0x9B, 0x1E, 0x87, 0xE9, 0xCE, 0x55, 0x28, 0xDF,
    0x8C, 0xA1, 0x89, 0x0D, 0xBF, 0xE6, 0x42, 0x68, 0x41, 0x99, 0x2D, 0x0F, 0xB0, 0x54, 0xBB, 0x16,
)
_AES_INV_SBOX = tuple(_AES_SBOX.index(i) for i in range(256))
_AES_RCON = (0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36, 0x6C, 0xD8, 0xAB, 0x4D)
_AES_SHIFT_ROWS = (0, 5, 10, 15, 4, 9, 14, 3, 8, 13, 2, 7, 12, 1, 6, 11)
_AES_INV_SHIFT_ROWS = (0, 13, 10, 7, 4, 1, 14, 11, 8, 5, 2, 15, 12, 9, 6, 3)


def _aes_xtime(a: int) -> int:
    return ((a << 1) ^ 0x1B) & 0xFF if (a & 0x80) else (a << 1)


def _aes256_expand_key(key32: bytes) -> List[bytes]:
    if len(key32) != 32:
        raise ValueError("AES-256 key must be 32 bytes")
    w = [int.from_bytes(key32[4 * i : 4 * (i + 1)], "big") for i in range(8)]
    for i in range(8, 60):
        temp = w[i - 1]
        if i % 8 == 0:
            rot = ((temp << 8) | (temp >> 24)) & 0xFFFFFFFF
            sub = (
                (_AES_SBOX[(rot >> 24) & 0xFF] << 24)
                | (_AES_SBOX[(rot >> 16) & 0xFF] << 16)
                | (_AES_SBOX[(rot >> 8) & 0xFF] << 8)
                | _AES_SBOX[rot & 0xFF]
            )
            temp = sub ^ (_AES_RCON[i // 8] << 24)
        elif i % 8 == 4:
            temp = (
                (_AES_SBOX[(temp >> 24) & 0xFF] << 24)
                | (_AES_SBOX[(temp >> 16) & 0xFF] << 16)
                | (_AES_SBOX[(temp >> 8) & 0xFF] << 8)
                | _AES_SBOX[temp & 0xFF]
            )
        w.append(w[i - 8] ^ temp)
    round_keys: List[bytes] = []
    for r in range(15):
        rk = bytearray(16)
        for c in range(4):
            rk[4 * c : 4 * (c + 1)] = w[4 * r + c].to_bytes(4, "big")
        round_keys.append(bytes(rk))
    return round_keys


def _aes256_encrypt_block(block16: bytes, round_keys: List[bytes]) -> bytes:
    rk0 = round_keys[0]
    s = [block16[i] ^ rk0[i] for i in range(16)]
    for r in range(1, 14):
        s = [_AES_SBOX[s[i]] for i in _AES_SHIFT_ROWS]
        rk = round_keys[r]
        for c in range(0, 16, 4):
            a0, a1, a2, a3 = s[c], s[c + 1], s[c + 2], s[c + 3]
            t = a0 ^ a1 ^ a2 ^ a3
            s[c] = a0 ^ t ^ _aes_xtime(a0 ^ a1) ^ rk[c]
            s[c + 1] = a1 ^ t ^ _aes_xtime(a1 ^ a2) ^ rk[c + 1]
            s[c + 2] = a2 ^ t ^ _aes_xtime(a2 ^ a3) ^ rk[c + 2]
            s[c + 3] = a3 ^ t ^ _aes_xtime(a3 ^ a0) ^ rk[c + 3]
    rk14 = round_keys[14]
    return bytes(_AES_SBOX[s[_AES_SHIFT_ROWS[i]]] ^ rk14[i] for i in range(16))


def _aes256_decrypt_block(block16: bytes, round_keys: List[bytes]) -> bytes:
    rk14 = round_keys[14]
    s = [block16[i] ^ rk14[i] for i in range(16)]
    for r in range(13, 0, -1):
        rk = round_keys[r]
        s = [_AES_INV_SBOX[s[_AES_INV_SHIFT_ROWS[i]]] ^ rk[i] for i in range(16)]
        for c in range(0, 16, 4):
            a0, a1, a2, a3 = s[c], s[c + 1], s[c + 2], s[c + 3]
            u = _aes_xtime(_aes_xtime(a0 ^ a2))
            v = _aes_xtime(_aes_xtime(a1 ^ a3))
            b0, b1, b2, b3 = a0 ^ u, a1 ^ v, a2 ^ u, a3 ^ v
            t = b0 ^ b1 ^ b2 ^ b3
            s[c] = b0 ^ t ^ _aes_xtime(b0 ^ b1)
            s[c + 1] = b1 ^ t ^ _aes_xtime(b1 ^ b2)
            s[c + 2] = b2 ^ t ^ _aes_xtime(b2 ^ b3)
            s[c + 3] = b3 ^ t ^ _aes_xtime(b3 ^ b0)
    rk0 = round_keys[0]
    return bytes(_AES_INV_SBOX[s[_AES_INV_SHIFT_ROWS[i]]] ^ rk0[i] for i in range(16))


def _nip04_encrypt(shared_key_bytes: bytes, plaintext: str) -> str:
    iv = os.urandom(16)
    raw = plaintext.encode("utf-8")
    pad_len = 16 - (len(raw) % 16)
    padded = raw + bytes([pad_len] * pad_len)
    round_keys = _aes256_expand_key(shared_key_bytes)
    out = bytearray()
    prev = iv
    for offset in range(0, len(padded), 16):
        blk = bytes(padded[offset + i] ^ prev[i] for i in range(16))
        prev = _aes256_encrypt_block(blk, round_keys)
        out.extend(prev)
    return f"{base64.b64encode(bytes(out)).decode('ascii')}?iv={base64.b64encode(iv).decode('ascii')}"


def _nip04_decrypt(shared_key_bytes: bytes, content: str) -> str:
    if not isinstance(content, str) or "?iv=" not in content:
        raise ValueError("Invalid NIP-04 encrypted content")
    ct_b64, _, iv_b64 = content.partition("?iv=")
    ciphertext = base64.b64decode(ct_b64)
    iv = base64.b64decode(iv_b64)
    if len(iv) != 16 or len(ciphertext) == 0 or len(ciphertext) % 16 != 0:
        raise ValueError("Invalid NIP-04 ciphertext or IV length")
    round_keys = _aes256_expand_key(shared_key_bytes)
    padded = bytearray()
    prev = iv
    for offset in range(0, len(ciphertext), 16):
        ct_blk = ciphertext[offset : offset + 16]
        dec_blk = _aes256_decrypt_block(ct_blk, round_keys)
        padded.extend(dec_blk[i] ^ prev[i] for i in range(16))
        prev = ct_blk
    pad_len = padded[-1]
    if pad_len < 1 or pad_len > 16 or bytes(padded[-pad_len:]) != bytes([pad_len] * pad_len):
        raise ValueError("Invalid NIP-04 PKCS#7 padding")
    return bytes(padded[:-pad_len]).decode("utf-8")


# ---------------------------------------------------------------------------
# 4. NWC URI Parsing, Persistence, and NIP-47 WebSocket Client
# ---------------------------------------------------------------------------

def parse_nwc_uri(uri: str, route_via_tor: Optional[bool] = None) -> Dict[str, Any]:
    """Validate and normalize a NIP-47 nostr+walletconnect:// URI (capping at 3 relays)."""
    if not isinstance(uri, str) or not uri.strip():
        raise ValueError("NWC Connection URI is required.")

    trimmed = uri.strip()
    lower = trimmed.lower()
    if lower.startswith("nostr+walletconnect://"):
        rest = trimmed[len("nostr+walletconnect://") :]
    elif lower.startswith("nostrwalletconnect://"):
        rest = trimmed[len("nostrwalletconnect://") :]
    else:
        raise ValueError("NWC Connection URI must start with nostr+walletconnect://")

    pubkey_part, sep, query_part = rest.partition("?")
    wallet_pubkey = pubkey_part.strip().rstrip("/").lower()
    if not _HEX64_RE.fullmatch(wallet_pubkey) or _secp_lift_x(int(wallet_pubkey, 16)) is None:
        raise ValueError("NWC Connection URI must contain a valid 64-character hex wallet pubkey.")
    if not sep or not query_part:
        raise ValueError("NWC Connection URI is missing query parameters (?relay=...&secret=...).")

    params = parse_qsl(query_part, keep_blank_values=True)
    raw_relays = [v.strip() for k, v in params if k == "relay" and v.strip()]
    if not raw_relays:
        raise ValueError("NWC Connection URI must include at least one relay= parameter.")

    relays: List[str] = []
    relay_hosts: List[str] = []
    has_onion_relay = False

    for raw_relay in raw_relays:
        parsed_relay = urlparse(raw_relay)
        scheme = (parsed_relay.scheme or "").lower()
        hostname = (parsed_relay.hostname or "").lower()
        if not hostname:
            raise ValueError("Invalid relay URL in NWC Connection URI.")
        is_onion = hostname.endswith(".onion")
        if scheme == "ws":
            if not is_onion:
                raise ValueError(
                    f"Plaintext ws:// relay ({hostname}) is only allowed for .onion hidden services."
                )
            if route_via_tor is False:
                raise ValueError(
                    f"Onion ws:// relay ({hostname}) requires Tor routing to be enabled."
                )
        elif scheme != "wss":
            raise ValueError(f"Relay URL ({hostname}) must use wss:// (or ws:// for .onion).")

        try:
            port = parsed_relay.port
        except ValueError as exc:
            raise ValueError(f"Invalid port in relay URL ({hostname}).") from exc

        if is_onion:
            has_onion_relay = True
        host_literal = f"[{hostname}]" if ":" in hostname else hostname
        path = parsed_relay.path if parsed_relay.path and parsed_relay.path != "/" else ""
        query_suffix = f"?{parsed_relay.query}" if parsed_relay.query else ""
        if query_suffix and not path:
            path = "/"
        port_suffix = f":{port}" if port else ""
        normalized_relay = f"{scheme}://{host_literal}{port_suffix}{path}{query_suffix}"
        if normalized_relay not in relays:
            relays.append(normalized_relay)
        if host_literal not in relay_hosts:
            relay_hosts.append(host_literal)

    relays = relays[:NWC_MAX_RELAYS]
    relay_hosts = relay_hosts[:NWC_MAX_RELAYS]

    secrets_list = [v.strip().lower() for k, v in params if k == "secret" and v.strip()]
    if not secrets_list:
        raise ValueError("NWC Connection URI is missing the secret= parameter.")
    secret = secrets_list[0]
    if not _HEX64_RE.fullmatch(secret):
        raise ValueError("NWC Connection URI secret must be a 64-character hex string.")
    secret_scalar = int(secret, 16)
    if not (1 <= secret_scalar < _SECP_N):
        raise ValueError("NWC Connection URI secret is out of secp256k1 scalar range.")

    lud16_list = [v.strip() for k, v in params if k == "lud16" and v.strip()]
    lud16 = lud16_list[0] if lud16_list else ""

    relay_query = "&".join(f"relay={quote(r, safe='')}" for r in relays)
    normalized_uri = f"nostr+walletconnect://{wallet_pubkey}?{relay_query}&secret={secret}"
    if lud16:
        normalized_uri += f"&lud16={quote(lud16, safe='')}"

    return {
        "uri": normalized_uri,
        "wallet_pubkey": wallet_pubkey,
        "walletPubkey": wallet_pubkey,
        "relays": relays,
        "relay_host": relay_hosts[0],
        "relayHost": relay_hosts[0],
        "secret": secret,
        "lud16": lud16,
        "has_onion_relay": has_onion_relay,
        "hasOnionRelay": has_onion_relay,
    }


def save_nwc_wallet(
    data_dir: str,
    uri: str,
    auto_renew_duration: Union[str, int] = "match",
    route_via_tor: bool = False,
) -> Dict[str, Any]:
    """Validate and persist NWC wallet credentials to /data/nwc-wallet.json with mode 0600."""
    parsed = parse_nwc_uri(uri, route_via_tor=True if route_via_tor else None)
    effective_tor = bool(route_via_tor or parsed["has_onion_relay"])

    norm_duration: Union[str, int]
    if str(auto_renew_duration).strip().lower() in ("match", ""):
        norm_duration = "match"
    elif str(auto_renew_duration).strip().lower().endswith("m"):
        months_str = str(auto_renew_duration).strip().lower()[:-1]
        if months_str.isdigit() and int(months_str) in (1, 3, 6, 12):
            norm_duration = int(months_str)
        else:
            raise ValueError("Invalid autoRenewDuration. Allowed values: 'match', 1, 3, 6, 12.")
    else:
        try:
            months_int = int(auto_renew_duration)
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid autoRenewDuration. Allowed values: 'match', 1, 3, 6, 12.") from exc
        if months_int not in (1, 3, 6, 12):
            raise ValueError("Invalid autoRenewDuration. Allowed values: 'match', 1, 3, 6, 12.")
        norm_duration = months_int

    os.makedirs(data_dir, exist_ok=True)
    wallet_path = os.path.join(data_dir, NWC_WALLET_FILE)
    tmp_path = os.path.join(data_dir, f".{NWC_WALLET_FILE}.tmp.{uuid.uuid4().hex}")

    wallet_record = {
        "uri": parsed["uri"],
        "walletPubkey": parsed["wallet_pubkey"],
        "relays": parsed["relays"],
        "relayHost": parsed["relay_host"],
        "secret": parsed["secret"],
        "lud16": parsed["lud16"],
        "autoRenewDuration": norm_duration,
        "routeViaTor": effective_tor,
        "connectedAt": datetime.now(timezone.utc).isoformat(),
    }

    with open(tmp_path, "w", encoding="utf-8") as fp:
        json.dump(wallet_record, fp, indent=2)
    os.chmod(tmp_path, 0o600)
    try:
        os.chown(tmp_path, 1000, 1000)
    except OSError:
        pass
    os.replace(tmp_path, wallet_path)
    os.chmod(wallet_path, 0o600)
    return wallet_record


def load_nwc_wallet(data_dir: str) -> Optional[Dict[str, Any]]:
    """Read /data/nwc-wallet.json if present and valid."""
    wallet_path = os.path.join(data_dir, NWC_WALLET_FILE)
    if not os.path.exists(wallet_path):
        return None
    try:
        with open(wallet_path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        if not isinstance(data, dict):
            return None
        if data.get("uri"):
            parsed = parse_nwc_uri(data["uri"])
            data.setdefault("walletPubkey", parsed["wallet_pubkey"])
            data.setdefault("relays", parsed["relays"])
            data.setdefault("relayHost", parsed["relay_host"])
            data.setdefault("secret", parsed["secret"])
        elif not (data.get("walletPubkey") and data.get("secret") and data.get("relays")):
            return None
        data["relays"] = list(data.get("relays") or [])[:NWC_MAX_RELAYS]
        return data
    except Exception:
        return None


def delete_nwc_wallet(data_dir: str) -> bool:
    """Remove /data/nwc-wallet.json from disk."""
    wallet_path = os.path.join(data_dir, NWC_WALLET_FILE)
    try:
        if os.path.exists(wallet_path):
            os.remove(wallet_path)
        return True
    except OSError:
        return False


def _build_nip47_request_event(
    secret_hex: str,
    wallet_pubkey_hex: str,
    method: str,
    params: Dict[str, Any],
    created_at: Optional[int] = None,
) -> Tuple[Dict[str, Any], bytes]:
    client_pubkey = _nostr_pubkey_from_secret(secret_hex)
    shared_key = _nip04_shared_secret(secret_hex, wallet_pubkey_hex)
    payload_str = json.dumps({"method": method, "params": params}, separators=(",", ":"))
    content = _nip04_encrypt(shared_key, payload_str)
    ts = int(created_at if created_at is not None else time.time())
    tags = [["p", wallet_pubkey_hex]]
    commitment = json.dumps(
        [0, client_pubkey, ts, 23194, tags, content],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    event_id_bytes = hashlib.sha256(commitment.encode("utf-8")).digest()
    sig = _schnorr_sign(secret_hex, event_id_bytes)
    return (
        {
            "id": event_id_bytes.hex(),
            "pubkey": client_pubkey,
            "created_at": ts,
            "kind": 23194,
            "tags": tags,
            "content": content,
            "sig": sig,
        },
        shared_key,
    )


def _parse_nip47_response_event(
    resp_event: Dict[str, Any],
    request_event_id: str,
    wallet_pubkey_hex: str,
    shared_key: bytes,
) -> Dict[str, Any]:
    if not isinstance(resp_event, dict):
        raise ValueError("Invalid Nostr event object")
    if resp_event.get("kind") != 23195:
        raise ValueError("Unexpected Nostr event kind")
    if str(resp_event.get("pubkey") or "").lower() != wallet_pubkey_hex.lower():
        raise ValueError("Response event pubkey does not match wallet pubkey")
    tags = resp_event.get("tags")
    if not isinstance(tags, list):
        raise ValueError("Response event has invalid tags")
    e_tags = [t[1] for t in tags if isinstance(t, list) and len(t) >= 2 and t[0] == "e"]
    if request_event_id not in e_tags:
        raise ValueError("Response event does not reference our request event ID")
    commitment = json.dumps(
        [
            0,
            resp_event.get("pubkey"),
            resp_event.get("created_at"),
            resp_event.get("kind"),
            tags,
            resp_event.get("content"),
        ],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    expected_id_bytes = hashlib.sha256(commitment.encode("utf-8")).digest()
    if resp_event.get("id") != expected_id_bytes.hex():
        raise ValueError("Response event ID does not match commitment hash")
    if not _schnorr_verify(wallet_pubkey_hex, expected_id_bytes, str(resp_event.get("sig") or "")):
        raise ValueError("Response event Schnorr signature verification failed")
    decrypted = _nip04_decrypt(shared_key, str(resp_event.get("content") or ""))
    body = json.loads(decrypted)
    if not isinstance(body, dict):
        raise ValueError("Decrypted NIP-47 response is not a JSON object")
    return body


def _recv_exact(sock: socket.socket, length: int, deadline: Optional[float] = None) -> bytes:
    buf = bytearray()
    while len(buf) < length:
        if deadline is not None:
            rem = deadline - time.monotonic()
            if rem <= 0:
                raise TimeoutError("Timed out reading from NWC relay")
            sock.settimeout(min(rem, 15.0))
        chunk = sock.recv(length - len(buf))
        if not chunk:
            raise ConnectionError("Socket closed prematurely")
        buf.extend(chunk)
    return bytes(buf)


def _connect_socks5h(target_host: str, target_port: int, timeout: float = 15.0) -> socket.socket:
    host_bytes = target_host.encode("idna")
    if not (1 <= len(host_bytes) <= 255):
        raise NwcError("Invalid relay hostname for SOCKS5h proxy")
    deadline = time.monotonic() + timeout
    sock: Optional[socket.socket] = None
    try:
        sock = socket.create_connection((TOR_SOCKS_HOST, int(TOR_SOCKS_PORT)), timeout=timeout)
        sock.settimeout(timeout)
        sock.sendall(b"\x05\x01\x00")
        ver, method = _recv_exact(sock, 2, deadline=deadline)
        if ver != 5 or method != 0:
            sock.close()
            raise NwcError("Tor SOCKS5 proxy rejected authentication method")
        req = (
            b"\x05\x01\x00\x03"
            + bytes([len(host_bytes)])
            + host_bytes
            + int(target_port).to_bytes(2, "big")
        )
        sock.sendall(req)
        hdr = _recv_exact(sock, 4, deadline=deadline)
        if hdr[0] != 5 or hdr[1] != 0:
            sock.close()
            raise NwcError(f"Tor SOCKS5 proxy failed to connect to {target_host}:{target_port}")
        atyp = hdr[3]
        if atyp == 1:
            _recv_exact(sock, 4 + 2, deadline=deadline)
        elif atyp == 3:
            alen = _recv_exact(sock, 1, deadline=deadline)[0]
            _recv_exact(sock, alen + 2, deadline=deadline)
        elif atyp == 4:
            _recv_exact(sock, 16 + 2, deadline=deadline)
        else:
            sock.close()
            raise NwcError("Tor SOCKS5 proxy returned unsupported address type")
        return sock
    except NwcError:
        raise
    except Exception as exc:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        raise NwcError(f"Tor SOCKS5 proxy error connecting to {target_host}: {exc}") from exc


def _ws_send_frame(sock: socket.socket, opcode: int, payload: bytes) -> None:
    mask_key = os.urandom(4)
    header = bytearray([0x80 | (opcode & 0x0F)])
    n = len(payload)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header.extend(n.to_bytes(2, "big"))
    else:
        header.append(0x80 | 127)
        header.extend(n.to_bytes(8, "big"))
    header.extend(mask_key)
    masked = bytes(b ^ mask_key[i & 3] for i, b in enumerate(payload))
    sock.sendall(bytes(header) + masked)


def _ws_read_single_frame(
    sock: socket.socket, deadline: float, max_bytes: int = 262_144
) -> Tuple[int, int, bytes]:
    b0, b1 = _recv_exact(sock, 2, deadline=deadline)
    fin = (b0 >> 7) & 1
    opcode = b0 & 0x0F
    masked = (b1 >> 7) & 1
    length = b1 & 0x7F
    if length == 126:
        length = int.from_bytes(_recv_exact(sock, 2, deadline=deadline), "big")
    elif length == 127:
        length = int.from_bytes(_recv_exact(sock, 8, deadline=deadline), "big")
    if length > max_bytes:
        raise NwcError("WebSocket frame exceeds size limit")
    mask_key = _recv_exact(sock, 4, deadline=deadline) if masked else b""
    payload = _recv_exact(sock, length, deadline=deadline)
    if masked:
        payload = bytes(b ^ mask_key[i & 3] for i, b in enumerate(payload))
    return fin, opcode, payload


def _ws_recv_frame(
    sock: socket.socket, deadline: float, max_bytes: int = 262_144
) -> Tuple[int, bytes]:
    fin, opcode, payload = _ws_read_single_frame(sock, deadline, max_bytes=max_bytes)
    if fin or opcode >= 0x8:
        return opcode, payload
    assembled = bytearray(payload)
    while True:
        c_fin, c_opcode, c_payload = _ws_read_single_frame(sock, deadline, max_bytes=max_bytes)
        if c_opcode == 0x8:
            return 0x8, c_payload
        if c_opcode == 0x9:
            _ws_send_frame(sock, 0xA, c_payload)
            continue
        if c_opcode == 0xA:
            continue
        if c_opcode != 0x0:
            raise NwcError("Expected WebSocket continuation frame")
        if len(assembled) + len(c_payload) > max_bytes:
            raise NwcError("Reassembled WebSocket message exceeds size limit")
        assembled.extend(c_payload)
        if c_fin:
            return opcode, bytes(assembled)


def _ws_open(relay_url: str, route_via_tor: bool = False, timeout: float = 15.0) -> socket.socket:
    parsed = urlparse(relay_url)
    scheme = (parsed.scheme or "").lower()
    hostname = (parsed.hostname or "").lower()
    is_onion = hostname.endswith(".onion")
    if scheme == "ws" and not is_onion:
        raise NwcError("Refusing plaintext ws:// connection to non-onion relay", permanent=True)
    port = parsed.port or (443 if scheme == "wss" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    deadline = time.monotonic() + timeout
    use_tor = bool(route_via_tor or is_onion)
    if use_tor:
        raw_sock = _connect_socks5h(hostname, port, timeout=timeout)
    else:
        try:
            raw_sock = socket.create_connection((hostname, port), timeout=timeout)
            raw_sock.settimeout(timeout)
        except Exception as exc:
            raise NwcError(f"Could not connect to NWC relay {hostname}: {exc}") from exc

    sock: socket.socket = raw_sock
    try:
        if scheme == "wss":
            ctx = ssl.create_default_context()
            if hasattr(ssl, "TLSVersion"):
                ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            sock = ctx.wrap_socket(raw_sock, server_hostname=hostname)
            sock.settimeout(timeout)

        ws_key = base64.b64encode(os.urandom(16)).decode("ascii")
        default_port = (scheme == "wss" and port == 443) or (scheme == "ws" and port == 80)
        host_literal = f"[{hostname}]" if ":" in hostname else hostname
        host_hdr = host_literal if default_port else f"{host_literal}:{port}"
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host_hdr}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {ws_key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "User-Agent: TunnelSats/3.5.0\r\n\r\n"
        )
        sock.sendall(handshake.encode("ascii"))

        resp_buf = bytearray()
        while b"\r\n\r\n" not in resp_buf:
            rem = deadline - time.monotonic()
            if rem <= 0:
                raise NwcError(f"Timed out waiting for WebSocket handshake from relay {hostname}")
            sock.settimeout(min(rem, 15.0))
            chunk = sock.recv(1)
            if not chunk:
                raise NwcError(f"Relay {hostname} closed connection during WebSocket handshake")
            resp_buf.extend(chunk)
            if len(resp_buf) > 16384:
                raise NwcError(f"Relay {hostname} sent oversized WebSocket handshake")

        header_text = resp_buf.decode("latin1", errors="replace")
        lines = header_text.split("\r\n")
        status_line = lines[0] if lines else ""
        if not status_line.startswith("HTTP/1.1 101"):
            raise NwcError(f"Relay {hostname} rejected WebSocket upgrade: {status_line[:80]}")
        expected_accept = base64.b64encode(
            hashlib.sha1((ws_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        headers: Dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                k, _, v = line.partition(":")
                headers[k.strip().lower()] = v.strip()
        if headers.get("sec-websocket-accept") != expected_accept:
            raise NwcError(f"Relay {hostname} returned invalid Sec-WebSocket-Accept")
        return sock
    except Exception:
        try:
            sock.close()
        except Exception:
            pass
        raise


def nwc_rpc_call(
    wallet: Dict[str, Any],
    method: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = 30.0,
    deadline: Optional[float] = None,
) -> Dict[str, Any]:
    """Execute a NIP-47 RPC command over the configured NWC relay(s) within a monotonic deadline."""
    wallet_pubkey = str(wallet.get("walletPubkey") or wallet.get("wallet_pubkey") or "")
    secret = str(wallet.get("secret") or "")
    relays = list(wallet.get("relays") or [])[:NWC_MAX_RELAYS]
    route_via_tor = bool(wallet.get("routeViaTor") or wallet.get("route_via_tor"))
    if not wallet_pubkey or not secret or not relays:
        raise NwcError("NWC wallet configuration is incomplete", permanent=True)

    req_event, shared_key = _build_nip47_request_event(
        secret, wallet_pubkey, method, params or {}
    )
    req_id = req_event["id"]
    client_pubkey = req_event["pubkey"]
    sub_id = f"ts-{req_id[:12]}"
    last_error: Optional[Exception] = None

    for relay_url in relays:
        if deadline is not None:
            rem_total = deadline - time.monotonic()
            if rem_total <= 0:
                raise NwcError(
                    f"NWC {method} timed out before trying relay {relay_url}",
                    code="TIMEOUT",
                )
            relay_timeout = min(float(timeout), rem_total)
        else:
            relay_timeout = float(timeout)

        relay_deadline = time.monotonic() + relay_timeout
        sock: Optional[socket.socket] = None
        try:
            sock = _ws_open(
                relay_url,
                route_via_tor=route_via_tor,
                timeout=min(15.0, relay_timeout),
            )
            req_filter = {
                "kinds": [23195],
                "authors": [wallet_pubkey],
                "#e": [req_id],
                "#p": [client_pubkey],
            }
            _ws_send_frame(
                sock,
                0x1,
                json.dumps(["REQ", sub_id, req_filter], separators=(",", ":")).encode("utf-8"),
            )
            _ws_send_frame(
                sock,
                0x1,
                json.dumps(["EVENT", req_event], separators=(",", ":")).encode("utf-8"),
            )

            while time.monotonic() < relay_deadline:
                opcode, raw_frame = _ws_recv_frame(sock, relay_deadline)
                if opcode == 0x8:
                    raise NwcError("NWC relay closed WebSocket connection")
                if opcode == 0x9:
                    _ws_send_frame(sock, 0xA, raw_frame)
                    continue
                if opcode != 0x1:
                    continue
                msg = json.loads(raw_frame.decode("utf-8"))
                if not isinstance(msg, list) or len(msg) < 2:
                    continue
                mtype = msg[0]
                if mtype == "OK" and len(msg) >= 3 and msg[1] == req_id and msg[2] is False:
                    reason = str(msg[3]) if len(msg) >= 4 else "Relay rejected request event"
                    raise NwcError(f"NWC relay rejected request: {reason}")
                if mtype == "EVENT" and len(msg) >= 3 and msg[1] == sub_id:
                    body = _parse_nip47_response_event(msg[2], req_id, wallet_pubkey, shared_key)
                    try:
                        _ws_send_frame(sock, 0x1, json.dumps(["CLOSE", sub_id]).encode("utf-8"))
                        _ws_send_frame(sock, 0x8, b"")
                    except Exception:
                        pass
                    err = body.get("error")
                    if isinstance(err, dict) and (err.get("code") or err.get("message")):
                        code = str(err.get("code") or "OTHER")
                        message = str(err.get("message") or code)
                        perm = code in (
                            "UNAUTHORIZED",
                            "RESTRICTED",
                            "NOT_IMPLEMENTED",
                            "QUOTA_EXCEEDED",
                        )
                        raise NwcError(f"NWC wallet error ({code}): {message}", code=code, permanent=perm)
                    res_obj = body.get("result")
                    return res_obj if isinstance(res_obj, dict) else {}
            raise NwcError(f"Timed out waiting for NIP-47 {method} response from relay", code="TIMEOUT")
        except NwcError as exc:
            last_error = exc
            if exc.permanent:
                raise
        except Exception as exc:
            last_error = NwcError(f"NWC relay error: {exc}")
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass

    if isinstance(last_error, NwcError):
        raise last_error
    raise NwcError(str(last_error or "All NWC relays failed"))


def _invoice_already_settled(result: Dict[str, Any]) -> bool:
    if not isinstance(result, dict):
        return False
    state = str(result.get("state") or result.get("status") or "").lower()
    if state in ("settled", "paid", "complete", "completed"):
        return True
    if result.get("settled_at") or result.get("preimage"):
        return True
    return False


def pay_invoice_via_nwc(
    data_dir: str,
    invoice: str,
    expected_payment_hash: Optional[str] = None,
    expected_amount_sats: Optional[int] = None,
    deadline_seconds: float = 60.0,
) -> Dict[str, Any]:
    """Verify a BOLT11 invoice, check lookup_invoice to prevent double payment, and pay via NWC."""
    wallet = load_nwc_wallet(data_dir)
    if not wallet:
        return {
            "success": False,
            "error": "No NWC wallet is connected.",
            "code": "NOT_CONNECTED",
        }

    verification = verify_bolt11_invoice(
        invoice,
        expected_payment_hash=expected_payment_hash,
        expected_amount_sats=expected_amount_sats,
        max_amount_sats=NWC_MAX_AUTO_RENEW_SATS,
        check_expiry=True,
    )
    if not verification.get("valid"):
        return {
            "success": False,
            "error": str(verification.get("error") or "BOLT11 verification failed"),
            "code": "INVALID_INVOICE",
        }

    deadline = time.monotonic() + max(5.0, float(deadline_seconds))
    payment_hash = verification["payment_hash"]

    # Guard against double-payment via lookup_invoice when supported by the wallet
    try:
        lookup_res = nwc_rpc_call(
            wallet,
            "lookup_invoice",
            {"payment_hash": payment_hash, "invoice": invoice.strip()},
            timeout=min(15.0, max(5.0, deadline - time.monotonic())),
            deadline=deadline,
        )
        if _invoice_already_settled(lookup_res):
            return {
                "success": True,
                "already_paid": True,
                "preimage": str(lookup_res.get("preimage") or ""),
                "payment_hash": payment_hash,
                "amount_sats": verification["amount_sats"],
            }
    except NwcError:
        # Many NWC wallets return NOT_FOUND or NOT_IMPLEMENTED for unpaid outgoing invoices
        pass

    try:
        pay_res = nwc_rpc_call(
            wallet,
            "pay_invoice",
            {"invoice": invoice.strip()},
            timeout=max(5.0, deadline - time.monotonic()),
            deadline=deadline,
        )
        return {
            "success": True,
            "already_paid": False,
            "preimage": str(pay_res.get("preimage") or ""),
            "payment_hash": payment_hash,
            "amount_sats": verification["amount_sats"],
        }
    except NwcError as exc:
        return {
            "success": False,
            "error": str(exc),
            "code": exc.code,
            "permanent": exc.permanent,
        }


def _parse_iso_dt(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def maybe_nwc_auto_renew(
    data_dir: str,
    meta: Dict[str, Any],
    create_renewal_fn: Callable[[str, str, int], Optional[Dict[str, Any]]],
    now_dt: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Evaluate and execute NWC auto-renewal within a 180s monotonic deadline.
    Respects 7-day window, 1h -> 6h -> 12h backoff, and max 3 attempts before fallbackTaskRaised.
    """
    now = now_dt.astimezone(timezone.utc) if now_dt else datetime.now(timezone.utc)
    if not isinstance(meta, dict) or not meta.get("nwcConnected"):
        return {"attempted": False, "reason": "nwc_not_connected"}

    wallet = load_nwc_wallet(data_dir)
    state = dict(meta.get("nwcAutoRenewState") or {}) if isinstance(meta.get("nwcAutoRenewState"), dict) else {}
    if not wallet:
        state["restoreReconnectNeeded"] = True
        meta["nwcAutoRenewState"] = state
        return {"attempted": False, "reason": "wallet_file_missing", "state_changed": True}

    expiry_dt = _parse_iso_dt(meta.get("expiresAt"))
    if expiry_dt is None:
        return {"attempted": False, "reason": "no_expiry"}

    days_remaining = (expiry_dt - now).total_seconds() / 86400.0
    if days_remaining > NWC_RENEW_WINDOW_DAYS:
        return {"attempted": False, "reason": "outside_renew_window", "days_remaining": days_remaining}

    # Check if we already exhausted attempts for this expiry cycle
    target_expiry_str = str(meta.get("expiresAt") or "")
    if state.get("targetExpiry") != target_expiry_str:
        state = {
            "targetExpiry": target_expiry_str,
            "attempts": 0,
            "lastAttemptAt": None,
            "nextRetryAt": None,
            "lastError": None,
            "budgetWarning": False,
            "fallbackTaskRaised": False,
            "restoreReconnectNeeded": False,
        }

    attempts = int(state.get("attempts") or 0)
    if attempts >= NWC_MAX_ATTEMPTS or state.get("fallbackTaskRaised"):
        state["fallbackTaskRaised"] = True
        meta["nwcAutoRenewState"] = state
        return {"attempted": False, "reason": "max_attempts_reached", "state_changed": True}

    next_retry_dt = _parse_iso_dt(state.get("nextRetryAt"))
    if next_retry_dt is not None and now < next_retry_dt:
        return {"attempted": False, "reason": "backoff_active", "next_retry_at": state.get("nextRetryAt")}

    wg_pubkey = str(meta.get("wgPublicKey") or "").strip()
    server_id = str(meta.get("serverId") or "").strip()
    if not wg_pubkey or not server_id:
        return {"attempted": False, "reason": "missing_subscription_identity"}

    # Determine duration (months)
    pref_dur = wallet.get("autoRenewDuration", meta.get("nwcAutoRenewDuration", "match"))
    if str(pref_dur).strip().lower() in ("match", ""):
        last_dur = meta.get("lastDuration", 1)
        try:
            if isinstance(last_dur, str) and last_dur.lower().endswith("m"):
                duration_months = int(last_dur[:-1])
            else:
                duration_months = int(last_dur)
        except (TypeError, ValueError):
            duration_months = 1
    else:
        try:
            duration_months = int(str(pref_dur).rstrip("mM"))
        except (TypeError, ValueError):
            duration_months = 1
    if duration_months not in (1, 3, 6, 12):
        duration_months = 1

    deadline = time.monotonic() + NWC_AUTO_RENEW_DEADLINE_SECONDS

    def _record_failure(error_msg: str, budget_warn: bool = False) -> Dict[str, Any]:
        new_attempts = attempts + 1
        backoff_idx = min(new_attempts - 1, len(NWC_BACKOFF_HOURS) - 1)
        backoff_hours = NWC_BACKOFF_HOURS[backoff_idx]
        state["attempts"] = new_attempts
        state["lastAttemptAt"] = now.isoformat()
        state["lastError"] = error_msg
        state["budgetWarning"] = bool(budget_warn)
        if new_attempts >= NWC_MAX_ATTEMPTS:
            state["fallbackTaskRaised"] = True
            state["nextRetryAt"] = None
        else:
            state["nextRetryAt"] = (now + timedelta(hours=backoff_hours)).isoformat()
        meta["nwcAutoRenewState"] = state
        return {
            "attempted": True,
            "success": False,
            "error": error_msg,
            "attempts": new_attempts,
            "fallback_task_raised": bool(state.get("fallbackTaskRaised")),
            "state_changed": True,
        }

    # Reuse unexpired pendingRenewal or create a new one
    pending = meta.get("pendingRenewal") if isinstance(meta.get("pendingRenewal"), dict) else None
    invoice = ""
    payment_hash = ""
    expected_amount: Optional[int] = None

    if pending and pending.get("invoice") and pending.get("wgPublicKey") == wg_pubkey:
        chk = verify_bolt11_invoice(
            str(pending["invoice"]),
            expected_payment_hash=pending.get("paymentHash"),
            check_expiry=True,
        )
        if chk.get("valid"):
            invoice = str(pending["invoice"])
            payment_hash = str(pending.get("paymentHash") or chk["payment_hash"])
            expected_amount = chk["amount_sats"]

    if not invoice:
        try:
            created = create_renewal_fn(wg_pubkey, server_id, duration_months)
        except Exception as exc:
            return _record_failure(f"Failed to request renewal invoice: {exc}")
        if not isinstance(created, dict) or not created.get("invoice"):
            return _record_failure("Renewal API did not return a valid BOLT11 invoice")
        invoice = str(created["invoice"]).strip()
        payment_hash = str(created.get("paymentHash") or "").strip()
        raw_amt = created.get("amountSats") or created.get("amount")
        if raw_amt is not None:
            try:
                expected_amount = int(raw_amt)
            except (TypeError, ValueError):
                expected_amount = None
        meta["pendingRenewal"] = {
            "paymentHash": payment_hash,
            "invoice": invoice,
            "amountSats": expected_amount,
            "serverId": server_id,
            "wgPublicKey": wg_pubkey,
            "duration": duration_months,
            "createdAt": now.isoformat(),
        }

    verification = verify_bolt11_invoice(
        invoice,
        expected_payment_hash=payment_hash or None,
        expected_amount_sats=expected_amount,
        max_amount_sats=NWC_MAX_AUTO_RENEW_SATS,
        check_expiry=True,
    )
    if not verification.get("valid"):
        meta.pop("pendingRenewal", None)
        return _record_failure(str(verification.get("error") or "Invalid renewal BOLT11 invoice"))

    invoice_sats = int(verification["amount_sats"])
    payment_hash = str(verification["payment_hash"])

    # Pre-check wallet budget/balance if supported
    for check_method in ("get_budget", "get_balance"):
        rem = deadline - time.monotonic()
        if rem <= 5.0:
            break
        try:
            res_b = nwc_rpc_call(wallet, check_method, {}, timeout=min(10.0, rem), deadline=deadline)
            if isinstance(res_b, dict):
                msat_key = "remaining_budget" if check_method == "get_budget" else "balance"
                if msat_key in res_b and isinstance(res_b[msat_key], (int, float)):
                    avail_sats = int(res_b[msat_key]) // 1000
                    if 0 <= avail_sats < invoice_sats:
                        return _record_failure(
                            f"Insufficient NWC wallet {check_method.replace('get_', '')}: "
                            f"{avail_sats} sats available, {invoice_sats} sats required",
                            budget_warn=True,
                        )
        except NwcError:
            pass

    rem = deadline - time.monotonic()
    if rem <= 0:
        return _record_failure("NWC auto-renewal exceeded 180s deadline before payment")

    pay_res = pay_invoice_via_nwc(
        data_dir,
        invoice,
        expected_payment_hash=payment_hash,
        expected_amount_sats=invoice_sats,
        deadline_seconds=rem,
    )
    if not pay_res.get("success"):
        err_code = str(pay_res.get("code") or "")
        is_budget = err_code in ("QUOTA_EXCEEDED", "INSUFFICIENT_BALANCE")
        return _record_failure(str(pay_res.get("error") or "NWC payment failed"), budget_warn=is_budget)

    # Payment succeeded
    state["attempts"] = 0
    state["lastAttemptAt"] = now.isoformat()
    state["lastPaidAt"] = now.isoformat()
    state["lastPaymentHash"] = payment_hash
    state["lastError"] = None
    state["nextRetryAt"] = None
    state["budgetWarning"] = False
    state["fallbackTaskRaised"] = False
    state["restoreReconnectNeeded"] = False
    meta["nwcAutoRenewState"] = state
    meta["lastDuration"] = duration_months
    meta["lastAmountSats"] = invoice_sats
    meta.pop("pendingRenewal", None)
    return {
        "attempted": True,
        "success": True,
        "payment_hash": payment_hash,
        "amount_sats": invoice_sats,
        "duration": duration_months,
        "preimage": pay_res.get("preimage", ""),
        "state_changed": True,
    }


# ---------------------------------------------------------------------------
# 5. Bandwidth Telemetry & Subscription Notices Engine
# ---------------------------------------------------------------------------

def compute_bandwidth_telemetry(
    status_info: Dict[str, Any],
    now_dt: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Compute bandwidth usage, UTC month boundary rollover, resets remaining,
    and burn-rate projection (when >= 24h into the UTC month and resets_this_month == 0).
    """
    now = now_dt.astimezone(timezone.utc) if now_dt else datetime.now(timezone.utc)
    info = status_info if isinstance(status_info, dict) else {}

    def _float_val(keys: Tuple[str, ...], default: float) -> float:
        for k in keys:
            v = info.get(k)
            if v is not None and not isinstance(v, bool):
                try:
                    f = float(v)
                    if math.isfinite(f) and f >= 0.0:
                        return f
                except (TypeError, ValueError):
                    pass
        return default

    def _int_val(keys: Tuple[str, ...], default: int) -> int:
        for k in keys:
            v = info.get(k)
            if v is not None and not isinstance(v, bool):
                try:
                    i = int(v)
                    if i >= 0:
                        return i
                except (TypeError, ValueError):
                    pass
        return default

    used_gb = _float_val(("bandwidth_used_gb", "bandwidthUsedGb", "used_gb"), 0.0)
    limit_gb = _float_val(("bandwidth_limit_gb", "bandwidthLimitGb", "limit_gb"), 100.0)
    if limit_gb <= 0.0:
        limit_gb = 100.0

    resets_this_month = _int_val(("bandwidth_resets_this_month", "resetsThisMonth", "resets_this_month"), 0)
    max_resets = _int_val(("max_resets_per_month", "maxResetsPerMonth", "max_resets"), 2)
    if max_resets <= 0:
        max_resets = 2

    last_synced_raw = (
        info.get("last_synced_at")
        or info.get("lastSyncedAt")
        or info.get("lastSync")
        or ""
    )
    last_synced_dt = _parse_iso_dt(last_synced_raw)

    # Validate UTC month boundary: if cached telemetry is from a prior UTC month, reset monthly counters
    month_rolled_over = False
    if last_synced_dt is not None:
        if (last_synced_dt.year, last_synced_dt.month) != (now.year, now.month):
            used_gb = 0.0
            resets_this_month = 0
            month_rolled_over = True

    used_pct = round((used_gb / limit_gb) * 100.0, 1) if limit_gb > 0 else 0.0
    resets_remaining = max(0, max_resets - resets_this_month)

    # Next free reset is the 1st of the next UTC month at 00:00 UTC
    if now.month == 12:
        next_month_dt = datetime(now.year + 1, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    else:
        next_month_dt = datetime(now.year, now.month + 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    next_free_reset_utc = next_month_dt.strftime("%b 1 UTC")
    next_free_reset_iso = next_month_dt.isoformat()

    month_start_dt = datetime(now.year, now.month, 1, 0, 0, 0, tzinfo=timezone.utc)
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    total_month_seconds = float(days_in_month * 86400)
    elapsed_seconds = max(0.0, (now - month_start_dt).total_seconds())

    pace_pct: Optional[float] = None
    projected_gb: Optional[float] = None
    over_pace = False

    if elapsed_seconds >= 86400.0 and resets_this_month == 0 and total_month_seconds > 0:
        fraction = min(1.0, elapsed_seconds / total_month_seconds)
        pace_pct = round(fraction * 100.0, 1)
        projected_gb = round(used_gb / fraction, 1) if fraction > 0 else 0.0
        over_pace = bool(projected_gb > limit_gb)

    sub_status = str(
        info.get("subscription_status")
        or info.get("subscriptionStatus")
        or info.get("status")
        or "unknown"
    )

    days_until_reset = max(0, int(math.ceil((next_month_dt - now).total_seconds() / 86400.0)))

    return {
        "bandwidth_used_gb": round(used_gb, 2),
        "bandwidth_limit_gb": round(limit_gb, 2),
        "used_gb": round(used_gb, 2),
        "limit_gb": round(limit_gb, 2),
        "quota_gb": round(limit_gb, 2),
        "used_pct": used_pct,
        "bandwidth_resets_this_month": resets_this_month,
        "max_resets_per_month": max_resets,
        "resets_remaining_this_month": resets_remaining,
        "next_free_reset_utc": next_free_reset_utc,
        "next_free_reset_iso": next_free_reset_iso,
        "next_reset_at": next_free_reset_iso,
        "days_until_reset": days_until_reset,
        "pace_pct": pace_pct,
        "projected_gb": projected_gb,
        "over_pace": over_pace,
        "month_rolled_over": month_rolled_over,
        "subscription_status": sub_status,
        "last_synced_at": str(last_synced_raw) if last_synced_raw else None,
    }


def evaluate_subscription_notices(
    data_dir: str,
    meta: Dict[str, Any],
    now_dt: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Evaluate subscription expiry timeline, bandwidth thresholds, and NWC health notices.
    Suppresses manual 7d/3d expiry banners when NWC is connected and healthy.
    Persists notice history to /data/subscription-notices.json with catch-up deduplication.
    """
    now = now_dt.astimezone(timezone.utc) if now_dt else datetime.now(timezone.utc)
    meta_dict = meta if isinstance(meta, dict) else {}

    nwc_connected = bool(meta_dict.get("nwcConnected"))
    nwc_state = (
        meta_dict.get("nwcAutoRenewState")
        if isinstance(meta_dict.get("nwcAutoRenewState"), dict)
        else {}
    )
    budget_warning = bool(nwc_state.get("budgetWarning"))
    fallback_raised = bool(nwc_state.get("fallbackTaskRaised"))
    restore_needed = bool(nwc_state.get("restoreReconnectNeeded"))
    nwc_healthy = bool(nwc_connected and not budget_warning and not fallback_raised and not restore_needed)

    expiry_raw = meta_dict.get("expiresAt")
    expiry_dt = _parse_iso_dt(expiry_raw)

    phase = "none"
    days_remaining: Optional[float] = None
    seconds_remaining: Optional[int] = None
    suppressed_by_nwc = False
    banners: List[Dict[str, Any]] = []
    most_severe_due: Optional[str] = None

    if expiry_dt is not None:
        sec_rem = int((expiry_dt - now).total_seconds())
        seconds_remaining = max(0, sec_rem)
        days_remaining = round(sec_rem / 86400.0, 2)

        if sec_rem <= 0:
            phase = "expired"
            most_severe_due = "lapsed"
            banners.append(
                {
                    "id": "expiry_expired",
                    "kind": "expiry",
                    "severity": "red",
                    "phase": "expired",
                    "title": "Subscription Expired",
                    "action_type": "renew",
                    "message": "Subscription has expired. Renew to restore VPN port forwarding.",
                }
            )
        elif sec_rem <= 3 * 86400:
            phase = "renew_now"
            most_severe_due = "3d"
            if nwc_healthy:
                suppressed_by_nwc = True
            else:
                banners.append(
                    {
                        "id": "expiry_3d",
                        "kind": "expiry",
                        "severity": "red",
                        "phase": "renew_now",
                        "title": "Subscription Expiring Within 3 Days",
                        "action_type": "renew",
                        "message": "Subscription expires in less than 3 days. Renew now to avoid interruption.",
                    }
                )
        elif sec_rem <= 7 * 86400:
            phase = "renew_soon"
            most_severe_due = "7d"
            if nwc_healthy:
                suppressed_by_nwc = True
            else:
                banners.append(
                    {
                        "id": "expiry_7d",
                        "kind": "expiry",
                        "severity": "amber",
                        "phase": "renew_soon",
                        "title": "Subscription Expiring Soon",
                        "action_type": "renew",
                        "message": "Subscription expires within 7 days.",
                    }
                )
        else:
            phase = "on_track"

    # Bandwidth threshold notices (70% amber, 90% red, 100% exhausted red)
    telemetry_src = (
        meta_dict.get("subscriptionTelemetry")
        if isinstance(meta_dict.get("subscriptionTelemetry"), dict)
        else meta_dict
    )
    telemetry = compute_bandwidth_telemetry(telemetry_src, now_dt=now)
    used_pct = telemetry["used_pct"]

    if used_pct >= 100.0 or telemetry["subscription_status"] == "exhausted":
        banners.append(
            {
                "id": "bandwidth_100",
                "kind": "bandwidth",
                "severity": "red",
                "threshold_pct": 100,
                "title": "Monthly Bandwidth Exhausted (100%)",
                "action_type": "bandwidth_reset",
                "message": "Monthly bandwidth allowance is exhausted (100%). Reset bandwidth to restore full speed.",
            }
        )
    elif used_pct >= 90.0:
        banners.append(
            {
                "id": "bandwidth_90",
                "kind": "bandwidth",
                "severity": "red",
                "threshold_pct": 90,
                "title": "Critical Monthly Bandwidth Usage (>= 90%)",
                "action_type": "bandwidth_reset",
                "message": f"Bandwidth usage is at {used_pct}% of your {telemetry['bandwidth_limit_gb']} GB monthly limit.",
            }
        )
    elif used_pct >= 70.0:
        banners.append(
            {
                "id": "bandwidth_70",
                "kind": "bandwidth",
                "severity": "amber",
                "threshold_pct": 70,
                "title": "High Monthly Bandwidth Usage (>= 70%)",
                "action_type": "bandwidth_reset",
                "message": f"Bandwidth usage has reached {used_pct}% of your {telemetry['bandwidth_limit_gb']} GB monthly limit.",
            }
        )

    # NWC health warning banners
    if restore_needed:
        banners.append(
            {
                "id": "nwc_restore_reconnect",
                "kind": "nwc",
                "severity": "amber",
                "title": "NWC Wallet Reconnect Required",
                "action_type": "nwc",
                "message": "NWC wallet credentials need to be reconnected after backup restore.",
            }
        )
    if budget_warning:
        banners.append(
            {
                "id": "nwc_budget_warning",
                "kind": "nwc",
                "severity": "amber",
                "title": "NWC Auto-Renewal Budget Warning",
                "action_type": "nwc",
                "message": str(
                    nwc_state.get("lastError")
                    or "NWC wallet budget or balance is insufficient for auto-renewal."
                ),
            }
        )
    if fallback_raised:
        banners.append(
            {
                "id": "nwc_fallback_manual",
                "kind": "nwc",
                "severity": "red",
                "title": "NWC Auto-Renewal Failed",
                "action_type": "nwc",
                "message": "NWC auto-renewal failed after 3 attempts. Please renew manually.",
            }
        )

    # Persist notice state with catch-up logic to /data/subscription-notices.json
    if data_dir and os.path.exists(data_dir):
        notices_path = os.path.join(data_dir, SUBSCRIPTION_NOTICES_FILE)
        try:
            existing: Dict[str, Any] = {}
            if os.path.exists(notices_path):
                with open(notices_path, "r", encoding="utf-8") as fp:
                    loaded = json.load(fp)
                    if isinstance(loaded, dict):
                        existing = loaded

            exp_key = str(expiry_raw or "")
            sent_list = list(existing.get("sentThresholds") or []) if existing.get("expiresAt") == exp_key else []
            changed = existing.get("expiresAt") != exp_key or existing.get("phase") != phase

            if most_severe_due and not suppressed_by_nwc:
                # Catch-up rule: mark earlier thresholds as superseded so only the most severe fires
                order = ("7d", "3d", "lapsed")
                idx = order.index(most_severe_due)
                for earlier in order[: idx + 1]:
                    if earlier not in sent_list:
                        sent_list.append(earlier)
                        changed = True

            if changed:
                record = {
                    "expiresAt": exp_key,
                    "phase": phase,
                    "mostSevereThreshold": most_severe_due,
                    "sentThresholds": sent_list,
                    "suppressedByNwc": suppressed_by_nwc,
                    "updatedAt": now.isoformat(),
                }
                tmp_path = os.path.join(data_dir, f".{SUBSCRIPTION_NOTICES_FILE}.tmp.{uuid.uuid4().hex}")
                with open(tmp_path, "w", encoding="utf-8") as fp:
                    json.dump(record, fp, indent=2)
                os.chmod(tmp_path, 0o600)
                os.replace(tmp_path, notices_path)
        except OSError:
            pass

    return {
        "phase": phase,
        "days_remaining": days_remaining,
        "seconds_remaining": seconds_remaining,
        "banners": banners,
        "suppressed_by_nwc": suppressed_by_nwc,
    }
