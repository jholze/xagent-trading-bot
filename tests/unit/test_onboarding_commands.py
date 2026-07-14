"""Unit tests for onboarding_commands parser and handle flows.

Focus: make the flexible private-message onboarding robust and testable.
"""

import os
import unittest
from unittest.mock import patch, MagicMock

from notifications.telegram_commands import onboarding_commands as oc


class TestLooksLikeCredential(unittest.TestCase):
    def test_telegram_token_looks_good(self):
        self.assertTrue(oc._looks_like_credential("123456:AAHfakeBOTTOKEN1234567890ABCDEF"))

    def test_long_gate_style(self):
        self.assertTrue(oc._looks_like_credential("GATEKEY1234567890ABCDEFHIJKLMNOPQR12345"))
        self.assertTrue(oc._looks_like_credential("GATESECRET9876543210ZYXWVUTSRQPONML"))

    def test_too_short_rejected(self):
        self.assertFalse(oc._looks_like_credential("short"))
        self.assertFalse(oc._looks_like_credential("12345678901234"))

    def test_no_colon_and_shortish_rejected(self):
        self.assertFalse(oc._looks_like_credential("someordinarytextwithoutmuchlengthatall"))
        # Even long all-lowercase no digits/upper should be rejected
        self.assertFalse(oc._looks_like_credential("thisisjustarandomenglishsentencewithoutanydigitsoruppercaselettersatall"))


class TestLooksLikeOnboardingData(unittest.TestCase):
    def test_good_4line_with_name(self):
        text = "max\n123456:AAHfakeBOTTOKEN1234567890ABCDEF\nGATEKEY1234567890ABCDEF1234567890ABCD\nGATESECRET9876543210ZYXWVUTSRQPONMLKJIHGFEDCB"
        self.assertTrue(oc._looks_like_onboarding_data(text))

    def test_good_3line_positional(self):
        text = "123456:AAHfakeBOTTOKEN1234567890ABCDEF\nGATEKEY1234567890ABCDEF1234567890ABCD\nGATESECRET9876543210ZYXWVUTSRQPONMLKJIHGFEDCB"
        self.assertTrue(oc._looks_like_onboarding_data(text))

    def test_good_kv_style(self):
        text = "token: 123456:AAHfakeBOTTOKEN1234567890ABCDEF\nkey: GATEKEY1234567890ABCDEF1234567890ABCD\nsecret: GATESECRET9876543210ZYXWVUTSRQPONMLKJIHGFEDCB"
        self.assertTrue(oc._looks_like_onboarding_data(text))

    def test_kv_with_labels(self):
        text = "bot token: 123456:AAHfakeBOTTOKEN1234567890ABCDEF\nGate Key: GATEKEY1234567890ABCDEF1234567890ABCD\nGate Secret: GATESECRET9876543210ZYXWVUTSRQPONMLKJIHGFEDCB"
        self.assertTrue(oc._looks_like_onboarding_data(text))

    def test_rejects_normal_conversation(self):
        self.assertFalse(oc._looks_like_onboarding_data("Hey, how are you doing today?"))
        self.assertFalse(oc._looks_like_onboarding_data("Just testing some random stuff here and there."))

    def test_rejects_4_short_lines(self):
        self.assertFalse(oc._looks_like_onboarding_data("hi\nthere\nfoo\nbar"))

    def test_rejects_3_lines_without_real_creds(self):
        self.assertFalse(oc._looks_like_onboarding_data("hello world\nfoo bar\nbaz quux"))

    def test_rejects_help_text(self):
        self.assertFalse(oc._looks_like_onboarding_data("/help onboarding"))
        self.assertFalse(oc._looks_like_onboarding_data("Please send /help for more info"))

    def test_ignores_onboard_prefix(self):
        text = "onboard max\n123456:AAHfakeBOTTOKEN1234567890ABCDEF\nGATEKEY1234567890ABCDEF1234567890ABCD\nGATESECRET9876543210ZYXWVUTSRQPONMLKJIHGFEDCB"
        self.assertTrue(oc._looks_like_onboarding_data(text))


