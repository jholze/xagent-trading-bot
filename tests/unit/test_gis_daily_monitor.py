"""Unit tests for GIS daily monitor pure helpers + report builder."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from services.gis_monitor.pure import (
    DEFAULT_ELIGIBLE_MIN_VOL,
    compute_kpis,
    is_eligible_leader,
    is_gainer_source,
    is_leverage_symbol,
    is_spot_usdt_base,
    join_leaders_to_fills,
    normalize_symbol,
    rank_leaders_from_tickers,
)
from scripts.gis_daily_monitor import build_report, parse_day_arg, render_markdown, write_report


class TestNormalizeAndFlags(unittest.TestCase):
    def test_normalize_underscore(self):
        self.assertEqual(normalize_symbol("btc_usdt"), "BTC/USDT")

    def test_leverage(self):
        self.assertTrue(is_leverage_symbol("BTC3L/USDT"))
        self.assertFalse(is_leverage_symbol("BTC/USDT"))
        self.assertTrue(is_leverage_symbol("PEPE5S/USDT"))

    def test_spot_usdt_allows_meme_no_price_gate(self):
        self.assertTrue(is_spot_usdt_base("PEPE/USDT"))
        self.assertTrue(is_spot_usdt_base("SHIB/USDT"))
        self.assertFalse(is_spot_usdt_base("USDT/USDT"))
        self.assertFalse(is_spot_usdt_base("BTC/USDC"))


class TestEligible(unittest.TestCase):
    def test_eligible_boundary_500k(self):
        ok, reason = is_eligible_leader(quote_vol=500_000, leverage=False)
        self.assertTrue(ok)
        self.assertIsNone(reason)
        ok2, reason2 = is_eligible_leader(quote_vol=499_999.99, leverage=False)
        self.assertFalse(ok2)
        self.assertEqual(reason2, "low_volume")

    def test_leverage_not_eligible_even_high_vol(self):
        ok, reason = is_eligible_leader(quote_vol=5_000_000, leverage=True)
        self.assertFalse(ok)
        self.assertEqual(reason, "leverage")

    def test_default_min_vol_constant(self):
        self.assertEqual(DEFAULT_ELIGIBLE_MIN_VOL, 500_000.0)


class TestRankLeaders(unittest.TestCase):
    def test_rank_order_no_vol_cut_lists_thin_and_flags(self):
        tickers = {
            "THIN/USDT": {"percentage": 90.0, "quoteVolume": 20_000, "last": 0.00001},
            "FAT/USDT": {"percentage": 40.0, "quoteVolume": 2_000_000, "last": 1.0},
            "MID/USDT": {"percentage": 50.0, "quoteVolume": 600_000, "last": 0.5},
            "BTC3L/USDT": {"percentage": 99.0, "quoteVolume": 9_000_000, "last": 1.0},
            "ETH/USDC": {"percentage": 100.0, "quoteVolume": 9_000_000, "last": 1.0},
        }
        leaders = rank_leaders_from_tickers(tickers, top_n=10)
        syms = [L["symbol"] for L in leaders]
        # leverage + thin included in recognize; USDC pair excluded
        self.assertIn("THIN/USDT", syms)
        self.assertIn("BTC3L/USDT", syms)
        self.assertNotIn("ETH/USDC", syms)
        # rank by pct: BTC3L 99, THIN 90, MID 50, FAT 40
        self.assertEqual(leaders[0]["symbol"], "BTC3L/USDT")
        self.assertEqual(leaders[0]["rank"], 1)
        self.assertTrue(leaders[0]["leverage"])
        self.assertFalse(leaders[0]["eligible"])
        thin = next(L for L in leaders if L["symbol"] == "THIN/USDT")
        self.assertFalse(thin["eligible"])
        self.assertEqual(thin["reject_reason"], "low_volume")
        mid = next(L for L in leaders if L["symbol"] == "MID/USDT")
        self.assertTrue(mid["eligible"])
        # no min-price: tiny last still ranked
        self.assertEqual(thin["last"], 0.00001)

    def test_top_n_cap(self):
        tickers = {
            f"C{i}/USDT": {"percentage": float(i), "quoteVolume": 1_000_000, "last": 1.0}
            for i in range(30)
        }
        leaders = rank_leaders_from_tickers(tickers, top_n=20)
        self.assertEqual(len(leaders), 20)
        self.assertEqual(leaders[0]["pct_24h"], 29.0)


class TestJoinAndKpis(unittest.TestCase):
    def test_join_missed_and_sources(self):
        leaders = rank_leaders_from_tickers(
            {
                "A/USDT": {"percentage": 50, "quoteVolume": 2_000_000, "last": 1},
                "B/USDT": {"percentage": 40, "quoteVolume": 2_000_000, "last": 1},
                "C/USDT": {"percentage": 30, "quoteVolume": 100_000, "last": 1},
            },
            top_n=10,
        )
        fills = [
            {"symbol": "A/USDT", "side": "buy", "status": "filled", "source": "grid"},
            {
                "symbol": "B/USDT",
                "side": "buy",
                "status": "filled",
                "source": "gainer_rank_entry",
            },
            {
                "symbol": "B/USDT",
                "side": "sell",
                "status": "filled",
                "source": "gainer_rank_entry",
                "exit_source": "trailing_stop",
                "pnl": 12.5,
            },
        ]
        join = join_leaders_to_fills(
            leaders, fills, recognized_symbols={"A/USDT", "Z/USDT"}, missed_rank_max=10
        )
        by = {r["symbol"]: r for r in join}
        self.assertTrue(by["A/USDT"]["bought_other"])
        self.assertFalse(by["A/USDT"]["bought_gainer"])
        self.assertFalse(by["A/USDT"]["missed"])  # bought other
        self.assertTrue(by["B/USDT"]["bought_gainer"])
        self.assertEqual(by["B/USDT"]["sell_pnl"], 12.5)
        # C low vol not eligible → not missed
        self.assertFalse(by["C/USDT"]["eligible"])
        self.assertFalse(by["C/USDT"]["missed"])
        self.assertTrue(by["A/USDT"]["recognized"])
        self.assertFalse(by["B/USDT"]["recognized"])

        kpis = compute_kpis(
            leaders, join, fills, recognized_symbols={"A/USDT", "Z/USDT"}, top_k=3
        )
        self.assertIsNotNone(kpis["recall_proxy"])
        self.assertEqual(kpis["recall_proxy_reason"], "ok")
        # A is in IST and recognized
        self.assertGreater(kpis["recall_proxy"], 0)
        self.assertIn("pnl_by_source", kpis)
        self.assertAlmostEqual(kpis["pnl_by_source"].get("trailing_stop", 0), 12.5)
        self.assertTrue(kpis["rules"]["min_price_filter"] is False)
        self.assertEqual(kpis["eligible_min_vol_usdt"], 500_000.0)

    def test_recall_null_without_recognized(self):
        leaders = [{"symbol": "A/USDT", "rank": 1, "eligible": True, "pct_24h": 10}]
        join = join_leaders_to_fills(leaders, [], recognized_symbols=None)
        kpis = compute_kpis(leaders, join, [], recognized_symbols=None)
        self.assertIsNone(kpis["recall_proxy"])
        self.assertEqual(kpis["recall_proxy_reason"], "no_recognized_snapshot")

    def test_is_gainer_source(self):
        self.assertTrue(is_gainer_source("gainer_live_heat"))
        self.assertTrue(is_gainer_source("gate_prev_top"))
        self.assertFalse(is_gainer_source("grid"))


class TestReportBuilder(unittest.TestCase):
    def test_build_and_write_report(self):
        tickers = {
            "GOOD/USDT": {"percentage": 25.0, "quoteVolume": 1_500_000, "last": 1.2},
            "DUST/USDT": {"percentage": 80.0, "quoteVolume": 50_000, "last": 0.000001},
        }
        fills = [
            {
                "symbol": "GOOD/USDT",
                "side": "buy",
                "status": "filled",
                "source": "grid",
            }
        ]
        report = build_report(
            day_key="2026-08-04",
            top_n=20,
            scope="demo",
            tenant_id=None,
            tickers=tickers,
            fills=fills,
            mongo_meta={"mongo": "file", "n": 1},
            recognized={"GOOD/USDT"},
            recognized_source="test",
        )
        self.assertEqual(report["day_key"], "2026-08-04")
        self.assertEqual(len(report["leaders"]), 2)
        self.assertIn("kpis", report)
        self.assertFalse(report["kpis"]["rules"]["min_price_filter"])
        md = render_markdown(report)
        self.assertIn("GIS Monitor 2026-08-04", md)
        self.assertIn("min_price_filter: **false**", md)

        with tempfile.TemporaryDirectory() as td:
            jp, mp = write_report(report, Path(td))
            self.assertTrue(jp.exists())
            self.assertTrue(mp.exists())
            data = json.loads(jp.read_text())
            self.assertEqual(data["kpis"]["n_leaders"], 2)
            dust = next(L for L in data["leaders"] if L["symbol"] == "DUST/USDT")
            self.assertFalse(dust["eligible"])
            self.assertEqual(dust["reject_reason"], "low_volume")


class TestParseDay(unittest.TestCase):
    def test_parse_iso(self):
        self.assertEqual(parse_day_arg("2026-08-01"), "2026-08-01")


if __name__ == "__main__":
    unittest.main()
