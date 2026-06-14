from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from hermes.live_evidence import compute_live_metrics


NOW = datetime(2026, 6, 14, 13, 0, 0, tzinfo=timezone.utc)


def test_compute_live_metrics_stg_and_aria(sample_live_trade_history, sample_orders_live):
    with patch("hermes.live_evidence.load_live_trade_history", return_value=sample_live_trade_history), \
         patch("hermes.live_evidence._load_orders", return_value=sample_orders_live["orders"]):
        aria = compute_live_metrics("ARIA/USDT", lookback_days=7, now=NOW)
        stg = compute_live_metrics("STG/USDT", lookback_days=7, now=NOW)
        h = compute_live_metrics("H/USDT", lookback_days=7, now=NOW)

    assert aria.live_sell_trades == 2
    assert aria.live_sell_pnl == pytest.approx(-11.04, abs=0.01)
    assert aria.live_pnl_by_source.get("cmc", 0) < 0

    assert stg.live_sell_trades == 2
    assert stg.live_sell_pnl == pytest.approx(4.34, abs=0.01)

    assert h.live_sell_pnl == pytest.approx(42.70, abs=0.01)


def test_exclude_manual_trades(sample_live_trade_history, sample_orders_live):
    with patch("hermes.live_evidence.load_live_trade_history", return_value=sample_live_trade_history), \
         patch("hermes.live_evidence._load_orders", return_value=[]):
        stg = compute_live_metrics(
            "STG/USDT",
            lookback_days=7,
            include_manual_trades=False,
            now=NOW,
        )
    assert stg.live_trades == 2
    assert "manual" not in stg.live_pnl_by_source


def test_reject_rate(sample_orders_live):
    with patch("hermes.live_evidence.load_live_trade_history", return_value={"trades": []}), \
         patch("hermes.live_evidence._load_orders", return_value=sample_orders_live["orders"]):
        stg = compute_live_metrics("STG/USDT", lookback_days=7, now=NOW)
    assert stg.live_order_attempts == 2
    assert stg.live_rejections == 1
    assert stg.live_reject_rate == pytest.approx(0.5)