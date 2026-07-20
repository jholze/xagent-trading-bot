FROM python:3.13-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    wget \
    && rm -rf /var/lib/apt/lists/* \
    && wget -q http://prdownloads.sourceforge.net/ta-lib/ta-lib-0.4.0-src.tar.gz \
    && tar -xzf ta-lib-0.4.0-src.tar.gz \
    && cd ta-lib \
    && ./configure --prefix=/usr \
    && make -j1 \
    && make install \
    && cd .. \
    && rm -rf ta-lib ta-lib-0.4.0-src.tar.gz \
    && ldconfig

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

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