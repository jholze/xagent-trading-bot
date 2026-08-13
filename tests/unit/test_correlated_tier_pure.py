"""Unit tests for correlated-tier group resolution + drawdown detector (no network)."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from services.correlated_tier.drawdown_tracker import (
    GroupDrawdownTracker,
    compute_drawdown_pct,
)
from strategies.correlated_tier_overlay import (
    apply_correlated_tier_overlay,
    resolve_correlated_group,
)


US_STOCK = ["CRWVG/USDT", "NBISG/USDT", "SOXLG/USDT", "MVLLG/USDT"]


def _cfg(*, enabled: bool = True, extra_groups: dict | None = None) -> dict:
    groups = {
        "us_stock": {
            "proxy_symbols": list(US_STOCK),
            "member_symbols": list(US_STOCK),
            "drawdown_pct": 5.0,
            "window_sec": 600,
            "min_confirming": 2,
            "trailing_take_profit": {
                "trail_pct": 3.5,
                "arm_gain_pct": 10,
                "min_gain_pct": 8,
            },
            "trailing_stop": {
                "activation_gain_pct": 8,
                "min_trail_pct": 6,
                "max_trail_pct": 15,
            },
            "full_close_gain_pct": 12,
        },
        "crypto_market": {
            "proxy_symbols": ["BTC/USDT", "ETH/USDT"],
            "member_symbols": "*",
            "drawdown_pct": 4.0,
            "window_sec": 900,
            "min_confirming": 1,
        },
    }
    if extra_groups:
        groups.update(extra_groups)
    return {
        "sell_policy": {
            "correlated_tier": {
                "enabled": enabled,
                "groups": groups,
                "eval_interval_sec": 5,
                "flag_ttl_sec": 30,
            }
        }
    }


class TestResolveCorrelatedGroup(unittest.TestCase):
    def test_explicit_membership_wins_over_star(self):
        cfg = _cfg()
        self.assertEqual(resolve_correlated_group("CRWVG/USDT", cfg), "us_stock")
        self.assertEqual(resolve_correlated_group("nbisg-usdt", cfg), "us_stock")
        self.assertEqual(resolve_correlated_group("SOXLG_USDT", cfg), "us_stock")

    def test_star_fallback_for_unlisted_symbol(self):
        cfg = _cfg()
        self.assertEqual(resolve_correlated_group("SOL/USDT", cfg), "crypto_market")
        self.assertEqual(resolve_correlated_group("DOGE/USDT", cfg), "crypto_market")

    def test_proxy_never_resolves_as_star_member(self):
        """BTC/ETH are crypto_market proxies — not members of '*'."""
        cfg = _cfg()
        self.assertIsNone(resolve_correlated_group("BTC/USDT", cfg))
        self.assertIsNone(resolve_correlated_group("ETH/USDT", cfg))

    def test_proxy_of_explicit_group_still_resolves_when_listed_as_member(self):
        """us_stock proxies are also explicit members and must keep the overlay."""
        cfg = _cfg()
        self.assertEqual(resolve_correlated_group("MVLLG/USDT", cfg), "us_stock")

    def test_no_match_returns_none(self):
        cfg = {
            "sell_policy": {
                "correlated_tier": {
                    "enabled": True,
                    "groups": {
                        "us_stock": {
                            "proxy_symbols": list(US_STOCK),
                            "member_symbols": list(US_STOCK),
                        }
                    },
                }
            }
        }
        self.assertIsNone(resolve_correlated_group("SOL/USDT", cfg))
        self.assertIsNone(resolve_correlated_group("", cfg))
        self.assertIsNone(resolve_correlated_group("SOL/USDT", {}))

    def test_disabled_group_is_skipped(self):
        cfg = _cfg(
            extra_groups={
                "us_stock": {
                    "enabled": False,
                    "proxy_symbols": list(US_STOCK),
                    "member_symbols": list(US_STOCK),
                }
            }
        )
        # us_stock disabled → CRWVG is not explicit; it is a proxy of that
        # (disabled) group so it also must not fall through to '*'.
        self.assertIsNone(resolve_correlated_group("CRWVG/USDT", cfg))
        self.assertEqual(resolve_correlated_group("SOL/USDT", cfg), "crypto_market")


class TestApplyCorrelatedTierOverlay(unittest.TestCase):
    def test_noop_when_disabled(self):
        params = {"trailing_take_profit": {"trail_pct": 6.0, "arm_gain_pct": 15}}
        out = apply_correlated_tier_overlay(params, "CRWVG/USDT", _cfg(enabled=False))
        self.assertEqual(out["trailing_take_profit"]["trail_pct"], 6.0)
        self.assertNotIn("correlated_tier_group", out)

    def test_us_stock_overlays_trail_and_full_close(self):
        params = {
            "trailing_take_profit": {"trail_pct": 6.0, "arm_gain_pct": 15, "enabled": True},
            "trailing_stop": {"min_trail_pct": 8},
        }
        out = apply_correlated_tier_overlay(params, "CRWVG/USDT", _cfg(enabled=True))
        self.assertEqual(out["correlated_tier_group"], "us_stock")
        self.assertEqual(out["trailing_take_profit"]["trail_pct"], 3.5)
        self.assertEqual(out["trailing_take_profit"]["arm_gain_pct"], 10)
        self.assertEqual(out["trailing_take_profit"]["full_close_gain_pct"], 12)
        self.assertEqual(out["trailing_stop"]["min_trail_pct"], 6)
        self.assertFalse(out["trailing_take_profit"].get("dynamic_trail"))

    def test_crypto_market_has_no_trail_overlay(self):
        params = {"trailing_take_profit": {"trail_pct": 6.0, "arm_gain_pct": 15}}
        out = apply_correlated_tier_overlay(params, "SOL/USDT", _cfg(enabled=True))
        self.assertEqual(out["correlated_tier_group"], "crypto_market")
        self.assertEqual(out["trailing_take_profit"]["trail_pct"], 6.0)
        self.assertNotIn("full_close_gain_pct", out["trailing_take_profit"])


class TestComputeDrawdownPct(unittest.TestCase):
    def test_empty_or_bad_window(self):
        self.assertIsNone(compute_drawdown_pct([], 100.0, 600.0))
        self.assertIsNone(compute_drawdown_pct([(90.0, 1.0)], 100.0, 0.0))

    def test_window_high_not_all_time_high(self):
        # ATH 10, later window high 8, last 7.6 → 5% off window high (not 24% off ATH)
        samples = [
            (0.0, 10.0),
            (1000.0, 8.0),
            (1100.0, 7.6),
        ]
        dd = compute_drawdown_pct(samples, now=1100.0, window_sec=200.0)
        self.assertIsNotNone(dd)
        self.assertAlmostEqual(dd, (1.0 - 7.6 / 8.0) * 100.0, places=5)


class TestGroupDrawdownTracker(unittest.TestCase):
    def test_quiet_not_active(self):
        tr = GroupDrawdownTracker(
            "us_stock",
            ["AAA/USDT", "BBB/USDT"],
            drawdown_pct=5.0,
            window_sec=600.0,
            min_confirming=2,
        )
        t0 = 1_700_000_000.0
        for i in range(10):
            now = t0 + i * 30
            tr.on_tick("AAA/USDT", 1.0, now=now)
            tr.on_tick("BBB/USDT", 2.0, now=now)
        ev = tr.evaluate(now=t0 + 9 * 30)
        self.assertFalse(ev["active"])
        self.assertEqual(ev["confirming"], 0)
        self.assertFalse(ev["per_symbol"]["AAA/USDT"])
        self.assertFalse(ev["per_symbol"]["BBB/USDT"])

    def test_fast_confirmed_drop_active(self):
        tr = GroupDrawdownTracker(
            "us_stock",
            ["AAA/USDT", "BBB/USDT"],
            drawdown_pct=5.0,
            window_sec=600.0,
            min_confirming=2,
        )
        t0 = 1_700_000_000.0
        tr.on_tick("AAA/USDT", 100.0, now=t0)
        tr.on_tick("BBB/USDT", 50.0, now=t0)
        tr.on_tick("AAA/USDT", 94.0, now=t0 + 30)  # 6%
        tr.on_tick("BBB/USDT", 47.0, now=t0 + 30)  # 6%
        ev = tr.evaluate(now=t0 + 30)
        self.assertTrue(ev["active"])
        self.assertEqual(ev["confirming"], 2)
        self.assertTrue(ev["per_symbol"]["AAA/USDT"])
        self.assertTrue(ev["per_symbol"]["BBB/USDT"])

    def test_single_leg_does_not_confirm(self):
        tr = GroupDrawdownTracker(
            "us_stock",
            ["AAA/USDT", "BBB/USDT"],
            drawdown_pct=5.0,
            window_sec=600.0,
            min_confirming=2,
        )
        t0 = 1_700_000_000.0
        tr.on_tick("AAA/USDT", 100.0, now=t0)
        tr.on_tick("BBB/USDT", 50.0, now=t0)
        tr.on_tick("AAA/USDT", 90.0, now=t0 + 20)
        tr.on_tick("BBB/USDT", 49.5, now=t0 + 20)
        ev = tr.evaluate(now=t0 + 20)
        self.assertFalse(ev["active"])
        self.assertEqual(ev["confirming"], 1)

    def test_slow_bleed_not_active(self):
        """~10% over an hour, window=600s → in-window drawdown stays small."""
        tr = GroupDrawdownTracker(
            "us_stock",
            ["AAA/USDT", "BBB/USDT"],
            drawdown_pct=5.0,
            window_sec=600.0,
            min_confirming=2,
        )
        t0 = 1_700_000_000.0
        # 10% over 3600s ≈ 1.67% per 600s window
        for i in range(0, 61):
            now = t0 + i * 60
            frac = i / 60.0
            tr.on_tick("AAA/USDT", 100.0 * (1.0 - 0.10 * frac), now=now)
            tr.on_tick("BBB/USDT", 50.0 * (1.0 - 0.10 * frac), now=now)
        ev = tr.evaluate(now=t0 + 3600)
        self.assertFalse(ev["active"])
        self.assertEqual(ev["confirming"], 0)

    def test_recovery_within_window_flips_false(self):
        tr = GroupDrawdownTracker(
            "crypto_market",
            ["BTC/USDT", "ETH/USDT"],
            drawdown_pct=4.0,
            window_sec=900.0,
            min_confirming=1,
        )
        t0 = 1_700_000_000.0
        tr.on_tick("BTC/USDT", 100.0, now=t0)
        tr.on_tick("BTC/USDT", 95.0, now=t0 + 60)  # 5% — active
        ev1 = tr.evaluate(now=t0 + 60)
        self.assertTrue(ev1["active"])
        tr.on_tick("BTC/USDT", 100.0, now=t0 + 120)  # recovered
        ev2 = tr.evaluate(now=t0 + 120)
        self.assertFalse(ev2["active"])
        self.assertEqual(ev2["confirming"], 0)


if __name__ == "__main__":
    unittest.main()
