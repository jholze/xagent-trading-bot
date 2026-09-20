"""Per-chat command context for short follow-up input (e.g. ``1 25`` after ``/buy``)."""

from __future__ import annotations

import json
import os
from contextvars import ContextVar
from datetime import datetime, timedelta
from pathlib import Path

from data_manager import atomic_write_json
from notifications.telegram_commands.utils import safe_float, safe_int
from telegram_notifier import send_telegram_message

_CONTEXT_FILE = Path(__file__).resolve().parents[2] / "data" / "telegram_command_context.json"
_TTL_MINUTES = 15
CANCEL_CALLBACK = "cmdctx:cancel"

_chat_id_var: ContextVar[str] = ContextVar("telegram_chat_id", default="")


def set_chat_id(chat_id: str | int | None) -> None:
    if chat_id is not None:
        _chat_id_var.set(str(chat_id))


def current_chat_id() -> str:
    cid = _chat_id_var.get()
    if cid:
        return cid
    return os.getenv("TELEGRAM_CHAT_ID", "")


def _load_store() -> dict:
    if not _CONTEXT_FILE.exists():
        return {"contexts": {}, "sections": {}}
    try:
        with open(_CONTEXT_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {
            "contexts": data.get("contexts") or {},
            "sections": data.get("sections") or {},
        }
    except Exception:
        return {"contexts": {}, "sections": {}}


def _save_store(data: dict) -> None:
    _CONTEXT_FILE.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(str(_CONTEXT_FILE), data)


def _is_expired(entry: dict) -> bool:
    updated = entry.get("updated_at", "")
    if not updated:
        return True
    try:
        ts = datetime.fromisoformat(str(updated).replace("Z", ""))
        return datetime.now() - ts > timedelta(minutes=_TTL_MINUTES)
    except Exception:
        return True


# Commands that `_build_command` can turn into a slash line. Anything else
# (orders_blocked, orders_month, …) must not be armed: follow-up text would
# sit in "invalid input" for 15 minutes with no builder (#454).
_RESOLVABLE_COMMANDS = frozenset({
    "buy", "sell", "add", "remove", "why", "ask", "orders",
    "maxpositions", "mode", "addx", "removex",
    "sandbox_results", "sandbox_promote",
    "backtest_lock", "backtest_results", "testaccount",
    "lock", "unlock", "short", "cover",
})


def cancel_keyboard() -> list:
    """One-row inline keyboard: Abbrechen → ``CANCEL_CALLBACK``."""
    from notifications.telegram_i18n import t

    return [[{"text": t("pending_cancel_btn"), "callback_data": CANCEL_CALLBACK}]]


def pending_reply_markup() -> dict:
    return {"inline_keyboard": cancel_keyboard()}


def pending_reminder_text(command: str, meta: dict | None = None) -> str:
    from notifications.telegram_i18n import t

    meta = meta or {}
    if command == "buy":
        label = str(meta.get("label") or meta.get("coin") or "").strip()
        if str(meta.get("state") or "") == "buy_awaiting_usdt" and label:
            return t("pending_reminder_buy_usdt", coin=label)
        return t("pending_reminder_buy")
    if command == "sell":
        label = str(meta.get("label") or meta.get("position") or "").strip()
        if str(meta.get("state") or "") == "sell_awaiting_pct" and label:
            return t("pending_reminder_sell_pct", position=label)
        return t("pending_reminder_sell")
    return t("pending_reminder", command=command)


def send_pending_cancel_chrome(command: str, meta: dict | None = None) -> None:
    """One-line pending reminder + Abbrechen. Does not place an order."""
    send_telegram_message(
        pending_reminder_text(command, meta),
        reply_markup=pending_reply_markup(),
    )


def activate_command(command: str, **meta) -> None:
    """Set context for the current webhook chat (or TELEGRAM_CHAT_ID).

    Waiting flows get an inline Abbrechen control (#450). Pass
    ``chrome=False`` when the caller already attached ``pending_reply_markup``
    to the prompt (so Henry does not see two Cancel buttons). ``chrome`` is
    not stored in context meta.
    """
    if command not in _RESOLVABLE_COMMANDS:
        return
    chrome = bool(meta.pop("chrome", True))
    cid = current_chat_id()
    if cid:
        set_context(cid, command, **meta)
        if chrome:
            send_pending_cancel_chrome(command, meta)


def set_context(chat_id: str | int, command: str, **meta) -> None:
    cid = str(chat_id)
    if not cid:
        return
    store = _load_store()
    store["contexts"][cid] = {
        "command": command,
        "meta": meta,
        "updated_at": datetime.now().isoformat(),
    }
    _save_store(store)


def get_context(chat_id: str | int | None = None) -> dict | None:
    cid = str(chat_id or current_chat_id())
    if not cid:
        return None
    store = _load_store()
    entry = store["contexts"].get(cid)
    if not entry or _is_expired(entry):
        if entry:
            clear_context(cid)
        return None
    return entry


def clear_context(chat_id: str | int | None = None) -> None:
    cid = str(chat_id or current_chat_id())
    if not cid:
        return
    store = _load_store()
    store["contexts"].pop(cid, None)
    _save_store(store)


def set_active_section(chat_id: str | int, section_id: str) -> None:
    cid = str(chat_id)
    if not cid:
        return
    store = _load_store()
    store["sections"][cid] = section_id
    _save_store(store)


def get_active_section(chat_id: str | int | None = None) -> str | None:
    cid = str(chat_id or current_chat_id())
    if not cid:
        return None
    return _load_store()["sections"].get(cid)


def clear_active_section(chat_id: str | int | None = None) -> None:
    cid = str(chat_id or current_chat_id())
    if not cid:
        return
    store = _load_store()
    store["sections"].pop(cid, None)
    _save_store(store)


def _invalid(msg: str) -> bool:
    send_telegram_message(msg)
    return False


def parse_sell_percent_token(token: str) -> str | None:
    """Return a canonical 1–100 percent token, or None if missing/invalid.

    Used by the guided /sell percent step. Does not default to 50.
    """
    raw = (token or "").strip().rstrip("%").strip()
    if not raw:
        return None
    val = safe_float(raw)
    if val is None or val <= 0 or val > 100:
        return None
    if float(val).is_integer():
        return str(int(val))
    return raw


def _sell_position_token(raw: str) -> str:
    token = (raw or "").strip()
    if token.replace(".", "").isdigit():
        return token
    return token.upper()


def _build_command(command: str, text: str, meta: dict) -> str | None:
    parts = text.strip().split()
    if not parts:
        return None

    if command == "buy":
        state = str(meta.get("state") or "")
        token = parts[0]
        coin_token = token if token.replace(".", "").isdigit() else token.upper()
        if state == "buy_awaiting_usdt":
            coin = str(meta.get("coin") or "").strip()
            if not coin:
                return None
            usdt = parts[1] if len(parts) > 1 else parts[0]
            val = safe_float(usdt)
            if val is None or val <= 0:
                return None
            return f"/buy {coin} {usdt}"
        if state == "buy_awaiting_coin":
            # Wizard coin step: never silently fill max_usdt (#446).
            if len(parts) > 1:
                return f"/buy {coin_token} {parts[1]}"
            return f"/buy {coin_token}"
        # Legacy unstated context (frozen #450/#454/#498): keep default_usdt.
        if token.replace(".", "").isdigit():
            usdt = parts[1] if len(parts) > 1 else str(meta.get("default_usdt", ""))
            if not usdt:
                return None
            return f"/buy {token} {usdt}"
        usdt = parts[1] if len(parts) > 1 else str(meta.get("default_usdt", ""))
        if not usdt:
            return None
        return f"/buy {token.upper()} {usdt}"

    if command == "sell":
        if str(meta.get("state") or "") == "sell_awaiting_pct":
            position = str(meta.get("position") or "").strip()
            if not position:
                return None
            pct_token = parts[1] if len(parts) > 1 else parts[0]
            canonical = parse_sell_percent_token(pct_token)
            if canonical is None:
                return None
            return f"/sell {position} {canonical}"
        if len(parts) > 1:
            pct = parts[1]
            return f"/sell {_sell_position_token(parts[0])} {pct}"
        return f"/sell {_sell_position_token(parts[0])}"

    if command == "add":
        return f"/add {parts[0].upper()}"

    if command == "remove":
        if not parts[0].isdigit():
            return None
        return f"/remove {parts[0]}"

    if command == "why":
        return f"/why {parts[0].upper()}"

    if command == "ask":
        return f"/ask {text.strip()}"

    if command == "orders":
        if parts[0].lower() == "page":
            page = parts[1] if len(parts) > 1 else "1"
            return f"/orders page {page}"
        if parts[0].isdigit():
            return f"/orders {parts[0]}"
        return None

    if command == "maxpositions":
        if not parts[0].isdigit():
            return None
        return f"/maxpositions {parts[0]}"

    if command == "mode":
        mode = parts[0].lower()
        if mode in ("paper", "live", "off"):
            return f"/mode {mode}"
        return None

    if command in ("addx", "removex"):
        account = parts[0].lstrip("@")
        return f"/{command} {account}"

    if command in ("sandbox_results", "sandbox_promote"):
        return f"/{command} {parts[0]}"

    if command in ("backtest_lock", "backtest_results"):
        sym = parts[0].upper()
        if "/" not in sym:
            sym = f"{sym}/USDT"
        return f"/{command} {sym}"

    if command == "testaccount":
        account = parts[0].lstrip("@")
        days = parts[1] if len(parts) > 1 else ""
        return f"/testaccount {account} {days}".strip()

    if command in ("lock", "unlock", "short", "cover"):
        return f"/{command} {text.strip()}"

    return None


def is_keyboard_navigation(text: str) -> bool:
    """True for reply-keyboard back/help/section/home taps (#454).

    These must not be consumed as pending-command input; the keyboard
    handler in ``menu_commands.handle_text`` owns them.
    """
    stripped = (text or "").strip()
    if not stripped:
        return False
    from notifications.telegram_commands.menu_i18n import (
        home_label_to_key,
        is_back_label,
        is_help_label,
        is_more_label,
        title_to_section_id,
    )

    if is_back_label(stripped) or is_help_label(stripped) or is_more_label(stripped):
        return True
    if title_to_section_id(stripped):
        return True
    if home_label_to_key(stripped):
        return True
    return False


def try_resolve(chat_id: str | int, text: str) -> bool:
    """Map short follow-up text to a slash command using active context."""
    set_chat_id(chat_id)
    stripped = (text or "").strip()
    if stripped.startswith("/"):
        clear_context(chat_id)
        from notifications.telegram_commands.router import dispatch_command

        return dispatch_command(stripped)

    # Reply-keyboard labels: drop the wizard and let handle_telegram_text
    # fall through to menu_commands.handle_text (same as menu:run:).
    if is_keyboard_navigation(stripped):
        if get_context(chat_id):
            clear_context(chat_id)
        return False

    entry = get_context(chat_id)
    if not entry:
        return False

    command = entry.get("command", "")
    meta = entry.get("meta") or {}
    built = _build_command(command, text, meta)
    if not built:
        if command == "sell" and str(meta.get("state") or "") == "sell_awaiting_pct":
            from notifications.telegram_commands.trading_commands import prompt_sell_percentage

            label = str(meta.get("label") or meta.get("position") or "")
            prompt_sell_percentage(label, invalid=True)
            set_context(chat_id, command, **meta)
            return True
        if command == "buy" and str(meta.get("state") or "") == "buy_awaiting_usdt":
            from notifications.telegram_commands.trading_commands import prompt_buy_amount

            label = str(meta.get("label") or meta.get("coin") or "")
            prompt_buy_amount(label, invalid=True)
            set_context(chat_id, command, **meta)
            return True
        from notifications.telegram_commands.menu_i18n import current_language, short_input_invalid

        _invalid(short_input_invalid(command, current_language()))
        return True

    from notifications.telegram_commands.router import dispatch_command

    clear_context(chat_id)
    return dispatch_command(built)


def handle_callback(callback_query: dict) -> bool:
    """Clear pending command_context. Never dispatches a buy/sell/lock/…."""
    data = str((callback_query or {}).get("data") or "")
    if data != CANCEL_CALLBACK:
        return False

    from telegram_notifier import answer_callback_query
    from notifications.telegram_i18n import t

    callback_id = (callback_query or {}).get("id")
    if callback_id:
        answer_callback_query(callback_id)
    message = (callback_query or {}).get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    if not chat_id:
        from logger import log

        log("cmdctx cancel callback missing chat id", "WARNING")
        return True
    set_chat_id(chat_id)
    clear_context(chat_id)
    send_telegram_message(t("pending_cancel_done"))
    return True
