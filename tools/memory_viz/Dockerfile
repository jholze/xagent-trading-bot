# Memory Cortex (#88 V2) — demo + optional Mongo + WebSocket
FROM python:3.13-slim-bookworm

WORKDIR /app

COPY requirements.txt ./tools/memory_viz/requirements.txt
RUN pip install --no-cache-dir -r ./tools/memory_viz/requirements.txt

COPY . ./tools/memory_viz/
RUN touch ./tools/__init__.py

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPATH=/app
ENV MEMORY_VIZ_HOST=0.0.0.0
# Prefer mongo when MONGO_URL is set; else demo
ENV MEMORY_VIZ_DEMO=0
ENV MEMORY_VIZ_WATCHER=1
ENV MEMORY_VIZ_POLL_SEC=5
ENV MEMORY_VIZ_ALLOW_INGEST=1

EXPOSE 8765

CMD ["sh", "-c", "python -m tools.memory_viz.server"]
