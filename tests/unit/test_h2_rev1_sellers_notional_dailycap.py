"""H2 / #735 Spec Revision 1 (new file). Base 930792a3 + mutants/proto_fix_h2_rev1.py. Repo floors (min_position 500,
min_sell_notional 300, sold cap 0.99), entry 1.0, both tenants where the path is tenant-aware.

(S) Non-stop sellers without exit_source (TradeOrder.exit_source default "", core/models.py:266) never become FULL:
    real TradingService.execute_sell (trading_service.py:621-645); real recovery helper _trade_order_from_record
    (execution/recovery.py:595-623, drops exit_source) + execute_order; real worker _consume_redis_once
    (workers/trading_engine.py:50-58, drops exit_source; fake Redis + architecture seam); order shapes of
    manual_order_flow.py:374, dca_sniper/bot_http.py:523, signal_orchestrator.py:745 (dca_fund) via the locked path.
    Worker/recovery: NON-GOAL of #735 -- a REAL hard stop routed through them does not escalate (exit_source dropped;
    pass-through is #726). The tests pin exactly that (stop_loss in the intent/record -> still a partial reject).
(N) Decision min_notional: position value ABOVE min_position, partial notional BELOW min_sell_notional -> exactly ONE
    SELL_STOP_FULL over the WHOLE holding (WS and cycle long_hard_stop). Killers hr7, hr8.
(D) Decision daily-cap bypass (risk_manager.py:513-523, pending Viktor verdict): the bypass applies only with an
    explicit hard-stop origin (escalated FULL from exit_ws / long_hard_stop). A fallback-labelled or originless
    stop_loss sell is capped. Killers hr5 (bypass ignores origin), hr6 (bypass removed).
"""
from __future__ import annotations

import contextlib
import json
from unittest.mock import patch

import pytest

from core.models import TradeOrder
from test_h2_ws_partial_min_size import (  # noqa: F401  (autouse fixture imported by name)
    FLOOR, MIN_POS, STRK_PX, TF, TENANTS, _analysis, _assert_one_full, _cycle, _daily_cap_reached, _filled_sells,
    _held, _logs, _orders, _paper, _repo_floors_and_clean_books, _rm, _seed, _tenant, _upgrades, _vol_params, _ws,
)

LOT = 250.0  # value 175 at STRK_PX 0.70 < min_position 500


def _no_full(sym, amount, *, filled_ok=False):
    rows = _orders(sym)
    assert [o for o in rows if o.get("signal") == "SELL_STOP_FULL"] == [], [(o.get("signal"), o.get("status")) for o in rows]
    if not filled_ok:
        assert _filled_sells(sym) == [] and _held(sym) == pytest.approx(amount)


