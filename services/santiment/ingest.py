"""HTTP ingest handler for Santiment sidecar snapshots."""

from __future__ import annotations

import os
from typing import Any

from logger import log
from services.santiment.store import store_snapshot


def santiment_ingest_enabled(config_raw: dict | None = None) -> bool:
    if config_raw is None:
        try:
            from core.config import get_bot_config

            config_raw = get_bot_config().raw
        except Exception:
            config_raw = {}
    arch = (config_raw or {}).get("architecture") or {}
    if "santiment_ingest_enabled" in arch:
        return bool(arch.get("santiment_ingest_enabled"))
    # Default on when token configured (test-friendly).
    return bool(
        (os.getenv("SANTIMENT_INGEST_TOKEN") or "").strip()
        or (os.getenv("SANTIMENT_INGEST_ENABLED") or "").strip().lower() in ("1", "true", "yes")
    )


def santiment_token_ok(provided: str | None, config_raw: dict | None = None) -> bool:
    env_token = (os.getenv("SANTIMENT_INGEST_TOKEN") or "").strip()
    if env_token:
        return (provided or "").strip() == env_token
    if config_raw is None:
        try:
            from core.config import get_bot_config

            config_raw = get_bot_config().raw
        except Exception:
            config_raw = {}
    arch = (config_raw or {}).get("architecture") or {}
    cfg_token = str(arch.get("santiment_ingest_token") or "").strip()
    if cfg_token:
        return (provided or "").strip() == cfg_token
    # No token configured → allow in local/dev only is dangerous; require token in prod.
    # For empty token: accept only if explicitly enabled without token (local tests).
    return bool(arch.get("santiment_ingest_allow_no_token", False))


def process_santiment_ingest(body: dict | None, *, config_raw: dict | None = None) -> dict[str, Any]:
    if not santiment_ingest_enabled(config_raw):
        return {"ok": False, "message": "santiment_ingest_disabled"}
    if not isinstance(body, dict):
        return {"ok": False, "message": "invalid_json"}
    try:
        meta = store_snapshot(body)
        log(
            f"santiment ingest: regime={meta.get('regime')} "
            f"(prev={meta.get('prev_regime')}) redis={meta.get('redis')}",
            "INFO",
        )
        return {"ok": True, "applied": True, **meta}
    except ValueError as e:
        return {"ok": False, "message": str(e)}
    except Exception as e:
        log(f"santiment ingest error: {e}", "WARNING")
        return {"ok": False, "message": "store_failed"}
