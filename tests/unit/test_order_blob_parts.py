"""Orders blob stays under Mongo's 16MB document cap (#669)."""

import os
import sys

import pytest
from bson import BSON

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from storage.mongo_client import drop_database
from storage.mongo_ledger import MongoLedgerStore
from storage.order_blob_parts import (
    HOT_MAX_BYTES,
    bson_size,
    merge_order_rows,
    split_order_payload,
)


def _orders(count: int, pad: int) -> list[dict]:
    return [
        {
            "id": f"o{i}",
            "display_seq": i,
            "symbol": "W/USDT",
            "status": "executed",
            "pad": "x" * pad,
        }
        for i in range(count)
    ]


def _payload(orders: list[dict]) -> dict:
    return {
        "_id": "default:demo",
        "tenant_id": "default",
        "ledger_scope": "demo",
        "orders": orders,
        "migrated_from_trades": False,
    }


def test_split_keeps_every_order_and_bounds_each_part():
    orders = _orders(40, pad=80)
    hot, parts = split_order_payload(_payload(orders), hot_max=1500, part_max=1500)
    assert parts
    assert bson_size(hot) <= 1500
    for part in parts:
        assert bson_size(part) <= 1500
        assert part["kind"] == "orders_archive"
    merged = merge_order_rows(*(part["orders"] for part in parts), hot["orders"])
    assert [row["id"] for row in merged] == [row["id"] for row in orders]
    assert hot["orders"][-1]["id"] == "o39"
    assert hot["orders"][0]["id"] != "o0"


def test_split_under_budget_writes_no_archive():
    orders = _orders(2, pad=1)
    hot, parts = split_order_payload(_payload(orders), hot_max=HOT_MAX_BYTES)
    assert parts == []
    assert [row["id"] for row in hot["orders"]] == ["o0", "o1"]


def test_later_copy_of_same_order_wins():
    merged = merge_order_rows(
        [{"id": "a", "status": "executed"}],
        [{"id": "a", "status": "pending_confirmation"}, {"id": "b", "status": "executed"}],
    )
    assert [row["id"] for row in merged] == ["a", "b"]
    assert merged[0]["status"] == "pending_confirmation"


def test_split_does_not_mutate_the_caller_list():
    orders = _orders(30, pad=40)
    before = [row["id"] for row in orders]
    split_order_payload(_payload(orders), hot_max=1200, part_max=1200)
    assert [row["id"] for row in orders] == before


@pytest.fixture
def mongo_store():
    drop_database(test=True)
    store = MongoLedgerStore(test=True)
    yield store
    drop_database(test=True)


def test_mongo_spill_roundtrip_keeps_tail_and_old_status(mongo_store, monkeypatch):
    import storage.order_blob_parts as parts

    monkeypatch.setattr(parts, "HOT_MAX_BYTES", 2200)
    monkeypatch.setattr(parts, "PART_MAX_BYTES", 2200)
    orders = _orders(25, pad=120)
    mongo_store.save_orders(
        {"orders": orders, "migrated_from_trades": False},
        "demo",
        tenant_id="default",
    )
    loaded = mongo_store.load_orders("demo", tenant_id="default")
    assert [row["id"] for row in loaded["orders"]] == [f"o{i}" for i in range(25)]

    hot = mongo_store._collection("orders").find_one({"_id": "default:demo"})
    assert len(hot["orders"]) < 25
    assert len(BSON.encode(hot)) <= 2200
    archives = list(mongo_store._collection("orders_archive").find())
    assert archives
    assert all(len(BSON.encode(doc)) <= 2200 + 64 for doc in archives)

    loaded["orders"][0]["status"] = "cancelled"
    loaded["orders"][-1]["status"] = "pending_confirmation"
    mongo_store.save_orders(loaded, "demo", tenant_id="default")
    again = mongo_store.load_orders("demo", tenant_id="default")
    ids = [row["id"] for row in again["orders"]]
    assert ids == [f"o{i}" for i in range(25)]
    assert again["orders"][0]["status"] == "cancelled"
    assert again["orders"][-1]["status"] == "pending_confirmation"
