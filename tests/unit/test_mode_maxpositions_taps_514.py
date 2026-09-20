"""#514 — tap-first /mode and /maxpositions (control UI, not #497 copy).

Nothing here writes into ``data/``: persist is patched, context lives in tmp.
Frozen mode persistence stays in ``test_pause_tenant_config_patch.py``,
``test_telegram_simulated_live_copy_497.py``, and ``test_mode_commands.py``.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.simulated_trading import simulated_live_config_updates
from notifications.telegram_commands import command_context as ctx
from notifications.telegram_commands.menu_i18n import reload_menu_data, set_user_language
from notifications.telegram_commands.mode_commands import (
    MAXPOS_CALLBACK_PREFIX,
    MAXPOS_PRESETS,
    MODE_CALLBACK_PREFIX,
    handle,
    handle_callback,
)
from notifications.telegram_commands.router import dispatch_callback
from notifications.telegram_i18n import reload_messages
from tests.support.telegram_capture import texts

MC = "notifications.telegram_commands.mode_commands"
CHAT = "514"
OPERATOR = "100"


def _query(data: str, chat_id=CHAT, cid="cb", *, missing_chat: bool = False) -> dict:
    q = {"id": cid, "data": data}
    if missing_chat:
        return q
    q["message"] = {"chat": {"id": chat_id}}
    return q


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


class TestModeMaxpositionsTaps514(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        ctx._CONTEXT_FILE = Path(self.tmp.name) / "ctx.json"
        ctx.set_chat_id(CHAT)
        set_user_language("de")
        reload_messages()
        reload_menu_data()

    def tearDown(self):
        ctx.clear_context(CHAT)
        ctx.clear_context(OPERATOR)
        ctx.set_chat_id("")
        self.tmp.cleanup()

    def _install_capture(self, tg: list):
        from notifications.telegram_commands import command_context as cmdctx
        from notifications.telegram_commands import mode_commands as mcmod

        def _msg(text, reply_markup=None, **kwargs):
            tg.append({
                "kind": "message",
                "text": "" if text is None else str(text),
                "reply_markup": reply_markup,
                **kwargs,
            })
            return True

        for mod, attr, fn in (
            (mcmod, "send_telegram_message", _msg),
            (cmdctx, "send_telegram_message", _msg),
        ):
            p = patch.object(mod, attr, fn)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(mcmod, "answer_callback_query", lambda *a, **k: True)
        p.start()
        self.addCleanup(p.stop)
        return tg

    def _patch_mode_status(self):
        svc = MagicMock()
        svc.mode_label.return_value = "Simulated Live (paper/json)"
        patches = [
            patch(f"{MC}.TradingService", return_value=svc),
            patch(f"{MC}.format_identity_section", return_value="identity"),
            patch(f"{MC}.is_simulated_trading", return_value=True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _patch_persist(self):
        saved: list[dict] = []

        def _save(updates):
            saved.append(dict(updates))
            return True

        patches = [
            patch(f"{MC}._save_mode_updates", side_effect=_save),
            patch(f"{MC}.reload_config"),
            patch(f"{MC}.on_trading_mode_change", return_value=""),
            patch(f"{MC}.get_config", return_value={
                "trading_mode": "off",
                "max_open_positions": 5,
                "live": {"dry_run": True},
            }),
            patch(f"{MC}.count_open_positions", return_value=2),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return saved

    def test_bare_mode_taps_sim_live_and_off_not_live_confirm(self):
        tg = []
        self._install_capture(tg)
        self._patch_mode_status()
        with patch(f"{MC}._save_mode_updates") as save:
            self.assertTrue(handle("/mode"))
            save.assert_not_called()
        callbacks = _callbacks(tg)
        self.assertEqual(
            [c for c in callbacks if c != ctx.CANCEL_CALLBACK],
            [f"{MODE_CALLBACK_PREFIX}live", f"{MODE_CALLBACK_PREFIX}off"],
        )
        self.assertIn(ctx.CANCEL_CALLBACK, callbacks)
        self.assertTrue(all(
            not c.startswith("sellpos:")
            and not c.startswith("lotsell:")
            and not c.startswith("lockpos:")
            and not c.startswith("shortcoin:")
            for c in callbacks
        ))
        joined = "\n".join(texts(tg))
        self.assertIn("Simulated Live", joined)
        self.assertNotIn("modepick:paper", "".join(callbacks))
        self.assertFalse(any("live_confirm" in c for c in callbacks))
        entry = ctx.get_context(CHAT)
        self.assertEqual(entry["command"], "mode")
        self.assertEqual(entry["meta"]["state"], "mode_awaiting_choice")

    def test_mode_tap_sim_live_uses_live_path_not_paper_slash(self):
        tg = []
        self._install_capture(tg)
        self._patch_mode_status()
        saved = self._patch_persist()
        self.assertTrue(handle("/mode"))
        tg.clear()
        self.assertTrue(handle_callback(_query(f"{MODE_CALLBACK_PREFIX}live")))
        self.assertEqual(saved, [simulated_live_config_updates()])
        self.assertIn("Simulated Live", "\n".join(texts(tg)))
        self.assertIsNone(ctx.get_context(CHAT))

    def test_mode_tap_off_persists_analysis_only(self):
        tg = []
        self._install_capture(tg)
        self._patch_mode_status()
        saved = self._patch_persist()
        self.assertTrue(handle("/mode"))
        self.assertTrue(handle_callback(_query(f"{MODE_CALLBACK_PREFIX}off")))
        self.assertEqual(saved, [{"trading_mode": "off", "virtual_trading": False}])
        self.assertIsNone(ctx.get_context(CHAT))

    def test_typed_mode_paper_still_maps_to_simulated_live(self):
        tg = []
        self._install_capture(tg)
        saved = self._patch_persist()
        self.assertTrue(handle("/mode paper"))
        self.assertEqual(saved, [simulated_live_config_updates()])
        self.assertIsNone(ctx.get_context(CHAT))

    def test_stand_has_no_mode_taps(self):
        tg = []
        self._install_capture(tg)
        self._patch_mode_status()
        self.assertTrue(handle("/stand"))
        self.assertEqual(_callbacks(tg), [])
        self.assertIsNone(ctx.get_context(CHAT))

    def test_modepick_missing_chat_id_fails_closed(self):
        tg = []
        self._install_capture(tg)
        self._patch_mode_status()
        ctx.set_context(OPERATOR, "buy", default_usdt=25)
        ctx.set_context(CHAT, "mode", state="mode_awaiting_choice")
        with patch.dict(os.environ, {"TELEGRAM_CHAT_ID": OPERATOR}, clear=False), \
             patch(f"{MC}._save_mode_updates") as save, \
             patch("logger.log") as mock_log:
            self.assertTrue(handle_callback(
                _query(f"{MODE_CALLBACK_PREFIX}live", missing_chat=True)
            ))
            save.assert_not_called()
            mock_log.assert_any_call("modepick callback missing chat id", "WARNING")
        self.assertEqual(ctx.get_context(OPERATOR)["command"], "buy")
        self.assertEqual(ctx.get_context(CHAT)["command"], "mode")

    def test_mode_cancel_chrome_clears_without_persist(self):
        tg = []
        self._install_capture(tg)
        self._patch_mode_status()
        self.assertTrue(handle("/mode"))
        self.assertIsNotNone(ctx.get_context(CHAT))
        with patch(f"{MC}._save_mode_updates") as save, \
             patch("telegram_notifier.answer_callback_query"):
            self.assertTrue(dispatch_callback(_query(ctx.CANCEL_CALLBACK)))
            save.assert_not_called()
        self.assertIsNone(ctx.get_context(CHAT))

    def test_bare_maxpositions_taps_presets_and_keeps_typed_hint(self):
        tg = []
        self._install_capture(tg)
        with patch(f"{MC}.get_config", return_value={"max_open_positions": 5}), \
             patch(f"{MC}.count_open_positions", return_value=3), \
             patch(f"{MC}._save_mode_updates") as save:
            self.assertTrue(handle("/maxpositions"))
            save.assert_not_called()
        callbacks = _callbacks(tg)
        self.assertEqual(
            [c for c in callbacks if c != ctx.CANCEL_CALLBACK],
            [f"{MAXPOS_CALLBACK_PREFIX}{n}" for n in MAXPOS_PRESETS],
        )
        self.assertIn(ctx.CANCEL_CALLBACK, callbacks)
        joined = "\n".join(texts(tg))
        self.assertIn("nur Zahl", joined)
        self.assertIn("5", joined)
        self.assertIn("3", joined)
        entry = ctx.get_context(CHAT)
        self.assertEqual(entry["command"], "maxpositions")
        self.assertEqual(entry["meta"]["state"], "maxpos_awaiting_value")

    def test_maxpositions_tap_sets_cap_without_slash(self):
        tg = []
        self._install_capture(tg)
        saved = self._patch_persist()
        self.assertTrue(handle("/maxpositions"))
        tg.clear()
        self.assertTrue(handle_callback(_query(f"{MAXPOS_CALLBACK_PREFIX}10")))
        self.assertEqual(saved, [{"max_open_positions": 10}])
        self.assertIn("10", "\n".join(texts(tg)))
        self.assertIsNone(ctx.get_context(CHAT))

    def test_maxpositions_typed_override_still_works(self):
        tg = []
        self._install_capture(tg)
        saved = self._patch_persist()
        self.assertTrue(handle("/maxpositions 12"))
        self.assertEqual(saved, [{"max_open_positions": 12}])

    def test_maxpos_missing_chat_id_fails_closed(self):
        tg = []
        self._install_capture(tg)
        ctx.set_context(OPERATOR, "buy", default_usdt=25)
        ctx.set_context(CHAT, "maxpositions", state="maxpos_awaiting_value")
        with patch.dict(os.environ, {"TELEGRAM_CHAT_ID": OPERATOR}, clear=False), \
             patch(f"{MC}._save_mode_updates") as save, \
             patch("logger.log") as mock_log:
            self.assertTrue(handle_callback(
                _query(f"{MAXPOS_CALLBACK_PREFIX}10", missing_chat=True)
            ))
            save.assert_not_called()
            mock_log.assert_any_call("maxpos callback missing chat id", "WARNING")
        self.assertEqual(ctx.get_context(OPERATOR)["command"], "buy")
        self.assertEqual(ctx.get_context(CHAT)["command"], "maxpositions")

    def test_maxpositions_cancel_clears_without_persist(self):
        tg = []
        self._install_capture(tg)
        with patch(f"{MC}.get_config", return_value={"max_open_positions": 5}), \
             patch(f"{MC}.count_open_positions", return_value=1):
            self.assertTrue(handle("/maxpositions"))
        with patch(f"{MC}._save_mode_updates") as save, \
             patch("telegram_notifier.answer_callback_query"):
            self.assertTrue(dispatch_callback(_query(ctx.CANCEL_CALLBACK)))
            save.assert_not_called()
        self.assertIsNone(ctx.get_context(CHAT))
