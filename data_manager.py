import json
import locale
import os

WATCHLIST_FILE = "watchlist.json"


def load_watchlist():
    if not os.path.exists(WATCHLIST_FILE):
        return []
    try:
        with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
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
        print(f"Fehler beim Laden der Watchlist: {e}")
        return []


def save_watchlist(coins):
    """Speichert die Watchlist in die JSON-Datei"""
    try:
        with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
            json.dump({"coins": coins}, f, indent=4, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"Fehler beim Speichern der Watchlist: {e}")
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
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except:
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


def save_config(config):
    try:
        with open("config.json", "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        return True
    except:
        return False


def load_x_accounts():
    if not os.path.exists("x_accounts.json"):
        return []
    try:
        with open("x_accounts.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return []


def save_x_accounts(accounts):
    try:
        with open("x_accounts.json", "w", encoding="utf-8") as f:
            json.dump(accounts, f, indent=2, ensure_ascii=False)
        return True
    except:
        return False



TRADE_HISTORY_FILE = "trade_history.json"

def load_trade_history():
    if not os.path.exists(TRADE_HISTORY_FILE):
        return {"virtual_balance": 5000.0, "realized_pnl": 0.0, "open_positions": 0, "trades": []}
    try:
        with open(TRADE_HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {"virtual_balance": 5000.0, "realized_pnl": 0.0, "open_positions": 0, "trades": []}

def save_trade_history(data):
    try:
        with open(TRADE_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return True
    except:
        return False

def record_trade(trade):
    history = load_trade_history()
    history["trades"].append(trade)
    if trade.get("type") == "BUY":
        history["open_positions"] = min(history.get("open_positions", 0) + 1, 5)
        history["virtual_balance"] = max(0, history["virtual_balance"] - trade.get("usdt_amount", 0))
    else:
        history["open_positions"] = max(0, history.get("open_positions", 0) - 1)
        history["virtual_balance"] += trade.get("usdt_received", 0)
        history["realized_pnl"] += trade.get("pnl", 0)
    save_trade_history(history)
    return history


TRANSLATIONS = {}

def load_translations():
    global TRANSLATIONS
    try:
        with open("locales/en.json", "r", encoding="utf-8") as f:
            TRANSLATIONS["en"] = json.load(f)
        with open("locales/de.json", "r", encoding="utf-8") as f:
            TRANSLATIONS["de"] = json.load(f)
    except:
        TRANSLATIONS = {"en": {}, "de": {}}

def get_system_lang():
    try:
        lang = locale.getdefaultlocale()[0] or "en_US"
        if lang.lower().startswith("de"):
            return "de"
        return "en"
    except:
        return "en"

def get_text(key, default=""):
    lang = get_system_lang()
    trans = TRANSLATIONS.get(lang, TRANSLATIONS.get("en", {}))
    return trans.get(key, default or key)

load_translations()

