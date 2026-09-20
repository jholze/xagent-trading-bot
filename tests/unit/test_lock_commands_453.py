"""#453 — Telegram /lock requires a confirm that names stop-loss off."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from notifications.telegram_commands import command_context as ctx
from notifications.telegram_commands import lock_commands
from notifications.telegram_commands.lock_commands import (
    LOCK_CONFIRM_TTL_SEC,
    consume_lock_token,
    handle,
    handle_callback,
    reset_lock_confirm_for_tests,
)
from notifications.telegram_commands.menu_i18n import (
    command_description,
    command_help_line,
    command_hint,
    reload_menu_data,
    set_user_language,
)
from notifications.telegram_commands.router import dispatch_callback
from notifications.telegram_commands.usage_hints import clear_usage_cache
from notifications.telegram_i18n import reload_messages, t
from strategies.position_lock import DEFAULT_MODES
from tests.support.telegram_capture import install_telegram_capture, texts

LC = "notifications.telegram_commands.lock_commands"
ROOT = Path(__file__).resolve().parents[2]
CHAT = "453"


class _Clock:
    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def _pos(symbol="BLESS/USDT", timeframe="1h", amount=10):
    return {"symbol": symbol, "timeframe": timeframe, "amount": amount}


def _open_bless():
    pos = _pos()
    return {
        "position_locks_enabled": True,
        "list_active_positions": [pos],
        "get_prices_batch": {pos["symbol"]: 1.0},
        "resolve_position_by_symbol": pos,
        "get_position": pos,
        "is_open_position": True,
    }


def _patch_open(monkeypatch, *, extra=None):
    cfg = _open_bless()
    if extra:
        cfg.update(extra)
    monkeypatch.setattr(f"{LC}.position_locks_enabled", lambda *a, **k: cfg["position_locks_enabled"])
    monkeypatch.setattr(f"{LC}.list_active_positions", lambda: cfg["list_active_positions"])
    monkeypatch.setattr(f"{LC}.get_prices_batch", lambda symbols: cfg["get_prices_batch"])
    monkeypatch.setattr(
        f"{LC}.resolve_position_by_symbol",
        lambda active, query, prices: cfg["resolve_position_by_symbol"]
        if not callable(cfg["resolve_position_by_symbol"])
        else cfg["resolve_position_by_symbol"](active, query, prices),
    )
    monkeypatch.setattr(
        f"{LC}.get_position",
        lambda *a, **k: cfg["get_position"]
        if not callable(cfg["get_position"])
        else cfg["get_position"](*a, **k),
    )
    monkeypatch.setattr(f"{LC}.is_open_position", lambda *a, **k: cfg["is_open_position"])
    return cfg


def _buttons(tg):
    return [item for item in tg if item.get("kind") == "buttons"]


def _token_from(tg) -> str:
    keyboard = _buttons(tg)[0]["buttons"]
    return keyboard[0][0]["callback_data"].split(":", 1)[1]


def _lock_ok(token: str, chat_id=CHAT, cid="cb") -> dict:
    return {
        "id": cid,
        "data": f"lock_ok:{token}",
        "message": {"chat": {"id": chat_id}},
    }


def test_lock_without_symbol_lists_and_does_not_persist(monkeypatch):
    reset_lock_confirm_for_tests()
    tg = install_telegram_capture(monkeypatch)
    pos = _pos()
    monkeypatch.setattr(f"{LC}.position_locks_enabled", lambda *a, **k: True)
    monkeypatch.setattr(f"{LC}.list_active_positions", lambda: [pos])
    monkeypatch.setattr(f"{LC}.get_position", lambda *a, **k: pos)
    monkeypatch.setattr(f"{LC}.get_lock", lambda *a, **k: None)
    with patch(f"{LC}.set_position_lock") as set_lock:
        assert handle("/lock") is True
        set_lock.assert_not_called()
    joined = "\n".join(texts(tg))
    assert "Position Locks" in joined
    buttons = _buttons(tg)
    assert buttons
    callbacks = [btn["callback_data"] for row in buttons[0]["buttons"] for btn in row]
    assert callbacks == ["lockpos:BLESS:1h"]
    assert all(not c.startswith("lock_ok:") and not c.startswith("lock_no:") for c in callbacks)


def test_lock_symbol_prompts_and_does_not_persist(monkeypatch):
    reset_lock_confirm_for_tests()
    set_user_language("de")
    reload_messages()
    tg = install_telegram_capture(monkeypatch)
    _patch_open(monkeypatch)
    with patch(f"{LC}.set_position_lock") as set_lock:
        assert handle("/lock BLESS") is True
        set_lock.assert_not_called()
    buttons = _buttons(tg)
    assert buttons
    msg = buttons[0]["text"]
    assert "BLESS/USDT" in msg
    assert "Stop-Loss" in msg
    assert "AUS" in msg
    row = buttons[0]["buttons"][0]
    assert row[0]["callback_data"].startswith("lock_ok:")
    assert row[1]["callback_data"].startswith("lock_no:")
    assert "Stop-Loss aus" in row[0]["text"]


def test_lock_symbol_en_names_stop_loss_off(monkeypatch):
    reset_lock_confirm_for_tests()
    set_user_language("en")
    reload_messages()
    tg = install_telegram_capture(monkeypatch)
    _patch_open(monkeypatch)
    with patch(f"{LC}.set_position_lock") as set_lock:
        assert handle("/lock BLESS") is True
        set_lock.assert_not_called()
    msg = _buttons(tg)[0]["text"]
    assert "BLESS/USDT" in msg
    assert "Stop-loss" in msg
    assert "OFF" in msg
    assert "Manual" in msg or "manual" in msg
    set_user_language("de")
    reload_messages()


def test_confirm_persists_bound_lock(monkeypatch):
    reset_lock_confirm_for_tests()
    set_user_language("de")
    reload_messages()
    tg = install_telegram_capture(monkeypatch)
    _patch_open(monkeypatch)
    monkeypatch.setattr(f"{LC}.answer_callback_query", lambda *a, **k: True)
    with patch(f"{LC}.set_position_lock") as set_lock:
        assert handle("/lock BLESS 24h hold_news") is True
        set_lock.assert_not_called()
        token = _token_from(tg)
        assert handle_callback(_lock_ok(token)) is True
        set_lock.assert_called_once()
        sym, tf, lock = set_lock.call_args.args[:3]
        assert sym == "BLESS/USDT"
        assert tf == "1h"
        assert set_lock.call_args.kwargs.get("persist") is True
        assert lock["reason"] == "hold_news"
        assert lock["locked_by"] == "telegram"
        assert list(lock["modes"]) == list(DEFAULT_MODES)
        assert lock.get("until")


def test_cancel_writes_nothing(monkeypatch):
    reset_lock_confirm_for_tests()
    tg = install_telegram_capture(monkeypatch)
    _patch_open(monkeypatch)
    monkeypatch.setattr(f"{LC}.answer_callback_query", lambda *a, **k: True)
    with patch(f"{LC}.set_position_lock") as set_lock:
        assert handle("/lock BLESS") is True
        token = _token_from(tg)
        tg.clear()
        assert handle_callback({"id": "cb", "data": f"lock_no:{token}"}) is True
        set_lock.assert_not_called()
    joined = "\n".join(texts(tg)).lower()
    assert "abgebrochen" in joined or "cancelled" in joined


def test_missing_token_writes_nothing(monkeypatch):
    reset_lock_confirm_for_tests()
    tg = install_telegram_capture(monkeypatch)
    monkeypatch.setattr(f"{LC}.answer_callback_query", lambda *a, **k: True)
    with patch(f"{LC}.set_position_lock") as set_lock:
        assert handle_callback(_lock_ok("missingtoken")) is True
        set_lock.assert_not_called()
    joined = "\n".join(texts(tg)).lower()
    assert "abgelaufen" in joined or "expired" in joined


def test_expired_token_writes_nothing(monkeypatch):
    clock = _Clock(1000.0)
    reset_lock_confirm_for_tests(clock=clock)
    tg = install_telegram_capture(monkeypatch)
    _patch_open(monkeypatch)
    monkeypatch.setattr(f"{LC}.answer_callback_query", lambda *a, **k: True)
    with patch(f"{LC}.set_position_lock") as set_lock:
        assert handle("/lock BLESS") is True
        token = _token_from(tg)
        clock.t += LOCK_CONFIRM_TTL_SEC + 0.1
        assert consume_lock_token("not-this") is None
        tg.clear()
        assert handle_callback(_lock_ok(token)) is True
        set_lock.assert_not_called()
    joined = "\n".join(texts(tg)).lower()
    assert "abgelaufen" in joined or "expired" in joined


def test_confirm_binds_first_symbol_not_later_lock(monkeypatch):
    """Same class of bug as #330: confirm must restore the token's coin, not a later /lock."""
    reset_lock_confirm_for_tests()
    tg = install_telegram_capture(monkeypatch)
    bless = _pos("BLESS/USDT", "1h")
    rave = _pos("RAVE/USDT", "4h")

    def _resolve(active, query, prices):
        q = (query or "").upper()
        if "RAVE" in q:
            return rave
        if "BLESS" in q:
            return bless
        return None

    def _get_position(sym, tf="1h"):
        if str(sym).startswith("RAVE"):
            return rave
        return bless

    monkeypatch.setattr(f"{LC}.position_locks_enabled", lambda *a, **k: True)
    monkeypatch.setattr(f"{LC}.list_active_positions", lambda: [bless, rave])
    monkeypatch.setattr(
        f"{LC}.get_prices_batch",
        lambda symbols: {"BLESS/USDT": 1.0, "RAVE/USDT": 2.0},
    )
    monkeypatch.setattr(f"{LC}.resolve_position_by_symbol", _resolve)
    monkeypatch.setattr(f"{LC}.get_position", _get_position)
    monkeypatch.setattr(f"{LC}.is_open_position", lambda *a, **k: True)
    monkeypatch.setattr(f"{LC}.answer_callback_query", lambda *a, **k: True)

    with patch(f"{LC}.set_position_lock") as set_lock:
        assert handle("/lock BLESS") is True
        token_bless = _token_from(tg)
        tg.clear()
        assert handle("/lock RAVE 7d later_reason") is True
        assert handle_callback(_lock_ok(token_bless)) is True
        set_lock.assert_called_once()
        sym, tf, lock = set_lock.call_args.args[:3]
        assert sym == "BLESS/USDT"
        assert tf == "1h"
        assert lock["reason"] == "telegram_lock"
        assert "later_reason" not in str(lock.get("reason") or "")


