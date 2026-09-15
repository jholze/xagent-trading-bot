"""#452 — /short and /cover must preview + confirm, never execute on the typed line.

Two layers:
* ``short_commands`` only parses and hands off to ``request_*_confirmation``;
  ``execute_short`` / ``execute_cover`` are never reached from the command.
* ``manual_order_flow`` stores a ``pending_confirmation`` row, shows the
  resolved default size / percent on the preview, and executes solely from the
  ``manual_ok`` callback.
"""

from __future__ import annotations

import inspect
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.models import RiskDecision, TradeOrder, TradeResult
from notifications.telegram_commands import short_commands
from notifications.telegram_commands.manual_order_flow import (
    handle_callback,
    request_cover_confirmation,
    request_short_confirmation,
)
from services.order_service import OrderService

SC = "notifications.telegram_commands.short_commands"
MOF = "notifications.telegram_commands.manual_order_flow"


def _short_pos(sym="H/USDT", amount=400.0, entry=0.05, lev=2.0, tf="4h"):
    return {
        "symbol": sym,
        "amount": amount,
        "average_entry": entry,
        "side": "short",
        "leverage": lev,
        "timeframe": tf,
    }


def _enabled_cfg():
    cfg = MagicMock()
    cfg.raw = {"shorts": {"enabled": True}}
    return cfg


