"""Unit tests for RelVol pure detector (no network)."""

from __future__ import annotations

import unittest

from services.gainer_universe.relvol_pure import (
    abs_vol_24h_from_qs,
    detect_ignition_at_t,
    find_signals_ccxt,
    qvol_ccxt,
)


def _bar(ts: int, o: float, h: float, l: float, c: float, base_vol: float) -> list[float]:
    return [ts * 1000, o, h, l, c, base_vol]


class TestRelvolPure(unittest.TestCase):
    def test_quiet_series_no_signal(self):
        bars = []
        for i in range(20):
            # steady ~1000 quote vol (base_vol * mid)
            bars.append(_bar(1_700_000_000 + i * 3600, 1.0, 1.01, 0.99, 1.0, 1000.0))
        sigs = find_signals_ccxt("T/USDT", bars, mult=10, win=12, only_last_closed=False)
        self.assertEqual(sigs, [])

    def test_spike_fires_green(self):
        bars = []
        for i in range(15):
            bars.append(_bar(1_700_000_000 + i * 3600, 1.0, 1.0, 1.0, 1.0, 100.0))
        # ignition: green + 20x vol
        bars.append(_bar(1_700_000_000 + 15 * 3600, 1.0, 1.2, 1.0, 1.1, 3000.0))
        # pad one more for find_signals range end
        bars.append(_bar(1_700_000_000 + 16 * 3600, 1.1, 1.1, 1.1, 1.1, 100.0))
        sigs = find_signals_ccxt(
            "SPIKE/USDT",
            bars,
            mult=10,
            win=12,
            min_ign_qvol=500,
            baseline_floor=50,
            only_last_closed=False,
        )
        self.assertTrue(any(s["symbol"] == "SPIKE/USDT" for s in sigs))
        self.assertGreater(sigs[0]["factor"], 10)

    def test_red_candle_blocked(self):
        bars = []
        for i in range(15):
            bars.append(_bar(1_700_000_000 + i * 3600, 1.0, 1.0, 1.0, 1.0, 100.0))
        bars.append(_bar(1_700_000_000 + 15 * 3600, 1.1, 1.1, 0.9, 0.95, 3000.0))  # red
        bars.append(_bar(1_700_000_000 + 16 * 3600, 0.95, 0.95, 0.95, 0.95, 100.0))
        sigs = find_signals_ccxt(
            "RED/USDT",
            bars,
            mult=10,
            win=12,
            min_ign_qvol=500,
            baseline_floor=50,
            require_green=True,
        )
        self.assertEqual(sigs, [])

    def test_only_last_closed(self):
        bars = []
        for i in range(15):
            bars.append(_bar(1_700_000_000 + i * 3600, 1.0, 1.0, 1.0, 1.0, 100.0))
        bars.append(_bar(1_700_000_000 + 15 * 3600, 1.0, 1.2, 1.0, 1.1, 3000.0))
        sigs = find_signals_ccxt(
            "L/USDT",
            bars,
            mult=10,
            win=12,
            min_ign_qvol=500,
            baseline_floor=50,
            only_last_closed=True,
        )
        self.assertEqual(len(sigs), 1)

    def test_abs_vol_24h(self):
        qs = [1.0] * 30
        self.assertEqual(abs_vol_24h_from_qs(qs, 29), 24.0)

    def test_qvol_positive(self):
        b = _bar(0, 2, 3, 1, 2, 10)
        self.assertGreater(qvol_ccxt(b), 0)

    def test_size_usdt_participation(self):
        from services.gainer_universe.relvol_shadow import size_usdt_for_signal

        cfg = {
            "participation": 0.02,
            "max_ticket_usdt": 500,
            "min_ticket_usdt": 50,
            "max_pct_of_vol_24h": 0.02,
        }
        # 1h qvol 50k → 2% = 1000 → capped 500
        u = size_usdt_for_signal(
            qvol_1h=50_000, abs_vol_24h=1_000_000, cfg=cfg, max_usdt_per_trade=4500
        )
        self.assertEqual(u, 500.0)
        # too thin
        u2 = size_usdt_for_signal(
            qvol_1h=1_000, abs_vol_24h=2_000, cfg=cfg, max_usdt_per_trade=4500
        )
        self.assertEqual(u2, 0.0)


if __name__ == "__main__":
    unittest.main()
