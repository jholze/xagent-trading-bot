"""Merge legacy scope Mongo docs into default:<scope> compound ledgers."""

from __future__ import annotations

import copy
from typing import Any

from core.tenant_context import DEFAULT_TENANT
from storage.mongo_ledger import (
    ORDERS_COLLECTION,
    POSITIONS_COLLECTION,
    TRADE_HISTORY_COLLECTION,
    MongoLedgerStore,
)
from storage.tenant_keys import compound_ledger_id, is_legacy_doc

COLLECTIONS = (
    (ORDERS_COLLECTION, "orders"),
    (POSITIONS_COLLECTION, "positions"),
    (TRADE_HISTORY_COLLECTION, "trades"),
)


def _order_sort_key(order: dict) -> tuple:
    return (
        int(order.get("display_seq") or 0),
        str((order.get("timestamps") or {}).get("created") or ""),
    )


def merge_order_lists(legacy_orders: list | None, compound_orders: list | None) -> list:
    by_id: dict[str, dict] = {}
    for order in legacy_orders or []:
        oid = str(order.get("id") or "")
        if oid:
            by_id[oid] = copy.deepcopy(order)
    for order in compound_orders or []:
        oid = str(order.get("id") or "")
        if not oid:
            continue
        if oid not in by_id:
            by_id[oid] = copy.deepcopy(order)
            continue
        if _order_sort_key(order) >= _order_sort_key(by_id[oid]):
            by_id[oid] = copy.deepcopy(order)
    return sorted(by_id.values(), key=_order_sort_key)


def _position_ts(pos: dict) -> str:
    return str(pos.get("last_trade_at") or pos.get("updated_at") or "")


def merge_position_maps(legacy_pos: dict | None, compound_pos: dict | None) -> dict:
    out = copy.deepcopy(legacy_pos or {})
    for key, pos in (compound_pos or {}).items():
        if key not in out:
            out[key] = copy.deepcopy(pos)
            continue
        if _position_ts(pos) >= _position_ts(out[key]):
            out[key] = copy.deepcopy(pos)
    return out


def _trade_key(trade: dict) -> str:
    return "|".join(
        [
            str(trade.get("order_id") or ""),
            str(trade.get("timestamp") or ""),
            str(trade.get("type") or ""),
            str(trade.get("symbol") or ""),
        ]
    )


def merge_trade_lists(legacy_trades: list | None, compound_trades: list | None) -> list:
    seen: set[str] = set()
    merged: list[dict] = []
    for trade in (legacy_trades or []) + (compound_trades or []):
        key = _trade_key(trade)
        if key in seen:
            continue
        seen.add(key)
        merged.append(copy.deepcopy(trade))
    merged.sort(key=lambda t: str(t.get("timestamp") or ""))
    return merged


def merge_trade_history_docs(legacy: dict | None, compound: dict | None) -> dict:
    base = copy.deepcopy(compound or legacy or {})
    legacy = legacy or {}
    compound = compound or {}
    base["trades"] = merge_trade_lists(legacy.get("trades"), compound.get("trades"))
    for field in (
        "virtual_balance",
        "realized_pnl",
        "total_pnl",
        "open_positions",
        "peak_equity",
    ):
        if field in compound and compound.get(field) is not None:
            base[field] = compound[field]
        elif field in legacy and legacy.get(field) is not None:
            base[field] = legacy[field]
    return base


def merge_operator_ledger_scope(
    *,
    scope: str,
    dry_run: bool = True,
    test: bool = False,
    delete_legacy: bool = True,
) -> dict[str, Any]:
    """Union legacy `_id=<scope>` into `default:<scope>`; compound becomes canonical."""
    store = MongoLedgerStore(test=test)
    stats: dict[str, Any] = {
        "scope": scope,
        "dry_run": dry_run,
        "delete_legacy": delete_legacy,
        "collections": {},
    }

    for coll_name, payload_key in COLLECTIONS:
        coll = store._collection(coll_name)
        legacy = coll.find_one({"_id": scope})
        compound_id = compound_ledger_id(DEFAULT_TENANT, scope)
        compound = coll.find_one({"_id": compound_id}) or {}

        if not legacy or not is_legacy_doc(legacy):
            stats["collections"][coll_name] = {"skipped": True, "reason": "no_legacy"}
            continue

        legacy_n = len(legacy.get(payload_key) or ([] if payload_key != "positions" else {}))
        compound_n = len(compound.get(payload_key) or ([] if payload_key != "positions" else {}))

        if payload_key == "orders":
            merged_payload = merge_order_lists(
                legacy.get("orders"), compound.get("orders")
            )
            merged_n = len(merged_payload)
        elif payload_key == "positions":
            merged_payload = merge_position_maps(
                legacy.get("positions"), compound.get("positions")
            )
            merged_n = len(merged_payload)
        else:
            merged_payload = merge_trade_history_docs(legacy, compound)
            merged_n = len(merged_payload.get("trades") or [])

        stats["collections"][coll_name] = {
            "legacy_entries": legacy_n,
            "compound_entries_before": compound_n,
            "merged_entries": merged_n,
        }
        if dry_run:
            continue

        out = copy.deepcopy(compound if compound else legacy)
        if payload_key == "orders":
            out["orders"] = merged_payload
        elif payload_key == "positions":
            out["positions"] = merged_payload
        else:
            out.update(merged_payload)
        out["_id"] = compound_id
        out["tenant_id"] = DEFAULT_TENANT
        out["ledger_scope"] = scope
        coll.replace_one({"_id": compound_id}, out, upsert=True)
        if delete_legacy:
            coll.delete_one({"_id": scope})

    return stats