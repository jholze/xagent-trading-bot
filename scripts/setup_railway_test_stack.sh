#!/usr/bin/env bash
# Provision an isolated Railway test stack entirely via CLI.
# Usage: bash scripts/setup_railway_test_stack.sh
# Optional: RAILWAY_TEST_ENV=test RAILWAY_TEST_SERVICE=xagent-test bash scripts/setup_railway_test_stack.sh
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_NAME="${RAILWAY_TEST_ENV:-test}"
SERVICE_NAME="${RAILWAY_TEST_SERVICE:-xagent-test}"
REPO="${RAILWAY_TEST_REPO:-jholze/xagent-trading-bot}"
BRANCH="${RAILWAY_TEST_BRANCH:-feature/entry-guard-15m}"

if ! command -v railway >/dev/null 2>&1; then
  echo "❌ railway CLI missing — install: brew install railway"
  exit 1
fi

if ! railway whoami >/dev/null 2>&1; then
  echo "❌ Not logged in — run: railway login"
  exit 1
fi

echo "=== Railway test stack setup ==="
echo "Project:  $(railway status 2>/dev/null | awk '/^Project:/{print $2}' || echo '?')"
echo "Target env: ${ENV_NAME}"
echo "Bot service: ${SERVICE_NAME}"

# --- Load local secrets (.env base, .env.local overrides dev Telegram token) ---
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi
if [[ -f .env.local ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env.local
  set +a
fi

if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then
  echo "❌ TELEGRAM_BOT_TOKEN missing — set in .env.local (dev bot, not production)"
  exit 1
fi
if [[ -z "${TELEGRAM_CHAT_ID:-}" ]]; then
  echo "❌ TELEGRAM_CHAT_ID missing — set in .env or run scripts/get_telegram_chat_id.sh"
  exit 1
fi

# --- Create / link test environment ---
ENV_EXISTS="$(
  railway environment list --json 2>/dev/null \
    | python3 -c "import json,sys; env='${ENV_NAME}'; data=json.load(sys.stdin); print('yes' if any(e.get('name')==env for e in data.get('environments',[])) else 'no')"
)"

if [[ "$ENV_EXISTS" != "yes" ]]; then
  echo "Creating environment: ${ENV_NAME}"
  railway environment new "${ENV_NAME}" --json >/dev/null
else
  echo "Environment exists: ${ENV_NAME}"
fi

railway environment link "${ENV_NAME}" --json >/dev/null 2>&1 || railway environment link "${ENV_NAME}"
echo "Linked environment: ${ENV_NAME}"

service_exists() {
  local name="$1"
  railway service list --json 2>/dev/null \
    | python3 -c "import json,sys; n=sys.argv[1]; data=json.load(sys.stdin); print('yes' if any(s.get('name')==n for s in data) else 'no')" "$name"
}

find_service_by_image() {
  local needle="$1"
  railway service list --json 2>/dev/null \
    | python3 -c "
import json,sys
needle=sys.argv[1].lower()
for s in json.load(sys.stdin):
    img=(s.get('source') or {}).get('image') or ''
    if needle in img.lower():
        print(s['name'])
        break
" "$needle"
}

# --- MongoDB ---
MONGO_SVC="$(find_service_by_image mongo || true)"
if [[ -z "$MONGO_SVC" ]]; then
  echo "Adding MongoDB..."
  railway add --database mongo --json >/dev/null
  sleep 3
  MONGO_SVC="$(find_service_by_image mongo)"
fi
echo "Mongo service: ${MONGO_SVC}"

# --- Redis ---
REDIS_SVC="$(find_service_by_image redis || true)"
if [[ -z "$REDIS_SVC" ]]; then
  echo "Adding Redis..."
  railway add --database redis --json >/dev/null
  sleep 3
  REDIS_SVC="$(find_service_by_image redis)"
fi
echo "Redis service: ${REDIS_SVC}"

# --- Bot service ---
if [[ "$(service_exists "$SERVICE_NAME")" != "yes" ]]; then
  echo "Adding bot service: ${SERVICE_NAME} (branch ${BRANCH})"
  railway add --repo "${REPO}" --branch "${BRANCH}" --service "${SERVICE_NAME}" --json >/dev/null 2>&1 \
    || railway add --service "${SERVICE_NAME}" --json >/dev/null
  sleep 2
fi

railway service link "${SERVICE_NAME}" --json >/dev/null 2>&1 || railway service link "${SERVICE_NAME}"
echo "Linked service: ${SERVICE_NAME}"

# --- Public domain ---
DOMAIN_JSON="$(railway domain list --service "${SERVICE_NAME}" --json 2>/dev/null || echo '{"domains":[]}')"
PUBLIC_DOMAIN="$(
  echo "$DOMAIN_JSON" | python3 -c "
import json,sys
data=json.load(sys.stdin)
domains=data.get('domains', data if isinstance(data, list) else [])
for d in domains:
    dom=d.get('domain') or d.get('host') or ''
    if dom:
        print(dom); break
" 2>/dev/null || true
)"
if [[ -z "$PUBLIC_DOMAIN" ]]; then
  echo "Generating Railway domain..."
  railway domain --service "${SERVICE_NAME}" --json >/dev/null 2>&1 || railway domain --service "${SERVICE_NAME}" >/dev/null
  sleep 3
  DOMAIN_JSON="$(railway domain list --service "${SERVICE_NAME}" --json 2>/dev/null || echo '{"domains":[]}')"
  PUBLIC_DOMAIN="$(
    echo "$DOMAIN_JSON" | python3 -c "
