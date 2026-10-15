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

from strategies.dca import (
    effective_stop_loss_thresholds,
    evaluate_long_hard_stop,
    should_pause_partial_stop,
)
from strategies.sell_profile import overlay_volatile_sell

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
    lot = henry_2z_lot()
    lot["last_dca_at"] = (datetime.now() - timedelta(hours=2)).isoformat()
    hit = evaluate_long_hard_stop(
        price=HENRY_2Z_LAST, entry=lot["average_entry"], position=lot,
        strategy_params=_mid_cap_stable_params(**{CAP_KEY: CAP}), base_stop_loss_pct=BASE_SL,
    )
    assert hit is None


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
