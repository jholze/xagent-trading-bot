"""Stop hole #695: absolute loss cap, grace-only partial pause, guard escalation.

Fixtures are the 2026-10-15 lots (stable mid-cap, one volatile guard case).
Strategy params are what the staging resolver yields for a stable mid-cap lot:
no stop_loss_pct (falls back to config.json stop_loss_pct), dca block =
config.json mid_cap_defaults.dca.

The cap value is the one in config.json (20). The first F1 case sits just
above that cap because the stop fires on a strict greater-than.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta

import pytest

import data_manager as _dm_mod
from strategies.dca import (
    effective_stop_loss_thresholds,
    evaluate_long_hard_stop,
    should_pause_partial_stop,
)
from strategies.sell_profile import overlay_volatile_sell

# The unit fixture replaces save_config with an in-memory writer. These
# tests call the real validator path.
_REAL_SAVE_CONFIG = _dm_mod.save_config

with open("config.json", encoding="utf-8") as fh:
    _CFG = json.load(fh)

BASE_SL = float(_CFG["stop_loss_pct"])
MID_CAP_DCA = dict(_CFG["mid_cap_defaults"]["dca"])
VOLATILE_CFG = dict(_CFG["volatile_altcoin"])

CAP_KEY = "long_hard_stop_max_loss_pct"
CAP = float(_CFG["risk"][CAP_KEY])


def _mid_cap_stable_params(**extra) -> dict:
    p = {"volatility_tier": "stable", "strategy_profile": "stable_altcoin", "dca": dict(MID_CAP_DCA)}
    p.update(extra)
    return p


def henry_2z_lot() -> dict:
    return {
        "symbol": "2Z/USDT",
        "timeframe": "1h",
        "average_entry": 0.05906740314035669,
        "entry_price": 0.05906740314035669,
        "last_buy_price": 0.046182179999999996,
        "amount": 37307.613384722994,
        "dca_rounds": 2,
        "last_dca_at": "2026-10-02T22:16:00.916160",
        "strategy_tier": "stable",
        "entry_source": "entry_sensor_15m",
        "sold_percent": 0.0,
        "lock": None,
    }


HENRY_2Z_LAST = 0.03365  # Gate last 15.10 18:12 UTC -> -43.0 %


def default_arc_lot() -> dict:
    q = 1259.4999999999998 / 0.07524018 + 800.0 / 0.07020012
    avg = (1259.4999999999998 + 800.0) / q
    return {
        "symbol": "ARC/USDT",
        "timeframe": "1h",
        "average_entry": avg,
        "entry_price": avg,
        "amount": q,
        "dca_rounds": 1,
        "last_dca_at": "2026-10-02T20:31:40",
        "strategy_tier": "stable",
        "entry_source": "gainer_relvol",
        "sold_percent": 0.0,
        "lock": None,
    }


def _px_at_loss(lot: dict, loss_pct: float) -> float:
    return lot["average_entry"] * (1 - loss_pct / 100.0)


# --------------------------------------------------------------------------
# Controls: describe staging as it is. Must stay GREEN before and after fix.
# --------------------------------------------------------------------------

def test_ctrl_arc_fired_at_real_exit_price():
    lot = default_arc_lot()
    hit = evaluate_long_hard_stop(
        price=0.03652, entry=lot["average_entry"], position=lot,
        strategy_params=_mid_cap_stable_params(), base_stop_loss_pct=BASE_SL,
    )
    assert hit == ("SELL_STOP_FULL", "stop_loss")


def test_ctrl_partial_paused_inside_dca_grace():
    lot = henry_2z_lot()
    lot["last_dca_at"] = (datetime.now() - timedelta(hours=2)).isoformat()
    assert should_pause_partial_stop(lot, _mid_cap_stable_params()) is True


def test_ctrl_grace_after_fresh_dca_still_suppresses():
    """Deliberate reversal (review C3), same kind as the :1545 partial flip.

    Grace pauses only the partial stop. It does not pause the hard cap.
    This lot is about -43 % with cap 20 and a DCA fill two hours ago, so
    the full stop fires during grace.
    """
    lot = henry_2z_lot()
    lot["last_dca_at"] = (datetime.now() - timedelta(hours=2)).isoformat()
    hit = evaluate_long_hard_stop(
        price=HENRY_2Z_LAST, entry=lot["average_entry"], position=lot,
        strategy_params=_mid_cap_stable_params(**{CAP_KEY: CAP}), base_stop_loss_pct=BASE_SL,
    )
    assert hit == ("SELL_STOP_FULL", "stop_loss")


def test_ctrl_no_cap_configured_keeps_today_full_stop():
    full, _p, _g = effective_stop_loss_thresholds(henry_2z_lot(), _mid_cap_stable_params(), BASE_SL)
    assert full == BASE_SL


# --------------------------------------------------------------------------
# F2: partial stop is paused forever once any DCA round was used
#     (strategies/dca.py:371). Docstring says "while a loser still has unused
#     DCA rounds". henry 2Z used 2/2 rounds, last DCA 13 days ago.
# --------------------------------------------------------------------------

def test_f2_partial_resumes_when_dca_rounds_exhausted_and_grace_over():
    lot = henry_2z_lot()
    params = _mid_cap_stable_params()
    assert should_pause_partial_stop(lot, params) is False
    hit = evaluate_long_hard_stop(
        price=HENRY_2Z_LAST, entry=lot["average_entry"], position=lot,
        strategy_params=params, base_stop_loss_pct=BASE_SL,
    )
    assert hit == ("SELL_STOP_PARTIAL", "stop_loss"), (
        f"henry 2Z at -43 % with 2/2 DCA rounds, grace long over, got {hit}"
    )


def test_f2_arc_partial_not_paused_forever_when_dca_blocked_below_avg():
    """ARC: 1/2 DCA rounds used on 02.10; the 2nd round never comes because
    dca_guard_below_avg rejects every DCA while live < avg. Staging keeps the
    partial paused ("rounds remain") -> no stop attempt between -33.5 % and -50 %."""
    lot = default_arc_lot()
    hit = evaluate_long_hard_stop(
        price=_px_at_loss(lot, 35.0), entry=lot["average_entry"], position=lot,
        strategy_params=_mid_cap_stable_params(), base_stop_loss_pct=BASE_SL,
    )
    assert hit == ("SELL_STOP_PARTIAL", "stop_loss"), f"ARC at -35 % got {hit}"


# --------------------------------------------------------------------------
# F3: partial_sell_guard (risk/risk_manager.py:3966-3975, used at :502-504)
#     blocks a hard-stop partial on small lots; no escalation to full.
#     Real case: default TRUST/USDT (volatile, 2172 units, avg 0.06840654,
#     ~113 USDT) rejected 48x on 15.10 with "position $107 below minimum $500".
# --------------------------------------------------------------------------

class _CfgStub:
    def __init__(self, raw):
        self.raw = raw
        self.risk_config = raw.get("risk") or {}
        self.cmc_config = raw.get("cmc") or {}

    def strategy_params(self, symbol, tf):
        return {}


def _guard(monkeypatch, lot, order):
    import risk.risk_manager as rm_mod
    from risk.risk_manager import RiskManager

    monkeypatch.setattr(rm_mod, "get_position", lambda sym, tf=None, *a, **k: dict(lot))
    monkeypatch.setattr(
        "strategies.registry.resolve_strategy_params",
        lambda *a, **k: {"volatility_tier": "volatile"},
    )
    rm = RiskManager.__new__(RiskManager)
    rm.config = _CfgStub(_CFG)
    return rm._partial_sell_blocked(order, "1h", order.source)


def _trust_lot():
    return {"symbol": "TRUST/USDT", "timeframe": "1h", "amount": 2172.0472925541912,
            "average_entry": 0.06840654, "sold_percent": 0.0, "dca_rounds": 0,
            "strategy_tier": "volatile"}


def _order(signal, *, price, qty, exit_source="stop_loss", source="exit_ws"):
    from core.models import TradeOrder

    o = TradeOrder(type="SELL", symbol="TRUST/USDT", price=price, signal=signal,
                   source=source, exit_source=exit_source)
    o.amount = qty
    return o


def test_f3_guard_blocked_hard_partial_escalates_to_full(monkeypatch):
    lot = _trust_lot()
    order = _order("SELL_STOP_PARTIAL", price=0.0505, qty=lot["amount"] * 0.5)
    blocked, why = _guard(monkeypatch, lot, order)
    assert blocked is False, f"stop exit failed at partial guard: {why}"
    assert order.signal == "SELL_STOP_FULL"
    assert order.amount == pytest.approx(lot["amount"])


def test_f3_small_live_ticket_25usdt_hard_partial_escalates(monkeypatch):
    # Lena: Klein-Live tickets 20-30 USDT can never pass 500/300 minimums.
    lot = {"symbol": "TRUST/USDT", "amount": 500.0, "average_entry": 0.06, "sold_percent": 0.0}
    order = _order("SELL_STOP_PARTIAL", price=0.05, qty=250.0)
    blocked, why = _guard(monkeypatch, lot, order)
    assert blocked is False, why
    assert order.signal == "SELL_STOP_FULL" and order.amount == pytest.approx(500.0)


def test_ctrl_f3_normal_partial_sell_still_guarded(monkeypatch):
    # Guard must keep blocking non-stop partial sells (no blanket bypass).
    lot = _trust_lot()
    order = _order("SELL_30", price=0.0505, qty=lot["amount"] * 0.3, exit_source="rsi", source="auto")
    blocked, _why = _guard(monkeypatch, lot, order)
    assert blocked is True
    assert order.signal == "SELL_30"


def test_ctrl_f3_non_stop_signal_with_stop_source_not_escalated(monkeypatch):
    # Only SELL_STOP_PARTIAL escalates, even if exit_source says stop_loss.
    lot = _trust_lot()
    order = _order("SELL_30", price=0.0505, qty=lot["amount"] * 0.3)
    blocked, _why = _guard(monkeypatch, lot, order)
    assert blocked is True
    assert order.signal == "SELL_30"


def test_ctrl_f3_manual_or_relabelled_partial_not_escalated(monkeypatch):
    # A SELL_STOP_PARTIAL without exit_source=stop_loss is not proof of a stop.
    lot = _trust_lot()
    order = _order("SELL_STOP_PARTIAL", price=0.0505, qty=lot["amount"] * 0.5, exit_source="", source="auto")
    blocked, _why = _guard(monkeypatch, lot, order)
    assert blocked is True
    assert order.signal == "SELL_STOP_PARTIAL"


# --------------------------------------------------------------------------
# F1: no absolute loss cap. Full stop = stop_loss_pct(50) + rounds*widen,
#     so ARC/2Z can only exit at >= -50 %.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("loss_pct", [CAP + 0.5, 30.0, 43.0])
def test_f1_henry_2z_full_stop_respects_cap(loss_pct):
    lot = henry_2z_lot()
    hit = evaluate_long_hard_stop(
        price=_px_at_loss(lot, loss_pct), entry=lot["average_entry"], position=lot,
        strategy_params=_mid_cap_stable_params(**{CAP_KEY: CAP}), base_stop_loss_pct=BASE_SL,
    )
    assert hit == ("SELL_STOP_FULL", "stop_loss"), f"2Z at -{loss_pct} % got {hit}"


def test_f1_arc_would_have_exited_at_cap_not_at_minus_50():
    lot = default_arc_lot()
    params = _mid_cap_stable_params(**{CAP_KEY: CAP})
    first = None
    for i in range(0, 5100):
        loss = i / 100.0
        hit = evaluate_long_hard_stop(
            price=_px_at_loss(lot, loss), entry=lot["average_entry"], position=lot,
            strategy_params=params, base_stop_loss_pct=BASE_SL,
        )
        if hit == ("SELL_STOP_FULL", "stop_loss"):
            first = loss
            break
    assert first is not None and first <= CAP + 0.01, f"ARC first full stop at -{first} %"


def test_f1_cap_beats_dca_widen_volatile():
    # henry QNT-like: volatile, 3 DCA rounds, widen 6 -> 68 % on staging.
    lot = {"average_entry": 268.499, "dca_rounds": 3, "last_dca_at": "2026-10-03T20:00:34"}
    params = {"dca": dict(VOLATILE_CFG["dca"]), "partial_stop_pct": 25, CAP_KEY: CAP}
    full, _p, _g = effective_stop_loss_thresholds(lot, params, BASE_SL)
    assert full <= CAP


# --------------------------------------------------------------------------
# F4: volatile partial_stop_pct=25 only reaches the pure volatile profile;
#     the overlay path (explicit / Hermes rows) drops it -> 33.5 %.
# --------------------------------------------------------------------------

def test_f4_volatile_overlay_carries_stop_keys():
    merged = overlay_volatile_sell({}, VOLATILE_CFG, tier="volatile", symbol="X/USDT", tf="1h")
    assert merged.get("partial_stop_pct") == VOLATILE_CFG.get("partial_stop_pct")


def test_f1_resolver_config_cap_limits_dca_lot():
    """The real config.json reaches a 2-round DCA lot through the resolver."""
    from core.config import BotConfig
    from strategies.registry import resolve_strategy_params

    assert _CFG["risk"][CAP_KEY] == 20
    cfg = BotConfig(raw=copy.deepcopy(_CFG))
    params = resolve_strategy_params(
        {"symbol": "AAA/USDT", "timeframe": "1h"},
        has_position=True,
        frozen_tier="stable",
        cfg=cfg,
    )
    assert params.get(CAP_KEY) == 20
    lot = {
        "average_entry": 1.0,
        "dca_rounds": 2,
        "last_dca_at": "2020-01-01T00:00:00",
    }
    full, _partial, _grace = effective_stop_loss_thresholds(
        lot, params, cfg.stop_loss_pct
    )
    assert full == 20


def test_f1_cycle_path_full_stops_henry_dca_lot():
    """The cycle stop in technical_rsi_bb uses resolver params.

    Henry's DCA'd long (2Z-like, about -43 %) is past the cap and short of
    the uncapped full stop. The cap has to arrive through the resolver;
    the cycle does not add one of its own.
    """
    from core.config import BotConfig
    from core.models import MarketContext
    from core.tenant_context import tenant_context
    from strategies.registry import resolve_strategy_params
    from strategies.technical_rsi_bb import TechnicalRSIStrategy

    lot = henry_2z_lot()
    loss = (1 - HENRY_2Z_LAST / lot["average_entry"]) * 100.0
    assert loss > CAP
    assert loss < BASE_SL

    cfg = BotConfig(raw=copy.deepcopy(_CFG))
    with tenant_context("henry"):
        params = resolve_strategy_params(
            {"symbol": lot["symbol"], "timeframe": lot["timeframe"]},
            has_position=True,
            frozen_tier=lot["strategy_tier"],
            cfg=cfg,
        )
        assert params.get(CAP_KEY) == CAP
        market = MarketContext(
            symbol=lot["symbol"],
            timeframe=lot["timeframe"],
            current_price=HENRY_2Z_LAST,
            rsi=40.0,
            lower_bb=HENRY_2Z_LAST * 0.9,
            vol_multiplier=1.0,
            has_position=True,
            open_positions=1,
            average_entry=lot["average_entry"],
            sim_state={
                "last_rsi": 45.0,
                "dca_rounds": lot["dca_rounds"],
                "last_dca_at": lot["last_dca_at"],
                "average_entry": lot["average_entry"],
                "amount": lot["amount"],
            },
            strategy_params=params,
        )
        result = TechnicalRSIStrategy().analyze(
            {"symbol": lot["symbol"], "timeframe": lot["timeframe"]},
            market,
        )
    assert result.action == "SELL_STOP_FULL"
    assert result.long_hard_stop is True
    assert "stop_loss" in result.sources


def _logs(monkeypatch):
    import data_manager as dm
    import logger

    found = []

    def _rec(message, level="INFO"):
        found.append((level, str(message)))

    monkeypatch.setattr(dm, "log", _rec)
    monkeypatch.setattr(logger, "log", _rec)
    return found


def test_f1_cap_key_missing_rejected_at_start(monkeypatch):
    import data_manager as dm
    from core.config_guardrails import refuse_start_if_stop_cap_invalid

    bad = copy.deepcopy(_CFG)
    bad["risk"].pop(CAP_KEY)
    logs = _logs(monkeypatch)
    with pytest.raises(SystemExit):
        refuse_start_if_stop_cap_invalid(bad)
    assert any(level == "ERROR" for level, _msg in logs)

    monkeypatch.setattr(dm, "_load_default_config_from_disk", lambda: copy.deepcopy(bad))
    dm._config_cache = None
    with pytest.raises(SystemExit):
        dm.load_config()
    assert dm._config_cache is None


def test_f1_cap_key_invalid_rejected_at_start(monkeypatch):
    import data_manager as dm

    _logs(monkeypatch)
    for bad_cap in (0, -5, 60, "abc"):
        bad = copy.deepcopy(_CFG)
        bad["risk"][CAP_KEY] = bad_cap

        def _load(body=bad):
            return copy.deepcopy(body)

        monkeypatch.setattr(dm, "_load_default_config_from_disk", _load)
        dm._config_cache = None
        with pytest.raises(SystemExit):
            dm.load_config()
        assert dm._config_cache is None


def test_f1_reload_without_cap_keeps_last_valid(monkeypatch):
    import data_manager as dm

    live = dm._config_cache
    assert live["risk"][CAP_KEY] == 20
    snapshot = copy.deepcopy(live)
    bad = copy.deepcopy(live)
    bad["risk"].pop(CAP_KEY)
    bad["stop_loss_pct"] = 11.0
    bad["max_usdt_per_trade"] = 1
    bad["marker_from_rejected_reload"] = True
    logs = _logs(monkeypatch)
    monkeypatch.setattr(dm, "_load_default_config_from_disk", lambda: copy.deepcopy(bad))
    reloaded = dm.reload_config()
    assert reloaded is live
    assert dm._config_cache is live
    assert reloaded["risk"][CAP_KEY] == 20
    assert reloaded["stop_loss_pct"] == snapshot["stop_loss_pct"]
    assert reloaded["max_usdt_per_trade"] == snapshot["max_usdt_per_trade"]
    assert "marker_from_rejected_reload" not in reloaded
    assert any(level == "ERROR" for level, _msg in logs)


def test_f1_tenant_overlay_cap_60_rejected(monkeypatch):
    import data_manager as dm

    base = copy.deepcopy(_CFG)
    assert base["risk"][CAP_KEY] == 20
    monkeypatch.setattr(dm, "_load_default_config_from_disk", lambda: copy.deepcopy(base))
    monkeypatch.setattr(dm, "_should_use_mongo_for_tenant_config", lambda cfg=None: True)
    logs = _logs(monkeypatch)

    def _install(overlay):
        def _load(tid, default_cfg):
            return copy.deepcopy(overlay)

        monkeypatch.setattr(dm, "_load_tenant_config_body", _load)

    rejected = (
        {"risk": {CAP_KEY: 60}, "marker": "overlay-60"},
        {"risk": {CAP_KEY: None}, "marker": "overlay-removed"},
        {"risk": {CAP_KEY: 0}, "marker": "overlay-zero"},
    )
    for overlay in rejected:
        logs.clear()
        _install(overlay)
        cfg = dm.load_config(tenant_id="t695")
        assert cfg["risk"][CAP_KEY] == 20
        assert "marker" not in cfg
        assert any(level == "ERROR" for level, _msg in logs)

    _install({"risk": {CAP_KEY: 15}, "marker": "ok"})
    ok = dm.load_config(tenant_id="t695")
    assert ok["risk"][CAP_KEY] == 15
    assert ok["marker"] == "ok"

    _install({"risk": {"max_daily_buys": 7}, "marker": "partial"})
    partial = dm.load_config(tenant_id="t695")
    assert partial["risk"][CAP_KEY] == 20
    assert partial["risk"]["max_daily_buys"] == 7
    assert partial["marker"] == "partial"


def _fresh_dca(lot: dict) -> dict:
    out = dict(lot)
    out["last_dca_at"] = (datetime.now() - timedelta(hours=2)).isoformat()
    return out


def test_cap_fires_during_dca_grace_at_minus_25():
    """Grace pauses a partial. A loss past the cap is still a full stop."""
    lot = _fresh_dca(henry_2z_lot())
    params = _mid_cap_stable_params(**{CAP_KEY: CAP})
    hit = evaluate_long_hard_stop(
        price=_px_at_loss(lot, 25.0), entry=lot["average_entry"], position=lot,
        strategy_params=params, base_stop_loss_pct=BASE_SL,
    )
    assert hit == ("SELL_STOP_FULL", "stop_loss")
    # Strict greater-than: exactly the cap does not fire, in grace or not.
    exact = evaluate_long_hard_stop(
        price=_px_at_loss(lot, CAP), entry=lot["average_entry"], position=lot,
        strategy_params=params, base_stop_loss_pct=BASE_SL,
    )
    assert exact is None
    # Past a tighter partial, short of the cap: grace still holds.
    tight = _mid_cap_stable_params(**{CAP_KEY: CAP, "partial_stop_pct": 10})
    held = evaluate_long_hard_stop(
        price=_px_at_loss(lot, 15.0), entry=lot["average_entry"], position=lot,
        strategy_params=tight, base_stop_loss_pct=BASE_SL,
    )
    assert held is None
    # No cap configured: grace still suppresses the widened full stop.
    wide = evaluate_long_hard_stop(
        price=_px_at_loss(lot, 90.0), entry=lot["average_entry"], position=lot,
        strategy_params=_mid_cap_stable_params(), base_stop_loss_pct=BASE_SL,
    )
    assert wide is None


def test_cap_measures_average_entry_after_dca_not_first_fill():
    """The cap is the loss versus the running average, not the first entry."""
    from services.exit_realtime.shadow_eval import evaluate_would_sells

    params = _mid_cap_stable_params(**{CAP_KEY: CAP, "stop_loss_pct": BASE_SL})
    # -25 % versus the average (1.00 → 0.75), only -6 % versus the first fill.
    lot = {
        "symbol": "AAA/USDT",
        "average_entry": 1.0,
        "entry_price": 0.80,
        "amount": 100.0,
        "dca_rounds": 2,
        "last_dca_at": "2020-01-01T00:00:00",
        "sold_percent": 0.0,
    }
    events = evaluate_would_sells(
        symbol="AAA/USDT",
        timeframe="1h",
        price=0.75,
        bid=0.75,
        position=lot,
        stop_position=lot,
        strategy_params=params,
        sources={"stop_loss"},
    )
    assert any(ev.get("action") == "SELL_STOP_FULL" for ev in events)
    # -15 % versus the average, -43 % versus a higher first fill. Not a full stop.
    other = dict(lot, entry_price=1.5)
    events = evaluate_would_sells(
        symbol="AAA/USDT",
        timeframe="1h",
        price=0.85,
        bid=0.85,
        position=other,
        stop_position=other,
        strategy_params=params,
        sources={"stop_loss"},
    )
    assert not any(ev.get("action") == "SELL_STOP_FULL" for ev in events)


def test_ws_book_dca_lot_below_cap_exits_full():
    """Hub book params reach the WS stop. Stripping the cap there stays partial or hold."""
    from core.config import BotConfig
    from services.exit_realtime.hub import _book_row_for_position
    from services.exit_realtime.shadow_eval import evaluate_would_sells

    lot = {
        "symbol": "AAA/USDT",
        "timeframe": "1h",
        "average_entry": 1.0,
        "entry_price": 1.0,
        "amount": 100.0,
        "dca_rounds": 2,
        "last_dca_at": "2020-01-01T00:00:00",
        "strategy_tier": "stable",
        "sold_percent": 0.0,
    }
    row = _book_row_for_position(
        key="AAA_USDT_1h",
        pos=lot,
        tenant_id="default",
        cfg=BotConfig(raw=copy.deepcopy(_CFG)),
    )
    assert row is not None
    assert row["strategy_params"].get(CAP_KEY) == CAP
    # -25 % is past the cap and short of the partial (33.5) and the base (50).
    events = evaluate_would_sells(
        symbol=row["symbol"],
        timeframe=row["timeframe"],
        price=0.75,
        bid=0.75,
        position=row["position"],
        stop_position=row["position"],
        strategy_params=row["strategy_params"],
        sources={"stop_loss"},
        atr_pct=row["atr_pct"],
    )
    assert any(ev.get("action") == "SELL_STOP_FULL" for ev in events)


def test_hub_fallback_params_carry_the_cap(monkeypatch):
    from core.config import BotConfig
    from services.exit_realtime import hub as hub_mod
    from services.exit_realtime.shadow_eval import evaluate_would_sells

    def _boom(*_a, **_k):
        raise RuntimeError("resolver down")

    monkeypatch.setattr("strategies.registry.resolve_strategy_params", _boom)
    lot = {
        "symbol": "AAA/USDT",
        "timeframe": "1h",
        "average_entry": 1.0,
        "amount": 100.0,
        "dca_rounds": 0,
        "strategy_tier": "stable",
        "sold_percent": 0.0,
    }
    row = hub_mod._book_row_for_position(
        key="AAA_USDT_1h",
        pos=lot,
        tenant_id="default",
        cfg=BotConfig(raw=copy.deepcopy(_CFG)),
    )
    assert row is not None
    assert row["strategy_params"].get(CAP_KEY) == CAP
    events = evaluate_would_sells(
        symbol="AAA/USDT",
        timeframe="1h",
        price=0.75,
        bid=0.75,
        position=row["position"],
        strategy_params=row["strategy_params"],
        sources={"stop_loss"},
    )
    assert any(ev.get("action") == "SELL_STOP_FULL" for ev in events)


def test_cycle_fallback_params_carry_the_cap():
    from core.models import MarketContext
    from strategies.technical_rsi_bb import TechnicalRSIStrategy

    market = MarketContext(
        symbol="AAA/USDT",
        timeframe="1h",
        current_price=0.75,
        rsi=40.0,
        lower_bb=0.1,
        vol_multiplier=1.0,
        has_position=True,
        open_positions=1,
        average_entry=1.0,
        sim_state={
            "last_rsi": 45.0,
            "dca_rounds": 2,
            "last_dca_at": "2020-01-01T00:00:00",
            "average_entry": 1.0,
            "entry_price": 0.8,
            "amount": 100.0,
        },
    )
    result = TechnicalRSIStrategy().analyze(
        {"symbol": "AAA/USDT", "timeframe": "1h"},
        market,
    )
    assert result.action == "SELL_STOP_FULL"
    assert result.long_hard_stop is True


def test_fallback_attach_does_not_invent_a_cap(monkeypatch):
    from core.config import BotConfig
    from strategies.registry import attach_long_hard_stop_cap

    raw = copy.deepcopy(_CFG)
    raw["risk"].pop(CAP_KEY)
    logs = _logs(monkeypatch)
    out = attach_long_hard_stop_cap({}, BotConfig(raw=raw))
    assert CAP_KEY not in out
    assert out.get(CAP_KEY) != 20
    assert any(level == "ERROR" and "fallback" in msg for level, msg in logs)


def test_resolver_does_not_invent_cap_when_risk_key_missing():
    from core.config import BotConfig
    from strategies.registry import resolve_strategy_params

    raw = copy.deepcopy(_CFG)
    raw["risk"].pop(CAP_KEY)
    params = resolve_strategy_params(
        {"symbol": "AAA/USDT", "timeframe": "1h"},
        has_position=True,
        frozen_tier="stable",
        cfg=BotConfig(raw=raw),
    )
    assert CAP_KEY not in params
    assert params.get(CAP_KEY) != 20


def test_startup_hook_refuses_invalid_stop_cap():
    import ast
    from pathlib import Path

    import aria_bot

    tree = ast.parse(Path(aria_bot.__file__).read_text(encoding="utf-8"))
    calls = []
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            func = node.value.func
            if isinstance(func, ast.Name):
                calls.append(func.id)
    assert "refuse_start_if_stop_cap_invalid" in calls


def test_disk_edit_reload_tenant_keeps_cap_20(monkeypatch):
    """A runtime-invalid config.json must not strip the tenant cap."""
    import data_manager as dm

    live = dm._config_cache
    assert live["risk"][CAP_KEY] == 20
    logs = _logs(monkeypatch)
    monkeypatch.setattr(dm, "_should_use_mongo_for_tenant_config", lambda cfg=None: True)

    def _install(overlay):
        monkeypatch.setattr(
            dm,
            "_load_tenant_config_body",
            lambda tid, default_cfg, body=overlay: copy.deepcopy(body),
        )

    cases = (
        ("missing", None),
        ("sixty", 60),
        ("zero", 0),
    )
    for name, bad_cap in cases:
        bad = copy.deepcopy(_CFG)
        if bad_cap is None:
            bad["risk"].pop(CAP_KEY)
        else:
            bad["risk"][CAP_KEY] = bad_cap
        monkeypatch.setattr(dm, "_load_default_config_from_disk", lambda body=bad: copy.deepcopy(body))
        logs.clear()
        reloaded = dm.reload_config()
        assert reloaded["risk"][CAP_KEY] == 20
        assert dm.get_config()["risk"][CAP_KEY] == 20
        assert not any("at start" in msg for _level, msg in logs)

        logs.clear()
        _install({"risk": {"max_daily_buys": 4}, "marker": f"henry-{name}"})
        tenant = dm.load_config(tenant_id="henry")
        assert tenant["risk"][CAP_KEY] == 20, name
        assert tenant["marker"] == f"henry-{name}"
        assert any(level == "ERROR" for level, _msg in logs)
        again = dm.reload_config(tenant_id="henry")
        assert again["risk"][CAP_KEY] == 20

        logs.clear()
        _install({"risk": {CAP_KEY: 60}, "marker": "overlay-60"})
        rejected = dm.load_config(tenant_id="henry")
        assert rejected["risk"][CAP_KEY] == 20
        assert "marker" not in rejected


def test_tenant_invalid_base_without_cache_does_not_return_overlay(monkeypatch):
    import data_manager as dm

    previous = dm._config_cache
    bad = copy.deepcopy(_CFG)
    bad["risk"].pop(CAP_KEY)
    logs = _logs(monkeypatch)
    monkeypatch.setattr(dm, "_load_default_config_from_disk", lambda: copy.deepcopy(bad))
    monkeypatch.setattr(dm, "_should_use_mongo_for_tenant_config", lambda cfg=None: True)
    monkeypatch.setattr(
        dm,
        "_load_tenant_config_body",
        lambda tid, default_cfg: {"risk": {CAP_KEY: 60}, "marker": "unvalidated"},
    )
    dm._config_cache = None
    try:
        with pytest.raises(SystemExit):
            dm.load_config(tenant_id="henry")
    finally:
        dm._config_cache = previous
    assert not any("unvalidated" in msg for _level, msg in logs)


def test_tenant_reload_drops_invalid_overlay(monkeypatch):
    import data_manager as dm

    monkeypatch.setattr(dm, "_load_default_config_from_disk", lambda: copy.deepcopy(_CFG))
    monkeypatch.setattr(dm, "_should_use_mongo_for_tenant_config", lambda cfg=None: True)
    monkeypatch.setattr(
        dm,
        "_load_tenant_config_body",
        lambda tid, default_cfg: {"risk": {CAP_KEY: 60}, "marker": "nope"},
    )
    _logs(monkeypatch)
    cfg = dm.reload_config(tenant_id="henry")
    assert cfg["risk"][CAP_KEY] == 20
    assert "marker" not in cfg


def test_save_without_cap_keeps_last_valid_config(monkeypatch):
    import data_manager as dm
    from core.config_guardrails import ConfigValidationError, validate_config_for_save

    wrote = []
    monkeypatch.setattr(dm, "atomic_write_json", lambda *a, **k: wrote.append(a))
    monkeypatch.setattr(dm, "save_config", _REAL_SAVE_CONFIG)
    logs = _logs(monkeypatch)
    before = dm.get_config()
    assert before["risk"][CAP_KEY] == 20
    bad = copy.deepcopy(_CFG)
    bad["risk"].pop(CAP_KEY)
    with pytest.raises(ConfigValidationError):
        _REAL_SAVE_CONFIG(bad)
    assert wrote == []
    assert dm.get_config() is before or dm.get_config()["risk"][CAP_KEY] == 20
    assert dm._config_cache is before
    assert any("on save" in msg for _level, msg in logs)
    assert not any("at start" in msg for _level, msg in logs)
    # Present but illegal values stay rejected and do not exit the process.
    for illegal in (0, 60):
        worse = copy.deepcopy(_CFG)
        worse["risk"][CAP_KEY] = illegal
        with pytest.raises(ConfigValidationError):
            _REAL_SAVE_CONFIG(worse)
    assert wrote == []
    assert dm.get_config()["risk"][CAP_KEY] == 20
    # A partial body with no stop_loss_pct is not this gate.
    validate_config_for_save({
        "max_open_positions": 5,
        "max_usdt_per_trade": 100,
        "trading_mode": "paper",
        "live": {"execution": "shadow", "max_usdt_per_trade": 100},
        "risk": {
            "liquidity_guard": {"depth_to_order_multiple_min": 1},
            "venue_quality": {"min_volume_to_order_multiple_min": 20},
        },
    })


def test_config_revert_to_pre_695_snapshot_rejected(monkeypatch):
    import data_manager as dm
    from notifications.telegram_commands import config_commands

    wrote = []
    monkeypatch.setattr(dm, "atomic_write_json", lambda *a, **k: wrote.append(a))
    monkeypatch.setattr(dm, "save_config", _REAL_SAVE_CONFIG)
    logs = _logs(monkeypatch)
    sent = []
    monkeypatch.setattr(config_commands, "send_telegram_message", lambda text, *a, **k: sent.append(text))
    pre = copy.deepcopy(_CFG)
    pre["risk"].pop(CAP_KEY)
    assert "stop_loss_pct" in pre
    snap = type("Snap", (), {"body": pre, "snapshot_id": "pre695", "n": 1})()
    monkeypatch.setattr("storage.config_history.get_snapshot_by_id", lambda tid, sid: snap)
    before_cap = dm.get_config()["risk"][CAP_KEY]
    config_commands._apply_revert(1, "default", "pre695")
    assert wrote == []
    assert sent
    assert dm.get_config()["risk"][CAP_KEY] == before_cap == 20
    assert any("on save" in msg for _level, msg in logs)
    assert not any("at start" in msg for _level, msg in logs)


def test_stop_partial_sold_percent_09999_escalates_to_full(monkeypatch):
    lot = _trust_lot()
    lot["amount"] = 100_000.0
    lot["sold_percent"] = 0.9999
    order = _order("SELL_STOP_PARTIAL", price=1.0, qty=1_000.0)
    blocked, why = _guard(monkeypatch, lot, order)
    assert blocked is False, why
    assert order.signal == "SELL_STOP_FULL"
    assert order.amount == pytest.approx(lot["amount"])


def test_escalation_skips_manual_order_source(monkeypatch):
    import risk.risk_manager as rm_mod
    from risk.risk_manager import RiskManager

    lot = _trust_lot()
    order = _order(
        "SELL_STOP_PARTIAL",
        price=0.0505,
        qty=lot["amount"] * 0.5,
        source="manual",
    )
    monkeypatch.setattr(rm_mod, "get_position", lambda sym, tf=None, *a, **k: dict(lot))
    monkeypatch.setattr(
        "strategies.registry.resolve_strategy_params",
        lambda *a, **k: {"volatility_tier": "volatile"},
    )
    rm = RiskManager.__new__(RiskManager)
    rm.config = _CfgStub(_CFG)
    blocked, _why = rm._partial_sell_blocked(order, "1h", "exit_ws")
    assert blocked is True
    assert order.signal == "SELL_STOP_PARTIAL"


def test_cycle_below_venue_min_full_stop_uses_cap(monkeypatch):
    from core.config import BotConfig
    from core.models import TradeOrder
    from risk.risk_manager import RiskManager
    from strategies.positions import (
        BELOW_VENUE_MINIMUM,
        clear_positions_memory,
        get_position,
        set_position_field,
        update_position,
    )

    symbol = "AAA/USDT"
    tf = "1h"
    clear_positions_memory()
    try:
        update_position(symbol, tf, "BUY", 1.0, 40.0)
        set_position_field(symbol, tf, "venue_min_state", BELOW_VENUE_MINIMUM)
        set_position_field(symbol, tf, "average_entry", 1.0)
        set_position_field(symbol, tf, "dca_rounds", 2)
        set_position_field(symbol, tf, "last_dca_at", "2020-01-01T00:00:00")
        set_position_field(symbol, tf, "strategy_tier", "stable")
        assert get_position(symbol, tf).get("venue_min_state") == BELOW_VENUE_MINIMUM
        rm = RiskManager(BotConfig(raw=copy.deepcopy(_CFG)))
        monkeypatch.setattr(rm, "_trade_cooldown_blocked", lambda *a, **k: (False, ""))
        monkeypatch.setattr(rm, "_refresh_venue_mark", lambda *a, **k: None)
        monkeypatch.setattr(rm, "_daily_sells_count", lambda *a, **k: 0)
        order = TradeOrder(
            type="SELL",
            symbol=symbol,
            price=0.75,
            signal="SELL_STOP_FULL",
            source="auto",
            exit_source="stop_loss",
        )
        order.amount = 40.0
        assert not hasattr(order, "_full_hard_stop_params") or getattr(order, "_full_hard_stop_params", None) is None
        decision = rm.evaluate(order, tf, source="auto")
        assert decision.code != "below_venue_minimum"
        assert order.signal == "SELL_STOP_FULL"
        assert order.amount == pytest.approx(40.0)
    finally:
        clear_positions_memory()


def test_dca_guard_below_avg_stays_on_for_henry_overlay(monkeypatch):
    from core.config import BotConfig
    from core.models import TradeOrder
    from risk.risk_manager import RiskManager

    raw = copy.deepcopy(_CFG)
    raw["dca_guard_below_avg"] = False
    raw["risk"]["dca_guard_below_avg"] = False
    raw["risk"]["disable_dca_guard"] = True
    rm = RiskManager(BotConfig(raw=raw))
    monkeypatch.setattr("strategies.dca_policy._price_is_stale", lambda *a, **k: False)
    monkeypatch.setattr(rm, "_open_cost_usdt", lambda *a, **k: (0.0, None))

    def _venue(*_a, **_k):
        raise RuntimeError("venue skipped")

    monkeypatch.setattr("services.venue_quality.check_venue_for_buy", _venue)
    pos = {
        "symbol": "AAA/USDT",
        "amount": 100.0,
        "average_entry": 1.0,
        "entry_price": 0.8,
        "dca_rounds": 1,
        "lock": None,
    }
    order = TradeOrder(
        type="BUY",
        symbol="AAA/USDT",
        price=0.75,
        signal="BUY_DCA",
        source="dca",
        usdt_amount=100.0,
    )
    result = rm._buy_lock_decision(
        order,
        source="dca",
        pos=pos,
        has_position=True,
        planned_usdt=100.0,
        indicators={"price_age_sec": 0, "rsi": 25},
    )
    assert result.decision is not None
    assert result.decision.approved is False
    codes = list((result.decision.details or {}).get("codes") or [])
    assert "dca_guard_below_avg" in codes
    assert result.decision.code == "dca_guard_below_avg"
