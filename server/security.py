import secrets
from ipaddress import ip_address
from urllib.parse import urlsplit


class ManagementSecurity:
    """Pure management-request security primitives used by the Flask boundary."""

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

    @classmethod
    def host_is_allowed(cls, raw_host, direct_remote_addr):
        # app_proxy owns authentication. Once the actual socket peer is
        # loopback, accept every syntactically valid host so custom DNS,
        # Tailscale, Tor, and additional reverse proxies cannot lock users out.
        return cls.parse_host_authority(raw_host) is not None and cls.is_loopback_ip(
            direct_remote_addr
        )

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
