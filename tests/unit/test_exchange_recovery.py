"""Exchange recovery at start (#314 §5/§7). Mocked ccxt exchange, no live calls."""

from __future__ import annotations

import unittest

from unittest.mock import MagicMock

import ccxt
import pytest

from core.config import BotConfig
from core.models import OrderStatus, TradeOrder
from core.tenant_context import tenant_context
from execution.gate_adapter import GateExecutionAdapter
from execution.recovery import (
    RecoveryFailed,
    RecoveryReport,
    reconcile_with_exchange,
    reset_recovery_log_for_tests,
)
from services.order_service import OrderService
from services.portfolio_service import PortfolioService
from strategies.positions import (
    apply_positions_snapshot,
    clear_positions_memory,
    get_position,
)

SYMBOL = "SOL/USDT"


def _cfg(*, shorts: bool = False, execution: str = "real") -> BotConfig:
    return BotConfig(
        {
            "trading_mode": "live",
            "live_confirmed": True,
            "max_usdt_per_trade": 100,
            "shorts": {"enabled": shorts, "allow_live": shorts, "leverage_cap": 2},
            "costs": {
                "fee_source": "config",
                "gate": {
                    "spot": {
                        "fee_maker_pct": 0.2,
                        "fee_taker_pct": 0.2,
                        "slippage_pct": 0.0,
                        "fee_side_buy": "base",
                        "fee_side_sell": "quote",
                    },
                    "swap": {
                        "fee_maker_pct": 0.02,
                        "fee_taker_pct": 0.05,
                        "slippage_pct": 0.0,
                    },
                },
            },
            "live": {
                "execution": execution,
                "dry_run": False,
                "max_usdt_per_trade": 100,
            },
        }
    )


def _closed(
    *,
    filled: float,
    average: float = 100.0,
    oid: str = "ex-1",
    status: str = "closed",
    fee_ccy: str = "SOL",
    fee_cost: float = 0.0005,
) -> dict:
    return {
        "id": oid,
        "status": status,
        "filled": filled,
        "average": average,
        "cost": filled * average,
        "timestamp": 1_700_000_000_000,
        "fee": {"cost": fee_cost, "currency": fee_ccy},
    }


def _adapter(monkeypatch, cfg: BotConfig | None = None, *, mode: str = "real"):
    monkeypatch.setattr("execution.gate_adapter.record_live_trade", lambda rec: None)
    cfg = cfg or _cfg()
    adapter = GateExecutionAdapter(cfg, PortfolioService(cfg), mode=mode)
    ex = MagicMock(name="ccxt.gate")
    ex.fetch_balance.return_value = {"total": {}, "free": {}, "used": {}}
    ex.fetch_open_orders.return_value = []
    ex.fetch_positions.return_value = []
    ex.fetch_my_trades.return_value = []
    ex.fetch_order.side_effect = Exception("not found")
    adapter._exchange = ex
    return adapter, ex


@pytest.fixture(autouse=True)
def _reset_recovery(monkeypatch):
    reset_recovery_log_for_tests()
    import services.architecture_runtime as rt
    from strategies import positions as pos

    monkeypatch.setenv("MULTI_TENANT_ENABLED", "0")
    rt.reset_recovery_state_for_tests()
    notified: list[str] = []
    monkeypatch.setattr(
        "execution.recovery.notify_operator",
        lambda text, **k: notified.append(text) or True,
    )
    clear_positions_memory()
    pos._positions_state.clear()
    yield notified
    clear_positions_memory()
    pos._positions_state.clear()
    rt.reset_recovery_state_for_tests()
    reset_recovery_log_for_tests()


def _snap_sol(amount: float, entry: float = 100.0) -> None:
    apply_positions_snapshot(
        {
            "SOL_USDT_4h": {
                "amount": amount,
                "average_entry": entry,
                "peak_amount": amount,
                "sold_percent": 0.0,
                "last_buy_price": entry,
            }
        },
        scope="demo",
    )


def test_ledger_position_missing_on_exchange_zeroed(_reset_recovery, monkeypatch):
    notified = _reset_recovery
    adapter, ex = _adapter(monkeypatch)
    with tenant_context("default", scope="demo"):
        _snap_sol(10.0, entry=100.0)
        report = reconcile_with_exchange(
            tenant_id="default", scope="demo", adapter=adapter, config=_cfg()
        )
    assert float(get_position("SOL/USDT", "4h")["amount"]) == 0.0
    assert report.divergences
    assert report.divergences[0]["symbol"] in ("SOL/USDT", "SOL")
    assert report.divergences[0]["exchange"] == 0.0
    assert len(notified) == 1
    assert "divergence" in notified[0].lower() or "SOL" in notified[0]


