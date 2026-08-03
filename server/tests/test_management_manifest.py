from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO_ROOT / "tunnelsats" / "docker-compose.yml"
MANIFEST_PATH = REPO_ROOT / "tunnelsats" / "umbrel-app.yml"


def test_umbrel_passes_per_app_password_and_explicit_hosts():
    with COMPOSE_PATH.open(encoding="utf-8") as compose_file:
        compose = yaml.safe_load(compose_file)

    environment = compose["services"]["tunnelsats"]["environment"]
    assert "MANAGEMENT_PASSWORD=${APP_PASSWORD}" in environment
    allowed_hosts = next(
        entry for entry in environment if entry.startswith("MANAGEMENT_ALLOWED_HOSTS=")
    )
    assert "${DEVICE_HOSTNAME}" in allowed_hosts
    assert "${DEVICE_DOMAIN_NAME}" in allowed_hosts
    assert "${APP_HIDDEN_SERVICE}" in allowed_hosts
    assert "localhost" in allowed_hosts


def test_umbrel_surfaces_management_login_credentials():
    with MANIFEST_PATH.open(encoding="utf-8") as manifest_file:
        manifest = yaml.safe_load(manifest_file)

    assert manifest["defaultUsername"] == "tunnelsats"
    assert manifest["defaultPassword"] == "$APP_PASSWORD"
