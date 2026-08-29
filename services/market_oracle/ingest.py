"""HTTP ingest for market oracle snapshots."""

from __future__ import annotations

import os
from typing import Any

from logger import log
from services.market_oracle.store import store_snapshot


def market_oracle_ingest_enabled(config_raw: dict | None = None) -> bool:
    if config_raw is None:
        try:
            from core.config import get_bot_config

            config_raw = get_bot_config().raw
        except Exception:
            config_raw = {}
    arch = (config_raw or {}).get("architecture") or {}
    if "market_oracle_ingest_enabled" in arch:
        return bool(arch.get("market_oracle_ingest_enabled"))
    return bool(
        (os.getenv("MARKET_ORACLE_INGEST_TOKEN") or "").strip()
        or (os.getenv("MARKET_ORACLE_INGEST_ENABLED") or "").strip().lower()
        in ("1", "true", "yes")
    )


def market_oracle_token_ok(provided: str | None, config_raw: dict | None = None) -> bool:
    env_token = (os.getenv("MARKET_ORACLE_INGEST_TOKEN") or "").strip()
    if env_token:
        return (provided or "").strip() == env_token
    if config_raw is None:
        try:
            from core.config import get_bot_config

            config_raw = get_bot_config().raw
        except Exception:
            config_raw = {}
    arch = (config_raw or {}).get("architecture") or {}
    cfg_token = str(arch.get("market_oracle_ingest_token") or "").strip()
    if cfg_token:
        return (provided or "").strip() == cfg_token
    return bool(arch.get("market_oracle_ingest_allow_no_token", False))


def process_market_oracle_ingest(body: dict | None, *, config_raw: dict | None = None) -> dict[str, Any]:
    if not market_oracle_ingest_enabled(config_raw):
        return {"ok": False, "message": "market_oracle_ingest_disabled"}
    if not isinstance(body, dict):
        return {"ok": False, "message": "invalid_json"}
    try:
        meta = store_snapshot(body)
        log(
            f"market_oracle ingest: state={meta.get('state')} "
            f"(prev={meta.get('prev_state')}) redis={meta.get('redis')}",
            "INFO",
        )
        return {"ok": True, "applied": True, **meta}
    except ValueError as e:
        return {"ok": False, "message": str(e)}
    except Exception as e:
        log(f"market_oracle ingest error: {e}", "WARNING")
        return {"ok": False, "message": "store_failed"}
