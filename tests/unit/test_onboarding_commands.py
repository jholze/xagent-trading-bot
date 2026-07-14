"""Unit tests for onboarding_commands parser and handle flows."""

import os
import unittest
from unittest.mock import patch

from notifications.telegram_commands import onboarding_commands as oc


class TestLooksLikeCredential(unittest.TestCase):
    def test_telegram_token_looks_good(self):
        self.assertTrue(oc._looks_like_credential("123456:AAHfakeBOTTOKEN1234567890ABCDEF"))

    def test_long_gate_style(self):
        self.assertTrue(oc._looks_like_credential("GATEKEY1234567890ABCDEFHIJKLMNOPQR12345"))
        self.assertTrue(oc._looks_like_credential("GATESECRET9876543210ZYXWVUTSRQPONML"))

    def test_too_short_rejected(self):
        self.assertFalse(oc._looks_like_credential("short"))


class TestTenantIdValidation(unittest.TestCase):
    def test_valid_ids(self):
        self.assertTrue(oc._is_valid_tenant_id("henry"))
        self.assertTrue(oc._is_valid_tenant_id("user_123"))

    def test_invalid_ids(self):
        self.assertFalse(oc._is_valid_tenant_id("a"))
        self.assertFalse(oc._is_valid_tenant_id(""))
        self.assertFalse(oc._is_valid_tenant_id("bad-id"))


class TestParseOnboardingMessage(unittest.TestCase):
    def test_paper_single_line(self):
        data = oc._parse_onboarding_message("henry")
        self.assertEqual(data["tenant_id"], "henry")
        self.assertEqual(data.get("paper_only"), "1")

    def test_paper_with_chat_id(self):
        data = oc._parse_onboarding_message("henry\n123456789")
        self.assertEqual(data["tenant_id"], "henry")
        self.assertEqual(data["owner_chat_id"], "123456789")

    def test_full_credentials_4_lines(self):
        text = (
            "max\n123456:AAHfakeBOTTOKEN1234567890ABCDEF\n"
            "GATEKEY1234567890ABCDEF1234567890ABCD\n"
            "GATESECRET9876543210ZYXWVUTSRQPONMLKJIHGFEDCB"
        )
        data = oc._parse_onboarding_message(text)
        self.assertEqual(data["tenant_id"], "max")
        self.assertEqual(data["bot_token"], "123456:AAHfakeBOTTOKEN1234567890ABCDEF")

    def test_kv_partial(self):
        text = "tenant: anna\nbot_token: 123456:AAHxxx\nkey: GK\nsecret: GS"
        data = oc._parse_onboarding_message(text)
        self.assertEqual(data["tenant_id"], "anna")
        self.assertEqual(data["bot_token"], "123456:AAHxxx")


