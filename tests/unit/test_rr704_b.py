"""Lena B1/B2 and Viktor C1/A3 on top of b4d8d52a.

Fresh threads do not inherit contextvars. Backtests must not touch the live
book. The gainer route must not retry. The entry sensor stays on default.
"""

from __future__ import annotations

import copy
import os
import threading
from contextlib import contextmanager

import numpy as np
import pandas as pd
import pytest

import strategies.positions as P
from core.models import MarketContext
from core.tenant_context import DEFAULT_TENANT, tenant_context
from tests.support.offline import gate_prices_listed
from tests.unit.test_mcp_lock_tenant_race_lockbug import (  # noqa: F401
    _lot,
    ledger,
)

_ZEC = "ZEC/USDT"
_TF = "4h"
_ZEC_KEY = "ZEC_USDT_4h"


def _fresh(fn):
    out = {}

    def _run():
        try:
            out["v"] = fn()
        except BaseException as exc:  # noqa: BLE001
            out["exc"] = exc

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(20)
    assert not thread.is_alive(), "fresh thread hung"
    return out


def _book_sig() -> dict:
    return {
        key: (
            float(row.get("amount") or 0),
            float(row.get("average_entry") or 0),
            bool(row.get("time_profit_exit_done")),
        )
        for key, row in P.positions.items()
        if isinstance(row, dict)
    }


def _synthetic_ohlcv(rows: int = 80) -> pd.DataFrame:
    rng = np.random.default_rng(3)
    close = 100 + np.cumsum(rng.normal(0, 0.3, rows))
    for i in range(30, rows, 25):
        close[i : i + 3] -= 8
    return pd.DataFrame(
        {
            "ts": [1_700_000_000_000 + i * 4 * 3600 * 1000 for i in range(rows)],
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": rng.uniform(1000, 5000, rows),
        }
    )


def _patch_prices(monkeypatch):
    monkeypatch.setattr("price_fetcher.get_gate_prices_batch", gate_prices_listed)
    monkeypatch.setattr("price_fetcher.get_ticker_price", lambda symbol, exchange=None: 1.0)
    monkeypatch.setattr(
        "services.market_service.MarketService._fetch_ohlcv",
        lambda self, *a, **k: None,
    )
    monkeypatch.setattr(
        "services.market_service.MarketService.fetch_15m_sensor_metrics",
        lambda self, *a, **k: None,
    )
    monkeypatch.setattr(
        "services.market_service.MarketService.fetch_exit_metrics_15m",
        lambda self, *a, **k: None,
    )
    monkeypatch.setattr(
        "services.market_service.MarketService.fetch_exit_metrics_1h",
        lambda self, *a, **k: None,
    )


def _forbid_live_positions(monkeypatch) -> list:
    calls: list[str] = []

    def boom(name):
        def _inner(*_a, **_k):
            calls.append(name)
            raise AssertionError(f"live {name} under sim_state")

        return _inner

    monkeypatch.setattr("strategies.decision_engine.get_position", boom("get_position"))
    monkeypatch.setattr("strategies.technical_rsi_bb.get_position", boom("get_position"))
    monkeypatch.setattr("strategies.decision_engine.flush_positions", boom("flush_positions"))
    monkeypatch.setattr(
        "strategies.decision_engine.mark_trailing_take_profit_step",
        boom("mark_trailing_take_profit_step"),
    )
    monkeypatch.setattr(
        "strategies.decision_engine.mark_profit_max_lifetime_done",
        boom("mark_profit_max_lifetime_done"),
    )
    return calls


