"""Operator onboarding for new tenants via Telegram.

Paper-only (staging): tenant id (+ optional owner chat_id) is enough — no Gate keys.
Optional BYOB: custom bot token + Gate credentials for live later.

Examples (private message, operator only):

  /onboard henry
  onboard henry 987654321
  henry
  key: GATEKEY...   (only when adding live keys later)

Shared bot: uses TELEGRAM_BOT_TOKEN from env when no bot token is given.
"""

from __future__ import annotations

import os
import re
import secrets

from logger import log
from notifications.telegram_commands.command_context import (
    clear_context,
    current_chat_id,
    get_context,
    set_context,
)
from storage.tenant_registry import create_tenant
from telegram_notifier import send_telegram_message, set_webhook_for_bot, send_message_with_bot_token


DEFAULT_WATCHLIST = [
    {"symbol": "BTC/USDT", "active": True},
    {"symbol": "ETH/USDT", "active": True},
    {"symbol": "SOL/USDT", "active": True},
    {"symbol": "PEPE/USDT", "active": True},
]

_TENANT_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}$")
_SKIP_VALUES = frozenset({"-", "skip", "auto", "none", "nein", "no", "überspringen", "shared"})


def _is_operator() -> bool:
    op_chat = os.getenv("TELEGRAM_CHAT_ID", "")
    return str(current_chat_id()) == str(op_chat)


def _generate_tenant_id() -> str:
    chat = str(current_chat_id())[-6:]
    return f"user_{chat}" if chat else f"tenant_{secrets.token_hex(3)}"


def _is_skip(value: str) -> bool:
    return (value or "").strip().lower() in _SKIP_VALUES


def _is_valid_tenant_id(value: str) -> bool:
    return bool(_TENANT_ID_RE.match((value or "").strip().lower()))


def _normalize_tenant_id(value: str) -> str:
    return (value or "").strip().lower()


