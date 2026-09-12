"""#367: shadow SHORT/COVER must not hit shorts_live_blocked when allow_live=false.

The live kill switch still blocks resolved-real execution. Shadow is the extra
clause: the gate follows resolve_execution_mode, not the deprecated live.dry_run
alias that is_real_live_trading keys off. A live+execution=shadow config with
dry_run false (or omitted) must reach sizing, not this gate.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from core.config import BotConfig
from core.models import TradeOrder
from risk.risk_manager import RiskManager
from strategies.positions import clear_positions_memory, update_position


_LIVE_BLOCK_MSG = "shorts.allow_live=false (no Gate futures in v0)"
_GATE_CREDS = {"GATE_API_KEY": "test-key", "GATE_API_SECRET": "test-secret"}


def _live_cfg(execution: str, *, dry_run: bool = False) -> dict:
    return {
        "trading_mode": "live",
        "live_confirmed": True,
        "max_usdt_per_trade": 150,
        "live": {
            "execution": execution,
            "dry_run": dry_run,
            "api_key_env": "GATE_API_KEY",
            "api_secret_env": "GATE_API_SECRET",
        },
        "shorts": {
            "enabled": True,
            "allow_live": False,
            "leverage_default": 2,
            "leverage_cap": 2,
            "max_open": 6,
            "max_margin_pct": 20,
            "volatile": {"market_cap_min_usd": 0},
        },
        "risk": {},
    }


def _manager(execution: str, *, dry_run: bool = False) -> RiskManager:
    return RiskManager(config=BotConfig(_live_cfg(execution, dry_run=dry_run)))


class TestRiskShortGateShadow(unittest.TestCase):
    def setUp(self):
        clear_positions_memory()

    def tearDown(self):
        clear_positions_memory()

    def test_shadow_short_not_shorts_live_blocked_when_dry_run_false(self):
        """live+shadow+dry_run=false+confirmed used to reject every SHORT at the live gate."""
        rm = _manager("shadow")
        with patch.dict(os.environ, _GATE_CREDS, clear=False), patch.dict(
            os.environ, {"DEMO_MODE": ""}, clear=False
        ), patch.object(rm, "_available_usdt", return_value=10_000), patch.object(
            rm, "_portfolio_equity", return_value=10_000
        ):
            dec = rm.evaluate(
                TradeOrder(
                    type="SHORT",
                    symbol="SHD/USDT",
                    price=1.0,
                    amount=0,
                    usdt_amount=100,
                ),
                "4h",
                source="manual",
            )
        self.assertNotEqual(dec.code, "shorts_live_blocked")
        self.assertTrue(dec.approved, dec.message)
        self.assertIsNotNone(dec.order)
        self.assertEqual(dec.order.type, "SHORT")
        self.assertEqual(float(dec.order.leverage), 2.0)

    def test_shadow_cover_not_shorts_live_blocked_when_dry_run_false(self):
        """COVER of an existing short lot must also skip the live gate in shadow."""
        update_position("SCVR/USDT", "4h", "SHORT", 1.0, 8, leverage=2)
        rm = _manager("shadow")
        with patch.dict(os.environ, _GATE_CREDS, clear=False), patch.object(
            rm, "_available_usdt", return_value=10_000
        ), patch.object(rm, "_portfolio_equity", return_value=10_000):
            dec = rm.evaluate(
                TradeOrder(type="COVER", symbol="SCVR/USDT", price=0.9, amount=8),
                "4h",
                source="manual",
            )
        self.assertNotEqual(dec.code, "shorts_live_blocked")
        self.assertTrue(dec.approved, dec.message)
        self.assertEqual(dec.order.type, "COVER")

    def test_deployed_shadow_with_dry_run_true_short_approved(self):
        """Today's deployed shape (live+shadow+dry_run true) never hit this gate."""
        rm = _manager("shadow", dry_run=True)
        with patch.dict(os.environ, _GATE_CREDS, clear=False), patch.object(
            rm, "_available_usdt", return_value=10_000
        ), patch.object(rm, "_portfolio_equity", return_value=10_000):
            dec = rm.evaluate(
                TradeOrder(
                    type="SHORT",
                    symbol="DEP/USDT",
                    price=1.0,
                    amount=0,
                    usdt_amount=100,
                ),
                "4h",
                source="manual",
            )
        self.assertNotEqual(dec.code, "shorts_live_blocked")
        self.assertTrue(dec.approved, dec.message)
        self.assertEqual(dec.order.type, "SHORT")
        self.assertEqual(float(dec.order.leverage), 2.0)

    def test_real_short_still_shorts_live_blocked(self):
        """Real mode must keep the live kill switch; message and code frozen."""
        rm = _manager("real")
        env = {**_GATE_CREDS, "DEMO_MODE": ""}
        with patch.dict(os.environ, env, clear=False), patch.object(
            rm, "_available_usdt", return_value=10_000
        ), patch.object(rm, "_portfolio_equity", return_value=10_000):
            dec = rm.evaluate(
                TradeOrder(
                    type="SHORT",
                    symbol="REAL/USDT",
                    price=1.0,
                    amount=0,
                    usdt_amount=100,
                ),
                "4h",
                source="manual",
            )
        self.assertFalse(dec.approved)
        self.assertEqual(dec.code, "shorts_live_blocked")
        self.assertEqual(dec.message, _LIVE_BLOCK_MSG)

    def test_real_without_creds_fail_closed(self):
        """resolve_execution_mode raises without GATE creds → reject, no escape."""
        rm = _manager("real")
        empty = {"GATE_API_KEY": "", "GATE_API_SECRET": "", "DEMO_MODE": ""}
        with patch.dict(os.environ, empty, clear=False), patch.object(
            rm, "_available_usdt", return_value=10_000
        ), patch.object(rm, "_portfolio_equity", return_value=10_000):
            dec = rm.evaluate(
                TradeOrder(
                    type="SHORT",
                    symbol="NOCRED/USDT",
                    price=1.0,
                    amount=0,
                    usdt_amount=100,
                ),
                "4h",
                source="manual",
            )
        self.assertFalse(dec.approved)
        self.assertEqual(dec.code, "shorts_live_blocked")
        self.assertEqual(dec.message, _LIVE_BLOCK_MSG)
