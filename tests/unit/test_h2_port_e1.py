"""H2 port of #723 annex B erratum E1 tests (spec_723_partial_escalation/tests/test_723_annex_b_e1.py, credit Kay/Lena/
Viktor). Base staging 930792a3 + proto_fix_h2.py. Same ACs E1-E11; adapted to the H2 rule (is_hard_stop_order, WS included):
  E4 (WS)  partial tick now escalates on WS -> exactly one FULL; the following full tick finds nothing (still one FULL).
  E10      INVERTED: WS escalates (P723-NOWS removed), the next cycle does not sell again.
All other classes unchanged except symbol suffix 723 -> H2 and helper imports (h2_port_helpers.py).
"""

from __future__ import annotations

import ast
import os
from unittest.mock import patch

import pytest

from h2_port_helpers import (  # noqa: F401  (autouse fixture imported by name)
    BAD_CAPS, CAP_KEY, ENTRY, FULL_ZONE_PX, HBAR_AMOUNT, HBAR_PEAK, HBAR_PX, MAX_SOLD, TENANTS, TF, _POP, _analysis,
    _assert_one_full, _cycle, _errors, _filled_sells, _fire, _full_analysis, _guard_call, _held, _hub, _key_missing,
    _logs_lvl, _orders, _paper, _params, _prod_floors_and_clean_books, _repo_config, _rm, _row, _seed_t,
    _strategy_override, _tenant,
)


# ----------------------------------------------------------------------------- E1
class TestE1NoStartupAbort:
    @pytest.mark.parametrize("tid", TENANTS)
    def test_report_logs_error_and_returns_without_systemexit(self, tid):
        from core.config_guardrails import report_missing_required_keys

        cfg = _repo_config()
        cfg["risk"].pop(CAP_KEY)
        with _logs_lvl() as lines:
            try:
                bad = report_missing_required_keys(cfg, tenant=tid)
            except SystemExit as exc:  # pragma: no cover - the mutant path
                pytest.fail(f"startup abort: SystemExit({exc.code})")
        assert bad == ["risk." + CAP_KEY]
        assert _errors(lines, "config_required_key_missing", CAP_KEY, f"tenant={tid}"), lines

    def test_report_without_args_reads_the_runtime_default_config(self):
        """Prod call path (aria_bot passes no config): the report must read get_config() itself."""
        import data_manager
        from core.config_guardrails import report_missing_required_keys

        risk = data_manager.get_config().setdefault("risk", {})
        prev = risk.pop(CAP_KEY, None)
        try:
            with _logs_lvl() as lines:
                bad = report_missing_required_keys()
        finally:
            risk[CAP_KEY] = prev
        assert bad == ["risk." + CAP_KEY]
        assert _errors(lines, "config_required_key_missing", CAP_KEY, "tenant=default"), lines

    def test_report_is_silent_on_repo_config(self):
        from core.config_guardrails import report_missing_required_keys

        with _logs_lvl() as lines:
            assert report_missing_required_keys(_repo_config()) == []
        assert not _errors(lines, "config_required_key_missing"), lines

    def test_aria_bot_reports_before_the_orchestrator_and_never_exits_there(self):
        tree = ast.parse(open(os.path.join(os.getcwd(), "aria_bot.py")).read())
        found = False
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if not isinstance(body, list):
                continue
            for i, stmt in enumerate(body):
                if (isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call)
                        and getattr(stmt.value.func, "id", None) == "SignalOrchestrator"
                        and any(getattr(t, "id", None) == "orchestrator" for t in stmt.targets)):
                    found = True
                    prev = body[i - 1]
                    src = ast.unparse(prev)
                    assert "report_missing_required_keys()" in src, src
                    assert "assert_startup_required_keys" not in src, src
                    # the call must be a direct statement of the try body (not nested in an if/def/loop;
                    # kills e1m_report_in_dead_branch, e1 adaptation of Viktor's vk723b_startup_call_after_orch)
                    direct = [st for st in (prev.body if isinstance(prev, ast.Try) else [prev])
                              if isinstance(st, ast.Expr) and isinstance(st.value, ast.Call)
                              and ast.unparse(st.value.func).endswith("report_missing_required_keys")]
                    assert len(direct) == 1, src
                    for sub in ast.walk(prev):
                        assert not isinstance(sub, ast.Raise), f"raise before orchestrator: {src}"
                        if isinstance(sub, ast.Call):
                            name = ast.unparse(sub.func)
                            assert name not in ("sys.exit", "exit", "os._exit", "SystemExit"), src
        assert found


