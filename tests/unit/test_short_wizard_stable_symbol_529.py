"""#529 — /short size tap keeps the tapped symbol.

Nothing here writes the checkout ``data/`` tree: command context is a tmp file.
Fake Telegram updates only. ``request_short_confirmation`` is the preview, not
an order.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from notifications.telegram_commands import command_context as ctx
from notifications.telegram_commands.menu_i18n import reload_menu_data, set_user_language
from notifications.telegram_commands.short_commands import (
    SHORT_AMT_CALLBACK_PREFIX,
    SHORT_COIN_CALLBACK_PREFIX,
    handle,
    handle_callback,
)
from notifications.telegram_i18n import reload_messages
from tests.support.telegram_capture import texts

SC = "notifications.telegram_commands.short_commands"
CHAT = "529"
OTHER = "5299"


def _coin(symbol: str, name: str | None = None) -> dict:
    ticker = symbol.split("/")[0]
    return {"symbol": symbol, "name": name or ticker, "active": True}


def _query(data: str, chat_id=CHAT, cid: str = "cb") -> dict:
    return {
        "id": cid,
        "data": data,
        "from": {"id": int(chat_id) if str(chat_id).isdigit() else 1},
        "message": {"chat": {"id": chat_id}},
    }


def _callbacks(tg: list) -> list[str]:
    out: list[str] = []
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


class TestShortWizardStableSymbol529(unittest.TestCase):
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
        self.coins = [_coin("ARIA/USDT"), _coin("SOL/USDT")]

    def tearDown(self):
        ctx.clear_context(CHAT)
        ctx.clear_context(OTHER)
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

    def _patch_short_stack(self, *, coins=None, prices=None, list_coins=None, prices_fn=None):
        if list_coins is None:
            coins = list(self.coins if coins is None else coins)

            def list_coins():
                return list(coins)

        if prices_fn is None:
            prices = prices or {}

            def prices_fn(symbols):
                return {s: prices.get(s, 1.0) for s in (symbols or [])}

        patches = [
            patch(f"{SC}.get_bot_config", return_value=self.cfg),
            patch(f"{SC}.shorts_enabled", return_value=True),
            patch(f"{SC}.list_coins", side_effect=list_coins),
            patch(f"{SC}.get_prices_batch", side_effect=prices_fn),
            patch(f"{SC}.answer_callback_query", lambda *a, **k: True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _confirmed_symbol(self, req) -> str:
        self.assertTrue(req.called, "expected a confirmation preview")
        return req.call_args.kwargs["symbol"]

    def test_ac1_size_tap_confirms_tapped_symbol_after_reorder_rotate_remove(self):
        watch = {"coins": [_coin("ARIA/USDT"), _coin("SOL/USDT")]}
        tg: list = []
        self._install_capture(tg)
        self._patch_short_stack(list_coins=lambda: list(watch["coins"]))
        with patch(f"{SC}.request_short_confirmation") as req, \
             patch(f"{SC}.resolve_coin_by_display_index") as resolve:
            self.assertTrue(handle("/short"))
            req.assert_not_called()
            self.assertTrue(handle_callback(_query(f"{SHORT_COIN_CALLBACK_PREFIX}ARIA/USDT")))
            req.assert_not_called()
            self.assertEqual(ctx.get_context(CHAT)["meta"]["coin"], "ARIA/USDT")

            watch["coins"] = [_coin("SOL/USDT"), _coin("ARIA/USDT")]
            self.assertTrue(handle_callback(_query(f"{SHORT_AMT_CALLBACK_PREFIX}25")))
            self.assertEqual(self._confirmed_symbol(req), "ARIA/USDT")
            resolve.assert_not_called()

            req.reset_mock()
            ctx.clear_context(CHAT)
            watch["coins"] = [_coin("ARIA/USDT"), _coin("SOL/USDT")]
            self.assertTrue(handle("/short"))
            self.assertTrue(handle_callback(_query(f"{SHORT_COIN_CALLBACK_PREFIX}SOL/USDT")))
            watch["coins"] = [_coin("ARIA/USDT")]
            self.assertTrue(handle_callback(_query(f"{SHORT_AMT_CALLBACK_PREFIX}10")))
            self.assertEqual(self._confirmed_symbol(req), "SOL/USDT")
            resolve.assert_not_called()

    def test_ac2_picker_callback_is_ticker_not_index(self):
        tg: list = []
        self._install_capture(tg)
        self._patch_short_stack()
        with patch(f"{SC}.request_short_confirmation") as req:
            self.assertTrue(handle("/short"))
            req.assert_not_called()
        coin_cbs = [c for c in _callbacks(tg) if c.startswith(SHORT_COIN_CALLBACK_PREFIX)]
        self.assertEqual(coin_cbs, ["shortcoin:ARIA/USDT", "shortcoin:SOL/USDT"])
        self.assertNotIn("shortcoin:1", coin_cbs)
        self.assertNotIn("shortcoin:2", coin_cbs)

    def test_ac2_numeric_callback_does_not_select_a_different_coin(self):
        watch = {"coins": [_coin("SOL/USDT"), _coin("ARIA/USDT")]}
        tg: list = []
        self._install_capture(tg)
        self._patch_short_stack(list_coins=lambda: list(watch["coins"]))
        with patch(f"{SC}.request_short_confirmation") as req, \
             patch(f"{SC}.resolve_coin_by_display_index") as resolve:
            self.assertTrue(handle("/short"))
            self.assertTrue(handle_callback(_query(f"{SHORT_COIN_CALLBACK_PREFIX}1")))
            req.assert_not_called()
            resolve.assert_not_called()
        blob = "\n".join(texts(tg))
        self.assertIn("abgelaufen", blob.lower())
        self.assertNotIn("SOL/USDT", blob)
        entry = ctx.get_context(CHAT)
        self.assertEqual(entry["command"], "short")
        self.assertEqual(entry["meta"]["state"], "short_awaiting_coin")
        self.assertNotEqual(entry["meta"].get("coin"), "SOL")

    def test_ac3_legacy_index_in_size_context_does_not_reresolve(self):
        watch = {"coins": [_coin("SOL/USDT"), _coin("ARIA/USDT")]}
        tg: list = []
        self._install_capture(tg)
        self._patch_short_stack(list_coins=lambda: list(watch["coins"]))
        ctx.set_context(CHAT, "short", state="short_awaiting_usdt", coin="1", label="ARIA")
        with patch(f"{SC}.request_short_confirmation") as req, \
             patch(f"{SC}.resolve_coin_by_display_index") as resolve:
            self.assertTrue(handle_callback(_query(f"{SHORT_AMT_CALLBACK_PREFIX}25")))
            req.assert_not_called()
            resolve.assert_not_called()
        self.assertNotIn("SOL", "\n".join(texts(tg)))

    def test_ac4_empty_watchlist_arms_typed_ticker(self):
        tg: list = []
        self._install_capture(tg)
        self._patch_short_stack(coins=[])
        with patch(f"{SC}.request_short_confirmation") as req:
            self.assertTrue(handle("/short"))
            req.assert_not_called()
            entry = ctx.get_context(CHAT)
            self.assertIsNotNone(entry)
            self.assertEqual(entry["command"], "short")
            self.assertEqual(entry["meta"]["state"], "short_awaiting_coin")
            self.assertTrue(ctx.try_resolve(CHAT, "H"))
            self.assertEqual(self._confirmed_symbol(req), "H/USDT")

    def test_ac4_list_coins_failure_arms_typed_ticker_and_logs_without_secrets(self):
        secret = "SUPERSECRETVALUE"
        tg: list = []
        self._install_capture(tg)

        def _boom():
            raise RuntimeError(f"watchlist down token={secret} https://user:pass@example.test/x")

        self._patch_short_stack(list_coins=_boom)
        with patch(f"{SC}.request_short_confirmation") as req, \
             patch("logger.log") as mock_log:
            self.assertTrue(handle("/short"))
            req.assert_not_called()
            entry = ctx.get_context(CHAT)
            self.assertEqual(entry["meta"]["state"], "short_awaiting_coin")
            logged = " ".join(str(c) for c in mock_log.call_args_list)
            self.assertIn("list_coins", logged)
            self.assertIn("WARNING", logged)
            self.assertNotIn(secret, logged)
            self.assertNotIn("https://", logged)
            self.assertNotIn("user:pass", logged)
            self.assertTrue(ctx.try_resolve(CHAT, "H"))
            self.assertEqual(self._confirmed_symbol(req), "H/USDT")

    def test_ac5_price_failure_logs_without_secrets_and_keeps_ticker_buttons(self):
        secret = "PRICESECRETVALUE"
        tg: list = []
        self._install_capture(tg)

        def _boom(_symbols):
            raise RuntimeError(f"price token={secret}")

        self._patch_short_stack(prices_fn=_boom)
        with patch(f"{SC}.request_short_confirmation") as req, \
             patch("logger.log") as mock_log:
            self.assertTrue(handle("/short"))
            req.assert_not_called()
        coin_cbs = [c for c in _callbacks(tg) if c.startswith("shortcoin:")]
        self.assertEqual(coin_cbs, ["shortcoin:ARIA/USDT", "shortcoin:SOL/USDT"])
        logged = " ".join(str(c) for c in mock_log.call_args_list)
        self.assertIn("get_prices_batch", logged)
        self.assertNotIn(secret, logged)

    def test_ac6_wrong_chat_does_not_confirm(self):
        tg: list = []
        self._install_capture(tg)
        self._patch_short_stack()
        ctx.set_context(OTHER, "buy", default_usdt=25)
        with patch(f"{SC}.request_short_confirmation") as req:
            self.assertTrue(handle("/short"))
            self.assertTrue(handle_callback(
                _query(f"{SHORT_COIN_CALLBACK_PREFIX}ARIA/USDT", chat_id=OTHER, cid="other")
            ))
            req.assert_not_called()
        self.assertEqual(ctx.get_context(CHAT)["meta"]["state"], "short_awaiting_coin")
        self.assertEqual(ctx.get_context(OTHER)["command"], "buy")

    def test_ac6_wrong_tenant_does_not_confirm(self):
        from core.tenant_context import tenant_context

        tg: list = []
        self._install_capture(tg)
        self._patch_short_stack()
        with patch(f"{SC}.request_short_confirmation") as req:
            self.assertTrue(handle("/short"))
            with tenant_context("henry", scope="paper", owner_chat_id=OTHER):
                self.assertTrue(handle_callback(_query(f"{SHORT_COIN_CALLBACK_PREFIX}ARIA/USDT")))
            req.assert_not_called()
            self.assertEqual(ctx.get_context(CHAT)["meta"]["state"], "short_awaiting_coin")
            self.assertTrue(handle_callback(_query(f"{SHORT_COIN_CALLBACK_PREFIX}ARIA/USDT")))
            req.assert_not_called()
            self.assertEqual(ctx.get_context(CHAT)["meta"]["state"], "short_awaiting_usdt")
            self.assertEqual(ctx.get_context(CHAT)["meta"]["coin"], "ARIA/USDT")
            with tenant_context("henry", scope="paper", owner_chat_id=OTHER):
                self.assertTrue(handle_callback(_query(f"{SHORT_AMT_CALLBACK_PREFIX}25")))
            req.assert_not_called()
            self.assertEqual(ctx.get_context(CHAT)["meta"]["coin"], "ARIA/USDT")

    def test_ac6_stale_size_callback_does_not_confirm(self):
        tg: list = []
        self._install_capture(tg)
        self._patch_short_stack()
        with patch(f"{SC}.request_short_confirmation") as req:
            self.assertTrue(handle("/short"))
            self.assertTrue(handle_callback(_query(f"{SHORT_AMT_CALLBACK_PREFIX}25")))
            req.assert_not_called()
        self.assertEqual(ctx.get_context(CHAT)["meta"]["state"], "short_awaiting_coin")

    def test_ac7_size_tap_previews_and_does_not_execute(self):
        tg: list = []
        self._install_capture(tg)
        self._patch_short_stack(prices={"ARIA/USDT": 0.05, "SOL/USDT": 2.0})
        trading = MagicMock()
        with patch(f"{SC}._trading", trading), \
             patch(f"{SC}.request_short_confirmation", return_value=True) as req:
            self.assertTrue(handle("/short"))
            self.assertTrue(handle_callback(_query(f"{SHORT_COIN_CALLBACK_PREFIX}ARIA/USDT")))
            req.assert_not_called()
            trading.execute_short.assert_not_called()
            self.assertTrue(handle_callback(_query(f"{SHORT_AMT_CALLBACK_PREFIX}25")))
        req.assert_called_once()
        self.assertEqual(req.call_args.kwargs["symbol"], "ARIA/USDT")
        self.assertAlmostEqual(req.call_args.kwargs["usdt"], 25.0)
        self.assertIsNone(req.call_args.kwargs["leverage"])
        trading.execute_short.assert_not_called()
        self.assertIsNone(ctx.get_context(CHAT))

    def test_ac7_missing_price_names_the_selected_symbol_only(self):
        tg: list = []
        self._install_capture(tg)

        def _prices(symbols):
            out = {}
            for sym in symbols or []:
                out[sym] = 0.0 if str(sym).startswith("ARIA") else 9.0
            return out

        self._patch_short_stack(prices_fn=_prices)
        with patch(f"{SC}.request_short_confirmation") as req:
            self.assertTrue(handle("/short"))
            self.assertTrue(handle_callback(_query(f"{SHORT_COIN_CALLBACK_PREFIX}ARIA/USDT")))
            self.assertTrue(handle_callback(_query(f"{SHORT_AMT_CALLBACK_PREFIX}25")))
            req.assert_not_called()
        blob = "\n".join(texts(tg))
        self.assertIn("ARIA/USDT", blob)
        self.assertIn("Kein Preis", blob)
        self.assertNotIn("SOL/USDT", blob)

    def _arm_size(self, coin: str, *, tenant: bool = True):
        from core.tenant_context import tenant_snapshot

        meta = {
            "state": "short_awaiting_usdt",
            "coin": coin,
            "label": coin,
        }
        if tenant:
            tid, scope, _owner = tenant_snapshot()
            meta["tenant_id"] = tid
            meta["scope"] = scope
            meta["bound_chat_id"] = CHAT
        ctx.set_context(CHAT, "short", **meta)

    def test_ac2b_quote_currency_stays_on_the_button_and_the_confirmation(self):
        watch = {"coins": [_coin("ETH/USDT"), _coin("ETH/BTC")]}
        tg: list = []
        self._install_capture(tg)
        self._patch_short_stack(
            list_coins=lambda: list(watch["coins"]),
            prices={"ETH/USDT": 3000.0, "ETH/BTC": 0.05},
        )
        with patch(f"{SC}.request_short_confirmation") as req:
            self.assertTrue(handle("/short"))
            coin_cbs = [c for c in _callbacks(tg) if c.startswith("shortcoin:")]
            self.assertEqual(coin_cbs, ["shortcoin:ETH/USDT", "shortcoin:ETH/BTC"])
            self.assertEqual(len(set(coin_cbs)), 2)
            self.assertTrue(handle_callback(_query("shortcoin:ETH/BTC")))
            self.assertEqual(ctx.get_context(CHAT)["meta"]["coin"], "ETH/BTC")
            self.assertTrue(handle_callback(_query(f"{SHORT_AMT_CALLBACK_PREFIX}25")))
            self.assertEqual(self._confirmed_symbol(req), "ETH/BTC")
            req.reset_mock()
            self.assertTrue(handle("/short"))
            self.assertTrue(handle_callback(_query("shortcoin:ETH/USDT")))
            self.assertTrue(handle_callback(_query(f"{SHORT_AMT_CALLBACK_PREFIX}10")))
            self.assertEqual(self._confirmed_symbol(req), "ETH/USDT")

    def test_ac3b_typed_size_refuses_legacy_index(self):
        watch = {"coins": [_coin("SOL/USDT"), _coin("ARIA/USDT")]}
        tg: list = []
        self._install_capture(tg)
        self._patch_short_stack(list_coins=lambda: list(watch["coins"]))
        self._arm_size("1")
        with patch(f"{SC}.request_short_confirmation") as req, \
             patch(f"{SC}.resolve_coin_by_display_index") as resolve:
            self.assertTrue(ctx.try_resolve(CHAT, "25"))
            req.assert_not_called()
            resolve.assert_not_called()
        blob = "\n".join(texts(tg)).lower()
        self.assertIn("abgelaufen", blob)
        self.assertNotIn("sol", blob)
        self.assertIsNone(ctx.get_context(CHAT))

    def test_ac6b_typed_size_wrong_tenant_does_not_confirm(self):
        from core.tenant_context import tenant_context

        tg: list = []
        self._install_capture(tg)
        self._patch_short_stack(prices={"ARIA/USDT": 0.05, "SOL/USDT": 2.0})
        with patch(f"{SC}.request_short_confirmation") as req:
            self.assertTrue(handle("/short"))
            self.assertTrue(handle_callback(_query("shortcoin:ARIA/USDT")))
            self.assertEqual(ctx.get_context(CHAT)["meta"]["coin"], "ARIA/USDT")
            with tenant_context("henry", scope="paper", owner_chat_id=OTHER):
                self.assertTrue(ctx.try_resolve(CHAT, "25"))
            req.assert_not_called()
            self.assertEqual(ctx.get_context(CHAT)["meta"]["state"], "short_awaiting_usdt")
            self.assertEqual(ctx.get_context(CHAT)["meta"]["coin"], "ARIA/USDT")

    def test_ac6b_size_step_without_tenant_does_not_confirm(self):
        tg: list = []
        self._install_capture(tg)
        self._patch_short_stack(prices={"ARIA/USDT": 0.05})
        self._arm_size("ARIA/USDT", tenant=False)
        with patch(f"{SC}.request_short_confirmation") as req:
            self.assertTrue(ctx.try_resolve(CHAT, "25"))
            req.assert_not_called()
            self._arm_size("ARIA/USDT", tenant=False)
            self.assertTrue(handle_callback(_query(f"{SHORT_AMT_CALLBACK_PREFIX}25")))
            req.assert_not_called()
        self.assertIn("abgelaufen", "\n".join(texts(tg)).lower())

    def test_ac9_skipped_symbol_is_logged(self):
        tg: list = []
        self._install_capture(tg)
        self._patch_short_stack(coins=[_coin("ETH/USDT"), _coin("BAD-COIN/USDT")])
        with patch("logger.log") as mock_log, \
             patch(f"{SC}.request_short_confirmation") as req:
            self.assertTrue(handle("/short"))
            req.assert_not_called()
        logged = " ".join(str(call) for call in mock_log.call_args_list)
        self.assertIn("BAD-COIN/USDT", logged)
        self.assertIn("WARNING", logged)
        self.assertIn("skipped", logged)
        coin_cbs = [c for c in _callbacks(tg) if c.startswith("shortcoin:")]
        self.assertEqual(coin_cbs, ["shortcoin:ETH/USDT"])

    def test_ac9_non_finite_size_callback_does_not_confirm(self):
        tg: list = []
        self._install_capture(tg)
        self._patch_short_stack(prices={"ARIA/USDT": 0.05, "SOL/USDT": 2.0})
        with patch(f"{SC}.request_short_confirmation") as req:
            self.assertTrue(handle("/short"))
            self.assertTrue(handle_callback(_query("shortcoin:ARIA/USDT")))
            for raw in ("nan", "inf", "0", "-1"):
                self.assertTrue(handle_callback(_query(f"{SHORT_AMT_CALLBACK_PREFIX}{raw}")))
                req.assert_not_called()
            self.assertEqual(ctx.get_context(CHAT)["meta"]["coin"], "ARIA/USDT")
        blob = "\n".join(texts(tg)).lower()
        self.assertIn("ungültig", blob)
        self.assertNotIn("sol/usdt", blob)


if __name__ == "__main__":
    unittest.main()
