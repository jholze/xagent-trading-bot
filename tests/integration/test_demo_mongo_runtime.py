"""Demo ledger runtime invariants when DEMO_LEDGER_BACKEND=mongo."""

import os

import pytest


@pytest.fixture
def demo_mongo_env(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.setenv("DEMO_LEDGER_BACKEND", "mongo")
    monkeypatch.setenv("MONGODB_DB", "xagent_pytest")
    monkeypatch.delenv("DEMO_LEDGER_JSON_FALLBACK", raising=False)


def test_load_orders_refuses_json_fallback_on_mongo_error(demo_mongo_env, monkeypatch):
    from data_manager import load_orders

    class BrokenStore:
        def load_orders(self, scope):
            raise ConnectionError("mongo down")

    monkeypatch.setattr("data_manager._mongo_ledger_store", lambda *a, **k: BrokenStore())
    with pytest.raises(ConnectionError, match="mongo down"):
        load_orders("demo")


def test_load_orders_uses_json_fallback_when_opt_in(demo_mongo_env, monkeypatch, tmp_path):
    from data_manager import load_orders

    orders_path = tmp_path / "orders.demo.json"
    orders_path.write_text(
        '{"ledger_scope":"demo","orders":[{"id":"x1","symbol":"BTC/USDT"}],"migrated_from_trades":false}',
        encoding="utf-8",
    )

    class BrokenStore:
        def load_orders(self, scope):
            raise ConnectionError("mongo down")

    monkeypatch.setenv("DEMO_LEDGER_JSON_FALLBACK", "1")
    monkeypatch.setattr("data_manager._mongo_ledger_store", lambda *a, **k: BrokenStore())
    monkeypatch.setattr(
        "data_manager.resolve_orders_file",
        lambda scope: str(orders_path),
    )

    doc = load_orders("demo")
    assert doc["orders"][0]["id"] == "x1"


def test_resolve_store_demo_returns_mongo_adapter(demo_mongo_env):
    from storage.ledger_router import MongoLedgerStoreAdapter, resolve_store

    store = resolve_store("demo", {"demo": {"backend": "mongo"}})
    assert isinstance(store, MongoLedgerStoreAdapter)


def test_demo_hybrid_backend_deprecated_to_mongo(demo_mongo_env, monkeypatch):
    from storage.ledger_router import resolve_ledger_backend

    monkeypatch.setenv("DEMO_LEDGER_BACKEND", "demo_hybrid")
    assert resolve_ledger_backend("demo", {"demo": {"backend": "mongo"}}) == "mongo"