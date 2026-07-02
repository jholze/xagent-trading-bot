"""Mongo ledger save/load/schema tests per scope."""

import json

import pytest

from storage.mongo_client import drop_database
from storage.mongo_ledger import MongoLedgerStore

FIXTURES = (
    __import__("pathlib").Path(__file__).resolve().parents[1] / "fixtures" / "hermes"
)


@pytest.fixture
def mongo_store():
    drop_database(test=True)
    store = MongoLedgerStore(test=True)
    yield store
    drop_database(test=True)


def _load_fixture(name: str) -> dict:
    with open(FIXTURES / name, encoding="utf-8") as f:
        return json.load(f)


@pytest.mark.parametrize(
    "scope,orders_fixture,positions_fixture,history_fixture",
    [
        (
            "live",
            "orders.live.sample.json",
            "positions.live.sample.json",
            "live_trade_history.sample.json",
        ),
    ],
)
def test_orders_roundtrip_matches_fixture(
    mongo_store, scope, orders_fixture, positions_fixture, history_fixture
):
    expected = _load_fixture(orders_fixture)
    mongo_store.save_orders(expected, scope)
    loaded = mongo_store.load_orders(scope)
    assert loaded["ledger_scope"] == expected["ledger_scope"]
    assert len(loaded["orders"]) == len(expected["orders"])
    assert loaded["orders"][0]["symbol"] == expected["orders"][0]["symbol"]


def test_positions_roundtrip_matches_fixture(mongo_store):
    expected = _load_fixture("positions.live.sample.json")
    mongo_store.save_positions(expected, "live")
    loaded = mongo_store.load_positions("live")
    assert loaded["ledger_scope"] == "live"
    assert set(loaded["positions"].keys()) == set(expected["positions"].keys())
    key = next(iter(expected["positions"]))
    assert loaded["positions"][key]["amount"] == expected["positions"][key]["amount"]


def test_trade_history_roundtrip_matches_fixture(mongo_store):
    expected = _load_fixture("live_trade_history.sample.json")
    mongo_store.save_trade_history(expected, "live")
    loaded = mongo_store.load_trade_history("live")
    assert len(loaded["trades"]) == len(expected["trades"])
    assert loaded["trades"][0]["symbol"] == expected["trades"][0]["symbol"]


def test_paper_scope_empty_defaults(mongo_store):
    orders = mongo_store.load_orders("paper")
    positions = mongo_store.load_positions("paper")
    history = mongo_store.load_trade_history("paper")
    assert orders["orders"] == []
    assert positions["positions"] == {}
    assert history["trades"] == []


def test_tenant_compound_key_roundtrip(mongo_store):
    from core.tenant_context import DEFAULT_TENANT
    from storage.tenant_keys import compound_ledger_id

    payload = {"orders": [{"symbol": "TEN/USDT"}], "migrated_from_trades": False}
    mongo_store.save_orders(payload, "paper", tenant_id="tenant_x")
    doc = mongo_store._collection("orders").find_one(
        {"_id": compound_ledger_id("tenant_x", "paper")}
    )
    assert doc is not None
    assert doc["tenant_id"] == "tenant_x"
    loaded = mongo_store.load_orders("paper", tenant_id="tenant_x")
    assert loaded["orders"][0]["symbol"] == "TEN/USDT"
    default_loaded = mongo_store.load_orders("paper", tenant_id=DEFAULT_TENANT)
    assert default_loaded["orders"] == []


def test_trade_history_includes_tenant_fields(mongo_store):
    from core.tenant_context import DEFAULT_TENANT

    mongo_store.save_trade_history({"trades": [{"symbol": "H/USDT"}]}, "paper")
    loaded = mongo_store.load_trade_history("paper", tenant_id=DEFAULT_TENANT)
    assert loaded["tenant_id"] == DEFAULT_TENANT
    assert loaded["ledger_scope"] == "paper"
    assert loaded["trades"][0]["symbol"] == "H/USDT"


def test_legacy_scope_fallback_for_default(mongo_store):
    from core.tenant_context import DEFAULT_TENANT

    coll = mongo_store._collection("orders")
    coll.replace_one(
        {"_id": "live"},
        {
            "_id": "live",
            "ledger_scope": "live",
            "orders": [{"symbol": "OLD/LIVE"}],
            "migrated_from_trades": True,
        },
        upsert=True,
    )
    loaded = mongo_store.load_orders("live", tenant_id=DEFAULT_TENANT)
    assert loaded["orders"][0]["symbol"] == "OLD/LIVE"