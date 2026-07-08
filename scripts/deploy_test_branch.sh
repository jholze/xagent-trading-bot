#!/usr/bin/env bash
# Deploy xagent-test via git push (GitHub → Railway). No railway up.
# /mode uses RAILWAY_GIT_* from the webhook build; GIT_COMMIT/GIT_BRANCH are backup.
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_NAME="${RAILWAY_TEST_ENV:-test}"
SERVICE_NAME="${RAILWAY_TEST_SERVICE:-xagent-test}"
BRANCH="${RAILWAY_TEST_BRANCH:-feature/entry-guard-15m}"
REPO="${RAILWAY_TEST_REPO:-jholze/xagent-trading-bot}"
HEALTH_URL="${RAILWAY_TEST_HEALTH_URL:-https://xagent-test-test.up.railway.app/health}"
HEALTH_DETAIL_URL="${RAILWAY_TEST_HEALTH_DETAIL_URL:-https://xagent-test-test.up.railway.app/health/detail}"

if ! command -v railway >/dev/null 2>&1; then
  echo "ERROR: railway CLI missing"
  exit 1
fi
if ! railway whoami >/dev/null 2>&1; then
  echo "ERROR: railway login required"
  exit 1
fi

CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
if [[ "$CURRENT_BRANCH" != "$BRANCH" ]]; then
  echo "ERROR: expected branch ${BRANCH}, on ${CURRENT_BRANCH}"
  echo "  git checkout ${BRANCH}"
  exit 1
fi

DIRTY="$(git status --porcelain)"
if [[ -n "$DIRTY" ]]; then
  echo "ERROR: uncommitted changes — commit first so test matches git:"
  git status --short | head -20
  exit 1
fi

echo "=== Deploy test from git push ==="
echo "Branch:  ${BRANCH}"
echo "Service: ${SERVICE_NAME} (${ENV_NAME})"
echo "Repo:    ${REPO} (source must be linked to this branch)"

railway environment link "${ENV_NAME}" >/dev/null 2>&1 || railway environment link "${ENV_NAME}"
railway service link "${SERVICE_NAME}" >/dev/null 2>&1 || railway service link "${SERVICE_NAME}"

COMMIT="$(git rev-parse --short HEAD)"

echo "Ensuring GitHub source is linked to ${BRANCH}..."
railway service source connect \
  --repo "${REPO}" \
  --branch "${BRANCH}" \
  --service "${SERVICE_NAME}" 2>/dev/null || true

echo "Pushing ${BRANCH} @ ${COMMIT} to origin..."
git push origin "${BRANCH}"

echo "Syncing backup build vars (runtime fallback for /mode)..."
railway variable set "GIT_COMMIT=${COMMIT}" -s "${SERVICE_NAME}" -e "${ENV_NAME}" --skip-deploys
railway variable set "GIT_BRANCH=${BRANCH}" -s "${SERVICE_NAME}" -e "${ENV_NAME}" --skip-deploys
railway variable set "BOT_STACK=test" -s "${SERVICE_NAME}" -e "${ENV_NAME}" --skip-deploys

# Push triggers GitHub webhook deploy (injects RAILWAY_GIT_COMMIT_SHA / RAILWAY_GIT_BRANCH).
# Do NOT use redeploy --from-source here — it has pulled the wrong branch before.
echo "Waiting for GitHub webhook deploy (no railway up / no redeploy --from-source)..."

echo "Waiting for health (build may take several minutes)..."
for i in $(seq 1 45); do
  if curl -sf -m 10 "${HEALTH_URL}" >/dev/null 2>&1; then
    BUILD_COMMIT=""
    if command -v jq >/dev/null 2>&1; then
      BUILD_COMMIT="$(curl -sf -m 10 "${HEALTH_DETAIL_URL}" 2>/dev/null | jq -r '.build.commit // empty' || true)"
    fi
    echo "Health OK: ${HEALTH_URL}"
    if [[ -n "${BUILD_COMMIT}" && "${BUILD_COMMIT}" != "${COMMIT}" && "${BUILD_COMMIT}" != "unknown" ]]; then
      echo "WARN: /health/detail build.commit=${BUILD_COMMIT} != pushed ${COMMIT} — deploy may still be rolling"
    else
      echo ""
      echo "Verify in Telegram: /mode"
      echo "  Expect: ${COMMIT} · ${BRANCH} · test (no * dirty)"
      exit 0
    fi
  fi
  sleep 10
done

echo "WARN: health check timed out — check logs:"
echo "  railway logs --service ${SERVICE_NAME} --environment ${ENV_NAME}"
exit 1