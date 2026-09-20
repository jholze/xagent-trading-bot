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
from notifications.telegram_commands.command_context import activate_command, clear_context, get_context, set_chat_id
from telegram_notifier import answer_callback_query, send_telegram_buttons, send_telegram_message

# Telegram hard limit 4096; leave headroom for prefix/HTML
_WATCHLIST_CHUNK_LIMIT = 3900
_ADD_CANDIDATE_LIMIT = 8
ADD_PICK_CALLBACK_PREFIX = "addpick:"
ADD_TYPE_CALLBACK = "addtype"
ADD_BACK_CALLBACK = "addback"
REM_PICK_CALLBACK_PREFIX = "rempick:"


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


def _wl_menu_text(command: str, field: str, **kwargs) -> str:
    from notifications.telegram_commands.menu_i18n import _command_entry, _pack, current_language

    template = str(_command_entry(_pack(current_language()), command).get(field) or "")
    for key, value in kwargs.items():
        template = template.replace(f"{{{key}}}", str(value))
    return template


def _ticker_of(coin: dict) -> str:
    return _coin_symbol(coin).split("/")[0].upper()


def add_candidates_not_on_list(watchlist: list | None = None, limit: int = _ADD_CANDIDATE_LIMIT) -> list[dict]:
    """Short tap list: overlay coins that are not already on the watchlist."""
    coins = watchlist if watchlist is not None else list_coins()
    on_tickers = {_ticker_of(c) for c in coins}
    seen: set[str] = set()
    out: list[dict] = []
    for src in (_overlay_candidate_coins(),):
        for c in src:
            if not isinstance(c, dict):
                continue
            ticker = _ticker_of(c)
            if not ticker or ticker in on_tickers or ticker in seen:
                continue
            seen.add(ticker)
            out.append({
                "symbol": f"{ticker}/USDT",
                "name": c.get("name") or ticker,
                "active": True,
            })
            if len(out) >= limit:
                return out
    return out


def _overlay_candidate_coins() -> list:
    coins: list = []
    try:
        from data_manager import load_cmc_trending_overlay, load_dry_run_overlay

        coins.extend(load_cmc_trending_overlay().get("coins") or [])
        coins.extend(load_dry_run_overlay().get("coins") or [])
    except Exception:
        pass
    return coins


def format_remove_pick_button_label(coin: dict) -> str:
    return _ticker_of(coin)


def _add_candidate_button_rows(candidates: list) -> list:
    rows = []
    for coin in candidates:
        ticker = _ticker_of(coin)
        rows.append([{
            "text": ticker,
            "callback_data": f"{ADD_PICK_CALLBACK_PREFIX}{ticker}",
        }])
    rows.append([{
        "text": _wl_menu_text("add", "type_fallback_btn"),
        "callback_data": ADD_TYPE_CALLBACK,
    }])
    return rows


def _remove_button_rows(coins: list) -> list:
    rows = []
    for index, coin in enumerate(coins, start=1):
        rows.append([{
            "text": format_remove_pick_button_label(coin),
            "callback_data": f"{REM_PICK_CALLBACK_PREFIX}{index}",
        }])
    return rows


def _send_chunked_picker(chunks: list[str], buttons: list) -> None:
    if not chunks:
        return
    last = len(chunks) - 1
    for i, chunk in enumerate(chunks):
        if i == last and buttons:
            send_telegram_buttons(chunk, buttons)
        else:
            send_telegram_message(chunk)


def prompt_add_type_ticker(*, invalid: bool = False) -> None:
    prompt = _wl_menu_text("add", "type_prompt")
    if invalid:
        prompt = hint("add") + "\n\n" + prompt
    buttons = [[{
        "text": _wl_menu_text("add", "back_btn"),
        "callback_data": ADD_BACK_CALLBACK,
    }]]
    send_telegram_buttons(prompt, buttons)


def _show_add_picker() -> None:
    try:
        coins = list_coins()
    except Exception:
        coins = []
    candidates = add_candidates_not_on_list(coins)
    prompt = _wl_menu_text("add", "pick_prompt")
    send_telegram_buttons(prompt, _add_candidate_button_rows(candidates))
    activate_command("add", state="add_awaiting_pick")


