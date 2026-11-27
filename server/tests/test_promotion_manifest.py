import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SUBMISSION_URL = "https://github.com/getumbrel/umbrel-apps/pull/4919"
OFFICIAL_DEFAULT_GALLERY = ["1.webp", "2.webp", "3.webp", "4.webp"]


def environment_map(service):
    environment = service.get("environment", {})
    if isinstance(environment, dict):
        return environment
    return dict(entry.split("=", 1) for entry in environment)


def setup_promotion_project(tmp_path):
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "scripts" / "sync.sh", project / "scripts" / "sync.sh")
    promote_script = REPO_ROOT / "scripts" / "promote_official_store.py"
    if promote_script.exists():
        shutil.copy2(promote_script, project / "scripts" / "promote_official_store.py")
    shutil.copytree(REPO_ROOT / "tunnelsats-community", project / "tunnelsats-community")
    return project


def make_fake_docker_bin(tmp_path, digest_hex_char="a"):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    fake_docker = fake_bin / "docker"
    digest = f"sha256:{digest_hex_char * 64}"
    fake_docker.write_text(
        f"#!/bin/sh\nprintf '%s\\n' 'Name: test' 'Digest: {digest}'\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    return fake_bin, digest


def test_canonical_community_source_manifest_and_gitkeep_parity():
    root_compose = REPO_ROOT / "docker-compose.yml"
    assert root_compose.is_symlink()
    assert os.readlink(root_compose) == "tunnelsats-community/docker-compose.yml"

    data_gitkeep = REPO_ROOT / "tunnelsats-community" / "data" / ".gitkeep"
    runtime_gitkeep = REPO_ROOT / "tunnelsats-community" / "runtime" / ".gitkeep"
    assert data_gitkeep.exists() and data_gitkeep.stat().st_size == 0
    assert runtime_gitkeep.exists() and runtime_gitkeep.stat().st_size == 0

    compose = yaml.safe_load((REPO_ROOT / "tunnelsats-community" / "docker-compose.yml").read_text(encoding="utf-8"))
    assert "version" not in compose
    assert "container_name" not in compose["services"]["tunnelsats-web"]
    assert "container_name" not in compose["services"]["tunnelsats-daemon"]
    proxy_env = environment_map(compose["services"]["app_proxy"])
    assert proxy_env["APP_HOST"] == "tunnelsats-community_tunnelsats-web_1"
    assert str(proxy_env["APP_PORT"]) == "9740"
    assert environment_map(compose["services"]["tunnelsats-web"])["UMBREL_APP_ID"] == "tunnelsats-community"
    assert environment_map(compose["services"]["tunnelsats-daemon"])["UMBREL_APP_ID"] == "tunnelsats-community"
    daemon_volumes = [str(v) for v in compose["services"]["tunnelsats-daemon"]["volumes"]]
    data_idx = daemon_volumes.index("${APP_DATA_DIR}/../tunnelsats-data:/data")
    assert daemon_volumes[data_idx + 1] == "${APP_DATA_DIR}/runtime:/run/tunnelsats"

    manifest = yaml.safe_load((REPO_ROOT / "tunnelsats-community" / "umbrel-app.yml").read_text(encoding="utf-8"))
    assert manifest["id"] == "tunnelsats-community"
    assert manifest["name"] == "TunnelSats (Community)"
    assert manifest["port"] == 9741
    assert manifest["tagline"] == "Run your Lightning node on clearnet without exposing your home IP"
    assert manifest["icon"] == "https://raw.githubusercontent.com/Tunnelsats/ts-umbrel-app/master/tunnelsats-community/icon.svg"
    assert manifest["gallery"] == [
        "https://raw.githubusercontent.com/Tunnelsats/ts-umbrel-app/master/tunnelsats-community/gallery/1.png",
        "https://raw.githubusercontent.com/Tunnelsats/ts-umbrel-app/master/tunnelsats-community/gallery/2.png",
        "https://raw.githubusercontent.com/Tunnelsats/ts-umbrel-app/master/tunnelsats-community/gallery/3.png",
    ]
    assert "storage" not in manifest
    assert "even before you subscribe" not in manifest["description"]
    assert "Before you activate a subscription, your node's networking is not touched." in manifest["description"]
    assert "Migrating from legacy Community TunnelSats:" in manifest["description"]
    assert "Do not run Official TunnelSats and TunnelSats (Community) simultaneously." in manifest["description"]
    assert manifest["widgets"][0]["endpoint"] == "tunnelsats-web:9740/api/local/widgets/status"
    assert manifest["widgets"][1]["endpoint"] == "tunnelsats-web:9740/api/local/widgets/overview"
    assert manifest["widgets"][1]["example"]["progressLabel"] == "14.2 / 100 GB (14%)"
    assert manifest["widgets"][1]["example"]["progress"] == 0.14

    # Temporary conflict-guard stub stays valid and free of hardcoded container_name until Official v3.6.3 merges
    stub_manifest = yaml.safe_load((REPO_ROOT / "tunnelsats" / "umbrel-app.yml").read_text(encoding="utf-8"))
    stub_compose = yaml.safe_load((REPO_ROOT / "tunnelsats" / "docker-compose.yml").read_text(encoding="utf-8"))
    assert stub_manifest["id"] == "tunnelsats"
    assert "container_name" not in stub_compose["services"]["tunnelsats-web"]
    assert "container_name" not in stub_compose["services"]["tunnelsats-daemon"]


def test_promotion_generates_linter_compatible_split_manifest(tmp_path):
    project = setup_promotion_project(tmp_path)
    store = tmp_path / "umbrel-apps"
    store.mkdir()
    fake_bin, digest = make_fake_docker_bin(tmp_path, "a")

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "UMBREL_APPS_DIR": str(store),
            "SUBMISSION_URL": DEFAULT_SUBMISSION_URL,
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

    target_dir = store / "tunnelsats"
    assert not (target_dir / "scripts").exists()
    assert not (target_dir / "icon.svg").exists()
    assert not (target_dir / "gallery").exists()
    assert (target_dir / "data" / ".gitkeep").stat().st_size == 0
    assert (target_dir / "runtime" / ".gitkeep").stat().st_size == 0

    compose = yaml.safe_load((target_dir / "docker-compose.yml").read_text(encoding="utf-8"))
    assert "version" not in compose
    services = compose["services"]
    proxy_environment = environment_map(services["app_proxy"])

    assert proxy_environment["APP_HOST"] == "tunnelsats_tunnelsats-web_1"
    assert str(proxy_environment["APP_PORT"]) == "9740"
    assert "PROXY_AUTH_ADD" not in proxy_environment
    assert "PROXY_AUTH_WHITELIST" not in proxy_environment
    assert services["tunnelsats-web"].get("network_mode") != "host"
    assert services["tunnelsats-daemon"]["network_mode"] == "host"

    expected_suffix = f"@{digest}"
    assert services["tunnelsats-web"]["image"].endswith(expected_suffix)
    assert services["tunnelsats-daemon"]["image"].endswith(expected_suffix)
    assert "container_name" not in services["tunnelsats-web"]
    assert "container_name" not in services["tunnelsats-daemon"]
    assert services["tunnelsats-web"]["user"] == "1000:1000"
    assert services["tunnelsats-web"]["cap_drop"] == ["ALL"]
    assert services["tunnelsats-web"]["security_opt"] == ["no-new-privileges:true"]
    assert services["tunnelsats-web"]["volumes"] == [
        "${APP_DATA_DIR}/data:/data",
        "${APP_DATA_DIR}/runtime:/run/tunnelsats",
    ]
    assert services["tunnelsats-daemon"]["cap_add"] == ["NET_ADMIN"]
    assert "security_opt" not in services["tunnelsats-daemon"]

    web_environment = environment_map(services["tunnelsats-web"])
    assert "UMBREL_APP_ID" not in web_environment
    daemon_environment = environment_map(services["tunnelsats-daemon"])
    assert "UMBREL_APP_ID" not in daemon_environment
    assert daemon_environment["SECURE_MODE"] == "true"
    daemon_volumes = [str(volume) for volume in services["tunnelsats-daemon"]["volumes"]]
    assert daemon_volumes == [
        "${APP_DATA_DIR}/data:/data",
        "${APP_DATA_DIR}/runtime:/run/tunnelsats",
        "${APP_DATA_DIR}/../lightning/data/lnd:/lightning-data/lnd:ro",
        "${APP_DATA_DIR}/../core-lightning/data/lightningd:/lightning-data/cln:ro",
    ]

    source_manifest = yaml.safe_load((project / "tunnelsats-community" / "umbrel-app.yml").read_text(encoding="utf-8"))
    promoted_manifest = yaml.safe_load((target_dir / "umbrel-app.yml").read_text(encoding="utf-8"))
    assert promoted_manifest["id"] == "tunnelsats"
    assert promoted_manifest["storage"] == {"dataRoot": "data"}
    assert promoted_manifest["name"] == "TunnelSats"
    assert promoted_manifest["port"] == 9739
    assert promoted_manifest["version"] == source_manifest["version"]
    assert promoted_manifest["tagline"] == source_manifest["tagline"]
    assert "After you activate it, the app shows the settings" in promoted_manifest["description"]
    assert "Uninstalling also deletes your WireGuard config" in promoted_manifest["description"]
    assert "tunnelsats-data" not in promoted_manifest["description"]
    assert "tunnelsats-community" not in promoted_manifest["description"]
    assert "TunnelSats (Community)" not in promoted_manifest["description"]
    assert "Migrating from legacy Community" not in promoted_manifest["description"]
    assert promoted_manifest["releaseNotes"].startswith(
        "Community Store users: Do NOT click Update here (switches to Official Secure Mode)."
    )
    assert source_manifest["releaseNotes"] in promoted_manifest["releaseNotes"]
    assert "\n" not in promoted_manifest["releaseNotes"]
    assert "icon" not in promoted_manifest
    assert promoted_manifest["gallery"] == OFFICIAL_DEFAULT_GALLERY
    assert promoted_manifest["widgets"] == source_manifest["widgets"]
    assert promoted_manifest["submitter"] == "Tunnelsats"
    assert promoted_manifest["submission"] == DEFAULT_SUBMISSION_URL

    keys = list(promoted_manifest.keys())
    assert keys.index("storage") == keys.index("id") + 1
    assert keys[-2:] == ["submitter", "submission"]


def test_promotion_defaults_submission_url_and_preserves_existing_official_gallery(tmp_path):
    project = setup_promotion_project(tmp_path)
    store = tmp_path / "umbrel-apps"
    existing_target = store / "tunnelsats"
    existing_target.mkdir(parents=True)
    custom_gallery = ["1.webp", "2.webp", "3.webp", "4.webp", "5.webp"]
    (existing_target / "umbrel-app.yml").write_text(
        yaml.safe_dump({"id": "tunnelsats", "gallery": custom_gallery}),
        encoding="utf-8",
    )
    fake_bin, _ = make_fake_docker_bin(tmp_path, "c")

    environment = os.environ.copy()
    environment.pop("SUBMISSION_URL", None)
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "UMBREL_APPS_DIR": str(store),
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

    promoted_manifest = yaml.safe_load((existing_target / "umbrel-app.yml").read_text(encoding="utf-8"))
    assert promoted_manifest["submission"] == DEFAULT_SUBMISSION_URL
    assert promoted_manifest["submitter"] == "Tunnelsats"
    assert promoted_manifest["gallery"] == custom_gallery


@pytest.mark.parametrize(
    "existing_gallery",
    [
        [],
        ["https://raw.githubusercontent.com/Tunnelsats/ts-umbrel-app/master/tunnelsats/gallery/1.png"],
        ["../escape.webp"],
        ["1.webp", "1.webp"],
    ],
    ids=["empty_gallery", "remote_url_gallery", "traversal_gallery", "duplicate_gallery"],
)
def test_promotion_ignores_invalid_or_empty_existing_gallery(tmp_path, existing_gallery):
    project = setup_promotion_project(tmp_path)
    store = tmp_path / "umbrel-apps"
    existing_target = store / "tunnelsats"
    existing_target.mkdir(parents=True)
    (existing_target / "umbrel-app.yml").write_text(
        yaml.safe_dump({"id": "tunnelsats", "gallery": existing_gallery}),
        encoding="utf-8",
    )
    fake_bin, _ = make_fake_docker_bin(tmp_path, "d")

    environment = os.environ.copy()
    environment.pop("SUBMISSION_URL", None)
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "UMBREL_APPS_DIR": str(store),
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
    promoted_manifest = yaml.safe_load((existing_target / "umbrel-app.yml").read_text(encoding="utf-8"))
    assert promoted_manifest["gallery"] == OFFICIAL_DEFAULT_GALLERY


def test_promotion_dry_run_previews_diff_without_modifying_target(tmp_path):
    project = setup_promotion_project(tmp_path)
    store = tmp_path / "umbrel-apps"
    existing_target = store / "tunnelsats"
    existing_target.mkdir(parents=True)
    original_manifest = yaml.safe_dump({"id": "tunnelsats", "gallery": ["1.webp", "2.webp"]})
    (existing_target / "umbrel-app.yml").write_text(original_manifest, encoding="utf-8")
    (existing_target / "docker-compose.yml").write_text("sentinel: keep\n", encoding="utf-8")
    fake_bin, _ = make_fake_docker_bin(tmp_path, "e")

    environment = os.environ.copy()
    environment.pop("SUBMISSION_URL", None)
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "UMBREL_APPS_DIR": str(store),
        }
    )
    result = subprocess.run(
        ["bash", str(project / "scripts" / "sync.sh"), "promote", "--dry-run"],
        cwd=project,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "tunnelsats_tunnelsats-web_1" in result.stdout
    assert "2.webp" in result.stdout
    assert (existing_target / "umbrel-app.yml").read_text(encoding="utf-8") == original_manifest
    assert (existing_target / "docker-compose.yml").read_text(encoding="utf-8") == "sentinel: keep\n"


def test_promotion_fails_closed_on_source_compose_drift_without_modifying_target(tmp_path):
    project = setup_promotion_project(tmp_path)
    compose_path = project / "tunnelsats-community" / "docker-compose.yml"
    compose_data = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    compose_data["services"]["tunnelsats-web"]["user"] = "0:0"
    compose_path.write_text(yaml.safe_dump(compose_data), encoding="utf-8")

    store = tmp_path / "umbrel-apps"
    sentinel = store / "tunnelsats" / "docker-compose.yml"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("sentinel: unchanged\n", encoding="utf-8")
    fake_bin, _ = make_fake_docker_bin(tmp_path, "f")

    environment = os.environ.copy()
    environment.pop("SUBMISSION_URL", None)
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "UMBREL_APPS_DIR": str(store),
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


def test_promotion_falls_back_to_docker_hub_api_when_docker_cli_unavailable(tmp_path):
    project = setup_promotion_project(tmp_path)

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
            "SUBMISSION_URL": DEFAULT_SUBMISSION_URL,
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

    compose = yaml.safe_load((store / "tunnelsats" / "docker-compose.yml").read_text(encoding="utf-8"))
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
    project = setup_promotion_project(tmp_path)

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
            "SUBMISSION_URL": DEFAULT_SUBMISSION_URL,
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


def test_community_and_official_descriptions_match_respective_uninstall_storage_and_mode(
    tmp_path,
):
    source_manifest = yaml.safe_load(
        (REPO_ROOT / "tunnelsats-community" / "umbrel-app.yml").read_text(encoding="utf-8")
    )
    community_desc = source_manifest["description"]
    assert "tunnelsats-data" in community_desc
    assert "Uninstalling also deletes your WireGuard config" not in community_desc

    project = setup_promotion_project(tmp_path)
    store = tmp_path / "umbrel-apps"
    store.mkdir()
    fake_bin, _ = make_fake_docker_bin(tmp_path, "d")
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "UMBREL_APPS_DIR": str(store),
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

    official_manifest = yaml.safe_load(
        (store / "tunnelsats" / "umbrel-app.yml").read_text(encoding="utf-8")
    )
    official_desc = official_manifest["description"]
    assert "Uninstalling also deletes your WireGuard config" in official_desc
    assert "After you activate it, the app shows the settings" in official_desc
    assert "tunnelsats-data" not in official_desc
    assert "tunnelsats-community" not in official_desc
    assert "TunnelSats (Community)" not in official_desc
    assert "On the Community App Store" not in official_desc
    assert "Migrating from legacy Community" not in official_desc


def test_promotion_rejects_legacy_tunnelsats_stub_and_community_description_leaks(tmp_path):
    promote_script = REPO_ROOT / "scripts" / "promote_official_store.py"
    target = tmp_path / "out"
    digest = "sha256:" + ("9" * 64)

    # Attempting to promote the temporary legacy tunnelsats/ stub must fail closed
    stub_res = subprocess.run(
        [
            "python3",
            str(promote_script),
            "--source-dir",
            str(REPO_ROOT / "tunnelsats"),
            "--target-dir",
            str(target),
            "--version",
            "3.6.3",
            "--digest",
            digest,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert stub_res.returncode != 0
    assert "tunnelsats-community" in stub_res.stderr
    assert not target.exists()

    # Stray community-only text in description must fail closed
    bad_source = tmp_path / "bad-source"
    shutil.copytree(REPO_ROOT / "tunnelsats-community", bad_source)
    manifest_path = bad_source / "umbrel-app.yml"
    manifest_data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest_data["description"] += "\n\nExtra stray mention of tunnelsats-community."
    manifest_path.write_text(yaml.safe_dump(manifest_data), encoding="utf-8")

    leak_res = subprocess.run(
        [
            "python3",
            str(promote_script),
            "--source-dir",
            str(bad_source),
            "--target-dir",
            str(target),
            "--version",
            "3.6.3",
            "--digest",
            digest,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert leak_res.returncode != 0
    assert "forbidden community token" in leak_res.stderr
    assert not target.exists()


def test_promotion_no_community_notice_flag_omits_migration_prefix(tmp_path):
    promote_script = REPO_ROOT / "scripts" / "promote_official_store.py"
    target = tmp_path / "out"
    digest = "sha256:" + ("7" * 64)
    source_manifest = yaml.safe_load(
        (REPO_ROOT / "tunnelsats-community" / "umbrel-app.yml").read_text(encoding="utf-8")
    )

    res = subprocess.run(
        [
            "python3",
            str(promote_script),
            "--source-dir",
            str(REPO_ROOT / "tunnelsats-community"),
            "--target-dir",
            str(target),
            "--version",
            "3.6.3",
            "--digest",
            digest,
            "--no-community-notice",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    promoted = yaml.safe_load((target / "umbrel-app.yml").read_text(encoding="utf-8"))
    assert promoted["releaseNotes"] == source_manifest["releaseNotes"]
    assert "Community Store users: Do NOT click Update here" not in promoted["releaseNotes"]

