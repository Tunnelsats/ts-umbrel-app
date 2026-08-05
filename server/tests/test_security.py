import os
import sys

import pytest


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from security import ManagementSecurity


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
def test_valid_hosts_are_accepted_only_from_loopback_proxy(host):
    assert ManagementSecurity.host_is_allowed(host, "127.0.0.1") is True
    assert ManagementSecurity.host_is_allowed(host, "192.168.1.20") is False


@pytest.mark.parametrize("host", ["", "bad host", "bad.example/path", "[broken"])
def test_malformed_hosts_are_rejected_even_from_loopback(host):
    assert ManagementSecurity.host_is_allowed(host, "127.0.0.1") is False


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
