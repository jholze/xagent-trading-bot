"""H2 standalone spec tests: a hard partial stop that partial_sell_guard rejects for SIZE becomes one full stop.

Base: staging 930792a3 (no #723). PAPER only, synthetic ledger symbols, private mongod (:27696 via the
mongoport696 plugin). Helpers are self-contained (pattern borrowed from the #723 bundle tests, not imported).

Rule under test (spec section 3): an order with is_hard_stop_order(order, source) (SELL_STOP_PARTIAL +
exit_source stop_loss + not manual), from ANY source (exit_ws, cycle, any caller of TradingService.execute_order),
that partial_sell_guard rejects with code min_position or min_notional, becomes exactly ONE SELL_STOP_FULL over the
held amount inside the locked evaluate(). no_auto_sell lock and the sold_percent cap still block.

Repo values read at test time (config.json at 930792a3): risk :616 min_sell_notional_usdt 300, :617
min_position_usdt_for_partial_sell 500, :618 cap 0.99, :623 partial_stop_ratio 0.67, top-level stop_loss_pct :117
50, volatile_altcoin.partial_stop_pct :1480 25, stable_altcoin (:1659) has no partial_stop_pct (ratio path 33.5).
Henry's live floor 300 is taken from the prod guard text "below minimum $300" (key UNCLEAR, spec section 9) and set
through the BotConfig.risk_config seam (tenant configs load fresh per call, raw-dict edits do not reach henry).

AC map (each AC has its own killing mutant, mutants/h2m*.py; spec section 6):
  AC1 WS tick, henry STRK-like (min_position) -> exactly 1 SELL_STOP_FULL, amount = held, later ticks: no rows.
  AC2 WS tick, henry ULTIMA-like (ratio path), default USELESS-like (min_notional, $252.06 < 300), default
      HBAR-like (min_position, $394.64 < 500) -> exactly 1 SELL_STOP_FULL, amount = held.
  AC3 cycle hard partial WITHOUT any order flag, exit_source stop_loss (two analysis shapes) -> SELL_STOP_FULL.
  AC4 no_auto_sell lock -> no FULL (position_locked); sold >= cap -> no FULL (partial_sell_guard stays);
      AC4-D debounce (concept #723 AC-3): a rejected WS partial does not consume the full-stop window, and
      rejected partial retries stay throttled.
  AC5 10 WS ticks + 1 cycle -> exactly 1 fill: standalone shared file test_ac5_ws_ticks_plus_cycle_one_fill.py.
  AC6 replay with the real henry STRK/ULTIMA states (read-only MCP, 2026-10-26 ~09:52 CET): the guard reproduces
      the prod text exactly; the WS tick at the prod price closes FULL over the real quantity (red on base).
  AC7 max_daily_sells reached -> the escalated FULL still executes (hard-stop bypass); non-hard sell still capped.
  AC8 tenant isolation: henry's escalation leaves the default book (same symbol) untouched, and vice versa.
"""
from __future__ import annotations

import contextlib
import json
import os
from unittest.mock import patch

import pytest

from core.models import SignalAnalysis, TradeOrder
from services.exit_realtime.hub import ExitRealtimeHub

TF = "1h"
ENTRY = 1.0
TENANTS = [pytest.param("default", id="default"), pytest.param("henry", id="henry")]
HENRY_MIN_POS = 300.0  # live henry floor from the prod guard text (UNCLEAR which key, spec section 9)


def _cfg():
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "..", "..", "config.json"), encoding="utf-8") as fh:
        return json.load(fh)


CFG = _cfg()
REPO_RISK = CFG["risk"]
FLOOR = float(REPO_RISK["min_sell_notional_usdt"])  # 300 (config.json:616)
MIN_POS = float(REPO_RISK["min_position_usdt_for_partial_sell"])  # 500 (:617)
MAX_SOLD = float(REPO_RISK["block_partial_sell_if_sold_percent_above"])  # 0.99 (:618)
RATIO = float(REPO_RISK["partial_stop_ratio"])  # 0.67 (:623)
BASE_SL = float(CFG["stop_loss_pct"])  # 50.0 (:117)
VOL = CFG["volatile_altcoin"]
STB = CFG["stable_altcoin"]

