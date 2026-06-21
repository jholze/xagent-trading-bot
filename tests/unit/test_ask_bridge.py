import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from services import telegram_ask_bridge as bridge


class TestAskBridge(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self._testMethodName + "_queue.json")
        self.cfg_patch = patch.object(
            bridge,
            "_cfg",
            return_value={
                "enabled": True,
                "response_mode": "grok_fallback",
                "queue_path": str(self.tmp),
                "cursor_priority_sec": 8,
                "cursor_timeout_sec": 120,
                "auto_respond_enabled": True,
                "grok_fallback_enabled": True,
                "rate_limit_per_hour": 20,
                "max_question_length": 500,
            },
        )
        self.cfg_patch.start()
        self.addCleanup(self.cfg_patch.stop)
        if self.tmp.exists():
            self.tmp.unlink()

    def tearDown(self):
        if self.tmp.exists():
            self.tmp.unlink()

    @patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "999"})
    def test_enqueue_and_submit_answer(self):
        notify_path = Path("test_notify_pending.json")
        with patch.object(bridge, "pending_notify_path", return_value=notify_path):
            qid, err = bridge.enqueue_question("999", "Warum NEAR?")
        self.assertFalse(err)
        self.assertTrue(qid)
        self.assertTrue(notify_path.is_file())
        notify_path.unlink(missing_ok=True)

        pending = bridge.list_pending_questions()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["id"], qid)

        ok, err = bridge.submit_answer(qid, "LC-Signal mit AltRank-Sprung.")
        self.assertTrue(ok)
        self.assertEqual(err, "")

        pending = bridge.list_pending_questions()
        self.assertEqual(len(pending), 0)

    @patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "999"})
    def test_unauthorized_chat_rejected(self):
        qid, err = bridge.enqueue_question("123", "Hallo?")
        self.assertIsNone(qid)
        self.assertIn("autorisiert", err.lower())

    @patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "999"})
    @patch("telegram_notifier.send_telegram_message")
    def test_deliver_answer_to_telegram(self, mock_send):
        mock_send.return_value = True
        qid, _ = bridge.enqueue_question("999", "Test?")
        bridge.submit_answer(qid, "Antwort hier.")
        bridge._tick_once()

        mock_send.assert_called()
        data = json.loads(self.tmp.read_text(encoding="utf-8"))
        item = data["questions"][0]
        self.assertEqual(item["status"], "delivered")
        self.assertTrue(item.get("delivered_at"))

    @patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "999"})
    def test_grok_fallback_after_timeout(self):
        qid, _ = bridge.enqueue_question("999", "Timeout test?")
        data = json.loads(self.tmp.read_text(encoding="utf-8"))
        old = datetime.now(timezone.utc) - timedelta(seconds=200)
        data["questions"][0]["created_at"] = old.isoformat()
        self.tmp.write_text(json.dumps(data), encoding="utf-8")

        with patch.object(bridge, "_grok_fallback_answer", return_value="Grok sagt hallo."):
            with patch("telegram_notifier.send_telegram_message", return_value=True):
                bridge._tick_once()

        data = json.loads(self.tmp.read_text(encoding="utf-8"))
        item = data["questions"][0]
        self.assertEqual(item["answered_by"], "grok")
        self.assertIn("Grok", item["answer"])


    def test_format_cursor_notify_marker(self):
        payload = {"id": "abc", "question": "Test?"}
        line = bridge.format_cursor_notify(payload)
        self.assertIn(bridge.CURSOR_NOTIFY_MARKER, line)
        self.assertIn("abc", line)

    def test_format_agent_action_marker(self):
        payload = {"id": "xyz", "question": "Hallo?", "response_mode": "cursor_only"}
        line = bridge.format_agent_action(payload)
        self.assertIn(bridge.CURSOR_ACTION_MARKER, line)
        self.assertIn("xyz", line)

    def test_extract_urls_from_question(self):
        urls = bridge._extract_urls("check https://dex.coinmarketcap.com/token/solana/ABC")
        self.assertEqual(len(urls), 1)
        self.assertIn("coinmarketcap", urls[0])

    def test_extract_symbols_skips_german_stopwords(self):
        symbols = bridge._extract_symbols("wie gehts dir heute?")
        self.assertEqual(symbols, [])
        self.assertEqual(bridge._extract_symbols("wie ist der markt heute"), [])

    def test_cursor_only_disables_grok(self):
        with patch.object(
            bridge,
            "_cfg",
            return_value={
                "response_mode": "cursor_only",
                "grok_fallback_enabled": False,
                "auto_respond_enabled": False,
            },
        ):
            self.assertEqual(bridge._response_mode(), "cursor_only")
            self.assertFalse(bridge.grok_fallback_enabled())
            self.assertFalse(bridge.auto_respond_enabled())

    @patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "999"})
    def test_auto_answer_blocked_in_cursor_only(self):
        with patch.object(
            bridge,
            "_cfg",
            return_value={
                "enabled": True,
                "response_mode": "cursor_only",
                "queue_path": str(self.tmp),
                "grok_fallback_enabled": False,
                "auto_respond_enabled": False,
            },
        ):
            qid, _ = bridge.enqueue_question("999", "Test?")
            ok, err = bridge.auto_answer_if_pending(qid)
        self.assertFalse(ok)
        self.assertIn("cursor_only", err)

    @patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "999"})
    def test_auto_answer_if_pending(self):
        qid, _ = bridge.enqueue_question("999", "Kurze Frage?")
        with patch.object(bridge, "_grok_fallback_answer", return_value="Auto-Antwort."):
            ok, err = bridge.auto_answer_if_pending(qid)
        self.assertTrue(ok)
        self.assertEqual(err, "")

        data = json.loads(self.tmp.read_text(encoding="utf-8"))
        item = data["questions"][0]
        self.assertEqual(item["status"], "answered")
        self.assertEqual(item["answered_by"], "grok")
        self.assertEqual(item["answer"], "Auto-Antwort.")

    @patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "999"})
    def test_auto_answer_skips_when_already_answered(self):
        qid, _ = bridge.enqueue_question("999", "Schon beantwortet?")
        bridge.submit_answer(qid, "Manuell.")
        ok, err = bridge.auto_answer_if_pending(qid)
        self.assertFalse(ok)
        self.assertIn("pending", err.lower())


if __name__ == "__main__":
    unittest.main()