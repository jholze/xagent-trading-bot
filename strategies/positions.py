import json
import os
import threading
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from logger import log

# Basic lock to reduce risk of concurrent modifications (price loop + Flask).
_positions_lock = threading.Lock()

try:
    from data_manager import atomic_write_json, resolve_ledger_scope, resolve_positions_file
except Exception:
    def resolve_ledger_scope(trading_mode=None):
        return "paper"

    def resolve_positions_file(scope):
        return "positions.json"

    def atomic_write_json(path, data):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

# Module-level state (global mutable dictionary).
# This is a known technical debt item. All access should go through the functions below.
positions = {}
_active_scope = "paper"

DUST_AMOUNT_EPSILON = 1e-12
MIN_OPEN_POSITION_USDT = 1.0


def position_notional_usdt(pos: dict) -> float:
    amount = float(pos.get("amount", 0) or 0)
    if amount <= 0:
        return 0.0
    for key in ("average_entry", "entry_price", "last_buy_price"):
        entry = float(pos.get(key, 0) or 0)
        if entry > 0:
            return amount * entry
    return 0.0


def has_position_amount(pos: dict) -> bool:
    return float(pos.get("amount", 0) or 0) > DUST_AMOUNT_EPSILON


def is_open_position(pos: dict) -> bool:
    """True when the lot is material (BTC-sized fractions, not token-dust)."""
    if not has_position_amount(pos):
        return False
    notional = position_notional_usdt(pos)
    if notional > 0:
        return notional >= MIN_OPEN_POSITION_USDT
    return True


def get_active_scope() -> str:
    return _active_scope


def _deserialize_position(raw: dict) -> dict:
    amount = Decimal(str(raw.get("amount", 0)))
    peak = float(raw.get("peak_amount", 0) or 0)
    if peak <= 0 and float(amount) > 0:
        sold = float(raw.get("sold_percent", 0) or 0)
        if 0 < sold < 1:
            peak = float(amount) / (1.0 - sold)
        else:
            peak = float(amount)
    return {
        "amount": amount,
        "peak_amount": peak,
        "sold_percent": float(raw.get("sold_percent", 0)),
        "average_entry": float(raw.get("average_entry", raw.get("entry_price", 0))),
        "realized_pnl": float(raw.get("realized_pnl", 0)),
        "last_buy_price": float(raw.get("last_buy_price", 0)),
        "last_ampel": raw.get("last_ampel", "🟡"),
        "last_rsi": float(raw.get("last_rsi", 45.0)),
        "last_action": raw.get("last_action"),
        "last_trade_at": raw.get("last_trade_at"),
        "last_trade_type": raw.get("last_trade_type"),
        "rsi_sell_tiers_done": dict(raw.get("rsi_sell_tiers_done") or {}),
        "last_cmc_sell_at": raw.get("last_cmc_sell_at"),
        "recent_high": float(raw.get("recent_high", 0)),
        "strategy_tier": raw.get("strategy_tier"),
        "exit_ladder_step": int(raw.get("exit_ladder_step", 0) or 0),
    }


def _serialize_positions() -> dict:
    data = {"positions": {}, "ledger_scope": _active_scope}
    for tf, p in positions.items():
        data["positions"][tf] = {
            "amount": float(p["amount"]),
            "peak_amount": float(p.get("peak_amount", 0) or 0),
            "sold_percent": p["sold_percent"],
            "average_entry": float(p.get("average_entry", p.get("entry_price", 0))),
            "realized_pnl": float(p.get("realized_pnl", 0)),
            "last_buy_price": p["last_buy_price"],
            "last_ampel": p.get("last_ampel", "🟡"),
            "last_rsi": p.get("last_rsi", 45.0),
            "last_action": p.get("last_action"),
            "last_trade_at": p.get("last_trade_at"),
            "last_trade_type": p.get("last_trade_type"),
            "rsi_sell_tiers_done": dict(p.get("rsi_sell_tiers_done") or {}),
            "last_cmc_sell_at": p.get("last_cmc_sell_at"),
            "recent_high": float(p.get("recent_high", 0)),
            "strategy_tier": p.get("strategy_tier"),
            "exit_ladder_step": int(p.get("exit_ladder_step", 0) or 0),
        }
    return data


def apply_positions_snapshot(snapshot: dict, scope: str = None) -> None:
    global _active_scope
    target = scope or _active_scope
    with _positions_lock:
        positions.clear()
        for key, raw in snapshot.items():
            positions[key] = _deserialize_position(raw)
        _active_scope = target


def load_positions(scope: str = None):
    global _active_scope
    target = scope or resolve_ledger_scope()
    path = resolve_positions_file(target)
    with _positions_lock:
        positions.clear()
        _active_scope = target
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for tf, p in data.get("positions", {}).items():
                positions[tf] = _deserialize_position(p)
        except Exception as e:
            log(f"Failed to load {path}: {e}", "ERROR")


