"""H2 / #735 Spec Revision 1, Viktor veto points (new file). Base 930792a3 + mutants/proto_fix_h2_rev1m.py + fixkey_h2.py.
Repo floors (min_position 500, min_sell_notional 300, sold cap 0.99), entry 1.0, both tenants.

(M)  Dedicated origin marker TradeOrder.hard_stop_origin, NOT the label: set only in services/exit_realtime/execute.py
     (WS) and services/signal_orchestrator.py (long_hard_stop). A risk-direct / execute_order order with source exit_ws
     and exit_source stop_loss but WITHOUT the marker stays a partial reject (inverts the 11 label/source-derived pins
     E18H2[exit_ws-*], Rev1 helper controls, VB risk-direct marked stop). Killers: hv01 (marker ignored), hv02
     (fallback counts as hard: the orchestrator marks every stop_loss label), hv03/hv04 (marker not set).
(T)  min_sell_notional != 300: 450 with a lot worth 860 (>= min_position 500, half 430 < 450) -> exactly 1 FULL ==
     holding, block=min_notional (WS chain + cycle). Killer: Viktor vm02 (min_notional fixed 300).
(E)  Boundary price == entry -> no FULL (risk direct with marker, cycle long_hard_stop). Killer: Viktor vm04 (<=).
(U)  The unlocked pre-check (locked_sell_checks=False) neither escalates nor modifies the order. Killer: Viktor vm08.
(D)  Daily-cap bypass DECIDED (Viktor): only locked AND marker AND price < entry; the FULL still counts in daily_sells;
     log line daily_sells_limit_bypassed_hard_stop carries origin=<marker> escalated=<0|1>. Unattributed / fallback
     partials at a reached cap stay rejected. Killers: hv06 (bypass for any SELL_STOP_* without marker = base rule),
     hv07 (bypass unlocked), hv08 (bypass at/above entry), hv09 (bypass removed), hv10 (no escalated flag).
(W)  Worker / external engine: no escalation (fail-closed, #726) + startup WARNING when trading_engine_mode !=
     in_process (config.json:442 = in_process). Killer: hv11 (warning removed).
"""
from __future__ import annotations

import contextlib
import os
import re
from unittest.mock import patch

import pytest

from core.models import TradeOrder
from test_h2_ws_partial_min_size import (  # noqa: F401  (autouse fixture imported by name)
    ENTRY, FLOOR, MIN_POS, STRK_PX, TF, TENANTS, _analysis, _assert_one_full, _cycle, _daily_cap_reached,
    _filled_sells, _held, _logs, _orders, _paper, _repo_floors_and_clean_books, _rm, _seed, _tenant, _upgrades,
    _vol_params, _ws,
)

SMALL = 250.0  # lot amount; value 175 at 0.70 < min_position 500 -> guard rejects the partial (min_position)
BIG = 1400.0 / STRK_PX  # value 1400 at 0.70: half 700 >= 300 -> the guard passes the partial


def _bypass(lines):
    return [m for _, m in lines if "daily_sells_limit_bypassed_hard_stop" in m]


def _hard_partial(sym, amount, *, price=STRK_PX, origin="exit_ws", source="exit_ws", signal="SELL_STOP_PARTIAL",
                  exit_source="stop_loss"):
    return TradeOrder(type="SELL", symbol=sym, price=price, amount=amount, signal=signal, source=source,
                      exit_source=exit_source, hard_stop_origin=origin)


def _eval(order, *, locked=True, source=None):
    with patch("strategies.registry.resolve_strategy_params", return_value=_vol_params()):
        return _rm().evaluate(order, TF, source=source or order.source, locked_sell_checks=locked)


