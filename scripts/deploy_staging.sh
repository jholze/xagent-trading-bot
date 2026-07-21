#!/usr/bin/env bash
# Deploy Railway staging instance (xagent-test): git push → GitHub integration.
# Requires Railway service source: repo jholze/xagent-trading-bot + branch staging.
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_NAME="${RAILWAY_STAGING_ENV:-${RAILWAY_TEST_ENV:-test}}"
SERVICE_NAME="${RAILWAY_STAGING_SERVICE:-${RAILWAY_TEST_SERVICE:-xagent-test}}"
BRANCH="${RAILWAY_STAGING_BRANCH:-staging}"
HEALTH_URL="${RAILWAY_STAGING_HEALTH_URL:-${RAILWAY_TEST_HEALTH_URL:-https://xagent-test-test.up.railway.app/health}}"
HEALTH_DETAIL_URL="${RAILWAY_STAGING_HEALTH_DETAIL_URL:-${RAILWAY_TEST_HEALTH_DETAIL_URL:-https://xagent-test-test.up.railway.app/health/detail}}"

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

# Only block on modified tracked files (untracked analysis/ops scripts are OK).
DIRTY="$(git status --porcelain | grep -v '^??' || true)"
if [[ -n "$DIRTY" ]]; then
  echo "ERROR: uncommitted tracked changes — commit first:"
  echo "$DIRTY" | head -20
  exit 1
fi

echo "=== Deploy staging via git push ==="
echo "Branch:   ${BRANCH}"
echo "Service:  ${SERVICE_NAME} (env ${ENV_NAME})"
echo "Flow:     push → Railway GitHub webhook (no RAILWAY_TOKEN / no GitHub Actions)"
echo ""
echo "Railway must have repo + branch connected on service ${SERVICE_NAME}."
echo "  Dashboard: Service → Settings → Source → GitHub repo + branch staging"

railway environment link "${ENV_NAME}" >/dev/null 2>&1 || railway environment link "${ENV_NAME}"
railway service link "${SERVICE_NAME}" >/dev/null 2>&1 || railway service link "${SERVICE_NAME}"

COMMIT="$(git rev-parse HEAD)"
SHORT="$(git rev-parse --short HEAD)"

if [[ "${SKIP_MT_SMOKE:-}" == "1" ]]; then
  echo "WARN: SKIP_MT_SMOKE=1 — skipping pre-push multi-tenant smoke"
else
  echo "=== Pre-push: local verification (MT + trending sync + ledger guards) ==="
  bash scripts/verify_pre_staging.sh
fi

echo ""
echo "Pushing ${BRANCH} @ ${SHORT} to origin..."
git push origin "${BRANCH}"

echo ""
echo "Waiting for Railway build (up to ~10 min)..."
DEPLOY_SEEN=""
for i in $(seq 1 60); do
  if DEPLOY_JSON="$(railway deployment list --service "${SERVICE_NAME}" --environment "${ENV_NAME}" --json 2>/dev/null)"; then
    DEPLOY_STATUS="$(echo "$DEPLOY_JSON" | python3 -c "
import json,sys
target=sys.argv[1].lower()
data=json.load(sys.stdin)
items=data if isinstance(data,list) else data.get('deployments',[])
for d in items[:8]:
    h=(d.get('meta') or {}).get('commitHash','').lower()
    if h.startswith(target) or target.startswith(h[:7]):
        print(d.get('status','?'))
        break
" "$COMMIT" 2>/dev/null || true)"
    if [[ -n "$DEPLOY_STATUS" ]]; then
      DEPLOY_SEEN="$DEPLOY_STATUS"
      echo "  Railway deployment: ${DEPLOY_STATUS} (attempt ${i}/60)"
      if [[ "$DEPLOY_STATUS" == "SUCCESS" ]]; then
        break
      fi
      if [[ "$DEPLOY_STATUS" == "FAILED" || "$DEPLOY_STATUS" == "CRASHED" ]]; then
        echo "ERROR: deployment ${DEPLOY_STATUS}"
        echo "  railway logs --service ${SERVICE_NAME} --environment ${ENV_NAME}"
        exit 1
      fi
    else
      echo "  Waiting for deployment row (attempt ${i}/60)..."
    fi
  fi

  if command -v jq >/dev/null 2>&1 && curl -sf -m 10 "${HEALTH_DETAIL_URL}" >/dev/null 2>&1; then
    LIVE="$(curl -sf -m 10 "${HEALTH_DETAIL_URL}" 2>/dev/null | jq -r '.build.commit // empty' || true)"
    if [[ "$LIVE" == "$SHORT" ]]; then
      echo ""
      echo "Health OK — live commit ${LIVE}"
      echo "Verify in Telegram: /mode"
      echo "  Expect: ${SHORT} · ${BRANCH} · staging"
      echo "  Soft-reload runs automatically on bot start (new commit → all scopes;"
      echo "  same commit restart → cache only). Manual: /reload"
      exit 0
    fi
  fi

  sleep 10
done

if [[ -z "$DEPLOY_SEEN" ]]; then
  echo ""
  echo "ERROR: no Railway deployment started within 10 minutes."
  echo "  Check Railway Dashboard → ${SERVICE_NAME} → Deployments"
  echo "  Ensure GitHub source is connected and branch trigger is ${BRANCH}"
  exit 1
fi

if curl -sf -m 10 "${HEALTH_URL}" >/dev/null 2>&1; then
  echo ""
  echo "Health OK: ${HEALTH_URL}"
  echo "Verify in Telegram: /mode"
  exit 0
fi

echo "WARN: deployment ${DEPLOY_SEEN} but health check timed out"
echo "  railway logs --service ${SERVICE_NAME} --environment ${ENV_NAME}"
exit 1