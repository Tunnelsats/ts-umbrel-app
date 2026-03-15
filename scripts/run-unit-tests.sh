#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
if [ -z "${VENV_DIR:-}" ]; then
  # Prioritize workspace root .venv as Source of Truth (SOT)
  WORKSPACE_ROOT_VENV="$(cd "${ROOT_DIR}/.." && pwd)/.venv"
  VENV_DIR="${WORKSPACE_ROOT_VENV}"

  # Fallback to local .venv if workspace root is missing
  if [ ! -d "${VENV_DIR}" ] && [ -d "${ROOT_DIR}/.venv" ]; then
    VENV_DIR="${ROOT_DIR}/.venv"
  fi
fi
REQ_FILE="${ROOT_DIR}/server/requirements-test.txt"
REQ_HASH_FILE="${VENV_DIR}/.requirements-test.sha256"

if [ ! -d "${VENV_DIR}" ]; then
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

# shellcheck disable=SC1091
. "${VENV_DIR}/bin/activate"

REQ_HASH="$(sha256sum "${REQ_FILE}" | awk '{print $1}')"
INSTALLED_HASH="$(cat "${REQ_HASH_FILE}" 2>/dev/null || true)"

if [ "${REQ_HASH}" != "${INSTALLED_HASH}" ]; then
  python -m pip install --upgrade pip -q
  python -m pip install -q -r "${REQ_FILE}"
  printf '%s' "${REQ_HASH}" > "${REQ_HASH_FILE}"
fi

python -m pytest "${ROOT_DIR}/server/tests" "$@"