# ----------------------------------------------------------------------------- E2 + E3
class TestE2E3HardPartialKeyMissingIsOneFull:
    @pytest.mark.parametrize("tid", TENANTS)
    @pytest.mark.parametrize("lot_usdt", [250.0, 700.0, 1400.0], ids=["small_lot", "lot_700", "lot_1400"])
    def test_exactly_one_full_error_logged_then_nothing_more(self, tid, lot_usdt):
        sym = f"E2{tid[:1].upper()}{int(lot_usdt)}H2/USDT"
        amount = lot_usdt / HBAR_PX
        with _tenant(tid), _key_missing():
            _seed_t(sym, amount, peak_amount=amount * 2, sold_percent=0.5)
            with _paper(), _logs_lvl() as lines:
                res = _cycle(sym, HBAR_PX)
                assert res is not None and res.executed, getattr(res, "message", None)
                _assert_one_full(sym, amount)
                assert not [o for o in _filled_sells(sym) if o.get("signal") == "SELL_STOP_PARTIAL"]
                assert _errors(lines, "partial_stop_upgraded_to_full", "block=sold_cap_unconfigured", f"tenant={tid}"), lines
                assert _errors(lines, "partial_sell_cap_missing", CAP_KEY), lines
                # E3: WS tick in the partial zone and a second cycle after the FULL -> no second FULL
                hub = _hub(cooldown=0.0)
                from strategies.positions import get_position

                row = _row(sym, dict(get_position(sym, TF) or {}))
                if tid != "default":
                    row["tenant_id"] = tid
                hub.update_book([row])
                _fire(hub, sym, HBAR_PX)
                _fire(hub, sym, FULL_ZONE_PX)
                _cycle(sym, HBAR_PX)
                _cycle(sym, FULL_ZONE_PX, analysis=_full_analysis(sym))
            fulls = [o for o in _filled_sells(sym) if o.get("signal") == "SELL_STOP_FULL"]
            assert len(fulls) == 1, [(o.get("signal"), o.get("status")) for o in _orders(sym)]
            assert _held(sym) == pytest.approx(0.0, abs=1e-9)


class TestE2InvalidValuesEscalateToo:
    """Replaces annex B test_missing_or_invalid_key_has_no_default_and_blocks_partials[7] (superseded)."""

    @pytest.mark.parametrize("tid", TENANTS)
    @pytest.mark.parametrize("bad", BAD_CAPS)
    def test_missing_or_invalid_key_hard_partial_is_one_full(self, bad, tid):
        sym = "E2V" + tid[:1].upper() + "".join(c for c in repr(bad) if c.isalnum())[:8].upper() + "H2/USDT"
        amount = 250.0 / HBAR_PX
        with _tenant(tid), _key_missing(bad):
            _seed_t(sym, amount, peak_amount=amount * 2, sold_percent=0.5)
            with _paper(), _logs_lvl() as lines:
                res = _cycle(sym, HBAR_PX)
            assert res is not None and res.executed, getattr(res, "message", None)
            _assert_one_full(sym, amount)
            assert _errors(lines, "partial_stop_upgraded_to_full", "block=sold_cap_unconfigured"), lines
            assert _errors(lines, "partial_sell_cap_missing", CAP_KEY), lines


# ----------------------------------------------------------------------------- E4
class TestE4FullThresholdWithoutPartialContext:
    @pytest.mark.parametrize("tid", TENANTS)
    def test_cycle_full_stop_with_key_missing(self, tid):
        sym = f"E4C{tid[:1].upper()}H2/USDT"
        with _tenant(tid), _key_missing():
            _seed_t(sym, HBAR_AMOUNT, peak_amount=HBAR_PEAK, sold_percent=0.5)
            with _paper():
                res = _cycle(sym, FULL_ZONE_PX, analysis=_full_analysis(sym))
                assert res is not None and res.executed, getattr(res, "message", None)
                _assert_one_full(sym, HBAR_AMOUNT)

    @pytest.mark.parametrize("tid", TENANTS)
    def test_ws_partial_rejected_then_full_tick_sells_the_same_lot(self, tid):
        sym = f"E4W{tid[:1].upper()}H2/USDT"
        with _tenant(tid), _key_missing():
            lot = _seed_t(sym, HBAR_AMOUNT, peak_amount=HBAR_PEAK, sold_percent=0.5)
            hub = _hub(cooldown=30.0)
            row = _row(sym, lot)
            if tid != "default":
                row["tenant_id"] = tid
            hub.update_book([row])
            with _paper():
                _fire(hub, sym, HBAR_PX)  # H2: partial zone on WS escalates to one FULL on WS
                _assert_one_full(sym, HBAR_AMOUNT)
                _fire(hub, sym, FULL_ZONE_PX)  # full zone, same lot: nothing left, still one FULL
                _assert_one_full(sym, HBAR_AMOUNT)


