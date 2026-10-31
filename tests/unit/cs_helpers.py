"""Shared helpers for the cycle-silence spec tests (self-contained; no imports from other bundles).

PAPER only, synthetic symbols. Mocked at the edge only: ticker (get_prices_batch), OHLCV/indicator reads
(MarketService fetch_*), Telegram/operator notify, TradingService.refresh. NOT mocked: process_eval_job,
SignalOrchestrator.process_coin, DecisionEngine.evaluate/_merge_sell, get_strategy, TechnicalRSIStrategy.analyze,
GridStrategy.analyze, TradingService.execute_order, RiskManager.evaluate (both are wrapped as spies only).
"""
from __future__ import annotations

import contextlib
import json
import os
import time
from unittest.mock import patch

TF = "4h"
ENTRY = 1.0

NEUTRAL = {  # indicators that give TechnicalRSIStrategy a HOLD (no RSI/BB sell, no buy)
    "rsi": 42.0, "lower_bb": 0.50, "middle_bb": 0.70, "upper_bb": 0.90,
    "vol_multiplier": 1.0, "atr": 0.02, "atr_pct": 3.0,
}


def repo_cfg():
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "..", "..", "config.json"), encoding="utf-8") as fh:
        return json.load(fh)


CFG = repo_cfg()


# ----------------------------------------------------------------------------- fake redis (real ZSET/HASH semantics)
class FakeRedis:
    """In-memory subset of redis-py used by bus/eval_queue.py: ZADD/ZSCORE/ZPOPMIN/ZRANGE/ZREVRANGE/ZREM/ZCARD/
    ZREMRANGEBYRANK, HGET/HSET/HDEL/HGETALL, GET/SET/INCR/DELETE, pipeline() (executes immediately, in order)."""

    def __init__(self):
        self.z: dict[str, dict[str, float]] = {}
        self.h: dict[str, dict[str, str]] = {}
        self.kv: dict[str, str] = {}
        self.writes = 0

    # pipeline: immediate execution keeps order semantics of a MULTI/EXEC block for a single client
    def pipeline(self, *a, **k):
        return self

    def execute(self):
        return []

    def _zs(self, key):
        return self.z.setdefault(key, {})

    def _sorted(self, key):
        return sorted(self._zs(key).items(), key=lambda kv: (kv[1], kv[0]))

    def zadd(self, key, mapping, **_k):
        self.writes += 1
        self._zs(key).update({str(m): float(s) for m, s in mapping.items()})
        return len(mapping)

    def zscore(self, key, member):
        return self._zs(key).get(str(member))

    def zcard(self, key):
        return len(self._zs(key))

    def zrem(self, key, *members):
        self.writes += 1
        n = 0
        for m in members:
            n += 1 if self._zs(key).pop(str(m), None) is not None else 0
        return n

    @staticmethod
    def _slice(items, start, end):
        n = len(items)
        if start < 0:
            start = max(0, n + start)
        if end < 0:
            end = n + end
        return items[start:end + 1]

    def zrange(self, key, start, end, withscores=False, **_k):
        items = self._slice(self._sorted(key), start, end)
        return items if withscores else [m for m, _ in items]

    def zrevrange(self, key, start, end, withscores=False, **_k):
        items = self._slice(list(reversed(self._sorted(key))), start, end)
        return items if withscores else [m for m, _ in items]

    def zremrangebyrank(self, key, start, end):
        self.writes += 1
        victims = self._slice(self._sorted(key), start, end)
        for m, _ in victims:
            self._zs(key).pop(m, None)
        return len(victims)

    def zpopmin(self, key, count=1):
        items = self._sorted(key)[: max(1, int(count))]
        for m, _ in items:
            self._zs(key).pop(m, None)
        return items

    def hget(self, key, field):
        return self.h.get(key, {}).get(str(field))

    def hset(self, key, field=None, value=None, mapping=None):
        self.writes += 1
        d = self.h.setdefault(key, {})
        if mapping:
            d.update({str(k): str(v) for k, v in mapping.items()})
        if field is not None:
            d[str(field)] = str(value)
        return 1

    def hdel(self, key, *fields):
        self.writes += 1
        return sum(1 for f in fields if self.h.get(key, {}).pop(str(f), None) is not None)

    def hgetall(self, key):
        return dict(self.h.get(key, {}))

    def get(self, key):
        return self.kv.get(key)

    def set(self, key, value, **_k):
        self.writes += 1
        self.kv[key] = str(value)
        return True

    def incr(self, key, amount=1):
        self.writes += 1
        self.kv[key] = str(int(self.kv.get(key, "0")) + int(amount))
        return int(self.kv[key])

    def delete(self, *keys):
        self.writes += 1
        for k in keys:
            self.z.pop(k, None)
            self.h.pop(k, None)
            self.kv.pop(k, None)
            self.kv.pop(("list", k), None)
        return len(keys)

    def lpush(self, key, *values):
        self.writes += 1
        lst = self.kv.setdefault(("list", key), [])
        for v in values:
            lst.insert(0, str(v))
        return len(lst)

    def lrange(self, key, start, end):
        return self._slice(list(self.kv.get(("list", key), [])), start, end)

    def scan_iter(self, match=None, count=None):
        import fnmatch

        for k in list(self.z) + list(self.h) + [k for k in self.kv if isinstance(k, str)]:
            if match is None or fnmatch.fnmatch(k, match):
                yield k


