"""Tenant rule switches T, G, V, E and limits (specs/tenant-rule-switches)."""

from __future__ import annotations

import copy
import math
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

from core.config import BotConfig
from core.models import TradeOrder
from data_manager import get_config
from risk import rule_switches as rs
from risk.risk_manager import RiskManager

H = 3600
D = 86400
# Decision time: 30 min into an hour, 10.5 h into a UTC day.
DAY0 = 1_790_000_000 - (1_790_000_000 % D)
NOW = DAY0 + 10 * H + 1800
SYM = "ABC/USDT"


# --- fixtures -------------------------------------------------------------------------


def _cfg(rule_switches: dict | None = None, **top) -> BotConfig:
    raw = copy.deepcopy(get_config())
    raw["trading_mode"] = "paper"
    raw["initial_capital_usdt"] = 100_000
    raw["max_open_positions"] = 24
    raw["max_usdt_per_trade"] = 2500
    raw["max_position_percent"] = 30
    risk = dict(raw.get("risk") or {})
    risk["cash_floor_pct"] = 0
    risk["cash_floor_basis"] = "initial"
    risk["dca_reserve_pct"] = 0
    risk["min_trade_usdt"] = 10.0
    risk["venue_quality"] = {"enabled": False, "min_volume_to_order_multiple": 20}
    risk["position_capacity"] = {"enabled": False}
    risk["cash_policy"] = {"enabled": False}
    risk["slot_eviction"] = {"enabled": False}
    risk["moderate_deploy"] = {"enabled": False}
    raw["risk"] = risk
    uni = dict(raw.get("universe") or {})
    uni["split_enabled"] = False
    raw["universe"] = uni
    raw.pop("rule_switches", None)
    if rule_switches is not None:
        raw["rule_switches"] = rule_switches
    raw.update(top)
    return BotConfig(raw)


