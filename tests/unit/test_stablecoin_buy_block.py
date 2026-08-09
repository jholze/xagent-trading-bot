"""Stablecoin buy block — permanent rail (not volatility_tier stable)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from core.coin_eligibility import passes_coin_filters
from core.stablecoins import (
    is_stablecoin_base,
    is_stablecoin_symbol,
    stablecoin_buys_blocked,
)


class TestStablecoinDetect(unittest.TestCase):
    def test_known_bases(self):
        for b in ("GUSD", "USDP", "USDC", "DAI", "FDUSD", "PYUSD"):
            self.assertTrue(is_stablecoin_base(b), b)

    def test_not_vol_stable_alts(self):
        for b in ("BTC", "ETH", "SOL", "PEPE", "NEAR", "SUSHI", "XAUT", "PAXG"):
            self.assertFalse(is_stablecoin_base(b), b)

    def test_symbol_forms(self):
        self.assertTrue(is_stablecoin_symbol("GUSD/USDT"))
        self.assertTrue(is_stablecoin_symbol("USDP/USDT"))
        self.assertTrue(is_stablecoin_symbol("gusd_usdt_1h"))
        self.assertFalse(is_stablecoin_symbol("BTC/USDT"))
        self.assertFalse(is_stablecoin_symbol("LAB/USDT"))

    def test_kill_switch(self):
        self.assertTrue(stablecoin_buys_blocked({}))
        self.assertTrue(stablecoin_buys_blocked({"risk": {}}))
        self.assertFalse(stablecoin_buys_blocked({"risk": {"block_stablecoin_buys": False}}))
        self.assertTrue(stablecoin_buys_blocked({"risk": {"block_stablecoin_buys": True}}))


class TestEligibilityRail(unittest.TestCase):
    def test_blocks_gusd_even_if_filters_disabled(self):
        cfg = {"coin_filters": {"enabled": False}, "risk": {"block_stablecoin_buys": True}}
        ok, reason = passes_coin_filters(
            {"symbol": "GUSD/USDT"}, None, cfg, context="buy"
        )
        self.assertFalse(ok)
        self.assertIn("stablecoin", reason.lower())

    def test_allows_normal_alt(self):
        cfg = {"coin_filters": {"enabled": False}, "risk": {"block_stablecoin_buys": True}}
        ok, reason = passes_coin_filters(
            {"symbol": "NEAR/USDT"}, None, cfg, context="buy"
        )
        self.assertTrue(ok)

    def test_kill_allows_stablecoin_buy_in_filters(self):
        cfg = {"coin_filters": {"enabled": False}, "risk": {"block_stablecoin_buys": False}}
        ok, _ = passes_coin_filters({"symbol": "USDP/USDT"}, None, cfg, context="buy")
        self.assertTrue(ok)


class TestRiskManagerRail(unittest.TestCase):
    def test_risk_rejects_stablecoin_buy(self):
        from core.models import TradeOrder
        from risk.risk_manager import RiskManager

        cfg = MagicMock()
        cfg.raw = {"risk": {"block_stablecoin_buys": True}}
        cfg.risk_config = {}
        for attr in (
            "max_usdt_per_trade",
            "max_open_positions",
            "trade_cooldown_hours",
        ):
            setattr(cfg, attr, 100)

        rm = RiskManager(cfg)
        order = TradeOrder(
            type="BUY",
            symbol="GUSD/USDT",
            amount=100.0,
            price=1.0,
            usdt_amount=100.0,
            source="auto",
        )
        with patch.object(rm, "_trade_cooldown_blocked", return_value=(False, "")):
            decision = rm.evaluate(order, timeframe="1h", source="auto")
        self.assertFalse(decision.approved)
        self.assertEqual(getattr(decision, "code", None) or "", "stablecoin_blocked")


if __name__ == "__main__":
    unittest.main()
