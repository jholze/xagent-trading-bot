import json
import os
from decimal import Decimal
from pathlib import Path

POSITIONS_FILE = "positions.json"

def load_positions():
    if os.path.exists(POSITIONS_FILE):
        try:
            with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
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
                    }
        except:
            pass

def save_positions():
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
            }
        with open(POSITIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except:
        pass

positions = {}
load_positions()

def get_key(symbol, timeframe):
    return f"{symbol.replace('/', '_')}_{timeframe}"

def init_position(symbol, timeframe):
    key = get_key(symbol, timeframe)
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
        }

def get_position(symbol, timeframe):
    init_position(symbol, timeframe)
    return positions[get_key(symbol, timeframe)]

def update_position(symbol, timeframe, signal, current_price, amount_traded=0):
    init_position(symbol, timeframe)
    key = get_key(symbol, timeframe)
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
    elif signal == "SELL_30" or signal == "SELL_20" or "SELL" in signal:
        sell_amount = pos["amount"] * Decimal("0.3" if "30" in signal else "0.2")
        pos["amount"] -= sell_amount
        pos["sold_percent"] += 0.3 if "30" in signal else 0.2
        pos["last_action"] = "SELL"
    if pos["amount"] < 0:
        pos["amount"] = Decimal("0")
    save_positions()

def get_total_aria():
    total = Decimal("0")
    for pos in positions.values():
        total += pos["amount"]
    return total

def list_active_positions():
    active = []
    for key, p in positions.items():
        if float(p.get("amount", 0)) > 0.01:
            symbol = key.split("_")[0] if "_" in key else key
            if not symbol.upper().startswith("TEST"):
                highlight = "🔥 " if p.get("last_action") == "BUY" else ""
                active.append({
                    "symbol": symbol,
                    "amount": float(p["amount"]),
                    "entry_price": p.get("entry_price", 0),
                    "realized_pnl": p.get("realized_pnl", 0),
                    "highlight": highlight,
                })
    return active
