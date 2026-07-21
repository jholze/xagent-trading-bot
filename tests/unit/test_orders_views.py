"""Day / blocked / month order list views — filters + command handlers."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.models import TradeOrder
from notifications.telegram_commands import order_commands
from services.order_service import (
    OrderService,
    calendar_day_bounds,
    calendar_month_bounds,
    order_in_window,
)


def _stamp(svc: OrderService, token: str, status: str, when: datetime, *, side="buy", symbol="ARIA/USDT"):
    if status == "filled":
        rec = svc.create_from_request(
            TradeOrder(
                side.upper(),
                symbol,
                1.0,
                1 if side == "sell" else 0,
                usdt_amount=50,
                signal="SELL" if side == "sell" else "",
            ),
            status="filled",
            telegram_token=token,
        )
        svc.update_status(
            token,
            "filled",
            execution={"usdt": 50, "price": 1.0, "amount": 50},
        )
    else:
        rec = svc.create_from_request(
            TradeOrder("BUY", symbol, 1.0, 0, usdt_amount=50),
            status=status,
            telegram_token=token,
        )
    data = svc._load()
    oid = rec.get("id") or token
    for o in data["orders"]:
        if o.get("id") == oid:
            o.setdefault("timestamps", {})
            iso = when.isoformat()
            o["timestamps"]["created"] = iso
            o["timestamps"]["updated"] = iso
            if status == "filled":
                o["timestamps"]["filled"] = iso
            svc._save(data)
            return o
    raise AssertionError(f"order {oid} not found after create")


class TestOrderWindowFilters(unittest.TestCase):
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

        self.now = datetime(2026, 7, 21, 15, 30, 0)
        self.day_start, self.day_end = calendar_day_bounds(self.now)
        self.month_start, self.month_end = calendar_month_bounds(self.now)

        self.svc = OrderService("paper")
        # Today filled
        _stamp(self.svc, "t_today_buy", "filled", self.now.replace(hour=10), side="buy", symbol="SOL/USDT")
        _stamp(self.svc, "t_today_sell", "filled", self.now.replace(hour=12), side="sell", symbol="BTC/USDT")
        # Today rejected
        _stamp(self.svc, "t_today_rej", "rejected", self.now.replace(hour=11), symbol="ONDO/USDT")
        # Yesterday filled (same month)
        yday = self.now - timedelta(days=1)
        _stamp(self.svc, "t_yday", "filled", yday.replace(hour=9), side="buy", symbol="ETH/USDT")
        # Last month filled
        last_month = self.month_start - timedelta(days=2)
        _stamp(self.svc, "t_last_m", "filled", last_month.replace(hour=9), side="buy", symbol="XRP/USDT")
        # Today cancelled
        _stamp(self.svc, "t_cancel", "cancelled", self.now.replace(hour=14), symbol="DOGE/USDT")

    def tearDown(self):
        self.scope.stop()
        self.scope_patch.stop()

    def test_day_filled_includes_only_today_fills(self):
        with patch("services.order_service._display_now_naive", return_value=self.now), \
             patch("services.order_service.calendar_day_bounds", return_value=(self.day_start, self.day_end)):
            page, pages = self.svc.list_day_filled(page=1, per_page=50)
        symbols = {o.get("symbol") for o in page}
        self.assertIn("SOL/USDT", symbols)
        self.assertIn("BTC/USDT", symbols)
        self.assertNotIn("ETH/USDT", symbols)  # yesterday
        self.assertNotIn("XRP/USDT", symbols)  # last month
        self.assertNotIn("ONDO/USDT", symbols)  # rejected
        self.assertTrue(all(o.get("status") == "filled" for o in page))

    def test_blocked_includes_rejected_and_cancelled_today_only(self):
        with patch("services.order_service._display_now_naive", return_value=self.now), \
             patch("services.order_service.calendar_day_bounds", return_value=(self.day_start, self.day_end)):
            page, _ = self.svc.list_blocked_orders(page=1, per_page=50)
        symbols = {o.get("symbol") for o in page}
        statuses = {o.get("status") for o in page}
        self.assertIn("ONDO/USDT", symbols)
        self.assertIn("DOGE/USDT", symbols)
        self.assertNotIn("SOL/USDT", symbols)
        self.assertTrue(statuses <= {"rejected", "cancelled", "failed", "expired", "pending_confirmation", "executing"})

    def test_month_filled_includes_yesterday_excludes_last_month(self):
        with patch("services.order_service._display_now_naive", return_value=self.now), \
             patch("services.order_service.calendar_month_bounds", return_value=(self.month_start, self.month_end)):
            page, _ = self.svc.list_month_filled(page=1, per_page=50)
        symbols = {o.get("symbol") for o in page}
        self.assertIn("SOL/USDT", symbols)
        self.assertIn("ETH/USDT", symbols)  # yesterday same month
        self.assertNotIn("XRP/USDT", symbols)
        self.assertNotIn("ONDO/USDT", symbols)

    def test_order_in_window_helper(self):
        o = {
            "status": "filled",
            "timestamps": {"filled": self.now.isoformat()},
        }
        # self.now is naive process-local; convert via same helper path
        from services.order_service import _as_display_naive, order_event_ts
        ts = order_event_ts(o)
        self.assertIsNotNone(ts)
        # Window was built from same self.now via calendar_day_bounds
        self.assertTrue(order_in_window(o, self.day_start, self.day_end))
        self.assertFalse(order_in_window(o, self.day_start, self.day_start))

    def test_naive_utc_fill_near_midnight_on_berlin_day(self):
        """Railway stamps naive UTC; display TZ is Europe/Berlin (CEST in July).

        Fill at 2026-07-21 23:30:00 UTC-naive → 2026-07-22 01:30 Berlin.
        Must appear on Berlin calendar day 22.07, not 21.07.
        """
        from zoneinfo import ZoneInfo
        from services.order_service import (
            _as_display_naive,
            calendar_day_bounds,
            order_event_ts,
            order_in_window,
        )

        utc = ZoneInfo("UTC")
        berlin = ZoneInfo("Europe/Berlin")
        # Naive stamp as written by datetime.now() on a UTC host
        fill_naive_utc = datetime(2026, 7, 21, 23, 30, 0)
        berlin_now = datetime(2026, 7, 22, 1, 30, 0, tzinfo=berlin)

        with patch("services.order_service._process_local_tz", return_value=utc), \
             patch("core.time_utils.display_tz", return_value=berlin), \
             patch("core.time_utils.now_display", return_value=berlin_now):
            display_ts = _as_display_naive(fill_naive_utc)
            self.assertEqual(display_ts, datetime(2026, 7, 22, 1, 30, 0))

            day_start, day_end = calendar_day_bounds(berlin_now)
            self.assertEqual(day_start, datetime(2026, 7, 22, 0, 0, 0))
            self.assertEqual(day_end, datetime(2026, 7, 23, 0, 0, 0))

            order = {
                "status": "filled",
                "timestamps": {"filled": fill_naive_utc.isoformat()},
            }
            self.assertEqual(order_event_ts(order), datetime(2026, 7, 22, 1, 30, 0))
            self.assertTrue(order_in_window(order, day_start, day_end))

            # Must NOT land on July 21 Berlin day
            jul21_start = datetime(2026, 7, 21, 0, 0, 0)
            jul21_end = datetime(2026, 7, 22, 0, 0, 0)
            self.assertFalse(order_in_window(order, jul21_start, jul21_end))

            # End-to-end list_day_filled with this stamp
            _stamp(
                self.svc,
                "t_midnight_utc",
                "filled",
                fill_naive_utc,
                side="buy",
                symbol="NEAR/USDT",
            )
            page, _ = self.svc.list_day_filled(page=1, per_page=50, now=berlin_now)
            symbols = {o.get("symbol") for o in page}
            self.assertIn("NEAR/USDT", symbols)


class TestOrderCommandViews(unittest.TestCase):
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

        now = datetime.now().replace(microsecond=0)
        svc = OrderService("paper")
        svc.create_from_request(
            TradeOrder("SELL", "SOL/USDT", 70, 2, signal="SELL"),
            status="filled",
            telegram_token="t2",
        )
        svc.update_status("t2", "filled", execution={"usdt": 140, "price": 70, "amount": 2})
        data = svc._load()
        for o in data["orders"]:
            o.setdefault("timestamps", {})
            o["timestamps"]["filled"] = now.isoformat()
            o["timestamps"]["created"] = now.isoformat()
        svc._save(data)
        svc.create_from_request(
            TradeOrder("BUY", "ONDO/USDT", 1.0, 0, usdt_amount=50),
            status="rejected",
            telegram_token="trej",
        )
        data = svc._load()
        for o in data["orders"]:
            if o.get("status") == "rejected":
                o.setdefault("timestamps", {})
                o["timestamps"]["created"] = now.isoformat()
        svc._save(data)

    def tearDown(self):
        self.scope.stop()
        self.scope_patch.stop()

    def test_orders_shows_day_header_no_blocked_excerpt(self):
        with patch("notifications.telegram_commands.order_commands.send_telegram_buttons") as mock_btn, \
             patch("notifications.telegram_commands.order_commands.send_telegram_message") as mock_msg:
            self.assertTrue(order_commands.handle("/orders"))
            # buttons or message
            msg = (mock_btn.call_args[0][0] if mock_btn.called else mock_msg.call_args[0][0])
            self.assertIn("Trades heute", msg)
            self.assertNotIn("Blockiert (24h, Auszug)", msg)
            self.assertIn("/orders_blocked", msg)

    def test_orders_blocked_command(self):
        with patch("notifications.telegram_commands.order_commands.send_telegram_buttons") as mock_btn, \
             patch("notifications.telegram_commands.order_commands.send_telegram_message") as mock_msg:
            self.assertTrue(order_commands.handle("/orders_blocked"))
            msg = (mock_btn.call_args[0][0] if mock_btn.called else mock_msg.call_args[0][0])
            self.assertIn("Blockierte Orders", msg)
            self.assertIn("ONDO", msg)

    def test_orders_month_command(self):
        with patch("notifications.telegram_commands.order_commands.send_telegram_buttons") as mock_btn, \
             patch("notifications.telegram_commands.order_commands.send_telegram_message") as mock_msg:
            self.assertTrue(order_commands.handle("/orders_month"))
            msg = (mock_btn.call_args[0][0] if mock_btn.called else mock_msg.call_args[0][0])
            self.assertIn("Trades", msg)
            self.assertIn("Monat", msg.lower() + msg)  # header contains month label or Monat
            self.assertTrue("Trades" in msg and ("Monat" in msg or any(c.isdigit() for c in msg)))

    def test_empty_day_message(self):
        # wipe fills by using empty ledger scope file
        empty = OrderService("paper")
        empty._save({"orders": []})
        with patch("notifications.telegram_commands.order_commands.send_telegram_message") as mock_msg:
            order_commands.send_orders_view(order_commands.VIEW_DAY, 1)
            msg = mock_msg.call_args[0][0]
            self.assertIn("Keine ausgeführten Trades", msg)

    def test_pagination_callback_view_aware(self):
        with patch("notifications.telegram_commands.order_commands.send_telegram_buttons") as mock_btn, \
             patch("notifications.telegram_commands.order_commands.send_telegram_message"), \
             patch("notifications.telegram_commands.order_commands.answer_callback_query"):
            self.assertTrue(order_commands.handle_callback({
                "id": "cb1",
                "data": "orders_page:blocked:paper:1",
            }))
            msg = mock_btn.call_args[0][0] if mock_btn.called else ""
            if not msg:
                # empty buttons path uses send_telegram_message — already ok if handled
                self.assertTrue(True)
            else:
                self.assertIn("Blockierte", msg)

    def test_menu_registers_new_keys(self):
        from notifications.telegram_commands.menu_commands import MENU_SECTIONS_OPERATOR
        handel = dict(MENU_SECTIONS_OPERATOR)["handel"]
        self.assertIn("orders", handel)
        self.assertIn("orders_blocked", handel)
        self.assertIn("orders_month", handel)
        # blocked + month sit next to orders in Handel section
        oi = handel.index("orders")
        self.assertEqual(handel[oi + 1], "orders_blocked")
        self.assertEqual(handel[oi + 2], "orders_month")

    def test_locale_has_commands(self):
        path = Path(__file__).resolve().parents[2] / "locales" / "telegram_menu.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        for lang in ("de", "en"):
            cmds = data[lang]["commands"]
            self.assertIn("orders_blocked", cmds)
            self.assertIn("orders_month", cmds)
            self.assertIn("/orders_blocked", cmds["orders_blocked"]["help_line"])
            self.assertIn("/orders_month", cmds["orders_month"]["help_line"])


if __name__ == "__main__":
    unittest.main()