STRK_PX = 0.70  # fixture -30 % (volatile partial band 25..50)
ULTIMA_PX = 0.65  # fixture -35 % (ratio band 33.5..50)
FULL_ZONE_PX = 0.45  # fixture -55 % > stop_loss_pct 50
LOT_USDT = 250.0  # fixture: below henry 300 and below repo 500
USELESS_LOT_USDT = 2 * 252.06  # Viktor: partial notional $252.06 < 300 (half of the lot, fraction 0.5)
HBAR_LOT_USDT = 394.64  # Viktor: position $394.64 < 500


def _vol_params():
    return {"stop_loss_pct": BASE_SL, "partial_stop_pct": VOL["partial_stop_pct"], "dca": VOL["dca"],
            "exit_ladder": VOL["exit_ladder"]}


def _stb_params():
    assert "partial_stop_pct" not in STB and "partial_stop_ratio" not in STB
    return {"stop_loss_pct": BASE_SL, "dca": STB["dca"], "exit_ladder": STB["exit_ladder"]}


# ----------------------------------------------------------------------------- helpers (self-contained)
def _lot(amount, entry=ENTRY, **extra):
    from strategies.positions import _deserialize_position

    raw = {"amount": amount, "average_entry": entry, "recent_high": extra.pop("recent_high", entry), "side": "long",
           "peak_amount": extra.pop("peak_amount", amount), "sold_percent": extra.pop("sold_percent", 0.0)}
    lock = extra.pop("lock", None)
    raw.update(extra)
    lot = _deserialize_position(raw)
    for k, v in extra.items():
        if k not in lot:
            lot[k] = v
    if lock is not None:
        lot["lock"] = lock
    return lot


def _seed(symbol, amount, **extra):
    """Seed into the ACTIVE tenant's RAM book (default: pm.positions; henry: its own store)."""
    from strategies import positions as pm

    lot = _lot(amount, **extra)
    pm._active_store()[pm.get_key(symbol, TF)] = lot
    pm._positions_state[pm._current_active_key()] = "known"
    return lot


@contextlib.contextmanager
def _tenant(tid):
    if tid == "default":
        yield
        return
    from core.tenant_routing import tenant_cycle_context
    from strategies import positions as pm

    # unit-env seams: no Redis (ledger lock no-op), satellite book = RAM seed (bootstrap stubbed)
    with patch("bus.locks.ledger_lock", lambda *_a, **_k: contextlib.nullcontext()), patch(
        "strategies.positions.bootstrap_positions", lambda *_a, **_k: None
    ), tenant_cycle_context(tid):
        assert pm._current_active_key()[0] == tid
        yield


@contextlib.contextmanager
def _henry_min_pos(value=HENRY_MIN_POS):
    from core.config import BotConfig
    from core.tenant_context import resolve_tenant_id

    orig = BotConfig.risk_config

    def _risk(self):
        risk = dict(orig.fget(self) or {})
        if resolve_tenant_id() == "henry":
            risk["min_position_usdt_for_partial_sell"] = value
        return risk

    with patch.object(BotConfig, "risk_config", property(_risk)):
        yield


@contextlib.contextmanager
def _paper():
    import data_manager
    from execution.gate_adapter import GateExecutionAdapter
    from services.trading_service import TradingService

    shorts = data_manager.get_config().setdefault("shorts", {})
    prev_auto = shorts.get("auto_after_sell", None)
    shorts["auto_after_sell"] = False
    prev_f, prev_c = GateExecutionAdapter._shadow_markets_failed, GateExecutionAdapter._shadow_markets_cache
    GateExecutionAdapter._shadow_markets_failed, GateExecutionAdapter._shadow_markets_cache = True, None
    with patch.object(TradingService, "refresh", lambda self: self), patch(
        "notifications.telegram_commands.position_display.send_positions_snapshot", lambda *a, **k: None
    ), patch("core.operator_notify.notify_operator", lambda *a, **k: None):
        try:
            yield
        finally:
            GateExecutionAdapter._shadow_markets_failed, GateExecutionAdapter._shadow_markets_cache = prev_f, prev_c
            if prev_auto is None:
                shorts.pop("auto_after_sell", None)
            else:
                shorts["auto_after_sell"] = prev_auto


