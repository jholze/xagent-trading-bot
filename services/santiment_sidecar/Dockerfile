FROM python:3.13-slim-bookworm

WORKDIR /app

RUN pip install --no-cache-dir requests==2.32.3

# Copy only sidecar + package path from monorepo root context.
COPY services/santiment_sidecar ./services/santiment_sidecar

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PORT=8080

EXPOSE 8080

CMD ["python", "-m", "services.santiment_sidecar"]
