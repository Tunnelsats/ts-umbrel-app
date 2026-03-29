import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest
import json
import stat
import tempfile
from unittest.mock import patch, MagicMock
from app import app
import app as app_module

# --- Fixtures ---

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

# --- Existing Tests ---

def test_status_endpoint(client):
    res = client.get('/api/local/status')
    assert res.status_code == 200
    data = json.loads(res.data)
    assert 'wg_status' in data


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

    @patch('app.subprocess.run')
    def test_upload_config_saves_tunnelsats_conf_and_meta(self, mock_run, client, data_dir):
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
            "# Valid Until: 2026-04-05T10:30:00.000Z\n"
            "[Interface]\n"
            "PrivateKey = clientPrivateKeyBase64==\n"
            "\n"
            "[Peer]\n"
            "PublicKey = serverPublicKeyBase64==\n"
            "AllowedIPs = 0.0.0.0/0\n"
            "Endpoint = de2.tunnelsats.com:51820\n"
        )
        expected_saved_config = config_text + "PersistentKeepalive = 25\n"

        res = client.post('/api/local/upload-config', json={"config": config_text})
        assert res.status_code == 200
        payload = json.loads(res.data)
        assert payload["success"] is True
        assert payload["message"] == "Configuration saved and parsed."
        assert payload["meta"]["serverId"] == "de2"
        assert payload["meta"]["wgPublicKey"] == "derivedPubKeyBase64=="
        assert payload["meta"]["expiresAt"] == "2026-04-05T10:30:00.000Z"
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
        assert meta["expiresAt"] == "2026-04-05T10:30:00.000Z"
        assert meta["vpnPort"] == 35825

        mock_run.assert_called_once_with(
            ["wg", "pubkey"],
            input="clientPrivateKeyBase64==",
            text=True,
            capture_output=True,
            check=True,
            timeout=5,
        )

    @patch('app.subprocess.run')
    def test_upload_config_does_not_duplicate_existing_keepalive(self, mock_run, client, data_dir):
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
        assert data['rules_synced'] is False
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
            mock_restart.assert_called_once_with(r'^lightning[_-]lnd[_-]\d+$', is_lnd=True)
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
            if pattern == r"^lightning[_-]app[_-]\d+$":
                return "middleware_id_long_identifier"
            if pattern == r"^lightning[_-]lnd[_-]\d+$":
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

        # Verify verbose logging with truncated IDs (12 chars strictly)
        # middleware_id_long_identifier -> middleware_i (12 chars: m-i-d-d-l-e-w-a-r-e-_-i)
        # daemon_id_long_identifier -> daemon_id_lo (12 chars: d-a-e-m-o-n-_-i-d-_-l-o)
        mock_logger.info.assert_any_call("Found LND middleware container (ID: middleware_i). Restarting...")
        mock_logger.info.assert_any_call("Found LND daemon container (ID: daemon_id_lo). Restarting...")

    @patch('app.container_id_by_match')
    @patch('app.docker_api_post')
    @patch('app.app.logger')
    def test_restart_container_by_pattern_sequential_middleware_failure(self, mock_logger, mock_post, mock_id, client):
        # Mocking ID for middleware
        mock_id.return_value = "middleware_id"
        mock_post.return_value = False # Simulate failure

        from app import restart_container_by_pattern
        result = restart_container_by_pattern(r"(^|[_-])lnd([_-]|$)", is_lnd=True)

        assert result is False
        mock_logger.error.assert_called_with("LND middleware restart failed. Aborting sequential restart.")
        # Ensure it didn't proceed to sleep or daemon restart (mock_post only called once)
        assert mock_post.call_count == 1

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
            mock_restart.assert_called_once_with(r'^lightning[_-]lnd[_-]\d+$', is_lnd=True)

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
                        res = client.post('/api/local/configure-node', json={'nodeType': 'cln'})

            assert res.status_code == 200
            payload = json.loads(res.data)
            assert payload['success'] is True
            assert payload['lnd'] is False
            assert payload['cln'] is True
            assert payload['port'] == 35825
            assert payload['dns'] == 'de2.tunnelsats.com'
            mock_restart.assert_called_once_with(r'(^|[_-])(core-lightning|clightning|lightningd)([_-]|$)')

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
                        res = client.post('/api/local/configure-node', json={'nodeType': 'cln'})

            assert res.status_code == 200
            with open(cln_path, 'r') as f:
                cln_content = f.read()

            assert cln_content.count('announce-addr=de2.tunnelsats.com:35825\n') == 1
            assert cln_content.count('always-use-proxy=false\n') == 1
            assert cln_content.count('bind-addr=0.0.0.0:9736\n') == 1
            assert 'old.tunnelsats.com' not in cln_content

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
                json.dump({'vpnPort': 35825, 'serverDomain': 'de2.tunnelsats.com'}, f)
            with open(cln_path, 'w') as f:
                f.write(original_content)

            with patch('app.DATA_DIR', tmp_dir):
                with patch('app.CLN_CONFIG_PATH', cln_path):
                    with patch('app.os.replace', side_effect=OSError('replace failed')):
                        with patch('app.restart_container_by_pattern', return_value=True) as mock_restart:
                            res = client.post('/api/local/configure-node', json={'nodeType': 'cln'})

            assert res.status_code == 500
            payload = json.loads(res.data)
            assert payload['success'] is False
            assert payload['error'] == 'Failed to modify CLN config.'
            mock_restart.assert_not_called()

            with open(cln_path, 'r') as f:
                assert f.read() == original_content

    @patch('app.container_ids_by_match', return_value=['mock'])
    def test_configure_node_lnd_forces_restart_even_when_config_matches(self, mock_ids, client):
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
            assert payload['lnd_changed'] is False
            mock_restart.assert_called_once_with(r'^lightning[_-]lnd[_-]\d+$', is_lnd=True)

    @patch('app.container_ids_by_match', return_value=['mock'])
    def test_configure_node_lnd_returns_500_when_restart_fails(self, mock_ids, client):
        with tempfile.TemporaryDirectory() as tmp_dir:
            meta_path = os.path.join(tmp_dir, app_module.META_FILE)
            lnd_path = os.path.join(tmp_dir, 'tunnelsats.conf')

            with open(meta_path, 'w') as f:
                json.dump({'vpnPort': 35825, 'serverDomain': 'de2.tunnelsats.com'}, f)

            with open(lnd_path, 'w') as f:
                f.write('[Application Options]\nfoo=bar\n')

            with patch('app.DATA_DIR', tmp_dir):
                with patch('app.LND_CONFIG_PATH', lnd_path):
                    with patch('app.restart_container_by_pattern', return_value=False):
                        res = client.post('/api/local/configure-node', json={'nodeType': 'lnd'})

            assert res.status_code == 500
            payload = json.loads(res.data)
            assert payload['success'] is False
            assert payload['error'] == 'Failed to restart LND container.'

            with open(meta_path, 'r') as f:
                updated_meta = json.load(f)
            assert updated_meta['lndRestartPending'] is True

    @patch('app.container_ids_by_match', return_value=['mock'])
    def test_configure_node_lnd_retries_restart_when_pending_flag_set(self, mock_ids, client):
        with tempfile.TemporaryDirectory() as tmp_dir:
            meta_path = os.path.join(tmp_dir, app_module.META_FILE)
            lnd_path = os.path.join(tmp_dir, 'tunnelsats.conf')

            with open(meta_path, 'w') as f:
                json.dump({'vpnPort': 35825, 'serverDomain': 'de2.tunnelsats.com', 'lndRestartPending': True}, f)

            with open(lnd_path, 'w') as f:
                f.write('[Application Options]\nexternalhosts=de2.tunnelsats.com:35825\n')

            with patch('app.DATA_DIR', tmp_dir):
                with patch('app.LND_CONFIG_PATH', lnd_path):
                    with patch('app.restart_container_by_pattern', return_value=True) as mock_restart:
                        res = client.post('/api/local/configure-node', json={'nodeType': 'lnd'})

            assert res.status_code == 200
            payload = json.loads(res.data)
            assert payload['success'] is True
            assert payload['lnd_changed'] is False
            mock_restart.assert_called_once_with(r'^lightning[_-]lnd[_-]\d+$', is_lnd=True)

            with open(meta_path, 'r') as f:
                updated_meta = json.load(f)
            assert 'lndRestartPending' not in updated_meta

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
            mock_restart.assert_any_call(r'^lightning[_-]lnd[_-]\d+$', is_lnd=True)
            mock_restart.assert_any_call(r'(^|[_-])(core-lightning|clightning|lightningd)([_-]|$)')

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
        mock_subprocess.return_value = b"interface: tunnelsatsv2\n  public key: pubKey123\n  private key: (hidden)\n  listening port: 51820\n"
        
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
