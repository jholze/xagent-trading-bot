"""Split one orders document so MongoDB can store the whole book.

MongoDB rejects a single document over 16MB. The order book is one document,
and open lots are replayed from every executed order, so rows are not deleted.
Overflow goes to ``orders_archive`` parts. The hot document keeps the newest
tail. Load merges the parts back; a later copy of the same order id wins.
"""

from __future__ import annotations

from typing import Any

from bson import BSON

from storage.errors import LedgerWriteFailed

# MongoDB hard cap. Not configurable on Railway.
BSON_HARD_MAX = 16 * 1024 * 1024
# Headroom for the next order plus the writer-lease fence field.
HOT_MAX_BYTES = 12 * 1024 * 1024
PART_MAX_BYTES = 12 * 1024 * 1024
ORDERS_ARCHIVE_COLLECTION = "orders_archive"


def bson_size(doc: dict) -> int:
    return len(BSON.encode(doc))


def order_identity(order: dict) -> tuple:
    oid = order.get("id")
    if oid:
        return ("id", str(oid))
    timestamps = order.get("timestamps")
    created = timestamps.get("created") if isinstance(timestamps, dict) else None
    filled = timestamps.get("filled") if isinstance(timestamps, dict) else None
    return (
        "fallback",
        order.get("display_seq"),
        order.get("symbol"),
        created,
        filled,
    )


def merge_order_rows(*groups: list) -> list:
    """Oldest-first groups. A later group replaces the same identity in place."""
    index: dict[tuple, int] = {}
    rows: list[dict] = []
    for group in groups:
        for order in group or []:
            if not isinstance(order, dict):
                continue
            key = order_identity(order)
            if key in index:
                rows[index[key]] = order
            else:
                index[key] = len(rows)
                rows.append(order)
    return rows


def _with_orders(template: dict, orders: list) -> dict:
    trial = dict(template)
    trial["orders"] = orders
    return trial


def _smallest_start_that_fits(template: dict, orders: list, limit: int) -> int:
    """Smallest start index whose suffix encodes within ``limit``.

    An empty suffix always fits. ``len(orders)`` means even one row does not.
    """
    if not orders or bson_size(_with_orders(template, orders)) <= limit:
        return 0
    lo, hi = 0, len(orders)
    while lo < hi:
        mid = (lo + hi) // 2
        if bson_size(_with_orders(template, orders[mid:])) <= limit:
            hi = mid
        else:
            lo = mid + 1
    return lo


def _chunk_from_the_left(template: dict, orders: list, limit: int) -> list[list]:
    chunks: list[list] = []
    rest = list(orders)
    while rest:
        one = _with_orders(template, rest[:1])
        if bson_size(one) > limit:
            if bson_size(one) >= BSON_HARD_MAX:
                raise LedgerWriteFailed(
                    "single order exceeds MongoDB document limit",
                    op="save_orders",
                    scope=template.get("ledger_scope"),
                    tenant_id=template.get("tenant_id"),
                )
            chunks.append(rest[:1])
            rest = rest[1:]
            continue
        lo, hi = 1, len(rest)
        best = 1
        while lo <= hi:
            mid = (lo + hi) // 2
            if bson_size(_with_orders(template, rest[:mid])) <= limit:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        chunks.append(rest[:best])
        rest = rest[best:]
    return chunks


def split_order_payload(
    payload: dict,
    *,
    hot_max: int = HOT_MAX_BYTES,
    part_max: int = PART_MAX_BYTES,
) -> tuple[dict, list[dict]]:
    """Return ``(hot_payload, archive_parts)``.

    ``hot_payload`` does not share its ``orders`` list with ``payload``.
    Archive parts have no ``_id`` yet. A book that already fits returns no parts.
    """
    orders = [row for row in (payload.get("orders") or []) if isinstance(row, dict)]
    hot_template = dict(payload)
    if bson_size(_with_orders(hot_template, orders)) <= hot_max:
        return _with_orders(hot_template, list(orders)), []

    start = _smallest_start_that_fits(hot_template, orders, hot_max)
    spilled = orders[:start]
    hot_orders = orders[start:]
    if not hot_orders and spilled:
        hot_orders = spilled[-1:]
        spilled = spilled[:-1]
        if bson_size(_with_orders(hot_template, hot_orders)) >= BSON_HARD_MAX:
            raise LedgerWriteFailed(
                "single order exceeds MongoDB document limit",
                op="save_orders",
                scope=payload.get("ledger_scope"),
                tenant_id=payload.get("tenant_id"),
            )

    part_template = {
        "tenant_id": payload.get("tenant_id"),
        "ledger_scope": payload.get("ledger_scope"),
        "kind": "orders_archive",
        "part_index": 0,
    }
    raw_chunks = (
        _chunk_from_the_left(part_template, spilled, part_max) if spilled else []
    )
    parts: list[dict[str, Any]] = []
    for index, chunk in enumerate(raw_chunks):
        parts.append(
            {
                "tenant_id": payload.get("tenant_id"),
                "ledger_scope": payload.get("ledger_scope"),
                "kind": "orders_archive",
                "part_index": index,
                "orders": chunk,
            }
        )
    return _with_orders(hot_template, list(hot_orders)), parts
