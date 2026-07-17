"""Environment config for the Santiment sidecar (no bot imports)."""

from __future__ import annotations

import os


def _bool(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


def load_config() -> dict:
    return {
        "api_key": (os.getenv("SANTIMENT_API_KEY") or "").strip(),
        "bot_ingest_url": (os.getenv("BOT_INGEST_URL") or "").strip(),
        "bot_ingest_token": (os.getenv("BOT_INGEST_TOKEN") or os.getenv("SANTIMENT_INGEST_TOKEN") or "").strip(),
        "poll_interval_sec": max(60, _int("POLL_INTERVAL_SEC", 900)),
        "heartbeat_sec": max(300, _int("HEARTBEAT_SEC", 3600)),
        "size_delta_push": float(os.getenv("SIZE_DELTA_PUSH", "0.1") or 0.1),
        "dry_run": _bool("DRY_RUN", False),
        "port": _int("PORT", 8080),
        "log_level": (os.getenv("LOG_LEVEL") or "INFO").upper(),
        "schema_version": 1,
        "ttl_sec": max(300, _int("SNAPSHOT_TTL_SEC", 1800)),
    }
