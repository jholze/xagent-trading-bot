"""Telegram /lock and /unlock — per-position auto-sell / DCA / eviction hold."""

from __future__ import annotations

import time
import uuid
from html import escape as _esc
from typing import Any, Callable

from notifications.telegram_commands.command_context import (
    activate_command,
    clear_context,
    get_context,
    set_chat_id,
)
from notifications.telegram_commands.position_display import (
    encode_lot_callback,
    long_lots_for_sell,
    lot_ticker,
    lot_timeframe,
    parse_lot_callback,
    position_symbol,
    resolve_position_by_symbol,
    resolve_position_by_symbol_tf,
)
from notifications.telegram_i18n import t
from price_fetcher import get_prices_batch
from strategies.position_lock import (
    DEFAULT_MODES,
    build_lock,
    get_lock,
    lock_is_active,
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
LOCK_POS_CALLBACK_PREFIX = "lockpos:"
LOCK_DUR_CALLBACK_PREFIX = "lockdur:"
UNLOCK_POS_CALLBACK_PREFIX = "unlockpos:"
LOCK_DURATION_PRESETS = ("24h", "7d", "permanent")

_clock: Callable[[], float] = time.monotonic
_pending_lock: dict[str, dict[str, Any]] = {}
_pending_unlock: dict[str, dict[str, Any]] = {}


def reset_lock_confirm_for_tests(*, clock: Callable[[], float] | None = None) -> None:
    """Drop pending lock tokens; optionally pin the clock."""
    global _clock
    _pending_lock.clear()
    _pending_unlock.clear()
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


def _create_unlock_token(
    *,
    symbol: str,
    timeframe: str,
    ttl: float = LOCK_CONFIRM_TTL_SEC,
) -> str:
    token = uuid.uuid4().hex[:12]
    _pending_unlock[token] = {
        "expires_at": _now() + float(ttl),
        "symbol": symbol,
        "timeframe": timeframe,
    }
    return token


def consume_unlock_token(token: str) -> dict[str, Any] | None:
    rec = _pending_unlock.pop(token, None)
    if rec is None:
        return None
    if _now() >= float(rec.get("expires_at") or 0):
        return None
    return rec


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


def _open_longs() -> list:
    return long_lots_for_sell(list_active_positions())


def _locked_lots() -> list:
    locked = []
    for p in list_active_positions() or []:
        pos = get_position(position_symbol(p), p.get("timeframe") or "1h")
        lock = get_lock(pos if pos else p)
        if lock and lock_is_active(lock):
            locked.append(p)
    return locked


def _lot_button_label(p: dict) -> str:
    ticker = lot_ticker(position_symbol(p))
    tf = lot_timeframe(p.get("timeframe"))
    return f"{ticker} · {tf}"


def _lot_button_rows(lots: list, prefix: str) -> list:
    rows = []
    for p in lots:
        rows.append([{
            "text": _lot_button_label(p),
            "callback_data": encode_lot_callback(
                prefix, position_symbol(p), p.get("timeframe"),
            ),
        }])
    return rows


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


def _apply_pending_unlock(rec: dict[str, Any], chat_id: str | int | None = None) -> None:
    clear_context(chat_id)
    sym = str(rec.get("symbol") or "")
    tf = str(rec.get("timeframe") or "1h")
    if not sym:
        send_telegram_message(t("unlock_confirm_expired"))
        return
    set_position_lock(sym, tf, None, persist=True)
    send_telegram_message(
        f"🔓 <b>Unlocked</b> <code>{sym}</code> ({tf})\n"
        f"Auto-Sell / Trail / Eviction wieder erlaubt."
    )


def _offer_lock_confirm(sym: str, tf: str, duration_tok: str | None, reason: str) -> bool:
    until = parse_duration_to_until(duration_tok)
    lock = build_lock(
        reason=(reason or "telegram_lock")[:120],
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


def prompt_lock_duration(position_label: str, *, invalid: bool = False) -> None:
    prompt = t("lock_dur_prompt", position=position_label)
    if invalid:
        prompt = t("lock_dur_invalid") + "\n\n" + prompt
    send_telegram_buttons(prompt, _duration_keyboard())


_DURATION_BTN_KEYS = {
    "24h": "lock_dur_btn_24h",
    "7d": "lock_dur_btn_7d",
    "permanent": "lock_dur_btn_permanent",
}


def _duration_keyboard() -> list:
    return [[
        {
            "text": t(_DURATION_BTN_KEYS[tok]),
            "callback_data": f"{LOCK_DUR_CALLBACK_PREFIX}{tok}",
        }
        for tok in LOCK_DURATION_PRESETS
    ]]


def parse_lock_duration_token(token: str) -> str | None:
    raw = (token or "").strip().lower()
    if raw in LOCK_DURATION_PRESETS:
        return raw
    return None


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
    if data.startswith(LOCK_POS_CALLBACK_PREFIX):
        return _handle_lock_pos_callback(callback_query)
    if data.startswith(LOCK_DUR_CALLBACK_PREFIX):
        return _handle_lock_dur_callback(callback_query)
    if data.startswith(UNLOCK_POS_CALLBACK_PREFIX):
        return _handle_unlock_pos_callback(callback_query)
    if data.startswith("unlock_ok:") or data.startswith("unlock_no:"):
        return _handle_unlock_confirm_callback(callback_query)
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


def _callback_chat_id(callback_query: dict, log_label: str):
    from logger import log

    callback_id = (callback_query or {}).get("id")
    if callback_id:
        answer_callback_query(callback_id)
    chat_id = ((callback_query.get("message") or {}).get("chat") or {}).get("id")
    if not chat_id:
        log(f"{log_label} callback missing chat id", "WARNING")
        return None
    set_chat_id(chat_id)
    return chat_id


def _wizard_entry(chat_id, command: str, state: str) -> dict | None:
    entry = get_context(chat_id)
    meta = (entry or {}).get("meta") or {}
    if not entry or entry.get("command") != command or str(meta.get("state") or "") != state:
        return None
    return entry


def _handle_lock_pos_callback(callback_query: dict) -> bool:
    chat_id = _callback_chat_id(callback_query, "lockpos")
    if not chat_id:
        return True
    data = str((callback_query or {}).get("data") or "")
    if _wizard_entry(chat_id, "lock", "lock_awaiting_position") is None:
        send_telegram_message(t("lock_wizard_expired"))
        return True
    parsed = parse_lot_callback(data, LOCK_POS_CALLBACK_PREFIX)
    if parsed is None:
        send_telegram_message(t("lock_wizard_expired"))
        return True
    ticker, tf = parsed
    p = resolve_position_by_symbol_tf(_open_longs(), ticker, tf)
    if not p:
        send_telegram_message(t("lock_wizard_expired"))
        return True
    return _continue_lock_after_position(p)


def _continue_lock_after_position(p: dict) -> bool:
    sym = position_symbol(p)
    tf = p.get("timeframe") or "1h"
    ticker = lot_ticker(sym)
    prompt_lock_duration(ticker)
    activate_command(
        "lock",
        state="lock_awaiting_duration",
        position=ticker,
        symbol=sym,
        timeframe=str(tf),
        label=ticker,
    )
    return True


def _handle_lock_dur_callback(callback_query: dict) -> bool:
    chat_id = _callback_chat_id(callback_query, "lockdur")
    if not chat_id:
        return True
    data = str((callback_query or {}).get("data") or "")
    entry = _wizard_entry(chat_id, "lock", "lock_awaiting_duration")
    if entry is None:
        send_telegram_message(t("lock_wizard_expired"))
        return True
    raw = data.split(":", 1)[1] if ":" in data else ""
    duration = parse_lock_duration_token(raw)
    meta = entry.get("meta") or {}
    label = str(meta.get("label") or meta.get("position") or "")
    if duration is None:
        prompt_lock_duration(label, invalid=True)
        return True
    return _continue_lock_after_duration(chat_id, meta, duration)


def _continue_lock_after_duration(chat_id, meta: dict, duration: str) -> bool:
    sym = str(meta.get("symbol") or "").strip()
    tf = str(meta.get("timeframe") or "1h").strip() or "1h"
    ticker = str(meta.get("position") or lot_ticker(sym)).strip()
    if not sym and ticker:
        p = resolve_position_by_symbol_tf(_open_longs(), ticker, tf)
        if p:
            sym = position_symbol(p)
            tf = p.get("timeframe") or tf
    if not sym:
        send_telegram_message(t("lock_wizard_expired"))
        return True
    clear_context(chat_id)
    return _offer_lock_confirm(sym, tf, duration, "telegram_lock")


def _handle_unlock_pos_callback(callback_query: dict) -> bool:
    chat_id = _callback_chat_id(callback_query, "unlockpos")
    if not chat_id:
        return True
    data = str((callback_query or {}).get("data") or "")
    if _wizard_entry(chat_id, "unlock", "unlock_awaiting_position") is None:
        send_telegram_message(t("unlock_confirm_expired"))
        return True
    parsed = parse_lot_callback(data, UNLOCK_POS_CALLBACK_PREFIX)
    if parsed is None:
        send_telegram_message(t("unlock_confirm_expired"))
        return True
    ticker, tf = parsed
    p = resolve_position_by_symbol_tf(_locked_lots(), ticker, tf)
    if not p:
        send_telegram_message(t("unlock_confirm_expired"))
        return True
    return _offer_unlock_confirm(chat_id, p)


def _offer_unlock_confirm(chat_id, p: dict) -> bool:
    sym = position_symbol(p)
    tf = p.get("timeframe") or "1h"
    token = _create_unlock_token(symbol=sym, timeframe=tf)
    clear_context(chat_id)
    keyboard = [
        [
            {"text": t("unlock_confirm_btn_ok"), "callback_data": f"unlock_ok:{token}"},
            {"text": t("unlock_confirm_btn_no"), "callback_data": f"unlock_no:{token}"},
        ]
    ]
    send_telegram_buttons(
        t("unlock_confirm", symbol=_esc(sym), timeframe=_esc(str(tf))),
        keyboard,
    )
    return True


def _handle_unlock_confirm_callback(callback_query: dict) -> bool:
    data = str((callback_query or {}).get("data") or "")
    parts = data.split(":", 1)
    if len(parts) != 2 or not parts[1]:
        answer_callback_query((callback_query or {}).get("id"))
        send_telegram_message(t("unlock_confirm_expired"))
        return True
    action, token = parts
    if action == "unlock_no":
        answer_callback_query((callback_query or {}).get("id"))
        _pending_unlock.pop(token, None)
        send_telegram_message(t("unlock_confirm_cancelled"))
        return True
    if action != "unlock_ok":
        return False
    chat_id = _callback_chat_id(callback_query, "unlock_ok")
    if not chat_id:
        return True
    rec = consume_unlock_token(token)
    if rec is None:
        send_telegram_message(t("unlock_confirm_expired"))
        return True
    _apply_pending_unlock(rec, chat_id)
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
        longs = _open_longs()
        if not longs:
            send_telegram_message("Keine offenen Positionen zum Locken.")
            return True
        send_telegram_buttons(t("lock_pick"), _lot_button_rows(longs, LOCK_POS_CALLBACK_PREFIX))
        activate_command("lock", state="lock_awaiting_position")
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

    reason = " ".join(reason_parts).strip() or "telegram_lock"
    return _offer_lock_confirm(sym, tf, duration_tok, reason)


def _handle_unlock(text: str) -> bool:
    parts = [p for p in text.split() if p.strip()]
    if len(parts) < 2:
        locked = _locked_lots()
        if not locked:
            send_telegram_message(t("unlock_empty"))
            return True
        send_telegram_buttons(
            t("unlock_pick"),
            _lot_button_rows(locked, UNLOCK_POS_CALLBACK_PREFIX),
        )
        activate_command("unlock", state="unlock_awaiting_position")
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
