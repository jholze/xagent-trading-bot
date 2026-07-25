from core.config import get_bot_config
from data_manager import (
    add_coin,
    is_dry_run_enhanced,
    is_demo_mode,
    list_coins,
    remove_coin,
    uses_watchlist_expansion,
)
from notifications.telegram_commands.usage_hints import hint
from notifications.telegram_commands.utils import safe_int
from notifications.telegram_commands.command_context import activate_command
from telegram_notifier import send_telegram_message

# Telegram hard limit 4096; leave headroom for prefix/HTML
_WATCHLIST_CHUNK_LIMIT = 3900


def _coin_symbol(coin: dict) -> str:
    sym = coin.get("symbol", "")
    return sym if "/" in sym else f"{sym}/USDT"


def _watchlist_mode_label() -> str:
    if is_demo_mode():
        return "Demo"
    if is_dry_run_enhanced():
        return "Enhanced Dry Run"
    if uses_watchlist_expansion():
        return "Dry Run"
    return ""


def format_watchlist_message(coins: list = None) -> str:
    """Single-message format (may exceed Telegram limit — prefer chunk_watchlist_messages)."""
    return "\n\n".join(chunk_watchlist_messages(coins))


def chunk_watchlist_messages(coins: list = None, *, limit: int = _WATCHLIST_CHUNK_LIMIT) -> list[str]:
    """Split watchlist into Telegram-safe HTML chunks (one line per coin)."""
    coins = coins if coins is not None else list_coins()
    if not coins:
        return ["📋 Watchlist ist leer."]
    mode = _watchlist_mode_label()
    title = f"📋 <b>Watchlist</b> ({mode})" if mode else "📋 <b>Aktive Watchlist</b>"
    header = f"{title}\n\n"
    cont = "📋 <b>Watchlist</b> <i>(Fortsetzung)</i>\n\n"
    chunks: list[str] = []
    current = header
    for i, coin in enumerate(coins, 1):
        line = _format_coin_line(i, coin) + "\n"
        # hard-split oversized single line
        if len(line) > limit:
            if current.strip() and current != header and current != cont:
                chunks.append(current.rstrip())
            for start in range(0, len(line), limit):
                chunks.append(line[start : start + limit].rstrip())
            current = cont
            continue
        candidate = current + line
        if len(candidate) > limit and current not in (header, cont):
            chunks.append(current.rstrip())
            current = cont + line
        elif len(candidate) > limit:
            # header alone + line still too long — emit line as own chunk
            chunks.append(current.rstrip())
            current = cont + line
            if len(current) > limit:
                chunks.append(current[:limit].rstrip())
                current = cont
        else:
            current = candidate
    if current.strip() and current not in (header, cont):
        chunks.append(current.rstrip())
    if not chunks:
        return [header.rstrip() + "\n<i>Keine Einträge.</i>"]
    if len(chunks) > 1:
        tagged = []
        total = len(chunks)
        for i, ch in enumerate(chunks):
            tagged.append(f"{ch}\n\n<i>({i + 1}/{total})</i>")
        return tagged
    return chunks


def send_watchlist_messages(coins: list = None) -> None:
    for part in chunk_watchlist_messages(coins):
        send_telegram_message(part)


def format_buy_list_message(coins: list, prices: dict) -> str:
    from price_fetcher import format_usdt_price

    from notifications.telegram_i18n import t

    if not coins:
        return t("watchlist_empty_add")
    default_usdt = get_bot_config().max_usdt_per_trade
    msg = "<b>🛒 Coins kaufen</b>\n\n"
    for i, coin in enumerate(coins, 1):
        sym = _coin_symbol(coin)
        price = float(prices.get(sym, 0) or 0)
        price_str = format_usdt_price(price)
        msg += f"{_format_coin_line(i, coin)}\n   └ Kurs <b>{price_str}</b>\n"
    from notifications.telegram_commands.menu_i18n import context_footer, current_language

    msg += "\n" + context_footer(
        "buy",
        current_language(),
        default_usdt=f"{default_usdt:.0f}",
        example=f"1 {default_usdt:.0f}",
    )
    return msg


def _format_coin_line(index: int, coin: dict, trending: bool = False) -> str:
    from notifications.coin_links import format_ticker_html

    name = coin.get("name", "")
    ticker = coin.get("symbol", "").split("/")[0]
    sym_html = format_ticker_html(ticker, name=name, symbol_suffix="/USDT")
    suffix = f" ({name})" if name else ""
    inactive = "" if coin.get("active", True) else " <i>(inaktiv)</i>"
    if trending or coin.get("source") in ("cmc_trending", "dry_run_expansion"):
        rank = coin.get("trending_rank")
        tag = f" 📈Trending #{rank}" if rank else " 📈Trending"
    else:
        tag = ""
    return f"<b>{index}.</b> <b>{sym_html}</b>{suffix}{inactive}{tag}"


