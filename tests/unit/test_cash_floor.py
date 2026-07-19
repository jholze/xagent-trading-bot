"""P1: cash floor + max open block new buys when ledger is full."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from core.config import BotConfig
from core.models import TradeOrder
from data_manager import get_config
from risk.risk_manager import RiskManager


def _cfg(**risk_over) -> BotConfig:
    raw = dict(get_config())
    raw["trading_mode"] = "paper"
    raw["initial_capital_usdt"] = 100_000
    raw["max_open_positions"] = 24
    raw["max_usdt_per_trade"] = 2500
    # Deep-copy risk so we never mutate the shared get_config() cache
    base_risk = raw.get("risk") if isinstance(raw.get("risk"), dict) else {}
    risk = dict(base_risk)
    risk["cash_floor_pct"] = 18
    risk["cash_floor_basis"] = "initial"
    risk["dca_reserve_pct"] = 0
    risk["min_trade_usdt"] = 100.0
    risk["venue_quality"] = {"enabled": False}
    # Force static floor path — config.json may enable adaptive policy/capacity
    risk["position_capacity"] = {"enabled": False}
    risk["cash_policy"] = {"enabled": False}
    risk["slot_eviction"] = {"enabled": False}
    risk.update(risk_over)
    raw["risk"] = risk
    return BotConfig(raw)


class TestCashFloor:
    def test_floor_abs_from_initial(self):
        rm = RiskManager(_cfg())
        with patch.object(rm, "_initial_capital", return_value=100_000.0):
            assert rm._cash_floor_abs() == pytest.approx(18_000.0)

    def test_spendable_respects_floor(self):
        rm = RiskManager(_cfg())
        with patch.object(rm, "_available_usdt", return_value=20_000.0), patch.object(
            rm, "_cash_floor_abs", return_value=18_000.0
        ):
            sp = rm._spendable_usdt(100_000.0, is_dca=False)
        assert sp == pytest.approx(2_000.0)

    def test_spendable_zero_when_cash_below_floor(self):
        rm = RiskManager(_cfg())
        with patch.object(rm, "_available_usdt", return_value=500.0), patch.object(
            rm, "_cash_floor_abs", return_value=18_000.0
        ):
            assert rm._spendable_usdt(100_000.0, is_dca=False) == 0.0
            assert rm._spendable_usdt(100_000.0, is_dca=True) == 0.0

    def test_buy_blocked_cash_floor_code(self):
        rm = RiskManager(_cfg())
        order = TradeOrder(
            type="BUY", symbol="BTC/USDT", price=50_000, amount=0, usdt_amount=500, signal="BUY",
        )
        with patch.object(rm, "_available_usdt", return_value=0.0), patch.object(
            rm, "_portfolio_equity", return_value=100_000.0
        ), patch.object(rm, "_initial_capital", return_value=100_000.0), patch(
            "risk.risk_manager.count_open_full_slots", return_value=5
        ), patch(
            "risk.risk_manager.get_position",
            return_value={"amount": 0, "average_entry": 0},
        ), patch.object(rm, "_trade_cooldown_blocked", return_value=(False, "")), patch.object(
            rm, "_daily_buy_limit_blocked", return_value=None
        ), patch(
            "services.market_policy_fusion.get_global_market_bias",
            return_value={"block_buys": False, "size_mult": 1.0, "regime": "NEUTRAL"},
        ):
            decision = rm.evaluate(order, "4h", source="entry_sensor_15m")
        assert not decision.approved
        assert decision.code == "cash_floor"
        assert "floor" in (decision.message or "").lower()

    def test_new_open_blocked_when_over_cap(self):
        rm = RiskManager(_cfg())
        order = TradeOrder(
            type="BUY", symbol="NEW/USDT", price=1.0, amount=0, usdt_amount=500, signal="BUY",
        )
        with patch(
            "risk.risk_manager.count_open_full_slots", return_value=24
        ), patch(
            "risk.risk_manager.get_position",
            return_value={"amount": 0},
        ), patch.object(rm, "_trade_cooldown_blocked", return_value=(False, "")), patch.object(
            rm, "_available_usdt", return_value=50_000.0
        ), patch.object(rm, "_initial_capital", return_value=100_000.0), patch(
            "services.market_policy_fusion.get_global_market_bias",
            return_value={"block_buys": False, "size_mult": 1.0, "regime": "NEUTRAL"},
        ), patch.object(rm, "_open_book_memory_counts", return_value=(0, 0, 0)), patch.object(
            rm, "_process_uptime_sec", return_value=3600.0
        ):
            decision = rm.evaluate(order, "4h", source="auto")
        assert not decision.approved
        assert decision.code == "max_open_positions"

    def test_dca_on_existing_allowed_when_under_cap_if_cash_ok(self):
        """Existing position can DCA if cash above floor (has_position=True)."""
        rm = RiskManager(_cfg())
        order = TradeOrder(
            type="BUY",
            symbol="ETH/USDT",
            price=3000,
            amount=0,
            usdt_amount=400,
            signal="BUY_DCA",
            source="dca",
        )
        with patch(
            "risk.risk_manager.count_open_full_slots", return_value=30
        ), patch(
            "risk.risk_manager.get_position",
            return_value={"amount": 1.0, "average_entry": 2800},
        ), patch.object(rm, "_trade_cooldown_blocked", return_value=(False, "")), patch.object(
            rm, "_daily_buy_limit_blocked", return_value=None
        ), patch.object(rm, "_daily_dca_usdt_limit_blocked", return_value=None), patch.object(
            rm, "_available_usdt", return_value=25_000.0
        ), patch.object(rm, "_portfolio_equity", return_value=100_000.0), patch.object(
            rm, "_initial_capital", return_value=100_000.0
        ), patch.object(rm, "_is_dca_buy", return_value=True), patch.object(
            rm, "_equity_drawdown_pct", return_value=0.0
        ):
            decision = rm.evaluate(order, "4h", source="dca")
        # 30 open but has_position → not max_open; cash 25k - 18k floor = 7k spendable
        assert decision.approved
        assert decision.order.usdt_amount >= 100
