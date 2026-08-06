import os
import shutil
import subprocess
from pathlib import Path

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
