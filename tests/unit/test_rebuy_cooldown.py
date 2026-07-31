"""Unit tests for per-coin rebuy cooldown resolver."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from risk.rebuy_cooldown import (
    format_rebuy_reject_message,
    is_hard_stop_sell,
    normalize_exit_key,
    prepare_dynamic_config,
    rebuy_cooldown_enabled,
    resolve_rebuy_cooldown_hours,
    signal_quality_from_confidence,
)


def _cfg(**over):
    base = {
        "enabled": True,
        "base_hours_by_regime": {
            "RISK_ON": 1.0,
            "NEUTRAL": 2.0,
            "RISK_OFF": 3.5,
            "CRASH": 6.0,
            "WARMUP": 3.0,
        },
        "min_hours": 0.75,
        "max_hours": 8.0,
        "stop_loss_hours": 24.0,
        "block_rebuy_if_last_sell_was_stop": True,
        "quality_mult": {"default": 1.0, "high_conviction_entry": 0.7},
        "vol_tier_mult": {"volatile": 0.9, "stable": 1.1},
        "memory": {
            "enabled": True,
            "min_samples": 3,
            "win_rate_high": 0.55,
            "win_rate_low": 0.4,
            "high_wr_mult": 0.75,
            "low_wr_mult": 1.4,
            "prefer_mult": 0.85,
            "soft_block_mult": 1.5,
            "soft_block_min_hours": 4.0,
            "size_bias_weight": 0.0,
            "gross_loss_cooloff_hours": 12.0,
            "gross_loss_pct": -8.0,
            "gross_loss_usdt": 500,
            "structure_risk_mult": 1.35,
            "missing_profile_mult": 1.0,
        },
        "exit_source_mult": {
            "trailing_take_profit": 0.85,
            "bb_upper": 0.9,
            "technical": 1.0,
            "stop": 1.0,
            "default": 1.0,
        },
    }
    base.update(over)
    return base


class TestHardStopAndNormalize(unittest.TestCase):
    def test_hard_stop_signals(self):
        self.assertTrue(is_hard_stop_sell("SELL_STOP_FULL"))
        self.assertTrue(is_hard_stop_sell("SELL_STOP_PARTIAL"))
        self.assertTrue(is_hard_stop_sell("X_STOP_LOSS"))
        self.assertFalse(is_hard_stop_sell("trailing_stop"))
        self.assertFalse(is_hard_stop_sell("trailing_take_profit"))
        self.assertFalse(is_hard_stop_sell("SELL_FULL"))
        self.assertFalse(is_hard_stop_sell(""))

    def test_normalize_exit_keys(self):
        self.assertEqual(normalize_exit_key("trailing_take_profit"), "trailing_take_profit")
        self.assertEqual(normalize_exit_key("ttp"), "trailing_take_profit")
        self.assertEqual(normalize_exit_key("bb_upper"), "bb_upper")
        self.assertEqual(normalize_exit_key("SELL_FULL"), "technical")
        self.assertEqual(normalize_exit_key("sell_full"), "technical")
        self.assertEqual(normalize_exit_key("trailing_stop"), "trailing_stop")
        self.assertEqual(normalize_exit_key("grid"), "grid")
        self.assertEqual(normalize_exit_key("cmc_spike"), "social")
        self.assertEqual(normalize_exit_key(""), "default")

    def test_signal_quality_from_confidence(self):
        self.assertEqual(signal_quality_from_confidence(80), "high_conviction_entry")
        self.assertEqual(signal_quality_from_confidence(50), "default")
        self.assertEqual(signal_quality_from_confidence(None), "default")
        self.assertEqual(signal_quality_from_confidence("bad"), "default")


class TestRebuyCooldown(unittest.TestCase):
    def test_stop_sell_always_24h(self):
        r = resolve_rebuy_cooldown_hours(
            regime="RISK_ON",
            last_sell_signal="SELL_STOP_FULL",
            config=_cfg(),
        )
        self.assertTrue(r.stop_sell)
        self.assertEqual(r.hours, 24.0)
        self.assertIn("stop_loss", r.reasons)

    def test_stop_in_signal_name(self):
        r = resolve_rebuy_cooldown_hours(
            regime="NEUTRAL",
            last_sell_signal="SELL_STOP_PARTIAL",
            config=_cfg(),
        )
        self.assertTrue(r.stop_sell)
        self.assertEqual(r.hours, 24.0)

    def test_trailing_stop_exit_not_hard_stop_loss(self):
        """ATR trailing_stop is not the 24h hard stop-loss rebuy path."""
        r = resolve_rebuy_cooldown_hours(
            regime="NEUTRAL",
            last_sell_signal="trailing_stop",
            config=_cfg(),
        )
        self.assertFalse(r.stop_sell)
        self.assertLess(r.hours, 24.0)
        self.assertEqual(r.factors.get("exit_key"), "trailing_stop")

    def test_risk_on_ttp_no_profile_shorter_than_4(self):
        r = resolve_rebuy_cooldown_hours(
            regime="RISK_ON",
            last_sell_signal="trailing_take_profit",
            profile=None,
            config=_cfg(),
        )
        # 1.0 * 0.85 = 0.85 → clamp min 0.75
        self.assertFalse(r.stop_sell)
        self.assertLess(r.hours, 4.0)
        self.assertGreaterEqual(r.hours, 0.75)
        self.assertAlmostEqual(r.hours, 0.85, places=2)

    def test_neutral_bb_upper_high_wr(self):
        prof = SimpleNamespace(
            win_rate=0.7,
            sells_30d=5,
            trades_30d=5,
            entry_bias="prefer",
            size_bias=1.0,
            features={},
        )
        r = resolve_rebuy_cooldown_hours(
            regime="NEUTRAL",
            last_sell_signal="bb_upper",
            profile=prof,
            config=_cfg(),
        )
        # 2.0 * 0.9 * 0.75 * 0.85 = 1.1475
        self.assertLess(r.hours, 4.0)
        self.assertAlmostEqual(r.hours, 2.0 * 0.9 * 0.75 * 0.85, places=3)
        self.assertIn("high_wr", r.reasons)
        self.assertIn("bias_prefer", r.reasons)

    def test_soft_block_floor(self):
        prof = SimpleNamespace(
            win_rate=0.8,
            sells_30d=10,
            entry_bias="soft_block",
            size_bias=1.0,
            features={},
        )
        r = resolve_rebuy_cooldown_hours(
            regime="RISK_ON",
            last_sell_signal="trailing_take_profit",
            profile=prof,
            config=_cfg(),
        )
        self.assertGreaterEqual(r.hours, 4.0)
        self.assertIn("soft_block_floor", r.reasons)

    def test_under_sample_ignores_wr(self):
        prof = SimpleNamespace(
            win_rate=0.9,
            sells_30d=1,
            trades_30d=1,
            entry_bias="neutral",
            size_bias=1.0,
            features={},
        )
        r = resolve_rebuy_cooldown_hours(
            regime="NEUTRAL",
            last_sell_signal="technical",
            profile=prof,
            config=_cfg(),
        )
        self.assertIn("wr_under_sample", r.reasons)
        self.assertAlmostEqual(r.hours, 2.0, places=2)

    def test_missing_profile_fail_open(self):
        r = resolve_rebuy_cooldown_hours(
            regime="NEUTRAL",
            last_sell_signal="technical",
            profile=None,
            config=_cfg(),
        )
        self.assertIn("missing_profile", r.reasons)
        self.assertAlmostEqual(r.hours, 2.0, places=2)

    def test_clamp_max(self):
        prof = SimpleNamespace(
            win_rate=0.1,
            sells_30d=10,
            entry_bias="soft_block",
            size_bias=0.5,
            features={"structure_risk": True},
        )
        cfg = _cfg()
        cfg["memory"]["size_bias_weight"] = 0.15
        r = resolve_rebuy_cooldown_hours(
            regime="CRASH",
            last_sell_signal="technical",
            profile=prof,
            config=cfg,
        )
        # soft_block_floor may push to 4; structure*low_wr*crash can hit max
        self.assertLessEqual(r.hours, max(8.0, 4.0))
        self.assertGreaterEqual(r.hours, 4.0)  # soft_block floor

    def test_gross_loss_floor(self):
        prof = SimpleNamespace(
            win_rate=0.5,
            sells_30d=5,
            entry_bias="neutral",
            size_bias=1.0,
            features={
                "last_loss_at": "2026-07-01T00:00:00Z",
                "worst_loss_usdt": 800,
                "worst_loss_pct": 10,
            },
        )
        r = resolve_rebuy_cooldown_hours(
            regime="RISK_ON",
            last_sell_signal="trailing_take_profit",
            profile=prof,
            config=_cfg(),
        )
        self.assertGreaterEqual(r.hours, 12.0)
        self.assertIn("gross_loss_floor", r.reasons)

    def test_enabled_flag_helper(self):
        self.assertTrue(rebuy_cooldown_enabled({"rebuy_cooldown": {"enabled": True}}))
        self.assertFalse(rebuy_cooldown_enabled({"rebuy_cooldown": {"enabled": False}}))
        self.assertFalse(rebuy_cooldown_enabled({}))

    def test_disabled_uses_fallback_hours(self):
        r = resolve_rebuy_cooldown_hours(
            regime="RISK_ON",
            last_sell_signal="trailing_take_profit",
            config=_cfg(enabled=False),
            fallback_hours=4.0,
        )
        self.assertEqual(r.hours, 4.0)
        self.assertIn("disabled_fallback", r.reasons)

    def test_reject_message_format(self):
        r = resolve_rebuy_cooldown_hours(
            regime="NEUTRAL",
            last_sell_signal="bb_upper",
            profile=None,
            config=_cfg(),
        )
        msg = format_rebuy_reject_message(elapsed_h=1.8, result=r)
        self.assertIn("Rebuy cooldown", msg)
        self.assertIn("1.8h", msg)
        self.assertIn("regime=NEUTRAL", msg)

    def test_prefers_last_exit_source_over_generic_signal(self):
        r = resolve_rebuy_cooldown_hours(
            regime="NEUTRAL",
            last_sell_signal="SELL",
            last_exit_source="trailing_take_profit",
            profile=None,
            config=_cfg(),
        )
        self.assertEqual(r.factors.get("exit_key"), "trailing_take_profit")
        self.assertAlmostEqual(r.hours, 2.0 * 0.85, places=2)

    def test_sell_full_maps_to_technical(self):
        r = resolve_rebuy_cooldown_hours(
            regime="NEUTRAL",
            last_sell_signal="SELL_FULL",
            profile=None,
            config=_cfg(),
        )
        self.assertEqual(r.factors.get("exit_key"), "technical")
        self.assertAlmostEqual(r.hours, 2.0, places=2)

    def test_total_pnl_strong_shortens(self):
        prof = SimpleNamespace(
            win_rate=0.5,
            sells_30d=5,
            trades_30d=5,
            entry_bias="neutral",
            size_bias=1.0,
            total_pnl_usdt=80.0,
            avg_pnl_usdt=16.0,
            risk_score=0.4,
            dca_count_30d=0,
            features={},
        )
        base = resolve_rebuy_cooldown_hours(
            regime="NEUTRAL",
            last_sell_signal="technical",
            profile=SimpleNamespace(
                win_rate=0.5,
                sells_30d=5,
                entry_bias="neutral",
                size_bias=1.0,
                total_pnl_usdt=0.0,
                avg_pnl_usdt=0.0,
                risk_score=0.5,
                dca_count_30d=0,
                features={},
            ),
            config=_cfg(),
        )
        strong = resolve_rebuy_cooldown_hours(
            regime="NEUTRAL",
            last_sell_signal="technical",
            profile=prof,
            config=_cfg(),
        )
        self.assertLess(strong.hours, base.hours)
        self.assertIn("pnl_strong", strong.reasons)

    def test_channel_pnl_weak_lengthens_grid(self):
        prof = SimpleNamespace(
            win_rate=0.5,
            sells_30d=5,
            entry_bias="neutral",
            size_bias=1.0,
            total_pnl_usdt=0.0,
            avg_pnl_usdt=0.0,
            risk_score=0.5,
            dca_count_30d=0,
            features={
                "by_source": {"grid": {"buys": 3, "sells": 3, "pnl_usdt": -80.0}},
            },
        )
        r = resolve_rebuy_cooldown_hours(
            regime="NEUTRAL",
            last_exit_source="grid",
            last_sell_signal="SELL",
            profile=prof,
            config=_cfg(),
        )
        self.assertIn("channel_pnl_weak", r.reasons)
        self.assertGreater(r.hours, 2.0)

    def test_high_conviction_shortens(self):
        base = resolve_rebuy_cooldown_hours(
            regime="NEUTRAL",
            last_sell_signal="technical",
            signal_quality="default",
            config=_cfg(),
        )
        high = resolve_rebuy_cooldown_hours(
            regime="NEUTRAL",
            last_sell_signal="technical",
            signal_quality="high_conviction_entry",
            config=_cfg(),
        )
        self.assertLess(high.hours, base.hours)
        self.assertAlmostEqual(high.hours, 2.0 * 0.7, places=2)

    def test_prepare_dynamic_config_merges_arch(self):
        out = prepare_dynamic_config(
            {"enabled": True},
            {"block_rebuy_if_last_sell_was_stop": True, "rebuy_after_stop_loss_hours": 30},
        )
        self.assertTrue(out["block_rebuy_if_last_sell_was_stop"])
        self.assertEqual(out["stop_loss_hours"], 30.0)

    def test_unknown_regime_falls_to_neutral(self):
        r = resolve_rebuy_cooldown_hours(
            regime="WEIRD",
            last_sell_signal="technical",
            config=_cfg(),
        )
        self.assertEqual(r.factors.get("regime"), "NEUTRAL")
        self.assertAlmostEqual(r.hours, 2.0, places=2)


if __name__ == "__main__":
    unittest.main()
