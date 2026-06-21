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


def activate_command(command: str, **meta) -> None:
    """Set context for the current webhook chat (or TELEGRAM_CHAT_ID)."""
    cid = current_chat_id()
    if cid:
        set_context(cid, command, **meta)


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


def _build_command(command: str, text: str, meta: dict) -> str | None:
    parts = text.strip().split()
    if not parts:
        return None

    if command == "buy":
        if parts[0].replace(".", "").isdigit():
            num = parts[0]
            usdt = parts[1] if len(parts) > 1 else str(meta.get("default_usdt", ""))
            if not usdt:
                return None
            return f"/buy {num} {usdt}"
        sym = parts[0].upper()
        usdt = parts[1] if len(parts) > 1 else str(meta.get("default_usdt", ""))
        if not usdt:
            return None
        return f"/buy {sym} {usdt}"

    if command == "sell":
        if not parts[0].replace(".", "").isdigit():
            return None
        num = parts[0]
        pct = parts[1] if len(parts) > 1 else "50"
        return f"/sell {num} {pct}"

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

    return None


def try_resolve(chat_id: str | int, text: str) -> bool:
    """Map short follow-up text to a slash command using active context."""
    entry = get_context(chat_id)
    if not entry:
        return False

    command = entry.get("command", "")
    meta = entry.get("meta") or {}
    built = _build_command(command, text, meta)
    if not built:
        from notifications.telegram_commands.menu_i18n import current_language, short_input_invalid

        _invalid(short_input_invalid(command, current_language()))
        return True

    from notifications.telegram_commands.router import dispatch_command

    clear_context(chat_id)
    return dispatch_command(built)