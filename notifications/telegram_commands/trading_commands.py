from core.config import get_bot_config
from data_manager import list_coins
from notifications.telegram_commands.usage_hints import hint
from notifications.telegram_commands.utils import safe_float, safe_int
from price_fetcher import get_prices, get_prices_batch
from services.trading_service import TradingService
from notifications.telegram_commands.position_display import (
    chunk_positions_message,
    format_sell_list_message,
    position_symbol,
    resolve_position_by_display_index,
    resolve_position_by_symbol,
)
from notifications.telegram_commands.manual_order_flow import (
    request_buy_confirmation,
    request_sell_confirmation,
)
from notifications.telegram_commands.watchlist_commands import (
    _coin_symbol,
    format_buy_list_message,
    resolve_coin_by_display_index,
)
from strategies.positions import get_position, list_active_positions
from notifications.telegram_commands.command_context import activate_command
from notifications.telegram_i18n import t
from telegram_notifier import send_telegram_message

# Portfolio snapshot after manual buy/sell is sent by TradingService.execute_order.

_trading = TradingService()


def handle(text: str) -> bool:
    if text == "/buy":
        coins = list_coins()
        if not coins:
            send_telegram_message(t("watchlist_empty"))
            return True
        symbols = [_coin_symbol(c) for c in coins]
        prices = get_prices_batch(symbols)
        default_usdt = get_bot_config().max_usdt_per_trade
        activate_command("buy", default_usdt=default_usdt)
        send_telegram_message(format_buy_list_message(coins, prices))
        return True

    if text.startswith("/buy "):
        parts = [p.strip() for p in text.split() if p.strip()]
        coins = list_coins()
        if len(parts) < 2:
            send_telegram_message(hint("buy"))
            return True

        sym = None
        if parts[1].replace(".", "").isdigit():
            idx = safe_int(parts[1]) - 1
            coin = resolve_coin_by_display_index(coins, idx)
            if coin:
                sym = coin["symbol"]
            else:
                send_telegram_message(t("invalid_number_buy"))
                return True
        else:
            sym = (parts[1].upper() + "/USDT") if len(parts) > 1 else None

        usdt = safe_float(parts[2]) if len(parts) > 2 else get_bot_config().max_usdt_per_trade
        if not sym or usdt is None or usdt <= 0:
            send_telegram_message(hint("buy"))
            return True

        # WQE-R4: warn (soft/shadow) or block (enforce) manual buys
        try:
            from services.watchlist_quality.config import wqe_mode
            from services.watchlist_quality.enforce import buy_allowed
            from services.watchlist_quality.store import load_quality_scores

            cfg = get_bot_config().raw
            mode = wqe_mode(cfg)
            if mode in ("shadow", "soft", "enforce"):
                data = load_quality_scores()
                scored = next(
                    (c for c in (data.get("coins") or []) if c.get("symbol") == sym),
                    {"symbol": sym},
                )
                ok, reason = buy_allowed(
                    sym,
                    scored_row=scored,
                    config=cfg,
                    source="manual_telegram",
                    is_new_add=True,
                )
                q = scored.get("quality_shadow_ai")
                if q is None:
                    q = scored.get("quality_score")
                if mode == "enforce" and not ok:
                    send_telegram_message(
                        f"❌ WQE block <code>{sym}</code>: {reason}"
                        + (f" (score={q})" if q is not None else "")
                    )
                    return True
                if mode in ("shadow", "soft") and (
                    not ok or (q is not None and float(q) < 0.4)
                ):
                    send_telegram_message(
                        f"⚠️ WQE hint <code>{sym}</code>: {reason if not ok else 'low_score'}"
                        + (f" score={q}" if q is not None else "")
                        + " — fortfahren möglich"
                    )
        except Exception:
            pass

        price = get_prices(sym)[0]
        if price and price > 0:
            request_buy_confirmation(_trading, symbol=sym, timeframe="4h", price=price, usdt=usdt)
        else:
            send_telegram_message(t("price_fetch_failed_check", sym=sym))
        return True

    if text.startswith("/sell"):
        parts = [p.strip() for p in text.split() if p.strip()]
        if len(parts) == 1:
            active = list_active_positions()
            if not active:
                send_telegram_message(t("no_positions_sell"))
                return True
            symbols = [position_symbol(p) for p in active]
            prices = get_prices_batch(symbols)
            activate_command("sell")
            for chunk in chunk_positions_message(format_sell_list_message(active, prices)):
                send_telegram_message(chunk)
            return True

        active = list_active_positions()
        if not active:
            send_telegram_message(t("no_positions_sell"))
            return True

        arg = parts[1]
        pct = safe_float(parts[2]) / 100 if len(parts) > 2 else 0.5
        if pct is None or pct <= 0 or pct > 1:
            send_telegram_message(t("invalid_sell_pct"))
            return True

        symbols = [position_symbol(p) for p in active]
        prices = get_prices_batch(symbols)

        if arg.replace(".", "").isdigit():
            idx = safe_int(arg) - 1
            if idx is None:
                send_telegram_message(hint("sell"))
                return True
            p = resolve_position_by_display_index(active, prices, idx)
        else:
            p = resolve_position_by_symbol(active, arg, prices)

        if not p:
            send_telegram_message(t("no_open_position", arg=arg.upper()))
            return True

        sym = position_symbol(p)
        tf = p.get("timeframe") or "4h"
        price = prices.get(sym) or get_prices(sym)[0]
        if not price or price <= 0:
            send_telegram_message(t("price_fetch_failed", sym=sym))
            return True

        pos = get_position(sym, tf)
        amount_sold = float(pos.get("amount", 0)) * pct
        if amount_sold <= 0:
            send_telegram_message(t("no_sellable_amount", sym=sym, tf=tf))
            return True

        request_sell_confirmation(
            _trading,
            symbol=sym,
            timeframe=tf,
            price=price,
            amount=amount_sold,
            pct=pct,
        )
        return True

    return False


def handle_callback(callback_query: dict) -> bool:
    from notifications.telegram_commands.manual_order_flow import handle_callback as handle_manual_callback

    return handle_manual_callback(callback_query)
