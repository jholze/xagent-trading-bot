"""#447 — one /positions and one /orders; extra views as buttons.

Satellite Handel/home/Mehr must not list positions_full / orders_blocked /
orders_month as first-class keys. The same bodies stay reachable from
compact /positions (Mehr Details) and /orders (Alle / Blockiert / Dieser Monat).
Slash aliases remain for the operator.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from core.models import TradeOrder
from notifications.telegram_commands import order_commands, portfolio_commands, router
from notifications.telegram_commands.command_context import _chat_id_var
from notifications.telegram_commands.menu_commands import (
    HOME_KEYS,
    MENU_SECTIONS_OPERATOR,
    MENU_SECTIONS_SATELLITE,
    MORE_GROUPS_OPERATOR,
    MORE_GROUPS_SATELLITE,
)
from notifications.telegram_commands.position_display import send_positions_snapshot
from services.order_service import OrderService

HIDDEN = ("positions_full", "orders_blocked", "orders_month")


def _pin_de():
    from notifications.telegram_commands.menu_i18n import set_user_language

    set_user_language("de")


def _flat_keys(sections) -> set[str]:
    return {k for _, keys in sections for k in keys}


class TestSatelliteKeyboardHidesAliases(unittest.TestCase):
    def setUp(self):
        _pin_de()

    def test_satellite_catalog_home_and_mehr_omit_extra_slash_names(self):
        sat = _flat_keys(MENU_SECTIONS_SATELLITE)
        mehr = _flat_keys(MORE_GROUPS_SATELLITE)
        for key in HIDDEN:
            self.assertNotIn(key, sat)
            self.assertNotIn(key, mehr)
            self.assertNotIn(key, HOME_KEYS)
        self.assertIn("positions", sat)
        self.assertIn("orders", sat)
        self.assertIn("positions", HOME_KEYS)
        self.assertIn("orders", mehr)

    def test_operator_keeps_aliases_on_handel_and_mehr(self):
        op = _flat_keys(MENU_SECTIONS_OPERATOR)
        mehr = _flat_keys(MORE_GROUPS_OPERATOR)
        handel = dict(MENU_SECTIONS_OPERATOR)["handel"]
        for key in HIDDEN:
            self.assertIn(key, op)
            self.assertIn(key, mehr)
            self.assertIn(key, handel)

    def test_hidden_aliases_are_operator_only_typed_commands(self):
        for key in HIDDEN:
            self.assertIn(key, router.OPERATOR_ONLY)
        self.assertIn("positions full", router.OPERATOR_ONLY)
        self.assertTrue(router._is_operator_only_text("/positions_full"))
        self.assertTrue(router._is_operator_only_text("/positions full"))
        self.assertTrue(router._is_operator_only_text("/orders_blocked"))
        self.assertTrue(router._is_operator_only_text("/orders_month"))
        self.assertFalse(router._is_operator_only_callback("pos_more:full"))
        self.assertFalse(router._is_operator_only_callback("orders_view:blocked"))


class TestPositionsMehrDetails(unittest.TestCase):
    def setUp(self):
        _pin_de()

    def _snapshot_ctx(self):
        return {
            "history": {"virtual_balance": 5000, "trades": []},
            "cash_balance": 5000.0,
            "cash_label": "Cash",
            "gate_holdings": None,
            "active": [],
        }

    def test_compact_snapshot_attaches_mehr_details_button(self):
        with patch("telegram_notifier.send_telegram_buttons", return_value=True) as mock_btn, \
             patch("telegram_notifier.send_telegram_message", return_value=True), \
             patch("price_fetcher.get_prices_batch", return_value=({}, {})), \
             patch(
                 "notifications.telegram_commands.position_display.resolve_portfolio_context",
                 return_value=self._snapshot_ctx(),
             ), \
             patch("services.trading_service.TradingService") as mock_svc:
            mock_svc.return_value.mode_label.return_value = "demo"
            send_positions_snapshot(
                fast=True, detail_level="compact", tenant_id="default", scope="demo"
            )
        mock_btn.assert_called_once()
        buttons = mock_btn.call_args[0][1]
        self.assertEqual(buttons[0][0]["callback_data"], "pos_more:full")
        self.assertIn(buttons[0][0]["text"], ("Mehr Details", "More details"))

    def test_full_snapshot_has_no_mehr_details_button(self):
        with patch("telegram_notifier.send_telegram_buttons", return_value=True) as mock_btn, \
             patch("telegram_notifier.send_telegram_message", return_value=True) as mock_msg, \
             patch("price_fetcher.get_prices_batch", return_value=({}, {})), \
             patch(
                 "notifications.telegram_commands.position_display.resolve_portfolio_context",
                 return_value=self._snapshot_ctx(),
             ), \
             patch("services.trading_service.TradingService") as mock_svc:
            mock_svc.return_value.mode_label.return_value = "demo"
            send_positions_snapshot(
                fast=True, detail_level="full", tenant_id="default", scope="demo"
            )
        mock_btn.assert_not_called()
        self.assertTrue(mock_msg.called)

    def test_pos_more_callback_runs_positions_full(self):
        with patch.object(portfolio_commands, "handle", return_value=True) as handle, \
             patch.object(portfolio_commands, "answer_callback_query") as ans, \
             patch.object(portfolio_commands, "set_chat_id") as set_cid:
            ok = portfolio_commands.handle_callback({
                "id": "cq447",
                "data": "pos_more:full",
                "message": {"chat": {"id": 999}, "message_id": 4},
            })
        self.assertTrue(ok)
        ans.assert_called_once_with("cq447")
        set_cid.assert_called_once_with(999)
        handle.assert_called_once_with("/positions full")

    def test_pos_more_unknown_level_is_swallowed(self):
        with patch.object(portfolio_commands, "handle") as handle, \
             patch.object(portfolio_commands, "answer_callback_query"):
            self.assertTrue(portfolio_commands.handle_callback({
                "id": "cq",
                "data": "pos_more:nope",
            }))
        handle.assert_not_called()

    def test_router_dispatches_pos_more_callback(self):
        with patch(
            "notifications.telegram_commands.trading_commands.handle_callback",
            return_value=False,
        ), patch(
            "notifications.telegram_commands.portfolio_commands.handle_callback",
            return_value=True,
        ) as mock_pos, patch(
            "notifications.telegram_commands.order_commands.handle_callback",
        ) as mock_orders:
            self.assertTrue(router.dispatch_callback({
                "id": "1",
                "data": "pos_more:full",
                "message": {"chat": {"id": 999}},
            }))
        mock_pos.assert_called_once()
        mock_orders.assert_not_called()


class TestOrdersViewButtons(unittest.TestCase):
    def setUp(self):
        _pin_de()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.scope_patch = patch("data_manager.ORDERS_SCOPE_FILES", {
            "demo": os.path.join(self.tmp.name, "orders.demo.json"),
            "paper": os.path.join(self.tmp.name, "orders.paper.json"),
            "live": os.path.join(self.tmp.name, "orders.live.json"),
        })
        self.scope_patch.start()
        self.addCleanup(self.scope_patch.stop)
        self.scope = patch("services.order_service.resolve_tenant_scope", return_value="paper")
        self.scope.start()
        self.addCleanup(self.scope.stop)
        svc = OrderService("paper")
        svc.create_from_request(
            TradeOrder("SELL", "SOL/USDT", 70, 2, signal="SELL"),
            status="filled",
            telegram_token="t447",
        )
        svc.update_status("t447", "filled", execution={"usdt": 140, "price": 70, "amount": 2})

    def tearDown(self):
        from storage.order_ledger_v2 import reset_order_ledger_v2_for_tests

        reset_order_ledger_v2_for_tests()

    def test_filter_buttons_are_orders_view_not_pager(self):
        rows = order_commands._view_filter_buttons(order_commands.VIEW_DAY)
        datas = [b["callback_data"] for b in rows[0]]
        self.assertEqual(
            datas,
            ["orders_view:day", "orders_view:blocked", "orders_view:month"],
        )
        labels = [b["text"] for b in rows[0]]
        self.assertTrue(any("Alle" in x or "All" in x for x in labels))
        self.assertTrue(any("Blockiert" in x or "Blocked" in x for x in labels))
        self.assertTrue(any("Monat" in x or "month" in x.lower() for x in labels))
        self.assertFalse(any("orders_page:" in d for d in datas))

    def test_orders_keeps_detail_buttons_under_filters(self):
        with patch("notifications.telegram_commands.order_commands.send_telegram_buttons") as mock_btn:
            self.assertTrue(order_commands.handle("/orders"))
        buttons = mock_btn.call_args[0][1]
        datas = [b.get("callback_data") or "" for row in buttons for b in row]
        self.assertTrue(any(d.startswith("orders_view:") for d in datas))
        self.assertTrue(any(d.startswith("order_detail:") for d in datas))
        self.assertFalse(any("orders_page:" in d for d in datas))

    def test_orders_view_callback_switches_to_blocked(self):
        with patch("notifications.telegram_commands.order_commands.send_telegram_buttons") as mock_btn, \
             patch("notifications.telegram_commands.order_commands.send_telegram_message"), \
             patch("notifications.telegram_commands.order_commands.answer_callback_query"):
            self.assertTrue(order_commands.handle_callback({
                "id": "cb447",
                "data": "orders_view:blocked",
            }))
        msg = mock_btn.call_args[0][0] if mock_btn.called else ""
        self.assertIn("Blockierte", msg)

    def test_slash_aliases_still_handle(self):
        with patch("notifications.telegram_commands.order_commands.send_telegram_buttons"), \
             patch("notifications.telegram_commands.order_commands.send_telegram_message"):
            self.assertTrue(order_commands.handle("/orders_blocked"))
            self.assertTrue(order_commands.handle("/orders_month"))
        self.assertIn("/positions_full", portfolio_commands._FULL_COMMANDS)
        self.assertIn("/positions full", portfolio_commands._FULL_COMMANDS)


class TestSatelliteTypedAliasesGated(unittest.TestCase):
    def setUp(self):
        _pin_de()

    def test_satellite_typed_orders_blocked_is_denied(self):
        tok = _chat_id_var.set("999")
        try:
            with patch("core.tenant_context.multi_tenant_enabled", return_value=True), \
                 patch(
                     "storage.tenant_registry.find_tenant_by_owner_chat_id",
                     return_value={"tenant_id": "henry"},
                 ), \
                 patch.dict("os.environ", {"TELEGRAM_CHAT_ID": "12345"}, clear=False), \
                 patch("notifications.telegram_commands.router.send_telegram_message") as send, \
                 patch.object(order_commands, "handle") as handle:
                self.assertTrue(router.dispatch_command("/orders_blocked"))
            handle.assert_not_called()
            self.assertIn("Nur Operator", send.call_args[0][0])
        finally:
            _chat_id_var.reset(tok)


if __name__ == "__main__":
    unittest.main()
