"""Ledger storage routing: local JSON, Mongo, and dual-write backends."""

from __future__ import annotations

import json
import os
import time
from typing import Protocol

from logger import log

ORDERS_SCOPE_FILES = {
    "demo": "orders.demo.json",
    "paper": "orders.paper.json",
    "live": "orders.live.json",
}

POSITIONS_SCOPE_FILES = {
    "demo": "positions.demo.json",
    "paper": "positions.paper.json",
    "live": "positions.live.json",
}

TRADE_HISTORY_SCOPE_FILES = {
    "paper": "trade_history.json",
    "live": "live_trade_history.json",
}


class LedgerStore(Protocol):
    def load_orders(self, scope: str) -> dict: ...
    def save_orders(self, data: dict, scope: str) -> bool: ...
    def load_positions(self, scope: str) -> dict: ...
    def save_positions(self, data: dict, scope: str) -> bool: ...
    def load_trade_history(self, scope: str) -> dict: ...
    def save_trade_history(self, data: dict, scope: str) -> bool: ...


def resolve_ledger_backend(scope: str, config: dict) -> str:
    if scope == "demo":
        env_backend = os.environ.get("DEMO_LEDGER_BACKEND", "").strip()
        if env_backend:
            return env_backend
        demo_backend = (config.get("demo") or {}).get("backend")
        if demo_backend:
            return str(demo_backend)
        return "demo_hybrid"
    if scope == "paper":
        paper_backend = (config.get("paper") or {}).get("backend")
        if paper_backend:
            return str(paper_backend)
    arch = config.get("architecture", {}) or {}
    return str(arch.get("ledger_backend", "local"))


def ledger_dual_write_enabled(config: dict) -> bool:
    return bool((config.get("architecture", {}) or {}).get("ledger_dual_write", False))


def _mongo_test_mode(config: dict | None = None) -> bool:
    if os.environ.get("MONGODB_DB", "") == "xagent_test":
        return True
    from data_manager import is_demo_mode

    if is_demo_mode():
        return True
    return False


def _mongo_store(config: dict):
    from storage.mongo_ledger import get_ledger_store

    return get_ledger_store(test=_mongo_test_mode(config), config=config)


class JsonLedgerStore:
    def __init__(self, config: dict | None = None):
        self._config = config

    def _get_data_file(self, base: str) -> str:
        from data_manager import get_data_file

        return get_data_file(base)

    def _atomic_write(self, path: str, data: dict) -> bool:
        from data_manager import atomic_write_json

        try:
            atomic_write_json(path, data)
            return True
        except Exception:
            return False

    def _scope_files(self):
        from data_manager import ORDERS_SCOPE_FILES, POSITIONS_SCOPE_FILES

        return ORDERS_SCOPE_FILES, POSITIONS_SCOPE_FILES

    def _orders_path(self, scope: str) -> str:
        orders_files, _ = self._scope_files()
        return orders_files[scope]

    def _positions_path(self, scope: str) -> str:
        _, positions_files = self._scope_files()
        if scope == "demo":
            return self._get_data_file("positions.json")
        return positions_files[scope]

    def _trade_history_path(self, scope: str) -> str:
        from data_manager import TRADE_HISTORY_FILE, LIVE_TRADE_HISTORY_FILE

        if scope == "demo":
            return self._get_data_file(LIVE_TRADE_HISTORY_FILE)
        scope_files = {"paper": TRADE_HISTORY_FILE, "live": LIVE_TRADE_HISTORY_FILE}
        return self._get_data_file(scope_files.get(scope, TRADE_HISTORY_FILE))

    def load_orders(self, scope: str) -> dict:
        path = self._orders_path(scope)
        if not os.path.exists(path):
            return {"ledger_scope": scope, "orders": [], "migrated_from_trades": False}
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            data.setdefault("orders", [])
            data["ledger_scope"] = scope
            return data
        except Exception as e:
            log(f"Failed to load {path}: {e}", "WARNING")
            return {"ledger_scope": scope, "orders": [], "migrated_from_trades": False}

    def save_orders(self, data: dict, scope: str) -> bool:
        payload = dict(data)
        payload["ledger_scope"] = scope
        return self._atomic_write(self._orders_path(scope), payload)

    def load_positions(self, scope: str) -> dict:
        path = self._positions_path(scope)
        if not os.path.exists(path):
            return {"ledger_scope": scope, "positions": {}}
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            data.setdefault("positions", {})
            data["ledger_scope"] = scope
            return data
        except Exception as e:
            log(f"Failed to load {path}: {e}", "WARNING")
            return {"ledger_scope": scope, "positions": {}}

    def save_positions(self, data: dict, scope: str) -> bool:
        payload = dict(data)
        payload["ledger_scope"] = scope
        return self._atomic_write(self._positions_path(scope), payload)

    def _empty_trade_history(self, scope: str) -> dict:
        if scope == "live":
            return {"trades": [], "total_pnl": 0.0, "realized_pnl": 0.0}
        return {
            "virtual_balance": 5000.0,
            "realized_pnl": 0.0,
            "open_positions": 0,
            "trades": [],
        }

    def load_trade_history(self, scope: str) -> dict:
        path = self._trade_history_path(scope)
        if not os.path.exists(path):
            return self._empty_trade_history(scope)
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log(f"Failed to load {path}: {e}", "WARNING")
            return self._empty_trade_history(scope)

    def save_trade_history(self, data: dict, scope: str) -> bool:
        return self._atomic_write(self._trade_history_path(scope), data)