# ----------------------------------------------------------------------------- (S)
class TestRev1NonStopSellersNeverFull:
    @pytest.mark.parametrize("tid", TENANTS)
    @pytest.mark.parametrize("signal", ["SELL_STOP_PARTIAL", "SELL_PARTIAL_50"])
    def test_execute_sell_helper(self, signal, tid):
        from services.trading_service import TradingService

        sym = f"R1S{signal[5:8]}{tid[:1].upper()}/USDT"
        with _tenant(tid):
            _seed(sym, LOT)
            with _paper(), _logs() as lines, patch("strategies.registry.resolve_strategy_params",
                                                   return_value=_vol_params()):
                res = TradingService().execute_sell(sym, TF, STRK_PX, signal, LOT * 0.5, source="auto")
            assert res is not None and not res.executed and res.code == "partial_sell_guard", (
                getattr(res, "code", None), getattr(res, "message", None))
            _no_full(sym, LOT)
        assert not _upgrades(lines), lines

    @pytest.mark.parametrize("tid", TENANTS)
    @pytest.mark.parametrize("rec_source", ["exit_ws", "auto"])
    def test_recovery_record_stop_loss_dropped_non_goal(self, rec_source, tid):
        """Non-goal (#726): the record carries exit_source stop_loss, the recovery helper drops it -> partial reject."""
        from execution.recovery import _trade_order_from_record
        from services.trading_service import TradingService

        sym = f"R1REC{rec_source[:2].upper()}{tid[:1].upper()}/USDT"
        rec = {"side": "sell", "symbol": sym, "signal": "SELL_STOP_PARTIAL", "source": rec_source,
               "exit_source": "stop_loss", "qty": LOT * 0.5, "request": {"amount": LOT * 0.5, "price": STRK_PX},
               "id": f"rec-{sym}", "status": "active"}
        with _tenant(tid):
            _seed(sym, LOT)
            order = _trade_order_from_record(rec)
            assert not order.exit_source  # dropped (recovery.py:604-623)
            with _paper(), _logs() as lines, patch("strategies.registry.resolve_strategy_params",
                                                   return_value=_vol_params()):
                res = TradingService().execute_order(order, timeframe=TF, source=order.source)
            assert res is not None and not res.executed and res.code == "partial_sell_guard", (
                getattr(res, "code", None), getattr(res, "message", None))
            _no_full(sym, LOT)
        assert not _upgrades(lines), lines

    @pytest.mark.parametrize("tid", TENANTS)
    @pytest.mark.parametrize("intent_source", ["exit_ws", "auto"])
    def test_worker_intent_stop_loss_dropped_non_goal(self, intent_source, tid):
        """Non-goal (#726): real _consume_redis_once; the intent's exit_source stop_loss is dropped -> partial reject."""
        import workers.trading_engine as W

        sym = f"R1WK{intent_source[:2].upper()}{tid[:1].upper()}/USDT"
        fields = {"order": json.dumps({"type": "SELL", "symbol": sym, "price": STRK_PX, "amount": LOT * 0.5,
                                       "signal": "SELL_STOP_PARTIAL", "exit_source": "stop_loss"}),
                  "source": intent_source, "idempotency_key": f"wk-{sym}", "intent_id": f"wk-{sym}",
                  "scope": "paper", "timeframe": TF, "tenant_id": tid, "owner_chat_id": ""}
        acked, wlog = [], []

        class _FakeRedis:
            def xgroup_create(self, *a, **k):
                return True

            def xreadgroup(self, *a, **k):
                return [("aria:commands.trade", [("1-0", fields)])]

            def xack(self, *a):
                acked.append(a)

        class _Cfg:
            architecture_config = {"trading_engine_mode": "external", "key_prefix": "aria:"}

        with _tenant(tid):
            _seed(sym, LOT)
            with _paper(), _logs() as lines, patch.object(W, "get_bot_config", lambda: _Cfg()), \
                    patch("bus.redis_client.get_redis", lambda *a, **k: _FakeRedis()), \
                    patch("bus.locks.ledger_lock", lambda *a, **k: contextlib.nullcontext()), \
                    patch.object(W, "log", lambda m, lvl="INFO": wlog.append((lvl, m))), \
                    patch("strategies.registry.resolve_strategy_params", return_value=_vol_params()):
                assert W._consume_redis_once() is True
            assert acked, wlog
            assert wlog == [("INFO", f"External engine filled SELL {sym}: False")], wlog  # worker module logger
            _no_full(sym, LOT)
        assert not _upgrades(lines), lines

    SHAPES = {  # (signal, source, exit_source) as built at 930792a3
        "manual_order_flow_374": ("SELL", "manual", ""),
        "dca_sniper_bot_http_523_partial": ("SELL_PARTIAL_50", "dca_sniper_fund", ""),
        "dca_sniper_bot_http_523_full": ("SELL_FULL", "dca_sniper_fund", ""),
        "orchestrator_745_dca_fund": ("SELL_FULL", "dca_fund_auto", ""),
    }

    @pytest.mark.parametrize("tid", TENANTS)
    @pytest.mark.parametrize("shape", list(SHAPES))
    def test_seller_shapes_locked_path(self, shape, tid):
        signal, src, exit_source = self.SHAPES[shape]
        sym = f"R1SH{list(self.SHAPES).index(shape)}{tid[:1].upper()}/USDT"
        amount = LOT * (0.5 if "PARTIAL" in signal or src == "manual" else 1.0)
        with _tenant(tid):
            _seed(sym, LOT)
            order = TradeOrder(type="SELL", symbol=sym, price=STRK_PX, amount=amount, signal=signal, source=src,
                               exit_source=exit_source)
            with _logs() as lines, patch("strategies.registry.resolve_strategy_params", return_value=_vol_params()):
                dec = _rm().evaluate(order, TF, source=src, locked_sell_checks=True)
        out = getattr(dec, "order", None)
        assert out is None or (out.signal == signal and out.amount == pytest.approx(amount)), (dec.code, dec.message)
        assert not _upgrades(lines), lines


