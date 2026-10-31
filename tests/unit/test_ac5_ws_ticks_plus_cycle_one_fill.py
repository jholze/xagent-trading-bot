"""Shared AC5: 10 WS ticks + 1 cycle on a small hard-stop lot -> exactly ONE fill, amount = held, nothing left.

Standalone and byte-identical in two bundles: spec_h2_ws_partial_min_size (AC5) and spec_cycle_silence_fix.
No imports from other spec test files or helper modules, no fix-specific log or code assertions: only the order tape
and the book. PAPER only, synthetic symbols, private test mongod via the runner's plugin.

Fixture: lot 250 USDT (below the repo floors: risk.min_position_usdt_for_partial_sell 500, min_sell_notional_usdt
300 at config.json:616-617), entry 1.0, price 0.70 (-30 %, inside the volatile_altcoin partial band 25..50 %,
config.json:1480, stop_loss_pct 50 at :117). The WS book row is re-added before every tick (stale book) and the
execute-layer recent-exit/in-flight guards are cleared between ticks, so neither shortcut can hide a second sell.
Then one cycle evaluation (SignalOrchestrator.execute_if_needed) with a hard-stop SELL_STOP_PARTIAL analysis.
"""
from __future__ import annotations

import contextlib
import json
import os
from unittest.mock import patch

import pytest

TF = "1h"
ENTRY = 1.0
PX = 0.70
LOT_USDT = 250.0
TENANTS = [pytest.param("default", id="default"), pytest.param("henry", id="henry")]


def _cfg():
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "..", "..", "config.json"), encoding="utf-8") as fh:
        return json.load(fh)


CFG = _cfg()
RISK = CFG["risk"]
VOL = CFG["volatile_altcoin"]


def _params():
    return {"stop_loss_pct": float(CFG["stop_loss_pct"]), "partial_stop_pct": VOL["partial_stop_pct"],
            "dca": VOL["dca"], "exit_ladder": VOL["exit_ladder"]}


def _clear_exec_guards():
    import services.exit_realtime.execute as ex

    ex._inflight.clear()
    ex._last_exit_at.clear()


@pytest.fixture(autouse=True)
def _repo_floors_and_clean_books():
    import data_manager
    from strategies.positions import reset_all_position_stores_for_tests

    risk = data_manager.get_config().setdefault("risk", {})
    keys = ("min_sell_notional_usdt", "min_position_usdt_for_partial_sell", "block_partial_sell_if_sold_percent_above")
    saved = {k: risk.get(k) for k in keys}
    for k in keys:
        risk[k] = RISK[k]
    reset_all_position_stores_for_tests()
    _clear_exec_guards()
    yield
    _clear_exec_guards()
    reset_all_position_stores_for_tests()
    for k, v in saved.items():
        if v is None:
            risk.pop(k, None)
        else:
            risk[k] = v


@contextlib.contextmanager
def _tenant(tid):
    if tid == "default":
        yield
        return
    from core.tenant_routing import tenant_cycle_context
    from strategies import positions as pm

    with patch("bus.locks.ledger_lock", lambda *_a, **_k: contextlib.nullcontext()), patch(
        "strategies.positions.bootstrap_positions", lambda *_a, **_k: None
    ), tenant_cycle_context(tid):
        assert pm._current_active_key()[0] == tid
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


def _seed(symbol, amount):
    from strategies import positions as pm
    from strategies.positions import _deserialize_position

    lot = _deserialize_position({"amount": amount, "average_entry": ENTRY, "recent_high": ENTRY, "side": "long",
                                 "peak_amount": amount, "sold_percent": 0.0})
    pm._active_store()[pm.get_key(symbol, TF)] = lot
    pm._positions_state[pm._current_active_key()] = "known"
    return lot


def _orders(symbol):
    from data_manager import load_orders, resolve_ledger_scope
    from services import order_service

    order_service._ORDERS_READ_CACHE.clear()
    doc = load_orders(resolve_ledger_scope()) or {}
    return [o for o in (doc.get("orders") or []) if o.get("symbol") == symbol]


def _filled_sells(symbol):
    return [o for o in _orders(symbol)
            if o.get("status") == "filled" and str(o.get("type") or o.get("side") or "").upper() == "SELL"]


def _amount(row):
    for k in ("amount", "filled_amount", "requested_amount"):
        if row.get(k) is not None:
            return float(row[k])
    return float((row.get("request") or {}).get("amount") or 0)


def _held(symbol):
    from strategies.positions import get_position

    return float((get_position(symbol, TF) or {}).get("amount") or 0)


def _row(symbol, lot, tid):
    row = {"symbol": symbol, "timeframe": TF, "position": dict(lot), "average_entry": ENTRY, "recent_high": ENTRY,
           "strategy_params": _params(), "atr_pct": 3.0}
    if tid != "default":
        row["tenant_id"] = tid
    return row


def _fire(hub, symbol, px):
    hub.handle_ws_message(json.dumps({"time": 1, "channel": "spot.tickers", "event": "update", "result": {
        "currency_pair": symbol.replace("/", "_"), "last": str(px), "highest_bid": str(px),
        "change_percentage": "-12.5", "quote_volume": "1000"}}))


def _cycle(symbol, price):
    from core.models import SignalAnalysis
    from services.signal_orchestrator import SignalOrchestrator

    an = SignalAnalysis(action="SELL_STOP_PARTIAL", symbol=symbol, timeframe=TF, rsi=40.0, lower_bb=0.2,
                        vol_multiplier=1.0, ampel_emoji="", ampel_text="", sources=["technical", "stop_loss"],
                        normalized_action="SELL_PARTIAL_50", rationale="hard stop", confidence=80.0,
                        sell_source="partial_stop", long_hard_stop=True)
    orch = SignalOrchestrator(notify_callback=lambda *a, **k: None)
    with patch("strategies.registry.resolve_strategy_params", return_value=_params()):
        return orch.execute_if_needed(an, {"symbol": symbol, "timeframe": TF}, price)


class TestAC5TenWsTicksPlusCycleOneFill:
    @pytest.mark.parametrize("tid", TENANTS)
    def test_ten_ticks_and_cycle_one_fill(self, tid):
        from services.exit_realtime.hub import ExitRealtimeHub

        sym = f"AC5T{tid[:1].upper()}/USDT"
        amount = LOT_USDT / PX
        assert LOT_USDT < float(RISK["min_sell_notional_usdt"]) <= float(RISK["min_position_usdt_for_partial_sell"])
        with _tenant(tid):
            lot = _seed(sym, amount)
            with _paper():
                hub = ExitRealtimeHub({"exit_realtime": {"enabled": True, "mode": "live", "sources": ["stop_loss"],
                                                         "live_cooldown_sec": 0.0, "default_atr_pct": 3.0}})
                for _ in range(10):
                    hub.update_book([_row(sym, lot, tid)])  # stale book row on every tick
                    _fire(hub, sym, PX)
                    _clear_exec_guards()  # no recent-exit shortcut between ticks
                _cycle(sym, PX)
            fills = _filled_sells(sym)
            assert len(fills) == 1, [(o.get("signal"), o.get("status"), _amount(o)) for o in _orders(sym)]
            assert _amount(fills[0]) == pytest.approx(amount, rel=1e-9)
            assert _held(sym) == pytest.approx(0.0, abs=1e-12)