QCFG = {
    "architecture": {
        "eval_queue_enabled": True,
        "key_prefix": "cstest:",
        "eval_debounce_sec": 45,
        "eval_meta_interval_sec": 0,
        "eval_position_heartbeat_sec": 300,
        "eval_stale_sec": 7200,
        "eval_queue_max_len": 500,
    }
}


@contextlib.contextmanager
def fake_queue(redis=None):
    r = redis or FakeRedis()
    with patch("bus.eval_queue._client", return_value=r):
        yield r


# ----------------------------------------------------------------------------- tenants / books / paper
@contextlib.contextmanager
def tenant(tid):
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


def seed_lot(symbol, amount, entry=ENTRY, tf=TF, **extra):
    from strategies import positions as pm
    from strategies.positions import _deserialize_position

    raw = {"amount": amount, "average_entry": entry, "recent_high": extra.pop("recent_high", entry), "side": "long",
           "peak_amount": amount, "sold_percent": 0.0, "dca_rounds": 0, "strategy_tier": extra.pop("tier", "stable")}
    raw.update(extra)
    lot = _deserialize_position(raw)
    for k, v in extra.items():
        lot.setdefault(k, v)
    pm._active_store()[pm.get_key(symbol, tf)] = lot
    pm._positions_state[pm._current_active_key()] = "known"
    return lot


def held(symbol, tf=TF):
    from strategies.positions import get_position

    return float((get_position(symbol, tf) or {}).get("amount") or 0)


@contextlib.contextmanager
def paper(price_by_symbol, indicators=None, grid_weight=None):
    """Edge mocks only. grid_weight=(grid, momentum) forces allocation weights through the REAL allocator output,
    so the REAL get_strategy (registry.py:541-544) selects GridStrategy."""
    import data_manager
    from execution.gate_adapter import GateExecutionAdapter
    from services.market_service import MarketService
    from services.trading_service import TradingService

    ind = dict(indicators or NEUTRAL)
    shorts = data_manager.get_config().setdefault("shorts", {})
    prev_auto = shorts.get("auto_after_sell", None)
    shorts["auto_after_sell"] = False
    prev_f, prev_c = GateExecutionAdapter._shadow_markets_failed, GateExecutionAdapter._shadow_markets_cache
    GateExecutionAdapter._shadow_markets_failed, GateExecutionAdapter._shadow_markets_cache = True, None
    stack = contextlib.ExitStack()
    try:
        stack.enter_context(patch("services.eval_queue_runtime.get_prices_batch",
                                  lambda syms, *a, **k: {s: price_by_symbol.get(s, 0.0) for s in syms}))
        _wl = lambda *a, **k: [{"symbol": s, "timeframe": TF, "active": True} for s in price_by_symbol]  # noqa: E731
        stack.enter_context(patch("services.eval_queue_runtime.load_effective_watchlist", _wl))
        stack.enter_context(patch("strategies.decision_engine.load_effective_watchlist", _wl))
        # edge: exchange listing lookup (network) on the no-position 15m-watch path -> "not listed", no watch
        stack.enter_context(patch("price_fetcher.passes_exchange_filter", lambda *a, **k: (False, "test")))
        stack.enter_context(patch.object(MarketService, "fetch_ohlcv_and_indicators",
                                         lambda self, s, tf, px, limit=100, **k: (None, dict(ind))))
        stack.enter_context(patch.object(MarketService, "fetch_indicators", lambda self, *a, **k: dict(ind)))
        stack.enter_context(patch.object(MarketService, "fetch_ohlcv", lambda self, *a, **k: None))
        stack.enter_context(patch.object(MarketService, "fetch_funding_rate", lambda self, *a, **k: None))
        stack.enter_context(patch.object(MarketService, "fetch_15m_sensor_metrics", lambda self, *a, **k: None))
        stack.enter_context(patch.object(TradingService, "refresh", lambda self: self))
        # unit env has no Redis: ledger lock is a no-op (same seam as the H2 bundle's tenant helper)
        stack.enter_context(patch("bus.locks.ledger_lock", lambda *_a, **_k: contextlib.nullcontext()))
        stack.enter_context(patch("notifications.telegram_commands.position_display.send_positions_snapshot",
                                  lambda *a, **k: None))
        stack.enter_context(patch("core.operator_notify.notify_operator", lambda *a, **k: None))
        if grid_weight is not None:
            g, m = grid_weight
            import intelligence.strategy_allocator as sa

            orig = sa.StrategyAllocator.allocate if hasattr(sa, "StrategyAllocator") else None
            if orig is not None:
                def _alloc(self, *a, **k):
                    out = orig(self, *a, **k)
                    out.strategy_weights = {**dict(out.strategy_weights or {}), "grid": g, "momentum": m}
                    return out
                stack.enter_context(patch.object(sa.StrategyAllocator, "allocate", _alloc))
        yield
    finally:
        stack.close()
        GateExecutionAdapter._shadow_markets_failed, GateExecutionAdapter._shadow_markets_cache = prev_f, prev_c
        if prev_auto is None:
            shorts.pop("auto_after_sell", None)
        else:
            shorts["auto_after_sell"] = prev_auto