# ----------------------------------------------------------------------------- (M)
class TestM1MarkerNotLabel:
    @pytest.mark.parametrize("tid", TENANTS)
    @pytest.mark.parametrize("origin", ["", "stop_loss", "manual", "ws", "cycle"])
    def test_without_valid_marker_label_alone_stays_rejected(self, origin, tid):
        sym = f"VMN{['', 'stop_loss', 'manual', 'ws', 'cycle'].index(origin)}{tid[:1].upper()}/USDT"
        with _tenant(tid):
            _seed(sym, SMALL)
            with _logs() as lines:
                order = _hard_partial(sym, SMALL * 0.5, origin=origin)
                dec = _eval(order)
            assert not dec.approved and dec.code == "partial_sell_guard", (dec.code, dec.message)
            assert order.signal == "SELL_STOP_PARTIAL" and order.amount == pytest.approx(SMALL * 0.5)
        assert not _upgrades(lines), lines

    @pytest.mark.parametrize("tid", TENANTS)
    @pytest.mark.parametrize("origin,source", [("exit_ws", "exit_ws"), ("long_hard_stop", "auto")])
    def test_control_marker_escalates_one_full(self, origin, source, tid):
        sym = f"VMC{origin[:2].upper()}{tid[:1].upper()}/USDT"
        with _tenant(tid):
            _seed(sym, SMALL)
            with _logs() as lines:
                dec = _eval(_hard_partial(sym, SMALL * 0.5, origin=origin, source=source))
            assert dec.approved and dec.order.signal == "SELL_STOP_FULL", (dec.code, dec.message)
            assert dec.order.amount == pytest.approx(SMALL)
        up = _upgrades(lines)
        assert len(up) == 1 and f"origin={origin}" in up[0][1] and "reason=partial_sell_guard" in up[0][1], up

    @pytest.mark.parametrize("tid", TENANTS)
    def test_marker_without_stop_loss_exit_source_not_hard(self, tid):
        sym = f"VMX{tid[:1].upper()}/USDT"
        with _tenant(tid):
            _seed(sym, SMALL)
            with _logs() as lines:
                dec = _eval(_hard_partial(sym, SMALL * 0.5, exit_source=""))
            assert not dec.approved and dec.code == "partial_sell_guard"
        assert not _upgrades(lines), lines

    @pytest.mark.parametrize("tid", TENANTS)
    def test_ws_chain_sets_marker_exit_ws(self, tid):
        sym = f"VMW{tid[:1].upper()}/USDT"
        with _tenant(tid):
            lot = _seed(sym, SMALL)
            with _paper(), _logs() as lines:
                _ws(sym, lot, _vol_params(), tid, [STRK_PX])
            _assert_one_full(sym, SMALL)
        up = _upgrades(lines)
        assert len(up) == 1 and "origin=exit_ws" in up[0][1], up

    @pytest.mark.parametrize("tid", TENANTS)
    def test_cycle_sets_marker_only_with_long_hard_stop(self, tid):
        sym_h, sym_f = f"VMCH{tid[:1].upper()}/USDT", f"VMCF{tid[:1].upper()}/USDT"
        fallback = _analysis(sym_f, long_hard_stop=False, sell_source="", sources=())
        with _tenant(tid):
            _seed(sym_h, SMALL)
            _seed(sym_f, SMALL)
            with _paper(), _logs() as lines:
                res_h = _cycle(sym_h, STRK_PX, _vol_params())
                res_f = _cycle(sym_f, STRK_PX, _vol_params(), analysis=fallback)
            assert res_h is not None and res_h.executed
            _assert_one_full(sym_h, SMALL)
            assert res_f is None or not res_f.executed
            assert _filled_sells(sym_f) == [] and _held(sym_f) == pytest.approx(SMALL)
        up = _upgrades(lines)
        assert len(up) == 1 and sym_h in up[0][1] and "origin=long_hard_stop" in up[0][1], up

    def test_marker_assigned_only_in_ws_execute_and_orchestrator(self):
        """Structural: the only production writers of hard_stop_origin are execute.py and signal_orchestrator.py."""
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        pat = re.compile(r"hard_stop_origin\s*=(?!=)")
        writers = set()
        for d, dirs, files in os.walk(root):
            dirs[:] = [x for x in dirs if x not in (".git", "tests", "venv", ".venv", "node_modules", "__pycache__")]
            for f in files:
                if f.endswith(".py"):
                    p = os.path.join(d, f)
                    with open(p, encoding="utf-8", errors="replace") as fh:
                        src = fh.read()
                    for line in src.splitlines():
                        s = line.strip()
                        if s.startswith("#") or s.startswith("hard_stop_origin: str"):
                            continue
                        if pat.search(s) and "getattr" not in s:
                            writers.add(os.path.relpath(p, root))
        assert writers == {"services/exit_realtime/execute.py", "services/signal_orchestrator.py"}, writers


