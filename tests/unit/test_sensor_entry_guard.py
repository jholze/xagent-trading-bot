"""Sensor-entry-guard (V1+M1+M2): venue, hold_override, size, memory — BDX counterfactual.

Drives shipped production functions with BDX-like fixtures.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from intelligence.memory.models import TradeMemory
from intelligence.memory.rebuild import compute_profile_from_trades, orders_to_trade_memories
from services.venue_quality import (
    VenueMetrics,
    check_venue_for_buy,
    evaluate_venue_quality,
    reset_venue_cache_for_tests,
    stamp_venue_for_fill,
    venue_quality_config,
)
from strategies.entry_sensor_15m import evaluate_entry_sensor_15m
from strategies.sensor_entry_policy import (
    apply_sensor_hold_policy,
    resolve_sensor_usdt,
    should_block_hold_override,
)
from strategies.trading_modes import MODE_GRID, MODE_HYBRID, MODE_MOMENTUM, entry_sensor_buy_usdt_frac


# --- BDX-like thin Gate book (from live ticker audit 2026-07-18) ---
BDX_THIN = VenueMetrics(
    symbol="BDX/USDT",
    quote_volume_24h_usdt=2118.0,
    base_volume_24h=11694.0,
    last=0.16589,
    bid=0.12335,
    ask=0.16589,
    bid_size=170.0,
    ask_size=86.0,
    spread_pct=29.4,
    top_book_bid_usdt=20.97,
    top_book_ask_usdt=14.26,
    exchange="gate",
    capture="ok",
)

BTC_THICK = VenueMetrics(
    symbol="BTC/USDT",
    quote_volume_24h_usdt=500_000_000.0,
    base_volume_24h=5000.0,
    last=65000.0,
    bid=64990.0,
    ask=65010.0,
    bid_size=2.0,
    ask_size=2.0,
    spread_pct=0.03,
    top_book_bid_usdt=129980.0,
    top_book_ask_usdt=130020.0,
    exchange="gate",
    capture="ok",
)

VENUE_CFG = {
    "enabled": True,
    "min_quote_volume_24h_usdt": 50_000,
    "max_spread_pct": 1.5,
    "min_top_book_usdt_per_side": 200,
    "min_volume_to_order_multiple": 20,
    "apply_to": ["entry_sensor_15m", "vol_spike_15m", "grid_new_entry"],
    "on_fetch_error": "block_sensor",
}


class TestVenueQuality(unittest.TestCase):
    def setUp(self):
        reset_venue_cache_for_tests()

    def test_bdx_thin_fails(self):
        r = evaluate_venue_quality(BDX_THIN, VENUE_CFG, planned_usdt=3125.0)
        self.assertFalse(r.ok, r.reasons)
        self.assertTrue(any("quote_vol" in x or "spread" in x for x in r.reasons))

    def test_btc_thick_passes(self):
        r = evaluate_venue_quality(BTC_THICK, VENUE_CFG, planned_usdt=3125.0)
        self.assertTrue(r.ok, r.reasons)

    def test_check_venue_for_buy_blocks_sensor_on_thin(self):
        r = check_venue_for_buy(
            "BDX/USDT",
            source="entry_sensor_15m",
            planned_usdt=3125.0,
            config_raw={"risk": {"venue_quality": VENUE_CFG}},
            metrics=BDX_THIN,
        )
        self.assertFalse(r.ok)

    def test_sell_source_not_in_apply_still_ok_when_disabled_path(self):
        # venue only applies to configured sources — grid sell isn't buy path
        r = check_venue_for_buy(
            "BDX/USDT",
            source="manual_sell_not_listed",
            planned_usdt=0,
            config_raw={"risk": {"venue_quality": VENUE_CFG}},
            metrics=BDX_THIN,
        )
        self.assertTrue(r.ok)
        self.assertIn("source_not_in_apply_to", r.reasons)

    def test_stamp_missing_on_missing_metrics(self):
        stamp = stamp_venue_for_fill(
            "ZZZ/USDT",
            planned_usdt=1000,
            metrics=VenueMetrics(symbol="ZZZ/USDT", capture="missing"),
        )
        self.assertEqual(stamp.get("capture"), "missing")


class TestSensorHoldOverrideAndSize(unittest.TestCase):
    def test_momentum_blocks_hold_override(self):
        block, reason = should_block_hold_override(
            tech_is_hold=True,
            trading_mode=MODE_MOMENTUM,
            cfg={"hold_override_by_mode": {"MOMENTUM": "block"}},
        )
        self.assertTrue(block)
        self.assertIn("block", reason)

    def test_grid_slice_only_allows_hold_sensor(self):
        block, _ = should_block_hold_override(
            tech_is_hold=True,
            trading_mode=MODE_GRID,
            cfg={"hold_override_by_mode": {"GRID": "slice_only"}},
        )
        self.assertFalse(block)

    def test_hybrid_not_full_size(self):
        frac = entry_sensor_buy_usdt_frac(MODE_HYBRID, volatility_tier="stable")
        self.assertLess(frac, 1.0)
        self.assertAlmostEqual(frac, 0.40)
        usdt = resolve_sensor_usdt(
            MODE_HYBRID,
            volatility_tier="stable",
            max_usdt_per_trade=2500,
            cfg={"max_usdt_absolute": 1000},
        )
        self.assertLessEqual(usdt, 1000)
        self.assertAlmostEqual(usdt, 1000.0)  # 0.4*2500=1000

    def test_momentum_size_capped_not_full(self):
        usdt = resolve_sensor_usdt(
            MODE_MOMENTUM,
            max_usdt_per_trade=2500,
            cfg={
                "max_usdt_absolute": 1000,
                "size_hint_by_mode": {"MOMENTUM": {"default": 0.30}},
            },
        )
        self.assertLessEqual(usdt, 1000)
        self.assertAlmostEqual(usdt, 750.0)  # 0.3 * 2500

    def test_apply_policy_blocks_momentum_hold(self):
        action, reason = apply_sensor_hold_policy(
            tech_normalized="HOLD",
            trading_mode=MODE_MOMENTUM,
            cfg={"hold_override_by_mode": {"MOMENTUM": "block"}},
            sensor_action="BUY",
            tech_already_buy=False,
        )
        self.assertIsNone(action)
        self.assertIn("block", reason)

    def test_evaluate_sensor_venue_block(self):
        metrics = {
            "volume_spike_ratio": 3.5,
            "body_atr_ratio": 0.5,
            "price_momentum": True,
        }
        r = evaluate_entry_sensor_15m(
            watched=True,
            metrics=metrics,
            cfg={"enabled": True, "mode": "active", "vol_spike_mult": 2.0},
            rsi_4h=40.0,
            venue_ok=False,
            venue_reason="quote_vol thin",
        )
        self.assertFalse(r.triggered)
        self.assertIn("thin", r.rationale.lower() or "quote")


class TestMemoryGrossLossAndVenue(unittest.TestCase):
    def test_single_gross_loss_soft_block(self):
        trades = [
            TradeMemory(
                trade_id="b1",
                symbol="BDX/USDT",
                direction="buy",
                entry_price=0.25617,
                source="entry_sensor_15m",
                entry_time="2026-07-16T15:50:26Z",
                metadata={
                    "usdt": 3125,
                    "venue": {
                        "quote_volume_24h_usdt": 2118,
                        "spread_pct": 30.0,
                        "venue_ok": False,
                        "planned_usdt": 3125,
                        "capture": "ok",
                    },
                },
            ),
            TradeMemory(
                trade_id="s1",
                symbol="BDX/USDT",
                direction="sell",
                entry_price=0.2546,
                exit_price=0.1222,
                pnl_usdt=-1768.11,
                source="auto",
                outcome="loss",
                entry_time="2026-07-18T11:42:26Z",
                exit_time="2026-07-18T11:42:26Z",
                metadata={"usdt": 1607},
            ),
        ]
        prof = compute_profile_from_trades(
            "BDX/USDT",
            trades,
            ledger_scope="demo",
            tenant_id="default",
            min_samples=3,
            config_raw={
                "memory": {
                    "gross_loss": {
                        "enabled": True,
                        "min_loss_pct": 25,
                        "min_loss_usdt": 500,
                        "size_bias_cap": 0.5,
                        "soft_block_scope": "sensor_only",
                        "soft_block_ttl_hours": 336,
                    }
                },
                "risk": {"venue_quality": VENUE_CFG},
            },
        )
        self.assertEqual(prof.entry_bias, "soft_block")
        self.assertLessEqual(prof.size_bias, 0.5)
        self.assertIn("gross_loss", prof.rationale)
        self.assertEqual(prof.features.get("soft_block_scope"), "sensor_only")
        self.assertIn("by_source", prof.features)
        self.assertIn("venue", prof.features)
        self.assertGreaterEqual(prof.features["venue"].get("entries_thin_30d", 0), 1)

    def test_orders_to_trade_memories_copies_venue(self):
        orders = [
            {
                "id": "x1",
                "status": "filled",
                "side": "buy",
                "symbol": "BDX/USDT",
                "source": "entry_sensor_15m",
                "tenant_id": "default",
                "timestamps": {"filled": "2026-07-16T15:50:26"},
                "execution": {
                    "price": 0.25,
                    "usdt": 1000,
                    "venue": {"quote_volume_24h_usdt": 2000, "capture": "ok"},
                },
                "request": {},
            }
        ]
        trades = orders_to_trade_memories(
            orders, ledger_scope="demo", tenant_id="default", lookback_days=365
        )
        self.assertEqual(len(trades), 1)
        self.assertIn("venue", trades[0].metadata)


class TestRiskVenueAndSoftBlockScope(unittest.TestCase):
    def test_risk_blocks_thin_sensor_buy(self):
        from core.models import TradeOrder
        from risk.risk_manager import RiskManager

        rm = RiskManager(config=MagicMock())
        rm.config.raw = {
            "risk": {"venue_quality": VENUE_CFG, "cash_floor_pct": 0},
            "memory": {"gross_loss": {"soft_block_scope": "sensor_only"}},
        }
        rm.config.max_open_positions = 50
        rm.config.max_position_percent = 100
        rm.config.max_usdt_per_trade = 2500
        rm.config.risk_config = rm.config.raw["risk"]
        rm.config.aggression_config = {}
        rm.config.entry_sensor_15m_config = {
            "ignore_aggression_boost": True,
            "max_usdt_absolute": 1000,
        }
        order = TradeOrder(
            type="BUY",
            symbol="BDX/USDT",
            price=0.25,
            amount=0,
            usdt_amount=3125,
            signal="BUY",
            source="entry_sensor_15m",
        )
        with patch("risk.risk_manager.get_position", return_value={"amount": 0}), patch(
            "risk.risk_manager.count_open_full_slots", return_value=0
        ), patch(
            "risk.risk_manager.count_open_positions", return_value=0
        ), patch(
            "services.venue_quality.get_venue_metrics", return_value=BDX_THIN
        ), patch.object(rm, "_cash_floor_blocked", return_value=None), patch.object(
            rm, "_daily_buy_limit_blocked", return_value=None
        ), patch.object(rm, "_trade_cooldown_blocked", return_value=(False, "")), patch.object(
            rm, "_is_dca_buy", return_value=False
        ):
            dec = rm.evaluate(order, "1h", source="entry_sensor_15m")
        self.assertFalse(dec.approved)
        self.assertEqual(dec.code, "venue_liquidity_block")

    def test_risk_sell_not_venue_gated(self):
        from core.models import TradeOrder
        from risk.risk_manager import RiskManager

        rm = RiskManager(config=MagicMock())
        rm.config.raw = {"risk": {"venue_quality": VENUE_CFG}}
        rm.config.risk_config = {}
        order = TradeOrder(
            type="SELL",
            symbol="BDX/USDT",
            price=0.12,
            amount=100,
            signal="SELL_FULL",
            source="auto",
        )
        with patch.object(rm, "_trade_cooldown_blocked", return_value=(False, "")), patch.object(
            rm, "_resolve_sell_order", return_value=order
        ), patch.object(rm, "_partial_sell_blocked", return_value=(False, "")), patch.object(
            rm, "_effective_max_daily_sells", return_value=0
        ), patch.object(rm, "_daily_sells_count", return_value=0):
            dec = rm.evaluate(order, "1h", source="auto")
        self.assertTrue(dec.approved)
        self.assertNotEqual(dec.code, "venue_liquidity_block")

    def test_soft_block_sensor_only_allows_grid(self):
        from core.models import TradeOrder
        from risk.risk_manager import RiskManager
        from intelligence.memory.models import CoinProfile

        rm = RiskManager(config=MagicMock())
        rm.config.raw = {
            "risk": {"venue_quality": {"enabled": False}, "cash_floor_pct": 0},
            "memory": {"gross_loss": {"soft_block_scope": "sensor_only"}},
        }
        rm.config.max_open_positions = 50
        rm.config.max_position_percent = 100
        rm.config.max_usdt_per_trade = 500
        rm.config.risk_config = {"min_trade_usdt": 1, "min_size_multiplier": 0.1}
        rm.config.aggression_config = {}
        rm.config.entry_sensor_15m_config = {}
        prof = CoinProfile(
            symbol="BDX/USDT",
            entry_bias="soft_block",
            size_bias=0.5,
            rationale="gross_loss",
            features={"soft_block_scope": "sensor_only"},
        )
        order = TradeOrder(
            type="BUY", symbol="BDX/USDT", price=0.2, amount=0, usdt_amount=200, signal="BUY", source="grid"
        )
        with patch("risk.risk_manager.get_position", return_value={"amount": 0}), patch(
            "risk.risk_manager.count_open_full_slots", return_value=0
        ), patch(
            "intelligence.memory.cache.get_entry_bias", return_value="soft_block"
        ), patch(
            "intelligence.memory.cache.get_coin_profile", return_value=prof
        ), patch.object(rm, "_cash_floor_blocked", return_value=None), patch.object(
            rm, "_daily_buy_limit_blocked", return_value=None
        ), patch.object(rm, "_trade_cooldown_blocked", return_value=(False, "")), patch.object(
            rm, "_is_dca_buy", return_value=False
        ), patch.object(rm, "_dynamic_size", return_value=(200.0, {"total_multiplier": 1.0})), patch.object(
            rm, "_portfolio_equity", return_value=100_000
        ), patch.object(rm, "_available_usdt", return_value=50_000):
            dec = rm.evaluate(order, "4h", source="grid")
        # grid should not be hard-blocked by sensor_only soft_block
        self.assertNotEqual(dec.code, "coin_memory_soft_block")


class TestBdxCounterfactualEndToEnd(unittest.TestCase):
    """Single narrative: BDX entry would be stopped by shipped gates."""

    def test_bdx_full_story(self):
        # 1) Venue kills entry
        v = evaluate_venue_quality(BDX_THIN, VENUE_CFG, planned_usdt=3125)
        self.assertFalse(v.ok)

        # 2) MOMENTUM hold override kills TA-HOLD sensor buy
        action, reason = apply_sensor_hold_policy(
            tech_normalized="HOLD",
            trading_mode=MODE_MOMENTUM,
            cfg={"hold_override_by_mode": {"MOMENTUM": "block"}},
            sensor_action="BUY",
            tech_already_buy=False,
        )
        self.assertIsNone(action)

        # 3) Even if override allowed, size would be capped << 3125
        usdt = resolve_sensor_usdt(
            MODE_MOMENTUM,
            max_usdt_per_trade=2500,
            cfg={"max_usdt_absolute": 1000, "size_hint_by_mode": {"MOMENTUM": {"default": 0.3}}},
        )
        self.assertLessEqual(usdt, 1000)
        self.assertLess(usdt, 3125)

        # 4) After −52% sell, memory soft_blocks sensor re-entry
        trades = [
            TradeMemory(
                trade_id="1",
                symbol="BDX/USDT",
                direction="buy",
                source="entry_sensor_15m",
                entry_price=0.25617,
                entry_time="2026-07-16T15:50:26Z",
                metadata={
                    "venue": {
                        "quote_volume_24h_usdt": 2118,
                        "spread_pct": 30,
                        "venue_ok": False,
                        "planned_usdt": 3125,
                        "capture": "ok",
                    }
                },
            ),
            TradeMemory(
                trade_id="2",
                symbol="BDX/USDT",
                direction="sell",
                source="auto",
                pnl_usdt=-1768.11,
                entry_price=0.2546,
                exit_price=0.1222,
                entry_time="2026-07-18T11:42:26Z",
                exit_time="2026-07-18T11:42:26Z",
            ),
        ]
        prof = compute_profile_from_trades(
            "BDX/USDT",
            trades,
            ledger_scope="demo",
            tenant_id="default",
            min_samples=3,
            config_raw={
                "memory": {
                    "gross_loss": {
                        "enabled": True,
                        "min_loss_pct": 25,
                        "min_loss_usdt": 500,
                        "size_bias_cap": 0.5,
                        "soft_block_scope": "sensor_only",
                        "soft_block_ttl_hours": 336,
                    }
                },
                "risk": {"venue_quality": VENUE_CFG},
            },
        )
        self.assertEqual(prof.entry_bias, "soft_block")
        # Document gates that fire
        self.assertTrue(
            (not v.ok) or (action is None) or (usdt < 3125 and prof.entry_bias == "soft_block")
        )


if __name__ == "__main__":
    unittest.main()
