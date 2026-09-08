"""Backtester: fee 0 / slip 0 matches naive; 0.2 % fee lowers pnl by the modelled cost."""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from core.config import BotConfig
from hermes.backtester import Backtester


def _flat_ohlcv(rows: int = 50, price: float = 100.0) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts": range(rows),
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": 1000.0,
        }
    )


def _cfg(fee: float, slip: float) -> BotConfig:
    return BotConfig(
        {
            "hermes": {
                "backtest_mode": "ta_only",
                "initial_capital_usdt": 10_000,
                "usdt_per_trade": 1000,
            },
            "stop_loss_pct": 50.0,
            "costs": {
                "fee_source": "config",
                "gate": {
                    "spot": {
                        "fee_maker_pct": fee,
                        "fee_taker_pct": fee,
                        "slippage_pct": slip,
                        "fee_side_buy": "base",
                        "fee_side_sell": "quote",
                    }
                },
            },
        }
    )


def _force_round_trips(bt: Backtester) -> None:
    def analyze(_coin, market):
        if not market.has_position:
            return SimpleNamespace(action="BUY")
        return SimpleNamespace(action="SELL_FULL")

    bt.strategy.analyze = analyze  # type: ignore[method-assign]


def _naive_pnl(n_sells: int, price: float = 100.0, usdt: float = 1000.0) -> float:
    """Old backtester math with slip 0: pnl = (price − entry) · qty, qty = usdt / price."""
    qty = usdt / price
    return n_sells * (price - price) * qty


def test_fee_zero_slip_zero_matches_naive():
    bt = Backtester(_cfg(0.0, 0.0))
    _force_round_trips(bt)
    result = bt.run("TEST/USDT", "4h", {}, ohlcv_df=_flat_ohlcv())
    sells = [t for t in result.trades if t.get("type") == "SELL"]
    assert sells
    naive = _naive_pnl(len(sells))
    realized = sum(float(t.get("pnl") or 0) for t in sells)
    assert realized == pytest.approx(naive, abs=1e-9)
    assert result.metrics.realized_pnl == pytest.approx(naive, abs=1e-6)


def test_fee_0_2_pct_lowers_pnl_by_modelled_cost():
    zero = Backtester(_cfg(0.0, 0.0))
    taxed = Backtester(_cfg(0.2, 0.0))
    _force_round_trips(zero)
    _force_round_trips(taxed)
    df = _flat_ohlcv()
    r0 = zero.run("TEST/USDT", "4h", {}, ohlcv_df=df)
    r1 = taxed.run("TEST/USDT", "4h", {}, ohlcv_df=df)
    sells0 = [t for t in r0.trades if t.get("type") == "SELL"]
    sells1 = [t for t in r1.trades if t.get("type") == "SELL"]
    assert len(sells0) == len(sells1)
    assert sells1
    # Same-price round trip at 1000 USDT / 0.2 %: pnl == −3.996 per sell (design §6.4).
    per_rt = -3.996
    realized1 = sum(float(t.get("pnl") or 0) for t in sells1)
    realized0 = sum(float(t.get("pnl") or 0) for t in sells0)
    assert realized0 == pytest.approx(0.0, abs=1e-9)
    assert realized1 == pytest.approx(per_rt * len(sells1), abs=1e-6)
    assert realized1 < realized0
