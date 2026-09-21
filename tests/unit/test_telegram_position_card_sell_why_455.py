"""#455 — Verkaufen/Warum? on /positions and stop/fill, identity by symbol+tf."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from notifications.telegram_commands import command_context as ctx
from notifications.telegram_commands import trading_commands
from notifications.telegram_commands.position_display import (
    LOT_SELL_CALLBACK_PREFIX,
    LOT_WHY_CALLBACK_PREFIX,
    encode_lot_callback,
    parse_lot_callback,
    position_card_action_rows,
    resolve_position_by_symbol_tf,
    send_positions_snapshot,
)
from telegram_notifier import send_signal_message


def _pin_de():
    from notifications.telegram_commands.menu_i18n import set_user_language

    set_user_language("de")


def _long(symbol: str, timeframe: str, amount: float, entry: float = 1.0) -> dict:
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "amount": amount,
        "average_entry": entry,
        "side": "long",
    }


class TestLotCallbackIdentity(unittest.TestCase):
    def test_encode_parse_roundtrip_under_64_bytes(self):
        data = encode_lot_callback(LOT_SELL_CALLBACK_PREFIX, "RAVE/USDT", "1h")
        self.assertEqual(data, "lotsell:RAVE:1h")
        self.assertLessEqual(len(data.encode("utf-8")), 64)
        self.assertEqual(parse_lot_callback(data, LOT_SELL_CALLBACK_PREFIX), ("RAVE", "1h"))

    def test_legacy_index_payload_is_rejected(self):
        self.assertIsNone(parse_lot_callback("sellpos:1", "sellpos:"))

    def test_resolve_by_symbol_tf_ignores_value_sort(self):
        small = _long("SMALL/USDT", "1h", 10.0)
        big = _long("BIG/USDT", "4h", 100.0)
        prices_now = {"SMALL/USDT": 50.0, "BIG/USDT": 1.0}
        picked = resolve_position_by_symbol_tf([small, big], "SMALL", "1h")
        self.assertIs(picked, small)
        self.assertIs(
            resolve_position_by_symbol_tf([small, big], "BIG", "4h"),
            big,
        )
        self.assertIsNone(resolve_position_by_symbol_tf([small, big], "SMALL", "4h"))
        del prices_now


class TestPositionsCardButtons(unittest.TestCase):
    def setUp(self):
        _pin_de()

    def _snapshot_ctx(self, active):
        return {
            "history": {"virtual_balance": 5000, "trades": []},
            "cash_balance": 5000.0,
            "cash_label": "Cash",
            "gate_holdings": None,
            "active": active,
        }

    def test_compact_rows_are_ticker_tf_not_index(self):
        longs = [_long("RAVE/USDT", "1h", 20.0), _long("ARIA/USDT", "4h", 200.0)]
        prices = {"RAVE/USDT": 1.0, "ARIA/USDT": 1.0}
        rows = position_card_action_rows(longs, prices)
        callbacks = [btn["callback_data"] for row in rows for btn in row]
        self.assertEqual(
            callbacks,
            [
                "poswhy:ARIA:4h",
                "poswhy:RAVE:1h",
            ],
        )
        self.assertTrue(any("Warum?" in btn["text"] for row in rows for btn in row))
        self.assertFalse(any(cb.startswith("lotsell:") for cb in callbacks))
        self.assertTrue(all(":" in cb and not cb.endswith(":1") for cb in callbacks))

    def test_compact_snapshot_keeps_mehr_details_after_lot_rows(self):
        active = [_long("RAVE/USDT", "1h", 20.0)]
        prices = {"RAVE/USDT": 1.0}
        with patch("telegram_notifier.send_telegram_buttons", return_value=True) as mock_btn, \
             patch("telegram_notifier.send_telegram_message", return_value=True), \
             patch("price_fetcher.get_prices_batch", return_value=(prices, {})), \
             patch(
                 "notifications.telegram_commands.position_display.resolve_portfolio_context",
                 return_value=self._snapshot_ctx(active),
             ), \
             patch("services.trading_service.TradingService") as mock_svc:
            mock_svc.return_value.mode_label.return_value = "demo"
            send_positions_snapshot(
                fast=True, detail_level="compact", tenant_id="default", scope="demo"
            )
        mock_btn.assert_called_once()
        buttons = mock_btn.call_args[0][1]
        callbacks = [btn["callback_data"] for row in buttons for btn in row]
        self.assertFalse(any(cb.startswith("lotsell:") for cb in callbacks))
        self.assertEqual(buttons[0][0]["callback_data"], "poswhy:RAVE:1h")
        self.assertEqual(buttons[-1][0]["callback_data"], "pos_more:full")


class TestLotSellWhyCallbacks(unittest.TestCase):
    def setUp(self):
        _pin_de()
        self.tmp = tempfile.TemporaryDirectory()
        ctx._CONTEXT_FILE = Path(self.tmp.name) / "ctx.json"
        ctx.set_chat_id("99")
        self.rave = _long("RAVE/USDT", "1h", 200.0, 0.5)
        self.aria = _long("ARIA/USDT", "4h", 10.0, 1.0)
        self.patches = [
            patch(
                "notifications.telegram_commands.trading_commands.list_active_positions",
                return_value=[self.rave, self.aria],
            ),
            patch(
                "notifications.telegram_commands.trading_commands.get_prices_batch",
                return_value={"RAVE/USDT": 0.65, "ARIA/USDT": 50.0},
            ),
            patch(
                "notifications.telegram_commands.trading_commands.get_prices",
                return_value=(0.65, 0.65, None),
            ),
            patch(
                "notifications.telegram_commands.trading_commands.get_position",
                return_value={"amount": 200.0},
            ),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        ctx.clear_context("99")
        ctx.set_chat_id("")
        self.tmp.cleanup()

    def _query(self, data: str):
        return {
            "id": "cb-455",
            "data": data,
            "message": {"chat": {"id": "99"}},
        }

    def test_lotsell_without_sell_context_starts_pct_wizard_for_that_lot(self):
        ctx.clear_context("99")
        with patch(
            "notifications.telegram_commands.trading_commands.prompt_sell_percentage",
        ) as mock_prompt, patch(
            "notifications.telegram_commands.trading_commands.request_sell_confirmation",
        ) as mock_confirm, patch(
            "notifications.telegram_commands.trading_commands.send_telegram_buttons",
        ), patch(
            "notifications.telegram_commands.trading_commands.answer_callback_query",
        ):
            self.assertTrue(
                trading_commands.handle_callback(self._query("lotsell:RAVE:1h"))
            )
            mock_confirm.assert_not_called()
            mock_prompt.assert_called_once()
            self.assertEqual(mock_prompt.call_args[0][0], "RAVE")
        entry = ctx.get_context("99")
        self.assertEqual(entry["meta"]["state"], "sell_awaiting_pct")
        self.assertEqual(entry["meta"]["position"], "RAVE")
        self.assertEqual(entry["meta"]["timeframe"], "1h")

    def test_lotsell_survives_price_resort(self):
        """ARIA is now more valuable; tapping RAVE must not open ARIA."""
        ctx.clear_context("99")
        with patch(
            "notifications.telegram_commands.trading_commands.prompt_sell_percentage",
        ) as mock_prompt, patch(
            "notifications.telegram_commands.trading_commands.request_sell_confirmation",
        ), patch(
            "notifications.telegram_commands.trading_commands.send_telegram_buttons",
        ), patch(
            "notifications.telegram_commands.trading_commands.answer_callback_query",
        ):
            self.assertTrue(
                trading_commands.handle_callback(self._query("lotsell:RAVE:1h"))
            )
            self.assertEqual(mock_prompt.call_args[0][0], "RAVE")
        self.assertEqual(ctx.get_context("99")["meta"]["position"], "RAVE")
        self.assertNotEqual(ctx.get_context("99")["meta"]["position"], "ARIA")

    def test_poswhy_dispatches_why_symbol(self):
        with patch(
            "notifications.telegram_commands.trading_commands.answer_callback_query",
        ), patch(
            "notifications.telegram_commands.router.dispatch_command",
            return_value=True,
        ) as dispatch:
            self.assertTrue(
                trading_commands.handle_callback(self._query("poswhy:RAVE:1h"))
            )
            dispatch.assert_called_once_with("/why RAVE")


class TestSignalStopFillButtons(unittest.TestCase):
    def setUp(self):
        _pin_de()

    def _send(self, *, real_live: bool):
        coin = {"symbol": "RAVE/USDT", "name": "RaveDAO"}
        with patch("data_manager.is_demo_mode", return_value=not real_live), \
             patch("core.simulated_trading.is_real_live_trading", return_value=real_live), \
             patch("notifications.chart_image.send_trade_chart_if_enabled", return_value=False), \
             patch("telegram_notifier.send_telegram_message", return_value=True) as mock_send:
            send_signal_message(
                "SELL_STOP_FULL",
                coin,
                0.65,
                72.0,
                0.60,
                0.8,
                "🔴",
                "Bearish",
                executed=True,
                timeframe="1h",
            )
        return mock_send

    def test_paper_primary_actions_are_why_and_sell_not_gate(self):
        mock_send = self._send(real_live=False)
        markup = mock_send.call_args.kwargs.get("reply_markup")
        self.assertIsNotNone(markup)
        rows = markup["inline_keyboard"]
        first = rows[0]
        self.assertEqual(
            [btn["callback_data"] for btn in first],
            ["poswhy:RAVE:1h", "lotsell:RAVE:1h"],
        )
        self.assertIn("Warum?", first[0]["text"])
        self.assertIn("Verkaufen", first[1]["text"])
        self.assertNotIn("url", first[0])
        self.assertNotIn("url", first[1])
        url_buttons = [btn for row in rows[1:] for btn in row]
        self.assertTrue(any(btn.get("text") == "Chart" for btn in url_buttons))
        self.assertFalse(
            any("gate.io" in str(btn.get("url") or "").lower() for btn in url_buttons)
        )
        self.assertFalse(
            any("gate.io" in str(btn.get("url") or "").lower() for btn in first)
        )

    def test_live_keeps_gate_on_secondary_row(self):
        mock_send = self._send(real_live=True)
        rows = mock_send.call_args.kwargs["reply_markup"]["inline_keyboard"]
        self.assertEqual(rows[0][0]["callback_data"], "poswhy:RAVE:1h")
        self.assertEqual(rows[0][1]["callback_data"], "lotsell:RAVE:1h")
        url_buttons = [btn for row in rows[1:] for btn in row]
        self.assertTrue(any("gate.io" in str(btn.get("url") or "").lower() for btn in url_buttons))
        self.assertTrue(any(btn.get("text") == "Chart" for btn in url_buttons))


if __name__ == "__main__":
    unittest.main()
