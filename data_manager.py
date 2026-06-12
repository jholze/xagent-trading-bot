import json
import locale
import os
import shutil
from datetime import datetime

from logger import log

def is_demo_mode() -> bool:
    """Returns True if the bot is running in demo mode (--demo flag or pytest)."""
    return os.environ.get("DEMO_MODE", "0") == "1"


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
DRY_RUN_OVERLAY_FILE = "watchlist.dry_run_overlay.json"


def is_dry_run_enhanced(config: dict = None) -> bool:
    """True when live + dry_run + dry_run_enhanced — never when dry_run is false."""
    cfg = config or get_config()
    if cfg.get("trading_mode") != "live":
        return False
    live = cfg.get("live", {})
    if not live.get("dry_run", True):
        return False
    return bool(live.get("dry_run_enhanced", False))


def simulated_balance_usdt(config: dict = None) -> float:
    cfg = config or get_config()
    return float(cfg.get("live", {}).get("simulated_balance_usdt", 5000))


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
    return load_effective_watchlist()


def load_dry_run_overlay():
    path = get_data_file(DRY_RUN_OVERLAY_FILE)
    if not os.path.exists(path):
        return {"refreshed_at": "", "source": "", "coins": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"Failed to load {path}: {e}", "WARNING")
        return {"refreshed_at": "", "source": "", "coins": []}


def save_dry_run_overlay(data: dict) -> bool:
    path = get_data_file(DRY_RUN_OVERLAY_FILE)
    try:
        atomic_write_json(path, data)
        return True
    except Exception:
        return False


def _dedupe_watchlist_coins(coins: list) -> list:
    seen = set()
    unique = []
    for c in coins:
        sym = c.get("symbol", "")
        if sym and sym not in seen:
            seen.add(sym)
            unique.append(c)
    return unique


def load_effective_watchlist():
    """Base watchlist merged with CMC trending overlay when enhanced dry run is active."""
    base = load_watchlist()
    if not is_dry_run_enhanced():
        return base
    overlay = load_dry_run_overlay()
    overlay_coins = overlay.get("coins", [])
    if not overlay_coins:
        return base
    return _dedupe_watchlist_coins(base + overlay_coins)


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

def _ensure_live_virtual_balance(history: dict, config: dict = None) -> dict:
    cfg = config or get_config()
    if not is_dry_run_enhanced(cfg):
        return history
    if "virtual_balance" not in history:
        history["virtual_balance"] = simulated_balance_usdt(cfg)
    return history


def _reconcile_live_trade_sources(history: dict) -> tuple:
    changed = False
    try:
        from services.order_service import OrderService, infer_manual_source

        svc = OrderService("live")
        if svc.reconcile_legacy_sources():
            changed = True
        by_id = {o["id"]: o.get("source") for o in svc._load().get("orders", []) if o.get("id")}
        for trade in history.get("trades", []):
            oid = trade.get("order_id")
            src = by_id.get(oid) if oid else None
            if not src:
                side = "buy" if trade.get("type") == "BUY" else "sell"
                signal = "" if trade.get("type") == "BUY" else "SELL"
                src = infer_manual_source({
                    "side": side,
                    "signal": signal,
                    "source": trade.get("source"),
                })
            if src and trade.get("source") != src:
                trade["source"] = src
                changed = True
    except Exception:
        pass
    return history, changed


def load_live_trade_history():
    path = get_data_file(LIVE_TRADE_HISTORY_FILE)
    if not os.path.exists(path):
        history = {"trades": [], "total_pnl": 0.0, "realized_pnl": 0.0}
        return _ensure_live_virtual_balance(history)
    try:
        with open(path, "r", encoding="utf-8") as f:
            history = json.load(f)
            if history.get("total_pnl") is not None and history.get("realized_pnl") is None:
                history["realized_pnl"] = history["total_pnl"]
            history, reconciled = _reconcile_live_trade_sources(history)
            history = _ensure_live_virtual_balance(history)
            if reconciled:
                save_live_trade_history(history)
            return history
    except Exception as e:
        log(f"Failed to load {path}: {e}", "WARNING")
        history = {"trades": [], "total_pnl": 0.0, "realized_pnl": 0.0}
        return _ensure_live_virtual_balance(history)


def save_live_trade_history(data):
    path = get_data_file(LIVE_TRADE_HISTORY_FILE)
    try:
        atomic_write_json(path, data)
        return True
    except Exception:
        return False


def record_live_trade(trade):
    cfg = get_config()
    history = load_live_trade_history()
    history.setdefault("trades", []).append(trade)
    if is_dry_run_enhanced(cfg):
        history = _ensure_live_virtual_balance(history, cfg)
        if trade.get("type") == "BUY":
            history["virtual_balance"] = max(
                0.0,
                float(history.get("virtual_balance", simulated_balance_usdt(cfg)))
                - float(trade.get("usdt_amount", 0) or 0),
            )
        elif trade.get("type") == "SELL":
            history["virtual_balance"] = (
                float(history.get("virtual_balance", simulated_balance_usdt(cfg)))
                + float(trade.get("usdt_received", 0) or 0)
            )
    if trade.get("type") == "SELL":
        history["total_pnl"] = history.get("total_pnl", 0) + trade.get("pnl", 0)
        history["realized_pnl"] = history["total_pnl"]
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

POSITIONS_SCOPE_FILES = {
    "demo": "positions.demo.json",
    "paper": "positions.paper.json",
    "live": "positions.live.json",
}


def resolve_orders_file(scope: str) -> str:
    if scope not in ORDERS_SCOPE_FILES:
        raise ValueError(f"Invalid ledger scope: {scope}")
    return ORDERS_SCOPE_FILES[scope]


def resolve_positions_file(scope: str) -> str:
    if scope not in POSITIONS_SCOPE_FILES:
        raise ValueError(f"Invalid ledger scope: {scope}")
    if scope == "demo":
        return get_data_file("positions.json")
    return POSITIONS_SCOPE_FILES[scope]


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


STRATEGY_BACKTEST_FILE = "strategy_backtest.json"


def load_strategy_backtest_results() -> dict:
    path = get_data_file(STRATEGY_BACKTEST_FILE)
    if not os.path.exists(path):
        return {"coins": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            data.setdefault("coins", {})
            return data
    except Exception as e:
        log(f"Failed to load {path}: {e}", "WARNING")
        return {"coins": {}}


def save_strategy_backtest_results(data: dict) -> bool:
    path = get_data_file(STRATEGY_BACKTEST_FILE)
    try:
        data.setdefault("coins", {})
        atomic_write_json(path, data)
        return True
    except Exception:
        return False


def get_strategy_backtest_entry(key: str) -> dict:
    return load_strategy_backtest_results().get("coins", {}).get(key, {})


def save_strategy_backtest_entry(key: str, entry: dict) -> bool:
    data = load_strategy_backtest_results()
    data.setdefault("coins", {})[key] = entry
    return save_strategy_backtest_results(data)


def list_strategy_targets() -> list:
    """Unique strategy entries from config.strategies (no trending-only coins)."""
    cfg = get_config()
    seen = set()
    targets = []
    for entry in cfg.get("strategies", []):
        symbol = entry.get("symbol")
        tf = entry.get("timeframe", "4h")
        if not symbol:
            continue
        if entry.get("live_enabled") is False and cfg.get("trading_mode") == "live":
            continue
        key = f"{symbol}_{tf}"
        if key in seen:
            continue
        seen.add(key)
        targets.append(dict(entry))
    return targets


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

