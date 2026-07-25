"""P0 Telegram command smokes — offline, capture outbound messages (T1).

No api.telegram.org. Uses dispatch_command + TelegramCapture.
Related: GitHub #133 · plans/telegram-headless-testing.md
"""

from __future__ import annotations

import pytest

from notifications.telegram_commands.router import dispatch_callback, dispatch_command
from tests.support.telegram_capture import any_text_contains, clear, install_telegram_capture


@pytest.fixture
def tg(monkeypatch):
    """Capture all outbound Telegram traffic for one test."""
    # Force sync notification path if architecture tries async bus
    try:
        from core.config import get_bot_config

        raw = dict(get_bot_config().raw)
        arch = dict(raw.get("architecture") or {})
        arch["notification_mode"] = "sync"
        raw["architecture"] = arch

        class _Cfg:
            def __init__(self, r):
                self.raw = r
                self.architecture_config = arch

        monkeypatch.setattr("core.config.get_bot_config", lambda *a, **k: _Cfg(raw))
    except Exception:
        pass

    out = install_telegram_capture(monkeypatch)
    return out


class TestTelegramP0Smokes:
    def test_help_sends_message(self, tg):
        assert dispatch_command("/help") is True
        assert tg, "expected at least one outbound message"
        assert any_text_contains(tg, "help", "befehl", "command", "/")

    def test_unknown_command_hint(self, tg):
        assert dispatch_command("/this_command_does_not_exist_xyz") is True
        assert tg
        # unknown handler uses usage_hints
        assert any_text_contains(tg, "unbekannt", "unknown", "help", "nicht", "/")

    def test_mode_command(self, tg):
        assert dispatch_command("/mode") is True
        assert tg
        assert any_text_contains(tg, "mode", "modus", "paper", "live", "demo", "trading")

    def test_list_or_watchlist(self, tg):
        # Either /list or /watchlist should be handled without crash
        ok = dispatch_command("/list") or dispatch_command("/watchlist")
        assert ok is True
        assert tg

    def test_positions_read_only(self, tg):
        assert dispatch_command("/positions") is True
        assert tg  # empty book still sends a message

    def test_orders_read_only(self, tg):
        assert dispatch_command("/orders") is True
        assert tg

    def test_non_slash_ignored(self, tg):
        assert dispatch_command("hello world") is False
        assert not tg

    def test_dispatch_does_not_raise_on_empty(self, tg):
        assert dispatch_command("") is False
        assert dispatch_command(None) is False  # type: ignore[arg-type]


class TestTelegramCallbackSmoke:
    def test_menu_callback_safe(self, tg):
        """Unknown/empty callback should not crash (may return False)."""
        q = {"id": "1", "data": "menu:main", "message": {"chat": {"id": 123}, "message_id": 1}}
        try:
            dispatch_callback(q)
        except Exception as e:
            pytest.fail(f"dispatch_callback raised: {e}")
