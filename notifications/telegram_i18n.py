"""Telegram user-facing message catalog (DE/EN).

Use ``t(key, **kwargs)`` for all bot→user strings. Language comes from
``menu_i18n.current_language()`` (tenant ui_language / Telegram update).
"""

from __future__ import annotations

import json
from pathlib import Path

from notifications.telegram_commands.menu_i18n import (
    SUPPORTED_LANGS,
    current_language,
)

_MESSAGES: dict | None = None
_PATH = Path(__file__).resolve().parents[1] / "locales" / "telegram_messages.json"


def _load() -> dict:
    global _MESSAGES
    if _MESSAGES is not None:
        return _MESSAGES
    with open(_PATH, encoding="utf-8") as f:
        _MESSAGES = json.load(f)
    return _MESSAGES


def reload_messages() -> None:
    global _MESSAGES
    _MESSAGES = None
    _load()


def t(key: str, lang: str | None = None, **kwargs) -> str:
    """Translate *key* for the active UI language; optional format kwargs."""
    pack_lang = lang or current_language()
    if pack_lang not in SUPPORTED_LANGS:
        pack_lang = "de"
    data = _load()
    pack = data.get(pack_lang) or data.get("de") or {}
    fallback = (data.get("de") or {}).get(key) or key
    template = pack.get(key, fallback)
    if not kwargs:
        return str(template)
    try:
        return str(template).format(**kwargs)
    except (KeyError, ValueError):
        return str(template)


def money(value: float, *, decimals: int = 0) -> str:
    """Format USD amount for templates (no $ sign — templates include it)."""
    if decimals <= 0:
        return f"{float(value):,.0f}"
    return f"{float(value):,.{decimals}f}"


def signed_money(value: float, *, decimals: int = 0) -> str:
    v = float(value)
    body = money(abs(v), decimals=decimals)
    if v < 0:
        return f"-{body}"
    return f"+{body}"
