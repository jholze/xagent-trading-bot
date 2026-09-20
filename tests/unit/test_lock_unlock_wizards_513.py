"""#513 — tap-first /lock /unlock wizards (sell-wizard shape).

Nothing here writes into ``data/``: persist is patched, context lives in tmp.
Frozen lock-confirm assertions in ``test_lock_commands_453.py`` and
``test_lock_confirm_chat_id_498.py`` stay in those files.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from notifications.telegram_commands import command_context as ctx
from notifications.telegram_commands.lock_commands import (
    LOCK_DUR_CALLBACK_PREFIX,
    LOCK_POS_CALLBACK_PREFIX,
    UNLOCK_POS_CALLBACK_PREFIX,
    handle,
    handle_callback,
    reset_lock_confirm_for_tests,
)
from notifications.telegram_commands.menu_i18n import set_user_language
from notifications.telegram_commands.router import dispatch_callback
from notifications.telegram_i18n import reload_messages
from tests.support.telegram_capture import texts

LC = "notifications.telegram_commands.lock_commands"
CHAT = "513"
OPERATOR = "100"


def _pos(symbol="BLESS/USDT", timeframe="1h", amount=10, **extra):
    row = {"symbol": symbol, "timeframe": timeframe, "amount": amount}
    row.update(extra)
    return row


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
    for item in _buttons(tg):
        for row in item.get("buttons") or []:
            for btn in row:
                out.append(btn.get("callback_data") or "")
    return out


class TestLockUnlockWizards513(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        ctx._CONTEXT_FILE = Path(self.tmp.name) / "ctx.json"
        ctx.set_chat_id(CHAT)
        set_user_language("de")
        reload_messages()
        reset_lock_confirm_for_tests()

    def tearDown(self):
        ctx.clear_context(CHAT)
        ctx.clear_context(OPERATOR)
        ctx.set_chat_id("")
        reset_lock_confirm_for_tests()
        self.tmp.cleanup()

    def _patch_longs(self, lots, *, locks=None):
        locks = locks or {}

        def _get_pos(sym, tf="1h"):
            for p in lots:
                if p["symbol"] == sym and str(p.get("timeframe") or "1h") == str(tf):
                    return p
            return lots[0] if lots else {}

        def _get_lock(pos):
            if not pos:
                return None
            key = f"{pos.get('symbol')}|{pos.get('timeframe') or '1h'}"
            if key in locks:
                return locks[key]
            return pos.get("lock")

        patches = [
            patch(f"{LC}.position_locks_enabled", lambda *a, **k: True),
            patch(f"{LC}.list_active_positions", lambda: list(lots)),
            patch(f"{LC}.get_position", _get_pos),
            patch(f"{LC}.get_lock", _get_lock),
            patch(f"{LC}.is_open_position", lambda *a, **k: True),
            patch(f"{LC}.get_prices_batch", lambda symbols: {s: 1.0 for s in symbols}),
            patch(f"{LC}.answer_callback_query", lambda *a, **k: True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return lots

    def test_bare_lock_taps_symbol_timeframe_not_display_index(self):
        bless_1h = _pos("BLESS/USDT", "1h")
        bless_4h = _pos("BLESS/USDT", "4h")
        rave = _pos("RAVE/USDT", "1h")
        self._patch_longs([bless_1h, bless_4h, rave])
        tg = []
        self._install_capture(tg)
        with patch(f"{LC}.set_position_lock") as set_lock:
            self.assertTrue(handle("/lock"))
            set_lock.assert_not_called()
        callbacks = _callbacks(tg)
        self.assertEqual(
            callbacks,
            ["lockpos:BLESS:1h", "lockpos:BLESS:4h", "lockpos:RAVE:1h"],
        )
        self.assertTrue(all(
            not c.startswith("sellpos:") and not c.startswith("lotsell:")
            for c in callbacks
        ))
        self.assertTrue(all(not c.startswith("lock_ok:") for c in callbacks))
        self.assertIn("Position Locks", "\n".join(texts(tg)))
        entry = ctx.get_context(CHAT)
        self.assertEqual(entry["command"], "lock")
        self.assertEqual(entry["meta"]["state"], "lock_awaiting_position")

    def _install_capture(self, tg: list):
        from notifications.telegram_commands import command_context as cmdctx
        from notifications.telegram_commands import lock_commands as lcmod

        def _msg(text, reply_markup=None, **kwargs):
            tg.append({"kind": "message", "text": "" if text is None else str(text),
                       "reply_markup": reply_markup, **kwargs})
            return True

        def _btns(text, buttons, **kwargs):
            tg.append({"kind": "buttons", "text": "" if text is None else str(text),
                       "buttons": buttons, **kwargs})
            return True

        for mod, attr, fn in (
            (lcmod, "send_telegram_message", _msg),
            (lcmod, "send_telegram_buttons", _btns),
            (cmdctx, "send_telegram_message", _msg),
        ):
            p = patch.object(mod, attr, fn)
            p.start()
            self.addCleanup(p.stop)
        return tg

    def test_lock_happy_path_never_types_ticker(self):
        tg = []
        self._install_capture(tg)
        self._patch_longs([_pos()])
        with patch(f"{LC}.set_position_lock") as set_lock:
            self.assertTrue(handle("/lock"))
            set_lock.assert_not_called()
            tg.clear()
            self.assertTrue(handle_callback(_query(f"{LOCK_POS_CALLBACK_PREFIX}BLESS:1h")))
            set_lock.assert_not_called()
            self.assertEqual(
                _callbacks(tg),
                [
                    f"{LOCK_DUR_CALLBACK_PREFIX}24h",
                    f"{LOCK_DUR_CALLBACK_PREFIX}7d",
                    f"{LOCK_DUR_CALLBACK_PREFIX}permanent",
                ],
            )
            self.assertEqual(ctx.get_context(CHAT)["meta"]["state"], "lock_awaiting_duration")
            tg.clear()
            self.assertTrue(handle_callback(_query(f"{LOCK_DUR_CALLBACK_PREFIX}24h")))
            set_lock.assert_not_called()
            confirm = _callbacks(tg)
            self.assertTrue(confirm[0].startswith("lock_ok:"))
            self.assertTrue(confirm[1].startswith("lock_no:"))
            token = confirm[0].split(":", 1)[1]
            self.assertIsNone(ctx.get_context(CHAT))
            tg.clear()
            self.assertTrue(handle_callback(_query(f"lock_ok:{token}")))
            set_lock.assert_called_once()
            sym, tf, lock = set_lock.call_args.args[:3]
            self.assertEqual(sym, "BLESS/USDT")
            self.assertEqual(tf, "1h")
            self.assertTrue(set_lock.call_args.kwargs.get("persist"))
            self.assertTrue(lock.get("until"))

    def test_lock_duration_permanent_reaches_existing_confirm(self):
        tg = []
        self._install_capture(tg)
        self._patch_longs([_pos("RAVE/USDT", "4h")])
        with patch(f"{LC}.set_position_lock") as set_lock:
            self.assertTrue(handle("/lock"))
            self.assertTrue(handle_callback(_query(f"{LOCK_POS_CALLBACK_PREFIX}RAVE:4h")))
            tg.clear()
            self.assertTrue(handle_callback(_query(f"{LOCK_DUR_CALLBACK_PREFIX}permanent")))
            set_lock.assert_not_called()
            msg = _buttons(tg)[0]["text"]
            self.assertIn("RAVE/USDT", msg)
            self.assertIn("Stop-Loss", msg)
            self.assertTrue(_callbacks(tg)[0].startswith("lock_ok:"))

    def test_lockpos_missing_chat_id_fails_closed(self):
        tg = []
        self._install_capture(tg)
        self._patch_longs([_pos()])
        ctx.set_context(OPERATOR, "buy", default_usdt=25)
        ctx.set_context(CHAT, "lock", state="lock_awaiting_position")
        with patch.dict(os.environ, {"TELEGRAM_CHAT_ID": OPERATOR}, clear=False), \
             patch(f"{LC}.set_position_lock") as set_lock, \
             patch("logger.log") as mock_log:
            self.assertTrue(handle_callback(
                _query(f"{LOCK_POS_CALLBACK_PREFIX}BLESS:1h", missing_chat=True)
            ))
            set_lock.assert_not_called()
            mock_log.assert_any_call("lockpos callback missing chat id", "WARNING")
        self.assertEqual(ctx.get_context(OPERATOR)["command"], "buy")
        self.assertEqual(ctx.get_context(CHAT)["command"], "lock")

    def test_lock_cancel_chrome_clears_without_persist(self):
        tg = []
        self._install_capture(tg)
        self._patch_longs([_pos()])
        self.assertTrue(handle("/lock"))
        self.assertIsNotNone(ctx.get_context(CHAT))
        with patch(f"{LC}.set_position_lock") as set_lock, \
             patch("telegram_notifier.answer_callback_query"):
            self.assertTrue(dispatch_callback(_query(ctx.CANCEL_CALLBACK)))
            set_lock.assert_not_called()
        self.assertIsNone(ctx.get_context(CHAT))

    def test_unlock_empty_state(self):
        tg = []
        self._install_capture(tg)
        self._patch_longs([_pos()], locks={})
        with patch(f"{LC}.set_position_lock") as set_lock:
            self.assertTrue(handle("/unlock"))
            set_lock.assert_not_called()
        self.assertFalse(_buttons(tg))
        self.assertIn("keine gelockten", "\n".join(texts(tg)).lower())
        self.assertIsNone(ctx.get_context(CHAT))

    def test_unlock_happy_path_tap_then_confirm(self):
        tg = []
        self._install_capture(tg)
        pos = _pos("H/USDT", "4h")
        lock = {"until": None, "modes": ["no_auto_sell"], "enabled": True}
        self._patch_longs([pos], locks={"H/USDT|4h": lock})
        with patch(f"{LC}.set_position_lock") as set_lock:
            self.assertTrue(handle("/unlock"))
            set_lock.assert_not_called()
            self.assertEqual(_callbacks(tg), [f"{UNLOCK_POS_CALLBACK_PREFIX}H:4h"])
            self.assertEqual(ctx.get_context(CHAT)["meta"]["state"], "unlock_awaiting_position")
            tg.clear()
            self.assertTrue(handle_callback(_query(f"{UNLOCK_POS_CALLBACK_PREFIX}H:4h")))
            set_lock.assert_not_called()
            confirm = _callbacks(tg)
            self.assertTrue(confirm[0].startswith("unlock_ok:"))
            self.assertTrue(confirm[1].startswith("unlock_no:"))
            self.assertIsNone(ctx.get_context(CHAT))
            token = confirm[0].split(":", 1)[1]
            tg.clear()
            self.assertTrue(handle_callback(_query(f"unlock_ok:{token}")))
            set_lock.assert_called_once_with("H/USDT", "4h", None, persist=True)

    def test_unlock_no_writes_nothing(self):
        tg = []
        self._install_capture(tg)
        pos = _pos("H/USDT", "4h")
        lock = {"until": None, "modes": ["no_auto_sell"], "enabled": True}
        self._patch_longs([pos], locks={"H/USDT|4h": lock})
        with patch(f"{LC}.set_position_lock") as set_lock:
            self.assertTrue(handle("/unlock"))
            self.assertTrue(handle_callback(_query(f"{UNLOCK_POS_CALLBACK_PREFIX}H:4h")))
            token = [c for c in _callbacks(tg) if c.startswith("unlock_no:")][0].split(":", 1)[1]
            tg.clear()
            self.assertTrue(handle_callback(_query(f"unlock_no:{token}")))
            set_lock.assert_not_called()
        joined = "\n".join(texts(tg)).lower()
        self.assertTrue("abgebrochen" in joined or "cancelled" in joined)

    def test_unlock_ok_missing_chat_id_fails_closed(self):
        tg = []
        self._install_capture(tg)
        pos = _pos("H/USDT", "4h")
        lock = {"until": None, "modes": ["no_auto_sell"], "enabled": True}
        self._patch_longs([pos], locks={"H/USDT|4h": lock})
        ctx.set_context(OPERATOR, "buy", default_usdt=25)
        with patch.dict(os.environ, {"TELEGRAM_CHAT_ID": OPERATOR}, clear=False), \
             patch(f"{LC}.set_position_lock") as set_lock:
            self.assertTrue(handle("/unlock"))
            self.assertTrue(handle_callback(_query(f"{UNLOCK_POS_CALLBACK_PREFIX}H:4h")))
            token = [c for c in _callbacks(tg) if c.startswith("unlock_ok:")][0].split(":", 1)[1]
            set_lock.assert_not_called()
            self.assertTrue(handle_callback(_query(f"unlock_ok:{token}", missing_chat=True)))
            set_lock.assert_not_called()
        self.assertEqual(ctx.get_context(OPERATOR)["command"], "buy")

    def test_typed_unlock_with_symbol_stays_immediate(self):
        tg = []
        self._install_capture(tg)
        pos = _pos("H/USDT", "4h")
        lock = {"until": None, "modes": ["no_auto_sell"]}
        self._patch_longs([pos], locks={"H/USDT|4h": lock})
        with patch(f"{LC}.resolve_position_by_symbol", return_value=pos), \
             patch(f"{LC}.set_position_lock") as set_lock:
            self.assertTrue(handle("/unlock H"))
            set_lock.assert_called_once_with("H/USDT", "4h", None, persist=True)
        self.assertFalse(any(c.startswith("unlock_ok:") for c in _callbacks(tg)))

    def test_typed_lock_symbol_still_uses_existing_confirm(self):
        tg = []
        self._install_capture(tg)
        pos = _pos()
        self._patch_longs([pos])
        with patch(f"{LC}.resolve_position_by_symbol", return_value=pos), \
             patch(f"{LC}.set_position_lock") as set_lock:
            self.assertTrue(handle("/lock BLESS"))
            set_lock.assert_not_called()
        callbacks = _callbacks(tg)
        self.assertTrue(callbacks[0].startswith("lock_ok:"))
        self.assertTrue(callbacks[1].startswith("lock_no:"))
        self.assertIsNone(ctx.get_context(CHAT))

    def test_lock_build_command_unstated_meta_unchanged(self):
        self.assertEqual(ctx._build_command("lock", "BLESS", {}), "/lock BLESS")
        self.assertEqual(ctx._build_command("unlock", "BLESS", {}), "/unlock BLESS")
        self.assertEqual(
            ctx._build_command(
                "lock",
                "24h",
                {"state": "lock_awaiting_duration", "position": "BLESS"},
            ),
            "/lock BLESS 24h",
        )

    def test_cancel_callback_missing_chat_id_does_not_pop_lock_wizard(self):
        ctx.set_chat_id("")
        ctx.set_context(OPERATOR, "lock", state="lock_awaiting_position")
        with patch.dict(os.environ, {"TELEGRAM_CHAT_ID": OPERATOR}, clear=False), \
             patch("telegram_notifier.answer_callback_query"), \
             patch("notifications.telegram_commands.command_context.send_telegram_message") as send, \
             patch("logger.log") as mock_log:
            self.assertTrue(ctx.handle_callback(_query(ctx.CANCEL_CALLBACK, missing_chat=True)))
            send.assert_not_called()
            mock_log.assert_any_call("cmdctx cancel callback missing chat id", "WARNING")
        self.assertEqual(ctx.get_context(OPERATOR)["command"], "lock")


if __name__ == "__main__":
    unittest.main()
