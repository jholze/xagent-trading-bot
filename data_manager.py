import json
import locale
import os
import shutil
from datetime import datetime

from logger import log

_DEMO_MODE = os.environ.get("DEMO_MODE", "0") == "1"


def is_demo_mode() -> bool:
    """Returns True if the bot is running in demo mode (--demo flag)."""
    return _DEMO_MODE


def get_data_file(base_name: str) -> str:
    """
    Returns the correct filename depending on demo mode.
    If in demo mode and the .demo.json does not exist yet, it copies the real file as starting point.
    """
    if not is_demo_mode():
        return base_name

    if base_name.endswith(".demo.json"):
        demo_path = base_name
    else:
        demo_path = base_name.replace(".json", ".demo.json") if base_name.endswith(".json") else base_name + ".demo.json"

    # If demo file doesn't exist, copy the real one as template (very convenient for testing)
    if not os.path.exists(demo_path) and os.path.exists(base_name):
        try:
            shutil.copy2(base_name, demo_path)
            log(f"Created demo file from existing data: {demo_path}", "INFO")
        except Exception as e:
            log(f"Could not copy {base_name} to {demo_path}: {e}", "WARNING")

    return demo_path


def atomic_write_json(path: str, data: dict):
    """
    Write JSON data atomically using a temp file + rename.
    This greatly reduces the risk of corrupted files on crash or kill.
    """
    dir_name = os.path.dirname(path) or "."
    os.makedirs(dir_name, exist_ok=True)

    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)  # atomic rename
    except Exception as e:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        log(f"Atomic write failed for {path}: {e}", "ERROR")
        raise

WATCHLIST_FILE = "watchlist.json"


def load_watchlist():
    path = get_data_file(WATCHLIST_FILE)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            coins = data.get("coins", [])
            seen = set()
            unique = []
            for c in coins:
                s = c.get("symbol", "")
                if s and s not in seen:
                    seen.add(s)
                    unique.append(c)
            return unique
    except Exception as e:
        log(f"Failed to load watchlist from {path}: {e}", "WARNING")
        return []


def save_watchlist(coins):
    """Speichert die Watchlist in die JSON-Datei"""
    path = get_data_file(WATCHLIST_FILE)
    try:
        atomic_write_json(path, {"coins": coins})
        return True
    except Exception:
        return False


def add_coin(symbol):
    """Einfaches Hinzufügen eines Coins"""
    coins = load_watchlist()
    symbol = symbol.upper().strip()

    if any(c["symbol"] == symbol for c in coins):
        return False, f"{symbol} ist bereits in der Watchlist."

    new_coin = {
        "symbol": symbol,
        "ticker": symbol.split("/")[0] if "/" in symbol else symbol,
        "name": symbol.split("/")[0] if "/" in symbol else symbol,
        "active": True,
    }
    coins.append(new_coin)
    save_watchlist(coins)
    return True, f"✅ {symbol} wurde zur Watchlist hinzugefügt."


def remove_coin(symbol):
    """Entfernt einen Coin aus der Watchlist"""
    coins = load_watchlist()
    new_coins = [c for c in coins if c["symbol"] != symbol.upper()]
    if len(new_coins) == len(coins):
        return False, f"{symbol} nicht in der Watchlist gefunden."
    save_watchlist(new_coins)
    return True, f"✅ {symbol} wurde entfernt."


def list_coins():
    """Gibt alle Coins zurück"""
    return load_watchlist()


def save_full_coin(coin_data):
    coins = load_watchlist()
    coins = [c for c in coins if c.get("symbol") != coin_data.get("symbol")]
    coins.append(coin_data)
    save_watchlist(coins)
    return (
        True,
        f"✅ {coin_data.get('name', coin_data.get('symbol'))} wurde hinzugefügt.",
    )


