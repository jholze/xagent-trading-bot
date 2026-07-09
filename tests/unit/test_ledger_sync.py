import os
import sys
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from data_manager import load_orders, resolve_ledger_scope, resolve_positions_file
from services.ledger_sync import (
    _build_positions_snapshot_from_orders,
    backfill_orders_from_trade_history,
    count_open_positions_from_orders,
    on_trading_mode_change,
    rebuild_positions_from_orders,
    reconcile_recent_highs,
    sync_positions_on_startup,
)
from services.order_service import OrderService
from strategies.positions import (
    _positions_lock,
    bootstrap_positions,
    count_open_positions,
    get_active_scope,
    get_key,
    get_position,
    positions,
)


class TestLedgerSync(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.orders_files = {
            "demo": os.path.join(self.tmp.name, "orders.demo.json"),
            "paper": os.path.join(self.tmp.name, "orders.paper.json"),
            "live": os.path.join(self.tmp.name, "orders.live.json"),
        }
        self.positions_files = {
            "demo": os.path.join(self.tmp.name, "positions.demo.json"),
            "paper": os.path.join(self.tmp.name, "positions.paper.json"),
            "live": os.path.join(self.tmp.name, "positions.live.json"),
        }
        self._positions_backup = {
            k: {**v, "amount": Decimal(str(v["amount"]))} for k, v in positions.items()
        }
        positions.clear()

        self.orders_patch = patch("data_manager.ORDERS_SCOPE_FILES", self.orders_files)
        self.positions_patch = patch("data_manager.POSITIONS_SCOPE_FILES", self.positions_files)
        self.router_orders_patch = patch(
            "storage.ledger_router.ORDERS_SCOPE_FILES", self.orders_files
        )
        self.router_positions_patch = patch(
            "storage.ledger_router.POSITIONS_SCOPE_FILES", self.positions_files
        )
        from storage.ledger_router import JsonLedgerStore

        self.resolve_store_patch = patch(
            "storage.ledger_router.resolve_store",
            side_effect=lambda scope, cfg=None: JsonLedgerStore(cfg),
        )
        self.orders_patch.start()
        self.positions_patch.start()
        self.router_orders_patch.start()
        self.router_positions_patch.start()
        self.resolve_store_patch.start()
        from storage import ledger_router
        from services import order_service

        ledger_router._store_cache.clear()
        order_service._ORDERS_READ_CACHE.clear()

    def tearDown(self):
        self.resolve_store_patch.stop()
        self.router_positions_patch.stop()
        self.router_orders_patch.stop()
        self.positions_patch.stop()
        self.orders_patch.stop()
        from storage import ledger_router
        from services import order_service

        ledger_router._store_cache.clear()
        order_service._ORDERS_READ_CACHE.clear()
        positions.clear()
        positions.update(self._positions_backup)

    def _filled_buy(self, scope, symbol, price, amount):
        from core.models import TradeOrder

        svc = OrderService(scope)
        order = svc.create_from_request(
            TradeOrder("BUY", symbol, price, amount, usdt_amount=price * amount),
            telegram_token=f"{scope}_buy",
        )
        svc.update_status(
            order["id"],
            "filled",
            execution={"price": price, "amount": amount, "usdt": price * amount},
        )

    def _filled_sell(self, scope, symbol, price, amount):
        from core.models import TradeOrder

        svc = OrderService(scope)
        order = svc.create_from_request(
            TradeOrder("SELL", symbol, price, amount, signal="SELL_FULL"),
            telegram_token=f"{scope}_sell",
        )
        svc.update_status(
            order["id"],
            "filled",
            execution={"price": price, "amount": amount, "usdt": price * amount},
            pnl=-1.0,
        )

    def test_live_scope_does_not_inherit_paper_positions(self):
        self._filled_buy("paper", "ARIA/USDT", 0.05, 1000)
        self._filled_buy("live", "SOL/USDT", 60.0, 2.0)

        with patch("data_manager.is_demo_mode", return_value=False), \
             patch("data_manager.get_config", return_value={"trading_mode": "paper"}):
            rebuild_positions_from_orders("paper")
            self.assertEqual(get_active_scope(), "paper")
            self.assertGreater(float(get_position("ARIA/USDT", "4h")["amount"]), 0)

        with patch("data_manager.is_demo_mode", return_value=False), \
             patch("data_manager.get_config", return_value={"trading_mode": "live"}):
            rebuild_positions_from_orders("live")
            self.assertEqual(get_active_scope(), "live")
            self.assertEqual(float(get_position("ARIA/USDT", "4h")["amount"]), 0)
            self.assertGreater(float(get_position("SOL/USDT", "4h")["amount"]), 0)

    def test_mode_switch_rebuilds_target_ledger(self):
        self._filled_buy("paper", "HIGH/USDT", 0.08, 500)
        self._filled_sell("live", "HIGH/USDT", 0.05, 500)

        with patch("data_manager.is_demo_mode", return_value=False), \
             patch("data_manager.get_config", return_value={"trading_mode": "live"}):
            msg = on_trading_mode_change("paper", "live")

        self.assertEqual(get_active_scope(), "live")
        self.assertEqual(float(get_position("HIGH/USDT", "4h")["amount"]), 0)
        self.assertIn("LIVE", msg)

    def test_resolve_positions_file_scopes(self):
        with patch("data_manager.is_demo_mode", return_value=False):
            self.assertEqual(resolve_positions_file("paper"), self.positions_files["paper"])
            self.assertEqual(resolve_positions_file("live"), self.positions_files["live"])

    def test_demo_mode_uses_demo_scope(self):
        with patch("data_manager.is_demo_mode", return_value=True):
            self.assertEqual(resolve_ledger_scope(), "demo")

    def _filled_dca_buy(self, scope, symbol, price, amount):
        from core.models import TradeOrder

        svc = OrderService(scope)
        order = svc.create_from_request(
            TradeOrder(
                "BUY",
                symbol,
                price,
                amount,
                usdt_amount=price * amount,
                source="dca",
                signal="BUY_DCA",
            ),
            telegram_token=f"{scope}_dca_{symbol}_{price}",
        )
        svc.update_status(
            order["id"],
            "filled",
            execution={"price": price, "amount": amount, "usdt": price * amount},
        )

    def test_partial_sell_peak_amount_from_orders(self):
        self._filled_buy("paper", "XPL/USDT", 1.0, 100.0)
        self._filled_sell("paper", "XPL/USDT", 1.2, 30.0)

        rebuild_positions_from_orders("paper")
        pos = get_position("XPL/USDT", "4h")
        self.assertAlmostEqual(float(pos["amount"]), 70.0, places=2)
        self.assertAlmostEqual(float(pos["peak_amount"]), 100.0, places=2)
        self.assertAlmostEqual(pos["sold_percent"], 0.3, places=2)

    def test_rebuild_preserves_dca_rounds_from_orders(self):
        self._filled_buy("paper", "LAB/USDT", 12.0, 100.0)
        self._filled_dca_buy("paper", "LAB/USDT", 10.0, 40.0)

        import json

        with open(self.positions_files["paper"], "w", encoding="utf-8") as f:
            json.dump(
                {
                    "ledger_scope": "paper",
                    "positions": {
                        "LAB_USDT_4h": {
                            "amount": 140.0,
                            "peak_amount": 140.0,
                            "sold_percent": 0.0,
                            "average_entry": 11.43,
                            "dca_rounds": 0,
                            "last_dca_at": None,
                        }
                    },
                },
                f,
            )

        rebuild_positions_from_orders("paper")
        pos = get_position("LAB/USDT", "4h")
        self.assertEqual(pos["dca_rounds"], 1)
        self.assertIsNotNone(pos["last_dca_at"])

    def test_order_snapshot_infers_dca_without_stale_cache(self):
        self._filled_buy("paper", "SKYAI/USDT", 0.05, 1000.0)
        self._filled_dca_buy("paper", "SKYAI/USDT", 0.04, 500.0)
        self._filled_dca_buy("paper", "SKYAI/USDT", 0.03, 500.0)

        snap = _build_positions_snapshot_from_orders("paper")
        pos = snap["SKYAI_USDT_4h"]
        self.assertEqual(pos["dca_rounds"], 2)
        self.assertIsNotNone(pos["last_dca_at"])

    def test_sync_positions_on_startup_rebuilds_on_drift(self):
        self._filled_buy("live", "ARIA/USDT", 0.05, 1000)
        with open(self.positions_files["live"], "w", encoding="utf-8") as f:
            f.write('{"positions": {}, "ledger_scope": "live"}')

        with patch("data_manager.is_demo_mode", return_value=False), \
             patch("data_manager.get_config", return_value={"trading_mode": "live"}), \
             patch("services.ledger_sync.migrate_legacy_positions"):
            bootstrap_positions()
            sync_positions_on_startup()

        self.assertEqual(count_open_positions_from_orders("live"), count_open_positions())
        self.assertGreater(float(get_position("ARIA/USDT", "4h")["amount"]), 0)

    def test_sync_preserves_legacy_positions_when_ledger_exceeds_orders(self):
        import json

        with open(self.positions_files["paper"], "w", encoding="utf-8") as f:
            json.dump(
                {
                    "ledger_scope": "paper",
                    "positions": {
                        "CAT_USDT_4h": {
                            "amount": 1_000_000.0,
                            "peak_amount": 1_000_000.0,
                            "sold_percent": 0.0,
                            "average_entry": 0.01,
                            "realized_pnl": 0.0,
                            "last_buy_price": 1.5e-06,
                            "last_ampel": "🟡",
                            "last_rsi": 45.0,
                        }
                    },
                },
                f,
            )
        with open(self.orders_files["paper"], "w", encoding="utf-8") as f:
            json.dump({"ledger_scope": "paper", "orders": [], "migrated_from_trades": False}, f)

        with patch("data_manager.is_demo_mode", return_value=False), \
             patch("data_manager.get_config", return_value={"trading_mode": "paper"}), \
             patch("services.ledger_sync.migrate_legacy_positions"):
            bootstrap_positions()
            sync_positions_on_startup()

        self.assertGreater(count_open_positions(), count_open_positions_from_orders("paper"))
        self.assertGreater(float(get_position("CAT/USDT", "4h")["amount"]), 0)

    def test_backfill_orders_from_trade_history_uses_trade_order_id(self):
        import json

        trade_path = os.path.join(self.tmp.name, "trade_history.json")
        with open(trade_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "trades": [
                        {
                            "type": "BUY",
                            "symbol": "CAT/USDT",
                            "price": 1.5e-06,
                            "amount": 100.0,
                            "usdt_amount": 50.0,
                            "order_id": "legacy-cat-01",
                            "timestamp": "2026-06-24T10:00:00",
                            "mode": "live",
                        }
                    ]
                },
                f,
            )
        with patch("data_manager.TRADE_HISTORY_FILE", "trade_history.json"), \
             patch("data_manager.get_data_file", side_effect=lambda name: trade_path if name == "trade_history.json" else os.path.join(self.tmp.name, name)):
            added = backfill_orders_from_trade_history("paper")
        self.assertEqual(added, 1)
        orders = load_orders("paper").get("orders", [])
        self.assertEqual(orders[-1]["id"], "legacy-cat-01")

    def test_reconcile_recent_highs_updates_stale_peak(self):
        self._filled_buy("paper", "MAGMA/USDT", 0.34, 1000.0)
        rebuild_positions_from_orders("paper")
        key = get_key("MAGMA/USDT", "4h")
        with _positions_lock:
            positions[key]["recent_high"] = 0.35

        with patch("strategies.positions.save_positions_document", return_value=True) as mock_save:
            changed = reconcile_recent_highs(
                "paper",
                price_map={"MAGMA/USDT": 0.39},
            )

        self.assertTrue(changed)
        mock_save.assert_called()
        self.assertEqual(float(get_position("MAGMA/USDT", "4h")["recent_high"]), 0.39)


if __name__ == "__main__":
    unittest.main()