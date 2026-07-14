import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.models import SignalAnalysis
from services.audit_trail import AuditTrail
from services.observability_store import append_jsonl


class TestDecisionsCommands(unittest.TestCase):
    def _immediate_thread(self):
        captured = {}

        def factory(*, target=None, args=(), kwargs=None, **kw):
            captured["target"] = target
            captured["args"] = args
            captured["kwargs"] = kwargs or {}

            class _T:
                def start(self):
                    captured["target"](*captured["args"], **captured["kwargs"])

            return _T()

        return captured, factory

    def test_why_dispatches_loading_then_background_reply(self):
        from notifications.telegram_commands import decisions_commands

        captured, thread_factory = self._immediate_thread()
        messages: list[str] = []

        def _capture(msg, **kwargs):
            messages.append(msg)

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "decisions.jsonl"
            append_jsonl(
                str(log_path),
                {
                    "symbol": "BTC/USDT",
                    "action": "HOLD",
                    "normalized_action": "HOLD",
                    "rationale": "Grid monitoring",
                    "timestamp": "2026-07-14T12:00:00",
                },
            )
            with patch("notifications.telegram_commands.decisions_commands.DECISIONS_LOG_FILE", str(log_path)), \
                 patch("notifications.telegram_commands.decisions_commands.send_telegram_message", side_effect=_capture), \
                 patch("notifications.telegram_commands.decisions_commands.threading.Thread", side_effect=thread_factory), \
                 patch("notifications.telegram_commands.decisions_commands.current_chat_id", return_value="651111"), \
                 patch(
                     "notifications.telegram_commands.decisions_commands.tenant_snapshot",
                     return_value=("henry", "demo", "651111"),
                 ), \
                 patch("notifications.telegram_commands.decisions_commands.resolve_coin_config", return_value={}):
                self.assertTrue(decisions_commands.handle("/why BTC"))

        self.assertGreaterEqual(len(messages), 2)
        self.assertIn("geladen", messages[0].lower())
        self.assertIn("Warum", messages[1])
        self.assertIn("BTC", messages[1])
        self.assertIn("Grid monitoring", messages[1])

    def test_decisions_symbol_routes_to_async_why(self):
        from notifications.telegram_commands import decisions_commands

        with patch.object(decisions_commands, "_dispatch_why_async", return_value=True) as dispatch:
            self.assertTrue(decisions_commands.handle("/decisions ETH"))
        dispatch.assert_called_once_with("ETH")

    def test_decisions_list_still_async(self):
        from notifications.telegram_commands import decisions_commands

        captured, thread_factory = self._immediate_thread()
        with patch("notifications.telegram_commands.decisions_commands.send_telegram_message") as send, \
             patch("notifications.telegram_commands.decisions_commands.threading.Thread", side_effect=thread_factory), \
             patch("notifications.telegram_commands.decisions_commands.current_chat_id", return_value="1"), \
             patch(
                 "notifications.telegram_commands.decisions_commands.tenant_snapshot",
                 return_value=("default", "demo", "1"),
             ), \
             patch.object(decisions_commands, "_build_decisions_list") as build:
            self.assertTrue(decisions_commands.handle("/decisions"))
        send.assert_called()
        self.assertIn("Entscheidungen", send.call_args[0][0])
        build.assert_called_once()

    def test_find_latest_decision_uses_tail_order(self):
        from notifications.telegram_commands.decisions_commands import _find_latest_decision

        entries = [
            {"symbol": "BTC/USDT", "rationale": "old"},
            {"symbol": "BTC/USDT", "rationale": "new"},
        ]
        match = _find_latest_decision("btc", entries)
        self.assertEqual(match["rationale"], "new")


class TestAuditTrailTenant(unittest.TestCase):
    def test_record_includes_tenant_id_and_ledger_scope(self):
        analysis = SignalAnalysis(
            action="HOLD",
            symbol="TST/USDT",
            timeframe="4h",
            rsi=50.0,
            lower_bb=1.0,
            vol_multiplier=1.0,
            ampel_emoji="🟡",
            ampel_text="neutral",
            normalized_action="HOLD",
            rationale="test",
        )

        class _Cfg:
            raw = {"observability": {"decisions_audit": True}}
            trading_mode = "demo"

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "decisions.jsonl"
            with patch("logger.DECISIONS_LOG_FILE", str(log_path)), \
                 patch("services.observability_store.mongo_sync_enabled", return_value=False), \
                 patch("services.observability_store.persist_decision"), \
                 patch("strategies.positions.get_position", return_value={"amount": 0}), \
                 patch("core.tenant_context.resolve_tenant_id", return_value="henry"), \
                 patch("core.tenant_context.resolve_tenant_scope", return_value="demo"):
                AuditTrail(config=_Cfg()).record(
                    {"symbol": "TST/USDT", "timeframe": "4h"},
                    analysis,
                    price=1.0,
                )
            rec = json.loads(log_path.read_text(encoding="utf-8").strip())
            self.assertEqual(rec["tenant_id"], "henry")
            self.assertEqual(rec["ledger_scope"], "demo")


if __name__ == "__main__":
    unittest.main()