def test_unlock_stays_immediate(monkeypatch):
    reset_lock_confirm_for_tests()
    tg = install_telegram_capture(monkeypatch)
    pos = _pos("H/USDT", "4h")
    lock = {"until": None, "modes": ["no_auto_sell"]}
    monkeypatch.setattr(f"{LC}.list_active_positions", lambda: [pos])
    monkeypatch.setattr(f"{LC}.get_prices_batch", lambda symbols: {"H/USDT": 1.0})
    monkeypatch.setattr(f"{LC}.resolve_position_by_symbol", lambda *a, **k: pos)
    monkeypatch.setattr(f"{LC}.get_position", lambda *a, **k: pos)
    monkeypatch.setattr(f"{LC}.get_lock", lambda *a, **k: lock)
    with patch(f"{LC}.set_position_lock") as set_lock:
        assert handle("/unlock H") is True
        set_lock.assert_called_once_with("H/USDT", "4h", None, persist=True)
    assert not _buttons(tg)


def test_confirm_prompt_and_success_do_not_arm_context(monkeypatch):
    reset_lock_confirm_for_tests()
    ctx.set_chat_id(CHAT)
    tg = install_telegram_capture(monkeypatch)
    _patch_open(monkeypatch)
    monkeypatch.setattr(f"{LC}.answer_callback_query", lambda *a, **k: True)
    ctx.set_context(CHAT, "lock")
    with patch(f"{LC}.set_position_lock"):
        assert handle("/lock BLESS") is True
        assert ctx.get_context(CHAT) is None
        token = _token_from(tg)
        ctx.set_context(CHAT, "lock")
        assert handle_callback(_lock_ok(token)) is True
        assert ctx.get_context(CHAT) is None
    ctx.set_chat_id("")