# ----------------------------------------------------------------------------- (T)
@contextlib.contextmanager
def _floors(**overrides):
    """Both tenants: risk floors overridden through the tenant risk_config seam."""
    from core.config import BotConfig

    orig = BotConfig.risk_config

    def _risk(self):
        risk = dict(orig.fget(self) or {})
        risk.update(overrides)
        return risk

    with patch.object(BotConfig, "risk_config", property(_risk)):
        yield


NOTIONAL_450 = 450.0
LOT_860 = 860.0  # value; half 430 < 450, value >= min_position 500


class TestT1MinNotional450:
    @pytest.mark.parametrize("tid", TENANTS)
    def test_guard_code_min_notional_450(self, tid):
        sym = f"VT4G{tid[:1].upper()}/USDT"
        amount = LOT_860 / STRK_PX
        with _tenant(tid), _floors(min_sell_notional_usdt=NOTIONAL_450, min_position_usdt_for_partial_sell=MIN_POS):
            _seed(sym, amount)
            order = _hard_partial(sym, amount * 0.5)
            with patch("strategies.registry.resolve_strategy_params", return_value=_vol_params()):
                blocked, msg = _rm()._partial_sell_blocked(order, TF, "exit_ws")
        assert blocked and order._partial_sell_block_code == "min_notional" and "$450" in msg, msg

    @pytest.mark.parametrize("tid", TENANTS)
    def test_ws_chain_one_full_whole_holding(self, tid):
        sym = f"VT4W{tid[:1].upper()}/USDT"
        amount = LOT_860 / STRK_PX
        with _tenant(tid), _floors(min_sell_notional_usdt=NOTIONAL_450, min_position_usdt_for_partial_sell=MIN_POS):
            lot = _seed(sym, amount)
            with _paper(), _logs() as lines:
                _ws(sym, lot, _vol_params(), tid, [STRK_PX] * 3)
            _assert_one_full(sym, amount)
            assert _held(sym) == pytest.approx(0.0, abs=1e-9)
        up = _upgrades(lines)
        assert len(up) == 1 and "block=min_notional" in up[0][1] and "origin=exit_ws" in up[0][1], up

    @pytest.mark.parametrize("tid", TENANTS)
    def test_cycle_long_hard_stop_one_full_whole_holding(self, tid):
        sym = f"VT4C{tid[:1].upper()}/USDT"
        amount = LOT_860 / STRK_PX
        with _tenant(tid), _floors(min_sell_notional_usdt=NOTIONAL_450, min_position_usdt_for_partial_sell=MIN_POS):
            _seed(sym, amount)
            with _paper(), _logs() as lines:
                res = _cycle(sym, STRK_PX, _vol_params())
            assert res is not None and res.executed, getattr(res, "message", None)
            _assert_one_full(sym, amount)
        up = _upgrades(lines)
        assert len(up) == 1 and "block=min_notional" in up[0][1], up


# ----------------------------------------------------------------------------- (E)
class TestE1PriceAtEntryNoFull:
    @pytest.mark.parametrize("tid", TENANTS)
    @pytest.mark.parametrize("origin,source", [("exit_ws", "exit_ws"), ("long_hard_stop", "auto")])
    def test_risk_direct_marker_at_entry(self, origin, source, tid):
        sym = f"VE{origin[:2].upper()}{tid[:1].upper()}/USDT"
        with _tenant(tid):
            _seed(sym, SMALL)
            with _logs() as lines:
                order = _hard_partial(sym, SMALL * 0.5, price=ENTRY, origin=origin, source=source)
                dec = _eval(order)
            assert not dec.approved and dec.code == "partial_sell_guard", (dec.code, dec.message)
            assert order.signal == "SELL_STOP_PARTIAL"
        assert not _upgrades(lines), lines

    @pytest.mark.parametrize("tid", TENANTS)
    def test_cycle_long_hard_stop_at_entry(self, tid):
        sym = f"VECY{tid[:1].upper()}/USDT"
        with _tenant(tid):
            _seed(sym, SMALL)
            with _paper(), _logs() as lines:
                res = _cycle(sym, ENTRY, _vol_params())
            assert res is None or not res.executed
            assert [o for o in _orders(sym) if o.get("signal") == "SELL_STOP_FULL"] == []
            assert _held(sym) == pytest.approx(SMALL)
        assert not _upgrades(lines), lines


