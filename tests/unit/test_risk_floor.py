"""Issue #302 items 1–3: exposure_multiplier, MTM peak drawdown, daily loss halt."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from unittest.mock import patch

import pytest

from core.config import BotConfig
from core.models import TradeOrder
from risk.risk_manager import RiskManager
from services.portfolio_nav_history import capture_current_nav_snapshot, persist_peak_equity


def _cfg(**risk_over) -> BotConfig:
    raw = {
        "max_usdt_per_trade": 1000,
        "max_position_percent": 80,
        "max_open_positions": 50,
        "trading_mode": "paper",
        "update_interval": 120,
        "paper": {"initial_capital_usdt": 100_000},
        "aggression": {"max_position_multiplier": 2.0},
        "risk": {
            "min_trade_usdt": 1,
            "min_size_multiplier": 0.25,
            "drawdown_throttle_pct": 10.0,
            "drawdown_size_multiplier": 0.5,
            "max_daily_loss_pct": 0,
            "cash_policy": {"enabled": False},
            "position_capacity": {"enabled": False},
            "moderate_deploy": {"enabled": False},
            "venue_quality": {"enabled": False},
        },
        "architecture": {},
    }
    raw["risk"].update(risk_over)
    cfg = BotConfig()
    cfg._raw = raw
    return cfg


def _buy(exposure=None, usdt=100.0) -> TradeOrder:
    return TradeOrder(
        type="BUY",
        symbol="AAA/USDT",
        price=1.0,
        amount=0,
        usdt_amount=usdt,
        signal="BUY",
        source="auto",
        exposure_multiplier=exposure,
    )


@contextmanager
def _size_env(
    rm: RiskManager,
    *,
    equity=100_000.0,
    history=None,
    patch_drawdown=None,
    extra=(),
):
    hist = history if history is not None else {
        "virtual_balance": 80_000.0,
        "peak_equity": float(equity) if equity is not None else 100_000.0,
        "trades": [],
    }
    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "services.market_policy_fusion.get_global_market_bias",
                return_value={"active": False, "apply_size_mult": False},
            )
        )
        stack.enter_context(patch("intelligence.memory.cache.get_size_bias", return_value=1.0))
        stack.enter_context(patch("intelligence.memory.cache.get_coin_profile", return_value=None))
        stack.enter_context(
            patch(
                "intelligence.macro.snapshot.get_risk_multipliers",
                return_value={
                    "calendar_mult": 1.0,
                    "session_mult": 1.0,
                    "pm_mult": 1.0,
                },
            )
        )
        stack.enter_context(patch("risk.moderate_deploy.size_boost_for_regime", return_value=1.0))
        if equity is not None:
            stack.enter_context(patch.object(rm, "_portfolio_equity", return_value=equity))
        stack.enter_context(patch.object(rm, "_available_usdt", return_value=80_000.0))
        stack.enter_context(patch.object(rm, "_spendable_usdt", return_value=80_000.0))
        stack.enter_context(patch.object(rm, "_initial_capital", return_value=100_000.0))
        if patch_drawdown is not None:
            stack.enter_context(patch.object(rm, "_equity_drawdown_pct", return_value=patch_drawdown))
        stack.enter_context(patch.object(rm, "_daily_buys_count", return_value=0))
        stack.enter_context(patch.object(rm.market, "fetch_indicators", return_value={"atr_pct": 3.0}))
        stack.enter_context(patch("risk.risk_manager.count_open_full_slots", return_value=0))
        stack.enter_context(patch("risk.risk_manager.count_open_positions", return_value=0))
        stack.enter_context(
            patch("risk.risk_manager.get_position", return_value={"amount": 0, "sold_percent": 0})
        )
        stack.enter_context(patch("risk.risk_manager.find_open_position_for_symbol", return_value=None))
        stack.enter_context(patch.object(rm, "_trade_cooldown_blocked", return_value=(False, "")))
        stack.enter_context(patch.object(rm, "_partial_sell_blocked", return_value=(False, "")))
        stack.enter_context(
            patch(
                "strategies.position_lock.auto_sell_blocked",
                return_value=(False, ""),
            )
        )
        stack.enter_context(patch("risk.risk_manager.load_trade_history", return_value=hist))
        stack.enter_context(patch("risk.risk_manager.load_live_trade_history", return_value=hist))
        for p in extra:
            stack.enter_context(p)
        yield


class TestExposureMultiplier:
    def test_exposure_0_4_scales_resolved_size(self):
        rm = RiskManager(_cfg())
        with _size_env(rm, patch_drawdown=0.0):
            base = rm.evaluate(
                _buy(None), "4h", trust_score=70, confidence=50, indicators={"atr_pct": 3.0}
            )
            scaled = rm.evaluate(
                _buy(0.4), "4h", trust_score=70, confidence=50, indicators={"atr_pct": 3.0}
            )
        assert base.approved, base.message
        assert scaled.approved, scaled.message
        assert scaled.order.usdt_amount == pytest.approx(base.order.usdt_amount * 0.4)

    def test_exposure_1_5_clamped_to_1(self):
        rm = RiskManager(_cfg())
        with _size_env(rm, patch_drawdown=0.0):
            base = rm.evaluate(
                _buy(None), "4h", trust_score=70, confidence=50, indicators={"atr_pct": 3.0}
            )
            boosted = rm.evaluate(
                _buy(1.5), "4h", trust_score=70, confidence=50, indicators={"atr_pct": 3.0}
            )
        assert base.approved and boosted.approved
        assert boosted.order.usdt_amount == pytest.approx(base.order.usdt_amount)

    def test_exposure_none_unchanged(self):
        rm = RiskManager(_cfg())
        with _size_env(rm, patch_drawdown=0.0):
            a = rm.evaluate(
                _buy(None), "4h", trust_score=70, confidence=50, indicators={"atr_pct": 3.0}
            )
            b = rm.evaluate(
                _buy(), "4h", trust_score=70, confidence=50, indicators={"atr_pct": 3.0}
            )
        assert a.approved and b.approved
        assert a.order.usdt_amount == pytest.approx(b.order.usdt_amount)


class TestPeakEquityDrawdown:
    def test_nav_snapshots_set_peak_and_throttle_arms(self):
        history = {"trades": [], "virtual_balance": 100_000.0}

        def _load():
            return history

        def _save(data):
            snapshot = dict(data)
            history.clear()
            history.update(snapshot)
            return True

        snaps = [
            {
                "total_value": 100_000.0,
                "balance": 100_000.0,
                "positions_market_value": 0.0,
                "initial_capital": 100_000.0,
            },
            {
                "total_value": 120_000.0,
                "balance": 120_000.0,
                "positions_market_value": 0.0,
                "initial_capital": 100_000.0,
            },
            {
                "total_value": 90_000.0,
                "balance": 90_000.0,
                "positions_market_value": 0.0,
                "initial_capital": 100_000.0,
            },
        ]

        with patch(
            "notifications.terminal_dashboard._portfolio_snapshot",
            side_effect=snaps,
        ), patch(
            "services.portfolio_nav_history._trade_history_io",
            return_value=(_load, _save),
        ):
            assert capture_current_nav_snapshot() is not None
            assert capture_current_nav_snapshot() is not None
            assert capture_current_nav_snapshot() is not None

        assert history["peak_equity"] == pytest.approx(120_000.0)
        assert history.get("peak_equity_at")

        rm = RiskManager(_cfg(drawdown_throttle_pct=10.0, drawdown_size_multiplier=0.5))
        with _size_env(
            rm,
            equity=90_000.0,
            history=history,
            extra=(patch.object(rm, "_initial_capital", return_value=100_000.0),),
        ):
            dd = rm._equity_drawdown_pct()
            decision = rm.evaluate(
                _buy(None), "4h", trust_score=70, confidence=50, indicators={"atr_pct": 3.0}
            )
            status = rm.status_summary()
        assert dd == pytest.approx(25.0)
        assert status["drawdown_throttle_active"] is True
        assert decision.approved, decision.message
        assert decision.size_multiplier == pytest.approx(0.5)

    def test_prices_unavailable_throttles_not_identity(self):
        rm = RiskManager(_cfg(drawdown_throttle_pct=10.0, drawdown_size_multiplier=0.5))
        hist = {"trades": [], "virtual_balance": 100_000.0, "peak_equity": 100_000.0}
        extra = (
            patch("services.portfolio_nav_history.latest_fresh_nav", return_value=None),
            patch.object(rm, "_mark_to_market_equity", return_value=None),
            patch.object(rm, "_trailing_24h_realized_pnl", return_value=0.0),
        )
        with _size_env(rm, equity=None, history=hist, extra=extra):
            assert rm._portfolio_equity() is None
            dd = rm._equity_drawdown_pct()
            decision = rm.evaluate(
                _buy(None), "4h", trust_score=70, confidence=50, indicators={"atr_pct": 3.0}
            )
        assert dd == pytest.approx(10.0)
        assert decision.approved, decision.message
        assert decision.size_multiplier == pytest.approx(0.5)
        assert decision.size_multiplier != pytest.approx(1.0)


class TestDailyLossLimit:
    def test_realized_loss_denies_buy_allows_sell_and_persists(self):
        history = {"trades": [], "virtual_balance": 100_000.0}
        rm = RiskManager(_cfg(max_daily_loss_pct=5.0))
        notified = []

        def _notify(*_a, **_k):
            notified.append(True)
            return True

        extra = (
            patch.object(rm, "_trailing_24h_realized_pnl", return_value=-6_000.0),
            patch("core.operator_notify.notify_operator", side_effect=_notify),
            patch.object(rm, "_risk_history_load", side_effect=lambda: history),
            patch.object(
                rm,
                "_risk_history_save",
                side_effect=lambda data: history.update(data),
            ),
        )
        with _size_env(rm, equity=100_000.0, history=history, patch_drawdown=0.0, extra=extra):
            buy = rm.evaluate(
                _buy(None), "4h", trust_score=70, confidence=50, indicators={"atr_pct": 3.0}
            )
            sell = rm.evaluate(
                TradeOrder(type="SELL", symbol="AAA/USDT", price=1.0, amount=50, signal="SELL"),
                "4h",
            )
        assert buy.approved is False
        assert buy.code == "daily_loss_limit"
        assert sell.approved is True
        assert history.get("risk_halt_until")
        assert notified == [True]

        # Simulated restart: new manager, persisted halt still denies.
        rm2 = RiskManager(_cfg(max_daily_loss_pct=5.0))
        extra2 = (
            patch.object(rm2, "_trailing_24h_realized_pnl", return_value=0.0),
            patch("core.operator_notify.notify_operator", side_effect=_notify),
            patch.object(rm2, "_risk_history_load", side_effect=lambda: history),
            patch.object(rm2, "_risk_history_save", side_effect=lambda data: history.update(data)),
        )
        with _size_env(rm2, equity=100_000.0, history=history, patch_drawdown=0.0, extra=extra2):
            again = rm2.evaluate(
                _buy(None), "4h", trust_score=70, confidence=50, indicators={"atr_pct": 3.0}
            )
        assert again.approved is False
        assert again.code == "daily_loss_limit"
        assert len(notified) == 1

    def test_limit_zero_never_denies(self):
        history = {"trades": [], "virtual_balance": 100_000.0}
        rm = RiskManager(_cfg(max_daily_loss_pct=0))
        extra = (
            patch.object(rm, "_trailing_24h_realized_pnl", return_value=-6_000.0),
            patch.object(rm, "_risk_history_load", return_value=history),
        )
        with _size_env(rm, equity=100_000.0, history=history, patch_drawdown=0.0, extra=extra), patch(
            "core.operator_notify.notify_operator"
        ) as notify:
            buy = rm.evaluate(
                _buy(None), "4h", trust_score=70, confidence=50, indicators={"atr_pct": 3.0}
            )
        assert buy.approved, buy.message
        assert buy.code != "daily_loss_limit"
        notify.assert_not_called()
        assert not history.get("risk_halt_until")


def test_persist_peak_equity_monotonic():
    history = {"peak_equity": 50_000.0, "trades": []}

    def _load():
        return history

    def _save(data):
        snapshot = dict(data)
        history.clear()
        history.update(snapshot)
        return True

    with patch(
        "services.portfolio_nav_history._trade_history_io",
        return_value=(_load, _save),
    ):
        persist_peak_equity(40_000.0)
        persist_peak_equity(80_000.0)
        persist_peak_equity(70_000.0)
    assert history["peak_equity"] == pytest.approx(80_000.0)
    assert history.get("peak_equity_at")
