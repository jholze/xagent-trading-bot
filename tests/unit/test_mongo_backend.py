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


def _isolated_ledger_paths(tmp_path):
    root = str(tmp_path)
    return {
        "orders": {
            "demo": os.path.join(root, "orders.demo.json"),
            "paper": os.path.join(root, "orders.paper.json"),
            "live": os.path.join(root, "orders.live.json"),
        },
        "positions": {
            "demo": os.path.join(root, "positions.demo.json"),
            "paper": os.path.join(root, "positions.paper.json"),
            "live": os.path.join(root, "positions.live.json"),
        },
        "history": {
            "paper": os.path.join(root, "trade_history.json"),
            "live": os.path.join(root, "live_trade_history.json"),
        },
    }


def _apply_backend_config(monkeypatch, cfg: dict, paths: dict):
    import data_manager

    monkeypatch.setattr(data_manager, "_config_cache", cfg)
    monkeypatch.setattr(data_manager, "get_config", lambda: cfg)
    monkeypatch.setattr(data_manager, "ORDERS_SCOPE_FILES", paths["orders"])
    monkeypatch.setattr(data_manager, "POSITIONS_SCOPE_FILES", paths["positions"])
    monkeypatch.setattr(data_manager, "TRADE_HISTORY_SCOPE_FILES", paths["history"])
    monkeypatch.setattr(data_manager, "is_demo_mode", lambda: False)
    monkeypatch.setattr(data_manager, "resolve_ledger_scope", lambda trading_mode=None: "paper")


def _normalize_positions(positions: dict) -> dict:
    return {
        key: {
            k: (float(v) if k == "amount" else v)
            for k, v in value.items()
            if k not in ("last_trade_at", "last_dca_at")
        }
        for key, value in positions.items()
    }


def _ledger_snapshot(scope: str = "paper") -> dict:
    orders = load_orders(scope)
    positions = load_positions_document(scope)
    history = load_trade_history_document(scope)
    filled = [
        {
            "side": o.get("side"),
            "symbol": o.get("symbol"),
            "status": o.get("status"),
            "usdt": (o.get("execution") or {}).get("usdt"),
            "amount": (o.get("execution") or {}).get("amount"),
            "pnl": o.get("pnl"),
        }
        for o in orders.get("orders", [])
    ]
    return {
        "orders": filled,
        "positions": _normalize_positions(positions.get("positions", {})),
        "virtual_balance": round(float(history.get("virtual_balance", 0)), 2),
        "realized_pnl": round(float(history.get("realized_pnl", 0)), 2),
        "trade_count": len(history.get("trades", [])),
    }


def _run_buy_sell_cycle(monkeypatch, cfg: dict, paths: dict):
    from unittest.mock import patch

    from core.config import BotConfig
    from core.models import RiskDecision, TradeOrder
    from services.trading_service import TradingService
    from strategies.positions import positions

    _apply_backend_config(monkeypatch, cfg, paths)
    from strategies.positions import load_positions

    positions.clear()
    load_positions("paper")

    empty_history = {
        "virtual_balance": 1000.0,
        "realized_pnl": 0.0,
        "open_positions": 0,
        "trades": [],
    }
    save_trade_history_document(empty_history, "paper", config=cfg)

    bot = BotConfig(raw=cfg)
    trading = TradingService(config=bot)
    buy_order = TradeOrder(type="BUY", symbol="EQ/USDT", price=2.0, amount=0, usdt_amount=50)
    sell_order = TradeOrder(type="SELL", symbol="EQ/USDT", price=2.0, amount=25, signal="SELL")

    with patch.object(trading.risk, "evaluate") as mock_eval, \
         patch("notifications.telegram_commands.position_display.send_positions_snapshot"):
        mock_eval.side_effect = [
            RiskDecision(approved=True, order=buy_order),
            RiskDecision(approved=True, order=sell_order),
        ]
        buy_result = trading.execute_order(buy_order, "4h", source="manual")
        sell_result = trading.execute_order(sell_order, "4h", source="manual")

    snapshot = _ledger_snapshot("paper")
    snapshot["buy_executed"] = buy_result.executed
    snapshot["sell_executed"] = sell_result.executed
    snapshot["buy_amount"] = round(float(buy_result.amount or 0), 4)
    snapshot["sell_amount"] = round(float(sell_result.amount or 0), 4)
    return snapshot


