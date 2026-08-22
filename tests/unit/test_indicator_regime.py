"""Indicator-regime overlay: RSI punch-through without colliding with grind/TS/BB."""

from __future__ import annotations

import unittest

from core.actions import SELL_FULL, SELL_PARTIAL_20, SELL_PARTIAL_30
from core.models import MarketContext
from core.tenant_context import tenant_context
from strategies.indicator_regime import (
    apply_rsi_sell_overlay,
    normalize_rsi_candidates,
    overlay_active,
    relabel_technical_as_rsi_sell,
    resolve_indicator_mode,
    trail_allow_rsi,
)
from strategies.oracle_climax import (
    MODE_GRIND,
    MODE_HARVEST,
    MODE_IDLE,
    ClimaxDecision,
    filter_grind_candidates,
    reset_cycle,
)
from strategies.sell_rotation_policy import filter_trail_exclusive, rotation_config
from strategies.sell_sources import RSI_SELL_SOURCE


def _raw(**ir_over) -> dict:
    body = {
        "enabled": True,
        "trail_allow_rsi": True,
        "rsi_full_close": True,
        "tenants": ["default", "henry"],
    }
    body.update(ir_over)
    return {"sell_policy": {"indicator_regime": body, "oracle_climax": {"enabled": True}}}


def _trail_params(arm: float = 15.0) -> dict:
    return {
        "trailing_take_profit": {
            "enabled": True,
            "mode": "live",
            "arm_gain_pct": arm,
        }
    }


def _market(entry: float = 1.0, price: float = 1.60) -> MarketContext:
    return MarketContext(
        symbol="ZEC/USDT",
        timeframe="4h",
        current_price=price,
        has_position=True,
        average_entry=entry,
    )


class TestIndicatorRegimeConfig(unittest.TestCase):
    def test_disabled_is_off(self):
        raw = _raw(enabled=False)
        self.assertFalse(overlay_active(raw))
        self.assertFalse(trail_allow_rsi(raw))

    def test_tenant_gate(self):
        raw = _raw()
        with tenant_context("henry", scope="demo"):
            self.assertTrue(trail_allow_rsi(raw))
        with tenant_context("ctexp", scope="demo"):
            self.assertFalse(trail_allow_rsi(raw))

    def test_fusion_risk_off_aliases_harvest_not_stacked_with_grind(self):
        mode = resolve_indicator_mode(
            _raw(), climax_mode=MODE_GRIND, fusion_regime="RISK_OFF"
        )
        self.assertEqual(mode, MODE_HARVEST)

    def test_grind_raises_rsi_bars(self):
        params = {"rsi_sell_30": 68.0, "rsi_sell_20": 78.0, "rsi_sell_min_gain_pct": 15.0}
        out = apply_rsi_sell_overlay(params, _raw(), climax_mode=MODE_GRIND, fusion_regime="NEUTRAL")
        self.assertEqual(out["rsi_sell_30"], 76.0)
        self.assertEqual(out["rsi_sell_20"], 86.0)
        self.assertEqual(out["rsi_sell_min_gain_pct"], 18.0)

    def test_idle_leaves_disk(self):
        params = {"rsi_sell_30": 68.0, "rsi_sell_20": 78.0}
        out = apply_rsi_sell_overlay(params, _raw(), climax_mode=MODE_IDLE, fusion_regime="NEUTRAL")
        self.assertEqual(out["rsi_sell_30"], 68.0)
        self.assertEqual(out["rsi_sell_20"], 78.0)


class TestNoCollisionWithOldLayers(unittest.TestCase):
    def setUp(self):
        reset_cycle()

    def tearDown(self):
        reset_cycle()

    def test_relabel_rsi_partial_to_full_rsi_sell(self):
        action, src = relabel_technical_as_rsi_sell(
            action=SELL_PARTIAL_20,
            source="technical",
            technical_sources=["technical"],
            config_raw=_raw(),
        )
        self.assertEqual(src, RSI_SELL_SOURCE)
        self.assertEqual(action, SELL_FULL)

    def test_take_profit_stays_technical_blocked(self):
        action, src = relabel_technical_as_rsi_sell(
            action="SELL_TP",
            source="technical",
            technical_sources=["technical", "take_profit_60"],
            config_raw=_raw(),
        )
        self.assertEqual(src, "technical")
        self.assertEqual(action, "SELL_TP")

    def test_exclusive_allows_rsi_sell_blocks_bb_and_technical(self):
        cfg = rotation_config(_raw(), _trail_params())
        self.assertTrue(cfg.get("trail_allow_rsi"))
        market = _market()
        pos = {"recent_high": 1.70}
        cands = [
            (SELL_FULL, 5, RSI_SELL_SOURCE),
            (SELL_PARTIAL_30, 3, "bb_upper"),
            (SELL_PARTIAL_20, 3, "technical"),
            (SELL_FULL, 7, "trailing_take_profit"),
        ]
        kept, blocked = filter_trail_exclusive(
            cands, market, pos, cfg, strategy_params=_trail_params(),
        )
        srcs = {c[2] for c in kept}
        self.assertIn(RSI_SELL_SOURCE, srcs)
        self.assertIn("trailing_take_profit", srcs)
        self.assertNotIn("bb_upper", srcs)
        self.assertNotIn("technical", srcs)
        self.assertIn("bb_upper", blocked)
        self.assertIn("technical", blocked)

    def test_grind_still_blocks_bb_ttp_ts_keeps_rsi_sell(self):
        dec = ClimaxDecision(MODE_GRIND, ("grind",), {})
        cands = [
            (SELL_FULL, 5, RSI_SELL_SOURCE),
            (SELL_FULL, 3, "bb_upper"),
            (SELL_FULL, 7, "trailing_take_profit"),
            (SELL_FULL, 6, "trailing_stop"),
            (SELL_FULL, 7, "stop_loss"),
        ]
        kept, blocked = filter_grind_candidates(cands, dec)
        srcs = {c[2] for c in kept}
        self.assertEqual(srcs, {RSI_SELL_SOURCE, "stop_loss"})
        self.assertIn("trailing_stop", blocked)
        self.assertIn("bb_upper", blocked)

    def test_normalize_forces_full_on_rollover_too(self):
        raw = _raw()
        out = normalize_rsi_candidates(
            [(SELL_PARTIAL_20, 4, "exit_1h_rsi_rollover"), (SELL_PARTIAL_30, 3, "bb_upper")],
            raw,
        )
        self.assertEqual(out[0][0], SELL_FULL)
        self.assertEqual(out[0][2], "exit_1h_rsi_rollover")
        self.assertEqual(out[1][0], SELL_PARTIAL_30)

    def test_kill_exclusive_allow_restores_block(self):
        raw = _raw(trail_allow_rsi=False)
        cfg = rotation_config(raw, _trail_params())
        self.assertFalse(cfg.get("trail_allow_rsi"))
        market = _market()
        pos = {"recent_high": 1.70}
        kept, blocked = filter_trail_exclusive(
            [(SELL_FULL, 5, RSI_SELL_SOURCE)],
            market,
            pos,
            cfg,
            strategy_params=_trail_params(),
        )
        self.assertEqual(kept, [])
        self.assertEqual(blocked, [RSI_SELL_SOURCE])


if __name__ == "__main__":
    unittest.main()
