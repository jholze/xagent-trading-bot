import os

from core.config import get_bot_config
from data_manager import reload_config
from execution.gate_adapter import GateExecutionAdapter
from price_fetcher import get_prices_batch
from services.gate_balance import fetch_spot_holdings, format_holdings_lines
from services.trading_service import TradingService
from telegram_notifier import send_telegram_message


def _gate_section(title: str, cfg: dict, adapter: GateExecutionAdapter) -> str:
    key_env = cfg.get("api_key_env", "GATE_API_KEY")
    secret_env = cfg.get("api_secret_env", "GATE_API_SECRET")
    has_key = bool(os.getenv(key_env))
    has_secret = bool(os.getenv(secret_env))
    balance = adapter._fetch_usdt_balance() if has_key and has_secret else 0.0
    dry_default = True if not adapter.testnet else False
    dry = cfg.get("dry_run", dry_default)

    return f"""<b>{title}</b>
{key_env}: {'✅ gesetzt' if has_key else '❌ fehlt'}
{secret_env}: {'✅ gesetzt' if has_secret else '❌ fehlt'}
Dry Run: <b>{'ON' if dry else 'OFF'}</b>
Max/Trade: ${cfg.get('max_usdt_per_trade', 150):.0f} USDT
USDT verfügbar: <b>${balance:,.2f}</b>"""


def handle(text: str) -> bool:
    if text not in ["/gate", "/gatestatus", "/gate_status", "/gate testnet", "/gate mainnet"]:
        return False

    reload_config()
    cfg = get_bot_config()
    trading = TradingService(cfg)

    mainnet_adapter = GateExecutionAdapter(cfg, testnet=False)
    testnet_adapter = GateExecutionAdapter(cfg, testnet=True)

    show_mainnet = text != "/gate testnet"
    show_testnet = text in ["/gate", "/gate testnet"]

    msg = f"<b>🔗 Gate.io Status</b>\n\n<b>Bot-Modus:</b> {trading.mode_label()}\n\n"

    if show_mainnet:
        msg += _gate_section("Mainnet (Live)", cfg.live_config, mainnet_adapter)
        msg += "\n\n"

    if show_testnet:
        msg += _gate_section("Testnet (Paper auf Gate)", cfg.gate_testnet_config, testnet_adapter)
        msg += "\n\n"

    if show_mainnet and cfg.trading_mode == "live":
        holdings = fetch_spot_holdings(cfg)
        if holdings:
            prices = get_prices_batch([h["symbol"] for h in holdings])
            msg += "<b>Mainnet Spot-Bestände</b>\n"
            msg += "\n".join(format_holdings_lines(holdings, prices))
            msg += "\n\n"
    elif show_testnet and cfg.trading_mode == "gate_testnet":
        holdings = fetch_spot_holdings(cfg)
        if holdings:
            prices = get_prices_batch([h["symbol"] for h in holdings])
            msg += "<b>Testnet Spot-Bestände</b>\n"
            msg += "\n".join(format_holdings_lines(holdings, prices))
            msg += "\n\n"

    msg += """<b>Modi</b>
/mode paper — Lokales Paper (JSON-Ledger)
/mode gate_testnet — Orders auf Gate Testnet
/mode live + /live_confirm — Echtes Spot-Trading

<b>Testnet aktivieren</b>
1. Gate → API Management → Account type: <b>Testnet</b>
2. Keys in .env: GATE_TESTNET_API_KEY / GATE_TESTNET_API_SECRET
3. /mode gate_testnet
4. Trades sichtbar auf Gate Testnet (Spot Order History)
"""
    send_telegram_message(msg)
    return True