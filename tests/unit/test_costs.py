"""CostModel acceptance tests — exact values from docs/umbau/costmodel-design.md §6."""

from __future__ import annotations

import pytest

from core.costs import COST_MODEL_VERSION, CostModel, CostParams


def _cm(*, fee: float = 0.2, slip: float = 0.0, **kwargs) -> CostModel:
    return CostModel(
        CostParams(
            fee_maker_pct=fee,
            fee_taker_pct=fee,
            slippage_pct=slip,
            **kwargs,
        )
    )


def test_buy_fee_in_base():
    fill = _cm().simulate_buy(100, usdt=1000)
    assert fill.fill_price == pytest.approx(100)
    assert fill.qty_gross == pytest.approx(10.0)
    assert fill.fee_base == pytest.approx(0.02)
    assert fill.qty_net == pytest.approx(9.98)
    assert fill.quote_net == pytest.approx(1000.0)
    assert fill.fee_usdt == pytest.approx(2.0)
    assert fill.fee_quote == pytest.approx(0.0)
    assert fill.cost_model_version == COST_MODEL_VERSION


def test_buy_by_qty():
    fill = _cm().simulate_buy(100, qty=9.98)
    assert fill.qty_gross == pytest.approx(10.0)
    assert fill.quote_net == pytest.approx(1000.0)
    assert fill.qty_net == pytest.approx(9.98)


def test_sell_fee_in_quote():
    fill = _cm().simulate_sell(100, 9.98)
    assert fill.quote_gross == pytest.approx(998.0)
    assert fill.fee_quote == pytest.approx(1.996)
    assert fill.quote_net == pytest.approx(996.004)
    assert fill.fee_base == pytest.approx(0)


def test_round_trip_pnl_equals_cash_delta():
    cm = _cm()
    cash_before = 1000.0
    buy = cm.simulate_buy(100, usdt=1000)
    avg_entry_net = buy.quote_net / buy.qty_net
    cash = cash_before - buy.quote_net
    sell = cm.simulate_sell(100, buy.qty_net)
    cash_after = cash + sell.quote_net
    pnl = CostModel.realized_pnl(
        qty_sold=buy.qty_net, avg_entry_net=avg_entry_net, sell=sell,
    )
    assert pnl == pytest.approx(sell.quote_net - 1000)
    assert pnl == pytest.approx(-3.996)
    assert abs(pnl - (cash_after - cash_before)) < 1e-9


def test_slippage_adverse():
    cm = _cm(slip=0.15)
    buy = cm.simulate_buy(100, usdt=1000)
    sell = cm.simulate_sell(100, 9.98)
    assert buy.fill_price == pytest.approx(100.15)
    assert sell.fill_price == pytest.approx(99.85)
    assert buy.slippage_usdt == pytest.approx(abs(buy.fill_price - 100) * buy.qty_gross)
    assert sell.slippage_usdt == pytest.approx(abs(sell.fill_price - 100) * sell.qty_gross)


def test_maker_taker():
    cm = CostModel(CostParams(fee_maker_pct=0.1, fee_taker_pct=0.2, slippage_pct=0.15))
    assert cm.fee_pct("limit") == pytest.approx(0.1)
    assert cm.fee_pct("market") == pytest.approx(0.2)
    assert cm.round_trip_pct("market") == pytest.approx(2 * 0.2 + 2 * 0.15)
    assert cm.round_trip_pct("limit") == pytest.approx(2 * 0.1 + 2 * 0.15)


def test_from_config_missing_block_uses_vip0_defaults_and_warns(monkeypatch):
    import core.costs as costs_mod

    costs_mod._MISSING_BLOCK_WARNED.clear()
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "core.costs.log",
        lambda msg, level="INFO": calls.append((str(msg), str(level))),
    )
    cm = CostModel.from_config({})
    assert cm.fee_pct("market") == pytest.approx(0.2)
    assert cm.fee_pct("limit") == pytest.approx(0.2)
    assert cm.params.slippage_pct == pytest.approx(0.15)
    assert cm.params.fee_side_buy == "base"
    assert cm.params.fee_side_sell == "quote"
    assert any(level == "WARNING" for _, level in calls)


def test_from_config_reads_tier_slippage():
    cfg = {
        "costs": {
            "fee_source": "config",
            "gate": {
                "spot": {
                    "fee_maker_pct": 0.2,
                    "fee_taker_pct": 0.2,
                    "slippage_pct": 0.15,
                    "fee_side_buy": "base",
                    "fee_side_sell": "quote",
                }
            },
            "slippage_by_tier": {"volatile": 0.35, "mid": 0.20, "stable": 0.10},
        }
    }
    cm = CostModel.from_config(cfg, symbol="PEPE/USDT")
    assert cm.params.slippage_pct == pytest.approx(0.35)


def test_fill_from_exchange_base_fee():
    cm = _cm()
    raw = {"average": 100, "filled": 10, "fee": {"cost": 0.02, "currency": "BTC"}}
    fill = cm.fill_from_exchange(
        raw, side="buy", base="BTC", quote="USDT", request_price=100,
    )
    assert fill.fee_base == pytest.approx(0.02)
    assert fill.qty_net == pytest.approx(9.98)
    assert fill.fee_quote == pytest.approx(0)


def test_fill_from_exchange_quote_fee():
    cm = _cm()
    raw = {"average": 100, "filled": 9.98, "fee": {"cost": 1.996, "currency": "USDT"}}
    fill = cm.fill_from_exchange(
        raw, side="sell", base="BTC", quote="USDT", request_price=100,
    )
    assert fill.fee_quote == pytest.approx(1.996)
    assert fill.fee_base == pytest.approx(0)
    assert fill.quote_net == pytest.approx(996.004)


def test_fill_from_exchange_unknown_currency_raises():
    cm = _cm()
    raw = {"average": 100, "filled": 10, "fee": {"cost": 0.02, "currency": "GT"}}
    with pytest.raises(ValueError, match="Unknown fee currency"):
        cm.fill_from_exchange(
            raw, side="buy", base="BTC", quote="USDT", request_price=100,
        )


def test_fee_side_quote_on_buy():
    cm = _cm(fee_side_buy="quote")
    fill = cm.simulate_buy(100, usdt=1000)
    assert fill.qty_net == pytest.approx(fill.qty_gross)
    assert fill.quote_net == pytest.approx(1000 + fill.fee_quote)
    assert fill.fee_quote == pytest.approx(2.0)
    assert fill.fee_base == pytest.approx(0)
