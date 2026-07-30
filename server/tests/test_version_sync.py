import os
import re

import yaml

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

MANIFEST_PATH = os.path.join(REPO_ROOT, "tunnelsats", "umbrel-app.yml")
COMPOSE_PATH = os.path.join(REPO_ROOT, "tunnelsats", "docker-compose.yml")
APP_PY_PATH = os.path.join(REPO_ROOT, "server", "app.py")
INDEX_HTML_PATH = os.path.join(REPO_ROOT, "web", "index.html")
DEPLOYMENT_K3S_PATH = os.path.join(REPO_ROOT, "k3s", "deployment.yaml")
CHANGELOG_PATH = os.path.join(REPO_ROOT, "CHANGELOG.md")

LEGACY_STATIC_RELEASE_NOTES = "Rebuilt from the ground up to support the new umbrelOS immutable architecture"

def get_manifest_version_and_notes():
    assert os.path.exists(MANIFEST_PATH), f"Manifest file missing at {MANIFEST_PATH}"
    with open(MANIFEST_PATH, "r") as f:
        data = yaml.safe_load(f)
    version = str(data.get("version", "")).strip().lstrip("v")
    notes = str(data.get("releaseNotes", "")).strip()
    return version, notes

def get_compose_version():
    assert os.path.exists(COMPOSE_PATH), f"Compose file missing at {COMPOSE_PATH}"
    with open(COMPOSE_PATH, "r") as f:
        content = f.read()
    match = re.search(r'image:\s*tunnelsats/ts-umbrel-app:v?([0-9a-zA-Z.-]+)', content)
    assert match, "Could not extract version from docker-compose.yml"
    return match.group(1).strip()

def get_app_py_version():
    assert os.path.exists(APP_PY_PATH), f"app.py missing at {APP_PY_PATH}"
    with open(APP_PY_PATH, "r") as f:
        content = f.read()
    match = re.search(r'APP_VERSION\s*=\s*["\']v?([0-9a-zA-Z.-]+)["\']', content)
    assert match, "Could not extract APP_VERSION from server/app.py"
    return match.group(1).strip()

def get_index_html_version():
    assert os.path.exists(INDEX_HTML_PATH), f"index.html missing at {INDEX_HTML_PATH}"
    with open(INDEX_HTML_PATH, "r") as f:
        content = f.read()
    match = re.search(r'id=["\']app-version["\'][^>]*>\s*v?([0-9a-zA-Z.-]+)\s*</span>', content)
    assert match, "Could not extract app-version from web/index.html"
    return match.group(1).strip()

def get_k3s_deployment_version():
    assert os.path.exists(DEPLOYMENT_K3S_PATH), f"deployment.yaml missing at {DEPLOYMENT_K3S_PATH}"
    with open(DEPLOYMENT_K3S_PATH, "r") as f:
        content = f.read()
    match = re.search(r'image:\s*tunnelsats/ts-umbrel-app:v?([0-9a-zA-Z.-]+)', content)
    assert match, "Could not extract image version from k3s/deployment.yaml"
    return match.group(1).strip()


def get_changelog_notes(version):
    assert os.path.exists(CHANGELOG_PATH), f"CHANGELOG.md missing at {CHANGELOG_PATH}"
    with open(CHANGELOG_PATH, "r", encoding="utf-8") as f:
        content = f.read()
    pattern = (
        rf"^##[ \t]+\[{re.escape(version)}\][^\n]*\n"
        rf"(?s:.*?)(?=^##[ \t]+\[|\Z)"
    )
    sections = re.findall(pattern, content, flags=re.MULTILINE)
    assert len(sections) == 1, (
        f"Expected exactly one CHANGELOG.md section for version {version}, "
        f"found {len(sections)}"
    )
    match = re.fullmatch(
        rf"##[ \t]+\[{re.escape(version)}\][^\n]*\n\n-[ \t]+([^\r\n]+)\n?",
        sections[0],
    )
    assert match, f"Could not extract release notes for version {version}"
    return match.group(1).strip()


def test_all_version_locations_are_in_sync():
    manifest_ver, notes = get_manifest_version_and_notes()
    compose_ver = get_compose_version()
    app_py_ver = get_app_py_version()
    index_ver = get_index_html_version()
    k3s_ver = get_k3s_deployment_version()

    assert manifest_ver, "Manifest version is empty"
    assert compose_ver == manifest_ver, f"docker-compose.yml version ({compose_ver}) != manifest version ({manifest_ver})"
    assert app_py_ver == manifest_ver, f"server/app.py version ({app_py_ver}) != manifest version ({manifest_ver})"
    assert index_ver == manifest_ver, f"web/index.html version ({index_ver}) != manifest version ({manifest_ver})"
    assert k3s_ver == manifest_ver, f"k3s/deployment.yaml version ({k3s_ver}) != manifest version ({manifest_ver})"


def test_manifest_release_notes_are_dynamic_and_non_empty():
    version, notes = get_manifest_version_and_notes()
    assert len(notes) > 0, "Manifest releaseNotes field is empty"
    assert LEGACY_STATIC_RELEASE_NOTES not in notes, (
        "Manifest releaseNotes contains static legacy text rather than version-specific notes"
    )
    assert get_changelog_notes(version) == notes, (
        "CHANGELOG.md notes for the current version do not match manifest releaseNotes"
    )
