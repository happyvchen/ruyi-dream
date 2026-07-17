#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-}"

if [[ -z "${PYTHON_BIN}" ]]; then
  if [[ -x "${ROOT_DIR}/backend/.venv/bin/python" ]]; then
    PYTHON_BIN="${ROOT_DIR}/backend/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
  else
    echo "No Python runtime found. Set PYTHON_BIN=/path/to/python." >&2
    exit 1
  fi
fi

echo "== Backend tests =="
(
  cd "${ROOT_DIR}"
  PYTHONPATH=backend "${PYTHON_BIN}" -m pytest backend/tests -q
)

echo "== Frontend tests =="
(
  cd "${ROOT_DIR}/frontend"
  npm test
)

echo "== Frontend typecheck =="
(
  cd "${ROOT_DIR}/frontend"
  npm run typecheck
)

echo "== Frontend production build =="
(
  cd "${ROOT_DIR}/frontend"
  npm run build
)

echo "== Load test script availability =="
if command -v k6 >/dev/null 2>&1; then
  k6 inspect "${ROOT_DIR}/scripts/loadtest/k6-browse.js" >/dev/null
  k6 inspect "${ROOT_DIR}/scripts/loadtest/k6-write.js" >/dev/null
  echo "k6 scripts parsed successfully."
else
  echo "k6 not installed; skipped k6 script parse. Install k6 on the deployment host before final load testing."
fi

if [[ "${RUN_K6_LOADTESTS:-0}" == "1" ]]; then
  : "${BASE_URL:?Set BASE_URL before RUN_K6_LOADTESTS=1}"
  echo "== k6 browse load test =="
  k6 run -e BASE_URL="${BASE_URL}" -e USERNAME="${USERNAME:-user}" -e PASSWORD="${PASSWORD:-}" "${ROOT_DIR}/scripts/loadtest/k6-browse.js"

  if [[ "${RUN_K6_WRITE_LOADTEST:-0}" == "1" ]]; then
    echo "== k6 write load test =="
    k6 run -e BASE_URL="${BASE_URL}" -e USERNAME="${USERNAME:-user}" -e PASSWORD="${PASSWORD:-}" "${ROOT_DIR}/scripts/loadtest/k6-write.js"
  else
    echo "Skipped k6 write load test. Set RUN_K6_WRITE_LOADTEST=1 only on a prepared environment with cleanup/backup."
  fi
fi

echo "Production gate completed."
