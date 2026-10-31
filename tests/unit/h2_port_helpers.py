"""H2 port helpers (NOT a test module). Verbatim copies, with credit, of the #723 bundle test helpers:
  spec_723_partial_escalation/tests/test_723_partial_escalation.py (constants, fixtures, _cycle/_hub/_fire ...),
  test_723_annex_a.py (_rm, _partial_order, _guard_call), test_723_annex_b_e1.py (_tenant, _key_missing, _logs_lvl,
  _seed_t, ...). Only change: the repo floors are read from config.json at 930792a3 (same values: 300 / 500 / 0.99).
The #723 files themselves are untouched.
"""

from __future__ import annotations

import contextlib
import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from core.models import SignalAnalysis
from services.exit_realtime.hub import ExitRealtimeHub

TF = "1h"
FULL_PCT = 30.0  # fixture
PARTIAL_PCT = 20.0  # fixture
ENTRY = 1.0
HBAR_PX = 0.774  # -22.6 %
HBAR_AMOUNT = 323.0  # 323 * 0.774 = 250.0 USDT remaining
HBAR_PEAK = 646.0  # sold_percent 0.5
FULL_ZONE_PX = 0.68  # -32 % > FULL_PCT


def _repo_risk() -> dict:
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.abspath(os.path.join(here, "..", ".."))
    with open(os.path.join(root, "config.json"), encoding="utf-8") as fh:
        return json.load(fh)["risk"]


REPO_RISK = _repo_risk()
FLOOR = float(REPO_RISK["min_sell_notional_usdt"])  # 300 at 930792a3 (config.json:616)
MIN_POS = float(REPO_RISK["min_position_usdt_for_partial_sell"])  # 500 (config.json:617)
MAX_SOLD = float(REPO_RISK["block_partial_sell_if_sold_percent_above"])  # 0.99 (config.json:618)


def _params(full=FULL_PCT, partial=PARTIAL_PCT):
    return {
        "stop_loss_pct": full,
        "partial_stop_pct": partial,
        "dca": {"pause_partial_stop_during_dca": True, "grace_hours_after_dca": 12},
    }


def _lot(amount, entry=ENTRY, **extra):
    from strategies.positions import _deserialize_position

    raw = {
        "amount": amount,
        "average_entry": entry,
        "recent_high": entry,
        "side": "long",
        "peak_amount": extra.pop("peak_amount", amount),
        "sold_percent": extra.pop("sold_percent", 0.0),
    }
    lock = extra.pop("lock", None)
    raw.update(extra)
    lot = _deserialize_position(raw)
    for key, value in extra.items():
        if key not in lot:
            lot[key] = value
    if lock is not None:
        lot["lock"] = lock
    return lot


def _seed(symbol, amount, **extra):
    from strategies import positions as pm

    lot = _lot(amount, **extra)
    pm.positions[pm.get_key(symbol, TF)] = lot
    return lot


def _seed_hbar(symbol, **extra):
    return _seed(symbol, HBAR_AMOUNT, peak_amount=HBAR_PEAK, sold_percent=0.5, **extra)


def _held(symbol):
    from strategies.positions import get_position

    return float((get_position(symbol, TF) or {}).get("amount") or 0)


def _orders(symbol):
    from data_manager import load_orders, resolve_ledger_scope
    from services import order_service

    order_service._ORDERS_READ_CACHE.clear()
    doc = load_orders(resolve_ledger_scope()) or {}
    return [o for o in (doc.get("orders") or []) if o.get("symbol") == symbol]


def _filled_sells(symbol):
    return [
        o
        for o in _orders(symbol)
        if o.get("status") == "filled" and str(o.get("type") or o.get("side") or "").upper() == "SELL"
    ]


def _amount(row) -> float:
    for key in ("amount", "filled_amount", "requested_amount"):
        if row.get(key) is not None:
            return float(row[key])
    req = row.get("request") or {}
    return float(req.get("amount") or 0)


def _analysis(symbol, *, action="SELL_STOP_PARTIAL", long_hard_stop=True, sell_source="partial_stop",
              sources=("technical", "stop_loss"), normalized_action="SELL_PARTIAL_50"):
    return SignalAnalysis(
        action=action,
        symbol=symbol,
        timeframe=TF,
        rsi=40.0,
        lower_bb=0.2,
        vol_multiplier=1.0,
        ampel_emoji="",
        ampel_text="",
        sources=list(sources),
        normalized_action=normalized_action,
        rationale="hard stop",
        confidence=80.0,
        sell_source=sell_source,
        long_hard_stop=long_hard_stop,
    )


