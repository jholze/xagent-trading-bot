"""#448 leftover — typed /reload <scope> and /dryrun need confirm.

Nothing here writes into ``data/``: reload/dryrun persist is patched, context
lives in tmp. Frozen help/unknown-scope assertions stay in
``test_reload_registry.py``. Home IA stays in ``test_menu_home_more_445.py``.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from notifications.telegram_commands import command_context as ctx
from notifications.telegram_commands.gate_commands import (
    DRYRUN_CALLBACK_PREFIX,
    handle as dryrun_handle,
    handle_callback as dryrun_handle_callback,
)
from notifications.telegram_commands.menu_commands import HOME_KEYS
from notifications.telegram_commands.menu_i18n import set_user_language
from notifications.telegram_commands.reload_commands import (
    RELOAD_CALLBACK_PREFIX,
    handle as reload_handle,
    handle_callback as reload_handle_callback,
)
from notifications.telegram_commands.router import dispatch_callback
from notifications.telegram_i18n import reload_messages
from tests.support.telegram_capture import texts

RC = "notifications.telegram_commands.reload_commands"
GC = "notifications.telegram_commands.gate_commands"
CHAT = "448"
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


class TestReloadDryrunConfirm448(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        ctx._CONTEXT_FILE = Path(self.tmp.name) / "ctx.json"
        ctx.set_chat_id(CHAT)
        set_user_language("de")
        reload_messages()

    def tearDown(self):
        ctx.clear_context(CHAT)
        ctx.clear_context(OPERATOR)
        ctx.set_chat_id("")
        self.tmp.cleanup()

    def _install_capture(self, tg: list, *modules):
        from notifications.telegram_commands import command_context as cmdctx
        from notifications.telegram_commands import gate_commands as gcmod
        from notifications.telegram_commands import reload_commands as rcmod

        def _msg(text, reply_markup=None, **kwargs):
            tg.append({
                "kind": "message",
                "text": "" if text is None else str(text),
                "reply_markup": reply_markup,
                **kwargs,
            })
            return True

        targets = [rcmod, gcmod, cmdctx, *modules]
        seen = set()
        for mod in targets:
            key = id(mod)
            if key in seen:
                continue
            seen.add(key)
            p = patch.object(mod, "send_telegram_message", _msg)
            p.start()
            self.addCleanup(p.stop)
        for mod, attr in ((rcmod, "answer_callback_query"), (gcmod, "answer_callback_query")):
            p = patch.object(mod, attr, lambda *a, **k: True)
            p.start()
            self.addCleanup(p.stop)
        return tg

    def test_home_keys_unchanged(self):
        self.assertEqual(
            HOME_KEYS,
            ["positions", "buy", "sell", "pause", "help", "menu"],
        )
        self.assertNotIn("reload", HOME_KEYS)
        self.assertNotIn("dryrun", HOME_KEYS)

    def test_typed_reload_all_does_not_run_until_confirm(self):
        tg = []
        self._install_capture(tg)
        with patch(f"{RC}.run_reload") as run:
            self.assertTrue(reload_handle("/reload all"))
            run.assert_not_called()
        callbacks = _callbacks(tg)
        self.assertIn(RELOAD_CALLBACK_PREFIX, callbacks)
        self.assertIn(ctx.CANCEL_CALLBACK, callbacks)
        joined = "\n".join(texts(tg))
        self.assertIn("all", joined)
        self.assertIn("Reload", joined)
        entry = ctx.get_context(CHAT)
        self.assertEqual(entry["command"], "reload")
        self.assertEqual(entry["meta"]["state"], "reload_awaiting_confirm")
        self.assertEqual(entry["meta"]["scopes"], "all")

    def test_typed_reload_ui_needs_confirm(self):
        tg = []
        self._install_capture(tg)
        with patch(f"{RC}.run_reload") as run:
            self.assertTrue(reload_handle("/reload ui"))
            run.assert_not_called()
        self.assertIn("ui", "\n".join(texts(tg)))

    def test_reload_help_still_immediate(self):
        tg = []
        self._install_capture(tg)
        with patch(f"{RC}.run_reload") as run:
            self.assertTrue(reload_handle("/reload"))
            run.assert_not_called()
        self.assertIsNone(ctx.get_context(CHAT))
        self.assertEqual(_callbacks(tg), [])
        self.assertIn("Soft Reload", "\n".join(texts(tg)))

    def test_reload_confirm_runs_scoped_reload(self):
        tg = []
        self._install_capture(tg)
        report = MagicMock()
        report.ok = True
        with patch(f"{RC}.run_reload", return_value=report) as run, \
             patch(f"{RC}.format_reload_report_html", return_value="did reload"):
            self.assertTrue(reload_handle("/reload all"))
            tg.clear()
            self.assertTrue(reload_handle_callback(_query(RELOAD_CALLBACK_PREFIX)))
            run.assert_called_once()
            scopes = run.call_args.args[0]
            self.assertEqual(scopes, ["ui", "config", "lists", "cache"])
            self.assertEqual(run.call_args.kwargs.get("source"), "telegram")
        self.assertIn("did reload", "\n".join(texts(tg)))
        self.assertIsNone(ctx.get_context(CHAT))

    def test_reload_cancel_chrome_does_not_run(self):
        tg = []
        self._install_capture(tg)
        with patch(f"{RC}.run_reload") as run, \
             patch("telegram_notifier.answer_callback_query"):
            self.assertTrue(reload_handle("/reload all"))
            self.assertIsNotNone(ctx.get_context(CHAT))
            self.assertTrue(dispatch_callback(_query(ctx.CANCEL_CALLBACK)))
            run.assert_not_called()
        self.assertIsNone(ctx.get_context(CHAT))

    def test_reload_ok_missing_chat_id_fails_closed(self):
        tg = []
        self._install_capture(tg)
        ctx.set_context(OPERATOR, "buy", default_usdt=25)
        ctx.set_context(CHAT, "reload", state="reload_awaiting_confirm", scopes="all")
        with patch.dict(os.environ, {"TELEGRAM_CHAT_ID": OPERATOR}, clear=False), \
             patch(f"{RC}.run_reload") as run, \
             patch("logger.log") as mock_log:
            self.assertTrue(reload_handle_callback(
                _query(RELOAD_CALLBACK_PREFIX, missing_chat=True)
            ))
            run.assert_not_called()
            mock_log.assert_any_call("reload_ok callback missing chat id", "WARNING")
        self.assertEqual(ctx.get_context(OPERATOR)["command"], "buy")
        self.assertEqual(ctx.get_context(CHAT)["command"], "reload")

    def test_typed_dryrun_does_not_run_until_confirm(self):
        tg = []
        self._install_capture(tg)
        with patch(f"{GC}.reload_config") as reload_cfg, \
             patch(f"{GC}._format_dryrun_status") as fmt:
            self.assertTrue(dryrun_handle("/dryrun"))
            reload_cfg.assert_not_called()
            fmt.assert_not_called()
        callbacks = _callbacks(tg)
        self.assertIn(DRYRUN_CALLBACK_PREFIX, callbacks)
        self.assertIn(ctx.CANCEL_CALLBACK, callbacks)
        entry = ctx.get_context(CHAT)
        self.assertEqual(entry["command"], "dryrun")
        self.assertEqual(entry["meta"]["state"], "dryrun_awaiting_confirm")

    def test_dryrun_alias_also_confirms(self):
        tg = []
        self._install_capture(tg)
        with patch(f"{GC}.reload_config") as reload_cfg:
            self.assertTrue(dryrun_handle("/dry_run"))
            reload_cfg.assert_not_called()
        self.assertIn(DRYRUN_CALLBACK_PREFIX, _callbacks(tg))

    def test_dryrun_confirm_loads_status(self):
        tg = []
        self._install_capture(tg)
        with patch(f"{GC}.reload_config") as reload_cfg, \
             patch(f"{GC}._format_dryrun_status", return_value="dry status"):
            self.assertTrue(dryrun_handle("/dryrun"))
            tg.clear()
            self.assertTrue(dryrun_handle_callback(_query(DRYRUN_CALLBACK_PREFIX)))
            reload_cfg.assert_called_once()
        self.assertIn("dry status", "\n".join(texts(tg)))
        self.assertIsNone(ctx.get_context(CHAT))

    def test_dryrun_cancel_chrome_does_not_run(self):
        tg = []
        self._install_capture(tg)
        with patch(f"{GC}.reload_config") as reload_cfg, \
             patch("telegram_notifier.answer_callback_query"):
            self.assertTrue(dryrun_handle("/dryrun"))
            self.assertTrue(dispatch_callback(_query(ctx.CANCEL_CALLBACK)))
            reload_cfg.assert_not_called()
        self.assertIsNone(ctx.get_context(CHAT))

    def test_dryrun_ok_missing_chat_id_fails_closed(self):
        tg = []
        self._install_capture(tg)
        ctx.set_context(OPERATOR, "buy", default_usdt=25)
        ctx.set_context(CHAT, "dryrun", state="dryrun_awaiting_confirm")
        with patch.dict(os.environ, {"TELEGRAM_CHAT_ID": OPERATOR}, clear=False), \
             patch(f"{GC}.reload_config") as reload_cfg, \
             patch("logger.log") as mock_log:
            self.assertTrue(dryrun_handle_callback(
                _query(DRYRUN_CALLBACK_PREFIX, missing_chat=True)
            ))
            reload_cfg.assert_not_called()
            mock_log.assert_any_call("dryrun_ok callback missing chat id", "WARNING")
        self.assertEqual(ctx.get_context(OPERATOR)["command"], "buy")
        self.assertEqual(ctx.get_context(CHAT)["command"], "dryrun")

    def test_gate_path_unchanged(self):
        """#448 must not wrap /gate in the dryrun confirm."""
        import inspect

        from notifications.telegram_commands import gate_commands as gcmod

        src = inspect.getsource(gcmod.handle)
        self.assertIn('_gate_section("Mainnet (Live)"', src)
        self.assertIn("_prompt_dryrun_confirm()", src)
        gate_branch = src.split("if text not in", 1)[1]
        self.assertNotIn("_prompt_dryrun_confirm", gate_branch)
        self.assertNotIn("_execute_dryrun", gate_branch)


if __name__ == "__main__":
    unittest.main()
