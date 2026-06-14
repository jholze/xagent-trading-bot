"""Telegram menu translations (DE/EN) tied to user language_code."""

from __future__ import annotations

import json
from contextvars import ContextVar
from pathlib import Path

_user_lang: ContextVar[str] = ContextVar("telegram_user_lang", default="de")
_MENU_DATA: dict | None = None

SUPPORTED_LANGS = ("de", "en")


def _load_menu_data() -> dict:
    global _MENU_DATA
    if _MENU_DATA is not None:
        return _MENU_DATA
    path = Path(__file__).resolve().parents[2] / "locales" / "telegram_menu.json"
    with open(path, encoding="utf-8") as f:
        _MENU_DATA = json.load(f)
    return _MENU_DATA


def resolve_language(code: str | None) -> str:
    """Map Telegram language_code (e.g. de-DE, en-US) to de or en."""
    if not code:
        try:
            from core.config import get_bot_config

            cfg_lang = get_bot_config().telegram_command_menu_config.get("default_language")
            if cfg_lang in SUPPORTED_LANGS:
                return cfg_lang
        except Exception:
            pass
        return "de"
    base = str(code).lower().split("-")[0]
    return "de" if base == "de" else "en"


def set_user_language(lang: str) -> None:
    _user_lang.set(lang if lang in SUPPORTED_LANGS else "de")


def current_language() -> str:
    return _user_lang.get()


def set_user_language_from_update(update: dict | None) -> str:
    lang = "de"
    if update:
        user = None
        if "message" in update:
            user = update["message"].get("from")
        elif "callback_query" in update:
            user = update["callback_query"].get("from")
        if user:
            lang = resolve_language(user.get("language_code"))
    set_user_language(lang)
    return lang


def _pack(lang: str) -> dict:
    data = _load_menu_data()
    return data.get(lang) or data["de"]


def section_title(section_id: str, lang: str | None = None) -> str:
    lang = lang or current_language()
    return _pack(lang)["sections"][section_id]["title"]


def section_short(section_id: str, lang: str | None = None) -> str:
    lang = lang or current_language()
    return _pack(lang)["sections"][section_id]["short"]


def command_description(key: str, lang: str | None = None) -> str:
    lang = lang or current_language()
    pack = _pack(lang)
    desc = pack["commands"].get(key)
    if not desc:
        from notifications.telegram_commands.usage_hints import USAGE

        desc = USAGE.get(key, {}).get("menu_description", key)
    return desc


def prefixed_command_description(section_id: str, key: str, lang: str | None = None) -> str:
    short = section_short(section_id, lang)
    desc = command_description(key, lang)
    return f"{short} · {desc}"[:256]


def back_label(lang: str | None = None) -> str:
    return _pack(lang or current_language())["back"]


def home_intro(lang: str | None = None) -> str:
    return _pack(lang or current_language())["home_intro"]


def home_inline(lang: str | None = None) -> str:
    return _pack(lang or current_language())["home_inline"]


def section_pick(lang: str | None = None) -> str:
    return _pack(lang or current_language())["section_pick"]


def all_section_titles(lang: str | None = None) -> list[str]:
    lang = lang or current_language()
    return [section_title(sid, lang) for sid in _pack(lang)["sections"]]


def title_to_section_id(title: str) -> str | None:
    """Resolve tapped keyboard title in any supported language."""
    title = (title or "").strip()
    for lang in SUPPORTED_LANGS:
        sections = _pack(lang)["sections"]
        for sid, meta in sections.items():
            if meta["title"] == title:
                return sid
    return None


def is_back_label(text: str) -> bool:
    text = (text or "").strip()
    return text in {back_label("de"), back_label("en")}