def save_positions(scope: str = None):
    target = scope or _active_scope
    path = resolve_positions_file(target)
    with _positions_lock:
        payload = _serialize_positions()
        payload["ledger_scope"] = target
        try:
            atomic_write_json(path, payload)
        except Exception as e:
            log(f"Failed to save {path}: {e}", "ERROR")


def _bootstrap_positions():
    try:
        load_positions(scope=resolve_ledger_scope())
    except Exception as e:
        log(f"Position bootstrap failed: {e}", "WARNING")


_bootstrap_positions()

def update_market_snapshot(symbol: str, timeframe: str, current_price: float, atr_pct: float = 0.0):
    init_position(symbol, timeframe)
    key = get_key(symbol, timeframe)
    with _positions_lock:
        pos = positions[key]
        pos["recent_high"] = max(float(pos.get("recent_high") or 0), current_price)
    save_positions()


def lock_strategy_tier(symbol: str, timeframe: str, tier: str) -> None:
    if tier not in ("stable", "volatile"):
        return
    init_position(symbol, timeframe)
    key = get_key(symbol, timeframe)
    with _positions_lock:
        if not positions[key].get("strategy_tier"):
            positions[key]["strategy_tier"] = tier
    save_positions()


def get_key(symbol, timeframe):
    return f"{symbol.replace('/', '_')}_{timeframe}"

def init_position(symbol, timeframe):
    key = get_key(symbol, timeframe)
    with _positions_lock:
        if key not in positions:
            positions[key] = {
                "amount": Decimal("0"),
                "peak_amount": 0.0,
                "sold_percent": 0.0,
                "average_entry": 0.0,
                "realized_pnl": 0.0,
                "last_buy_price": 0.0,
                "last_ampel": "🟡",
                "last_rsi": 45.0,
                "last_action": None,
                "last_trade_at": None,
                "last_trade_type": None,
                "rsi_sell_tiers_done": {},
                "last_cmc_sell_at": None,
                "recent_high": 0.0,
                "strategy_tier": None,
                "exit_ladder_step": 0,
            }

def get_position(symbol, timeframe):
    init_position(symbol, timeframe)
    with _positions_lock:
        return positions[get_key(symbol, timeframe)]


def reset_rsi_sell_tiers_if_cooled(
    symbol: str,
    timeframe: str,
    current_rsi: float,
    rsi_sell_30: float,
    rsi_sell_20: float,
    buffer: float = 5.0,
):
    """Clear sell-tier flags after RSI drops below threshold minus buffer."""
    init_position(symbol, timeframe)
    key = get_key(symbol, timeframe)
    changed = False
    with _positions_lock:
        pos = positions[key]
        tiers = dict(pos.get("rsi_sell_tiers_done") or {})
        if tiers.get("30") and current_rsi < rsi_sell_30 - buffer:
            tiers["30"] = False
            changed = True
        if tiers.get("20") and current_rsi < rsi_sell_20 - buffer:
            tiers["20"] = False
            changed = True
        if tiers.get("tp") and current_rsi < rsi_sell_30 - buffer:
            tiers["tp"] = False
            changed = True
        if changed:
            pos["rsi_sell_tiers_done"] = tiers
    if changed:
        save_positions()


def is_rsi_sell_tier_done(symbol: str, timeframe: str, tier: str) -> bool:
    pos = get_position(symbol, timeframe)
    return bool((pos.get("rsi_sell_tiers_done") or {}).get(tier))


def sell_fraction_for_signal(
    signal: str,
    symbol: str | None = None,
    timeframe: str | None = None,
    price: float = 0.0,
    strategy_params: dict | None = None,
) -> float:
    """Map sell signal names to fraction of position to close."""
    if symbol and timeframe and strategy_params:
        from strategies.exit_ladder import resolve_sell_fraction

        ladder_frac = resolve_sell_fraction(signal, symbol, timeframe, price, strategy_params)
        if ladder_frac is not None:
            return ladder_frac

    if signal in ("SELL_STOP_FULL", "SELL_FULL"):
        return 1.0
    if signal == "SELL_STOP_PARTIAL":
        return 0.5
    if signal in ("SELL_30", "SELL_TP", "SELL_PARTIAL_30"):
        return 0.3
    if signal in ("SELL_10", "SELL_PARTIAL_10"):
        return 0.1
    if signal == "SELL_20":
        return 0.2
    if "FULL" in signal:
        return 1.0
    if "PARTIAL" in signal:
        return 0.5
    if "30" in signal:
        return 0.3
    return 0.2