# ----------------------------------------------------------------------------- (U)
class TestU1UnlockedPreCheckNeverEscalates:
    @pytest.mark.parametrize("tid", TENANTS)
    @pytest.mark.parametrize("origin,source", [("exit_ws", "exit_ws"), ("long_hard_stop", "auto")])
    def test_unlocked_evaluate_no_escalation_no_mutation(self, origin, source, tid):
        sym = f"VU{origin[:2].upper()}{tid[:1].upper()}/USDT"
        with _tenant(tid):
            _seed(sym, SMALL)
            order = _hard_partial(sym, SMALL * 0.5, origin=origin, source=source)
            with _logs() as lines:
                dec = _eval(order, locked=False)
            assert not dec.approved and dec.code == "partial_sell_guard", (dec.code, dec.message)
            assert order.signal == "SELL_STOP_PARTIAL" and order.amount == pytest.approx(SMALL * 0.5)
            assert not getattr(order, "_h2_escalated", False)
            assert _held(sym) == pytest.approx(SMALL)
        assert not _upgrades(lines), lines


# ----------------------------------------------------------------------------- (D)
class TestD1DailyCapBypassDecided:
    @pytest.mark.parametrize("tid", TENANTS)
    def test_ws_escalated_full_bypasses_logs_escalated_1(self, tid):
        sym = f"VDW{tid[:1].upper()}/USDT"
        with _tenant(tid), _daily_cap_reached():
            lot = _seed(sym, SMALL)
            with _paper(), _logs() as lines:
                _ws(sym, lot, _vol_params(), tid, [STRK_PX])
            _assert_one_full(sym, SMALL)
        b = _bypass(lines)
        assert len(b) == 1 and "escalated=1" in b[0] and "origin=exit_ws" in b[0] and "SELL_STOP_FULL" in b[0], b

    @pytest.mark.parametrize("tid", TENANTS)
    def test_cycle_escalated_full_bypasses_logs_escalated_1(self, tid):
        sym = f"VDC{tid[:1].upper()}/USDT"
        with _tenant(tid), _daily_cap_reached():
            _seed(sym, SMALL)
            with _paper(), _logs() as lines:
                res = _cycle(sym, STRK_PX, _vol_params())
            assert res is not None and res.executed
            _assert_one_full(sym, SMALL)
        b = _bypass(lines)
        assert len(b) == 1 and "escalated=1" in b[0] and "origin=long_hard_stop" in b[0], b

    @pytest.mark.parametrize("tid", TENANTS)
    def test_marked_full_not_escalated_logs_escalated_0(self, tid):
        sym = f"VDN{tid[:1].upper()}/USDT"
        with _tenant(tid), _daily_cap_reached():
            _seed(sym, SMALL)
            with _logs() as lines:
                dec = _eval(_hard_partial(sym, SMALL, signal="SELL_STOP_FULL"))
        assert dec.approved, (dec.code, dec.message)
        b = _bypass(lines)
        assert len(b) == 1 and "escalated=0" in b[0] and "origin=exit_ws" in b[0], b

    @pytest.mark.parametrize("tid", TENANTS)
    @pytest.mark.parametrize("size", ["small", "big"])
    def test_unattributed_partial_at_cap_stays_rejected(self, size, tid):
        """Cycle SELL_STOP_PARTIAL whose stop_loss label is only the attribution fallback (no long_hard_stop, no
        marker). Small lot: guard reject stands. Big lot (guard passes): capped by max_daily_sells (base bypasses)."""
        sym = f"VDU{size[:1].upper()}{tid[:1].upper()}/USDT"
        amount = SMALL if size == "small" else BIG
        an = _analysis(sym, long_hard_stop=False, sell_source="", sources=())
        with _tenant(tid), _daily_cap_reached():
            _seed(sym, amount)
            with _paper(), _logs() as lines:
                res = _cycle(sym, STRK_PX, _vol_params(), analysis=an)
            assert res is not None and not res.executed, getattr(res, "message", None)
            assert res.code == ("partial_sell_guard" if size == "small" else "max_daily_sells"), (res.code, res.message)
            assert _filled_sells(sym) == [] and _held(sym) == pytest.approx(amount)
        assert not _bypass(lines) and not _upgrades(lines), lines

    @pytest.mark.parametrize("tid", TENANTS)
    def test_label_only_risk_direct_partial_at_cap_capped(self, tid):
        """source exit_ws + stop_loss label, guard passes (big lot), no marker -> max_daily_sells."""
        sym = f"VDL{tid[:1].upper()}/USDT"
        with _tenant(tid), _daily_cap_reached():
            _seed(sym, BIG)
            with _logs() as lines:
                dec = _eval(_hard_partial(sym, BIG * 0.5, origin=""))
        assert not dec.approved and dec.code == "max_daily_sells", (dec.code, dec.message)
        assert not _bypass(lines), lines

    @pytest.mark.parametrize("tid", TENANTS)
    def test_marked_full_at_entry_capped(self, tid):
        sym = f"VDE{tid[:1].upper()}/USDT"
        with _tenant(tid), _daily_cap_reached():
            _seed(sym, SMALL)
            with _logs() as lines:
                dec = _eval(_hard_partial(sym, SMALL, price=ENTRY, signal="SELL_STOP_FULL"))
        assert not dec.approved and dec.code == "max_daily_sells", (dec.code, dec.message)
        assert not _bypass(lines), lines

    @pytest.mark.parametrize("tid", TENANTS)
    def test_marked_full_unlocked_capped(self, tid):
        sym = f"VDK{tid[:1].upper()}/USDT"
        with _tenant(tid), _daily_cap_reached():
            _seed(sym, SMALL)
            with _logs() as lines:
                dec = _eval(_hard_partial(sym, SMALL, signal="SELL_STOP_FULL"), locked=False)
        assert not dec.approved and dec.code == "max_daily_sells", (dec.code, dec.message)
        assert not _bypass(lines), lines

    @pytest.mark.parametrize("tid", TENANTS)
    def test_escalated_full_counts_in_daily_sells(self, tid):
        from risk.risk_manager import RiskManager

        sym = f"VDZ{tid[:1].upper()}/USDT"
        with _tenant(tid), patch.object(RiskManager, "_effective_max_daily_sells", lambda self: 1000):
            lot = _seed(sym, SMALL)
            with _paper(), _logs():
                before = _rm()._daily_sells_count()
                _ws(sym, lot, _vol_params(), tid, [STRK_PX])
                after = _rm()._daily_sells_count()
            _assert_one_full(sym, SMALL)
        assert after == before + 1, (before, after)


