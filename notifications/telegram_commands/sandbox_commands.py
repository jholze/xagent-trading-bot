from notifications.telegram_commands.command_context import activate_command
from notifications.telegram_commands.usage_hints import hint
from strategies.paper_sandbox import PaperSandbox
from telegram_notifier import send_telegram_message


_sandbox = PaperSandbox()


def handle(text: str) -> bool:
    if text == "/sandbox":
        _sandbox.config.refresh()
        testing = _sandbox.list_testing()
        if not testing:
            send_telegram_message("📦 <b>Paper Sandbox</b>\n\nNo hypotheses in testing. Strategy concepts are auto-discovered from X posts with RSI/volume/breakout keywords.")
            return True

        msg = "<b>📦 Paper Sandbox — Testing</b>\n\n"
        for hyp in testing[:10]:
            metrics = hyp.get("metrics") or {}
            msg += (
                f"<b>{hyp.get('id')}</b> — {hyp.get('name')}\n"
                f"  {hyp.get('symbol') or 'any'} | {hyp.get('timeframe')} | @{hyp.get('source_account')}\n"
                f"  WR: {metrics.get('win_rate', 0):.0f}% | Sharpe: {metrics.get('sharpe', 0):.2f} | "
                f"DD: {metrics.get('max_drawdown_pct', 0):.0f}% | Trades: {metrics.get('trades', 0)}\n\n"
            )
        msg += "Use /sandbox_results ID or /sandbox_promote ID"
        send_telegram_message(msg)
        return True

    if text == "/sandbox_results":
        activate_command("sandbox_results")
        send_telegram_message(hint("sandbox_results"))
        return True

    if text == "/sandbox_promote":
        activate_command("sandbox_promote")
        send_telegram_message(hint("sandbox_promote"))
        return True

    if text.startswith("/sandbox_results"):
        parts = text.split()
        if len(parts) < 2:
            send_telegram_message(hint("sandbox_results"))
            return True
        hyp_id = parts[1].strip()
        from intelligence.strategy_discovery import StrategyDiscovery
        discovery = StrategyDiscovery()
        hyp = discovery.get_hypothesis(hyp_id)
        if not hyp:
            send_telegram_message(f"❌ Hypothesis {hyp_id} not found.")
            return True

        metrics = _sandbox.compute_metrics(hyp_id)
        ready, reason = _sandbox.promotion_ready(hyp_id)
        msg = f"""<b>📊 Sandbox Results — {hyp.get('name')}</b>

ID: <code>{hyp_id}</code>
Status: {hyp.get('status')}
Symbol: {hyp.get('symbol') or '—'} | TF: {hyp.get('timeframe')}
Source: @{hyp.get('source_account')}

<b>Metrics</b>
Win rate: {metrics.win_rate:.1f}%
Sharpe: {metrics.sharpe:.2f}
Max drawdown: {metrics.max_drawdown_pct:.1f}%
Trades: {metrics.trades}
Realized PnL: ${metrics.realized_pnl:.2f}
Equity: ${metrics.equity:.2f}

<b>Promotion</b>
{'✅ ' + reason if ready else '⏳ ' + reason}
"""
        send_telegram_message(msg)
        return True

    if text.startswith("/sandbox_promote"):
        parts = text.split()
        if len(parts) < 2:
            send_telegram_message(hint("sandbox_promote"))
            return True
        hyp_id = parts[1].strip()
        ok, msg = _sandbox.promote(hyp_id)
        send_telegram_message(f"{'✅' if ok else '❌'} {msg}")
        return True

    return False