def update_position(symbol, timeframe, signal, current_price, amount_traded=0):
    init_position(symbol, timeframe)
    key = get_key(symbol, timeframe)
    with _positions_lock:
        pos = positions[key]
        if signal == "BUY" and amount_traded > 0:
            old_amount = pos["amount"]
            old_average = pos.get("average_entry", current_price)
            new_amount = old_amount + Decimal(str(amount_traded))
            if old_amount > 0:
                pos["average_entry"] = float((old_average * float(old_amount) + current_price * float(amount_traded)) / float(new_amount))
            else:
                pos["average_entry"] = current_price
            pos["amount"] = new_amount
            pos["peak_amount"] = float(new_amount)
            pos["sold_percent"] = 0.0
            pos["last_buy_price"] = current_price
            pos["last_action"] = "BUY"
            pos["rsi_sell_tiers_done"] = {}
            pos["recent_high"] = current_price
            pos["exit_ladder_step"] = 0
            pos["last_trade_at"] = datetime.now().isoformat()
            pos["last_trade_type"] = "BUY"
            if old_amount <= 0:
                pos["strategy_tier"] = None
        elif "SELL" in signal:
            original_amount = float(pos["amount"])
            strategy_params = None
            try:
                from strategies.registry import resolve_strategy_params

                strategy_params = resolve_strategy_params(
                    {"symbol": symbol, "timeframe": timeframe},
                    has_position=True,
                    frozen_tier=pos.get("strategy_tier"),
                )
            except Exception:
                strategy_params = None
            if amount_traded > 0:
                sell_amount = min(Decimal(str(amount_traded)), pos["amount"])
            else:
                fraction = sell_fraction_for_signal(
                    signal, symbol, timeframe, current_price, strategy_params,
                )
                sell_amount = pos["amount"] * Decimal(str(fraction))
            peak = float(pos.get("peak_amount") or 0)
            if peak <= 0 and original_amount > 0:
                peak = original_amount
                pos["peak_amount"] = peak
            pos["amount"] -= sell_amount
            if peak > 0:
                pos["sold_percent"] = min(
                    1.0, max(0.0, 1.0 - float(pos["amount"]) / peak)
                )
            pos["last_action"] = "SELL"
            pos["last_trade_at"] = datetime.now().isoformat()
            pos["last_trade_type"] = "SELL"
            tiers = dict(pos.get("rsi_sell_tiers_done") or {})
            if "TP" in signal.upper():
                tiers["tp"] = True
            elif "30" in signal:
                tiers["30"] = True
            elif "20" in signal:
                tiers["20"] = True
            pos["rsi_sell_tiers_done"] = tiers
            if strategy_params:
                from strategies.exit_ladder import advance_ladder_step

                advance_ladder_step(
                    pos,
                    signal,
                    strategy_params,
                    amount_sold=float(sell_amount),
                    amount_before=original_amount,
                )
        if pos["amount"] < 0:
            pos["amount"] = Decimal("0")
    save_positions()
    _sync_open_positions_count()


def _sync_open_positions_count():
    try:
        from data_manager import load_live_trade_history, load_trade_history, save_trade_history, uses_exchange_ledger

        open_count = count_open_positions()
        if uses_exchange_ledger():
            history = load_live_trade_history()
            history["open_positions"] = open_count
            from data_manager import save_live_trade_history
            save_live_trade_history(history)
        else:
            history = load_trade_history()
            history["open_positions"] = open_count
            save_trade_history(history)
    except Exception as e:
        log(f"Failed to sync open_positions count: {e}", "WARNING")


def count_open_positions():
    with _positions_lock:
        return sum(1 for p in positions.values() if is_open_position(p))

def get_total_aria():
    with _positions_lock:
        total = Decimal("0")
        for pos in positions.values():
            total += pos["amount"]
        return total

def list_active_positions():
    with _positions_lock:
        active = []
        for key, p in positions.items():
            if is_open_position(p):
                base, _, tf = key.rpartition("_")
                symbol = base.replace("_", "/") if "/" not in base else base
                if not symbol.upper().startswith("TEST"):
                    highlight = "🔥 " if p.get("last_action") == "BUY" else ""
                    active.append({
                        "symbol": symbol,
                        "timeframe": tf,
                        "amount": float(p["amount"]),
                        "average_entry": p.get("average_entry", 0),
                        "entry_price": p.get("average_entry", 0),
                        "last_buy_price": p.get("last_buy_price", 0),
                        "realized_pnl": p.get("realized_pnl", 0),
                        "peak_amount": float(p.get("peak_amount", 0) or 0),
                        "sold_percent": float(p.get("sold_percent", 0)),
                        "last_action": p.get("last_action"),
                        "highlight": highlight,
                    })
        return active
