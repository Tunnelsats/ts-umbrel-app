#!/usr/bin/env python3
"""Official Umbrel App Store promotion transformer for TunnelSats.

Validates the canonical Community Store manifests under tunnelsats/ and emits
deterministic, linter-compliant Official Umbrel App Store manifests matching
getumbrel/umbrel-apps PR #6433 parity without brittle sed mutations.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml


DEFAULT_SUBMISSION_URL = "https://github.com/getumbrel/umbrel-apps/pull/4919"
DEFAULT_SUBMITTER = "Tunnelsats"
DEFAULT_OFFICIAL_GALLERY = ["1.webp", "2.webp", "3.webp", "4.webp"]
COMMUNITY_SETUP_SENTENCE = (
    "On the Community App Store, TunnelSats can configure your Lightning Node or "
    "Core Lightning automatically (or show manual steps if you enable Secure Mode)."
)
OFFICIAL_SETUP_SENTENCE = (
    "After you activate it, the app shows the settings to add to your Lightning Node "
    "or Core Lightning config, and you restart that app yourself."
)
COMMUNITY_UNINSTALL_STORAGE_SENTENCE = (
    "On the Community App Store, your WireGuard config is kept in persistent storage "
    "(tunnelsats-data) across uninstalls; download a backup in the app if you plan to "
    "move to the Official Umbrel App Store or another node."
)
OFFICIAL_UNINSTALL_STORAGE_SENTENCE = (
    "Uninstalling also deletes your WireGuard config, so download a backup in the app "
    "first if you want to restore your subscription later."
)
IMAGE_REPO = "tunnelsats/ts-umbrel-app"
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
VERSION_PATTERN = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*)?$"
)
URL_PATTERN = re.compile(r"^https?://\S+$")
LOCAL_GALLERY_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _environment_items(service: dict[str, Any]) -> list[tuple[str, str]]:
    """Return ordered (key, value) pairs from a Compose environment block."""
    raw = service.get("environment")
    if isinstance(raw, dict):
        return [(str(k), "" if v is None else str(v)) for k, v in raw.items()]
    if isinstance(raw, list):
        items: list[tuple[str, str]] = []
        for entry in raw:
            if not isinstance(entry, str) or "=" not in entry:
                raise ValueError(f"Unsupported Compose environment entry: {entry!r}")
            key, value = entry.split("=", 1)
            items.append((key, value))
        return items
    raise ValueError("Compose service is missing a valid 'environment' mapping or list")


def _environment_map(service: dict[str, Any]) -> dict[str, str]:
    return dict(_environment_items(service))


def _yaml_scalar_line(value: Any, *, quote_strings: bool = False) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if quote_strings:
        return json.dumps(text, ensure_ascii=False)
    if (
        text == ""
        or text != text.strip()
        or any(ch in text for ch in ("\n", "\r", ":", "#", '"', "'", "[", "]", "{", "}", ",", "&", "*", "!", "|", ">", "%", "@", "`"))
        or text.lower() in {"true", "false", "yes", "no", "on", "off", "null", "~"}
    ):
        return json.dumps(text, ensure_ascii=False)
    return text


def _format_yaml_value(value: Any, indent: int, *, quote_strings: bool = True) -> list[str]:
    """Deterministically serialize nested YAML structures preserving key order."""
    prefix = " " * indent
    if isinstance(value, dict):
        if not value:
            return [f"{prefix}{{}}"]
        lines: list[str] = []
        for key, item in value.items():
            if isinstance(item, dict) and item:
                lines.append(f"{prefix}{key}:")
                lines.extend(_format_yaml_value(item, indent + 2, quote_strings=quote_strings))
            elif isinstance(item, list) and item:
                lines.append(f"{prefix}{key}:")
                lines.extend(_format_yaml_value(item, indent + 2, quote_strings=quote_strings))
            elif isinstance(item, list):
                lines.append(f"{prefix}{key}: []")
            elif isinstance(item, dict):
                lines.append(f"{prefix}{key}: {{}}")
            else:
                lines.append(f"{prefix}{key}: {_yaml_scalar_line(item, quote_strings=quote_strings)}")
        return lines
    if isinstance(value, list):
        if not value:
            return [f"{prefix}[]"]
        lines = []
        for item in value:
            if isinstance(item, dict) and item:
                entries = list(item.items())
                first_key, first_val = entries[0]
                if isinstance(first_val, (dict, list)) and first_val:
                    lines.append(f"{prefix}- {first_key}:")
                    lines.extend(_format_yaml_value(first_val, indent + 4, quote_strings=quote_strings))
                elif isinstance(first_val, list):
                    lines.append(f"{prefix}- {first_key}: []")
                elif isinstance(first_val, dict):
                    lines.append(f"{prefix}- {first_key}: {{}}")
                else:
                    lines.append(
                        f"{prefix}- {first_key}: {_yaml_scalar_line(first_val, quote_strings=quote_strings)}"
                    )
                for key, subval in entries[1:]:
                    sub_prefix = " " * (indent + 2)
                    if isinstance(subval, (dict, list)) and subval:
                        lines.append(f"{sub_prefix}{key}:")
                        lines.extend(_format_yaml_value(subval, indent + 4, quote_strings=quote_strings))
                    elif isinstance(subval, list):
                        lines.append(f"{sub_prefix}{key}: []")
                    elif isinstance(subval, dict):
                        lines.append(f"{sub_prefix}{key}: {{}}")
                    else:
                        lines.append(
                            f"{sub_prefix}{key}: {_yaml_scalar_line(subval, quote_strings=quote_strings)}"
                        )
            else:
                lines.append(f"{prefix}- {_yaml_scalar_line(item, quote_strings=quote_strings)}")
        return lines
    return [f"{prefix}{_yaml_scalar_line(value, quote_strings=quote_strings)}"]


def _format_folded_description(description: str) -> str:
    """Format a multi-paragraph description as a YAML >- folded scalar."""
    normalized = description.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    # In YAML folded scalars, N consecutive newlines in the block yield N-1 newlines
    # in the parsed string for unindented lines. Expanding each newline run by +1
    # preserves exact paragraph and bullet spacing on round-trip.
    expanded = re.sub(r"\n+", lambda match: "\n" * (len(match.group(0)) + 1), normalized)
    body_lines = [f"  {line}" if line else "" for line in expanded.split("\n")]
    return "description: >-\n" + "\n".join(body_lines)


def _format_env_entry(key: str, value: str) -> str:
    entry = f"{key}={value}"
    # Quote only if YAML would misinterpret the entry as a non-string or mapping.
    if any(ch in entry for ch in ("\n", "\r", "#")) or entry.startswith((" ", "-", "?", ":", "[", "]", "{", "}", '"', "'")):
        return f"      - {json.dumps(entry, ensure_ascii=False)}"
    return f"      - {entry}"


def normalize_version(version: str) -> str:
    clean = version.strip()
    if clean.startswith("v"):
        clean = clean[1:]
    if not VERSION_PATTERN.fullmatch(clean):
        raise ValueError(f"Invalid version {version!r}: expected SemVer (for example 3.6.1)")
    return clean


def validate_digest(digest: str) -> str:
    clean = digest.strip()
    if not DIGEST_PATTERN.fullmatch(clean):
        raise ValueError(f"Invalid digest {digest!r}: expected sha256:<64 hex chars>")
    return clean


def validate_submission_url(submission_url: str) -> str:
    clean = submission_url.strip()
    if not URL_PATTERN.fullmatch(clean):
        raise ValueError(f"Invalid submission URL {submission_url!r}: expected http(s) URL")
    return clean


def validate_source_compose(compose: Any) -> dict[str, Any]:
    if not isinstance(compose, dict):
        raise ValueError("Source docker-compose.yml must parse to a YAML mapping")
    services = compose.get("services")
    if not isinstance(services, dict):
        raise ValueError("Source docker-compose.yml is missing 'services'")

    for required in ("app_proxy", "tunnelsats-web", "tunnelsats-daemon"):
        if not isinstance(services.get(required), dict):
            raise ValueError(f"Source docker-compose.yml is missing required service '{required}'")

    proxy = services["app_proxy"]
    proxy_env = _environment_map(proxy)
    if str(proxy_env.get("APP_PORT", "")) != "9740":
        raise ValueError(f"Expected app_proxy APP_PORT '9740', found {proxy_env.get('APP_PORT')!r}")
    if not proxy_env.get("APP_HOST"):
        raise ValueError("Source app_proxy is missing APP_HOST")

    web = services["tunnelsats-web"]
    if not str(web.get("image", "")).startswith(f"{IMAGE_REPO}:"):
        raise ValueError(f"Unexpected tunnelsats-web image: {web.get('image')!r}")
    if web.get("restart") != "on-failure":
        raise ValueError(f"Expected tunnelsats-web restart 'on-failure', found {web.get('restart')!r}")
    if web.get("network_mode") == "host":
        raise ValueError("tunnelsats-web must not use host networking")
    if str(web.get("user", "")) != "1000:1000":
        raise ValueError(f"Expected tunnelsats-web user '1000:1000', found {web.get('user')!r}")
    if web.get("cap_drop") != ["ALL"]:
        raise ValueError(f"Expected tunnelsats-web cap_drop ['ALL'], found {web.get('cap_drop')!r}")
    web_sec = web.get("security_opt") or []
    if not isinstance(web_sec, list) or "no-new-privileges:true" not in web_sec:
        raise ValueError("tunnelsats-web must set security_opt: [no-new-privileges:true]")
    web_env = _environment_map(web)
    expected_web_env = {
        "TUNNELSATS_ROLE": "web",
        "TUNNELSATS_DAEMON_SOCKET": "/run/tunnelsats/daemon.sock",
        "DASHBOARD_BIND_HOST": "0.0.0.0",
        "DASHBOARD_BIND_PORT": "9740",
        "MANAGEMENT_BROWSER_SECURITY_ENABLED": "true",
        "MANAGEMENT_TRUSTED_PROXY_HOST": "app_proxy",
    }
    for key, expected in expected_web_env.items():
        if web_env.get(key) != expected:
            raise ValueError(f"Expected tunnelsats-web env {key}={expected!r}, found {web_env.get(key)!r}")
    web_volumes = [str(v) for v in (web.get("volumes") or [])]
    if not any(v.endswith(":/data") for v in web_volumes):
        raise ValueError("tunnelsats-web is missing :/data volume mount")
    if "${APP_DATA_DIR}/runtime:/run/tunnelsats" not in web_volumes:
        raise ValueError("tunnelsats-web is missing ${APP_DATA_DIR}/runtime:/run/tunnelsats mount")

    daemon = services["tunnelsats-daemon"]
    if not str(daemon.get("image", "")).startswith(f"{IMAGE_REPO}:"):
        raise ValueError(f"Unexpected tunnelsats-daemon image: {daemon.get('image')!r}")
    if daemon.get("restart") != "on-failure":
        raise ValueError(f"Expected tunnelsats-daemon restart 'on-failure', found {daemon.get('restart')!r}")
    if daemon.get("network_mode") != "host":
        raise ValueError("tunnelsats-daemon must use network_mode: 'host'")
    daemon_caps = daemon.get("cap_add") or []
    if not isinstance(daemon_caps, list) or "NET_ADMIN" not in daemon_caps:
        raise ValueError("tunnelsats-daemon must include NET_ADMIN in cap_add")
    daemon_env = _environment_map(daemon)
    expected_daemon_env = {
        "TUNNELSATS_ROLE": "daemon",
        "TUNNELSATS_DAEMON_SOCKET": "/run/tunnelsats/daemon.sock",
        "TUNNELSATS_WEB_UID": "1000",
        "TUNNELSATS_WEB_GID": "1000",
        "MANAGEMENT_BROWSER_SECURITY_ENABLED": "false",
    }
    for key, expected in expected_daemon_env.items():
        if daemon_env.get(key) != expected:
            raise ValueError(f"Expected tunnelsats-daemon env {key}={expected!r}, found {daemon_env.get(key)!r}")
    if "SECURE_MODE" not in daemon_env:
        raise ValueError("tunnelsats-daemon is missing SECURE_MODE environment variable")
    daemon_volumes = [str(v) for v in (daemon.get("volumes") or [])]
    if not any(v.endswith(":/data") for v in daemon_volumes):
        raise ValueError("tunnelsats-daemon is missing :/data volume mount")
    if "${APP_DATA_DIR}/runtime:/run/tunnelsats" not in daemon_volumes:
        raise ValueError("tunnelsats-daemon is missing ${APP_DATA_DIR}/runtime:/run/tunnelsats mount")
    if not any(":/lightning-data/lnd" in v for v in daemon_volumes):
        raise ValueError("tunnelsats-daemon is missing :/lightning-data/lnd volume mount")
    if not any(":/lightning-data/cln" in v for v in daemon_volumes):
        raise ValueError("tunnelsats-daemon is missing :/lightning-data/cln volume mount")

    return compose


def render_official_compose(source_compose: dict[str, Any], version: str, digest: str) -> str:
    validate_source_compose(source_compose)
    pinned_image = f"{IMAGE_REPO}:{version}@{digest}"

    web_env_items = _environment_items(source_compose["services"]["tunnelsats-web"])
    daemon_env_items = _environment_items(source_compose["services"]["tunnelsats-daemon"])

    lines: list[str] = [
        "services:",
        "  app_proxy:",
        "    environment:",
        "      APP_HOST: tunnelsats_tunnelsats-web_1",
        '      APP_PORT: "9740"',
        "",
        "  tunnelsats-web:",
        f"    image: {pinned_image}",
        "    restart: on-failure",
        '    user: "1000:1000"',
        "    cap_drop:",
        "      - ALL",
        "    security_opt: [no-new-privileges:true]",
        "    environment:",
    ]
    for key, value in web_env_items:
        lines.append(_format_env_entry(key, value))
    lines.extend(
        [
            "    volumes:",
            "      - ${APP_DATA_DIR}/data:/data",
            "      - ${APP_DATA_DIR}/runtime:/run/tunnelsats",
            "",
            "  # Host network and NET_ADMIN are needed to create the WireGuard interface and",
            "  # the routing and firewall rules that send Lightning traffic through it",
            "  tunnelsats-daemon:",
            f"    image: {pinned_image}",
            "    restart: on-failure",
            '    network_mode: "host"',
            "    cap_add:",
            "      - NET_ADMIN",
            "    environment:",
        ]
    )
    for key, value in daemon_env_items:
        if key == "SECURE_MODE":
            lines.append("      # Secure Mode never edits Lightning config or uses the Docker socket")
            lines.append("      - SECURE_MODE=true")
        else:
            lines.append(_format_env_entry(key, value))
    lines.extend(
        [
            "    volumes:",
            "      - ${APP_DATA_DIR}/data:/data",
            "      - ${APP_DATA_DIR}/runtime:/run/tunnelsats",
            "      # Read-only, to detect the node and show the right setup steps. Only one",
            "      # of these exists; Docker creates the other as an empty directory, and the",
            "      # Lightning app fills it normally if it is installed later.",
            "      - ${APP_DATA_DIR}/../lightning/data/lnd:/lightning-data/lnd:ro",
            "      - ${APP_DATA_DIR}/../core-lightning/data/lightningd:/lightning-data/cln:ro",
            "",
        ]
    )
    rendered = "\n".join(lines)

    # Round-trip validation before returning
    parsed = yaml.safe_load(rendered)
    if not isinstance(parsed, dict) or "version" in parsed:
        raise ValueError("Generated official docker-compose.yml failed structure verification")
    proxy_env = _environment_map(parsed["services"]["app_proxy"])
    if proxy_env.get("APP_HOST") != "tunnelsats_tunnelsats-web_1" or "PROXY_AUTH_ADD" in proxy_env:
        raise ValueError("Generated app_proxy environment failed verification")
    daemon_env = _environment_map(parsed["services"]["tunnelsats-daemon"])
    if daemon_env.get("SECURE_MODE") != "true":
        raise ValueError("Generated tunnelsats-daemon SECURE_MODE must be hard-pinned to 'true'")
    return rendered


def resolve_official_gallery(existing_manifest_path: Path | None) -> list[str]:
    if existing_manifest_path is None or not existing_manifest_path.exists():
        return list(DEFAULT_OFFICIAL_GALLERY)

    existing = yaml.safe_load(existing_manifest_path.read_text(encoding="utf-8"))
    if not isinstance(existing, dict):
        raise ValueError(f"Existing official manifest {existing_manifest_path} is not a YAML mapping")

    gallery = existing.get("gallery")
    if isinstance(gallery, list) and gallery:
        valid_entries: list[str] = []
        seen: set[str] = set()
        for entry in gallery:
            if not isinstance(entry, str):
                return list(DEFAULT_OFFICIAL_GALLERY)
            cleaned = entry.strip()
            if (
                not cleaned
                or "://" in cleaned
                or cleaned.startswith("/")
                or ".." in cleaned
                or not LOCAL_GALLERY_NAME_PATTERN.fullmatch(cleaned)
                or cleaned in seen
            ):
                return list(DEFAULT_OFFICIAL_GALLERY)
            seen.add(cleaned)
            valid_entries.append(cleaned)
        return valid_entries

    return list(DEFAULT_OFFICIAL_GALLERY)


def validate_source_manifest(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise ValueError("Source umbrel-app.yml must parse to a YAML mapping")
    required_fields = (
        "manifestVersion",
        "id",
        "category",
        "name",
        "version",
        "tagline",
        "description",
        "developer",
        "website",
        "dependencies",
        "repo",
        "support",
        "port",
        "path",
    )
    for field in required_fields:
        if field not in manifest:
            raise ValueError(f"Source umbrel-app.yml is missing required field '{field}'")
    if manifest["id"] != "tunnelsats":
        raise ValueError(f"Expected manifest id 'tunnelsats', found {manifest['id']!r}")
    if not isinstance(manifest["description"], str) or not manifest["description"].strip():
        raise ValueError("Source umbrel-app.yml description must be a non-empty string")
    if not isinstance(manifest["dependencies"], list):
        raise ValueError("Source umbrel-app.yml dependencies must be a list")
    return manifest


def adapt_description_for_official_store(description: str) -> str:
    """Adapt Community Store storage and setup notices to Official Store Secure Mode semantics."""
    adapted = description.replace(COMMUNITY_SETUP_SENTENCE, OFFICIAL_SETUP_SENTENCE)
    adapted = adapted.replace(
        COMMUNITY_UNINSTALL_STORAGE_SENTENCE, OFFICIAL_UNINSTALL_STORAGE_SENTENCE
    )
    return adapted


def render_official_manifest(
    source_manifest: dict[str, Any],
    version: str,
    submission_url: str,
    gallery: list[str],
) -> str:
    validate_source_manifest(source_manifest)

    tagline = str(source_manifest["tagline"]).strip().rstrip(".")
    description = adapt_description_for_official_store(str(source_manifest["description"]))
    release_notes = str(source_manifest.get("releaseNotes", ""))
    default_username = str(source_manifest.get("defaultUsername", ""))
    default_password = str(source_manifest.get("defaultPassword", ""))
    path_val = str(source_manifest.get("path", ""))
    dependencies = source_manifest["dependencies"]
    widgets = source_manifest.get("widgets")

    lines: list[str] = [
        f"manifestVersion: {source_manifest['manifestVersion']}",
        f"id: {source_manifest['id']}",
        "storage:",
        "  dataRoot: data",
        f"category: {source_manifest['category']}",
        f"name: {_yaml_scalar_line(source_manifest['name'])}",
        f"version: {json.dumps(version, ensure_ascii=False)}",
        f"tagline: {_yaml_scalar_line(tagline)}",
        _format_folded_description(description),
        f"releaseNotes: {json.dumps(release_notes, ensure_ascii=False)}",
        "",
        f"developer: {_yaml_scalar_line(source_manifest['developer'])}",
        f"website: {source_manifest['website']}",
    ]

    if dependencies:
        lines.append("dependencies:")
        for dep in dependencies:
            lines.append(f"  - {_yaml_scalar_line(dep)}")
    else:
        lines.append("dependencies: []")

    lines.extend(
        [
            f"repo: {source_manifest['repo']}",
            f"support: {source_manifest['support']}",
            "",
            f"port: {source_manifest['port']}",
            "gallery:",
        ]
    )
    for item in gallery:
        lines.append(f"  - {item}")
    lines.extend(
        [
            f"path: {json.dumps(path_val, ensure_ascii=False)}",
            "",
            f"defaultUsername: {json.dumps(default_username, ensure_ascii=False)}",
            f"defaultPassword: {json.dumps(default_password, ensure_ascii=False)}",
        ]
    )

    if widgets is not None:
        lines.append("")
        lines.append("widgets:")
        lines.extend(_format_yaml_value(widgets, 2, quote_strings=True))

    lines.extend(
        [
            "",
            f"submitter: {DEFAULT_SUBMITTER}",
            f"submission: {submission_url}",
            "",
        ]
    )
    rendered = "\n".join(lines)

    # Round-trip verification to guarantee lossless YAML output
    parsed = yaml.safe_load(rendered)
    if not isinstance(parsed, dict):
        raise ValueError("Generated official umbrel-app.yml failed YAML parsing")
    if "icon" in parsed:
        raise ValueError("Generated official umbrel-app.yml must not include 'icon'")
    if parsed.get("storage") != {"dataRoot": "data"}:
        raise ValueError("Generated official umbrel-app.yml missing storage.dataRoot: data")
    if parsed.get("description") != description.replace("\r\n", "\n").replace("\r", "\n").strip("\n"):
        raise ValueError("Generated official umbrel-app.yml description failed round-trip check")
    if parsed.get("releaseNotes") != release_notes:
        raise ValueError("Generated official umbrel-app.yml releaseNotes failed round-trip check")
    if parsed.get("gallery") != gallery:
        raise ValueError("Generated official umbrel-app.yml gallery failed round-trip check")
    if widgets is not None and parsed.get("widgets") != widgets:
        raise ValueError("Generated official umbrel-app.yml widgets failed round-trip check")
    if list(parsed.keys())[-2:] != ["submitter", "submission"]:
        raise ValueError("Generated official umbrel-app.yml must end with submitter and submission")
    return rendered


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o644)
        os.replace(temp_name, path)
    except BaseException:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
        raise


def promote_official_store(
    source_dir: Path,
    target_dir: Path,
    version: str,
    digest: str,
    submission_url: str = DEFAULT_SUBMISSION_URL,
    existing_manifest: Path | None = None,
) -> None:
    clean_version = normalize_version(version)
    clean_digest = validate_digest(digest)
    clean_submission_url = validate_submission_url(submission_url)

    source_compose_path = source_dir / "docker-compose.yml"
    source_manifest_path = source_dir / "umbrel-app.yml"
    if not source_compose_path.is_file():
        raise FileNotFoundError(f"Missing source compose file: {source_compose_path}")
    if not source_manifest_path.is_file():
        raise FileNotFoundError(f"Missing source manifest file: {source_manifest_path}")

    source_compose = yaml.safe_load(source_compose_path.read_text(encoding="utf-8"))
    source_manifest = yaml.safe_load(source_manifest_path.read_text(encoding="utf-8"))
    gallery = resolve_official_gallery(existing_manifest)

    rendered_compose = render_official_compose(source_compose, clean_version, clean_digest)
    rendered_manifest = render_official_manifest(
        source_manifest,
        clean_version,
        clean_submission_url,
        gallery,
    )

    target_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target_dir / "docker-compose.yml", rendered_compose)
    atomic_write_text(target_dir / "umbrel-app.yml", rendered_manifest)
    atomic_write_text(target_dir / "data" / ".gitkeep", "")
    atomic_write_text(target_dir / "runtime" / ".gitkeep", "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate Official Umbrel App Store manifests from tunnelsats/ source."
    )
    parser.add_argument("--source-dir", required=True, type=Path, help="Source tunnelsats/ directory")
    parser.add_argument("--target-dir", required=True, type=Path, help="Target official store tunnelsats/ directory")
    parser.add_argument("--version", required=True, help="Release SemVer (for example 3.6.1)")
    parser.add_argument("--digest", required=True, help="Multi-arch image digest (sha256:...)")
    parser.add_argument(
        "--submission-url",
        default=DEFAULT_SUBMISSION_URL,
        help=f"Official store PR submission URL (default: {DEFAULT_SUBMISSION_URL})",
    )
    parser.add_argument(
        "--existing-manifest",
        type=Path,
        default=None,
        help="Optional existing official store umbrel-app.yml to preserve local gallery entries",
    )
    args = parser.parse_args(argv)

    try:
        promote_official_store(
            source_dir=args.source_dir,
            target_dir=args.target_dir,
            version=args.version,
            digest=args.digest,
            submission_url=args.submission_url,
            existing_manifest=args.existing_manifest,
        )
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
