"""#564 execute_cascade_exit: lock, profit, COVER_FULL, 60s guard, timer-after-fill."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core.actions import COVER_FULL, SELL_FULL
from services.exit_realtime.cascade_state import CascadeState
from services.exit_realtime.execute import execute_cascade_exit, recently_exited
from strategies.entry_guard import _is_stop_loss_source, entry_sell_allowed
from strategies.position_lock import auto_sell_blocked, build_lock
from strategies.sell_sources import LIQ_CASCADE_SOURCE, STOP_SOURCES


WINNER_CFG = {
    "enabled": True,
    "sources": ["entry_sensor_15m"],
    "fresh_entry_window_minutes": 120,
    "vol_spike_mult": 2.0,
    "vol_exhaustion_15m_max": 0.85,
    "exhaustion_min_gain_pct": 5.0,
    "mega_pump_gain_pct": 12.0,
    "block_loss_sells_minutes": 15,
    "by_tier": {
        "volatile": {"min_hold_minutes": 45, "min_gain_structure_pct": 8},
    },
}


def _fresh_guarded(*, minutes_ago: float = 2.0) -> dict:
    entry_at = (datetime.now() - timedelta(minutes=minutes_ago)).isoformat()
    return {
        "entry_source": "entry_sensor_15m",
        "entry_at": entry_at,
        "first_buy_at": entry_at,
        "strategy_tier": "volatile",
    }


def _continuation_metrics() -> dict:
    return {"volume_spike_ratio": 2.8, "price_momentum": True}


def _clear_guards(symbol: str) -> None:
    import services.exit_realtime.execute as ex

    with ex._inflight_lock:
        ex._inflight.discard(symbol)
        ex._last_exit_at.pop(symbol, None)


def _long_lot(**over):
    lot = {
        "symbol": "AAA/USDT",
        "timeframe": "1h",
        "amount": 10.0,
        "average_entry": 1.0,
        "side": "long",
        "current_price": 1.10,
    }
    lot.update(over)
    return lot


def _short_lot(**over):
    lot = {
        "symbol": "BBB/USDT",
        "timeframe": "4h",
        "amount": 10.0,
        "average_entry": 1.0,
        "side": "short",
        "current_price": 0.90,
    }
    lot.update(over)
    return lot


def _locked(lot: dict) -> dict:
    out = dict(lot)
    out["lock"] = build_lock(reason="hold")
    return out


def _run(side, lots, trading=None, **kwargs):
    trading = trading or MagicMock()
    trading.execute_order.return_value = SimpleNamespace(executed=True, message="ok filled")
    by_sym = {x["symbol"]: dict(x) for x in lots}

    def _get_pos(symbol, _tf):
        return dict(by_sym[symbol])

    with patch("strategies.positions.get_position", side_effect=_get_pos), patch(
        "strategies.positions.is_open_position", return_value=True
    ), patch(
        "strategies.position_lock.attach_lock_from_ledger",
        side_effect=lambda pos, *a, **k: pos,
    ), patch(
        "core.costs.CostModel.round_trip_pct", return_value=0.5
    ):
        out = execute_cascade_exit(
            side=side,
            lots=lots,
            prices={lt["symbol"]: float(lt["current_price"]) for lt in lots},
            trading=trading,
            fire_enabled=True,
            **kwargs,
        )
    return out, trading


class TestExecuteCascadeExit:
    def test_unlocked_in_profit_long_sell_full(self):
        _clear_guards("AAA/USDT")
        out, trading = _run("long", [_long_lot()])
        assert out["executed"] is True
        assert out["action"] == SELL_FULL
        order = trading.execute_order.call_args[0][0]
        assert order.type == "SELL"
        assert order.signal == SELL_FULL
        assert order.source == LIQ_CASCADE_SOURCE
        assert "STOP" not in order.signal
        assert "STOP" not in (order.exit_rationale or "")
        trading.execute_order.assert_called_once()

    def test_unlocked_in_profit_short_cover_full_no_stop(self):
        _clear_guards("BBB/USDT")
        out, trading = _run("short", [_short_lot()])
        assert out["executed"] is True
        assert out["action"] == COVER_FULL
        assert "STOP" not in out["action"]
        order = trading.execute_order.call_args[0][0]
        assert order.type == "COVER"
        assert order.signal == COVER_FULL
        assert "STOP" not in order.signal
        assert "STOP" not in (order.exit_rationale or "")
        assert "STOP" not in (order.type or "")
        assert order.source == LIQ_CASCADE_SOURCE
        assert LIQ_CASCADE_SOURCE not in STOP_SOURCES

    def test_locked_long_not_flattened(self):
        _clear_guards("AAA/USDT")
        out, trading = _run("long", [_locked(_long_lot())])
        assert out["executed"] is False
        assert any(r.get("code") == "position_locked" for r in out["results"])
        trading.execute_order.assert_not_called()

    def test_locked_short_not_flattened(self):
        _clear_guards("BBB/USDT")
        out, trading = _run("short", [_locked(_short_lot())])
        assert out["executed"] is False
        assert any(r.get("code") == "position_locked" for r in out["results"])
        trading.execute_order.assert_not_called()

    def test_loss_lots_stay(self):
        _clear_guards("AAA/USDT")
        out, trading = _run("long", [_long_lot(current_price=0.90)])
        assert out["executed"] is False
        assert any(r.get("message") == "not_in_profit" for r in out["results"])
        trading.execute_order.assert_not_called()

    def test_timer_starts_only_after_fill(self):
        _clear_guards("AAA/USDT")
        st = CascadeState(cooldown_sec=600)
        out, _ = _run("long", [_locked(_long_lot())], state=st, now_mono=10.0)
        assert out["executed"] is False
        assert st.last_fill_mono["long"] is None
        _clear_guards("AAA/USDT")
        st2 = CascadeState(cooldown_sec=600)
        out2, _ = _run("long", [_long_lot()], state=st2, now_mono=20.0)
        assert out2["executed"] is True
        assert st2.last_fill_mono["long"] == 20.0

    def test_second_sell_same_coin_inside_60s_does_not_fire(self):
        _clear_guards("AAA/USDT")
        out1, trading1 = _run("long", [_long_lot()])
        assert out1["executed"] is True
        assert recently_exited("AAA/USDT", within_sec=60.0)
        out2, trading2 = _run("long", [_long_lot()])
        assert out2["executed"] is False
        assert any(r.get("message") == "recent_exit" for r in out2["results"])
        trading2.execute_order.assert_not_called()

    def test_fire_enabled_false_does_not_flatten(self):
        _clear_guards("AAA/USDT")
        trading = MagicMock()
        out = execute_cascade_exit(
            side="long",
            lots=[_long_lot()],
            trading=trading,
            fire_enabled=False,
        )
        assert out["executed"] is False
        assert out["message"] == "fire_disabled"
        trading.execute_order.assert_not_called()

    def test_does_not_route_through_trail_exit(self):
        _clear_guards("AAA/USDT")
        with patch("services.exit_realtime.execute.try_execute_trail_exit") as trail:
            _run("long", [_long_lot()])
            trail.assert_not_called()

    def test_lock_check_error_fail_closed(self):
        _clear_guards("AAA/USDT")
        trading = MagicMock()
        with patch(
            "strategies.positions.get_position", return_value=_long_lot()
        ), patch(
            "strategies.positions.is_open_position", return_value=True
        ), patch(
            "strategies.position_lock.attach_lock_from_ledger",
            side_effect=RuntimeError("boom"),
        ):
            out = execute_cascade_exit(
                side="long",
                lots=[_long_lot()],
                prices={"AAA/USDT": 1.10},
                trading=trading,
                fire_enabled=True,
            )
        assert out["executed"] is False
        assert any(r.get("code") == "position_lock_check_error" for r in out["results"])
        trading.execute_order.assert_not_called()

    def test_entry_sell_allowed_does_not_swallow_full_exits(self):
        pos = _fresh_guarded()
        metrics = _continuation_metrics()
        for action in (SELL_FULL, COVER_FULL):
            allowed, reason = entry_sell_allowed(
                position=pos,
                strategy_params={"volatility_tier": "volatile"},
                sell_source=LIQ_CASCADE_SOURCE,
                action=action,
                gain_pct=1.0,
                ta_bearish=False,
                metrics_15m=metrics,
                cfg=WINNER_CFG,
            )
            assert allowed, action
            assert reason == ""

    def test_cover_full_is_not_stop_loss_source(self):
        assert _is_stop_loss_source("liq_cascade", "COVER_FULL") is False

    def test_lock_still_blocks_liq_cascade_source(self):
        pos = _locked(_long_lot())
        blocked, _ = auto_sell_blocked(pos, LIQ_CASCADE_SOURCE)
        assert blocked is True

    def test_buy_during_600s_has_no_cascade_reject_code(self):
        import inspect

        from risk.risk_manager import RiskManager

        st = CascadeState(cooldown_sec=600)
        st.note_fill("long", 0.0)
        assert st.last_fill_mono["long"] == 0.0
        assert not hasattr(CascadeState, "block_buy")
        assert not hasattr(CascadeState, "reject_buy")
        impl = inspect.getsource(RiskManager._evaluate_impl)
        buy_half = impl.split('if order.type == "SELL":', 1)[0]
        assert "liq_cascade" not in buy_half
        assert "cascade_cooldown" not in impl
        assert "cascade_block" not in impl

    def test_risk_manager_cover_lock_only_liq_cascade(self):
        from core.models import TradeOrder
        from risk.risk_manager import RiskManager

        cfg = MagicMock()
        cfg.raw = {"risk": {"position_locks": {"enabled": True}}}
        cfg.risk_config = {}
        for attr in ("max_usdt_per_trade", "max_open_positions", "trade_cooldown_hours"):
            setattr(cfg, attr, 100)
        rm = RiskManager(cfg)
        locked = _locked(_short_lot())
        order = TradeOrder(
            type="COVER",
            symbol="BBB/USDT",
            price=0.90,
            amount=10.0,
            signal=COVER_FULL,
            source=LIQ_CASCADE_SOURCE,
            exit_source=LIQ_CASCADE_SOURCE,
            exit_rationale="liq cascade pump full cover",
        )
        with patch("risk.risk_manager.get_position", return_value=locked), patch(
            "strategies.position_lock.log_lock_block"
        ):
            decision = rm.evaluate(order, timeframe="4h", source=LIQ_CASCADE_SOURCE)
        assert decision.approved is False
        assert getattr(decision, "code", None) == "position_locked"
        assert "STOP" not in (order.signal or "")
        assert "STOP" not in (order.exit_rationale or "")
