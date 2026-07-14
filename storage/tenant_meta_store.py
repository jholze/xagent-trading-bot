"""Leaf module for tenant-specific config and watchlist I/O.

These functions take a pre-loaded default_cfg (from _load_default_config_from_disk)
and pass it explicitly to get_database so that mongo_client.mongo_config never
calls back into get_config(). They must NEVER import or call get_config/load_config/resolve.

Used by the thin dispatchers in data_manager.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from logger import log
from storage.mongo_client import get_database

# Do not import anything from data_manager or that can pull get_config.

TENANT_CONFIGS_COLL = "tenant_configs"
TENANT_WATCHLISTS_COLL = "tenant_watchlists"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_tenant_config(tid: str, *, default_cfg: dict, test: bool = False) -> dict:
    """Return tenant-specific config body if present in mongo, else default_cfg."""
    if not tid:
        return dict(default_cfg)
    try:
        db = get_database(test=test, config=default_cfg)
        doc = db[TENANT_CONFIGS_COLL].find_one({"tenant_id": tid})
        if doc and isinstance(doc.get("body"), dict):
            return dict(doc["body"])
    except Exception as e:
        log(f"tenant_meta_store: failed load_tenant_config for {tid}: {e}", "WARNING")
    return dict(default_cfg)


def save_tenant_config(tid: str, body: dict, *, default_cfg: dict, test: bool = False) -> bool:
    if not tid or not isinstance(body, dict):
        return False
    try:
        db = get_database(test=test, config=default_cfg)
        db[TENANT_CONFIGS_COLL].replace_one(
            {"tenant_id": tid},
            {"tenant_id": tid, "body": dict(body), "updated_at": _now_iso()},
            upsert=True,
        )
        return True
    except Exception as e:
        log(f"tenant_meta_store: failed save_tenant_config for {tid}: {e}", "WARNING")
        return False


def load_tenant_watchlist(tid: str, *, default_cfg: dict, test: bool = False) -> list[dict]:
    """Return tenant watchlist coins if present, else empty list (caller may merge with defaults)."""
    if not tid:
        return []
    try:
        db = get_database(test=test, config=default_cfg)
        doc = db[TENANT_WATCHLISTS_COLL].find_one({"tenant_id": tid})
        if doc and isinstance(doc.get("coins"), list):
            coins = doc["coins"]
            seen = set()
            unique = []
            for c in coins:
                s = (c or {}).get("symbol", "")
                if s and s not in seen:
                    seen.add(s)
                    unique.append(dict(c))
            return unique
    except Exception as e:
        log(f"tenant_meta_store: failed load_tenant_watchlist for {tid}: {e}", "WARNING")
    return []


def save_tenant_watchlist(tid: str, coins: list[dict], *, default_cfg: dict, test: bool = False) -> bool:
    if not tid or not isinstance(coins, list):
        return False
    try:
        db = get_database(test=test, config=default_cfg)
        db[TENANT_WATCHLISTS_COLL].replace_one(
            {"tenant_id": tid},
            {"tenant_id": tid, "coins": [dict(c) for c in coins], "updated_at": _now_iso()},
            upsert=True,
        )
        return True
    except Exception as e:
        log(f"tenant_meta_store: failed save_tenant_watchlist for {tid}: {e}", "WARNING")
        return False