def load_config():
    """Loads config from disk (always fresh read). Prefer get_config() for cached access."""
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"Failed to load config.json, using defaults: {e}", "WARNING")
        return {
            "virtual_trading": True,
            "initial_capital_usdt": 5000,
            "max_usdt_per_trade": 150,
            "stop_loss_pct": 12.0,
            "max_open_positions": 5,
            "debug": False,
            "x_accounts": ["CryptoCapo_", "Pentosh1"],
            "min_x_confidence": 65,
            "x_weight": 0.45,
            "technical_weight": 0.35,
            "onchain_weight": 0.2,
            "max_daily_trades": 5,
            "strategies": []
        }


# Simple module-level cache to avoid repeated disk reads
_config_cache = None


def get_config():
    """Returns cached config. Use this in most places instead of load_config()."""
    global _config_cache
    if _config_cache is None:
        _config_cache = load_config()
    return _config_cache


def reload_config():
    """Forces reload from disk (useful after manual config changes)."""
    global _config_cache
    _config_cache = None
    return get_config()


def save_config(config):
    path = "config.json"
    try:
        atomic_write_json(path, config)
        # Invalidate cache so subsequent get_config() calls see the change
        global _config_cache
        _config_cache = None
        return True
    except Exception:
        return False


def load_x_accounts():
    path = get_data_file("x_accounts.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"Failed to load {path}: {e}", "WARNING")
        return []


def save_x_accounts(accounts):
    path = get_data_file("x_accounts.json")
    try:
        atomic_write_json(path, accounts)
        return True
    except Exception:
        return False


def load_x_posts():
    path = get_data_file("x_posts.json")
    if not os.path.exists(path):
        return {"posts": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"Failed to load {path}: {e}", "WARNING")
        return {"posts": []}


def save_x_posts(data):
    path = get_data_file("x_posts.json")
    try:
        atomic_write_json(path, data)
        return True
    except Exception:
        return False


def load_demo_data():
    """Load realistic demo data for testing.
    Only has an effect when running with --demo.
    Writes into the .demo.json files.
    """
    if not is_demo_mode():
        log("load_demo_data() called without --demo. Doing nothing.", "WARNING")
        return

    log("Loading demo data into .demo.json files...", "INFO")
    # Demo watchlist with top coins
    watchlist = [
        {"symbol": "ARIA/USDT", "ticker": "ARIA", "name": "Aria AI", "active": True},
        {"symbol": "RAVE/USDT", "ticker": "RAVE", "name": "RaveDAO", "active": True},
        {"symbol": "HIGH/USDT", "ticker": "HIGH", "name": "Highstreet", "active": True},
        {"symbol": "SOL/USDT", "ticker": "SOL", "name": "Solana", "active": True},
        {"symbol": "BTC/USDT", "ticker": "BTC", "name": "Bitcoin", "active": True}
    ]
    save_watchlist(watchlist)

    # Demo positions with realistic average entry and PnL
    # We write via the normal positions module so it respects demo mode
    try:
        from decimal import Decimal
        from strategies.positions import positions as pos_dict, save_positions, get_key
        # Clear existing demo positions first
        pos_dict.clear()
        # Add demo positions
        for key, p in {
            "ARIA_USDT_4h": {"amount": 2150.75, "sold_percent": 0.0, "average_entry": 0.0523, "realized_pnl": 45.2, "last_buy_price": 0.0523, "last_ampel": "🟢", "last_rsi": 32.4, "last_action": "BUY"},
            "RAVE_USDT_4h": {"amount": 875.4, "sold_percent": 0.25, "average_entry": 0.481, "realized_pnl": 128.7, "last_buy_price": 0.481, "last_ampel": "🟡", "last_rsi": 52.1, "last_action": "SELL"},
            "HIGH_USDT_4h": {"amount": 1240.0, "sold_percent": 0.0, "average_entry": 0.172, "realized_pnl": -67.3, "last_buy_price": 0.172, "last_ampel": "🟢", "last_rsi": 41.8, "last_action": "BUY"},
        }.items():
            pos_dict[key] = {k: Decimal(str(v)) if k == "amount" else v for k, v in p.items()}
        save_positions()
    except Exception as e:
        log(f"Could not seed demo positions: {e}", "WARNING")

    # Demo trade history - use the normal save function (respects demo mode)
    trades = {
        "virtual_balance": 4250.75,
        "realized_pnl": 320.4,
        "open_positions": 3,
        "trades": [
            {"type": "BUY", "symbol": "ARIA/USDT", "price": 0.0523, "amount": 2150.75, "usdt_amount": 112.5, "timestamp": "2026-05-23T10:15:00"},
            {"type": "BUY", "symbol": "HIGH/USDT", "price": 0.172, "amount": 1240.0, "usdt_amount": 213.28, "timestamp": "2026-05-23T10:20:00"},
            {"type": "SELL", "symbol": "RAVE/USDT", "price": 0.62, "amount": 218.85, "usdt_received": 135.69, "pnl": 32.4, "timestamp": "2026-05-23T10:45:00"},
        ]
    }
    save_trade_history(trades)

    print("Demo data loaded for testing (into .demo.json files).")



TRADE_HISTORY_FILE = "trade_history.json"
LIVE_TRADE_HISTORY_FILE = "live_trade_history.json"

def load_trade_history():
    path = get_data_file(TRADE_HISTORY_FILE)
    if not os.path.exists(path):
        return {"virtual_balance": 5000.0, "realized_pnl": 0.0, "open_positions": 0, "trades": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"Failed to load {path}: {e}", "WARNING")
        return {"virtual_balance": 5000.0, "realized_pnl": 0.0, "open_positions": 0, "trades": []}

def save_trade_history(data):
    path = get_data_file(TRADE_HISTORY_FILE)
    try:
        atomic_write_json(path, data)
        return True
    except Exception:
        return False

def load_live_trade_history():
    path = get_data_file(LIVE_TRADE_HISTORY_FILE)
    if not os.path.exists(path):
        return {"trades": [], "total_pnl": 0.0}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"Failed to load {path}: {e}", "WARNING")
        return {"trades": [], "total_pnl": 0.0}


