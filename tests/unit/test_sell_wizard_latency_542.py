"""#542 — sell-wizard taps ack and reply before Gate/price/risk work."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from notifications.telegram_commands import command_context as ctx
from notifications.telegram_commands import trading_commands


def _pin_de():
    from notifications.telegram_commands.menu_i18n import set_user_language

    set_user_language("de")


class TestSellWizardLatency542(unittest.TestCase):
    def setUp(self):
        _pin_de()
        self.tmp = tempfile.TemporaryDirectory()
        ctx._CONTEXT_FILE = Path(self.tmp.name) / "ctx.json"
        ctx.set_chat_id("99")
        self.position = {
            "symbol": "RAVE/USDT",
            "timeframe": "1h",
            "amount": 200.0,
            "average_entry": 0.5,
        }
        self.patches = [
            patch(
                "notifications.telegram_commands.trading_commands.list_active_positions",
                return_value=[self.position],
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

    def _query(self, data: str, *, chat_id="99", missing_chat: bool = False):
        message = {} if missing_chat else {"chat": {"id": chat_id}}
        return {"id": "cb-542", "data": data, "message": message}

    def _slow_batch(self, symbols, **_kwargs):
        time.sleep(2.1)
        out = {sym: 0.65 for sym in (symbols or ["RAVE/USDT"])}
        return out

    def test_sellpct_sends_chat_message_before_slow_get_prices_batch(self):
        ctx.set_context(
            "99", "sell",
            state="sell_awaiting_pct",
            position="RAVE",
            label="RAVE",
            timeframe="1h",
        )
        sent_at = []
        order = []

        def _record_send(text, **_kwargs):
            order.append("send")
            sent_at.append((time.perf_counter(), str(text)))
            return True

        def _slow_then_prices(symbols, **_kwargs):
            order.append("batch")
            return self._slow_batch(symbols)

        t0 = time.perf_counter()
        with patch(
            "notifications.telegram_commands.trading_commands.get_prices_batch",
            side_effect=_slow_then_prices,
        ), patch(
            "notifications.telegram_commands.trading_commands.get_prices",
            side_effect=lambda *_a, **_k: (time.sleep(2.1), (0.65, 0.65, None))[1],
        ), patch(
            "notifications.telegram_commands.trading_commands.send_telegram_message",
            side_effect=_record_send,
        ), patch(
            "notifications.telegram_commands.trading_commands.send_telegram_buttons",
            return_value=True,
        ), patch(
            "notifications.telegram_commands.trading_commands.request_sell_confirmation",
        ) as mock_confirm, patch(
            "notifications.telegram_commands.trading_commands.answer_callback_query",
        ), patch(
            "notifications.telegram_commands.router.dispatch_command",
        ) as dispatch:
            self.assertTrue(
                trading_commands.handle_callback(self._query("sellpct:50"))
            )
            dispatch.assert_not_called()
            mock_confirm.assert_called_once()
        self.assertTrue(sent_at)
        self.assertEqual(order[0], "send")
        self.assertIn("batch", order)
        first_ms = (sent_at[0][0] - t0) * 1000.0
        self.assertLess(first_ms, 2000.0)
        self.assertIn("RAVE", sent_at[0][1])
        self.assertIn("50", sent_at[0][1])

    def test_sellpct_does_not_redispatch_typed_sell_index(self):
        ctx.set_context(
            "99", "sell",
            state="sell_awaiting_pct",
            position="1",
            label="RAVE",
            timeframe="1h",
        )
        with patch(
            "notifications.telegram_commands.trading_commands.get_prices_batch",
            return_value={"RAVE/USDT": 0.65},
        ), patch(
            "notifications.telegram_commands.trading_commands.get_prices",
            return_value=(0.65, 0.65, None),
        ), patch(
            "notifications.telegram_commands.trading_commands.send_telegram_message",
            return_value=True,
        ), patch(
            "notifications.telegram_commands.trading_commands.request_sell_confirmation",
        ) as mock_confirm, patch(
            "notifications.telegram_commands.trading_commands.answer_callback_query",
        ), patch(
            "notifications.telegram_commands.router.dispatch_command",
        ) as dispatch:
            self.assertTrue(
                trading_commands.handle_callback(self._query("sellpct:25"))
            )
            dispatch.assert_not_called()
            mock_confirm.assert_called_once()
            self.assertEqual(mock_confirm.call_args.kwargs["symbol"], "RAVE/USDT")
            self.assertEqual(mock_confirm.call_args.kwargs["timeframe"], "1h")

    def test_sellpct_missing_chat_id_fail_closed(self):
        ctx.set_context(
            "99", "sell",
            state="sell_awaiting_pct",
            position="RAVE",
            label="RAVE",
            timeframe="1h",
        )
        with patch(
            "notifications.telegram_commands.trading_commands.request_sell_confirmation",
        ) as mock_confirm, patch(
            "notifications.telegram_commands.trading_commands.send_telegram_message",
        ) as mock_send, patch(
            "notifications.telegram_commands.trading_commands.answer_callback_query",
        ), patch(
            "notifications.telegram_commands.router.dispatch_command",
        ) as dispatch, patch(
            "notifications.telegram_commands.trading_commands.get_prices_batch",
        ) as batch, patch(
            "logger.log",
        ) as mock_log:
            self.assertTrue(
                trading_commands.handle_callback(
                    self._query("sellpct:50", missing_chat=True)
                )
            )
            mock_confirm.assert_not_called()
            mock_send.assert_not_called()
            dispatch.assert_not_called()
            batch.assert_not_called()
            mock_log.assert_any_call("sellpct callback missing chat id", "WARNING")
        entry = ctx.get_context("99")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["meta"]["state"], "sell_awaiting_pct")

    def test_sellpos_pct_prompt_before_slow_get_prices_batch(self):
        ctx.set_context("99", "sell", state="sell_awaiting_position")
        sent_at = []

        def _record_buttons(text, _buttons, **_kwargs):
            sent_at.append(time.perf_counter())
            return True

        t0 = time.perf_counter()
        with patch(
            "notifications.telegram_commands.trading_commands.get_prices_batch",
            side_effect=self._slow_batch,
        ) as batch, patch(
            "notifications.telegram_commands.trading_commands.get_prices",
            side_effect=lambda *_a, **_k: (time.sleep(2.1), (0.65, 0.65, None))[1],
        ), patch(
            "notifications.telegram_commands.trading_commands.send_telegram_buttons",
            side_effect=_record_buttons,
        ), patch(
            "notifications.telegram_commands.trading_commands.send_telegram_message",
            return_value=True,
        ), patch(
            "notifications.telegram_commands.trading_commands.request_sell_confirmation",
        ) as mock_confirm, patch(
            "notifications.telegram_commands.trading_commands.answer_callback_query",
        ):
            self.assertTrue(
                trading_commands.handle_callback(self._query("sellpos:RAVE:1h"))
            )
            mock_confirm.assert_not_called()
            batch.assert_not_called()
        self.assertTrue(sent_at)
        first_ms = (sent_at[0] - t0) * 1000.0
        self.assertLess(first_ms, 2000.0)
        entry = ctx.get_context("99")
        self.assertEqual(entry["meta"]["state"], "sell_awaiting_pct")
        self.assertEqual(entry["meta"]["position"], "RAVE")
        self.assertEqual(entry["meta"]["timeframe"], "1h")

    def test_sellpct_missed_tf_does_not_confirm_other_lot_of_same_ticker(self):
        """% tap is symbol+tf. Missing 1h must not pick open RAVE/4h by value sort."""
        from notifications.telegram_i18n import t

        lot_4h = {
            "symbol": "RAVE/USDT",
            "timeframe": "4h",
            "amount": 800.0,
            "average_entry": 0.4,
        }
        ctx.set_context(
            "99", "sell",
            state="sell_awaiting_pct",
            position="RAVE",
            label="RAVE",
            timeframe="1h",
        )
        sent = []

        def _record_send(text, **_kwargs):
            sent.append(str(text))
            return True

        with patch(
            "notifications.telegram_commands.trading_commands.list_active_positions",
            return_value=[lot_4h],
        ), patch(
            "notifications.telegram_commands.trading_commands.get_prices_batch",
            return_value={"RAVE/USDT": 0.65},
        ), patch(
            "notifications.telegram_commands.trading_commands.send_telegram_message",
            side_effect=_record_send,
        ), patch(
            "notifications.telegram_commands.trading_commands.request_sell_confirmation",
        ) as mock_confirm, patch(
            "notifications.telegram_commands.trading_commands.resolve_position_by_symbol",
        ) as mock_by_symbol, patch(
            "notifications.telegram_commands.trading_commands.answer_callback_query",
        ), patch(
            "notifications.telegram_commands.router.dispatch_command",
        ) as dispatch:
            self.assertTrue(
                trading_commands.handle_callback(self._query("sellpct:50"))
            )
            dispatch.assert_not_called()
            mock_confirm.assert_not_called()
            mock_by_symbol.assert_not_called()
        expected = t("no_open_position", arg="RAVE")
        self.assertTrue(any(expected in msg for msg in sent), sent)


if __name__ == "__main__":
    unittest.main()
