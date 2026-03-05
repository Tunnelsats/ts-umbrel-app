import os
import tempfile
from unittest.mock import patch

import pytest

import server.app as app_module


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as test_client:
        yield test_client


def get_status(client, remote_addr="127.0.0.1", headers=None):
    with patch("server.app.subprocess.check_output", side_effect=Exception("wg unavailable")):
        return client.get(
            "/api/local/status",
            headers=headers or {},
            environ_base={"REMOTE_ADDR": remote_addr},
        )


def test_local_api_rejects_public_remote_ip(client):
    response = get_status(client, remote_addr="8.8.8.8")
    assert response.status_code == 403
    assert response.get_json() == {"error": "Forbidden"}


def test_local_api_allows_private_remote_ip(client):
    response = get_status(client, remote_addr="192.168.50.10")
    assert response.status_code == 200


def test_proxyfix_uses_forwarded_ip_for_local_restriction(client):
    response = get_status(
        client,
        remote_addr="127.0.0.1",
        headers={"X-Forwarded-For": "8.8.8.8"},
    )
    assert response.status_code == 403
    assert response.get_json() == {"error": "Forbidden"}


def test_upload_config_renames_existing_conf_to_backup(client):
    with tempfile.TemporaryDirectory() as temp_dir:
        old_config = os.path.join(temp_dir, "tunnelsats-old.conf")
        with open(old_config, "w", encoding="utf-8") as old_fp:
            old_fp.write("old-config")

        imported_config = os.path.join(temp_dir, "tunnelsats-imported.conf")
        with open(imported_config, "w", encoding="utf-8") as imported_fp:
            imported_fp.write("old-imported")

        payload = {"config_text": "[Interface]\nPrivateKey=x\n[Peer]\nPublicKey=y\n"}
        with patch.object(app_module, "DATA_DIR", temp_dir):
            response = client.post(
                "/api/local/upload-config",
                data=payload,
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )

        assert response.status_code == 200
        assert os.path.exists(os.path.join(temp_dir, "tunnelsats-old.conf.bak"))
        assert not os.path.exists(old_config)
        assert not os.path.exists(f"{imported_config}.bak")

        with open(imported_config, "r", encoding="utf-8") as imported_fp:
            assert imported_fp.read() == payload["config_text"]


def test_local_status_includes_manifest_version(client):
    with tempfile.TemporaryDirectory() as temp_dir:
        manifest_path = os.path.join(temp_dir, "umbrel-app.yml")
        with open(manifest_path, "w", encoding="utf-8") as manifest_fp:
            manifest_fp.write('version: "9.1.2"\n')

        with patch.object(app_module, "APP_MANIFEST_PATH", manifest_path):
            response = get_status(client, remote_addr="127.0.0.1")

        assert response.status_code == 200
        assert response.get_json().get("version") == "v9.1.2"


def test_restore_node_comments_tunnelsats_lines(client):
    with tempfile.TemporaryDirectory() as temp_dir:
        lnd_path = os.path.join(temp_dir, "tunnelsats.conf")
        with open(lnd_path, "w", encoding="utf-8") as lnd_fp:
            lnd_fp.write(
                "[Application Options]\n"
                "externalhosts=vpn.tunnelsats.com:9735\n"
                "# externalhosts=already-commented\n"
                "tor.skip-proxy-for-clearnet-targets=true\n"
            )

        cln_path = os.path.join(temp_dir, "config")
        with open(cln_path, "w", encoding="utf-8") as cln_fp:
            cln_fp.write(
                "foo=bar\n"
                "bind-addr=0.0.0.0:9735\n"
                "announce-addr=vpn.tunnelsats.com:9735\n"
                "always-use-proxy=false\n"
                "# bind-addr=already-commented\n"
            )

        with patch.object(app_module, "LND_TUNNELSATS_CONF_PATH", lnd_path):
            with patch.object(app_module, "CLN_CONFIG_PATH", cln_path):
                response = client.post(
                    "/api/local/restore-node",
                    environ_base={"REMOTE_ADDR": "127.0.0.1"},
                )

        assert response.status_code == 200
        assert response.get_json() == {"lnd": True, "cln": True}

        with open(lnd_path, "r", encoding="utf-8") as lnd_fp:
            lnd_content = lnd_fp.read()
        assert "# externalhosts=vpn.tunnelsats.com:9735\n" in lnd_content
        assert "# tor.skip-proxy-for-clearnet-targets=true\n" in lnd_content
        assert "# # externalhosts=already-commented" not in lnd_content

        with open(cln_path, "r", encoding="utf-8") as cln_fp:
            cln_content = cln_fp.read()
        assert "# bind-addr=0.0.0.0:9735\n" in cln_content
        assert "# announce-addr=vpn.tunnelsats.com:9735\n" in cln_content
        assert "# always-use-proxy=false\n" in cln_content
        assert "# # bind-addr=already-commented" not in cln_content


def test_restore_node_route_declared_once():
    rules = [rule for rule in app_module.app.url_map.iter_rules() if rule.rule == "/api/local/restore-node"]
    assert len(rules) == 1
