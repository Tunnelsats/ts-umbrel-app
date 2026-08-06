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


def test_umbrel_compose_uses_authenticated_host_network_app_proxy():
    compose = load_yaml(COMPOSE_PATH)
    proxy = compose["services"]["app_proxy"]
    environment = environment_map(proxy)

    assert proxy["network_mode"] == "host"
    assert environment["APP_HOST"] == "tunnelsats"
    assert int(environment["APP_PORT"]) == 9740
    assert environment["PROXY_AUTH_ADD"] == "true"
    assert "PROXY_AUTH_WHITELIST" not in environment


def test_umbrel_backend_is_loopback_only_with_browser_security_enabled():
    compose = load_yaml(COMPOSE_PATH)
    service = compose["services"]["tunnelsats"]
    environment = environment_map(service)

    assert service["network_mode"] == "host"
    assert environment["DASHBOARD_BIND_HOST"] == "127.0.0.1"
    assert int(environment["DASHBOARD_BIND_PORT"]) == 9740
    assert environment["MANAGEMENT_BROWSER_SECURITY_ENABLED"] == "true"


def test_umbrel_http_security_path_is_independent_of_secure_mode():
    compose = load_yaml(COMPOSE_PATH)
    environment = environment_map(compose["services"]["tunnelsats"])

    assert environment["SECURE_MODE"] == "${SECURE_MODE:-false}"
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
    environment = environment_map(compose["services"]["tunnelsats"])

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


def test_ci_compose_supplies_only_a_dormant_injected_proxy_stub():
    ci_compose = load_yaml(CI_COMPOSE_PATH)
    proxy = ci_compose["services"]["app_proxy"]
    workflow = TEST_WORKFLOW_PATH.read_text(encoding="utf-8")

    assert proxy["image"]
    assert proxy["profiles"] == ["umbrel-injected-app-proxy"]
    assert "COMPOSE_FILE: docker-compose.yml:docker-compose.ci.yml" in workflow
    assert "COMPOSE_PROFILES" not in workflow
