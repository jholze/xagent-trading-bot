"""#430 — exit-ws trail book and fire API must be tenant-aware.

The WS hub daemon thread does not inherit contextvars, so iterating the
module `positions` dict (DEFAULT_TENANT only) dropped henry/ctexp lots
from the trail book. Fire without tenant_id would sell default's lot on
a symbol collision. Tests mock ledger IO — nothing is written to data/.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from core.tenant_context import DEFAULT_TENANT, resolve_tenant_id, tenant_context
from services.exit_realtime.hub import ExitRealtimeHub


def _open_lot(*, amount: float = 10.0, entry: float = 1.0, high: float | None = None) -> dict:
    return {
        "amount": amount,
        "average_entry": entry,
        "recent_high": high if high is not None else entry * 1.1,
        "strategy_tier": "volatile",
    }


def _cfg():
    return SimpleNamespace(
        strategy_params=lambda _s, _t: {},
        risk_config={"atr_reference_pct": 3.0},
    )


@contextmanager
def _cycle_context(tid, **_kw):
    with tenant_context(tid, scope="paper"):
        yield


class TestLoadOpenBookTenantAware(unittest.TestCase):
    def setUp(self):
        from strategies.positions import reset_all_position_stores_for_tests

        reset_all_position_stores_for_tests()

    def tearDown(self):
        from strategies.positions import reset_all_position_stores_for_tests

        reset_all_position_stores_for_tests()

    def test_snapshot_rows_include_tenant_id_and_henry_stays_henry(self):
        from strategies.positions import get_key, positions

        stores = {
            DEFAULT_TENANT: {
                "AAA_USDT_1h": _open_lot(amount=10.0, entry=1.0),
            },
            "henry": {
                "NPC_USDT_1h": _open_lot(amount=5.0, entry=2.0),
            },
        }
        # Bot process snapshots the live default store (B4); do not rely on
        # load_positions(DEFAULT) to populate AAA.
        positions[get_key("AAA/USDT", "1h")] = dict(stores[DEFAULT_TENANT]["AAA_USDT_1h"])

        def fake_load(scope=None, tenant_id=None):
            tid = tenant_id or resolve_tenant_id()
            return dict(stores.get(tid) or {})

        with patch(
            "core.tenant_routing.iter_price_cycle_tenants",
            return_value=[DEFAULT_TENANT, "henry"],
        ), patch(
            "core.tenant_routing.tenant_cycle_context",
            side_effect=_cycle_context,
        ), patch(
            "strategies.positions.load_positions",
            side_effect=fake_load,
        ), patch(
            "core.config.get_bot_config",
            return_value=_cfg(),
        ), patch(
            "strategies.registry.resolve_strategy_params",
            return_value={},
        ):
            from services.exit_realtime.hub import _load_open_book

            rows = _load_open_book({})

        hub = ExitRealtimeHub({"exit_realtime": {"enabled": True, "mode": "shadow"}})
        hub.update_book(rows)
        snap = hub.book_snapshot()
        by_tid: dict[str, list[str]] = {}
        for row in snap:
            self.assertIn("tenant_id", row)
            by_tid.setdefault(str(row.get("tenant_id")), []).append(row["symbol"])

        self.assertIn("AAA/USDT", by_tid[DEFAULT_TENANT])
        self.assertIn("NPC/USDT", by_tid["henry"])
        self.assertNotIn("NPC/USDT", by_tid[DEFAULT_TENANT])
        self.assertNotIn("AAA/USDT", by_tid.get("henry") or [])

    def test_default_book_size_does_not_jump_when_henry_lots_added(self):
        from strategies.positions import get_key, positions

        stores = {
            DEFAULT_TENANT: {
                "AAA_USDT_1h": _open_lot(),
                "BBB_USDT_1h": _open_lot(),
            },
            "henry": {
                "NPC_USDT_1h": _open_lot(),
            },
        }
        positions[get_key("AAA/USDT", "1h")] = dict(stores[DEFAULT_TENANT]["AAA_USDT_1h"])
        positions[get_key("BBB/USDT", "1h")] = dict(stores[DEFAULT_TENANT]["BBB_USDT_1h"])

        def fake_load(scope=None, tenant_id=None):
            tid = tenant_id or resolve_tenant_id()
            return dict(stores.get(tid) or {})

        patches = dict(
            iter_tenants=patch(
                "core.tenant_routing.iter_price_cycle_tenants",
                return_value=[DEFAULT_TENANT, "henry"],
            ),
            cycle=patch(
                "core.tenant_routing.tenant_cycle_context",
                side_effect=_cycle_context,
            ),
            load=patch(
                "strategies.positions.load_positions",
                side_effect=fake_load,
            ),
            cfg=patch("core.config.get_bot_config", return_value=_cfg()),
            params=patch(
                "strategies.registry.resolve_strategy_params",
                return_value={},
            ),
        )
        with patches["iter_tenants"], patches["cycle"], patches["load"], patches[
            "cfg"
        ], patches["params"]:
            from services.exit_realtime.hub import _load_open_book

            first = _load_open_book({})
            default_n = sum(1 for r in first if r.get("tenant_id") == DEFAULT_TENANT)
            stores["henry"]["SENT_USDT_1h"] = _open_lot()
            stores["henry"]["SKYAI_USDT_1h"] = _open_lot()
            second = _load_open_book({})

        default_n2 = sum(1 for r in second if r.get("tenant_id") == DEFAULT_TENANT)
        henry_syms = [r["symbol"] for r in second if r.get("tenant_id") == "henry"]
        self.assertEqual(default_n, 2)
        self.assertEqual(default_n2, 2)
        self.assertEqual(default_n, default_n2)
        self.assertIn("SENT/USDT", henry_syms)
        self.assertIn("SKYAI/USDT", henry_syms)
        self.assertEqual(len(second), 5)

    def test_sync_loads_each_price_cycle_tenant(self):
        seen: list[str] = []

        def fake_load(scope=None, tenant_id=None):
            seen.append(str(tenant_id or resolve_tenant_id()))
            return {}

        with patch(
            "core.tenant_routing.iter_price_cycle_tenants",
            return_value=[DEFAULT_TENANT, "henry", "ctexp"],
        ), patch(
            "core.tenant_routing.tenant_cycle_context",
            side_effect=_cycle_context,
        ), patch(
            "strategies.positions.load_positions",
            side_effect=fake_load,
        ):
            from services.exit_realtime.hub import _sync_positions_from_ledger

            _sync_positions_from_ledger()

        self.assertEqual(seen, [DEFAULT_TENANT, "henry", "ctexp"])

    def test_shared_symbol_keeps_default_row_henry_only_still_appear(self):
        """B1: default and henry both hold X/USDT — incumbent (default) stays."""
        from strategies.positions import get_key, positions

        positions[get_key("X/USDT", "1h")] = _open_lot(amount=10.0, entry=1.0)
        positions[get_key("AAA/USDT", "1h")] = _open_lot(amount=1.0, entry=1.0)
        stores = {
            "henry": {
                "X_USDT_1h": _open_lot(amount=5.0, entry=2.0),
                "NPC_USDT_1h": _open_lot(amount=3.0, entry=3.0),
            },
        }

        def fake_load(scope=None, tenant_id=None):
            tid = tenant_id or resolve_tenant_id()
            return dict(stores.get(tid) or {})

        with patch(
            "core.tenant_routing.iter_price_cycle_tenants",
            return_value=[DEFAULT_TENANT, "henry"],
        ), patch(
            "core.tenant_routing.tenant_cycle_context",
            side_effect=_cycle_context,
        ), patch(
            "strategies.positions.load_positions",
            side_effect=fake_load,
        ), patch(
            "core.config.get_bot_config",
            return_value=_cfg(),
        ), patch(
            "strategies.registry.resolve_strategy_params",
            return_value={},
        ), patch(
            "services.exit_realtime.config.is_exit_radar_sidecar_process",
            return_value=False,
        ):
            from services.exit_realtime.hub import _load_open_book

            rows = _load_open_book({})

        default_n = sum(1 for r in rows if r.get("tenant_id") == DEFAULT_TENANT)
        self.assertEqual(default_n, 2)

        hub = ExitRealtimeHub({"exit_realtime": {"enabled": True, "mode": "shadow"}})
        hub.update_book(rows)
        snap = hub.book_snapshot()
        default_book = [r for r in snap if r.get("tenant_id") == DEFAULT_TENANT]
        self.assertEqual(len(default_book), 2)
        xrow = next(r for r in snap if r["symbol"] == "X/USDT")
        self.assertEqual(xrow["tenant_id"], DEFAULT_TENANT)
        self.assertEqual(float((xrow.get("position") or {}).get("amount") or 0), 10.0)
        self.assertTrue(
            any(
                r["symbol"] == "NPC/USDT" and r.get("tenant_id") == "henry"
                for r in snap
            )
        )
        self.assertGreaterEqual(hub.stats().get("book_tenant_collisions") or 0, 1)

    def test_update_book_does_not_overwrite_incumbent_tenant(self):
        """B1 unit: last writer must not evict a different tenant's row."""
        hub = ExitRealtimeHub({"exit_realtime": {"enabled": True, "mode": "shadow"}})
        hub.update_book(
            [
                {
                    "symbol": "X/USDT",
                    "timeframe": "1h",
                    "tenant_id": DEFAULT_TENANT,
                    "position": _open_lot(amount=10.0, entry=1.0),
                    "average_entry": 1.0,
                    "recent_high": 1.1,
                    "strategy_params": {},
                    "atr_pct": 3.0,
                },
                {
                    "symbol": "X/USDT",
                    "timeframe": "1h",
                    "tenant_id": "henry",
                    "position": _open_lot(amount=5.0, entry=2.0),
                    "average_entry": 2.0,
                    "recent_high": 2.2,
                    "strategy_params": {},
                    "atr_pct": 7.0,
                },
                {
                    "symbol": "NPC/USDT",
                    "timeframe": "1h",
                    "tenant_id": "henry",
                    "position": _open_lot(amount=3.0, entry=3.0),
                    "average_entry": 3.0,
                    "recent_high": 3.3,
                    "strategy_params": {},
                    "atr_pct": 7.0,
                },
            ]
        )
        snap = hub.book_snapshot()
        xrow = next(r for r in snap if r["symbol"] == "X/USDT")
        self.assertEqual(xrow["tenant_id"], DEFAULT_TENANT)
        self.assertEqual(float((xrow.get("position") or {}).get("amount") or 0), 10.0)
        self.assertTrue(
            any(r["symbol"] == "NPC/USDT" and r.get("tenant_id") == "henry" for r in snap)
        )
        self.assertEqual(len(snap), 2)
        self.assertGreaterEqual(hub.stats().get("book_tenant_collisions") or 0, 1)

    def test_satellite_rows_use_that_tenant_strategy_params(self):
        """B2: henry lots must carry henry's strategy_params / atr_pct."""
        from strategies.positions import get_key, positions

        positions[get_key("AAA/USDT", "1h")] = _open_lot(amount=10.0, entry=1.0)
        stores = {
            "henry": {
                "NPC_USDT_1h": _open_lot(amount=5.0, entry=2.0),
            },
        }

        def fake_load(scope=None, tenant_id=None):
            tid = tenant_id or resolve_tenant_id()
            return dict(stores.get(tid) or {})

        def fake_params(*_a, **_k):
            tid = resolve_tenant_id()
            if tid == "henry":
                return {
                    "trailing_stop": {"min_trail_pct": 15},
                    "atr_reference_pct": 7.0,
                }
            return {
                "trailing_stop": {"min_trail_pct": 8},
                "atr_reference_pct": 3.0,
            }

        def fake_cfg():
            tid = resolve_tenant_id()
            if tid == "henry":
                return SimpleNamespace(
                    strategy_params=lambda _s, _t: {
                        "trailing_stop": {"min_trail_pct": 15}
                    },
                    risk_config={"atr_reference_pct": 7.0},
                )
            return SimpleNamespace(
                strategy_params=lambda _s, _t: {"trailing_stop": {"min_trail_pct": 8}},
                risk_config={"atr_reference_pct": 3.0},
            )

        with patch(
            "core.tenant_routing.iter_price_cycle_tenants",
            return_value=[DEFAULT_TENANT, "henry"],
        ), patch(
            "core.tenant_routing.tenant_cycle_context",
            side_effect=_cycle_context,
        ), patch(
            "strategies.positions.load_positions",
            side_effect=fake_load,
        ), patch(
            "core.config.get_bot_config",
            side_effect=fake_cfg,
        ), patch(
            "strategies.registry.resolve_strategy_params",
            side_effect=fake_params,
        ), patch(
            "services.exit_realtime.config.is_exit_radar_sidecar_process",
            return_value=False,
        ):
            from services.exit_realtime.hub import _load_open_book

            rows = _load_open_book({})

        by_sym = {r["symbol"]: r for r in rows}
        self.assertEqual(by_sym["AAA/USDT"]["atr_pct"], 3.0)
        self.assertEqual(
            by_sym["AAA/USDT"]["strategy_params"]["trailing_stop"]["min_trail_pct"],
            8,
        )
        self.assertEqual(by_sym["NPC/USDT"]["atr_pct"], 7.0)
        self.assertEqual(
            by_sym["NPC/USDT"]["strategy_params"]["trailing_stop"]["min_trail_pct"],
            15,
        )
        self.assertEqual(by_sym["NPC/USDT"]["tenant_id"], "henry")

    def test_bot_process_does_not_load_positions_default(self):
        """B4: bot process must snapshot live default RAM, not store.clear()."""
        from strategies.positions import get_key, positions

        keep_key = get_key("KEEP/USDT", "1h")
        positions[keep_key] = _open_lot(amount=7.0, entry=1.0)
        stores = {
            "henry": {
                "NPC_USDT_1h": _open_lot(amount=3.0, entry=3.0),
            },
        }
        load_calls: list[str] = []

        def spy_load(scope=None, tenant_id=None):
            tid = str(tenant_id or resolve_tenant_id())
            load_calls.append(tid)
            if tid == DEFAULT_TENANT:
                positions.clear()
                return {}
            return dict(stores.get(tid) or {})

        with patch(
            "core.tenant_routing.iter_price_cycle_tenants",
            return_value=[DEFAULT_TENANT, "henry"],
        ), patch(
            "core.tenant_routing.tenant_cycle_context",
            side_effect=_cycle_context,
        ), patch(
            "strategies.positions.load_positions",
            side_effect=spy_load,
        ), patch(
            "core.config.get_bot_config",
            return_value=_cfg(),
        ), patch(
            "strategies.registry.resolve_strategy_params",
            return_value={},
        ), patch(
            "services.exit_realtime.config.is_exit_radar_sidecar_process",
            return_value=False,
        ):
            from services.exit_realtime.hub import _load_open_book

            rows = _load_open_book({})

        self.assertNotIn(DEFAULT_TENANT, load_calls)
        self.assertIn("henry", load_calls)
        self.assertIn(keep_key, positions)
        self.assertEqual(float(positions[keep_key]["amount"]), 7.0)
        self.assertTrue(
            any(
                r["symbol"] == "KEEP/USDT" and r.get("tenant_id") == DEFAULT_TENANT
                for r in rows
            )
        )
        self.assertTrue(
            any(r["symbol"] == "NPC/USDT" and r.get("tenant_id") == "henry" for r in rows)
        )

    def test_sidecar_process_still_load_positions_default(self):
        """B4: sidecar may still ledger-reload DEFAULT."""
        load_calls: list[str] = []

        def spy_load(scope=None, tenant_id=None):
            tid = str(tenant_id or resolve_tenant_id())
            load_calls.append(tid)
            if tid == DEFAULT_TENANT:
                return {"KEEP_USDT_1h": _open_lot(amount=7.0, entry=1.0)}
            return {}

        with patch(
            "core.tenant_routing.iter_price_cycle_tenants",
            return_value=[DEFAULT_TENANT, "henry"],
        ), patch(
            "core.tenant_routing.tenant_cycle_context",
            side_effect=_cycle_context,
        ), patch(
            "strategies.positions.load_positions",
            side_effect=spy_load,
        ), patch(
            "core.config.get_bot_config",
            return_value=_cfg(),
        ), patch(
            "strategies.registry.resolve_strategy_params",
            return_value={},
        ), patch(
            "services.exit_realtime.config.is_exit_radar_sidecar_process",
            return_value=True,
        ):
            from services.exit_realtime.hub import _load_open_book

            rows = _load_open_book({})

        self.assertIn(DEFAULT_TENANT, load_calls)
        self.assertTrue(any(r["symbol"] == "KEEP/USDT" for r in rows))

    def test_load_open_book_restores_active_key(self):
        """B3: exit-rt-book daemon must put _active_key back after henry."""
        from strategies.positions import _active_key, get_key, positions

        positions[get_key("AAA/USDT", "1h")] = _open_lot(amount=10.0, entry=1.0)
        self.assertEqual(_active_key[0], DEFAULT_TENANT)

        def fake_load(scope=None, tenant_id=None):
            tid = tenant_id or resolve_tenant_id()
            if tid == "henry":
                return {"NPC_USDT_1h": _open_lot(amount=5.0, entry=2.0)}
            return {}

        with patch(
            "core.tenant_routing.iter_price_cycle_tenants",
            return_value=[DEFAULT_TENANT, "henry"],
        ), patch(
            "storage.tenant_registry.get_tenant",
            side_effect=lambda tid, test=False: {
                "tenant_id": tid,
                "telegram": {"headless": True, "owner_chat_id": ""},
                "defaults": {"ledger_scope": "paper"},
            },
        ), patch(
            "data_manager.is_demo_mode",
            return_value=False,
        ), patch(
            "strategies.positions.bootstrap_positions",
        ), patch(
            "strategies.positions.load_positions",
            side_effect=fake_load,
        ), patch(
            "core.config.get_bot_config",
            return_value=_cfg(),
        ), patch(
            "strategies.registry.resolve_strategy_params",
            return_value={},
        ), patch(
            "services.exit_realtime.config.is_exit_radar_sidecar_process",
            return_value=False,
        ):
            from services.exit_realtime.hub import _load_open_book

            _load_open_book({})

        self.assertEqual(_active_key[0], DEFAULT_TENANT)


