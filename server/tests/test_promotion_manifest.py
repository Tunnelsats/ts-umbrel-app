import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]


def environment_map(service):
    environment = service.get("environment", {})
    if isinstance(environment, dict):
        return environment
    return dict(entry.split("=", 1) for entry in environment)


def test_promotion_generates_linter_compatible_split_manifest(tmp_path):
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "scripts" / "sync.sh", project / "scripts" / "sync.sh")
    shutil.copytree(REPO_ROOT / "tunnelsats", project / "tunnelsats")

    store = tmp_path / "umbrel-apps"
    store.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        "#!/bin/sh\nprintf '%s\\n' 'Name: test' "
        "'Digest: sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "UMBREL_APPS_DIR": str(store),
            "SUBMISSION_URL": "https://github.com/getumbrel/umbrel-apps/pull/4919",
        }
    )
    result = subprocess.run(
        ["bash", str(project / "scripts" / "sync.sh"), "promote"],
        cwd=project,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    compose = yaml.safe_load((store / "tunnelsats" / "docker-compose.yml").read_text())
    services = compose["services"]
    proxy_environment = environment_map(services["app_proxy"])

    assert proxy_environment["APP_HOST"] == "tunnelsats-web"
    assert "PROXY_AUTH_WHITELIST" not in proxy_environment
    assert services["tunnelsats-web"].get("network_mode") != "host"
    assert services[proxy_environment["APP_HOST"]].get("network_mode") != "host"
    assert services["tunnelsats-daemon"]["network_mode"] == "host"

    digest = "@sha256:" + ("a" * 64)
    assert services["tunnelsats-web"]["image"].endswith(digest)
    assert services["tunnelsats-daemon"]["image"].endswith(digest)
    assert "container_name" not in services["tunnelsats-web"]
    assert "container_name" not in services["tunnelsats-daemon"]
    assert "NET_RAW" not in services["tunnelsats-daemon"]["cap_add"]
    assert services["tunnelsats-web"]["security_opt"] == ["no-new-privileges:true"]

    daemon_environment = environment_map(services["tunnelsats-daemon"])
    assert daemon_environment["SECURE_MODE"] == "${SECURE_MODE:-true}"
    daemon_volumes = [str(volume) for volume in services["tunnelsats-daemon"]["volumes"]]
    assert not any("docker.sock" in volume for volume in daemon_volumes)
    assert not any("migration_source" in volume for volume in daemon_volumes)


def test_promotion_falls_back_to_docker_hub_api_when_docker_cli_unavailable(tmp_path):
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "scripts" / "sync.sh", project / "scripts" / "sync.sh")
    shutil.copytree(REPO_ROOT / "tunnelsats", project / "tunnelsats")

    store = tmp_path / "umbrel-apps"
    store.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text("#!/bin/sh\nexit 127\n", encoding="utf-8")
    fake_docker.chmod(0o755)

    expected_digest = "sha256:" + ("b" * 64)
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        f"#!/bin/sh\nprintf '%s\\n' '{{\"digest\": \"{expected_digest}\"}}'\n",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "UMBREL_APPS_DIR": str(store),
            "SUBMISSION_URL": "https://github.com/getumbrel/umbrel-apps/pull/4919",
        }
    )
    result = subprocess.run(
        ["bash", str(project / "scripts" / "sync.sh"), "promote"],
        cwd=project,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    compose = yaml.safe_load((store / "tunnelsats" / "docker-compose.yml").read_text())
    services = compose["services"]
    assert services["tunnelsats-web"]["image"].endswith(f"@{expected_digest}")
    assert services["tunnelsats-daemon"]["image"].endswith(f"@{expected_digest}")


@pytest.mark.parametrize(
    "curl_script",
    [
        "#!/bin/sh\nexit 22\n",
        "#!/bin/sh\nprintf '%s\\n' '{}'\n",
        "#!/bin/sh\nprintf '%s\\n' '{\"digest\": \"sha256:not-a-valid-hex-digest\"}'\n",
    ],
    ids=["curl_error", "missing_digest", "malformed_digest"],
)
def test_promotion_aborts_without_modifying_store_when_digest_lookup_fails(
    tmp_path, curl_script
):
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "scripts" / "sync.sh", project / "scripts" / "sync.sh")
    shutil.copytree(REPO_ROOT / "tunnelsats", project / "tunnelsats")

    store = tmp_path / "umbrel-apps"
    store.mkdir()
    sentinel = store / "tunnelsats" / "docker-compose.yml"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("sentinel: unchanged\n", encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text("#!/bin/sh\nexit 127\n", encoding="utf-8")
    fake_docker.chmod(0o755)

    fake_curl = fake_bin / "curl"
    fake_curl.write_text(curl_script, encoding="utf-8")
    fake_curl.chmod(0o755)

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "UMBREL_APPS_DIR": str(store),
            "SUBMISSION_URL": "https://github.com/getumbrel/umbrel-apps/pull/4919",
        }
    )
    result = subprocess.run(
        ["bash", str(project / "scripts" / "sync.sh"), "promote"],
        cwd=project,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert sentinel.read_text(encoding="utf-8") == "sentinel: unchanged\n"


