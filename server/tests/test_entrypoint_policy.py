import os
import subprocess


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
ENTRYPOINT_PATH = os.path.join(REPO_ROOT, "scripts", "entrypoint.sh")


def run_bash(script):
    return subprocess.run(
        ["bash", "-c", script, "policy-test", ENTRYPOINT_PATH],
        check=False,
        capture_output=True,
        text=True,
    )


def test_fallback_blackhole_rule_removes_conflicts_and_is_idempotent():
    result = run_bash(
        r'''
source "$1"

RULES=$'32765:\tfrom 10.9.9.0/25 lookup main\n32765:\tfrom 10.9.9.0/25 blackhole'
DELETE_COUNT=0
ADD_COUNT=0

ip() {
    if [[ "$1 $2 $3 $4" == "rule show pref 32765" ]]; then
        printf '%s\n' "${RULES}"
        return 0
    fi
    if [[ "$1 $2" == "rule del" ]]; then
        DELETE_COUNT=$((DELETE_COUNT + 1))
        RULES=""
        return 0
    fi
    if [[ "$1 $2" == "rule add" ]]; then
        ADD_COUNT=$((ADD_COUNT + 1))
        RULES=$'32765:\tfrom 10.9.9.0/25 blackhole'
        return 0
    fi
    return 1
}

ensure_fallback_blackhole_rule "10.9.9.0/25" "test failure"
[[ "${BLACKHOLE_CHANGED}" == "1" ]]
[[ "${DELETE_COUNT}" == "2" ]]
[[ "${ADD_COUNT}" == "1" ]]
[[ "${RULES}" == $'32765:\tfrom 10.9.9.0/25 blackhole' ]]

ensure_fallback_blackhole_rule "10.9.9.0/25" "test failure"
[[ "${BLACKHOLE_CHANGED}" == "0" ]]
[[ "${DELETE_COUNT}" == "2" ]]
[[ "${ADD_COUNT}" == "1" ]]
'''
    )

    assert result.returncode == 0, result.stderr
