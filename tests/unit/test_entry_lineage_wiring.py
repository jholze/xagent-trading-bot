"""D-LIN Rev 1: decision-time indicators survive the first fill.

Offline. No network. No data/ writes (ledger files stay on the test redirect).
Existing #600 assertions are not edited here.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from core.models import RiskDecision, TradeOrder
from services.entry_lineage import (
    SCHEMA,
    bind_exit_allocations,
    copy_decision_lineage,
    lineage_coverage,
    order_is_fill,
    public_lineage,
    stamp_decision_on_order,
    stamp_size_from_decision,
)
from services.mcp.explain import list_orders_public, sanitize_order
from services.mcp.tools import tool_lots
from services.order_service import OrderService
from services.portfolio_service import PortfolioService
from strategies.positions import (
    _ENTRY_SNAPSHOT_KEYS,
    _deserialize_position,
    _serialize_positions,
    clear_positions_memory,
    get_position,
    update_position,
)

_SUPPLIED = {
    "source": "lc",
    "timeframe": "4h",
    "rsi": 81,
    "atr": 2,
    "volume_factor": 3,
    "rationale": "decision text",
    "rationale_codes": ["rsi_high"],
    "size_before_mult": 100.0,
    "size_after_mult": 80.0,
    "cap_applied": False,
    "strategy_profile": "volatile_altcoin",
    "volatility_tier": "volatile",
    "extended": {
        "bb_upper": 9.0,
        "relvol_rank": 2,
        "oracle_state": "risk_off",
        "memory_ids": ["mem-1"],
        "embedding": [0.2, 0.3],
        "api_key": "nope",
    },
}


class EntryLineageWiringTest(unittest.TestCase):
    def setUp(self):
        clear_positions_memory()

    def tearDown(self):
        clear_positions_memory()

    def _buy(self, symbol="LIN/USDT", lineage=None, source="lc", **kwargs):
        bag = dict(_SUPPLIED if lineage is None else lineage)
        return PortfolioService().execute_buy(
            symbol,
            "4h",
            1.0,
            50,
            source=source,
            lineage=bag,
            sync_virtual_ledger=False,
            **kwargs,
        )

    def test_ac_lin_1_first_fill_snapshot(self):
        result = self._buy()
        self.assertTrue(result.executed)
        pos = get_position("LIN/USDT", "4h")
        snap = pos["entry_snapshot"]
        self.assertEqual(set(snap), set(_ENTRY_SNAPSHOT_KEYS))
        self.assertEqual(snap["rsi"], 81)
        self.assertEqual(snap["atr"], 2)
        self.assertEqual(snap["volume_factor"], 3)
        self.assertEqual(snap["source"], "lc")
        self.assertEqual(snap["timeframe"], "4h")
        self.assertEqual(snap["rationale_codes"], ["rsi_high"])
        self.assertEqual(snap["size_before_mult"], 100.0)
        self.assertEqual(snap["size_after_mult"], 80.0)
        self.assertIs(snap["cap_applied"], False)
        self.assertEqual(pos["entry_source"], "lc")
        lineage = pos["entry_lineage"]
        self.assertEqual(lineage["schema"], SCHEMA)
        self.assertEqual(lineage["rsi"], 81)
        self.assertTrue(str(lineage["entry_allocation_id"]).startswith("alloc:"))
        self.assertNotIn("embedding", lineage.get("extended") or {})
        self.assertNotIn("api_key", lineage.get("extended") or {})
        self.assertEqual(lineage["extended"]["bb_upper"], 9.0)
        self.assertNotIn("bb_upper", snap)

    def test_ac_lin_1_partial_dca_keeps_snapshot(self):
        self._buy()
        first = dict(get_position("LIN/USDT", "4h")["entry_snapshot"])
        first_line = dict(get_position("LIN/USDT", "4h")["entry_lineage"])
        PortfolioService().execute_buy(
            "LIN/USDT",
            "4h",
            1.4,
            20,
            source="dca",
            lineage={"source": "dca", "rsi": 1, "atr": 9, "volume_factor": 9},
            sync_virtual_ledger=False,
        )
        pos = get_position("LIN/USDT", "4h")
        self.assertEqual(pos["entry_snapshot"], first)
        self.assertEqual(pos["entry_lineage"]["entry_allocation_id"], first_line["entry_allocation_id"])
        self.assertEqual(pos["entry_lineage"]["rsi"], 81)
        self.assertEqual(pos["entry_source"], "lc")

    def test_ac_lin_1_snapshot_failure_keeps_fill(self):
        with patch("strategies.positions._build_entry_snapshot", side_effect=RuntimeError("boom")):
            result = self._buy(symbol="FAIL/USDT")
        self.assertTrue(result.executed)
        pos = get_position("FAIL/USDT", "4h")
        self.assertGreater(float(pos["amount"]), 0)
        self.assertNotIn("entry_snapshot", pos)
        # Lineage is a separate attach. A snapshot failure does not drop the fill.
        self.assertEqual(pos["entry_lineage"]["rsi"], 81)

    def test_ac_lin_1_lineage_failure_keeps_fill(self):
        with patch("strategies.positions._attach_entry_lineage", side_effect=RuntimeError("lineage boom")), patch(
            "strategies.positions.log"
        ) as log:
            result = self._buy(symbol="LFAIL/USDT")
        self.assertTrue(result.executed)
        pos = get_position("LFAIL/USDT", "4h")
        self.assertEqual(pos["entry_snapshot"]["rsi"], 81)
        self.assertNotIn("entry_lineage", pos)
        warnings = [c.args[0] for c in log.call_args_list if len(c.args) >= 2 and c.args[1] == "WARNING"]
        self.assertTrue(any("LFAIL/USDT" in str(msg) for msg in warnings))

    def test_ac_lin_1_no_remote_recompute(self):
        # A current-HUD number must not replace the decision value.
        hud = {"rsi": 10, "atr": 99, "volume_factor": 0}
        result = self._buy(lineage={**_SUPPLIED, "hud": hud})
        self.assertTrue(result.executed)
        snap = get_position("LIN/USDT", "4h")["entry_snapshot"]
        self.assertEqual(snap["rsi"], 81)
        self.assertEqual(snap["atr"], 2)
        self.assertEqual(snap["volume_factor"], 3)
        self.assertNotIn("hud", snap)

    def test_ac_lin_2_source_and_missingness_matrix(self):
        cases = ("lc", "cmc", "auto", "entry_sensor_15m", "gainer_relvol")
        for source in cases:
            symbol = f"SRC{source[:4]}/USDT"
            result = PortfolioService().execute_buy(
                symbol,
                "4h",
                1.0,
                40,
                source=source,
                lineage={"source": source, "timeframe": "4h"},
                sync_virtual_ledger=False,
            )
            self.assertTrue(result.executed, source)
            pos = get_position(symbol, "4h")
            self.assertEqual(pos["entry_source"], source, source)
            self.assertEqual(pos["entry_snapshot"]["source"], source, source)
            self.assertIsNone(pos["entry_snapshot"]["rsi"])
            self.assertIsNone(pos["entry_snapshot"]["atr"])
            self.assertIsNone(pos["entry_snapshot"]["volume_factor"])
            missing = pos["entry_lineage"]["missing"]
            self.assertEqual(missing["rsi"], "not_supplied", source)
            self.assertEqual(missing["atr"], "not_supplied", source)
            self.assertEqual(missing["volume_factor"], "not_supplied", source)
            self.assertNotIn("strategy_tier", pos["entry_lineage"] or {})
            self.assertIsNone(pos.get("strategy_tier"))

    def test_ac_lin_2_atr_pct_is_not_snapshot_atr(self):
        result = self._buy(lineage={
            "source": "lc",
            "rsi": 81,
            "volume_factor": 3,
            "atr_pct": 4.5,
        })
        self.assertTrue(result.executed)
        pos = get_position("LIN/USDT", "4h")
        self.assertIsNone(pos["entry_snapshot"]["atr"])
        self.assertEqual(pos["entry_lineage"]["extended"]["atr_pct"], 4.5)
        self.assertEqual(pos["entry_lineage"]["missing"]["atr"], "atr_pct_not_atr")

    def test_ac_lin_2_extended_context_not_in_snapshot_keys(self):
        self._buy()
        snap = get_position("LIN/USDT", "4h")["entry_snapshot"]
        self.assertEqual(set(snap), set(_ENTRY_SNAPSHOT_KEYS))
        self.assertNotIn("oracle_state", snap)
        self.assertNotIn("memory_ids", snap)

    def test_ac_lin_2_open_lot_is_not_backfilled(self):
        update_position("OLD/USDT", "4h", "BUY", 1.0, 10)
        self.assertIsNone(get_position("OLD/USDT", "4h").get("entry_source"))
        PortfolioService().execute_buy(
            "OLD/USDT",
            "4h",
            0.9,
            20,
            source="auto",
            lineage={"source": "auto", "rsi": 11},
            sync_virtual_ledger=False,
        )
        pos = get_position("OLD/USDT", "4h")
        self.assertIsNone(pos.get("entry_source"))
        self.assertNotEqual(pos.get("entry_snapshot", {}).get("rsi"), 11)

    def test_ac_lin_3_entry_exit_and_attempt_links(self):
        self._buy(order_id="buy-lin-1")
        pos = get_position("LIN/USDT", "4h")
        alloc = pos["entry_lineage"]["entry_allocation_id"]
        self.assertEqual(alloc, "ord:buy-lin-1")
        svc = OrderService()
        first = svc.create_from_request(
            TradeOrder("SELL", "LIN/USDT", 1.1, 2, signal="SELL"),
            timeframe="4h",
            status="executing",
            telegram_token="sell-a",
        )
        second = svc.create_from_request(
            TradeOrder("SELL", "LIN/USDT", 1.2, 2, signal="SELL"),
            timeframe="4h",
            status="executing",
            telegram_token="sell-b",
        )
        book = PortfolioService()
        book.execute_sell(
            "LIN/USDT", "4h", 1.1, "SELL", amount=2, order_id="sell-a", sync_virtual_ledger=False,
        )
        book.execute_sell(
            "LIN/USDT", "4h", 1.2, "SELL", amount=2, order_id="sell-b", sync_virtual_ledger=False,
        )
        row_a = svc.get_by_id(first["id"])
        row_b = svc.get_by_id(second["id"])
        self.assertEqual(row_a["exit_lineage"]["allocations"][0]["entry_allocation_id"], alloc)
        self.assertEqual(row_b["exit_lineage"]["allocations"][0]["entry_allocation_id"], alloc)
        self.assertEqual(row_a["exit_lineage"]["allocations"][0]["entry_source"], "lc")
        self.assertNotEqual(
            row_a["exit_lineage"]["allocations"][0]["entry_allocation_id"],
            "LIN/USDT|4h",
        )

    def test_ac_lin_3_multi_lot_allocations(self):
        lots = [
            {"entry_lineage": {"entry_allocation_id": "ord:a", "entry_order_id": "a", "source": "lc"}},
            {"entry_lineage": {"entry_allocation_id": "ord:b", "entry_order_id": "b", "source": "cmc"}},
            {"symbol": "NOPE/USDT", "entry_at": "2026-11-10T00:00:00"},
        ]
        bound = bind_exit_allocations("exit-1", lots, qty_by_index=[1.0, 2.0, 3.0])
        linked = [leg for leg in bound["allocations"] if leg.get("linked")]
        self.assertEqual([leg["entry_allocation_id"] for leg in linked], ["ord:a", "ord:b"])
        self.assertEqual(bound["unlinked"], 1)
        self.assertEqual(bound["allocations"][2]["reason"], "no_entry_allocation_id")

    def test_ac_lin_3_rejected_attempt_is_not_a_fill(self):
        svc = OrderService()
        order = TradeOrder("BUY", "REJ/USDT", 1.0, 0, usdt_amount=25, source="lc")
        order.decision_rsi = 81
        order.decision_source = "lc"
        order.idempotency_key = "corr-rej-1"
        rejected = svc.record_rejected(
            order,
            RiskDecision(approved=False, message="blocked", code="mode_blocked", order=order),
            timeframe="4h",
        )
        rejected["timestamps"]["filled"] = "2026-11-10T12:00:00"
        rejected["status"] = "filled"
        self.assertTrue(rejected.get("attempt_only"))
        self.assertEqual(rejected["attempt_id"], rejected["id"])
        self.assertEqual(rejected["correlation_id"], "corr-rej-1")
        self.assertFalse(order_is_fill(rejected))
        later = TradeOrder("BUY", "REJ/USDT", 1.0, 0, usdt_amount=25, source="lc")
        later.prior_attempt_id = rejected["attempt_id"]
        created = svc.create_from_request(later, timeframe="4h", status="executing", telegram_token="later-1")
        self.assertEqual(created["prior_attempt_id"], rejected["attempt_id"])
        self.assertFalse(created.get("attempt_only", False))
        stored = svc.get_by_id(rejected["id"])
        stored["status"] = "filled"
        stored["timestamps"]["filled"] = "2026-11-10T12:00:00"
        self.assertFalse(order_is_fill(stored))
        projected = sanitize_order(stored)
        self.assertFalse(projected["is_fill"])

    def test_ac_lin_4_mcp_projection_and_tenant_isolation(self):
        secret_lot = {
            "symbol": "LIN/USDT",
            "entry_source": "lc",
            "entry_snapshot": {"rsi": 81, "atr": 2, "volume_factor": 3, "source": "lc", "api_key": "hidden"},
            "entry_lineage": {
                "schema": SCHEMA,
                "rsi": 81,
                "atr": 2,
                "volume_factor": 3,
                "source": "lc",
                "entry_allocation_id": "ord:t1",
                "extended": {"oracle_state": "risk_off", "embedding": [1]},
                "api_key": "hidden",
            },
        }
        calls = []

        def snap(**kwargs):
            calls.append(kwargs["tenant_id"])
            if kwargs["tenant_id"] == "henry":
                return {"ok": True, "tenant_id": "henry", "lots": [secret_lot]}
            return {"ok": True, "tenant_id": kwargs["tenant_id"], "lots": []}

        from services.mcp.authz import Actor

        henry = Actor("henry-op", "operator", ("henry",), ("read",))
        other = Actor("other-op", "operator", ("other",), ("read",))
        henry_out = tool_lots(henry, tenant="henry", snapshot_fn=snap)
        other_out = tool_lots(other, tenant="other", snapshot_fn=snap)
        self.assertEqual(calls, ["henry", "other"])
        self.assertEqual(henry_out["lots"][0]["entry_lineage"]["rsi"], 81)
        self.assertEqual(other_out["lots"], [])
        self.assertNotIn("api_key", henry_out["lots"][0])
        self.assertNotIn("api_key", henry_out["lots"][0]["entry_snapshot"])
        self.assertNotIn("api_key", henry_out["lots"][0]["entry_lineage"])
        self.assertNotIn("api_key", str(public_lineage(secret_lot["entry_lineage"])))
        projected = public_lineage(secret_lot["entry_lineage"])
        self.assertNotIn("embedding", projected["extended"])
        self.assertEqual(projected["extended"]["oracle_state"], "risk_off")

        def orders(**kwargs):
            self.assertEqual(kwargs["tenant_id"], "henry")
            return [{
                "id": "o1",
                "status": "filled",
                "side": "sell",
                "symbol": "LIN/USDT",
                "tenant_id": "henry",
                "source": "lc",
                "attempt_id": "o1",
                "exit_lineage": {
                    "allocations": [{
                        "linked": True,
                        "entry_allocation_id": "ord:t1",
                        "entry_order_id": "buy-1",
                        "entry_source": "lc",
                        "exit_order_id": "o1",
                    }],
                },
                "request": {},
                "risk": {},
                "execution": {},
            }]

        listed = list_orders_public("henry", symbol="LIN", list_fn=orders)
        self.assertEqual(listed["orders"][0]["exit_lineage"]["allocations"][0]["entry_allocation_id"], "ord:t1")
        self.assertTrue(listed["orders"][0]["is_fill"])

    def test_ac_lin_4_legacy_lot_has_null_snapshot(self):
        raw = {
            "amount": 3,
            "average_entry": 1.5,
            "entry_source": "auto",
            "side": "long",
        }
        pos = _deserialize_position(raw)
        self.assertNotIn("entry_snapshot", pos)
        self.assertNotIn("entry_lineage", pos)
        self.assertEqual(pos["entry_source"], "auto")
        self.assertEqual(float(pos["amount"]), 3.0)

    def test_ac_lin_5_daily_check_export(self):
        self._buy(order_id="cov-buy")
        svc = OrderService()
        svc.create_from_request(
            TradeOrder("SELL", "LIN/USDT", 1.1, 2, signal="SELL"),
            timeframe="4h",
            status="executing",
            telegram_token="cov-sell",
        )
        PortfolioService().execute_sell(
            "LIN/USDT", "4h", 1.1, "SELL", amount=2, order_id="cov-sell", sync_virtual_ledger=False,
        )
        pos = get_position("LIN/USDT", "4h")
        pos["fully_supplied"] = True
        sell = svc.get_by_id("cov-sell")
        sell["status"] = "filled"
        report = lineage_coverage([pos], [sell])
        self.assertFalse(report["runtime_target_measured"])
        self.assertEqual(report["lots_total"], 1)
        self.assertEqual(report["missing_denominator"], 1)
        self.assertEqual(report["indicator_complete"], 1)
        self.assertEqual(report["by_source"]["lc"]["lots"], 1)
        self.assertEqual(report["fully_supplied_retained"], 1)
        self.assertEqual(report["exits_total"], 1)
        self.assertEqual(report["exits_linked"], 1)
        bare = {"entry_snapshot": {"source": "cmc"}, "entry_lineage": {"source": "cmc", "missing": {"rsi": "not_supplied", "atr": "not_supplied", "volume_factor": "not_supplied"}}}
        gap = lineage_coverage([bare], [])
        self.assertEqual(gap["lots_total"], 1)
        self.assertEqual(gap["indicator_complete"], 0)
        self.assertGreaterEqual(gap["missing_reasons"].get("rsi:not_supplied", 0), 1)

    def test_lineage_roundtrip_serialize_restart(self):
        self._buy(order_id="rt-1")
        before = get_position("LIN/USDT", "4h")
        payload = _serialize_positions()
        stored = payload["positions"]["LIN_USDT_4h"]
        self.assertEqual(stored["entry_snapshot"]["rsi"], 81)
        self.assertEqual(stored["entry_lineage"]["entry_allocation_id"], "ord:rt-1")
        clear_positions_memory()
        loaded = _deserialize_position(stored)
        self.assertEqual(loaded["entry_snapshot"]["atr"], 2)
        self.assertEqual(loaded["entry_lineage"]["volume_factor"], 3)
        self.assertEqual(loaded["entry_lineage"]["entry_order_id"], "rt-1")
        self.assertEqual(before["entry_snapshot"]["source"], loaded["entry_snapshot"]["source"])

    def test_risk_rebuild_keeps_decision_fields(self):
        class Analysis:
            rsi = 81
            atr = 2
            vol_multiplier = 3
            atr_pct = 4.5
            rationale = "because"
            timeframe = "4h"
            sources = ["lc"]
            decision_context = {"bb_upper": 9, "relvol_rank": 1, "secret_note": "nope"}

        original = TradeOrder("BUY", "LIN/USDT", 1.0, 0, usdt_amount=100, source="lc")
        original.entry_15m_vol_ratio = 2.5
        original.strategy_tier = "volatile"
        stamp_decision_on_order(original, Analysis(), timeframe="4h", source="lc")
        rebuilt = TradeOrder(
            "BUY", "LIN/USDT", 1.0, 0, usdt_amount=80, source="lc",
        )
        copy_decision_lineage(original, rebuilt)
        decision = RiskDecision(
            approved=True,
            order=rebuilt,
            size_multiplier=0.8,
            details={"planned_usdt": 100.0, "final_usdt": 80.0, "cap_applied": True},
        )
        stamp_size_from_decision(rebuilt, decision)
        self.assertEqual(rebuilt.decision_rsi, 81)
        self.assertEqual(rebuilt.decision_atr, 2)
        self.assertEqual(rebuilt.decision_volume_factor, 3)
        self.assertEqual(rebuilt.decision_atr_pct, 4.5)
        self.assertIsNone(rebuilt.entry_15m_vol_ratio)
        self.assertIsNone(rebuilt.strategy_tier)
        self.assertEqual(rebuilt.usdt_amount, 80)
        self.assertEqual(rebuilt.decision_size_before_mult, 100.0)
        self.assertEqual(rebuilt.decision_size_after_mult, 80.0)
        self.assertIs(rebuilt.decision_cap_applied, True)
        self.assertEqual(rebuilt.decision_context["bb_upper"], 9)
        self.assertNotIn("secret_note", rebuilt.decision_context)
        svc = OrderService()
        row = svc.create_from_request(rebuilt, timeframe="4h", status="executing", telegram_token="reb-1")
        self.assertEqual(row["entry_lineage"]["rsi"], 81)
        self.assertNotIn("atr", row["entry_lineage"]["missing"])
        clear_positions_memory()
        result = PortfolioService().execute_buy(
            "WIRE/USDT",
            "4h",
            1.0,
            80,
            source="lc",
            order_id="reb-1",
            sync_virtual_ledger=False,
        )
        self.assertTrue(result.executed)
        snap = get_position("WIRE/USDT", "4h")["entry_snapshot"]
        self.assertEqual(snap["rsi"], 81)
        self.assertEqual(snap["atr"], 2)
        self.assertEqual(snap["volume_factor"], 3)
        self.assertEqual(get_position("WIRE/USDT", "4h")["entry_lineage"]["extended"]["atr_pct"], 4.5)
        self.assertEqual(get_position("WIRE/USDT", "4h")["entry_lineage"]["entry_allocation_id"], "ord:reb-1")

    def test_non_whitelist_source_does_not_stamp_entry_at(self):
        for source in ("manual", "grid", "x", "dca_scheduled"):
            symbol = f"NW{source[:3].upper()}/USDT"
            result = PortfolioService().execute_buy(
                symbol, "4h", 1.0, 25, source=source, sync_virtual_ledger=False,
            )
            self.assertTrue(result.executed, source)
            pos = get_position(symbol, "4h")
            self.assertIsNone(pos.get("entry_source"), source)
            self.assertIsNone(pos.get("entry_at"), source)

    def test_signal_analysis_default_atr_pct_is_not_a_measurement(self):
        from core.models import SignalAnalysis
        from services.entry_lineage import decision_lineage_from_order

        analysis = SignalAnalysis(
            action="BUY",
            symbol="LIN/USDT",
            timeframe="4h",
            rsi=81,
            lower_bb=1.0,
            vol_multiplier=3,
            ampel_emoji="g",
            ampel_text="go",
            sources=["lc"],
            rationale="because",
        )
        self.assertEqual(analysis.atr_pct, 0.0)
        order = TradeOrder("BUY", "LIN/USDT", 1.0, 0, usdt_amount=40, source="lc")
        stamp_decision_on_order(order, analysis, timeframe="4h", source="lc")
        self.assertIsNone(order.decision_atr)
        self.assertIsNone(order.decision_atr_pct)
        self.assertEqual(order.decision_rsi, 81)
        self.assertEqual(order.decision_volume_factor, 3)
        block = decision_lineage_from_order(order, timeframe="4h")
        self.assertNotIn("atr_pct", block["extended"])
        self.assertIsNone(block["atr"])
        self.assertEqual(block["missing"]["atr"], "not_supplied")

    def test_volume_factor_conflict_is_missing(self):
        from services.entry_lineage import decision_lineage_from_order

        class Both:
            rsi = 81
            volume_factor = 3
            vol_multiplier = 9

        order = TradeOrder("BUY", "LIN/USDT", 1.0, 0, usdt_amount=40, source="lc")
        stamp_decision_on_order(order, Both(), timeframe="4h", source="lc")
        self.assertIsNone(order.decision_volume_factor)
        self.assertEqual(order.decision_volume_field, "conflict")
        block = decision_lineage_from_order(order, timeframe="4h")
        self.assertEqual(block["missing"]["volume_factor"], "volume_factor_conflict")

    def test_execute_if_needed_and_dca_stamp_the_order(self):
        from types import SimpleNamespace

        from core.models import SignalAnalysis, TradeResult
        from services.signal_orchestrator import SignalOrchestrator

        captured = []

        class Trading:
            def refresh(self):
                return None

            def execute_order(self, order, timeframe, **kwargs):
                captured.append(order)
                return TradeResult(True, "BUY", order.symbol, amount=1, price=1.0, usdt_amount=10)

        orch = SignalOrchestrator.__new__(SignalOrchestrator)
        orch.trading = Trading()
        orch.market = SimpleNamespace()
        orch.notify_callback = None
        orch.config = SimpleNamespace(raw={})
        analysis = SignalAnalysis(
            action="BUY",
            symbol="LIN/USDT",
            timeframe="4h",
            rsi=81,
            lower_bb=1.0,
            vol_multiplier=3,
            ampel_emoji="g",
            ampel_text="go",
            sources=["lc"],
            normalized_action="BUY",
            atr_pct=4.5,
            rationale="because",
        )
        with patch("services.signal_orchestrator.get_position", return_value={"amount": 0, "side": "long"}), \
             patch("services.signal_orchestrator.resolve_coin_config", return_value={}), \
             patch("hermes.memory.store.load_profile", return_value={}):
            orch.execute_if_needed(analysis, {"symbol": "LIN/USDT"}, 1.0)
        self.assertTrue(captured, "execute_if_needed did not build an order")
        self.assertEqual(captured[0].decision_rsi, 81)
        self.assertEqual(captured[0].decision_atr_pct, 4.5)
        self.assertEqual(captured[0].decision_volume_factor, 3)
        self.assertEqual(captured[0].source, "lc")
        self.assertIsNone(captured[0].decision_atr)

        buy = SimpleNamespace(
            symbol="DCA/USDT",
            timeframe="4h",
            source="lc",
            usdt_needed=12,
            score=1,
            rsi=70,
            atr=1.5,
            vol_multiplier=2,
        )
        plan = SimpleNamespace(
            buys=[buy], buy=buy, funding_sell=None, shadow_only=False, audit={},
        )
        class _Risk:
            def __init__(self, *_a, **_k):
                return None

            def _available_usdt(self):
                return 1000.0

        with patch("services.signal_orchestrator.portfolio_config", return_value={"enabled": True, "mode": "live"}), \
             patch("services.dca_sniper.config.dca_sniper_enabled", return_value=False), \
             patch("services.signal_orchestrator.build_portfolio_dca_plan", return_value=plan), \
             patch("risk.risk_manager.RiskManager", _Risk):
            orch.run_portfolio_dca_pass([{"symbol": "DCA/USDT"}], {"DCA/USDT": 1.0})
        self.assertEqual(captured[1].decision_rsi, 70)
        self.assertEqual(captured[1].decision_atr, 1.5)
        self.assertEqual(captured[1].decision_volume_factor, 2)
        self.assertEqual(captured[1].source, "lc")

    def test_short_and_cover_keep_allocation(self):
        opened = PortfolioService().execute_short(
            "SHLIN/USDT",
            "4h",
            2.0,
            40,
            source="lc",
            order_id="short-lin-1",
            lineage={"source": "lc", "rsi": 81, "atr": 2, "volume_factor": 3},
            sync_virtual_ledger=False,
        )
        self.assertTrue(opened.executed)
        pos = get_position("SHLIN/USDT", "4h")
        self.assertEqual(pos["entry_snapshot"]["rsi"], 81)
        self.assertEqual(pos["entry_snapshot"]["atr"], 2)
        self.assertEqual(pos["entry_lineage"]["entry_allocation_id"], "ord:short-lin-1")
        svc = OrderService()
        svc.create_from_request(
            TradeOrder("COVER", "SHLIN/USDT", 1.8, 5, signal="COVER"),
            timeframe="4h",
            status="executing",
            telegram_token="cover-lin-1",
        )
        covered = PortfolioService().execute_cover(
            "SHLIN/USDT",
            "4h",
            1.8,
            amount=float(pos["amount"]),
            order_id="cover-lin-1",
            sync_virtual_ledger=False,
        )
        self.assertTrue(covered.executed)
        row = svc.get_by_id("cover-lin-1")
        self.assertEqual(row["exit_lineage"]["allocations"][0]["entry_allocation_id"], "ord:short-lin-1")
        self.assertEqual(row["exit_lineage"]["allocations"][0]["entry_source"], "lc")

    def test_active_lot_projection_drops_secrets(self):
        self._buy(order_id="proj-1")
        from strategies.positions import _active_lot_from_store_key, get_key, list_active_positions

        key = get_key("LIN/USDT", "4h")
        raw = get_position("LIN/USDT", "4h")
        raw["entry_lineage"]["extended"]["embedding"] = [0.1]
        raw["api_key"] = "hidden"
        projected = _active_lot_from_store_key(key, raw)
        self.assertEqual(projected["entry_lineage"]["rsi"], 81)
        self.assertNotIn("embedding", projected["entry_lineage"].get("extended") or {})
        self.assertNotIn("api_key", projected)
        listed = list_active_positions()
        match = [lot for lot in listed if lot.get("symbol") == "LIN/USDT"]
        self.assertTrue(match)
        self.assertEqual(match[0]["entry_snapshot"]["atr"], 2)
        self.assertIn("entry_lineage", match[0])

    def test_execute_order_locked_copies_decision_not_guard_inputs(self):
        from core.models import TradeResult
        from services.order_service import OrderService
        from services.trading_service import TradingService

        svc = TradingService()
        request = TradeOrder("BUY", "LOCK/USDT", 1.0, 0, usdt_amount=100, source="lc")
        request.decision_rsi = 81
        request.decision_atr = 2
        request.decision_volume_factor = 3
        request.decision_source = "lc"
        request.entry_15m_vol_ratio = 2.5
        request.strategy_tier = "volatile"
        rebuilt = TradeOrder("BUY", "LOCK/USDT", 1.0, 0, usdt_amount=80, source="lc")
        seen = {}

        class _Adapter:
            mode = "shadow"

            def execute(self, order, timeframe):
                seen["order"] = order
                return TradeResult(True, "BUY", order.symbol, amount=1, price=1.0, usdt_amount=80)

        def _create(self, order, **kwargs):
            seen["created"] = order
            return {"id": "ledger-lock-1"}

        with patch("bus.writer_lease.require_lease_for_order"), \
             patch.object(svc, "refresh", return_value=None), \
             patch.object(svc, "can_execute", return_value=(True, "")), \
             patch.object(svc.risk, "evaluate", return_value=RiskDecision(approved=True, order=rebuilt, details={"planned_usdt": 100.0, "final_usdt": 80.0})), \
             patch("services.trading_service.get_execution_adapter", return_value=_Adapter()), \
             patch.object(OrderService, "find_by_idempotency_key", return_value=None), \
             patch.object(OrderService, "create_from_request", _create), \
             patch.object(OrderService, "link_execution_result", return_value=None):
            result = svc._execute_order_locked(request, "4h", source="lc")
        self.assertTrue(result.executed, getattr(result, "message", result))
        approved = seen["order"]
        self.assertEqual(approved.decision_rsi, 81)
        self.assertEqual(approved.decision_atr, 2)
        self.assertEqual(approved.decision_volume_factor, 3)
        self.assertIsNone(approved.entry_15m_vol_ratio)
        self.assertIsNone(approved.strategy_tier)
        self.assertEqual(approved.usdt_amount, 80)
        self.assertEqual(approved.decision_size_before_mult, 100.0)
        self.assertEqual(approved.decision_size_after_mult, 80.0)

    def test_missing_inputs_still_fill(self):
        result = PortfolioService().execute_buy(
            "NONE/USDT", "4h", 1.0, 25, source="auto", sync_virtual_ledger=False,
        )
        self.assertTrue(result.executed)
        pos = get_position("NONE/USDT", "4h")
        self.assertGreater(float(pos["amount"]), 0)
        self.assertIsNone(pos["entry_snapshot"]["rsi"])
        self.assertEqual(pos["entry_source"], "auto")


if __name__ == "__main__":
    unittest.main()
