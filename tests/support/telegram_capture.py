"""Capture outbound Telegram messages without hitting api.telegram.org.

Handlers bind ``from telegram_notifier import send_telegram_message`` at import
time, so we patch both the source module and every command module that holds a
local reference.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any, Callable


def _iter_command_modules():
    import notifications.telegram_commands as pkg

    yield pkg
    for info in pkgutil.iter_modules(pkg.__path__, pkg.__name__ + "."):
        if info.name.endswith(".__pycache__"):
            continue
        try:
            yield importlib.import_module(info.name)
        except Exception:
            continue


def install_telegram_capture(monkeypatch) -> list[dict[str, Any]]:
    """Install capture patches; returns shared outbound list."""
    out: list[dict[str, Any]] = []

    def _capture_message(
        text,
        reply_markup=None,
        *,
        chat_id=None,
        parse_mode="HTML",
        priority=None,
        **kwargs,
    ):
        out.append(
            {
                "kind": "message",
                "text": "" if text is None else str(text),
                "reply_markup": reply_markup,
                "chat_id": chat_id,
                "parse_mode": parse_mode,
                "priority": priority,
                **kwargs,
            }
        )
        return True

    def _capture_buttons(text, buttons, *, chat_id=None, **kwargs):
        out.append(
            {
                "kind": "buttons",
                "text": "" if text is None else str(text),
                "buttons": buttons,
                "chat_id": chat_id,
                **kwargs,
            }
        )
        return True

    def _capture_reply_keyboard(text, rows, *, one_time=False, chat_id=None, **kwargs):
        out.append(
            {
                "kind": "reply_keyboard",
                "text": "" if text is None else str(text),
                "rows": rows,
                "one_time": one_time,
                "chat_id": chat_id,
                **kwargs,
            }
        )
        return True

    def _capture_edit(text, chat_id, message_id, reply_markup=None, **kwargs):
        out.append(
            {
                "kind": "edit",
                "text": "" if text is None else str(text),
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": reply_markup,
                **kwargs,
            }
        )
        return True

    # Source module
    monkeypatch.setattr("telegram_notifier.send_telegram_message", _capture_message)
    monkeypatch.setattr("telegram_notifier.send_telegram_buttons", _capture_buttons)
    monkeypatch.setattr("telegram_notifier.send_reply_keyboard", _capture_reply_keyboard)
    monkeypatch.setattr("telegram_notifier.edit_telegram_message", _capture_edit)
    monkeypatch.setattr("telegram_notifier._send_telegram_direct", _capture_message)

    # Bound imports on command packages
    for mod in _iter_command_modules():
        if hasattr(mod, "send_telegram_message"):
            monkeypatch.setattr(mod, "send_telegram_message", _capture_message, raising=False)
        if hasattr(mod, "send_telegram_buttons"):
            monkeypatch.setattr(mod, "send_telegram_buttons", _capture_buttons, raising=False)
        if hasattr(mod, "send_reply_keyboard"):
            monkeypatch.setattr(mod, "send_reply_keyboard", _capture_reply_keyboard, raising=False)
        if hasattr(mod, "edit_telegram_message"):
            monkeypatch.setattr(mod, "edit_telegram_message", _capture_edit, raising=False)

    return out


def texts(out: list[dict[str, Any]]) -> list[str]:
    return [m.get("text") or "" for m in out]


def any_text_contains(out: list[dict[str, Any]], *needles: str, casefold: bool = True) -> bool:
    joined = "\n".join(texts(out))
    hay = joined.casefold() if casefold else joined
    for n in needles:
        needle = n.casefold() if casefold else n
        if needle in hay:
            return True
    return False


def clear(out: list[dict[str, Any]]) -> None:
    out.clear()
