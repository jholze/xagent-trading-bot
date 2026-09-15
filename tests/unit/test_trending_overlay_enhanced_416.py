"""#416: trending cap / size haircut must read the overlay enhanced dry-run writes.

Enhanced dry-run (``trading_mode: live``, ``live.dry_run: true``,
``live.dry_run_enhanced: true`` — the staging config) persists its trending coins
to ``watchlist.dry_run_overlay.json``; ``watchlist.cmc_trending_overlay.json`` is
never populated there. Two risk readers still read the CMC overlay
unconditionally, so shadow P&L saw neither ``max_open_from_trending`` nor the
``trending_trade_size_pct`` haircut.

Every test here patches the overlay loaders (nothing under ``data/`` is read or
written) and would have failed under CMC-only reads: the enhanced cases put the
symbols *only* into the dry-run overlay and keep the CMC overlay empty.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from core.config import BotConfig
from core.models import TradeOrder
from risk.risk_manager import RiskManager

_EMPTY_OVERLAY = {"refreshed_at": "", "source": "", "coins": [], "added": [], "removed": []}

TRENDING = [f"TR{i}/USDT" for i in range(11)]  # 11 open lots
NEW_TRENDING = "TR99/USDT"  # the 12th trending BUY
NOT_TRENDING = "BTC/USDT"


def _overlay(symbols) -> dict:
    return {
        "refreshed_at": "2026-09-15T00:00:00",
        "source": "test",
        "coins": [{"symbol": s, "name": s.split("/")[0]} for s in symbols],
    }


def _raw(*, enhanced: bool, cap: int = 11, size_pct: int = 50) -> dict:
    """Staging-shaped config: live + dry_run, enhanced flag toggled per test."""
    return {
        "trading_mode": "live",
        "max_usdt_per_trade": 500,
        "max_position_percent": 80,
        "max_open_positions": 50,
        "live": {
            "dry_run": True,
            "dry_run_enhanced": enhanced,
            "simulated_balance_usdt": 100_000,
        },
        "cmc": {
            "trending_watchlist": {
                "enabled": True,
                "live_enabled": True,
                "max_open_from_trending": cap,
            },
            "cmc_trending_fusion": {"trending_trade_size_pct": size_pct},
        },
        "risk": {"min_trade_usdt": 5},
        "architecture": {},
    }


def _rm(*, enhanced: bool, **kw) -> RiskManager:
    return RiskManager(BotConfig(_raw(enhanced=enhanced, **kw)))


def _buy(symbol: str, usdt: float = 200.0) -> TradeOrder:
    return TradeOrder(
        type="BUY",
        symbol=symbol,
        price=1.0,
        amount=0,
        usdt_amount=usdt,
        signal="BUY",
        source="cmc",
        timestamp="2026-09-15T00:00:00",
    )


def _lots(symbols) -> list[dict]:
    return [{"symbol": s, "amount": 10.0, "timeframe": "1h"} for s in symbols]


def _overlays(*, dry_run, cmc):
    """Patch both loaders where the risk helper imports them from (data_manager)."""
    return (
        patch("data_manager.load_dry_run_overlay", return_value=dry_run),
        patch("data_manager.load_cmc_trending_overlay", return_value=cmc),
    )


class TestModePredicate:
    """The staging-shaped raw config resolves the way the fix relies on."""

    def test_enhanced_config_is_dry_run_enhanced(self):
        from data_manager import is_dry_run_enhanced

        assert is_dry_run_enhanced(_raw(enhanced=True)) is True
        assert is_dry_run_enhanced(_raw(enhanced=False)) is False

    def test_real_live_is_never_enhanced(self):
        """Live-capital fidelity: real-money execution → CMC overlay regardless of the flag."""
        from data_manager import is_dry_run_enhanced

        raw = _raw(enhanced=True)
        raw["live"]["dry_run"] = False
        with patch("data_manager.places_real_orders", return_value=True):
            assert is_dry_run_enhanced(raw) is False
        rm = RiskManager(BotConfig(raw))
        p_dry, p_cmc = _overlays(dry_run=_overlay(TRENDING), cmc=_overlay([NEW_TRENDING]))
        with p_dry, p_cmc, patch("data_manager.places_real_orders", return_value=True):
            assert rm._trending_overlay_symbols() == {NEW_TRENDING}


class TestTrendingOverlaySymbols:
    def test_enhanced_reads_dry_run_overlay_not_cmc(self):
        rm = _rm(enhanced=True)
        p_dry, p_cmc = _overlays(dry_run=_overlay(TRENDING), cmc=_EMPTY_OVERLAY)
        with p_dry as dry, p_cmc as cmc:
            syms = rm._trending_overlay_symbols()
        assert syms == set(TRENDING)
        dry.assert_called_once()
        cmc.assert_not_called()

    def test_not_enhanced_reads_cmc_overlay_not_dry_run(self):
        rm = _rm(enhanced=False)
        p_dry, p_cmc = _overlays(dry_run=_overlay(["WRONG/USDT"]), cmc=_overlay(TRENDING))
        with p_dry as dry, p_cmc as cmc:
            syms = rm._trending_overlay_symbols()
        assert syms == set(TRENDING)
        cmc.assert_called_once()
        dry.assert_not_called()

    @pytest.mark.parametrize("enhanced", (True, False))
    def test_live_enabled_false_is_the_off_switch(self, enhanced):
        raw = _raw(enhanced=enhanced)
        raw["cmc"]["trending_watchlist"]["live_enabled"] = False
        rm = RiskManager(BotConfig(raw))
        p_dry, p_cmc = _overlays(dry_run=_overlay(TRENDING), cmc=_overlay(TRENDING))
        with p_dry as dry, p_cmc as cmc:
            assert rm._trending_overlay_symbols() == set()
        dry.assert_not_called()
        cmc.assert_not_called()


class TestTrendingPositionCap:
    """11 open trending lots, cap 11 → the 12th trending BUY is rejected."""

    def _cap(self, rm, symbol=NEW_TRENDING):
        return rm._trending_position_cap_blocked(_buy(symbol), "1h")

    def test_enhanced_cap_hits_from_dry_run_overlay_with_empty_cmc(self):
        rm = _rm(enhanced=True, cap=11)
        p_dry, p_cmc = _overlays(dry_run=_overlay(TRENDING + [NEW_TRENDING]), cmc=_EMPTY_OVERLAY)
        with p_dry, p_cmc, patch(
            "strategies.positions.list_active_positions", return_value=_lots(TRENDING)
        ):
            blocked, reason = self._cap(rm)
        assert blocked is True
        assert "Trending position cap: 11/11" in reason

    def test_enhanced_below_cap_passes(self):
        rm = _rm(enhanced=True, cap=11)
        p_dry, p_cmc = _overlays(dry_run=_overlay(TRENDING), cmc=_EMPTY_OVERLAY)
        with p_dry, p_cmc, patch(
            "strategies.positions.list_active_positions", return_value=_lots(TRENDING[:10])
        ):
            blocked, _ = self._cap(rm)
        assert blocked is False

    def test_enhanced_counts_only_lots_on_the_overlay(self):
        """Non-trending lots never count against the trending cap."""
        rm = _rm(enhanced=True, cap=11)
        p_dry, p_cmc = _overlays(dry_run=_overlay(TRENDING[:5]), cmc=_EMPTY_OVERLAY)
        with p_dry, p_cmc, patch(
            "strategies.positions.list_active_positions",
            return_value=_lots(TRENDING + [NOT_TRENDING]),
        ):
            blocked, _ = self._cap(rm)
        assert blocked is False  # only 5 of the 12 lots are on the overlay

    def test_not_enhanced_cap_still_uses_cmc_overlay(self):
        rm = _rm(enhanced=False, cap=11)
        p_dry, p_cmc = _overlays(dry_run=_EMPTY_OVERLAY, cmc=_overlay(TRENDING))
        with p_dry, p_cmc, patch(
            "strategies.positions.list_active_positions", return_value=_lots(TRENDING)
        ):
            blocked, reason = self._cap(rm)
        assert blocked is True
        assert "11/11" in reason

    def test_not_enhanced_ignores_dry_run_overlay(self):
        """Live path unchanged: dry-run overlay contents must not drive the cap."""
        rm = _rm(enhanced=False, cap=11)
        p_dry, p_cmc = _overlays(dry_run=_overlay(TRENDING), cmc=_EMPTY_OVERLAY)
        with p_dry, p_cmc, patch(
            "strategies.positions.list_active_positions", return_value=_lots(TRENDING)
        ):
            blocked, _ = self._cap(rm)
        assert blocked is False

    def test_enhanced_cap_rejects_via_evaluate(self):
        """End-to-end: RiskManager.evaluate() rejects the 12th CMC BUY (code trade_cooldown)."""
        rm = _rm(enhanced=True, cap=11)
        p_dry, p_cmc = _overlays(dry_run=_overlay(TRENDING + [NEW_TRENDING]), cmc=_EMPTY_OVERLAY)
        pos = {"amount": 0, "last_trade_at": "2026-01-01T00:00:00", "last_trade_type": "BUY"}
        with p_dry, p_cmc, patch(
            "strategies.positions.list_active_positions", return_value=_lots(TRENDING)
        ), patch("risk.risk_manager.get_position", return_value=pos), patch(
            "risk.risk_manager.find_open_position_for_symbol", return_value=None
        ), patch.object(rm, "_daily_loss_limit_blocked", return_value=None), patch(
            "services.watchlist_quality.soak_log.log_risk_reject"
        ):
            decision = rm.evaluate(_buy(NEW_TRENDING), "1h", source="cmc")
        assert decision.approved is False
        assert decision.code == "trade_cooldown"
        assert "Trending position cap: 11/11" in decision.message


class TestTrendingSizeHaircut:
    """CMC-source BUY of an overlay symbol is sized by trending_trade_size_pct."""

    def _evaluate(self, rm: RiskManager, order: TradeOrder):
        # _dynamic_size returns the base it was handed, so decision.order.usdt_amount
        # exposes the haircut (or its absence) directly.
        def passthrough(base_usdt, *a, **k):
            return float(base_usdt), {"total_multiplier": 1.0}

        with patch.object(rm, "_dynamic_size", side_effect=passthrough), patch.object(
            rm, "_portfolio_equity", return_value=100_000.0
        ), patch.object(rm, "_available_usdt", return_value=80_000.0), patch.object(
            rm, "_spendable_usdt", return_value=80_000.0
        ), patch.object(rm, "_daily_buys_count", return_value=0), patch.object(
            rm, "_daily_loss_limit_blocked", return_value=None
        ), patch.object(
            rm.market, "fetch_indicators", return_value={"atr_pct": 3.0}
        ), patch("risk.risk_manager.count_open_full_slots", return_value=0), patch(
            "risk.risk_manager.get_position", return_value={"amount": 0, "sold_percent": 0}
        ), patch(
            "risk.risk_manager.find_open_position_for_symbol", return_value=None
        ), patch(
            "risk.risk_manager.load_live_trade_history",
            return_value={"virtual_balance": 80_000.0},
        ), patch(
            "risk.risk_manager.load_trade_history",
            return_value={"virtual_balance": 80_000.0},
        ), patch("strategies.positions.list_active_positions", return_value=[]), patch(
            "services.watchlist_quality.soak_log.log_risk_reject"
        ):
            return rm.evaluate(order, "1h", source="cmc")

    def test_enhanced_haircut_from_dry_run_overlay_with_empty_cmc(self):
        rm = _rm(enhanced=True, size_pct=50)
        p_dry, p_cmc = _overlays(dry_run=_overlay([NEW_TRENDING]), cmc=_EMPTY_OVERLAY)
        with p_dry, p_cmc:
            decision = self._evaluate(rm, _buy(NEW_TRENDING, 200.0))
        assert decision.approved is True, decision.message
        assert decision.order.usdt_amount == pytest.approx(100.0)

    def test_enhanced_non_trending_symbol_keeps_full_size(self):
        rm = _rm(enhanced=True, size_pct=50)
        p_dry, p_cmc = _overlays(dry_run=_overlay([NEW_TRENDING]), cmc=_EMPTY_OVERLAY)
        with p_dry, p_cmc:
            decision = self._evaluate(rm, _buy(NOT_TRENDING, 200.0))
        assert decision.approved is True, decision.message
        assert decision.order.usdt_amount == pytest.approx(200.0)

    def test_not_enhanced_haircut_still_from_cmc_overlay(self):
        rm = _rm(enhanced=False, size_pct=50)
        p_dry, p_cmc = _overlays(dry_run=_EMPTY_OVERLAY, cmc=_overlay([NEW_TRENDING]))
        with p_dry, p_cmc:
            decision = self._evaluate(rm, _buy(NEW_TRENDING, 200.0))
        assert decision.approved is True, decision.message
        assert decision.order.usdt_amount == pytest.approx(100.0)

    def test_not_enhanced_ignores_dry_run_overlay(self):
        """Live path unchanged: a symbol only on the dry-run overlay is not haircut."""
        rm = _rm(enhanced=False, size_pct=50)
        p_dry, p_cmc = _overlays(dry_run=_overlay([NEW_TRENDING]), cmc=_EMPTY_OVERLAY)
        with p_dry, p_cmc:
            decision = self._evaluate(rm, _buy(NEW_TRENDING, 200.0))
        assert decision.approved is True, decision.message
        assert decision.order.usdt_amount == pytest.approx(200.0)
