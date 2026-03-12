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
    "fullConfig": (
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
    mock_resp.headers = {'Content-Type': 'application/json'}
    return mock_resp


class TestClaimSavesConfig:
    """Test that claim_subscription correctly intercepts and saves the config."""

    @patch('app.requests.post', side_effect=_mock_claim_post)
    def test_claim_saves_conf_file_from_fullConfig(self, mock_post, client, data_dir):
        """The .conf file must be written from the 'fullConfig' field."""
        res = client.post('/api/subscription/claim',
                          json={"paymentHash": "test-hash-123", "referralCode": None},
                          content_type='application/json')
        assert res.status_code == 200

        # Check that a .conf file was written
        conf_files = [f for f in os.listdir(data_dir) if f.endswith('.conf')]
        assert len(conf_files) == 1
        assert 'tunnelsats' in conf_files[0]

        # Verify content matches fullConfig
        with open(os.path.join(data_dir, conf_files[0])) as f:
            content = f.read()
        assert '[Interface]' in content
        assert 'clientPrivateKeyBase64==' in content
        assert '# Port Forwarding: 35825' in content

    @patch('app.requests.post', side_effect=_mock_claim_post)
    def test_claim_saves_metadata_json(self, mock_post, client, data_dir):
        """A tunnelsats-meta.json must be created with fields from the response."""
        res = client.post('/api/subscription/claim',
                          json={"paymentHash": "test-hash-123", "referralCode": None},
                          content_type='application/json')
        assert res.status_code == 200

        meta_path = os.path.join(data_dir, 'tunnelsats-meta.json')
        assert os.path.exists(meta_path), "tunnelsats-meta.json not created"

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
        meta_path = os.path.join(data_dir, 'tunnelsats-meta.json')

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
        meta_path = os.path.join(data_dir, 'tunnelsats-meta.json')
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
        meta_path = os.path.join(data_dir, 'tunnelsats-meta.json')
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
        meta_path = os.path.join(data_dir, 'tunnelsats-meta.json')
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
        meta_path = os.path.join(data_dir, 'tunnelsats-meta.json')
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

        meta_path = data_dir / 'tunnelsats-meta.json'
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

    def test_configure_node_lnd_injects_externalhosts_from_metadata(self, client):
        with tempfile.TemporaryDirectory() as tmp_dir:
            meta_path = os.path.join(tmp_dir, 'tunnelsats-meta.json')
            lnd_path = os.path.join(tmp_dir, 'tunnelsats.conf')

            with open(meta_path, 'w') as f:
                json.dump({'vpnPort': 35825, 'serverDomain': 'de2.tunnelsats.com'}, f)

            with open(lnd_path, 'w') as f:
                f.write('[Application Options]\nfoo=bar\n')

            with patch('app.DATA_DIR', tmp_dir):
                with patch('app.LND_TUNNELSATS_CONF_PATH', lnd_path):
                    with patch('app.restart_container_by_pattern', return_value=True) as mock_restart:
                        res = client.post('/api/local/configure-node', json={'nodeType': 'lnd'})

            assert res.status_code == 200
            payload = json.loads(res.data)
            assert payload['success'] is True
            assert payload['lnd'] is True
            assert payload['cln'] is False
            assert payload['port'] == 35825
            assert payload['dns'] == 'de2.tunnelsats.com'
            mock_restart.assert_called_once_with(r'(^|[_-])lnd([_-]|$)')

            with open(lnd_path, 'r') as f:
                lnd_content = f.read()
            assert 'externalhosts=de2.tunnelsats.com:35825' in lnd_content

    def test_configure_node_lnd_creates_application_options_section_when_missing(self, client):
        with tempfile.TemporaryDirectory() as tmp_dir:
            meta_path = os.path.join(tmp_dir, 'tunnelsats-meta.json')
            lnd_path = os.path.join(tmp_dir, 'tunnelsats.conf')

            with open(meta_path, 'w') as f:
                json.dump({'vpnPort': 35825, 'serverDomain': 'de2.tunnelsats.com'}, f)

            with open(lnd_path, 'w') as f:
                f.write('foo=bar\n')

            with patch('app.DATA_DIR', tmp_dir):
                with patch('app.LND_TUNNELSATS_CONF_PATH', lnd_path):
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

    def test_configure_node_lnd_creates_config_file_when_missing(self, client):
        with tempfile.TemporaryDirectory() as tmp_dir:
            meta_path = os.path.join(tmp_dir, 'tunnelsats-meta.json')
            lnd_path = os.path.join(tmp_dir, 'tunnelsats.conf')

            with open(meta_path, 'w') as f:
                json.dump({'vpnPort': 35825, 'serverDomain': 'de2.tunnelsats.com'}, f)

            with patch('app.DATA_DIR', tmp_dir):
                with patch('app.LND_TUNNELSATS_CONF_PATH', lnd_path):
                    with patch('app.restart_container_by_pattern', return_value=True) as mock_restart:
                        res = client.post('/api/local/configure-node', json={'nodeType': 'lnd'})

            assert res.status_code == 200
            payload = json.loads(res.data)
            assert payload['success'] is True
            assert payload['lnd'] is True
            assert os.path.exists(lnd_path)
            mock_restart.assert_called_once_with(r'(^|[_-])lnd([_-]|$)')

            with open(lnd_path, 'r') as f:
                lnd_content = f.read()
            assert '[Application Options]\n' in lnd_content
            assert 'externalhosts=de2.tunnelsats.com:35825\n' in lnd_content

    def test_configure_node_cln_injects_expected_lines_from_metadata(self, client):
        with tempfile.TemporaryDirectory() as tmp_dir:
            meta_path = os.path.join(tmp_dir, 'tunnelsats-meta.json')
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

    def test_configure_node_cln_dedupes_commented_and_active_lines(self, client):
        with tempfile.TemporaryDirectory() as tmp_dir:
            meta_path = os.path.join(tmp_dir, 'tunnelsats-meta.json')
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

    def test_configure_node_cln_leaves_file_unchanged_when_atomic_write_fails(self, client):
        with tempfile.TemporaryDirectory() as tmp_dir:
            meta_path = os.path.join(tmp_dir, 'tunnelsats-meta.json')
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

    def test_configure_node_lnd_skips_restart_when_config_already_matches(self, client):
        with tempfile.TemporaryDirectory() as tmp_dir:
            meta_path = os.path.join(tmp_dir, 'tunnelsats-meta.json')
            lnd_path = os.path.join(tmp_dir, 'tunnelsats.conf')

            with open(meta_path, 'w') as f:
                json.dump({'vpnPort': 35825, 'serverDomain': 'de2.tunnelsats.com'}, f)

            with open(lnd_path, 'w') as f:
                f.write('[Application Options]\nexternalhosts=de2.tunnelsats.com:35825\n')

            with patch('app.DATA_DIR', tmp_dir):
                with patch('app.LND_TUNNELSATS_CONF_PATH', lnd_path):
                    with patch('app.restart_container_by_pattern', return_value=True) as mock_restart:
                        res = client.post('/api/local/configure-node', json={'nodeType': 'lnd'})

            assert res.status_code == 200
            payload = json.loads(res.data)
            assert payload['success'] is True
            assert payload['lnd'] is True
            assert payload['lnd_changed'] is False
            mock_restart.assert_not_called()

    def test_configure_node_lnd_returns_500_when_restart_fails(self, client):
        with tempfile.TemporaryDirectory() as tmp_dir:
            meta_path = os.path.join(tmp_dir, 'tunnelsats-meta.json')
            lnd_path = os.path.join(tmp_dir, 'tunnelsats.conf')

            with open(meta_path, 'w') as f:
                json.dump({'vpnPort': 35825, 'serverDomain': 'de2.tunnelsats.com'}, f)

            with open(lnd_path, 'w') as f:
                f.write('[Application Options]\nfoo=bar\n')

            with patch('app.DATA_DIR', tmp_dir):
                with patch('app.LND_TUNNELSATS_CONF_PATH', lnd_path):
                    with patch('app.restart_container_by_pattern', return_value=False):
                        res = client.post('/api/local/configure-node', json={'nodeType': 'lnd'})

            assert res.status_code == 500
            payload = json.loads(res.data)
            assert payload['success'] is False
            assert payload['error'] == 'Failed to restart LND container.'

            with open(meta_path, 'r') as f:
                updated_meta = json.load(f)
            assert updated_meta['lndRestartPending'] is True

    def test_configure_node_lnd_retries_restart_when_pending_flag_set(self, client):
        with tempfile.TemporaryDirectory() as tmp_dir:
            meta_path = os.path.join(tmp_dir, 'tunnelsats-meta.json')
            lnd_path = os.path.join(tmp_dir, 'tunnelsats.conf')

            with open(meta_path, 'w') as f:
                json.dump({'vpnPort': 35825, 'serverDomain': 'de2.tunnelsats.com', 'lndRestartPending': True}, f)

            with open(lnd_path, 'w') as f:
                f.write('[Application Options]\nexternalhosts=de2.tunnelsats.com:35825\n')

            with patch('app.DATA_DIR', tmp_dir):
                with patch('app.LND_TUNNELSATS_CONF_PATH', lnd_path):
                    with patch('app.restart_container_by_pattern', return_value=True) as mock_restart:
                        res = client.post('/api/local/configure-node', json={'nodeType': 'lnd'})

            assert res.status_code == 200
            payload = json.loads(res.data)
            assert payload['success'] is True
            assert payload['lnd_changed'] is False
            mock_restart.assert_called_once_with(r'(^|[_-])lnd([_-]|$)')

            with open(meta_path, 'r') as f:
                updated_meta = json.load(f)
            assert 'lndRestartPending' not in updated_meta

    def test_configure_node_cln_returns_500_when_restart_fails(self, client):
        with tempfile.TemporaryDirectory() as tmp_dir:
            meta_path = os.path.join(tmp_dir, 'tunnelsats-meta.json')
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

    def test_restore_node_comments_expected_lines(self, client):
        with tempfile.TemporaryDirectory() as tmp_dir:
            lnd_path = os.path.join(tmp_dir, 'tunnelsats.conf')
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

            with patch('app.LND_TUNNELSATS_CONF_PATH', lnd_path):
                with patch('app.CLN_CONFIG_PATH', cln_path):
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

    def test_restore_node_reports_processed_without_changes(self, client):
        with tempfile.TemporaryDirectory() as tmp_dir:
            lnd_path = os.path.join(tmp_dir, 'tunnelsats.conf')
            cln_path = os.path.join(tmp_dir, 'config')

            with open(lnd_path, 'w') as f:
                f.write('[Application Options]\nfoo=bar\n')

            with open(cln_path, 'w') as f:
                f.write('foo=bar\n')

            with patch('app.LND_TUNNELSATS_CONF_PATH', lnd_path):
                with patch('app.CLN_CONFIG_PATH', cln_path):
                    res = client.post('/api/local/restore-node')

            assert res.status_code == 200
            payload = json.loads(res.data)
            assert payload['lnd'] is True
            assert payload['cln'] is True
            assert payload['lnd_changed'] is False
            assert payload['cln_changed'] is False

    def test_restore_node_route_declared_once(self):
        rules = [rule for rule in app_module.app.url_map.iter_rules() if rule.rule == '/api/local/restore-node']
        assert len(rules) == 1
