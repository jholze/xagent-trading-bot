"""Epic #119: /orders and /portfolio read-path performance guards."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.models import TradeOrder
from notifications.telegram_commands import order_commands
from services.order_service import OrderService, calendar_day_bounds


class TestOrderServiceReadPath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.scope_patch = patch("data_manager.ORDERS_SCOPE_FILES", {
            "demo": os.path.join(self.tmp.name, "orders.demo.json"),
            "paper": os.path.join(self.tmp.name, "orders.paper.json"),
            "live": os.path.join(self.tmp.name, "orders.live.json"),
        })
        self.scope_patch.start()
        self.scope = patch("services.order_service.resolve_tenant_scope", return_value="paper")
        self.scope.start()
        self.svc = OrderService("paper")

    def tearDown(self):
        self.scope.stop()
        self.scope_patch.stop()

    def test_list_day_does_not_expire_or_reconcile(self):
        with patch.object(self.svc, "expire_stale_pending") as exp, \
             patch.object(self.svc, "reconcile_legacy_sources") as rec:
            self.svc.list_day_filled_all()
            exp.assert_not_called()
            rec.assert_not_called()

    def test_stats_from_filled_orders_pure(self):
        orders = [
            {"status": "filled", "side": "buy", "execution": {"usdt": 100}},
            {"status": "filled", "side": "sell", "execution": {"usdt": 50}, "pnl": 12.5},
            {"status": "filled", "side": "sell", "execution": {"usdt": 40}, "pnl": -5.0},
        ]
        stats = OrderService.stats_from_filled_orders(orders)
        self.assertEqual(stats["buys"], 1)
        self.assertEqual(stats["sells"], 2)
        self.assertAlmostEqual(stats["realized_pnl"], 7.5)
        self.assertEqual(stats["sell_wins"], 1)
        self.assertEqual(stats["sell_losses"], 1)
        self.assertEqual(stats["wins"], 1)
        self.assertEqual(stats["losses"], 1)
        self.assertEqual(stats["unknown_side"], 0)

    def test_window_early_stop_skips_old_tail(self):
        now = datetime(2026, 7, 24, 15, 0, 0)
        start, end = calendar_day_bounds(now)
        # Newest-first: today, then many old
        orders = []
        for i in range(3):
            orders.append({
                "status": "filled",
                "side": "buy",
                "timestamps": {"filled": now.replace(hour=10 + i).isoformat()},
            })
        for i in range(50):
            old = now - timedelta(days=2, hours=i)
            orders.append({
                "status": "filled",
                "side": "buy",
                "timestamps": {"filled": old.isoformat()},
            })
        filtered = OrderService._filter_window_newest_first(
            orders, start, end, early_stop=True, stop_streak=5,
        )
        self.assertEqual(len(filtered), 3)

    def test_send_orders_view_single_fetch(self):
        now = datetime.now().replace(microsecond=0)
        rec = self.svc.create_from_request(
            TradeOrder("SELL", "SOL/USDT", 70, 1, signal="SELL"),
            status="filled",
            telegram_token="t_perf",
        )
        self.svc.update_status(
            "t_perf", "filled",
            execution={"usdt": 70, "price": 70, "amount": 1},
        )
        data = self.svc._load()
        for o in data["orders"]:
            if o.get("id") == (rec.get("id") or "t_perf"):
                o["pnl"] = 5.0
                o.setdefault("timestamps", {})
                o["timestamps"]["filled"] = now.isoformat()
                o["timestamps"]["created"] = now.isoformat()
        self.svc._save(data)

        with patch.object(
            OrderService, "list_day_filled_all", wraps=self.svc.list_day_filled_all,
        ) as list_all, \
             patch.object(OrderService, "stats_day_filled") as stats_day, \
             patch("notifications.telegram_commands.order_commands.send_telegram_buttons"), \
             patch("notifications.telegram_commands.order_commands.send_telegram_message"), \
             patch("notifications.telegram_commands.order_commands.OrderService", return_value=self.svc):
            order_commands.send_orders_view(order_commands.VIEW_DAY)
            # Body uses list_day_filled_all; header must NOT call stats_day_filled again
            self.assertGreaterEqual(list_all.call_count, 1)
            stats_day.assert_not_called()


class TestPortfolioWarmPath(unittest.TestCase):
    def test_warm_bundle_skips_replay(self):
        from notifications.telegram_commands.position_display import _sim_order_ledger_bundle

        cfg = MagicMock()
        cfg.trading_mode = "live"
        cfg.raw = {"demo_mode": True}
        history = {"virtual_balance": 90000.0, "realized_pnl": 500.0}
        active = [{"symbol": "BTC/USDT", "amount": 0.1, "average_entry": 50000}]

        with patch(
            "strategies.positions.list_active_positions", return_value=active,
        ), patch(
            "core.simulated_trading.simulated_ledger_scope", return_value="demo",
        ), patch(
            "services.order_service.OrderService",
        ) as mock_os, patch(
            "data_manager.load_orders",
        ) as load_orders, patch(
            "core.sim_ledger_replay.replay_simulated_ledger",
        ) as replay:
            mock_svc = MagicMock()
            mock_svc.stats_day_filled_fast.return_value = {
                "filled": 2, "buys": 1, "sells": 1, "realized_pnl": 10.0,
                "buy_usdt": 1, "sell_usdt": 1, "sell_wins": 1, "sell_losses": 0,
            }
            mock_os.return_value = mock_svc
            out = _sim_order_ledger_bundle(
                tenant_id="henry",
                scope="demo",
                history=history,
                cfg=cfg,
                prefer_memory=True,
            )
            load_orders.assert_not_called()
            replay.assert_not_called()
            mock_svc.stats_day_filled.assert_not_called()
            mock_svc.stats_day_filled_fast.assert_called_once()
            self.assertEqual(out["cash_balance"], 90000.0)
            self.assertEqual(out["day_stats"]["sells"], 1)

    def test_warm_bundle_empty_memory_uses_ledger_not_blob(self):
        from notifications.telegram_commands.position_display import _sim_order_ledger_bundle

        cfg = MagicMock()
        cfg.trading_mode = "live"
        cfg.raw = {"demo_mode": True}
        history = {"virtual_balance": 90000.0, "realized_pnl": 500.0}
        ledger_lots = [{"symbol": "ETH/USDT", "amount": 1.0, "average_entry": 2000}]

        with patch(
            "strategies.positions.list_active_positions", return_value=[],
        ), patch(
            "strategies.positions.list_active_positions_from_ledger",
            return_value=ledger_lots,
        ) as from_ledger, patch(
            "core.simulated_trading.simulated_ledger_scope", return_value="demo",
        ), patch(
            "services.order_service.OrderService",
        ) as mock_os, patch(
            "data_manager.load_orders",
        ) as load_orders, patch(
            "core.sim_ledger_replay.replay_simulated_ledger",
        ) as replay:
            mock_svc = MagicMock()
            mock_svc.stats_day_filled_fast.return_value = {"filled": 0, "buys": 0, "sells": 0}
            mock_os.return_value = mock_svc
            out = _sim_order_ledger_bundle(
                tenant_id="default",
                scope="demo",
                history=history,
                cfg=cfg,
                prefer_memory=True,
            )
            from_ledger.assert_called_once_with(scope="demo", tenant_id="default")
            load_orders.assert_not_called()
            replay.assert_not_called()
            self.assertEqual(out["active"], ledger_lots)
            self.assertEqual(out["cash_balance"], 90000.0)

    def test_stats_day_filled_fast_skips_blob(self):
        from services.order_service import OrderService

        svc = OrderService("demo")
        fake_store = MagicMock()
        fake_store.query_day.return_value = [
            {"status": "filled", "side": "sell", "pnl": 3.0, "execution": {"usdt": 10}},
        ]
        with patch("storage.order_ledger_v2.get_order_ledger_v2", return_value=fake_store), \
             patch.object(svc, "_v2_day_key", return_value="2026-09-01"), \
             patch("data_manager.load_orders") as load_orders:
            stats = svc.stats_day_filled_fast()
        load_orders.assert_not_called()
        fake_store.query_day.assert_called_once()
        self.assertEqual(stats["sells"], 1)
        self.assertAlmostEqual(stats["realized_pnl"], 3.0)


if __name__ == "__main__":
    unittest.main()
