"""Simple and very easy onboarding for new tenants via Telegram.

You (as operator) can now onboard a new user by simply sending a private message
to the bot in almost any format. No need for strict /onboard commands.

Examples of private messages you can send:

onboard max
token: 123456:AA...
key: GATEKEY123
secret: GATESECRET456

or even shorter:

max
123456:AA...
GATEKEY123
GATESECRET456

The bot will automatically:
- Create the tenant (auto-generates id if not provided)
- Seed a useful default watchlist
- Register the webhook for the new bot
- Send a welcome message directly to the new user in their bot

Only works in private chat with the operator (TELEGRAM_CHAT_ID).
"""

import os
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


def _is_operator() -> bool:
    op_chat = os.getenv("TELEGRAM_CHAT_ID", "")
    return str(current_chat_id()) == str(op_chat)


def _generate_tenant_id() -> str:
    chat = str(current_chat_id())[-6:]
    return f"user_{chat}" if chat else f"tenant_{secrets.token_hex(3)}"


def _looks_like_credential(value: str) -> bool:
    """Heuristic: does this string look like a plausible token/key/secret?

    Intentionally conservative.
    """
    v = (value or "").strip()
    if len(v) < 20:
        return False
    if " " in v:
        return False
    # Strong Telegram bot token signal
    if ":" in v and len(v) > 30:
        return True
    # Gate-style: long, dense alphanum, and contains uppercase or digits (natural language rarely does in this form)
    if len(v) >= 30:
        alphanum = sum(c.isalnum() for c in v)
        if alphanum / max(len(v), 1) > 0.85:
            has_upper_or_digit = any(c.isupper() or c.isdigit() for c in v)
            if has_upper_or_digit:
                return True
    return False


def _looks_like_onboarding_data(text: str) -> bool:
    """Detect if a normal private message looks like onboarding data.
    Stricter than before to avoid being fooled by random text.
    """
    t = text.lower().strip()

    # Key: value style — require all three credential indicators + at least one looks real
    has_token_kw = "token:" in t or "bot token" in t or "bottoken" in t
    has_key_kw = "key:" in t or "gate key" in t or "apikey" in t
    has_secret_kw = "secret:" in t or "gate secret" in t

    if has_token_kw and has_key_kw and has_secret_kw:
        # Extra: at least one value must look credential-like
        for line in text.splitlines():
            if ":" in line:
                val = line.split(":", 1)[1].strip()
                if _looks_like_credential(val):
                    return True
        # If keywords present but no long values, be conservative
        return False

    lines = [l.strip() for l in text.splitlines() if l.strip() and not l.lower().startswith("onboard")]

    # 4 lines: first may be tenant name, last three must look like credentials
    if len(lines) == 4:
        if all(_looks_like_credential(l) for l in lines[1:]):
            return True
        return False

    # 3 lines: must have credential indicators in first or lengths, and values look real
    if len(lines) == 3:
        first = lines[0]
        if (":" in first and len(first) > 15) or _looks_like_credential(first):
            if _looks_like_credential(lines[1]) and _looks_like_credential(lines[2]):
                return True

    return False


def _parse_onboarding_message(text: str) -> dict:
    """Parse a free-form private message for onboarding data."""
    data = {}
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    # Try key: value style first
    for line in lines:
        lower = line.lower()
        if ":" in line:
            key, val = line.split(":", 1)
            key = key.strip().lower().replace(" ", "_")
            val = val.strip()
            if "tenant" in key or key == "id":
                data["tenant_id"] = val
            elif "token" in key or "bot" in key:
                data["bot_token"] = val
            elif "key" in key:
                data["gate_key"] = val
            elif "secret" in key:
                data["gate_secret"] = val

    # Fallback: positional (4 or 3 values, first may be tenant or token)
    if not data.get("bot_token"):
        # Remove the word "onboard" (and variants) if present anywhere
        text_for_fallback = text
        for bad in ("onboard", "onboarding"):
            text_for_fallback = text_for_fallback.replace(bad, " ").replace(bad.upper(), " ")
        values = [v.strip() for v in text_for_fallback.split() if v.strip()]

        if len(values) >= 3:
            # Require that the credential values actually look plausible
            if len(values) == 4:
                if _looks_like_credential(values[1]) and _looks_like_credential(values[2]) and _looks_like_credential(values[3]):
                    data["tenant_id"] = values[0]
                    data["bot_token"] = values[1]
                    data["gate_key"] = values[2]
                    data["gate_secret"] = values[3]
            else:
                if _looks_like_credential(values[0]) and _looks_like_credential(values[1]) and _looks_like_credential(values[2]):
                    data["bot_token"] = values[0]
                    data["gate_key"] = values[1]
                    data["gate_secret"] = values[2]

    return data