@contextlib.contextmanager
def _logs():
    lines: list[tuple[str, str]] = []

    def _rec(message, level="INFO"):
        lines.append((str(level), str(message)))

    with patch("logger.log", side_effect=_rec), patch("services.exit_realtime.hub.log", side_effect=_rec):
        yield lines


def _upgrades(lines):
    return [(lvl, m) for lvl, m in lines if "partial_stop_upgraded_to_full" in m]


@contextlib.contextmanager
def _chain():
    """Spies (wraps, never replaces) on every layer of the real WS chain:
    shadow_eval.evaluate_would_sells -> hub -> execute.try_execute_trail_exit -> TradingService.execute_order
    -> RiskManager.evaluate. Records what each layer saw and returned."""
    import services.exit_realtime.hub as H
    from risk.risk_manager import RiskManager
    from services.trading_service import TradingService

    rec = {"shadow": [], "execute": [], "trading": [], "risk": []}
    o_ev, o_tx, o_eo, o_rv = H.evaluate_would_sells, H.try_execute_trail_exit, TradingService.execute_order, \
        RiskManager.evaluate

    def ev(**kw):
        out = o_ev(**kw)
        rec["shadow"].append([(e.get("action"), e.get("source")) for e in out if e.get("action")])
        return out

    def tx(**kw):
        out = o_tx(**kw)
        rec["execute"].append({"action": kw.get("action"), "exit_source": kw.get("exit_source"),
                               "sell_amount": kw.get("sell_amount"), "executed": bool(out.get("executed"))})
        return out

    def eo(self, order, *a, **k):
        entry = {"source": getattr(order, "source", None), "signal_in": order.signal, "amount_in": order.amount}
        res = o_eo(self, order, *a, **k)
        entry.update(executed=bool(getattr(res, "executed", False)), code=getattr(res, "code", None))
        rec["trading"].append(entry)
        return res

    def rv(self, order, *a, **k):
        entry = {"locked": bool(k.get("locked_sell_checks", False)), "source": k.get("source"),
                 "signal_in": order.signal, "amount_in": order.amount}
        dec = o_rv(self, order, *a, **k)
        out = getattr(dec, "order", None)
        entry.update(approved=bool(dec.approved), code=getattr(dec, "code", None),
                     signal_out=getattr(out, "signal", None), amount_out=getattr(out, "amount", None))
        rec["risk"].append(entry)
        return dec

    with patch.object(H, "evaluate_would_sells", side_effect=ev), patch.object(
        H, "try_execute_trail_exit", side_effect=tx
    ), patch.object(TradingService, "execute_order", autospec=True, side_effect=eo), patch.object(
        RiskManager, "evaluate", autospec=True, side_effect=rv
    ):
        yield rec


def _assert_chain(rec, held, tag):
    """The escalated FULL travelled shadow_eval -> hub -> execute -> trading -> risk(locked) and back."""
    print(f"CHAIN {tag} " + json.dumps(rec, default=str))
    assert ("SELL_STOP_PARTIAL", "stop_loss") in [e for evs in rec["shadow"] for e in evs], rec["shadow"]
    ex = rec["execute"][0]
    assert ex["action"] == "SELL_STOP_PARTIAL" and ex["exit_source"] == "stop_loss" and ex["executed"], rec["execute"]
    assert ex["sell_amount"] == pytest.approx(held * 0.5, rel=1e-9), ex
    tr = [t for t in rec["trading"] if t["source"] == "exit_ws"]
    assert tr and tr[0]["signal_in"] == "SELL_STOP_PARTIAL" and tr[0]["executed"], rec["trading"]
    locked = [r for r in rec["risk"] if r["locked"]]
    assert locked and locked[0]["signal_in"] == "SELL_STOP_PARTIAL" and locked[0]["approved"], rec["risk"]
    assert locked[0]["signal_out"] == "SELL_STOP_FULL" and locked[0]["amount_out"] == pytest.approx(held, rel=1e-9)


def _clear_guards():
    import services.exit_realtime.execute as ex

    ex._inflight.clear()
    ex._last_exit_at.clear()


