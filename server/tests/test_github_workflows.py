"""Guards for the GitHub Actions workflows.

From November 2, 2026 GitHub blocks ``pull_request_target`` in public
repositories that have no Actions event policy allowing it (#168), so no
workflow may use it. The Dependabot auto-merge workflow runs on
``pull_request`` instead. There, a Dependabot run gets a read-only
``GITHUB_TOKEN`` unless the workflow's ``permissions`` block raises it, and the
workflow file comes from the pull request's merge ref.
"""

import re
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
AUTO_MERGE_PATH = WORKFLOWS_DIR / "dependabot-auto-merge.yml"


def load_workflow(path):
    with path.open(encoding="utf-8") as source:
        return yaml.safe_load(source)


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

    # Approve, exemption status and auto-merge need these scopes. Without
    # them a Dependabot run on pull_request only has a read-only token.
    permissions = workflow["permissions"]
    for scope in ("contents", "pull-requests", "statuses"):
        assert permissions.get(scope) == "write", scope

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
