import importlib.util
import os
import shutil
import subprocess

import pytest
import yaml


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BUMP_SCRIPT_PATH = os.path.join(REPO_ROOT, "scripts", "bump_version.py")


@pytest.fixture
def bump_module():
    spec = importlib.util.spec_from_file_location("bump_version", BUMP_SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def version_workspace(tmp_path, bump_module, monkeypatch):
    relative_paths = (
        "tunnelsats/umbrel-app.yml",
        "tunnelsats/docker-compose.yml",
        "server/app.py",
        "web/index.html",
        "k3s/deployment.yaml",
        "CHANGELOG.md",
        "scripts/bump_version.py",
        "scripts/sync.sh",
    )
    for relative_path in relative_paths:
        source = os.path.join(REPO_ROOT, relative_path)
        destination = tmp_path / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    monkeypatch.setattr(bump_module, "REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(
        bump_module, "MANIFEST_PATH", str(tmp_path / "tunnelsats/umbrel-app.yml")
    )
    monkeypatch.setattr(
        bump_module, "COMPOSE_PATH", str(tmp_path / "tunnelsats/docker-compose.yml")
    )
    monkeypatch.setattr(bump_module, "APP_PY_PATH", str(tmp_path / "server/app.py"))
    monkeypatch.setattr(
        bump_module, "INDEX_HTML_PATH", str(tmp_path / "web/index.html")
    )
    monkeypatch.setattr(
        bump_module, "DEPLOYMENT_K3S_PATH", str(tmp_path / "k3s/deployment.yaml")
    )
    monkeypatch.setattr(bump_module, "CHANGELOG_PATH", str(tmp_path / "CHANGELOG.md"))

    return tmp_path


def snapshot_files(root):
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize(
    "version",
    (
        "",
        "v",
        "3.3",
        "3.3.4.5",
        "01.2.3",
        "3.3.4+build",
        "not-a-version",
        f"1.0.{('1' * 125)}",
    ),
)
def test_invalid_version_is_rejected_without_writes(
    bump_module, version_workspace, version
):
    before = snapshot_files(version_workspace)

    with pytest.raises(ValueError, match="Invalid version"):
        bump_module.bump_version(version, "Release notes")

    assert snapshot_files(version_workspace) == before


@pytest.mark.parametrize("notes", (None, "", " ", "\n\t"))
def test_missing_or_blank_release_notes_are_rejected_without_writes(
    bump_module, version_workspace, notes
):
    before = snapshot_files(version_workspace)

    with pytest.raises(ValueError, match="Release notes are required"):
        bump_module.bump_version("4.0.0", notes)

    assert snapshot_files(version_workspace) == before


@pytest.mark.parametrize(
    "notes",
    ("First line\nSecond line", "First line\r\nSecond line", "First line\rSecond line"),
)
def test_multiline_release_notes_are_rejected_without_writes(
    bump_module, version_workspace, notes
):
    before = snapshot_files(version_workspace)

    with pytest.raises(ValueError, match="single-line summary"):
        bump_module.bump_version("4.0.0", notes)

    assert snapshot_files(version_workspace) == before


def test_preflight_failure_does_not_partially_update_files(
    bump_module, version_workspace
):
    deployment = version_workspace / "k3s/deployment.yaml"
    deployment.write_text(
        deployment.read_text().replace(
            "image: tunnelsats/ts-umbrel-app:3.3.4", "image: example/other:latest"
        )
    )
    before = snapshot_files(version_workspace)

    with pytest.raises(ValueError, match="found 0"):
        bump_module.bump_version("4.0.0", "Release notes")

    assert snapshot_files(version_workspace) == before


def test_commit_failure_rolls_back_all_files(
    bump_module, version_workspace, monkeypatch
):
    before = snapshot_files(version_workspace)
    real_replace = os.replace
    replace_calls = 0

    def fail_second_replace(source, destination):
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 2:
            raise OSError("simulated replace failure")
        return real_replace(source, destination)

    monkeypatch.setattr(bump_module.os, "replace", fail_second_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        bump_module.bump_version("4.0.0", "Release notes")

    assert snapshot_files(version_workspace) == before


def test_interruption_rolls_back_all_files(
    bump_module, version_workspace, monkeypatch
):
    before = snapshot_files(version_workspace)
    real_replace = os.replace
    replace_calls = 0

    def interrupt_second_replace(source, destination):
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 2:
            real_replace(source, destination)
            raise KeyboardInterrupt
        return real_replace(source, destination)

    monkeypatch.setattr(bump_module.os, "replace", interrupt_second_replace)

    with pytest.raises(KeyboardInterrupt):
        bump_module.bump_version("4.0.0", "Release notes")

    assert snapshot_files(version_workspace) == before


def test_failed_rollback_preserves_original_recovery_copy(
    bump_module, version_workspace, monkeypatch
):
    manifest_path = version_workspace / "tunnelsats/umbrel-app.yml"
    original_manifest = manifest_path.read_text()
    real_replace = os.replace
    replace_calls = 0

    def fail_commit_and_rollback(source, destination):
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls in (2, 3):
            raise OSError(f"simulated replace failure {replace_calls}")
        return real_replace(source, destination)

    monkeypatch.setattr(bump_module.os, "replace", fail_commit_and_rollback)

    with pytest.raises(RuntimeError, match="original preserved at") as error:
        bump_module.bump_version("4.0.0", "Release notes")

    recovery_path = error.value.args[0].split("original preserved at ", 1)[1]
    assert os.path.exists(recovery_path)
    with open(recovery_path, encoding="utf-8") as recovery:
        assert recovery.read() == original_manifest
    assert 'version: "4.0.0"' in manifest_path.read_text()


def test_valid_version_and_release_notes_are_written_safely(
    bump_module, version_workspace
):
    notes = 'Windows path C:\\temp says "ready"'

    bump_module.bump_version("v4.0.0-rc.1", notes)

    manifest = yaml.safe_load(
        (version_workspace / "tunnelsats/umbrel-app.yml").read_text()
    )
    assert manifest["version"] == "4.0.0-rc.1"
    assert manifest["releaseNotes"] == notes
    assert (
        "tunnelsats/ts-umbrel-app:4.0.0-rc.1"
        in (version_workspace / "tunnelsats/docker-compose.yml").read_text()
    )


def test_existing_version_updates_its_changelog_section(
    bump_module, version_workspace
):
    notes = r"Corrected path C:\temp and literal \g<1>"

    bump_module.bump_version("3.3.4", notes)

    manifest = yaml.safe_load(
        (version_workspace / "tunnelsats/umbrel-app.yml").read_text()
    )
    changelog = (version_workspace / "CHANGELOG.md").read_text()
    assert manifest["releaseNotes"] == notes
    assert changelog.count("## [3.3.4]") == 1
    assert f"## [3.3.4]\n\n- {notes}\n" in changelog
    assert "Hardened dataplane policy routing" not in changelog


def test_sync_version_command_delegates_to_canonical_bump_tool(version_workspace):
    result = subprocess.run(
        [
            "bash",
            str(version_workspace / "scripts/sync.sh"),
            "version",
            "4.1.0",
            "--notes",
            "Delegated release",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert (
        'version: "4.1.0"'
        in (version_workspace / "tunnelsats/umbrel-app.yml").read_text()
    )
    assert (
        "tunnelsats/ts-umbrel-app:4.1.0"
        in (version_workspace / "tunnelsats/docker-compose.yml").read_text()
    )
    assert (
        'APP_VERSION = "v4.1.0"'
        in (version_workspace / "server/app.py").read_text()
    )
    assert ">v4.1.0</span>" in (version_workspace / "web/index.html").read_text()
    assert (
        "tunnelsats/ts-umbrel-app:4.1.0"
        in (version_workspace / "k3s/deployment.yaml").read_text()
    )
