"""MongoDB persistence for orders, positions, and trade history ledgers."""

from __future__ import annotations

import copy
from typing import Any

from core.tenant_context import DEFAULT_TENANT, multi_tenant_enabled, resolve_tenant_id
from storage.mongo_client import assert_safe_dev_db_mutation, get_database, resolve_database_name
from storage.tenant_keys import compound_ledger_id, is_legacy_doc

ORDERS_COLLECTION = "orders"
POSITIONS_COLLECTION = "positions"
TRADE_HISTORY_COLLECTION = "trade_history"


def _empty_orders(scope: str, tenant_id: str = DEFAULT_TENANT) -> dict:
    return {
        "tenant_id": tenant_id,
        "ledger_scope": scope,
        "orders": [],
        "migrated_from_trades": False,
    }


def _empty_positions(scope: str, tenant_id: str = DEFAULT_TENANT) -> dict:
    return {"tenant_id": tenant_id, "ledger_scope": scope, "positions": {}}


def _empty_trade_history(scope: str, tenant_id: str = DEFAULT_TENANT) -> dict:
    base = {"tenant_id": tenant_id, "ledger_scope": scope, "trades": []}
    if scope == "live":
        base.update({"total_pnl": 0.0, "realized_pnl": 0.0})
        return base
    base.update({
        "virtual_balance": 5000.0,
        "realized_pnl": 0.0,
        "open_positions": 0,
    })
    return base


def _strip_id(doc: dict | None) -> dict:
    if not doc:
        return {}
    payload = copy.deepcopy(doc)
    payload.pop("_id", None)
    return payload


def _legacy_payload_keys(collection: str) -> tuple[str, ...]:
    if collection == ORDERS_COLLECTION:
        return ("orders",)
    if collection == POSITIONS_COLLECTION:
        return ("positions",)
    if collection == TRADE_HISTORY_COLLECTION:
        return ("trades",)
    return ()


def _legacy_has_payload(doc: dict | None, collection: str) -> bool:
    if not doc:
        return False
    for key in _legacy_payload_keys(collection):
        value = doc.get(key)
        if value:
            return True
    return False


def _entry_ids(doc: dict | None, collection: str) -> set[str]:
    if not doc:
        return set()
    if collection == ORDERS_COLLECTION:
        return {str(o.get("id")) for o in (doc.get("orders") or []) if o.get("id")}
    if collection == TRADE_HISTORY_COLLECTION:
        return {str(t.get("id")) for t in (doc.get("trades") or []) if t.get("id")}
    return set()


