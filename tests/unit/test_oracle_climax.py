"""Two-stage Oracle climax overlay — tape fixtures from 18–20 Aug 2026."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.actions import HOLD, SELL_FULL
from core.models import MarketContext, SignalAnalysis
from strategies.decision_engine import DecisionEngine
from strategies.oracle_climax import (
    GRIND_BLOCK_SOURCES,
    HARVEST_SOURCE,
    MODE_GRIND,
    MODE_HARVEST,
    MODE_IDLE,
    MODE_TIGHTEN,
    ClimaxDecision,
    apply_ttp_climax_overlay,
    climax_ttp_adjust,
    evaluate_climax_mode,
    filter_grind_candidates,
    harvest_candidate,
    harvest_eligible,
    oracle_climax_config,
    position_blocked_from_harvest,
    reset_cycle,
)
from strategies.trailing_take_profit import evaluate_trailing_take_profit

_ROOT = Path(__file__).resolve().parents[2]


def _cfg(**overrides) -> dict:
    return {**oracle_climax_config({"sell_policy": {"oracle_climax": {"enabled": True}}}), **overrides}


def _snap(state: str = "RISK_ON", **feats) -> dict:
    return {"state": state, "regime": state, "features": feats}


def _armed_feats(**overrides) -> dict:
    base = dict(
        btc_ret_24h_pct=8.0,
        eth_ret_24h_pct=12.0,
        breadth_pct_green=0.82,
        btc_ret_4h_pct=2.1,
        btc_trend_4h=1.0,
        btc_ret_1h_pct=1.2,
    )
    base.update(overrides)
    return base


class TestOracleClimaxMode(unittest.TestCase):
    def setUp(self):
        reset_cycle()

    def tearDown(self):
        reset_cycle()

    def test_disabled_is_idle(self):
        dec = evaluate_climax_mode(
            oracle_snap=_snap(**_armed_feats()),
            fusion_regime="NEUTRAL",
            cfg=_cfg(enabled=False),
        )
        self.assertEqual(dec.mode, MODE_IDLE)
        self.assertIn("disabled", dec.reasons)

    def test_aug18_fusion_risk_off_never_sells(self):
        """18 Aug: Fusion RISK_OFF (breadth) while BTC still green — overlay idle."""
        snap = _snap(
            "RISK_OFF",
            btc_ret_24h_pct=2.0,
            eth_ret_24h_pct=1.0,
            breadth_pct_green=0.22,
            btc_ret_4h_pct=0.4,
            btc_trend_4h=1.0,
            btc_ret_1h_pct=0.2,
        )
        dec = evaluate_climax_mode(
            oracle_snap=snap, fusion_regime="RISK_OFF", cfg=_cfg()
        )
        self.assertEqual(dec.mode, MODE_IDLE)
        self.assertIn("fusion_risk_off", dec.reasons)

    def test_fusion_crash_idle_even_if_oracle_risk_on(self):
        dec = evaluate_climax_mode(
            oracle_snap=_snap(**_armed_feats(btc_ret_1h_pct=-0.5)),
            fusion_regime="CRASH",
            cfg=_cfg(),
        )
        self.assertEqual(dec.mode, MODE_IDLE)

    def test_oracle_not_risk_on_idle(self):
        dec = evaluate_climax_mode(
            oracle_snap=_snap("NEUTRAL", **_armed_feats()),
            fusion_regime="NEUTRAL",
            cfg=_cfg(),
        )
        self.assertEqual(dec.mode, MODE_IDLE)
        self.assertIn("oracle_not_risk_on", dec.reasons)

    def test_aug19_risk_on_not_armed_is_grind(self):
        """19 Aug: Oracle RISK_ON but extension/breadth not climax-hot → grind (hold BB/TTP)."""
        snap = _snap(
            btc_ret_24h_pct=3.5,
            eth_ret_24h_pct=4.0,
            breadth_pct_green=0.55,
            btc_ret_4h_pct=1.2,
            btc_trend_4h=1.0,
            btc_ret_1h_pct=0.8,
        )
        dec = evaluate_climax_mode(oracle_snap=snap, fusion_regime="NEUTRAL", cfg=_cfg())
        self.assertEqual(dec.mode, MODE_GRIND)
        self.assertIn("grind", dec.reasons)

    def test_aug20_armed_1h_green_is_grind(self):
        """20 Aug 09:33-class: climax numbers but 1h still ripping → keep holding runners."""
        dec = evaluate_climax_mode(
            oracle_snap=_snap(**_armed_feats(btc_ret_1h_pct=1.5)),
            fusion_regime="NEUTRAL",
            cfg=_cfg(),
        )
        self.assertEqual(dec.mode, MODE_GRIND)
        self.assertIn("1h_still_green", dec.reasons)

    def test_armed_stall_is_tighten(self):
        dec = evaluate_climax_mode(
            oracle_snap=_snap(**_armed_feats(btc_ret_1h_pct=0.1)),
            fusion_regime="NEUTRAL",
            cfg=_cfg(),
        )
        self.assertEqual(dec.mode, MODE_TIGHTEN)
        self.assertIn("stall_1h", dec.reasons)

    def test_armed_dump_1h_is_harvest(self):
        dec = evaluate_climax_mode(
            oracle_snap=_snap(**_armed_feats(btc_ret_1h_pct=-0.5)),
            fusion_regime="NEUTRAL",
            cfg=_cfg(),
        )
        self.assertEqual(dec.mode, MODE_HARVEST)
        self.assertIn("dump_1h", dec.reasons)

    def test_armed_deep_dump_is_harvest_not_grind(self):
        """Hysteresis can keep RISK_ON through a real dump — must not fall back to grind."""
        dec = evaluate_climax_mode(
            oracle_snap=_snap(**_armed_feats(btc_ret_1h_pct=-2.4)),
            fusion_regime="NEUTRAL",
            cfg=_cfg(),
        )
        self.assertEqual(dec.mode, MODE_HARVEST)
        self.assertIn("dump_1h", dec.reasons)

    def test_armed_15m_dump_harvests_even_if_1h_green(self):
        dec = evaluate_climax_mode(
            oracle_snap=_snap(**_armed_feats(btc_ret_1h_pct=1.2, btc_ret_15m_pct=-1.1)),
            fusion_regime="NEUTRAL",
            cfg=_cfg(),
        )
        self.assertEqual(dec.mode, MODE_HARVEST)
        self.assertIn("dump_15m", dec.reasons)

    def test_fusion_neutral_allowed(self):
        dec = evaluate_climax_mode(
            oracle_snap=_snap(**_armed_feats()),
            fusion_regime="NEUTRAL",
            cfg=_cfg(),
        )
        self.assertEqual(dec.mode, MODE_GRIND)


class TestGrindFilterAndHarvest(unittest.TestCase):
    def test_grind_blocks_bb_and_ttp_keeps_stop(self):
        dec = ClimaxDecision(MODE_GRIND, ("grind",), {})
        cands = [
            (SELL_FULL, 3, "bb_upper"),
            (SELL_FULL, 7, "trailing_take_profit"),
            (SELL_FULL, 7, "stop_loss"),
            (SELL_FULL, 6, "trailing_stop"),
        ]
        kept, blocked = filter_grind_candidates(cands, dec)
        srcs = {c[2] for c in kept}
        self.assertEqual(srcs, {"stop_loss", "trailing_stop"})
        self.assertIn("bb_upper", blocked)
        self.assertIn("trailing_take_profit", blocked)
        self.assertTrue(GRIND_BLOCK_SOURCES)

    def test_non_grind_does_not_filter(self):
        dec = ClimaxDecision(MODE_TIGHTEN, ("armed",), {})
        cands = [(SELL_FULL, 3, "bb_upper")]
        kept, blocked = filter_grind_candidates(cands, dec)
        self.assertEqual(kept, cands)
        self.assertEqual(blocked, [])

    def test_harvest_eligible_floor(self):
        dec = ClimaxDecision(MODE_HARVEST, ("dump_1h",), {})
        cfg = _cfg()
        self.assertTrue(harvest_eligible(gain_pct=12.0, decision=dec, cfg=cfg))
        self.assertFalse(harvest_eligible(gain_pct=11.9, decision=dec, cfg=cfg))
        self.assertFalse(harvest_eligible(gain_pct=20.0, decision=dec, cfg=cfg, locked=True))
        self.assertFalse(
            harvest_eligible(
                gain_pct=20.0,
                decision=ClimaxDecision(MODE_GRIND, ("grind",), {}),
                cfg=cfg,
            )
        )
        self.assertFalse(harvest_eligible(gain_pct=-1.0, decision=dec, cfg=cfg))

    def test_harvest_candidate_shape(self):
        dec = ClimaxDecision(MODE_HARVEST, ("dump_1h",), {})
        cand = harvest_candidate(gain_pct=14.0, decision=dec, cfg=_cfg())
        self.assertEqual(cand[0], SELL_FULL)
        self.assertEqual(cand[2], HARVEST_SOURCE)
        self.assertGreaterEqual(cand[1], 7)

    def test_lock_and_recovery_block_harvest(self):
        self.assertTrue(position_blocked_from_harvest(None))
        self.assertTrue(position_blocked_from_harvest({"recovery_hold": True}))
        self.assertTrue(position_blocked_from_harvest({"sniper_focus": True}))
        self.assertTrue(
            position_blocked_from_harvest(
                {"lock": {"enabled": True, "modes": ["no_auto_sell"]}}
            )
        )
        self.assertFalse(position_blocked_from_harvest({"amount": 10}))

    def test_ttp_overlay_tightens(self):
        dec = ClimaxDecision(MODE_TIGHTEN, ("stall_1h",), {})
        out = apply_ttp_climax_overlay(
            {"trail_pct": 6.0, "trail_pct_min": 3.0}, dec, _cfg()
        )
        self.assertEqual(out["trail_pct"], 1.5)
        self.assertEqual(out["trail_pct_min"], 1.5)

    def test_ttp_adjust_skips_grind(self):
        dec = ClimaxDecision(MODE_GRIND, ("1h_still_green",), {})
        raw = {"sell_policy": {"oracle_climax": {"enabled": True}}}
        _, skip = climax_ttp_adjust(
            {"trail_pct": 6.0}, config_raw=raw, climax_decision=dec
        )
        self.assertTrue(skip)

    def test_module_default_disabled_disk_enabled_for_staging(self):
        self.assertFalse(oracle_climax_config({})["enabled"])
        cfg_path = _ROOT / "config.json"
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        block = (raw.get("sell_policy") or {}).get("oracle_climax") or {}
        self.assertTrue(bool(block.get("enabled")))


class TestTrailingTakeProfitClimax(unittest.TestCase):
    def setUp(self):
        reset_cycle()

    def tearDown(self):
        reset_cycle()

    def _params(self):
        return {
            "exit_ladder": {"enabled": False},
            "trailing_take_profit": {
                "enabled": True,
                "mode": "live",
                "dynamic_trail": False,
                "trail_pct": 6.0,
                "arm_gain_pct": 10.0,
                "min_gain_pct": 8.0,
                "max_steps": 1,
                "cooldown_hours": 0,
            },
        }

    def _market(self, price=1.12):
        return MarketContext(
            symbol="DOGE/USDT",
            timeframe="1h",
            current_price=price,
            has_position=True,
            average_entry=1.0,
            atr_pct=8.0,
        )

    def test_grind_skips_ttp(self):
        pos = {"recent_high": 1.20}
        raw = {"sell_policy": {"oracle_climax": {"enabled": True}}}
        cand = evaluate_trailing_take_profit(
            self._market(1.12),
            pos,
            self._params(),
            climax_decision=ClimaxDecision(MODE_GRIND, ("grind",), {}),
            config_raw=raw,
        )
        self.assertIsNone(cand)

    def test_tighten_fires_on_smaller_drop(self):
        """Peak +20%, now +18.2% = 1.5% drop. Default 6% trail would hold; 1.5% overlay fires."""
        pos = {"recent_high": 1.20}
        raw = {"sell_policy": {"oracle_climax": {"enabled": True}}}
        params = self._params()
        hold = evaluate_trailing_take_profit(
            self._market(1.182),
            pos,
            params,
            climax_decision=ClimaxDecision(MODE_IDLE, ("disabled",), {}),
            config_raw={"sell_policy": {"oracle_climax": {"enabled": False}}},
        )
        self.assertIsNone(hold)
        fire = evaluate_trailing_take_profit(
            self._market(1.182),
            pos,
            params,
            climax_decision=ClimaxDecision(MODE_TIGHTEN, ("stall_1h",), {}),
            config_raw=raw,
        )
        self.assertIsNotNone(fire)
        self.assertEqual(fire.source, "trailing_take_profit")


class TestMergeSellClimax(unittest.TestCase):
    def setUp(self):
        reset_cycle()
        self.engine = DecisionEngine(market_service=MagicMock())
        self.engine.config = MagicMock()
        self.engine.config.raw = {
            "sell_policy": {"oracle_climax": {"enabled": True}, "mode": "active"}
        }
        self.engine.config.exit_sensor_config = {"enabled": False}
        self.engine.config.max_open_positions = 20
        self.engine.config.entry_sensor_15m_config = {"enabled": False}

    def tearDown(self):
        reset_cycle()

    def _technical(self):
        return SignalAnalysis(
            action="HOLD",
            symbol="DOGE/USDT",
            timeframe="1h",
            rsi=55.0,
            lower_bb=0.9,
            vol_multiplier=1.0,
            ampel_emoji="🟡",
            ampel_text="neutral",
            sources=[],
            confidence=50.0,
        )

    def _market(self, price=1.14):
        return MarketContext(
            symbol="DOGE/USDT",
            timeframe="1h",
            current_price=price,
            has_position=True,
            average_entry=1.0,
            atr_pct=8.0,
            strategy_params={"trailing_take_profit": {"enabled": False}},
        )

    def test_grind_blocks_bb_upper(self):
        bb = MagicMock()
        bb.action = SELL_FULL
        bb.priority = 3
        bb.source = "bb_upper"
        bb.rationale = "bb"
        bb.shadow_only = False
        dec = ClimaxDecision(MODE_GRIND, ("grind",), {})
        cfg = _cfg()
        with patch(
            "strategies.decision_engine.evaluate_market_structure_sells",
            return_value=[bb],
        ), patch(
            "strategies.decision_engine.evaluate_trailing_take_profit", return_value=None
        ), patch(
            "strategies.decision_engine.evaluate_profit_max_lifetime", return_value=None
        ), patch(
            "strategies.decision_engine.evaluate_trailing_stop", return_value=None
        ), patch(
            "strategies.decision_engine.evaluate_time_profit_exit", return_value=None
        ), patch(
            "strategies.decision_engine.sync_profit_armed_at", return_value=False
        ), patch.object(
            self.engine, "_oracle_climax_state", return_value=(dec, cfg)
        ):
            action, sources, *rest = self.engine._merge_sell(
                self._technical(),
                None,
                None,
                [],
                market=self._market(),
                position={"amount": 10.0, "average_entry": 1.0},
            )
        self.assertEqual(action, HOLD)
        self.assertTrue(any("grind blocked" in str(x) for x in rest[1] or []))

    def test_harvest_injects_full_close_on_green_runner(self):
        dec = ClimaxDecision(MODE_HARVEST, ("dump_1h",), {})
        cfg = _cfg()
        with patch(
            "strategies.decision_engine.evaluate_market_structure_sells",
            return_value=[],
        ), patch(
            "strategies.decision_engine.evaluate_trailing_take_profit", return_value=None
        ), patch(
            "strategies.decision_engine.evaluate_profit_max_lifetime", return_value=None
        ), patch(
            "strategies.decision_engine.evaluate_trailing_stop", return_value=None
        ), patch(
            "strategies.decision_engine.evaluate_time_profit_exit", return_value=None
        ), patch(
            "strategies.decision_engine.sync_profit_armed_at", return_value=False
        ), patch.object(
            self.engine, "_oracle_climax_state", return_value=(dec, cfg)
        ):
            action, sources, *rest = self.engine._merge_sell(
                self._technical(),
                None,
                None,
                [],
                market=self._market(1.14),
                position={"amount": 10.0, "average_entry": 1.0},
            )
        self.assertEqual(action, SELL_FULL)
        self.assertIn(HARVEST_SOURCE, sources)
        self.assertEqual(rest[2], HARVEST_SOURCE)

    def test_harvest_skips_locked_and_red(self):
        dec = ClimaxDecision(MODE_HARVEST, ("dump_1h",), {})
        cfg = _cfg()
        with patch(
            "strategies.decision_engine.evaluate_market_structure_sells",
            return_value=[],
        ), patch(
            "strategies.decision_engine.evaluate_trailing_take_profit", return_value=None
        ), patch(
            "strategies.decision_engine.evaluate_profit_max_lifetime", return_value=None
        ), patch(
            "strategies.decision_engine.evaluate_trailing_stop", return_value=None
        ), patch(
            "strategies.decision_engine.evaluate_time_profit_exit", return_value=None
        ), patch(
            "strategies.decision_engine.sync_profit_armed_at", return_value=False
        ), patch.object(
            self.engine, "_oracle_climax_state", return_value=(dec, cfg)
        ):
            locked_action, *_ = self.engine._merge_sell(
                self._technical(),
                None,
                None,
                [],
                market=self._market(1.20),
                position={
                    "amount": 10.0,
                    "average_entry": 1.0,
                    "lock": {"enabled": True, "modes": ["no_auto_sell"]},
                },
            )
            red_action, *_ = self.engine._merge_sell(
                self._technical(),
                None,
                None,
                [],
                market=self._market(0.90),
                position={"amount": 10.0, "average_entry": 1.0},
            )
        self.assertEqual(locked_action, HOLD)
        self.assertEqual(red_action, HOLD)


if __name__ == "__main__":
    unittest.main()