class TestParseOnboardingMessage(unittest.TestCase):
    def test_kv_full(self):
        text = "tenant: anna\nbot_token: 123456:AAHxxx\nkey: GK\nsecret: GS"
        data = oc._parse_onboarding_message(text)
        self.assertEqual(data["tenant_id"], "anna")
        self.assertEqual(data["bot_token"], "123456:AAHxxx")
        self.assertEqual(data["gate_key"], "GK")
        self.assertEqual(data["gate_secret"], "GS")

    def test_positional_4_lines(self):
        text = "max\n123456:AAHfakeBOTTOKEN1234567890ABCDEF\nGATEKEY1234567890ABCDEF1234567890ABCD\nGATESECRET9876543210ZYXWVUTSRQPONMLKJIHGFEDCB"
        data = oc._parse_onboarding_message(text)
        self.assertEqual(data["tenant_id"], "max")
        self.assertEqual(data["bot_token"], "123456:AAHfakeBOTTOKEN1234567890ABCDEF")

    def test_positional_3_lines(self):
        text = "123456:AAHfakeBOTTOKEN1234567890ABCDEF\nGATEKEY1234567890ABCDEF1234567890ABCD\nGATESECRET9876543210ZYXWVUTSRQPONMLKJIHGFEDCB"
        data = oc._parse_onboarding_message(text)
        self.assertNotIn("tenant_id", data)
        self.assertEqual(data["bot_token"], "123456:AAHfakeBOTTOKEN1234567890ABCDEF")

    def test_mixed_kv_and_positional_fallback(self):
        # kv partial then fallback shouldn't overwrite good data
        text = "token: 123456:AAHxxx\nGK123\nGS456"
        data = oc._parse_onboarding_message(text)
        self.assertEqual(data["bot_token"], "123456:AAHxxx")

    def test_strips_onboard_word(self):
        text = "onboard testuser 123456:AAHfakeBOTTOKEN1234567890ABCDEF GATEKEY1234567890ABCDEF1234567890ABCD GATESECRET9876543210ZYXWVUTSRQPONMLKJIHGFEDCB"
        data = oc._parse_onboarding_message(text)
        self.assertEqual(data.get("tenant_id"), "testuser")
        self.assertIn("bot_token", data)

    def test_kv_with_spaces_in_keys(self):
        text = "Bot Token: 123:AA\nGate Key: GKEY\nGate Secret: GSEC"
        data = oc._parse_onboarding_message(text)
        self.assertEqual(data["bot_token"], "123:AA")

    def test_does_not_parse_junk_as_credentials(self):
        text = "hello world how are you today friend"
        data = oc._parse_onboarding_message(text)
        # Should not populate the three keys from random words
        self.assertFalse(data.get("bot_token"))
        self.assertFalse(data.get("gate_key"))