# ----------------------------------------------------------------------------- (N)
class TestRev1MinNotionalSellsWholeHolding:
    """Deliberate decision: min_notional rejection -> FULL over the WHOLE holding, even if position >= min_position."""

    @staticmethod
    def _amount():
        # position value between min_position and 2 * min_notional: value >= 500, half < 300 -> code min_notional
        value = (MIN_POS + 2 * FLOOR) / 2.0  # 550
        assert value >= MIN_POS and value * 0.5 < FLOOR
        return value / STRK_PX

    @pytest.mark.parametrize("tid", TENANTS)
    def test_ws_min_notional_one_full_whole_holding(self, tid):
        amount = self._amount()
        sym = f"R1MNW{tid[:1].upper()}/USDT"
        with _tenant(tid):
            lot = _seed(sym, amount)
            with _paper(), _logs() as lines:
                _ws(sym, lot, _vol_params(), tid, [STRK_PX] * 3)
            _assert_one_full(sym, amount)
        up = _upgrades(lines)
        assert len(up) == 1 and "block=min_notional" in up[0][1] and "reason=partial_sell_guard" in up[0][1], up

    @pytest.mark.parametrize("tid", TENANTS)
    def test_cycle_long_hard_stop_min_notional_one_full_whole_holding(self, tid):
        amount = self._amount()
        sym = f"R1MNC{tid[:1].upper()}/USDT"
        with _tenant(tid):
            _seed(sym, amount)
            with _paper(), _logs() as lines:
                res = _cycle(sym, STRK_PX, _vol_params())
            assert res is not None and res.executed, getattr(res, "message", None)
            _assert_one_full(sym, amount)
        up = _upgrades(lines)
        assert len(up) == 1 and "block=min_notional" in up[0][1] and "origin=long_hard_stop" in up[0][1], up


# ----------------------------------------------------------------------------- (D)
class TestRev1DailyCapBypassOnlyExplicitOrigin:
    """Decision pending Viktor verdict: bypass only for the escalated FULL with explicit hard-stop origin."""

    @pytest.mark.parametrize("tid", TENANTS)
    def test_ws_escalated_full_bypasses(self, tid):
        sym = f"R1DW{tid[:1].upper()}/USDT"
        with _tenant(tid), _daily_cap_reached():
            lot = _seed(sym, LOT)
            with _paper(), _logs() as lines:
                _ws(sym, lot, _vol_params(), tid, [STRK_PX])
            _assert_one_full(sym, LOT)
        assert [m for _, m in lines if "daily_sells_limit_bypassed_hard_stop" in m and "SELL_STOP_FULL" in m], lines

    @pytest.mark.parametrize("tid", TENANTS)
    def test_cycle_long_hard_stop_escalated_full_bypasses(self, tid):
        sym = f"R1DC{tid[:1].upper()}/USDT"
        with _tenant(tid), _daily_cap_reached():
            _seed(sym, LOT)
            with _paper(), _logs() as lines:
                res = _cycle(sym, STRK_PX, _vol_params())
            assert res is not None and res.executed, getattr(res, "message", None)
            _assert_one_full(sym, LOT)
        assert [m for _, m in lines if "daily_sells_limit_bypassed_hard_stop" in m], lines

    @pytest.mark.parametrize("tid", TENANTS)
    def test_fallback_labelled_cycle_full_is_capped(self, tid):
        """SELL_STOP_FULL whose stop_loss label comes only from exit_attribution.py:95-97 (no long_hard_stop)."""
        sym = f"R1DF{tid[:1].upper()}/USDT"
        an = _analysis(sym, action="SELL_STOP_FULL", long_hard_stop=False, sell_source="", sources=(),
                       normalized_action="SELL_STOP_FULL")
        with _tenant(tid), _daily_cap_reached():
            _seed(sym, LOT)
            with _paper(), _logs() as lines:
                res = _cycle(sym, STRK_PX, _vol_params(), analysis=an)
            assert res is not None and not res.executed and res.code == "max_daily_sells", (
                getattr(res, "code", None), getattr(res, "message", None))
            assert _filled_sells(sym) == [] and _held(sym) == pytest.approx(LOT)
        assert not [m for _, m in lines if "daily_sells_limit_bypassed_hard_stop" in m], lines

    @pytest.mark.parametrize("tid", TENANTS)
    def test_originless_risk_direct_stop_full_is_capped(self, tid):
        sym = f"R1DR{tid[:1].upper()}/USDT"
        with _tenant(tid), _daily_cap_reached():
            _seed(sym, LOT)
            order = TradeOrder(type="SELL", symbol=sym, price=STRK_PX, amount=LOT, signal="SELL_STOP_FULL",
                               source="auto", exit_source="stop_loss")
            with patch("strategies.registry.resolve_strategy_params", return_value=_vol_params()):
                dec = _rm().evaluate(order, TF, source="auto", locked_sell_checks=True)
        assert not dec.approved and dec.code == "max_daily_sells", (dec.code, dec.message)
