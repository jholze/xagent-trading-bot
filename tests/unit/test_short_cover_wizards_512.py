"""#512 — tap-first /short /cover wizards (sell-wizard shape).

Nothing here writes into ``data/``: command context lives in a tmp file.
Frozen short/cover confirm assertions in ``test_short_cover_confirm.py``
stay in that file.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from notifications.telegram_commands import command_context as ctx
from notifications.telegram_commands.menu_i18n import reload_menu_data, set_user_language
from notifications.telegram_commands.router import dispatch_callback
from notifications.telegram_commands.short_commands import (
    COVER_LOT_CALLBACK_PREFIX,
    COVER_PCT_CALLBACK_PREFIX,
    SHORT_AMT_CALLBACK_PREFIX,
    SHORT_BACK_CALLBACK,
    SHORT_COIN_CALLBACK_PREFIX,
    handle,
    handle_callback,
)
from notifications.telegram_i18n import reload_messages
from tests.support.telegram_capture import texts

SC = "notifications.telegram_commands.short_commands"
CHAT = "512"
OPERATOR = "100"


def _coin(symbol="ARIA/USDT", name="Aria"):
    return {"symbol": symbol, "name": name, "active": True}


def _short_pos(symbol="H/USDT", timeframe="4h", amount=400.0, entry=0.05, lev=2.0):
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "amount": amount,
        "average_entry": entry,
        "side": "short",
        "leverage": lev,
    }


def _query(data: str, chat_id=CHAT, cid="cb", *, missing_chat: bool = False) -> dict:
    q = {"id": cid, "data": data}
    if missing_chat:
        return q
    q["message"] = {"chat": {"id": chat_id}}
    return q


def _buttons(tg):
    return [item for item in tg if item.get("kind") == "buttons"]


def _callbacks(tg) -> list[str]:
    out = []
    for item in tg:
        if item.get("kind") == "buttons":
            for row in item.get("buttons") or []:
                for btn in row:
                    out.append(btn.get("callback_data") or "")
        markup = item.get("reply_markup") or {}
        for row in markup.get("inline_keyboard") or []:
            for btn in row:
                out.append(btn.get("callback_data") or "")
    return out


class TestShortCoverWizards512(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        ctx._CONTEXT_FILE = Path(self.tmp.name) / "ctx.json"
        ctx.set_chat_id(CHAT)
        set_user_language("de")
        reload_messages()
        reload_menu_data()
        self.cfg = MagicMock()
        self.cfg.max_usdt_per_trade = 25
        self.cfg.raw = {"shorts": {"enabled": True}}
        self.coins = [_coin(), _coin("SOL/USDT", "Solana")]

    def tearDown(self):
        ctx.clear_context(CHAT)
        ctx.clear_context(OPERATOR)
        ctx.set_chat_id("")
        self.tmp.cleanup()

    def _install_capture(self, tg: list):
        from notifications.telegram_commands import command_context as cmdctx
        from notifications.telegram_commands import short_commands as scmod

        def _msg(text, reply_markup=None, **kwargs):
            tg.append({
                "kind": "message",
                "text": "" if text is None else str(text),
                "reply_markup": reply_markup,
                **kwargs,
            })
            return True

        def _btns(text, buttons, **kwargs):
            tg.append({
                "kind": "buttons",
                "text": "" if text is None else str(text),
                "buttons": buttons,
                **kwargs,
            })
            return True

        for mod, attr, fn in (
            (scmod, "send_telegram_message", _msg),
            (scmod, "send_telegram_buttons", _btns),
            (cmdctx, "send_telegram_message", _msg),
        ):
            p = patch.object(mod, attr, fn)
            p.start()
            self.addCleanup(p.stop)
        return tg

    def _patch_short_stack(self, *, coins=None, shorts=None, prices=None):
        coins = coins if coins is not None else self.coins
        shorts = shorts if shorts is not None else []
        prices = prices or {c["symbol"]: 1.0 for c in coins}
        for s in shorts:
            prices.setdefault(s["symbol"], 0.04)

        def _get_pos(sym, tf="4h"):
            for p in shorts:
                if p["symbol"] == sym and str(p.get("timeframe") or "4h") == str(tf):
                    return p
            return shorts[0] if shorts else {}

        patches = [
            patch(f"{SC}.get_bot_config", return_value=self.cfg),
            patch(f"{SC}.shorts_enabled", return_value=True),
            patch(f"{SC}.list_coins", return_value=list(coins)),
            patch(f"{SC}.list_active_positions", return_value=list(shorts)),
            patch(f"{SC}.get_position", _get_pos),
            patch(f"{SC}.get_prices_batch", lambda symbols: {
                s: prices.get(s, 1.0) for s in (symbols or [])
            }),
            patch(f"{SC}.answer_callback_query", lambda *a, **k: True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_bare_short_taps_watchlist_and_does_not_confirm(self):
        tg = []
        self._install_capture(tg)
        self._patch_short_stack()
        with patch(f"{SC}.request_short_confirmation") as req:
            self.assertTrue(handle("/short"))
            req.assert_not_called()
        callbacks = _callbacks(tg)
        self.assertEqual(
            [c for c in callbacks if c != ctx.CANCEL_CALLBACK],
            ["shortcoin:1", "shortcoin:2"],
        )
        self.assertIn(ctx.CANCEL_CALLBACK, callbacks)
        self.assertTrue(all(
            not c.startswith("sellpos:") and not c.startswith("lotsell:")
            for c in callbacks
        ))
        self.assertIn("bestätigt", "\n".join(texts(tg)))
        entry = ctx.get_context(CHAT)
        self.assertEqual(entry["command"], "short")
        self.assertEqual(entry["meta"]["state"], "short_awaiting_coin")

    def test_short_happy_path_never_types_size_or_leverage(self):
        tg = []
        self._install_capture(tg)
        self._patch_short_stack(prices={"ARIA/USDT": 0.05, "SOL/USDT": 145.0})
        with patch(f"{SC}.request_short_confirmation") as req:
            self.assertTrue(handle("/short"))
            req.assert_not_called()
            tg.clear()
            self.assertTrue(handle_callback(_query(f"{SHORT_COIN_CALLBACK_PREFIX}1")))
            req.assert_not_called()
            callbacks = _callbacks(tg)
            self.assertIn(f"{SHORT_AMT_CALLBACK_PREFIX}25", callbacks)
            self.assertIn(SHORT_BACK_CALLBACK, callbacks)
            self.assertEqual(ctx.get_context(CHAT)["meta"]["state"], "short_awaiting_usdt")
            tg.clear()
            self.assertTrue(handle_callback(_query(f"{SHORT_AMT_CALLBACK_PREFIX}25")))
            req.assert_called_once()
            kw = req.call_args.kwargs
            self.assertEqual(kw["symbol"], "ARIA/USDT")
            self.assertAlmostEqual(kw["usdt"], 25.0)
            self.assertIsNone(kw["leverage"])
        self.assertIsNone(ctx.get_context(CHAT))

    def test_typed_short_symbol_still_uses_existing_confirm(self):
        tg = []
        self._install_capture(tg)
        self._patch_short_stack(prices={"H/USDT": 0.05})
        with patch(f"{SC}.request_short_confirmation") as req:
            self.assertTrue(handle("/short H"))
            req.assert_called_once()
            self.assertEqual(req.call_args.kwargs["symbol"], "H/USDT")
            self.assertIsNone(req.call_args.kwargs["usdt"])
        self.assertIsNone(ctx.get_context(CHAT))

    def test_short_size_preset_includes_max_usdt_per_trade(self):
        tg = []
        self._install_capture(tg)
        self.cfg.max_usdt_per_trade = 40
        self._patch_short_stack()
        with patch(f"{SC}.request_short_confirmation"):
            self.assertTrue(handle("/short"))
            self.assertTrue(handle_callback(_query(f"{SHORT_COIN_CALLBACK_PREFIX}1")))
        callbacks = _callbacks(tg)
        self.assertIn(f"{SHORT_AMT_CALLBACK_PREFIX}40", callbacks)
        self.assertNotIn(f"{SHORT_AMT_CALLBACK_PREFIX}50", callbacks)

    def test_short_back_returns_to_coin_picker(self):
        tg = []
        self._install_capture(tg)
        self._patch_short_stack()
        with patch(f"{SC}.request_short_confirmation") as req:
            self.assertTrue(handle("/short"))
            self.assertTrue(handle_callback(_query(f"{SHORT_COIN_CALLBACK_PREFIX}1")))
            tg.clear()
            self.assertTrue(handle_callback(_query(SHORT_BACK_CALLBACK)))
            req.assert_not_called()
        self.assertEqual(_callbacks(tg)[:2], ["shortcoin:1", "shortcoin:2"])
        self.assertEqual(ctx.get_context(CHAT)["meta"]["state"], "short_awaiting_coin")

    def test_shortcoin_missing_chat_id_fails_closed(self):
        tg = []
        self._install_capture(tg)
        self._patch_short_stack()
        ctx.set_context(OPERATOR, "buy", default_usdt=25)
        ctx.set_context(CHAT, "short", state="short_awaiting_coin")
        with patch.dict(os.environ, {"TELEGRAM_CHAT_ID": OPERATOR}, clear=False), \
             patch(f"{SC}.request_short_confirmation") as req, \
             patch("logger.log") as mock_log:
            self.assertTrue(handle_callback(
                _query(f"{SHORT_COIN_CALLBACK_PREFIX}1", missing_chat=True)
            ))
            req.assert_not_called()
            mock_log.assert_any_call("shortcoin callback missing chat id", "WARNING")
        self.assertEqual(ctx.get_context(OPERATOR)["command"], "buy")
        self.assertEqual(ctx.get_context(CHAT)["command"], "short")

    def test_bare_cover_taps_symbol_timeframe_not_display_index(self):
        tg = []
        self._install_capture(tg)
        h_1h = _short_pos("H/USDT", "1h")
        h_4h = _short_pos("H/USDT", "4h")
        rave = _short_pos("RAVE/USDT", "1h")
        self._patch_short_stack(shorts=[h_1h, h_4h, rave])
        with patch(f"{SC}.request_cover_confirmation") as req:
            self.assertTrue(handle("/cover"))
            req.assert_not_called()
        callbacks = _callbacks(tg)
        self.assertEqual(
            [c for c in callbacks if c != ctx.CANCEL_CALLBACK],
            ["coverlot:H:1h", "coverlot:H:4h", "coverlot:RAVE:1h"],
        )
        self.assertIn(ctx.CANCEL_CALLBACK, callbacks)
        self.assertTrue(all(
            not c.startswith("sellpos:") and not c.startswith("lotsell:")
            for c in callbacks
        ))
        entry = ctx.get_context(CHAT)
        self.assertEqual(entry["command"], "cover")
        self.assertEqual(entry["meta"]["state"], "cover_awaiting_lot")

    def test_cover_happy_path_never_types_percent(self):
        tg = []
        self._install_capture(tg)
        self._patch_short_stack(shorts=[_short_pos()], prices={"H/USDT": 0.04})
        with patch(f"{SC}.request_cover_confirmation") as req:
            self.assertTrue(handle("/cover"))
            req.assert_not_called()
            tg.clear()
            self.assertTrue(handle_callback(_query(f"{COVER_LOT_CALLBACK_PREFIX}H:4h")))
            req.assert_not_called()
            self.assertEqual(
                [c for c in _callbacks(tg) if c.startswith("coverpct:") or c == "coverback"],
                [
                    f"{COVER_PCT_CALLBACK_PREFIX}25",
                    f"{COVER_PCT_CALLBACK_PREFIX}50",
                    f"{COVER_PCT_CALLBACK_PREFIX}75",
                    f"{COVER_PCT_CALLBACK_PREFIX}100",
                    "coverback",
                ],
            )
            self.assertEqual(ctx.get_context(CHAT)["meta"]["state"], "cover_awaiting_pct")
            tg.clear()
            self.assertTrue(handle_callback(_query(f"{COVER_PCT_CALLBACK_PREFIX}50")))
            req.assert_called_once()
            kw = req.call_args.kwargs
            self.assertEqual(kw["symbol"], "H/USDT")
            self.assertEqual(kw["timeframe"], "4h")
            self.assertAlmostEqual(kw["pct"], 0.5)
            self.assertAlmostEqual(kw["amount"], 200.0)
        self.assertIsNone(ctx.get_context(CHAT))

    def test_cover_lot_callback_keeps_timeframe_when_same_ticker_exists(self):
        tg = []
        self._install_capture(tg)
        lots = [_short_pos("H/USDT", "1h", amount=100.0), _short_pos("H/USDT", "4h", amount=400.0)]
        self._patch_short_stack(shorts=lots, prices={"H/USDT": 0.04})
        with patch(f"{SC}.request_cover_confirmation") as req:
            self.assertTrue(handle("/cover"))
            self.assertTrue(handle_callback(_query(f"{COVER_LOT_CALLBACK_PREFIX}H:1h")))
            self.assertTrue(handle_callback(_query(f"{COVER_PCT_CALLBACK_PREFIX}100")))
            req.assert_called_once()
            self.assertEqual(req.call_args.kwargs["timeframe"], "1h")
            self.assertAlmostEqual(req.call_args.kwargs["amount"], 100.0)

    def test_cover_empty_state(self):
        tg = []
        self._install_capture(tg)
        self._patch_short_stack(shorts=[])
        with patch(f"{SC}.request_cover_confirmation") as req:
            self.assertTrue(handle("/cover"))
            req.assert_not_called()
        self.assertFalse(_buttons(tg))
        self.assertIn("kein offener short", "\n".join(texts(tg)).lower())
        self.assertIsNone(ctx.get_context(CHAT))

    def test_coverlot_missing_chat_id_fails_closed(self):
        tg = []
        self._install_capture(tg)
        self._patch_short_stack(shorts=[_short_pos()])
        ctx.set_context(OPERATOR, "buy", default_usdt=25)
        ctx.set_context(CHAT, "cover", state="cover_awaiting_lot")
        with patch.dict(os.environ, {"TELEGRAM_CHAT_ID": OPERATOR}, clear=False), \
             patch(f"{SC}.request_cover_confirmation") as req, \
             patch("logger.log") as mock_log:
            self.assertTrue(handle_callback(
                _query(f"{COVER_LOT_CALLBACK_PREFIX}H:4h", missing_chat=True)
            ))
            req.assert_not_called()
            mock_log.assert_any_call("coverlot callback missing chat id", "WARNING")
        self.assertEqual(ctx.get_context(OPERATOR)["command"], "buy")
        self.assertEqual(ctx.get_context(CHAT)["command"], "cover")

    def test_short_cancel_chrome_clears_without_confirm(self):
        tg = []
        self._install_capture(tg)
        self._patch_short_stack()
        self.assertTrue(handle("/short"))
        self.assertIsNotNone(ctx.get_context(CHAT))
        with patch(f"{SC}.request_short_confirmation") as req, \
             patch("telegram_notifier.answer_callback_query"):
            self.assertTrue(dispatch_callback(_query(ctx.CANCEL_CALLBACK)))
            req.assert_not_called()
        self.assertIsNone(ctx.get_context(CHAT))

    def test_cancel_callback_missing_chat_id_does_not_pop_short_wizard(self):
        ctx.set_chat_id("")
        ctx.set_context(OPERATOR, "short", state="short_awaiting_coin")
        with patch.dict(os.environ, {"TELEGRAM_CHAT_ID": OPERATOR}, clear=False), \
             patch("telegram_notifier.answer_callback_query"), \
             patch("notifications.telegram_commands.command_context.send_telegram_message") as send, \
             patch("logger.log") as mock_log:
            self.assertTrue(ctx.handle_callback(_query(ctx.CANCEL_CALLBACK, missing_chat=True)))
            send.assert_not_called()
            mock_log.assert_any_call("cmdctx cancel callback missing chat id", "WARNING")
        self.assertEqual(ctx.get_context(OPERATOR)["command"], "short")

    def test_typed_amount_and_quick_button_produce_same_short_confirmation(self):
        self._patch_short_stack(prices={"ARIA/USDT": 0.05})

        def _run_typed():
            ctx.set_context(CHAT, "short", state="short_awaiting_usdt", coin="ARIA", label="ARIA")
            with patch(f"{SC}.request_short_confirmation") as mock_confirm, \
                 patch(f"{SC}.send_telegram_buttons"):
                self.assertTrue(ctx.try_resolve(CHAT, "25"))
                mock_confirm.assert_called_once()
                return mock_confirm.call_args.kwargs

        def _run_button():
            ctx.set_context(CHAT, "short", state="short_awaiting_usdt", coin="ARIA", label="ARIA")
            with patch(f"{SC}.request_short_confirmation") as mock_confirm, \
                 patch(f"{SC}.send_telegram_buttons"), \
                 patch(f"{SC}.answer_callback_query"):
                self.assertTrue(handle_callback(_query(f"{SHORT_AMT_CALLBACK_PREFIX}25")))
                mock_confirm.assert_called_once()
                return mock_confirm.call_args.kwargs

        typed_kwargs = _run_typed()
        button_kwargs = _run_button()
        self.assertEqual(typed_kwargs["symbol"], button_kwargs["symbol"])
        self.assertAlmostEqual(typed_kwargs["usdt"], 25)
        self.assertAlmostEqual(button_kwargs["usdt"], 25)

    def test_invalid_short_amount_reasks_instead_of_defaulting(self):
        self._patch_short_stack()
        ctx.set_context(CHAT, "short", state="short_awaiting_usdt", coin="ARIA", label="ARIA")
        with patch(f"{SC}.send_telegram_buttons") as mock_btn, \
             patch(f"{SC}.request_short_confirmation") as mock_confirm:
            self.assertTrue(ctx.try_resolve(CHAT, "nope"))
            mock_confirm.assert_not_called()
            self.assertTrue(mock_btn.called)
        entry = ctx.get_context(CHAT)
        self.assertEqual(entry["meta"]["state"], "short_awaiting_usdt")
        self.assertEqual(entry["meta"]["coin"], "ARIA")

    def test_short_cover_build_command_unstated_meta_unchanged(self):
        self.assertEqual(ctx._build_command("short", "H", {}), "/short H")
        self.assertEqual(ctx._build_command("cover", "H", {}), "/cover H")
        self.assertEqual(
            ctx._build_command(
                "short",
                "25",
                {"state": "short_awaiting_usdt", "coin": "ARIA"},
            ),
            "/short ARIA 25",
        )
        self.assertEqual(
            ctx._build_command(
                "cover",
                "50",
                {"state": "cover_awaiting_pct", "position": "H:4h"},
            ),
            "/cover H:4h 50",
        )

    def test_handle_callback_does_not_steal_sell_or_lock_prefixes(self):
        self.assertFalse(handle_callback(_query("sellpos:H:4h")))
        self.assertFalse(handle_callback(_query("lotsell:H:4h")))
        self.assertFalse(handle_callback(_query("lockpos:H:4h")))
        self.assertFalse(handle_callback(_query("buycoin:1")))


if __name__ == "__main__":
    unittest.main()