@pytest.fixture(autouse=True)
def _repo_floors_and_clean_books():
    """Repo config.json floors (the unit conftest lowers them)."""
    import data_manager
    from strategies.positions import reset_all_position_stores_for_tests

    risk = data_manager.get_config().setdefault("risk", {})
    saved = {k: risk.get(k) for k in ("min_sell_notional_usdt", "min_position_usdt_for_partial_sell",
                                      "block_partial_sell_if_sold_percent_above")}
    for k in saved:
        risk[k] = REPO_RISK[k]
    risk.setdefault("position_locks", {})["enabled"] = True
    reset_all_position_stores_for_tests()
    _clear_guards()
    yield
    _clear_guards()
    reset_all_position_stores_for_tests()
    for k, v in saved.items():
        if v is None:
            risk.pop(k, None)
        else:
            risk[k] = v


def _orders(symbol):
    from data_manager import load_orders, resolve_ledger_scope
    from services import order_service

    order_service._ORDERS_READ_CACHE.clear()
    doc = load_orders(resolve_ledger_scope()) or {}
    return [o for o in (doc.get("orders") or []) if o.get("symbol") == symbol]


def _filled_sells(symbol):
    return [o for o in _orders(symbol)
            if o.get("status") == "filled" and str(o.get("type") or o.get("side") or "").upper() == "SELL"]


def _guard_rows(symbol):
    return [o for o in _orders(symbol) if o.get("status") != "filled"]


def _amount(row):
    for k in ("amount", "filled_amount", "requested_amount"):
        if row.get(k) is not None:
            return float(row[k])
    return float((row.get("request") or {}).get("amount") or 0)


def _held(symbol):
    from strategies.positions import get_position

    return float((get_position(symbol, TF) or {}).get("amount") or 0)


def _err(row):
    return str(row.get("error") or (row.get("risk") or {}).get("message") or "")


def _hub(cooldown=0.0):
    return ExitRealtimeHub({"exit_realtime": {"enabled": True, "mode": "live", "sources": ["stop_loss"],
                                              "live_cooldown_sec": cooldown, "default_atr_pct": 3.0}})


def _row(symbol, position, params, tid):
    row = {"symbol": symbol, "timeframe": TF, "position": dict(position),
           "average_entry": float(position.get("average_entry") or ENTRY),
           "recent_high": float(position.get("recent_high") or ENTRY), "strategy_params": params, "atr_pct": 3.0}
    if tid != "default":
        row["tenant_id"] = tid
    return row


def _fire(hub, symbol, px):
    hub.handle_ws_message(json.dumps({"time": 1, "channel": "spot.tickers", "event": "update", "result": {
        "currency_pair": symbol.replace("/", "_"), "last": str(px), "highest_bid": str(px),
        "change_percentage": "-12.5", "quote_volume": "1000"}}))


def _ws(sym, lot, params, tid, prices, cooldown=0.0, hub=None):
    hub = hub or _hub(cooldown)
    hub.update_book([_row(sym, lot, params, tid)])
    for px in prices:
        _fire(hub, sym, px)
    return hub


def _analysis(symbol, *, action="SELL_STOP_PARTIAL", long_hard_stop=True, sell_source="partial_stop",
              sources=("technical", "stop_loss"), normalized_action="SELL_PARTIAL_50"):
    return SignalAnalysis(action=action, symbol=symbol, timeframe=TF, rsi=40.0, lower_bb=0.2, vol_multiplier=1.0,
                          ampel_emoji="", ampel_text="", sources=list(sources), normalized_action=normalized_action,
                          rationale="hard stop", confidence=80.0, sell_source=sell_source,
                          long_hard_stop=long_hard_stop)


def _cycle(symbol, price, params, analysis=None):
    from services.signal_orchestrator import SignalOrchestrator

    orch = SignalOrchestrator(notify_callback=lambda *a, **k: None)
    with patch("strategies.registry.resolve_strategy_params", return_value=params):
        return orch.execute_if_needed(analysis or _analysis(symbol), {"symbol": symbol, "timeframe": TF}, price)


def _rm():
    from risk.risk_manager import RiskManager
    from services.trading_service import TradingService

    svc = TradingService()
    return RiskManager(svc.config, portfolio=svc)


def _assert_one_full(sym, held_before):
    filled = _filled_sells(sym)
    assert len(filled) == 1, [(o.get("signal"), o.get("status"), _err(o)) for o in _orders(sym)]
    row = filled[0]
    assert row["signal"] == "SELL_STOP_FULL", row.get("signal")
    assert row.get("exit_source") == "stop_loss"
    assert _amount(row) == pytest.approx(held_before, rel=1e-9)
    assert _held(sym) == pytest.approx(0.0, abs=1e-12)


