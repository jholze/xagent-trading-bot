"""Mongo connectivity smoke tests against xagent_test."""

import pytest

from storage.mongo_client import TEST_DB_NAME, drop_database, ping_database
from storage.mongo_ledger import MongoLedgerStore


@pytest.fixture
def mongo_test_db():
    drop_database(test=True)
    yield TEST_DB_NAME
    drop_database(test=True)


def test_ping_database(mongo_test_db):
    assert ping_database(test=True) is True


def test_write_read_delete_cycle(mongo_test_db):
    store = MongoLedgerStore(test=True)
    scope = "paper"
    payload = {
        "ledger_scope": scope,
        "orders": [{"id": "t-1", "symbol": "ETH/USDT", "status": "filled"}],
        "migrated_from_trades": False,
    }
    assert store.save_orders(payload, scope) is True
    loaded = store.load_orders(scope)
    assert loaded["orders"][0]["id"] == "t-1"

    drop_database(test=True)
    empty = store.load_orders(scope)
    assert empty["orders"] == []