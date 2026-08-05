#!/usr/bin/env bash
# Always-local unit suite with live progress.
#
# Usage:
#   ./scripts/run_unit_tests.sh              # all tests/unit
#   ./scripts/run_unit_tests.sh -k path_stats
#   ./scripts/run_unit_tests.sh tests/unit/test_trailing_stop.py
#   UNIT_TEST_PROGRESS=0 ./scripts/run_unit_tests.sh -q   # silence progress
#   UNIT_TEST_PROGRESS_EVERY=25 ./scripts/run_unit_tests.sh -q
#
# Guarantees (via tests/conftest.py):
#   - PYTEST_RUNNING=1
#   - Mongo → local 127.0.0.1 / xagent_pytest (never Railway)
#   - demo ledger files isolated under tmp_path

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

echo "────────────────────────────────────────────"
echo "  run_unit_tests.sh  (LOCAL ONLY)"
echo "  python: $PY"
echo "  cwd:    $ROOT"
echo "  args:   ${*:-tests/unit}"
echo "────────────────────────────────────────────"

# Default target: unit only (not integration / e2e)
if [[ $# -eq 0 ]]; then
  set -- tests/unit
fi

# Default flags: line tb, show extras; user can override with more args after --
exec "$PY" -m pytest --tb=line -ra "$@"
