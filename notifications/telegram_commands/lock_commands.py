"""Telegram /lock and /unlock — per-position auto-sell / DCA / eviction hold."""

from __future__ import annotations

import time
import uuid
from html import escape as _esc
from typing import Any, Callable

from notifications.telegram_commands.command_context import activate_command, clear_context
from notifications.telegram_commands.position_display import (
    position_symbol,
    resolve_position_by_symbol,
)
from notifications.telegram_commands.usage_hints import hint
from notifications.telegram_i18n import t
from price_fetcher import get_prices_batch
from strategies.position_lock import (
    DEFAULT_MODES,
    build_lock,
    get_lock,
    lock_is_active,
    lock_summary,
    parse_duration_to_until,
    position_locks_enabled,
)
from strategies.positions import (
    get_position,
    is_open_position,
    list_active_positions,
    set_position_lock,
)
from telegram_notifier import (
    answer_callback_query,
    send_telegram_buttons,
    send_telegram_message,
)

LOCK_CONFIRM_TTL_SEC = 60.0

_clock: Callable[[], float] = time.monotonic
_pending_lock: dict[str, dict[str, Any]] = {}


def reset_lock_confirm_for_tests(*, clock: Callable[[], float] | None = None) -> None:
    """Drop pending lock tokens; optionally pin the clock."""
    global _clock
    _pending_lock.clear()
    _clock = clock or time.monotonic


def _now() -> float:
    return float(_clock())


def _create_lock_token(
    *,
    symbol: str,
    timeframe: str,
    lock: dict[str, Any],
    ttl: float = LOCK_CONFIRM_TTL_SEC,
) -> str:
    token = uuid.uuid4().hex[:12]
    _pending_lock[token] = {
        "expires_at": _now() + float(ttl),
        "symbol": symbol,
        "timeframe": timeframe,
        "lock": dict(lock),
    }
    return token


def consume_lock_token(token: str) -> dict[str, Any] | None:
    rec = _pending_lock.pop(token, None)
    if rec is None:
        return None
    if _now() >= float(rec.get("expires_at") or 0):
        return None
    return rec


def _fmt_lock(lock: dict | None) -> str:
    if not lock or not lock_is_active(lock):
        return "unlocked"
    from strategies.position_lock import lock_modes

    modes = ",".join(sorted(lock_modes(lock)) or list(DEFAULT_MODES))
    until = lock.get("until") or "∞"
    why = lock.get("reason") or ""
    by = lock.get("locked_by") or ""
    return f"modes=[{modes}] until={until} reason={why} by={by}"


def _resolve_open(query: str):
    active = list_active_positions()
    if not active:
        return None, "Keine offenen Positionen."
    symbols = [position_symbol(p) for p in active]
    prices = get_prices_batch(symbols)
    p = resolve_position_by_symbol(active, query, prices)
    if not p:
        return None, f"Keine offene Position für <code>{query.upper()}</code>."
    return p, None


def _locked_reply(sym: str, tf: str, lock: dict[str, Any]) -> str:
    until_s = lock.get("until") or "permanent"
    return (
        f"🔒 <b>Locked</b> <code>{_esc(sym)}</code> ({_esc(tf)})\n"
        f"modes: <code>{_esc(','.join(lock.get('modes') or []))}</code>\n"
        f"until: <code>{_esc(str(until_s))}</code>\n"
        f"reason: <i>{_esc(str(lock.get('reason') or ''))}</i>\n\n"
        f"Auto-Sell / Trail / Eviction blockiert.\n"
        f"DCA + Sniper bleiben erlaubt (Lock = nur Verkaufs-Hold).\n"
        f"Manueller <code>/sell</code> bleibt möglich.\n"
        f"Unlock: <code>/unlock {_esc(sym.split('/')[0])}</code>"
    )


def _apply_pending_lock(rec: dict[str, Any], chat_id: str | int | None = None) -> None:
    clear_context(chat_id)
    sym = str(rec.get("symbol") or "")
    tf = str(rec.get("timeframe") or "1h")
    lock = rec.get("lock")
    if not sym or not isinstance(lock, dict):
        send_telegram_message(t("lock_confirm_expired"))
        return
    set_position_lock(sym, tf, lock, persist=True)
    send_telegram_message(_locked_reply(sym, tf, lock))


def handle(text: str) -> bool:
    lower = (text or "").strip()
    if not lower:
        return False

    if lower == "/lock" or lower.startswith("/lock "):
        return _handle_lock(lower)
    if lower == "/unlock" or lower.startswith("/unlock "):
        return _handle_unlock(lower)
    return False


