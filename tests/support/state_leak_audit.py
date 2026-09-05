"""pytest plugin: attribute leftover Redis / module-global / Mongo state to tests.

Load with ``PYTHONPATH=tests/support`` and ``-p state_leak_audit``. Does not
import production modules until a test already has; snapshots only what is
already in ``sys.modules`` so collection order is unchanged.

Channels
--------
* Redis keys under the worker prefix ``pytest:<suffix>[_gwN]:`` (SCAN).
* A configurable list of module-level globals (dicts → sorted keys; lists →
  length; scalars as-is). Missing modules/attrs are skipped.
* Mongo collection document counts of the worker's test DB.

A test *leaves* a delta when its post-protocol snapshot has keys/globals/
counts that were not present just before that test (setup+call+teardown).
A later test on the same worker *consumes* a leftover when its pre-call
snapshot (after autouse fixtures, before the body) still contains it.

JSON dump: ``${STATE_LEAK_AUDIT_OUT}.{worker}.json`` (worker is ``main`` when
not under xdist). Set ``STATE_LEAK_AUDIT_VERBOSE=1`` to print every delta.
"""
from __future__ import annotations

import atexit
import json
import os
import sys
from typing import Any

# (module, attribute) — only snapshotted when the module is already imported.
_GLOBALS: tuple[tuple[str, str], ...] = (
    ("services.entry_sensor_loop", "_last_poll_at"),
    ("services.entry_sensor_loop", "_last_watch_seed_at"),
    ("services.entry_sensor_loop", "_loop_thread"),
    ("strategies.watch_15m_state", "_cache"),
    ("strategies.entry_sensor_15m", "_pending_results"),
    ("strategies.entry_sensor_15m", "_pending_metrics"),
    ("services.market_policy_fusion", "_DEGRADED_EPISODE"),
    ("risk.slot_eviction_runtime", "_EVICT_TS"),
    ("risk.slot_eviction_runtime", "_SYMBOL_COOLDOWN"),
    ("risk.risk_manager", "_EQUITY_MTM_UNAVAILABLE_LOGGED"),
    ("risk.rebuy_cooldown", "_last_sell_at"),
    ("notifications.daily_portfolio", "_nav_start_cache"),
    ("services.market_oracle.store", "_LATEST"),
    ("services.market_oracle.store", "_HISTORY"),
    ("services.santiment.store", "_LATEST"),
    ("services.santiment.store", "_HISTORY"),
    ("services.venue_quality", "_CACHE"),
    ("services.venue_quality", "_cache"),
    ("data.cmc_market_cap", "_CACHE"),
    ("data.cmc_market_cap", "_cache"),
    ("services.correlated_tier.api", "_CACHE"),
    ("services.correlated_tier.api", "_cache"),
    ("services.gate_balance", "_balance_cache"),
    ("storage.order_ledger_v2", "_STORE"),
    ("storage.order_ledger_v2", "_V2_DEGRADED"),
    ("storage.ledger_router", "_store_cache"),
    ("services.order_service", "_ORDERS_READ_CACHE"),
    ("bus.ohlcv_cache", "_PROCESS_CACHE"),
    ("bus.price_cache", "_PROCESS_CACHE"),
    ("bus.eval_queue", "_QUEUE"),
    ("core.interactive_priority", "_STATE"),
    ("services.architecture_runtime", "_RECOVERY_STATE"),
    ("execution.recovery", "_RECOVERY_LOG"),
    ("strategies.oracle_climax", "_STALE_EPISODE"),
    ("strategies.oracle_climax", "_cycle"),
    ("services.eval_queue_runtime", "_RUNTIME"),
    ("webhooks.store", "_STORE"),
    ("price_fetcher", "_GATE_TICKER_SNAPSHOT"),
    ("price_fetcher", "_price_cache"),
    ("intelligence.memory.cache", "_CACHE"),
    ("services.market_service", "_EXCHANGE_CACHE"),
)

_ENV_WATCH = (
    "ORDER_LEDGER_V2",
    "ORDER_LEDGER_V2_READS",
    "ORDER_LEDGER_V2_BACKEND",
    "ORDER_LEDGER_V2_BACKFILL_COMPLETE",
    "DEMO_LEDGER_BACKEND",
    "DEMO_MODE",
    "MULTI_TENANT_ENABLED",
)