class TestShortCommandNeverExecutesDirectly(unittest.TestCase):
    """The command layer must not touch execute_short / execute_cover at all."""

    def test_source_has_no_direct_execute_call(self):
        # Walk the AST: no attribute access named execute_short / execute_cover
        # anywhere in the command module (docstrings/comments don't count).
        import ast

        tree = ast.parse(inspect.getsource(short_commands))
        attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        for forbidden in ("execute_short", "execute_cover", "execute_order"):
            self.assertNotIn(forbidden, attrs)
            self.assertNotIn(forbidden, names)

    def test_short_without_size_requests_confirmation_with_defaults(self):
        trading = MagicMock()
        with patch(f"{SC}.get_bot_config", return_value=_enabled_cfg()), \
             patch(f"{SC}.shorts_enabled", return_value=True), \
             patch(f"{SC}.get_prices_batch", return_value={"H/USDT": 0.05}), \
             patch(f"{SC}._trading", trading), \
             patch(f"{SC}.request_short_confirmation", return_value=True) as req:
            self.assertTrue(short_commands.handle("/short H"))
        req.assert_called_once()
        kw = req.call_args.kwargs
        self.assertEqual(kw["symbol"], "H/USDT")
        self.assertEqual(kw["timeframe"], "4h")
        self.assertAlmostEqual(kw["price"], 0.05)
        self.assertIsNone(kw["usdt"])
        self.assertIsNone(kw["leverage"])
        trading.execute_short.assert_not_called()
        trading.refresh.return_value.execute_short.assert_not_called()

    def test_short_with_size_and_leverage_passes_them_through(self):
        trading = MagicMock()
        with patch(f"{SC}.get_bot_config", return_value=_enabled_cfg()), \
             patch(f"{SC}.shorts_enabled", return_value=True), \
             patch(f"{SC}.get_prices_batch", return_value={"H/USDT": 0.05}), \
             patch(f"{SC}._trading", trading), \
             patch(f"{SC}.request_short_confirmation", return_value=True) as req:
            self.assertTrue(short_commands.handle("/short h 400 2"))
        kw = req.call_args.kwargs
        self.assertEqual(kw["symbol"], "H/USDT")
        self.assertAlmostEqual(kw["usdt"], 400.0)
        self.assertAlmostEqual(kw["leverage"], 2.0)
        trading.execute_short.assert_not_called()

    def test_short_invalid_leverage_is_an_error_not_a_default(self):
        trading = MagicMock()
        with patch(f"{SC}.get_bot_config", return_value=_enabled_cfg()), \
             patch(f"{SC}.shorts_enabled", return_value=True), \
             patch(f"{SC}.get_prices_batch", return_value={"H/USDT": 0.05}), \
             patch(f"{SC}._trading", trading), \
             patch(f"{SC}.send_telegram_message") as send, \
             patch(f"{SC}.request_short_confirmation") as req:
            self.assertTrue(short_commands.handle("/short H 400 x"))
        req.assert_not_called()
        trading.execute_short.assert_not_called()
        self.assertIn("Hebel", send.call_args[0][0])

    def test_short_usage_mentions_confirmation(self):
        with patch(f"{SC}.get_bot_config", return_value=_enabled_cfg()), \
             patch(f"{SC}.shorts_enabled", return_value=True), \
             patch(f"{SC}.send_telegram_message") as send, \
             patch(f"{SC}.request_short_confirmation") as req:
            self.assertTrue(short_commands.handle("/short"))
        req.assert_not_called()
        self.assertIn("bestätigt", send.call_args[0][0])

    def test_cover_without_percent_requests_full_cover_confirmation(self):
        trading = MagicMock()
        pos = _short_pos()
        with patch(f"{SC}.list_active_positions", return_value=[pos]), \
             patch(f"{SC}.get_prices_batch", return_value={"H/USDT": 0.04}), \
             patch(f"{SC}.get_position", return_value=pos), \
             patch(f"{SC}._trading", trading), \
             patch(f"{SC}.request_cover_confirmation", return_value=True) as req:
            self.assertTrue(short_commands.handle("/cover H"))
        req.assert_called_once()
        kw = req.call_args.kwargs
        self.assertEqual(kw["symbol"], "H/USDT")
        self.assertEqual(kw["timeframe"], "4h")
        self.assertAlmostEqual(kw["pct"], 1.0)
        self.assertAlmostEqual(kw["amount"], 400.0)
        self.assertAlmostEqual(kw["price"], 0.04)
        trading.execute_cover.assert_not_called()
        trading.refresh.return_value.execute_cover.assert_not_called()

    def test_cover_with_percent_is_a_fraction(self):
        trading = MagicMock()
        pos = _short_pos()
        with patch(f"{SC}.list_active_positions", return_value=[pos]), \
             patch(f"{SC}.get_prices_batch", return_value={"H/USDT": 0.04}), \
             patch(f"{SC}.get_position", return_value=pos), \
             patch(f"{SC}._trading", trading), \
             patch(f"{SC}.request_cover_confirmation", return_value=True) as req:
            self.assertTrue(short_commands.handle("/cover H 50"))
        kw = req.call_args.kwargs
        self.assertAlmostEqual(kw["pct"], 0.5)
        self.assertAlmostEqual(kw["amount"], 200.0)
        trading.execute_cover.assert_not_called()

    def test_cover_invalid_percent_is_an_error_not_100(self):
        trading = MagicMock()
        pos = _short_pos()
        for bad in ("/cover H abc", "/cover H 0", "/cover H 150"):
            with patch(f"{SC}.list_active_positions", return_value=[pos]), \
                 patch(f"{SC}.get_prices_batch", return_value={"H/USDT": 0.04}), \
                 patch(f"{SC}.get_position", return_value=pos), \
                 patch(f"{SC}._trading", trading), \
                 patch(f"{SC}.send_telegram_message") as send, \
                 patch(f"{SC}.request_cover_confirmation") as req:
                self.assertTrue(short_commands.handle(bad), bad)
            req.assert_not_called()
            self.assertIn("Prozent", send.call_args[0][0], bad)
        trading.execute_cover.assert_not_called()

    def test_cover_on_long_refuses(self):
        trading = MagicMock()
        long_pos = {"symbol": "H/USDT", "amount": 10.0, "average_entry": 1.0, "timeframe": "4h"}
        with patch(f"{SC}.list_active_positions", return_value=[long_pos]), \
             patch(f"{SC}.get_prices_batch", return_value={"H/USDT": 1.1}), \
             patch(f"{SC}.get_position", return_value=long_pos), \
             patch(f"{SC}._trading", trading), \
             patch(f"{SC}.send_telegram_message") as send, \
             patch(f"{SC}.request_cover_confirmation") as req:
            self.assertTrue(short_commands.handle("/cover H"))
        req.assert_not_called()
        trading.execute_cover.assert_not_called()
        self.assertIn("/sell", send.call_args[0][0])


