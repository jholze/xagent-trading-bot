"""MongoDB persistence for orders, positions, and trade history ledgers."""

from __future__ import annotations

import copy
from typing import Any

from storage.mongo_client import get_database, resolve_database_name

ORDERS_COLLECTION = "orders"
POSITIONS_COLLECTION = "positions"
TRADE_HISTORY_COLLECTION = "trade_history"


def _empty_orders(scope: str) -> dict:
    return {"ledger_scope": scope, "orders": [], "migrated_from_trades": False}


def _empty_positions(scope: str) -> dict:
    return {"ledger_scope": scope, "positions": {}}


def _empty_trade_history(scope: str) -> dict:
    if scope == "live":
        return {"trades": [], "total_pnl": 0.0, "realized_pnl": 0.0}
    return {
        "virtual_balance": 5000.0,
        "realized_pnl": 0.0,
        "open_positions": 0,
        "trades": [],
    }


def _strip_id(doc: dict | None) -> dict:
    if not doc:
        return {}
    payload = copy.deepcopy(doc)
    payload.pop("_id", None)
    return payload


class MongoLedgerStore:
    """Scope-keyed ledger documents mirroring JSON ledger files."""

    def __init__(self, *, test: bool = False, config: dict | None = None):
        self._test = test
        self._config = config

    @property
    def database_name(self) -> str:
        return resolve_database_name(test=self._test, config=self._config)

    @property
    def _db(self):
        return get_database(test=self._test, config=self._config)

    def _collection(self, name: str):
        return self._db[name]

    def load_orders(self, scope: str) -> dict:
        doc = self._collection(ORDERS_COLLECTION).find_one({"_id": scope})
        if not doc:
            return _empty_orders(scope)
        data = _strip_id(doc)
        data.setdefault("orders", [])
        data["ledger_scope"] = scope
        return data

    def save_orders(self, data: dict, scope: str) -> bool:
        payload = dict(data)
        payload["_id"] = scope
        payload["ledger_scope"] = scope
        self._collection(ORDERS_COLLECTION).replace_one(
            {"_id": scope}, payload, upsert=True
        )
        return True

    def load_positions(self, scope: str) -> dict:
        doc = self._collection(POSITIONS_COLLECTION).find_one({"_id": scope})
        if not doc:
            return _empty_positions(scope)
        data = _strip_id(doc)
        data.setdefault("positions", {})
        data["ledger_scope"] = scope
        return data

    def save_positions(self, data: dict, scope: str) -> bool:
        payload = dict(data)
        payload["_id"] = scope
        payload["ledger_scope"] = scope
        self._collection(POSITIONS_COLLECTION).replace_one(
            {"_id": scope}, payload, upsert=True
        )
        return True

    def load_trade_history(self, scope: str) -> dict:
        doc = self._collection(TRADE_HISTORY_COLLECTION).find_one({"_id": scope})
        if not doc:
            return _empty_trade_history(scope)
        data = _strip_id(doc)
        data.setdefault("trades", [])
        return data

    def save_trade_history(self, data: dict, scope: str) -> bool:
        payload = dict(data)
        payload["_id"] = scope
        self._collection(TRADE_HISTORY_COLLECTION).replace_one(
            {"_id": scope}, payload, upsert=True
        )
        return True

    def count_documents(self) -> dict[str, int]:
        return {
            "orders": self._collection(ORDERS_COLLECTION).count_documents({}),
            "positions": self._collection(POSITIONS_COLLECTION).count_documents({}),
            "trade_history": self._collection(TRADE_HISTORY_COLLECTION).count_documents({}),
        }


def get_ledger_store(*, test: bool = False, config: dict | None = None) -> MongoLedgerStore:
    return MongoLedgerStore(test=test, config=config)