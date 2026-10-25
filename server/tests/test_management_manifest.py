from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO_ROOT / "tunnelsats" / "docker-compose.yml"
CI_COMPOSE_PATH = REPO_ROOT / "docker-compose.ci.yml"
MANIFEST_PATH = REPO_ROOT / "tunnelsats" / "umbrel-app.yml"
APP_PATH = REPO_ROOT / "server" / "app.py"
SECURITY_PATH = REPO_ROOT / "server" / "security.py"
TEST_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "test.yml"


def load_yaml(path):
    with path.open(encoding="utf-8") as source:
        return yaml.safe_load(source)


def environment_map(service):
    environment = service.get("environment", {})
    if isinstance(environment, dict):
        return environment
    result = {}
    for entry in environment:
        key, separator, value = entry.partition("=")
        result[key] = value if separator else None
    return result


def test_split_runtime_compose_and_proxy_contract():
    compose = load_yaml(COMPOSE_PATH)
    manifest = load_yaml(MANIFEST_PATH)
    ci_compose = load_yaml(CI_COMPOSE_PATH)
    workflow = TEST_WORKFLOW_PATH.read_text(encoding="utf-8")

    proxy = compose["services"]["app_proxy"]
    proxy_env = environment_map(proxy)
    assert "network_mode" not in proxy
    assert proxy_env["APP_HOST"] == "tunnelsats-web"
    assert int(proxy_env["APP_PORT"]) == 9740
    assert proxy_env["PROXY_AUTH_ADD"] == "true"
    assert "PROXY_AUTH_WHITELIST" not in proxy_env

    web = compose["services"]["tunnelsats-web"]
    web_env = environment_map(web)
    assert web.get("network_mode") != "host"
    assert "network_mode" not in web
    assert str(web["user"]) != "0"
    assert web["cap_drop"] == ["ALL"]
    assert "cap_add" not in web
    assert "no-new-privileges:true" in web["security_opt"]
    assert web_env["TUNNELSATS_ROLE"] == "web"
    assert web_env["DASHBOARD_BIND_HOST"] == "0.0.0.0"
    assert int(web_env["DASHBOARD_BIND_PORT"]) == 9740
    assert web_env["MANAGEMENT_BROWSER_SECURITY_ENABLED"] == "true"
    assert web_env["MANAGEMENT_TRUSTED_PROXY_HOST"] == "app_proxy"

    web_volumes = [str(volume) for volume in web.get("volumes", [])]
    assert not any("/var/run/docker.sock" in volume for volume in web_volumes)
    assert not any("/lightning-data/" in volume for volume in web_volumes)

    daemon = compose["services"]["tunnelsats-daemon"]
    daemon_env = environment_map(daemon)
    assert daemon["network_mode"] == "host"
    assert daemon_env["TUNNELSATS_ROLE"] == "daemon"
    assert set(daemon["cap_add"]) == {"NET_ADMIN", "NET_RAW"}
    assert "ports" not in daemon

    daemon_volumes = [str(volume) for volume in daemon["volumes"]]
    assert any("/var/run/docker.sock" in volume for volume in daemon_volumes)
    assert any("/lightning-data/lnd" in volume for volume in daemon_volumes)
    assert any("/lightning-data/cln" in volume for volume in daemon_volumes)

    assert daemon_env["SECURE_MODE"] == "${SECURE_MODE:-false}"
    for name in (
        "DASHBOARD_BIND_HOST",
        "DASHBOARD_BIND_PORT",
        "MANAGEMENT_BROWSER_SECURITY_ENABLED",
    ):
        assert "SECURE_MODE" not in str(web_env[name])

    assert manifest["port"] == 9739
    assert manifest["defaultUsername"] == ""
    assert manifest["defaultPassword"] == ""

    for service_name in ("tunnelsats-web", "tunnelsats-daemon"):
        env = environment_map(compose["services"][service_name])
        assert "APP_PASSWORD" not in env
        assert "MANAGEMENT_PASSWORD" not in env
        assert "MANAGEMENT_USERNAME" not in env

    ci_proxy = ci_compose["services"]["app_proxy"]
    ci_web = ci_compose["services"]["tunnelsats-web"]
    ci_web_env = environment_map(ci_web)
    assert ci_proxy["image"]
    assert ci_proxy["profiles"] == ["umbrel-injected-app-proxy"]
    assert ci_web_env["MANAGEMENT_BROWSER_SECURITY_ENABLED"] == "false"
    assert ci_web["ports"] == ["9740:9740"]
    assert "COMPOSE_FILE: docker-compose.yml:docker-compose.ci.yml" in workflow
    assert "APP_DATA_DIR: ${{ github.workspace }}/data" in workflow
    assert "head -1 | awk" in workflow
    assert "COMPOSE_PROFILES" not in workflow


def test_management_csrf_and_socket_contract():
    compose = load_yaml(COMPOSE_PATH)
    web_volumes = [str(volume) for volume in compose["services"]["tunnelsats-web"]["volumes"]]
    daemon_volumes = [str(volume) for volume in compose["services"]["tunnelsats-daemon"]["volumes"]]

    socket_mount = "${APP_DATA_DIR}/runtime:/run/tunnelsats"
    assert socket_mount in web_volumes
    assert socket_mount in daemon_volumes
    assert "volumes" not in compose

    assert SECURITY_PATH.exists()
    security_source = SECURITY_PATH.read_text(encoding="utf-8")
    app_source = APP_PATH.read_text(encoding="utf-8")

    assert "WWW-Authenticate" not in app_source
    assert "management-password" not in app_source
    assert "MANAGEMENT_PASSWORD" not in app_source
    assert "Basic realm=" not in app_source
    assert "class ManagementSecurity" in security_source
    assert "from security import ManagementSecurity" in app_source
