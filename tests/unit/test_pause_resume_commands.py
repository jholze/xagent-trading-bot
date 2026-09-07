"""#305 slice 3: /pause and /resume flip only entries_enabled and persist."""

from __future__ import annotations

from unittest.mock import patch

from notifications.telegram_commands.mode_commands import save_trading_flags
from notifications.telegram_commands.pause_commands import handle


def test_pause_sets_entries_false_keeps_exits_and_persists():
    captured = {}

    def _save(updates):
        captured.update(updates)
        return True

    cfg = {"trading": {"entries_enabled": True, "exits_enabled": True}, "trading_mode": "paper"}
    with patch("notifications.telegram_commands.mode_commands.get_config", return_value=cfg), \
         patch("notifications.telegram_commands.mode_commands._save_mode_updates", side_effect=_save) as save, \
         patch("notifications.telegram_commands.mode_commands.reload_config") as reload, \
         patch("notifications.telegram_commands.pause_commands.send_telegram_message") as send, \
         patch("notifications.telegram_commands.pause_commands.get_bot_config") as gbc:
        gbc.return_value.exits_enabled = True
        assert handle("/pause") is True
    save.assert_called_once()
    reload.assert_called_once()
    trading = captured["trading"]
    assert trading["entries_enabled"] is False
    assert trading["exits_enabled"] is True
    msg = send.call_args[0][0]
    assert "Entries" in msg
    assert "Exits" in msg
    assert "AUS" in msg
    assert "AN" in msg


def test_resume_sets_entries_true_keeps_exits():
    captured = {}

    def _save(updates):
        captured.update(updates)
        return True

    cfg = {"trading": {"entries_enabled": False, "exits_enabled": False}, "trading_mode": "paper"}
    with patch("notifications.telegram_commands.mode_commands.get_config", return_value=cfg), \
         patch("notifications.telegram_commands.mode_commands._save_mode_updates", side_effect=_save), \
         patch("notifications.telegram_commands.mode_commands.reload_config"), \
         patch("notifications.telegram_commands.pause_commands.send_telegram_message") as send, \
         patch("notifications.telegram_commands.pause_commands.get_bot_config") as gbc:
        gbc.return_value.exits_enabled = False
        assert handle("/resume") is True
    trading = captured["trading"]
    assert trading["entries_enabled"] is True
    assert trading["exits_enabled"] is False
    msg = send.call_args[0][0]
    assert "Entries" in msg
    assert "AUS" in msg  # exits still off


def test_save_trading_flags_uses_mode_save_path():
    cfg = {"trading": {"entries_enabled": True, "exits_enabled": True}, "other": 1}
    with patch("notifications.telegram_commands.mode_commands.get_config", return_value=cfg), \
         patch("notifications.telegram_commands.mode_commands._save_mode_updates", return_value=True) as save, \
         patch("notifications.telegram_commands.mode_commands.reload_config"):
        assert save_trading_flags(entries_enabled=False) is True
    save.assert_called_once_with(
        {"trading": {"entries_enabled": False, "exits_enabled": True}}
    )


def test_router_registers_pause_next_to_mode():
    from notifications.telegram_commands import mode_commands, pause_commands
    from notifications.telegram_commands.router import _HANDLERS

    assert _HANDLERS.index(pause_commands.handle) == _HANDLERS.index(mode_commands.handle) + 1
