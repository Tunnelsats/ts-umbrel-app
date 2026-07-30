#!/usr/bin/env python3
"""
Transactional Version Bump Tool for ts-umbrel-app
Updates version string across all 5 canonical file locations:
  1. tunnelsats/umbrel-app.yml (version and releaseNotes)
  2. tunnelsats/docker-compose.yml (image tag)
  3. server/app.py (APP_VERSION)
  4. web/index.html (#app-version)
  5. k3s/deployment.yaml (image tag)
Also updates or creates CHANGELOG.md.
"""

import argparse
import json
import os
import re
import stat
import tempfile

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

MANIFEST_PATH = os.path.join(REPO_ROOT, "tunnelsats", "umbrel-app.yml")
COMPOSE_PATH = os.path.join(REPO_ROOT, "tunnelsats", "docker-compose.yml")
APP_PY_PATH = os.path.join(REPO_ROOT, "server", "app.py")
INDEX_HTML_PATH = os.path.join(REPO_ROOT, "web", "index.html")
DEPLOYMENT_K3S_PATH = os.path.join(REPO_ROOT, "k3s", "deployment.yaml")
CHANGELOG_PATH = os.path.join(REPO_ROOT, "CHANGELOG.md")
MAX_DOCKER_TAG_LENGTH = 128

SEMVER_PATTERN = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*)?$"
)


def normalize_version(version):
    clean_version = version.strip()
    if clean_version.startswith("v"):
        clean_version = clean_version[1:]
    if (
        not SEMVER_PATTERN.fullmatch(clean_version)
        or len(clean_version) > MAX_DOCKER_TAG_LENGTH
    ):
        raise ValueError(
            f"Invalid version '{version}': expected SemVer compatible with a Docker tag "
            f"of at most {MAX_DOCKER_TAG_LENGTH} characters "
            "(for example 3.3.4 or 3.3.4-rc.1)"
        )
    return clean_version


def replace_required(content, pattern, replacement, path):
    new_content, count = re.subn(
        pattern,
        replacement,
        content,
        flags=re.MULTILINE,
    )
    if count != 1:
        raise ValueError(
            f"Expected exactly one match for pattern '{pattern}' in {path}, found {count}"
        )
    return new_content


def stage_file(path, content):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    mode = stat.S_IMODE(os.stat(path).st_mode) if os.path.exists(path) else 0o644
    fd, temp_path = tempfile.mkstemp(prefix=".version-bump.", dir=directory, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as staged:
            staged.write(content)
            staged.flush()
            os.fsync(staged.fileno())
        os.chmod(temp_path, mode)
    except BaseException:
        os.unlink(temp_path)
        raise
    return temp_path


def commit_updates(updates, originals):
    staged = {}
    rollbacks = {}
    preserved_rollbacks = set()
    try:
        for path, content in updates.items():
            staged[path] = stage_file(path, content)
            if originals[path] is not None:
                rollbacks[path] = stage_file(path, originals[path])

        for path in updates:
            os.replace(staged[path], path)
            staged[path] = None
    except BaseException:
        rollback_errors = []
        replaced_paths = [
            path
            for path in updates
            if path in staged
            and (
                staged[path] is None
                or not os.path.exists(staged[path])
            )
        ]
        for path in reversed(replaced_paths):
            try:
                if originals[path] is None:
                    os.unlink(path)
                else:
                    os.replace(rollbacks[path], path)
                    rollbacks[path] = None
            except BaseException as rollback_error:
                backup_path = rollbacks.get(path)
                if backup_path and os.path.exists(backup_path):
                    preserved_rollbacks.add(path)
                    recovery_hint = f"; original preserved at {backup_path}"
                else:
                    recovery_hint = ""
                rollback_errors.append(f"{path}: {rollback_error}{recovery_hint}")
        if rollback_errors:
            raise RuntimeError(
                "Version bump failed and rollback was incomplete: "
                + "; ".join(rollback_errors)
            )
        raise
    finally:
        cleanup_paths = list(staged.values())
        cleanup_paths.extend(
            temp_path
            for path, temp_path in rollbacks.items()
            if path not in preserved_rollbacks
        )
        for temp_path in cleanup_paths:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)

