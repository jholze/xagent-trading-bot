#!/usr/bin/env bash
# Railway entry for the xAI subscription-auth sidecar (#397).
# CMD of services/xai_auth_sidecar/Dockerfile (dedicated node:22-slim image,
# selected by services/xai_auth_sidecar/railway.toml). scripts/railway_start.sh
# also dispatches here for RAILWAY_SERVICE_NAME=xagent-xai-auth / RUN_XAI_AUTH=1
# as a safety net only — the shared Python image has no Node, so we fail fast.
#
# Contract:
#   - Node 22 must be in the image: services/xai_auth_sidecar/Dockerfile
#     provides it. The main bot image (root Dockerfile) stays Node-free.
#   - A Railway Volume must be attached (recommended mount: /data/grok). The
#     credential goes to $XAI_OAUTH_CREDENTIAL_PATH, defaulting to
#     $RAILWAY_VOLUME_MOUNT_PATH/xai_oauth.json. Without a volume the session
#     dies with every deploy — we warn loudly but still start.
set -euo pipefail
cd "$(dirname "$0")"

echo "=== xAI auth sidecar (SuperGrok device-code login, session on volume) ==="

if ! command -v node >/dev/null 2>&1; then
  echo "ERROR: node not found in this image — this is the shared Python image, not the sidecar image." >&2
  echo "       Point the xagent-xai-auth service at services/xai_auth_sidecar/railway.toml (config-as-code," >&2
  echo "       dockerfilePath = services/xai_auth_sidecar/Dockerfile) and redeploy. See services/xai_auth_sidecar/README.md" >&2
  exit 1
fi
NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]')"
if [[ "${NODE_MAJOR}" -lt 22 ]]; then
  echo "ERROR: Node >= 22 required, found $(node --version)" >&2
  exit 1
fi
echo "Node $(node --version)"

# --- Volume / credential location ------------------------------------------
# Resolution (src/credentials.js): XAI_OAUTH_CREDENTIAL_PATH > RAILWAY_VOLUME_MOUNT_PATH/xai_oauth.json
# > XDG. The same rule applies to `npm run login` in a `railway ssh` shell, so nothing here
# needs to be exported for the two to agree.
if [[ -n "${RAILWAY_VOLUME_MOUNT_PATH:-}" ]]; then
  mkdir -p "${RAILWAY_VOLUME_MOUNT_PATH}"
  chmod 700 "${RAILWAY_VOLUME_MOUNT_PATH}" 2>/dev/null || true
  # Grok CLI (if the operator uses `grok login --device-auth` via railway ssh) should land on the volume too.
  export GROK_HOME="${GROK_HOME:-${RAILWAY_VOLUME_MOUNT_PATH%/}/grok-cli}"
  echo "Volume: ${RAILWAY_VOLUME_MOUNT_PATH} (name=${RAILWAY_VOLUME_NAME:-?}) | GROK_HOME=${GROK_HOME}"
elif [[ -z "${XAI_OAUTH_CREDENTIAL_PATH:-}" ]]; then
  echo "WARN: no Railway volume attached (RAILWAY_VOLUME_MOUNT_PATH unset) and XAI_OAUTH_CREDENTIAL_PATH unset." >&2
  echo "      The session will NOT survive a redeploy. Attach a volume (mount /data/grok) — see services/xai_auth_sidecar/README.md" >&2
fi
if [[ -n "${XAI_OAUTH_CREDENTIAL_PATH:-}" ]]; then
  echo "Credential path: ${XAI_OAUTH_CREDENTIAL_PATH} (explicit)"
elif [[ -n "${RAILWAY_VOLUME_MOUNT_PATH:-}" ]]; then
  echo "Credential path: ${RAILWAY_VOLUME_MOUNT_PATH%/}/xai_oauth.json (volume default)"
else
  echo "Credential path: XDG default (ephemeral!)"
fi

# --- Dependencies -----------------------------------------------------------
# Normally baked at build time (Dockerfile). Fallback for images built without
# the ARG so a hot-fix deploy still comes up (needs registry access).
if [[ ! -d node_modules/@earendil-works/pi-ai ]]; then
  echo "node_modules missing — installing sidecar dependencies (npm ci --omit=dev)"
  npm ci --omit=dev --no-audit --no-fund
fi

export PORT="${PORT:-8787}"
export NODE_ENV="${NODE_ENV:-production}"
echo "Starting server on :${PORT} (GET /health, GET /status, POST /login)"
exec node src/server.js
