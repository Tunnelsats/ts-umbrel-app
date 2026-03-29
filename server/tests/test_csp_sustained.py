import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest
from app import app

@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client

def parse_csp(csp):
    """Robust CSP parser that handles multiple directives and source lists."""
    return {
        d.split()[0].strip().lower(): set(d.split()[1:])
        for d in csp.split(';') if d.strip()
    }

def test_csp_permits_threejs_blobs(client):
    """Test that script-src and img-src include 'blob:' for Three.js requirement."""
    res = client.get('/')
    csp = res.headers.get('Content-Security-Policy')
    directives = parse_csp(csp)
    
    assert 'blob:' in directives.get('script-src', set())
    assert "'unsafe-eval'" in directives.get('script-src', set())
    assert 'blob:' in directives.get('img-src', set())

def test_csp_permits_inline_styles_for_globe_layout(client):
    """Test that style-src includes 'unsafe-inline' for Globe.gl/Three.js layout injection."""
    res = client.get('/')
    csp = res.headers.get('Content-Security-Policy')
    directives = parse_csp(csp)
    
    assert "'unsafe-inline'" in directives.get('style-src', set())

def test_framing_permits_umbrel_dashboard(client):
    """Test that X-Frame-Options allows SAMEORIGIN for Umbrel dashboard integration."""
    res = client.get('/')
    assert res.headers.get('X-Frame-Options') == 'SAMEORIGIN'
    
    csp = res.headers.get('Content-Security-Policy')
    directives = parse_csp(csp)
    assert "'self'" in directives.get('frame-ancestors', set())

def test_sustained_fix_prevents_csp_duplication(client):
    """
    Test that the application provides exactly ONE Content-Security-Policy header.
    Multiple CSP headers are combined via intersection by browsers, so 
    we must ensure we aren't leaking a restrictive default header alongside our own.
    """
    res = client.get('/')
    # In Werkzeug/Flask test client, get_all() returns a list of header values.
    csp_headers = res.headers.getlist('Content-Security-Policy')
    assert len(csp_headers) == 1, f"Found multiple CSP headers: {csp_headers}"
