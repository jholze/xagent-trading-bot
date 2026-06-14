"""Register Telegram Bot Commands for the native menu button beside the input field."""

from __future__ import annotations

import os
import re

import requests

from logger import log
from notifications.telegram_commands.menu_commands import MENU_SECTIONS, all_menu_command_keys
from notifications.telegram_commands.menu_commands import send_main_section_keyboard
from notifications.telegram_commands.menu_i18n import (
    SUPPORTED_LANGS,
    menu_button_label,
    prefixed_command_description,
    resolve_language,
)

_COMMAND_RE = re.compile(r"^[a-z0-9_]{1,32}$")
_MAX_DESCRIPTION_LEN = 256
_MAX_BUTTON_TEXT_LEN = 64
_DEFAULT_BUTTON_TEXT = "Menü"

TELEGRAM_MENU_COMMAND_KEYS: list[str] = all_menu_command_keys()


def menu_button_text(lang: str | None = None) -> str:
    try:
        from core.config import get_bot_config

        cfg = get_bot_config().telegram_command_menu_config
        if not cfg.get("enabled", True):
            return _DEFAULT_BUTTON_TEXT
        override = str(cfg.get("button_text") or "").strip()
        if override:
            text = override
        else:
            text = menu_button_label(lang or resolve_language(cfg.get("default_language")))
    except Exception:
        text = menu_button_label(lang)
    if not text:
        text = _DEFAULT_BUTTON_TEXT
    if len(text) > _MAX_BUTTON_TEXT_LEN:
        text = text[:_MAX_BUTTON_TEXT_LEN]
    return text


def menu_button_payload(lang: str | None = None) -> dict:
    return {
        "type": "commands",
        "text": menu_button_text(lang),
    }


def all_bot_commands(lang: str = "de") -> list[dict[str, str]]:
    commands = []
    seen: set[str] = set()
    for section_id, keys in MENU_SECTIONS:
        for key in keys:
            if key in seen:
                raise ValueError(f"Duplicate menu command key: {key}")
            seen.add(key)
            if not _COMMAND_RE.match(key):
                raise ValueError(f"Invalid Telegram command name: {key}")
            description = prefixed_command_description(section_id, key, lang).strip()
            if not description:
                raise ValueError(f"Empty menu description for: {key}")
            if len(description) > _MAX_DESCRIPTION_LEN:
                raise ValueError(
                    f"Menu description too long for {key}: {len(description)} chars"
                )
            commands.append({"command": key, "description": description})
    return commands


def register_bot_commands(token: str | None = None) -> bool:
    """Publish DE + EN commands; Telegram picks by user language_code."""
    token = token or os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        log("Telegram command menu: TELEGRAM_BOT_TOKEN not set", "WARNING")
        return False

    try:
        from core.config import get_bot_config

        cfg = get_bot_config().telegram_command_menu_config
        if not cfg.get("enabled", True):
            log("Telegram command menu disabled in config", "INFO")
            return False
        default_lang = resolve_language(cfg.get("default_language"))
    except Exception:
        default_lang = "de"

    button = menu_button_payload(lang=default_lang)
    base = f"https://api.telegram.org/bot{token}"

    try:
        for lang in SUPPORTED_LANGS:
            commands = all_bot_commands(lang)
            resp = requests.post(
                f"{base}/setMyCommands",
                json={"commands": commands, "language_code": lang},
                timeout=10,
            )
            data = resp.json() if resp.content else {}
            if not resp.ok or not data.get("ok"):
                log(
                    f"Telegram setMyCommands({lang}) failed: {data.get('description', resp.text)}",
                    "WARNING",
                )
                return False

        fallback = all_bot_commands(default_lang)
        resp = requests.post(
            f"{base}/setMyCommands",
            json={"commands": fallback},
            timeout=10,
        )
        data = resp.json() if resp.content else {}
        if not resp.ok or not data.get("ok"):
            log(
                f"Telegram setMyCommands(default) failed: {data.get('description', resp.text)}",
                "WARNING",
            )
            return False

        menu_resp = requests.post(
            f"{base}/setChatMenuButton",
            json={"menu_button": button},
            timeout=10,
        )
        menu_data = menu_resp.json() if menu_resp.content else {}
        if not menu_resp.ok or not menu_data.get("ok"):
            log(
                f"Telegram setChatMenuButton failed: {menu_data.get('description', menu_resp.text)}",
                "WARNING",
            )
            return False

        log(
            f"Telegram command menu registered ({len(fallback)} commands, "
            f"langs de+en, button: {button['text']!r})",
            "INFO",
        )
        from notifications.telegram_commands.menu_i18n import set_user_language

        set_user_language(default_lang)
        send_main_section_keyboard(lang=default_lang)
        return True
    except Exception as e:
        log(f"Telegram command menu registration error: {e}", "WARNING")
        return False