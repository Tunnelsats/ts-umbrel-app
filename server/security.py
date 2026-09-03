import secrets
import socket
from functools import lru_cache
from ipaddress import ip_address
from urllib.parse import urlsplit


_ROUTE_FLAG_UP = 0x0001
_ROUTE_FLAG_GATEWAY = 0x0002
_ROUTE_FLAG_REJECT = 0x0200


class ManagementSecurity:
    """Management-request security primitives used by the Flask boundary."""

    csrf_header = "X-TunnelSats-CSRF-Token"
    csrf_refresh_header = "X-TunnelSats-CSRF-Refresh"

    def __init__(self, csrf_token=None):
        self.csrf_token = csrf_token or secrets.token_urlsafe(32)

    @staticmethod
    def normalize_ip(value):
        candidate = ip_address(value)
        ipv4_mapped = getattr(candidate, "ipv4_mapped", None)
        return str(ipv4_mapped or candidate)

    @classmethod
    def parse_host_authority(cls, raw_host):
        value = str(raw_host or "").strip()
        if (
            not value
            or any(char.isspace() for char in value)
            or any(char in value for char in "/?#@")
        ):
            return None
        if value.count(":") >= 2 and not value.startswith("["):
            try:
                return cls.normalize_ip(value), None
            except ValueError:
                return None
        try:
            parsed = urlsplit(f"//{value}")
            hostname = (parsed.hostname or "").rstrip(".").lower()
            port = parsed.port
        except ValueError:
            return None
        if not hostname or parsed.username is not None or parsed.password is not None:
            return None
        try:
            hostname = cls.normalize_ip(hostname)
        except ValueError:
            pass
        return hostname, port

    @classmethod
    def canonical_origin(cls, raw_origin):
        value = str(raw_origin or "").strip()
        if not value or value == "null":
            return None
        try:
            parsed = urlsplit(value)
            hostname = (parsed.hostname or "").rstrip(".").lower()
            port = parsed.port
        except ValueError:
            return None
        if (
            parsed.scheme not in ("http", "https")
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            return None
        try:
            hostname = cls.normalize_ip(hostname)
        except ValueError:
            pass
        default_port = 443 if parsed.scheme == "https" else 80
        return parsed.scheme, hostname, port or default_port

    @staticmethod
    def direct_remote_addr(environ):
        environ = environ or {}
        proxyfix_orig = environ.get("werkzeug.proxy_fix.orig")
        if isinstance(proxyfix_orig, dict):
            return proxyfix_orig.get("REMOTE_ADDR") or environ.get("REMOTE_ADDR", "")
        if isinstance(proxyfix_orig, (tuple, list)) and proxyfix_orig:
            return proxyfix_orig[0] or environ.get("REMOTE_ADDR", "")
        return environ.get("REMOTE_ADDR", "")

    @staticmethod
    def is_loopback_ip(value):
        if not value:
            return False
        try:
            candidate = ip_address(value)
            normalized = getattr(candidate, "ipv4_mapped", None) or candidate
            return normalized.is_loopback
        except (AttributeError, ValueError):
            return False

    @staticmethod
    @lru_cache(maxsize=1)
    def default_gateway_ip(route_path="/proc/net/route"):
        """Return the lowest-metric usable IPv4 default gateway from procfs."""
        candidates = []
        try:
            with open(route_path, "r", encoding="utf-8") as route_table:
                for line in route_table:
                    fields = line.split()
                    if len(fields) < 8:
                        continue
                    destination, raw_gateway, raw_flags = fields[1:4]
                    raw_metric, mask = fields[6:8]
                    if destination != "00000000" or mask != "00000000":
                        continue
                    try:
                        flags = int(raw_flags, 16)
                        metric = int(raw_metric, 10)
                        gateway_bytes = bytes.fromhex(raw_gateway)
                        if len(gateway_bytes) != 4 or metric < 0:
                            continue
                        gateway = ip_address(gateway_bytes[::-1])
                    except (TypeError, ValueError):
                        continue
                    required_flags = _ROUTE_FLAG_UP | _ROUTE_FLAG_GATEWAY
                    if (
                        flags & required_flags != required_flags
                        or flags & _ROUTE_FLAG_REJECT
                        or gateway.is_unspecified
                    ):
                        continue
                    candidates.append((metric, str(gateway)))
        except (OSError, UnicodeError):
            return None

        if not candidates:
            return None
        return min(candidates, key=lambda candidate: candidate[0])[1]

    def peer_is_trusted(self, direct_remote_addr, trusted_proxy_host=""):
        """Authenticate an immediate management peer by exact IP identity."""
        try:
            direct_ip = self.normalize_ip(direct_remote_addr)
        except (TypeError, ValueError):
            return False

        if self.is_loopback_ip(direct_ip):
            return True

        # umbrelOS AppGateway authenticates on the host but exposes no
        # backend-verifiable credential. Its kernel-assigned gateway address is
        # therefore the host trust boundary; adjacent containers retain their
        # own distinct source addresses and do not match it.
        gateway_ip = self.default_gateway_ip()
        if gateway_ip:
            try:
                if self.normalize_ip(gateway_ip) == direct_ip:
                    return True
            except (TypeError, ValueError):
                pass

        trusted_host = str(trusted_proxy_host or "").strip()
        if not trusted_host:
            return False
        try:
            resolved = socket.getaddrinfo(
                trusted_host, None, type=socket.SOCK_STREAM
            )
        except (OSError, ValueError):
            return False

        for result in resolved:
            try:
                if self.normalize_ip(result[4][0]) == direct_ip:
                    return True
            except (IndexError, TypeError, ValueError):
                continue
        return False

    @classmethod
    def host_is_allowed(cls, raw_host):
        # Immediate-peer authentication is handled separately. Once it passes,
        # accept every syntactically valid host so custom DNS, Tailscale, Tor,
        # and additional reverse proxies cannot lock users out.
        return cls.parse_host_authority(raw_host) is not None

    @classmethod
    def origin_matches(cls, raw_origin, scheme, raw_host):
        origin = cls.canonical_origin(raw_origin)
        authority = cls.parse_host_authority(raw_host)
        scheme = str(scheme or "").lower()
        if origin is None or authority is None or scheme not in ("http", "https"):
            return False
        hostname, port = authority
        default_port = 443 if scheme == "https" else 80
        return origin == (scheme, hostname, port or default_port)

    def csrf_matches(self, supplied_token):
        return bool(supplied_token) and secrets.compare_digest(
            supplied_token, self.csrf_token
        )
