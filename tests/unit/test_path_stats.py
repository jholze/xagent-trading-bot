"""Path-stats episode engine + kill-switch (no live DE wiring)."""

from __future__ import annotations

import os
import unittest

from intelligence.memory.path_stats import (
    band_label,
    compute_path_stats_for_ohlcv,
    extract_episodes,
    path_stats_enabled,
    summarize_episodes,
    upsert_path_summaries,
)


def _ramp_then_dump(
    *,
    trough: float = 100.0,
    peak_mult: float = 1.12,
    dump_mult: float = 0.90,
    n_before: int = 50,
    n_ramp: int = 20,
    n_after: int = 30,
) -> list[list[float]]:
    """Synthetic OHLCV: flat, ramp to peak, dump — enough bars for episodes."""
    rows: list[list[float]] = []
    t = 0
    px = trough
    peak = trough * peak_mult
    for _ in range(n_before):
        rows.append([t, px, px * 1.001, px * 0.999, px, 1.0])
        t += 1
    for i in range(n_ramp):
        px = trough + (peak - trough) * (i + 1) / n_ramp
        rows.append([t, px, px * 1.002, px * 0.998, px, 1.0])
        t += 1
    dump = peak * dump_mult
    for i in range(n_after):
        px = peak + (dump - peak) * (i + 1) / n_after
        rows.append([t, px, max(px, dump) * 1.001, min(px, dump) * 0.999, px, 1.0])
        t += 1
    return rows


class TestPathStatsKillSwitch(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("MEMORY_PATH_STATS", None)

    def test_default_disabled(self):
        os.environ.pop("MEMORY_PATH_STATS", None)
        self.assertFalse(path_stats_enabled({"memory": {}}))

    def test_env_enables(self):
        os.environ["MEMORY_PATH_STATS"] = "1"
        self.assertTrue(path_stats_enabled({"memory": {"path_stats": {"enabled": False}}}))

    def test_env_disables(self):
        os.environ["MEMORY_PATH_STATS"] = "0"
        self.assertFalse(path_stats_enabled({"memory": {"path_stats": {"enabled": True}}}))

    def test_config_enables(self):
        os.environ.pop("MEMORY_PATH_STATS", None)
        self.assertTrue(path_stats_enabled({"memory": {"path_stats": {"enabled": True}}}))

    def test_upsert_noop_when_disabled(self):
        os.environ["MEMORY_PATH_STATS"] = "0"
        n = upsert_path_summaries([], config={"memory": {"path_stats": {"enabled": True}}})
        self.assertEqual(n, 0)


class TestPathStatsEpisodes(unittest.TestCase):
    def test_band_label(self):
        self.assertEqual(band_label(0.10), "10pct")
        self.assertEqual(band_label(0.05), "5pct")

    def test_extract_finds_arm_and_giveback(self):
        rows = _ramp_then_dump(peak_mult=1.12, dump_mult=0.90)
        highs = [r[2] for r in rows]
        lows = [r[3] for r in rows]
        closes = [r[4] for r in rows]
        eps = extract_episodes(
            highs,
            lows,
            closes,
            bands=(0.05, 0.08, 0.10, 0.12),
            trough_lookback=20,
            forward_bars=15,
            trail_hit=0.08,
        )
        self.assertGreater(len(eps), 0)
        bands_hit = {e.band for e in eps}
        self.assertTrue(0.05 in bands_hit or 0.08 in bands_hit or 0.10 in bands_hit)
        # After dump, giveback should be positive for some episodes
        self.assertTrue(any(e.max_giveback > 0.02 for e in eps))

    def test_summarize_quality_thin(self):
        rows = _ramp_then_dump()
        highs = [r[2] for r in rows]
        lows = [r[3] for r in rows]
        closes = [r[4] for r in rows]
        eps = extract_episodes(highs, lows, closes, trough_lookback=20, forward_bars=15)
        summaries = summarize_episodes("TEST/USDT", "1h", eps)
        self.assertTrue(summaries)
        # Few episodes → thin is ok
        for s in summaries:
            self.assertIn(s.sample_quality, ("ok", "thin"))
            self.assertEqual(s.symbol, "TEST/USDT")
            doc = s.to_doc()
            self.assertIn("_id", doc)
            self.assertTrue(doc["_id"].startswith("default|demo|TEST/USDT|"))

    def test_compute_from_ohlcv_rows(self):
        rows = _ramp_then_dump(peak_mult=1.15)
        summaries = compute_path_stats_for_ohlcv(
            "ABC/USDT",
            "1h",
            rows,
            bands=(0.05, 0.10, 0.12),
            trough_lookback=20,
            forward_bars=15,
        )
        self.assertIsInstance(summaries, list)


if __name__ == "__main__":
    unittest.main()
