import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.runtime_identity import (
    format_identity_section,
    message_prefix,
    resolve_bot_stack,
    stack_badge,
)


class TestRuntimeIdentity(unittest.TestCase):
    def test_resolve_bot_stack_explicit(self):
        with patch.dict(os.environ, {"BOT_STACK": "test"}, clear=False):
            self.assertEqual(resolve_bot_stack(), "test")
        with patch.dict(os.environ, {"BOT_STACK": "production"}, clear=False):
            self.assertEqual(resolve_bot_stack(), "production")

    def test_resolve_bot_stack_from_service_name(self):
        with patch.dict(
            os.environ,
            {"BOT_STACK": "", "RAILWAY_SERVICE_NAME": "xagent-test"},
            clear=False,
        ):
            self.assertEqual(resolve_bot_stack(), "test")

    def test_stack_badge(self):
        self.assertIn("TEST", stack_badge("test"))
        self.assertIn("PROD", stack_badge("production"))

    def test_message_prefix_uses_stack(self):
        with patch.dict(os.environ, {"BOT_STACK": "test"}, clear=False):
            self.assertIn("[TEST]", message_prefix())

    def test_format_identity_section_contains_commit(self):
        with patch.dict(
            os.environ,
            {"BOT_STACK": "test", "GIT_COMMIT": "abc1234", "GIT_BRANCH": "main"},
            clear=False,
        ), patch("core.runtime_identity._feature_flags", return_value={"redis": True, "price_cache": True, "ohlcv_cache": True, "signal_webhook": True, "coin_webhook": True}):
            section = format_identity_section()
        self.assertIn("abc1234", section)
        self.assertIn("main", section)
        self.assertIn("Instanz", section)


if __name__ == "__main__":
    unittest.main()