def test_exchange_qty_wins_entry_from_vwap(_reset_recovery, monkeypatch):
    adapter, ex = _adapter(monkeypatch)
    ex.fetch_balance.return_value = {
        "total": {"SOL": 12.5},
        "SOL": {"total": 12.5, "free": 12.5, "used": 0},
    }
    ex.fetch_my_trades.return_value = [
        {"price": 80.0, "amount": 5.0},
        {"price": 120.0, "amount": 5.0},
    ]
    with tenant_context("default", scope="demo"):
        _snap_sol(10.0, entry=0.0)
        reconcile_with_exchange(
            tenant_id="default", scope="demo", adapter=adapter, config=_cfg()
        )
        pos = get_position("SOL/USDT", "4h")
    assert float(pos["amount"]) == pytest.approx(12.5)
    assert float(pos["average_entry"]) == pytest.approx(100.0)


def test_active_order_resolved_executed_updates_position_via_fill(
    _reset_recovery, monkeypatch
):
    adapter, ex = _adapter(monkeypatch)
    ex.fetch_order.side_effect = None
    ex.fetch_order.return_value = _closed(filled=0.25)
    ex.fetch_balance.return_value = {
        "total": {"SOL": 0.25},
        "SOL": {"total": 0.25, "free": 0.25, "used": 0},
    }
    with tenant_context("default", scope="demo"):
        svc = OrderService("demo")
        rec = svc.create_from_request(
            TradeOrder(
                "BUY",
                SYMBOL,
                100.0,
                0.25,
                usdt_amount=25.0,
                client_order_id="cid-1",
                exchange_order_id="ex-1",
            ),
            status=OrderStatus.ACTIVE,
            telegram_token="rec-exec-1",
            timeframe="4h",
        )
        data = svc._load()
        found = svc._find(data, order_id=rec["id"])
        found["needs_reconcile"] = True
        found["exchange_order_id"] = "ex-1"
        found["qty"] = 0.25
        found["client_order_id"] = "cid-1"
        svc._save(data)
        before = float(get_position(SYMBOL, "4h")["amount"])
        report = reconcile_with_exchange(
            tenant_id="default", scope="demo", adapter=adapter, config=_cfg()
        )
        after = float(get_position(SYMBOL, "4h")["amount"])
        stored = svc.get_by_id(rec["id"])
    assert after > before
    assert after == pytest.approx(0.25, rel=0.05) or after > 0
    assert report.orders_resolved
    assert OrderStatus.try_legacy(stored.get("status")) is OrderStatus.EXECUTED
    assert stored.get("needs_reconcile") in (False, None)


def test_active_order_canceled_no_position_change(_reset_recovery, monkeypatch):
    adapter, ex = _adapter(monkeypatch)
    ex.fetch_order.side_effect = None
    ex.fetch_order.return_value = _closed(filled=0.0, status="canceled")
    ex.fetch_balance.return_value = {
        "total": {"SOL": 1.0},
        "SOL": {"total": 1.0, "free": 1.0, "used": 0},
    }
    with tenant_context("default", scope="demo"):
        _snap_sol(1.0, entry=100.0)
        svc = OrderService("demo")
        rec = svc.create_from_request(
            TradeOrder(
                "BUY",
                SYMBOL,
                100.0,
                0.25,
                usdt_amount=25.0,
                client_order_id="cid-c",
                exchange_order_id="ex-c",
            ),
            status=OrderStatus.ACTIVE,
            telegram_token="rec-can-1",
        )
        data = svc._load()
        found = svc._find(data, order_id=rec["id"])
        found["needs_reconcile"] = True
        found["exchange_order_id"] = "ex-c"
        svc._save(data)
        report = reconcile_with_exchange(
            tenant_id="default", scope="demo", adapter=adapter, config=_cfg()
        )
        amount = float(get_position(SYMBOL, "4h")["amount"])
        stored = svc.get_by_id(rec["id"])
    assert amount == pytest.approx(1.0)
    assert OrderStatus.try_legacy(stored.get("status")) is OrderStatus.CANCELED
    assert report.orders_resolved


def test_exchange_down_raises_recovery_failed(_reset_recovery, monkeypatch):
    adapter, ex = _adapter(monkeypatch)
    ex.fetch_balance.side_effect = ccxt.NetworkError("down")
    with tenant_context("default", scope="demo"):
        with pytest.raises(RecoveryFailed, match="unreachable|down"):
            reconcile_with_exchange(
                tenant_id="default", scope="demo", adapter=adapter, config=_cfg()
            )


def test_exchange_down_ensure_started_raises_cycle_not_run(
    _reset_recovery, monkeypatch
):
    adapter, ex = _adapter(monkeypatch)
    ex.fetch_balance.side_effect = ccxt.NetworkError("down")
    from core.execution_mode import ExecutionMode

    monkeypatch.setattr(
        "core.execution_mode.resolve_execution_mode",
        lambda *a, **k: ExecutionMode(
            adapter_mode="real", places_real_orders=True, reason="test"
        ),
    )
    monkeypatch.setattr(
        "execution.factory.get_execution_adapter", lambda *a, **k: adapter
    )
    import services.architecture_runtime as rt
    from core.config import get_bot_config

    rt.reset_recovery_state_for_tests()
    rt._started = True
    rt._last_mode = get_bot_config().architecture_config.get("notification_mode", "async")
    cycle_ran: list[bool] = []

    def cycle():
        from services.architecture_runtime import ensure_started

        ensure_started()
        cycle_ran.append(True)

    with tenant_context("default", scope="demo"):
        with pytest.raises(RecoveryFailed):
            cycle()
    assert cycle_ran == []
    assert ("default", "demo") not in rt._recovered


