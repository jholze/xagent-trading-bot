#!/usr/bin/env python3
"""Push all Telegram notification types to the channel (no trades)."""
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv

load_dotenv()

from data.cmc_community_provider import CMCCommunitySignal
from notifications.terminal_dashboard import build_cycle_summary
from notifications.user_explain import explain_hermes_cycle
from telegram_notifier import (
    send_cmc_cycle_digest,
    send_cycle_summary,
    send_hold_explanation_message,
    send_signal_message,
    send_telegram_message,
    send_x_cycle_digest,
    send_x_recommendation_message,
)
from x_analyzer import XSignal

PAUSE = 0.55


def pause():
    time.sleep(PAUSE)


def section(title: str):
    send_telegram_message(f"<b>📬 Showcase — {title}</b>")
    pause()


def main() -> int:
    send_telegram_message(
        "<b>🔄 Transparenz-Showcase gestartet</b>\n"
        "Branch: <code>feature/telegram-coin-links</code>\n"
        "Alle Nachrichtentypen inkl. Coin-Links, Inline-Buttons und Mini-Charts — keine echten Trades."
    )
    pause()

    # 1) Trade signals
    section("1/9 Trade-Signale (BUY / SELL / BLOCKED)")
    coin = {"symbol": "H/USDT", "name": "Humanity Protocol"}
    send_signal_message(
        "BUY",
        coin,
        0.0425,
        38.0,
        0.040,
        1.35,
        "🟢",
        "Bullish",
        executed=True,
        why_de="Technische Analyse sieht Kaufchance. CMC-Community ist bullish.",
        tech_line="TA→BUY | CMC→BUY(78%) | RSI=38.0",
        source_de="Technische Analyse, CMC Community",
        social_lines=["CMC: BUY (78%) — Votes 120↑/45↓"],
        confidence=78,
    )
    pause()
    send_signal_message(
        "SELL_30",
        coin,
        0.048,
        74.5,
        0.045,
        0.9,
        "🔴",
        "Bearish",
        executed=True,
        why_de="RSI überkauft (Stufe 2) — 30 % der Position werden verkauft.",
        tech_line="TA→SELL_30 | RSI=74.5",
        source_de="Technische Analyse",
        confidence=50,
    )
    pause()
    send_signal_message(
        "SELL_30",
        coin,
        0.048,
        74.5,
        0.045,
        0.9,
        "🔴",
        "Bearish",
        executed=False,
        trade_message="Max open positions reached (5)",
        why_de="Verkaufssignal, aber Risiko-Manager hat blockiert.",
        tech_line="TA→SELL_30 | RSI=74.5",
        source_de="Technische Analyse",
    )
    pause()

    # 2) Hold explanation
    section("2/9 Kein Trade trotz Social")
    send_hold_explanation_message(
        "ARIA/USDT",
        "X (@CryptoCapo_) sagt BUY und CMC sagt BUY (82%), "
        "aber die Technik gibt noch kein klares Signal — daher kein Trade.",
        "TA: RSI=52.0 Vol=0.95x",
    )
    pause()

    # 3) X recommendation
    section("3/9 X-Empfehlung")
    send_x_recommendation_message(
        {
            "account": "CryptoCapo_",
            "coin": "BTC",
            "action": "SELL",
            "confidence": 78,
            "trust_at_signal": 72,
            "rationale": "Showcase — Gewinnmitnahme nahe Kursziel.",
            "raw_tweet": "BTC taking profits here, target reached...",
            "price_target": 105000.0,
            "stop_loss": 98000.0,
        }
    )
    pause()

    # 4) CMC digest
    section("4/9 CMC-Zyklus-Digest")
    cmc_signals = [
        CMCCommunitySignal("H", "BUY", 72, "Community bullish auf Dip", votes_bullish=85, votes_bearish=30),
        CMCCommunitySignal("ARIA", "BUY", 65, "Starkes Volumen + positives Sentiment", votes_bullish=60, votes_bearish=40),
    ]
    send_cmc_cycle_digest(cmc_signals)
    pause()

    # 5) X digest
    section("5/9 X-Zyklus-Digest")
    x_signals = [
        XSignal("Pentosh1", "SOL", "BUY", 80, rationale="Showcase — SOL Breakout erwartet", post_id="showcase_x_1"),
        XSignal("CryptoCapo_", "ETH", "SELL", 75, rationale="Showcase — Kurzfristige Korrektur", post_id="showcase_x_2"),
    ]
    for s in x_signals:
        s.trust_score = 70
        s.effective_confidence = s.confidence * 0.7
    send_x_cycle_digest(x_signals)
    pause()

    # 6) Cycle summary
    section("6/9 Zyklus-Zusammenfassung")
    summary = build_cycle_summary(
        coin_results=[
            {
                "symbol": "H/USDT",
                "action": "SELL_30",
                "normalized_action": "SELL_30",
                "executed": True,
                "order_type": "SELL",
                "why_de": "RSI überkauft — 30 % verkauft.",
                "rationale": "TA→SELL_30",
            },
            {
                "symbol": "ARIA/USDT",
                "action": "HOLD",
                "normalized_action": "HOLD",
                "executed": False,
                "why_de": "Social bullish, Technik noch neutral.",
                "rationale": "TA: RSI=52.0",
            },
        ],
        trading_mode="live",
        x_signal_count=2,
        cmc_signal_count=2,
        top_x="@Pentosh1 BUY SOL (56%)",
        top_cmc="H BUY (72%) Votes 85↑/30↓",
    )
    send_cycle_summary(summary)
    pause()

    # 7) Hermes messages
    section("7/9 Hermes (abgelehnt / promoted / veto)")
    send_telegram_message(
        f"🧠 <b>Hermes — Lern-Zyklus</b>\n{explain_hermes_cycle({
            'verdict': 'rejected',
            'variable': 'rsi_sell_30',
            'old_value': 70,
            'new_value': 68,
            'symbol': 'H/USDT',
            'verdict_reason': 'Won 1/4 folds (25% < 55%)',
            'folds_won': 1,
            'folds_total': 4,
            'counterfactual_metrics': {'pnl_delta': -2.5},
        })}"
    )
    pause()
    send_telegram_message(
        f"🧠 <b>Hermes — Strategie übernommen</b>\n{explain_hermes_cycle({
            'verdict': 'promoted',
            'variable': 'take_profit_pct',
            'old_value': 8,
            'new_value': 10,
            'symbol': 'ARIA/USDT',
            'verdict_reason': 'Variant improved and meets success criteria',
            'folds_won': 3,
            'folds_total': 4,
        })}"
    )
    pause()
    send_telegram_message(
        f"🧠 <b>Hermes — Live-Schutz</b>\n{explain_hermes_cycle({
            'verdict': 'rejected',
            'live_veto': True,
            'variable': 'take_profit_pct',
            'old_value': 8,
            'new_value': 12,
            'symbol': 'H/USDT',
            'live_metrics': {'live_sell_pnl': 4.5},
        })}"
    )
    pause()

    # 8) Strategy auto-tune style
    section("8/9 Strategie Auto-Tune")
    send_telegram_message(
        "🔧 <b>Strategie angepasst</b> — H/USDT 4h\n"
        "<b>Warum:</b> Backtest (14 Tage) war mit diesen Werten besser.\n"
        "• Gewinnziel %: 10\n"
        "• RSI Verkauf Stufe 30%: 72\n"
        "Sim-PnL: 12.4 USDT | Nächster Check: Mon 14:00"
    )
    pause()

    # 9) Decisions-style entry
    section("9/9 Entscheidungsprotokoll (Beispiel)")
    from notifications.user_explain import format_decision_entry

    entry = {
        "symbol": "H/USDT",
        "action": "SELL_30",
        "normalized_action": "SELL_30",
        "rationale": "TA→SELL_30 | TA: RSI=74.2 Vol=1.05x",
        "executed": True,
        "timestamp": "2026-06-14T15:30:00",
    }
    send_telegram_message(
        "<b>📜 Beispiel /decisions</b>\n\n" + format_decision_entry(entry)
    )
    pause()

    send_telegram_message(
        "<b>✅ Transparenz-Showcase abgeschlossen</b>\n\n"
        "Neue Befehle: <code>/decisions</code> · <code>/why SYMBOL</code> · <code>/hermes_last</code>\n"
        "Coin-Links: klickbare Ticker, <code>CMC · Gate · Chart</code>, Inline-Buttons bei EXECUTED."
    )
    print("Showcase finished — check Telegram.")
    return 0


if __name__ == "__main__":
    sys.exit(main())