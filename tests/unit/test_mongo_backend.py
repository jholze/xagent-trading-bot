"""Parametrized ledger backend equivalence: local JSON vs Mongo."""

import json
import os

import pytest

from data_manager import (
    load_orders,
    load_positions_document,
    load_trade_history_document,
    reload_config,
    save_orders,
    save_positions_document,
    save_trade_history_document,
)
from storage.mongo_client import drop_database

FIXTURES = (
    __import__("pathlib").Path(__file__).resolve().parents[1] / "fixtures" / "hermes"
)


@pytest.fixture
def mongo_test_env(monkeypatch):
    monkeypatch.setenv("MONGODB_DB", "xagent_test")
    drop_database(test=True)
    yield
    drop_database(test=True)
    monkeypatch.delenv("MONGODB_DB", raising=False)


def _mongo_config(base: dict) -> dict:
    cfg = json.loads(json.dumps(base))
    cfg.setdefault("architecture", {})
    cfg["architecture"]["ledger_backend"] = "mongo"
    cfg["architecture"]["ledger_dual_write"] = False
    cfg.setdefault("paper", {})
    cfg["paper"]["backend"] = "mongo"
    return cfg


def _local_config(base: dict) -> dict:
    cfg = json.loads(json.dumps(base))
    cfg.setdefault("architecture", {})
    cfg["architecture"]["ledger_backend"] = "local"
    cfg["architecture"]["ledger_dual_write"] = False
    cfg.setdefault("paper", {})
    cfg["paper"]["backend"] = "local"
    return cfg


def _dual_write_config(base: dict) -> dict:
    cfg = _mongo_config(base)
    cfg["architecture"]["ledger_dual_write"] = True
    return cfg


@pytest.mark.parametrize("scope", ["paper", "live"])
def test_orders_local_vs_mongo_equivalent(tmp_path, monkeypatch, mongo_test_env, scope):
    from data_manager import get_config

    base = get_config()
    orders = {
        "ledger_scope": scope,
        "orders": [{"id": "eq-1", "symbol": "BTC/USDT", "status": "filled"}],
        "migrated_from_trades": False,
    }

    local_cfg = _local_config(base)
    monkeypatch.setattr("data_manager._config_cache", None)
    monkeypatch.setattr("data_manager.get_config", lambda: local_cfg)
    assert save_orders(orders, scope) is True
    local_loaded = load_orders(scope)

    mongo_cfg = _mongo_config(base)
    monkeypatch.setattr("data_manager._config_cache", None)
    monkeypatch.setattr("data_manager.get_config", lambda: mongo_cfg)
    assert save_orders(orders, scope) is True
    mongo_loaded = load_orders(scope)

    assert local_loaded["orders"] == mongo_loaded["orders"]


def test_dual_write_reads_json_while_mongo_has_copy(
    monkeypatch, mongo_test_env,
):
    from data_manager import get_config

    base = get_config()
    scope = "paper"
    orders = {
        "ledger_scope": scope,
        "orders": [{"id": "dual-1", "symbol": "ETH/USDT", "status": "filled"}],
        "migrated_from_trades": False,
    }
    dual_cfg = _dual_write_config(base)
    monkeypatch.setattr("data_manager.get_config", lambda: dual_cfg)
    assert save_orders(orders, scope) is True

    read_back = load_orders(scope)
    assert read_back["orders"][0]["id"] == "dual-1"

    mongo_cfg = _mongo_config(base)
    monkeypatch.setattr("data_manager.get_config", lambda: mongo_cfg)
    mongo_read = load_orders(scope)
    assert mongo_read["orders"][0]["id"] == "dual-1"


def test_trade_history_paper_roundtrip_mongo(monkeypatch, mongo_test_env):
    from data_manager import get_config

    base = get_config()
    scope = "paper"
    history = {
        "virtual_balance": 1000.0,
        "realized_pnl": 0.0,
        "open_positions": 0,
        "trades": [{"type": "BUY", "symbol": "SOL/USDT", "usdt_amount": 50}],
    }
    mongo_cfg = _mongo_config(base)
    monkeypatch.setattr("data_manager.get_config", lambda: mongo_cfg)
    assert save_trade_history_document(history, scope) is True
    loaded = load_trade_history_document(scope)
    assert loaded["trades"][0]["symbol"] == "SOL/USDT"


def test_positions_document_roundtrip_mongo(monkeypatch, mongo_test_env):
    from data_manager import get_config

    base = get_config()
    scope = "paper"
    payload = {
        "ledger_scope": scope,
        "positions": {"BTC_USDT_4h": {"amount": 1.0, "average_entry": 50000}},
    }
    mongo_cfg = _mongo_config(base)
    monkeypatch.setattr("data_manager.get_config", lambda: mongo_cfg)
    assert save_positions_document(payload, scope) is True
    loaded = load_positions_document(scope)
    assert loaded["positions"]["BTC_USDT_4h"]["amount"] == 1.0