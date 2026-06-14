"""Register Telegram Bot Commands for the native menu button beside the input field."""

from __future__ import annotations

import os
import re

import requests

from logger import log
from notifications.telegram_commands.usage_hints import USAGE

_COMMAND_RE = re.compile(r"^[a-z0-9_]{1,32}$")
_MAX_DESCRIPTION_LEN = 256
_MAX_BUTTON_TEXT_LEN = 64
_DEFAULT_BUTTON_TEXT = "Menü"

# Canonical command names in menu display order (35 entries).
TELEGRAM_MENU_COMMAND_KEYS: list[str] = [
    "help",
    "list",
    "add",
    "remove",
    "buy",
    "sell",
    "positions",
    "orders",
    "risk",
    "mode",
    "maxpositions",
    "live_confirm",
    "live_cancel",
    "gate",
    "dryrun",
    "decisions",
    "why",
    "hermes",
    "hermes_last",
    "hermes_run",
    "cmc",
    "addx",
    "removex",
    "listx",
    "xposts",
    "xsignals",
    "xaccuracy",
    "tracktest",
    "testaccount",
    "sandbox",
    "sandbox_results",
    "sandbox_promote",
    "backtest",
    "backtest_lock",
    "backtest_results",
]


def menu_description(key: str) -> str:
    entry = USAGE.get(key) or {}
    desc = entry.get("menu_description") or entry.get("help_line", "")
    if not desc:
        raise ValueError(f"Missing menu_description for command key: {key}")
    return desc


def menu_button_text() -> str:
    try:
        from core.config import get_bot_config

        cfg = get_bot_config().telegram_command_menu_config
        if not cfg.get("enabled", True):
            return _DEFAULT_BUTTON_TEXT
        text = str(cfg.get("button_text") or _DEFAULT_BUTTON_TEXT).strip()
    except Exception:
        text = _DEFAULT_BUTTON_TEXT
    if not text:
        text = _DEFAULT_BUTTON_TEXT
    if len(text) > _MAX_BUTTON_TEXT_LEN:
        text = text[:_MAX_BUTTON_TEXT_LEN]
    return text


def menu_button_payload() -> dict:
    return {
        "type": "commands",
        "text": menu_button_text(),
    }


def all_bot_commands() -> list[dict[str, str]]:
    commands = []
    seen: set[str] = set()
    for key in TELEGRAM_MENU_COMMAND_KEYS:
        if key in seen:
            raise ValueError(f"Duplicate menu command key: {key}")
        seen.add(key)
        name = key
        if not _COMMAND_RE.match(name):
            raise ValueError(f"Invalid Telegram command name: {name}")
        description = menu_description(key).strip()
        if not description:
            raise ValueError(f"Empty menu description for: {name}")
        if len(description) > _MAX_DESCRIPTION_LEN:
            raise ValueError(
                f"Menu description too long for {name}: {len(description)} chars"
            )
        commands.append({"command": name, "description": description})
    return commands


def register_bot_commands(token: str | None = None) -> bool:
    """Publish commands to Telegram (setMyCommands + labeled menu button)."""
    token = token or os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        log("Telegram command menu: TELEGRAM_BOT_TOKEN not set", "WARNING")
        return False

    try:
        from core.config import get_bot_config

        if not get_bot_config().telegram_command_menu_config.get("enabled", True):
            log("Telegram command menu disabled in config", "INFO")
            return False
    except Exception:
        pass

    commands = all_bot_commands()
    button = menu_button_payload()
    base = f"https://api.telegram.org/bot{token}"

    try:
        resp = requests.post(
            f"{base}/setMyCommands",
            json={"commands": commands, "language_code": "de"},
            timeout=10,
        )
        data = resp.json() if resp.content else {}
        if not resp.ok or not data.get("ok"):
            log(
                f"Telegram setMyCommands failed: {data.get('description', resp.text)}",
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
            f"Telegram command menu registered ({len(commands)} commands, button: {button['text']!r})",
            "INFO",
        )
        return True
    except Exception as e:
        log(f"Telegram command menu registration error: {e}", "WARNING")
        return False