@pytest.mark.parametrize("backend", ["local", "mongo"])
def test_buy_sell_cycle_equivalent_results(tmp_path, monkeypatch, mongo_test_env, backend):
    from data_manager import get_config

    base = get_config()
    paths = _isolated_ledger_paths(tmp_path / backend)
    if backend == "local":
        cfg = _local_config(base)
    else:
        cfg = _mongo_config(base)
        monkeypatch.setenv("MONGODB_DB", "xagent_test")

    snapshot = _run_buy_sell_cycle(monkeypatch, cfg, paths)

    assert snapshot["buy_executed"] is True
    assert snapshot["sell_executed"] is True
    assert snapshot["buy_amount"] == 25.0
    assert snapshot["sell_amount"] == 25.0
    assert snapshot["trade_count"] == 2
    assert len(snapshot["orders"]) == 2
    assert snapshot["orders"][0]["status"] == "filled"
    assert snapshot["orders"][1]["status"] == "filled"
    assert "EQ_USDT_4h" in snapshot["positions"]
    assert float(snapshot["positions"]["EQ_USDT_4h"]["amount"]) == 0.0


def test_migration_json_matches_mongo_content(tmp_path, monkeypatch, mongo_test_env):
    import json
    from pathlib import Path

    from data_manager import get_config
    from scripts.mongo_migrate_json import migrate_scope

    base = get_config()
    paths = _isolated_ledger_paths(tmp_path / "migrate")
    scope = "paper"
    orders = {
        "ledger_scope": scope,
        "orders": [{"id": "mig-1", "symbol": "BTC/USDT", "status": "filled"}],
        "migrated_from_trades": False,
    }
    positions = {
        "ledger_scope": scope,
        "positions": {"BTC_USDT_4h": {"amount": 1.5, "average_entry": 42000}},
    }
    history = {
        "virtual_balance": 900.0,
        "realized_pnl": 1.0,
        "open_positions": 1,
        "trades": [{"type": "BUY", "symbol": "BTC/USDT", "usdt_amount": 100}],
    }
    for path in (paths["orders"][scope], paths["positions"][scope], paths["history"][scope]):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(paths["orders"][scope]).write_text(json.dumps(orders), encoding="utf-8")
    Path(paths["positions"][scope]).write_text(json.dumps(positions), encoding="utf-8")
    Path(paths["history"][scope]).write_text(json.dumps(history), encoding="utf-8")

    monkeypatch.setattr("scripts.mongo_migrate_json.resolve_orders_file", lambda s: paths["orders"][s])
    monkeypatch.setattr("scripts.mongo_migrate_json.resolve_positions_file", lambda s: paths["positions"][s])
    monkeypatch.setattr(
        "scripts.mongo_migrate_json.SCOPE_HISTORY_FILES",
        {scope: paths["history"][scope]},
    )

    summary = migrate_scope(scope, dry_run=False, test_db=True)
    assert summary["mongo_counts"]["orders"] == 1
    assert summary["mongo_counts"]["positions"] == 1
    assert summary["mongo_counts"]["trade_history"] == 1

    mongo_cfg = _mongo_config(base)
    monkeypatch.setattr("data_manager.get_config", lambda: mongo_cfg)
    assert load_orders(scope)["orders"] == orders["orders"]
    assert load_positions_document(scope)["positions"] == positions["positions"]
    assert load_trade_history_document(scope)["trades"] == history["trades"]


def test_local_vs_mongo_buy_sell_snapshots_match(tmp_path, monkeypatch, mongo_test_env):
    from data_manager import get_config

    base = get_config()
    monkeypatch.setenv("MONGODB_DB", "xagent_test")

    local_paths = _isolated_ledger_paths(tmp_path / "local_cmp")
    mongo_paths = _isolated_ledger_paths(tmp_path / "mongo_cmp")

    local_snap = _run_buy_sell_cycle(monkeypatch, _local_config(base), local_paths)
    mongo_snap = _run_buy_sell_cycle(monkeypatch, _mongo_config(base), mongo_paths)

    assert local_snap["buy_executed"] == mongo_snap["buy_executed"]
    assert local_snap["sell_executed"] == mongo_snap["sell_executed"]
    assert local_snap["buy_amount"] == mongo_snap["buy_amount"]
    assert local_snap["sell_amount"] == mongo_snap["sell_amount"]
    assert local_snap["orders"] == mongo_snap["orders"]
    assert local_snap["virtual_balance"] == mongo_snap["virtual_balance"]
    assert local_snap["realized_pnl"] == mongo_snap["realized_pnl"]
    assert local_snap["positions"] == mongo_snap["positions"]