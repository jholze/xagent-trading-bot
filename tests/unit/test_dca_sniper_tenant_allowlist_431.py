"""#431 — dca-sniper is one unscoped actor; it must only own allowlisted tenants.

Before the fix `run_portfolio_dca_pass` returned ``dca_sniper_authority`` for
*every* tenant as soon as the sniper was enabled globally (or via
``DCA_SNIPER_ENABLED=1``), so non-default tenants (henry/ctexp) silently lost
portfolio rotation DCA every cycle. These tests would have failed under that
global skip.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.tenant_context import tenant_context  # noqa: E402

SNIPER_ON = {"dca_sniper": {"enabled": True, "disable_cycle_dca_when_enabled": True}}
SNIPER_ON_HENRY = {
    "dca_sniper": {
        "enabled": True,
        "disable_cycle_dca_when_enabled": True,
        "tenants": ["default", "henry"],
    }
}
SNIPER_OFF_IN_CONFIG = {"dca_sniper": {"enabled": False}}

_ENV_KEYS = ("DCA_SNIPER_ENABLED", "RUN_DCA_SNIPER", "DCA_SNIPER_FORCE")


def _clean_env():
    return patch.dict(os.environ, {k: "" for k in _ENV_KEYS}, clear=False)


class TestTenantAllowlistHelper(unittest.TestCase):
    def test_missing_empty_or_malformed_list_falls_back_to_default(self):
        from services.dca_sniper.config import dca_sniper_tenants

        self.assertEqual(dca_sniper_tenants({}), ["default"])
        self.assertEqual(dca_sniper_tenants({"dca_sniper": {}}), ["default"])
        self.assertEqual(dca_sniper_tenants({"dca_sniper": {"tenants": []}}), ["default"])
        self.assertEqual(dca_sniper_tenants({"dca_sniper": {"tenants": None}}), ["default"])
        self.assertEqual(dca_sniper_tenants({"dca_sniper": {"tenants": 42}}), ["default"])
        self.assertEqual(
            dca_sniper_tenants({"dca_sniper": {"tenants": ["", None, "  "]}}), ["default"]
        )

    def test_explicit_list_is_normalised(self):
        from services.dca_sniper.config import dca_sniper_tenants

        self.assertEqual(
            dca_sniper_tenants({"dca_sniper": {"tenants": [" henry ", "default", "henry"]}}),
            ["henry", "default"],
        )
        self.assertEqual(dca_sniper_tenants({"dca_sniper": {"tenants": "henry"}}), ["henry"])

    def test_dca_sniper_config_surfaces_tenants(self):
        from services.dca_sniper.config import dca_sniper_config

        self.assertEqual(dca_sniper_config(SNIPER_ON)["tenants"], ["default"])
        self.assertEqual(dca_sniper_config(SNIPER_ON_HENRY)["tenants"], ["default", "henry"])

    def test_owns_default_only_unless_listed(self):
        from services.dca_sniper.config import sniper_owns_tenant

        # no tenant context → DEFAULT_TENANT
        self.assertTrue(sniper_owns_tenant(SNIPER_ON))
        with tenant_context("henry", scope="demo"):
            self.assertFalse(sniper_owns_tenant(SNIPER_ON))
            self.assertTrue(sniper_owns_tenant(SNIPER_ON_HENRY))
        # explicit tenant_id wins over context
        self.assertFalse(sniper_owns_tenant(SNIPER_ON, tenant_id="ctexp"))
        self.assertTrue(sniper_owns_tenant(SNIPER_ON_HENRY, tenant_id="henry"))

    def test_checked_config_json_lists_only_default(self):
        import json

        raw = json.loads((_ROOT / "config.json").read_text())
        self.assertEqual(raw["dca_sniper"]["tenants"], ["default"])


class TestSkipsPortfolioDcaTenantAware(unittest.TestCase):
    """`sniper_skips_portfolio_dca` feeds the decision_engine deferral; it must
    agree with the orchestrator gate or henry would double-DCA."""

    def test_default_still_skips_henry_does_not(self):
        from services.dca_sniper.config import sniper_skips_portfolio_dca

        with _clean_env():
            self.assertTrue(sniper_skips_portfolio_dca(SNIPER_ON))
            with tenant_context("henry", scope="demo"):
                self.assertFalse(sniper_skips_portfolio_dca(SNIPER_ON))
                self.assertTrue(sniper_skips_portfolio_dca(SNIPER_ON_HENRY))

    def test_env_short_circuit_does_not_widen_ownership(self):
        from services.dca_sniper.config import dca_sniper_enabled, sniper_skips_portfolio_dca

        with patch.dict(os.environ, {"DCA_SNIPER_ENABLED": "1"}):
            # env wins over config for *enabled* (existing behaviour)…
            self.assertTrue(dca_sniper_enabled(SNIPER_OFF_IN_CONFIG))
            self.assertTrue(sniper_skips_portfolio_dca(SNIPER_OFF_IN_CONFIG))
            # …but never for tenant ownership
            with tenant_context("henry", scope="demo"):
                self.assertFalse(sniper_skips_portfolio_dca(SNIPER_OFF_IN_CONFIG))
                self.assertFalse(sniper_skips_portfolio_dca(SNIPER_ON))
        with patch.dict(os.environ, {"RUN_DCA_SNIPER": "1", "DCA_SNIPER_ENABLED": ""}):
            with tenant_context("henry", scope="demo"):
                self.assertFalse(sniper_skips_portfolio_dca(SNIPER_OFF_IN_CONFIG))

    def test_owns_cycle_dca_follows_tenant(self):
        from services.dca_sniper.config import sniper_owns_cycle_dca

        with _clean_env():
            self.assertTrue(sniper_owns_cycle_dca(SNIPER_ON, {"sniper_focus": True}))
            with tenant_context("henry", scope="demo"):
                self.assertFalse(sniper_owns_cycle_dca(SNIPER_ON, {"sniper_focus": True}))


def _orchestrator(raw: dict):
    """Bare SignalOrchestrator with a fake config (no services constructed)."""
    from services.signal_orchestrator import SignalOrchestrator

    orch = SignalOrchestrator.__new__(SignalOrchestrator)
    raw = dict(raw)
    raw.setdefault("volatile_altcoin", {"dca": {"portfolio": {"enabled": True, "mode": "shadow"}}})
    orch.config = SimpleNamespace(raw=raw, trading_mode="demo")
    orch.market = MagicMock()
    return orch


def _run_pass(orch):
    """Run the pass with RiskManager + planner stubbed so it returns fast if not gated."""
    plan = MagicMock()
    plan.buys = []
    plan.buy = None
    plan.audit = {"stub": True}
    with patch("risk.risk_manager.RiskManager") as rm, patch(
        "services.signal_orchestrator.build_portfolio_dca_plan", return_value=plan
    ) as planner:
        rm.return_value._available_usdt.return_value = 1000.0
        result = orch.run_portfolio_dca_pass([], {}, quiet=True)
    return result, planner


class TestPortfolioPassTenantGate(unittest.TestCase):
    def test_henry_runs_portfolio_dca_with_sniper_enabled_globally(self):
        orch = _orchestrator(SNIPER_ON)
        with _clean_env(), tenant_context("henry", scope="demo"):
            result, planner = _run_pass(orch)
        self.assertNotEqual(result.get("reason"), "dca_sniper_authority", result)
        self.assertFalse(result.get("skipped"), result)
        planner.assert_called_once()

    def test_env_enabled_1_does_not_skip_henry(self):
        orch = _orchestrator(SNIPER_OFF_IN_CONFIG)
        with patch.dict(os.environ, {"DCA_SNIPER_ENABLED": "1"}), tenant_context(
            "henry", scope="demo"
        ):
            result, planner = _run_pass(orch)
        self.assertNotEqual(result.get("reason"), "dca_sniper_authority", result)
        planner.assert_called_once()

    def test_default_tenant_still_skips(self):
        orch = _orchestrator(SNIPER_ON)
        with _clean_env():
            result, planner = _run_pass(orch)
        self.assertTrue(result.get("skipped"))
        self.assertEqual(result.get("reason"), "dca_sniper_authority")
        self.assertEqual(result.get("tenant"), "default")
        self.assertTrue(result.get("sniper_standalone"))
        planner.assert_not_called()

    def test_default_tenant_skips_via_env_too(self):
        orch = _orchestrator(SNIPER_OFF_IN_CONFIG)
        with patch.dict(os.environ, {"DCA_SNIPER_ENABLED": "1"}):
            result, planner = _run_pass(orch)
        self.assertEqual(result.get("reason"), "dca_sniper_authority")
        planner.assert_not_called()

    def test_henry_skips_when_listed(self):
        orch = _orchestrator(SNIPER_ON_HENRY)
        with _clean_env(), tenant_context("henry", scope="demo"):
            result, planner = _run_pass(orch)
        self.assertEqual(result.get("reason"), "dca_sniper_authority")
        self.assertEqual(result.get("tenant"), "henry")
        planner.assert_not_called()

    def test_disable_cycle_dca_false_never_skips(self):
        raw = {"dca_sniper": {"enabled": True, "disable_cycle_dca_when_enabled": False}}
        orch = _orchestrator(raw)
        with _clean_env():
            result, planner = _run_pass(orch)
        self.assertNotEqual(result.get("reason"), "dca_sniper_authority")
        planner.assert_called_once()

    def test_in_process_tick_not_fired_for_foreign_tenant(self):
        """Redis/default state must not move on a henry cycle: the in-process
        sniper tick is never entered when henry is not on the allowlist."""
        raw = {
            "dca_sniper": {
                "enabled": True,
                "disable_cycle_dca_when_enabled": True,
                "in_process_tick": True,
            }
        }
        orch = _orchestrator(raw)
        tick = MagicMock(return_value={"mode": "in_process"})
        fake_inproc = MagicMock(maybe_tick_dca_sniper=tick)
        with _clean_env(), patch.dict(
            os.environ, {"DCA_SNIPER_IN_PROCESS": ""}
        ), patch.dict(
            "sys.modules", {"services.dca_sniper.inprocess": fake_inproc}
        ):
            with tenant_context("henry", scope="demo"):
                result, _ = _run_pass(orch)
            tick.assert_not_called()
            self.assertNotEqual(result.get("reason"), "dca_sniper_authority")
            # default tenant with the same config does tick (unchanged behaviour)
            result_default, _ = _run_pass(orch)
            tick.assert_called_once()
            self.assertEqual(result_default.get("reason"), "dca_sniper_authority")
            self.assertFalse(result_default.get("sniper_standalone"))

    def test_authority_check_error_is_logged_not_swallowed(self):
        orch = _orchestrator(SNIPER_ON)
        with _clean_env(), patch(
            "services.dca_sniper.config.dca_sniper_config",
            side_effect=RuntimeError("boom-431"),
        ), patch("services.signal_orchestrator.log") as log:
            result, planner = _run_pass(orch)
        # falls through to the normal pass (pre-existing behaviour) …
        self.assertNotEqual(result.get("reason"), "dca_sniper_authority")
        planner.assert_called_once()
        # … but the failure is now visible.
        levels = [c.args[1] for c in log.call_args_list if len(c.args) > 1]
        msgs = " | ".join(str(c.args[0]) for c in log.call_args_list)
        self.assertTrue(any(lvl in ("WARNING", "ERROR") for lvl in levels), log.call_args_list)
        self.assertIn("boom-431", msgs)


class TestBotHttpTenantRefusal(unittest.TestCase):
    def test_execute_refuses_foreign_tenant_before_touching_lots(self):
        from services.dca_sniper.bot_http import execute_sniper_dca

        fake_ts = MagicMock()
        with _clean_env(), patch.dict(
            "sys.modules", {"services.trading_service": fake_ts}
        ), patch("strategies.positions.get_position") as get_pos, patch(
            "strategies.positions.flush_positions"
        ) as flush, patch("strategies.position_gates.dca_add_blocked") as gate, patch(
            "services.dca_sniper.bot_http.log"
        ) as log:
            with tenant_context("henry", scope="demo"):
                body, code = execute_sniper_dca(
                    {"symbol": "Y/USDT", "timeframe": "1h", "usdt": 500, "price": 0.8}
                )
        self.assertEqual(code, 403, body)
        self.assertFalse(body.get("executed"))
        self.assertEqual(body.get("code"), "tenant_not_allowlisted")
        self.assertEqual(body.get("tenant"), "henry")
        get_pos.assert_not_called()
        gate.assert_not_called()
        flush.assert_not_called()
        fake_ts.TradingService.assert_not_called()
        self.assertTrue(
            any(len(c.args) > 1 and c.args[1] == "WARNING" for c in log.call_args_list),
            log.call_args_list,
        )

    def test_execute_allows_listed_tenant(self):
        """With henry on the allowlist the request reaches the (fail-closed) lock check."""
        from services.dca_sniper.bot_http import execute_sniper_dca

        with _clean_env(), patch(
            "services.dca_sniper.bot_http.sniper_owns_tenant", return_value=True
        ), patch("strategies.positions.get_position", return_value={"amount": 1}), patch(
            "strategies.position_gates.dca_add_blocked", return_value=(True, "position_locked_no_dca")
        ):
            with tenant_context("henry", scope="demo"):
                body, code = execute_sniper_dca(
                    {"symbol": "Y/USDT", "timeframe": "1h", "usdt": 500, "price": 0.8}
                )
        self.assertEqual(code, 409, body)
        self.assertEqual(body.get("code"), "position_locked")

    def test_execute_bad_args_still_400_under_foreign_tenant(self):
        from services.dca_sniper.bot_http import execute_sniper_dca

        with tenant_context("henry", scope="demo"):
            body, code = execute_sniper_dca({})
        self.assertEqual(code, 400)
        self.assertEqual(body.get("message"), "bad_args")

    def test_execute_lock_check_still_fails_closed_for_default(self):
        from services.dca_sniper.bot_http import execute_sniper_dca

        with _clean_env(), patch(
            "strategies.positions.get_position", side_effect=RuntimeError("db down")
        ):
            body, code = execute_sniper_dca(
                {"symbol": "Y/USDT", "timeframe": "1h", "usdt": 500, "price": 0.8}
            )
        self.assertEqual(code, 503, body)
        self.assertEqual(body.get("code"), "position_lock_check_error")

    def test_tenant_check_error_refuses(self):
        from services.dca_sniper.bot_http import execute_sniper_dca

        with patch(
            "services.dca_sniper.bot_http.sniper_owns_tenant",
            side_effect=RuntimeError("cfg broken"),
        ), patch("strategies.positions.get_position") as get_pos:
            body, code = execute_sniper_dca(
                {"symbol": "Y/USDT", "timeframe": "1h", "usdt": 500, "price": 0.8}
            )
        self.assertEqual(code, 503, body)
        self.assertEqual(body.get("code"), "tenant_check_error")
        get_pos.assert_not_called()

    def test_fund_sell_refuses_foreign_tenant_before_touching_lots(self):
        from services.dca_sniper.bot_http import execute_fund_sell

        fake_ts = MagicMock()
        with _clean_env(), patch.dict(
            "sys.modules", {"services.trading_service": fake_ts}
        ), patch("strategies.positions.get_position") as get_pos:
            with tenant_context("henry", scope="demo"):
                body, code = execute_fund_sell(
                    {"symbol": "X/USDT", "timeframe": "1h", "price": 1.2}
                )
        self.assertEqual(code, 403, body)
        self.assertEqual(body.get("code"), "tenant_not_allowlisted")
        get_pos.assert_not_called()
        fake_ts.TradingService.assert_not_called()


if __name__ == "__main__":
    unittest.main()
