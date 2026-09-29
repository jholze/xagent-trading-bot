"""#614 paper climax-fade SHORT — formula, cover doors, open helper, soak.

No network, no Mongo, no ledger files. Bars are lists. Frozen short-cover
assertions live in other files and must stay bit-identical.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from core.config_guardrails import ConfigValidationError, reject_frozen_shorts_patch
from core.models import TradeOrder, TradeResult
from strategies.climax_fade import (
    bar_return,
    climax_cover_decision,
    climax_entry_signal,
    climax_fade_config,
    is_climax_lot,
    is_closed_4h_bar,
    last_closed_4h_close,
    replay_climax_fade,
    vol_mult_4_20,
)
from strategies.short_cover import evaluate_short_cover
from strategies.short_policy import AUTO_SOURCES, is_auto_short_source

import data_manager as _data_manager

_REAL_SAVE_CONFIG = _data_manager.save_config
assert _REAL_SAVE_CONFIG.__name__ == "save_config"


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "climax_fade_synthetic_4h.json"
)
_FOUR_H_MS = 4 * 3600 * 1000

SCAN_CFG = {
    "enabled": True,
    "timeframe": "4h",
    "min_return_pct": 6.0,
    "vol_short": 4,
    "vol_long": 20,
    "vol_mult_min": 2.0,
    "cover_close_pct": 3.0,
    "stop_price_pct": 10.0,
    "time_cap_hours": 16,
    "size_factor": 0.5,
    "exclude_symbols": ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT"],
}

OPERATOR_SHORTS = {
    "enabled": True,
    "allow_live": False,
    "auto_after_sell": False,
    "max_open": 6,
    "leverage_default": 2,
    "leverage_cap": 2,
    "climax_fade": dict(SCAN_CFG),
}


def _threshold_closes_vols(*, ret: float = 0.06, vol_mult: float = 2.0):
    """20 bars: last close at ``ret`` vs prev; last-4 / prior-16 vol = ``vol_mult``."""
    prev = 100.0
    last = prev * (1.0 + ret)
    closes = [prev] * 19 + [last]
    vols = [100.0] * 16 + [100.0 * vol_mult] * 4
    return closes, vols


def _now() -> datetime:
    return datetime(2026, 9, 29, 16, 0, tzinfo=timezone.utc)


def _closed_bar_ts(now: datetime | None = None) -> int:
    n = now or _now()
    return int(n.timestamp() * 1000) - _FOUR_H_MS


def _climax_lot(*, entry=100.0, age_h=1.0, extra=None):
    opened = _now() - timedelta(hours=age_h)
    lot = {
        "side": "short",
        "amount": 10.0,
        "average_entry": entry,
        "leverage": 2.0,
        "short_recipe": "climax_fade",
        "exit_source": "climax_fade",
        "entry_at": opened.isoformat(),
        "symbol": "AAA/USDT",
        "timeframe": "4h",
    }
    if extra:
        lot.update(extra)
    return lot


def _raw_cfg(**over) -> dict:
    cf = dict(SCAN_CFG)
    cf.update(over)
    return {
        "shorts": {**OPERATOR_SHORTS, "climax_fade": cf},
        "max_usdt_per_trade": 400,
        "trading_mode": "live",
        "live": {"execution": "shadow", "max_usdt_per_trade": 400},
    }


# --- formula -----------------------------------------------------------------


def test_vol_mult_none_until_20_bars():
    assert vol_mult_4_20([100.0] * 19) is None


def test_vol_mult_4_over_20():
    assert vol_mult_4_20([100.0] * 16 + [250.0] * 4) == pytest.approx(2.5)


def test_bar_return_none_on_nonpositive_prev():
    assert bar_return(0, 106) is None
    assert bar_return(-1, 106) is None


def test_bar_return_six_percent():
    assert bar_return(100, 106) == pytest.approx(0.06)


def test_entry_true_at_threshold():
    closes, vols = _threshold_closes_vols(ret=0.06, vol_mult=2.0)
    assert climax_entry_signal(
        closes=closes, volumes=vols, bar_index=len(closes) - 1, cfg=SCAN_CFG
    ) is True


def test_entry_false_return_just_below():
    closes, vols = _threshold_closes_vols(ret=0.0599, vol_mult=2.0)
    assert climax_entry_signal(
        closes=closes, volumes=vols, bar_index=len(closes) - 1, cfg=SCAN_CFG
    ) is False


def test_entry_false_vol_just_below():
    closes, vols = _threshold_closes_vols(ret=0.06, vol_mult=1.999)
    assert climax_entry_signal(
        closes=closes, volumes=vols, bar_index=len(closes) - 1, cfg=SCAN_CFG
    ) is False


def test_entry_false_forming_bar():
    now = datetime(2026, 9, 1, 2, 0, tzinfo=timezone.utc)
    bar_ts = int(datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert is_closed_4h_bar(bar_ts, now) is False
    svc, locked = _open_svc()
    closes, vols = _threshold_closes_vols(ret=0.08, vol_mult=3.0)
    closed_bar = {
        "ts_ms": bar_ts,
        "closes": closes,
        "volumes": vols,
        "bar_index": len(closes) - 1,
    }
    with patch(
        "strategies.positions.find_open_position_for_symbol", return_value=None
    ), patch("strategies.positions.get_position", return_value={"amount": 0}), patch(
        "strategies.positions.is_open_position", return_value=False
    ), patch(
        "strategies.short_math.is_short", return_value=False
    ), patch(
        "strategies.positions.list_active_positions", return_value=[]
    ), patch(
        "data.cmc_market_cap.resolve_market_cap_usd", return_value=1e9
    ):
        out = svc._maybe_climax_fade_short("AAA/USDT", "4h", closed_bar, 108.0, now=now)
    assert out is None
    locked.assert_not_called()


def test_entry_uses_only_index_i_not_future():
    # Bar i is quiet; bar i+1 is the climax. Signal at i must not look ahead.
    closes = [100.0] * 20 + [106.0]
    vols = [100.0] * 17 + [200.0] * 4
    i = 19
    assert climax_entry_signal(closes=closes, volumes=vols, bar_index=i, cfg=SCAN_CFG) is False
    assert climax_entry_signal(
        closes=closes, volumes=vols, bar_index=i + 1, cfg=SCAN_CFG
    ) is True


@pytest.mark.parametrize("sym", ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "btc/usdt"])
def test_exclude_btc_eth_bnb_sol(sym):
    svc, locked = _open_svc()
    closes, vols = _threshold_closes_vols()
    closed_bar = {
        "ts_ms": _closed_bar_ts(),
        "closes": closes,
        "volumes": vols,
        "bar_index": len(closes) - 1,
    }
    with patch(
        "data.cmc_market_cap.resolve_market_cap_usd", return_value=1e9
    ):
        out = svc._maybe_climax_fade_short(sym, "4h", closed_bar, 106.0, now=_now())
    assert out is None
    locked.assert_not_called()


# --- cover -------------------------------------------------------------------


def test_no_cover_close_minus_2_9_pct():
    lot = _climax_lot()
    hit = climax_cover_decision(
        lot, closed_4h_close=97.1, mark=97.1, now=_now(), cfg=SCAN_CFG
    )
    assert hit is None


def test_cover_close_minus_3_0_pct():
    lot = _climax_lot()
    hit = climax_cover_decision(
        lot, closed_4h_close=97.0, mark=97.0, now=_now(), cfg=SCAN_CFG
    )
    assert hit is not None
    assert hit["source"] == "climax_cover"


def test_no_take_on_signal_bar_closed_before_entry():
    """Signal-bar close vs a later live mark must not fire climax_cover."""
    signal_open = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    entry_at = datetime(2026, 9, 29, 16, 5, tzinfo=timezone.utc)
    lot = _climax_lot(entry=103.1)
    lot["entry_at"] = entry_at.isoformat()
    lot["average_entry"] = 103.1
    hit = climax_cover_decision(
        lot,
        closed_4h_close=100.0,
        mark=103.1,
        now=entry_at,
        cfg=SCAN_CFG,
        bar_open_ts_ms=int(signal_open.timestamp() * 1000),
    )
    assert hit is None


def test_take_on_bar_opened_after_entry():
    entry_at = datetime(2026, 9, 29, 16, 5, tzinfo=timezone.utc)
    later_open = datetime(2026, 9, 29, 20, 0, tzinfo=timezone.utc)
    lot = _climax_lot(entry=100.0)
    lot["entry_at"] = entry_at.isoformat()
    hit = climax_cover_decision(
        lot,
        closed_4h_close=97.0,
        mark=97.0,
        now=later_open + timedelta(hours=4),
        cfg=SCAN_CFG,
        bar_open_ts_ms=int(later_open.timestamp() * 1000),
    )
    assert hit is not None
    assert hit["source"] == "climax_cover"


class _OHLCV:
    empty = False

    def __init__(self, ts, close):
        self.columns = ["ts", "close"]
        self._ts = list(ts)
        self._close = list(close)

    def __getitem__(self, key):
        class _Col:
            def __init__(self, data):
                self._data = data

            def tolist(self):
                return list(self._data)

        if key == "ts":
            return _Col(self._ts)
        if key == "close":
            return _Col(self._close)
        raise KeyError(key)


def test_last_closed_4h_close_skips_signal_bar_before_entry():
    signal_open = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    entry_at = datetime(2026, 9, 29, 16, 5, tzinfo=timezone.utc)
    now = datetime(2026, 9, 29, 16, 30, tzinfo=timezone.utc)
    signal_ts = int(signal_open.timestamp() * 1000)
    df = _OHLCV([signal_ts], [100.0])
    assert last_closed_4h_close("AAA/USDT", now, ohlcv=df) == pytest.approx(100.0)
    assert (
        last_closed_4h_close(
            "AAA/USDT",
            now,
            ohlcv=df,
            entry_at_ms=int(entry_at.timestamp() * 1000),
        )
        is None
    )


def test_cover_ignores_mark_wick_without_close():
    lot = _climax_lot()
    hit = climax_cover_decision(
        lot, closed_4h_close=98.0, mark=96.0, now=_now(), cfg=SCAN_CFG
    )
    assert hit is None


def test_stop_at_plus_10_pct_price():
    lot = _climax_lot()
    hit = climax_cover_decision(
        lot, closed_4h_close=100.0, mark=110.0, now=_now(), cfg=SCAN_CFG
    )
    assert hit is not None
    assert hit["source"] == "climax_stop"


def test_stop_not_margin_stop():
    lot = _climax_lot(age_h=1.0)
    hit = climax_cover_decision(
        lot, closed_4h_close=99.0, mark=105.0, now=_now(), cfg=SCAN_CFG
    )
    assert hit is None


def test_stop_wins_same_bar_as_take():
    lot = _climax_lot()
    hit = climax_cover_decision(
        lot, closed_4h_close=96.0, mark=110.0, now=_now(), cfg=SCAN_CFG
    )
    assert hit is not None
    assert hit["source"] == "climax_stop"


def test_time_cap_16h():
    lot = _climax_lot(age_h=16.0)
    hit = climax_cover_decision(
        lot, closed_4h_close=100.0, mark=100.0, now=_now(), cfg=SCAN_CFG
    )
    assert hit is not None
    assert hit["source"] == "climax_time"


def test_time_cap_not_4h():
    lot = _climax_lot(age_h=5.0)
    hit = climax_cover_decision(
        lot, closed_4h_close=100.0, mark=100.0, now=_now(), cfg=SCAN_CFG
    )
    assert hit is None


def test_no_rsi_cover():
    lot = _climax_lot(age_h=1.0, extra={"last_rsi": 28})
    hit = climax_cover_decision(
        lot, closed_4h_close=98.0, mark=94.0, now=_now(), cfg=SCAN_CFG
    )
    assert hit is None


def test_no_trail_arm_4pct():
    lot = _climax_lot(age_h=1.0, extra={"recent_low": 90.0})
    hit = climax_cover_decision(
        lot, closed_4h_close=98.0, mark=92.0, now=_now(), cfg=SCAN_CFG
    )
    assert hit is None


def test_kill_switch_still_stops():
    lot = _climax_lot()
    cfg = dict(SCAN_CFG)
    hit = climax_cover_decision(
        lot, closed_4h_close=100.0, mark=110.0, now=_now(), cfg=cfg
    )
    assert hit is not None
    assert hit["source"] == "climax_stop"


def test_reactive_lot_not_classified():
    lot = {
        "side": "short",
        "amount": 1.0,
        "average_entry": 100.0,
        "leverage": 2.0,
        "entry_at": (_now() - timedelta(hours=1)).isoformat(),
        "symbol": "AAA/USDT",
    }
    assert is_climax_lot(lot) is False
    hit = evaluate_short_cover(lot, 100.0, config_raw={"shorts": {"enabled": True}})
    assert hit is None or hit.get("source") != "climax_cover"


# --- open helper -------------------------------------------------------------


def _open_svc(raw=None):
    from core.config import BotConfig
    from services.trading_service import TradingService

    svc = TradingService(BotConfig(raw=raw or _raw_cfg()))
    opened = MagicMock(return_value=TradeResult(True, "SHORT", "AAA/USDT"))
    svc.execute_order = opened
    svc._execute_order_locked = MagicMock()
    svc._maybe_auto_short_after_sell = MagicMock()
    svc.max_usdt_for_order = MagicMock(return_value=400.0)
    return svc, opened


def _closed_ok_bar(symbol="AAA/USDT"):
    closes, vols = _threshold_closes_vols()
    return {
        "ts_ms": _closed_bar_ts(),
        "closes": closes,
        "volumes": vols,
        "bar_index": len(closes) - 1,
        "symbol": symbol,
    }


def _open_patches(*, long_open=False, short_open=False, dust_long=False, mcap=1e9):
    long_pos = {"amount": 10.0, "average_entry": 1.0, "side": "long"}
    dust = {"amount": 0.1, "average_entry": 1.0, "side": "long"}
    short_pos = {
        "amount": 5.0,
        "average_entry": 1.0,
        "side": "short",
        "short_recipe": "climax_fade",
    }
    if short_open:
        pos = short_pos
        found = ("4h", short_pos)
        is_open = True
        is_short = True
    elif long_open:
        pos = long_pos
        found = ("4h", long_pos)
        is_open = True
        is_short = False
    elif dust_long:
        pos = dust
        found = None
        is_open = False
        is_short = False
    else:
        pos = {"amount": 0}
        found = None
        is_open = False
        is_short = False
    return {
        "find": patch(
            "strategies.positions.find_open_position_for_symbol", return_value=found
        ),
        "get": patch("strategies.positions.get_position", return_value=pos),
        "open": patch("strategies.positions.is_open_position", return_value=is_open),
        "short": patch("strategies.short_math.is_short", return_value=is_short),
        "list": patch("strategies.positions.list_active_positions", return_value=[]),
        "mcap": patch("data.cmc_market_cap.resolve_market_cap_usd", return_value=mcap),
        "set": patch("strategies.positions.set_position_field"),
    }


def test_open_uses_size_factor_not_sell_fraction():
    svc, locked = _open_svc()
    bar = _closed_ok_bar()
    patches = _open_patches()
    with patches["find"], patches["get"], patches["open"], patches["short"], patches[
        "list"
    ], patches["mcap"], patches["set"]:
        svc._maybe_climax_fade_short("AAA/USDT", "4h", bar, 106.0, now=_now())
    locked.assert_called_once()
    order = locked.call_args[0][0]
    assert order.type == "SHORT"
    assert order.usdt_amount == pytest.approx(200.0)
    assert order.usdt_amount != pytest.approx(0.35 * 400.0)


def test_open_idempotent_per_bar_ts():
    svc, locked = _open_svc()
    bar = _closed_ok_bar()
    patches = _open_patches()
    with patches["find"], patches["get"], patches["open"], patches["short"], patches[
        "list"
    ], patches["mcap"], patches["set"]:
        svc._maybe_climax_fade_short("AAA/USDT", "4h", bar, 106.0, now=_now())
        svc._maybe_climax_fade_short("AAA/USDT", "4h", bar, 106.0, now=_now())
    assert locked.call_count == 1


def test_open_skipped_if_material_long():
    svc, locked = _open_svc()
    bar = _closed_ok_bar()
    patches = _open_patches(long_open=True)
    with patches["find"], patches["get"], patches["open"], patches["short"], patches[
        "list"
    ], patches["mcap"], patches["set"]:
        out = svc._maybe_climax_fade_short("AAA/USDT", "4h", bar, 106.0, now=_now())
    assert out is None
    locked.assert_not_called()


def test_open_allowed_if_dust_long():
    svc, locked = _open_svc()
    bar = _closed_ok_bar()
    patches = _open_patches(dust_long=True)
    with patches["find"], patches["get"], patches["open"], patches["short"], patches[
        "list"
    ], patches["mcap"], patches["set"]:
        svc._maybe_climax_fade_short("AAA/USDT", "4h", bar, 106.0, now=_now())
    locked.assert_called_once()


def test_open_skipped_if_short_already_open():
    svc, locked = _open_svc()
    bar = _closed_ok_bar()
    patches = _open_patches(short_open=True)
    with patches["find"], patches["get"], patches["open"], patches["short"], patches[
        "list"
    ], patches["mcap"], patches["set"]:
        out = svc._maybe_climax_fade_short("AAA/USDT", "4h", bar, 106.0, now=_now())
    assert out is None
    locked.assert_not_called()


def test_open_skipped_if_disabled():
    raw = _raw_cfg(enabled=False)
    svc, locked = _open_svc(raw)
    bar = _closed_ok_bar()
    patches = _open_patches()
    with patches["find"], patches["get"], patches["open"], patches["short"], patches[
        "list"
    ], patches["mcap"], patches["set"]:
        out = svc._maybe_climax_fade_short("AAA/USDT", "4h", bar, 106.0, now=_now())
    assert out is None
    locked.assert_not_called()


@pytest.mark.parametrize("tf", ["1h", "15m"])
def test_open_skipped_wrong_timeframe(tf):
    svc, locked = _open_svc()
    bar = _closed_ok_bar()
    patches = _open_patches()
    with patches["find"], patches["get"], patches["open"], patches["short"], patches[
        "list"
    ], patches["mcap"], patches["set"]:
        out = svc._maybe_climax_fade_short("AAA/USDT", tf, bar, 106.0, now=_now())
    assert out is None
    locked.assert_not_called()


def test_does_not_call_maybe_auto_short_after_sell():
    svc, locked = _open_svc()
    bar = _closed_ok_bar()
    patches = _open_patches()
    with patches["find"], patches["get"], patches["open"], patches["short"], patches[
        "list"
    ], patches["mcap"], patches["set"]:
        svc._maybe_climax_fade_short("AAA/USDT", "4h", bar, 106.0, now=_now())
    svc._maybe_auto_short_after_sell.assert_not_called()
    locked.assert_called_once()


def test_open_goes_through_execute_order_not_lock_held_shortcut():
    svc, opened = _open_svc()
    bar = _closed_ok_bar()
    patches = _open_patches()
    with patches["find"], patches["get"], patches["open"], patches["short"], patches[
        "list"
    ], patches["mcap"], patches["set"]:
        svc._maybe_climax_fade_short("AAA/USDT", "4h", bar, 106.0, now=_now())
    opened.assert_called_once()
    kwargs = opened.call_args.kwargs
    assert kwargs.get("source") == "auto"
    assert kwargs.get("idempotency_key", "").startswith("climaxfade|")
    svc._execute_order_locked.assert_not_called()
    assert "_lock_held" not in kwargs


def test_auto_sources_unchanged():
    assert "climax_fade" not in AUTO_SOURCES
    assert is_auto_short_source("climax_fade") is False
    assert is_auto_short_source("rsi_sell") is True


def test_auto_after_sell_false_skips_open():
    from core.config import BotConfig
    from services.trading_service import TradingService

    raw = _raw_cfg()
    raw["shorts"]["auto_after_sell"] = False
    svc = TradingService(BotConfig(raw=raw))
    locked = MagicMock()
    svc._execute_order_locked = locked
    order = TradeOrder(
        type="SELL",
        symbol="AAA/USDT",
        price=1.0,
        amount=10,
        usdt_amount=10,
        exit_source="rsi_sell",
    )
    result = type("R", (), {"price": 1.0, "usdt_amount": 1000.0, "executed": True})()
    out = svc._maybe_auto_short_after_sell(order, "4h", result)
    assert out is None
    locked.assert_not_called()


def test_auto_after_sell_missing_defaults_true():
    from strategies.short_policy import auto_after_sell_enabled

    assert auto_after_sell_enabled({"shorts": {"enabled": True}}) is True
    assert auto_after_sell_enabled({}) is True
    assert auto_after_sell_enabled({"shorts": {"auto_after_sell": False}}) is False


def test_config_json_auto_after_sell_false():
    with open(os.path.join(REPO_ROOT, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    assert cfg["shorts"]["auto_after_sell"] is False


def test_open_writes_paper_ledger_not_jsonl():
    svc, opened = _open_svc()
    bar = _closed_ok_bar()
    patches = _open_patches()
    with patches["find"], patches["get"], patches["open"], patches["short"], patches[
        "list"
    ], patches["mcap"], patches["set"]:
        svc._maybe_climax_fade_short("AAA/USDT", "4h", bar, 106.0, now=_now())
    opened.assert_called_once()
    svc._execute_order_locked.assert_not_called()
    order = opened.call_args[0][0]
    assert order.exit_source == "climax_fade"
    assert order.idempotency_key.startswith("climaxfade|")
    assert "jsonl" not in str(opened.call_args).lower()


# --- cycle / reactive --------------------------------------------------------


def test_cycle_take_only_on_closed_4h():
    from core.models import SignalAnalysis
    from services.signal_orchestrator import SignalOrchestrator

    orch = SignalOrchestrator()
    captured = {}

    def _exec(order, *a, **k):
        captured["order"] = order
        return TradeResult(True, "COVER", order.symbol, amount=order.amount, price=order.price)

    orch.trading.execute_order = _exec
    pos = _climax_lot()
    pos["timeframe"] = "4h"
    analysis = SignalAnalysis(
        action="HOLD",
        symbol="AAA/USDT",
        timeframe="4h",
        rsi=50.0,
        lower_bb=1.0,
        vol_multiplier=1.0,
        ampel_emoji="",
        ampel_text="",
        sources=["technical"],
        normalized_action="HOLD",
    )
    entry_ms = int(datetime.fromisoformat(pos["entry_at"]).timestamp() * 1000)
    bar_ts = entry_ms + _FOUR_H_MS
    with patch(
        "strategies.climax_fade.last_closed_4h_bar", return_value=(97.0, bar_ts)
    ):
        handled, result = orch._cycle_short_cover(pos, analysis, 97.0)
    assert handled is True
    assert result is not None and result.executed
    assert captured["order"].type == "COVER"
    assert captured["order"].exit_source == "climax_cover"


def test_cycle_short_cover_reactive_unchanged():
    from core.models import SignalAnalysis
    from services.signal_orchestrator import SignalOrchestrator

    orch = SignalOrchestrator()
    captured = {}

    def _exec(order, *a, **k):
        captured["order"] = order
        return TradeResult(True, "COVER", order.symbol)

    orch.trading.execute_order = _exec
    opened = _now() - timedelta(hours=5)
    pos = {
        "side": "short",
        "amount": 10.0,
        "average_entry": 100.0,
        "leverage": 2.0,
        "entry_at": opened.isoformat(),
        "symbol": "SHRT/USDT",
        "timeframe": "4h",
        "strategy_tier": "volatile",
    }
    analysis = SignalAnalysis(
        action="HOLD",
        symbol="SHRT/USDT",
        timeframe="4h",
        rsi=50.0,
        lower_bb=1.0,
        vol_multiplier=1.0,
        ampel_emoji="",
        ampel_text="",
        sources=["technical"],
        normalized_action="HOLD",
    )
    with patch(
        "strategies.short_cover.evaluate_short_cover",
        return_value={"source": "time_cap", "rationale": "held 5.0h >= cap 4h"},
    ) as cover:
        handled, result = orch._cycle_short_cover(pos, analysis, 100.0)
    cover.assert_called()
    assert handled is True
    assert captured["order"].exit_source == "time_cap"


# --- guardrail / config ------------------------------------------------------


def test_defaults_match_scan_numbers():
    cfg = climax_fade_config({"shorts": {"climax_fade": {}}})
    assert cfg["min_return_pct"] == 6.0
    assert cfg["vol_mult_min"] == 2.0
    assert cfg["cover_close_pct"] == 3.0
    assert cfg["stop_price_pct"] == 10.0
    assert cfg["time_cap_hours"] == 16
    assert cfg["size_factor"] == 0.5
    assert cfg["exclude_symbols"] == ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT"]
    missing = climax_fade_config({"shorts": {}})
    assert missing["enabled"] is False


def test_hermes_cannot_patch_climax_fade():
    with pytest.raises(ConfigValidationError) as ei:
        reject_frozen_shorts_patch(
            {"shorts": {"climax_fade": {"min_return_pct": 5.0}}}
        )
    assert ei.value.path == "shorts.climax_fade"


def test_hermes_cannot_patch_auto_after_sell():
    with pytest.raises(ConfigValidationError) as ei:
        reject_frozen_shorts_patch({"shorts": {"auto_after_sell": True}})
    assert ei.value.path == "shorts.auto_after_sell"


def test_allow_live_still_false():
    from core.config import get_bot_config

    with open(os.path.join(REPO_ROOT, "config.json"), encoding="utf-8") as f:
        disk = json.load(f)
    assert disk["shorts"]["allow_live"] is False
    loaded = get_bot_config().raw
    assert loaded["shorts"]["allow_live"] is False


def test_daily_line_zero_when_empty():
    from scripts.daily_auswertung import climax_fade_telegram_line

    line = climax_fade_telegram_line({}, [], now=_now())
    assert "climax-fade paper:" in line
    assert "open=0" in line


def test_daily_line_counts_climax_lots():
    from scripts.daily_auswertung import climax_fade_telegram_line

    now = _now()
    positions = {
        "AAA_USDT_4h": {
            "amount": 10,
            "average_entry": 100,
            "side": "short",
            "short_recipe": "climax_fade",
        },
        "BBB_USDT_4h": {
            "amount": 5,
            "average_entry": 1,
            "side": "short",
        },
    }
    trades = [
        {
            "type": "COVER",
            "symbol": "CCC/USDT",
            "timestamp": (now - timedelta(days=2)).isoformat(),
            "pnl": 12.0,
            "exit_source": "climax_cover",
            "short_recipe": "climax_fade",
            "source": "climax_cover",
        },
        {
            "type": "SHORT",
            "symbol": "DDD/USDT",
            "timestamp": (now - timedelta(days=1)).isoformat(),
            "pnl": 0,
            "exit_source": "rsi_sell",
            "source": "auto",
        },
    ]
    line = climax_fade_telegram_line(positions, trades, now=now)
    assert "open=1" in line
    assert "closed_7d=1" in line
    assert "climax_cover" in line
    assert "rsi_sell" not in line or line.count("closed_7d=1") == 1


def test_rescan_script_synthetic_fixture():
    from scripts.climax_fade_rescan import rescan_synthetic

    out = rescan_synthetic(FIXTURE)
    assert out["n_entries"] == 1
    assert out["pf"] is not None
    assert out["network"] is False


def test_replay_vector_one_entry_take_pnl_positive():
    with open(FIXTURE, encoding="utf-8") as f:
        data = json.load(f)
    result = replay_climax_fade(
        data["closes"],
        data["volumes"],
        cfg=SCAN_CFG,
        fee_bp_side=7.5,
        funding_rate_8h=0.0001,
        bar_hours=4.0,
    )
    assert result["n_entries"] == 1
    assert result["n_covers"] == 1
    assert result["cover_sources"] == ["climax_cover"]
    assert result["pnl_pct"] > 0
    assert result["pf"] == float("inf")
    assert result["pf"] != result["pnl_pct"]


def test_daily_summary_contains_line_when_empty(tmp_path, monkeypatch):
    from scripts import daily_auswertung as da

    (tmp_path / "config.json").write_text(
        json.dumps({"live": {"dry_run": True}, "observability": {}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        da,
        "_ledger_bundle",
        lambda **k: (
            {"trades": [], "virtual_balance": 0, "realized_pnl": 0},
            {"orders": []},
            {},
        ),
    )
    monkeypatch.setattr(da, "_telegram_portfolio_nav_block", lambda **k: "NAV $0")
    monkeypatch.setattr(
        da, "decision_stats", lambda *a, **k: {"buy_dca": 0, "buy_dca_executed": 0, "buy_dca_shadow": 0, "total": 0, "hold": 0}
    )
    monkeypatch.setattr("core.tenant_context.resolve_tenant_id", lambda: "default")
    summary = da.build_telegram_daily_summary(tmp_path, _now())
    assert "climax-fade paper:" in summary
    assert "open=0" in summary


def test_climax_mark_on_persisted_position_after_open():
    from strategies.positions import clear_positions_memory, get_position
    from services.portfolio_service import PortfolioService

    clear_positions_memory()
    svc, _opened = _open_svc()
    port = PortfolioService(svc.config)
    svc.execute_order = lambda order, *a, **k: port.execute_order(order, "4h")
    bar = _closed_ok_bar()
    patches = _open_patches()
    with patches["find"], patches["list"], patches["mcap"]:
        result = svc._maybe_climax_fade_short("AAA/USDT", "4h", bar, 106.0, now=_now())
    assert result is not None and result.executed
    pos = get_position("AAA/USDT", "4h")
    assert pos.get("short_recipe") == "climax_fade"
    assert pos.get("exit_source") == "climax_fade"
    assert is_climax_lot(pos) is True


def test_set_position_field_throw_cannot_leave_unmarked_executed_short():
    from strategies.positions import clear_positions_memory, get_position
    from services.portfolio_service import PortfolioService

    clear_positions_memory()
    svc, _opened = _open_svc()
    port = PortfolioService(svc.config)
    svc.execute_order = lambda order, *a, **k: port.execute_order(order, "4h")
    bar = _closed_ok_bar()
    patches = _open_patches()
    with patches["find"], patches["list"], patches["mcap"], patch(
        "strategies.positions.set_position_field", side_effect=RuntimeError("no stamp")
    ):
        result = svc._maybe_climax_fade_short("AAA/USDT", "4h", bar, 106.0, now=_now())
    assert result is not None and result.executed
    pos = get_position("AAA/USDT", "4h")
    assert is_climax_lot(pos) is True
    assert pos.get("short_recipe") == "climax_fade"


def test_full_cover_clears_climax_mark_for_next_lot():
    from strategies.positions import clear_positions_memory, get_position, update_position

    clear_positions_memory()
    update_position(
        "AAA/USDT",
        "4h",
        "SHORT",
        100.0,
        10,
        leverage=2,
        short_recipe="climax_fade",
        exit_source="climax_fade",
    )
    assert is_climax_lot(get_position("AAA/USDT", "4h")) is True
    update_position("AAA/USDT", "4h", "COVER", 97.0, 10)
    assert is_climax_lot(get_position("AAA/USDT", "4h")) is False
    update_position("AAA/USDT", "4h", "SHORT", 90.0, 8, leverage=2)
    assert is_climax_lot(get_position("AAA/USDT", "4h")) is False


def test_skip_after_signal_consumes_bar_idempotency():
    svc, opened = _open_svc()
    bar = _closed_ok_bar()
    shorts = [{"amount": 1.0, "side": "short"}] * 6
    patches = _open_patches()
    with patches["find"], patches["get"], patches["open"], patch(
        "strategies.positions.list_active_positions", return_value=shorts
    ), patches["mcap"], patches["set"]:
        out1 = svc._maybe_climax_fade_short("AAA/USDT", "4h", bar, 106.0, now=_now())
    assert out1 is None
    opened.assert_not_called()
    with patches["find"], patches["get"], patches["open"], patches["list"], patches[
        "mcap"
    ], patches["set"]:
        out2 = svc._maybe_climax_fade_short("AAA/USDT", "4h", bar, 109.0, now=_now())
    assert out2 is None
    opened.assert_not_called()


def test_save_config_rejects_dropping_auto_after_sell(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(_data_manager, "_config_cache", None)
    monkeypatch.setattr(
        "core.tenant_context.resolve_tenant_id", lambda tenant_id=None: "default"
    )
    prev = {
        "trading_mode": "paper",
        "shorts": {"auto_after_sell": False, "allow_live": False},
    }
    assert _REAL_SAVE_CONFIG(prev) is True
    body = {"trading_mode": "paper", "shorts": {"allow_live": False}}
    with pytest.raises(ConfigValidationError) as ei:
        _REAL_SAVE_CONFIG(body)
    assert ei.value.path == "shorts.auto_after_sell"