import json,sys
data=json.load(sys.stdin)
for d in data.get('domains', []):
    dom=d.get('domain') or ''
    if dom:
        print(dom); break
" 2>/dev/null || true
  )"
fi
if [[ -z "$PUBLIC_DOMAIN" ]]; then
  PUBLIC_DOMAIN="$(
    railway service list --json 2>/dev/null | python3 -c "
import json,sys
name=sys.argv[1]
for s in json.load(sys.stdin):
    if s.get('name')==name and s.get('url'):
        print(s['url'].replace('https://','').replace('http://','').rstrip('/'))
        break
" "${SERVICE_NAME}"
  )"
fi
WEBHOOK_BASE="https://${PUBLIC_DOMAIN}"
echo "Public URL: ${WEBHOOK_BASE}"

# --- Variables (batch, skip redeploy until end) ---
set_var() {
  railway variable set "$1" --service "${SERVICE_NAME}" --environment "${ENV_NAME}" --skip-deploys --json >/dev/null
}

set_var_stdin() {
  local key="$1"
  local val="$2"
  printf '%s' "$val" | railway variable set "${key}" --stdin --service "${SERVICE_NAME}" --environment "${ENV_NAME}" --skip-deploys --json >/dev/null
}

echo "Setting environment variables..."
set_var_stdin TELEGRAM_BOT_TOKEN "${TELEGRAM_BOT_TOKEN}"
set_var_stdin TELEGRAM_CHAT_ID "${TELEGRAM_CHAT_ID}"
set_var MONGODB_DB=xagent_test
set_var DEMO_MODE=1
set_var DEMO_LEDGER_BACKEND=mongo
set_var RAILWAY_DEPLOY=1
set_var BOT_STACK=test
set_var "WEBHOOK_BASE_URL=${WEBHOOK_BASE}"

# Railway service references (private networking)
set_var "MONGO_URL=\${{${MONGO_SVC}.MONGO_URL}}"
set_var "REDIS_URL=\${{${REDIS_SVC}.REDIS_URL}}"

[[ -n "${GATE_API_KEY:-}" ]] && set_var_stdin GATE_API_KEY "${GATE_API_KEY}"
[[ -n "${GATE_API_SECRET:-}" ]] && set_var_stdin GATE_API_SECRET "${GATE_API_SECRET}"
[[ -n "${CMC_API_KEY:-}" ]] && set_var_stdin CMC_API_KEY "${CMC_API_KEY}"
[[ -n "${XAI_API_KEY:-}" ]] && set_var_stdin OPENAI_API_KEY "${XAI_API_KEY}"
[[ -n "${X_API_BEARER_TOKEN:-}" ]] && set_var_stdin X_BEARER_TOKEN "${X_API_BEARER_TOKEN}"
[[ -n "${LUNARCRUSH_API_KEY:-}" ]] && set_var_stdin LUNARCRUSH_API_KEY "${LUNARCRUSH_API_KEY}"
[[ -n "${SIGNAL_WEBHOOK_TOKEN:-}" ]] && set_var_stdin SIGNAL_WEBHOOK_TOKEN "${SIGNAL_WEBHOOK_TOKEN}"

echo "Connecting GitHub source (branch ${BRANCH})..."
railway service source connect \
  --repo "${REPO}" \
  --branch "${BRANCH}" \
  --service "${SERVICE_NAME}" 2>/dev/null || true

if git status --porcelain | grep -q .; then
  DEPLOY_COMMIT="unknown"
else
  DEPLOY_COMMIT="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
fi
set_var "GIT_COMMIT=${DEPLOY_COMMIT}"
set_var "GIT_BRANCH=${BRANCH}"

echo "Deploy from git: bash scripts/deploy_test_branch.sh"
echo "  (push → GitHub Actions → Railway; no redeploy --from-source)"

echo ""
echo "=== Test stack ready ==="
echo "Environment:  ${ENV_NAME}"
echo "Service:      ${SERVICE_NAME}"
echo "Health:       ${WEBHOOK_BASE}/health"
echo "Health detail:${WEBHOOK_BASE}/health/detail"
echo "Signal hook:  ${WEBHOOK_BASE}/api/signals/webhook?source=tradingview"
echo "Coin prices:  ${WEBHOOK_BASE}/api/coins/prices"
echo ""
echo "Switch CLI context:"
echo "  railway environment link ${ENV_NAME}"
echo "  railway service link ${SERVICE_NAME}"
echo ""
echo "Logs: railway logs --service ${SERVICE_NAME} --environment ${ENV_NAME}"