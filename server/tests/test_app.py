import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest
import json
import stat
import subprocess
import tempfile
import time
from unittest.mock import patch, MagicMock
import requests
from app import app
import app as app_module
from daemon_transport import DaemonUnavailable, UnixHTTPResponse

# --- Fixtures ---

@pytest.fixture(autouse=True)
def clear_caches():
    """Ensure server-side caches are cleared between every test to prevent isolation leaks."""
    app_module._SUBSCRIPTION_CACHE.clear()
    if hasattr(app_module, '_probe_cache'):
        app_module._probe_cache.clear()
    if hasattr(app_module, '_in_flight_probes'):
        app_module._in_flight_probes.clear()
    yield


@pytest.fixture(autouse=True)
def mock_lnd_announcement_cleanup():
    """Keep legacy endpoint tests focused; privacy-specific tests override this result."""
    original = app_module.clean_and_verify_lnd_announcements
    with patch('app.clean_and_verify_lnd_announcements', return_value=(True, [], [])) as mocked:
        yield mocked, original


@pytest.fixture(autouse=True)
def mock_target_reconcile():
    """Keep configuration tests synchronous; reconciliation-specific tests use the original helper."""
    original = app_module.reconcile_target_and_wait
    result = {"state": {"target_impl": "lnd", "rules_synced": True, "last_error": None}}
    with patch('app.reconcile_target_and_wait', return_value=(True, result)) as mocked:
        yield mocked, original

@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client

@pytest.fixture
def data_dir(tmp_path):
    """Provide a temp DATA_DIR and patch it into the app."""
    with patch('app.DATA_DIR', str(tmp_path)):
        yield tmp_path


@pytest.fixture
def management_browser_security():
    previous = app.config.get("MANAGEMENT_BROWSER_SECURITY_ENABLED")
    app.config["MANAGEMENT_BROWSER_SECURITY_ENABLED"] = True
    try:
        yield
    finally:
        if previous is None:
            app.config.pop("MANAGEMENT_BROWSER_SECURITY_ENABLED", None)
        else:
            app.config["MANAGEMENT_BROWSER_SECURITY_ENABLED"] = previous

# --- Existing Tests ---

def test_status_endpoint(client):
    res = client.get('/api/local/status')
    assert res.status_code == 200
    data = json.loads(res.data)
    assert 'wg_status' in data


def test_web_role_proxies_privileged_local_routes(client):
    daemon_response = UnixHTTPResponse(
        status=200,
        reason="OK",
        headers=(("Content-Type", "application/json"),),
        body=b'{"rules_synced":true}',
    )
    with patch.object(app_module, "TUNNELSATS_ROLE", "web"), patch(
        "app.request_over_unix_socket", return_value=daemon_response
    ) as request_daemon:
        response = client.get("/api/local/status")

    assert response.status_code == 200
    assert response.get_json() == {"rules_synced": True}
    assert request_daemon.call_args.args[:3] == (
        app_module.DAEMON_SOCKET_PATH,
        "GET",
        "/api/local/status",
    )


def test_web_role_fails_closed_when_daemon_is_unavailable(client):
    with patch.object(app_module, "TUNNELSATS_ROLE", "web"), patch(
        "app.request_over_unix_socket", side_effect=DaemonUnavailable("offline")
    ):
        response = client.get("/api/local/status")

    assert response.status_code == 503
    assert response.get_json() == {
        "success": False,
        "error": "TunnelSats daemon is unavailable.",
    }


def test_web_role_forwards_only_bounded_management_request_data(client):
    daemon_response = UnixHTTPResponse(
        status=202,
        reason="Accepted",
        headers=(
            ("Content-Type", "application/json"),
            ("Content-Disposition", 'attachment; filename="result.json"'),
            ("Connection", "keep-alive"),
        ),
        body=b'{"accepted":true}',
    )
    with patch.object(app_module, "TUNNELSATS_ROLE", "web"), patch(
        "app.request_over_unix_socket", return_value=daemon_response
    ) as request_daemon:
        response = client.post(
            "/api/local/reconcile?source=ui",
            data=b'{"requested":true}',
            content_type="application/json",
        )

    assert response.status_code == 202
    assert response.headers["Content-Disposition"] == 'attachment; filename="result.json"'
    assert "Connection" not in response.headers
    assert request_daemon.call_args.args[:3] == (
        app_module.DAEMON_SOCKET_PATH,
        "POST",
        "/api/local/reconcile?source=ui",
    )
    assert request_daemon.call_args.kwargs["body"] == b'{"requested":true}'
    assert request_daemon.call_args.kwargs["headers"] == {
        "Content-Type": "application/json"
    }


def test_untrusted_web_peer_is_rejected_before_daemon_forwarding(
    client, management_browser_security
):
    with patch.object(app_module, "TUNNELSATS_ROLE", "web"), patch.dict(
        os.environ, {"MANAGEMENT_TRUSTED_PROXY_HOST": ""}, clear=False
    ), patch("app.request_over_unix_socket") as request_daemon:
        response = client.get(
            "/api/local/status",
            environ_base={"REMOTE_ADDR": "172.30.0.3"},
            headers={"Host": "umbrel.local:9739"},
        )

    assert response.status_code == 403
    request_daemon.assert_not_called()


def test_web_role_preserves_method_not_allowed_without_contacting_daemon(client):
    with patch.object(app_module, "TUNNELSATS_ROLE", "web"), patch(
        "app.request_over_unix_socket"
    ) as request_daemon:
        response = client.delete("/api/local/status")

    assert response.status_code == 405
    assert response.get_json()["error"] == "Method not allowed."
    request_daemon.assert_not_called()


def test_web_role_rejects_oversized_management_body_before_daemon(client):
    with patch.object(app_module, "TUNNELSATS_ROLE", "web"), patch(
        "app.request_over_unix_socket"
    ) as request_daemon:
        response = client.post(
            "/api/local/upload-config",
            data=b"x" * (app_module.DEFAULT_MAX_REQUEST_BYTES + 1),
            content_type="application/json",
        )

    assert response.status_code == 413
    request_daemon.assert_not_called()


def test_web_role_keeps_csrf_session_at_browser_boundary(client):
    with patch.object(app_module, "TUNNELSATS_ROLE", "web"), patch(
        "app.request_over_unix_socket"
    ) as request_daemon:
        response = client.get("/api/local/session")

    assert response.status_code == 200
    assert response.get_json()["csrf_token"] == app_module.MANAGEMENT_CSRF_TOKEN
    request_daemon.assert_not_called()


def test_daemon_role_rejects_non_management_routes(client):
    with patch.object(app_module, "TUNNELSATS_ROLE", "daemon"):
        response = client.get("/api/servers")

    assert response.status_code == 404


def test_management_security_trusts_only_resolved_app_proxy_peer(
    client, management_browser_security
):
    resolved_proxy = [(None, None, None, None, ("172.30.0.2", 0))]
    with patch.dict(
        os.environ, {"MANAGEMENT_TRUSTED_PROXY_HOST": "app_proxy"}, clear=False
    ), patch("app.socket.getaddrinfo", return_value=resolved_proxy):
        trusted = client.get(
            "/api/local/session",
            environ_base={"REMOTE_ADDR": "172.30.0.2"},
            headers={"Host": "umbrel.local:9739"},
        )
        untrusted = client.get(
            "/api/local/session",
            environ_base={"REMOTE_ADDR": "172.30.0.3"},
            headers={"Host": "umbrel.local:9739"},
        )

    assert trusted.status_code == 200
    assert untrusted.status_code == 403


@pytest.mark.parametrize("secure_mode", [False, True])
def test_dashboard_bind_config_honors_explicit_loopback_in_both_modes(secure_mode):
    with patch.dict(
        os.environ,
        {
            "DASHBOARD_BIND_HOST": "127.0.0.1",
            "DASHBOARD_BIND_PORT": "9740",
        },
        clear=False,
    ), patch.object(app_module, "SECURE_MODE", secure_mode):
        assert app_module.dashboard_bind_config() == ("127.0.0.1", 9740)


@pytest.mark.parametrize(
    ("k3s_mode", "expected"),
    [
        (False, ("127.0.0.1", 9740)),
        (True, ("0.0.0.0", 9739)),
    ],
)
def test_dashboard_bind_config_defaults_fail_closed_without_changing_k3s(
    k3s_mode, expected
):
    with patch.dict(os.environ, {}, clear=True), patch.object(
        app_module, "K3S_MODE", k3s_mode
    ):
        assert app_module.dashboard_bind_config() == expected


@pytest.mark.parametrize("invalid_port", ["", "not-a-port", "0", "65536"])
def test_dashboard_bind_config_rejects_invalid_ports(invalid_port):
    with patch.dict(
        os.environ,
        {
            "DASHBOARD_BIND_HOST": "127.0.0.1",
            "DASHBOARD_BIND_PORT": invalid_port,
        },
        clear=False,
    ):
        with pytest.raises(ValueError, match="DASHBOARD_BIND_PORT"):
            app_module.dashboard_bind_config()


@pytest.mark.parametrize("secure_mode", [False, True])
def test_management_session_bootstraps_uncached_csrf_in_both_modes(
    client, management_browser_security, secure_mode
):
    with patch.object(app_module, "SECURE_MODE", secure_mode):
        response = client.get("/api/local/session")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["authenticated"] is True
    assert isinstance(payload["csrf_token"], str)
    assert len(payload["csrf_token"]) >= 32
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"
    assert "WWW-Authenticate" not in response.headers


@pytest.mark.parametrize("secure_mode", [False, True])
@pytest.mark.parametrize(
    ("path", "request_kwargs"),
    [
        ("/api/local/upload-config", {"json": {"config": "test"}}),
        ("/api/local/restart", {}),
        ("/api/local/reconcile", {}),
        ("/api/local/configure-node", {"json": {"nodeType": "lnd"}}),
        ("/api/local/restore-node", {}),
        ("/api/subscription/renew", {"json": {"duration": 1}}),
        ("/api/subscription/claim", {"json": {"paymentHash": "hash"}}),
    ],
)
def test_management_mutations_require_browser_security_in_both_modes(
    client,
    management_browser_security,
    secure_mode,
    path,
    request_kwargs,
):
    with patch.object(app_module, "SECURE_MODE", secure_mode):
        response = client.post(path, **request_kwargs)

    assert response.status_code == 403
    assert response.get_json() == {"success": False, "error": "Forbidden"}
    assert "WWW-Authenticate" not in response.headers


@pytest.mark.parametrize("secure_mode", [False, True])
@pytest.mark.parametrize(
    "origin",
    [None, "null", "not-an-origin", "https://evil.example"],
)
def test_management_mutations_reject_invalid_origins_in_both_modes(
    client, management_browser_security, secure_mode, origin
):
    session = client.get("/api/local/session")
    csrf_token = session.get_json()["csrf_token"]
    headers = {"X-TunnelSats-CSRF-Token": csrf_token}
    if origin is not None:
        headers["Origin"] = origin

    with patch.object(app_module, "SECURE_MODE", secure_mode):
        response = client.post("/api/local/restart", headers=headers)

    assert response.status_code == 403
    assert response.get_json() == {"success": False, "error": "Forbidden"}


@pytest.mark.parametrize("secure_mode", [False, True])
def test_management_mutation_accepts_valid_same_origin_csrf_in_both_modes(
    client, management_browser_security, secure_mode
):
    session = client.get("/api/local/session")
    csrf_token = session.get_json()["csrf_token"]
    headers = {
        "Origin": "http://localhost",
        "X-TunnelSats-CSRF-Token": csrf_token,
        "Sec-Fetch-Site": "same-origin",
    }

    with patch.object(app_module, "SECURE_MODE", secure_mode), patch(
        "builtins.open", MagicMock()
    ):
        response = client.post("/api/local/restart", headers=headers)

    assert response.status_code == 200
    assert response.get_json()["success"] is True
    assert response.headers["Cache-Control"] == "no-store"


def test_management_security_rejects_non_loopback_direct_peer(
    client, management_browser_security
):
    response = client.get(
        "/api/local/session",
        headers={"X-Forwarded-For": "127.0.0.1"},
        environ_base={"REMOTE_ADDR": "192.168.1.20"},
    )

    assert response.status_code == 403
    assert response.get_json() == {"success": False, "error": "Forbidden"}


@pytest.mark.parametrize(
    ("proxyfix_orig", "expected"),
    [
        ({"REMOTE_ADDR": "127.0.0.2"}, "127.0.0.2"),
        (("127.0.0.3", "http", "umbrel.local", "9739"), "127.0.0.3"),
        (None, "127.0.0.4"),
    ],
)
def test_direct_peer_extraction_supports_proxyfix_versions(proxyfix_orig, expected):
    with app.test_request_context(environ_base={"REMOTE_ADDR": "127.0.0.4"}):
        if proxyfix_orig is not None:
            app_module.request.environ["werkzeug.proxy_fix.orig"] = proxyfix_orig

        assert app_module._get_direct_remote_addr() == expected