# ----------------------------------------------------------------------------- E5
class TestE5KeyPresentC3Unchanged:
    @pytest.mark.parametrize("tid", TENANTS)
    @pytest.mark.parametrize("cap", [MAX_SOLD, 0.95], ids=["cap_repo", "cap_0.95"])
    def test_sold_at_configured_cap_is_not_escalated(self, cap, tid):
        sym = f"E5{tid[:1].upper()}{int(round(cap * 1000))}H2/USDT"
        small = 200.0 / HBAR_PX
        with _tenant(tid), _key_missing(cap):
            _seed_t(sym, small, peak_amount=small / (1.0 - cap), sold_percent=cap)
            with _paper(), _logs_lvl() as lines:
                order, blocked, msg = _guard_call(sym, small * 0.5)
                assert blocked and order._partial_sell_block_code == "sold_cap", msg
                res = _cycle(sym, HBAR_PX)
            assert res is not None and not res.executed and res.code == "partial_sell_guard"
            assert _filled_sells(sym) == []
            assert _held(sym) == pytest.approx(small)
            assert not [m for _, m in lines if "partial_stop_upgraded_to_full" in m], lines
            assert not _errors(lines, "partial_sell_cap_missing"), lines


# ----------------------------------------------------------------------------- E6
class TestE6NonHardPartialKeyMissingStaysBlocked:
    def test_take_profit_partial_is_blocked_not_escalated(self):
        sym = "E6TPH2/USDT"
        amount = 700.0 / HBAR_PX
        _seed_t(sym, amount)
        tp = _analysis(sym, action="SELL_PARTIAL_50", long_hard_stop=False, sell_source="take_profit",
                       sources=("technical",), normalized_action="SELL_PARTIAL_50")
        with _key_missing(), _paper():
            res = _cycle(sym, 1.3, analysis=tp)
        assert res is not None and not res.executed and res.code == "partial_sell_guard", (res.code, res.message)
        assert _filled_sells(sym) == []
        assert _held(sym) == pytest.approx(amount)


# ----------------------------------------------------------------------------- E7
class TestE7GuardrailAcceptsRepoConfig:
    def test_repo_config_and_valid_caps(self):
        from core.config_guardrails import assert_required_keys_present

        cfg = _repo_config()
        assert cfg["risk"][CAP_KEY] == pytest.approx(MAX_SOLD)
        assert_required_keys_present(cfg)
        for ok in (0.5, 0.95, 1, 1.0):
            cfg["risk"][CAP_KEY] = ok
            assert_required_keys_present(cfg)


# ----------------------------------------------------------------------------- E8 (Lena lb2)
class TestE8InvalidStrategyOverrideFailsClosed:
    """Annex B section 5.3(3): an invalid strategy override does NOT fall back to the risk value."""

    @pytest.mark.parametrize("tid", TENANTS)
    @pytest.mark.parametrize("bad", [1.5, 0, "0.9"], ids=["above_one", "zero", "string"])
    def test_invalid_override_is_unconfigured_not_risk_value(self, bad, tid):
        sym = "E8" + tid[:1].upper() + "".join(c for c in repr(bad) if c.isalnum())[:5].upper() + "H2/USDT"
        with _tenant(tid), _strategy_override(bad), _logs_lvl() as lines:
            assert _rm()._partial_sell_limits(sym, TF)["max_sold_percent"] is None
        assert _errors(lines, "partial_sell_cap_missing", "invalid strategy=", f"tenant={tid}"), lines

    @pytest.mark.parametrize("tid", TENANTS)
    def test_invalid_override_big_lot_is_one_full_not_a_partial(self, tid):
        sym = f"E8BIG{tid[:1].upper()}H2/USDT"
        amount = 1400.0 / HBAR_PX  # with the risk value 0.99 this partial would be allowed
        with _tenant(tid), _strategy_override(1.5):
            _seed_t(sym, amount, peak_amount=amount * 2, sold_percent=0.5)
            with _paper(), _logs_lvl() as lines:
                res = _cycle(sym, HBAR_PX, params={**_params(), CAP_KEY: 1.5})
            assert res is not None and res.executed, getattr(res, "message", None)
            _assert_one_full(sym, amount)
            assert not [o for o in _filled_sells(sym) if o.get("signal") == "SELL_STOP_PARTIAL"]
            assert _errors(lines, "partial_stop_upgraded_to_full", "block=sold_cap_unconfigured"), lines


