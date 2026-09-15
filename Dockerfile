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