def bump_version(new_version, release_notes=None):
    clean_version = normalize_version(new_version)
    clean_release_notes = release_notes.strip() if release_notes is not None else ""
    if not clean_release_notes:
        raise ValueError("Release notes are required and must not be blank")
    if "\n" in clean_release_notes or "\r" in clean_release_notes:
        raise ValueError("Release notes must be a single-line summary")
    print(f"Bumping version to {clean_version} across all canonical files...")

    paths = (
        MANIFEST_PATH,
        COMPOSE_PATH,
        APP_PY_PATH,
        INDEX_HTML_PATH,
        DEPLOYMENT_K3S_PATH,
    )
    originals = {}
    for path in paths:
        with open(path, "r", encoding="utf-8") as source:
            originals[path] = source.read()

    updates = dict(originals)
    updates[MANIFEST_PATH] = replace_required(
        updates[MANIFEST_PATH],
        r'^version:\s*["\']?[0-9a-zA-Z.-]+["\']?',
        f'version: "{clean_version}"',
        MANIFEST_PATH,
    )
    formatted_notes = json.dumps(clean_release_notes, ensure_ascii=False)
    updates[MANIFEST_PATH] = replace_required(
        updates[MANIFEST_PATH],
        r'^releaseNotes:\s*\|?\s*\n?(?:[ \t]+.*|\n?)*$',
        lambda _match: f"releaseNotes: {formatted_notes}",
        MANIFEST_PATH,
    )

    updates[COMPOSE_PATH] = replace_required(
        updates[COMPOSE_PATH],
        r'(image:\s*tunnelsats/ts-umbrel-app:v?)[0-9a-zA-Z.-]+',
        lambda match: f"{match.group(1)}{clean_version}",
        COMPOSE_PATH,
    )

    updates[APP_PY_PATH] = replace_required(
        updates[APP_PY_PATH],
        r'(APP_VERSION\s*=\s*["\']v?)[0-9a-zA-Z.-]+(["\'])',
        lambda match: f"{match.group(1)}{clean_version}{match.group(2)}",
        APP_PY_PATH,
    )

    updates[INDEX_HTML_PATH] = replace_required(
        updates[INDEX_HTML_PATH],
        r'(id=["\']app-version["\'][^>]*>\s*v?)[0-9a-zA-Z.-]+(\s*</span>)',
        lambda match: f"{match.group(1)}{clean_version}{match.group(2)}",
        INDEX_HTML_PATH,
    )

    updates[DEPLOYMENT_K3S_PATH] = replace_required(
        updates[DEPLOYMENT_K3S_PATH],
        r'(image:\s*tunnelsats/ts-umbrel-app:v?)[0-9a-zA-Z.-]+',
        lambda match: f"{match.group(1)}{clean_version}",
        DEPLOYMENT_K3S_PATH,
    )

    entry = f"## [{clean_version}]\n\n- {clean_release_notes}\n"
    if os.path.exists(CHANGELOG_PATH):
        with open(CHANGELOG_PATH, "r", encoding="utf-8") as f:
            cl_content = f.read()
        section_pattern = (
            rf"^##[ \t]+\[{re.escape(clean_version)}\][^\n]*\n"
            rf"(?s:.*?)(?=^##[ \t]+\[|\Z)"
        )
        section_count = len(re.findall(section_pattern, cl_content, flags=re.MULTILINE))
        if section_count > 1:
            raise ValueError(
                f"Found duplicate changelog sections for version {clean_version}"
            )
        originals[CHANGELOG_PATH] = cl_content
        if section_count == 1:
            updates[CHANGELOG_PATH] = re.sub(
                section_pattern,
                lambda _match: entry,
                cl_content,
                count=1,
                flags=re.MULTILINE,
            )
        else:
            if cl_content.endswith("\n\n"):
                separator = ""
            elif cl_content.endswith("\n"):
                separator = "\n"
            else:
                separator = "\n\n"
            updates[CHANGELOG_PATH] = f"{cl_content}{separator}{entry}"
    else:
        originals[CHANGELOG_PATH] = None
        updates[CHANGELOG_PATH] = f"# Changelog\n\n{entry}"

    commit_updates(updates, originals)
    for path in updates:
        print(f"Updated {os.path.relpath(path, REPO_ROOT)}")

    print("Version bump complete successfully!")

def main():
    parser = argparse.ArgumentParser(
        description="Transactionally bump all canonical version locations"
    )
    parser.add_argument("version", help="New semver version (e.g. 3.3.4)")
    parser.add_argument(
        "--notes",
        required=True,
        help="Nonblank release notes summary for umbrel-app.yml and CHANGELOG.md",
    )
    args = parser.parse_args()

    bump_version(args.version, args.notes)

if __name__ == "__main__":
    main()
