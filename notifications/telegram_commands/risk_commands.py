from services.trading_service import TradingService
from telegram_notifier import send_telegram_message

_trading = TradingService()


def handle(text: str) -> bool:
    if text != "/risk":
        return False

    _trading.refresh()
    status = _trading.risk.status_summary()
    risk_cfg = _trading.config.risk_config
    aggression = _trading.config.aggression_config

    throttle = "ON (size halved)" if status["drawdown_throttle_active"] else "off"
    msg = f"""<b>🛡️ Risk Status</b>

<b>Portfolio</b>
Equity: <b>${status['portfolio_equity']:.0f}</b> | Balance: ${status['virtual_balance']:.0f}
Drawdown: <b>{status['drawdown_pct']:.1f}%</b> | Throttle: {throttle}

<b>Limits</b>
Open positions: {status['open_positions']}/{status['max_open_positions']}
Daily buys (24h): {status.get('daily_buys', status['daily_trades'])}/{status.get('max_daily_buys', status['max_daily_trades'])}
Daily sells (24h): {status.get('daily_sells', 0)}/{status.get('max_daily_sells', 0) or '∞'}
Max per coin: {status['max_position_percent']:.0f}% of portfolio
Base trade size: ${status['base_usdt_per_trade']:.0f} USDT

<b>Dynamic sizing</b>
ATR reference: {risk_cfg.get('atr_reference_pct', 3.0)}%
Drawdown throttle: {risk_cfg.get('drawdown_throttle_pct', 10.0)}% → ×{risk_cfg.get('drawdown_size_multiplier', 0.5)}
Max multiplier: ×{aggression.get('max_position_multiplier', 2.0)}
"""
    send_telegram_message(msg)
    return True