"""Leaf module for tenant-specific config and watchlist I/O.

These functions take a pre-loaded default_cfg (from _load_default_config_from_disk)
and pass it explicitly to get_database so that mongo_client.mongo_config never
calls back into get_config(). They must NEVER import or call get_config/load_config/resolve.

Used by the thin dispatchers in data_manager.
"""

from __future__ import annotations

import copy
import os
from datetime import datetime, timezone

from logger import log
from storage.errors import LedgerUnavailable, LedgerWriteFailed
from storage.mongo_client import get_database

# Do not import anything from data_manager or that can pull get_config.

TENANT_CONFIGS_COLL = "tenant_configs"
TENANT_WATCHLISTS_COLL = "tenant_watchlists"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_tenant_config_body(tid: str, *, default_cfg: dict, test: bool = False) -> dict | None:
    """Return tenant-specific config body from mongo, or None if not stored."""
    if not tid:
        return None
    try:
        db = get_database(test=test, config=default_cfg)
        doc = db[TENANT_CONFIGS_COLL].find_one({"tenant_id": tid})
        if doc and isinstance(doc.get("body"), dict):
            return dict(doc["body"])
    except LedgerUnavailable:
        raise
    except Exception as e:
        log(f"tenant_meta_store: failed load_tenant_config_body for {tid}: {e}", "ERROR")
        raise LedgerUnavailable(
            op="load_tenant_config_body", tenant_id=tid, cause=e
        ) from e
    return None


def load_tenant_config(tid: str, *, default_cfg: dict, test: bool = False) -> dict:
    """Return tenant body if present, else default_cfg (legacy callers)."""
    body = load_tenant_config_body(tid, default_cfg=default_cfg, test=test)
    if body is not None:
        return body
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


def _flatten_patch(prefix: str, updates: dict, out: dict) -> None:
    """Expand a nested patch into dotted ``$set`` paths under ``prefix``.

    Non-empty dicts recurse (so sibling keys in the stored body survive);
    scalars, lists and empty dicts replace the value at that path.
    """
    for key, val in updates.items():
        skey = str(key)
        if not skey or "." in skey or skey.startswith("$"):
            raise ValueError(f"unsupported config key for patch: {key!r}")
        path = f"{prefix}.{skey}"
        if isinstance(val, dict) and val:
            _flatten_patch(path, val, out)
        else:
            out[path] = copy.deepcopy(val)


def patch_tenant_config(tid: str, updates: dict, *, default_cfg: dict, test: bool = False) -> bool:
    """Deep-merge ``updates`` into the stored tenant body (#456).

    Only the given keys are written (``$set`` on dotted paths); everything
    else in the body — and every key the tenant inherits from the operator
    ``config.json`` / profile preset — stays untouched. Contrast with
    :func:`save_tenant_config`, which replaces the whole body.
    """
    if not tid or not isinstance(updates, dict):
        return False
    if not updates:
        return True
    try:
        set_doc: dict = {}
        _flatten_patch("body", updates, set_doc)
        set_doc["updated_at"] = _now_iso()
        db = get_database(test=test, config=default_cfg)
        db[TENANT_CONFIGS_COLL].update_one(
            {"tenant_id": tid},
            {"$set": set_doc},
            upsert=True,
        )
        return True
    except Exception as e:
        log(f"tenant_meta_store: failed patch_tenant_config for {tid}: {e}", "WARNING")
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
    except LedgerUnavailable:
        raise
    except Exception as e:
        log(f"tenant_meta_store: failed load_tenant_watchlist for {tid}: {e}", "ERROR")
        raise LedgerUnavailable(
            op="load_tenant_watchlist", tenant_id=tid, cause=e
        ) from e
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
        log(f"tenant_meta_store: failed save_tenant_watchlist for {tid}: {e}", "ERROR")
        raise LedgerWriteFailed(op="save_tenant_watchlist", tenant_id=tid, cause=e) from e
