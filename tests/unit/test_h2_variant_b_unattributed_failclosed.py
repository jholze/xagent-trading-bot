"""H2 / #735 VARIANT B "fail-closed" (new file; decision pending Viktor's verdict). Base staging 930792a3.

Variant B rule: a partial that partial_sell_guard rejects for size (min_position / min_notional / sold_cap_unconfigured)
and that has NO explicit exit attribution is NOT escalated; the partial rejection stands. That covers
  * orders without exit_source (manual / mcp / dca_sniper / tp-partial style), below AND above entry, and
  * cycle orders whose exit_source is only the action-string fallback "stop_loss" (exit_attribution.py:95-97,
    no sell_source / sources, evaluate_long_hard_stop not fired), below AND above entry.
Only explicitly marked hard stops (is_hard_stop_order with explicit exit_source stop_loss: WS stop_loss event,
cycle long_hard_stop, risk-direct exit_source stop_loss) escalate to exactly ONE SELL_STOP_FULL.

Expected: Variant B proto (mutants/proto_fix_h2_variant_b.py) green. Variant A (proto_fix_h2.py) red ONLY on the
cycle-fallback-below-entry cases (A escalates them, bounded by H2-BELOW-ENTRY). Base red on the "exactly 1 FULL" cases.
Mutants hvb1 (unattributed treated as hard) / hvb2 (fallback mark dropped) / hvb3 (explicit stop also fail-closed).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from core.models import TradeOrder
from test_h2_ws_partial_min_size import (  # noqa: F401  (autouse fixture imported by name)
    STRK_PX, TF, TENANTS, _analysis, _assert_chain, _assert_one_full, _chain, _cycle, _filled_sells, _held, _logs,
    _orders, _paper, _repo_floors_and_clean_books, _rm, _seed, _tenant, _upgrades, _vol_params, _ws,
)

LOT_USDT = 250.0  # entry 1.0: value 175 at 0.70 / 325 at 1.3, both < repo min_position 500 -> guard rejects
PRICES = {"below_entry": STRK_PX, "above_entry": 1.3}
# (order source, call source, signal, exit_source) -- none carries an exit attribution
# manual: never escalated (is_hard_stop_order excludes manual) and exempt from the guard (risk_manager.py:4002-4003) (approved as partial)
UNATTRIBUTED = {
    "manual": ("manual", "manual", "SELL_STOP_PARTIAL", None),
    "mcp": ("mcp", "mcp", "SELL_STOP_PARTIAL", None),
    "dca_sniper": ("dca_sniper", "dca_sniper", "SELL_STOP_PARTIAL", ""),
    "tp_partial": ("auto", "auto", "SELL_PARTIAL_50", None),
}


def _no_full_partial_reject(sym, amount):
    rows = _orders(sym)
    assert [o for o in rows if o.get("signal") == "SELL_STOP_FULL"] == [], [(o.get("signal"), o.get("status")) for o in rows]
    assert _filled_sells(sym) == [] and _held(sym) == pytest.approx(amount)


class TestVariantBUnattributedNeverEscalates:
    @pytest.mark.parametrize("where", list(PRICES))
    @pytest.mark.parametrize("kind", list(UNATTRIBUTED))
    def test_risk_direct_no_exit_source(self, kind, where):
        src, call_src, signal, exit_source = UNATTRIBUTED[kind]
        sym = f"VBR{kind[:3].upper()}{where[:1].upper()}/USDT"
        _seed(sym, LOT_USDT)
        order = TradeOrder(type="SELL", symbol=sym, price=PRICES[where], amount=LOT_USDT * 0.5, signal=signal,
                           source=src, exit_source=exit_source)
        with _logs() as lines, patch("strategies.registry.resolve_strategy_params", return_value=_vol_params()):
            dec = _rm().evaluate(order, TF, source=call_src, locked_sell_checks=True)
        out = getattr(dec, "order", None)
        if kind == "manual":
            # base 930792a3 (risk_manager.py:4002-4003, _partial_sell_blocked): manual sells bypass partial_sell_guard -> approved AS the partial
            assert dec.approved and out.signal == signal and out.amount == pytest.approx(LOT_USDT * 0.5), dec.message
        else:
            assert not dec.approved and dec.code == "partial_sell_guard", (dec.approved, dec.code, dec.message)
        assert getattr(out, "signal", signal) != "SELL_STOP_FULL"
        assert not _upgrades(lines), lines

    @pytest.mark.parametrize("tid", TENANTS)
    @pytest.mark.parametrize("where", list(PRICES))
    def test_ws_chain_unattributed_event(self, where, tid):
        """Real hub -> execute -> TradingService -> risk; only the shadow event is injected, with NO source."""
        import services.exit_realtime.hub as H

        sym = f"VBW{where[:1].upper()}{tid[:1].upper()}/USDT"
        with _tenant(tid):
            lot = _seed(sym, LOT_USDT)
            fake = [{"action": "SELL_STOP_PARTIAL", "source": "", "rationale": "unattributed"}]
            with _paper(), _logs() as lines, patch.object(H, "evaluate_would_sells", return_value=fake):
                _ws(sym, lot, _vol_params(), tid, [PRICES[where]])
            _no_full_partial_reject(sym, LOT_USDT)
        assert not _upgrades(lines), lines

    @pytest.mark.parametrize("tid", TENANTS)
    @pytest.mark.parametrize("where", list(PRICES))
    def test_cycle_fallback_attribution(self, where, tid):
        """exit_source only from the fallback (exit_attribution.py:95-97): stays a partial reject (Variant B)."""
        sym = f"VBC{where[:1].upper()}{tid[:1].upper()}/USDT"
        an = _analysis(sym, action="SELL_STOP_PARTIAL", long_hard_stop=False, sell_source="", sources=(),
                       normalized_action="SELL_STOP_PARTIAL")
        with _tenant(tid):
            _seed(sym, LOT_USDT)
            with _paper(), _logs() as lines:
                res = _cycle(sym, PRICES[where], _vol_params(), analysis=an)
            assert res is None or not res.executed, getattr(res, "message", None)
            _no_full_partial_reject(sym, LOT_USDT)
            rej = [o for o in _orders(sym) if o.get("status") == "rejected"]
            assert rej and all(o.get("signal") == "SELL_STOP_PARTIAL" for o in rej), [(o.get("signal"), o.get("status")) for o in _orders(sym)]
        assert not _upgrades(lines), lines


class TestVariantBMarkedStopStillOneFull:
    @pytest.mark.parametrize("tid", TENANTS)
    def test_ws_chain_marked_stop(self, tid):
        sym = f"VBMW{tid[:1].upper()}/USDT"
        with _tenant(tid):
            lot = _seed(sym, LOT_USDT)
            with _paper(), _logs() as lines, _chain() as rec:
                _ws(sym, lot, _vol_params(), tid, [STRK_PX] * 3)
            _assert_chain(rec, LOT_USDT, f"VB-MARKED {tid}")
            _assert_one_full(sym, LOT_USDT)
        assert len(_upgrades(lines)) == 1, _upgrades(lines)

    @pytest.mark.parametrize("tid", TENANTS)
    def test_cycle_long_hard_stop_marked(self, tid):
        sym = f"VBMC{tid[:1].upper()}/USDT"
        with _tenant(tid):
            _seed(sym, LOT_USDT)
            with _paper(), _logs() as lines:
                _cycle(sym, STRK_PX, _vol_params())  # default analysis: long_hard_stop=True, sources stop_loss
            _assert_one_full(sym, LOT_USDT)
        assert len(_upgrades(lines)) == 1, _upgrades(lines)
