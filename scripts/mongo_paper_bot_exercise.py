#!/usr/bin/env python3
"""Exercise paper+mongo ledger path via TradingService and verify Mongo writes."""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import BotConfig
from core.models import RiskDecision, TradeOrder
from data_manager import get_config, load_orders, load_positions_document, load_trade_history_document
from services.trading_service import TradingService
from storage.mongo_client import drop_database, get_database
from storage.mongo_ledger import MongoLedgerStore


def main() -> int:
    os.environ["MONGODB_DB"] = "xagent_test"
    drop_database(test=True)

    cfg = copy.deepcopy(get_config())
    cfg["trading_mode"] = "paper"
    cfg.setdefault("paper", {})["backend"] = "mongo"
    cfg.setdefault("architecture", {})["ledger_backend"] = "local"
    cfg["architecture"]["ledger_dual_write"] = False

    import data_manager

    data_manager._config_cache = cfg
    data_manager.get_config = lambda: cfg
    data_manager.reload_config = lambda: cfg
    data_manager.is_demo_mode = lambda: False

    from strategies.positions import load_positions, positions

    positions.clear()
    load_positions("paper")

    empty_history = {
        "virtual_balance": 1000.0,
        "realized_pnl": 0.0,
        "open_positions": 0,
        "trades": [],
    }
    data_manager.save_trade_history_document(empty_history, "paper", config=cfg)

    bot = BotConfig(raw=cfg)
    trading = TradingService(config=bot)
    print(f"mode_label: {trading.mode_label()}")

    buy_order = TradeOrder(type="BUY", symbol="MONGO/USDT", price=1.0, amount=0, usdt_amount=40)
    sell_order = TradeOrder(type="SELL", symbol="MONGO/USDT", price=1.0, amount=20, signal="SELL")

    from unittest.mock import patch

    with patch.object(trading.risk, "evaluate") as mock_eval, \
         patch("notifications.telegram_commands.position_display.send_positions_snapshot"):
        mock_eval.side_effect = [
            RiskDecision(approved=True, order=buy_order),
            RiskDecision(approved=True, order=sell_order),
        ]
        buy_result = trading.execute_order(buy_order, "4h", source="manual")
        sell_result = trading.execute_order(sell_order, "4h", source="manual")

    print(f"buy_executed: {buy_result.executed} amount={buy_result.amount}")
    print(f"sell_executed: {sell_result.executed} amount={sell_result.amount}")

    orders = load_orders("paper")
    positions_doc = load_positions_document("paper")
    history = load_trade_history_document("paper")
    print(f"orders_filled: {len(orders.get('orders', []))}")
    print(f"trades_recorded: {len(history.get('trades', []))}")
    print(f"position_keys: {list(positions_doc.get('positions', {}).keys())}")

    store = MongoLedgerStore(test=True, config=cfg)
    counts = store.count_documents()
    print(f"mongo_counts: {json.dumps(counts)}")

    db = get_database(test=True, config=cfg)
    mongo_orders = db["orders"].find_one({"_id": "paper"})
    mongo_positions = db["positions"].find_one({"_id": "paper"})
    mongo_history = db["trade_history"].find_one({"_id": "paper"})
    print(f"mongo_orders_present: {mongo_orders is not None}")
    print(f"mongo_positions_present: {mongo_positions is not None}")
    print(f"mongo_history_present: {mongo_history is not None}")

    ok = (
        buy_result.executed
        and sell_result.executed
        and counts["orders"] >= 1
        and counts["positions"] >= 1
        and counts["trade_history"] >= 1
        and mongo_orders is not None
        and mongo_positions is not None
        and mongo_history is not None
    )
    print("paper_mongo_exercise: PASSED" if ok else "paper_mongo_exercise: FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())