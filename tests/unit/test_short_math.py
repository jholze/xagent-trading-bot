"""Isolated short math — PnL, margin, stop-before-liq."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from strategies.short_math import (
    SIDE_SHORT,
    apply_liq_buffer,
    clamp_leverage,
    liquidation_price_isolated,
    margin_usdt,
    roe_pct,
    should_stop_or_liquidate,
    snapshot,
    stop_price,
    unrealized_pnl,
)
from strategies.short_policy import resolve_short_params

_REPO_ROOT = Path(__file__).resolve().parents[2]


class TestShortMath(unittest.TestCase):
    def test_short_pnl_falls_is_profit(self):
        self.assertAlmostEqual(unrealized_pnl("short", 10, 2.0, 1.5), 5.0)
        self.assertAlmostEqual(unrealized_pnl("long", 10, 2.0, 1.5), -5.0)

    def test_margin_is_notional_over_leverage(self):
        self.assertAlmostEqual(margin_usdt(10, 2.0, 2.0), 10.0)

    def test_leverage_clamped(self):
        self.assertEqual(clamp_leverage(99, cap=5), 5.0)
        self.assertEqual(clamp_leverage(0.2, cap=5), 1.0)

    def test_default_cap_is_two(self):
        self.assertEqual(clamp_leverage(5), 2.0)
        self.assertEqual(clamp_leverage(1.5), 1.5)
        self.assertGreaterEqual(clamp_leverage(0), 1.0)
        self.assertEqual(clamp_leverage(0), 1.0)

    def test_snapshot_never_reports_leverage_above_two(self):
        for lev in (2.0, 2.5, 5.0, 99.0, 0, None):
            pos = {"side": "short", "amount": 10, "average_entry": 2.0}
            if lev is not None:
                pos["leverage"] = lev
            snap = snapshot(pos, mark=1.8)
            self.assertLessEqual(float(snap["leverage"]), 2.0)
            self.assertGreaterEqual(float(snap["leverage"]), 1.0)

    def test_resolve_short_params_leverage_capped_for_every_tier_and_coin(self):
        with (_REPO_ROOT / "config.json").open() as fh:
            config_raw = json.load(fh)
        shorts = config_raw.get("shorts") if isinstance(config_raw.get("shorts"), dict) else {}
        coins = shorts.get("coins") if isinstance(shorts.get("coins"), dict) else {}
        symbols = [None, *list(coins.keys())]
        for tier in ("volatile", "mid", "stable"):
            for symbol in symbols:
                params = resolve_short_params(
                    symbol=symbol, tier=tier, config_raw=config_raw
                )
                self.assertLessEqual(
                    float(params["leverage"]),
                    2.0,
                    msg=f"leverage {params['leverage']} > 2 for tier={tier} symbol={symbol}",
                )

    def test_short_liq_above_entry(self):
        liq = liquidation_price_isolated("short", 100.0, 2.0)
        self.assertGreater(liq, 100.0)
        self.assertLess(liq, 160.0)

    def test_stop_is_margin_risk_not_full_price(self):
        # 10% of margin @ 2x → 5% price
        sp = stop_price("short", 100.0, 0.10, 2.0)
        self.assertAlmostEqual(sp, 105.0)

    def test_stop_fires_before_liq(self):
        entry = 100.0
        lev = 2.0
        stop = stop_price(SIDE_SHORT, entry, 0.12, lev)
        liq = liquidation_price_isolated(SIDE_SHORT, entry, lev)
        buffered = apply_liq_buffer(SIDE_SHORT, entry, liq, 0.05)
        self.assertLess(stop, liq)
        self.assertLessEqual(buffered, liq)
        self.assertEqual(
            should_stop_or_liquidate(SIDE_SHORT, stop, stop=stop, liq=liq),
            "stop",
        )
        self.assertEqual(
            should_stop_or_liquidate(SIDE_SHORT, liq, stop=stop, liq=liq),
            "liquidation",
        )

    def test_snapshot_short(self):
        snap = snapshot(
            {"side": "short", "amount": 10, "average_entry": 2.0, "leverage": 2},
            mark=1.8,
        )
        self.assertEqual(snap["side"], "short")
        self.assertAlmostEqual(float(snap["pnl"]), 2.0)
        self.assertAlmostEqual(float(snap["margin"]), 10.0)
        self.assertGreater(float(snap["roe_pct"]), 0)

    def test_roe(self):
        self.assertAlmostEqual(roe_pct(5, 10), 50.0)

    def test_funding_scales_with_hours(self):
        from strategies.short_math import funding_cost_usdt

        eight = funding_cost_usdt(10_000, 8, 0.0001)
        self.assertAlmostEqual(eight, 1.0)
        self.assertAlmostEqual(funding_cost_usdt(10_000, 4, 0.0001), 0.5)
