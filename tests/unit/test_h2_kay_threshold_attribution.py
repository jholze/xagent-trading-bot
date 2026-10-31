"""H2 / #735 Kay additions (new file). Base staging 930792a3 + proto_fix_h2.py.

(A) Henry's live 300 stays UNCLEAR (min_position_usdt_for_partial_sell override, or min_sell_notional_usdt). The fix must
    not depend on which key carries it: with the REAL henry STRK/ULTIMA states (read-only MCP 2026-10-26 ~09:52 CET,
    work/h2_prod_snapshot.json) and the 300 on EITHER key (the other one low), one WS tick through the real chain gives
    exactly ONE SELL_STOP_FULL over the held amount, with block=min_position resp. block=min_notional.
(B) An order whose exit attribution falls back to stop_loss (exit_attribution.py:95-97) priced ABOVE average_entry must
    never become FULL: it stays rejected (small lot) or executes as the partial (large lot). WS chain, risk-direct and
    cycle. Pins H2-BELOW-ENTRY (mutant hm15_below_entry_removed).
"""
from __future__ import annotations

import contextlib
from unittest.mock import patch

import pytest

from core.models import TradeOrder
from test_h2_ws_partial_min_size import (  # noqa: F401  (autouse fixture imported by name)
    REAL, TF, TENANTS, _analysis, _assert_chain, _assert_one_full, _chain, _cycle, _filled_sells, _guard_rows, _held,
    _logs, _orders, _paper, _real_params, _repo_floors_and_clean_books, _rm, _seed, _seed_real, _tenant, _upgrades,
    _vol_params, _ws,
)

LOW = 10.0  # the "other" key, far below both real lots
THRESHOLDS = {
    "min_position_300": ({"min_position_usdt_for_partial_sell": 300.0, "min_sell_notional_usdt": LOW}, "min_position"),
    "min_notional_300": ({"min_position_usdt_for_partial_sell": LOW, "min_sell_notional_usdt": 300.0}, "min_notional"),
}


@contextlib.contextmanager
def _henry_risk(overrides):
    """Tenant-config seam (henry loads fresh): henry's risk block gets exactly these two floors."""
    from core.config import BotConfig
    from core.tenant_context import resolve_tenant_id

    orig = BotConfig.risk_config

    def _risk(self):
        risk = dict(orig.fget(self) or {})
        if resolve_tenant_id() == "henry":
            risk.update(overrides)
        return risk

    with patch.object(BotConfig, "risk_config", property(_risk)):
        yield


# ----------------------------------------------------------------------------- (A)
class TestKayAThresholdKeyIndependent:
    @pytest.mark.parametrize("thr", list(THRESHOLDS))
    @pytest.mark.parametrize("case", list(REAL))
    def test_guard_code_follows_the_key(self, case, thr):
        overrides, code = THRESHOLDS[thr]
        r = REAL[case]
        sym = f"KAG{case[:3]}{thr[4:7].upper()}/USDT"
        with _tenant("henry"), _henry_risk(overrides):
            _, amount = _seed_real(sym, case)
            order = TradeOrder(type="SELL", symbol=sym, price=r["px"], amount=amount * 0.5,
                               signal="SELL_STOP_PARTIAL", source="exit_ws", exit_source="stop_loss")
            with patch("strategies.registry.resolve_strategy_params", return_value=_real_params(case)):
                blocked, msg = _rm()._partial_sell_blocked(order, TF, "exit_ws")
        assert blocked and getattr(order, "_partial_sell_block_code", None) == code, (msg, code)
        assert "$300" in msg, msg

    @pytest.mark.parametrize("thr", list(THRESHOLDS))
    @pytest.mark.parametrize("case", list(REAL))
    def test_ws_chain_one_full_either_key(self, case, thr):
        overrides, code = THRESHOLDS[thr]
        r = REAL[case]
        sym = f"KAW{case[:3]}{thr[4:7].upper()}/USDT"
        with _tenant("henry"), _henry_risk(overrides):
            lot, amount = _seed_real(sym, case)
            with _paper(), _logs() as lines, _chain() as rec:
                _ws(sym, lot, _real_params(case), "henry", [r["px"]] * 3)
            _assert_chain(rec, amount, f"KAY-A {case} {thr}")
            _assert_one_full(sym, amount)
            assert _guard_rows(sym) == []
        up = _upgrades(lines)
        assert len(up) == 1 and f"block={code}" in up[0][1] and "tenant=henry" in up[0][1], up


