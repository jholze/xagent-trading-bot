#!/usr/bin/env bash
# Deploy xagent-test from a git branch (not local railway up upload).
# Safe: only touches Railway environment "test" / service "xagent-test".
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_NAME="${RAILWAY_TEST_ENV:-test}"
SERVICE_NAME="${RAILWAY_TEST_SERVICE:-xagent-test}"
BRANCH="${RAILWAY_TEST_BRANCH:-feature/entry-guard-15m}"
REPO="${RAILWAY_TEST_REPO:-jholze/xagent-trading-bot}"
HEALTH_URL="${RAILWAY_TEST_HEALTH_URL:-https://xagent-test-test.up.railway.app/health}"

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

echo "=== Deploy test from git ==="
echo "Branch:  ${BRANCH}"
echo "Service: ${SERVICE_NAME} (${ENV_NAME})"

railway environment link "${ENV_NAME}" >/dev/null 2>&1 || railway environment link "${ENV_NAME}"
railway service link "${SERVICE_NAME}" >/dev/null 2>&1 || railway service link "${SERVICE_NAME}"

COMMIT="$(git rev-parse --short HEAD)"
echo "Pushing ${BRANCH} @ ${COMMIT} to origin..."
git push origin "${BRANCH}"

echo "Pinning deploy revision on Railway..."
railway variable set "GIT_COMMIT=${COMMIT}" -s "${SERVICE_NAME}" -e "${ENV_NAME}" --skip-deploys
railway variable set "GIT_BRANCH=${BRANCH}" -s "${SERVICE_NAME}" -e "${ENV_NAME}" --skip-deploys
railway variable set "BOT_STACK=test" -s "${SERVICE_NAME}" -e "${ENV_NAME}" --skip-deploys

# Upload exact git HEAD (clean tree). redeploy --from-source was still building main.
echo "Deploying commit ${COMMIT} (railway up = pinned branch snapshot)..."
railway up --service "${SERVICE_NAME}" --environment "${ENV_NAME}" --detach

echo "Waiting for health..."
for i in $(seq 1 30); do
  if curl -sf -m 10 "${HEALTH_URL}" >/dev/null 2>&1; then
    echo "Health OK: ${HEALTH_URL}"
    echo ""
    echo "Verify in Telegram: /mode"
    echo "  Expect: commit from git, branch ${BRANCH}, no * (dirty)"
    exit 0
  fi
  sleep 10
done

echo "WARN: health check timed out — check logs:"
echo "  railway logs --service ${SERVICE_NAME} --environment ${ENV_NAME}"
exit 1