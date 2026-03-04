import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest
from app import app
import json

@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client

def test_status_endpoint(client):
    res = client.get('/api/local/status')
    assert res.status_code == 200
    data = json.loads(res.data)
    assert 'wg_status' in data

def test_proxy_fix(client):
    # Test that when X-Forwarded-For is set, we bypass the local subnet restriction
    # We simulate a request from an external IP coming through the Umbrel proxy (127.0.0.1)
    res = client.get('/api/local/status', environ_base={'REMOTE_ADDR': '127.0.0.1', 'HTTP_X_FORWARDED_FOR': '192.168.1.50'})
    assert res.status_code == 200

# Additional tests can be built here for configure-node, upload-config etc.
