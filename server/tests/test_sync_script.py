from pathlib import Path
import shutil
import subprocess

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
SYNC_SCRIPT = REPO_ROOT / "scripts" / "sync.sh"


def test_node_hot_patch_uses_umbreld_and_updates_both_split_runtime_roles():
    source = SYNC_SCRIPT.read_text(encoding="utf-8")
    node_patch = source[source.index("run_node()") : source.index("run_monorepo()")]

    assert 'APP_ID="${APP_ID:-tunnelsats-community}"' in node_patch
    assert 'dev-patch/tunnelsats-community/docker-compose.yml' in node_patch
    assert 'apps.restart.mutate --appId "${APP_ID}"' in node_patch
    assert 'docker compose -f "${UMBREL_COMPOSE}" up' not in node_patch
    assert 'label=com.docker.compose.project=${APP_ID}' in node_patch
    assert 'label=com.docker.compose.service=tunnelsats-web' in node_patch
    assert 'label=com.docker.compose.service=tunnelsats-daemon' in node_patch
    assert 'docker restart "${WEB_CONTAINER}" "${DAEMON_CONTAINER}"' in node_patch
    assert r'SECURE_MODE=\${SECURE_MODE:-false}' in node_patch
    assert node_patch.index('dev-patch/tunnelsats-community/docker-compose.yml') < node_patch.index(
        'apps.restart.mutate --appId "${APP_ID}"'
    )
    assert node_patch.index('apps.restart.mutate --appId "${APP_ID}"') < node_patch.index(
        'docker cp /home/umbrel/dev-patch/server/. "${WEB_CONTAINER}"'
    )

    assert 'docker cp /home/umbrel/dev-patch/server/. "${WEB_CONTAINER}":/app/server/' in node_patch
    assert 'docker cp /home/umbrel/dev-patch/server/. "${DAEMON_CONTAINER}":/app/server/' in node_patch
    assert 'docker cp /home/umbrel/dev-patch/web/. "${WEB_CONTAINER}":/app/web/' in node_patch
    assert 'docker cp /home/umbrel/dev-patch/scripts/. "${WEB_CONTAINER}":/app/scripts/' in node_patch
    assert 'docker cp /home/umbrel/dev-patch/scripts/. "${DAEMON_CONTAINER}":/app/scripts/' in node_patch
    assert 'docker exec --user 0 "${WEB_CONTAINER}" chmod +x' in node_patch


def test_promote_reads_community_source_and_writes_official_tunnelsats():
    source = SYNC_SCRIPT.read_text(encoding="utf-8")
    promote_block = source[source.index("run_promote()") :]

    assert '${REPO_ROOT}/tunnelsats-community/umbrel-app.yml' in promote_block
    assert '--source-dir "${REPO_ROOT}/tunnelsats-community"' in promote_block
    assert '${UMBREL_APPS_DIR}/tunnelsats' in promote_block


def test_node_hot_patch_compiles_tailwind_css_before_rsync_staging():
    source = SYNC_SCRIPT.read_text(encoding="utf-8")
    node_patch = source[source.index("run_node()") : source.index("run_monorepo()")]

    assert 'node_modules/.bin/tailwindcss' in node_patch
    assert 'npm ci --ignore-scripts' in node_patch
    assert 'npm run build:css' in node_patch
    assert 'chmod -R a+rX "${REPO_ROOT}/web/dist"' in node_patch
    assert '--exclude="web/dist"' not in node_patch
    assert '--exclude="dist"' not in node_patch
    assert node_patch.index("npm run build:css") < node_patch.index("rsync -av --delete")


def test_node_hot_patch_rewrites_official_app_id_port_and_avoids_duplicate_secure_mode(tmp_path):
    source = SYNC_SCRIPT.read_text(encoding="utf-8")
    heredoc = source.split("cat << 'EOF'\n", 1)[1].split("\nEOF", 1)[0]

    # Extract the manifest/compose patching section before `run_sudo umbreld client`
    patch_block = heredoc.split("run_sudo umbreld client", 1)[0]

    dev_patch = tmp_path / "dev-patch"
    shutil.copytree(REPO_ROOT / "tunnelsats-community", dev_patch / "tunnelsats-community")
    app_data = tmp_path / "app-data" / "tunnelsats"
    app_data.mkdir(parents=True)

    # Execute the actual production patching commands from sync.sh against temp dirs
    harness_script = (
        "set -euo pipefail\n"
        'APP_ID="tunnelsats"\n'
        'SECURE_MODE_VAL="true"\n'
        "sudo() { \"$@\"; }\n"
        "docker() { if [ \"${1:-}\" = \"images\" ]; then echo 'tunnelsats/ts-umbrel-app:3.6.2'; fi; return 0; }\n"
        + patch_block.replace("/home/umbrel/dev-patch", str(dev_patch)).replace(
            "/home/umbrel/umbrel/app-data/${APP_ID}", str(app_data)
        )
    )
    res = subprocess.run(
        ["bash", "-c", harness_script],
        stdin=subprocess.DEVNULL,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0, res.stderr

    patched_compose = yaml.safe_load((app_data / "docker-compose.yml").read_text(encoding="utf-8"))
    patched_manifest = yaml.safe_load((app_data / "umbrel-app.yml").read_text(encoding="utf-8"))

    assert patched_manifest["id"] == "tunnelsats"
    assert patched_manifest["port"] == 9739

    proxy_env = patched_compose["services"]["app_proxy"]["environment"]
    assert proxy_env["APP_HOST"] == "tunnelsats_tunnelsats-web_1"
    assert str(proxy_env["PROXY_PORT"]) == "9739"
    assert proxy_env["MANAGER_IP"] == "tunnelsats_tunnelsats-web_1"

    web_env_list = patched_compose["services"]["tunnelsats-web"]["environment"]
    daemon_env_list = patched_compose["services"]["tunnelsats-daemon"]["environment"]
    assert "UMBREL_APP_ID=tunnelsats" in web_env_list
    assert "UMBREL_APP_ID=tunnelsats" in daemon_env_list
    assert "UMBREL_APP_ID=tunnelsats-community" not in web_env_list
    assert "UMBREL_APP_ID=tunnelsats-community" not in daemon_env_list
    assert web_env_list.count("SECURE_MODE=true") == 1
    assert daemon_env_list.count("SECURE_MODE=true") == 1
