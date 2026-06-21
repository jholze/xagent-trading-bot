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
    extra_path = path.parent / "telegram_section_help.json"
    if extra_path.exists():
        with open(extra_path, encoding="utf-8") as f:
            extra = json.load(f)
        for lang in SUPPORTED_LANGS:
            if lang in extra:
                _MENU_DATA.setdefault(lang, {}).update(extra[lang])
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


def _command_entry(pack: dict, key: str) -> dict:
    raw = pack.get("commands", {}).get(key)
    if isinstance(raw, str):
        return {"description": raw}
    if isinstance(raw, dict):
        return raw
    return {}


def section_title(section_id: str, lang: str | None = None) -> str:
    lang = lang or current_language()
    return _pack(lang)["sections"][section_id]["title"]


def section_short(section_id: str, lang: str | None = None) -> str:
    lang = lang or current_language()
    return _pack(lang)["sections"][section_id]["short"]


def command_description(key: str, lang: str | None = None) -> str:
    lang = lang or current_language()
    entry = _command_entry(_pack(lang), key)
    desc = entry.get("description")
    if not desc:
        from notifications.telegram_commands.usage_hints import USAGE

        desc = USAGE.get(key, {}).get("menu_description", key)
    return desc


def command_help_line(key: str, lang: str | None = None) -> str:
    lang = lang or current_language()
    entry = _command_entry(_pack(lang), key)
    line = entry.get("help_line")
    if not line:
        from notifications.telegram_commands.usage_hints import USAGE

        line = USAGE.get(key, {}).get("help_line", f"<code>/{key}</code>")
    return line


def command_hint(key: str, lang: str | None = None) -> str:
    lang = lang or current_language()
    pack = _pack(lang)
    entry = _command_entry(pack, key)
    if entry.get("hint"):
        return entry["hint"]
    return pack.get("unknown_hint", "❓")


def menu_button_label(lang: str | None = None) -> str:
    return _pack(lang or current_language()).get("button_text", "Menü")


def callback_unknown_command(lang: str | None = None) -> str:
    return _pack(lang or current_language()).get("callback_unknown_command", "Unknown command")


def build_help_message(lang: str | None = None) -> str:
    lang = lang or current_language()
    pack = _pack(lang)
    help_cfg = pack.get("help", {})
    lines = [help_cfg.get("title", ""), ""]
    for tip in help_cfg.get("tips", []):
        lines.append(tip)
    lines.append("")
    for section in help_cfg.get("sections", []):
        lines.append(section.get("title", ""))
        for key in section.get("keys", []):
            lines.append(command_help_line(key, lang))
        lines.append("")
    footer = help_cfg.get("footer")
    if footer:
        lines.append(footer)
    return "\n".join(lines)


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


def help_label(lang: str | None = None) -> str:
    return _pack(lang or current_language()).get("help_label", "❓ Hilfe")


def is_help_label(text: str) -> bool:
    text = (text or "").strip()
    return text in {help_label("de"), help_label("en")}


def short_input_invalid(command: str, lang: str | None = None) -> str:
    lang = lang or current_language()
    pack = _pack(lang)
    template = pack.get("short_input_invalid", "❌ Ungültige Eingabe für <code>/{cmd}</code>.")
    return template.replace("{cmd}", command)


def context_footer(key: str, lang: str | None = None, **kwargs) -> str:
    lang = lang or current_language()
    entry = _command_entry(_pack(lang), key)
    footer = entry.get("context_footer", "")
    for k, v in kwargs.items():
        footer = footer.replace(f"{{{k}}}", str(v))
    return footer


def build_section_help_message(section_id: str, lang: str | None = None) -> str:
    from notifications.telegram_commands.menu_commands import MENU_SECTIONS

    lang = lang or current_language()
    pack = _pack(lang)
    help_cfg = pack.get("section_help", {}).get(section_id, {})
    keys = dict(MENU_SECTIONS).get(section_id, [])
    lines = [help_cfg.get("title", section_title(section_id, lang)), ""]
    if help_cfg.get("intro"):
        lines.append(help_cfg["intro"])
        lines.append("")
    for key in keys:
        item = help_cfg.get("items", {}).get(key, {})
        lines.append(f"<b>/{key}</b> — {command_description(key, lang)}")
        if item.get("usage"):
            lines.append(f"  {item['usage']}")
        if item.get("example"):
            lines.append(f"  {item['example']}")
        elif command_help_line(key, lang):
            lines.append(f"  {command_help_line(key, lang)}")
        if item.get("tips"):
            lines.append(f"  <i>{item['tips']}</i>")
        lines.append("")
    footer = help_cfg.get("footer")
    if footer:
        lines.append(footer)
    return "\n".join(lines).rstrip()