# ----------------------------------------------------------------------------- (B)
ABOVE_PX = 1.3  # entry 1.0 -> +30 %
SIZES = {"small_250_rejected": 250.0, "large_1400_partial": 1400.0}  # value at 1.3: 325 (< 500) / 1820 (>= 500)


def _expect_no_full(sym, usdt, amount):
    fulls = [o for o in _orders(sym) if o.get("signal") == "SELL_STOP_FULL"]
    assert fulls == [], [(o.get("signal"), o.get("status")) for o in _orders(sym)]
    if usdt < 500:
        assert _filled_sells(sym) == [] and _held(sym) == pytest.approx(amount)
    else:
        assert [o["signal"] for o in _filled_sells(sym)] == ["SELL_STOP_PARTIAL"]
        assert 0 < _held(sym) < amount


class TestKayBNoAttributionAboveEntryNeverFull:
    @pytest.mark.parametrize("tid", TENANTS)
    @pytest.mark.parametrize("size", list(SIZES))
    def test_risk_direct_locked(self, size, tid):
        usdt = SIZES[size]
        amount = usdt / 1.0
        sym = f"KBR{size[:2].upper()}{tid[:1].upper()}/USDT"
        with _tenant(tid):
            _seed(sym, amount)
            order = TradeOrder(type="SELL", symbol=sym, price=ABOVE_PX, amount=amount * 0.5,
                               signal="SELL_STOP_PARTIAL", source="auto", exit_source="stop_loss")
            with patch("strategies.registry.resolve_strategy_params", return_value=_vol_params()):
                dec = _rm().evaluate(order, TF, source="auto", locked_sell_checks=True)
        out = getattr(dec, "order", None)
        if usdt * ABOVE_PX < 500:
            assert not dec.approved and dec.code == "partial_sell_guard", (dec.code, dec.message)
        else:
            assert dec.approved and out.signal == "SELL_STOP_PARTIAL", (dec.code, getattr(out, "signal", None))
        assert getattr(out, "signal", "SELL_STOP_PARTIAL") != "SELL_STOP_FULL"

    @pytest.mark.parametrize("tid", TENANTS)
    @pytest.mark.parametrize("size", list(SIZES))
    def test_ws_chain_fallback_stop_loss_event_above_entry(self, size, tid):
        """Real hub -> execute -> TradingService -> risk; only the shadow event is injected (a stop_loss-labelled
        partial above entry, as the attribution fallback would label it)."""
        import services.exit_realtime.hub as H

        usdt = SIZES[size]
        amount = usdt / 1.0
        sym = f"KBW{size[:2].upper()}{tid[:1].upper()}/USDT"
        with _tenant(tid):
            lot = _seed(sym, amount)
            fake = [{"action": "SELL_STOP_PARTIAL", "source": "stop_loss",
                     "rationale": "fallback attribution (exit_attribution.py:95-97)"}]
            with _paper(), _logs() as lines, patch.object(H, "evaluate_would_sells", return_value=fake):
                _ws(sym, lot, _vol_params(), tid, [ABOVE_PX])
            _expect_no_full(sym, usdt * ABOVE_PX, amount)
        assert not _upgrades(lines), lines

    @pytest.mark.parametrize("tid", TENANTS)
    @pytest.mark.parametrize("shape", ["norm_stop_partial", "norm_empty"])
    def test_cycle_no_attribution_above_entry(self, shape, tid):
        usdt = SIZES["small_250_rejected"]
        amount = usdt / 1.0
        sym = f"KBC{shape[5:8].upper()}{tid[:1].upper()}/USDT"
        an = _analysis(sym, action="SELL_STOP_PARTIAL", long_hard_stop=False, sell_source="", sources=(),
                       normalized_action="SELL_STOP_PARTIAL" if shape == "norm_stop_partial" else "")
        with _tenant(tid):
            _seed(sym, amount)
            with _paper(), _logs() as lines:
                res = _cycle(sym, ABOVE_PX, _vol_params(), analysis=an)
            assert res is None or not res.executed, getattr(res, "message", None)
            _expect_no_full(sym, usdt * ABOVE_PX, amount)
        assert not _upgrades(lines), lines
