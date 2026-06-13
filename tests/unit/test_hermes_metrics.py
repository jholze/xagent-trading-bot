from core.models import SandboxMetrics
from hermes.metrics import compute_trade_quality, enrich_sandbox_metrics, opportunity_score


def test_trade_quality_positive_for_winning_trades():
    trades = [
        {"type": "BUY", "usdt": 50},
        {"type": "SELL", "pnl": 10, "usdt_received": 55},
        {"type": "BUY", "usdt": 50},
        {"type": "SELL", "pnl": 5, "usdt_received": 52},
    ]
    tq = compute_trade_quality(trades)
    assert tq["trade_quality"] > 0
    assert tq["win_count"] == 2


def test_opportunity_score_scales_with_trade_frequency():
    trades = [
        {"type": "BUY"},
        {"type": "SELL", "pnl": 8, "usdt_received": 50},
        {"type": "BUY"},
        {"type": "SELL", "pnl": 6, "usdt_received": 50},
    ]
    score = opportunity_score(trades, bars_tested=42, bars_per_day=6)
    assert score > 0


def test_enrich_sandbox_metrics():
    metrics = SandboxMetrics(trades=2, win_rate=100.0)
    trades = [
        {"type": "BUY"},
        {"type": "SELL", "pnl": 5, "usdt_received": 50},
    ]
    enriched = enrich_sandbox_metrics(metrics, trades, bars_tested=30)
    assert enriched.trade_quality > 0
    assert enriched.buy_signals == 1