class MongoLedgerStore:
    """Tenant + scope keyed ledger documents mirroring JSON ledger files."""

    def __init__(self, *, test: bool = False, config: dict | None = None):
        self._test = test
        self._config = config

    @property
    def database_name(self) -> str:
        return resolve_database_name(test=self._test, config=self._config)

    @property
    def _db(self):
        return get_database(test=self._test, config=self._config)

    def _guard_dev_db(self) -> None:
        assert_safe_dev_db_mutation(self.database_name, action="write")

    def _collection(self, name: str):
        return self._db[name]

    def _resolve_tenant(self, tenant_id: str | None) -> str:
        return resolve_tenant_id(tenant_id)

    def _legacy_doc(self, collection: str, scope: str) -> dict | None:
        legacy = self._collection(collection).find_one({"_id": scope})
        if legacy and is_legacy_doc(legacy):
            return legacy
        return None

    def _default_compound_doc(self, collection: str, scope: str) -> dict | None:
        return self._collection(collection).find_one(
            {"_id": compound_ledger_id(DEFAULT_TENANT, scope)}
        )

    def _find_doc(self, collection: str, scope: str, tenant_id: str | None = None) -> dict | None:
        tid = self._resolve_tenant(tenant_id)
        coll = self._collection(collection)
        compound_id = compound_ledger_id(tid, scope)
        compound = coll.find_one({"_id": compound_id})

        if tid != DEFAULT_TENANT:
            return compound

        legacy = self._legacy_doc(collection, scope)
        if not multi_tenant_enabled():
            if compound:
                return compound
            return legacy

        # Shared-bot MT: operator history often remains in legacy scope docs while
        # misrouted tenant trades were written to default:<scope>.
        if legacy and _legacy_has_payload(legacy, collection):
            return legacy
        return compound

    def _leaked_default_entries(self, collection: str, scope: str) -> list[dict]:
        """Orders/trades saved under default:<scope> but not in operator legacy book."""
        legacy = self._legacy_doc(collection, scope)
        if not legacy or not _legacy_has_payload(legacy, collection):
            return []
        compound = self._default_compound_doc(collection, scope)
        if not compound:
            return []
        legacy_ids = _entry_ids(legacy, collection)
        if collection == ORDERS_COLLECTION:
            payload_key = "orders"
        elif collection == TRADE_HISTORY_COLLECTION:
            payload_key = "trades"
        else:
            return []
        leaked = []
        for entry in compound.get(payload_key) or []:
            entry_id = entry.get("id")
            if entry_id and str(entry_id) not in legacy_ids:
                leaked.append(copy.deepcopy(entry))
        return leaked

    def _leaked_default_positions(self, scope: str) -> dict:
        legacy = self._legacy_doc(POSITIONS_COLLECTION, scope)
        if not legacy or not _legacy_has_payload(legacy, POSITIONS_COLLECTION):
            return {}
        compound = self._default_compound_doc(POSITIONS_COLLECTION, scope)
        if not compound:
            return {}
        legacy_keys = set((legacy or {}).get("positions") or {})
        leaked = {}
        for key, pos in (compound.get("positions") or {}).items():
            if key not in legacy_keys:
                leaked[key] = copy.deepcopy(pos)
        return leaked

    def _prepare_payload(
        self, data: dict, scope: str, tenant_id: str | None = None
    ) -> dict:
        tid = self._resolve_tenant(tenant_id)
        payload = dict(data)
        payload["_id"] = compound_ledger_id(tid, scope)
        payload["tenant_id"] = tid
        payload["ledger_scope"] = scope
        return payload

    def load_orders(self, scope: str, tenant_id: str | None = None) -> dict:
        tid = self._resolve_tenant(tenant_id)
        doc = self._find_doc(ORDERS_COLLECTION, scope, tid)
        if not doc:
            data = _empty_orders(scope, tid)
        else:
            data = _strip_id(doc)
            data.setdefault("orders", [])
            data["ledger_scope"] = scope
            data.setdefault("tenant_id", tid)

        if tid != DEFAULT_TENANT and multi_tenant_enabled():
            existing_ids = {str(o.get("id")) for o in data.get("orders", []) if o.get("id")}
            for order in self._leaked_default_entries(ORDERS_COLLECTION, scope):
                oid = str(order.get("id"))
                if oid and oid not in existing_ids:
                    data.setdefault("orders", []).append(order)
                    existing_ids.add(oid)
        return data

    def save_orders(
        self, data: dict, scope: str, tenant_id: str | None = None
    ) -> bool:
        self._guard_dev_db()
        payload = self._prepare_payload(data, scope, tenant_id)
        self._collection(ORDERS_COLLECTION).replace_one(
            {"_id": payload["_id"]}, payload, upsert=True
        )
        return True

    def load_positions(self, scope: str, tenant_id: str | None = None) -> dict:
        tid = self._resolve_tenant(tenant_id)
        doc = self._find_doc(POSITIONS_COLLECTION, scope, tid)
        if not doc:
            data = _empty_positions(scope, tid)
        else:
            data = _strip_id(doc)
            data.setdefault("positions", {})
            data["ledger_scope"] = scope
            data.setdefault("tenant_id", tid)

        if tid != DEFAULT_TENANT and multi_tenant_enabled():
            positions = data.setdefault("positions", {})
            for key, pos in self._leaked_default_positions(scope).items():
                positions.setdefault(key, pos)
        return data

    def save_positions(
        self, data: dict, scope: str, tenant_id: str | None = None
    ) -> bool:
        self._guard_dev_db()
        payload = self._prepare_payload(data, scope, tenant_id)
        self._collection(POSITIONS_COLLECTION).replace_one(
            {"_id": payload["_id"]}, payload, upsert=True
        )
        return True

    def load_trade_history(self, scope: str, tenant_id: str | None = None) -> dict:
        tid = self._resolve_tenant(tenant_id)
        doc = self._find_doc(TRADE_HISTORY_COLLECTION, scope, tid)
        if not doc:
            data = _empty_trade_history(scope, tid)
        else:
            data = _strip_id(doc)
            data.setdefault("trades", [])
            data["ledger_scope"] = scope
            data.setdefault("tenant_id", tid)

        if tid != DEFAULT_TENANT and multi_tenant_enabled():
            existing_ids = {str(t.get("id")) for t in data.get("trades", []) if t.get("id")}
            for trade in self._leaked_default_entries(TRADE_HISTORY_COLLECTION, scope):
                tid_str = str(trade.get("id"))
                if tid_str and tid_str not in existing_ids:
                    data.setdefault("trades", []).append(trade)
                    existing_ids.add(tid_str)
        return data

    def save_trade_history(
        self, data: dict, scope: str, tenant_id: str | None = None
    ) -> bool:
        self._guard_dev_db()
        payload = self._prepare_payload(data, scope, tenant_id)
        self._collection(TRADE_HISTORY_COLLECTION).replace_one(
            {"_id": payload["_id"]}, payload, upsert=True
        )
        return True

    def count_documents(self, tenant_id: str | None = None) -> dict[str, int]:
        tid = self._resolve_tenant(tenant_id)
        filt: dict[str, Any] = {"tenant_id": tid}
        return {
            "orders": self._collection(ORDERS_COLLECTION).count_documents(filt),
            "positions": self._collection(POSITIONS_COLLECTION).count_documents(filt),
            "trade_history": self._collection(TRADE_HISTORY_COLLECTION).count_documents(
                filt
            ),
        }


def get_ledger_store(*, test: bool = False, config: dict | None = None) -> MongoLedgerStore:
    return MongoLedgerStore(test=test, config=config)