class TestShortCoverConfirmFlow(unittest.TestCase):
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

    def tearDown(self):
        self.scope.stop()
        self.scope_patch.stop()

    @staticmethod
    def _status():
        return {
            "virtual_balance": 4000,
            "open_positions": 1,
            "max_open_positions": 10,
            "daily_trades": 0,
            "max_daily_trades": 8,
            "max_position_percent": 30,
            "drawdown_pct": 0.0,
            "drawdown_throttle_active": False,
        }

    def _trading(self, decision):
        trading = MagicMock()
        trading.refresh.return_value = trading
        trading.evaluate_risk.return_value = decision
        trading.risk.status_summary.return_value = self._status()
        trading.config.risk_config = {"drawdown_throttle_pct": 10.0}
        return trading

    # -- /short -----------------------------------------------------------

    def test_short_preview_shows_default_size_and_leverage(self):
        # Operator typed no size / no leverage -> RiskManager resolved 25 USDT @ 2x.
        decision = RiskDecision(
            approved=True,
            order=TradeOrder("SHORT", "H/USDT", 0.05, amount=500.0, usdt_amount=25.0,
                             signal="SHORT", leverage=2.0),
            message="ok",
        )
        trading = self._trading(decision)
        with patch(f"{MOF}.get_position", return_value={}), \
             patch(f"{MOF}.send_telegram_buttons") as buttons, \
             patch(f"{MOF}.send_telegram_message") as send:
            self.assertTrue(request_short_confirmation(
                trading, symbol="H/USDT", timeframe="4h", price=0.05, usdt=None, leverage=None,
            ))
        trading.execute_short.assert_not_called()
        send.assert_not_called()
        buttons.assert_called_once()
        msg, keyboard = buttons.call_args[0][0], buttons.call_args[0][1]
        self.assertIn("Short", msg)
        self.assertIn("$25", msg)
        self.assertIn("Standardgröße", msg)
        self.assertIn("2×", msg)
        self.assertIn("Standard — kein Hebel", msg)
        self.assertNotIn("· ok", msg)
        self.assertIn("manual_ok:", keyboard[0][0]["callback_data"])
        self.assertIn("manual_no:", keyboard[0][1]["callback_data"])

        orders, _ = OrderService("paper").list_orders()
        self.assertEqual(len(orders), 1)
        rec = orders[0]
        self.assertEqual(rec["status"], "pending_confirmation")
        self.assertEqual(rec["side"], "short")
        self.assertEqual(rec["source"], "manual")
        # The previewed size is what gets stored — confirm executes exactly this.
        self.assertAlmostEqual(rec["request"]["usdt"], 25.0)
        self.assertAlmostEqual(rec["request"]["leverage"], 2.0)
        self.assertIsNone(rec["request"]["requested_usdt"])

    def test_short_preview_with_explicit_size(self):
        decision = RiskDecision(
            approved=True,
            order=TradeOrder("SHORT", "H/USDT", 0.05, amount=8000.0, usdt_amount=400.0,
                             signal="SHORT", leverage=2.0),
            message="ok",
        )
        trading = self._trading(decision)
        with patch(f"{MOF}.get_position", return_value={}), \
             patch(f"{MOF}.send_telegram_buttons") as buttons:
            request_short_confirmation(
                trading, symbol="H/USDT", timeframe="4h", price=0.05, usdt=400.0, leverage=2.0,
            )
        msg = buttons.call_args[0][0]
        self.assertIn("$400", msg)
        self.assertNotIn("Standardgröße", msg)
        self.assertIn("Margin", msg)

    def test_short_rejected_sends_message_no_buttons(self):
        decision = RiskDecision(approved=False, message="shorts.max_open reached", code="shorts_slots")
        trading = self._trading(decision)
        with patch(f"{MOF}.send_telegram_buttons") as buttons, \
             patch(f"{MOF}.send_telegram_message") as send:
            self.assertTrue(request_short_confirmation(
                trading, symbol="H/USDT", timeframe="4h", price=0.05, usdt=None, leverage=None,
            ))
        buttons.assert_not_called()
        send.assert_called_once()
        self.assertIn("Short blockiert", send.call_args[0][0])
        self.assertIn("shorts.max_open", send.call_args[0][0])
        trading.execute_short.assert_not_called()
        rejected, _ = OrderService("paper").list_orders(status_filter={"rejected"})
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["side"], "short")

    def test_confirm_executes_pending_short_with_previewed_size(self):
        trading = MagicMock()
        trading.refresh.return_value = trading
        trading.execute_short.return_value = TradeResult(
            True, "SHORT", "H/USDT", amount=500, price=0.05, usdt_amount=25,
        )
        OrderService("paper").create_from_request(
            TradeOrder("SHORT", "H/USDT", 0.05, amount=0, usdt_amount=25.0,
                       signal="SHORT", source="manual", leverage=2.0),
            timeframe="4h",
            status="pending_confirmation",
            request_extra={"requested_usdt": None, "requested_leverage": None},
            telegram_token="sh0rt1",
        )
        with patch(f"{MOF}.TradingService", return_value=trading), \
             patch("price_fetcher.get_prices", return_value=(0.05, 0.05, None)), \
             patch(f"{MOF}.answer_callback_query"):
            self.assertTrue(handle_callback({"id": "cb", "data": "manual_ok:sh0rt1"}))
        trading.execute_short.assert_called_once_with(
            "H/USDT", "4h", 0.05, usdt=25.0, leverage=2.0, order_id="sh0rt1",
        )
        trading.execute_cover.assert_not_called()

    def test_cancel_pending_short(self):
        svc = OrderService("paper")
        svc.create_from_request(
            TradeOrder("SHORT", "H/USDT", 0.05, amount=0, usdt_amount=25.0,
                       signal="SHORT", source="manual", leverage=2.0),
            timeframe="4h",
            status="pending_confirmation",
            telegram_token="sh0rt2",
        )
        trading = MagicMock()
        with patch(f"{MOF}.TradingService", return_value=trading), \
             patch(f"{MOF}.send_telegram_message") as send, \
             patch(f"{MOF}.answer_callback_query"):
            self.assertTrue(handle_callback({"id": "cb", "data": "manual_no:sh0rt2"}))
        self.assertEqual(svc.get_by_id("sh0rt2")["status"], "cancelled")
        self.assertIn("abgebrochen", send.call_args[0][0])
        trading.execute_short.assert_not_called()

    def test_confirm_twice_does_not_execute_twice(self):
        trading = MagicMock()
        trading.refresh.return_value = trading
        trading.execute_short.return_value = TradeResult(
            True, "SHORT", "H/USDT", amount=500, price=0.05, usdt_amount=25,
        )
        svc = OrderService("paper")
        svc.create_from_request(
            TradeOrder("SHORT", "H/USDT", 0.05, amount=0, usdt_amount=25.0,
                       signal="SHORT", source="manual", leverage=2.0),
            timeframe="4h",
            status="pending_confirmation",
            telegram_token="sh0rt3",
        )
        with patch(f"{MOF}.TradingService", return_value=trading), \
             patch("price_fetcher.get_prices", return_value=(0.05, 0.05, None)), \
             patch(f"{MOF}.send_telegram_message") as send, \
             patch(f"{MOF}.answer_callback_query"):
            handle_callback({"id": "cb", "data": "manual_ok:sh0rt3"})
            # First confirm hands the row to the (mocked) engine; a real run
            # flips it to active/filled. Simulate that, then tap again.
            svc.update_status("sh0rt3", "filled")
            handle_callback({"id": "cb", "data": "manual_ok:sh0rt3"})
        self.assertEqual(trading.execute_short.call_count, 1)
        self.assertIn("abgelaufen", send.call_args[0][0])

    # -- /cover -----------------------------------------------------------

    def test_cover_preview_shows_default_100_percent(self):
        decision = RiskDecision(
            approved=True,
            order=TradeOrder("COVER", "H/USDT", 0.04, amount=400.0, signal="COVER"),
            message="ok",
        )
        trading = self._trading(decision)
        with patch(f"{MOF}.get_position", return_value=_short_pos()), \
             patch(f"{MOF}.send_telegram_buttons") as buttons, \
             patch(f"{MOF}.send_telegram_message") as send:
            self.assertTrue(request_cover_confirmation(
                trading, symbol="H/USDT", timeframe="4h", price=0.04, amount=400.0, pct=1.0,
            ))
        trading.execute_cover.assert_not_called()
        send.assert_not_called()
        buttons.assert_called_once()
        msg, keyboard = buttons.call_args[0][0], buttons.call_args[0][1]
        self.assertIn("Cover", msg)
        self.assertIn("100%", msg)
        self.assertIn("Standard: alles", msg)
        self.assertIn("400.0000", msg)
        self.assertIn("PnL", msg)
        self.assertIn("manual_ok:", keyboard[0][0]["callback_data"])

        orders, _ = OrderService("paper").list_orders()
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["status"], "pending_confirmation")
        self.assertEqual(orders[0]["side"], "cover")
        self.assertAlmostEqual(orders[0]["request"]["pct"], 1.0)

    def test_cover_preview_partial_has_no_default_tag(self):
        decision = RiskDecision(
            approved=True,
            order=TradeOrder("COVER", "H/USDT", 0.04, amount=200.0, signal="COVER"),
            message="ok",
        )
        trading = self._trading(decision)
        with patch(f"{MOF}.get_position", return_value=_short_pos()), \
             patch(f"{MOF}.send_telegram_buttons") as buttons:
            request_cover_confirmation(
                trading, symbol="H/USDT", timeframe="4h", price=0.04, amount=200.0, pct=0.5,
            )
        msg = buttons.call_args[0][0]
        self.assertIn("50%", msg)
        self.assertNotIn("Standard: alles", msg)

    def test_cover_rejected_sends_message_no_buttons(self):
        decision = RiskDecision(approved=False, message="no short to cover", code="no_short")
        trading = self._trading(decision)
        with patch(f"{MOF}.send_telegram_buttons") as buttons, \
             patch(f"{MOF}.send_telegram_message") as send:
            request_cover_confirmation(
                trading, symbol="H/USDT", timeframe="4h", price=0.04, amount=400.0, pct=1.0,
            )
        buttons.assert_not_called()
        self.assertIn("Cover blockiert", send.call_args[0][0])
        trading.execute_cover.assert_not_called()

    def test_confirm_executes_pending_cover_from_live_position(self):
        trading = MagicMock()
        trading.refresh.return_value = trading
        trading.execute_cover.return_value = TradeResult(
            True, "COVER", "H/USDT", amount=200, price=0.04, usdt_amount=8, pnl=2.0,
        )
        OrderService("paper").create_from_request(
            TradeOrder("COVER", "H/USDT", 0.04, amount=200.0, signal="COVER", source="manual"),
            timeframe="4h",
            status="pending_confirmation",
            request_extra={"pct": 0.5, "amount": 200.0},
            telegram_token="c0ver1",
        )
        with patch(f"{MOF}.TradingService", return_value=trading), \
             patch(f"{MOF}.get_position", return_value=_short_pos(amount=400.0)), \
             patch("price_fetcher.get_prices", return_value=(0.04, 0.04, None)), \
             patch(f"{MOF}.answer_callback_query"):
            self.assertTrue(handle_callback({"id": "cb", "data": "manual_ok:c0ver1"}))
        trading.execute_cover.assert_called_once_with(
            "H/USDT", "4h", 0.04, amount=200.0, order_id="c0ver1",
        )
        trading.execute_short.assert_not_called()

    def test_confirm_cover_fails_closed_when_short_is_gone(self):
        trading = MagicMock()
        trading.refresh.return_value = trading
        svc = OrderService("paper")
        svc.create_from_request(
            TradeOrder("COVER", "H/USDT", 0.04, amount=400.0, signal="COVER", source="manual"),
            timeframe="4h",
            status="pending_confirmation",
            request_extra={"pct": 1.0, "amount": 400.0},
            telegram_token="c0ver2",
        )
        with patch(f"{MOF}.TradingService", return_value=trading), \
             patch(f"{MOF}.get_position", return_value={}), \
             patch("price_fetcher.get_prices", return_value=(0.04, 0.04, None)), \
             patch(f"{MOF}.send_telegram_message") as send, \
             patch(f"{MOF}.answer_callback_query"):
            self.assertTrue(handle_callback({"id": "cb", "data": "manual_ok:c0ver2"}))
        trading.execute_cover.assert_not_called()
        self.assertEqual(svc.get_by_id("c0ver2")["status"], "failed")
        self.assertIn("Kein offener Short", send.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
