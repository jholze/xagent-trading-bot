"""Telegram /lock and /unlock — per-position auto-sell / DCA / eviction hold."""

from __future__ import annotations

from notifications.telegram_commands.command_context import activate_command
from notifications.telegram_commands.position_display import (
    position_symbol,
    resolve_position_by_symbol,
)
from notifications.telegram_commands.usage_hints import hint
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
from telegram_notifier import send_telegram_message


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


def handle(text: str) -> bool:
    lower = (text or "").strip()
    if not lower:
        return False

    if lower == "/lock" or lower.startswith("/lock "):
        return _handle_lock(lower)
    if lower == "/unlock" or lower.startswith("/unlock "):
        return _handle_unlock(lower)
    return False


def _handle_lock(text: str) -> bool:
    activate_command("lock")
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
    set_position_lock(sym, tf, lock, persist=True)
    until_s = lock.get("until") or "permanent"
    send_telegram_message(
        f"🔒 <b>Locked</b> <code>{sym}</code> ({tf})\n"
        f"modes: <code>{','.join(lock.get('modes') or [])}</code>\n"
        f"until: <code>{until_s}</code>\n"
        f"reason: <i>{lock.get('reason')}</i>\n\n"
        f"Auto-Sell / Trail / Eviction blockiert.\n"
        f"DCA + Sniper bleiben erlaubt (Lock = nur Verkaufs-Hold).\n"
        f"Manueller <code>/sell</code> bleibt möglich.\n"
        f"Unlock: <code>/unlock {sym.split('/')[0]}</code>"
    )
    return True


def _handle_unlock(text: str) -> bool:
    activate_command("unlock")
    parts = [p for p in text.split() if p.strip()]
    if len(parts) < 2:
        send_telegram_message(hint("unlock"))
        return True

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
