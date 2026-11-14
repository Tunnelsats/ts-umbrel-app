"""Guards for the GitHub Actions workflows.

From November 2, 2026 GitHub blocks ``pull_request_target`` in public
repositories that have no Actions event policy allowing it (#168), so no
workflow may use it. The Dependabot auto-merge workflow runs on
``pull_request`` instead. There, a Dependabot run gets a read-only
``GITHUB_TOKEN`` unless the workflow's ``permissions`` block raises it, and the
workflow file comes from the pull request's merge ref.
"""

import re
import subprocess
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
DEPENDABOT_PATH = REPO_ROOT / ".github" / "dependabot.yml"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
AUTO_MERGE_PATH = WORKFLOWS_DIR / "dependabot-auto-merge.yml"
TEST_WORKFLOW_PATH = WORKFLOWS_DIR / "test.yml"
DOCKERFILE_PATH = REPO_ROOT / "Dockerfile"
DOCKERIGNORE_PATH = REPO_ROOT / ".dockerignore"
GITIGNORE_PATH = REPO_ROOT / ".gitignore"


def load_workflow(path):
    with path.open(encoding="utf-8") as source:
        return yaml.safe_load(source)


def test_dependabot_enforces_three_day_cooldown_across_all_ecosystems():
    config = load_workflow(DEPENDABOT_PATH)
    updates = config.get("updates", [])
    assert len(updates) == 4
    for entry in updates:
        assert entry.get("cooldown", {}).get("default-days") == 3
        if entry.get("package-ecosystem") == "npm":
            assert entry.get("versioning-strategy") == "increase"


def workflow_triggers(workflow):
    # PyYAML follows YAML 1.1 and reads the bare key `on` as the boolean True.
    triggers = workflow.get("on", workflow.get(True))
    if isinstance(triggers, str):
        return {triggers: None}
    if isinstance(triggers, list):
        return dict.fromkeys(triggers)
    return dict(triggers or {})


def test_no_workflow_uses_pull_request_target():
    paths = sorted([*WORKFLOWS_DIR.glob("*.yml"), *WORKFLOWS_DIR.glob("*.yaml")])
    assert AUTO_MERGE_PATH in paths

    for path in paths:
        triggers = workflow_triggers(load_workflow(path))
        assert triggers, f"{path.name}: no triggers found"
        assert "pull_request_target" not in triggers, path.name


def test_dependabot_auto_merge_runs_on_pull_request_for_dependabot_only():
    workflow = load_workflow(AUTO_MERGE_PATH)

    assert workflow_triggers(workflow) == {
        "pull_request": {"types": ["opened", "synchronize", "reopened"]}
    }

    # Approve, exemption status and auto-merge need exactly these scopes.
    # Without them a Dependabot run on pull_request only has a read-only
    # token. A job-level block would replace them, so allow none.
    assert workflow["permissions"] == {
        "contents": "write",
        "pull-requests": "write",
        "statuses": "write",
    }
    assert "permissions" not in workflow["jobs"]["auto-merge"]

    # A second job would inherit the write permissions, so allow only this one.
    assert set(workflow["jobs"]) == {"auto-merge"}
    job = workflow["jobs"]["auto-merge"]
    guard = job["if"]
    assert "github.actor == 'dependabot[bot]'" in guard
    assert "github.event.pull_request.user.login == 'dependabot[bot]'" in guard
    assert "&&" in guard
    assert "||" not in guard

    # The job holds a write token: allow no action except the SHA-pinned
    # metadata action, so no actions/checkout and no other third-party code.
    uses = [step["uses"] for step in job["steps"] if "uses" in step]
    assert len(uses) == 1
    action, _, ref = uses[0].partition("@")
    assert action == "dependabot/fetch-metadata"
    assert re.fullmatch(r"[0-9a-f]{40}", ref)


def test_frontend_tests_workflow_uses_supported_node_lts_and_verifies_css_build():
    workflow = load_workflow(TEST_WORKFLOW_PATH)
    job = workflow["jobs"]["frontend-tests"]
    steps = job["steps"]

    setup_node_steps = [
        step for step in steps if str(step.get("uses", "")).startswith("actions/setup-node@")
    ]
    assert len(setup_node_steps) == 1
    node_version = str(setup_node_steps[0]["with"]["node-version"])
    assert node_version == "22"

    cache_paths = str(setup_node_steps[0]["with"]["cache-dependency-path"]).splitlines()
    assert "package-lock.json" in [p.strip() for p in cache_paths]
    assert "web/package-lock.json" in [p.strip() for p in cache_paths]

    run_commands = [
        (step.get("working-directory", "."), str(step.get("run", "")).strip())
        for step in steps
        if "run" in step
    ]
    assert (".", "npm ci") in run_commands
    assert (
        ".",
        "npm run build:css\ntest -s web/dist/tailwind.css",
    ) in run_commands
    assert all("git diff" not in cmd for _, cmd in run_commands)
    assert ("./web", "npm ci") in run_commands
    assert ("./web", "npm test") in run_commands

    compose_steps = workflow["jobs"]["test-compose"]["steps"]
    compose_runs = "\n".join(
        str(step.get("run", "")) for step in compose_steps if "run" in step
    )
    assert (
        "curl --fail --silent http://127.0.0.1:9740/dist/tailwind.css >/dev/null"
        in compose_runs
    )


def test_dockerfile_compiles_tailwind_css_in_multistage_build_and_git_ignores_dist():
    dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8")
    from_lines = [
        line.strip()
        for line in dockerfile.splitlines()
        if line.strip().upper().startswith("FROM ")
    ]
    assert len(from_lines) == 2
    assert from_lines[0] == "FROM --platform=$BUILDPLATFORM node:22-slim AS css-builder"
    assert from_lines[1] == "FROM debian:13-slim"
    assert "RUN npm ci --ignore-scripts" in dockerfile
    assert "RUN npm run build:css" in dockerfile
    assert (
        "COPY --from=css-builder /build/web/dist/tailwind.css /app/web/dist/tailwind.css"
        in dockerfile
    )
    assert "chmod -R a+rX /app/web" in dockerfile

    assert DOCKERIGNORE_PATH.exists()
    dockerignore_entries = {
        line.strip()
        for line in DOCKERIGNORE_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    for required_ignore in ("**/node_modules", "web/dist", ".git", "venv"):
        assert required_ignore in dockerignore_entries

    gitignore_entries = {
        line.strip()
        for line in GITIGNORE_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert "web/dist/" in gitignore_entries

    tracked = subprocess.run(
        ["git", "ls-files", "web/dist/"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert tracked.stdout.strip() == "", "web/dist/ artifacts must not be tracked in git"