def save_live_trade_history(data):
    path = get_data_file(LIVE_TRADE_HISTORY_FILE)
    try:
        atomic_write_json(path, data)
        return True
    except Exception:
        return False


def record_live_trade(trade):
    history = load_live_trade_history()
    history.setdefault("trades", []).append(trade)
    if trade.get("type") == "SELL":
        history["total_pnl"] = history.get("total_pnl", 0) + trade.get("pnl", 0)
    save_live_trade_history(history)
    return history


def record_trade(trade):
    history = load_trade_history()
    history["trades"].append(trade)
    if trade.get("type") == "BUY":
        history["virtual_balance"] = max(0, history["virtual_balance"] - trade.get("usdt_amount", 0))
    else:
        history["virtual_balance"] += trade.get("usdt_received", 0)
        history["realized_pnl"] += trade.get("pnl", 0)
    try:
        from strategies.positions import count_open_positions
        history["open_positions"] = count_open_positions()
    except Exception:
        if trade.get("type") == "BUY":
            history["open_positions"] = history.get("open_positions", 0) + 1
        else:
            history["open_positions"] = max(0, history.get("open_positions", 0) - 1)
    save_trade_history(history)
    return history


ORDERS_SCOPE_FILES = {
    "demo": "orders.demo.json",
    "paper": "orders.paper.json",
    "live": "orders.live.json",
}


def resolve_orders_file(scope: str) -> str:
    if scope not in ORDERS_SCOPE_FILES:
        raise ValueError(f"Invalid ledger scope: {scope}")
    return ORDERS_SCOPE_FILES[scope]


def resolve_ledger_scope(trading_mode: str = None) -> str:
    if is_demo_mode():
        return "demo"
    mode = trading_mode or get_config().get("trading_mode", "paper")
    if mode == "live":
        return "live"
    return "paper"


def uses_exchange_ledger(trading_mode: str = None) -> bool:
    mode = trading_mode or get_config().get("trading_mode", "paper")
    return mode == "live"


