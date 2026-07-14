"""Portfolio must load tenant-scoped positions/trade history (no background-thread leak)."""

from __future__ import annotations

import os
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.tenant_context import DEFAULT_TENANT, tenant_context, tenant_snapshot
from notifications.telegram_commands import portfolio_commands as pc
from strategies.positions import load_positions


class TestPortfolioTenantIsolation(unittest.TestCase):
    def setUp(self):
        os.environ["MULTI_TENANT_ENABLED"] = "1"

    def tearDown(self):
        os.environ.pop("MULTI_TENANT_ENABLED", None)

    def test_tenant_snapshot_captures_active_context(self):
        with tenant_context("henry", scope="paper", owner_chat_id="222"):
            tid, scope, owner = tenant_snapshot()
        self.assertEqual(tid, "henry")
        self.assertEqual(scope, "paper")
        self.assertEqual(owner, "222")

    @patch("notifications.telegram_commands.portfolio_commands.send_positions_snapshot")
    def test_background_portfolio_thread_restores_tenant_context(self, mock_snapshot):
        seen: list[str] = []
        done = threading.Event()

        def _fake_snapshot(**kwargs):
            from core.tenant_context import resolve_tenant_id

            seen.append(resolve_tenant_id())
            done.set()
            return True

        mock_snapshot.side_effect = _fake_snapshot

        with tenant_context("henry", scope="paper", owner_chat_id="222"):
            tid, scope, owner = tenant_snapshot()
            threading.Thread(
                target=pc._build_positions,
                args=("222",),
                kwargs={
                    "detail_level": "compact",
                    "tenant_id": tid,
                    "scope": scope,
                    "owner_chat_id": owner,
                },
                daemon=True,
            ).start()

        self.assertTrue(done.wait(timeout=3))
        self.assertEqual(seen, ["henry"])

    @patch("services.ledger_sync._build_positions_snapshot_from_orders")
    @patch("data_manager.load_positions_document")
    def test_load_positions_uses_tenant_orders(self, mock_cache, mock_order_snap):
        mock_cache.return_value = {"positions": {}}

        def snap(scope):
            if scope == "paper":
                return {
                    "BTC_USDT_4h": {
                        "amount": 0.01,
                        "average_entry": 100.0,
                        "sold_percent": 0.0,
                        "peak_amount": 0.01,
                    }
                }
            return {}

        mock_order_snap.side_effect = snap

        cfg = {"trading_mode": "paper", "architecture": {"ledger_backend": "mongo"}}
        with patch("data_manager.get_config", return_value=cfg):
            with patch("data_manager._ledger_reads_mongo", return_value=False):
                with tenant_context("henry", scope="paper"):
                    load_positions(scope="paper", tenant_id="henry")
                    from strategies.positions import list_active_positions

                    active = list_active_positions()
                    self.assertEqual(len(active), 1)
                    self.assertEqual(active[0]["symbol"], "BTC/USDT")

                with tenant_context(DEFAULT_TENANT, scope="paper"):
                    mock_order_snap.side_effect = lambda scope: {}
                    load_positions(scope="paper", tenant_id=DEFAULT_TENANT)
                    from strategies.positions import list_active_positions

                    self.assertEqual(list_active_positions(), [])


if __name__ == "__main__":
    unittest.main()