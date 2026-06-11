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
    dry = cfg.get("dry_run", True)

    return f"""<b>{title}</b>
{key_env}: {'✅ gesetzt' if has_key else '❌ fehlt'}
{secret_env}: {'✅ gesetzt' if has_secret else '❌ fehlt'}
Dry Run: <b>{'ON' if dry else 'OFF'}</b>
Max/Trade: ${cfg.get('max_usdt_per_trade', 150):.0f} USDT
USDT verfügbar: <b>${balance:,.2f}</b>"""


def handle(text: str) -> bool:
    if text not in ["/gate", "/gatestatus", "/gate_status", "/gate mainnet"]:
        return False

    reload_config()
    cfg = get_bot_config()
    trading = TradingService(cfg)
    adapter = GateExecutionAdapter(cfg)

    msg = f"<b>🔗 Gate.io Status</b>\n\n<b>Bot-Modus:</b> {trading.mode_label()}\n\n"
    msg += _gate_section("Mainnet (Live)", cfg.live_config, adapter)
    msg += "\n\n"

    if cfg.trading_mode == "live":
        holdings = fetch_spot_holdings(cfg)
        if holdings:
            prices = get_prices_batch([h["symbol"] for h in holdings])
            msg += "<b>Spot-Bestände</b>\n"
            msg += "\n".join(format_holdings_lines(holdings, prices))
            msg += "\n\n"

    msg += """<b>Modi</b>
/mode paper — Lokales Paper (JSON-Ledger)
/mode live + /live_confirm — Echtes Spot-Trading auf Gate.io

<b>Live aktivieren</b>
1. Keys in .env: GATE_API_KEY / GATE_API_SECRET
2. /mode live → /live_confirm
3. <code>live.dry_run: false</code> in config.json für echte Orders
"""
    send_telegram_message(msg)
    return True