def _assert_upgrade(lines, tid, source, block):
    up = _upgrades(lines)
    assert len(up) == 1, up
    lvl, msg = up[0]
    assert lvl == "INFO" and f"tenant={tid}" in msg and f"source={source}" in msg and f"block={block}" in msg, up


def _hard_band(px, params, entry=ENTRY, lot=None):
    from strategies.dca import evaluate_long_hard_stop

    return evaluate_long_hard_stop(price=px, entry=entry, position=lot or {"amount": 1.0, "average_entry": entry,
                                                                           "dca_rounds": 0},
                                   strategy_params=params, base_stop_loss_pct=BASE_SL)


# ----------------------------------------------------------------------------- preconditions (green on base)
class TestPreconditions:
    def test_repo_values(self):
        assert (FLOOR, MIN_POS, MAX_SOLD, RATIO, BASE_SL) == (300.0, 500.0, 0.99, 0.67, 50.0)
        assert float(VOL["partial_stop_pct"]) == 25.0

    def test_fixture_bands(self):
        assert _hard_band(STRK_PX, _vol_params()) == ("SELL_STOP_PARTIAL", "stop_loss")
        assert _hard_band(ULTIMA_PX, _stb_params()) == ("SELL_STOP_PARTIAL", "stop_loss")
        assert _hard_band(FULL_ZONE_PX, _vol_params()) == ("SELL_STOP_FULL", "stop_loss")
        assert LOT_USDT < HENRY_MIN_POS < MIN_POS
        assert USELESS_LOT_USDT >= MIN_POS and USELESS_LOT_USDT / 2 < FLOOR
        assert HBAR_LOT_USDT < MIN_POS


# ----------------------------------------------------------------------------- AC1
class TestAC1WsStrkHenryMinPosition:
    def test_ws_tick_one_full_over_held_then_no_rows(self):
        sym = "H2A1STRK/USDT"
        amount = LOT_USDT / STRK_PX
        with _tenant("henry"), _henry_min_pos():
            lot = _seed(sym, amount)
            with _paper(), _logs() as lines, _chain() as rec:
                _ws(sym, lot, _vol_params(), "henry", [STRK_PX])
                _assert_one_full(sym, amount)
            _assert_chain(rec, amount, "AC1")
            with _paper(), _logs() as lines2:
                n_rows = len(_orders(sym))
                _ws(sym, _lot(0.0), _vol_params(), "henry", [STRK_PX] * 5)  # later ticks / refreshed book
            assert len(_orders(sym)) == n_rows == 1, [(o.get("signal"), o.get("status")) for o in _orders(sym)]
            assert _guard_rows(sym) == []
        _assert_upgrade(lines, "henry", "exit_ws", "min_position")
        assert not _upgrades(lines2), lines2
        assert [m for _, m in lines if "exit_ws stop fire" in m and "signal=SELL_STOP_PARTIAL" in m], lines


# ----------------------------------------------------------------------------- AC2
AC2_CASES = [
    pytest.param("henry", ULTIMA_PX, LOT_USDT, "stb", "min_position", id="ULTIMA_henry_ratio_min_position"),
    pytest.param("default", STRK_PX, USELESS_LOT_USDT, "vol", "min_notional", id="USELESS_default_min_notional"),
    pytest.param("default", STRK_PX, HBAR_LOT_USDT, "vol", "min_position", id="HBAR_default_min_position"),
]


class TestAC2WsUltimaUselessHbar:
    @pytest.mark.parametrize("tid,px,usdt,prof,block", AC2_CASES)
    def test_ws_tick_one_full(self, tid, px, usdt, prof, block):
        sym = f"H2A2{block[4:7].upper()}{tid[:1].upper()}{prof}/USDT"
        params = _stb_params() if prof == "stb" else _vol_params()
        amount = usdt / px
        with _tenant(tid), _henry_min_pos():
            lot = _seed(sym, amount)
            with _paper(), _logs() as lines, _chain() as rec:
                _ws(sym, lot, params, tid, [px, px, px])
            _assert_one_full(sym, amount)
            _assert_chain(rec, amount, f"AC2 {sym} {tid}")
            assert _guard_rows(sym) == []
        _assert_upgrade(lines, tid, "exit_ws", block)