def _empty_orders(scope: str) -> dict:
    return {"ledger_scope": scope, "orders": [], "migrated_from_trades": False}


def load_orders(scope: str):
    path = resolve_orders_file(scope)
    if not os.path.exists(path):
        return _empty_orders(scope)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("ledger_scope") != scope:
            data["ledger_scope"] = scope
        return data
    except Exception as e:
        log(f"Failed to load {path}: {e}", "WARNING")
        return _empty_orders(scope)


def save_orders(data: dict, scope: str) -> bool:
    path = resolve_orders_file(scope)
    try:
        data["ledger_scope"] = scope
        atomic_write_json(path, data)
        return True
    except Exception:
        return False


PAPER_STRATEGIES_FILE = "paper_strategies.json"
PAPER_SANDBOX_HISTORY_FILE = "paper_sandbox_history.json"
CMC_POSTS_FILE = "cmc_posts.json"


def load_cmc_posts():
    path = get_data_file(CMC_POSTS_FILE)
    if not os.path.exists(path):
        return {"posts": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"Failed to load {path}: {e}", "WARNING")
        return {"posts": []}


def save_cmc_posts(data):
    path = get_data_file(CMC_POSTS_FILE)
    try:
        atomic_write_json(path, data)
        return True
    except Exception:
        return False


def log_cmc_post(signal, post_id: str = None):
    data = load_cmc_posts()
    entry = {
        "timestamp": datetime.now().isoformat(),
        "post_id": post_id or getattr(signal, "post_id", None),
        "coin": getattr(signal, "coin", ""),
        "action": getattr(signal, "action", "HOLD"),
        "confidence": getattr(signal, "confidence", 0),
        "rationale": getattr(signal, "rationale", ""),
        "votes_bullish": getattr(signal, "votes_bullish", 0),
        "votes_bearish": getattr(signal, "votes_bearish", 0),
        "source": "cmc",
    }
    pid = entry.get("post_id")
    if pid and any(p.get("post_id") == pid for p in data.get("posts", [])):
        return data
    data.setdefault("posts", []).append(entry)
    save_cmc_posts(data)
    return data


def load_paper_strategies():
    path = get_data_file(PAPER_STRATEGIES_FILE)
    if not os.path.exists(path):
        return {"hypotheses": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"Failed to load {path}: {e}", "WARNING")
        return {"hypotheses": []}


def save_paper_strategies(data):
    path = get_data_file(PAPER_STRATEGIES_FILE)
    try:
        atomic_write_json(path, data)
        return True
    except Exception:
        return False


def load_paper_sandbox_history():
    path = get_data_file(PAPER_SANDBOX_HISTORY_FILE)
    if not os.path.exists(path):
        return {"portfolios": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"Failed to load {path}: {e}", "WARNING")
        return {"portfolios": {}}


def save_paper_sandbox_history(data):
    path = get_data_file(PAPER_SANDBOX_HISTORY_FILE)
    try:
        atomic_write_json(path, data)
        return True
    except Exception:
        return False


TRANSLATIONS = {}

def load_translations():
    global TRANSLATIONS
    try:
        with open("locales/en.json", "r", encoding="utf-8") as f:
            TRANSLATIONS["en"] = json.load(f)
        with open("locales/de.json", "r", encoding="utf-8") as f:
            TRANSLATIONS["de"] = json.load(f)
    except Exception as e:
        log(f"Failed to load translation files: {e}", "WARNING")
        TRANSLATIONS = {"en": {}, "de": {}}

def get_system_lang():
    try:
        lang = locale.getdefaultlocale()[0] or "en_US"
        if lang.lower().startswith("de"):
            return "de"
        return "en"
    except Exception as e:
        log(f"Failed to detect system language: {e}", "WARNING")
        return "en"

def get_text(key, default=""):
    lang = get_system_lang()
    trans = TRANSLATIONS.get(lang, TRANSLATIONS.get("en", {}))
    return trans.get(key, default or key)

load_translations()

