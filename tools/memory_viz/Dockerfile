# Memory Cortex (#88) — self-contained slim image (demo mode, no torch)
FROM python:3.13-slim-bookworm

WORKDIR /app

# Context = tools/memory_viz (railway up --path-as-root)
COPY . ./tools/memory_viz/
RUN touch ./tools/__init__.py

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPATH=/app
ENV MEMORY_VIZ_DEMO=1
ENV MEMORY_VIZ_HOST=0.0.0.0

EXPOSE 8765

CMD ["sh", "-c", "python -m tools.memory_viz.server"]
