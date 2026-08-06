"""Tests for personal entry recipes — call shipped modules only."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from strategies.entry_recipe import (
    PRIMARY_METRIC,
    STRATEGY_PROFILE_PERSONAL,
    add_indicators,
    build_personal_profile_payload,
    build_symbol_universe,
    compare_cohort,
    merge_personal_over_tier,
    normalize_personal_params,
    preserve_buy_params,
    renew_symbol_params,
    score_entry_params_on_df,
    select_best_params,
    tier_default_buy_params,
)


def _synth_df(n: int = 120, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rets = rng.normal(0, 0.01, size=n)
    price = 50 * np.exp(np.cumsum(rets))
    for i in range(25, n, 35):
        price[i : i + 4] *= 0.94
    vol = rng.uniform(800, 3000, size=n)
    vol[::11] *= 2.5
    return pd.DataFrame(
        {
            "open": price,
            "high": price * 1.01,
            "low": price * 0.99,
            "close": price,
            "volume": vol,
        }
    )


class TestUniverse(unittest.TestCase):
    def test_build_symbol_universe_union(self):
        symbols = build_symbol_universe(
            watchlist=[{"symbol": "btc/usdt"}, {"symbol": "ETH/USDT"}],
            open_positions=[{"symbol": "SOL/USDT", "amount": 1}],
            orders=[
                {"symbol": "DOGE/USDT", "status": "filled"},
                {"symbol": "ETH/USDT", "status": "filled"},
                {"symbol": "XXX/USDT", "status": "rejected"},
            ],
        )
        self.assertEqual(symbols[0], "BTC/USDT")
        self.assertIn("ETH/USDT", symbols)
        self.assertIn("SOL/USDT", symbols)
        self.assertIn("DOGE/USDT", symbols)
        self.assertNotIn("XXX/USDT", symbols)
        self.assertEqual(len(symbols), len(set(symbols)))


class TestParams(unittest.TestCase):
    def test_normalize_clamps_rsi_band(self):
        p = normalize_personal_params(
            {"rsi_buy_low": 5, "rsi_buy_high": 90, "volume_multiplier": 0.5}
        )
        self.assertGreaterEqual(p["rsi_buy_low"], 15)
        self.assertLessEqual(p["rsi_buy_high"], 60)
        self.assertGreater(p["rsi_buy_high"], p["rsi_buy_low"])
        self.assertGreaterEqual(p["volume_multiplier"], 1.0)
        self.assertIn(p["buy_regime"], ("dip", "reversal", "both"))

    def test_merge_personal_wins_over_tier(self):
        personal = {
            "rsi_buy_low": 26,
            "rsi_buy_high": 44,
            "volume_multiplier": 1.4,
            "buy_regime": "dip",
        }
        tier = tier_default_buy_params("volatile")
        merged = merge_personal_over_tier(personal, tier, tier="volatile")
        self.assertEqual(merged["rsi_buy_low"], 26)
        self.assertEqual(merged["rsi_buy_high"], 44)
        self.assertEqual(merged["volume_multiplier"], 1.4)
        self.assertEqual(merged["buy_regime"], "dip")

    def test_preserve_buy_params_after_overlay(self):
        preferred = {"rsi_buy_low": 22, "rsi_buy_high": 40, "buy_regime": "dip"}
        base = {"rsi_buy_low": 28, "rsi_buy_high": 48, "buy_regime": "both", "x": 1}
        out = preserve_buy_params(base, preferred)
        self.assertEqual(out["rsi_buy_low"], 22)
        self.assertEqual(out["buy_regime"], "dip")
        self.assertEqual(out["x"], 1)


class TestScoreAndSelect(unittest.TestCase):
    def test_score_and_select_on_synthetic(self):
        df = add_indicators(_synth_df())
        self.assertGreaterEqual(len(df), 40)
        baseline = tier_default_buy_params("volatile")
        sc = score_entry_params_on_df(df, baseline)
        self.assertEqual(sc.params["buy_regime"], baseline["buy_regime"])
        self.assertGreaterEqual(sc.bars, 40)
        # total_return is finite
        self.assertTrue(np.isfinite(sc.total_return_pct))

        best, psc, bsc, reason = select_best_params(df, tier="volatile", min_trades=1)
        self.assertIn("rsi_buy_low", best)
        self.assertTrue(np.isfinite(psc.total_return_pct))
        self.assertTrue(np.isfinite(bsc.total_return_pct))
        # either personal or documented fallback
        if reason:
            self.assertIsInstance(reason, str)

    def test_renew_persists_via_hermes_store(self):
        df = add_indicators(_synth_df(seed=99))
        with tempfile.TemporaryDirectory() as td:
            mem_dir = Path(td)
            with patch("hermes.memory.store.MEMORY_DIR", mem_dir):
                with patch.dict(os.environ, {"DEMO_MODE": "1"}):
                    # force demo suffix path under temp via DEMO_MODE + MEMORY_DIR
                    rr = renew_symbol_params(
                        "TEST1/USDT",
                        df,
                        timeframe="1h",
                        tier="volatile",
                        persist=True,
                    )
                    self.assertTrue(rr.persisted)
                    from hermes.memory import store

                    # re-patch for load
                    with patch("hermes.memory.store.MEMORY_DIR", mem_dir):
                        profile = store.load_profile("TEST1/USDT", "1h")
                    params = profile.get("params") or {}
                    self.assertIn("rsi_buy_low", params)
                    self.assertEqual(
                        params.get("strategy_profile"), STRATEGY_PROFILE_PERSONAL
                    )
                    self.assertTrue(params.get("personal_entry_renewed_at"))

    def test_compare_cohort_primary_metric(self):
        from strategies.entry_recipe import RenewalResult

        rows = [
            RenewalResult("A/USDT", "1h", {}, 5.0, 1.0, "", True),
            RenewalResult("B/USDT", "1h", {}, 2.0, 2.0, "min_trades:0<2", True),
        ]
        summary = compare_cohort(rows)
        self.assertEqual(summary["primary_metric"], PRIMARY_METRIC)
        self.assertEqual(summary["n_symbols"], 2)
        # A uses 5.0, B fallback uses baseline 2.0 → mean 3.5
        self.assertAlmostEqual(summary["personal_mean_total_return_pct"], 3.5)
        self.assertAlmostEqual(summary["baseline_mean_total_return_pct"], 1.5)
        self.assertTrue(summary["equal_or_better"])


class TestRegistryWiresPersonal(unittest.TestCase):
    def test_resolve_prefers_personal_buy_keys(self):
        from strategies.registry import resolve_strategy_params

        personal = normalize_personal_params(
            {
                "rsi_buy_low": 24,
                "rsi_buy_high": 41,
                "volume_multiplier": 1.45,
                "buy_regime": "dip",
                "strategy_profile": STRATEGY_PROFILE_PERSONAL,
                "personal_entry_renewed_at": "2026-08-06T00:00:00+00:00",
            }
        )

        with patch(
            "strategies.registry._hermes_memory_params",
            return_value={**personal, "symbol": "AAA/USDT", "timeframe": "1h"},
        ):
            coin = {"symbol": "AAA/USDT", "timeframe": "1h"}
            params = resolve_strategy_params(coin, has_position=False, atr_pct=5.0)
        self.assertEqual(params["rsi_buy_low"], 24)
        self.assertEqual(params["rsi_buy_high"], 41)
        self.assertEqual(params["volume_multiplier"], 1.45)
        self.assertEqual(params["buy_regime"], "dip")

    def test_personal_beats_config_strategies_entry_on_4h(self):
        """Live path: config.strategies[] must not hide personal_entry_v1 buy keys."""
        from strategies.registry import resolve_strategy_params

        personal = {
            "rsi_buy_low": 21,
            "rsi_buy_high": 39,
            "volume_multiplier": 1.77,
            "buy_regime": "dip",
            "strategy_profile": STRATEGY_PROFILE_PERSONAL,
            "personal_entry_renewed_at": "2026-08-06T12:00:00+00:00",
            "reversal_rsi_cross_low": 32,
            "reversal_rsi_cross_high": 38,
            "reversal_volume_multiplier": 1.0,
        }
        explicit = {
            "symbol": "ARIA/USDT",
            "timeframe": "4h",
            "rsi_buy_low": 25,
            "rsi_buy_high": 55,
            "volume_multiplier": 0.85,
            "buy_regime": "both",
            "strategy_class": "technical_rsi_bb",
        }
        with patch(
            "strategies.registry._hermes_memory_params",
            return_value={**personal, "symbol": "ARIA/USDT", "timeframe": "4h"},
        ):
            with patch(
                "strategies.registry._explicit_strategy_entry",
                return_value=explicit,
            ):
                params = resolve_strategy_params(
                    {"symbol": "ARIA/USDT", "timeframe": "4h"},
                    has_position=False,
                    atr_pct=3.0,
                )
        self.assertEqual(params["rsi_buy_low"], 21)
        self.assertEqual(params["rsi_buy_high"], 39)
        self.assertEqual(params["volume_multiplier"], 1.77)
        self.assertEqual(params["buy_regime"], "dip")
        self.assertEqual(params.get("strategy_profile"), STRATEGY_PROFILE_PERSONAL)


class TestGainerEntryRemainsOff(unittest.TestCase):
    def test_default_disabled(self):
        from services.gainer_signal.bot_http import (
            gainer_entry_enabled,
            process_gainer_signal,
        )
        from unittest.mock import MagicMock

        os.environ.pop("GAINER_ENTRY_ENABLED", None)
        self.assertFalse(gainer_entry_enabled({}))
        self.assertFalse(gainer_entry_enabled({"gainer_entry": {"enabled": False}}))
        buy = MagicMock()
        body, status = process_gainer_signal(
            {
                "symbol": "KOMA/USDT",
                "last": 0.01,
                "quote_vol": 2e6,
                "eligible": True,
                "rank": 1,
                "pct_24h": 15,
            },
            config={"gainer_entry": {"enabled": False}},
            positions=[],
            gainer_buys_today=0,
            execute_buy=buy,
        )
        self.assertEqual(status, 503)
        self.assertEqual(body["message"], "gainer_entry_disabled")
        buy.assert_not_called()


class TestProfilePayload(unittest.TestCase):
    def test_payload_marks_personal(self):
        df = add_indicators(_synth_df(seed=1))
        sc = score_entry_params_on_df(df, tier_default_buy_params())
        payload = build_personal_profile_payload(
            "Z/USDT", "1h", sc.params, sc, fallback_reason=""
        )
        self.assertEqual(
            payload["params"]["strategy_profile"], STRATEGY_PROFILE_PERSONAL
        )
        self.assertIn("personal_entry_renewed_at", payload["params"])


if __name__ == "__main__":
    unittest.main()
