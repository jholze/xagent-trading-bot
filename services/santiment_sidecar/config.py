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
    # lean (default): 4 policy metrics/poll. full: +dev +social +leverage (~10–12 calls).
    profile = (os.getenv("SANTIMENT_METRIC_PROFILE") or "lean").strip().lower()
    if profile not in ("lean", "full"):
        profile = "lean"
    return {
        "api_key": (os.getenv("SANTIMENT_API_KEY") or "").strip(),
        "bot_ingest_url": (os.getenv("BOT_INGEST_URL") or "").strip(),
        "bot_ingest_token": (os.getenv("BOT_INGEST_TOKEN") or os.getenv("SANTIMENT_INGEST_TOKEN") or "").strip(),
        # Default 1h — thrift API budget target (~2.2–3k Sanbase calls/month with lean metrics).
        "poll_interval_sec": max(60, _int("POLL_INTERVAL_SEC", 3600)),
        "heartbeat_sec": max(300, _int("HEARTBEAT_SEC", 3600)),
        "size_delta_push": float(os.getenv("SIZE_DELTA_PUSH", "0.1") or 0.1),
        "dry_run": _bool("DRY_RUN", False),
        "port": _int("PORT", 8080),
        "log_level": (os.getenv("LOG_LEVEL") or "INFO").upper(),
        "schema_version": 1,
        "ttl_sec": max(300, _int("SNAPSHOT_TTL_SEC", 3600)),
        "metric_profile": profile,
        # Space GraphQL calls to stay under per-minute limits.
        "inter_request_delay_sec": max(
            0.0, float(os.getenv("SANTIMENT_INTER_REQUEST_DELAY_SEC", "0.35") or 0.35)
        ),
        "abort_on_rate_limit": _bool("SANTIMENT_ABORT_ON_429", True),
        # After 429, sleep at least this many seconds before next full poll.
        "rate_limit_backoff_sec": max(300, _int("SANTIMENT_RATE_LIMIT_BACKOFF_SEC", 7200)),
        "fetch_social": _bool("SANTIMENT_FETCH_SOCIAL", profile == "full"),
        "fetch_leverage": _bool("SANTIMENT_FETCH_LEVERAGE", profile == "full"),
        "fetch_dev": _bool("SANTIMENT_FETCH_DEV", profile == "full"),
        # Lagged leverage research window doubles calls — off in thrifty mode.
        "leverage_research_fallback": _bool(
            "SANTIMENT_LEVERAGE_RESEARCH_FALLBACK", profile == "full"
        ),
    }