class TestHandleOperatorFlows(unittest.TestCase):
    def setUp(self):
        self.patcher_chat = patch(
            "notifications.telegram_commands.onboarding_commands.current_chat_id",
            return_value="123456789",
        )
        self.patcher_chat.start()
        os.environ["TELEGRAM_CHAT_ID"] = "123456789"
        os.environ["TELEGRAM_BOT_TOKEN"] = "999888:AAHsharedBOTTOKEN1234567890ABCDEF"

        from notifications.telegram_commands import command_context as cc
        try:
            cc.clear_context("123456789")
        except Exception:
            pass

    def tearDown(self):
        self.patcher_chat.stop()
        os.environ.pop("TELEGRAM_CHAT_ID", None)
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)

    @patch("notifications.telegram_commands.onboarding_commands._perform_onboard", return_value=True)
    def test_onboard_henry_command(self, mock_perform):
        self.assertTrue(oc.handle("/onboard henry"))
        mock_perform.assert_called_once_with("henry", paper_only=True)

    @patch("notifications.telegram_commands.onboarding_commands._perform_onboard", return_value=True)
    def test_onboard_henry_with_chat(self, mock_perform):
        self.assertTrue(oc.handle("/onboard henry 555666777"))
        mock_perform.assert_called_once_with(
            "henry", owner_chat_id="555666777", paper_only=True
        )

    @patch("notifications.telegram_commands.onboarding_commands._perform_onboard", return_value=True)
    def test_plain_henry_paper(self, mock_perform):
        self.assertTrue(oc.handle("henry"))
        mock_perform.assert_called_once()
        self.assertEqual(mock_perform.call_args[0][0], "henry")
        self.assertTrue(mock_perform.call_args[1].get("paper_only"))

    @patch("notifications.telegram_commands.onboarding_commands._perform_onboard", return_value=True)
    @patch("notifications.telegram_commands.onboarding_commands.send_telegram_message")
    def test_plain_message_junk_does_not_perform(self, mock_send, mock_perform):
        self.assertFalse(oc.handle("Just chatting about the weather today."))
        mock_send.assert_not_called()
        mock_perform.assert_not_called()

    def test_help_commands_are_never_swallowed(self):
        self.assertFalse(oc.handle("/help"))
        self.assertFalse(oc.handle("/help onboarding"))

    @patch("notifications.telegram_commands.onboarding_commands.set_context")
    @patch("notifications.telegram_commands.onboarding_commands.send_telegram_message")
    def test_onboard_command_starts_interactive(self, mock_send, mock_set_ctx):
        self.assertTrue(oc.handle("/onboard"))
        mock_set_ctx.assert_called_once()
        self.assertIn("Paper", mock_send.call_args[0][0])

    @patch("notifications.telegram_commands.onboarding_commands._perform_onboard", return_value=True)
    @patch("notifications.telegram_commands.onboarding_commands.get_context")
    @patch("notifications.telegram_commands.onboarding_commands.clear_context")
    @patch("notifications.telegram_commands.onboarding_commands.send_telegram_message")
    def test_interactive_paper_skip_gate(self, mock_send, mock_clear, mock_get_ctx, mock_perform):
        mock_get_ctx.return_value = {"command": "onboarding", "meta": {"step": "tenant_id", "data": {}}}
        self.assertTrue(oc.handle("henry"))

        mock_get_ctx.return_value = {
            "command": "onboarding",
            "meta": {"step": "owner_chat_id", "data": {"tenant_id": "henry"}},
        }
        self.assertTrue(oc.handle("skip"))

        mock_get_ctx.return_value = {
            "command": "onboarding",
            "meta": {"step": "bot_token", "data": {"tenant_id": "henry"}},
        }
        self.assertTrue(oc.handle("skip"))

        mock_get_ctx.return_value = {
            "command": "onboarding",
            "meta": {"step": "gate_key", "data": {"tenant_id": "henry"}},
        }
        self.assertTrue(oc.handle("skip"))

        mock_get_ctx.return_value = {
            "command": "onboarding",
            "meta": {"step": "gate_secret", "data": {"tenant_id": "henry"}},
        }
        self.assertTrue(oc.handle("skip"))

        mock_perform.assert_called_once()
        kwargs = mock_perform.call_args[1]
        self.assertTrue(kwargs.get("paper_only"))
        mock_clear.assert_called_once()


class TestPerformOnboardPaper(unittest.TestCase):
    @patch("notifications.telegram_commands.onboarding_commands.send_telegram_message")
    @patch("notifications.telegram_commands.onboarding_commands.send_message_with_bot_token")
    @patch("notifications.telegram_commands.onboarding_commands.set_webhook_for_bot")
    @patch("notifications.telegram_commands.onboarding_commands.create_tenant")
    @patch("data_manager.save_watchlist")
    @patch("data_manager.save_config")
    @patch("core.tenant_context.tenant_context")
    @patch("notifications.telegram_commands.onboarding_commands.current_chat_id", return_value="111")
    def test_paper_uses_shared_bot_no_webhook(
        self,
        _chat,
        _ctx,
        _save_cfg,
        _save_wl,
        mock_create,
        mock_webhook,
        _welcome,
        _confirm,
    ):
        os.environ["TELEGRAM_BOT_TOKEN"] = "999:AAHsharedBOTTOKEN1234567890ABCDEFGH"
        try:
            oc._perform_onboard("henry", paper_only=True)
        finally:
            os.environ.pop("TELEGRAM_BOT_TOKEN", None)

        mock_create.assert_called_once()
        kwargs = mock_create.call_args[1]
        self.assertEqual(kwargs["gate_api_key"], "")
        self.assertEqual(kwargs["gate_api_secret"], "")
        self.assertEqual(kwargs["bot_token"], "")
        mock_webhook.assert_not_called()


if __name__ == "__main__":
    unittest.main()