class MongoLedgerStoreAdapter:
    def __init__(self, config: dict | None = None):
        self._store = _mongo_store(config or {})

    def load_orders(self, scope: str) -> dict:
        return self._store.load_orders(scope)

    def save_orders(self, data: dict, scope: str) -> bool:
        return self._store.save_orders(data, scope)

    def load_positions(self, scope: str) -> dict:
        return self._store.load_positions(scope)

    def save_positions(self, data: dict, scope: str) -> bool:
        return self._store.save_positions(data, scope)

    def load_trade_history(self, scope: str) -> dict:
        return self._store.load_trade_history(scope)

    def save_trade_history(self, data: dict, scope: str) -> bool:
        return self._store.save_trade_history(data, scope)


class DemoLedgerStore:
    """Demo scope: orders/trade_history SOT in JSON; positions cache in Mongo (+ JSON fallback)."""

    def __init__(self, config: dict | None = None):
        cfg = config or {}
        self._json = JsonLedgerStore(cfg)
        self._mongo = MongoLedgerStoreAdapter(cfg)

    def load_orders(self, scope: str) -> dict:
        return self._json.load_orders(scope)

    def save_orders(self, data: dict, scope: str) -> bool:
        ok = self._json.save_orders(data, scope)
        try:
            ok = self._mongo.save_orders(data, scope) and ok
        except Exception as e:
            log(f"Mongo orders save failed ({scope}): {e}", "WARNING")
        return ok

    def load_positions(self, scope: str) -> dict:
        try:
            doc = self._mongo.load_positions(scope)
            if doc.get("positions"):
                return doc
        except Exception as e:
            log(f"Mongo positions load failed ({scope}), falling back to JSON: {e}", "WARNING")
        return self._json.load_positions(scope)

    def save_positions(self, data: dict, scope: str) -> bool:
        # Demo Mongo positions doc is read-only cache; never overwrite stable portfolio.
        return self._json.save_positions(data, scope)

    def load_trade_history(self, scope: str) -> dict:
        return self._json.load_trade_history(scope)

    def save_trade_history(self, data: dict, scope: str) -> bool:
        ok = self._json.save_trade_history(data, scope)
        try:
            ok = self._mongo.save_trade_history(data, scope) and ok
        except Exception as e:
            log(f"Mongo trade_history save failed ({scope}): {e}", "WARNING")
        return ok


class DualWriteLedgerStore:
    """Write JSON + Mongo; read from Mongo (authoritative)."""

    def __init__(self, config: dict | None = None):
        cfg = config or {}
        self._json = JsonLedgerStore(cfg)
        self._mongo = MongoLedgerStoreAdapter(cfg)

    def load_orders(self, scope: str) -> dict:
        try:
            return self._mongo.load_orders(scope)
        except Exception as e:
            log(f"Mongo orders load failed ({scope}), falling back to JSON: {e}", "WARNING")
            return self._json.load_orders(scope)

    def save_orders(self, data: dict, scope: str) -> bool:
        ok = self._json.save_orders(data, scope)
        try:
            ok = self._mongo.save_orders(data, scope) and ok
        except Exception as e:
            log(f"Mongo orders save failed ({scope}): {e}", "ERROR")
            ok = False
        return ok

    def load_positions(self, scope: str) -> dict:
        try:
            return self._mongo.load_positions(scope)
        except Exception as e:
            log(f"Mongo positions load failed ({scope}), falling back to JSON: {e}", "WARNING")
            return self._json.load_positions(scope)

    def save_positions(self, data: dict, scope: str) -> bool:
        ok = self._json.save_positions(data, scope)
        try:
            ok = self._mongo.save_positions(data, scope) and ok
        except Exception as e:
            log(f"Mongo positions save failed ({scope}): {e}", "ERROR")
            ok = False
        return ok

    def load_trade_history(self, scope: str) -> dict:
        try:
            return self._mongo.load_trade_history(scope)
        except Exception as e:
            log(f"Mongo trade_history load failed ({scope}), falling back to JSON: {e}", "WARNING")
            return self._json.load_trade_history(scope)

    def save_trade_history(self, data: dict, scope: str) -> bool:
        ok = self._json.save_trade_history(data, scope)
        try:
            ok = self._mongo.save_trade_history(data, scope) and ok
        except Exception as e:
            log(f"Mongo trade_history save failed ({scope}): {e}", "ERROR")
            ok = False
        return ok


_store_cache: dict[str, tuple[float, LedgerStore]] = {}
_STORE_CACHE_TTL = 30.0


def resolve_store(scope: str, config: dict | None = None) -> LedgerStore:
    from data_manager import get_config

    cfg = config or get_config()
    backend = resolve_ledger_backend(scope, cfg)
    dual = ledger_dual_write_enabled(cfg)
    key = f"{scope}:{backend}:dual={dual}"
    now = time.time()
    cached = _store_cache.get(key)
    if cached and now - cached[0] < _STORE_CACHE_TTL:
        return cached[1]
    if scope == "demo" or backend == "demo_hybrid":
        store: LedgerStore = DemoLedgerStore(cfg)
    elif dual:
        store = DualWriteLedgerStore(cfg)
    elif backend == "mongo":
        store = MongoLedgerStoreAdapter(cfg)
    else:
        store = JsonLedgerStore(cfg)
    _store_cache[key] = (now, store)
    return store


def reads_mongo(scope: str, config: dict | None = None) -> bool:
    from data_manager import get_config

    cfg = config or get_config()
    if scope == "demo":
        return True
    if ledger_dual_write_enabled(cfg):
        return True
    return resolve_ledger_backend(scope, cfg) == "mongo"