def test_B1_backtests_in_fresh_thread_do_not_touch_the_live_book(monkeypatch):
    """sim_state must not read or write the live book, and must not bind a tenant.

    A bound backtest inserts a qty-0 shell via get_position. Re-reading live
    positions under sim_state raises here.
    """
    from hermes.backtester import Backtester
    from hermes.pipeline_backtest import PipelineBacktester
    from strategies.decision_engine import DecisionEngine
    from strategies.technical_rsi_bb import TechnicalRSIStrategy

    _patch_prices(monkeypatch)
    calls = _forbid_live_positions(monkeypatch)
    P.reset_all_position_stores_for_tests()
    P.positions[_ZEC_KEY] = _lot(1.5945, 1534.20)
    before = _book_sig()
    before_keys = set(P.positions)
    params = {
        "buy_regime": "both",
        "rsi_buy_low": 20,
        "rsi_buy_high": 55,
        "volume_multiplier": 1.0,
        "cmc_trust_score": 70.0,
        "cmc_min_confidence": 50.0,
        "rsi_sell_30": 70,
        "rsi_sell_20": 85,
        "stop_loss_pct": 15.0,
    }
    df = _synthetic_ohlcv()

    def worker():
        from core.tenant_context import current_tenant_context

        assert current_tenant_context() is None
        pipeline = PipelineBacktester().run(
            "TEST/USDT",
            "4h",
            params,
            df,
            initial_position={"amount": 1.0, "average_entry": 100.0},
        )
        bt = Backtester()
        bt.hermes = {**bt.hermes, "backtest_mode": "ta_only"}
        ta = bt.run("TEST/USDT", "4h", params, ohlcv_df=df)
        sim = {"last_rsi": 45.0, "rsi_sell_tiers_done": {}, "amount": 1.0}
        market = MarketContext(
            symbol="TEST/USDT",
            timeframe="4h",
            current_price=90.0,
            rsi=35.0,
            lower_bb=100.0,
            vol_multiplier=2.0,
            has_position=True,
            average_entry=100.0,
            open_positions=1,
            strategy_params=params,
            sim_state=sim,
        )
        TechnicalRSIStrategy().analyze(
            {"symbol": "TEST/USDT", "timeframe": "4h", "strategy_params": params},
            market,
        )
        held = DecisionEngine().evaluate_with_market(
            {"symbol": "TEST/USDT", "timeframe": "4h", "strategy_params": params},
            market,
        )
        return pipeline.bars_tested, ta.bars_tested, held.action

    out = _fresh(worker)
    assert "exc" not in out, out.get("exc")
    pipe_bars, ta_bars, _action = out["v"]
    assert pipe_bars > 0
    assert ta_bars > 0
    assert calls == []
    assert _book_sig() == before
    assert set(P.positions) == before_keys


def _gainer_client():
    from flask import Flask

    from services.gainer_signal.bot_http import register_gainer_signal_routes

    app = Flask(__name__)
    register_gainer_signal_routes(app)
    return app.test_client()


def _gainer_post(client):
    return client.post(
        "/internal/gainer-signal",
        json={
            "symbol": "AAA/USDT",
            "last": 1.0,
            "quote_vol": 2e6,
            "eligible": True,
            "rank": 1,
            "pct_24h": 15,
            "tenant_id": "henry",
        },
        headers={"X-Gainer-Signal-Token": "secret", "X-Tenant-Id": "henry"},
    )


def test_B2_unreadable_tenant_doc_returns_503_and_does_not_buy(ledger, monkeypatch):
    """Fresh thread, henry's lot is on the ledger, the tenant doc will not load.

    The scope-less retry (M-f) answers something other than tenant_context_error
    and can buy against an empty RAM book.
    """
    assert "ZEC_USDT_4h" in ledger.orders["henry"]
    monkeypatch.setenv("GAINER_SIGNAL_TOKEN", "secret")
    buys: list[str] = []
    process_calls: list[int] = []

    def _get_tenant(*_a, **_k):
        raise RuntimeError("tenant doc unreadable")

    monkeypatch.setattr("storage.tenant_registry.get_tenant", _get_tenant)

    real = __import__(
        "services.gainer_signal.bot_http", fromlist=["process_gainer_signal"]
    ).process_gainer_signal

    def _process(*a, **k):
        process_calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr("services.gainer_signal.bot_http.process_gainer_signal", _process)

    def _execute(self, *_a, **_k):
        buys.append("buy")
        raise AssertionError("buy attempt")

    monkeypatch.setattr("services.trading_service.TradingService.execute_order", _execute)
    client = _gainer_client()

    def worker():
        from core.tenant_context import current_tenant_context

        assert current_tenant_context() is None
        return _gainer_post(client)

    out = _fresh(worker)
    assert "exc" not in out, out.get("exc")
    resp = out["v"]
    body = resp.get_json() or {}
    assert resp.status_code == 503, body
    assert body.get("message") == "tenant_context_error"
    assert buys == []
    assert process_calls == []
    assert "ZEC_USDT_4h" in ledger.orders["henry"]


def test_B2_exception_after_fill_calls_process_once(monkeypatch):
    """A throw after the fill must not run process_gainer_signal a second time."""
    monkeypatch.setenv("GAINER_SIGNAL_TOKEN", "secret")
    calls: list[str] = []

    @contextmanager
    def _ok(tid, **_k):
        yield

    def _process(_data, config=None):
        calls.append("process")
        raise RuntimeError("after fill")

    monkeypatch.setattr("core.tenant_routing.tenant_cycle_context", _ok)
    monkeypatch.setattr("services.gainer_signal.bot_http.process_gainer_signal", _process)
    client = _gainer_client()

    def worker():
        return _gainer_post(client)

    out = _fresh(worker)
    assert "exc" not in out, out.get("exc")
    resp = out["v"]
    body = resp.get_json() or {}
    assert resp.status_code == 503, body
    assert body.get("message") == "tenant_context_error"
    assert calls == ["process"]


