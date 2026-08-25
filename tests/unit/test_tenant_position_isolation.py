"""Multi-tenant position store must not leak operator lots into satellite ledgers."""

from __future__ import annotations

import os
import sys
import unittest
from decimal import Decimal
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.tenant_context import DEFAULT_TENANT, tenant_context
from strategies.positions import (
    _activate,
    _position_stores,
    _resolve_store_key,
    _serialize_positions,
    activate_tenant_positions,
    clear_positions_memory,
    count_open_full_slots,
    count_open_positions,
    count_open_tail_slots,
    flush_positions,
    get_key,
    list_active_positions,
    lock_strategy_tier,
    positions,
)


class TestTenantPositionIsolation(unittest.TestCase):
    def setUp(self):
        clear_positions_memory()
        clear_positions_memory(tenant_id="henry")
        clear_positions_memory(tenant_id="ctexp")
        _position_stores.clear()

    def tearDown(self):
        clear_positions_memory()
        clear_positions_memory(tenant_id="henry")
        clear_positions_memory(tenant_id="ctexp")
        _position_stores.clear()

    def _seed_default_open_lot(self, symbol: str = "SPCX/USDT", tf: str = "1h") -> None:
        key = get_key(symbol, tf)
        _activate(_resolve_store_key("demo", DEFAULT_TENANT))
        positions[key] = {
            "amount": Decimal("10"),
            "peak_amount": 10.0,
            "sold_percent": 0.0,
            "average_entry": 100.0,
            "realized_pnl": 0.0,
            "last_buy_price": 100.0,
            "last_ampel": "🟢",
            "last_rsi": 40.0,
            "last_action": "BUY",
            "last_trade_at": None,
            "last_trade_type": None,
            "rsi_sell_tiers_done": {},
            "last_cmc_sell_at": None,
            "recent_high": 110.0,
            "strategy_tier": "volatile",
            "exit_ladder_step": 0,
            "dca_rounds": 0,
            "dca_max_rounds": 0,
            "last_dca_at": None,
            "dca_total_usdt": 0.0,
            "dca_recovery_rounds": 0,
            "dca_recovery_max_rounds": 0,
            "last_dca_recovery_at": None,
            "last_recovery_ref_price": 0.0,
            "last_sell_signal": None,
            "first_buy_at": "2026-07-16T10:00:00",
            "entry_source": None,
            "entry_at": None,
            "entry_15m_vol_ratio": None,
            "time_profit_exit_done": False,
            "profit_armed_at": None,
            "trail_tp_steps": 0,
            "last_trail_tp_at": None,
            "profit_max_lifetime_done": False,
        }

    def test_count_open_full_slots_uses_active_tenant_store(self):
        """Risk max_open must not see default lots while cycling ctexp/henry."""
        self._seed_default_open_lot("AAA/USDT")
        self._seed_default_open_lot("BBB/USDT")
        _activate(_resolve_store_key("demo", DEFAULT_TENANT))
        self.assertEqual(count_open_full_slots({}), 2)

        with tenant_context("ctexp", scope="demo"):
            with patch(
                "services.ledger_sync._build_positions_snapshot_from_orders",
                return_value={},
            ), patch(
                "strategies.positions.load_positions_document",
                return_value={"positions": {}},
            ):
                activate_tenant_positions(scope="demo", tenant_id="ctexp")
            self.assertEqual(list_active_positions(), [])
            self.assertEqual(count_open_full_slots({}), 0)
            self.assertEqual(count_open_tail_slots({}), 0)
            self.assertEqual(count_open_positions(), 0)

        _activate(_resolve_store_key("demo", DEFAULT_TENANT))
        self.assertEqual(count_open_full_slots({}), 2)

    def test_list_active_positions_uses_current_tenant_store(self):
        self._seed_default_open_lot()
        self.assertEqual(len(list_active_positions()), 1)

        with tenant_context("henry", scope="demo"):
            with patch(
                "services.ledger_sync._build_positions_snapshot_from_orders",
                return_value={},
            ), patch(
                "strategies.positions.load_positions_document",
                return_value={"positions": {}},
            ):
                activate_tenant_positions(scope="demo")
            self.assertEqual(list_active_positions(), [])

    def test_activate_satellite_reloads_open_lots_from_ledger(self):
        """Stale empty RAM must not report pos=0 when orders have an open lot."""
        lot = {
            "amount": 100.0,
            "peak_amount": 100.0,
            "sold_percent": 0.0,
            "average_entry": 2.5,
        }
        with tenant_context("henry", scope="demo"):
            with patch(
                "services.ledger_sync._build_positions_snapshot_from_orders",
                return_value={},
            ), patch(
                "strategies.positions.load_positions_document",
                return_value={"positions": {}},
            ):
                activate_tenant_positions(scope="demo", tenant_id="henry")
            self.assertEqual(list_active_positions(tenant_id="henry", scope="demo"), [])

            with patch(
                "services.ledger_sync._build_positions_snapshot_from_orders",
                return_value={"BEAT_USDT_4h": lot},
            ), patch(
                "strategies.positions.load_positions_document",
                return_value={"positions": {}},
            ):
                activate_tenant_positions(scope="demo", tenant_id="henry")
            active = list_active_positions(tenant_id="henry", scope="demo")
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]["symbol"], "BEAT/USDT")

    def test_lock_strategy_tier_does_not_persist_empty_shell(self):
        with tenant_context("henry", scope="demo"):
            activate_tenant_positions(scope="demo")
            with patch("strategies.positions.save_positions_document", return_value=True) as mock_save:
                lock_strategy_tier("ZBT/USDT", "4h", "volatile")
                flush_positions(force=True)
                if mock_save.called:
                    payload = mock_save.call_args[0][0]
                    self.assertEqual(payload.get("positions") or {}, {})

    def test_serialize_skips_zero_amount_shells_without_trade_history(self):
        with tenant_context("henry", scope="demo"):
            activate_tenant_positions(scope="demo")
            lock_strategy_tier("AI/USDT", "1h", "stable")
            data = _serialize_positions()
            self.assertEqual(data.get("positions") or {}, {})

    def test_flush_debounce_pins_tenant_at_schedule_time(self):
        import time
        from strategies.positions import _ensure_store

        self._seed_default_open_lot()
        saved: list[tuple[str | None, list[str]]] = []

        def _capture(data, scope, config=None, tenant_id=None):
            saved.append((tenant_id, list((data.get("positions") or {}).keys())))
            return True

        with tenant_context("henry", scope="demo"):
            activate_tenant_positions(scope="demo")
            henry_key = ("henry", "demo")
            store = _ensure_store(henry_key)
            lot_key = get_key("BAS/USDT", "1h")
            store[lot_key] = dict(positions[get_key("SPCX/USDT", "1h")])
            store[lot_key]["amount"] = Decimal("5")
            with patch("strategies.positions._FLUSH_DEBOUNCE_SEC", 0.02), patch(
                "strategies.positions.save_positions_document", side_effect=_capture
            ):
                flush_positions(force=False)
                _activate(_resolve_store_key("demo", DEFAULT_TENANT))
                time.sleep(0.05)

        henry_saves = [s for s in saved if s[0] == "henry"]
        self.assertEqual(len(henry_saves), 1)
        self.assertEqual(henry_saves[0][1], [lot_key])


    def test_cycle_handoff_default_to_henry_no_ghost_persist(self):
        """Reproduce operator→henry handoff: henry must not inherit default open lots."""
        from core.tenant_routing import tenant_cycle_context

        self._seed_default_open_lot()
        saved: list[dict] = []

        def _capture(data, scope, config=None, tenant_id=None):
            saved.append({"tenant_id": tenant_id, "positions": dict(data.get("positions") or {})})
            return True

        with patch("storage.tenant_registry.get_tenant") as mock_get_tenant, patch(
            "strategies.positions.save_positions_document", side_effect=_capture
        ):
            mock_get_tenant.return_value = {
                "tenant_id": "henry",
                "telegram": {"owner_chat_id": "6512212782"},
                "defaults": {"ledger_scope": "demo"},
            }
            with tenant_cycle_context("henry"):
                self.assertEqual(list_active_positions(), [])
                lock_strategy_tier("SPCX/USDT", "1h", "volatile")
                flush_positions(force=True)

        henry_writes = [s for s in saved if s["tenant_id"] == "henry"]
        self.assertEqual(len(henry_writes), 1)
        self.assertEqual(henry_writes[0]["positions"], {})


class TestTenantCycleContextBootstrap(unittest.TestCase):
    @patch("strategies.positions.activate_tenant_positions")
    @patch("storage.tenant_registry.get_tenant")
    def test_tenant_cycle_context_activates_positions_store(
        self, mock_get_tenant, mock_activate
    ):
        from core.tenant_routing import tenant_cycle_context

        mock_get_tenant.return_value = {
            "tenant_id": "henry",
            "telegram": {"owner_chat_id": "6512212782"},
            "defaults": {"ledger_scope": "demo"},
        }
        with tenant_cycle_context("henry"):
            mock_activate.assert_called_once_with(scope="demo", tenant_id="henry")


if __name__ == "__main__":
    unittest.main()