def _shared_bot_token() -> str:
    return (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()


def _resolve_bot_token(provided: str | None) -> str:
    token = (provided or "").strip()
    if token and not _is_skip(token):
        return token
    return _shared_bot_token()


def _looks_like_credential(value: str) -> bool:
    """Heuristic: does this string look like a plausible token/key/secret?"""
    v = (value or "").strip()
    if len(v) < 20:
        return False
    if " " in v:
        return False
    if ":" in v and len(v) > 30:
        return True
    if len(v) >= 30:
        alphanum = sum(c.isalnum() for c in v)
        if alphanum / max(len(v), 1) > 0.85:
            has_upper_or_digit = any(c.isupper() or c.isdigit() for c in v)
            if has_upper_or_digit:
                return True
    return False


def _looks_like_owner_chat_id(value: str) -> bool:
    v = (value or "").strip()
    return v.isdigit() and len(v) >= 5


def _parse_onboarding_message(text: str) -> dict:
    """Parse a free-form private message for onboarding data."""
    data: dict[str, str] = {}
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    for line in lines:
        lower = line.lower()
        if ":" in line:
            key, val = line.split(":", 1)
            key = key.strip().lower().replace(" ", "_")
            val = val.strip()
            if "tenant" in key or key == "id":
                data["tenant_id"] = val
            elif "chat" in key:
                data["owner_chat_id"] = val
            elif "token" in key or "bot" in key:
                data["bot_token"] = val
            elif "key" in key:
                data["gate_key"] = val
            elif "secret" in key:
                data["gate_secret"] = val

    if data.get("tenant_id") or data.get("bot_token"):
        return data

    text_for_fallback = text
    for bad in ("onboard", "onboarding"):
        text_for_fallback = text_for_fallback.replace(bad, " ").replace(bad.upper(), " ")
    values = [v.strip() for v in text_for_fallback.split() if v.strip()]

    if len(values) == 1 and _is_valid_tenant_id(values[0]):
        data["tenant_id"] = values[0]
        data["paper_only"] = "1"
        return data

    if len(values) == 2 and _is_valid_tenant_id(values[0]) and _looks_like_owner_chat_id(values[1]):
        data["tenant_id"] = values[0]
        data["owner_chat_id"] = values[1]
        data["paper_only"] = "1"
        return data

    if len(values) == 4 and _is_valid_tenant_id(values[0]):
        if all(_looks_like_credential(v) for v in values[1:]):
            data["tenant_id"] = values[0]
            data["bot_token"] = values[1]
            data["gate_key"] = values[2]
            data["gate_secret"] = values[3]
        return data

    if len(values) == 3 and all(_looks_like_credential(v) for v in values):
        data["bot_token"] = values[0]
        data["gate_key"] = values[1]
        data["gate_secret"] = values[2]
        return data

    if len(lines) == 1 and _is_valid_tenant_id(lines[0]):
        data["tenant_id"] = lines[0]
        data["paper_only"] = "1"
    elif len(lines) == 2 and _is_valid_tenant_id(lines[0]) and _looks_like_owner_chat_id(lines[1]):
        data["tenant_id"] = lines[0]
        data["owner_chat_id"] = lines[1]
        data["paper_only"] = "1"

    return data


def _paper_onboard_from_data(data: dict) -> bool | None:
    """Return True/False if handled, None if data is not a paper-only onboard request."""
    tid = data.get("tenant_id", "")
    if not _is_valid_tenant_id(tid):
        return None
    if data.get("bot_token") and _looks_like_credential(data.get("bot_token", "")):
        return None
    owner = data.get("owner_chat_id") or ""
    if owner and not _looks_like_owner_chat_id(owner) and not _is_skip(owner):
        return None
    return _perform_onboard(
        _normalize_tenant_id(tid),
        bot_token=data.get("bot_token", ""),
        gate_key=data.get("gate_key", ""),
        gate_secret=data.get("gate_secret", ""),
        owner_chat_id=owner or None,
        paper_only=True,
    )


def _full_onboard_from_data(data: dict) -> bool | None:
    bt = data.get("bot_token", "")
    gk = data.get("gate_key", "")
    gs = data.get("gate_secret", "")
    if not bt or not _looks_like_credential(bt):
        return None
    if gk and not _looks_like_credential(gk):
        return None
    if gs and not _looks_like_credential(gs):
        return None
    tid = data.get("tenant_id") or _generate_tenant_id()
    return _perform_onboard(
        _normalize_tenant_id(tid),
        bot_token=bt,
        gate_key=gk,
        gate_secret=gs,
        owner_chat_id=data.get("owner_chat_id") or None,
        paper_only=not (gk and gs),
    )


def handle(text: str) -> bool:
    text = (text or "").strip()
    if not text:
        return False

    lower = text.lower()
    if lower.startswith(("/help", "/commands", "/?", "/menu", "/menü")):
        return False

    if _is_operator():
        ctx = get_context()
        if not (ctx and ctx.get("command") == "onboarding"):
            if lower.startswith("onboard "):
                rest = text.split(maxsplit=1)[1].strip()
                parts = rest.split()
                if len(parts) == 1 and _is_valid_tenant_id(parts[0]):
                    return _perform_onboard(_normalize_tenant_id(parts[0]), paper_only=True)
                if len(parts) == 2 and _is_valid_tenant_id(parts[0]) and _looks_like_owner_chat_id(parts[1]):
                    return _perform_onboard(
                        _normalize_tenant_id(parts[0]),
                        owner_chat_id=parts[1],
                        paper_only=True,
                    )

            data = _parse_onboarding_message(text)
            paper = _paper_onboard_from_data(data)
            if paper is not None:
                return paper
            full = _full_onboard_from_data(data)
            if full is not None:
                return full

            if (
                "token" in lower
                or "key" in lower
                or "secret" in lower
                or _is_valid_tenant_id(text)
            ):
                send_telegram_message(
                    "Hinweis: Paper-Onboarding (staging) — nur Tenant-ID reicht:\n\n"
                    "<code>/onboard henry</code>\n"
                    "oder <code>henry</code> / <code>henry 123456789</code> (Chat-ID)\n\n"
                    "Gate-Keys optional (nur für Live später).\n"
                    "Vollständig: <code>/help onboarding</code>"
                )
                return True

    if not text.startswith("/onboard"):
        ctx = get_context()
        if ctx and ctx.get("command") == "onboarding":
            return _continue_onboarding(text)
        return False

    if not _is_operator():
        send_telegram_message("❌ Nur der Operator kann neue User onboarden.")
        return True

    parts = text.split()

    if len(parts) == 2 and _is_valid_tenant_id(parts[1]):
        return _perform_onboard(_normalize_tenant_id(parts[1]), paper_only=True)

    if len(parts) == 3 and _is_valid_tenant_id(parts[1]) and _looks_like_owner_chat_id(parts[2]):
        return _perform_onboard(
            _normalize_tenant_id(parts[1]),
            owner_chat_id=parts[2],
            paper_only=True,
        )

    if len(parts) == 4:
        _, btoken, gkey, gsec = text.split(maxsplit=3)
        tid = _generate_tenant_id()
        return _perform_onboard(
            tid,
            bot_token=btoken.strip(),
            gate_key=gkey.strip(),
            gate_secret=gsec.strip(),
            paper_only=not (_looks_like_credential(gkey) and _looks_like_credential(gsec)),
        )

    if len(parts) == 5:
        _, tid, btoken, gkey, gsec = text.split(maxsplit=4)
        return _perform_onboard(
            _normalize_tenant_id(tid),
            bot_token=btoken.strip(),
            gate_key=gkey.strip(),
            gate_secret=gsec.strip(),
            paper_only=not (_looks_like_credential(gkey) and _looks_like_credential(gsec)),
        )

    set_context(current_chat_id(), "onboarding", step="tenant_id", data={})
    send_telegram_message(
        "🚀 <b>Onboarding gestartet</b>\n\n"
        "<b>Paper (staging):</b> <code>/onboard henry</code> reicht.\n"
        "Optional Chat-ID: <code>/onboard henry 123456789</code>\n\n"
        "Schritt 1/5: <code>tenant_id</code> (z.B. <code>henry</code>) oder <code>auto</code>:"
    )
    return True


def _continue_onboarding(text: str) -> bool:
    ctx = get_context()
    if not ctx:
        return False

    meta = ctx.get("meta", {})
    step = meta.get("step")
    data = meta.get("data", {}) or {}
    raw = text.strip()

    if step == "tenant_id":
        tid = raw
        if tid.lower() == "auto":
            tid = _generate_tenant_id()
        if not _is_valid_tenant_id(tid):
            send_telegram_message("❌ Ungültige tenant_id (a-z, 0-9, _, 2–31 Zeichen).")
            return True
        data["tenant_id"] = _normalize_tenant_id(tid)
        set_context(current_chat_id(), "onboarding", step="owner_chat_id", data=data)
        send_telegram_message(
            "Schritt 2/5: <b>Owner Chat-ID</b> des Users (oder <code>skip</code> für später):"
        )
        return True

    if step == "owner_chat_id":
        if not _is_skip(raw):
            if not _looks_like_owner_chat_id(raw):
                send_telegram_message("❌ Chat-ID muss numerisch sein (mind. 5 Ziffern) oder <code>skip</code>.")
                return True
            data["owner_chat_id"] = raw
        set_context(current_chat_id(), "onboarding", step="bot_token", data=data)
        send_telegram_message(
            "Schritt 3/5: <b>Bot-Token</b> (oder <code>skip</code> = gemeinsamer Staging-Bot):"
        )
        return True

    if step == "bot_token":
        if not _is_skip(raw):
            data["bot_token"] = raw
        set_context(current_chat_id(), "onboarding", step="gate_key", data=data)
        send_telegram_message(
            "Schritt 4/5: <b>Gate API Key</b> (oder <code>skip</code> = nur Paper):"
        )
        return True

    if step == "gate_key":
        if not _is_skip(raw):
            data["gate_key"] = raw
        set_context(current_chat_id(), "onboarding", step="gate_secret", data=data)
        send_telegram_message(
            "Schritt 5/5: <b>Gate Secret</b> (oder <code>skip</code> = nur Paper):"
        )
        return True

    if step == "gate_secret":
        if not _is_skip(raw):
            data["gate_secret"] = raw
        tid = data.get("tenant_id") or _generate_tenant_id()
        gk = data.get("gate_key", "")
        gs = data.get("gate_secret", "")
        success = _perform_onboard(
            tid,
            bot_token=data.get("bot_token", ""),
            gate_key=gk,
            gate_secret=gs,
            owner_chat_id=data.get("owner_chat_id") or None,
            paper_only=not (gk and gs),
        )
        clear_context()
        return success

    clear_context()
    return False


def _perform_onboard(
    tenant_id: str,
    *,
    bot_token: str = "",
    gate_key: str = "",
    gate_secret: str = "",
    owner_chat_id: str | None = None,
    paper_only: bool = False,
) -> bool:
    tid = _normalize_tenant_id(tenant_id)
    if not _is_valid_tenant_id(tid):
        send_telegram_message("❌ Ungültige tenant_id.")
        return True

    resolved_token = _resolve_bot_token(bot_token)
    if not resolved_token:
        send_telegram_message(
            "❌ Kein Bot-Token — setze TELEGRAM_BOT_TOKEN auf Staging oder gib einen Token an."
        )
        return True

    gk = (gate_key or "").strip()
    gs = (gate_secret or "").strip()
    if _is_skip(gk):
        gk = ""
    if _is_skip(gs):
        gs = ""
    if paper_only or not (gk and gs):
        gk = gk if _looks_like_credential(gk) else ""
        gs = gs if _looks_like_credential(gs) else ""
        paper_only = True

    owner = (owner_chat_id or "").strip()
    shared_bot = resolved_token == _shared_bot_token()

    try:
        create_tenant(
            tenant_id=tid,
            bot_token="" if shared_bot else resolved_token,
            gate_api_key=gk,
            gate_api_secret=gs,
            owner_chat_id=owner,
            plan="pro",
            test=False,
        )

        from core.tenant_context import tenant_context
        from core.trading_profiles import build_tenant_seed_config
        from data_manager import save_config, save_watchlist

        with tenant_context(tid, scope="paper"):
            save_config(build_tenant_seed_config("balanced"))
            save_watchlist(DEFAULT_WATCHLIST)

        webhook_ok = True
        if not shared_bot:
            webhook_ok = set_webhook_for_bot(resolved_token, tid)

        mode = "Paper" if paper_only else "Paper+Gate"
        welcome = (
            f"🎉 <b>Willkommen!</b>\n\n"
            f"Dein Trading-Bot ist aktiv (<b>{mode}</b>).\n"
            f"Tenant: <code>{tid}</code>\n\n"
            f"<code>/help</code> · <code>/menu</code>"
        )
        if owner:
            send_message_with_bot_token(resolved_token, owner, welcome)

        base = (os.getenv("WEBHOOK_BASE_URL") or "").rstrip("/")
        if shared_bot:
            webhook_note = "gemeinsamer Bot (kein Extra-Webhook)"
            webhook_url = f"{base}/" if base else "(Haupt-Webhook)"
        else:
            webhook_note = "✅ gesetzt" if webhook_ok else "⚠️ manuell setzen"
            webhook_url = f"{base}/webhook/{tid}" if base else "(WEBHOOK_BASE_URL nicht gesetzt)"

        gate_note = "nicht gesetzt (Paper)" if paper_only else "gespeichert"
        from notifications.telegram_commands.tenant_link_commands import invite_message_for_operator

        owner_line = (
            f"• Owner chat: <code>{owner}</code>\n"
            if owner
            else "• Owner: <i>wartet auf Einladung</i>\n"
        )
        msg = (
            f"✅ <b>Tenant <code>{tid}</code> onboarded</b> ({mode})\n\n"
            f"• Bot: {'gemeinsam' if shared_bot else 'eigener Token'}\n"
            f"• Gate: {gate_note}\n"
            f"{owner_line}"
            f"• Webhook: {webhook_note}\n"
            f"• Watchlist: 4 Coins\n\n"
            f"<b>URL:</b> <code>{webhook_url}</code>"
        )
        if not owner:
            msg += "\n\n" + invite_message_for_operator(tid)
        send_telegram_message(msg)

        log(f"[ONBOARDING] Tenant {tid} erstellt mode={mode} shared_bot={shared_bot}", "INFO")
        return True

    except Exception as e:
        log(f"[ONBOARDING] Fehler beim Onboarden von {tid}: {e}", "ERROR")
        send_telegram_message(f"❌ Onboarding fehlgeschlagen:\n<code>{e}</code>")
        return True