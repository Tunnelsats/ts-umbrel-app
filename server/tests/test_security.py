import os
import socket
import sys
from unittest.mock import patch

import pytest


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from security import ManagementSecurity


@pytest.fixture(autouse=True)
def clear_default_gateway_cache():
    gateway_lookup = ManagementSecurity._cached_default_gateway_ip
    gateway_lookup.cache_clear()
    yield
    gateway_lookup.cache_clear()


def route_table(*rows):
    header = (
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\t"
        "MTU\tWindow\tIRTT\n"
    )
    return header + "".join(f"{row}\n" for row in rows)


def test_default_gateway_uses_lowest_metric_usable_default_route(tmp_path):
    route_path = tmp_path / "route"
    route_path.write_text(
        route_table(
            "eth0\t00000000\t010015AC\t0003\t0\t0\t100\t00000000\t0\t0\t0",
            "eth1\t00000000\t01001EAC\t0003\t0\t0\t50\t00000000\t0\t0\t0",
            "eth2\t00000000\t010028AC\t0001\t0\t0\t1\t00000000\t0\t0\t0",
            "eth3\t00000000\t010032AC\t0003\t0\t0\t0\t00FFFFFF\t0\t0\t0",
            "eth4\t00000000\t01003CAC\t0203\t0\t0\t0\t00000000\t0\t0\t0",
        ),
        encoding="utf-8",
    )

    assert ManagementSecurity.default_gateway_ip(str(route_path)) == "172.30.0.1"


@pytest.mark.parametrize(
    "rows",
    [
        (),
        ("malformed",),
        ("eth0\t00000000\t00000000\t0003\t0\t0\t0\t00000000",),
        ("eth0\t00000000\tNOTHEX00\t0003\t0\t0\t0\t00000000",),
        ("eth0\t00000000\t010015AC\t0000\t0\t0\t0\t00000000",),
    ],
)
def test_default_gateway_ignores_malformed_or_unusable_routes(tmp_path, rows):
    route_path = tmp_path / "route"
    route_path.write_text(route_table(*rows), encoding="utf-8")

    assert ManagementSecurity.default_gateway_ip(str(route_path)) is None


def test_default_gateway_fails_closed_when_route_table_is_unavailable(tmp_path):
    assert ManagementSecurity.default_gateway_ip(str(tmp_path / "missing")) is None

    with patch("builtins.open", side_effect=PermissionError("denied")):
        assert ManagementSecurity.default_gateway_ip("/unreadable/route") is None


def test_default_gateway_result_is_cached(tmp_path):
    route_path = tmp_path / "route"
    route_path.write_text(
        route_table(
            "eth0\t00000000\t010015AC\t0003\t0\t0\t100\t00000000\t0\t0\t0"
        ),
        encoding="utf-8",
    )
    assert ManagementSecurity.default_gateway_ip(str(route_path)) == "172.21.0.1"

    route_path.write_text(
        route_table(
            "eth0\t00000000\t01001EAC\t0003\t0\t0\t100\t00000000\t0\t0\t0"
        ),
        encoding="utf-8",
    )

    assert ManagementSecurity.default_gateway_ip(str(route_path)) == "172.21.0.1"


def test_default_gateway_retries_after_transient_negative_lookup(tmp_path):
    route_path = tmp_path / "route"
    route_path.write_text(route_table(), encoding="utf-8")
    assert ManagementSecurity.default_gateway_ip(str(route_path)) is None

    route_path.write_text(
        route_table(
            "eth0\t00000000\t010015AC\t0003\t0\t0\t100\t00000000\t0\t0\t0"
        ),
        encoding="utf-8",
    )

    assert ManagementSecurity.default_gateway_ip(str(route_path)) == "172.21.0.1"


def test_peer_trust_accepts_loopback_without_external_lookups():
    security = ManagementSecurity()
    with patch.object(
        security, "default_gateway_ip", side_effect=AssertionError("gateway lookup")
    ), patch(
        "security.socket.getaddrinfo", side_effect=AssertionError("DNS lookup")
    ):
        assert security.peer_is_trusted("::ffff:127.0.0.1", "app_proxy") is True


def test_peer_trust_accepts_exact_default_gateway_before_dns():
    security = ManagementSecurity()
    with patch.object(
        security, "default_gateway_ip", return_value="172.30.0.1"
    ), patch(
        "security.socket.getaddrinfo", side_effect=AssertionError("DNS lookup")
    ):
        assert security.peer_is_trusted("172.30.0.1", "app_proxy") is True


def test_peer_trust_preserves_legacy_resolved_proxy():
    security = ManagementSecurity()
    resolved_proxy = [(None, None, None, None, ("172.30.0.2", 0))]
    with patch.object(
        security, "default_gateway_ip", return_value="172.30.0.1"
    ), patch("security.socket.getaddrinfo", return_value=resolved_proxy):
        assert security.peer_is_trusted("172.30.0.2", "app_proxy") is True


def test_peer_trust_fails_closed_when_proxy_dns_is_unavailable():
    security = ManagementSecurity()
    with patch.object(security, "default_gateway_ip", return_value=None), patch(
        "security.socket.getaddrinfo", side_effect=socket.gaierror("not found")
    ):
        assert security.peer_is_trusted("172.30.0.3", "app_proxy") is False


@pytest.mark.parametrize("direct_peer", ["", "not-an-ip", None])
def test_peer_trust_rejects_invalid_direct_peers_without_dns(direct_peer):
    security = ManagementSecurity()
    with patch(
        "security.socket.getaddrinfo", side_effect=AssertionError("DNS lookup")
    ):
        assert security.peer_is_trusted(direct_peer, "app_proxy") is False


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({"werkzeug.proxy_fix.orig": {"REMOTE_ADDR": "127.0.0.2"}}, "127.0.0.2"),
        ({"werkzeug.proxy_fix.orig": ("127.0.0.3", "http", "host", "9739")}, "127.0.0.3"),
        ({"REMOTE_ADDR": "127.0.0.4"}, "127.0.0.4"),
        ({"werkzeug.proxy_fix.orig": ()}, ""),
    ],
)
def test_direct_remote_addr_supports_proxyfix_metadata_variants(metadata, expected):
    assert ManagementSecurity.direct_remote_addr(metadata) == expected


@pytest.mark.parametrize(
    "host",
    [
        "umbrel.local:9739",
        "umbrel.lan:9739",
        "node.tailnet.ts.net:9739",
        "example.onion:9739",
        "192.168.1.20:9739",
        "[fd00::10]:9739",
    ],
)
def test_valid_host_authorities_are_accepted(host):
    assert ManagementSecurity.host_is_allowed(host) is True


@pytest.mark.parametrize("host", ["", "bad host", "bad.example/path", "[broken"])
def test_malformed_host_authorities_are_rejected(host):
    assert ManagementSecurity.host_is_allowed(host) is False


def test_origin_comparison_includes_scheme_host_and_effective_port():
    assert ManagementSecurity.origin_matches(
        "https://node.tailnet.ts.net:9739", "https", "node.tailnet.ts.net:9739"
    )
    assert not ManagementSecurity.origin_matches(
        "http://node.tailnet.ts.net:9739", "https", "node.tailnet.ts.net:9739"
    )


def test_csrf_comparison_uses_the_session_token():
    security = ManagementSecurity(csrf_token="known-token")

    assert security.csrf_matches("known-token") is True
    assert security.csrf_matches("wrong-token") is False
    assert security.csrf_matches("") is False