def handle_callback(callback_query: dict) -> bool:
    data = str((callback_query or {}).get("data") or "")
    if not (data.startswith("lock_ok:") or data.startswith("lock_no:")):
        return False
    answer_callback_query(callback_query.get("id"))
    parts = data.split(":", 1)
    if len(parts) != 2 or not parts[1]:
        send_telegram_message(t("lock_confirm_expired"))
        return True
    action, token = parts
    if action == "lock_no":
        _pending_lock.pop(token, None)
        send_telegram_message(t("lock_confirm_cancelled"))
        return True
    if action == "lock_ok":
        from logger import log

        chat_id = ((callback_query.get("message") or {}).get("chat") or {}).get("id")
        if not chat_id:
            log("lock_ok callback missing chat id", "WARNING")
            return True
        rec = consume_lock_token(token)
        if rec is None:
            send_telegram_message(t("lock_confirm_expired"))
            return True
        _apply_pending_lock(rec, chat_id)
        return True
    return True


def _handle_lock(text: str) -> bool:
    if not position_locks_enabled():
        send_telegram_message(
            "⚠️ Position-Locks sind deaktiviert "
            "(<code>risk.position_locks.enabled=false</code>)."
        )
        return True

    parts = [p for p in text.split() if p.strip()]
    # /lock  |  /lock SYMBOL [duration] [reason...]
    if len(parts) == 1:
        active = list_active_positions()
        if not active:
            send_telegram_message("Keine offenen Positionen zum Locken.")
            return True
        activate_command("lock")
        lines = ["<b>🔒 Position Locks</b>", ""]
        any_lock = False
        for p in active:
            sym = position_symbol(p)
            tf = p.get("timeframe") or "1h"
            pos = get_position(sym, tf)
            lock = get_lock(pos)
            if lock and lock_is_active(lock):
                any_lock = True
                lines.append(
                    f"• <code>{sym}</code> {lock_summary(pos) or _fmt_lock(lock)}"
                )
        if not any_lock:
            lines.append("<i>Keine gelockten Positionen.</i>")
        lines.append("")
        lines.append(
            "Locken: <code>/lock BLESS</code> · "
            "<code>/lock BLESS 24h manual_hold</code> · "
            "<code>/lock BLESS permanent</code>"
        )
        lines.append("Unlock: <code>/unlock BLESS</code>")
        send_telegram_message("\n".join(lines))
        return True

    clear_context()
    sym_q = parts[1]
    duration_tok = None
    reason_parts: list[str] = []
    rest = parts[2:]
    if rest:
        cand = rest[0].lower()
        if (
            cand in ("permanent", "forever", "perm", "inf", "infinite", "lock", "0")
            or cand.isdigit()
            or (len(cand) > 1 and cand[-1] in "hdm" and cand[:-1].isdigit())
        ):
            duration_tok = rest[0]
            reason_parts = rest[1:]
        else:
            reason_parts = rest

    p, err = _resolve_open(sym_q)
    if err:
        send_telegram_message(err)
        return True

    sym = position_symbol(p)
    tf = p.get("timeframe") or "1h"
    if not is_open_position(get_position(sym, tf)):
        send_telegram_message(f"Keine offene Position für <code>{sym}</code>.")
        return True

    until = parse_duration_to_until(duration_tok)
    reason = " ".join(reason_parts).strip() or "telegram_lock"
    lock = build_lock(
        reason=reason[:120],
        locked_by="telegram",
        until=until,
        modes=DEFAULT_MODES,
    )
    token = _create_lock_token(symbol=sym, timeframe=tf, lock=lock)
    until_s = lock.get("until") or "permanent"
    keyboard = [
        [
            {"text": t("lock_confirm_btn_ok"), "callback_data": f"lock_ok:{token}"},
            {"text": t("lock_confirm_btn_no"), "callback_data": f"lock_no:{token}"},
        ]
    ]
    send_telegram_buttons(
        t(
            "lock_confirm",
            symbol=_esc(sym),
            until=_esc(str(until_s)),
            reason=_esc(str(lock.get("reason") or "")),
            modes=_esc(",".join(lock.get("modes") or [])),
        ),
        keyboard,
    )
    return True


def _handle_unlock(text: str) -> bool:
    parts = [p for p in text.split() if p.strip()]
    if len(parts) < 2:
        activate_command("unlock")
        send_telegram_message(hint("unlock"))
        return True

    clear_context()
    p, err = _resolve_open(parts[1])
    if err:
        send_telegram_message(err)
        return True

    sym = position_symbol(p)
    tf = p.get("timeframe") or "1h"
    pos = get_position(sym, tf)
    lock = get_lock(pos)
    if not lock:
        send_telegram_message(f"<code>{sym}</code> war nicht gelockt.")
        return True

    set_position_lock(sym, tf, None, persist=True)
    send_telegram_message(
        f"🔓 <b>Unlocked</b> <code>{sym}</code> ({tf})\n"
        f"Auto-Sell / Trail / Eviction wieder erlaubt."
    )
    return True
