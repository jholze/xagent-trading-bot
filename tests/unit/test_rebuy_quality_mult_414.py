"""#414: rebuy quality_mult must use the confidence passed to RiskManager.evaluate().

TradeOrder has no `confidence` field, so the old `getattr(order, "confidence", None)`
in `_rebuy_after_sell_blocked` always resolved to "default" and quality_mult never
left 1.0. These tests drive the risk_manager path (not the helper in isolation) and
would fail under the old getattr lookup.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

import risk.rebuy_cooldown as rebuy_mod
from core.config import BotConfig
from core.models import TradeOrder
from data_manager import get_config
from risk.risk_manager import RiskManager

ROOT = Path(__file__).resolve().parents[2]

_REBUY_CFG = {
    "enabled": True,
    "log": False,
    "base_hours_by_regime": {"NEUTRAL": 2.0},
    "min_hours": 0.1,
    "max_hours": 8.0,
    "stop_loss_hours": 24.0,
    "block_rebuy_if_last_sell_was_stop": True,
    "quality_mult": {"high_conviction_entry": 0.7, "default": 1.0},
    "memory": {"enabled": False},
    "exit_source_mult": {"technical": 1.0, "default": 1.0},
}


def _rm() -> RiskManager:
    raw = dict(get_config())
    raw["trading_mode"] = "paper"
    risk = dict(raw.get("risk") or {})
    risk["rebuy_cooldown"] = dict(_REBUY_CFG)
    risk.pop("fail_closed_guards", None)
    raw["risk"] = risk
    return RiskManager(BotConfig(raw))


def _buy_order() -> TradeOrder:
    return TradeOrder(
        type="BUY",
        symbol="NEAR/USDT",
        price=1.0,
        amount=0,
        usdt_amount=200.0,
        signal="BUY",
        source="grid",
        timestamp="2026-01-01T00:00:00",
    )


def _pos_after_sell(hours_ago: float = 0.5) -> dict:
    last_ts = datetime.now() - timedelta(hours=hours_ago)
    return {
        "amount": 0.0,
        "last_trade_at": last_ts.isoformat(),
        "last_trade_type": "SELL",
        "last_sell_signal": "SELL_FULL",
    }


class _ResolveSpy:
    """Call-through recorder for resolve_rebuy_cooldown_hours (kwargs + real result)."""

    def __init__(self):
        self.calls: list[tuple[dict, object]] = []
        self._real = rebuy_mod.resolve_rebuy_cooldown_hours

    def __call__(self, **kwargs):
        result = self._real(**kwargs)
        self.calls.append((kwargs, result))
        return result

    @property
    def result(self):
        assert len(self.calls) == 1, "resolve_rebuy_cooldown_hours must run exactly once"
        return self.calls[0][1]

    @property
    def signal_quality(self) -> str:
        assert len(self.calls) == 1
        return self.calls[0][0]["signal_quality"]

    @property
    def quality_mult(self) -> float:
        return self.result.factors["quality_mult"]


@pytest.fixture
def rebuy_env():
    """Isolate the rebuy path: flat position after a SELL, NEUTRAL regime, no profile."""
    rm = _rm()
    spy = _ResolveSpy()
    with patch("risk.risk_manager.get_position", return_value=_pos_after_sell()), patch(
        "services.market_policy_fusion.get_global_market_bias",
        return_value={"regime": "NEUTRAL"},
    ), patch("intelligence.memory.cache.get_coin_profile", return_value=None), patch.object(
        rebuy_mod, "resolve_rebuy_cooldown_hours", new=spy
    ):
        yield rm, spy


def _quality_mult(spy: _ResolveSpy) -> float:
    return spy.quality_mult


def _signal_quality(spy: _ResolveSpy) -> str:
    return spy.signal_quality


class TestTradeCooldownThreadsConfidence:
    def test_high_confidence_resolves_0_7(self, rebuy_env):
        rm, spy = rebuy_env
        blocked, _reason = rm._trade_cooldown_blocked(
            _buy_order(), "4h", source="grid", confidence=80
        )
        assert _signal_quality(spy) == "high_conviction_entry"
        assert _quality_mult(spy) == pytest.approx(0.7)
        # 2.0h * 0.7 = 1.4h > 0.5h elapsed → still blocked, but shorter than default
        assert blocked is True
        assert spy.result.hours == pytest.approx(2.0 * 0.7)

    def test_threshold_75_is_high_conviction(self, rebuy_env):
        rm, spy = rebuy_env
        rm._trade_cooldown_blocked(_buy_order(), "4h", source="grid", confidence=75)
        assert _quality_mult(spy) == pytest.approx(0.7)

    @pytest.mark.parametrize("confidence", (None, 50))
    def test_default_confidence_resolves_1_0(self, rebuy_env, confidence):
        rm, spy = rebuy_env
        rm._trade_cooldown_blocked(
            _buy_order(), "4h", source="grid", confidence=confidence
        )
        assert _signal_quality(spy) == "default"
        assert _quality_mult(spy) == pytest.approx(1.0)
        assert spy.result.hours == pytest.approx(2.0)

    def test_omitted_confidence_defaults_to_1_0(self, rebuy_env):
        """Direct callers that predate #414 (no confidence kwarg) keep old behaviour."""
        rm, spy = rebuy_env
        rm._trade_cooldown_blocked(_buy_order(), "4h", source="grid")
        assert _quality_mult(spy) == pytest.approx(1.0)

    def test_high_confidence_clears_cooldown_default_does_not(self):
        """End-to-end effect: 1.5h after a sell, 80-confidence rebuy passes, 50 is blocked."""
        rm = _rm()
        real_resolve = rebuy_mod.resolve_rebuy_cooldown_hours
        with patch(
            "risk.risk_manager.get_position", return_value=_pos_after_sell(hours_ago=1.5)
        ), patch(
            "services.market_policy_fusion.get_global_market_bias",
            return_value={"regime": "NEUTRAL"},
        ), patch("intelligence.memory.cache.get_coin_profile", return_value=None), patch.object(
            rebuy_mod, "resolve_rebuy_cooldown_hours", wraps=real_resolve
        ):
            blocked_hi, _ = rm._trade_cooldown_blocked(
                _buy_order(), "4h", source="grid", confidence=80
            )
            blocked_lo, reason_lo = rm._trade_cooldown_blocked(
                _buy_order(), "4h", source="grid", confidence=50
            )
        assert blocked_hi is False
        assert blocked_lo is True
        assert "Rebuy cooldown" in reason_lo