class TestHandleOperatorFlows(unittest.TestCase):
    def setUp(self):
        self.patcher_chat = patch("notifications.telegram_commands.onboarding_commands.current_chat_id", return_value="123456789")
        self.mock_chat = self.patcher_chat.start()
        os.environ["TELEGRAM_CHAT_ID"] = "123456789"

        # Avoid cross-test pollution from real persisted command context
        from notifications.telegram_commands import command_context as cc
        try:
            cc.clear_context("123456789")
        except Exception:
            pass

    def tearDown(self):
        self.patcher_chat.stop()
        os.environ.pop("TELEGRAM_CHAT_ID", None)

    @patch("notifications.telegram_commands.onboarding_commands._perform_onboard", return_value=True)
    @patch("notifications.telegram_commands.onboarding_commands.send_telegram_message")
    def test_plain_message_good_data_calls_perform(self, mock_send, mock_perform):
        text = "anna\n123456:AAHfakeBOTTOKEN1234567890ABCDEF\nGATEKEY1234567890ABCDEF1234567890ABCD\nGATESECRET9876543210ZYXWVUTSRQPONMLKJIHGFEDCB"
        result = oc.handle(text)
        self.assertTrue(result)
        mock_perform.assert_called_once()
        # tenant id should be from first line
        self.assertEqual(mock_perform.call_args[0][0], "anna")

    @patch("notifications.telegram_commands.onboarding_commands._perform_onboard", return_value=True)
    @patch("notifications.telegram_commands.onboarding_commands.send_telegram_message")
    def test_plain_message_junk_does_not_perform_and_no_hint(self, mock_send, mock_perform):
        result = oc.handle("Just chatting about the weather and crypto today. Nothing special here at all.")
        self.assertFalse(result)
        mock_send.assert_not_called()
        mock_perform.assert_not_called()

    @patch("notifications.telegram_commands.onboarding_commands._perform_onboard", return_value=True)
    @patch("notifications.telegram_commands.onboarding_commands.send_telegram_message")
    def test_plain_message_suspicious_sends_hint(self, mock_send, mock_perform):
        # 3 lines that look a bit like creds but not quite full (should still trigger conservative hint)
        text = "maybe token here\nGATEKEY1234567890ABCDEF1234567890ABCD\nGATESECRET9876543210ZYXWVUTSRQPONMLKJIHGFEDCB"
        result = oc.handle(text)
        self.assertTrue(result)
        mock_send.assert_called_once()
        args = mock_send.call_args[0][0]
        self.assertIn("/help onboarding", args)
        mock_perform.assert_not_called()

    def test_help_commands_are_never_swallowed(self):
        self.assertFalse(oc.handle("/help"))
        self.assertFalse(oc.handle("/help onboarding"))
        self.assertFalse(oc.handle("/commands"))
        self.assertFalse(oc.handle("/menu"))

    @patch("notifications.telegram_commands.onboarding_commands.set_context")
    @patch("notifications.telegram_commands.onboarding_commands.send_telegram_message")
    def test_onboard_command_starts_interactive(self, mock_send, mock_set_ctx):
        result = oc.handle("/onboard")
        self.assertTrue(result)
        mock_set_ctx.assert_called_once()
        self.assertIn("Onboarding gestartet", mock_send.call_args[0][0])

    @patch("notifications.telegram_commands.onboarding_commands._perform_onboard", return_value=True)
    @patch("notifications.telegram_commands.onboarding_commands.get_context")
    @patch("notifications.telegram_commands.onboarding_commands.clear_context")
    @patch("notifications.telegram_commands.onboarding_commands.send_telegram_message")
    def test_interactive_continuation(self, mock_send, mock_clear, mock_get_ctx, mock_perform):
        # Simulate step by step. Because of the ctx check at top of handle,
        # long credential answers in steps should reach _continue_onboarding.
        mock_get_ctx.return_value = {"command": "onboarding", "meta": {"step": "tenant_id", "data": {}}}
        self.assertTrue(oc.handle("mytenant"))

        mock_get_ctx.return_value = {"command": "onboarding", "meta": {"step": "bot_token", "data": {"tenant_id": "mytenant"}}}
        self.assertTrue(oc.handle("123456:AAHfakeBOTTOKEN1234567890ABCDEF"))

        mock_get_ctx.return_value = {"command": "onboarding", "meta": {"step": "gate_key", "data": {"tenant_id": "mytenant", "bot_token": "123456:AAHfakeBOTTOKEN1234567890ABCDEF"}}}
        self.assertTrue(oc.handle("GATEKEY1234567890ABCDEF1234567890ABCD"))

        mock_get_ctx.return_value = {"command": "onboarding", "meta": {"step": "gate_secret", "data": {"tenant_id": "mytenant", "bot_token": "123456:AAHfakeBOTTOKEN1234567890ABCDEF", "gate_key": "GATEKEY1234567890ABCDEF1234567890ABCD"}}}
        result = oc.handle("GATESECRET9876543210ZYXWVUTSRQPONMLKJIHGFEDCB")
        self.assertTrue(result)
        mock_perform.assert_called_once()
        mock_clear.assert_called_once()

    @patch("notifications.telegram_commands.onboarding_commands.send_telegram_message")
    def test_non_operator_gets_rejected_on_explicit_onboard(self, mock_send):
        with patch("notifications.telegram_commands.onboarding_commands._is_operator", return_value=False):
            result = oc.handle("/onboard foo bar baz")
            self.assertTrue(result)
            mock_send.assert_called_with("❌ Nur der Operator kann neue User onboarden.")


if __name__ == "__main__":
    unittest.main()
