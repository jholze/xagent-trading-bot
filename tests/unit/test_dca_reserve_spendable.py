"""Regression tests: dca_reserve_pct must not zero non-DCA buys when equity >> cash."""

from unittest.mock import patch

import pytest

from core.config import BotConfig
from core.models import SignalAnalysis, TradeOrder, TradeResult
from data_manager import get_config
from risk.risk_manager import RiskManager
from services.signal_orchestrator import SignalOrchestrator


def _railway_like_config() -> BotConfig:
    raw = dict(get_config())
    raw["trading_mode"] = "paper"
    raw.setdefault("live", {})["dry_run_enhanced"] = False
    raw["max_usdt_per_trade"] = 2500
    raw["max_position_percent"] = 30
    raw["max_open_positions"] = 40
    risk = raw.setdefault("risk", {})
    risk["dca_reserve_pct"] = 18
    risk["cash_floor_pct"] = 0  # isolate legacy reserve tests
    risk["min_trade_usdt"] = 100.0
    risk["position_capacity"] = {"enabled": False}
    risk["cash_policy"] = {"enabled": False}
    risk["slot_eviction"] = {"enabled": False}
    risk["venue_quality"] = {"enabled": False}
    uni = dict(raw.get("universe") or {})
    uni["split_enabled"] = False
    raw["universe"] = uni
    return BotConfig(raw)


