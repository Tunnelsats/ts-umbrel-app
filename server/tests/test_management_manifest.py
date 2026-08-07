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


def test_umbrel_proxy_targets_bridge_network_web_service():
    compose = load_yaml(COMPOSE_PATH)
    proxy = compose["services"]["app_proxy"]
    environment = environment_map(proxy)

    assert "network_mode" not in proxy
    assert environment["APP_HOST"] == "tunnelsats-web"
    assert int(environment["APP_PORT"]) == 9740
    assert environment["PROXY_AUTH_ADD"] == "true"
    assert "PROXY_AUTH_WHITELIST" not in environment

    target = compose["services"][environment["APP_HOST"]]
    assert target.get("network_mode") != "host"


def test_umbrel_web_service_is_unprivileged_and_bridge_networked():
    compose = load_yaml(COMPOSE_PATH)
    service = compose["services"]["tunnelsats-web"]
    environment = environment_map(service)

    assert "network_mode" not in service
    assert str(service["user"]) != "0"
    assert service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in service["security_opt"]
    assert environment["TUNNELSATS_ROLE"] == "web"
    assert environment["DASHBOARD_BIND_HOST"] == "0.0.0.0"
    assert int(environment["DASHBOARD_BIND_PORT"]) == 9740
    assert environment["MANAGEMENT_BROWSER_SECURITY_ENABLED"] == "true"
    assert environment["MANAGEMENT_TRUSTED_PROXY_HOST"] == "app_proxy"

    volumes = [str(volume) for volume in service.get("volumes", [])]
    assert not any("/var/run/docker.sock" in volume for volume in volumes)
    assert not any("/lightning-data/" in volume for volume in volumes)


def test_umbrel_daemon_is_only_privileged_host_network_service():
    compose = load_yaml(COMPOSE_PATH)
    daemon = compose["services"]["tunnelsats-daemon"]
    environment = environment_map(daemon)

    assert daemon["network_mode"] == "host"
    assert environment["TUNNELSATS_ROLE"] == "daemon"
    assert set(daemon["cap_add"]) == {"NET_ADMIN", "NET_RAW"}
    assert "ports" not in daemon

    daemon_volumes = [str(volume) for volume in daemon["volumes"]]
    assert any("/var/run/docker.sock" in volume for volume in daemon_volumes)
    assert any("/lightning-data/lnd" in volume for volume in daemon_volumes)
    assert any("/lightning-data/cln" in volume for volume in daemon_volumes)

    web = compose["services"]["tunnelsats-web"]
    assert set(web["cap_drop"]) == {"ALL"}
    assert "cap_add" not in web


def test_split_services_share_private_app_data_runtime_mount():
    compose = load_yaml(COMPOSE_PATH)
    web_volumes = [str(volume) for volume in compose["services"]["tunnelsats-web"]["volumes"]]
    daemon_volumes = [str(volume) for volume in compose["services"]["tunnelsats-daemon"]["volumes"]]

    socket_mount = "${APP_DATA_DIR}/runtime:/run/tunnelsats"
    assert socket_mount in web_volumes
    assert socket_mount in daemon_volumes
    assert "volumes" not in compose


def test_umbrel_http_security_path_is_independent_of_secure_mode():
    compose = load_yaml(COMPOSE_PATH)
    environment = environment_map(compose["services"]["tunnelsats-web"])
    daemon_environment = environment_map(compose["services"]["tunnelsats-daemon"])

    assert daemon_environment["SECURE_MODE"] == "${SECURE_MODE:-false}"
    for name in (
        "DASHBOARD_BIND_HOST",
        "DASHBOARD_BIND_PORT",
        "MANAGEMENT_BROWSER_SECURITY_ENABLED",
    ):
        assert "SECURE_MODE" not in str(environment[name])


def test_umbrel_manifest_keeps_proxy_port_and_has_no_second_login():
    manifest = load_yaml(MANIFEST_PATH)

    assert manifest["port"] == 9739
    assert manifest["defaultUsername"] == ""
    assert manifest["defaultPassword"] == ""


def test_umbrel_compose_does_not_pass_application_password_credentials():
    compose = load_yaml(COMPOSE_PATH)
    for service_name in ("tunnelsats-web", "tunnelsats-daemon"):
        environment = environment_map(compose["services"][service_name])
        assert "APP_PASSWORD" not in environment
        assert "MANAGEMENT_PASSWORD" not in environment
        assert "MANAGEMENT_USERNAME" not in environment


def test_server_does_not_reintroduce_basic_auth_or_password_file():
    source = APP_PATH.read_text(encoding="utf-8")

    assert "WWW-Authenticate" not in source
    assert "management-password" not in source
    assert "MANAGEMENT_PASSWORD" not in source
    assert "Basic realm=" not in source


def test_management_security_rules_are_extracted_for_standalone_testing():
    assert SECURITY_PATH.exists()
    security_source = SECURITY_PATH.read_text(encoding="utf-8")
    app_source = APP_PATH.read_text(encoding="utf-8")

    assert "class ManagementSecurity" in security_source
    assert "from security import ManagementSecurity" in app_source


def test_ci_compose_supplies_dormant_proxy_and_private_web_smoke_port():
    ci_compose = load_yaml(CI_COMPOSE_PATH)
    proxy = ci_compose["services"]["app_proxy"]
    web = ci_compose["services"]["tunnelsats-web"]
    web_environment = environment_map(web)
    workflow = TEST_WORKFLOW_PATH.read_text(encoding="utf-8")

    assert proxy["image"]
    assert proxy["profiles"] == ["umbrel-injected-app-proxy"]
    assert web_environment["MANAGEMENT_BROWSER_SECURITY_ENABLED"] == "false"
    assert web["ports"] == ["9740:9740"]
    assert "COMPOSE_FILE: docker-compose.yml:docker-compose.ci.yml" in workflow
    assert "APP_DATA_DIR: ${{ github.workspace }}/data" in workflow
    assert "head -1 | awk" in workflow
    assert "COMPOSE_PROFILES" not in workflow