_cur: dict[str, Any] = {"nodeid": "<no test>"}
_order: list[str] = []
_left: list[dict[str, Any]] = []  # tests that added leftover
_consumed: list[dict[str, Any]] = []  # tests that saw leftover at call
_before_protocol: dict[str, dict[str, Any]] = {}
_after_protocol: dict[str, dict[str, Any]] = {}
_before_call: dict[str, dict[str, Any]] = {}
_last_after: dict[str, Any] | None = None


def _worker() -> str:
    return os.environ.get("PYTEST_XDIST_WORKER") or "main"


def _redis_prefix() -> str:
    raw = os.environ.get("PYTEST_DB_SUFFIX") or ""
    sanitized = "".join(
        ch for ch in raw.strip() if (ch.isalnum() and ord(ch) < 128) or ch == "_"
    )
    worker = (os.environ.get("PYTEST_XDIST_WORKER") or "").strip()
    if worker and not sanitized.endswith(f"_{worker}"):
        sanitized = f"{sanitized or 'default'}_{worker}"
    return f"pytest:{sanitized or 'default'}:"


def _stable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return f"bytes:{len(value)}"
    if isinstance(value, dict):
        keys = sorted(str(k) for k in value.keys())
        if len(keys) > 40:
            return {"len": len(keys), "keys_head": keys[:40]}
        return keys
    if isinstance(value, (list, tuple, set, frozenset)):
        return {"type": type(value).__name__, "len": len(value)}
    # threading.Thread
    is_alive = getattr(value, "is_alive", None)
    if callable(is_alive):
        try:
            return {"thread": getattr(value, "name", "?"), "alive": bool(is_alive())}
        except Exception:
            return {"thread": True}
    # MemoryOrderLedgerV2 / similar stores
    orders = getattr(value, "_orders", None)
    if isinstance(orders, dict):
        return {"store_orders": len(orders)}
    ram = getattr(value, "_ram", None)
    if isinstance(ram, dict):
        return {"ram": len(ram)}
    return type(value).__name__


def _snap_redis() -> list[str]:
    if "bus.redis_client" not in sys.modules:
        return []
    try:
        from bus.redis_client import get_redis

        client = get_redis()
        if not client:
            return []
        prefix = _redis_prefix()
        keys = list(client.scan_iter(match=f"{prefix}*", count=200))
        return sorted(str(k) for k in keys)
    except Exception:
        return []


def _snap_globals() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for mod_name, attr in _GLOBALS:
        mod = sys.modules.get(mod_name)
        if mod is None or not hasattr(mod, attr):
            continue
        try:
            out[f"{mod_name}.{attr}"] = _stable(getattr(mod, attr))
        except Exception as exc:
            out[f"{mod_name}.{attr}"] = f"<err:{type(exc).__name__}>"
    return out


def _snap_mongo() -> dict[str, int]:
    if "storage.mongo_client" not in sys.modules:
        return {}
    try:
        from storage.mongo_client import get_client, resolve_test_db_name

        db_name = (
            os.environ.get("MONGODB_TEST_DB")
            or os.environ.get("MONGODB_DB")
            or resolve_test_db_name()
        )
        client = get_client()
        db = client[db_name]
        counts: dict[str, int] = {}
        for name in db.list_collection_names():
            try:
                n = int(db[name].estimated_document_count())
            except Exception:
                n = int(db[name].count_documents({}))
            counts[name] = n
        return counts
    except Exception:
        return {}


def _snap_env() -> dict[str, str]:
    return {k: os.environ.get(k, "") for k in _ENV_WATCH}


def snapshot() -> dict[str, Any]:
    return {
        "redis": _snap_redis(),
        "globals": _snap_globals(),
        "mongo": _snap_mongo(),
        "env": _snap_env(),
    }