def _cycle(symbol, price, analysis=None, params=None):
    from services.signal_orchestrator import SignalOrchestrator

    orch = SignalOrchestrator(notify_callback=lambda *a, **k: None)
    with patch("strategies.registry.resolve_strategy_params", return_value=params or _params()):
        return orch.execute_if_needed(
            analysis or _analysis(symbol), {"symbol": symbol, "timeframe": TF}, price
        )


def _hub(cooldown):
    return ExitRealtimeHub(
        {
            "exit_realtime": {
                "enabled": True,
                "mode": "live",
                "sources": ["stop_loss"],
                "live_cooldown_sec": cooldown,
                "default_atr_pct": 3.0,
            }
        }
    )


def _row(symbol, position, params=None):
    return {
        "symbol": symbol,
        "timeframe": TF,
        "position": dict(position),
        "average_entry": float(position.get("average_entry") or ENTRY),
        "recent_high": float(position.get("recent_high") or ENTRY),
        "strategy_params": params or _params(),
        "atr_pct": 3.0,
    }


def _fire(hub, symbol, px):
    hub.handle_ws_message(
        json.dumps(
            {
                "time": 1,
                "channel": "spot.tickers",
                "event": "update",
                "result": {
                    "currency_pair": symbol.replace("/", "_"),
                    "last": str(px),
                    "highest_bid": str(px),
                    "change_percentage": "-12.5",
                    "quote_volume": "1000",
                },
            }
        )
    )


@contextmanager
def _paper():
    import data_manager
    from execution.gate_adapter import GateExecutionAdapter
    from services.trading_service import TradingService

    raw = data_manager.get_config()
    shorts = raw.setdefault("shorts", {})
    prev_auto = shorts.get("auto_after_sell", None)
    shorts["auto_after_sell"] = False
    prev_failed = GateExecutionAdapter._shadow_markets_failed
    prev_cache = GateExecutionAdapter._shadow_markets_cache
    GateExecutionAdapter._shadow_markets_failed = True
    GateExecutionAdapter._shadow_markets_cache = None
    with patch.object(TradingService, "refresh", lambda self: self), patch(
        "notifications.telegram_commands.position_display.send_positions_snapshot",
        lambda *a, **k: None,
    ), patch("core.operator_notify.notify_operator", lambda *a, **k: None):
        try:
            yield
        finally:
            GateExecutionAdapter._shadow_markets_failed = prev_failed
            GateExecutionAdapter._shadow_markets_cache = prev_cache
            if prev_auto is None:
                shorts.pop("auto_after_sell", None)
            else:
                shorts["auto_after_sell"] = prev_auto


@contextmanager
def _logs():
    lines: list[str] = []

    def _rec(message, level="INFO"):
        lines.append(str(message))

    with patch("logger.log", side_effect=_rec), patch(
        "services.exit_realtime.hub.log", side_effect=_rec
    ):
        yield lines


def _clear_guards():
    import services.exit_realtime.execute as ex

    ex._inflight.clear()
    ex._last_exit_at.clear()


@pytest.fixture(autouse=True)
def _prod_floors_and_clean_books():
    """Repo config.json floors (the unit conftest lowers them to 15/25)."""
    import data_manager
    from strategies.positions import reset_all_position_stores_for_tests

    raw = data_manager.get_config()
    risk = raw.setdefault("risk", {})
    for key in (
        "min_sell_notional_usdt",
        "min_position_usdt_for_partial_sell",
        "block_partial_sell_if_sold_percent_above",
    ):
        risk[key] = REPO_RISK[key]
    risk.setdefault("position_locks", {})["enabled"] = True
    reset_all_position_stores_for_tests()
    _clear_guards()
    yield
    _clear_guards()
    reset_all_position_stores_for_tests()


def _assert_one_full(symbol, held_before):
    filled = _filled_sells(symbol)
    assert len(filled) == 1, [(o.get("signal"), o.get("status")) for o in _orders(symbol)]
    row = filled[0]
    assert row["signal"] == "SELL_STOP_FULL", row.get("signal")
    assert row.get("exit_source") == "stop_loss"
    assert _amount(row) == pytest.approx(held_before, rel=1e-6)
    assert _held(symbol) == pytest.approx(0.0, abs=1e-9)



