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