def test_router_dispatches_lock_callback(monkeypatch):
    reset_lock_confirm_for_tests()
    tg = install_telegram_capture(monkeypatch)
    _patch_open(monkeypatch)
    monkeypatch.setattr(f"{LC}.answer_callback_query", lambda *a, **k: True)
    monkeypatch.setattr("telegram_notifier.answer_callback_query", lambda *a, **k: True)
    with patch(f"{LC}.set_position_lock") as set_lock:
        assert handle("/lock BLESS") is True
        token = _token_from(tg)
        assert dispatch_callback(_lock_ok(token, cid="cq")) is True
        set_lock.assert_called_once()


def test_menu_copy_does_not_imply_protection():
    reload_menu_data()
    clear_usage_cache()
    forbidden = (
        "gegen auto-sell",
        "gegen Auto-Sell",
        "schützen",
        "protect",
        "against auto-sell",
    )
    for lang in ("de", "en"):
        blob = "\n".join(
            [
                command_description("lock", lang),
                command_help_line("lock", lang),
                command_hint("lock", lang),
            ]
        )
        low = blob.lower()
        for needle in forbidden:
            assert needle.lower() not in low, f"{lang}: found {needle!r} in {blob!r}"
        if lang == "de":
            assert "Stop-Loss" in blob
            assert "aus" in low
            assert "Halt" in blob or "Verkaufssperre" in blob
        else:
            assert "stop-loss" in low
            assert "off" in low


def test_locale_files_have_de_en_lock_confirm_keys():
    menu = json.loads((ROOT / "locales" / "telegram_menu.json").read_text(encoding="utf-8"))
    messages = json.loads((ROOT / "locales" / "telegram_messages.json").read_text(encoding="utf-8"))
    keys = (
        "lock_confirm",
        "lock_confirm_btn_ok",
        "lock_confirm_btn_no",
        "lock_confirm_expired",
        "lock_confirm_cancelled",
    )
    for lang in ("de", "en"):
        for key in keys:
            assert key in messages[lang], f"missing {lang}.{key}"
        desc = menu[lang]["commands"]["lock"]["description"]
        assert "gegen Auto-Sell" not in desc
        assert "against auto-sell" not in desc.lower()
    reload_messages()
    assert "AUS" in t("lock_confirm", lang="de", symbol="X", until="permanent", reason="r", modes="m")
    assert "OFF" in t("lock_confirm", lang="en", symbol="X", until="permanent", reason="r", modes="m")