def _flatten(snap: dict[str, Any]) -> dict[str, Any]:
    """Channel → comparable mapping of leftover items."""
    flat: dict[str, Any] = {}
    for key in snap.get("redis") or []:
        flat[f"redis:{key}"] = True
    for name, val in (snap.get("globals") or {}).items():
        if _global_empty(val):
            continue
        flat[f"global:{name}"] = val
    for coll, n in (snap.get("mongo") or {}).items():
        if int(n or 0) <= 0:
            continue
        flat[f"mongo:{coll}"] = int(n)
    for name, val in (snap.get("env") or {}).items():
        # Baseline under pytest: DEMO_MODE=1, MULTI_TENANT_ENABLED unset/0.
        if name == "DEMO_MODE" and val in ("", "1"):
            continue
        if name == "MULTI_TENANT_ENABLED" and val in ("", "0"):
            continue
        if val in ("", "0"):
            continue
        flat[f"env:{name}"] = val
    return flat


def _global_empty(val: Any) -> bool:
    if val is None or val is False or val == 0 or val == 0.0 or val == "":
        return True
    if val == [] or val == {} or val == ():
        return True
    if isinstance(val, list) and not val:
        return True
    if isinstance(val, dict):
        if val.get("len") == 0:
            return True
        if val.get("store_orders") == 0:
            return True
        if val.get("ram") == 0:
            return True
        if val.get("alive") is False:
            return True
        if list(val.keys()) == ["type", "len"] and val.get("len") == 0:
            return True
    return False


def _added(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    b, a = _flatten(before), _flatten(after)
    out: dict[str, Any] = {}
    for k, v in a.items():
        if k not in b:
            out[k] = v
        elif b[k] != v:
            out[k] = {"from": b[k], "to": v}
    return out


def _present(snap: dict[str, Any]) -> dict[str, Any]:
    return _flatten(snap)


def _verbose() -> bool:
    return (os.environ.get("STATE_LEAK_AUDIT_VERBOSE") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _log(msg: str) -> None:
    print(msg, flush=True)


def pytest_runtest_setup(item):
    _cur["nodeid"] = item.nodeid


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    nodeid = item.nodeid
    _order.append(nodeid)
    before = snapshot()
    _before_protocol[nodeid] = before
    yield
    after = snapshot()
    _after_protocol[nodeid] = after
    global _last_after
    _last_after = after
    added = _added(before, after)
    if added:
        rec = {"test": nodeid, "worker": _worker(), "left": added}
        _left.append(rec)
        if _verbose():
            _log(f"LEAK-LEFT {_worker()} {nodeid} {json.dumps(added, default=str)}")
    _cur["nodeid"] = "<between tests>"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    nodeid = item.nodeid
    before = snapshot()
    _before_call[nodeid] = before
    leftover = _present(before)
    if leftover and _left:
        # Attribute to the most recent leaker that produced overlapping keys.
        sources = []
        keys = set(leftover)
        for rec in reversed(_left):
            overlap = keys & set(rec["left"])
            if overlap:
                sources.append({"leaker": rec["test"], "keys": sorted(overlap)})
                keys -= overlap
            if not keys:
                break
        rec = {
            "test": nodeid,
            "worker": _worker(),
            "saw": leftover,
            "from": sources,
        }
        _consumed.append(rec)
        if _verbose():
            _log(f"LEAK-SAW {_worker()} {nodeid} {json.dumps(rec['from'], default=str)}")
    yield


def pytest_runtest_teardown(item):
    _cur["nodeid"] = "<between tests>"


def _dump() -> None:
    out = os.environ.get("STATE_LEAK_AUDIT_OUT")
    payload = {
        "worker": _worker(),
        "prefix": _redis_prefix(),
        "order": _order,
        "left": _left,
        "consumed": _consumed,
    }
    if out:
        path = f"{out}.{_worker()}.json"
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=1, default=str)
        except OSError as exc:
            _log(f"LEAK-DUMP-FAIL {path}: {exc}")
            return
        _log(f"LEAK-DUMP {path} left={len(_left)} consumed={len(_consumed)} tests={len(_order)}")
    else:
        _log(
            f"LEAK-SUMMARY worker={_worker()} left={len(_left)} "
            f"consumed={len(_consumed)} tests={len(_order)}"
        )


atexit.register(_dump)
