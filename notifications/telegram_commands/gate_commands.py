import os

from core.build_info import format_build_line
from core.config import get_bot_config
from data_manager import is_dry_run_enhanced, reload_config
from execution.gate_adapter import GateExecutionAdapter
from price_fetcher import get_prices_batch
from services.gate_balance import fetch_spot_holdings, format_holdings_lines
from services.trading_service import TradingService
from telegram_notifier import send_telegram_message


def _gate_key_status(cfg: dict, adapter: GateExecutionAdapter) -> tuple:
    key_env = cfg.get("api_key_env", "GATE_API_KEY")
    secret_env = cfg.get("api_secret_env", "GATE_API_SECRET")
    has_key = bool(os.getenv(key_env))
    has_secret = bool(os.getenv(secret_env))
    balance = 0.0
    api_hint = ""

    if not has_key or not has_secret:
        api_hint = (
            f"\n⚠️ Keys in <code>.env</code> setzen: <code>{key_env}</code> / <code>{secret_env}</code>"
        )
    else:
        balance = adapter._fetch_usdt_balance()
        err = getattr(adapter, "_last_api_error", "") or ""
        if "INVALID_KEY" in err or "Invalid key" in err:
            api_hint = (
                "\n⚠️ <b>INVALID_KEY</b> — Gate lehnt die API-Keys ab.\n"
                "• Neue Keys im Gate.io Dashboard erstellen (Spot-Trading, Read + Trade)\n"
                "• IP-Whitelist prüfen (leer = alle IPs)\n"
                "• <code>.env</code> aktualisieren und Bot neu starten"
            )
        elif err:
            api_hint = f"\n⚠️ Gate API: <code>{err[:80]}</code>"

    return key_env, secret_env, balance, api_hint, has_key, has_secret


def _gate_section(title: str, cfg: dict, adapter: GateExecutionAdapter, bot_config=None) -> str:
    key_env, secret_env, balance, api_hint, has_key, has_secret = _gate_key_status(cfg, adapter)
    dry = cfg.get("dry_run", True)
    enhanced = bot_config.is_dry_run_enhanced() if bot_config else (
        is_dry_run_enhanced({"trading_mode": "live", "live": cfg})
    )
    balance_line = f"USDT verfügbar: <b>${balance:,.2f}</b>"
    if enhanced:
        from services.gate_balance import fetch_usdt_balance
        sim = fetch_usdt_balance(bot_config or get_bot_config())
        balance_line = (
            f"Simulated USDT: <b>${sim:,.2f}</b>\n"
            f"Gate USDT (API): <b>${balance:,.2f}</b>"
        )

    enhanced_line = "\nEnhanced Dry Run: <b>ON</b>" if enhanced else ""

    return f"""<b>{title}</b>
{key_env}: {'✅ gesetzt' if has_key else '❌ fehlt'}
{secret_env}: {'✅ gesetzt' if has_secret else '❌ fehlt'}
Dry Run: <b>{'ON' if dry else 'OFF'}</b>{enhanced_line}
Max/Trade: ${cfg.get('max_usdt_per_trade', 150):.0f} USDT
{balance_line}{api_hint}"""


def _format_dryrun_status() -> str:
    from services.dry_run_watchlist import DryRunWatchlistSync

    cfg = get_bot_config()
    status = DryRunWatchlistSync(cfg).status()
    if not status.get("enabled"):
        return "Enhanced Dry Run ist <b>OFF</b> (live.dry_run_enhanced in config.json)."

    refreshed = status.get("refreshed_at") or "—"
    return (
        "<b>🧪 Enhanced Dry Run</b>\n\n"
        f"Status: <b>ON</b>\n"
        f"Simulated USDT: <b>${status.get('simulated_balance', 0):,.2f}</b>\n"
        f"Trending Coins: <b>{status.get('trending_count', 0)}</b>\n"
        f"Letzte Sync: <code>{refreshed}</code>\n"
        f"Quelle: <code>{status.get('source') or '—'}</code>"
    )


def handle(text: str) -> bool:
    if text in ["/dryrun", "/dry_run"]:
        reload_config()
        send_telegram_message(_format_dryrun_status())
        return True

    if text not in ["/gate", "/gatestatus", "/gate_status", "/gate mainnet"]:
        return False

    reload_config()
    cfg = get_bot_config()
    trading = TradingService(cfg)
    adapter = GateExecutionAdapter(cfg)

    msg = (
        f"<b>🔗 Gate.io Status</b>\n\n"
        f"<b>Bot-Modus:</b> {trading.mode_label()}\n"
        f"{format_build_line()}\n\n"
    )
    msg += _gate_section("Mainnet (Live)", cfg.live_config, adapter, bot_config=cfg)
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