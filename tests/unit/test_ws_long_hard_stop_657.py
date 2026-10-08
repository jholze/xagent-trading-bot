"""#657 — long hard stop on the existing WS exit path (paper only).

Synthetic symbols only. Thresholds and fractions come from the params the
test passes in, or from the loaded config, never from a coin name.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.models import SignalAnalysis, TradeOrder
from services.exit_realtime.execute import exit_guard_key, recently_exited
from services.exit_realtime.hub import ExitRealtimeHub, tick_age_label
from services.exit_realtime.shadow_eval import evaluate_would_sells
from strategies.dca import evaluate_long_hard_stop


def _params(*, full=10.0, partial=5.0, **extra):
    out = {
        "stop_loss_pct": full,
        "partial_stop_pct": partial,
        "dca": {"pause_partial_stop_during_dca": True, "grace_hours_after_dca": 12},
    }
    out.update(extra)
    return out


def _lot(amount=100.0, entry=1.0, **extra):
    from strategies.positions import _deserialize_position

    raw = {
        "amount": amount,
        "average_entry": entry,
        "recent_high": extra.pop("recent_high", entry),
        "side": "long",
        "peak_amount": amount,
        "sold_percent": 0.0,
    }
    raw.update(extra)
    lot = _deserialize_position(raw)
    for key in ("recovery_hold", "sniper_focus"):
        if key in raw:
            lot[key] = raw[key]
    return lot


def _seed(symbol, timeframe="1h", amount=100.0, entry=1.0, tenant_id=None, **extra):
    from strategies import positions as pm

    lot = _lot(amount, entry, **extra)
    key = pm.get_key(symbol, timeframe)
    if tenant_id and tenant_id != "default":
        from core.tenant_context import resolve_tenant_scope

        store_key = (tenant_id, resolve_tenant_scope())
        store = pm._position_stores.setdefault(store_key, {})
        store[key] = lot
    else:
        pm.positions[key] = lot
    return lot


def _held(symbol, timeframe="1h"):
    from strategies.positions import get_position

    return float(get_position(symbol, timeframe).get("amount") or 0)


def _hub(sources=("stop_loss",), cooldown=0.01, **extra):
    raw = {
        "exit_realtime": {
            "enabled": True,
            "mode": "live",
            "sources": list(sources),
            "live_cooldown_sec": cooldown,
            "default_atr_pct": 3.0,
        }
    }
    raw.update(extra)
    return ExitRealtimeHub(raw)


def _row(symbol, timeframe="1h", position=None, params=None, tenant_id=None, entry=1.0):
    pos = position if position is not None else _lot(entry=entry)
    row = {
        "symbol": symbol,
        "timeframe": timeframe,
        "position": dict(pos),
        "average_entry": float(pos.get("average_entry") or entry),
        "recent_high": float(pos.get("recent_high") or entry),
        "strategy_params": params if params is not None else _params(),
        "atr_pct": 3.0,
    }
    if tenant_id:
        row["tenant_id"] = tenant_id
    return row


def _frame(pair, last, bid, time_ms=None):
    result = {
        "currency_pair": pair,
        "last": last,
        "highest_bid": bid,
        "change_percentage": "-12.5",
        "quote_volume": "1000",
    }
    if time_ms is not None:
        result["time_ms"] = time_ms
    return json.dumps(
        {"time": 1, "channel": "spot.tickers", "event": "update", "result": result}
    )


def _analysis(symbol, action, timeframe="1h", sell_source="stop_loss"):
    normalized = "SELL_FULL" if "FULL" in action else "SELL_PARTIAL_50"
    return SignalAnalysis(
        action=action,
        symbol=symbol,
        timeframe=timeframe,
        rsi=40.0,
        lower_bb=0.2,
        vol_multiplier=1.0,
        ampel_emoji="",
        ampel_text="",
        sources=["technical", "stop_loss"],
        normalized_action=normalized,
        rationale="hard stop",
        confidence=80.0,
        sell_source=sell_source,
    )


def _orders(symbol=None):
    from data_manager import load_orders, resolve_ledger_scope
    from services import order_service

    order_service._ORDERS_READ_CACHE.clear()
    doc = load_orders(resolve_ledger_scope()) or {}
    rows = list(doc.get("orders") or [])
    if symbol:
        rows = [o for o in rows if o.get("symbol") == symbol]
    return rows


def _seed_filled_sells(n, symbol_prefix="ZZ"):
    from data_manager import load_orders, resolve_ledger_scope, save_orders
    from services import order_service

    scope = resolve_ledger_scope()
    doc = load_orders(scope) or {"ledger_scope": scope, "orders": []}
    now = datetime.now().isoformat()
    for i in range(n):
        doc.setdefault("orders", []).append(
            {
                "id": f"seed-{symbol_prefix}-{i}-{uuid.uuid4().hex[:6]}",
                "status": "filled",
                "side": "sell",
                "type": "SELL",
                "symbol": f"{symbol_prefix}{i}/USDT",
                "timeframe": "1h",
                "signal": "SELL_FULL",
                "source": "auto",
                "exit_source": "trailing_stop",
                "timestamps": {"filled": now, "created": now},
            }
        )
    save_orders(doc, scope)
    order_service._ORDERS_READ_CACHE.clear()


@contextmanager
def _tenant_config_is_normalized():
    """A non-default context must not load Mongo tenant config in this test."""
    from core.config import get_bot_config

    raw = get_bot_config().raw
    with patch("data_manager.get_config", lambda *a, **k: raw), patch(
        "data_manager.load_config", lambda *a, **k: raw
    ):
        yield


@contextmanager
def _paper():
    """Paper TradingService path: no Gate markets, no config reload, no auto-short."""
    import data_manager
    from execution.gate_adapter import GateExecutionAdapter
    from services.trading_service import TradingService

    # Same cache get_bot_config() copies. A copy here would not reach the service.
    raw = data_manager.get_config()
    shorts = raw.setdefault("shorts", {})
    prev_auto = shorts.get("auto_after_sell", None)
    shorts["auto_after_sell"] = False
    prev_failed = GateExecutionAdapter._shadow_markets_failed
    GateExecutionAdapter._shadow_markets_failed = True

    def _refresh(self):
        return self

    with patch.object(TradingService, "refresh", _refresh), patch(
        "notifications.telegram_commands.position_display.send_positions_snapshot",
        lambda *a, **k: None,
    ), patch("core.operator_notify.notify_operator", lambda *a, **k: None):
        try:
            yield
        finally:
            GateExecutionAdapter._shadow_markets_failed = prev_failed
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


def _sell(symbol, amount, signal, *, timeframe="1h", source="auto", exit_source="stop_loss", price=0.5):
    from services.trading_service import TradingService

    order = TradeOrder(
        type="SELL",
        symbol=symbol,
        price=price,
        amount=amount,
        signal=signal,
        source=source,
        exit_source=exit_source,
    )
    key = uuid.uuid4().hex
    return TradingService().execute_order(
        order,
        timeframe,
        source=source,
        idempotency_key=key,
    )


def _fire(hub, symbol, last, bid, *, pair=None, time_ms=None):
    hub.handle_ws_message(
        _frame(pair or symbol.replace("/", "_"), last, bid, time_ms=time_ms)
    )


@pytest.fixture(autouse=True)
def _reset_books():
    from strategies.positions import reset_all_position_stores_for_tests

    reset_all_position_stores_for_tests()
    _clear_guards()
    yield
    _clear_guards()
    reset_all_position_stores_for_tests()


class TestT1SameRule:
    def test_ws_and_cycle_store_the_same_hard_stop(self):
        params = _params(full=10, partial=5)
        _seed("AAA/USDT", amount=100, entry=1.0)
        _seed("BBB/USDT", amount=80, entry=1.0)
        hub = _hub()
        hub.update_book([_row("AAA/USDT", params=params)])
        with _paper(), _logs():
            _fire(hub, "AAA/USDT", "0.50", "0.50")
            from services.signal_orchestrator import SignalOrchestrator

            orch = SignalOrchestrator(notify_callback=lambda *a, **k: None)
            with patch(
                "strategies.registry.resolve_strategy_params", return_value=params
            ):
                cycle = orch.execute_if_needed(
                    _analysis("BBB/USDT", "SELL_STOP_FULL"),
                    {"symbol": "BBB/USDT", "timeframe": "1h"},
                    0.50,
                )
        assert cycle is not None and cycle.executed
        ws_rows = [o for o in _orders("AAA/USDT") if o.get("status") == "filled"]
        cy_rows = [o for o in _orders("BBB/USDT") if o.get("status") == "filled"]
        assert ws_rows and cy_rows
        assert ws_rows[-1]["signal"] == "SELL_STOP_FULL"
        assert cy_rows[-1]["signal"] == "SELL_STOP_FULL"
        assert ws_rows[-1]["exit_source"] == "stop_loss"
        assert cy_rows[-1]["exit_source"] == "stop_loss"
        assert ws_rows[-1]["source"] == "exit_ws"
        assert cy_rows[-1]["source"] == "auto"
        assert _held("AAA/USDT") == 0

    def test_partial_fraction_matches_cycle_and_ladder(self):
        params = _params(full=50, partial=10)
        ladder = {
            "enabled": True,
            "tiers": [0.35, 0.35, 0.30],
            "min_remainder_pct": 0.01,
            "min_remainder_usdt_floor": 1,
        }
        params["exit_ladder"] = ladder
        _seed("AAA/USDT", amount=100, entry=2.0)
        _seed("BBB/USDT", amount=100, entry=2.0)
        # 20% loss: past partial 10, short of full 50.
        hub = _hub()
        hub.update_book([_row("AAA/USDT", params=params, entry=2.0)])
        with _paper():
            _fire(hub, "AAA/USDT", "1.60", "1.60")
            from services.signal_orchestrator import SignalOrchestrator

            orch = SignalOrchestrator(notify_callback=lambda *a, **k: None)
            with patch(
                "strategies.registry.resolve_strategy_params", return_value=params
            ):
                cycle = orch.execute_if_needed(
                    _analysis("BBB/USDT", "SELL_STOP_PARTIAL", sell_source="partial_stop"),
                    {"symbol": "BBB/USDT", "timeframe": "1h"},
                    1.60,
                )
        assert cycle is not None and cycle.executed
        # Ladder first tier is 0.35 of the lot. Both paths use that fraction.
        assert _held("AAA/USDT") == pytest.approx(65.0, abs=0.01)
        assert _held("BBB/USDT") == pytest.approx(65.0, abs=0.01)
        ws = [o for o in _orders("AAA/USDT") if o.get("status") == "filled"][-1]
        cy = [o for o in _orders("BBB/USDT") if o.get("status") == "filled"][-1]
        assert ws["signal"] == cy["signal"] == "SELL_STOP_PARTIAL"
        assert ws["exit_source"] == cy["exit_source"] == "stop_loss"

    def test_equal_loss_does_not_fire(self):
        # loss_pct is strict '>'. entry 1, full 10 → price 0.90 is exactly 10.
        # Strict '>'. 3/4 is exact in binary: loss 25. Exactly 25 does not
        # clear a 25 stop. Exactly 25 against a 40 full stop is not a full.
        params = _params(full=40, partial=25)
        assert (
            evaluate_long_hard_stop(
                price=3.0,
                entry=4.0,
                position=_lot(entry=4.0),
                strategy_params=params,
                base_stop_loss_pct=40,
            )
            is None
        )
        exact_full = evaluate_long_hard_stop(
            price=3.0,
            entry=4.0,
            position=_lot(entry=4.0),
            strategy_params=_params(full=25, partial=10),
            base_stop_loss_pct=25,
        )
        assert exact_full == ("SELL_STOP_PARTIAL", "stop_loss")
        _seed("AAA/USDT", amount=100, entry=4.0)
        hub = _hub()
        hub.update_book([_row("AAA/USDT", params=params, entry=4.0)])
        with _paper():
            _fire(hub, "AAA/USDT", "3.0", "3.0")
        assert _orders("AAA/USDT") == []
        assert _held("AAA/USDT") == 100


class TestT2CapUnderLock:
    def test_second_full_stop_is_lot_closed(self):
        _seed("AAA/USDT", amount=40, entry=1.0)
        with _paper():
            first = _sell("AAA/USDT", 40, "SELL_STOP_FULL")
            second = _sell("AAA/USDT", 40, "SELL_STOP_FULL")
        assert first.executed
        assert not second.executed
        assert second.code == "sell_lot_closed"
        rejected = [o for o in _orders("AAA/USDT") if o.get("status") == "rejected"]
        assert rejected
        assert rejected[-1].get("signal") == "SELL_STOP_FULL"

    def test_stale_full_after_partial_caps_to_remainder(self):
        _seed("AAA/USDT", amount=100, entry=1.0)
        with _paper(), _logs() as lines:
            partial = _sell("AAA/USDT", 50, "SELL_STOP_PARTIAL", price=1.0)
            stale = _sell("AAA/USDT", 100, "SELL_STOP_FULL", price=1.0)
        assert partial.executed and stale.executed
        assert _held("AAA/USDT") == 0
        assert any("sell_amount_capped" in line and "old_amount=100" in line for line in lines)

    def test_cap_uses_the_order_timeframe_only(self):
        _seed("AAA/USDT", "1h", amount=10, entry=1.0)
        _seed("AAA/USDT", "4h", amount=25, entry=1.0)
        with _paper(), _logs() as lines:
            result = _sell("AAA/USDT", 100, "SELL_STOP_FULL", timeframe="1h", price=1.0)
        assert result.executed
        assert _held("AAA/USDT", "1h") == 0
        assert _held("AAA/USDT", "4h") == 25
        assert any("sell_amount_capped" in line and "new_amount=10" in line for line in lines)


class TestT3GraceAndPause:
    def test_grace_suppresses_then_full_fires_with_partial_paused(self):
        params = _params(full=10, partial=5)
        recent = datetime.now().isoformat()
        old = (datetime.now() - timedelta(hours=48)).isoformat()
        graceful = _lot(amount=100, entry=1.0, dca_rounds=1, last_dca_at=recent)
        assert (
            evaluate_long_hard_stop(
                price=0.50,
                entry=1.0,
                position=graceful,
                strategy_params=params,
                base_stop_loss_pct=10,
            )
            is None
        )
        aged = _lot(amount=100, entry=1.0, dca_rounds=1, last_dca_at=old)
        hit = evaluate_long_hard_stop(
            price=0.50,
            entry=1.0,
            position=aged,
            strategy_params=params,
            base_stop_loss_pct=10,
        )
        assert hit == ("SELL_STOP_FULL", "stop_loss")
        # Partial band, pause still on: no partial after grace.
        paused = evaluate_long_hard_stop(
            price=0.92,
            entry=1.0,
            position=aged,
            strategy_params=params,
            base_stop_loss_pct=10,
        )
        assert paused is None
        _seed(
            "AAA/USDT",
            amount=100,
            entry=1.0,
            dca_rounds=1,
            last_dca_at=recent,
        )
        hub = _hub()
        hub.update_book([_row("AAA/USDT", params=params)])
        with _paper():
            _fire(hub, "AAA/USDT", "0.50", "0.50")
        assert _orders("AAA/USDT") == []


class TestT4ThresholdsFromConfig:
    def test_trigger_follows_config_and_moves_when_params_change(self):
        from core.config import get_bot_config

        cfg = get_bot_config()
        volatile = cfg.raw.get("volatile_altcoin") or {}
        partial = float(volatile["partial_stop_pct"])
        base = float(cfg.stop_loss_pct)
        other = base * 0.67
        vol_params = {"stop_loss_pct": base, "partial_stop_pct": partial, "dca": {}}
        other_params = {"stop_loss_pct": base, "partial_stop_ratio": 0.67, "dca": {}}
        # Just inside the volatile partial does not fire; just through it does.
        inside = 1.0 * (1.0 - (partial - 0.1) / 100.0)
        through = 1.0 * (1.0 - (partial + 0.1) / 100.0)
        assert (
            evaluate_long_hard_stop(
                price=inside,
                entry=1.0,
                position=_lot(),
                strategy_params=vol_params,
                base_stop_loss_pct=base,
            )
            is None
        )
        assert evaluate_long_hard_stop(
            price=through,
            entry=1.0,
            position=_lot(),
            strategy_params=vol_params,
            base_stop_loss_pct=base,
        )[0] == "SELL_STOP_PARTIAL"
        # A config-shaped change of the partial moves that same price.
        moved = dict(vol_params)
        moved["partial_stop_pct"] = partial + 5
        assert (
            evaluate_long_hard_stop(
                price=through,
                entry=1.0,
                position=_lot(),
                strategy_params=moved,
                base_stop_loss_pct=base,
            )
            is None
        )
        widened = {
            "stop_loss_pct": base,
            "partial_stop_pct": partial,
            "dca": {"stop_loss_widen_pct_per_round": 5, "pause_partial_stop_during_dca": False},
        }
        plain = evaluate_long_hard_stop(
            price=1.0 * (1.0 - (base + 0.1) / 100.0),
            entry=1.0,
            position=_lot(),
            strategy_params=widened,
            base_stop_loss_pct=base,
        )
        assert plain[0] == "SELL_STOP_FULL"
        after_round = evaluate_long_hard_stop(
            price=1.0 * (1.0 - (base + 0.1) / 100.0),
            entry=1.0,
            position=_lot(dca_rounds=2, last_dca_at=(datetime.now() - timedelta(days=3)).isoformat()),
            strategy_params=widened,
            base_stop_loss_pct=base,
        )
        assert after_round is None or after_round[0] != "SELL_STOP_FULL"
        # The non-volatile ratio path is the config fallback, not a literal in code.
        assert evaluate_long_hard_stop(
            price=1.0 * (1.0 - (other + 0.1) / 100.0),
            entry=1.0,
            position=_lot(),
            strategy_params=other_params,
            base_stop_loss_pct=base,
        )[0] == "SELL_STOP_PARTIAL"


class TestT5ShortUnchanged:
    def test_long_stop_price_does_not_sell_a_short(self):
        pos = _lot(amount=10, entry=1.0, side="short", leverage=2.0)
        _seed("AAA/USDT", amount=10, entry=1.0, side="short", leverage=2.0)
        hub = _hub()
        hub.update_book([_row("AAA/USDT", position=pos, params=_params())])
        with _paper(), patch(
            "services.exit_realtime.hub.try_execute_trail_exit",
            return_value={"ok": True, "executed": True, "message": "ok"},
        ) as mock_ex:
            _fire(hub, "AAA/USDT", "0.40", "0.40")
        for call in mock_ex.call_args_list:
            action = (call.kwargs or {}).get("action") or ""
            assert "SELL" not in action

    def test_climax_ws_tick_still_covers(self):
        pos = _lot(
            amount=10,
            entry=1.0,
            side="short",
            short_recipe="climax_fade",
            leverage=2.0,
            entry_at="2026-09-29T12:00:00+00:00",
        )
        _seed(
            "AAA/USDT",
            amount=10,
            entry=1.0,
            side="short",
            short_recipe="climax_fade",
            leverage=2.0,
            entry_at="2026-09-29T12:00:00+00:00",
        )
        hub = _hub(
            sources=(),
            shorts={
                "enabled": True,
                "climax_fade": {
                    "enabled": True,
                    "stop_price_pct": 10.0,
                    "cover_close_pct": 3.0,
                    "time_cap_hours": 16,
                },
            },
        )
        hub.update_book([_row("AAA/USDT", position=pos, params={})])
        with patch(
            "services.exit_realtime.hub.try_execute_trail_exit",
            return_value={"ok": True, "executed": True, "message": "ok"},
        ) as mock_ex:
            hub.on_ticker("AAA_USDT", 1.10, highest_bid="1.10")
        mock_ex.assert_called()
        assert mock_ex.call_args.kwargs.get("action") == "COVER"
        assert mock_ex.call_args.kwargs.get("exit_source") == "climax_stop"


class TestT6WsOffCycleStillSells:
    def test_disabled_hub_does_not_start_and_cycle_still_stops(self):
        from services.exit_realtime.hub import ensure_started

        assert ensure_started({"exit_realtime": {"enabled": False, "mode": "live"}}) is None
        _seed("AAA/USDT", amount=20, entry=1.0)
        with _paper():
            from services.signal_orchestrator import SignalOrchestrator

            orch = SignalOrchestrator(notify_callback=lambda *a, **k: None)
            result = orch.execute_if_needed(
                _analysis("AAA/USDT", "SELL_STOP_FULL"),
                {"symbol": "AAA/USDT", "timeframe": "1h"},
                0.40,
            )
        assert result is not None and result.executed
        filled = [o for o in _orders("AAA/USDT") if o.get("status") == "filled"]
        assert filled[-1]["signal"] == "SELL_STOP_FULL"
        assert filled[-1]["exit_source"] == "stop_loss"


class TestT7PriceAndHold:
    def test_zero_price_does_not_ws_stop_and_shared_fn_keeps_cycle_rule(self):
        # R6: the shared function does not special-case price 0 (that would
        # change the cycle). The WS ticker path already returns on price <= 0.
        assert (
            evaluate_long_hard_stop(
                price=0,
                entry=1.0,
                position=_lot(),
                strategy_params=_params(),
                base_stop_loss_pct=10,
            )
            is not None
        )
        assert evaluate_would_sells(
            symbol="AAA/USDT",
            timeframe="1h",
            price=0,
            position={"amount": 10, "average_entry": 1.0},
            strategy_params=_params(),
            sources=frozenset({"stop_loss"}),
            bid=0,
        ) == []
        _seed("AAA/USDT", amount=10, entry=1.0)
        hub = _hub()
        hub.update_book([_row("AAA/USDT")])
        with _paper():
            hub.on_ticker("AAA_USDT", 0, highest_bid="0.4")
        assert _orders("AAA/USDT") == []

    def test_recovery_hold_allows_full_and_blocks_partial(self):
        params = _params(full=50, partial=10)
        _seed("AAA/USDT", amount=40, entry=1.0, recovery_hold=True, sniper_focus=True)
        _seed("BBB/USDT", amount=40, entry=1.0, recovery_hold=True, sniper_focus=True)
        hub = _hub()
        hub.update_book(
            [
                _row("AAA/USDT", params=params),
                _row(
                    "BBB/USDT",
                    params=params,
                    position=_lot(amount=40, entry=1.0, recovery_hold=True),
                ),
            ]
        )
        with _paper():
            _fire(hub, "BBB/USDT", "0.85", "0.85")
            _fire(hub, "AAA/USDT", "0.40", "0.40")
        assert _orders("BBB/USDT") == []
        assert _held("BBB/USDT") == 40
        filled = [o for o in _orders("AAA/USDT") if o.get("status") == "filled"]
        assert filled
        assert filled[-1]["signal"] == "SELL_STOP_FULL"
        assert filled[-1]["exit_source"] == "stop_loss"

    def test_position_lock_blocks_the_ws_stop(self):
        from strategies.position_lock import build_lock

        _seed(
            "AAA/USDT",
            amount=40,
            entry=1.0,
            lock=build_lock(reason="hold", locked_by="test"),
        )
        hub = _hub()
        hub.update_book([_row("AAA/USDT")])
        with _paper():
            _fire(hub, "AAA/USDT", "0.40", "0.40")
        assert _orders("AAA/USDT") == []
        assert _held("AAA/USDT") == 40


class TestT8BidConfirm:
    def test_last_through_full_bid_short_of_partial_does_not_sell(self):
        # full 10, partial 5. last 0.80 is 20% (full). bid 0.96 is 4% (neither).
        _seed("AAA/USDT", amount=30, entry=1.0)
        hub = _hub()
        hub.update_book([_row("AAA/USDT", params=_params(full=10, partial=5))])
        payload = {
            "currency_pair": "AAA_USDT",
            "last": "0.80",
            "highest_bid": "0.96",
            "change_percentage": "-20",
            "quote_volume": "10",
        }
        assert "highest_size" not in payload and "lowest_size" not in payload
        with _paper():
            hub.handle_ws_message(
                json.dumps({"channel": "spot.tickers", "event": "update", "result": payload})
            )
        assert _orders("AAA/USDT") == []

    def test_both_through_full_sells_and_partial_band_sells_partial(self):
        _seed("AAA/USDT", amount=30, entry=1.0)
        _seed("BBB/USDT", amount=30, entry=1.0)
        hub = _hub()
        params = _params(full=10, partial=5)
        hub.update_book(
            [
                _row("AAA/USDT", params=params),
                _row("BBB/USDT", params=params),
            ]
        )
        with _paper():
            _fire(hub, "AAA/USDT", "0.80", "0.80")
            _fire(hub, "BBB/USDT", "0.92", "0.92")
        aaa = [o for o in _orders("AAA/USDT") if o.get("status") == "filled"][-1]
        bbb = [o for o in _orders("BBB/USDT") if o.get("status") == "filled"][-1]
        assert aaa["signal"] == "SELL_STOP_FULL"
        assert bbb["signal"] == "SELL_STOP_PARTIAL"
        assert _held("BBB/USDT") == pytest.approx(15.0, abs=0.01)


class TestT9MissingBid:
    @pytest.mark.parametrize("bid", [None, "", "0", 0, -1, "nope", "  "])
    def test_unusable_bid_skips_once_and_cycle_still_sells(self, bid):
        _seed("AAA/USDT", amount=20, entry=1.0)
        hub = _hub(cooldown=60)
        hub.update_book([_row("AAA/USDT", params=_params(full=10, partial=5))])
        with _paper(), _logs() as lines:
            hub.on_ticker("AAA_USDT", 0.50, highest_bid=bid)
            hub.on_ticker("AAA_USDT", 0.50, highest_bid=bid)
            from services.signal_orchestrator import SignalOrchestrator

            orch = SignalOrchestrator(notify_callback=lambda *a, **k: None)
            result = orch.execute_if_needed(
                _analysis("AAA/USDT", "SELL_STOP_FULL"),
                {"symbol": "AAA/USDT", "timeframe": "1h"},
                0.50,
            )
        assert result is not None and result.executed
        skips = [line for line in lines if "ws_stop_skip_no_bid" in line]
        assert len(skips) == 1
        assert "symbol=AAA/USDT" in skips[0]
        assert hub.stats()["ws_stop_skip_no_bid"] == 1
        assert not any(o.get("source") == "exit_ws" and o.get("status") == "filled" for o in _orders("AAA/USDT"))

    def test_string_bid_without_size_keys_sells(self):
        _seed("AAA/USDT", amount=20, entry=1.0)
        hub = _hub()
        hub.update_book([_row("AAA/USDT")])
        payload = {
            "currency_pair": "AAA_USDT",
            "last": "0.40",
            "highest_bid": "0.40",
            "change_percentage": "-60",
            "quote_volume": "5",
        }
        with _paper():
            hub.handle_ws_message(
                json.dumps({"channel": "spot.tickers", "event": "update", "result": payload})
            )
        filled = [o for o in _orders("AAA/USDT") if o.get("status") == "filled"]
        assert filled
        assert filled[-1]["signal"] == "SELL_STOP_FULL"
        assert filled[-1]["exit_source"] == "stop_loss"


class TestT10TenantContext:
    def _henry_doc(self, tid, test=False):
        return {
            "tenant_id": tid,
            "telegram": {"headless": True, "owner_chat_id": ""},
            "defaults": {"ledger_scope": "paper"},
        }

    def test_henry_tick_uses_henry_lot_and_restores_active_key(self):
        from strategies import positions as pm

        _seed("AAA/USDT", amount=10, entry=2.0)
        _seed("AAA/USDT", amount=8, entry=1.0, tenant_id="henry")
        before_keys = set(pm.positions)
        before_active = pm._active_key
        seen: list[str] = []
        real_get = pm.get_position

        def _spy(symbol, timeframe):
            from core.tenant_context import resolve_tenant_id

            seen.append(resolve_tenant_id())
            return real_get(symbol, timeframe)

        hub = _hub()
        hub.update_book(
            [
                _row(
                    "AAA/USDT",
                    position=_lot(amount=8, entry=1.0),
                    params=_params(full=10, partial=5),
                    tenant_id="henry",
                ),
            ]
        )
        # 0.85 is 15% under henry's entry and a gain vs default's entry 2.0.
        with _paper(), _tenant_config_is_normalized(), patch(
            "storage.tenant_registry.get_tenant", side_effect=self._henry_doc
        ), patch("strategies.positions.bootstrap_positions"), patch(
            "strategies.positions.get_position", side_effect=_spy
        ):
            _fire(hub, "AAA/USDT", "0.85", "0.85")
        assert seen
        assert set(seen) == {"henry"}
        assert pm._active_key == before_active
        assert set(pm.positions) == before_keys
        assert float(pm.positions[pm.get_key("AAA/USDT", "1h")]["amount"]) == 10
        from core.tenant_context import resolve_tenant_scope

        henry = pm._position_stores[( "henry", resolve_tenant_scope())]
        assert float(henry[pm.get_key("AAA/USDT", "1h")]["amount"]) == 0

    def test_henry_short_precheck_does_not_touch_default(self):
        from strategies import positions as pm

        _seed("AAA/USDT", amount=10, entry=1.0)
        short = _lot(
            amount=4,
            entry=1.0,
            side="short",
            short_recipe="climax_fade",
            leverage=2.0,
            entry_at="2026-09-29T12:00:00+00:00",
        )
        _seed(
            "AAA/USDT",
            amount=4,
            entry=1.0,
            tenant_id="henry",
            side="short",
            short_recipe="climax_fade",
            leverage=2.0,
            entry_at="2026-09-29T12:00:00+00:00",
        )
        before = set(pm.positions)
        before_active = pm._active_key
        seen: list[str] = []
        real_get = pm.get_position

        def _spy(symbol, timeframe):
            from core.tenant_context import resolve_tenant_id

            seen.append(resolve_tenant_id())
            return real_get(symbol, timeframe)

        hub = _hub(
            shorts={
                "enabled": True,
                "climax_fade": {
                    "enabled": True,
                    "stop_price_pct": 10.0,
                    "cover_close_pct": 3.0,
                    "time_cap_hours": 16,
                },
            }
        )
        hub.update_book([_row("AAA/USDT", position=short, params={}, tenant_id="henry")])
        with _tenant_config_is_normalized(), patch(
            "storage.tenant_registry.get_tenant", side_effect=self._henry_doc
        ), patch("strategies.positions.bootstrap_positions"), patch(
            "strategies.positions.get_position", side_effect=_spy
        ), patch(
            "services.exit_realtime.hub.try_execute_trail_exit",
            return_value={"ok": True, "executed": True, "message": "ok"},
        ) as mock_ex:
            hub.on_ticker("AAA_USDT", 1.20, highest_bid="1.20")
        assert seen and set(seen) == {"henry"}
        assert mock_ex.call_args.kwargs.get("action") == "COVER"
        assert pm._active_key == before_active
        assert set(pm.positions) == before


class TestT11DebounceThenCycleRemainder:
    def test_ws_partial_blocks_ws_full_and_cycle_sells_remainder(self):
        params = _params(full=40, partial=10)
        _seed("AAA/USDT", amount=100, entry=1.0)
        hub = _hub(cooldown=0.01)
        hub.update_book([_row("AAA/USDT", params=params)])
        with _paper():
            _fire(hub, "AAA/USDT", "0.80", "0.80")
            held_after = _held("AAA/USDT")
            _fire(hub, "AAA/USDT", "0.40", "0.40")
            assert recently_exited("AAA/USDT", within_sec=60.0) or not any(
                r.get("symbol") == "AAA/USDT" for r in hub.book_snapshot()
            )
            rest = _sell("AAA/USDT", 100, "SELL_STOP_FULL", price=0.40)
        assert held_after == pytest.approx(50.0, abs=0.01)
        assert rest.executed
        assert _held("AAA/USDT") == 0
        ws = [o for o in _orders("AAA/USDT") if o.get("source") == "exit_ws" and o.get("status") == "filled"]
        assert len(ws) == 1
        assert ws[0]["signal"] == "SELL_STOP_PARTIAL"


class TestT12DailySellExemption:
    def _arm_limit(self, limit=1):
        # get_bot_config() deep-copies the normalized cache. Mutate the cache
        # so the copy the orchestrator builds still sees the limit.
        import data_manager

        raw = data_manager.get_config()
        raw.setdefault("risk", {})["max_daily_sells"] = limit
        raw.setdefault("dry_run_defaults", {})["max_daily_sells"] = limit

    def test_hard_stops_bypass_and_other_sells_do_not(self):
        self._arm_limit(1)
        _seed_filled_sells(1)
        _seed("AAA/USDT", amount=40, entry=2.0)
        _seed("BBB/USDT", amount=40, entry=2.0)
        hub = _hub()
        hub.update_book([_row("BBB/USDT", params=_params(full=50, partial=10), entry=2.0)])
        with _paper(), _logs() as lines:
            from services.signal_orchestrator import SignalOrchestrator

            orch = SignalOrchestrator(notify_callback=lambda *a, **k: None)
            cycle = orch.execute_if_needed(
                _analysis("AAA/USDT", "SELL_STOP_FULL"),
                {"symbol": "AAA/USDT", "timeframe": "1h"},
                1.0,
            )
            _fire(hub, "BBB/USDT", "1.60", "1.60")
        assert cycle is not None and cycle.executed
        bbb = [o for o in _orders("BBB/USDT") if o.get("status") == "filled"]
        assert bbb
        assert bbb[-1]["signal"] == "SELL_STOP_PARTIAL"
        assert bbb[-1]["source"] == "exit_ws"
        assert bbb[-1]["exit_source"] == "stop_loss"
        bypasses = [line for line in lines if "daily_sells_limit_bypassed_hard_stop" in line]
        assert len(bypasses) >= 2
        assert any("signal=SELL_STOP_FULL" in line for line in bypasses)
        assert any("signal=SELL_STOP_PARTIAL" in line and "source=exit_ws" in line for line in bypasses)

        from risk.risk_manager import RiskManager
        from services.trading_service import TradingService

        count = RiskManager(TradingService().config)._daily_sells_count()
        assert count >= 3

        negatives = [
            ("CCC/USDT", "SELL_FULL", "auto", "trailing_take_profit"),
            ("DDD/USDT", "SELL_FULL", "auto", "trailing_stop"),
            ("EEE/USDT", "SELL_FULL", "rotation", "rotation"),
            ("FFF/USDT", "SELL_FULL", "manual", ""),
            ("GGG/USDT", "SELL_STOP_FULL", "manual", "stop_loss"),
            ("HHH/USDT", "SELL_STOP_FULL", "auto", ""),
        ]
        with _paper():
            for symbol, signal, source, exit_source in negatives:
                _seed(symbol, amount=50, entry=2.0)
                result = _sell(
                    symbol,
                    50,
                    signal,
                    source=source,
                    exit_source=exit_source,
                    price=2.0,
                )
                assert result.executed is False
                assert result.code == "max_daily_sells", (symbol, result.code, result.message)

    def test_closed_lot_and_stale_amount_still_apply_at_the_limit(self):
        self._arm_limit(1)
        _seed_filled_sells(1)
        _seed("AAA/USDT", amount=12, entry=2.0)
        with _paper(), _logs() as lines:
            closed = _sell("BBB/USDT", 10, "SELL_STOP_FULL", price=2.0)
            capped = _sell("AAA/USDT", 40, "SELL_STOP_FULL", price=2.0)
        assert closed.code == "sell_lot_closed"
        assert capped.executed
        assert _held("AAA/USDT") == 0
        assert any("sell_amount_capped" in line and "old_amount=40" in line for line in lines)
        assert any("daily_sells_limit_bypassed_hard_stop" in line for line in lines)


class TestT13TenantGuards:
    def test_two_tenants_no_collision_and_one_exit_does_not_block_the_other(self):
        from core.tenant_context import resolve_tenant_scope
        from strategies import positions as pm

        # Default entry 0.80: 0.85 is a gain. Henry entry 1.0: 0.85 is a 15% loss.
        _seed("AAA/USDT", amount=10, entry=0.80)
        _seed("AAA/USDT", amount=10, entry=1.0, tenant_id="henry")
        hub = _hub(cooldown=30)
        hub.update_book(
            [
                _row("AAA/USDT", position=_lot(amount=10, entry=0.80), params=_params(), tenant_id="default"),
                _row("AAA/USDT", position=_lot(amount=10, entry=1.0), params=_params(), tenant_id="henry"),
                _row("AAA/USDT", position=_lot(amount=10, entry=1.0), params=_params(), tenant_id="henry", timeframe="4h"),
            ]
        )
        # Same (tenant, symbol) twice keeps the incumbent and counts a collision.
        assert hub.stats()["book_tenant_collisions"] == 1
        snap = hub.book_snapshot()
        assert len([r for r in snap if r["symbol"] == "AAA/USDT"]) == 2
        hub2 = _hub()
        hub2.update_book(
            [
                _row("AAA/USDT", position=_lot(amount=10, entry=0.80), params=_params(full=10, partial=5), tenant_id="default"),
                _row("AAA/USDT", position=_lot(amount=10, entry=1.0), params=_params(full=10, partial=5), tenant_id="henry"),
            ]
        )
        assert hub2.stats()["book_tenant_collisions"] == 0

        def _doc(tid, test=False):
            return {
                "tenant_id": tid,
                "telegram": {"headless": True, "owner_chat_id": ""},
                "defaults": {"ledger_scope": "paper"},
            }

        with _paper(), _tenant_config_is_normalized(), patch(
            "storage.tenant_registry.get_tenant", side_effect=_doc
        ), patch("strategies.positions.bootstrap_positions"):
            _fire(hub2, "AAA/USDT", "0.85", "0.85")
            henry_left = float(
                pm._position_stores[("henry", resolve_tenant_scope())][
                    pm.get_key("AAA/USDT", "1h")
                ]["amount"]
            )
            assert henry_left == 0
            assert float(pm.positions[pm.get_key("AAA/USDT", "1h")]["amount"]) == 10
            assert not recently_exited("AAA/USDT", within_sec=60.0, tenant_id="default")
            assert recently_exited("AAA/USDT", within_sec=60.0, tenant_id="henry")
            _fire(hub2, "AAA/USDT", "0.40", "0.40")
        assert float(pm.positions[pm.get_key("AAA/USDT", "1h")]["amount"]) == 0


class TestT14TickAge:
    def test_age_on_fire_and_skip_and_missing_does_not_change_the_decision(self):
        fixed = 1_700_000_000.0
        time_ms = int((fixed - 1.25) * 1000)
        assert tick_age_label(time_ms, now=fixed) == "1.250"
        assert tick_age_label(None, now=fixed) == "missing"
        _seed("AAA/USDT", amount=20, entry=1.0)
        _seed("BBB/USDT", amount=20, entry=1.0)
        fire_hub = _hub()
        skip_hub = _hub(cooldown=60)
        fire_hub.update_book([_row("AAA/USDT")])
        skip_hub.update_book([_row("BBB/USDT")])
        with _paper(), _logs() as lines, patch(
            "services.exit_realtime.hub.time.time", return_value=fixed
        ):
            fire_hub.handle_ws_message(_frame("AAA_USDT", "0.40", "0.40", time_ms=time_ms))
            skip_hub.on_ticker("BBB_USDT", 0.40, highest_bid=None, time_ms=time_ms)
            fire_hub2 = _hub()
            _seed("CCC/USDT", amount=20, entry=1.0)
            fire_hub2.update_book([_row("CCC/USDT")])
            fire_hub2.handle_ws_message(_frame("CCC_USDT", "0.40", "0.40"))
        text = "\n".join(lines)
        assert "tick_age=1.250" in text
        assert "ws_stop_skip_no_bid" in text and "tick_age=1.250" in text
        assert "tick_age=missing" in text
        assert any(o.get("status") == "filled" for o in _orders("AAA/USDT"))
        assert any(o.get("status") == "filled" for o in _orders("CCC/USDT"))
        assert not any(o.get("status") == "filled" and o.get("source") == "exit_ws" for o in _orders("BBB/USDT"))