def _hourly(closes: list[float], lows: list[float] | None = None, *, end_open: int | None = None):
    """Hourly bars, last one opening at *end_open* (default: last closed hour + forming)."""
    end_open = end_open if end_open is not None else (NOW // H) * H  # forming bar
    n = len(closes)
    out = []
    for i, c in enumerate(closes):
        t = end_open - (n - 1 - i) * H
        lo = lows[i] if lows is not None else c * 0.999
        out.append([t * 1000, c, max(c, lo) * 1.001, lo, c, 1000.0])
    return out


def _daily(closes: list[float]):
    """Daily bars; the last one is today's forming bar."""
    end_open = (NOW // D) * D
    n = len(closes)
    return [[(end_open - (n - 1 - i) * D) * 1000, c, c * 1.01, c * 0.99, c, 1e6] for i, c in enumerate(closes)]


FLAT_H = [100.0] * 72


def _uptrend_daily(last: float = 105.0) -> list[float]:
    # 59 closed days at 100, last closed day = *last*, then forming bar.
    return [100.0] * 59 + [last] + [last]


class _Market:
    def __init__(self, hourly=None, daily=None):
        self.hourly = hourly if hourly is not None else _hourly(FLAT_H)
        self.daily = daily if daily is not None else _daily(_uptrend_daily())
        self.calls: list[tuple] = []

    def fetch_ohlcv(self, symbol, timeframe, limit=100):
        self.calls.append((symbol, timeframe, limit))
        return self.hourly if timeframe == "1h" else self.daily

    def fetch_indicators(self, *a, **k):
        return {"atr_pct": 3.0, "rsi": 45.0}


def _order(price=100.0, usdt=2000.0, signal="BUY", source="auto", symbol=SYM, type_="BUY", amount=0.0):
    return TradeOrder(
        type=type_, symbol=symbol, price=price, amount=amount, usdt_amount=usdt, signal=signal, source=source
    )


def _evaluate(rm: RiskManager, order: TradeOrder, *, source="auto", position=None, open_slots=5,
              lots=None, equity=100_000.0, peak=None, extra=()):
    pos = position if position is not None else {"amount": 0, "average_entry": 0}
    with ExitStack() as st:
        st.enter_context(patch("risk.risk_manager.count_open_full_slots", return_value=open_slots))
        st.enter_context(patch("risk.risk_manager.get_position", return_value=pos))
        st.enter_context(patch("risk.risk_manager.find_open_position_for_symbol", return_value=None))
        st.enter_context(
            patch("risk.risk_manager.find_open_positions_for_symbol", return_value=lots or [])
        )
        st.enter_context(patch.object(rm, "_trade_cooldown_blocked", return_value=(False, "")))
        st.enter_context(patch.object(rm, "_daily_buy_limit_blocked", return_value=None))
        st.enter_context(patch.object(rm, "_daily_dca_usdt_limit_blocked", return_value=None))
        st.enter_context(patch.object(rm, "_available_usdt", return_value=90_000.0))
        st.enter_context(patch.object(rm, "_portfolio_equity", return_value=equity))
        st.enter_context(patch.object(rm, "_equity_for_sizing", return_value=equity or 100_000.0))
        st.enter_context(patch.object(rm, "_initial_capital", return_value=100_000.0))
        st.enter_context(patch.object(rm, "_equity_drawdown_pct", return_value=0.0))
        st.enter_context(patch.object(rm, "_open_book_memory_counts", return_value=(0, 0, 0)))
        st.enter_context(patch.object(rm, "_process_uptime_sec", return_value=3600.0))
        st.enter_context(patch.object(rm, "_rule_switch_now", return_value=NOW))
        st.enter_context(
            patch("risk.risk_manager.load_trade_history",
                  return_value={"peak_equity": peak if peak is not None else 100_000.0})
        )
        st.enter_context(
            patch(
                "services.market_policy_fusion.get_global_market_bias",
                return_value={"block_buys": False, "size_mult": 1.0, "regime": "NEUTRAL"},
            )
        )
        for p in extra:
            st.enter_context(p)
        return rm.evaluate(order, "4h", source=source)


def _rm(switches=None, market=None) -> RiskManager:
    return RiskManager(_cfg(switches), market_service=market or _Market())


def _on(rule: str, **kw) -> dict:
    return {rule: {"enabled": True, **kw}}


# --- AC1 defaults ---------------------------------------------------------------------


def test_ac1_defaults_off():
    raw = _cfg().raw
    for rule in rs.RULES:
        assert rs.rule_config(raw, rule)["enabled"] is False
    assert not rs.limits_config(raw).active
    assert rs.limits_config({"rule_switches": {"limits": {"max_open_positions": 3}}}) == rs.Limits()
    assert rs.rule_config({"rule_switches": {"trend_filter": {"enabled": "false"}}}, "trend_filter")["enabled"] is False


def test_ac1_shipped_config_has_no_switches_on():
    raw = get_config()
    for rule in rs.RULES:
        assert rs.rule_config(raw, rule)["enabled"] is False


def test_ac1_buy_unchanged_without_block():
    m_off = _Market()
    base = _evaluate(_rm(None, m_off), _order())
    disabled = _evaluate(
        _rm({r: {"enabled": False} for r in rs.RULES}, _Market()), _order()
    )
    assert base.approved and disabled.approved
    assert base.order.usdt_amount == pytest.approx(disabled.order.usdt_amount)
    assert m_off.calls == []  # no candle fetch when nothing is on


# --- AC2 trend filter -----------------------------------------------------------------


def test_ac2_trend_filter_below_sma_rejected():
    m = _Market(daily=_daily(_uptrend_daily(last=95.0)))
    d = _evaluate(_rm(_on("trend_filter"), m), _order())
    assert not d.approved and d.code == "trend_filter_below_sma"
    assert _evaluate(_rm(None, m), _order()).approved


def test_ac2_trend_filter_extended_rejected():
    m = _Market(daily=_daily(_uptrend_daily(last=130.0)))
    d = _evaluate(_rm(_on("trend_filter"), m), _order())
    assert not d.approved and d.code == "trend_filter_extended"


def test_ac2_trend_filter_pump_24h_rejected():
    closes = [100.0] * 47 + [125.0] * 25  # last closed close 125 vs 100 24 bars earlier
    closes[-26] = 100.0
    m = _Market(hourly=_hourly(closes))
    d = _evaluate(_rm(_on("trend_filter"), m), _order(price=125.0))
    assert not d.approved and d.code == "trend_filter_pump_24h"


def test_ac2_trend_filter_pass_and_missing_data_kept():
    assert _evaluate(_rm(_on("trend_filter")), _order()).approved
    short = _Market(daily=_daily([100.0] * 20))
    assert _evaluate(_rm(_on("trend_filter"), short), _order()).approved
    gap = _daily(_uptrend_daily(last=95.0))
    del gap[10]
    assert _evaluate(_rm(_on("trend_filter"), _Market(daily=gap)), _order()).approved


def test_ac2_trend_filter_skips_dca_adds():
    m = _Market(daily=_daily(_uptrend_daily(last=95.0)))
    pos = {"amount": 10.0, "average_entry": 100.0, "dca_rounds": 0}
    d = _evaluate(
        _rm(_on("trend_filter"), m), _order(signal="BUY_DCA", source="dca"), source="dca", position=pos
    )
    assert d.code != "trend_filter_below_sma"


# --- AC3 no-chase ---------------------------------------------------------------------


def test_ac3_no_chase_rejects_above_limit():
    m = _Market()
    d = _evaluate(_rm(_on("no_chase"), m), _order(price=116.0))
    assert not d.approved and d.code == "no_chase_48h"
    assert _evaluate(_rm(None, _Market()), _order(price=116.0)).approved


def test_ac3_no_chase_allows_within_limit_and_short_history():
    assert _evaluate(_rm(_on("no_chase")), _order(price=114.0)).approved
    short = _Market(hourly=_hourly([100.0] * 20))
    assert _evaluate(_rm(_on("no_chase"), short), _order(price=200.0)).approved


def test_ac3_no_chase_custom_limit():
    d = _evaluate(_rm(_on("no_chase", max_above_48h_low_pct=10)), _order(price=112.0))
    assert d.code == "no_chase_48h"


# --- AC4 vol sizing -------------------------------------------------------------------


def _wiggle(step: float) -> list[float]:
    out, px = [], 100.0
    for i in range(72):
        px *= (1 + step) if i % 2 == 0 else (1 - step)
        out.append(px)
    return out


def test_ac4_vol_sizing_scales_size():
    closes = _wiggle(0.03)
    m = _Market(hourly=_hourly(closes))
    off = _evaluate(_rm(None, _Market(hourly=_hourly(closes))), _order())
    on = _evaluate(_rm(_on("vol_sizing"), m), _order())
    rv = rs.realized_daily_vol(rs.closed_bars(m.hourly, NOW, H))
    mult = min(1.0, 0.05 / rv)
    assert 0 < mult < 1
    assert on.approved and off.approved
    assert on.order.usdt_amount == pytest.approx(off.order.usdt_amount * mult, abs=0.01)


def test_ac4_vol_sizing_low_vol_or_missing_unchanged():
    off = _evaluate(_rm(None), _order())
    low = _evaluate(_rm(_on("vol_sizing"), _Market(hourly=_hourly(_wiggle(0.001)))), _order())
    missing = _evaluate(_rm(_on("vol_sizing"), _Market(hourly=_hourly([100.0] * 10))), _order())
    assert low.order.usdt_amount == pytest.approx(off.order.usdt_amount)
    assert missing.order.usdt_amount == pytest.approx(off.order.usdt_amount)


def test_ac4_vol_sizing_applies_to_dca_adds():
    closes = _wiggle(0.03)
    pos = {"amount": 1.0, "average_entry": 100.0, "dca_rounds": 0}
    kw = dict(source="dca", position=pos)
    off = _evaluate(_rm(None, _Market(hourly=_hourly(closes))), _order(signal="BUY_DCA", source="dca"), **kw)
    on = _evaluate(_rm(_on("vol_sizing"), _Market(hourly=_hourly(closes))),
                   _order(signal="BUY_DCA", source="dca"), **kw)
    assert off.approved and on.approved
    assert on.order.usdt_amount < off.order.usdt_amount


def test_vol_multiplier_formula():
    assert rs.vol_multiplier(0.10, 0.05) == pytest.approx(0.5)
    assert rs.vol_multiplier(0.02, 0.05) == 1.0
    assert rs.vol_multiplier(None, 0.05) == 1.0
    assert rs.vol_multiplier(0.0, 0.05) == 1.0


# --- AC5 limits -----------------------------------------------------------------------


def test_ac5_limit_max_usdt_per_order():
    d = _evaluate(_rm(_on("limits", max_usdt_per_order=700)), _order(usdt=2000))
    assert d.approved and d.order.usdt_amount <= 700 + 1e-6
    # Off: the shipped ticket cap (2500) is the only cap.
    assert _evaluate(_rm(None), _order(usdt=2000)).order.usdt_amount > 700


def test_ac5_limit_max_open_positions():
    sw = _on("limits", max_open_positions=20)
    d = _evaluate(_rm(sw), _order(), open_slots=20)
    assert not d.approved and d.code == "tenant_max_open_positions"
    assert _evaluate(_rm(sw), _order(), open_slots=19).approved
    assert _evaluate(_rm(None), _order(), open_slots=20).approved


def test_ac5_limit_max_position_pct():
    sw = _on("limits", max_position_pct=8)
    pos = {"amount": 80.0, "average_entry": 100.0, "dca_rounds": 0}  # 8,000 = 8 %
    d = _evaluate(_rm(sw), _order(signal="BUY_DCA", source="dca"), source="dca", position=pos)
    assert not d.approved and d.code == "max_position_percent"
    pos2 = {"amount": 70.0, "average_entry": 100.0, "dca_rounds": 0}  # room 1,000
    d2 = _evaluate(_rm(sw), _order(signal="BUY_DCA", source="dca"), source="dca", position=pos2)
    assert d2.approved and d2.order.usdt_amount <= 1000 + 1e-6
    d3 = _evaluate(_rm(None), _order(signal="BUY_DCA", source="dca"), source="dca", position=pos)
    assert d3.code != "max_position_percent"


def test_ac5_limit_max_lots_per_coin():
    sw = _on("limits", max_lots_per_coin=1)
    lots = [("1h", {"symbol": SYM, "amount": 1.0, "side": "long"})]
    d = _evaluate(_rm(sw), _order(), lots=lots)
    assert not d.approved and d.code == "tenant_max_lots_per_coin"
    shorts = [("1h", {"symbol": SYM, "amount": 1.0, "side": "short"})]
    assert _evaluate(_rm(sw), _order(), lots=shorts).approved
    assert _evaluate(_rm(sw), _order(), lots=[]).approved


def test_ac5_limit_drawdown_brake():
    sw = _on("limits", drawdown_brake_pct=12)
    d = _evaluate(_rm(sw), _order(), equity=87_000.0, peak=100_000.0)
    assert not d.approved and d.code == "tenant_drawdown_brake"
    assert _evaluate(_rm(sw), _order(), equity=89_000.0, peak=100_000.0).approved
    unknown = _evaluate(_rm(sw), _order(), equity=None, peak=100_000.0)
    assert not unknown.approved and unknown.code == "tenant_drawdown_brake"
    assert _evaluate(_rm(None), _order(), equity=87_000.0, peak=100_000.0).code != "tenant_drawdown_brake"


def test_ac5_dca_adds_pass_open_lots_and_brake():
    sw = _on("limits", max_open_positions=20, max_lots_per_coin=1, drawdown_brake_pct=12)
    pos = {"amount": 1.0, "average_entry": 100.0, "dca_rounds": 0}
    lots = [("4h", {"symbol": SYM, "amount": 1.0, "side": "long"})]
    d = _evaluate(
        _rm(sw), _order(signal="BUY_DCA", source="dca"), source="dca", position=pos,
        open_slots=30, lots=lots, equity=80_000.0, peak=100_000.0,
    )
    assert d.code not in ("tenant_max_open_positions", "tenant_max_lots_per_coin", "tenant_drawdown_brake")


def test_ac5_limits_parse_and_clamp():
    lim = rs.limits_config({"rule_switches": {"limits": {
        "enabled": True, "max_usdt_per_order": "2500", "max_open_positions": 20.0,
        "max_position_pct": 500, "max_lots_per_coin": 0, "drawdown_brake_pct": -1}}})
    assert lim.max_usdt_per_order == 2500 and lim.max_open_positions == 20
    assert lim.max_position_pct == 100.0
    assert lim.max_lots_per_coin is None and lim.drawdown_brake_pct is None


# --- AC6 exits never blocked ------------------------------------------------------------

ALL_ON = {
    "trend_filter": {"enabled": True},
    "no_chase": {"enabled": True},
    "vol_sizing": {"enabled": True},
    "age_loss_exit": {"enabled": True, "include_locked": True},
    "limits": {"enabled": True, "max_usdt_per_order": 1, "max_open_positions": 1,
               "max_position_pct": 0.01, "max_lots_per_coin": 1, "drawdown_brake_pct": 0.01},
}


@pytest.mark.parametrize("signal,source", [("SELL_FULL", "auto"), ("STOP_LOSS", "auto"),
                                           ("SELL_FULL", "exit_ws"), ("SELL_FULL", "manual")])
def test_ac6_sells_never_blocked(signal, source):
    pos = {"amount": 5.0, "average_entry": 200.0, "symbol": SYM, "entry_at": "2026-01-01T00:00:00"}
    crash = _Market(hourly=_hourly([100.0] * 71 + [10.0]), daily=_daily([500.0] * 61))
    rm = _rm(ALL_ON, crash)
    with patch.object(rm, "_rule_switch_buy_gate", side_effect=AssertionError("gate on sell")):
        d = _evaluate(rm, _order(type_="SELL", signal=signal, source=source, amount=5.0, price=50.0),
                      source=source, position=pos, open_slots=99, equity=1.0, peak=100_000.0,
                      extra=[patch("risk.risk_manager.find_open_position_for_symbol",
                                   return_value=("4h", pos))])
    assert d.approved, d.message


def test_ac6_gate_ignores_non_buy():
    rm = _rm(ALL_ON)
    assert rm._rule_switch_buy_gate(_order(type_="SELL"), "auto", has_position=True, is_dca=False) == (None, {})


# --- AC7 no lookahead -------------------------------------------------------------------


def test_ac7_no_lookahead_forming_bars_ignored():
    # Forming 1h bar (and its low) and forming daily bar carry extreme values.
    closes = [100.0] * 72
    lows = [100.0] * 71 + [50.0]
    h = _hourly(closes[:-1] + [300.0], lows)
    dly = _daily(_uptrend_daily(105.0)[:-1] + [10.0])
    closed_h = rs.closed_bars(h, NOW, H)
    assert closed_h[-1][0] + H <= NOW and len(closed_h) == 71
    assert rs.closed_bars(dly, NOW, D)[-1][4] == 105.0
    rm = _rm({"trend_filter": {"enabled": True}, "no_chase": {"enabled": True}}, _Market(hourly=h, daily=dly))
    assert _evaluate(rm, _order(price=110.0)).approved


def test_ac7_no_lookahead_future_bars_ignored():
    future = _hourly([100.0] * 72, end_open=(NOW // H) * H + 10 * H)
    closed = rs.closed_bars(future, NOW, H)
    assert all(b[0] + H <= NOW for b in closed)
    base = rs.closed_bars(_hourly([100.0] * 72), NOW, H)
    assert closed[-1][0] == base[-1][0]


# --- AC8 parity with the harness -------------------------------------------------------


def test_ac8_parity_with_harness():
    efr = pytest.importorskip("tools.entry_filter_replay")
    from services.shadow_rules import pump_pct_48h

    closes_h = _wiggle(0.02)
    lows_h = [c * (0.97 if i == 40 else 0.995) for i, c in enumerate(closes_h)]
    h_ms = _hourly(closes_h, lows_h)
    h_s = [tuple([b[0] // 1000] + b[1:]) for b in h_ms]
    d_ms = _daily([100 + math.sin(i / 3) * 5 + i * 0.3 for i in range(61)])
    d_s = [tuple([b[0] // 1000] + b[1:]) for b in d_ms]
    hc = rs.closed_bars(h_ms, NOW, H)
    dc = rs.closed_bars(d_ms, NOW, D)
    above, ext, _ = efr._sma_above(d_s, NOW, 50)
    assert rs.sma_extension(dc, 50) == (above, pytest.approx(ext))
    assert rs.pump_24h_pct(hc) == pytest.approx(efr._pump_24h(h_s, NOW))
    assert rs.realized_daily_vol(hc) == pytest.approx(efr._rv_daily(h_s, NOW))
    assert rs.pump_above_low_pct(hc, 103.0, NOW) == pytest.approx(pump_pct_48h(h_s, 103.0, NOW))


# --- AC9 per-tenant isolation ----------------------------------------------------------


def test_ac9_tenant_isolation():
    m = _Market(daily=_daily(_uptrend_daily(last=95.0)))
    henry = _rm({**_on("trend_filter"), **_on("limits", max_open_positions=3)}, m)
    other = _rm(None, m)
    assert _evaluate(henry, _order(), open_slots=1).code == "trend_filter_below_sma"
    assert _evaluate(henry, _order(), open_slots=3).code == "tenant_max_open_positions"
    d = _evaluate(other, _order(), open_slots=10)
    assert d.approved
    assert m.calls and all(c[1] in ("1h", "1d") for c in m.calls)
    assert _evaluate(henry, _order(), open_slots=1).code == "trend_filter_below_sma"


# --- AC10 rule E -----------------------------------------------------------------------

E_ON = {"age_loss_exit": {"enabled": True}}


def _lot(age_days: float, avg=100.0, amount=3.0, **extra):
    from datetime import datetime, timezone

    entry = datetime.fromtimestamp(NOW - age_days * D, tz=timezone.utc).isoformat()
    return {"symbol": SYM, "amount": amount, "average_entry": avg, "entry_at": entry, "side": "long", **extra}


@pytest.mark.parametrize("age,price,due", [(15, 84.0, True), (14, 85.0, True), (13.9, 50.0, False),
                                            (30, 86.0, False)])
def test_ac10_age_loss_exit_condition(age, price, due):
    cfg = rs.rule_config({"rule_switches": E_ON}, "age_loss_exit")
    assert rs.age_loss_exit_due(cfg, _lot(age), price, now_unix=NOW)[0] is due


def test_ac10_age_loss_exit_short_and_empty_lots():
    cfg = rs.rule_config({"rule_switches": E_ON}, "age_loss_exit")
    assert not rs.age_loss_exit_due(cfg, _lot(30, side="short"), 10.0, now_unix=NOW)[0]
    assert not rs.age_loss_exit_due(cfg, _lot(30, amount=0), 10.0, now_unix=NOW)[0]
    assert not rs.age_loss_exit_due(cfg, {"amount": 1, "average_entry": 100}, 10.0, now_unix=NOW)[0]


def _orch(switches):
    from services.signal_orchestrator import SignalOrchestrator

    orch = SignalOrchestrator.__new__(SignalOrchestrator)
    orch.config = _cfg(switches)
    orch.trading = MagicMock()
    orch.trading.execute_order.return_value = MagicMock(executed=True)
    return orch


def _run_e(switches, lot, price, locked=False):
    orch = _orch(switches)
    with patch("services.signal_orchestrator.find_open_position_for_symbol", return_value=("4h", lot)), \
         patch("risk.rule_switches.time.time", return_value=NOW), \
         patch("strategies.position_lock.attach_lock_from_ledger", side_effect=lambda p, *a, **k: p), \
         patch("strategies.position_lock.auto_sell_blocked", return_value=(locked, "locked")):
        orch._try_age_loss_exit(SYM, price, "4h")
    return orch.trading.execute_order


def test_ac10_age_loss_exit_sells_full_lot():
    ex = _run_e(E_ON, _lot(20, amount=3.0), 80.0)
    ex.assert_called_once()
    order, tf = ex.call_args.args[:2]
    assert order.type == "SELL" and order.signal == "SELL_FULL"
    assert order.amount == 3.0 and order.source == order.exit_source == "age_loss_exit"
    assert tf == "4h" and ex.call_args.kwargs["source"] == "age_loss_exit"


def test_ac10_age_loss_exit_no_order_when_not_due_or_off():
    _run_e(E_ON, _lot(5), 50.0).assert_not_called()
    _run_e(E_ON, _lot(20), 90.0).assert_not_called()
    _run_e(None, _lot(20), 50.0).assert_not_called()
    _run_e({"age_loss_exit": {"enabled": False}}, _lot(20), 50.0).assert_not_called()


def test_ac10_age_loss_exit_locked_lot():
    _run_e(E_ON, _lot(20), 50.0, locked=True).assert_not_called()
    with_locked = {"age_loss_exit": {"enabled": True, "include_locked": True}}
    _run_e(with_locked, _lot(20), 50.0, locked=True).assert_called_once()


def _lock_override(switches, source, lot, price):
    rm = _rm(switches)
    with patch.object(rm, "_rule_switch_now", return_value=NOW):
        return rm._age_loss_exit_lock_override(_order(type_="SELL", price=price), lot, source)


def test_ac10_lock_override_only_for_age_loss_source():
    on = {"age_loss_exit": {"enabled": True, "include_locked": True}}
    assert _lock_override(on, "age_loss_exit", _lot(20), 50.0) is True
    assert _lock_override(on, "exit_ws", _lot(20), 50.0) is False
    assert _lock_override(on, "age_loss_exit", _lot(5), 50.0) is False
    assert _lock_override(E_ON, "age_loss_exit", _lot(20), 50.0) is False
    assert _lock_override(None, "age_loss_exit", _lot(20), 50.0) is False


def test_ac10_locked_sell_path_in_risk_manager():
    """A locked lot: age_loss_exit passes with include_locked; other auto sells stay blocked."""
    on = {"age_loss_exit": {"enabled": True, "include_locked": True}}
    lot = _lot(20, amount=3.0)
    lock = patch("strategies.position_lock.auto_sell_blocked", return_value=(True, "locked by owner"))
    attach = patch("strategies.position_lock.attach_lock_from_ledger", side_effect=lambda p, *a, **k: p)
    sell = _order(type_="SELL", signal="SELL_FULL", source="age_loss_exit", amount=3.0, price=50.0)
    d = _evaluate(_rm(on), sell, source="age_loss_exit", position=lot, extra=[lock, attach])
    assert d.approved and d.code != "position_locked", d.message
    lock2 = patch("strategies.position_lock.auto_sell_blocked", return_value=(True, "locked by owner"))
    attach2 = patch("strategies.position_lock.attach_lock_from_ledger", side_effect=lambda p, *a, **k: p)
    other = _order(type_="SELL", signal="SELL_FULL", source="exit_ws", amount=3.0, price=50.0)
    d2 = _evaluate(_rm(on), other, source="exit_ws", position=lot, extra=[lock2, attach2])
    assert not d2.approved and d2.code == "position_locked"


# --- AC11 manual -------------------------------------------------------------------------


def test_ac11_manual_buy_skips_entry_rules():
    m = _Market(daily=_daily(_uptrend_daily(last=95.0)), hourly=_hourly(_wiggle(0.03)))
    sw = {**_on("trend_filter"), **_on("no_chase"), **_on("vol_sizing"), **_on("limits", drawdown_brake_pct=5)}
    d = _evaluate(_rm(sw, m), _order(price=150.0, source="manual"), source="manual",
                  equity=80_000.0, peak=100_000.0)
    assert d.code not in ("trend_filter_below_sma", "no_chase_48h", "tenant_drawdown_brake")
    base = _evaluate(_rm(None, m), _order(price=150.0, source="manual"), source="manual",
                     equity=80_000.0, peak=100_000.0)
    assert d.approved == base.approved
    if d.approved:
        assert d.order.usdt_amount == pytest.approx(base.order.usdt_amount)


def test_rule_switch_module_has_no_coin_literals():
    import pathlib
    import re

    src = pathlib.Path(rs.__file__).read_text()
    assert not re.search(r"\b(BTC|ETH|SOL|XRP|DOGE)\b", src)