@pytest.mark.parametrize(
    "forwarded_host",
    [
        "umbrel.local:9739",
        "examplehiddenservice.onion:9739",
        "tunnelsats.example:9739",
    ],
)
def test_management_security_accepts_known_forwarded_umbrel_hosts(
    client, management_browser_security, forwarded_host
):
    response = client.get(
        "/api/local/session",
        headers={
            "X-Forwarded-For": "192.168.1.20",
            "X-Forwarded-Host": forwarded_host,
            "X-Forwarded-Proto": "http",
        },
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 200


@pytest.mark.parametrize(
    "forwarded_host",
    ["umbrel.lan:9739", "node.tailnet.ts.net:9739", "custom.home:9739"],
)
def test_management_security_accepts_any_valid_host_from_loopback_proxy(
    client, management_browser_security, forwarded_host
):
    response = client.get(
        "/api/local/session",
        headers={"X-Forwarded-Host": forwarded_host},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 200


@pytest.mark.parametrize("forwarded_host", ["bad host", "bad.example/path", "[broken"])
def test_management_security_rejects_malformed_host_from_loopback_proxy(
    client, management_browser_security, forwarded_host
):
    response = client.get(
        "/api/local/session",
        headers={"X-Forwarded-Host": forwarded_host},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 403
    assert response.get_json() == {"success": False, "error": "Forbidden"}


@pytest.mark.parametrize(
    "forwarded_host",
    [
        "192.168.1.10:9739",
        "100.100.10.20:9739",
        "[fd00::10]:9739",
    ],
)
def test_management_security_accepts_ip_hosts_from_loopback_proxy(
    client, management_browser_security, forwarded_host
):
    response = client.get(
        "/api/local/session",
        headers={"X-Forwarded-Host": forwarded_host},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 200


def test_management_security_accepts_exact_forwarded_origin(
    client, management_browser_security
):
    session = client.get("/api/local/session")
    csrf_token = session.get_json()["csrf_token"]
    with patch.dict(
        os.environ, {"DEVICE_DOMAIN_NAME": "umbrel.local"}, clear=False
    ), patch("builtins.open", MagicMock()):
        response = client.post(
            "/api/local/restart",
            headers={
                "Origin": "https://umbrel.local:9739",
                "X-Forwarded-Host": "umbrel.local:9739",
                "X-Forwarded-Proto": "https",
                "X-TunnelSats-CSRF-Token": csrf_token,
            },
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )

    assert response.status_code == 200


def test_json_management_route_rejects_form_compatible_body(
    client, management_browser_security
):
    session = client.get("/api/local/session")
    csrf_token = session.get_json()["csrf_token"]
    response = client.post(
        "/api/local/upload-config",
        data={"config_text": "test"},
        headers={
            "Origin": "http://localhost",
            "X-TunnelSats-CSRF-Token": csrf_token,
        },
    )

    assert response.status_code == 403
    assert response.get_json() == {"success": False, "error": "Forbidden"}


def test_management_audit_log_does_not_record_supplied_token(
    client, management_browser_security, caplog
):
    supplied_secret = "secret-token-must-not-be-logged"
    response = client.post(
        "/api/local/restart",
        headers={
            "Origin": "http://localhost",
            "X-TunnelSats-CSRF-Token": supplied_secret,
        },
    )

    assert response.status_code == 403
    assert response.headers["X-TunnelSats-CSRF-Refresh"] == "required"
    assert "reason=csrf" in caplog.text
    assert supplied_secret not in caplog.text


def test_authenticated_claim_logs_do_not_record_subscription_secrets(
    client, management_browser_security, caplog
):
    session = client.get("/api/local/session")
    csrf_token = session.get_json()["csrf_token"]
    payment_secret = "payment-hash-must-not-be-logged"
    upstream_secret = "upstream-body-must-not-be-logged"
    upstream_response = MagicMock()
    upstream_response.status_code = 200
    upstream_response.content = upstream_secret.encode()
    upstream_response.json.side_effect = json.JSONDecodeError(
        "invalid",
        upstream_secret,
        0,
    )

    with patch("app.requests.post", return_value=upstream_response):
        response = client.post(
            "/api/subscription/claim",
            json={
                "paymentHash": payment_secret,
                "wgPresharedKey": "preshared-key-must-not-be-logged",
            },
            headers={
                "Origin": "http://localhost",
                "X-TunnelSats-CSRF-Token": csrf_token,
            },
        )

    assert response.status_code == 400
    assert payment_secret not in caplog.text
    assert upstream_secret not in caplog.text
    assert "preshared-key-must-not-be-logged" not in caplog.text


@pytest.mark.parametrize("secure_mode", [False, True])
def test_subscription_status_with_local_side_effect_requires_csrf_in_both_modes(
    client, management_browser_security, secure_mode
):
    upstream_response = MagicMock()
    upstream_response.status_code = 200
    upstream_response.content = b'{"status":"pending"}'
    upstream_response.headers = {"Content-Type": "application/json"}

    with patch.object(app_module, "SECURE_MODE", secure_mode), patch(
        "app.requests.get", return_value=upstream_response
    ) as mock_get:
        response = client.get("/api/subscription/payment-hash")

    assert response.status_code == 403
    assert response.get_json() == {"success": False, "error": "Forbidden"}
    mock_get.assert_not_called()


@pytest.mark.parametrize("secure_mode", [False, True])
def test_subscription_status_accepts_csrf_protected_poll_in_both_modes(
    client, management_browser_security, secure_mode
):
    session = client.get("/api/local/session")
    csrf_token = session.get_json()["csrf_token"]
    upstream_response = MagicMock()
    upstream_response.status_code = 200
    upstream_response.content = b'{"status":"pending"}'
    upstream_response.headers = {"Content-Type": "application/json"}

    with patch.object(app_module, "SECURE_MODE", secure_mode), patch(
        "app.requests.get", return_value=upstream_response
    ):
        response = client.get(
            "/api/subscription/payment-hash",
            headers={"X-TunnelSats-CSRF-Token": csrf_token},
        )

    assert response.status_code == 200


@patch('app.time.time', return_value=1_000_000)
@patch('app.docker_api', return_value=[])
@patch('app.subprocess.check_output')
def test_local_status_reports_disconnected_when_latest_handshake_is_zero(mock_check_output, _mock_docker_api, _mock_time, client):
    def check_output_side_effect(cmd, **kwargs):
        if cmd == ["wg", "show", "tunnelsatsv2"]:
            return (
                b"interface: tunnelsatsv2\n"
                b"  public key: localPubKey123\n"
                b"peer: remotePeerKey123\n"
            )
        if cmd == ["wg", "show", "tunnelsatsv2", "latest-handshakes"]:
            return b"remotePeerKey123\t0\n"
        raise AssertionError(f"Unexpected command: {cmd}")

    mock_check_output.side_effect = check_output_side_effect

    res = client.get('/api/local/status')
    assert res.status_code == 200
    data = json.loads(res.data)
    assert data['wg_status'] == 'Disconnected'
    assert data['vpn_active'] is False
    assert data['wg_pubkey'] == 'localPubKey123'


@patch('app.time.time', return_value=1_000_000)
@patch('app.docker_api', return_value=[])
@patch('app.subprocess.check_output')
def test_local_status_reports_disconnected_when_latest_handshake_is_stale(mock_check_output, _mock_docker_api, _mock_time, client):
    def check_output_side_effect(cmd, **kwargs):
        if cmd == ["wg", "show", "tunnelsatsv2"]:
            return (
                b"interface: tunnelsatsv2\n"
                b"  public key: localPubKey123\n"
                b"peer: remotePeerKey123\n"
            )
        if cmd == ["wg", "show", "tunnelsatsv2", "latest-handshakes"]:
            return b"remotePeerKey123\t999819\n"
        raise AssertionError(f"Unexpected command: {cmd}")

    mock_check_output.side_effect = check_output_side_effect

    res = client.get('/api/local/status')
    assert res.status_code == 200
    data = json.loads(res.data)
    assert data['wg_status'] == 'Disconnected'
    assert data['vpn_active'] is False
    assert data['wg_pubkey'] == 'localPubKey123'


@patch('app.time.time', return_value=1_000_000)
@patch('app.docker_api', return_value=[])
@patch('app.subprocess.check_output')
def test_local_status_reports_connected_when_latest_handshake_is_recent(mock_check_output, _mock_docker_api, _mock_time, client):
    def check_output_side_effect(cmd, **kwargs):
        if cmd == ["wg", "show", "tunnelsatsv2"]:
            return (
                b"interface: tunnelsatsv2\n"
                b"  public key: localPubKey123\n"
                b"peer: remotePeerKey123\n"
            )
        if cmd == ["wg", "show", "tunnelsatsv2", "latest-handshakes"]:
            return b"remotePeerKey123\t999950\n"
        raise AssertionError(f"Unexpected command: {cmd}")

    mock_check_output.side_effect = check_output_side_effect

    res = client.get('/api/local/status')
    assert res.status_code == 200
    data = json.loads(res.data)
    assert data['wg_status'] == 'Connected'
    assert data['vpn_active'] is True


@patch('app.time.time', return_value=1_000_000)
@patch('app.docker_api', return_value=[])
@patch('app.subprocess.check_output')
def test_local_status_reports_connected_when_latest_handshake_is_exactly_threshold(mock_check_output, _mock_docker_api, _mock_time, client):
    def check_output_side_effect(cmd, **kwargs):
        if cmd == ["wg", "show", "tunnelsatsv2"]:
            return (
                b"interface: tunnelsatsv2\n"
                b"  public key: localPubKey123\n"
                b"peer: remotePeerKey123\n"
            )
        if cmd == ["wg", "show", "tunnelsatsv2", "latest-handshakes"]:
            return b"remotePeerKey123\t999820\n"
        raise AssertionError(f"Unexpected command: {cmd}")

    mock_check_output.side_effect = check_output_side_effect

    res = client.get('/api/local/status')
    assert res.status_code == 200
    data = json.loads(res.data)
    assert data['wg_status'] == 'Connected'
    assert data['vpn_active'] is True


@patch('app.time.time', return_value=1_000_000)
@patch('app.docker_api', return_value=[])
@patch('app.subprocess.check_output')
def test_local_status_reports_disconnected_when_latest_handshakes_query_fails(mock_check_output, _mock_docker_api, _mock_time, client):
    def check_output_side_effect(cmd, **kwargs):
        if cmd == ["wg", "show", "tunnelsatsv2"]:
            return (
                b"interface: tunnelsatsv2\n"
                b"  public key: localPubKey123\n"
                b"peer: remotePeerKey123\n"
            )
        if cmd == ["wg", "show", "tunnelsatsv2", "latest-handshakes"]:
            raise subprocess.CalledProcessError(returncode=1, cmd=cmd, output=b"")
        raise AssertionError(f"Unexpected command: {cmd}")

    mock_check_output.side_effect = check_output_side_effect

    res = client.get('/api/local/status')
    assert res.status_code == 200
    data = json.loads(res.data)
    assert data['wg_status'] == 'Disconnected'
    assert data['vpn_active'] is False
    assert data['wg_pubkey'] == 'localPubKey123'


def test_security_headers_present(client):
    """Test that security headers (CSP, X-Frame-Options) are present on all responses."""
    res = client.get('/')
    assert res.status_code == 200
    
    # Verify Content-Security-Policy
    csp = res.headers.get('Content-Security-Policy')
    assert csp is not None
    assert "default-src 'self'" in csp
    assert "tunnelsats.com" in csp
    assert "fonts.googleapis.com" not in csp
    
    # Verify Defense-in-Depth headers
    assert res.headers.get('X-Frame-Options') == 'SAMEORIGIN'
    assert res.headers.get('X-Content-Type-Options') == 'nosniff'


def test_exception_handler_does_not_expose_internal_details():
    with app.test_request_context('/api/test'):
        response, status = app_module.handle_exception(
            RuntimeError('secret=/data/private/token')
        )

    assert status == 500
    assert response.get_json() == {
        'success': False,
        'error': 'Internal server error',
    }
    assert b'secret' not in response.data


def test_denied_api_request_returns_json_with_original_status(client):
    res = client.get(
        '/api/local/status',
        environ_base={'REMOTE_ADDR': '203.0.113.10'},
    )

    assert res.status_code == 403
    assert res.is_json
    assert res.get_json() == {
        'success': False,
        'error': 'Forbidden',
    }


def test_missing_static_asset_remains_404(client):
    res = client.get('/js/missing.js')

    assert res.status_code == 404
    assert b'<title>TunnelSats</title>' not in res.data


def test_localized_vendor_assets_are_reachable(client):
    """Test that localized 3D assets in /web/vendor are correctly served."""
    vendor_files = [
        '/vendor/globe.gl.min.js',
        '/vendor/img/earth-dark.jpg',
        '/vendor/img/earth-topology.png',
        '/vendor/inter.css',
        '/dist/tailwind.css',
        '/vendor/qrcode.min.js'
    ]
    for file_path in vendor_files:
        res = client.get(file_path)
        assert res.status_code == 200, f"Failed to reach localized asset: {file_path}"

def test_proxy_fix(client):
    res = client.get('/api/local/status', environ_base={
        'REMOTE_ADDR': '127.0.0.1',
        'HTTP_X_FORWARDED_FOR': '192.168.1.50'
    })
    assert res.status_code == 200


def test_default_cln_config_path_matches_compose_mount_contract():
    # docker-compose mounts .../lightningd/bitcoin at /lightning-data/cln.
    # The default CLN config path must stay aligned with that runtime contract.
    assert app_module.CLN_CONFIG_PATH == '/lightning-data/cln/config'
 
 
def test_default_lnd_config_path_matches_compose_mount_contract():
    # docker-compose mounts .../lightning/data/lnd at /lightning-data/lnd.
    # The default LND config path must stay aligned with that runtime contract.
    assert app_module.LND_CONFIG_PATH == '/lightning-data/lnd/lnd.conf'



# --- Phase 1: Claim Tests ---

MOCK_CLAIM_RESPONSE = {
    "success": True,
    "message": "Subscription claimed successfully",
    "subscription": {
        "id": "sub-xyz789",
        "serverId": "eu-de",
        "expiresAt": "2026-04-05T10:30:00.000Z"
    },
    "server": {
        "publicKey": "serverPublicKeyBase64==",
        "endpoint": "de2.tunnelsats.com:51820",
        "allowedIPs": "0.0.0.0/0, ::/0"
    },
    "peer": {
        "address": "10.8.0.42/32",
        "privateKey": "clientPrivateKeyBase64==",
        "presharedKey": "presharedKeyBase64=="
    },
    "config": (
        "# TunnelSats WireGuard Configuration\n"
        "# Server: de2.tunnelsats.com\n"
        "# Port Forwarding: 35825\n"
        "# myPubKey: L7vkSGz/ODjzBTmYo+gkJADq9GRF0NfxjOsFBNDVjQ4=\n"
        "# Valid Until: 2026-04-05T10:30:00.000Z\n"
        "[Interface]\n"
        "PrivateKey = clientPrivateKeyBase64==\n"
        "Address = 10.8.0.42/32\n"
        "\n"
        "[Peer]\n"
        "PublicKey = serverPublicKeyBase64==\n"
        "PresharedKey = presharedKeyBase64==\n"
        "Endpoint = de2.tunnelsats.com:51820\n"
        "AllowedIPs = 0.0.0.0/0, ::/0\n"
        "PersistentKeepalive = 25\n"
    )
}

def _mock_claim_post(*args, **kwargs):
    """Mock requests.post for the claim endpoint."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = MOCK_CLAIM_RESPONSE
    mock_resp.content = json.dumps(MOCK_CLAIM_RESPONSE).encode()
    mock_resp.headers = {"Content-Type": "application/json"}
    return mock_resp


class TestClaimSavesConfig:
    """Test that claim_subscription correctly intercepts and saves the config."""

    @patch('app.requests.post', side_effect=_mock_claim_post)
    def test_claim_saves_conf_file_from_config(self, mock_post, client, data_dir):
        """The .conf file must be written from the 'config' field."""
        res = client.post('/api/subscription/claim',
                          json={"paymentHash": "test-hash-123", "referralCode": None},
                          content_type='application/json')
        assert res.status_code == 200

        # Check that a .conf file was written
        conf_files = [f for f in os.listdir(data_dir) if f.endswith('.conf')]
        assert len(conf_files) == 1
        assert 'tunnelsats' in conf_files[0]

        # Verify content matches config
        with open(os.path.join(data_dir, conf_files[0])) as f:
            content = f.read()
        assert '[Interface]' in content
        assert 'clientPrivateKeyBase64==' in content
        assert '# Port Forwarding: 35825' in content

    @patch('app.requests.post')
    def test_claim_saves_conf_file_from_legacy_fullconfig_fallback(self, mock_post, client, data_dir):
        """Legacy 'fullConfig' fallback should still be accepted."""
        legacy_response = MOCK_CLAIM_RESPONSE.copy()
        legacy_response["fullConfig"] = legacy_response["config"]
        legacy_response.pop("config", None)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = legacy_response
        mock_resp.content = json.dumps(legacy_response).encode()
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_post.return_value = mock_resp

        res = client.post('/api/subscription/claim',
                          json={"paymentHash": "test-hash-123", "referralCode": None},
                          content_type='application/json')
        assert res.status_code == 200

        conf_files = [f for f in os.listdir(data_dir) if f.endswith('.conf')]
        assert len(conf_files) == 1

    @patch('app.requests.post')
    def test_claim_adds_missing_persistent_keepalive(self, mock_post, client, data_dir):
        """Claimed peers must keep handshakes fresh while source traffic is quarantined."""
        claim_response = MOCK_CLAIM_RESPONSE.copy()
        claim_response["config"] = claim_response["config"].replace(
            "PersistentKeepalive = 25\n", ""
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = claim_response
        mock_resp.content = json.dumps(claim_response).encode()
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_post.return_value = mock_resp

        res = client.post(
            '/api/subscription/claim',
            json={"paymentHash": "test-hash-123", "referralCode": None},
            content_type='application/json',
        )

        assert res.status_code == 200
        conf_files = [f for f in os.listdir(data_dir) if f.endswith('.conf')]
        assert len(conf_files) == 1
        with open(os.path.join(data_dir, conf_files[0])) as config_file:
            saved_config = config_file.read()
        assert saved_config.count("PersistentKeepalive = 25") == 1

    @patch('app.requests.post', side_effect=_mock_claim_post)
    def test_claim_saves_metadata_json(self, mock_post, client, data_dir):
        """A metadata file must be created with fields from the response."""
        res = client.post('/api/subscription/claim',
                          json={"paymentHash": "test-hash-123", "referralCode": None},
                          content_type='application/json')
        assert res.status_code == 200

        meta_path = os.path.join(data_dir, app_module.META_FILE)
        assert os.path.exists(meta_path), f"{app_module.META_FILE} not created"

        with open(meta_path) as f:
            meta = json.load(f)

        assert meta['serverId'] == 'eu-de'
        assert meta['paymentHash'] == 'test-hash-123'
        assert meta['peerAddress'] == '10.8.0.42/32'
        assert meta['presharedKey'] == 'presharedKeyBase64=='
        assert meta['vpnPort'] == 35825
        assert meta['serverDomain'] == 'de2.tunnelsats.com'
        assert meta['wgEndpoint'] == 'de2.tunnelsats.com:51820'
        assert meta['expiresAt'] == '2026-04-05T10:30:00.000Z'
        assert 'wgPublicKey' in meta
        assert 'claimedAt' in meta

    def test_parse_config_comments_accepts_space_separated_vpnport(self):
        parsed = app_module._parse_config_comments(
            "# VPNPort 35825\n[Interface]\nPrivateKey = x\n[Peer]\nPublicKey = y\n"
        )
        assert parsed["vpnPort"] == 35825

    @patch('app.requests.post', side_effect=_mock_claim_post)
    def test_claim_files_have_chmod_600(self, mock_post, client, data_dir):
        """Both .conf and meta.json must have 600 permissions."""
        client.post('/api/subscription/claim',
                     json={"paymentHash": "test-hash-123", "referralCode": None},
                     content_type='application/json')

        conf_files = [f for f in os.listdir(data_dir) if f.endswith('.conf')]
        assert len(conf_files) == 1
        conf_path = os.path.join(data_dir, conf_files[0])
        meta_path = os.path.join(data_dir, app_module.META_FILE)

        conf_mode = oct(os.stat(conf_path).st_mode & 0o777)
        meta_mode = oct(os.stat(meta_path).st_mode & 0o777)
        assert conf_mode == '0o600', f"Config has {conf_mode}, expected 0o600"
        assert meta_mode == '0o600', f"Metadata has {meta_mode}, expected 0o600"

    @patch('app.requests.post', side_effect=_mock_claim_post)
    def test_claim_renames_existing_configs_to_bak(self, mock_post, client, data_dir):
        """Existing .conf files should be renamed to .conf.bak, not deleted."""
        # Plant an existing config
        old_conf = data_dir / 'tunnelsats-old.conf'
        old_conf.write_text('[Interface]\nPrivateKey = old\n')

        client.post('/api/subscription/claim',
                     json={"paymentHash": "test-hash-123", "referralCode": None},
                     content_type='application/json')

        assert os.path.exists(str(old_conf) + '.bak'), "Old config was not backed up"
        assert not os.path.exists(str(old_conf)), "Old config should be renamed"

        # The new config should exist
        new_confs = [f for f in os.listdir(data_dir) if f.endswith('.conf')]
        assert len(new_confs) == 1

    @patch('app.requests.post')
    def test_claim_returns_400_when_upstream_returns_status_error(self, mock_post, client, data_dir):
        """If upstream returns 200 OK but status=error, proxy must fail loudly with 400."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "error", "message": "Subscription already claimed"}
        mock_resp.content = b'{"status": "error", "message": "Subscription already claimed"}'
        mock_resp.headers = {'Content-Type': 'application/json'}
        mock_post.return_value = mock_resp

        res = client.post('/api/subscription/claim',
                          json={"paymentHash": "test-hash-123", "referralCode": None},
                          content_type='application/json')
        
        assert res.status_code == 400
        assert b"Invalid upstream payload" in res.data or b"Already claimed" in res.data or b"Subscription already claimed" in res.data

        # Ensure no config was saved
        confs = [f for f in os.listdir(data_dir) if f.endswith('.conf')]
        assert len(confs) == 0

    @patch('app.requests.post')
    def test_claim_returns_400_when_upstream_returns_success_false(self, mock_post, client, data_dir):
        """If upstream returns 200 OK but success=False explicitly, proxy must fail loudly with 400."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"success": False, "status": "error", "message": "Subscription already claimed"}
        mock_resp.content = b'{"success": false, "status": "error", "message": "Subscription already claimed"}'
        mock_resp.headers = {'Content-Type': 'application/json'}
        mock_post.return_value = mock_resp

        res = client.post('/api/subscription/claim',
                          json={"paymentHash": "test-hash-123", "referralCode": None},
                          content_type='application/json')
        
        assert res.status_code == 400
        assert b"Invalid upstream payload" in res.data or b"Already claimed" in res.data or b"Subscription already claimed" in res.data

        # Ensure no config was saved
        confs = [f for f in os.listdir(data_dir) if f.endswith('.conf')]
        assert len(confs) == 0

    @patch('app.requests.post')
    def test_claim_returns_400_when_upstream_omits_config(self, mock_post, client, data_dir):
        """If upstream returns 200 OK but omits all WireGuard config keys, proxy must fail with 400."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "active", "message": "Success but no config", "subscription": {}}
        mock_resp.content = b'{"status": "active", "message": "Success but no config", "subscription": {}}'
        mock_resp.headers = {'Content-Type': 'application/json'}
        mock_post.return_value = mock_resp

        res = client.post('/api/subscription/claim',
                          json={"paymentHash": "test-hash-123", "referralCode": None},
                          content_type='application/json')
        
        assert res.status_code == 400
        assert b"Invalid upstream payload" in res.data

        # Ensure no config was saved
        confs = [f for f in os.listdir(data_dir) if f.endswith('.conf')]
        assert len(confs) == 0

    @patch('app.requests.post')
    def test_claim_returns_400_when_upstream_config_contains_exec_hook(self, mock_post, client, data_dir):
        malicious_response = MOCK_CLAIM_RESPONSE.copy()
        malicious_response["config"] = (
            "[Interface]\n"
            "PrivateKey = clientPrivateKeyBase64==\n"
            "PostUp = touch /tmp/pwned\n"
            "\n"
            "[Peer]\n"
            "PublicKey = serverPublicKeyBase64==\n"
            "Endpoint = de2.tunnelsats.com:51820\n"
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = malicious_response
        mock_resp.content = json.dumps(malicious_response).encode()
        mock_resp.headers = {'Content-Type': 'application/json'}
        mock_post.return_value = mock_resp

        res = client.post('/api/subscription/claim',
                          json={"paymentHash": "test-hash-123", "referralCode": None},
                          content_type='application/json')

        assert res.status_code == 400
        assert b"Unsafe WireGuard configuration" in res.data
        confs = [f for f in os.listdir(data_dir) if f.endswith('.conf')]
        assert len(confs) == 0

    @patch('app.requests.post')
    def test_claim_returns_400_when_upstream_returns_non_object_json(self, mock_post, client, data_dir):
        """If upstream returns JSON but not an object, claim endpoint should reject with 400."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = []
        mock_resp.content = b"[]"
        mock_resp.headers = {'Content-Type': 'application/json'}
        mock_post.return_value = mock_resp

        res = client.post('/api/subscription/claim',
                          json={"paymentHash": "test-hash-123", "referralCode": None},
                          content_type='application/json')

        assert res.status_code == 400
        assert b"Invalid upstream payload" in res.data

        confs = [f for f in os.listdir(data_dir) if f.endswith('.conf')]
        assert len(confs) == 0


# --- Phase 1: Servers Proxy Test ---

class TestServersProxy:
    """Test that /api/servers proxies correctly to the upstream API."""

    @patch('app.requests.get')
    def test_servers_proxy_returns_upstream_data(self, mock_get, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = json.dumps({
            "servers": [
                {"id": "eu-de", "country": "Germany", "city": "Nuremberg", "flag": "🇩🇪", "status": "online"},
                {"id": "us-east", "country": "USA", "city": "Ashburn", "flag": "🇺🇸", "status": "online"}
            ]
        }).encode()
        mock_resp.headers = {'Content-Type': 'application/json'}
        mock_get.return_value = mock_resp

        res = client.get('/api/servers')
        assert res.status_code == 200
        data = json.loads(res.data)
        assert 'servers' in data
        assert len(data['servers']) == 2
        
        # Verify Enrichment
        de = next(s for s in data['servers'] if s['id'] == 'eu-de')
        assert de['lat'] == 49.4521  # Nuremberg default for 'de'
        assert de['label'] == 'NUREMBERG, DE'
        assert de['flag'] == '🇩🇪'

        us = next(s for s in data['servers'] if s['id'] == 'us-east')
        assert us['lat'] == 40.7128  # NY default for 'us'
        assert us['label'] == 'NEW YORK, US'
        assert us['flag'] == '🇺🇸'

class TestServerEnrichment:
    """Test the enrichment of server data with coordinates."""

    def test_local_status_enrichment(self, client, data_dir):
        # Setup metadata file
        meta_path = os.path.join(data_dir, 'tunnelsats-meta.json')
        with open(meta_path, 'w') as f:
            json.dump({
                "serverDomain": "au1.tunnelsats.com",
                "expiresAt": "2025-12-31T23:59:59Z",
                "vpnPort": "42521"
            }, f)
        
        res = client.get('/api/local/status')
        assert res.status_code == 200
        data = json.loads(res.data)
        assert data['server_domain'] == "au1.tunnelsats.com"
        assert data['lat'] == -33.8688
        assert data['lng'] == 151.2093
        assert data['label'] == "SYDNEY, AU"
        assert data['flag'] == "🇦🇺"


# --- Phase 1: Meta Endpoint Test ---

class TestMetaEndpoint:
    """Test that /api/local/meta returns stored metadata."""

    def test_meta_returns_empty_when_no_metadata(self, client, data_dir):
        res = client.get('/api/local/meta')
        assert res.status_code == 200
        data = json.loads(res.data)
        assert data == {}

    def test_meta_returns_stored_metadata(self, client, data_dir):
        meta = {"serverId": "eu-de", "vpnPort": 35825}
        meta_path = os.path.join(data_dir, app_module.META_FILE)
        with open(meta_path, 'w') as f:
            json.dump(meta, f)

        res = client.get('/api/local/meta')
        assert res.status_code == 200
        data = json.loads(res.data)
        assert data['serverId'] == 'eu-de'
        assert data['vpnPort'] == 35825

    def test_meta_drops_sensitive_secrets(self, client, data_dir):
        meta = {
            "serverId": "eu-de",
            "presharedKey": "SuperSecretXYZ",
            "paymentHash": "hash12345"
        }
        meta_path = os.path.join(data_dir, app_module.META_FILE)
        with open(meta_path, 'w') as f:
            json.dump(meta, f)

        res = client.get('/api/local/meta')
        assert res.status_code == 200
        data = json.loads(res.data)
        assert data['serverId'] == 'eu-de'
        assert 'presharedKey' not in data
        assert 'paymentHash' not in data

# --- Phase 2: Renew Endpoint Test ---

class TestRenewEndpoint:
    """Test that /api/subscription/renew autofills missing data from metadata."""

    @patch('app.requests.post')
    def test_renew_autofills_missing_fields_from_metadata(self, mock_post, client, data_dir):
        # Create metadata
        meta = {"serverId": "au-syd", "wgPublicKey": "pubkey123"}
        meta_path = os.path.join(data_dir, app_module.META_FILE)
        with open(meta_path, 'w') as f:
            json.dump(meta, f)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b'{"success": true}'
        mock_resp.headers = {'Content-Type': 'application/json'}
        mock_post.return_value = mock_resp

        # Send renew request with duration only, missing serverId and wgPublicKey
        res = client.post('/api/subscription/renew', json={'duration': 3})
        assert res.status_code == 200
        
        # Verify proxy_request was called with the autofilled payload
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args.kwargs
        assert call_kwargs['json']['duration'] == 3
        assert call_kwargs['json']['wgPublicKey'] == 'pubkey123'

    def test_renew_rejects_external_ip(self):
        # We need to manually invoke the proxy fix app structure to test the before_request
        from app import app
        with app.test_client() as client:
            res = client.post(
                '/api/subscription/renew',
                json={'duration': 3},
                environ_base={'REMOTE_ADDR': '203.0.113.1'} # External IP
            )
            assert res.status_code == 403

    @patch('app.requests.post')
    def test_renew_does_not_override_provided_fields(self, mock_post, client, data_dir):
        meta = {"serverId": "au-syd", "wgPublicKey": "oldkey123"}
        meta_path = os.path.join(data_dir, app_module.META_FILE)
        with open(meta_path, 'w') as f:
            json.dump(meta, f)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b'{"success": true}'
        mock_post.return_value = mock_resp

        # Send renew request with explicit explicit data
        res = client.post('/api/subscription/renew', json={'duration': 1, 'serverId': 'new-server', 'wgPublicKey': 'newkey'})
        assert res.status_code == 200
        
        # Should use provided data, not autofilled from meta
        call_kwargs = mock_post.call_args.kwargs
        assert call_kwargs['json']['serverId'] == 'new-server'
        assert call_kwargs['json']['wgPublicKey'] == 'newkey'

    @patch('app.requests.post')
    def test_renew_handles_non_object_json_body(self, mock_post, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b'{"success": true}'
        mock_resp.headers = {'Content-Type': 'application/json'}
        mock_post.return_value = mock_resp

        res = client.post('/api/subscription/renew', json=['invalid'])
        assert res.status_code == 200

        call_kwargs = mock_post.call_args.kwargs
        assert call_kwargs['json'] == {}


class TestDataplaneAndRegressionFixes:
    def test_proxyfix_blocks_forwarded_public_ip(self, client):
        res = client.get('/api/local/status', environ_base={
            'REMOTE_ADDR': '127.0.0.1',
            'HTTP_X_FORWARDED_FOR': '8.8.8.8'
        })
        assert res.status_code == 403

    def test_direct_client_cannot_spoof_forwarded_private_ip(self, client):
        # Direct non-loopback caller should be validated against the direct peer IP, not spoofed X-Forwarded-For.
        res = client.get('/api/local/status', environ_base={
            'REMOTE_ADDR': '203.0.113.9',
            'HTTP_X_FORWARDED_FOR': '10.0.0.2'
        })
        assert res.status_code == 403

    def test_local_api_allows_ipv6_loopback(self, client):
        res = client.get('/api/local/status', environ_base={
            'REMOTE_ADDR': '::1'
        })
        assert res.status_code == 200

    def test_local_api_allows_ipv6_ula(self, client):
        res = client.get('/api/local/status', environ_base={
            'REMOTE_ADDR': 'fd00::1'
        })
        assert res.status_code == 200

    def test_local_api_rejects_ipv6_link_local(self, client):
        res = client.get('/api/local/status', environ_base={
            'REMOTE_ADDR': 'fe80::1'
        })
        assert res.status_code == 403

    def test_local_api_allows_ipv4_mapped_private_address(self, client):
        res = client.get('/api/local/status', environ_base={
            'REMOTE_ADDR': '::ffff:192.168.1.50'
        })
        assert res.status_code == 200

    def test_local_api_allows_tailscale_cgnat_address(self, client):
        for ip in ['100.64.0.1', '100.117.194.79', '100.127.255.254']:
            res = client.get('/api/local/status', environ_base={
                'REMOTE_ADDR': ip
            })
            assert res.status_code == 200, f"Expected 200 for Tailscale CGNAT IP {ip}"

    def test_local_api_allows_ipv4_mapped_tailscale_address(self, client):
        res = client.get('/api/local/status', environ_base={
            'REMOTE_ADDR': '::ffff:100.64.1.2'
        })
        assert res.status_code == 200

    def test_local_api_rejects_non_cgnat_100_address(self, client):
        for ip in ['100.63.255.255', '100.128.0.1']:
            res = client.get('/api/local/status', environ_base={
                'REMOTE_ADDR': ip
            })
            assert res.status_code == 403, f"Expected 403 for non-CGNAT 100.x IP {ip}"

    @patch('app.requests.post', side_effect=requests.RequestException("No network in tests"))
    @patch('app.subprocess.run')
    def test_upload_config_saves_tunnelsats_conf_and_meta(self, mock_run, mock_post, client, data_dir):
        old_conf = data_dir / 'tunnelsats-old.conf'
        old_conf.write_text('[Interface]\nPrivateKey=old\n')
        target_conf = data_dir / 'tunnelsats.conf'
        target_conf.write_text('[Interface]\nPrivateKey=old-current\n')

        mock_proc = MagicMock()
        mock_proc.stdout = 'derivedPubKeyBase64==\n'
        mock_proc.returncode = 0
        mock_run.return_value = mock_proc

        config_text = (
            "# Port Forwarding: 35825\n"
            "# Valid Until: 2030-04-05T10:30:00.000Z\n"
            "[Interface]\n"
            "PrivateKey = clientPrivateKeyBase64==\n"
            "\n"
            "[Peer]\n"
            "PublicKey = serverPublicKeyBase64==\n"
            "AllowedIPs = 0.0.0.0/0\n"
            "Endpoint = de2.tunnelsats.com:51820\n"
        )
        expected_saved_config = (
            "# Port Forwarding: 35825\n"
            "# Valid Until: 2030-04-05T10:30:00.000Z\n"
            "\n"
            "[Interface]\n"
            "PrivateKey = clientPrivateKeyBase64==\n"
            "\n"
            "[Peer]\n"
            "PublicKey = serverPublicKeyBase64==\n"
            "AllowedIPs = 0.0.0.0/0\n"
            "Endpoint = de2.tunnelsats.com:51820\n"
            "PersistentKeepalive = 25\n"
        )

        res = client.post('/api/local/upload-config', json={"config": config_text})
        assert res.status_code == 200
        payload = json.loads(res.data)
        assert payload["success"] is True
        assert payload["message"] == "Configuration saved and parsed."
        assert payload["meta"]["serverId"] == "de2"
        assert payload["meta"]["wgPublicKey"] == "derivedPubKeyBase64=="
        assert payload["meta"]["expiresAt"] == "2030-04-05T10:30:00.000Z"
        assert payload["meta"]["vpnPort"] == 35825

        assert os.path.exists(str(old_conf) + '.bak')
        assert not os.path.exists(old_conf)
        assert os.path.exists(str(target_conf) + '.bak')
        assert target_conf.read_text() == expected_saved_config

        meta_path = data_dir / app_module.META_FILE
        with open(meta_path, 'r') as fp:
            meta = json.load(fp)
        assert meta["serverId"] == "de2"
        assert meta["wgPublicKey"] == "derivedPubKeyBase64=="
        assert meta["expiresAt"] == "2030-04-05T10:30:00.000Z"
        assert meta["vpnPort"] == 35825

        mock_run.assert_called_once_with(
            ["wg", "pubkey"],
            input="clientPrivateKeyBase64==",
            text=True,
            capture_output=True,
            check=True,
            timeout=5,
        )

    @patch('app.requests.post')
    @patch('app.subprocess.run')
    def test_upload_config_authoritative_sync_expired_blocks_persistence(self, mock_run, mock_post, client, data_dir):
        """If the API says it is expired, persistence should be blocked unless confirm=true."""
        # 1. API says EXPIRED
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "expiry": "2023-01-01T00:00:00Z", # Past
            "status": "disabled",
            "server_domain": "de2.tunnelsats.com"
        }
        mock_post.return_value = mock_resp

        mock_proc = MagicMock()
        mock_proc.stdout = 'derivedPubKeyBase64==\n'
        mock_proc.returncode = 0
        mock_run.return_value = mock_proc

        config_text = (
            "# Valid Until: 2023-01-01T00:00:00Z\n"
            "[Interface]\nPrivateKey = clientPrivateKeyBase64==\n"
            "[Peer]\nPublicKey = server==\nEndpoint = de2.tunnelsats.com:51820\n"
        )

        # 2. Upload WITHOUT confirm
        res = client.post('/api/local/upload-config', json={"config": config_text})
        assert res.status_code == 200 # We return 200 but with is_expired=True and no persistence message
        payload = json.loads(res.data)
        assert payload["is_expired"] is True
        assert "Configuration saved" not in payload.get("message", "")
        
        # Verify NO file was written
        assert not os.path.exists(data_dir / "tunnelsats.conf")

        # 3. Upload WITH confirm
        res = client.post('/api/local/upload-config', json={"config": config_text, "confirm": True})
        assert res.status_code == 200
        payload = json.loads(res.data)
        assert payload["success"] is True
        assert "Configuration saved" in payload["message"]
        
        # Verify file WAS written
        assert os.path.exists(data_dir / "tunnelsats.conf")
        assert (data_dir / app_module.META_FILE).exists()
        with open(data_dir / app_module.META_FILE) as f:
            meta = json.load(f)
            assert meta["expiresAt"] == "2023-01-01T00:00:00Z"

    @patch('app.requests.post')
    @patch('app.subprocess.run')
    def test_upload_config_authoritative_sync_active_persists_immediately(self, mock_run, mock_post, client, data_dir):
        """If the API says it is ACTIVE, it should persist immediately even without confirm=true."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "expiry": "2100-01-01T00:00:00Z", # Future
            "status": "enabled",
            "server_domain": "au1.tunnelsats.com"
        }
        mock_post.return_value = mock_resp

        mock_proc = MagicMock()
        mock_proc.stdout = 'derivedPubKeyBase64==\n'
        mock_proc.returncode = 0
        mock_run.return_value = mock_proc

        config_text = (
            "[Interface]\nPrivateKey = clientPrivateKeyBase64==\n"
            "[Peer]\nPublicKey = server==\nEndpoint = au1.tunnelsats.com:51820\n"
        )

        res = client.post('/api/local/upload-config', json={"config": config_text})
        assert res.status_code == 200
        payload = json.loads(res.data)
        assert payload["success"] is True
        assert payload["is_expired"] is False
        assert os.path.exists(data_dir / "tunnelsats.conf")

    @patch('app.requests.post')
    @patch('app.subprocess.run')
    def test_upload_config_expired_requires_literal_true_confirmation(self, mock_run, mock_post, client, data_dir):
        """Only JSON boolean true should bypass expired warning persistence guard."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "expiry": "2023-01-01T00:00:00Z",
            "status": "disabled",
            "server_domain": "de2.tunnelsats.com"
        }
        mock_post.return_value = mock_resp

        mock_proc = MagicMock()
        mock_proc.stdout = 'derivedPubKeyBase64==\n'
        mock_proc.returncode = 0
        mock_run.return_value = mock_proc

        config_text = (
            "[Interface]\nPrivateKey = clientPrivateKeyBase64==\n"
            "[Peer]\nPublicKey = server==\nEndpoint = de2.tunnelsats.com:51820\n"
        )

        res = client.post('/api/local/upload-config', json={"config": config_text, "confirm": "false"})
        assert res.status_code == 200
        payload = json.loads(res.data)
        assert payload["success"] is True
        assert payload["warning"] == "Expired"
        assert payload["is_expired"] is True
        assert not os.path.exists(data_dir / "tunnelsats.conf")

    @patch('app.requests.post')
    @patch('app.subprocess.run')
    def test_upload_config_ignores_cached_expired_state_and_refetches(self, mock_run, mock_post, client, data_dir):
        """Cached expired/disabled entries should not gate a fresh authoritative re-check."""
        app_module._SUBSCRIPTION_CACHE["derivedPubKeyBase64=="] = (
            time.time(),
            {
                "expiry": "2023-01-01T00:00:00Z",
                "status": "disabled",
                "server_domain": "de2.tunnelsats.com",
            },
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "expiry": "2100-01-01T00:00:00Z",
            "status": "enabled",
            "server_domain": "au1.tunnelsats.com"
        }
        mock_post.return_value = mock_resp

        mock_proc = MagicMock()
        mock_proc.stdout = 'derivedPubKeyBase64==\n'
        mock_proc.returncode = 0
        mock_run.return_value = mock_proc

        config_text = (
            "[Interface]\nPrivateKey = clientPrivateKeyBase64==\n"
            "[Peer]\nPublicKey = server==\nEndpoint = de2.tunnelsats.com:51820\n"
        )

        res = client.post('/api/local/upload-config', json={"config": config_text})
        assert res.status_code == 200
        payload = json.loads(res.data)
        assert payload["success"] is True
        assert payload["is_expired"] is False
        assert payload["meta"]["expiresAt"] == "2100-01-01T00:00:00Z"
        assert os.path.exists(data_dir / "tunnelsats.conf")
        assert mock_post.call_count == 1

    @patch('app.requests.post', side_effect=requests.RequestException("No network in tests"))
    @patch('app.subprocess.run')
    def test_upload_config_does_not_duplicate_existing_keepalive(self, mock_run, mock_post, client, data_dir):
        mock_proc = MagicMock()
        mock_proc.stdout = 'derivedPubKeyBase64==\n'
        mock_proc.returncode = 0
        mock_run.return_value = mock_proc

        config_text = (
            "[Interface]\n"
            "PrivateKey = clientPrivateKeyBase64==\n"
            "\n"
            "[Peer]\n"
            "PublicKey = serverPublicKeyBase64==\n"
            "Endpoint = de2.tunnelsats.com:51820\n"
            "PersistentKeepalive = 25\n"
        )

        res = client.post('/api/local/upload-config', json={"config": config_text})
        assert res.status_code == 200

        saved = (data_dir / 'tunnelsats.conf').read_text()
        assert saved.count("PersistentKeepalive = 25") == 1

    @patch('app.requests.post', side_effect=requests.RequestException("No network in tests"))
    @patch('app.subprocess.run')
    def test_upload_config_accepts_semicolon_comments(self, mock_run, mock_post, client, data_dir):
        mock_proc = MagicMock()
        mock_proc.stdout = 'derivedPubKeyBase64==\n'
        mock_proc.returncode = 0
        mock_run.return_value = mock_proc

        config_text = (
            "; TunnelSats WireGuard Configuration\n"
            "# Port Forwarding: 35825\n"
            "[Interface]\n"
            "PrivateKey = clientPrivateKeyBase64==\n"
            "\n"
            "; Peer settings\n"
            "[Peer]\n"
            "PublicKey = serverPublicKeyBase64==\n"
            "Endpoint = de2.tunnelsats.com:51820\n"
        )

        res = client.post('/api/local/upload-config', json={"config": config_text})
        assert res.status_code == 200
        saved = (data_dir / 'tunnelsats.conf').read_text()
        assert "; TunnelSats WireGuard Configuration" in saved
        assert "; Peer settings" in saved

    @pytest.mark.parametrize("directive", ["PreUp", "PostUp", "PreDown", "PostDown"])
    @patch('app.subprocess.run')
    def test_upload_config_rejects_wireguard_exec_hooks_before_deriving_key(self, mock_run, directive, client, data_dir):
        config_text = (
            "[Interface]\n"
            "PrivateKey = clientPrivateKeyBase64==\n"
            f"{directive} = touch /tmp/pwned\n"
            "\n"
            "[Peer]\n"
            "PublicKey = serverPublicKeyBase64==\n"
            "AllowedIPs = 0.0.0.0/0\n"
            "Endpoint = de2.tunnelsats.com:51820\n"
        )

        res = client.post('/api/local/upload-config', json={"config": config_text})
        assert res.status_code == 400
        payload = json.loads(res.data)
        assert payload["success"] is False
        assert payload["error"] == f"Unsafe WireGuard directive {directive} is not allowed."
        assert not os.path.exists(data_dir / "tunnelsats.conf")
        mock_run.assert_not_called()

    @patch('app.subprocess.run')
    def test_upload_config_rejects_unsupported_wireguard_section_before_deriving_key(self, mock_run, client, data_dir):
        config_text = (
            "[Interface]\n"
            "PrivateKey = clientPrivateKeyBase64==\n"
            "\n"
            "[Peer]\n"
            "PublicKey = serverPublicKeyBase64==\n"
            "Endpoint = de2.tunnelsats.com:51820\n"
            "\n"
            "[Script]\n"
            "Command = touch /tmp/pwned\n"
        )

        res = client.post('/api/local/upload-config', json={"config": config_text})
        assert res.status_code == 400
        payload = json.loads(res.data)
        assert payload["success"] is False
        assert payload["error"] == "Unsupported WireGuard section [Script]."
        assert not os.path.exists(data_dir / "tunnelsats.conf")
        mock_run.assert_not_called()

    @patch('app.subprocess.run')
    def test_upload_config_rejects_unsupported_wireguard_directive_before_deriving_key(self, mock_run, client, data_dir):
        config_text = (
            "[Interface]\n"
            "PrivateKey = clientPrivateKeyBase64==\n"
            "Table = auto\n"
            "\n"
            "[Peer]\n"
            "PublicKey = serverPublicKeyBase64==\n"
            "Endpoint = de2.tunnelsats.com:51820\n"
        )

        res = client.post('/api/local/upload-config', json={"config": config_text})
        assert res.status_code == 400
        payload = json.loads(res.data)
        assert payload["success"] is False
        assert payload["error"] == "Unsupported WireGuard directive Table in [Interface]."
        assert not os.path.exists(data_dir / "tunnelsats.conf")
        mock_run.assert_not_called()

    def test_upload_config_rejects_missing_required_blocks(self, client):
        config_text = "[Interface]\nPrivateKey = clientPrivateKeyBase64==\n"
        res = client.post('/api/local/upload-config', json={"config": config_text})
        assert res.status_code == 400
        payload = json.loads(res.data)
        assert payload["success"] is False
        assert payload["error"] == "Invalid WireGuard configuration format. Missing [Interface] or [Peer] block."

    def test_upload_config_rejects_missing_private_key(self, client):
        config_text = "[Interface]\nAddress = 10.8.0.42/32\n\n[Peer]\nPublicKey = server==\n"
        res = client.post('/api/local/upload-config', json={"config": config_text})
        assert res.status_code == 400
        payload = json.loads(res.data)
        assert payload["success"] is False
        assert payload["error"] == "Invalid WireGuard configuration format. Missing Interface PrivateKey."

    @patch('app.subprocess.run')
    def test_upload_config_rejects_overly_long_private_key_without_spawning_wg(self, mock_run, client):
        long_key = "A" * 2048
        config_text = (
            "[Interface]\n"
            f"PrivateKey = {long_key}\n"
            "\n"
            "[Peer]\n"
            "PublicKey = serverPublicKeyBase64==\n"
            "Endpoint = de2.tunnelsats.com:51820\n"
        )

        res = client.post('/api/local/upload-config', json={"config": config_text})
        assert res.status_code == 400
        payload = json.loads(res.data)
        assert payload["success"] is False
        assert payload["error"] == "Unable to derive public key from provided PrivateKey."
        mock_run.assert_not_called()

    def test_local_status_includes_manifest_version_and_dataplane_defaults(self, client):
        with tempfile.TemporaryDirectory() as tmp_dir:
            manifest_path = os.path.join(tmp_dir, 'umbrel-app.yml')
            with open(manifest_path, 'w') as f:
                f.write('version: "9.1.2"\n')

            with patch('app.APP_MANIFEST_PATH', manifest_path):
                with patch('app.STATE_FILE', os.path.join(tmp_dir, 'missing-state.json')):
                    res = client.get('/api/local/status')

        assert res.status_code == 200
        data = json.loads(res.data)
        assert data['version'] == 'v9.1.2'
        assert data['dataplane_mode'] == 'docker-full-parity'
        assert data['docker_network']['name'] == 'docker-tunnelsats'
        assert data['k3s_bypass_cidrs'] == []
        assert data['rules_synced'] is False
        assert data['ipv4_rules_synced'] is False
        assert data['ipv6_rules_synced'] is False
        assert data['ipv6_policy'] == 'deny'
        assert data['target_ipv6_addresses'] == []
        assert data['target_ipv6_default_route'] is False
        assert data['last_error'] is None

    @patch('app.docker_api')
    def test_status_queries_only_running_containers_for_ips(self, mock_docker_api, client):
        mock_docker_api.return_value = []
        res = client.get('/api/local/status')
        assert res.status_code == 200
        assert mock_docker_api.call_args_list[0].args[0] == '/containers/json?all=0'

    def test_reconcile_endpoint_creates_trigger_and_status_transitions(self, client):
        with tempfile.TemporaryDirectory() as tmp_dir:
            trigger_dir = os.path.join(tmp_dir, 'triggers')
            result_dir = os.path.join(tmp_dir, 'results')
            legacy_result = os.path.join(tmp_dir, 'legacy.json')
            with patch('app.RECONCILE_TRIGGER_DIR', trigger_dir):
                with patch('app.RECONCILE_RESULT_DIR', result_dir):
                    with patch('app.RECONCILE_RESULT_LEGACY', legacy_result):
                        trigger_res = client.post('/api/local/reconcile')
                        assert trigger_res.status_code == 202
                        trigger_payload = json.loads(trigger_res.data)
                        request_id = trigger_payload['request_id']
                        assert trigger_payload['accepted'] is True
                        assert request_id

                        trigger_path = os.path.join(trigger_dir, f'{request_id}.trigger')
                        assert os.path.exists(trigger_path)

                        pending = client.get(f'/api/local/reconcile/{request_id}')
                        assert pending.status_code == 202
                        pending_payload = json.loads(pending.data)
                        assert pending_payload['complete'] is False

                        os.makedirs(result_dir, exist_ok=True)
                        with open(os.path.join(result_dir, f'{request_id}.json'), 'w') as f:
                            json.dump({'request_id': request_id, 'changed': True, 'state': {'rules_synced': True}}, f)

                        complete = client.get(f'/api/local/reconcile/{request_id}')
                        assert complete.status_code == 200
                        complete_payload = json.loads(complete.data)
                        assert complete_payload['complete'] is True
                        assert complete_payload['success'] is True
                        assert complete_payload['changed'] is True

    def test_reconcile_status_reports_failure_when_rules_unsynced(self, client):
        with tempfile.TemporaryDirectory() as tmp_dir:
            result_dir = os.path.join(tmp_dir, 'results')
            os.makedirs(result_dir, exist_ok=True)
            request_id = 'req-unsynced-1'
            with open(os.path.join(result_dir, f'{request_id}.json'), 'w') as f:
                json.dump({'request_id': request_id, 'changed': False, 'state': {'rules_synced': False}}, f)

            with patch('app.RECONCILE_RESULT_DIR', result_dir):
                with patch('app.RECONCILE_RESULT_LEGACY', os.path.join(tmp_dir, 'legacy.json')):
                    res = client.get(f'/api/local/reconcile/{request_id}')

        assert res.status_code == 200
        payload = json.loads(res.data)
        assert payload['complete'] is True
        assert payload['success'] is False

    @patch('app.container_ids_by_match', return_value=['mock'])
    def test_configure_node_lnd_injects_externalhosts_from_metadata(self, mock_ids, client):
        with tempfile.TemporaryDirectory() as tmp_dir:
            meta_path = os.path.join(tmp_dir, app_module.META_FILE)
            lnd_path = os.path.join(tmp_dir, 'tunnelsats.conf')

            with open(meta_path, 'w') as f:
                json.dump({'vpnPort': 35825, 'serverDomain': 'de2.tunnelsats.com'}, f)

            with open(lnd_path, 'w') as f:
                f.write('[Application Options]\nfoo=bar\n')

            with patch('app.DATA_DIR', tmp_dir):
                with patch('app.LND_CONFIG_PATH', lnd_path):
                    with patch('app.restart_container_by_pattern', return_value=True) as mock_restart:
                        res = client.post('/api/local/configure-node', json={'nodeType': 'lnd'})

            assert res.status_code == 200
            payload = json.loads(res.data)
            assert payload['success'] is True
            assert payload['lnd'] is True
            assert payload['cln'] is False
            assert payload['port'] == 35825
            assert payload['dns'] == 'de2.tunnelsats.com'
            mock_restart.assert_not_called()
            with open(lnd_path, 'r') as f:
                lnd_content = f.read()
            assert 'externalhosts=de2.tunnelsats.com:35825' in lnd_content

    @patch('app.container_ids_by_match', return_value=[])
    def test_configure_node_returns_error_when_container_not_found(self, mock_ids, client):
        """Verifies P1 feedback: configure_node should return success=False when container is missing."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            meta_path = os.path.join(tmp_dir, app_module.META_FILE)
            with open(meta_path, 'w') as f:
                json.dump({'vpnPort': 35825, 'serverDomain': 'de2.tunnelsats.com'}, f)

            with patch('app.DATA_DIR', tmp_dir):
                # Test LND
                res = client.post('/api/local/configure-node', json={'nodeType': 'lnd'})
                assert res.status_code == 422
                payload = json.loads(res.data)
                assert payload['success'] is False
                assert 'LND container not found' in payload['error']

                # Test CLN
                res = client.post('/api/local/configure-node', json={'nodeType': 'cln'})
                assert res.status_code == 422
                payload = json.loads(res.data)
                assert payload['success'] is False
                assert 'CLN container not found' in payload['error']

    @patch('app.docker_api')
    @patch('app.docker_api_post')
    def test_restart_container_by_pattern_restarts_all_matches_general(self, mock_post, mock_docker_api, client):
        mock_docker_api.return_value = [
            {"Id": "id1", "Names": ["/some_service_1"]},
            {"Id": "id2", "Names": ["/some_service_2"]},
            {"Id": "id3", "Names": ["/other_container"]}
        ]
        mock_post.return_value = True
        
        from app import restart_container_by_pattern
        result = restart_container_by_pattern(r"(^|[_-])some_service([_-]|$)")
        
        assert result is True
        assert mock_post.call_count == 2
        mock_post.assert_any_call("/containers/id1/restart")
        mock_post.assert_any_call("/containers/id2/restart")

    @patch('app.container_id_by_match')
    @patch('app.docker_api_post')
    @patch('app.time.sleep')
    @patch('app.app.logger')
    def test_restart_container_by_pattern_sequential_lnd_sequence(self, mock_logger, mock_sleep, mock_post, mock_id, client):
        # Mock IDs for middleware and daemon
        def side_effect(pattern):
            if pattern == app_module.LND_MIDDLEWARE_PATTERN:
                return "middleware_id_long_identifier"
            if pattern == app_module.LND_CONTAINER_PATTERN:
                return "daemon_id_long_identifier"
            return ""
        mock_id.side_effect = side_effect
        mock_post.return_value = True

        from app import restart_container_by_pattern, LND_RESTART_DELAY
        result = restart_container_by_pattern(r"(^|[_-])lnd([_-]|$)", is_lnd=True)

        assert result is True
        # Assert calls in order
        assert mock_post.call_count == 2
        mock_post.assert_any_call("/containers/middleware_id_long_identifier/restart")
        mock_post.assert_any_call("/containers/daemon_id_long_identifier/restart")
        
        # Verify middleware was restarted FIRST
        first_call = mock_post.call_args_list[0]
        assert first_call.args[0] == "/containers/middleware_id_long_identifier/restart"
        
        # Verify sleep was called between them
        mock_sleep.assert_called_once_with(LND_RESTART_DELAY)
        
        # Verify daemon was restarted LAST
        second_call = mock_post.call_args_list[1]
        assert second_call.args[0] == "/containers/daemon_id_long_identifier/restart"

        mock_logger.info.assert_any_call("Found LND middleware container (ID: middleware_i). Restarting...")
        mock_logger.info.assert_any_call("Found LND daemon container (ID: daemon_id_lo). Restarting...")

    @patch('app.container_id_by_match')
    @patch('app.docker_api_post')
    @patch('app.app.logger')
    def test_restart_container_by_pattern_sequential_middleware_failure(self, mock_logger, mock_post, mock_id, client):
        mock_id.return_value = "middleware_id"
        mock_post.return_value = False

        from app import restart_container_by_pattern
        result = restart_container_by_pattern(r"(^|[_-])lnd([_-]|$)", is_lnd=True)

        assert result is False
        mock_logger.error.assert_called_with("LND middleware restart failed. Aborting restart sequence.")
        mock_post.assert_called_once_with("/containers/middleware_id/restart")
        mock_id.assert_called_once_with(app_module.LND_MIDDLEWARE_PATTERN)

    @pytest.mark.parametrize(
        "name",
        [
            "lnd",
            "lnd_1",
            "lnd-1",
            "lightning_lnd",
            "lightning-lnd-1",
            "umbrel_lightning_lnd_1",
        ],
    )
    def test_lnd_container_pattern_matches_daemon_names(self, name):
        assert app_module.re.search(app_module.LND_CONTAINER_PATTERN, name)

    @pytest.mark.parametrize(
        "name",
        [
            "lnd_app_1",
            "lnd-proxy-1",
            "lnd-rest-1",
            "foo-lnd-backup-1",
            "lightning_app_1",
        ],
    )
    def test_lnd_container_pattern_rejects_helper_names(self, name):
        assert not app_module.re.search(app_module.LND_CONTAINER_PATTERN, name)

    @pytest.mark.parametrize(
        "name",
        [
            "lightning_app_1",
            "lightning-app-1",
            "umbrel_lightning_app_1",
            "lnd_app_1",
            "lightning_ui_1",
        ],
    )
    def test_lnd_middleware_pattern_matches_middleware_names(self, name):
        assert app_module.re.search(app_module.LND_MIDDLEWARE_PATTERN, name)

    @pytest.mark.parametrize(
        "name",
        [
            "lightning_app_proxy_1",
            "umbrel-lightning-app-proxy-1",
            "lnd_app_proxy_1",
            "lightning_ui_proxy_1",
            "lightning_app_backup_1",
        ],
    )
    def test_lnd_middleware_pattern_rejects_helper_names(self, name):
        assert not app_module.re.search(app_module.LND_MIDDLEWARE_PATTERN, name)

    @patch('app.container_ids_by_match', return_value=['mock'])
    def test_configure_node_lnd_creates_application_options_section_when_missing(self, mock_ids, client):
        with tempfile.TemporaryDirectory() as tmp_dir:
            meta_path = os.path.join(tmp_dir, app_module.META_FILE)
            lnd_path = os.path.join(tmp_dir, 'tunnelsats.conf')

            with open(meta_path, 'w') as f:
                json.dump({'vpnPort': 35825, 'serverDomain': 'de2.tunnelsats.com'}, f)

            with open(lnd_path, 'w') as f:
                f.write('foo=bar\n')

            with patch('app.DATA_DIR', tmp_dir):
                with patch('app.LND_CONFIG_PATH', lnd_path):
                    with patch('app.restart_container_by_pattern', return_value=True):
                        res = client.post('/api/local/configure-node', json={'nodeType': 'lnd'})

            assert res.status_code == 200
            payload = json.loads(res.data)
            assert payload['success'] is True
            assert payload['lnd'] is True

            with open(lnd_path, 'r') as f:
                lnd_content = f.read()

            section_idx = lnd_content.find('[Application Options]\n')
            host_idx = lnd_content.find('externalhosts=de2.tunnelsats.com:35825\n')
            assert section_idx != -1
            assert host_idx != -1
            assert section_idx < host_idx

    @patch('app.container_ids_by_match', return_value=['mock'])
    def test_configure_node_lnd_creates_config_file_when_missing(self, mock_ids, client):
        with tempfile.TemporaryDirectory() as tmp_dir:
            meta_path = os.path.join(tmp_dir, app_module.META_FILE)
            lnd_path = os.path.join(tmp_dir, 'tunnelsats.conf')

            with open(meta_path, 'w') as f:
                json.dump({'vpnPort': 35825, 'serverDomain': 'de2.tunnelsats.com'}, f)

            with patch('app.DATA_DIR', tmp_dir):
                with patch('app.LND_CONFIG_PATH', lnd_path):
                    with patch('app.restart_container_by_pattern', return_value=True) as mock_restart:
                        res = client.post('/api/local/configure-node', json={'nodeType': 'lnd'})

            assert res.status_code == 200
            payload = json.loads(res.data)
            assert payload['success'] is True
            assert payload['lnd'] is True
            assert os.path.exists(lnd_path)
            mock_restart.assert_not_called()

            with open(lnd_path, 'r') as f:
                lnd_content = f.read()
            assert '[Application Options]\n' in lnd_content
            assert 'externalhosts=de2.tunnelsats.com:35825\n' in lnd_content

    @patch('app.container_ids_by_match', return_value=['mock'])
    def test_configure_node_cln_injects_expected_lines_from_metadata(self, mock_ids, client):
        with tempfile.TemporaryDirectory() as tmp_dir:
            meta_path = os.path.join(tmp_dir, app_module.META_FILE)
            cln_path = os.path.join(tmp_dir, 'config')

            with open(meta_path, 'w') as f:
                json.dump({'vpnPort': 35825, 'serverDomain': 'de2.tunnelsats.com'}, f)

            with open(cln_path, 'w') as f:
                f.write('foo=bar\n')

            with patch('app.DATA_DIR', tmp_dir):
                with patch('app.CLN_CONFIG_PATH', cln_path):
                    with patch('app.restart_container_by_pattern', return_value=True) as mock_restart:
                        res = client.post('/api/local/configure-node', json={
                            'nodeType': 'cln',
                            'confirmAddressChanges': True,
                        })

            assert res.status_code == 200
            payload = json.loads(res.data)
            assert payload['success'] is True
            assert payload['lnd'] is False
            assert payload['cln'] is True
            assert payload['port'] == 35825
            assert payload['dns'] == 'de2.tunnelsats.com'
            mock_restart.assert_called_once_with(app_module.CLN_CONTAINER_PATTERN)

            with open(cln_path, 'r') as f:
                cln_content = f.read()
            assert 'bind-addr=0.0.0.0:9736' in cln_content
            assert 'announce-addr=de2.tunnelsats.com:35825' in cln_content
            assert 'always-use-proxy=false' in cln_content

    @patch('app.container_ids_by_match', return_value=['mock'])
    def test_configure_node_cln_dedupes_commented_and_active_lines(self, mock_ids, client):
        with tempfile.TemporaryDirectory() as tmp_dir:
            meta_path = os.path.join(tmp_dir, app_module.META_FILE)
            cln_path = os.path.join(tmp_dir, 'config')

            with open(meta_path, 'w') as f:
                json.dump({'vpnPort': 35825, 'serverDomain': 'de2.tunnelsats.com'}, f)

            with open(cln_path, 'w') as f:
                f.write(
                    '# announce-addr=old.tunnelsats.com:1111\n'
                    'announce-addr=old.tunnelsats.com:2222\n'
                    '# always-use-proxy=true\n'
                    'always-use-proxy=true\n'
                )

            with patch('app.DATA_DIR', tmp_dir):
                with patch('app.CLN_CONFIG_PATH', cln_path):
                    with patch('app.restart_container_by_pattern', return_value=True):
                        res = client.post('/api/local/configure-node', json={
                            'nodeType': 'cln',
                            'confirmAddressChanges': True,
                        })

            assert res.status_code == 200
            with open(cln_path, 'r') as f:
                cln_content = f.read()

            assert cln_content.count('announce-addr=de2.tunnelsats.com:35825\n') == 1
            assert cln_content.count('always-use-proxy=false\n') == 1
            assert cln_content.count('bind-addr=0.0.0.0:9736\n') == 1
            assert '\nannounce-addr=old.tunnelsats.com' not in cln_content
            assert '# TunnelSats disabled: announce-addr=old.tunnelsats.com:2222' in cln_content

    @patch('app.container_ids_by_match', return_value=['mock'])
    def test_configure_node_cln_leaves_file_unchanged_when_atomic_write_fails(self, mock_ids, client):
        with tempfile.TemporaryDirectory() as tmp_dir:
            meta_path = os.path.join(tmp_dir, app_module.META_FILE)
            cln_path = os.path.join(tmp_dir, 'config')
            original_content = (
                'foo=bar\n'
                'announce-addr=old.tunnelsats.com:1111\n'
                'always-use-proxy=true\n'
            )

            with open(meta_path, 'w') as f:
                json.dump({'vpnPort': 35825, 'serverDomain': 'de2.tunnelsats.com', 'nodeType': 'cln'}, f)
            with open(cln_path, 'w') as f:
                f.write(original_content)

            with patch('app.DATA_DIR', tmp_dir):
                with patch('app.CLN_CONFIG_PATH', cln_path):
                    with patch('app.os.replace', side_effect=OSError('replace failed')):
                        with patch('app.restart_container_by_pattern', return_value=True) as mock_restart:
                            res = client.post('/api/local/configure-node', json={
                                'nodeType': 'cln',
                                'confirmAddressChanges': True,
                            })

            assert res.status_code == 500
            payload = json.loads(res.data)
            assert payload['success'] is False
            assert payload['error'] == 'Failed to back up CLN announcement settings.'
            mock_restart.assert_not_called()

            with open(cln_path, 'r') as f:
                assert f.read() == original_content

    @patch('app.container_ids_by_match', return_value=['mock'])
    def test_configure_node_lnd_does_not_restart_container(self, mock_ids, client):
        with tempfile.TemporaryDirectory() as tmp_dir:
            meta_path = os.path.join(tmp_dir, app_module.META_FILE)
            lnd_path = os.path.join(tmp_dir, 'tunnelsats.conf')

            with open(meta_path, 'w') as f:
                json.dump({'vpnPort': 35825, 'serverDomain': 'de2.tunnelsats.com'}, f)

            with open(lnd_path, 'w') as f:
                f.write('[Application Options]\nexternalhosts=de2.tunnelsats.com:35825\n')

            with patch('app.DATA_DIR', tmp_dir):
                with patch('app.LND_CONFIG_PATH', lnd_path):
                    with patch('app.restart_container_by_pattern', return_value=True) as mock_restart:
                        res = client.post('/api/local/configure-node', json={'nodeType': 'lnd'})

            assert res.status_code == 200
            payload = json.loads(res.data)
            assert payload['success'] is True
            assert payload['lnd'] is True
            assert payload['lnd_changed'] is True
            mock_restart.assert_not_called()

    @patch('app.container_ids_by_match', return_value=['mock'])
    def test_configure_node_cln_returns_500_when_restart_fails(self, mock_ids, client):
        with tempfile.TemporaryDirectory() as tmp_dir:
            meta_path = os.path.join(tmp_dir, app_module.META_FILE)
            cln_path = os.path.join(tmp_dir, 'config')

            with open(meta_path, 'w') as f:
                json.dump({'vpnPort': 35825, 'serverDomain': 'de2.tunnelsats.com'}, f)

            with open(cln_path, 'w') as f:
                f.write('foo=bar\n')

            with patch('app.DATA_DIR', tmp_dir):
                with patch('app.CLN_CONFIG_PATH', cln_path):
                    with patch('app.restart_container_by_pattern', return_value=False):
                        res = client.post('/api/local/configure-node', json={'nodeType': 'cln'})

            assert res.status_code == 500
            payload = json.loads(res.data)
            assert payload['success'] is False
            assert payload['error'] == 'Failed to restart CLN container.'

            with open(meta_path, 'r') as f:
                updated_meta = json.load(f)
            assert updated_meta['clnRestartPending'] is True

    @patch('app.container_ids_by_match', return_value=['mock'])
    def test_restore_node_comments_expected_lines(self, mock_ids, client):
        with tempfile.TemporaryDirectory() as tmp_dir:
            lnd_path = os.path.join(tmp_dir, 'lnd.conf')
            cln_path = os.path.join(tmp_dir, 'config')

            with open(lnd_path, 'w') as f:
                f.write(
                    '[Application Options]\n'
                    'externalhosts=vpn.tunnelsats.com:9735\n'
                    '# externalhosts=already-commented\n'
                    'tor.skip-proxy-for-clearnet-targets=true\n'
                )

            with open(cln_path, 'w') as f:
                f.write(
                    'foo=bar\n'
                    'bind-addr=0.0.0.0:9735\n'
                    'announce-addr=vpn.tunnelsats.com:9735\n'
                    'always-use-proxy=false\n'
                    '# bind-addr=already-commented\n'
                )

            with patch('app.LND_CONFIG_PATH', lnd_path):
                with patch('app.CLN_CONFIG_PATH', cln_path):
                    with patch('app.restart_container_by_pattern', return_value=True):
                        res = client.post('/api/local/restore-node')

            assert res.status_code == 200
            payload = json.loads(res.data)
            assert payload['lnd'] is True
            assert payload['cln'] is True
            assert payload['lnd_changed'] is True
            assert payload['cln_changed'] is True

            with open(lnd_path, 'r') as f:
                lnd_content = f.read()
            assert '# externalhosts=vpn.tunnelsats.com:9735\n' in lnd_content
            assert '# tor.skip-proxy-for-clearnet-targets=true\n' in lnd_content
            assert '# # externalhosts=already-commented' not in lnd_content

            with open(cln_path, 'r') as f:
                cln_content = f.read()
            assert '# bind-addr=0.0.0.0:9735\n' in cln_content
            assert '# announce-addr=vpn.tunnelsats.com:9735\n' in cln_content
            assert '# always-use-proxy=false\n' in cln_content
            assert '# bind-addr=already-commented\n' in cln_content

    @patch('app.container_ids_by_match', return_value=['mock'])
    def test_restore_node_reports_processed_without_changes(self, mock_ids, client):
        with tempfile.TemporaryDirectory() as tmp_dir:
            lnd_path = os.path.join(tmp_dir, 'lnd.conf')
            cln_path = os.path.join(tmp_dir, 'config')

            with open(lnd_path, 'w') as f:
                f.write('[Application Options]\nfoo=bar\n')

            with open(cln_path, 'w') as f:
                f.write('foo=bar\n')

            with patch('app.LND_CONFIG_PATH', lnd_path):
                with patch('app.CLN_CONFIG_PATH', cln_path):
                    with patch('app.restart_container_by_pattern', return_value=True):
                        res = client.post('/api/local/restore-node')

            assert res.status_code == 200
            payload = json.loads(res.data)
            assert payload['lnd'] is True
            assert payload['cln'] is True
            assert payload['lnd_changed'] is False
            assert payload['cln_changed'] is False

    @patch('app.container_ids_by_match', return_value=[])
    def test_restore_node_still_comments_configs_when_containers_are_not_running(self, mock_ids, client):
        with tempfile.TemporaryDirectory() as tmp_dir:
            lnd_path = os.path.join(tmp_dir, 'lnd.conf')
            cln_path = os.path.join(tmp_dir, 'config')

            with open(lnd_path, 'w') as f:
                f.write('externalhosts=de2.tunnelsats.com:35825\n')

            with open(cln_path, 'w') as f:
                f.write('announce-addr=de2.tunnelsats.com:35825\n')

            with patch('app.LND_CONFIG_PATH', lnd_path):
                with patch('app.CLN_CONFIG_PATH', cln_path):
                    with patch('app.restart_container_by_pattern', return_value=True) as mock_restart:
                        res = client.post('/api/local/restore-node')

            assert res.status_code == 200
            payload = json.loads(res.data)
            assert payload['lnd'] is True
            assert payload['cln'] is True
            assert payload['lnd_changed'] is True
            assert payload['cln_changed'] is True
            mock_restart.assert_not_called()

            with open(lnd_path, 'r') as f:
                assert '# externalhosts=de2.tunnelsats.com:35825\n' in f.read()
            with open(cln_path, 'r') as f:
                assert '# announce-addr=de2.tunnelsats.com:35825\n' in f.read()

    def test_restore_node_route_declared_once(self):
        rules = [rule for rule in app_module.app.url_map.iter_rules() if rule.rule == '/api/local/restore-node']
        assert len(rules) == 1

    @patch('app.container_ids_by_match', return_value=['mock'])
    def test_restore_node_forces_restarts(self, mock_ids, client):
        with tempfile.TemporaryDirectory() as tmp_dir:
            lnd_path = os.path.join(tmp_dir, 'tunnelsats.conf')
            cln_path = os.path.join(tmp_dir, 'config')

            with open(lnd_path, 'w') as f:
                f.write('externalhosts=de2.tunnelsats.com:35825\n')
            with open(cln_path, 'w') as f:
                f.write('announce-addr=de2.tunnelsats.com:35825\n')

            with patch('app.DATA_DIR', tmp_dir):
                with patch('app.LND_CONFIG_PATH', lnd_path):
                    with patch('app.CLN_CONFIG_PATH', cln_path):
                        with patch('app.restart_container_by_pattern', return_value=True) as mock_restart:
                            res = client.post('/api/local/restore-node')

            assert res.status_code == 200
            payload = json.loads(res.data)
            assert payload['lnd'] is True
            assert payload['cln'] is True
            # Should have called restart for both LND and CLN
            assert mock_restart.call_count == 2
            mock_restart.assert_any_call(app_module.LND_CONTAINER_PATTERN, is_lnd=True)
            mock_restart.assert_any_call(app_module.CLN_CONTAINER_PATTERN)

    @patch('app.read_dataplane_state')
    @patch('app.docker_api')
    @patch('app.subprocess.run')
    def test_local_status_includes_vpn_internal_ip(self, mock_run, mock_docker_api, mock_read_dataplane, client):
        # Mocking the output of 'ip -4 addr show dev tunnelsatsv2'
        mock_output = """
1875: tunnelsatsv2: <POINTOPOINT,NOARP,UP,LOWER_UP> mtu 1420 qdisc noqueue state UNKNOWN group default qlen 1000
    link/none 
    inet 10.9.0.100/32 scope global tunnelsatsv2
       valid_lft forever preferred_lft forever
"""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = mock_output
        mock_run.return_value = mock_result

        # Mock dependencies called by local_status
        mock_docker_api.return_value = []
        mock_read_dataplane.return_value = {
            "dataplane_mode": "container",
            "target_container": "lnd",
            "target_ip": "172.18.0.2",
            "target_impl": "lnd",
            "docker_network": "umbrel_main_network",
            "forwarding_port": 35825,
            "rules_synced": True,
            "last_reconcile_at": "2026-03-15T12:00:00Z",
            "last_error": None
        }

        res = client.get('/api/local/status')
        assert res.status_code == 200
        data = json.loads(res.data)
        assert data['vpn_internal_ip'] == '10.9.0.100'

        # Verify subprocess was called correctly
        mock_run.assert_called_with(
            ["ip", "-4", "addr", "show", "dev", "tunnelsatsv2"],
            capture_output=True, text=True, timeout=2
        )
        assert mock_docker_api.call_count == 1

    @patch('app.requests.get')
    def test_check_subscription_updates_metadata_on_paid(self, mock_get, client):
        # Case 1: Standard subscription object (e.g. for claim/new buy)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.content = json.dumps({
            "status": "paid",
            "subscription": {
                "expiresAt": "2027-04-10T20:55:39.663Z"
            }
        }).encode('utf-8')
        mock_resp.json.return_value = {
            "status": "paid",
            "subscription": {
                "expiresAt": "2027-04-10T20:55:39.663Z"
            }
        }
        mock_get.return_value = mock_resp

        with tempfile.TemporaryDirectory() as tmp_dir:
            meta_path = os.path.join(tmp_dir, app_module.META_FILE)
            initial_meta = { "expiresAt": "2027-03-10T20:55:39.663Z" }
            with open(meta_path, 'w') as f: json.dump(initial_meta, f)

            with patch('app.DATA_DIR', tmp_dir):
                client.get('/api/subscription/hash1')
                with open(meta_path, 'r') as f:
                    assert json.load(f)['expiresAt'] == "2027-04-10T20:55:39.663Z"

        # Case 2: Renewal format (flat structure with newExpiry)
        mock_resp.content = json.dumps({
            "status": "paid",
            "oldExpiry": "2027-04-10T20:55:39.663Z",
            "newExpiry": "2027-05-10T20:55:39.663Z"
        }).encode('utf-8')
        mock_resp.json.return_value = {
            "status": "paid",
            "oldExpiry": "2027-04-10T20:55:39.663Z",
            "newExpiry": "2027-05-10T20:55:39.663Z"
        }
        
        with tempfile.TemporaryDirectory() as tmp_dir:
            meta_path = os.path.join(tmp_dir, app_module.META_FILE)
            initial_meta = { "expiresAt": "2027-04-10T20:55:39.663Z" }
            with open(meta_path, 'w') as f: json.dump(initial_meta, f)

            with patch('app.DATA_DIR', tmp_dir):
                client.get('/api/subscription/hash2')
                with open(meta_path, 'r') as f:
                    assert json.load(f)['expiresAt'] == "2027-05-10T20:55:39.663Z"

    @patch('app.requests.get')
    def test_check_subscription_preserves_new_expiry_when_subscription_exists(self, mock_get, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.content = json.dumps({
            "status": "paid",
            "subscription": {},
            "newExpiry": "2027-06-10T20:55:39.663Z"
        }).encode('utf-8')
        mock_resp.json.return_value = {
            "status": "paid",
            "subscription": {},
            "newExpiry": "2027-06-10T20:55:39.663Z"
        }
        mock_get.return_value = mock_resp

        with tempfile.TemporaryDirectory() as tmp_dir:
            meta_path = os.path.join(tmp_dir, app_module.META_FILE)
            with open(meta_path, 'w') as f:
                json.dump({"expiresAt": "2027-05-10T20:55:39.663Z"}, f)

            with patch('app.DATA_DIR', tmp_dir):
                res = client.get('/api/subscription/hash3')
                assert res.status_code == 200
                with open(meta_path, 'r') as f:
                    assert json.load(f)['expiresAt'] == "2027-06-10T20:55:39.663Z"

    @patch('app.requests.get')
    @patch('app._update_local_metadata')
    def test_check_subscription_handles_non_object_response(self, mock_update_metadata, mock_get, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.content = b"[]"
        mock_resp.json.return_value = []
        mock_get.return_value = mock_resp

        res = client.get('/api/subscription/hash-edge')

        assert res.status_code == 200
        assert res.data == b"[]"
        mock_update_metadata.assert_not_called()

    @patch('app.requests.get')
    def test_check_subscription_ignores_invalid_metadata_shape(self, mock_get, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.content = json.dumps({
            "status": "paid",
            "subscription": {
                "expiresAt": "2027-04-10T20:55:39.663Z"
            }
        }).encode('utf-8')
        mock_resp.json.return_value = {
            "status": "paid",
            "subscription": {
                "expiresAt": "2027-04-10T20:55:39.663Z"
            }
        }
        mock_get.return_value = mock_resp

        with tempfile.TemporaryDirectory() as tmp_dir:
            meta_path = os.path.join(tmp_dir, app_module.META_FILE)
            with open(meta_path, 'w') as f:
                json.dump([], f)

            with patch('app.DATA_DIR', tmp_dir):
                res = client.get('/api/subscription/hash-invalid-meta')
                assert res.status_code == 200
                with open(meta_path, 'r') as f:
                    assert json.load(f) == []


class TestAnnouncementPrivacy:
    @patch('app.container_ids_by_match', return_value=['mock'])
    def test_lnd_conflicts_require_confirmation_then_backup_disable_and_restore(
        self, _mock_ids, mock_lnd_announcement_cleanup, client
    ):
        cleanup_mock, _original_cleanup = mock_lnd_announcement_cleanup
        cleanup_mock.return_value = (True, ['198.51.100.10:9735'], [])
        with tempfile.TemporaryDirectory() as tmp_dir:
            meta_path = os.path.join(tmp_dir, app_module.META_FILE)
            lnd_path = os.path.join(tmp_dir, 'lnd.conf')
            original = (
                '[Application Options]\n'
                'externalip=198.51.100.10:9735\n'
                'externalip=203.0.113.9:9735\n'
                'externalip=examplehiddenservice.onion:9735\n'
                'externalhosts=old.example.com:9735\n'
                'nat=1\n'
                'nat=true # automatic discovery'
            )
            with open(meta_path, 'w') as f:
                json.dump({'vpnPort': 35825, 'serverDomain': 'de2.tunnelsats.com'}, f)
            with open(lnd_path, 'w') as f:
                f.write(original)

            with patch('app.DATA_DIR', tmp_dir), patch('app.LND_CONFIG_PATH', lnd_path), \
                    patch('app.restart_container_by_pattern', return_value=True):
                blocked = client.post('/api/local/configure-node', json={'nodeType': 'lnd'})
                assert blocked.status_code == 409
                assert json.loads(blocked.data)['requires_confirmation'] is True
                with open(lnd_path) as f:
                    assert f.read() == original

                configured = client.post('/api/local/configure-node', json={
                    'nodeType': 'lnd',
                    'confirmAddressChanges': True,
                    'retainTorAnnouncements': True,
                })
                assert configured.status_code == 200

                with open(lnd_path) as f:
                    protected = f.read()
                assert '# TunnelSats disabled: externalip=198.51.100.10:9735' in protected
                assert '# TunnelSats disabled: externalip=203.0.113.9:9735' in protected
                assert 'externalip=examplehiddenservice.onion:9735\n' in protected
                assert 'externalhosts=de2.tunnelsats.com:35825\n' in protected
                assert 'nat=false\n' in protected
                assert '\nnat=1\n' not in protected
                assert '# TunnelSats disabled: nat=true # automatic discovery\n' in protected

                with open(meta_path) as f:
                    protected_meta = json.load(f)
                assert protected_meta['backupConfig']['lnd']['lines'] == [
                    'externalip=198.51.100.10:9735',
                    'externalip=203.0.113.9:9735',
                    'externalip=examplehiddenservice.onion:9735',
                    'externalhosts=old.example.com:9735',
                    'nat=1',
                    'nat=true # automatic discovery',
                ]
                assert protected_meta['lndAnnouncementVerification']['verified'] is True
                cleanup_mock.assert_called_once_with('de2.tunnelsats.com', 35825, True)

                restored = client.post('/api/local/restore-node')
                assert restored.status_code == 200
                with open(lnd_path) as f:
                    restored_content = f.read()
                for expected in original.splitlines()[1:]:
                    assert f'\n{expected}\n' in restored_content
                assert '\nexternalhosts=de2.tunnelsats.com:35825\n' not in restored_content
                with open(meta_path) as f:
                    restored_meta = json.load(f)
                assert 'backupConfig' not in restored_meta
                assert 'lndAnnouncementVerification' not in restored_meta

    @patch('app.container_ids_by_match', return_value=['mock'])
    def test_cln_discovery_and_clearnet_announcement_require_confirmation(self, _mock_ids, client):
        with tempfile.TemporaryDirectory() as tmp_dir:
            meta_path = os.path.join(tmp_dir, app_module.META_FILE)
            cln_path = os.path.join(tmp_dir, 'config')
            with open(meta_path, 'w') as f:
                json.dump({'vpnPort': 35825, 'serverDomain': 'de2.tunnelsats.com'}, f)
            with open(cln_path, 'w') as f:
                f.write(
                    'announce-addr=198.51.100.20:9735\n'
                    'announce-addr=keepme.onion:9735\n'
                    'ip-discovery=true\n'
                )

            with patch('app.DATA_DIR', tmp_dir), patch('app.CLN_CONFIG_PATH', cln_path), \
                    patch('app.restart_container_by_pattern', return_value=True):
                blocked = client.post('/api/local/configure-node', json={'nodeType': 'cln'})
                payload = json.loads(blocked.data)
                assert blocked.status_code == 409
                assert payload['conflicts'] == [
                    'announce-addr=198.51.100.20:9735',
                    'ip-discovery=true',
                ]
                configured = client.post('/api/local/configure-node', json={
                    'nodeType': 'cln',
                    'confirmAddressChanges': True,
                    'retainTorAnnouncements': True,
                })
                assert configured.status_code == 200

                with open(cln_path) as f:
                    first_configured_content = f.read()
                configured_again = client.post('/api/local/configure-node', json={'nodeType': 'cln'})
                assert configured_again.status_code == 200
                assert json.loads(configured_again.data)['cln_changed'] is False
                with open(cln_path) as f:
                    assert f.read() == first_configured_content

            with open(cln_path) as f:
                content = f.read()
            assert '\nannounce-addr=198.51.100.20:9735\n' not in content
            assert 'announce-addr=keepme.onion:9735\n' in content
            assert 'announce-addr=de2.tunnelsats.com:35825\n' in content
            assert 'ip-discovery=false\n' in content

    def test_live_lnd_cleanup_withdraws_real_address_and_verifies_again(
        self, mock_lnd_announcement_cleanup
    ):
        _cleanup_mock, original_cleanup = mock_lnd_announcement_cleanup
        first = json.dumps({'uris': [
            'pubkey@198.51.100.10:9735',
            'pubkey@nodehidden.onion:9735',
            'pubkey@de2.tunnelsats.com:35825',
        ]})
        final = json.dumps({'uris': [
            'pubkey@nodehidden.onion:9735',
            'pubkey@de2.tunnelsats.com:35825',
        ]})
        with patch('app.container_id_by_match', return_value='container123'), \
                patch('app.docker_exec', side_effect=[
                    (True, first),
                    (True, ''),
                    (True, final),
                ]) as exec_mock:
            result = original_cleanup('de2.tunnelsats.com', 35825, True)

        assert result == (True, ['198.51.100.10:9735'], [])
        assert exec_mock.call_args_list[1].args[1] == [
            'lncli', 'peers', 'updatenodeannouncement',
            '--address_remove=198.51.100.10:9735',
        ]

    def test_status_fails_closed_on_active_lnd_announcement_conflicts(self, client):
        with tempfile.TemporaryDirectory() as tmp_dir:
            lnd_path = os.path.join(tmp_dir, 'lnd.conf')
            with open(lnd_path, 'w') as f:
                f.write(
                    '[Application Options]\n'
                    'externalhosts=de2.tunnelsats.com:35825\n'
                    'externalip=198.51.100.42:9735\n'
                    'nat=true\n'
                )
            with open(os.path.join(tmp_dir, app_module.META_FILE), 'w') as f:
                json.dump({'vpnPort': 35825, 'serverDomain': 'de2.tunnelsats.com'}, f)
            dataplane = app_module.read_dataplane_state()
            dataplane.update({'rules_synced': True, 'last_error': None})
            containers = [{
                'Names': ['/lightning_lnd_1'], 'Id': 'lnd1',
                'NetworkSettings': {'Networks': {'main': {'IPAddress': '10.21.21.9'}}},
            }]
            run_result = MagicMock(returncode=1, stdout='')
            with patch('app.DATA_DIR', tmp_dir), patch('app.LND_CONFIG_PATH', lnd_path), \
                    patch('app._get_wireguard_state', return_value=('Connected', '')), \
                    patch('app.list_containers', return_value=containers), \
                    patch('app.read_dataplane_state', return_value=dataplane), \
                    patch('app.subprocess.run', return_value=run_result):
                response = client.get('/api/local/status')

        payload = json.loads(response.data)
        assert payload['rules_synced'] is False
        assert payload['last_error'] == app_module.ANNOUNCEMENT_CONFLICT_ERROR
        assert payload['announcement_conflicts'] == [
            'lnd:externalip=198.51.100.42:9735',
            'lnd:nat=true',
        ]

    def test_status_fails_closed_when_detected_lnd_config_is_unavailable(self, client):
        with tempfile.TemporaryDirectory() as tmp_dir:
            missing_lnd_path = os.path.join(tmp_dir, 'missing-lnd.conf')
            with open(os.path.join(tmp_dir, app_module.META_FILE), 'w') as f:
                json.dump({'vpnPort': 35825, 'serverDomain': 'de2.tunnelsats.com'}, f)
            dataplane = app_module.read_dataplane_state()
            dataplane.update({'rules_synced': True, 'last_error': None})
            containers = [{
                'Names': ['/lightning_lnd_1'], 'Id': 'lnd1',
                'NetworkSettings': {'Networks': {'main': {'IPAddress': '10.21.21.9'}}},
            }]
            with patch('app.DATA_DIR', tmp_dir), \
                    patch('app.LND_CONFIG_PATH', missing_lnd_path), \
                    patch('app._get_wireguard_state', return_value=('Connected', '')), \
                    patch('app.list_containers', return_value=containers), \
                    patch('app.read_dataplane_state', return_value=dataplane), \
                    patch('app.subprocess.run', return_value=MagicMock(returncode=1, stdout='')):
                response = client.get('/api/local/status')

        payload = json.loads(response.data)
        assert payload['rules_synced'] is False
        assert payload['last_error'] == app_module.ANNOUNCEMENT_CONFLICT_ERROR
        assert payload['announcement_conflicts'] == ['lnd:config-unavailable']

    def test_status_rpc_verification_and_secure_mode_trusts_clean_config(self, client):
        with tempfile.TemporaryDirectory() as tmp_dir:
            lnd_path = os.path.join(tmp_dir, 'lnd.conf')
            meta_path = os.path.join(tmp_dir, app_module.META_FILE)
            with open(lnd_path, 'w') as f:
                f.write(
                    '[Application Options]\n'
                    'externalhosts=de2.tunnelsats.com:35825\n'
                    'nat=false\n'
                )
            with open(meta_path, 'w') as f:
                json.dump({'vpnPort': 35825, 'serverDomain': 'de2.tunnelsats.com'}, f)
            dataplane = app_module.read_dataplane_state()
            dataplane.update({'rules_synced': True, 'last_error': None})
            containers = [{
                'Names': ['/lightning_lnd_1'], 'Id': 'lnd1',
                'NetworkSettings': {'Networks': {'main': {'IPAddress': '10.21.21.9'}}},
            }]
            common_patches = (
                patch('app.DATA_DIR', tmp_dir),
                patch('app.LND_CONFIG_PATH', lnd_path),
                patch('app._get_wireguard_state', return_value=('Connected', '')),
                patch('app.list_containers', return_value=containers),
                patch('app.read_dataplane_state', return_value=dataplane),
                patch('app.subprocess.run', return_value=MagicMock(returncode=1, stdout='')),
                patch('app.clean_and_verify_lnd_announcements', return_value=(False, [], ["missing expected announcement de2.tunnelsats.com:35825"])),
            )
            with common_patches[0], common_patches[1], common_patches[2], \
                    common_patches[3], common_patches[4], common_patches[5], common_patches[6]:
                unverified = json.loads(client.get('/api/local/status').data)

            assert unverified['rules_synced'] is False
            assert unverified['last_error'] == app_module.ANNOUNCEMENT_VERIFICATION_ERROR

            with open(meta_path, 'w') as f:
                json.dump({
                    'vpnPort': 35825,
                    'serverDomain': 'de2.tunnelsats.com',
                    'lndAnnouncementVerification': {
                        'verified': True,
                        'endpoint': 'de2.tunnelsats.com:35825',
                    },
                }, f)
            with patch('app.DATA_DIR', tmp_dir), patch('app.LND_CONFIG_PATH', lnd_path), \
                    patch('app._get_wireguard_state', return_value=('Connected', '')), \
                    patch('app.list_containers', return_value=containers), \
                    patch('app.read_dataplane_state', return_value=dataplane), \
                    patch('app.subprocess.run', return_value=MagicMock(returncode=1, stdout='')):
                verified = json.loads(client.get('/api/local/status').data)
            assert verified['rules_synced'] is True
            assert verified['announcement_verified'] is True

            # In secure_mode the lnd dir is mounted :ro — readable but no lncli access.
            # Trust the config file: conflict-free audit means announcement_verified = True.
            with patch('app.DATA_DIR', tmp_dir), patch('app.LND_CONFIG_PATH', lnd_path), \
                    patch('app.SECURE_MODE', True), patch('app.lnd_exists', return_value=True), \
                    patch('app.cln_exists', return_value=False), \
                    patch('app._get_wireguard_state', return_value=('Connected', '')), \
                    patch('app.read_dataplane_state', return_value=dataplane), \
                    patch('app.subprocess.run', return_value=MagicMock(returncode=1, stdout='')):
                secure = json.loads(client.get('/api/local/status').data)
            assert secure['rules_synced'] is True
            assert secure['announcement_verified'] is True
            assert secure['last_error'] is None

class TestFullE2E_Workflow:
    @patch('app.requests.post')
    @patch('app.requests.get')
    @patch('app.docker_api')
    @patch('app.docker_api_post')
    @patch('app.subprocess.check_output')
    def test_full_workflow(self, mock_subprocess, mock_docker_post, mock_docker_api, mock_get, mock_post, client, data_dir):
        # 1. Create Sub
        mock_post_create = MagicMock()
        mock_post_create.status_code = 200
        mock_post_create.json.return_value = {"invoice": "lnbc123", "paymentHash": "hash123"}
        mock_post_create.headers = {'Content-Type': 'application/json'}
        mock_post_create.content = b'{"invoice": "lnbc123", "paymentHash": "hash123"}'
        
        # Set up a side effect for POST to route to different responses based on url
        def mock_post_side_effect(url, **kwargs):
            if "claim" in url:
                mock_post_claim = MagicMock()
                mock_post_claim.status_code = 200
                mock_post_claim.headers = {'Content-Type': 'application/json'}
                mock_post_claim.json.return_value = {
                    "success": True, 
                    "message": "Claimed", 
                    "config": "[Interface]\nPrivateKey = secret123\nAddress = 10.0.0.1/32\n\n[Peer]\nPublicKey = pub123\nEndpoint = wg.example.com:51820\nAllowedIPs = 0.0.0.0/0\n"
                }
                mock_post_claim.content = json.dumps(mock_post_claim.json.return_value).encode('utf-8')
                return mock_post_claim
            return mock_post_create
            
        mock_post.side_effect = mock_post_side_effect

        res = client.post('/api/subscription/create', json={"serverId": "eu-de", "duration": 1})
        assert res.status_code == 200
        assert json.loads(res.data)["paymentHash"] == "hash123"

        # 2. Poll Status (Paid)
        mock_get_status = MagicMock()
        mock_get_status.status_code = 200
        mock_get_status.json.return_value = {"status": "paid", "isProvisioned": False}
        mock_get_status.headers = {'Content-Type': 'application/json'}
        mock_get_status.content = b'{"status": "paid", "isProvisioned": false}'
        mock_get.return_value = mock_get_status

        res = client.get('/api/subscription/hash123')
        assert res.status_code == 200
        assert json.loads(res.data)["status"] == "paid"

        # 3. Claim Sub
        res = client.post('/api/subscription/claim', json={"paymentHash": "hash123", "wgPublicKey": "", "wgPresharedKey": "", "referralCode": None})
        assert res.status_code == 200

        # Verify files were saved
        conf_path = os.path.join(data_dir, "tunnelsats.conf")
        meta_path = os.path.join(data_dir, app_module.META_FILE)
        assert os.path.exists(conf_path)
        assert os.path.exists(meta_path)
        
        with open(meta_path, 'r') as f:
            meta = json.load(f)
            assert meta["paymentHash"] == "hash123"

        # 4. Trigger Restart
        mock_docker_post.return_value = ({}, 200)
        res = client.post('/api/local/restart')
        assert res.status_code == 200

        # 5. Status Check
        def mock_wg_show_side_effect(cmd, **kwargs):
            if cmd == ["wg", "show", "tunnelsatsv2"]:
                return (
                    b"interface: tunnelsatsv2\n"
                    b"  public key: pubKey123\n"
                    b"  private key: (hidden)\n"
                    b"  listening port: 51820\n"
                    b"peer: peerPubKey123\n"
                )
            if cmd == ["wg", "show", "tunnelsatsv2", "latest-handshakes"]:
                return f"peerPubKey123\t{int(time.time())}\n".encode('utf-8')
            raise AssertionError(f"Unexpected command: {cmd}")

        mock_subprocess.side_effect = mock_wg_show_side_effect
        
        res = client.get('/api/local/status')
        assert res.status_code == 200
        status_data = json.loads(res.data)
        assert status_data["wg_status"] == "Connected"
        assert status_data["wg_pubkey"] == "pubKey123"
        assert "tunnelsats.conf" in status_data["configs_found"]



class TestMetadataSync:
    def test_update_local_metadata_skips_when_file_missing(self, client, data_dir):
        """Verifies that _update_local_metadata does not create a sparse file when it's missing."""
        from app import _update_local_metadata
        meta_path = os.path.join(data_dir, app_module.META_FILE)
        assert not os.path.exists(meta_path)
        
        # Call with some data
        sync_data = {"expiresAt": "2026-05-01T12:00:00Z"}
        result = _update_local_metadata(sync_data, payment_hash="hash123")
        
        assert result is False
        assert not os.path.exists(meta_path), "Should not create a sparse metadata file"

    def test_update_local_metadata_skips_when_metadata_not_object(self, client, data_dir):
        from app import _update_local_metadata
        meta_path = os.path.join(data_dir, app_module.META_FILE)
        with open(meta_path, 'w') as f:
            json.dump([], f)

        result = _update_local_metadata({"expiresAt": "2026-05-01T12:00:00Z"}, payment_hash="hash123")

        assert result is False
        with open(meta_path, 'r') as f:
            assert json.load(f) == []

    def test_update_local_metadata_prefers_new_expiry_over_expires_at(self, client, data_dir):
        from app import _update_local_metadata
        meta_path = os.path.join(data_dir, app_module.META_FILE)
        with open(meta_path, 'w') as f:
            json.dump({"expiresAt": "2027-01-01T00:00:00Z"}, f)

        result = _update_local_metadata(
            {"expiresAt": "2027-01-01T00:00:00Z", "newExpiry": "2027-02-01T00:00:00Z"},
            payment_hash="hash123"
        )

        assert result is True
        with open(meta_path, 'r') as f:
            meta = json.load(f)
        assert meta["expiresAt"] == "2027-02-01T00:00:00Z"

def test_claim_subscription_invalid_config(client, data_dir):
    """Verify that claim_subscription returns 400 if the upstream config is malformed."""
    malformed_response = MOCK_CLAIM_RESPONSE.copy()
    malformed_response["config"] = "[Interface]\nPrivateKey = 123\n# Missing Peer block"
    
    with patch('app.requests.post') as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = malformed_response
        mock_resp.content = json.dumps(malformed_response).encode()
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_post.return_value = mock_resp
        
        res = client.post('/api/subscription/claim',
                         json={"paymentHash": "abc"},
                         headers={"Content-Type": "application/json"})
        
        assert res.status_code == 400
        data = json.loads(res.data)
        assert data["success"] is False
        assert "Invalid upstream payload" in data["error"]


class TestLazySubscriptionSync:
    @pytest.fixture(autouse=True)
    def setup_sync_test(self):
        # Clear the global next sync dictionary before/after each test
        if hasattr(app_module, '_next_subscription_sync_time'):
            app_module._next_subscription_sync_time.clear()

    def _wait_for_sync_thread(self):
        import threading
        for t in threading.enumerate():
            if t.name.startswith("sync_worker_"):
                t.join(timeout=2.0)

    @patch('app._get_wireguard_state', return_value=('Connected', 'pubKey123'))
    @patch('app._fetch_subscription_status')
    def test_local_status_triggers_lazy_subscription_sync(self, mock_fetch_status, _mock_wg_state, client, data_dir):
        # Setup metadata file
        meta_path = os.path.join(data_dir, app_module.META_FILE)
        with open(meta_path, 'w') as f:
            json.dump({
                "serverDomain": "au1.tunnelsats.com",
                "expiresAt": "2026-05-04T19:06:14.000Z",
                "wgPublicKey": "pubKey123"
            }, f)

        # Mock the upstream API status response with simulated latency
        def mock_fetch_with_latency(*args, **kwargs):
            time.sleep(0.05)
            return {
                "expiry": "2026-06-04T19:06:14.000Z",
                "status": "enabled"
            }
        mock_fetch_status.side_effect = mock_fetch_with_latency

        # First request to status endpoint should return old expiry immediately but trigger sync
        res = client.get('/api/local/status')
        assert res.status_code == 200
        data = json.loads(res.data)
        assert data["expires_at"] == "2026-05-04T19:06:14.000Z"

        # Wait for the background thread to finish
        self._wait_for_sync_thread()

        # The metadata file on disk should now be updated
        with open(meta_path, 'r') as f:
            meta = json.load(f)
            assert meta["expiresAt"] == "2026-06-04T19:06:14.000Z"

        # Second request to status endpoint should now return the new expiry
        res = client.get('/api/local/status')
        assert res.status_code == 200
        data = json.loads(res.data)
        assert data["expires_at"] == "2026-06-04T19:06:14.000Z"
        
        # Upstream API should only have been called once
        mock_fetch_status.assert_called_once_with('pubKey123')

    @patch('app._get_wireguard_state', return_value=('Connected', 'pubKey123'))
    @patch('app._fetch_subscription_status')
    def test_lazy_subscription_sync_is_throttled(self, mock_fetch_status, _mock_wg_state, client, data_dir):
        # Setup metadata
        meta_path = os.path.join(data_dir, app_module.META_FILE)
        with open(meta_path, 'w') as f:
            json.dump({
                "expiresAt": "2026-05-04T19:06:14.000Z",
                "wgPublicKey": "pubKey123"
            }, f)

        mock_fetch_status.return_value = {
            "expiry": "2026-06-04T19:06:14.000Z",
            "status": "enabled"
        }

        # Make two rapid requests
        client.get('/api/local/status')
        client.get('/api/local/status')

        # Wait for threads
        self._wait_for_sync_thread()

        # Upstream API should only have been called once due to the cache
        assert mock_fetch_status.call_count == 1

    @patch('app._get_wireguard_state', return_value=('Connected', 'pubKey123'))
    @patch('app._fetch_subscription_status')
    @patch('app.time.time')
    def test_lazy_subscription_sync_retries_on_failure(self, mock_time, mock_fetch_status, _mock_wg_state, client, data_dir):
        # Start at time 1000000
        mock_time.return_value = 1000000.0

        # Setup metadata
        meta_path = os.path.join(data_dir, app_module.META_FILE)
        with open(meta_path, 'w') as f:
            json.dump({
                "expiresAt": "2026-05-04T19:06:14.000Z",
                "wgPublicKey": "pubKey123"
            }, f)

        # First attempt: API fails
        mock_fetch_status.return_value = None

        client.get('/api/local/status')
        self._wait_for_sync_thread()
        
        # Verify first call made
        assert mock_fetch_status.call_count == 1

        # Request again at 1000000 + 1800 (30 mins later). It should NOT call API again yet
        mock_time.return_value = 1000000.0 + 1800.0
        client.get('/api/local/status')
        self._wait_for_sync_thread()
        assert mock_fetch_status.call_count == 1

        # Request at 1000000 + 3601 (1 hour and 1 second later). It SHOULD retry
        mock_time.return_value = 1000000.0 + 3601.0
        # This time API succeeds
        mock_fetch_status.return_value = {
            "expiry": "2026-06-04T19:06:14.000Z",
            "status": "enabled"
        }
        client.get('/api/local/status')
        self._wait_for_sync_thread()
        assert mock_fetch_status.call_count == 2

        with open(meta_path, 'r') as f:
            meta = json.load(f)
            assert meta["expiresAt"] == "2026-06-04T19:06:14.000Z"


class TestK3SModeSupport:
    """TDD unit tests for Kubernetes/k3s helper functions."""

    @patch('app.K8S_SA_TOKEN_PATH', '/dev/null')
    @patch('app._k8s_session.get')
    def test_k8s_get_pod_name_success(self, mock_get):
        # Setup mock response from k8s API
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "items": [
                {
                    "metadata": {"name": "lnd-pod-abc"},
                    "status": {"phase": "Running"}
                }
            ]
        }
        mock_get.return_value = mock_resp

        # Clear cache first to ensure a clean run
        with app_module._k8s_cache_lock:
            app_module._k8s_cache.clear()

        name = app_module.k8s_get_pod_name("app=lnd", namespace="default")
        assert name == "lnd-pod-abc"

    def test_k8s_get_pod_name_empty_selector(self):
        # Defensive guard test
        name = app_module.k8s_get_pod_name("", namespace="default")
        assert name is None
        name = app_module.k8s_get_pod_name(None, namespace="default")
        assert name is None

    @patch('app.K8S_SA_TOKEN_PATH', '/dev/null')
    @patch('app._k8s_session.delete')
    def test_k8s_delete_pod_404_success(self, mock_delete):
        # HTTP 404 should return True because pod is already gone
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_delete.return_value = mock_resp

        # Clear cache first to ensure a clean run
        with app_module._k8s_cache_lock:
            app_module._k8s_cache.clear()

        res = app_module.k8s_delete_pod("lnd-pod-abc", namespace="default")
        assert res is True

    def test_k8s_delete_pod_empty_name(self):
        # Defensive guard test
        res = app_module.k8s_delete_pod("", namespace="default")
        assert res is False
        res = app_module.k8s_delete_pod(None, namespace="default")
        assert res is False

    @patch('app.K3S_MODE', True)
    @patch('app.lnd_exists', return_value=True)
    @patch('app.cln_exists', return_value=True)
    @patch('socket.getaddrinfo')
    @patch('app._get_wireguard_state', return_value=('Connected', 'pubKey123'))
    def test_resolve_svc_caching(self, mock_wg_state, mock_getaddrinfo, _mock_cln, _mock_lnd, client):
        # We set environment variables for the service names
        os.environ["LND_K8S_SERVICE"] = "lnd-svc"
        os.environ["CLN_K8S_SERVICE"] = "cln-svc"

        # Mock socket.getaddrinfo to return IP addresses
        mock_getaddrinfo.side_effect = lambda host, port, family=0, type=0, proto=0, flags=0: (
            [(2, 1, 6, '', ("10.42.0.50", 0))] if "lnd" in host else [(2, 1, 6, '', ("10.42.0.60", 0))]
        )

        # Clear cache first to ensure a clean run
        with app_module._k8s_cache_lock:
            app_module._k8s_cache.clear()

        # Call endpoint twice
        res1 = client.get('/api/local/status')
        assert res1.status_code == 200

        res2 = client.get('/api/local/status')
        assert res2.status_code == 200

        # Assert socket.getaddrinfo was called only once per unique service/FQDN
        # (Total of 2 calls, one for LND service and one for CLN service)
        # Without caching, it would be called at least 4 times (2 calls * 2 endpoints)
        svc_calls = [c for c in mock_getaddrinfo.call_args_list if "svc" in c[0][0]]
        assert len(svc_calls) == 2


class TestSecureModeToggle:
    @patch('app.SECURE_MODE', True)
    @patch('app.check_tcp_port', return_value=True)
    def test_list_containers_secure_mode(self, mock_tcp):
        from app import list_containers
        containers = list_containers()
        assert len(containers) == 2
        assert containers[0]["Names"] == ["/lightning_lnd_1"]
        assert containers[1]["Names"] == ["/lightning_cln_1"]

    @patch('app.SECURE_MODE', True)
    @patch('app.lnd_exists', return_value=True)
    @patch('app.cln_exists', return_value=True)
    def test_configure_node_secure_mode_returns_instructions(self, mock_cln, mock_lnd, client):
        with tempfile.TemporaryDirectory() as tmp_dir:
            meta_path = os.path.join(tmp_dir, app_module.META_FILE)
            with open(meta_path, 'w') as f:
                json.dump({'vpnPort': 35825, 'serverDomain': 'de2.tunnelsats.com'}, f)

            with patch('app.DATA_DIR', tmp_dir):
                res = client.post('/api/local/configure-node', json={'nodeType': 'lnd'})
                assert res.status_code == 200
                payload = json.loads(res.data)
                assert payload['success'] is True
                assert payload['manual_mode'] is True
                assert payload['node_type'] == 'lnd'
                assert 'externalhosts=de2.tunnelsats.com:35825' in payload['config_lines'][0]

    @patch('app.SECURE_MODE', True)
    @patch('app.lnd_exists', return_value=True)
    @patch('app.cln_exists', return_value=True)
    def test_restore_node_secure_mode_returns_instructions(self, mock_cln, mock_lnd, client):
        res = client.post('/api/local/restore-node')
        assert res.status_code == 200
        payload = json.loads(res.data)
        assert payload['success'] is True
        assert payload['manual_mode'] is True
        assert payload['restore'] is True
        assert len(payload['targets']) == 2
        assert payload['targets'][0]['node_type'] == 'lnd'
        assert 'externalhosts=' in payload['targets'][0]['config_lines']

    def test_detect_cln_network_caching(self):
        import app
        # Reset cache
        app._cln_network_cache = None
        app._cln_network_cache_time = 0.0

        with patch('os.path.exists', return_value=True), \
             patch('os.path.getmtime', side_effect=[100.0, 50.0, 50.0, 50.0, 200.0, 50.0, 50.0, 50.0]) as mock_mtime:
            # First call: bitcoin has mtime 100.0
            net1 = app.detect_cln_network()
            assert net1 == "bitcoin"
            assert mock_mtime.call_count == 4

            # Second call: within 60s TTL, should use cache and not call getmtime
            net2 = app.detect_cln_network()
            assert net2 == "bitcoin"
            assert mock_mtime.call_count == 4

            # Advance time by 61 seconds
            with patch('time.time', return_value=time.time() + 61.0):
                # Third call: cache expired, calls getmtime again
                net3 = app.detect_cln_network()
                assert net3 == "bitcoin"
                assert mock_mtime.call_count == 8


def test_is_expected_tunnelsats_address_resolves_domain_ip():
    with patch('socket.gethostbyname_ex', return_value=('us3.tunnelsats.com', [], ['178.156.167.202'])):
        assert app_module._is_expected_tunnelsats_address('178.156.167.202:23217', 'us3.tunnelsats.com', 23217) is True
        assert app_module._is_expected_tunnelsats_address('us3.tunnelsats.com:23217', 'us3.tunnelsats.com', 23217) is True
        assert app_module._is_expected_tunnelsats_address('203.0.113.1:23217', 'us3.tunnelsats.com', 23217) is False


def test_export_config_returns_file_and_404_when_missing(client):
    with tempfile.TemporaryDirectory() as tmp_dir:
        with patch('app.DATA_DIR', tmp_dir):
            res_missing = client.get('/api/local/export-config')
            assert res_missing.status_code == 404

            conf_path = os.path.join(tmp_dir, 'tunnelsats.conf')
            with open(conf_path, 'w') as f:
                f.write('[Interface]\nPrivateKey=testkey\n[Peer]\nPublicKey=serverkey\n')

            res_found = client.get('/api/local/export-config')
            assert res_found.status_code == 200
            assert b'PrivateKey=testkey' in res_found.data


def test_parse_cln_getinfo_addresses():
    raw_cln = json.dumps({
        "id": "0200e0d6a3d224ff99a28c576385d78b1df86a67464f99c9de2774842b5885e155",
        "address": [
            {"type": "ipv4", "address": "178.156.167.202", "port": 23217},
            {"type": "torv3", "address": "db4bzummaxm5emq5w4d5xjowpmr4whduouonujz53stk553vptsulhid.onion", "port": 9735}
        ]
    })
    parsed = app_module._parse_cln_getinfo_addresses(raw_cln)
    assert parsed == ["178.156.167.202:23217", "db4bzummaxm5emq5w4d5xjowpmr4whduouonujz53stk553vptsulhid.onion:9735"]


def test_clean_and_verify_cln_announcements():
    raw_cln = json.dumps({
        "address": [{"type": "ipv4", "address": "178.156.167.202", "port": 23217}]
    })
    with patch('app.SECURE_MODE', False), patch('app.K3S_MODE', False), \
            patch('app.container_id_by_match', return_value='cln1'), \
            patch('app.docker_exec', return_value=(True, raw_cln)), \
            patch('socket.gethostbyname_ex', return_value=('us3.tunnelsats.com', [], ['178.156.167.202'])):
        ok, removed, conflicts = app_module.clean_and_verify_cln_announcements('us3.tunnelsats.com', 23217, True)
        assert ok is True
        assert conflicts == []


def test_update_announcement_metadata_cln():
    with tempfile.TemporaryDirectory() as tmp_dir:
        meta_path = os.path.join(tmp_dir, "tunnelsats-meta.json")
        initial_data = {
            "serverDomain": "us3.tunnelsats.com",
            "vpnPort": 23217,
            "expiresAt": "2027-05-10T13:19:06.000Z",
            "subscriptionHash": "abc123hash",
        }
        with open(meta_path, "w") as f:
            json.dump(initial_data, f)

        verification = {"endpoint": "us3.tunnelsats.com:23217", "verified": True}
        ok = app_module._update_announcement_metadata(meta_path, "cln", verification=verification)
        assert ok is True
        with open(meta_path, "r") as f:
            data = json.load(f)
        assert data["clnAnnouncementVerification"] == verification
        assert data["serverDomain"] == "us3.tunnelsats.com"
        assert data["vpnPort"] == 23217
        assert data["expiresAt"] == "2027-05-10T13:19:06.000Z"
        assert data["subscriptionHash"] == "abc123hash"


def test_audit_node_announcement_config_requires_tunnelsats_endpoint():
    with tempfile.TemporaryDirectory() as tmp_dir:
        lnd_conf = os.path.join(tmp_dir, "lnd.conf")
        with open(lnd_conf, "w") as f:
            f.write("[Application Options]\nexternalhosts=someonion.onion:9735\n")
        res = app_module.audit_node_announcement_config("lnd", lnd_conf, "us3.tunnelsats.com", 23217)
        assert res["readable"] is True
        assert res["has_expected_tunnelsats"] is False


def test_audit_cln_config_bind_addr_does_not_satisfy_tunnelsats_endpoint():
    with tempfile.TemporaryDirectory() as tmp_dir:
        cln_conf = os.path.join(tmp_dir, "config")
        with open(cln_conf, "w") as f:
            f.write("bind-addr=us3.tunnelsats.com:23217\nannounce-addr=someonion.onion:9735\n")
        res = app_module.audit_node_announcement_config("cln", cln_conf, "us3.tunnelsats.com", 23217)
        assert res["readable"] is True
        assert res["has_expected_tunnelsats"] is False


def test_status_reports_reconciled_target_while_requested_switch_is_pending(client):
    with tempfile.TemporaryDirectory() as tmp_dir:
        meta_path = os.path.join(tmp_dir, app_module.META_FILE)
        with open(meta_path, "w") as f:
            json.dump({"nodeType": "cln", "serverDomain": "us3.tunnelsats.com", "vpnPort": 23217}, f)

        dataplane = app_module.read_dataplane_state()
        dataplane.update({
            "target_impl": "lnd",
            "target_container": "lightning_lnd_1",
            "target_ip": "10.21.21.9",
            "rules_synced": True,
            "last_error": None,
        })
        containers = [
            {"Names": ["/lightning_lnd_1"], "Id": "lnd1", "NetworkSettings": {"Networks": {"main": {"IPAddress": "10.21.21.9"}}}},
            {"Names": ["/lightning_cln_1"], "Id": "cln1", "NetworkSettings": {"Networks": {"main": {"IPAddress": "10.21.21.94"}}}},
        ]
        with patch('app.DATA_DIR', tmp_dir), patch('app._get_wireguard_state', return_value=('Connected', 'pub123')), \
                patch('app.list_containers', return_value=containers), \
                patch('app.read_dataplane_state', return_value=dataplane), \
                patch('app.clean_and_verify_cln_announcements', return_value=(True, [], [])):
            res = json.loads(client.get('/api/local/status').data)
        assert res["target_impl"] == "lnd"
        assert res["requested_target_impl"] == "cln"
        assert res["target_switch_pending"] is True
        assert res["rules_synced"] is False
        assert res["last_error"] == app_module.TARGET_SWITCH_PENDING_ERROR


def test_configure_node_persists_node_type_in_standard_mode(client, mock_target_reconcile):
    with tempfile.TemporaryDirectory() as tmp_dir:
        meta_path = os.path.join(tmp_dir, app_module.META_FILE)
        with open(meta_path, "w") as f:
            json.dump({"serverDomain": "us3.tunnelsats.com", "vpnPort": 23217}, f)

        with patch('app.DATA_DIR', tmp_dir), patch('app.SECURE_MODE', False), patch('app.K3S_MODE', False), \
                patch('app.container_ids_by_match', return_value=['cln1']), \
                patch('app.restart_container_by_pattern', return_value=True), \
                patch('app.resolve_node_config', return_value=(os.path.join(tmp_dir, 'config'), os.path.join(tmp_dir, 'config'))), \
                patch('app.clean_and_verify_cln_announcements', return_value=(True, [], [])):
            with open(os.path.join(tmp_dir, 'config'), 'w') as f:
                f.write('')
            res = client.post('/api/local/configure-node', json={"nodeType": "cln"})
            assert res.status_code == 200

        with open(meta_path, 'r') as f:
            meta = json.load(f)
        assert meta["nodeType"] == "cln"
        mock_target_reconcile[0].assert_called_once_with("cln")
        with open(os.path.join(tmp_dir, 'config'), 'r') as f:
            assert "announce-addr=us3.tunnelsats.com:23217" in f.read()


def test_configure_node_does_not_persist_node_type_on_failure(client):
    with tempfile.TemporaryDirectory() as tmp_dir:
        meta_path = os.path.join(tmp_dir, app_module.META_FILE)
        with open(meta_path, "w") as f:
            json.dump({"nodeType": "lnd", "serverDomain": "us3.tunnelsats.com", "vpnPort": 23217}, f)

        with patch('app.DATA_DIR', tmp_dir), patch('app.SECURE_MODE', False), patch('app.K3S_MODE', False), \
                patch('app.container_ids_by_match', return_value=['cln1']), \
                patch('app.restart_container_by_pattern', return_value=False), \
                patch('app.resolve_node_config', return_value=(os.path.join(tmp_dir, 'config'), os.path.join(tmp_dir, 'config'))):
            with open(os.path.join(tmp_dir, 'config'), 'w') as f:
                f.write('')
            res = client.post('/api/local/configure-node', json={"nodeType": "cln"})
            assert res.status_code == 500

        with open(meta_path, 'r') as f:
            meta = json.load(f)
        assert meta["nodeType"] == "lnd"


def test_configure_node_removes_node_type_on_first_time_failure(client):
    with tempfile.TemporaryDirectory() as tmp_dir:
        meta_path = os.path.join(tmp_dir, app_module.META_FILE)
        with open(meta_path, "w") as f:
            json.dump({"serverDomain": "us3.tunnelsats.com", "vpnPort": 23217}, f)

        with patch('app.DATA_DIR', tmp_dir), patch('app.SECURE_MODE', False), patch('app.K3S_MODE', False), \
                patch('app.container_ids_by_match', return_value=['cln1']), \
                patch('app.restart_container_by_pattern', return_value=False), \
                patch('app.resolve_node_config', return_value=(os.path.join(tmp_dir, 'config'), os.path.join(tmp_dir, 'config'))):
            with open(os.path.join(tmp_dir, 'config'), 'w') as f:
                f.write('')
            res = client.post('/api/local/configure-node', json={"nodeType": "cln"})
            assert res.status_code == 500

        with open(meta_path, 'r') as f:
            meta = json.load(f)
        assert "nodeType" not in meta


def test_configure_node_atomic_persistence_success(client):
    with tempfile.TemporaryDirectory() as tmp_dir:
        meta_path = os.path.join(tmp_dir, app_module.META_FILE)
        with open(meta_path, "w") as f:
            json.dump({"nodeType": "lnd", "serverDomain": "us3.tunnelsats.com", "vpnPort": 23217}, f)

        with patch('app.DATA_DIR', tmp_dir), patch('app.SECURE_MODE', False), patch('app.K3S_MODE', False), \
                patch('app.container_ids_by_match', return_value=['cln1']), \
                patch('app.restart_container_by_pattern', return_value=True), \
                patch('app.resolve_node_config', return_value=(os.path.join(tmp_dir, 'config'), os.path.join(tmp_dir, 'config'))):
            with open(os.path.join(tmp_dir, 'config'), 'w') as f:
                f.write('')
            res = client.post('/api/local/configure-node', json={"nodeType": "cln"})
            assert res.status_code == 200

        with open(meta_path, 'r') as f:
            meta = json.load(f)
        assert meta["nodeType"] == "cln"


def test_configure_node_restores_previous_target_when_reconcile_fails(client):
    with tempfile.TemporaryDirectory() as tmp_dir:
        meta_path = os.path.join(tmp_dir, app_module.META_FILE)
        cln_path = os.path.join(tmp_dir, 'config')
        with open(meta_path, "w") as f:
            json.dump({"nodeType": "lnd", "serverDomain": "us3.tunnelsats.com", "vpnPort": 23217}, f)
        with open(cln_path, 'w') as f:
            f.write('')

        failed = {"state": {"target_impl": "lnd", "rules_synced": False}, "error": "switch failed"}
        restored = {"state": {"target_impl": "lnd", "rules_synced": True}}
        with patch('app.DATA_DIR', tmp_dir), patch('app.SECURE_MODE', False), patch('app.K3S_MODE', False), \
                patch('app.container_ids_by_match', return_value=['cln1']), \
                patch('app.restart_container_by_pattern', return_value=True), \
                patch('app.resolve_node_config', return_value=(cln_path, cln_path)), \
                patch('app.reconcile_target_and_wait', side_effect=[(False, failed), (True, restored)]) as reconcile:
            res = client.post('/api/local/configure-node', json={"nodeType": "cln"})

        assert res.status_code == 500
        assert json.loads(res.data)["error"] == "switch failed"
        with open(meta_path, 'r') as f:
            assert json.load(f)["nodeType"] == "lnd"
        assert [call.args[0] for call in reconcile.call_args_list] == ["cln", "lnd"]


def test_reconcile_target_and_wait_rejects_a_different_synced_target(mock_target_reconcile):
    original_reconcile = mock_target_reconcile[1]
    result = {
        "request_id": "request-1",
        "state": {"target_impl": "lnd", "rules_synced": True, "last_error": None},
    }
    with patch('app.uuid.uuid4', return_value='request-1'), \
            patch('app.ensure_reconcile_dirs'), \
            patch('app.atomic_write_text'), \
            patch('app.read_reconcile_result', return_value=result):
        ok, detail = original_reconcile("cln", timeout=1)

    assert ok is False
    assert "does not match requested target cln" in detail["error"]
