# xAI subscription-auth sidecar (#397) — its OWN Node 22 image.
#
# Build context = repository root (Railway service root directory = repo root,
# config file = services/xai_auth_sidecar/railway.toml, which points
# dockerfilePath at this file — same pattern as services/santiment/sidecar).
# The shared root Dockerfile (bot, mcp, every Python sidecar) stays Node-free
# and never learns about this service. No Python, no TA-Lib, and no BuildKit
# bind mounts: Railway's builder only accepts cache-type RUN mounts.
FROM node:22-slim

ENV NODE_ENV=production \
    PORT=8787

WORKDIR /app/services/xai_auth_sidecar

# Dependencies first (cached layer) — committed lockfile, production deps only.
COPY services/xai_auth_sidecar/package.json services/xai_auth_sidecar/package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund \
    && npm cache clean --force

# Sidecar sources (node_modules is in the root .dockerignore, so the installed
# tree above is not overwritten). Credentials never live here: they go to the
# Railway Volume ($RAILWAY_VOLUME_MOUNT_PATH, recommended mount /data/grok).
COPY services/xai_auth_sidecar/ ./

EXPOSE 8787

# start.sh: Node >= 22 check, volume/credential path logging, `node src/server.js`.
CMD ["bash", "start.sh"]
