"""Per-tenant grid plan persistence (Phase B rest).

Primary: Mongo collection ``grid_plans`` keyed by tenant + ledger scope.
Read-only fallback: leftover ``config.grid_states`` until the one-shot
startup migrate has copied those keys into Mongo and stripped the config key.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from logger import log
from storage.errors import LedgerUnavailable

GRID_PLANS_COLLECTION = "grid_plans"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _plan_key(symbol: str, timeframe: str) -> str:
    return f"{symbol}_{timeframe}"


def _doc_id(tenant_id: str, scope: str) -> str:
    from storage.tenant_keys import compound_ledger_id

    return compound_ledger_id(tenant_id, scope)


def _resolve_tenant_scope(
    tenant_id: str | None = None,
    scope: str | None = None,
) -> tuple[str, str]:
    from core.tenant_context import resolve_tenant_id, resolve_tenant_scope

    return resolve_tenant_id(tenant_id), resolve_tenant_scope(scope)


def load_grid_plans_document(
    *,
    tenant_id: str | None = None,
    scope: str | None = None,
    test: bool = False,
) -> dict[str, Any]:
    """Return ``{plans: {key: plan_dict}, ...}`` (empty plans if missing)."""
    tid, sc = _resolve_tenant_scope(tenant_id, scope)
    empty = {
        "tenant_id": tid,
        "ledger_scope": sc,
        "plans": {},
    }
    try:
        from storage.mongo_client import get_database

        db = get_database(test=test)
        doc = db[GRID_PLANS_COLLECTION].find_one({"_id": _doc_id(tid, sc)})
        if not doc:
            return empty
        plans = doc.get("plans") if isinstance(doc.get("plans"), dict) else {}
        return {
            "tenant_id": tid,
            "ledger_scope": sc,
            "plans": dict(plans),
            "updated_at": doc.get("updated_at"),
        }
    except LedgerUnavailable:
        raise
    except Exception as e:
        log(f"grid_plan_store load failed ({tid}/{sc}): {e}", "ERROR")
        raise LedgerUnavailable(
            op="load_grid_plans_document", tenant_id=tid, scope=sc, cause=e
        ) from e


def save_grid_plans_document(
    plans: dict[str, dict],
    *,
    tenant_id: str | None = None,
    scope: str | None = None,
    test: bool = False,
) -> bool:
    tid, sc = _resolve_tenant_scope(tenant_id, scope)
    try:
        from storage.mongo_client import assert_safe_dev_db_mutation, get_database, resolve_database_name

        assert_safe_dev_db_mutation(resolve_database_name(test=test), action="write")
        db = get_database(test=test)
        payload = {
            "_id": _doc_id(tid, sc),
            "tenant_id": tid,
            "ledger_scope": sc,
            "plans": dict(plans or {}),
            "updated_at": _now_iso(),
        }
        db[GRID_PLANS_COLLECTION].replace_one(
            {"_id": payload["_id"]}, payload, upsert=True,
        )
        return True
    except Exception as e:
        log(f"grid_plan_store save failed ({tid}/{sc}): {e}", "WARNING")
        return False


def load_grid_plan(
    symbol: str,
    timeframe: str,
    *,
    tenant_id: str | None = None,
    scope: str | None = None,
    test: bool = False,
) -> dict | None:
    key = _plan_key(symbol, timeframe)
    doc = load_grid_plans_document(tenant_id=tenant_id, scope=scope, test=test)
    plan = (doc.get("plans") or {}).get(key)
    if isinstance(plan, dict) and plan:
        return dict(plan)
    # Legacy fallback: config.grid_states
    try:
        from data_manager import get_config

        gs = (get_config() or {}).get("grid_states") or {}
        legacy = gs.get(key) or gs.get(f"{symbol}_{timeframe}")
        if isinstance(legacy, dict) and legacy:
            return dict(legacy)
    except Exception:
        pass
    return None


def save_grid_plan(
    symbol: str,
    timeframe: str,
    plan: dict,
    *,
    tenant_id: str | None = None,
    scope: str | None = None,
    test: bool = False,
    also_legacy_config: bool = False,
) -> bool:
    """Upsert one plan into Mongo. Does not write ``config.json``.

    ``also_legacy_config`` is a deprecated opt-in to mirror into
    ``config.grid_states``. The default is off (#330 slice 3) so grid
    persist no longer burns config snapshot slots.
    """
    key = _plan_key(symbol, timeframe)
    doc = load_grid_plans_document(tenant_id=tenant_id, scope=scope, test=test)
    plans = dict(doc.get("plans") or {})
    payload = dict(plan or {})
    payload.setdefault("symbol", symbol)
    payload.setdefault("timeframe", timeframe)
    if "center" in payload and "center_price" not in payload:
        payload["center_price"] = payload["center"]
    plans[key] = payload
    ok = save_grid_plans_document(plans, tenant_id=tenant_id, scope=scope, test=test)
    if also_legacy_config:
        try:
            from data_manager import get_config, save_config

            cfg = dict(get_config() or {})
            gs = cfg.setdefault("grid_states", {})
            gs[key] = payload
            save_config(cfg)
        except Exception as e:
            log(f"grid_plan_store legacy config mirror skipped: {e}", "DEBUG")
    return ok


def list_grid_plan_keys(
    *,
    tenant_id: str | None = None,
    scope: str | None = None,
    test: bool = False,
) -> list[str]:
    doc = load_grid_plans_document(tenant_id=tenant_id, scope=scope, test=test)
    return sorted((doc.get("plans") or {}).keys())


def migrate_legacy_grid_states_once(
    *,
    tenant_id: str | None = None,
    scope: str | None = None,
    test: bool = False,
) -> dict[str, Any]:
    """Copy leftover ``config.grid_states`` into Mongo, then strip that key.

    Idempotent: a second call is a no-op when the key is already gone.
    Fail closed: if the Mongo upsert fails, ``config.grid_states`` is left intact.
    """
    from data_manager import get_config, save_config

    result: dict[str, Any] = {
        "ok": True,
        "skipped": False,
        "stripped": False,
        "migrated_keys": [],
    }
    try:
        cfg = dict(get_config() or {})
    except Exception as e:
        log(f"legacy grid_states migrate: cannot read config: {e}", "ERROR")
        result["ok"] = False
        result["error"] = str(e)
        return result

    gs = cfg.get("grid_states")
    if not isinstance(gs, dict) or not gs:
        result["skipped"] = True
        return result

    try:
        doc = load_grid_plans_document(tenant_id=tenant_id, scope=scope, test=test)
    except Exception as e:
        log(
            f"legacy grid_states migrate: mongo load failed, leaving config.grid_states: {e}",
            "ERROR",
        )
        result["ok"] = False
        result["error"] = str(e)
        return result

    plans = dict(doc.get("plans") or {})
    migrated: list[str] = []
    for key, plan in gs.items():
        if not isinstance(plan, dict) or not plan:
            continue
        if key in plans:
            continue
        payload = dict(plan)
        if "_" in str(key):
            sym, tf = str(key).rsplit("_", 1)
            payload.setdefault("symbol", sym)
            payload.setdefault("timeframe", tf)
        if "center" in payload and "center_price" not in payload:
            payload["center_price"] = payload["center"]
        plans[key] = payload
        migrated.append(str(key))

    # Confirm Mongo is writable before stripping config, even when every key
    # was already present in the store (Mongo wins; config is only a leftover).
    if not save_grid_plans_document(plans, tenant_id=tenant_id, scope=scope, test=test):
        log(
            "legacy grid_states migrate: mongo upsert failed, leaving config.grid_states",
            "ERROR",
        )
        result["ok"] = False
        result["error"] = "mongo upsert failed"
        return result

    result["migrated_keys"] = migrated
    new_cfg = dict(cfg)
    new_cfg.pop("grid_states", None)
    try:
        saved = save_config(new_cfg)
    except Exception as e:
        log(
            f"legacy grid_states migrate: Mongo ok but config strip failed: {e}",
            "ERROR",
        )
        result["ok"] = False
        result["error"] = str(e)
        return result
    if not saved:
        log(
            "legacy grid_states migrate: Mongo ok but config strip returned False",
            "ERROR",
        )
        result["ok"] = False
        result["error"] = "save_config returned False"
        return result

    result["stripped"] = True
    log(
        f"legacy grid_states migrate: copied {len(migrated)} new keys, "
        "stripped config.grid_states",
        "INFO",
    )
    return result