# ----------------------------------------------------------------------------- AC3
AC3_SHAPES = [
    # Rev1: a stop_loss label without long_hard_stop stays a partial reject
    # (TestRev1CycleWithoutLongHardStopStaysRejected). Only the explicit cycle origin escalates.
    pytest.param(dict(long_hard_stop=True), id="analysis_long_hard_stop"),
]


class TestAC3CyclePartialNoOrderFlag:
    @pytest.mark.parametrize("shape", AC3_SHAPES)
    @pytest.mark.parametrize("tid", TENANTS)
    def test_cycle_hard_partial_becomes_full(self, tid, shape):
        sym = f"H2A3{tid[:1].upper()}{int(shape['long_hard_stop'])}/USDT"
        amount = LOT_USDT / STRK_PX
        with _tenant(tid), _henry_min_pos():
            _seed(sym, amount)
            with _paper(), _logs() as lines:
                res = _cycle(sym, STRK_PX, _vol_params(), _analysis(sym, **shape))
            assert res is not None and res.executed, (getattr(res, "code", None), getattr(res, "message", None))
            _assert_one_full(sym, amount)
        _assert_upgrade(lines, tid, "auto", "min_position")


# ----------------------------------------------------------------------------- AC4
class TestAC4LockAndSoldCapStillBlock:
    @pytest.mark.parametrize("tid", TENANTS)
    def test_lock_ws_and_cycle_no_full(self, tid):
        sym = f"H2A4L{tid[:1].upper()}/USDT"
        amount = LOT_USDT / STRK_PX
        lock = {"enabled": True, "modes": ["no_auto_sell"], "reason": "spec H2 AC4"}
        with _tenant(tid), _henry_min_pos():
            lot = _seed(sym, amount, lock=lock)
            with _paper(), _logs() as lines:
                _ws(sym, lot, _vol_params(), tid, [STRK_PX])
                res = _cycle(sym, STRK_PX, _vol_params())
            assert res is None or not res.executed
            assert _filled_sells(sym) == [] and _held(sym) == pytest.approx(amount)
        assert not _upgrades(lines), lines

    @pytest.mark.parametrize("tid", TENANTS)
    def test_lock_risk_level_ws_order(self, tid):
        sym = f"H2A4R{tid[:1].upper()}/USDT"
        amount = LOT_USDT / STRK_PX
        with _tenant(tid), _henry_min_pos():
            _seed(sym, amount, lock={"enabled": True, "modes": ["no_auto_sell"], "reason": "spec H2 AC4"})
            order = TradeOrder(type="SELL", symbol=sym, price=STRK_PX, amount=amount * 0.5,
                               signal="SELL_STOP_PARTIAL", source="exit_ws", exit_source="stop_loss")
            with _logs() as lines, patch("strategies.registry.resolve_strategy_params", return_value=_vol_params()):
                dec = _rm().evaluate(order, TF, source="exit_ws", locked_sell_checks=True)
        assert not dec.approved and dec.code == "position_locked", (dec.code, dec.message)
        assert not _upgrades(lines), lines

    @pytest.mark.parametrize("tid", TENANTS)
    def test_sold_cap_reached_no_full(self, tid):
        sym = f"H2A4S{tid[:1].upper()}/USDT"
        amount = LOT_USDT / STRK_PX
        with _tenant(tid), _henry_min_pos():
            lot = _seed(sym, amount, peak_amount=amount * 200, sold_percent=MAX_SOLD)
            with _paper(), _logs() as lines:
                _ws(sym, lot, _vol_params(), tid, [STRK_PX])
                res = _cycle(sym, STRK_PX, _vol_params())
            assert res is not None and not res.executed and res.code == "partial_sell_guard", (res.code, res.message)
            assert _filled_sells(sym) == [] and _held(sym) == pytest.approx(amount)
            rej = _guard_rows(sym)
            assert rej and all("already sold" in _err(o) or "below minimum" in _err(o) for o in rej)
        assert not _upgrades(lines), lines

    @pytest.mark.parametrize("tid", TENANTS)
    def test_debounce_rejected_partial_does_not_consume_full_window(self, tid):
        """#723 AC-3a concept: sold-cap partial rejected, then a full-zone tick within the cooldown -> FULL."""
        sym = f"H2A4D{tid[:1].upper()}/USDT"
        amount = LOT_USDT / STRK_PX
        with _tenant(tid), _henry_min_pos():
            lot = _seed(sym, amount, peak_amount=amount * 200, sold_percent=MAX_SOLD)
            with _paper():
                _ws(sym, lot, _vol_params(), tid, [STRK_PX, FULL_ZONE_PX], cooldown=600.0)
            _assert_one_full(sym, amount)

    @pytest.mark.parametrize("tid", TENANTS)
    def test_debounce_rejected_partial_retries_throttled(self, tid):
        """#723 AC-3b concept: 10 rejected partial ticks inside the cooldown -> one guard row."""
        sym = f"H2A4T{tid[:1].upper()}/USDT"
        amount = LOT_USDT / STRK_PX
        with _tenant(tid), _henry_min_pos():
            lot = _seed(sym, amount, peak_amount=amount * 200, sold_percent=MAX_SOLD)
            with _paper():
                _ws(sym, lot, _vol_params(), tid, [STRK_PX] * 10, cooldown=600.0)
            assert len(_guard_rows(sym)) == 1, [(o.get("signal"), _err(o)) for o in _orders(sym)]
            assert _filled_sells(sym) == []