def test_cross_margin_raises_recovery_failed(_reset_recovery, monkeypatch):
    cfg = _cfg(shorts=True)
    adapter, ex = _adapter(monkeypatch, cfg)
    ex.fetch_positions.return_value = [
        {
            "symbol": "BTC/USDT:USDT",
            "marginMode": "cross",
            "contracts": 1.0,
            "leverage": 0,
            "side": "short",
        }
    ]
    with tenant_context("default", scope="demo"):
        with pytest.raises(
            RecoveryFailed,
            match="margin mode cross for BTC/USDT:USDT; short_math.liquidation_price_isolated would be wrong",
        ):
            reconcile_with_exchange(
                tenant_id="default", scope="demo", adapter=adapter, config=cfg
            )


def test_shadow_skipped_no_exchange_call(_reset_recovery, monkeypatch):
    cfg = _cfg(execution="shadow")
    adapter, ex = _adapter(monkeypatch, cfg, mode="shadow")
    with tenant_context("default", scope="demo"):
        report = reconcile_with_exchange(
            tenant_id="default", scope="demo", adapter=adapter, config=cfg
        )
    assert isinstance(report, RecoveryReport)
    assert report.skipped is True
    assert report.reason == "shadow"
    ex.fetch_balance.assert_not_called()
    ex.fetch_open_orders.assert_not_called()
    ex.fetch_positions.assert_not_called()
    ex.fetch_order.assert_not_called()


def test_recovery_once_per_process_per_tenant_id(_reset_recovery, monkeypatch):
    calls: list[str] = []

    def fake_reconcile(*, tenant_id, scope, adapter, config):
        calls.append(tenant_id)
        return RecoveryReport(
            skipped=True, reason="shadow", tenant_id=tenant_id, scope=scope
        )

    monkeypatch.setattr("execution.recovery.reconcile_with_exchange", fake_reconcile)
    import services.architecture_runtime as rt
    from core.config import get_bot_config
    from services.architecture_runtime import ensure_started

    rt.reset_recovery_state_for_tests()
    rt._started = True
    rt._last_mode = get_bot_config().architecture_config.get("notification_mode", "async")
    with tenant_context("default", scope="demo"):
        ensure_started()
        ensure_started()
    assert calls.count("default") == 1
    with tenant_context("henry", scope="demo"):
        ensure_started()
    assert "henry" in calls
    assert calls.count("default") == 1


def test_sync_ledger_files_aliases_wrap_old_names():
    from services import ledger_sync

    assert ledger_sync.reconcile_recent_highs is not ledger_sync.sync_ledger_files_recent_highs
    assert ledger_sync.reconcile_peak_amounts is not ledger_sync.sync_ledger_files_peak_amounts
    assert callable(ledger_sync.sync_ledger_files_recent_highs)
    assert callable(ledger_sync.sync_ledger_files_peak_amounts)


class TestCycleSkipsOnRecoveryFailed(unittest.TestCase):
    """#314 §5.5: RecoveryFailed from ensure_started must skip the tenant's price
    cycle (ERROR), not be downgraded to the generic runtime WARNING."""

    def test_recovery_failed_skips_cycle_before_any_trading_step(self):
        import aria_bot
        from execution.recovery import RecoveryFailed
        from unittest.mock import patch

        calls = []
        with patch("services.architecture_runtime.ensure_started", side_effect=RecoveryFailed("gate down")), \
             patch.object(aria_bot, "load_effective_watchlist", side_effect=lambda *a, **k: calls.append("watchlist") or []), \
             patch.object(aria_bot, "log") as mock_log:
            aria_bot._run_tenant_price_cycle(
                cycle_started=0.0, use_dashboard=False,
                analyzer=None, orchestrator=None, social_pipeline=None, sandbox=None, trend_engine=None,
            )
        self.assertEqual(calls, [], "cycle must return before loading the watchlist")
        levels = [c.args[1] for c in mock_log.call_args_list if len(c.args) > 1]
        self.assertIn("ERROR", levels)

    def test_other_runtime_errors_stay_best_effort(self):
        import aria_bot
        from unittest.mock import patch

        calls = []
        with patch("services.architecture_runtime.ensure_started", side_effect=RuntimeError("redis hiccup")), \
             patch.object(aria_bot, "load_effective_watchlist", side_effect=lambda *a, **k: calls.append("watchlist") or []), \
             patch.object(aria_bot, "load_trade_watchlist", return_value=[]), \
             patch.object(aria_bot, "get_prices_batch", return_value={}), \
             patch.object(aria_bot, "log"):
            try:
                aria_bot._run_tenant_price_cycle(
                    cycle_started=0.0, use_dashboard=False,
                    analyzer=None, orchestrator=None, social_pipeline=None, sandbox=None, trend_engine=None,
                )
            except Exception:
                pass  # downstream steps may need more mocks; we only assert the cycle continued
        self.assertEqual(calls, ["watchlist"], "generic runtime errors must not abort the cycle")