# ----------------------------------------------------------------------------- (W)
class TestW1WorkerFailClosedWarning:
    @pytest.mark.parametrize("mode", ["external", "distributed"])
    def test_warning_when_not_in_process(self, mode):
        from core.config_guardrails import report_hard_stop_escalation_scope

        with _logs() as lines:
            assert report_hard_stop_escalation_scope({"architecture": {"trading_engine_mode": mode}}, "henry") is True
        w = [(lvl, m) for lvl, m in lines if "h2_hard_stop_escalation_inactive" in m]
        assert len(w) == 1 and w[0][0] == "WARNING" and f"trading_engine_mode={mode}" in w[0][1] and "#726" in w[0][1], w

    @pytest.mark.parametrize("cfg", [{"architecture": {"trading_engine_mode": "in_process"}}, {"architecture": {}}, {}])
    def test_no_warning_in_process(self, cfg):
        from core.config_guardrails import report_hard_stop_escalation_scope

        with _logs() as lines:
            assert report_hard_stop_escalation_scope(cfg) is False
        assert not [m for _, m in lines if "h2_hard_stop_escalation_inactive" in m], lines

    def test_repo_config_is_in_process(self):
        from test_h2_ws_partial_min_size import CFG

        assert CFG["architecture"]["trading_engine_mode"] == "in_process"  # config.json:442

    @pytest.mark.parametrize("path", ["aria_bot.py", "workers/trading_engine.py"])
    def test_called_at_startup(self, path):
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        with open(os.path.join(root, path), encoding="utf-8") as fh:
            assert "report_hard_stop_escalation_scope(" in fh.read()
