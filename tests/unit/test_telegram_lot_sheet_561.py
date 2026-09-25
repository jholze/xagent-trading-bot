"""#561 — compact /positions coin tap opens a one-lot sheet."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from notifications.coin_links import gate_trade_url
from notifications.telegram_commands import command_context as ctx
from notifications.telegram_commands import portfolio_commands, router, trading_commands
from notifications.telegram_commands.menu_i18n import set_user_language
from notifications.telegram_commands.position_display import (
    LOT_BUYS_CALLBACK_PREFIX,
    LOT_SHEET_CALLBACK_PREFIX,
    encode_lot_callback,
    format_lot_buys_message,
    long_lots_for_sell,
    lot_sheet_keyboard,
    one_line_why_for_symbol,
    position_card_action_rows,
)
from notifications.telegram_i18n import reload_messages, t


CHAT = "561"


def _pin(lang: str) -> None:
    set_user_language(lang)
    reload_messages()


def _long(symbol: str, timeframe: str, amount: float, entry: float = 0.5) -> dict:
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "amount": amount,
        "average_entry": entry,
        "side": "long",
        "first_buy_at": "2026-09-01T12:30:00",
    }


def _short(symbol: str, timeframe: str, amount: float, entry: float = 0.5) -> dict:
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "amount": amount,
        "average_entry": entry,
        "side": "short",
        "leverage": 2,
        "first_buy_at": "2026-09-01T12:30:00",
    }


def _query(data: str, chat_id=CHAT, *, missing_chat: bool = False) -> dict:
    q = {"id": "cb-561", "data": data}
    if missing_chat:
        return q
    q["message"] = {"chat": {"id": chat_id}}
    return q


class TestTelegramLotSheet561(unittest.TestCase):
    def setUp(self):
        _pin("de")
        self.tmp = tempfile.TemporaryDirectory()
        ctx._CONTEXT_FILE = Path(self.tmp.name) / "ctx.json"
        ctx.set_chat_id(CHAT)
        self.rave_1h = _long("RAVE/USDT", "1h", 200.0, 0.5)
        self.rave_4h = _long("RAVE/USDT", "4h", 50.0, 0.4)
        self.aria = _long("ARIA/USDT", "4h", 10.0, 1.0)
        self.short_h = _short("H/USDT", "4h", 400.0, 0.05)

    def tearDown(self):
        ctx.clear_context(CHAT)
        ctx.set_chat_id("")
        self.tmp.cleanup()
        _pin("de")

    def _start_patches(self, lots, *, price=0.65, why_rationale="hold-the-line"):
        decisions = [{
            "symbol": "RAVE/USDT",
            "action": "HOLD",
            "normalized_action": "HOLD",
            "rationale": why_rationale,
            "timestamp": "2026-09-01T12:00:00",
        }]
        mocks = {
            "lots": patch(
                "strategies.positions.list_active_positions",
                return_value=lots,
            ),
            "prices": patch(
                "price_fetcher.get_prices_batch",
                return_value={"RAVE/USDT": price, "ARIA/USDT": 1.0, "H/USDT": 0.04},
            ),
            "why": patch(
                "notifications.telegram_commands.decisions_commands._load_decisions",
                return_value=decisions,
            ),
            "ack": patch(
                "notifications.telegram_commands.portfolio_commands.answer_callback_query",
            ),
            "buttons": patch(
                "notifications.telegram_commands.portfolio_commands.send_telegram_buttons",
                return_value=True,
            ),
            "msg": patch(
                "notifications.telegram_commands.portfolio_commands.send_telegram_message",
                return_value=True,
            ),
        }
        started = {name: p.start() for name, p in mocks.items()}
        for p in mocks.values():
            self.addCleanup(p.stop)
        return started

    def test_poslot_acks_then_one_lot_message(self):
        self._start_patches([self.rave_1h, self.aria])
        ok = portfolio_commands.handle_callback(
            _query(encode_lot_callback(LOT_SHEET_CALLBACK_PREFIX, "RAVE", "1h"))
        )
        self.assertTrue(ok)
        portfolio_commands.answer_callback_query.assert_called_once_with("cb-561")
        portfolio_commands.send_telegram_buttons.assert_called_once()
        msg = portfolio_commands.send_telegram_buttons.call_args[0][0]
        self.assertIn("RAVE", msg)
        self.assertIn("1h", msg)
        self.assertNotIn("ARIA", msg)
        self.assertIn("0.6500", msg)
        self.assertIn("$+30.0", msg)
        self.assertIn("09.01 12:30", msg)
        self.assertIn("0.5000", msg)
        self.assertIn("200.0000", msg)
        self.assertIn("hold-the-line", msg)
        self.assertNotIn("Letzte Entscheidung", msg)
        self.assertNotIn("/why", msg)
        self.assertEqual(portfolio_commands.send_telegram_message.call_count, 0)

    def test_sheet_buttons_kurs_kaeufe_lotsell_for_accepted_long(self):
        self._start_patches([self.rave_1h])
        self.assertTrue(long_lots_for_sell([self.rave_1h]))
        portfolio_commands.handle_callback(
            _query(encode_lot_callback(LOT_SHEET_CALLBACK_PREFIX, "RAVE", "1h"))
        )
        buttons = portfolio_commands.send_telegram_buttons.call_args[0][1]
        flat = [btn for row in buttons for btn in row]
        labels = [btn["text"] for btn in flat]
        self.assertIn(t("positions_btn_price"), labels)
        self.assertIn(t("positions_btn_buys"), labels)
        self.assertIn(t("positions_btn_sell"), labels)
        kurs = next(btn for btn in flat if btn["text"] == t("positions_btn_price"))
        self.assertEqual(kurs["url"], gate_trade_url("RAVE"))
        self.assertNotIn("callback_data", kurs)
        kaeufe = next(btn for btn in flat if btn["text"] == t("positions_btn_buys"))
        self.assertEqual(kaeufe["callback_data"], "posbuys:RAVE:1h")
        sell = next(btn for btn in flat if btn["text"] == t("positions_btn_sell"))
        self.assertEqual(sell["callback_data"], "lotsell:RAVE:1h")
        self.assertFalse(any(str(btn.get("callback_data") or "").startswith("poswhy:") for btn in flat))
        self.assertFalse(any("coverlot:" in str(btn.get("callback_data") or "") for btn in flat))

    def test_kaeufe_callback_is_this_lot_only(self):
        self._start_patches([self.rave_1h, self.rave_4h])

        def fake_tree(position, *, mark_price, **kwargs):
            return [f"tree-{position.get('timeframe')}"]

        with patch(
            "notifications.telegram_commands.position_ledger.build_position_trade_tree",
            side_effect=fake_tree,
        ) as mock_tree:
            self.assertTrue(portfolio_commands.handle_callback(
                _query(encode_lot_callback(LOT_BUYS_CALLBACK_PREFIX, "RAVE", "1h"))
            ))
            mock_tree.assert_called_once()
            called_pos = mock_tree.call_args[0][0]
            self.assertEqual(called_pos.get("timeframe"), "1h")
            self.assertIs(called_pos, self.rave_1h)
        body = portfolio_commands.send_telegram_message.call_args[0][0]
        self.assertIn("tree-1h", body)
        self.assertNotIn("tree-4h", body)
        self.assertIn("RAVE", body)
        self.assertIn("1h", body)

    def test_short_sheet_has_no_verkaufen_and_no_cover(self):
        self._start_patches([self.short_h], price=0.04)
        self.assertFalse(long_lots_for_sell([self.short_h]))
        portfolio_commands.handle_callback(
            _query(encode_lot_callback(LOT_SHEET_CALLBACK_PREFIX, "H", "4h"))
        )
        buttons = portfolio_commands.send_telegram_buttons.call_args[0][1]
        flat = [btn for row in buttons for btn in row]
        texts = [btn["text"] for btn in flat]
        callbacks = [str(btn.get("callback_data") or "") for btn in flat]
        self.assertNotIn(t("positions_btn_sell"), texts)
        self.assertNotIn("Verkaufen", texts)
        self.assertNotIn("Cover", texts)
        self.assertFalse(any(cb.startswith("lotsell:") for cb in callbacks))
        self.assertFalse(any(cb.startswith("coverlot:") for cb in callbacks))
        self.assertIn(t("positions_btn_price"), texts)
        self.assertIn(t("positions_btn_buys"), texts)

    def test_missing_chat_id_does_not_clear_context(self):
        ctx.set_context(CHAT, "buy", default_usdt=25)
        mocks = self._start_patches([self.rave_1h])
        with patch(
            "notifications.telegram_commands.command_context.clear_context",
        ) as mock_clear, patch("logger.log") as mock_log:
            self.assertTrue(portfolio_commands.handle_callback(
                _query(
                    encode_lot_callback(LOT_SHEET_CALLBACK_PREFIX, "RAVE", "1h"),
                    missing_chat=True,
                )
            ))
            mock_clear.assert_not_called()
            mock_log.assert_any_call("poslot callback missing chat id", "WARNING")
        portfolio_commands.answer_callback_query.assert_called_once_with("cb-561")
        portfolio_commands.send_telegram_buttons.assert_not_called()
        mocks["prices"].assert_not_called()
        self.assertEqual(ctx.get_context(CHAT)["command"], "buy")

    def test_two_timeframes_of_one_ticker_stay_distinct(self):
        rows = position_card_action_rows(
            [self.rave_1h, self.rave_4h],
            {"RAVE/USDT": 0.65},
        )
        callbacks = [btn["callback_data"] for row in rows for btn in row]
        labels = [btn["text"] for row in rows for btn in row]
        self.assertEqual(set(callbacks), {"poslot:RAVE:1h", "poslot:RAVE:4h"})
        self.assertEqual(set(labels), {"RAVE 1h", "RAVE 4h"})
        self.assertNotEqual(callbacks[0], callbacks[1])

        self._start_patches([self.rave_1h, self.rave_4h])
        self.assertTrue(portfolio_commands.handle_callback(
            _query("poslot:RAVE:1h")
        ))
        msg = portfolio_commands.send_telegram_buttons.call_args[0][0]
        self.assertIn("1h", msg)
        self.assertNotIn("4h", msg.split("\n")[0])

    def test_live_fetch_is_at_most_that_one_symbol(self):
        mocks = self._start_patches([self.rave_1h, self.aria, self.short_h])
        self.assertTrue(portfolio_commands.handle_callback(_query("poslot:RAVE:1h")))
        mocks["prices"].assert_called()
        args, kwargs = mocks["prices"].call_args
        symbols = list(args[0])
        self.assertEqual(symbols, ["RAVE/USDT"])
        self.assertEqual(len(symbols), 1)

    def test_de_and_en_button_labels(self):
        _pin("de")
        de = lot_sheet_keyboard(self.rave_1h)
        de_labels = [btn["text"] for row in de for btn in row]
        self.assertEqual(de_labels, ["Kurs", "Käufe", "Verkaufen"])
        _pin("en")
        en = lot_sheet_keyboard(self.rave_1h)
        en_labels = [btn["text"] for row in en for btn in row]
        self.assertEqual(en_labels, ["Price", "Buys", "Sell"])
        self.assertNotEqual(de_labels, en_labels)
        _pin("de")
        de_short = [btn["text"] for row in lot_sheet_keyboard(self.short_h) for btn in row]
        self.assertEqual(de_short, ["Kurs", "Käufe"])
        _pin("en")
        en_short = [btn["text"] for row in lot_sheet_keyboard(self.short_h) for btn in row]
        self.assertEqual(en_short, ["Price", "Buys"])

    def test_router_sends_poslot_to_portfolio_not_lotsell_or_poswhy(self):
        query = _query("poslot:RAVE:1h")
        with patch.object(
            portfolio_commands, "handle_callback", return_value=True,
        ) as mock_port, patch.object(
            trading_commands, "handle_callback", return_value=True,
        ) as mock_trade:
            self.assertTrue(router.dispatch_callback(query))
            mock_port.assert_called_once_with(query)
            mock_trade.assert_not_called()

    def test_one_line_why_loader_raise_is_load_failed_not_no_decision(self):
        err = RuntimeError("disk-full")
        with patch(
            "notifications.telegram_commands.decisions_commands._load_decisions",
            side_effect=err,
        ):
            with self.assertLogs(
                "notifications.telegram_commands.position_display",
                level="WARNING",
            ) as cm:
                text = one_line_why_for_symbol("RAVE/USDT")
        self.assertEqual(text, t("why_load_failed", error=err))
        self.assertNotEqual(text, t("why_no_decision"))
        self.assertNotIn(t("why_no_decision"), text)
        self.assertTrue(any("WARNING" in line for line in cm.output))
        self.assertTrue(any("RAVE/USDT" in line and "disk-full" in line for line in cm.output))

    def test_format_lot_buys_tree_raise_is_load_failed_not_empty(self):
        err = RuntimeError("tree-boom")
        with patch(
            "notifications.telegram_commands.position_ledger.build_position_trade_tree",
            side_effect=err,
        ):
            with self.assertLogs(
                "notifications.telegram_commands.position_display",
                level="WARNING",
            ) as cm:
                body = format_lot_buys_message(self.rave_1h, 0.65)
        self.assertIn(t("lot_sheet_buys_load_failed", error=err), body)
        self.assertNotIn(t("lot_sheet_buys_empty"), body)
        self.assertTrue(any("WARNING" in line for line in cm.output))
        self.assertTrue(
            any("RAVE" in line and "1h" in line and "tree-boom" in line for line in cm.output)
        )

    def test_format_lot_buys_empty_tree_is_empty_not_load_failed(self):
        with patch(
            "notifications.telegram_commands.position_ledger.build_position_trade_tree",
            return_value=[],
        ):
            body = format_lot_buys_message(self.rave_1h, 0.65)
        self.assertIn(t("lot_sheet_buys_empty"), body)
        self.assertNotIn(t("lot_sheet_buys_load_failed", error="tree-boom"), body)
        self.assertNotIn("konnten nicht geladen werden", body)
