import os

from core.config import get_bot_config
from data_manager import get_config, reload_config
from execution.gate_adapter import GateExecutionAdapter
from services.trading_service import TradingService
from telegram_notifier import send_telegram_message


def handle(text: str) -> bool:
    if text not in ["/gate", "/gatestatus", "/gate_status"]:
        return False

    reload_config()
    cfg = get_bot_config()
    live = cfg.live_config
    key_env = live.get("api_key_env", "GATE_API_KEY")
    secret_env = live.get("api_secret_env", "GATE_API_SECRET")
    has_key = bool(os.getenv(key_env))
    has_secret = bool(os.getenv(secret_env))

    adapter = GateExecutionAdapter(cfg)
    balance = adapter._fetch_usdt_balance() if has_key and has_secret else 0.0
    trading = TradingService(cfg)

    msg = f"""<b>🔗 Gate.io Status</b>

<b>API Keys</b>
{key_env}: {'✅ gesetzt' if has_key else '❌ fehlt'}
{secret_env}: {'✅ gesetzt' if has_secret else '❌ fehlt'}

<b>Trading</b>
Modus: <b>{trading.mode_label()}</b>
Dry Run: <b>{'ON (keine echten Orders)' if live.get('dry_run', True) else 'OFF ⚠️'}</b>
Max/Trade: ${live.get('max_usdt_per_trade', 150):.0f} USDT
Min Trust (X-Live): {live.get('require_min_trust_score', 70)}

<b>Konto</b>
USDT verfügbar: <b>${balance:,.2f}</b>

<b>Live aktivieren</b>
1. Keys in .env eintragen
2. /mode live
3. /live_confirm
4. Erst dry_run testen, dann live.dry_run: false
"""
    send_telegram_message(msg)
    return True