@contextlib.contextmanager
def spies():
    """Wrap (never replace) TradingService.execute_order, RiskManager.evaluate and the strategy analyze calls."""
    from risk.risk_manager import RiskManager
    from services.trading_service import TradingService
    from strategies.technical_rsi_bb import TechnicalRSIStrategy
    from strategies.grid import GridStrategy
    from core.tenant_context import resolve_tenant_id

    rec = {"orders": [], "risk": [], "analyze": [], "grid": []}
    o_eo, o_rv = TradingService.execute_order, RiskManager.evaluate
    o_ta, o_ga = TechnicalRSIStrategy.analyze, GridStrategy.analyze

    def eo(self, order, *a, **k):
        res = o_eo(self, order, *a, **k)
        rec["orders"].append({"tenant": resolve_tenant_id(), "symbol": order.symbol, "signal": order.signal,
                              "source": getattr(order, "source", None) or k.get("source"),
                              "exit_source": getattr(order, "exit_source", None), "amount": order.amount,
                              "executed": bool(getattr(res, "executed", False)),
                              "code": getattr(res, "code", None), "msg": str(getattr(res, "message", "") or "")[:160]})
        return res

    def rv(self, order, *a, **k):
        dec = o_rv(self, order, *a, **k)
        rec["risk"].append({"tenant": resolve_tenant_id(), "symbol": order.symbol, "signal": order.signal,
                            "approved": bool(dec.approved), "locked": bool(k.get("locked_sell_checks", False)),
                            "code": getattr(dec, "code", None)})
        return dec

    def ta(self, coin, market, *a, **k):
        rec["analyze"].append((resolve_tenant_id(), coin.get("symbol")))
        return o_ta(self, coin, market, *a, **k)

    def ga(self, coin, market, *a, **k):
        rec["grid"].append((resolve_tenant_id(), coin.get("symbol")))
        return o_ga(self, coin, market, *a, **k)

    with patch.object(TradingService, "execute_order", autospec=True, side_effect=eo), patch.object(
        RiskManager, "evaluate", autospec=True, side_effect=rv
    ), patch.object(TechnicalRSIStrategy, "analyze", autospec=True, side_effect=ta), patch.object(
        GridStrategy, "analyze", autospec=True, side_effect=ga
    ):
        yield rec


def orchestrator():
    from services.signal_orchestrator import SignalOrchestrator

    return SignalOrchestrator(notify_callback=lambda *a, **k: None)


def job(symbol, reason="position_heartbeat", priority=30, tid="default", tf=TF):
    from bus.eval_queue import EvalJob

    return EvalJob(symbol=symbol, timeframe=tf, reason=reason, priority=priority, enqueued_at=time.time(),
                   tenant_id=tid)


def orders_for(symbol):
    from data_manager import load_orders, resolve_ledger_scope
    from services import order_service

    order_service._ORDERS_READ_CACHE.clear()
    doc = load_orders(resolve_ledger_scope()) or {}
    return [o for o in (doc.get("orders") or []) if o.get("symbol") == symbol]


def auto_orders(symbol):
    return [o for o in orders_for(symbol) if str(o.get("source") or "") == "auto"]
