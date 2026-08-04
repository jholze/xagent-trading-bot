"""Path-stats soft bias (trail/arm) — pure unit tests, no Mongo."""

from __future__ import annotations

import os
import unittest

from intelligence.memory.path_stats import PathBandSummary
from intelligence.memory.path_stats_bias import (
    apply_path_stats_soft_bias,
    compute_bias_deltas,
    pick_band_summary,
    soft_bias_enabled,
)


def _summary(
    *,
    band: float = 0.10,
    n: int = 12,
    giveback: float = 0.12,
    p_trail: float = 0.6,
    p_ext: float = 0.3,
    quality: str | None = None,
) -> PathBandSummary:
    s = PathBandSummary(
        symbol="LAB/USDT",
        timeframe="1h",
        band=band,
        band_key=f"{int(band * 100)}pct",
        n=n,
        median_max_giveback=giveback,
        p_hit_trail=p_trail,
        p_hit_extension=p_ext,
        median_end_gain=0.08,
        sample_quality=quality or ("ok" if n >= 5 else "thin"),
    )
    # PathBandSummary.__post_init__ overwrites sample_quality from n
    if quality:
        s.sample_quality = quality
    return s


class TestSoftBiasKill(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("MEMORY_PATH_STATS", None)

    def test_disabled_when_path_stats_off(self):
        os.environ["MEMORY_PATH_STATS"] = "0"
        self.assertFalse(
            soft_bias_enabled(
                {"memory": {"path_stats": {"enabled": True, "soft_bias": {"enabled": True}}}}
            )
        )

    def test_soft_bias_flag_off(self):
        os.environ["MEMORY_PATH_STATS"] = "1"
        self.assertFalse(
            soft_bias_enabled(
                {"memory": {"path_stats": {"enabled": True, "soft_bias": {"enabled": False}}}}
            )
        )

    def test_soft_bias_on_when_path_stats_on(self):
        os.environ["MEMORY_PATH_STATS"] = "1"
        self.assertTrue(
            soft_bias_enabled(
                {"memory": {"path_stats": {"enabled": True, "soft_bias": {"enabled": True}}}}
            )
        )


class TestPickAndDeltas(unittest.TestCase):
    def test_pick_skips_thin(self):
        thin = _summary(n=2, quality="thin")
        ok = _summary(band=0.12, n=10, quality="ok")
        picked = pick_band_summary([thin, ok], prefer_band=0.10, require_quality="ok")
        self.assertIsNotNone(picked)
        self.assertEqual(picked.band, 0.12)

    def test_tighten_on_high_giveback(self):
        s = _summary(giveback=0.15, p_trail=0.7, p_ext=0.2)
        d = compute_bias_deltas(s)
        self.assertEqual(d["reason"], "tighten")
        self.assertLess(d["trail_delta_pct"], 0)
        self.assertLess(d["arm_delta_pct"], 0)
        self.assertGreaterEqual(d["trail_delta_pct"], -3.0)

    def test_loosen_on_low_giveback_high_ext(self):
        s = _summary(giveback=0.03, p_trail=0.2, p_ext=0.55)
        d = compute_bias_deltas(s)
        self.assertEqual(d["reason"], "loosen")
        self.assertGreater(d["trail_delta_pct"], 0)

    def test_neutral_mid_range(self):
        s = _summary(giveback=0.07, p_trail=0.4, p_ext=0.3)
        d = compute_bias_deltas(s)
        self.assertEqual(d["reason"], "neutral")
        self.assertEqual(d["trail_delta_pct"], 0.0)


class TestApplyBias(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("MEMORY_PATH_STATS", None)

    def _params(self):
        return {
            "symbol": "LAB/USDT",
            "trailing_stop": {
                "enabled": True,
                "activation_gain_pct": 5.0,
                "min_trail_pct": 8.0,
                "max_trail_pct": 25.0,
                "floor_at_entry": True,
                "arm_on_peak": True,
            },
            "trailing_take_profit": {
                "enabled": True,
                "arm_gain_pct": 10.0,
                "trail_pct": 6.0,
                "trail_pct_min": 3.0,
                "trail_pct_max": 12.0,
                "trail_above_zero_after_arm": True,
            },
        }

    def test_apply_tighten_and_preserve_rails(self):
        os.environ["MEMORY_PATH_STATS"] = "1"
        cfg = {"memory": {"path_stats": {"enabled": True, "soft_bias": {"enabled": True}}}}
        base = self._params()
        out = apply_path_stats_soft_bias(
            base,
            "LAB/USDT",
            config=cfg,
            summaries=[_summary(giveback=0.16, p_trail=0.75)],
        )
        self.assertTrue(out["_path_stats_bias"]["applied"])
        self.assertLess(out["trailing_stop"]["min_trail_pct"], 8.0)
        self.assertLess(out["trailing_stop"]["activation_gain_pct"], 5.0)
        self.assertTrue(out["trailing_stop"]["floor_at_entry"])
        self.assertTrue(out["trailing_stop"]["arm_on_peak"])
        self.assertTrue(out["trailing_take_profit"]["trail_above_zero_after_arm"])
        # TTP trail moves less hard
        ts_delta = out["trailing_stop"]["min_trail_pct"] - 8.0
        ttp_delta = out["trailing_take_profit"]["trail_pct"] - 6.0
        self.assertLess(abs(ttp_delta), abs(ts_delta) + 0.01)

    def test_thin_no_apply(self):
        os.environ["MEMORY_PATH_STATS"] = "1"
        cfg = {"memory": {"path_stats": {"enabled": True, "soft_bias": {"enabled": True}}}}
        thin = _summary(n=2, quality="thin")
        thin.sample_quality = "thin"
        out = apply_path_stats_soft_bias(
            self._params(),
            "LAB/USDT",
            config=cfg,
            summaries=[thin],
        )
        self.assertNotIn("_path_stats_bias", out)
        self.assertEqual(out["trailing_stop"]["min_trail_pct"], 8.0)

    def test_disabled_identity(self):
        os.environ["MEMORY_PATH_STATS"] = "0"
        out = apply_path_stats_soft_bias(
            self._params(),
            "LAB/USDT",
            config={"memory": {"path_stats": {"enabled": True}}},
            summaries=[_summary()],
        )
        self.assertEqual(out["trailing_stop"]["min_trail_pct"], 8.0)
        self.assertNotIn("_path_stats_bias", out)


class TestMaybeRefreshThrottle(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("MEMORY_PATH_STATS", None)
        import intelligence.memory.path_stats_refresh as r

        r._LAST_REFRESH_AT = 0.0
        r._LAST_RESULT = {}

    def test_throttle_skips(self):
        import intelligence.memory.path_stats_refresh as r

        os.environ["MEMORY_PATH_STATS"] = "1"
        r._LAST_REFRESH_AT = __import__("time").time()
        out = r.maybe_refresh_path_stats(
            config={
                "memory": {
                    "path_stats": {
                        "enabled": True,
                        "refresh_in_memory_cycle": True,
                        "refresh_interval_hours": 12,
                    }
                }
            }
        )
        self.assertTrue(out.get("skipped"))
        self.assertEqual(out.get("reason"), "throttled")

    def test_disabled_skips(self):
        from intelligence.memory.path_stats_refresh import maybe_refresh_path_stats

        os.environ["MEMORY_PATH_STATS"] = "0"
        out = maybe_refresh_path_stats(
            config={"memory": {"path_stats": {"enabled": True, "refresh_in_memory_cycle": True}}}
        )
        self.assertTrue(out.get("skipped"))
        self.assertEqual(out.get("reason"), "disabled")


if __name__ == "__main__":
    unittest.main()
