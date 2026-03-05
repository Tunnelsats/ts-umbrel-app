import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest
import json
import stat
import tempfile
from unittest.mock import patch, MagicMock
from app import app

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