# ----------------------------------------------------------------------------- E9 (Lena lb4)
class TestE9OneErrorLinePerTenant:
    def test_same_symbol_logs_once_for_default_and_once_for_henry(self):
        
        sym = "E9DEDUPH2/USDT"
        with _logs_lvl() as lines:
            for tid in ("default", "henry", "default", "henry"):
                with _tenant(tid), _key_missing():
                    assert _rm()._partial_sell_limits(sym, TF)["max_sold_percent"] is None
        got = [m for m in _errors(lines, "partial_sell_cap_missing", sym)]
        assert len([m for m in got if "tenant=default" in m]) == 1, got
        assert len([m for m in got if "tenant=henry" in m]) == 1, got
        assert len(got) == 2, got


# ----------------------------------------------------------------------------- E10 (H2: INVERTED, WS escalates)
class TestE10H2WsEscalatesNextCycleNothingMore:
    """#723 E10 asserted 'WS never escalates' (P723-NOWS). H2 removes NOWS: the WS tick itself closes FULL with the
    ERROR upgrade line (key missing), the next cycle finds nothing to sell."""

    @pytest.mark.parametrize("tid", TENANTS)
    def test_ws_partial_escalates_then_next_cycle_no_second_fill(self, tid):
        sym = f"E10W{tid[:1].upper()}H2/USDT"
        with _tenant(tid), _key_missing():
            lot = _seed_t(sym, HBAR_AMOUNT, peak_amount=HBAR_PEAK, sold_percent=0.5)
            hub = _hub(cooldown=0.0)
            row = _row(sym, lot)
            if tid != "default":
                row["tenant_id"] = tid
            hub.update_book([row])
            with _paper(), _logs_lvl() as lines:
                _fire(hub, sym, HBAR_PX)  # partial band on WS
                _assert_one_full(sym, HBAR_AMOUNT)
                assert _errors(lines, "partial_stop_upgraded_to_full", "block=sold_cap_unconfigured",
                               f"tenant={tid}", "source=exit_ws"), lines
                res = _cycle(sym, HBAR_PX)  # next cycle, same price band
            assert res is None or not res.executed, getattr(res, "message", None)
            _assert_one_full(sym, HBAR_AMOUNT)


# ----------------------------------------------------------------------------- E11 (Viktor: FULL exits untouched)
class TestE11FullExitsUntouchedWithKeyMissing:
    @pytest.mark.parametrize("tid", TENANTS)
    @pytest.mark.parametrize("signal", ["SELL_STOP_FULL", "SELL_FULL", "COVER_FULL"])
    def test_guard_does_not_block_full_exits(self, signal, tid):
        from core.models import TradeOrder
        
        sym = f"E11{signal[:6].replace('_', '')}{tid[:1].upper()}H2/USDT"
        with _tenant(tid), _key_missing():
            _seed_t(sym, HBAR_AMOUNT, peak_amount=HBAR_PEAK, sold_percent=0.5)
            order = TradeOrder(type="SELL" if signal != "COVER_FULL" else "BUY", symbol=sym, price=HBAR_PX,
                               amount=HBAR_AMOUNT, signal=signal, source="auto", exit_source="stop_loss")
            with patch("strategies.registry.resolve_strategy_params", return_value=_params()):
                blocked, msg = _rm()._partial_sell_blocked(order, TF, "auto")
            assert blocked is False, msg

    @pytest.mark.parametrize("tid", TENANTS)
    def test_take_profit_sell_full_executes_once_with_key_missing(self, tid):
        sym = f"E11TPF{tid[:1].upper()}H2/USDT"
        amount = 700.0 / HBAR_PX
        tpf = _analysis(sym, action="SELL_FULL", long_hard_stop=False, sell_source="take_profit",
                        sources=("technical",), normalized_action="SELL_FULL")
        with _tenant(tid), _key_missing():
            _seed_t(sym, amount)
            with _paper():
                res = _cycle(sym, 1.3, analysis=tpf)
            assert res is not None and res.executed, (getattr(res, "code", None), getattr(res, "message", None))
            sells = _filled_sells(sym)
            assert len(sells) == 1 and _held(sym) == pytest.approx(0.0, abs=1e-9), sells