# ----------------------------------------------------------------------------- AC5
# AC5 lives in the standalone shared file tests/test_ac5_ws_ticks_plus_cycle_one_fill.py
# (byte-identical in spec_cycle_silence_fix).


# ----------------------------------------------------------------------------- AC6 (real henry states)
# Values ONLY from read-only xagent MCP results fetched 2026-10-26 ~09:52 CET (see work/h2_prod_snapshot.json):
#   xagent_lots(tenant=henry), xagent_snapshot(henry, sym), xagent_orders(henry, sym, hours=48).
# Stop params / min_notional / cap on henry not readable -> repo values (UNCLEAR, spec section 9).
REAL = {
    "STRK": {"tier": "volatile", "px": 0.04553,  # order 6b60fe676c71, created 2026-10-26T09:51:10.422557 (naive)
             "lot": {"amount": 4275.486981347987, "average_entry": 0.06560188376753506,
                     "peak_amount": 4275.486981347987, "sold_percent": 0.0, "dca_rounds": 0, "last_dca_at": None,
                     "recovery_hold": False, "recent_high": 0.06595},
             "prod_error": "Partial sell blocked: position $194.66 below minimum $300 (use full close or manual sell)"},
    "ULTIMA": {"tier": "stable", "px": 1209.0,  # order d3aea179dfcc, created 2026-10-26T09:37:37.719370 (naive)
               "lot": {"amount": 0.22765209327962072, "average_entry": 1870.2661322645288,
                       "peak_amount": 0.22765209327962072, "sold_percent": 0.0, "dca_rounds": 0, "last_dca_at": None,
                       "recovery_hold": False, "recent_high": 2118.0},
               "prod_error": "Partial sell blocked: position $275.23 below minimum $300 (use full close or manual sell)"},
}


def _real_params(case):
    return _vol_params() if REAL[case]["tier"] == "volatile" else _stb_params()


def _seed_real(sym, case):
    lot = dict(REAL[case]["lot"])
    amount = lot.pop("amount")
    return _seed(sym, amount, entry=lot.pop("average_entry"), **lot), amount


