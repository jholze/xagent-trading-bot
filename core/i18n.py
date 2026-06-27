"""Translation helpers (extracted from data_manager to keep ledger module thin)."""

from __future__ import annotations

import json
import locale

from logger import log

TRANSLATIONS: dict[str, dict] = {}


def load_translations() -> None:
    global TRANSLATIONS
    try:
        with open("locales/en.json", "r", encoding="utf-8") as f:
            TRANSLATIONS["en"] = json.load(f)
        with open("locales/de.json", "r", encoding="utf-8") as f:
            TRANSLATIONS["de"] = json.load(f)
    except Exception as e:
        log(f"Failed to load translation files: {e}", "WARNING")
        TRANSLATIONS.clear()
        TRANSLATIONS.update({"en": {}, "de": {}})


def get_system_lang() -> str:
    try:
        lang = locale.getdefaultlocale()[0] or "en_US"
        if lang.lower().startswith("de"):
            return "de"
        return "en"
    except Exception as e:
        log(f"Failed to detect system language: {e}", "WARNING")
        return "en"


def get_text(key: str, default: str = "") -> str:
    lang = get_system_lang()
    trans = TRANSLATIONS.get(lang, TRANSLATIONS.get("en", {}))
    return trans.get(key, default or key)


load_translations()