def resolve_coin_by_display_index(coins: list, index: int):
    """Map 0-based display index (from /list or /buy list) to a watchlist coin."""
    if 0 <= index < len(coins):
        return coins[index]
    return None


def _format_wqe_status() -> str:
    """W6: operator visibility for scores / soak / mode."""
    try:
        from services.watchlist_quality.config import wqe_mode
        from services.watchlist_quality.soak import format_soak_report
        from services.watchlist_quality.store import load_quality_scores
        from core.config import get_bot_config

        cfg = get_bot_config().raw
        mode = wqe_mode(cfg)
        data = load_quality_scores()
        coins = data.get("coins") or []
        age = None
        try:
            from services.watchlist_quality.store import score_age_seconds
            from services.watchlist_quality.metrics import snapshot

            age = score_age_seconds()
            snap = snapshot()
        except Exception:
            snap = {}
        age_s = f"{age:.0f}s" if age is not None else "—"
        try:
            from services.watchlist_quality.event_log import log_path

            elog = log_path()
        except Exception:
            elog = "logs/wqe_events.jsonl"
        try:
            from services.watchlist_quality.soak_log import (
                cycle_summary_path,
                last_cycle_summary_age_sec,
            )
            from services.watchlist_quality.store import scores_path

            clog = cycle_summary_path()
            slog = scores_path()
            c_age = last_cycle_summary_age_sec()
            c_age_s = f"{c_age:.0f}s" if c_age is not None else "—"
        except Exception:
            clog = "logs/cycle_summary.jsonl"
            slog = "logs/watchlist_quality_scores.json"
            c_age_s = "—"
        lines = [
            f"<b>WQE</b> mode=<code>{mode}</code>",
            f"scores_as_of={data.get('updated_at') or '—'} age={age_s}",
            f"n_scored={len(coins)}",
            format_soak_report(),
            f"metrics blocked={snap.get('wqe_buy_blocked_total', 0)} "
            f"ai_ok={snap.get('wqe_ai_ok', 0)} ai_err={snap.get('wqe_ai_error', 0)}",
            f"event_log=<code>{elog}</code>",
            f"scores=<code>{slog}</code>",
            f"cycle_summary=<code>{clog}</code> last_age={c_age_s}",
            "",
            "<b>Top scores</b>",
        ]
        ranked = sorted(
            [c for c in coins if isinstance(c, dict)],
            key=lambda c: float(
                c.get("quality_shadow_ai")
                if c.get("quality_shadow_ai") is not None
                else c.get("quality_score")
                or 0
            ),
            reverse=True,
        )[:12]
        for i, c in enumerate(ranked, 1):
            q = c.get("quality_shadow_ai")
            if q is None:
                q = c.get("quality_score")
            tier = c.get("tier_hint") or c.get("tier") or "?"
            lines.append(
                f"{i}. <code>{c.get('symbol')}</code> "
                f"q={q} tier={tier} "
                f"ai={(c.get('ai') or {}).get('stance', '—')}"
            )
        if not ranked:
            lines.append("<i>No scores yet — set watchlist_quality.mode=shadow and sync.</i>")
        return "\n".join(lines)
    except Exception as e:
        return f"WQE status error: {e}"


def handle(text: str) -> bool:
    if text in ("/wqe", "/watchlist_quality", "/wqescores"):
        send_telegram_message(_format_wqe_status())
        return True

    if text in ("/wqe soak", "/wqe_soak"):
        try:
            from services.watchlist_quality.soak import format_soak_report

            send_telegram_message(format_soak_report())
        except Exception as e:
            send_telegram_message(f"WQE soak error: {e}")
        return True

    if text == "/add":
        activate_command("add")
        send_telegram_message(hint("add"))
        return True

    if text == "/remove":
        activate_command("remove")
        send_telegram_message(hint("remove"))
        return True

    if text.startswith("/add "):
        query = text[5:].strip().upper()
        if not query:
            send_telegram_message(hint("add"))
            return True
        success, msg = add_coin(query)
        send_telegram_message(f"{'✅' if success else '❌'} {msg}")
        return True

    if text.startswith("/remove "):
        index = safe_int(text[8:].strip())
        if index is None:
            send_telegram_message(hint("remove"))
            return True
        coins = list_coins()
        if index < 1 or index > len(coins):
            from notifications.telegram_i18n import t

            send_telegram_message(t("invalid_number"))
            return True
        symbol = coins[index - 1]["symbol"]
        success, msg = remove_coin(symbol)
        send_telegram_message(f"{'✅' if success else '❌'} {msg}")
        return True

    if text in ["/list", "/watchlist", "/show"]:
        send_watchlist_messages(list_coins())
        return True

    return False