class TestEvaluatePassesConfidence:
    """`evaluate(confidence=)` must hand the value to `_trade_cooldown_blocked`, not drop it."""

    def _evaluate(self, rm, order, **kw):
        with patch.object(
            rm, "_trade_cooldown_blocked", return_value=(True, "cooldown-sentinel")
        ) as cd, patch.object(rm, "_daily_loss_limit_blocked", return_value=None), patch(
            "risk.risk_manager.get_position", return_value={"amount": 0}
        ), patch(
            "risk.risk_manager.find_open_position_for_symbol", return_value=None
        ), patch("services.watchlist_quality.soak_log.log_risk_reject"):
            decision = rm.evaluate(order, "4h", **kw)
        return decision, cd

    def test_buy_path_forwards_confidence(self):
        rm = _rm()
        decision, cd = self._evaluate(rm, _buy_order(), source="grid", confidence=80)
        assert decision.approved is False
        assert decision.code == "trade_cooldown"
        cd.assert_called_once()
        assert cd.call_args.kwargs["confidence"] == 80

    def test_sell_path_forwards_confidence(self):
        rm = _rm()
        order = TradeOrder(
            type="SELL",
            symbol="NEAR/USDT",
            price=1.0,
            amount=1.0,
            signal="SELL_PARTIAL_20",
            source="auto",
            timestamp="2026-01-01T00:00:00",
        )
        decision, cd = self._evaluate(rm, order, source="auto", confidence=66)
        assert decision.code == "trade_cooldown"
        cd.assert_called_once()
        assert cd.call_args.kwargs["confidence"] == 66

    def test_buy_path_confidence_none_when_omitted(self):
        rm = _rm()
        _decision, cd = self._evaluate(rm, _buy_order(), source="grid")
        assert cd.call_args.kwargs["confidence"] is None


class TestConfigQualityMultKeys:
    def test_config_keys_are_reachable_buckets(self):
        """Every quality_mult key must be a value signal_quality_from_confidence can return."""
        with open(ROOT / "config.json", encoding="utf-8") as fh:
            cfg = json.load(fh)
        qm = cfg["risk"]["rebuy_cooldown"]["quality_mult"]
        reachable = {
            rebuy_mod.signal_quality_from_confidence(c) for c in (None, 0, 50, 74.9, 75, 100)
        }
        assert reachable == {"default", "high_conviction_entry"}
        assert set(qm) <= reachable, f"dead quality_mult keys: {set(qm) - reachable}"
        assert qm["high_conviction_entry"] == pytest.approx(0.7)
        assert qm["default"] == pytest.approx(1.0)
