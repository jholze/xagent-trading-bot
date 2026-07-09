#!/usr/bin/env bash
# Pull health + local observability exports for prod vs staging comparison.
set -euo pipefail
cd "$(dirname "$0")/.."

OUT_DIR="${STACK_OBS_OUT:-logs/remote}"
mkdir -p "$OUT_DIR"
STAMP="$(date +%Y%m%d_%H%M%S)"

STAGING_HEALTH="${RAILWAY_STAGING_HEALTH_URL:-https://xagent-test-test.up.railway.app/health/detail}"
PROD_HEALTH="${RAILWAY_PROD_HEALTH_URL:-https://xagent-bot-production.up.railway.app/health/detail}"

echo "=== Pull stack observability @ ${STAMP} ==="

if command -v curl >/dev/null 2>&1; then
  curl -sf -m 15 "$STAGING_HEALTH" -o "${OUT_DIR}/staging_health_${STAMP}.json" && echo "staging health OK" || echo "WARN: staging health failed"
  curl -sf -m 15 "$PROD_HEALTH" -o "${OUT_DIR}/prod_health_${STAMP}.json" && echo "prod health OK" || echo "WARN: prod health failed"
fi

if [[ -f logs/decisions.jsonl ]]; then
  cp logs/decisions.jsonl "${OUT_DIR}/local_decisions_${STAMP}.jsonl"
  echo "copied local decisions.jsonl"
fi
if [[ -f logs/position_snapshots.jsonl ]]; then
  cp logs/position_snapshots.jsonl "${OUT_DIR}/local_snapshots_${STAMP}.jsonl"
  echo "copied local position_snapshots.jsonl"
fi

if command -v railway >/dev/null 2>&1 && env -u RAILWAY_TOKEN railway whoami >/dev/null 2>&1; then
  env -u RAILWAY_TOKEN railway environment link test >/dev/null 2>&1 || true
  env -u RAILWAY_TOKEN railway service link xagent-test >/dev/null 2>&1 || true
  env -u RAILWAY_TOKEN railway logs --service xagent-test --environment test 2>&1 \
    | tail -500 > "${OUT_DIR}/staging_logs_${STAMP}.txt" || true
  echo "staging logs tail saved"
fi

echo "Done → ${OUT_DIR}"