class TestFireHttpTenantFailClosed(unittest.TestCase):
    def setUp(self):
        import services.exit_realtime.execute as ex

        ex._inflight.clear()
        ex._last_exit_at.clear()

    def tearDown(self):
        import services.exit_realtime.execute as ex

        ex._inflight.clear()
        ex._last_exit_at.clear()
        os.environ.pop("EXIT_WS_INTERNAL_TOKEN", None)

    def _client(self):
        from flask import Flask

        from services.exit_realtime.fire_http import register_exit_ws_fire_routes

        os.environ["EXIT_WS_INTERNAL_TOKEN"] = "good"
        app = Flask(__name__)
        register_exit_ws_fire_routes(app)
        return app.test_client()

    def test_missing_tenant_id_is_400_and_does_not_execute(self):
        client = self._client()
        with patch(
            "services.exit_realtime.execute.try_execute_trail_exit"
        ) as mock_ex:
            r = client.post(
                "/internal/exit-ws/fire",
                json={
                    "symbol": "TAG/USDT",
                    "timeframe": "1h",
                    "price": 1.5,
                    "exit_source": "trailing_stop",
                },
                headers={"X-Exit-Ws-Token": "good"},
            )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json().get("message"), "missing_tenant_id")
        self.assertFalse(r.get_json().get("executed"))
        mock_ex.assert_not_called()

    def test_blank_tenant_id_is_400_and_does_not_execute(self):
        client = self._client()
        with patch(
            "services.exit_realtime.execute.try_execute_trail_exit"
        ) as mock_ex:
            r = client.post(
                "/internal/exit-ws/fire",
                json={
                    "symbol": "TAG/USDT",
                    "timeframe": "1h",
                    "price": 1.5,
                    "exit_source": "trailing_stop",
                    "tenant_id": "   ",
                },
                headers={"X-Exit-Ws-Token": "good"},
            )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json().get("message"), "missing_tenant_id")
        mock_ex.assert_not_called()

    def test_unknown_tenant_id_is_400_and_does_not_execute(self):
        client = self._client()
        with patch(
            "core.tenant_routing.iter_price_cycle_tenants",
            return_value=[DEFAULT_TENANT, "henry"],
        ), patch(
            "services.exit_realtime.execute.try_execute_trail_exit"
        ) as mock_ex:
            r = client.post(
                "/internal/exit-ws/fire",
                json={
                    "symbol": "TAG/USDT",
                    "timeframe": "1h",
                    "price": 1.5,
                    "exit_source": "trailing_stop",
                    "tenant_id": "nope",
                },
                headers={"X-Exit-Ws-Token": "good"},
            )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json().get("message"), "unknown_tenant_id")
        self.assertFalse(r.get_json().get("executed"))
        mock_ex.assert_not_called()

    def test_fire_henry_does_not_touch_default_lot_of_same_symbol(self):
        client = self._client()
        seen_tenants: list[str] = []
        default_executed = {"n": 0}

        def fake_ex(**kwargs):
            tid = resolve_tenant_id()
            seen_tenants.append(tid)
            if tid == DEFAULT_TENANT:
                default_executed["n"] += 1
                return {
                    "ok": True,
                    "executed": True,
                    "message": "default-should-not-run",
                    "amount": 999.0,
                }
            return {
                "ok": True,
                "executed": True,
                "message": "henry-filled",
                "amount": 5.0,
                "tenant_id": tid,
            }

        with patch(
            "core.tenant_routing.iter_price_cycle_tenants",
            return_value=[DEFAULT_TENANT, "henry"],
        ), patch(
            "core.tenant_routing.tenant_cycle_context",
            side_effect=_cycle_context,
        ), patch(
            "services.exit_realtime.execute.try_execute_trail_exit",
            side_effect=fake_ex,
        ) as mock_ex:
            r = client.post(
                "/internal/exit-ws/fire",
                json={
                    "symbol": "TAG/USDT",
                    "timeframe": "1h",
                    "price": 1.5,
                    "action": "SELL_FULL",
                    "exit_source": "trailing_stop",
                    "tenant_id": "henry",
                },
                headers={"X-Exit-Ws-Token": "good"},
            )
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["executed"])
        self.assertEqual(body.get("message"), "henry-filled")
        self.assertEqual(seen_tenants, ["henry"])
        self.assertEqual(default_executed["n"], 0)
        self.assertEqual(mock_ex.call_count, 1)
        self.assertEqual(mock_ex.call_args.kwargs["symbol"], "TAG/USDT")

    def test_try_execute_uses_active_tenant_store(self):
        """Same symbol, two lots: execute under henry must see henry's amount."""
        from services.exit_realtime.execute import try_execute_trail_exit

        henry_pos = _open_lot(amount=5.0, entry=2.0)
        default_pos = _open_lot(amount=100.0, entry=1.0)
        trading = MagicMock()
        trading.execute_order.return_value = SimpleNamespace(
            executed=True, message="ok"
        )

        def fake_get(sym, tf):
            tid = resolve_tenant_id()
            if tid == "henry":
                return henry_pos
            return default_pos

        with patch(
            "strategies.positions.get_position", side_effect=fake_get
        ), patch(
            "strategies.positions.is_open_position", return_value=True
        ), patch(
            "strategies.short_math.is_short", return_value=False
        ), patch(
            "strategies.recovery_hold.maybe_promote_recovery_hold",
            return_value=False,
        ), patch(
            "strategies.position_lock.attach_lock_from_ledger",
            side_effect=lambda pos, *_a, **_k: pos,
        ), patch(
            "strategies.position_lock.auto_sell_blocked",
            return_value=(False, ""),
        ), patch(
            "core.tenant_routing.tenant_cycle_context",
            side_effect=_cycle_context,
        ):
            r = try_execute_trail_exit(
                symbol="HENRYTAG/USDT",
                timeframe="1h",
                price=1.5,
                action="SELL_FULL",
                exit_source="trailing_stop",
                trading=trading,
                force_local=True,
                tenant_id="henry",
            )
        self.assertTrue(r["executed"])
        order = trading.execute_order.call_args[0][0]
        self.assertEqual(order.amount, 5.0)
        self.assertNotEqual(order.amount, 100.0)

    def test_fire_http_restores_active_key_after_henry(self):
        """B3: Flask fire must not leave _active_key on henry."""
        from strategies.positions import (
            _active_key,
            _ensure_store,
            _resolve_store_key,
            get_key,
            positions,
            reset_all_position_stores_for_tests,
        )

        reset_all_position_stores_for_tests()
        self.addCleanup(reset_all_position_stores_for_tests)
        lot_key = get_key("TAG/USDT", "1h")
        positions[lot_key] = _open_lot(amount=100.0, entry=1.0)
        henry_store = _ensure_store(_resolve_store_key("paper", "henry"))
        henry_store[lot_key] = _open_lot(amount=5.0, entry=2.0)
        self.assertEqual(_active_key[0], DEFAULT_TENANT)

        client = self._client()
        with patch(
            "core.tenant_routing.iter_price_cycle_tenants",
            return_value=[DEFAULT_TENANT, "henry"],
        ), patch(
            "storage.tenant_registry.get_tenant",
            side_effect=lambda tid, test=False: {
                "tenant_id": tid,
                "telegram": {"headless": True, "owner_chat_id": ""},
                "defaults": {"ledger_scope": "paper"},
            },
        ), patch(
            "data_manager.is_demo_mode",
            return_value=False,
        ), patch(
            "strategies.positions.bootstrap_positions",
        ), patch(
            "services.exit_realtime.execute.try_execute_trail_exit",
            return_value={
                "ok": True,
                "executed": True,
                "message": "henry-filled",
                "amount": 5.0,
            },
        ):
            r = client.post(
                "/internal/exit-ws/fire",
                json={
                    "symbol": "TAG/USDT",
                    "timeframe": "1h",
                    "price": 1.5,
                    "action": "SELL_FULL",
                    "exit_source": "trailing_stop",
                    "tenant_id": "henry",
                },
                headers={"X-Exit-Ws-Token": "good"},
            )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(_active_key[0], DEFAULT_TENANT)

    def test_try_execute_uses_real_henry_store_and_restores_active_key(self):
        """B3: hub/fire path switches _active_key then restores; sells henry's lot."""
        from services.exit_realtime.execute import try_execute_trail_exit
        from strategies.positions import (
            _active_key,
            _ensure_store,
            _resolve_store_key,
            get_key,
            get_position,
            positions,
            reset_all_position_stores_for_tests,
        )

        reset_all_position_stores_for_tests()
        self.addCleanup(reset_all_position_stores_for_tests)
        lot_key = get_key("TAG/USDT", "1h")
        positions[lot_key] = _open_lot(amount=100.0, entry=1.0)
        henry_store = _ensure_store(_resolve_store_key("paper", "henry"))
        henry_store[lot_key] = _open_lot(amount=5.0, entry=2.0)
        self.assertEqual(_active_key[0], DEFAULT_TENANT)

        trading = MagicMock()
        trading.execute_order.return_value = SimpleNamespace(
            executed=True, message="ok"
        )

        with patch(
            "storage.tenant_registry.get_tenant",
            side_effect=lambda tid, test=False: {
                "tenant_id": tid,
                "telegram": {"headless": True, "owner_chat_id": ""},
                "defaults": {"ledger_scope": "paper"},
            },
        ), patch(
            "data_manager.is_demo_mode",
            return_value=False,
        ), patch(
            "strategies.positions.bootstrap_positions",
        ), patch(
            "strategies.short_math.is_short",
            return_value=False,
        ), patch(
            "strategies.recovery_hold.maybe_promote_recovery_hold",
            return_value=False,
        ), patch(
            "strategies.position_lock.attach_lock_from_ledger",
            side_effect=lambda pos, *_a, **_k: pos,
        ), patch(
            "strategies.position_lock.auto_sell_blocked",
            return_value=(False, ""),
        ):
            r = try_execute_trail_exit(
                symbol="TAG/USDT",
                timeframe="1h",
                price=1.5,
                action="SELL_FULL",
                exit_source="trailing_stop",
                trading=trading,
                force_local=True,
                tenant_id="henry",
            )
        self.assertTrue(r["executed"])
        order = trading.execute_order.call_args[0][0]
        self.assertEqual(order.amount, 5.0)
        self.assertNotEqual(order.amount, 100.0)
        self.assertEqual(_active_key[0], DEFAULT_TENANT)
        pos = get_position("TAG/USDT", "1h")
        self.assertEqual(float(pos.get("amount") or 0), 100.0)

    def test_remote_payload_includes_tenant_id(self):
        from services.exit_realtime.execute import try_execute_trail_exit

        os.environ["EXIT_EXECUTE_URL"] = "http://bot.example/internal/exit-ws/fire"
        os.environ["EXIT_WS_INTERNAL_TOKEN"] = "secret-token"
        self.addCleanup(lambda: os.environ.pop("EXIT_EXECUTE_URL", None))
        self.addCleanup(lambda: os.environ.pop("EXIT_WS_INTERNAL_TOKEN", None))

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(
                    {"ok": True, "executed": True, "message": "ok"}
                ).encode()

        with patch(
            "services.exit_realtime.execute.urllib.request.urlopen",
            return_value=_Resp(),
        ) as mock_open:
            with tenant_context("henry", scope="paper"):
                r = try_execute_trail_exit(
                    symbol="TAG/USDT",
                    timeframe="1h",
                    price=1.23,
                    action="SELL_FULL",
                    exit_source="trailing_stop",
                    rationale="test",
                )
        self.assertTrue(r["executed"])
        req = mock_open.call_args[0][0]
        payload = json.loads(req.data.decode())
        self.assertEqual(payload.get("tenant_id"), "henry")


if __name__ == "__main__":
    unittest.main()
