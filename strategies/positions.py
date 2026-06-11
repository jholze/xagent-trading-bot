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
    from data_manager import atomic_write_json, get_data_file, is_demo_mode
except Exception:
    def get_data_file(name): return name
    def is_demo_mode(): return False

    def atomic_write_json(path, data):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

POSITIONS_FILE = "positions.json"

# Module-level state (global mutable dictionary).
# This is a known technical debt item. All access should go through the functions below.
positions = {}


def load_positions():
    path = get_data_file(POSITIONS_FILE)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                for tf, p in data.get("positions", {}).items():
                    positions[tf] = {
                        "amount": Decimal(str(p.get("amount", 0))),
                        "sold_percent": float(p.get("sold_percent", 0)),
                        "average_entry": float(p.get("average_entry", p.get("entry_price", 0))),
                        "realized_pnl": float(p.get("realized_pnl", 0)),
                        "last_buy_price": float(p.get("last_buy_price", 0)),
                        "last_ampel": p.get("last_ampel", "🟡"),
                        "last_rsi": float(p.get("last_rsi", 45.0)),
                        "last_action": p.get("last_action"),
                        "last_trade_at": p.get("last_trade_at"),
                        "last_trade_type": p.get("last_trade_type"),
                        "rsi_sell_tiers_done": dict(p.get("rsi_sell_tiers_done") or {}),
                    }
        except Exception as e:
            log(f"Failed to load {path}: {e}", "ERROR")


def save_positions():
    path = get_data_file(POSITIONS_FILE)
    with _positions_lock:
        try:
            data = {"positions": {}}
            for tf, p in positions.items():
                data["positions"][tf] = {
                    "amount": float(p["amount"]),
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
                }
            atomic_write_json(path, data)
        except Exception as e:
            log(f"Failed to save {path}: {e}", "ERROR")


# Initialize state on import (after positions dict exists)
load_positions()

def get_key(symbol, timeframe):
    return f"{symbol.replace('/', '_')}_{timeframe}"

def init_position(symbol, timeframe):
    key = get_key(symbol, timeframe)
    with _positions_lock:
        if key not in positions:
            positions[key] = {
                "amount": Decimal("0"),
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


def sell_fraction_for_signal(signal: str) -> float:
    """Map sell signal names to fraction of position to close."""
    if signal in ("SELL_STOP_FULL", "SELL_FULL"):
        return 1.0
    if signal == "SELL_STOP_PARTIAL":
        return 0.5
    if signal in ("SELL_30", "SELL_TP", "SELL_PARTIAL_30"):
        return 0.3
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
            pos["sold_percent"] = 0.0
            pos["last_buy_price"] = current_price
            pos["last_action"] = "BUY"
            pos["rsi_sell_tiers_done"] = {}
            pos["last_trade_at"] = datetime.now().isoformat()
            pos["last_trade_type"] = "BUY"
        elif "SELL" in signal:
            original_amount = float(pos["amount"])
            if amount_traded > 0:
                sell_amount = min(Decimal(str(amount_traded)), pos["amount"])
            else:
                fraction = sell_fraction_for_signal(signal)
                sell_amount = pos["amount"] * Decimal(str(fraction))
            if original_amount > 0:
                pos["sold_percent"] = min(1.0, pos["sold_percent"] + float(sell_amount) / original_amount)
            pos["amount"] -= sell_amount
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
        if pos["amount"] < 0:
            pos["amount"] = Decimal("0")
    save_positions()
    _sync_open_positions_count()


def _sync_open_positions_count():
    try:
        from data_manager import load_trade_history, save_trade_history
        history = load_trade_history()
        history["open_positions"] = count_open_positions()
        save_trade_history(history)
    except Exception as e:
        log(f"Failed to sync open_positions count: {e}", "WARNING")


def count_open_positions():
    with _positions_lock:
        return sum(1 for p in positions.values() if float(p.get("amount", 0)) > 0.01)

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
            if float(p.get("amount", 0)) > 0.01:
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
                        "realized_pnl": p.get("realized_pnl", 0),
                        "sold_percent": float(p.get("sold_percent", 0)),
                        "last_action": p.get("last_action"),
                        "highlight": highlight,
                    })
        return active