def _show_remove_picker() -> bool:
    coins = list_coins()
    if not coins:
        from notifications.telegram_i18n import t

        send_telegram_message(t("watchlist_empty"))
        return True
    header = _wl_menu_text("remove", "pick_prompt")
    lines = [header, ""]
    for i, coin in enumerate(coins, 1):
        lines.append(_format_coin_line(i, coin))
    text = "\n".join(lines)
    chunks = [text] if len(text) <= _WATCHLIST_CHUNK_LIMIT else chunk_watchlist_messages(coins)
    if chunks and chunks[0] and not chunks[0].startswith(header):
        chunks[0] = header + "\n\n" + chunks[0]
    _send_chunked_picker(chunks, _remove_button_rows(coins))
    activate_command("remove", state="remove_awaiting_pick")
    return True


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
        _show_add_picker()
        return True

    if text == "/remove":
        return _show_remove_picker()

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


def _wl_callback_chat_id(callback_query: dict, log_label: str):
    from logger import log

    answer_callback_query(callback_query.get("id"))
    message = (callback_query or {}).get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    if not chat_id:
        log(f"{log_label} callback missing chat id", "WARNING")
        return None
    set_chat_id(chat_id)
    return chat_id


def _handle_add_pick_callback(callback_query: dict) -> bool:
    chat_id = _wl_callback_chat_id(callback_query, "addpick")
    if not chat_id:
        return True
    entry = get_context(chat_id)
    meta = (entry or {}).get("meta") or {}
    if (
        not entry
        or entry.get("command") != "add"
        or str(meta.get("state") or "") not in ("add_awaiting_pick", "add_awaiting_ticker")
    ):
        send_telegram_message(_wl_menu_text("add", "expired"))
        return True
    data = str(callback_query.get("data") or "")
    ticker = data.split(":", 1)[1] if ":" in data else ""
    ticker = "".join(ch for ch in ticker.upper() if ch.isalnum())
    if not ticker:
        send_telegram_message(_wl_menu_text("add", "expired"))
        return True
    from notifications.telegram_commands.router import dispatch_command

    clear_context(chat_id)
    return dispatch_command(f"/add {ticker}")


def _handle_add_type_callback(callback_query: dict) -> bool:
    chat_id = _wl_callback_chat_id(callback_query, "addtype")
    if not chat_id:
        return True
    entry = get_context(chat_id)
    meta = (entry or {}).get("meta") or {}
    if not entry or entry.get("command") != "add" or str(meta.get("state") or "") != "add_awaiting_pick":
        send_telegram_message(_wl_menu_text("add", "expired"))
        return True
    prompt_add_type_ticker()
    activate_command("add", state="add_awaiting_ticker")
    return True


def _handle_add_back_callback(callback_query: dict) -> bool:
    chat_id = _wl_callback_chat_id(callback_query, "addback")
    if not chat_id:
        return True
    entry = get_context(chat_id)
    if not entry or entry.get("command") != "add":
        send_telegram_message(_wl_menu_text("add", "expired"))
        return True
    _show_add_picker()
    return True


def _handle_remove_pick_callback(callback_query: dict) -> bool:
    chat_id = _wl_callback_chat_id(callback_query, "rempick")
    if not chat_id:
        return True
    entry = get_context(chat_id)
    meta = (entry or {}).get("meta") or {}
    if (
        not entry
        or entry.get("command") != "remove"
        or str(meta.get("state") or "") != "remove_awaiting_pick"
    ):
        send_telegram_message(_wl_menu_text("remove", "expired"))
        return True
    data = str(callback_query.get("data") or "")
    raw = data.split(":", 1)[1] if ":" in data else ""
    idx = safe_int(raw)
    if idx is None or idx < 1:
        send_telegram_message(_wl_menu_text("remove", "expired"))
        return True
    from notifications.telegram_commands.router import dispatch_command

    clear_context(chat_id)
    return dispatch_command(f"/remove {idx}")


def handle_callback(callback_query: dict) -> bool:
    data = str((callback_query or {}).get("data") or "")
    if data.startswith(ADD_PICK_CALLBACK_PREFIX):
        return _handle_add_pick_callback(callback_query)
    if data == ADD_TYPE_CALLBACK:
        return _handle_add_type_callback(callback_query)
    if data == ADD_BACK_CALLBACK:
        return _handle_add_back_callback(callback_query)
    if data.startswith(REM_PICK_CALLBACK_PREFIX):
        return _handle_remove_pick_callback(callback_query)
    return False