def test_M_i_entry_sensor_binds_only_default_and_keeps_unflushed_exit_marker(monkeypatch):
    """Polling a satellite, or reloading default, drops time_profit_exit_done (M-i)."""
    import core.tenant_routing as routing
    import services.entry_sensor_loop as sensor

    P.reset_all_position_stores_for_tests()
    lot = _lot(1.5945, 1534.20)
    lot["time_profit_exit_done"] = True
    P.positions[_ZEC_KEY] = lot
    monkeypatch.setattr("core.tenant_context.multi_tenant_enabled", lambda: True)
    monkeypatch.setattr(
        routing,
        "iter_price_cycle_tenants",
        lambda **_k: [DEFAULT_TENANT, "henry"],
    )
    seen: list[str] = []
    real_ctx = routing.tenant_cycle_context

    @contextmanager
    def _record(tid, **kwargs):
        seen.append(str(tid))
        with real_ctx(tid, **kwargs):
            yield

    def _load(scope=None, tenant_id=None, **_k):
        # The document still has the flag false. A default reload drops the RAM marker.
        tid = DEFAULT_TENANT if tenant_id is None else str(tenant_id)
        if tid == DEFAULT_TENANT:
            row = P.positions.get(_ZEC_KEY)
            if isinstance(row, dict):
                row["time_profit_exit_done"] = False
        return {}

    monkeypatch.setattr(routing, "tenant_cycle_context", _record)
    monkeypatch.setattr(P, "load_positions", _load)
    sensor._stop_event.clear()
    monkeypatch.setattr(sensor, "_poll_once", lambda _orch: None)

    def _wait(_sec=0):
        sensor._stop_event.set()

    monkeypatch.setattr(sensor._stop_event, "wait", _wait)

    def worker():
        sensor._loop_main(None)

    try:
        out = _fresh(worker)
    finally:
        sensor._stop_event.set()
    assert "exc" not in out, out.get("exc")
    assert seen == [DEFAULT_TENANT], seen
    assert P.positions[_ZEC_KEY]["time_profit_exit_done"] is True


def test_C2_unscoped_sniper_403_when_default_not_allowlisted(monkeypatch):
    from services.dca_sniper.bot_http import execute_sniper_dca

    monkeypatch.setattr(
        "services.dca_sniper.config.dca_sniper_tenants",
        lambda config=None: ["henry"],
    )
    P.reset_all_position_stores_for_tests()
    before_stores = set(P._position_stores)

    def worker():
        from core.tenant_context import current_tenant_context

        assert current_tenant_context() is None
        return execute_sniper_dca(
            {"symbol": _ZEC, "timeframe": _TF, "usdt": 10, "price": 100}
        )

    out = _fresh(worker)
    assert "exc" not in out, out.get("exc")
    body, status = out["v"]
    assert status == 403, body
    assert body.get("code") == "tenant_not_allowlisted"
    assert set(P._position_stores) == before_stores
    assert "henry" not in {k[0] for k in P._position_stores}


def test_C3_fund_sell_lock_check_error_is_503(monkeypatch):
    from services.dca_sniper.bot_http import execute_fund_sell

    P.reset_all_position_stores_for_tests()
    P.positions[_ZEC_KEY] = _lot(1.5945, 1534.20)
    sells: list[str] = []

    def _locked(*_a, **_k):
        raise RuntimeError("lock unreadable")

    def _execute(self, *_a, **_k):
        sells.append("sell")
        raise AssertionError("fund sell continued")

    monkeypatch.setattr("strategies.position_lock.auto_sell_blocked", _locked)
    monkeypatch.setattr("services.trading_service.TradingService.execute_order", _execute)

    def worker():
        return execute_fund_sell(
            {"symbol": _ZEC, "timeframe": _TF, "price": 1600}
        )

    out = _fresh(worker)
    assert "exc" not in out, out.get("exc")
    body, status = out["v"]
    assert status == 503, body
    assert body.get("code") == "position_lock_check_error"
    assert sells == []


def test_C3_promote_refuses_foreign_tenant(monkeypatch):
    from services.dca_sniper.bot_http import promote_position

    reads: list[str] = []

    def _get(*_a, **_k):
        reads.append("get_position")
        raise AssertionError("promote read a foreign book")

    monkeypatch.setattr("strategies.positions.get_position", _get)

    def worker():
        with tenant_context("henry", scope="paper"):
            return promote_position(
                {"symbol": _ZEC, "timeframe": _TF, "price": 1600}
            )

    out = _fresh(worker)
    assert "exc" not in out, out.get("exc")
    body, status = out["v"]
    assert status == 403, body
    assert body.get("code") == "tenant_not_allowlisted"
    assert reads == []


def test_C4_sniper_decorator_uses_wraps():
    from services.dca_sniper.bot_http import execute_sniper_dca

    assert execute_sniper_dca.__name__ == "execute_sniper_dca"
    assert execute_sniper_dca.__wrapped__.__name__ == "execute_sniper_dca"
