# Build context: services/santiment_sidecar/  (railway up from this directory)
# Or monorepo root with dockerfilePath = services/santiment_sidecar/Dockerfile
# and context adjusted — prefer `railway up` from this folder.
FROM python:3.13-slim-bookworm

WORKDIR /app

RUN pip install --no-cache-dir "requests>=2.32,<3"

# Layout so `python -m services.santiment_sidecar` works
COPY . ./services/santiment_sidecar/

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPATH=/app
ENV PORT=8080

EXPOSE 8080

CMD ["python", "-m", "services.santiment_sidecar"]
