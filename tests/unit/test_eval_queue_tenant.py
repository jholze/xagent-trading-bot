"""Eval queue must execute jobs in the correct tenant ledger context."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from bus.eval_queue import EvalJob, PRIORITY_ENTRY_15M
from core.tenant_context import DEFAULT_TENANT, tenant_context
from services.eval_queue_runtime import process_eval_job, reset_eval_runtime_for_tests


class TestEvalQueueTenantExecution(unittest.TestCase):
    def setUp(self):
        reset_eval_runtime_for_tests()
        os.environ["MULTI_TENANT_ENABLED"] = "1"

    def tearDown(self):
        os.environ.pop("MULTI_TENANT_ENABLED", None)

    @patch("storage.tenant_registry.get_tenant")
    @patch("services.eval_queue_runtime.get_prices_batch", return_value={"SUI/USDT": 1.0})
    @patch("data_manager.load_effective_watchlist")
    def test_process_eval_job_sets_tenant_context(self, mock_wl, _prices, mock_get_tenant):
        mock_get_tenant.return_value = {
            "tenant_id": "henry",
            "defaults": {"ledger_scope": "demo"},
            "telegram": {"owner_chat_id": "6512212782"},
        }
        mock_wl.return_value = [{"symbol": "SUI/USDT", "timeframe": "4h", "active": True}]
        seen = {}

        class FakeOrchestrator:
            class market:
                @staticmethod
                def fetch_ohlcv(*_a, **_k):
                    return None

                @staticmethod
                def compute_15m_sensor_metrics(*_a, **_k):
                    return {}

            def process_coin(self, coin, price, x, cmc, lc, quiet=True):
                from core.tenant_context import resolve_tenant_id

                seen["tenant"] = resolve_tenant_id()
                return {"action": "HOLD", "symbol": coin["symbol"], "executed": False}

        job = EvalJob(
            symbol="SUI/USDT",
            timeframe="4h",
            reason="stale_watchlist",
            priority=PRIORITY_ENTRY_15M,
            enqueued_at=0.0,
            tenant_id="henry",
        )
        with tenant_context(DEFAULT_TENANT, scope="demo"):
            process_eval_job(FakeOrchestrator(), job)
        self.assertEqual(seen.get("tenant"), "henry")


if __name__ == "__main__":
    unittest.main()