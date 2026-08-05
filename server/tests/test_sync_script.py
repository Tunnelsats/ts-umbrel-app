from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SYNC_SCRIPT = REPO_ROOT / "scripts" / "sync.sh"


def test_node_hot_patch_uses_umbreld_for_authenticated_proxy_compose():
    source = SYNC_SCRIPT.read_text(encoding="utf-8")
    node_patch = source[source.index("run_node()") : source.index("run_monorepo()")]

    assert 'dev-patch/tunnelsats/docker-compose.yml' in node_patch
    assert 'apps.restart.mutate --appId "${APP_ID}"' in node_patch
    assert 'docker compose -f "${UMBREL_COMPOSE}" up' not in node_patch
    assert 'docker restart "${APP_ID}"' in node_patch
    assert r'SECURE_MODE=\${SECURE_MODE:-false}' in node_patch
    assert node_patch.index('dev-patch/tunnelsats/docker-compose.yml') < node_patch.index(
        'apps.restart.mutate --appId "${APP_ID}"'
    )
    assert node_patch.index('apps.restart.mutate --appId "${APP_ID}"') < node_patch.index(
        'docker cp /home/umbrel/dev-patch/server/.'
    )