def handle(text: str) -> bool:
    text = (text or "").strip()
    if not text:
        return False

    # Never swallow global help or menu commands (they have their own handlers later in router).
    # Protects /help onboarding, /help, etc. from the "onboard" substring or len-based hint.
    lower = text.lower()
    if lower.startswith(("/help", "/commands", "/?", "/menu", "/menü")):
        return False

    # Super simple private message onboarding (operator only)
    # The operator can just send the data as a normal private message.
    if _is_operator():
        # If we are already in an interactive onboarding session, let the continuation logic run.
        # This prevents a pasted token/key from the step-by-step flow from being misinterpreted
        # as a new full onboarding paste.
        ctx = get_context()
        if not (ctx and ctx.get("command") == "onboarding"):
            data = _parse_onboarding_message(text)

            # If we got the three required fields AND they look plausible, onboard immediately
            bt = data.get("bot_token", "")
            gk = data.get("gate_key", "")
            gs = data.get("gate_secret", "")
            if bt and gk and gs and _looks_like_credential(bt) and _looks_like_credential(gk) and _looks_like_credential(gs):
                tid = data.get("tenant_id") or _generate_tenant_id()
                return _perform_onboard(tid, bt, gk, gs)

            # Helpful hint only when not in interactive flow
            if _looks_like_onboarding_data(text) or (len(text) > 30 and ("token" in lower or "key" in lower or "secret" in lower)):
                send_telegram_message(
                    "Hinweis: Für Onboarding einfach die Daten als private Nachricht senden.\n\n"
                    "Vollständige Anleitung: <code>/help onboarding</code>\n\n"
                    "<code>max\n"
                    "123456:AA... (Token)\n"
                    "GATEKEY...\n"
                    "GATESECRET...</code>"
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

    parts = text.split(maxsplit=4)

    if len(parts) == 4:
        # /onboard <bot_token> <gate_key> <gate_secret> → auto tenant_id
        _, btoken, gkey, gsec = parts
        tid = _generate_tenant_id()
        return _perform_onboard(tid, btoken.strip(), gkey.strip(), gsec.strip())

    if len(parts) == 5:
        # full one-shot
        _, tid, btoken, gkey, gsec = parts
        return _perform_onboard(tid.strip(), btoken.strip(), gkey.strip(), gsec.strip())

    # start interactive
    set_context(current_chat_id(), "onboarding", step="tenant_id", data={})
    send_telegram_message(
        "🚀 <b>Onboarding gestartet</b>\n\n"
        "Du kannst jetzt einfach die Daten als private Nachricht senden (z.B.):\n\n"
        "<code>max\n"
        "token: 123456:AA...\n"
        "key: GATEKEY...\n"
        "secret: SECRET...</code>\n\n"
        "Vollständige Kurzanleitung: <code>/help onboarding</code>\n\n"
        "Oder Schritt für Schritt:\n"
        "Schritt 1/4: Sende gewünschte <code>tenant_id</code> oder schreibe <code>auto</code>:"
    )
    return True



def _continue_onboarding(text: str) -> bool:
    ctx = get_context()
    if not ctx:
        return False

    meta = ctx.get("meta", {})
    step = meta.get("step")
    data = meta.get("data", {}) or {}

    if step == "tenant_id":
        tid = text.strip()
        if tid.lower() == "auto":
            tid = _generate_tenant_id()
        data["tenant_id"] = tid
        set_context(current_chat_id(), "onboarding", step="bot_token", data=data)
        send_telegram_message("Schritt 2/4: Sende den <b>Bot-Token</b> von @BotFather:")
        return True

    if step == "bot_token":
        data["bot_token"] = text.strip()
        set_context(current_chat_id(), "onboarding", step="gate_key", data=data)
        send_telegram_message("Schritt 3/4: Sende <b>Gate API Key</b>:")
        return True

    if step == "gate_key":
        data["gate_key"] = text.strip()
        set_context(current_chat_id(), "onboarding", step="gate_secret", data=data)
        send_telegram_message("Schritt 4/4: Sende <b>Gate Secret</b>:")
        return True

    if step == "gate_secret":
        data["gate_secret"] = text.strip()
        tid = data.get("tenant_id") or _generate_tenant_id()
        btoken = data.get("bot_token", "")
        gkey = data.get("gate_key", "")
        gsec = data.get("gate_secret", "")

        success = _perform_onboard(tid, btoken, gkey, gsec)
        clear_context()
        return success

    clear_context()
    return False


def _perform_onboard(tenant_id: str, bot_token: str, gate_key: str, gate_secret: str) -> bool:
    if not all([tenant_id, bot_token, gate_key, gate_secret]):
        send_telegram_message("❌ Unvollständige Daten. Onboarding abgebrochen.")
        return True

    try:
        create_tenant(
            tenant_id=tenant_id,
            bot_token=bot_token,
            gate_api_key=gate_key,
            gate_api_secret=gate_secret,
            owner_chat_id=str(current_chat_id()),
            plan="pro",
            test=False,
        )

        # Seed sensible defaults for the new tenant
        from core.tenant_context import tenant_context
        from data_manager import save_config, save_watchlist

        from core.trading_profiles import build_tenant_seed_config

        with tenant_context(tenant_id, scope="paper"):
            save_config(build_tenant_seed_config("balanced"))
            save_watchlist(DEFAULT_WATCHLIST)

        # Register webhook automatically
        webhook_ok = set_webhook_for_bot(bot_token, tenant_id)

        # Send welcome message directly into the new user's bot
        welcome = (
            f"🎉 <b>Willkommen!</b>\n\n"
            f"Dein persönlicher Trading-Bot ist jetzt aktiv.\n"
            f"Tenant: <code>{tenant_id}</code>\n\n"
            f"Du kannst jetzt direkt mit mir chatten.\n"
            f"Schau dir <code>/help</code> und <code>/menu</code> an."
        )
        owner = str(current_chat_id())
        send_message_with_bot_token(bot_token, owner, welcome)

        # Nice confirmation for the operator
        base = (os.getenv("WEBHOOK_BASE_URL") or "").rstrip("/")
        webhook_url = f"{base}/webhook/{tenant_id}" if base else "(WEBHOOK_BASE_URL nicht gesetzt)"

        msg = (
            f"✅ <b>Tenant <code>{tenant_id}</code> erfolgreich onboarded!</b>\n\n"
            f"• Webhook: {'✅ automatisch gesetzt' if webhook_ok else '⚠️ manuell setzen'}\n"
            f"• Watchlist: 4 Coins vorgeladen\n"
            f"• Willkommensnachricht an User gesendet\n\n"
            f"<b>Webhook-URL:</b>\n<code>{webhook_url}</code>"
        )
        send_telegram_message(msg)

        log(f"[ONBOARDING] Tenant {tenant_id} erfolgreich erstellt", "INFO")
        return True

    except Exception as e:
        log(f"[ONBOARDING] Fehler beim Onboarden von {tenant_id}: {e}", "ERROR")
        send_telegram_message(f"❌ Onboarding fehlgeschlagen:\n<code>{e}</code>")
        return True
