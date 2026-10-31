"""H2 port of the #723 WS debounce tests (credit: spec_723_partial_escalation/tests/test_723_partial_escalation.py AC-3,
test_723_annex_a.py AC-A4 / AC-A6; design of the separate partial key "stop_loss:partial" and the restamp on an
executed partial is #723's P723-KEY / P723-STAMP, carried in proto_fix_h2.py as H2-KEY / H2-STAMP).
Base staging 930792a3 + proto_fix_h2.py.
Adaptation: under H2 a min_position/min_notional rejected hard partial escalates on WS itself, so the AC-3 "rejected
partial must not consume the FULL cooldown" pin uses a sold-cap lot (code sold_cap -> never escalated, stays rejected).
A4 / A6 unchanged.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from h2_port_helpers import (  # noqa: F401
    FULL_ZONE_PX, HBAR_AMOUNT, HBAR_PX, MAX_SOLD, TF, _filled_sells, _fire, _held, _hub, _orders, _paper,
    _prod_floors_and_clean_books, _row, _seed, _seed_hbar,
)


def _seed_soldcap(sym):
    return _seed(sym, HBAR_AMOUNT, peak_amount=HBAR_AMOUNT * 200, sold_percent=MAX_SOLD)


# ----------------------------------------------------------------------------- AC-3 (from #723)
class TestDebounceAC3RejectedPartialKeepsFullCooldownFree:
    def test_full_on_next_tick_is_not_delayed(self):
        sym = "WSH2D/USDT"
        lot = _seed_soldcap(sym)
        hub = _hub(cooldown=30.0)  # >> test runtime, so a stamped key would block
        hub.update_book([_row(sym, lot)])
        with _paper():
            _fire(hub, sym, HBAR_PX)  # partial zone -> guard rejects (sold_cap, not escalated)
            assert _filled_sells(sym) == []
            _fire(hub, sym, FULL_ZONE_PX)  # full zone, next tick
        filled = _filled_sells(sym)
        assert len(filled) == 1, [(o.get("signal"), o.get("status")) for o in _orders(sym)]
        assert filled[0]["signal"] == "SELL_STOP_FULL"
        assert _held(sym) == pytest.approx(0.0, abs=1e-9)

    def test_rejected_partial_retries_stay_throttled(self):
        """Regression: un-stamping must not turn every tick into a new rejected order."""
        sym = "WTH2D/USDT"
        lot = _seed_soldcap(sym)
        hub = _hub(cooldown=30.0)
        hub.update_book([_row(sym, lot)])
        with _paper():
            for _ in range(3):
                _fire(hub, sym, HBAR_PX)
        partial_rows = [o for o in _orders(sym) if o.get("signal") == "SELL_STOP_PARTIAL"]
        assert len(partial_rows) <= 1, len(partial_rows)
        assert _filled_sells(sym) == []

    def test_min_position_partial_escalates_once_then_throttled(self):
        """H2 addition: the escalating case on the same hub -> one FULL, no further rows within the window."""
        sym = "WEH2D/USDT"
        lot = _seed_hbar(sym)
        hub = _hub(cooldown=30.0)
        hub.update_book([_row(sym, lot)])
        with _paper():
            for _ in range(3):
                _fire(hub, sym, HBAR_PX)
        filled = _filled_sells(sym)
        assert [o["signal"] for o in filled] == ["SELL_STOP_FULL"], [(o.get("signal"), o.get("status")) for o in _orders(sym)]
        assert len(_orders(sym)) == 1


# ----------------------------------------------------------------------------- AC-A4 (from #723)
class TestDebounceA4ExecutedPartialStampsFullKey:
    def test_full_tick_inside_cooldown_after_executed_partial_is_debounced(self):
        """Cooldown 120 s > recently_exited 60 s window."""
        import services.exit_realtime.execute as ex
        from strategies.positions import get_position

        sym = "A4RH2D/USDT"
        amount = 700.0 / HBAR_PX
        lot = _seed(sym, amount)
        hub = _hub(cooldown=120.0)
        hub.update_book([_row(sym, lot)])
        with _paper():
            _fire(hub, sym, HBAR_PX)  # approved partial executes
            assert [o["signal"] for o in _filled_sells(sym)] == ["SELL_STOP_PARTIAL"]
            hub.update_book([_row(sym, dict(get_position(sym, TF) or {}))])
            for key in list(ex._last_exit_at):
                ex._last_exit_at[key] -= 61.0
            ex._inflight.clear()
            assert not ex.recently_exited(sym, within_sec=60.0)
            _fire(hub, sym, FULL_ZONE_PX)
        assert [o["signal"] for o in _filled_sells(sym)] == ["SELL_STOP_PARTIAL"]
        assert _held(sym) == pytest.approx(amount * 0.5, rel=1e-6)


# ----------------------------------------------------------------------------- AC-A6 (from #723)
class TestDebounceA6KeysPerTenant:
    def test_two_tenants_same_symbol_partial_then_full_each(self):
        import services.exit_realtime.hub as H

        sym = "A6TH2D/USDT"
        lot = _seed_hbar(sym)
        hub = _hub(cooldown=30.0)
        rows = []
        for tid in ("default", "henry"):
            r = _row(sym, lot)
            r["tenant_id"] = tid
            rows.append(r)
        hub.update_book(rows)
        calls = []
        state = {"action": "SELL_STOP_PARTIAL"}

        def fake_eval(**kw):
            return [{"action": state["action"], "source": "stop_loss", "rationale": "spec A6"}]

        def fake_exec(**kw):
            calls.append((kw.get("tenant_id"), kw.get("action")))
            if kw.get("action") == "SELL_STOP_PARTIAL":
                return {"ok": True, "executed": False,
                        "message": "Partial sell blocked: position $250.00 below minimum $500"}
            return {"ok": True, "executed": False, "message": "spec A6: not executed"}

        with patch.object(H, "evaluate_would_sells", side_effect=fake_eval), patch.object(
            H, "try_execute_trail_exit", side_effect=fake_exec
        ):
            _fire(hub, sym, HBAR_PX)
            _fire(hub, sym, HBAR_PX)  # throttled per tenant
            partial_calls = sorted(c for c in calls if c[1] == "SELL_STOP_PARTIAL")
            state["action"] = "SELL_STOP_FULL"
            _fire(hub, sym, FULL_ZONE_PX)
        full_calls = sorted(c for c in calls if c[1] == "SELL_STOP_FULL")
        assert partial_calls == [("default", "SELL_STOP_PARTIAL"), ("henry", "SELL_STOP_PARTIAL")], calls
        assert full_calls == [("default", "SELL_STOP_FULL"), ("henry", "SELL_STOP_FULL")], calls
