#!/usr/bin/env python3
"""
Atomic Version Bump Tool for ts-umbrel-app
Updates version string across all 5 canonical file locations:
  1. tunnelsats/umbrel-app.yml (version and releaseNotes)
  2. tunnelsats/docker-compose.yml (image tag)
  3. server/app.py (APP_VERSION)
  4. web/index.html (#app-version)
  5. k3s/deployment.yaml (image tag)
Also updates or creates CHANGELOG.md.
"""

import sys
import os
import re
import argparse

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

MANIFEST_PATH = os.path.join(REPO_ROOT, "tunnelsats", "umbrel-app.yml")
COMPOSE_PATH = os.path.join(REPO_ROOT, "tunnelsats", "docker-compose.yml")
APP_PY_PATH = os.path.join(REPO_ROOT, "server", "app.py")
INDEX_HTML_PATH = os.path.join(REPO_ROOT, "web", "index.html")
DEPLOYMENT_K3S_PATH = os.path.join(REPO_ROOT, "k3s", "deployment.yaml")
CHANGELOG_PATH = os.path.join(REPO_ROOT, "CHANGELOG.md")

def update_file(path, pattern, replacement):
    with open(path, "r") as f:
        content = f.read()
    new_content, count = re.subn(pattern, replacement, content, flags=re.MULTILINE)
    if count == 0:
        raise ValueError(f"Pattern '{pattern}' not found in {path}")
    with open(path, "w") as f:
        f.write(new_content)
    print(f"Updated {os.path.relpath(path, REPO_ROOT)}")

def bump_version(new_version, release_notes=None):
    clean_version = new_version.strip().lstrip("v")
    print(f"Bumping version to {clean_version} across all canonical files...")

    # 1. tunnelsats/umbrel-app.yml
    update_file(
        MANIFEST_PATH,
        r'^version:\s*["\']?[0-9a-zA-Z.-]+["\']?',
        f'version: "{clean_version}"'
    )
    if release_notes:
        formatted_notes = release_notes.strip().replace('"', '\\"')
        update_file(
            MANIFEST_PATH,
            r'^releaseNotes:\s*\|?\s*\n?(?:[ \t]+.*|\n?)*$',
            f'releaseNotes: "{formatted_notes}"'
        )

    # 2. tunnelsats/docker-compose.yml
    update_file(
        COMPOSE_PATH,
        r'(image:\s*tunnelsats/ts-umbrel-app:v?)[0-9a-zA-Z.-]+',
        rf'\g<1>{clean_version}'
    )

    # 3. server/app.py
    update_file(
        APP_PY_PATH,
        r'(APP_VERSION\s*=\s*["\']v?)[0-9a-zA-Z.-]+(["\'])',
        rf'\g<1>{clean_version}\g<2>'
    )

    # 4. web/index.html
    update_file(
        INDEX_HTML_PATH,
        r'(id=["\']app-version["\'][^>]*>\s*v?)[0-9a-zA-Z.-]+(\s*</span>)',
        rf'\g<1>{clean_version}\g<2>'
    )

    # 5. k3s/deployment.yaml
    update_file(
        DEPLOYMENT_K3S_PATH,
        r'(image:\s*tunnelsats/ts-umbrel-app:v?)[0-9a-zA-Z.-]+',
        rf'\g<1>{clean_version}'
    )

    # 6. Update CHANGELOG.md if release_notes provided
    if release_notes:
        entry = f"\n## [{clean_version}]\n\n- {release_notes.strip()}\n"
        if os.path.exists(CHANGELOG_PATH):
            with open(CHANGELOG_PATH, "r") as f:
                cl_content = f.read()
            if f"[{clean_version}]" not in cl_content:
                with open(CHANGELOG_PATH, "w") as f:
                    f.write(cl_content + entry)
                print(f"Appended version {clean_version} to CHANGELOG.md")
        else:
            with open(CHANGELOG_PATH, "w") as f:
                f.write(f"# Changelog\n{entry}")
            print(f"Created CHANGELOG.md with version {clean_version}")

    print("Version bump complete successfully!")

def main():
    parser = argparse.ArgumentParser(description="Bump version atomically across all 5 file locations")
    parser.add_argument("version", help="New semver version (e.g. 3.3.4)")
    parser.add_argument("--notes", help="Release notes summary for umbrel-app.yml and CHANGELOG.md", default=None)
    args = parser.parse_args()

    bump_version(args.version, args.notes)

if __name__ == "__main__":
    main()