class TestDcaReserveSpendable:
    """Cash reserve must scale with balance, not portfolio equity."""

    def test_equity_based_reserve_would_zero_railway_cash(self):
        """Documents the production bug: equity reserve exceeded available cash."""
        cash = 18_419.30
        # Live price refresh pushed equity above cash / 0.18 on Railway test.
        equity = 102_329.44
        reserve_pct = 18
        buggy_spendable = max(0.0, cash - equity * (reserve_pct / 100.0))
        fixed_spendable = cash * (1 - reserve_pct / 100.0)

        assert buggy_spendable < 1.0
        assert fixed_spendable > 15_000.0

    def test_spendable_uses_cash_not_equity(self):
        cfg = _railway_like_config()
        rm = RiskManager(cfg)
        cash = 18_419.30
        equity = 98_624.96

        with patch.object(rm, "_available_usdt", return_value=cash):
            spendable = rm._spendable_usdt(equity, is_dca=False)

        expected = cash * (1 - 0.18)
        assert spendable == pytest.approx(expected, rel=1e-4)
        assert spendable > 15_000

    def test_dca_bypasses_percent_reserve_not_cash_floor(self):
        """DCA ignores dca_reserve_pct but still respects absolute cash_floor."""
        cfg = _railway_like_config()
        cfg.raw["risk"]["cash_floor_pct"] = 18
        cfg.raw["initial_capital_usdt"] = 100_000
        rm = RiskManager(cfg)
        cash = 20_000.0

        with patch.object(rm, "_available_usdt", return_value=cash), patch.object(
            rm, "_initial_capital", return_value=100_000.0
        ):
            # floor $18k → free $2k; DCA does not apply extra 18% on remaining
            spendable = rm._spendable_usdt(100_000.0, is_dca=True)
            non_dca = rm._spendable_usdt(100_000.0, is_dca=False)

        assert spendable == pytest.approx(2_000.0)
        # non-DCA also only floor when dca_reserve is 18% of remaining after floor
        # free after floor 2000, then *0.82 if reserve on remaining
        assert non_dca == pytest.approx(2_000.0 * 0.82)

    def test_auto_buy_approved_with_deployed_portfolio(self):
        """Reproduces Railway test: high equity, moderate cash, many positions."""
        cfg = _railway_like_config()
        rm = RiskManager(cfg)
        order = TradeOrder(
            type="BUY",
            symbol="BEAT/USDT",
            price=0.05,
            amount=0,
            usdt_amount=0,
            signal="BUY",
            source="auto",
        )
        indicators = {"atr_pct": 3.0, "rsi": 45.0}

        with patch.object(rm, "_portfolio_equity", return_value=98_624.96), \
             patch.object(rm, "_available_usdt", return_value=18_419.30), \
             patch.object(rm, "_trade_cooldown_blocked", return_value=(False, "")), \
             patch.object(rm, "_daily_buy_limit_blocked", return_value=None), \
             patch.object(rm.market, "fetch_indicators", return_value=indicators), \
             patch("risk.risk_manager.get_position", return_value={"amount": 0}), \
             patch("risk.risk_manager.count_open_full_slots", return_value=30):
            decision = rm.evaluate(
                order,
                "1h",
                source="auto",
                confidence=70,
                indicators=indicators,
            )

        assert decision.approved, f"expected approved, got {decision.code}: {decision.message}"
        assert decision.order.usdt_amount >= 100.0

    def test_grid_buy_approved_with_deployed_portfolio(self):
        cfg = _railway_like_config()
        rm = RiskManager(cfg)
        order = TradeOrder(
            type="BUY",
            symbol="BEAT/USDT",
            price=0.05,
            amount=0,
            usdt_amount=0,
            signal="BUY",
            source="grid",
        )
        indicators = {"atr_pct": 3.0, "rsi": 45.0}

        with patch.object(rm, "_portfolio_equity", return_value=102_329.44), \
             patch.object(rm, "_available_usdt", return_value=18_419.30), \
             patch.object(rm, "_trade_cooldown_blocked", return_value=(False, "")), \
             patch.object(rm, "_daily_buy_limit_blocked", return_value=None), \
             patch.object(rm.market, "fetch_indicators", return_value=indicators), \
             patch("risk.risk_manager.get_position", return_value={"amount": 0}), \
             patch("risk.risk_manager.count_open_full_slots", return_value=30):
            decision = rm.evaluate(
                order,
                "1h",
                source="grid",
                confidence=70,
                indicators=indicators,
            )

        assert decision.approved, f"expected approved, got {decision.code}: {decision.message}"
        assert decision.order.usdt_amount >= 100.0
        assert decision.code != "size_too_small"

    def test_entry_sensor_buy_not_zero_sized(self):
        cfg = _railway_like_config()
        rm = RiskManager(cfg)
        order = TradeOrder(
            type="BUY",
            symbol="ICNT/USDT",
            price=0.1,
            amount=0,
            usdt_amount=0,
            signal="BUY",
            source="entry_sensor_15m",
        )
        indicators = {"atr_pct": 3.0, "rsi": 45.0}

        with patch.object(rm, "_portfolio_equity", return_value=98_624.96), \
             patch.object(rm, "_available_usdt", return_value=18_419.30), \
             patch.object(rm, "_trade_cooldown_blocked", return_value=(False, "")), \
             patch.object(rm, "_daily_buy_limit_blocked", return_value=None), \
             patch.object(rm.market, "fetch_indicators", return_value=indicators), \
             patch("risk.risk_manager.get_position", return_value={"amount": 0}), \
             patch("risk.risk_manager.count_open_full_slots", return_value=30):
            decision = rm.evaluate(
                order,
                "1h",
                source="entry_sensor_15m",
                confidence=70,
                indicators=indicators,
            )

        assert decision.approved
        assert decision.order.usdt_amount >= 100.0


def test_grid_source_when_strategy_profile_grid():
    orch = SignalOrchestrator()
    coin = {"symbol": "BEAT/USDT", "timeframe": "1h"}
    analysis = SignalAnalysis(
        action="BUY",
        symbol="BEAT/USDT",
        timeframe="1h",
        rsi=40.0,
        lower_bb=1.0,
        vol_multiplier=1.5,
        ampel_emoji="🔵",
        ampel_text="Grid buy level",
        sources=["grid"],
        strategy_profile="grid",
        rationale="Grid buy level @ 0.05",
    )

    with patch("services.signal_orchestrator.resolve_coin_config", return_value={"symbol": "BEAT/USDT", "timeframe": "1h", "strategy_params": {}}), \
         patch.object(orch.trading, "execute_order", return_value=TradeResult(False, "BUY", "BEAT/USDT")) as mock_exec:
        orch.execute_if_needed(analysis, coin, 0.05)
        mock_exec.assert_called_once()
        assert mock_exec.call_args.kwargs["source"] == "grid"
        assert mock_exec.call_args.args[0].source == "grid"