#!/usr/bin/env bash
# Always-local unit suite with live progress.
#
# Usage:
#   ./scripts/run_unit_tests.sh              # all tests/unit
#   ./scripts/run_unit_tests.sh -k path_stats
#   ./scripts/run_unit_tests.sh tests/unit/test_trailing_stop.py
#   UNIT_TEST_PROGRESS=0 ./scripts/run_unit_tests.sh -q   # silence progress
#   UNIT_TEST_PROGRESS_EVERY=25 ./scripts/run_unit_tests.sh -q
#   PYTEST_DB_SUFFIX=ci ./scripts/run_unit_tests.sh       # Mongo DB xagent_pytest_ci
#   ./scripts/run_unit_tests.sh --parallel                # xdist -n auto --dist loadfile
#
# Guarantees (via tests/conftest.py):
#   - PYTEST_RUNNING=1
#   - Mongo → local 127.0.0.1 / xagent_pytest (never Railway)
#     PYTEST_DB_SUFFIX=<id> (sanitized [A-Za-z0-9_]) → xagent_pytest_<id>
#     so two concurrent runs do not share a database. Unset = xagent_pytest.
#   - demo ledger files isolated under tmp_path
#   - --parallel is opt-in (not in pytest.ini addopts). Each xdist worker
#     appends PYTEST_XDIST_WORKER to the suffix so Mongo/Redis stay isolated.

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Prefer project-known local Python with deps; fall back to python3
if [[ -x /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 ]]; then
  PY="${PY:-/Library/Frameworks/Python.framework/Versions/3.13/bin/python3}"
elif command -v python3 >/dev/null 2>&1; then
  PY="${PY:-python3}"
else
  echo "ERROR: no python3 found" >&2
  exit 127
fi

export PYTEST_RUNNING=1
export UNIT_TEST_PROGRESS="${UNIT_TEST_PROGRESS:-1}"
# Keep network accidents from hitting remote by default
export MONGODB_URI="${MONGODB_URI:-mongodb://127.0.0.1:27017}"

PARALLEL=0
ARGS=()
for arg in "$@"; do
  if [[ "$arg" == "--parallel" ]]; then
    PARALLEL=1
  else
    ARGS+=("$arg")
  fi
done

# Default target: unit only (not integration / e2e)
if [[ ${#ARGS[@]} -eq 0 ]]; then
  ARGS=(tests/unit)
fi

echo "────────────────────────────────────────────"
echo "  run_unit_tests.sh  (LOCAL ONLY)"
echo "  python: $PY"
echo "  cwd:    $ROOT"
echo "  args:   ${ARGS[*]}"
if [[ "$PARALLEL" -eq 1 ]]; then
  echo "  parallel: -n auto --dist loadfile"
fi
echo "────────────────────────────────────────────"

# Default flags: line tb, show extras. --parallel is stripped and never
# forwarded to pytest; -n stays out of pytest.ini until #321 is proven.
if [[ "$PARALLEL" -eq 1 ]]; then
  exec "$PY" -m pytest --tb=line -ra -n auto --dist loadfile "${ARGS[@]}"
else
  exec "$PY" -m pytest --tb=line -ra "${ARGS[@]}"
fi
