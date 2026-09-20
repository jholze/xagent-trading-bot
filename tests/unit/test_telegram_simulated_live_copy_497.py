"""#497 — Telegram /gate and /mode copy match Simulated Live; /live_cancel un-confirms.

Nothing here writes into ``data/``: persist is patched, Gate is not called.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.simulated_trading import simulated_live_config_updates
from notifications.telegram_commands import gate_commands, mode_commands
from notifications.telegram_commands.menu_i18n import (
    build_section_help_message,
    command_help_line,
    command_hint,
    current_language,
    reload_menu_data,
    set_user_language,
)
from notifications.telegram_commands.usage_hints import clear_usage_cache

ROOT = Path(__file__).resolve().parents[2]
MENU_PATH = ROOT / "locales" / "telegram_menu.json"

_STALE_COPY = (
    "JSON-Ledger",
    "Lokales Paper",
    "Echtes Spot-Trading",
    "live.dry_run: false",
    "Virtuelles Geld",
    "Echtes Geld auf Gate.io",
    "Mainnet bestätigen",
    "zurück zu Paper",
    "Virtual money",
    "Real money on Gate.io",
    "Confirm live trading on Gate.io mainnet",
    "back to paper",
)


def _reload_locales():
    reload_menu_data()
    clear_usage_cache()


@pytest.fixture(autouse=True)
def _restore_ui_language():
    prev = current_language()
    yield
    set_user_language(prev)


def _menu() -> dict:
    return json.loads(MENU_PATH.read_text(encoding="utf-8"))


def test_menu_json_stays_valid():
    data = _menu()
    assert set(data) >= {"de", "en"}


@pytest.mark.parametrize("lang", ("de", "en"))
def test_mode_hint_points_paper_at_simulated_live(lang):
    _reload_locales()
    hint = command_hint("mode", lang)
    for stale in _STALE_COPY:
        assert stale not in hint, f"{lang} mode.hint still has {stale!r}"
    assert "Simulated Live" in hint
    assert "paper" in hint
    if lang == "de":
        assert "veraltet" in hint
        assert "Echtes Geld" not in hint
    else:
        assert "deprecated" in hint
        assert "Real money" not in hint


@pytest.mark.parametrize("lang", ("de", "en"))
def test_live_confirm_help_is_simulated_live_without_mainnet(lang):
    _reload_locales()
    line = command_help_line("live_confirm", lang)
    for stale in _STALE_COPY:
        assert stale not in line, f"{lang} live_confirm.help_line still has {stale!r}"
    assert "Simulated Live" in line
    assert "mainnet" not in line.lower()
    assert "Mainnet" not in line


@pytest.mark.parametrize("lang", ("de", "en"))
def test_live_cancel_help_is_simulated_live_not_paper(lang):
    _reload_locales()
    line = command_help_line("live_cancel", lang)
    for stale in _STALE_COPY:
        assert stale not in line, f"{lang} live_cancel.help_line still has {stale!r}"
    assert "Simulated Live" in line
    assert "paper" not in line.lower()


@pytest.mark.parametrize("lang", ("de", "en"))
def test_modus_section_help_live_confirm_cancel_has_no_stale_copy(lang):
    _reload_locales()
    msg = build_section_help_message(
        "modus", lang, command_keys=["live_confirm", "live_cancel"]
    )
    for stale in _STALE_COPY:
        assert stale not in msg, f"{lang} modus section help still has {stale!r}"
    assert "Simulated Live" in msg


def test_gate_mainnet_pane_title_unchanged():
    src = inspect.getsource(gate_commands.handle)
    assert '_gate_section("Mainnet (Live)"' in src


def test_gate_footer_is_simulated_live_not_paper_or_real_orders():
    cfg = MagicMock()
    cfg.trading_mode = "live"
    cfg.live_config = {"dry_run": True}
    cfg.raw = {}
    trading = MagicMock()
    trading.mode_label.return_value = "Simulated Live"

    with patch.object(gate_commands, "reload_config"), \
         patch.object(gate_commands, "get_bot_config", return_value=cfg), \
         patch.object(gate_commands, "TradingService", return_value=trading), \
         patch.object(gate_commands, "GateExecutionAdapter") as adapter_cls, \
         patch.object(
             gate_commands,
             "_gate_section",
             return_value="<b>Mainnet (Live)</b>\nUSDT verfügbar: <b>$1.00</b>",
         ) as pane, \
         patch.object(gate_commands, "fetch_spot_holdings", return_value=[]), \
         patch.object(gate_commands, "send_telegram_message") as send, \
         patch("data_manager.is_demo_mode", return_value=False):
        assert gate_commands.handle("/gate") is True

    pane.assert_called_once()
    assert pane.call_args[0][0] == "Mainnet (Live)"
    assert pane.call_args[0][1] is cfg.live_config
    assert pane.call_args[0][2] is adapter_cls.return_value
    msg = send.call_args[0][0]
    assert "<b>Mainnet (Live)</b>" in msg
    assert "Simulated Live" in msg
    assert "veraltet" in msg
    for stale in _STALE_COPY:
        assert stale not in msg, f"/gate footer still has {stale!r}"
    assert "echte Orders" not in msg


def test_live_cancel_persists_unconfirmed_simulated_live(monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    saved: list[dict] = []

    def _fake_save(updates):
        saved.append(dict(updates))
        return True

    cfg = {
        "trading_mode": "live",
        "live_confirmed": True,
        "live": {"dry_run": True},
    }
    with patch.object(mode_commands, "get_config", return_value=cfg), \
         patch.object(mode_commands, "_save_mode_updates", side_effect=_fake_save) as mock_save, \
         patch.object(mode_commands, "reload_config"), \
         patch.object(mode_commands, "on_trading_mode_change", return_value=""), \
         patch.object(mode_commands, "send_telegram_message") as send:
        assert mode_commands.handle("/live_cancel") is True

    mock_save.assert_called_once()
    body = saved[0]
    helper = simulated_live_config_updates()
    assert helper["live_confirmed"] is True
    assert body["live_confirmed"] is False
    assert body["trading_mode"] == "live"
    assert body["virtual_trading"] is False
    assert body["live"] == {"dry_run": True}
    assert body != helper
    assert "Simulated Live" in send.call_args[0][0]
