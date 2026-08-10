"""Unit tests for WS/ticker RelVol tracker (no network)."""

from __future__ import annotations

import unittest

from services.gainer_signal.relvol_tracker import RelvolTracker


class TestRelvolTracker(unittest.TestCase):
    def test_quiet_no_fire(self):
        tr = RelvolTracker(mult=10, min_ign_qvol=100, cooldown_sec=0)
        t0 = 1_700_000_000.0
        # flat 24h vol for 14h
        for h in range(0, 15):
            tr.sample_tickers(
                {"AAA/USDT": {"last": 1.0, "quoteVolume": 100_000}},
                now=t0 + h * 3600,
            )
        fires = tr.evaluate(now=t0 + 14 * 3600)
        self.assertEqual(fires, [])

    def test_volume_spike_fires(self):
        tr = RelvolTracker(
            mult=10,
            min_ign_qvol=1000,
            baseline_floor=50,
            cooldown_sec=0,
            baseline_hours=12,
        )
        t0 = 1_700_000_000.0
        # baseline: ~1000 per hour of new volume (24h qv climbs slowly)
        qv = 10_000.0
        for h in range(0, 13):
            qv += 1_000.0  # +1k per hour
            tr.sample_tickers(
                {"SPIKE/USDT": {"last": 1.0 + h * 0.001, "quoteVolume": qv}},
                now=t0 + h * 3600,
            )
        # ignition hour: +50k in one hour, price up
        qv += 50_000.0
        tr.sample_tickers(
            {"SPIKE/USDT": {"last": 1.05, "quoteVolume": qv}},
            now=t0 + 13 * 3600,
        )
        fires = tr.evaluate(now=t0 + 13 * 3600)
        syms = {f["symbol"] for f in fires}
        self.assertIn("SPIKE/USDT", syms)
        self.assertGreater(fires[0]["factor"], 5)


if __name__ == "__main__":
    unittest.main()