def _risk_code(row):
    return (row.get("risk") or {}).get("code")


def _rm():
    from risk.risk_manager import RiskManager
    from services.trading_service import TradingService

    svc = TradingService()
    return RiskManager(svc.config, portfolio=svc)


def _partial_order(symbol, amount, price=HBAR_PX, source="auto"):
    from core.models import TradeOrder

    return TradeOrder(
        type="SELL", symbol=symbol, price=price, amount=amount, signal="SELL_STOP_PARTIAL",
        source=source, exit_source="stop_loss",
    )


def _guard_call(symbol, amount, source="auto", price=HBAR_PX):
    order = _partial_order(symbol, amount, price=price, source=source)
    with patch("strategies.registry.resolve_strategy_params", return_value=_params()):
        blocked, msg = _rm()._partial_sell_blocked(order, TF, source)
    return order, blocked, msg



CAP_KEY = "block_partial_sell_if_sold_percent_above"
TENANTS = [pytest.param("default", id="default"), pytest.param("henry", id="henry")]


@contextlib.contextmanager
def _tenant(tid):
    if tid == "default":
        yield
        return
    # production per-tenant cycle context (core/tenant_routing.py:157-179): tenant_context + activate the book
    from core.tenant_routing import tenant_cycle_context
    from strategies import positions as pm

    # Test seams (henry only): no Redis in the unit env, so the satellite ledger lock is a no-op, as in
    # tests/unit/test_issue_644_live_caps.py:2124 and test_mcp_lock_tenant_race_lockbug.py:94; and the satellite
    # book is the RAM seed (bootstrap_positions from the Mongo ledger, strategies/positions.py:1006, is stubbed),
    # so the WS path's restoring_tenant_cycle_context (hub.py:1272) sees the same lot as the cycle.
    with patch("bus.locks.ledger_lock", lambda *_a, **_k: contextlib.nullcontext()), patch(
        "strategies.positions.bootstrap_positions", lambda *_a, **_k: None
    ), tenant_cycle_context(tid):
        assert pm._current_active_key()[0] == tid, pm._current_active_key()
        yield


_POP = object()
BAD_CAPS = [
    pytest.param(_POP, id="missing"),
    pytest.param(None, id="none"),
    pytest.param(0, id="zero"),
    pytest.param(-0.5, id="negative"),
    pytest.param(1.5, id="above_one"),
    pytest.param("0.99", id="string"),
    pytest.param(True, id="bool"),
]


@contextlib.contextmanager
def _key_missing(value=_POP):
    """risk_config without CAP_KEY (value=_POP) or with an invalid CAP_KEY value; rest of the block unchanged."""
    from core.config import BotConfig

    def _risk(self):
        risk = {k: v for k, v in (self._raw.get("risk") or {}).items() if k != CAP_KEY}
        if value is not _POP:
            risk[CAP_KEY] = value
        return risk

    with patch.object(BotConfig, "risk_config", property(_risk)):
        yield


@contextlib.contextmanager
def _logs_lvl():
    lines: list[tuple[str, str]] = []

    def _rec(message, level="INFO"):
        lines.append((str(level), str(message)))

    with patch("logger.log", side_effect=_rec), patch("services.exit_realtime.hub.log", side_effect=_rec):
        yield lines


def _errors(lines, *needles):
    return [m for lvl, m in lines if lvl == "ERROR" and all(n in m for n in needles)]


def _seed_t(symbol, amount, **extra):
    """Seed into the ACTIVE tenant's RAM book (default: pm.positions; henry: its own store)."""
    from strategies import positions as pm

    lot = _lot(amount, **extra)
    store = pm._active_store()
    store[pm.get_key(symbol, TF)] = lot
    pm._positions_state[pm._current_active_key()] = "known"
    return lot


def _full_analysis(sym):
    return _analysis(sym, action="SELL_STOP_FULL", sell_source="stop_loss", normalized_action="SELL_FULL")


def _repo_config():
    with open(os.path.join(os.getcwd(), "config.json")) as fh:
        return json.load(fh)



@contextlib.contextmanager
def _strategy_override(value):
    """Strategy-level override of CAP_KEY on top of the real strategy params (risk block unchanged)."""
    from core.config import BotConfig

    orig = BotConfig.strategy_params

    def _sp(self, symbol, timeframe):
        return {**(orig(self, symbol, timeframe) or {}), CAP_KEY: value}

    with patch.object(BotConfig, "strategy_params", _sp):
        yield


