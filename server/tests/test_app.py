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

    def test_upload_config_renames_old_conf_but_not_imported_conf(self, client, data_dir):
        old_conf = data_dir / 'tunnelsats-old.conf'
        old_conf.write_text('[Interface]\nPrivateKey=old\n')
        imported_conf = data_dir / 'tunnelsats-imported.conf'
        imported_conf.write_text('[Interface]\nPrivateKey=old-imported\n')

        res = client.post('/api/local/upload-config', data={
            'config_text': '[Interface]\nPrivateKey = x\n[Peer]\nPublicKey = y\n'
        })
        assert res.status_code == 200
        assert os.path.exists(str(old_conf) + '.bak')
        assert not os.path.exists(old_conf)
        assert not os.path.exists(str(imported_conf) + '.bak')
        assert imported_conf.read_text() == '[Interface]\nPrivateKey = x\n[Peer]\nPublicKey = y\n'

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
            assert '# # bind-addr=already-commented' not in cln_content

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
