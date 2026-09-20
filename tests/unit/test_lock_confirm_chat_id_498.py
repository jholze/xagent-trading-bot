"""#498 — lock confirm callback must arm the confirming chat, not TELEGRAM_CHAT_ID.

Nothing here writes into ``data/``: persist is patched, context lives in tmp.
"""

from __future__ import annotations

import inspect
from unittest.mock import patch

import pytest

from notifications.telegram_commands import command_context as ctx
from notifications.telegram_commands import lock_commands
from notifications.telegram_commands.lock_commands import (
    consume_lock_token,
    handle_callback,
    reset_lock_confirm_for_tests,
)
from strategies.position_lock import DEFAULT_MODES
from telegram_notifier import handle_telegram_callback
from tests.support.telegram_capture import install_telegram_capture

LC = "notifications.telegram_commands.lock_commands"
OPERATOR = "100"
SATELLITE = "200"


@pytest.fixture(autouse=True)
def _reset_chat_and_tokens():
    reset_lock_confirm_for_tests()
    ctx.set_chat_id("")
    yield
    ctx.set_chat_id("")
    reset_lock_confirm_for_tests()


def _pending(**kwargs) -> str:
    lock = {
        "reason": "telegram_lock",
        "locked_by": "telegram",
        "modes": list(DEFAULT_MODES),
        "until": None,
    }
    lock.update(kwargs.pop("lock", {}))
    return lock_commands._create_lock_token(
        symbol=kwargs.get("symbol", "BLESS/USDT"),
        timeframe=kwargs.get("timeframe", "1h"),
        lock=lock,
    )


def _ok_query(token: str, chat_id=SATELLITE, *, missing_chat: bool = False) -> dict:
    q = {"id": "cb", "data": f"lock_ok:{token}"}
    if missing_chat:
        return q
    q["message"] = {"chat": {"id": chat_id}}
    return q


def _arm_operator_and_satellite():
    ctx.set_context(OPERATOR, "buy", default_usdt=25)
    ctx.set_context(SATELLITE, "lock")


def test_lock_ok_callback_does_not_pop_operator_context(monkeypatch):
    """ContextVar empty → confirm must not clear TELEGRAM_CHAT_ID's wizard."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID", OPERATOR)
    monkeypatch.setattr(f"{LC}.answer_callback_query", lambda *a, **k: True)
    install_telegram_capture(monkeypatch)
    _arm_operator_and_satellite()
    token = _pending()

    with patch(f"{LC}.set_position_lock") as set_lock:
        assert handle_telegram_callback(_ok_query(token)) is True
        set_lock.assert_called_once()

    entry = ctx.get_context(OPERATOR)
    assert entry is not None
    assert entry["command"] == "buy"
    with patch("notifications.telegram_commands.router.dispatch_command", return_value=True) as mock:
        assert ctx.try_resolve(OPERATOR, "1") is True
        mock.assert_called_once_with("/buy 1 25")


def test_lock_ok_callback_clears_confirming_chat_lock_context(monkeypatch):
    """After confirm, the confirming chat's next plain word is not ``/lock``."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID", OPERATOR)
    monkeypatch.setattr(f"{LC}.answer_callback_query", lambda *a, **k: True)
    install_telegram_capture(monkeypatch)
    _arm_operator_and_satellite()
    token = _pending()

    with patch(f"{LC}.set_position_lock"):
        assert handle_telegram_callback(_ok_query(token)) is True

    assert ctx.get_context(SATELLITE) is None
    with patch("notifications.telegram_commands.router.dispatch_command") as mock:
        assert ctx.try_resolve(SATELLITE, "BLESS") is False
        mock.assert_not_called()


def test_lock_ok_missing_chat_id_fails_closed(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", OPERATOR)
    monkeypatch.setattr("telegram_notifier.answer_callback_query", lambda *a, **k: True)
    monkeypatch.setattr(f"{LC}.answer_callback_query", lambda *a, **k: True)
    install_telegram_capture(monkeypatch)
    _arm_operator_and_satellite()
    token = _pending()

    with patch(f"{LC}.set_position_lock") as set_lock, patch(
        "notifications.telegram_commands.router.dispatch_callback"
    ) as dispatch:
        assert handle_telegram_callback(_ok_query(token, missing_chat=True)) is True
        dispatch.assert_not_called()
        set_lock.assert_not_called()

    assert ctx.get_context(OPERATOR)["command"] == "buy"
    assert ctx.get_context(SATELLITE)["command"] == "lock"
    assert consume_lock_token(token) is not None


def test_lock_ok_handle_callback_missing_chat_id_fails_closed(monkeypatch):
    """Same fail-closed if dispatch reaches lock_commands without chat.id."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID", OPERATOR)
    monkeypatch.setattr(f"{LC}.answer_callback_query", lambda *a, **k: True)
    install_telegram_capture(monkeypatch)
    _arm_operator_and_satellite()
    token = _pending()

    with patch(f"{LC}.set_position_lock") as set_lock:
        assert handle_callback(_ok_query(token, missing_chat=True)) is True
        set_lock.assert_not_called()

    assert ctx.get_context(OPERATOR)["command"] == "buy"
    assert ctx.get_context(SATELLITE)["command"] == "lock"
    assert consume_lock_token(token) is not None


def test_sellpct_and_sellpos_still_dispatched_when_chat_id_present(monkeypatch):
    monkeypatch.setattr("telegram_notifier.answer_callback_query", lambda *a, **k: True)
    queries = (
        {"id": "cb-pct", "data": "sellpct:50", "message": {"chat": {"id": 99}}},
        {"id": "cb-pos", "data": "sellpos:1", "message": {"chat": {"id": 99}}},
    )
    for q in queries:
        with patch(
            "notifications.telegram_commands.trading_commands.handle_callback",
            return_value=True,
        ) as trade:
            assert handle_telegram_callback(q) is True
            trade.assert_called_once_with(q)
        assert ctx.current_chat_id() == "99"


def test_typed_lock_with_symbol_still_uses_bare_clear_context():
    """Typed ``/lock SYMBOL`` already set the ContextVar; do not add a chat id here."""
    src = inspect.getsource(lock_commands._handle_lock)
    assert "clear_context()" in src
    assert "clear_context(chat_id)" not in src
