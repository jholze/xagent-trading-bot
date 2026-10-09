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

        # Multi-tenant: compound default:<scope> is canonical (read == write).
        # Legacy scope docs are merged on startup via merge_operator_ledger_scope().
        # Paper/live: never fall back to `_id: paper` (mixes leftover operator book).
        # Demo staging still uses `_id: demo` for the operator default tenant.
        if collection == TRADE_HISTORY_COLLECTION and (legacy or compound):
            from storage.ledger_merge import merge_trade_history_docs

            return merge_trade_history_docs(legacy, compound) or compound or legacy
        if scope == "demo":
            if compound and _legacy_has_payload(compound, collection):
                return compound
            if legacy and _legacy_has_payload(legacy, collection):
                return legacy
            return compound or legacy
        return compound

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
        groups = self._archived_order_groups(tid, scope)
        if groups:
            from storage.order_blob_parts import merge_order_rows

            data["orders"] = merge_order_rows(*groups, data.get("orders") or [])
        return data

    def save_orders(
        self, data: dict, scope: str, tenant_id: str | None = None
    ) -> bool:
        self._guard_dev_db()
        payload = self._prepare_payload(data, scope, tenant_id)
        hot = self._persist_order_archive(payload)
        self._replace_with_fence(ORDERS_COLLECTION, hot)
        return True

    def _archived_order_groups(self, tenant_id: str, scope: str) -> list[list]:
        from storage.order_blob_parts import ORDERS_ARCHIVE_COLLECTION

        base = compound_ledger_id(tenant_id, scope)
        docs = []
        for doc in self._collection(ORDERS_ARCHIVE_COLLECTION).find(
            {"tenant_id": tenant_id, "ledger_scope": scope, "kind": "orders_archive"}
        ):
            doc_id = str(doc.get("_id") or "")
            if not doc_id.startswith(f"{base}:part:"):
                continue
            docs.append(doc)
        docs.sort(key=lambda row: int(row.get("part_index") or 0))
        return [list(row.get("orders") or []) for row in docs]

    def _persist_order_archive(self, payload: dict) -> dict:
        """Write overflow parts first. Return the hot document for the fenced replace.

        A failed part write leaves the previous hot document unchanged.
        """
        from storage.errors import LedgerWriteFailed
        from storage.order_blob_parts import (
            BSON_HARD_MAX,
            HOT_MAX_BYTES,
            ORDERS_ARCHIVE_COLLECTION,
            PART_MAX_BYTES,
            bson_size,
            split_order_payload,
        )

        hot, parts = split_order_payload(
            payload,
            hot_max=HOT_MAX_BYTES,
            part_max=PART_MAX_BYTES,
        )
        if bson_size(hot) + 4096 >= BSON_HARD_MAX:
            raise LedgerWriteFailed(
                "orders document still exceeds MongoDB 16MB limit",
                op="save_orders",
                scope=payload.get("ledger_scope"),
                tenant_id=payload.get("tenant_id"),
            )
        base = str(payload.get("_id") or "")
        for part in parts:
            body = dict(part)
            body["_id"] = f"{base}:part:{int(part['part_index']):04d}"
            self._replace_with_fence(ORDERS_ARCHIVE_COLLECTION, body)
        self._delete_extra_order_parts(base, keep=len(parts))
        return hot

    def _delete_extra_order_parts(self, base: str, keep: int) -> None:
        import re

        from bus.writer_lease import mongo_fence_filter, write_fence
        from storage.errors import LedgerWriteFailed
        from storage.order_blob_parts import ORDERS_ARCHIVE_COLLECTION

        prefix = f"{base}:part:"
        coll = self._collection(ORDERS_ARCHIVE_COLLECTION)
        find = getattr(coll, "find", None)
        # Writer-lease unit doubles only implement replace_one. Nothing to drop.
        if find is None:
            return
        fence = write_fence()
        for doc in list(find({"_id": {"$regex": f"^{re.escape(prefix)}" }})):
            raw = str(doc.get("_id") or "")[len(prefix):]
            try:
                index = int(doc.get("part_index") if doc.get("part_index") is not None else raw)
            except (TypeError, ValueError):
                index = keep
            if index < keep:
                continue
            if fence is None:
                coll.delete_one({"_id": doc["_id"]})
                continue
            result = coll.delete_one(mongo_fence_filter(doc["_id"], fence))
            if int(getattr(result, "deleted_count", 0) or 0) == 0 and coll.find_one(
                {"_id": doc["_id"]}
            ):
                raise LedgerWriteFailed(
                    "stale fence",
                    op="save_orders_archive",
                    scope=doc.get("ledger_scope"),
                    tenant_id=doc.get("tenant_id"),
                )

    def load_positions(self, scope: str, tenant_id: str | None = None) -> dict:
        tid = self._resolve_tenant(tenant_id)
        doc = self._find_doc(POSITIONS_COLLECTION, scope, tid)
        if not doc:
            return _empty_positions(scope, tid)
        data = _strip_id(doc)
        data.setdefault("positions", {})
        data["ledger_scope"] = scope
        data.setdefault("tenant_id", tid)
        return data

    def save_positions(
        self, data: dict, scope: str, tenant_id: str | None = None
    ) -> bool:
        self._guard_dev_db()
        payload = self._prepare_payload(data, scope, tenant_id)
        self._replace_with_fence(POSITIONS_COLLECTION, payload)
        return True

    def load_trade_history(self, scope: str, tenant_id: str | None = None) -> dict:
        tid = self._resolve_tenant(tenant_id)
        doc = self._find_doc(TRADE_HISTORY_COLLECTION, scope, tid)
        if not doc:
            return _empty_trade_history(scope, tid)
        data = _strip_id(doc)
        data.setdefault("trades", [])
        data["ledger_scope"] = scope
        data.setdefault("tenant_id", tid)
        return data

    def save_trade_history(
        self, data: dict, scope: str, tenant_id: str | None = None
    ) -> bool:
        self._guard_dev_db()
        payload = self._prepare_payload(data, scope, tenant_id)
        self._replace_with_fence(TRADE_HISTORY_COLLECTION, payload)
        return True

    def _replace_with_fence(self, collection: str, payload: dict) -> None:
        from bus.writer_lease import mongo_fence_filter, write_fence
        from storage.errors import LedgerWriteFailed

        fence = write_fence()
        if fence is None:
            self._collection(collection).replace_one(
                {"_id": payload["_id"]}, payload, upsert=True
            )
            return
        payload["fence"] = int(fence)
        filt = mongo_fence_filter(payload["_id"], fence)
        try:
            result = self._collection(collection).replace_one(
                filt, payload, upsert=True
            )
        except Exception as e:
            if type(e).__name__ == "DuplicateKeyError" or "duplicate key" in str(
                e
            ).lower():
                raise LedgerWriteFailed(
                    "stale fence",
                    op=f"save_{collection}",
                    scope=payload.get("ledger_scope"),
                    tenant_id=payload.get("tenant_id"),
                    cause=e,
                ) from e
            raise
        upserted = getattr(result, "upserted_id", None)
        if int(getattr(result, "matched_count", 0) or 0) == 0 and upserted is None:
            raise LedgerWriteFailed(
                "stale fence",
                op=f"save_{collection}",
                scope=payload.get("ledger_scope"),
                tenant_id=payload.get("tenant_id"),
            )

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