"""Telegram ``/xai_login`` — operator-only trigger for the SuperGrok device-code login (#397).

The login itself runs on the ``xagent-xai-auth`` Railway sidecar
(``services/xai_auth_sidecar``); this command only pokes its ``POST /login``
over Railway private networking. The sidecar then sends the verification URL +
user code to the operator chat itself, and "session stored, expires …" once the
poll succeeds. Nothing here ever sees or prints an access/refresh token.

Gate: same as ``/onboard`` — only ``TELEGRAM_CHAT_ID`` (``_is_operator``).
Off by default: without ``XAI_AUTH_SIDECAR_URL`` the command only explains
how to configure it.

  /xai_login          start the device-code login on the sidecar
  /xai_login status   credential present? expiry? login running?
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from html import escape as html_escape

from logger import log
from notifications.telegram_commands.onboarding_commands import _is_operator
from telegram_notifier import send_telegram_message

SIDECAR_URL_VAR = "XAI_AUTH_SIDECAR_URL"
TRIGGER_TOKEN_VAR = "XAI_AUTH_TRIGGER_TOKEN"
_TIMEOUT_SEC = 8.0
_COMMAND = "/xai_login"


def _sidecar_url() -> str:
    return (os.getenv(SIDECAR_URL_VAR) or "").strip().rstrip("/")


def _trigger_token() -> str:
    return (os.getenv(TRIGGER_TOKEN_VAR) or "").strip()


def _request(method: str, path: str, *, token: str = "") -> tuple[int, dict]:
    """Call the sidecar. Returns (http_status, json_body); status 0 on transport error."""
    url = f"{_sidecar_url()}{path}"
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SEC) as resp:  # noqa: S310 - internal URL from env
            return int(resp.status), _parse_json(resp.read())
    except urllib.error.HTTPError as e:
        try:
            body = _parse_json(e.read())
        except Exception:
            body = {}
        return int(e.code), body
    except Exception as e:  # URLError, timeout, ConnectionRefused …
        log(f"/xai_login: sidecar unreachable ({type(e).__name__}: {e})", "WARNING")
        return 0, {"error": f"{type(e).__name__}"}


def _parse_json(raw: bytes) -> dict:
    try:
        data = json.loads(raw.decode("utf-8") or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _fmt_status(body: dict) -> str:
    """Token-free status text; only whitelisted fields are read from the sidecar reply."""
    cred = body.get("credential") if isinstance(body.get("credential"), dict) else {}
    login = body.get("login") if isinstance(body.get("login"), dict) else {}
    keep = body.get("keepalive") if isinstance(body.get("keepalive"), dict) else {}
    lines = ["🔐 <b>xAI SuperGrok session</b>"]
    if cred.get("present"):
        exp = html_escape(str(cred.get("expiresAt") or "?"))
        soon = " ⚠️ expiring soon" if cred.get("expiringSoon") else ""
        lines.append(f"Credential: stored, expires {exp}{soon}")
    else:
        err = cred.get("error")
        lines.append("Credential: <b>none</b>" + (f" ({html_escape(str(err))})" if err else ""))
    if login.get("running"):
        lines.append(f"Login: running since {html_escape(str(login.get('startedAt') or '?'))}")
    elif login.get("lastResult"):
        tail = f" — {html_escape(str(login.get('lastError')))}" if login.get("lastError") else ""
        lines.append(f"Last login: {html_escape(str(login.get('lastResult')))}{tail}")
    if keep:
        state = "on" if keep.get("enabled") else "off"
        fails = int(keep.get("failures") or 0)
        lines.append(f"Keepalive: {state}" + (f", {fails} refresh failure(s)" if fails else ""))
    if body.get("triggerEnabled") is False:
        lines.append(f"Trigger: disabled on sidecar (set {TRIGGER_TOKEN_VAR} there)")
    return "\n".join(lines)


def _not_configured() -> str:
    return (
        "ℹ️ /xai_login is not configured.\n"
        f"Set <code>{SIDECAR_URL_VAR}</code> (e.g. <code>http://xagent-xai-auth.railway.internal:8787</code>) "
        f"and <code>{TRIGGER_TOKEN_VAR}</code> on the bot service.\n"
        "Alternative: <code>railway ssh</code> into xagent-xai-auth → "
        "<code>cd services/xai_auth_sidecar && npm run login</code> "
        "(see services/xai_auth_sidecar/README.md)."
    )


def handle(text: str) -> bool:
    text = (text or "").strip()
    if text != _COMMAND and not text.startswith(_COMMAND + " "):
        return False

    if not _is_operator():
        send_telegram_message("⛔ /xai_login: operator only.")
        return True

    if not _sidecar_url():
        send_telegram_message(_not_configured())
        return True

    arg = text[len(_COMMAND):].strip().lower()
    if arg == "status":
        code, body = _request("GET", "/status")
        if code == 200:
            send_telegram_message(_fmt_status(body))
        else:
            send_telegram_message(f"⚠️ xai-auth sidecar unreachable (HTTP {code}). Check the xagent-xai-auth service.")
        return True

    token = _trigger_token()
    if not token:
        send_telegram_message(
            f"⚠️ <code>{TRIGGER_TOKEN_VAR}</code> is not set on the bot service — cannot trigger the sidecar. "
            "Set the same value on bot and xagent-xai-auth."
        )
        return True

    code, body = _request("POST", "/login", token=token)
    if code == 202:
        tg = body.get("telegram")
        where = (
            "URL + code arrive here in a separate message from the sidecar."
            if tg
            else "Sidecar has no TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID — read URL + code from its Railway logs."
        )
        send_telegram_message(
            "🔐 xAI SuperGrok login started on the sidecar.\n"
            f"{where}\nOpen the URL, sign in with the X/SuperGrok account, enter the code. "
            "You get \"session stored\" when it succeeds."
        )
    elif code == 409:
        send_telegram_message("⏳ A login is already running — check for the URL + code message or Railway logs.")
    elif code == 403:
        send_telegram_message(
            f"⚠️ Sidecar refuses: trigger disabled. Set <code>{TRIGGER_TOKEN_VAR}</code> on xagent-xai-auth too."
        )
    elif code == 401:
        send_telegram_message(f"⚠️ Sidecar rejected the trigger token — <code>{TRIGGER_TOKEN_VAR}</code> differs between services.")
    elif code == 0:
        send_telegram_message(
            f"⚠️ xai-auth sidecar unreachable at <code>{html_escape(_sidecar_url())}</code>. "
            "Is xagent-xai-auth deployed and RUN_XAI_AUTH=1 set?"
        )
    else:
        err = html_escape(str(body.get("error") or ""))
        send_telegram_message(f"⚠️ Sidecar answered HTTP {code}{(' — ' + err) if err else ''}.")
    return True
