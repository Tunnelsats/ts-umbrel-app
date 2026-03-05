import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

if "requests" not in sys.modules:
    requests_stub = types.ModuleType("requests")

    class RequestException(Exception):
        pass

    def _requests_unavailable(*args, **kwargs):
        raise RequestException("requests module is unavailable in unit-test environment")

    requests_stub.RequestException = RequestException
    requests_stub.get = _requests_unavailable
    requests_stub.post = _requests_unavailable
    sys.modules["requests"] = requests_stub

import server.app as app_module


DATAPLANE_STATUS = {
    "dataplane_mode": "docker-full-parity",
    "target_container": "",
    "target_ip": "",
    "forwarding_port": "",
    "rules_synced": False,
    "last_reconcile_at": "",
    "last_error": None,
    "docker_network": {
        "name": "docker-tunnelsats",
        "subnet": "10.9.9.0/25",
        "bridge": "",
    },
}


class AppTestCase(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def _get_status(self, remote_addr="127.0.0.1", headers=None):
        with patch("server.app.subprocess.check_output", side_effect=Exception("wg unavailable")):
            with patch("server.app.container_ip_by_match", return_value=""):
                with patch("server.app.read_dataplane_state", return_value=DATAPLANE_STATUS):
                    return self.client.get(
                        "/api/local/status",
                        headers=headers or {},
                        environ_base={"REMOTE_ADDR": remote_addr},
                    )

    def test_local_api_rejects_public_remote_ip(self):
        response = self._get_status(remote_addr="8.8.8.8")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json(), {"error": "Forbidden"})

    def test_local_api_allows_private_remote_ip(self):
        response = self._get_status(remote_addr="192.168.50.10")
        self.assertEqual(response.status_code, 200)

    def test_proxyfix_uses_forwarded_ip_for_local_restriction(self):
        response = self._get_status(
            remote_addr="127.0.0.1",
            headers={"X-Forwarded-For": "8.8.8.8"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json(), {"error": "Forbidden"})

    def test_upload_config_renames_existing_conf_to_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            old_config = os.path.join(temp_dir, "tunnelsats-old.conf")
            with open(old_config, "w", encoding="utf-8") as old_fp:
                old_fp.write("old-config")

            imported_config = os.path.join(temp_dir, "tunnelsats-imported.conf")
            with open(imported_config, "w", encoding="utf-8") as imported_fp:
                imported_fp.write("old-imported")

            payload = {"config_text": "[Interface]\nPrivateKey=x\n[Peer]\nPublicKey=y\n"}
            with patch.object(app_module, "DATA_DIR", temp_dir):
                response = self.client.post(
                    "/api/local/upload-config",
                    data=payload,
                    environ_base={"REMOTE_ADDR": "127.0.0.1"},
                )

            self.assertEqual(response.status_code, 200)
            self.assertTrue(os.path.exists(os.path.join(temp_dir, "tunnelsats-old.conf.bak")))
            self.assertFalse(os.path.exists(old_config))
            self.assertFalse(os.path.exists(f"{imported_config}.bak"))

            with open(imported_config, "r", encoding="utf-8") as imported_fp:
                self.assertEqual(imported_fp.read(), payload["config_text"])

    def test_local_status_includes_manifest_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = os.path.join(temp_dir, "umbrel-app.yml")
            with open(manifest_path, "w", encoding="utf-8") as manifest_fp:
                manifest_fp.write('version: "9.1.2"\n')

            with patch.object(app_module, "APP_MANIFEST_PATH", manifest_path):
                response = self._get_status(remote_addr="127.0.0.1")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json().get("version"), "v9.1.2")

    def test_restore_node_comments_tunnelsats_lines(self):
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
                    response = self.client.post(
                        "/api/local/restore-node",
                        environ_base={"REMOTE_ADDR": "127.0.0.1"},
                    )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json(), {"lnd": True, "cln": True})

            with open(lnd_path, "r", encoding="utf-8") as lnd_fp:
                lnd_content = lnd_fp.read()
            self.assertIn("# externalhosts=vpn.tunnelsats.com:9735\n", lnd_content)
            self.assertIn("# tor.skip-proxy-for-clearnet-targets=true\n", lnd_content)
            self.assertNotIn("# # externalhosts=already-commented", lnd_content)

            with open(cln_path, "r", encoding="utf-8") as cln_fp:
                cln_content = cln_fp.read()
            self.assertIn("# bind-addr=0.0.0.0:9735\n", cln_content)
            self.assertIn("# announce-addr=vpn.tunnelsats.com:9735\n", cln_content)
            self.assertIn("# always-use-proxy=false\n", cln_content)
            self.assertNotIn("# # bind-addr=already-commented", cln_content)


if __name__ == "__main__":
    unittest.main()
