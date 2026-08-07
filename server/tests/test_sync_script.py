from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SYNC_SCRIPT = REPO_ROOT / "scripts" / "sync.sh"


def test_node_hot_patch_uses_umbreld_for_authenticated_proxy_compose():
    source = SYNC_SCRIPT.read_text(encoding="utf-8")
    node_patch = source[source.index("run_node()") : source.index("run_monorepo()")]

    assert 'dev-patch/tunnelsats/docker-compose.yml' in node_patch
    assert 'apps.restart.mutate --appId "${APP_ID}"' in node_patch
    assert 'docker compose -f "${UMBREL_COMPOSE}" up' not in node_patch
    assert 'label=com.docker.compose.service=tunnelsats-web' in node_patch
    assert 'label=com.docker.compose.service=tunnelsats-daemon' in node_patch
    assert 'docker restart "${WEB_CONTAINER}" "${DAEMON_CONTAINER}"' in node_patch
    assert r'SECURE_MODE=\${SECURE_MODE:-false}' in node_patch
    assert node_patch.index('dev-patch/tunnelsats/docker-compose.yml') < node_patch.index(
        'apps.restart.mutate --appId "${APP_ID}"'
    )
    assert node_patch.index('apps.restart.mutate --appId "${APP_ID}"') < node_patch.index(
        'docker cp /home/umbrel/dev-patch/server/. "${WEB_CONTAINER}"'
    )


def test_node_hot_patch_updates_both_split_runtime_roles():
    source = SYNC_SCRIPT.read_text(encoding="utf-8")
    node_patch = source[source.index("run_node()") : source.index("run_monorepo()")]

    assert 'docker cp /home/umbrel/dev-patch/server/. "${WEB_CONTAINER}":/app/server/' in node_patch
    assert 'docker cp /home/umbrel/dev-patch/server/. "${DAEMON_CONTAINER}":/app/server/' in node_patch
    assert 'docker cp /home/umbrel/dev-patch/web/. "${WEB_CONTAINER}":/app/web/' in node_patch
    assert 'docker cp /home/umbrel/dev-patch/scripts/. "${WEB_CONTAINER}":/app/scripts/' in node_patch
    assert 'docker cp /home/umbrel/dev-patch/scripts/. "${DAEMON_CONTAINER}":/app/scripts/' in node_patch
    assert 'docker exec --user 0 "${WEB_CONTAINER}" chmod +x' in node_patch
