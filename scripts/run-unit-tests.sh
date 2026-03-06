#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-${ROOT_DIR}/.venv-tests}"
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
