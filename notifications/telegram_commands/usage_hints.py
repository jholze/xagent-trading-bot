"""Telegram command usage hints — loaded from locales/telegram_menu.json (DE/EN)."""

from __future__ import annotations

from notifications.telegram_commands.menu_i18n import (
    build_help_message,
    command_hint,
    current_language,
)

# Lazy-filled from locale JSON (German) for tests and legacy fallbacks.
USAGE: dict = {}


def _ensure_usage_cache() -> None:
    if USAGE:
        return
    from notifications.telegram_commands.menu_i18n import _load_menu_data

    data = _load_menu_data()
    de_cmds = data.get("de", {}).get("commands", {})
    for key, raw in de_cmds.items():
        if isinstance(raw, str):
            entry = {"description": raw}
        else:
            entry = dict(raw)
        USAGE[key] = {
            "menu_description": entry.get("description", ""),
            "help_line": entry.get("help_line", ""),
        }
        if entry.get("hint"):
            USAGE[key]["hint"] = entry["hint"]
    USAGE["unknown"] = {"hint": data.get("de", {}).get("unknown_hint", "")}


def hint(key: str) -> str:
    return command_hint(key, current_language())