# syntax=docker/dockerfile:1
# Desk SPA (Vite) — bake dist into the Python image. Do not commit dist/ or node_modules.
FROM node:22-slim AS desk
WORKDIR /desk
COPY tools/desk/package.json tools/desk/package-lock.json ./
RUN npm ci
COPY tools/desk ./
RUN npm run build

# Fast Railway image: TA-Lib comes as a manylinux wheel (0.6+) — no Sourceforge
# C compile (was make -j1 + wget to prdownloads.sourceforge.net every cold build).
FROM python:3.13-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -U pip \
    && pip install --no-cache-dir -r requirements.txt \
    && python -c "import talib; print('talib', getattr(talib, '__version__', 'ok'))"

COPY . .
COPY --from=desk /desk/dist /app/tools/desk/dist

# --- #397 xAI subscription-auth sidecar: Node 22 ONLY for that service ------
# Railway passes a service variable as build ARG when it is declared here. Only
# the `xagent-xai-auth` service sets RUN_XAI_AUTH=1, so for the bot and every
# other sidecar this RUN is a no-op and the image stays Node-free (same ARG
# value -> shared layer cache). The Node binary + npm come from the `desk`
# stage (node:22-slim, also Debian bookworm/glibc) — no download, pinned by tag.
ARG RUN_XAI_AUTH=
RUN --mount=type=bind,from=desk,source=/usr/local,target=/mnt/node-usr-local \
    if [ "$RUN_XAI_AUTH" = "1" ]; then \
      echo "RUN_XAI_AUTH=1: baking Node 22 + xai_auth_sidecar deps into this image" \
      && cp -a /mnt/node-usr-local/bin/node /usr/local/bin/node \
      && cp -a /mnt/node-usr-local/lib/node_modules /usr/local/lib/ \
      && ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
      && ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
      && node --version && npm --version \
      && cd services/xai_auth_sidecar \
      && npm ci --omit=dev --no-audit --no-fund \
      && rm -rf /root/.npm; \
    else \
      echo "RUN_XAI_AUTH unset: image stays Node-free"; \
    fi

# Bake commit/branch when Railway (or CI) injects git env at build time.
# runtime railway_start will not overwrite a real commit with "unknown".
ARG RAILWAY_GIT_COMMIT_SHA=
ARG RAILWAY_GIT_BRANCH=
ARG GIT_COMMIT=
ARG GIT_BRANCH=
RUN RAILWAY_GIT_COMMIT_SHA="$RAILWAY_GIT_COMMIT_SHA" \
    RAILWAY_GIT_BRANCH="$RAILWAY_GIT_BRANCH" \
    GIT_COMMIT="$GIT_COMMIT" \
    GIT_BRANCH="$GIT_BRANCH" \
    python3 scripts/write_build_meta.py || true

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

EXPOSE 5000

CMD ["bash", "scripts/railway_start.sh"]
