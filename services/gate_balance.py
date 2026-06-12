"""Gate.io balance and holdings helpers for live mode."""

from __future__ import annotations

from core.config import BotConfig, get_bot_config
from data_manager import (
    is_dry_run_enhanced,
    load_live_trade_history,
    load_trade_history,
    simulated_balance_usdt,
    uses_exchange_ledger,
)
from logger import log
from price_fetcher import get_prices_batch


def get_gate_adapter(config: BotConfig = None):
    from execution.gate_adapter import GateExecutionAdapter

    cfg = config or get_bot_config()
    return GateExecutionAdapter(cfg)


def fetch_usdt_balance(config: BotConfig = None) -> float:
    cfg = config or get_bot_config()
    if is_dry_run_enhanced(cfg.raw):
        history = load_live_trade_history()
        return float(history.get("virtual_balance", cfg.simulated_balance_usdt))
    if not uses_exchange_ledger(cfg.trading_mode):
        return float(load_trade_history().get("virtual_balance", 0))
    return get_gate_adapter(cfg)._fetch_usdt_balance()


def fetch_spot_holdings(config: BotConfig = None, min_amount: float = 0.0) -> list[dict]:
    cfg = config or get_bot_config()
    if not uses_exchange_ledger(cfg.trading_mode):
        return []
    adapter = get_gate_adapter(cfg)
    exchange = adapter._get_exchange()
    if not exchange:
        return []
    try:
        balance = exchange.fetch_balance()
        free = balance.get("free", {})
        holdings = []
        for currency, amount in free.items():
            if currency in ("USDT", "info") or not amount:
                continue
            amt = float(amount or 0)
            if amt <= min_amount:
                continue
            holdings.append({
                "currency": currency,
                "symbol": f"{currency}/USDT",
                "amount": amt,
            })
        holdings.sort(key=lambda h: h["amount"], reverse=True)
        return holdings
    except Exception as e:
        log(f"Gate holdings fetch failed: {e}", "WARNING")
        return []


def fetch_portfolio_equity(config: BotConfig = None, reference_prices: dict = None) -> float:
    cfg = config or get_bot_config()
    if is_dry_run_enhanced(cfg.raw):
        cash = fetch_usdt_balance(cfg)
        try:
            from strategies.positions import list_active_positions
        except Exception:
            return cash
        active = list_active_positions()
        if not active:
            return cash
        symbols = [
            p["symbol"] if "/" in p["symbol"] else f"{p['symbol']}/USDT"
            for p in active
        ]
        prices = reference_prices or get_prices_batch(symbols)
        total = cash
        for p in active:
            sym = p["symbol"] if "/" in p["symbol"] else f"{p['symbol']}/USDT"
            amount = float(p.get("amount", 0) or 0)
            price = float(prices.get(sym, 0) or 0)
            if price > 0 and amount > 0:
                total += amount * price
        return total
    if not uses_exchange_ledger(cfg.trading_mode):
        history = load_trade_history()
        return float(history.get("virtual_balance", 0))

    usdt = fetch_usdt_balance(cfg)
    holdings = fetch_spot_holdings(cfg)
    if not holdings:
        return usdt

    symbols = [h["symbol"] for h in holdings]
    prices = reference_prices or get_prices_batch(symbols)
    total = usdt
    for h in holdings:
        price = float(prices.get(h["symbol"], 0) or 0)
        if price > 0:
            total += h["amount"] * price
    return total


def format_holdings_lines(holdings: list[dict], prices: dict = None, limit: int = 8) -> list[str]:
    if not holdings:
        return ["  <i>Keine Spot-Bestände (außer USDT).</i>"]
    prices = prices or get_prices_batch([h["symbol"] for h in holdings])
    lines = []
    for h in holdings[:limit]:
        price = float(prices.get(h["symbol"], 0) or 0)
        value = h["amount"] * price if price > 0 else 0
        value_part = f" · <b>${value:.0f}</b>" if value > 0 else ""
        lines.append(f"  · <b>{h['currency']}</b> <code>{h['amount']:.4f}</code>{value_part}")
    if len(holdings) > limit:
        lines.append(f"  <i>… +{len(holdings) - limit} weitere</i>")
    return lines