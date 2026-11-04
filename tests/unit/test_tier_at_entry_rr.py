"""RED (review rr, d78bd49): cost tier at entry is not stored on the lot (tier-at-entry).

- strategies/positions.py:2045-2065 writes entry_snapshot on the first fill, but every
  production caller of update_position (services/portfolio_service.py:257, 362, 462, 595)
  passes none of strategy_tier / volatility_tier / source / fee, so the snapshot fields are null.
- core/costs.py:119-139 (_tier_for_symbol) + :377-383 pick the slippage tier per fill from the
  *current* classification; the tier/surcharge used for the entry fill is not kept on the lot.
- strategies/decision_engine.py:501-509 freezes lot.strategy_tier on the first cycle that sees
  the open lot, from the classification at that time (lock_strategy_tier, positions.py:1147-1157),
  not from the tier at entry.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from core.config import BotConfig
from core.costs import _tier_for_symbol
from data_manager import save_trade_history
from services.portfolio_service import PortfolioService
from strategies.positions import clear_positions_memory, get_position, lock_strategy_tier

SYM = "TIER/USDT"
TF = "4h"
_CLASSIFY = "intelligence.strategy_backtest.classify_coin"


def _cfg() -> BotConfig:
    return BotConfig({
        "max_usdt_per_trade": 1000,
        "costs": {
            "fee_source": "config",
            "gate": {"spot": {"fee_maker_pct": 0.2, "fee_taker_pct": 0.2, "slippage_pct": 0.15,
                              "fee_side_buy": "base", "fee_side_sell": "quote"}},
            "slippage_by_tier": {"volatile": 0.35, "mid": 0.20, "stable": 0.10},
        },
    })


@pytest.fixture
def svc(monkeypatch):
    monkeypatch.setattr("data_manager._reconcile_scoped_trade_history",
                        lambda history, scope, config=None, **kw: (history, False))
    monkeypatch.setattr("data_manager._ledger_reads_mongo_trade_history", lambda *a, **k: False)
    monkeypatch.setattr("data_manager._ledger_writes_mongo", lambda *a, **k: False)
    clear_positions_memory()
    save_trade_history({"virtual_balance": 5000.0, "realized_pnl": 0.0, "open_positions": 0, "trades": []})
    yield PortfolioService(_cfg())
    clear_positions_memory()


def _open_as_large_cap(svc):
    with patch(_CLASSIFY, return_value="large_cap"):
        entry_tier = _tier_for_symbol(SYM, _cfg().raw)
        res = svc.execute_buy(SYM, TF, price=1.0, usdt_amount=100.0, source="entry_sensor_15m")
    assert res.executed, res.message
    assert entry_tier == "stable"
    return entry_tier


def test_open_lot_stores_tier_at_entry(svc):
    entry_tier = _open_as_large_cap(svc)
    pos = get_position(SYM, TF)
    snap = pos.get("entry_snapshot") or {}
    assert snap, "no entry_snapshot on the lot"
    recorded = snap.get("cost_tier") or snap.get("volatility_tier") or snap.get("strategy_tier")
    nulls = sorted(k for k in ("strategy_tier", "volatility_tier", "source", "fee") if snap.get(k) is None)
    assert recorded == entry_tier, f"tier at entry not stored; entry_snapshot nulls={nulls}"


def test_later_reclassification_does_not_change_lot_tier(svc):
    entry_tier = _open_as_large_cap(svc)
    # Classification changes before the first decision cycle sees the lot.
    with patch(_CLASSIFY, return_value="meme"):
        current = _tier_for_symbol(SYM, _cfg().raw)
        assert current == "volatile"
        # Same call as strategies/decision_engine.py:509 on the first cycle with the open lot.
        lock_strategy_tier(SYM, TF, current)
    pos = get_position(SYM, TF)
    assert pos.get("strategy_tier") == entry_tier, (
        f"lot tier={pos.get('strategy_tier')!r}, tier at entry={entry_tier!r}")


def test_control_frozen_tier_is_not_overwritten(svc):
    _open_as_large_cap(svc)
    lock_strategy_tier(SYM, TF, "stable")
    lock_strategy_tier(SYM, TF, "volatile")
    assert get_position(SYM, TF).get("strategy_tier") == "stable"


def test_snapshot_keeps_cost_provenance(svc):
    entry_tier = _open_as_large_cap(svc)
    pos = get_position(SYM, TF)
    snap = pos["entry_snapshot"]
    assert pos["strategy_tier"] == entry_tier
    assert pos["strategy_tier_source"] == "entry"
    assert snap["strategy_tier"] == entry_tier
    assert snap["cost_tier"] == "stable"
    assert snap["coin_class"] == "large_cap"
    assert snap["slippage_pct"] == pytest.approx(0.10)
    assert snap["source"] == "entry_sensor_15m"
    assert snap["fee"] is not None and snap["fee"] >= 0
    assert snap["captured_at"]


def test_late_freeze_is_marked(svc):
    with patch(_CLASSIFY, return_value="mid_cap"):
        res = svc.execute_buy(SYM, TF, price=1.0, usdt_amount=100.0, source="gainer_relvol")
    assert res.executed, res.message
    pos = get_position(SYM, TF)
    # mid cap without an ATR: no guess at entry, the cycle freezes it later.
    assert not pos.get("strategy_tier")
    lock_strategy_tier(SYM, TF, "volatile")
    pos = get_position(SYM, TF)
    assert pos["strategy_tier"] == "volatile"
    assert pos["strategy_tier_source"] == "first_cycle"
