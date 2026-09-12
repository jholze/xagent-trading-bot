"""#347: testnet SHORT/COVER must not hit shorts_live_blocked when allow_live=false.

The live kill switch (shorts.allow_live) still blocks real execution. Testnet is
the one extra clause; shadow and real behaviour is unchanged.
"""

from __future__ import annotations

import os
import unittest
from contextlib import nullcontext
from unittest.mock import MagicMock, patch

from core.config import BotConfig
from core.models import TradeOrder, TradeResult
from risk.risk_manager import RiskManager
from strategies.positions import clear_positions_memory, update_position


_LIVE_BLOCK_MSG = "shorts.allow_live=false (no Gate futures in v0)"
_GATE_CREDS = {"GATE_API_KEY": "test-key", "GATE_API_SECRET": "test-secret"}


def _live_cfg(execution: str) -> dict:
    return {
        "trading_mode": "live",
        "live_confirmed": True,
        "max_usdt_per_trade": 150,
        "live": {
            "execution": execution,
            "dry_run": False,
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


def _manager(execution: str) -> RiskManager:
    return RiskManager(config=BotConfig(_live_cfg(execution)))


class TestRiskShortGateTestnet(unittest.TestCase):
    def setUp(self):
        clear_positions_memory()

    def tearDown(self):
        clear_positions_memory()

    def test_testnet_short_not_shorts_live_blocked(self):
        """live+testnet+allow_live=false used to reject every SHORT at the live gate."""
        rm = _manager("testnet")
        with patch.dict(os.environ, _GATE_CREDS, clear=False), patch.dict(
            os.environ, {"DEMO_MODE": ""}, clear=False
        ), patch.object(rm, "_available_usdt", return_value=10_000), patch.object(
            rm, "_portfolio_equity", return_value=10_000
        ):
            dec = rm.evaluate(
                TradeOrder(
                    type="SHORT",
                    symbol="TNT/USDT",
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

    def test_testnet_without_creds_fail_closed(self):
        """resolve_execution_mode raises without GATE creds → old reject, no escape."""
        rm = _manager("testnet")
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

    def test_testnet_cover_not_shorts_live_blocked(self):
        """COVER of an existing short lot must also skip the live gate in testnet."""
        update_position("CVR/USDT", "4h", "SHORT", 1.0, 8, leverage=2)
        rm = _manager("testnet")
        with patch.dict(os.environ, _GATE_CREDS, clear=False), patch.object(
            rm, "_available_usdt", return_value=10_000
        ), patch.object(rm, "_portfolio_equity", return_value=10_000):
            dec = rm.evaluate(
                TradeOrder(type="COVER", symbol="CVR/USDT", price=0.9, amount=8),
                "4h",
                source="manual",
            )
        self.assertNotEqual(dec.code, "shorts_live_blocked")
        self.assertTrue(dec.approved, dec.message)
        self.assertEqual(dec.order.type, "COVER")

    def test_trading_service_short_reaches_adapter_in_testnet(self):
        """SHORT in testnet config must reach adapter.execute with leverage 2.

        refresh() is patched so BotConfig is not reloaded from disk. Writer
        lease, intent queue, ledger lock, and OrderService are stubbed so this
        does not need Redis/Mongo beyond what the suite already isolates.
        """
        from services.trading_service import TradingService

        cfg = BotConfig(_live_cfg("testnet"))
        svc = TradingService(cfg)
        mock_adapter = MagicMock()
        mock_adapter.mode = "testnet"
        mock_adapter.execute.return_value = TradeResult(
            True, "SHORT", "TSVC/USDT", amount=100, price=1.0, usdt_amount=100
        )
        mock_ledger = MagicMock()
        mock_ledger.find_by_idempotency_key.return_value = None
        mock_ledger.create_from_request.return_value = {"id": "ord-t347"}

        with patch.dict(os.environ, _GATE_CREDS, clear=False), patch.dict(
            os.environ, {"DEMO_MODE": ""}, clear=False
        ), patch.object(svc, "refresh"), patch.object(
            svc.risk, "_available_usdt", return_value=10_000
        ), patch.object(
            svc.risk, "_portfolio_equity", return_value=10_000
        ), patch(
            "services.trading_service.get_execution_adapter", return_value=mock_adapter
        ), patch(
            "bus.writer_lease.require_lease_for_order"
        ), patch(
            "services.trading_engine_runtime.should_queue_intent", return_value=False
        ), patch(
            "bus.locks.ledger_lock", return_value=nullcontext()
        ), patch(
            "services.trading_service.OrderService", return_value=mock_ledger
        ), patch(
            "notifications.telegram_commands.position_display.send_positions_snapshot"
        ):
            result = svc.execute_order(
                TradeOrder(
                    type="SHORT",
                    symbol="TSVC/USDT",
                    price=1.0,
                    amount=0,
                    usdt_amount=100,
                ),
                "4h",
                source="manual",
            )

        self.assertTrue(result.executed, result.message)
        mock_adapter.execute.assert_called_once()
        sent = mock_adapter.execute.call_args[0][0]
        self.assertEqual(sent.type, "SHORT")
        self.assertEqual(float(sent.leverage), 2.0)
