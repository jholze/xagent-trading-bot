"""Env config for market oracle (no bot imports)."""

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


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def load_config() -> dict:
    return {
        "bot_ingest_url": (os.getenv("BOT_INGEST_URL") or "").strip(),
        "bot_ingest_token": (
            os.getenv("BOT_INGEST_TOKEN") or os.getenv("MARKET_ORACLE_INGEST_TOKEN") or ""
        ).strip(),
        "poll_interval_sec": max(60, _int("POLL_INTERVAL_SEC", 300)),
        "heartbeat_sec": max(300, _int("HEARTBEAT_SEC", 1800)),
        "size_delta_push": _float("SIZE_DELTA_PUSH", 0.1),
        "dry_run": _bool("DRY_RUN", False),
        "port": _int("PORT", 8080),
        "log_level": (os.getenv("LOG_LEVEL") or "INFO").upper(),
        "schema_version": 1,
        "ttl_sec": max(300, _int("SNAPSHOT_TTL_SEC", 900)),
        # Thresholds (align with market-context plan)
        "btc_risk_off_24h_pct": _float("BTC_RISK_OFF_24H_PCT", -3.0),
        "btc_crash_24h_pct": _float("BTC_CRASH_24H_PCT", -6.0),
        "btc_risk_on_24h_pct": _float("BTC_RISK_ON_24H_PCT", 1.0),
        # A1: 1h cascade CRASH threshold; RISK_ON blocked if 1h at/below floor
        "btc_cascade_1h_pct": _float("BTC_CASCADE_1H_PCT", -2.5),
        "btc_risk_on_1h_floor_pct": _float("BTC_RISK_ON_1H_FLOOR_PCT", -1.0),
        "risk_off_size_mult": _float("RISK_OFF_SIZE_MULT", 0.35),
        "neutral_size_mult": _float("NEUTRAL_SIZE_MULT", 0.85),
        "min_bars_to_flip": max(1, _int("MIN_BARS_TO_FLIP", 2)),
    }