class TestAC6RealHenryReplay:
    @pytest.mark.parametrize("case", list(REAL))
    def test_guard_reproduces_prod_text(self, case):
        r = REAL[case]
        sym = f"H2A6G{case[:3]}/USDT"
        with _tenant("henry"), _henry_min_pos():
            _, amount = _seed_real(sym, case)
            assert _hard_band(r["px"], _real_params(case), entry=r["lot"]["average_entry"],
                              lot=dict(r["lot"])) == ("SELL_STOP_PARTIAL", "stop_loss")
            order = TradeOrder(type="SELL", symbol=sym, price=r["px"], amount=amount * 0.5,
                               signal="SELL_STOP_PARTIAL", source="exit_ws", exit_source="stop_loss")
            with patch("strategies.registry.resolve_strategy_params", return_value=_real_params(case)):
                blocked, msg = _rm()._partial_sell_blocked(order, TF, "exit_ws")
        assert (blocked, msg) == (True, r["prod_error"])

    @pytest.mark.parametrize("case", list(REAL))
    def test_ws_tick_at_prod_price_closes_full_real_qty(self, case):
        r = REAL[case]
        sym = f"H2A6W{case[:3]}/USDT"
        with _tenant("henry"), _henry_min_pos():
            lot, amount = _seed_real(sym, case)
            with _paper(), _logs() as lines, _chain() as rec:
                _ws(sym, lot, _real_params(case), "henry", [r["px"]] * 3)
            _assert_chain(rec, amount, f"AC6 {case}")
            base_text = [o for o in _guard_rows(sym) if _err(o) == r["prod_error"]]
            assert base_text == [], "prod rejection still produced"
            _assert_one_full(sym, amount)
        _assert_upgrade(lines, "henry", "exit_ws", "min_position")


# ----------------------------------------------------------------------------- AC7
@contextlib.contextmanager
def _daily_cap_reached(cap=1, count=1):
    from risk.risk_manager import RiskManager

    with patch.object(RiskManager, "_effective_max_daily_sells", lambda self: cap), patch.object(
        RiskManager, "_daily_sells_count", lambda self: count
    ):
        yield


class TestAC7DailyCapBypass:
    @pytest.mark.parametrize("tid", TENANTS)
    def test_control_non_hard_sell_capped(self, tid):
        sym = f"H2A7C{tid[:1].upper()}/USDT"
        amount = LOT_USDT / STRK_PX
        with _tenant(tid), _daily_cap_reached():
            _seed(sym, amount)
            order = TradeOrder(type="SELL", symbol=sym, price=STRK_PX, amount=amount, signal="SELL",
                               source="auto", exit_source="take_profit")
            with patch("strategies.registry.resolve_strategy_params", return_value=_vol_params()):
                dec = _rm().evaluate(order, TF, source="auto", locked_sell_checks=True)
        assert not dec.approved and dec.code == "max_daily_sells", (dec.code, dec.message)

    @pytest.mark.parametrize("tid", TENANTS)
    def test_ws_escalated_full_bypasses_daily_cap(self, tid):
        sym = f"H2A7F{tid[:1].upper()}/USDT"
        amount = LOT_USDT / STRK_PX
        with _tenant(tid), _henry_min_pos(), _daily_cap_reached():
            lot = _seed(sym, amount)
            with _paper(), _logs() as lines:
                _ws(sym, lot, _vol_params(), tid, [STRK_PX])
            _assert_one_full(sym, amount)
        assert [m for _, m in lines if "daily_sells_limit_bypassed_hard_stop" in m and "SELL_STOP_FULL" in m], lines


# ----------------------------------------------------------------------------- AC8 tenant isolation
class TestAC8TenantIsolation:
    def test_henry_escalation_leaves_default_untouched(self):
        sym = "H2A8ISO/USDT"
        amount = LOT_USDT / STRK_PX
        _seed(sym, amount * 4)  # default book, same symbol, $1000: no size reject on default
        with _tenant("henry"), _henry_min_pos():
            lot = _seed(sym, amount)
            with _paper(), _logs() as lines:
                _ws(sym, lot, _vol_params(), "henry", [STRK_PX])
            _assert_one_full(sym, amount)
        assert _held(sym) == pytest.approx(amount * 4)
        assert _orders(sym) == []
        _assert_upgrade(lines, "henry", "exit_ws", "min_position")

    def test_henry_floor_does_not_reach_default(self):
        """$400 lot: default (repo 500) escalates on min_position; henry (300) does not escalate on min_position."""
        from core.config import BotConfig

        lim = {}
        with _henry_min_pos():
            lim["default"] = _rm()._partial_sell_limits("H2A8LIM/USDT", TF)["min_position_usdt"]
            with _tenant("henry"):
                lim["henry"] = _rm()._partial_sell_limits("H2A8LIM/USDT", TF)["min_position_usdt"]
        assert lim == {"default": MIN_POS, "henry": HENRY_MIN_POS}, lim
        del BotConfig
