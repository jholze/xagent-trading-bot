"""Pre-buy memory gate (risk/pre_trade_gate.py, plan memory_gate §2)."""

from __future__ import annotations

import copy
from contextlib import ExitStack
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from core.config import BotConfig
from core.models import TradeOrder
from data_manager import get_config
from risk import pre_trade_gate as ptg
from risk.risk_manager import RiskManager
from storage.order_ledger_v2 import MemoryOrderLedgerV2

NOW = datetime(2026, 11, 4, 12, 0, 0)
SYM = "ABC/USDT"

# Thresholds used by the tests. The module itself ships none.
SETTINGS = {
    "mode": "shadow",
    "lookback_days": 90,
    "pool_tenants": [],
    "loss_min_usdt": 1.0,
    "half_life_days": None,
    "block_min_losses": 2,
    "block_net_loss_usdt": 100.0,
    "recent_days": 14,
    "recent_loss_pct": 25.0,
    "recent_loss_usdt": 500.0,
    "reduce_min_losses": 1,
    "reduce_net_loss_usdt": 0.0,
    "reduce_mult": 0.5,
    "min_mult": 0.25,
    "cache_ttl_sec": 0,
    "facts": {
        "enabled": True,
        "lookback_hours": 336,
        "max_events": 40,
        "block_flags": ["hard_negative", "structure_risk", "unlock"],
        "reduce_mults": {"flow_only": 0.5, "profit_taking": 0.7},
    },
    "dca": {"sma_days": 50, "max_below_sma_pct": 15.0},
}


def _cfg_dict(**over) -> dict:
    block = copy.deepcopy(SETTINGS)
    for k, v in over.items():
        block[k] = v
    return {"rule_switches": {"pre_trade_gate": block}}


def _sell(pnl, *, days_ago=5.0, usdt=1000.0, tenant="default", sym=SYM, oid=None):
    ts = NOW - timedelta(days=days_ago)
    return {
        "id": oid or f"s{tenant}{pnl}{days_ago}",
        "tenant_id": tenant,
        "ledger_scope": "demo",
        "symbol": sym,
        "side": "sell",
        "status": "filled",
        "pnl": pnl,
        "execution": {"usdt": usdt},
        "source": "exit_ws",
        "day_key": ts.strftime("%Y-%m-%d"),
        "ts_event": ts.isoformat(),
    }


def _buy(*, days_ago=6.0, source="auto", signal="BUY", tenant="default", oid=None):
    ts = NOW - timedelta(days=days_ago)
    return {
        "id": oid or f"b{tenant}{source}{days_ago}",
        "tenant_id": tenant,
        "ledger_scope": "demo",
        "symbol": SYM,
        "side": "buy",
        "status": "filled",
        "source": source,
        "signal": signal,
        "execution": {"usdt": 1000.0},
        "day_key": ts.strftime("%Y-%m-%d"),
        "ts_event": ts.isoformat(),
    }


def _gate(rows=None, events=None, *, cfg=None, is_dca=False, ext=None, h_err=None, f_err=None):
    calls = {"h": [], "f": []}

    def hl(**kw):
        calls["h"].append(kw)
        if h_err:
            raise h_err
        return list(rows or [])

    def fl(**kw):
        calls["f"].append(kw)
        if f_err:
            raise f_err
        return list(events or [])

    res = ptg.evaluate_gate(
        symbol=SYM,
        source="auto",
        tenant_id="default",
        scope="demo",
        config=cfg if cfg is not None else _cfg_dict(),
        is_dca=is_dca,
        dca_sma_ext_pct=ext,
        now=NOW,
        history_loader=hl,
        facts_loader=fl,
    )
    return res, calls


# --- config -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "block,expected",
    [
        (None, "off"),
        ({}, "off"),
        ({"mode": None}, "off"),
        ({"mode": "shadow"}, "shadow"),
        ({"mode": " ENFORCE "}, "enforce"),
        ({"mode": "off"}, "off"),
        ({"mode": True}, "enforce"),
        ({"mode": False}, "off"),
        ({"mode": "shdow"}, "enforce"),  # malformed must not lift the guard
    ],
)
def test_gate_mode(block, expected):
    raw = {} if block is None else {"rule_switches": {"pre_trade_gate": block}}
    assert ptg.gate_mode(raw) == expected


def test_shipped_config_is_shadow_with_thresholds():
    raw = get_config()
    s = ptg.gate_settings(raw)
    assert s["mode"] == "shadow"
    assert s["lookback_days"] == 90
    assert s["facts"]["enabled"] is True
    assert s["facts"]["lookback_hours"] == 336
    for k in ("block_min_losses", "block_net_loss_usdt", "recent_loss_pct", "reduce_mult"):
        assert s[k] is not None


def test_module_has_no_thresholds():
    s = ptg.gate_settings({"rule_switches": {"pre_trade_gate": {"mode": "enforce"}}})
    rows = [_sell(-900, days_ago=1), _sell(-900, days_ago=2), _sell(-900, days_ago=3)]
    h = ptg.summarize_history(rows, now=NOW, settings=s)
    flags = {"hard_negative": True, "unlock": True, "event_count": 3}
    res = ptg.decide(h, flags, s, is_dca=True, dca_sma_ext_pct=-80.0)
    assert res.verdict == "allow"


def test_tenant_override_merges_sub_blocks():
    raw = _cfg_dict()
    raw["rule_switches"]["pre_trade_gate"]["facts"] = {"block_flags": ["hard_negative"]}
    s = ptg.gate_settings(raw)
    assert s["facts"]["block_flags"] == ["hard_negative"]
    # untouched sub keys keep the shipped value from the same block when present
    assert "reduce_mults" in s["facts"]


# --- history math -------------------------------------------------------------------


def test_summarize_history_metrics():
    rows = [
        _sell(50, days_ago=40),
        _sell(-120, days_ago=20, usdt=380.0),  # cost 500 -> -24 %
        _sell(-0.5, days_ago=10),  # below loss_min: neither win nor loss
        _buy(source="dca", days_ago=21),
        _buy(signal="BUY_DCA", source="auto", days_ago=22),
        _buy(source="auto", days_ago=41),
    ]
    h = ptg.summarize_history(rows, now=NOW, settings=SETTINGS)
    assert h.n_fills == 6 and h.n_closed == 3
    assert h.wins == 1 and h.losses == 1
    assert h.net_pnl_usdt == pytest.approx(-70.5)
    assert h.worst_loss_usdt == pytest.approx(-120)
    assert h.worst_loss_pct == pytest.approx(-24.0)
    assert h.dca_buys == 2
    assert h.recent_big_loss is False
    assert h.last_loss_ts.startswith((NOW - timedelta(days=20)).strftime("%Y-%m-%d"))


def test_recent_big_loss_by_pct_and_usdt():
    pct = ptg.summarize_history([_sell(-300, days_ago=3, usdt=700.0)], now=NOW, settings=SETTINGS)
    assert pct.recent_big_loss  # -30 %
    usd = ptg.summarize_history([_sell(-600, days_ago=3, usdt=9000.0)], now=NOW, settings=SETTINGS)
    assert usd.recent_big_loss  # -600 USDT
    old = ptg.summarize_history([_sell(-600, days_ago=30, usdt=400.0)], now=NOW, settings=SETTINGS)
    assert not old.recent_big_loss


def test_half_life_weights_recent_trades():
    s = dict(SETTINGS, half_life_days=30)
    h = ptg.summarize_history([_sell(-200, days_ago=60), _sell(-200, days_ago=0)], now=NOW, settings=s)
    assert h.weighted_pnl_usdt == pytest.approx(-200 - 50, rel=1e-3)
    assert h.net_pnl_usdt == pytest.approx(-400)


# --- verdicts -----------------------------------------------------------------------


def test_new_coin_is_allowed():
    res, _ = _gate(rows=[], events=[])
    assert res.verdict == "allow" and res.codes == ["no_history"] and res.size_mult == 1.0


def test_good_history_allowed():
    res, _ = _gate(rows=[_sell(80), _sell(40, days_ago=30)])
    assert res.verdict == "allow" and res.codes == []


def test_block_two_losses_and_net_loss():
    res, _ = _gate(rows=[_sell(-80, days_ago=30), _sell(-60, days_ago=50), _sell(10, days_ago=60)])
    assert res.verdict == "block" and "hist_losses" in res.codes and res.size_mult == 0.0


def test_two_small_losses_only_reduce():
    res, _ = _gate(rows=[_sell(-20, days_ago=30), _sell(-30, days_ago=50)])
    assert res.verdict == "reduce" and res.codes == ["hist_mixed"]
    assert res.size_mult == pytest.approx(0.5)


def test_recent_big_loss_blocks():
    res, _ = _gate(rows=[_sell(-300, days_ago=2, usdt=700.0), _sell(400, days_ago=1)])
    assert res.verdict == "block" and "hist_recent_big_loss" in res.codes


def _ev(event_type, impact=0.0):
    return {"event_type": event_type, "impact_score": impact, "description": event_type}


@pytest.mark.parametrize(
    "etype,code",
    [("hack", "facts_hard_negative"), ("structure_risk", "facts_structure_risk"), ("unlock", "facts_unlock")],
)
def test_fact_flags_block(etype, code):
    res, calls = _gate(rows=[], events=[_ev(etype)])
    assert res.verdict == "block" and code in res.codes
    assert calls["f"][0]["since_iso"] == "2026-10-21T12:00:00Z"  # 336 h


def test_fact_flags_reduce_and_floor():
    res, _ = _gate(rows=[], events=[_ev("flow_only_move"), _ev("profit_taking_narrative")])
    assert res.verdict == "reduce"
    assert set(res.codes) == {"facts_flow_only", "facts_profit_taking"}
    assert res.size_mult == pytest.approx(0.35)
    res2, _ = _gate(
        rows=[_sell(-20, days_ago=30)], events=[_ev("flow_only_move"), _ev("profit_taking_narrative")]
    )
    assert res2.size_mult == pytest.approx(0.25)  # 0.5*0.5*0.7 floored at min_mult


def test_facts_disabled_ignores_events():
    cfg = _cfg_dict(facts={"enabled": False})
    res, calls = _gate(rows=[], events=[_ev("hack")], cfg=cfg)
    assert res.verdict == "allow" and calls["f"] == []


def test_dca_trend_rule_only_for_dca():
    res, _ = _gate(rows=[], is_dca=True, ext=-20.0)
    assert res.verdict == "block" and res.codes == ["dca_below_trend"]
    assert _gate(rows=[], is_dca=False, ext=-20.0)[0].verdict == "allow"
    assert _gate(rows=[], is_dca=True, ext=-10.0)[0].verdict == "allow"
    assert _gate(rows=[], is_dca=True, ext=None)[0].verdict == "allow"


# --- fail-closed -------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_history_read_error_blocks(mode):
    res, _ = _gate(cfg=_cfg_dict(mode=mode), h_err=ConnectionError("mongo down"))
    assert res.verdict == "block" and res.codes == ["db_error"] and res.mode == mode
    assert "ConnectionError" in (res.error or "")


def test_facts_read_error_blocks():
    res, _ = _gate(rows=[_sell(50)], f_err=TimeoutError("slow"))
    assert res.verdict == "block" and res.codes == ["db_error"]


def test_off_reads_nothing():
    res, calls = _gate(cfg=_cfg_dict(mode="off"), h_err=ConnectionError("x"))
    assert res.verdict == "allow" and res.mode == "off"
    assert calls == {"h": [], "f": []}


def test_orders_v2_disabled_is_db_error(monkeypatch):
    monkeypatch.setattr("storage.order_ledger_v2.get_order_ledger_v2", lambda: None)
    with pytest.raises(ptg.GateDataError):
        ptg.load_history_rows(tenant_ids=["default"], scope="demo", symbol=SYM, since_day="2026-08-01", limit=10)


# --- per-tenant history from orders_v2 (reset-proof) ---------------------------------


@pytest.fixture
def ledger(monkeypatch):
    store = MemoryOrderLedgerV2()
    monkeypatch.setattr("storage.order_ledger_v2.get_order_ledger_v2", lambda: store)
    return store


def test_history_is_per_tenant(ledger):
    for o in (
        _sell(-80, days_ago=30, tenant="henry", oid="h1"),
        _sell(-60, days_ago=40, tenant="henry", oid="h2"),
        _sell(90, days_ago=30, tenant="default", oid="d1"),
        _sell(-500, days_ago=30, tenant="default", sym="XYZ/USDT", oid="d2"),
    ):
        ledger.upsert_order(o)
    cfg = _cfg_dict()

    def run(tenant, c=cfg):
        return ptg.evaluate_gate(
            symbol=SYM, source="auto", tenant_id=tenant, scope="demo", config=c, now=NOW,
            facts_loader=lambda **k: [],
        )

    assert run("henry").verdict == "block"
    d = run("default")
    assert d.verdict == "allow" and d.history["n_closed"] == 1
    assert run("ctexp").codes == ["no_history"]
    pooled = run("default", _cfg_dict(pool_tenants=["henry"]))
    # pooled: -80 -60 +90 = -50 over 3 trades -> mixed, not a block
    assert pooled.history["tenants"] == ["default", "henry"] and pooled.history["n_closed"] == 3
    assert pooled.verdict == "reduce"


def test_pre_epoch_orders_count_and_window(ledger):
    # Fills from before the 2026-11-04 ledger reset stay in orders_v2 and count.
    ledger.upsert_order(_sell(-80, days_ago=60, tenant="henry", oid="old1"))
    ledger.upsert_order(_sell(-70, days_ago=80, tenant="henry", oid="old2"))
    ledger.upsert_order(_sell(-900, days_ago=120, tenant="henry", oid="tooold"))
    res = ptg.evaluate_gate(
        symbol=SYM, source="auto", tenant_id="henry", scope="demo", config=_cfg_dict(), now=NOW,
        facts_loader=lambda **k: [],
    )
    assert res.verdict == "block" and res.history["n_closed"] == 2


def test_other_scope_ignored(ledger):
    o = _sell(-900, days_ago=3, tenant="default", oid="live1")
    o["ledger_scope"] = "live"
    ledger.upsert_order(o)
    res = ptg.evaluate_gate(
        symbol=SYM, source="auto", tenant_id="default", scope="demo", config=_cfg_dict(), now=NOW,
        facts_loader=lambda **k: [],
    )
    assert res.codes == ["no_history"]


def test_mongo_query_symbol_fills_read_only():
    from storage.mongo_client import get_database
    from storage.order_ledger_v2 import ORDERS_V2_COLLECTION, MongoOrderLedgerV2

    try:
        db = get_database()
    except Exception:
        pytest.skip("local mongo not reachable")
    col = db[ORDERS_V2_COLLECTION]
    docs = [
        dict(_sell(-80, days_ago=5, tenant="henry", oid="m1"), _id="henry:demo:m1"),
        dict(_sell(-80, days_ago=5, tenant="default", oid="m2"), _id="default:demo:m2"),
        dict(_sell(-80, days_ago=200, tenant="henry", oid="m3"), _id="henry:demo:m3"),
        dict(_buy(tenant="henry", oid="m4"), _id="henry:demo:m4", status="rejected"),
    ]
    col.delete_many({"_id": {"$in": [d["_id"] for d in docs]}})
    col.insert_many(docs)
    try:
        store = MongoOrderLedgerV2()
        store._db = lambda: db
        rows = store.query_symbol_fills(["henry"], "demo", SYM, (NOW - timedelta(days=90)).strftime("%Y-%m-%d"))
        assert [r["id"] for r in rows] == ["m1"]
        assert "_id" not in rows[0] and rows[0]["pnl"] == -80
    finally:
        col.delete_many({"_id": {"$in": [d["_id"] for d in docs]}})


def test_fact_loader_query_shape():
    seen = {}

    class _Cur(list):
        def sort(self, *a):
            return self

        def limit(self, n):
            seen["limit"] = n
            return self

    class _Col:
        def find(self, q):
            seen["q"] = q
            return _Cur([{"_id": "e1", "event_id": "e1", "timestamp": "2026-11-01T00:00:00Z",
                          "event_type": "unlock", "symbols": ["ABC"], "tenant_id": "default"}])

    class _Db(dict):
        def __getitem__(self, k):
            seen["col"] = k
            return _Col()

    out = ptg.load_fact_events(symbol="abc/usdt", since_iso="2026-10-21T00:00:00Z", limit=7, db=_Db())
    assert seen["col"] == "memory_market_events" and seen["limit"] == 7
    assert set(seen["q"]["symbols"]["$in"]) == {"ABC/USDT", "ABC"}
    assert "tenant_id" not in seen["q"]  # market facts are global (#708)
    types = seen["q"]["$or"][0]["event_type"]["$in"]
    assert "unlock" in types and "dca_decision" not in types and "soft_block" not in types
    assert len(out) == 1 and out[0].event_type == "unlock"


# --- RiskManager integration --------------------------------------------------------


def _bot_cfg(gate: dict | None) -> BotConfig:
    raw = copy.deepcopy(get_config())
    raw["trading_mode"] = "paper"
    raw["initial_capital_usdt"] = 100_000
    raw["max_open_positions"] = 24
    raw["max_usdt_per_trade"] = 2500
    raw["max_position_percent"] = 30
    risk = dict(raw.get("risk") or {})
    risk.update(
        cash_floor_pct=0,
        cash_floor_basis="initial",
        dca_reserve_pct=0,
        min_trade_usdt=10.0,
        venue_quality={"enabled": False, "min_volume_to_order_multiple": 20},
        position_capacity={"enabled": False},
        cash_policy={"enabled": False},
        slot_eviction={"enabled": False},
        moderate_deploy={"enabled": False},
    )
    raw["risk"] = risk
    uni = dict(raw.get("universe") or {})
    uni["split_enabled"] = False
    raw["universe"] = uni
    raw.pop("rule_switches", None)
    if gate is not None:
        raw["rule_switches"] = {"pre_trade_gate": gate}
    return BotConfig(raw)


class _Market:
    def fetch_ohlcv(self, symbol, timeframe, limit=100):
        return []

    def fetch_indicators(self, *a, **k):
        return {"atr_pct": 3.0, "rsi": 45.0}


def _order(source="auto", signal="BUY", type_="BUY", usdt=2000.0):
    return TradeOrder(
        type=type_, symbol=SYM, price=100.0, amount=0.0 if type_ == "BUY" else 5.0,
        usdt_amount=usdt, signal=signal, source=source,
    )


def _run(gate, order, *, source="auto", rows=None, events=None, position=None, h_err=None):
    rm = RiskManager(_bot_cfg(gate), market_service=_Market())
    pos = position if position is not None else {"amount": 0, "average_entry": 0}
    calls = {"n": 0, "log": []}

    def hl(**kw):
        calls["n"] += 1
        if h_err:
            raise h_err
        return list(rows or [])

    def fake_log(msg, level="INFO", *a, **k):
        if str(msg).startswith("pre_trade_gate"):
            calls["log"].append(str(msg))

    with ExitStack() as st:
        st.enter_context(patch("risk.risk_manager.count_open_full_slots", return_value=1))
        st.enter_context(patch("risk.risk_manager.get_position", return_value=pos))
        st.enter_context(patch("risk.risk_manager.find_open_position_for_symbol", return_value=None))
        st.enter_context(patch("risk.risk_manager.find_open_positions_for_symbol", return_value=[]))
        st.enter_context(patch.object(rm, "_trade_cooldown_blocked", return_value=(False, "")))
        st.enter_context(patch.object(rm, "_daily_buy_limit_blocked", return_value=None))
        st.enter_context(patch.object(rm, "_daily_dca_usdt_limit_blocked", return_value=None))
        st.enter_context(patch.object(rm, "_available_usdt", return_value=90_000.0))
        st.enter_context(patch.object(rm, "_portfolio_equity", return_value=100_000.0))
        st.enter_context(patch.object(rm, "_equity_for_sizing", return_value=100_000.0))
        st.enter_context(patch.object(rm, "_initial_capital", return_value=100_000.0))
        st.enter_context(patch.object(rm, "_equity_drawdown_pct", return_value=0.0))
        st.enter_context(patch.object(rm, "_open_book_memory_counts", return_value=(0, 0, 0)))
        st.enter_context(patch.object(rm, "_process_uptime_sec", return_value=3600.0))
        st.enter_context(patch("risk.risk_manager.load_trade_history", return_value={"peak_equity": 100_000.0}))
        st.enter_context(
            patch(
                "services.market_policy_fusion.get_global_market_bias",
                return_value={"block_buys": False, "size_mult": 1.0, "regime": "NEUTRAL"},
            )
        )
        st.enter_context(patch("risk.pre_trade_gate.load_history_rows", side_effect=hl))
        st.enter_context(patch("risk.pre_trade_gate.load_fact_events", return_value=list(events or [])))
        st.enter_context(patch("logger.log", side_effect=fake_log))
        dec = rm.evaluate(order, "4h", source=source)
    return dec, calls


BAD = [_sell(-80, days_ago=1), _sell(-60, days_ago=2)]
MIXED = [_sell(-20, days_ago=1)]


def _gcfg(mode):
    g = copy.deepcopy(SETTINGS)
    g["mode"] = mode
    g["recent_days"] = None  # NOW in rows is fixed; keep the test independent of wall clock
    g["lookback_days"] = 36500
    return g


def test_rm_off_reads_nothing():
    dec, calls = _run(None, _order(), rows=BAD)
    assert dec.approved and calls["n"] == 0 and calls["log"] == []


def test_rm_shadow_logs_and_never_blocks():
    base, _ = _run(None, _order(), rows=BAD)
    dec, calls = _run(_gcfg("shadow"), _order(), rows=BAD)
    assert dec.approved and calls["n"] == 1
    assert dec.order.usdt_amount == pytest.approx(base.order.usdt_amount)
    assert calls["log"] and "verdict=block" in calls["log"][0]
    assert "tenant=default" in calls["log"][0] and f"symbol={SYM}" in calls["log"][0]
    assert "source=auto" in calls["log"][0] and "codes=hist_losses" in calls["log"][0]


def test_rm_shadow_db_error_does_not_block():
    dec, calls = _run(_gcfg("shadow"), _order(), h_err=ConnectionError("down"))
    assert dec.approved and "codes=db_error" in calls["log"][0]


def test_rm_enforce_db_error_blocks():
    dec, _ = _run(_gcfg("enforce"), _order(), h_err=ConnectionError("down"))
    assert not dec.approved and dec.code == "pre_trade_gate_block" and "db_error" in dec.message


def test_rm_enforce_crash_blocks():
    with patch("risk.pre_trade_gate.evaluate_gate", side_effect=RuntimeError("boom")):
        dec, _ = _run(_gcfg("enforce"), _order(), rows=[])
    assert not dec.approved and dec.code == "pre_trade_gate_block"
    with patch("risk.pre_trade_gate.evaluate_gate", side_effect=RuntimeError("boom")):
        dec2, _ = _run(_gcfg("shadow"), _order(), rows=[])
    assert dec2.approved


def test_rm_enforce_new_coin_allowed():
    base, _ = _run(None, _order(), rows=[])
    dec, _ = _run(_gcfg("enforce"), _order(), rows=[])
    assert dec.approved and dec.order.usdt_amount == pytest.approx(base.order.usdt_amount)


def test_rm_enforce_reduce_scales_size():
    base, _ = _run(None, _order(), rows=MIXED)
    dec, _ = _run(_gcfg("enforce"), _order(), rows=MIXED)
    assert dec.approved
    assert dec.order.usdt_amount == pytest.approx(base.order.usdt_amount * 0.5, rel=1e-6)


@pytest.mark.parametrize(
    "source,signal",
    [
        ("auto", "BUY"),
        ("lc", "BUY"),
        ("cmc", "BUY"),
        ("oracle", "BUY"),
        ("gainer_relvol", "GAINER_RELVOL"),
        ("entry_sensor_15m", "BUY"),
        ("dca_sniper", "BUY"),
    ],
)
def test_rm_enforce_blocks_every_bot_source(source, signal):
    dec, calls = _run(_gcfg("enforce"), _order(source=source, signal=signal), source=source, rows=BAD)
    assert not dec.approved, (source, dec.code, dec.message)
    assert dec.code == "pre_trade_gate_block" and calls["n"] == 1


def test_rm_enforce_blocks_dca_add():
    pos = {"amount": 10.0, "average_entry": 100.0, "dca_rounds": 0}
    dec, calls = _run(
        _gcfg("enforce"), _order(source="dca", signal="BUY_DCA"), source="dca", rows=BAD, position=pos
    )
    assert calls["n"] == 1
    assert not dec.approved and dec.code == "pre_trade_gate_block"


@pytest.mark.parametrize("source", ["manual", "telegram"])
def test_rm_manual_buy_exempt(source):
    from risk import rule_switches as rs

    if not rs.is_manual_source(source):
        pytest.skip(f"{source} is not a manual source here")
    dec, calls = _run(_gcfg("enforce"), _order(source=source), source=source, rows=BAD)
    assert calls["n"] == 0 and dec.code != "pre_trade_gate_block"


def test_rm_sell_never_evaluated():
    pos = {"amount": 5.0, "average_entry": 100.0}
    dec, calls = _run(
        _gcfg("enforce"), _order(type_="SELL", signal="SELL_FULL", usdt=500.0), rows=BAD, position=pos
    )
    assert calls["n"] == 0 and dec.code != "pre_trade_gate_block"


# --- backtest harness scenario (m) -----------------------------------------


def _bt_fill(tenant, symbol, side, ts, usdt):
    from backtest.harness.data import Fill

    return Fill(
        order_id=f"{tenant}-{symbol}-{side}-{ts.isoformat()}", tenant=tenant, symbol=symbol, timeframe="1h",
        side=side, signal="BUY" if side == "buy" else "SELL", source="auto", exit_source=None,
        ts=ts, price=1.0, amount=usdt, usdt=usdt,
    )


def _bt_episode(tenant, symbol, opened, closed, buy_usdt, sell_usdt):
    from backtest.harness.engine import Episode

    b = _bt_fill(tenant, symbol, "buy", opened, buy_usdt)
    events = [b]
    if closed is not None:
        events.append(_bt_fill(tenant, symbol, "sell", closed, sell_usdt))
    return Episode(tenant=tenant, symbol=symbol, timeframe="1h", entry=b, events=events, closed_at=closed)


def test_harness_m_history_has_no_lookahead_and_is_per_tenant():
    from datetime import timezone as _tz

    from backtest.harness import scenarios as sc

    t0 = datetime(2026, 9, 1, tzinfo=_tz.utc)
    past = _bt_episode("default", "XYZ/USDT", t0, t0 + timedelta(days=1), 1000, 800)  # -200, closed before
    later = _bt_episode("default", "XYZ/USDT", t0 + timedelta(days=2), t0 + timedelta(days=9), 1000, 500)
    other = _bt_episode("henry", "XYZ/USDT", t0, t0 + timedelta(days=1), 1000, 100)
    probe = _bt_episode("default", "XYZ/USDT", t0 + timedelta(days=5), None, 1000, 0)
    idx = sc._closed_trades_index([past, later, other, probe])

    rows = sc.memory_history_rows(idx, probe, lookback_days=90)
    assert [r["pnl"] for r in rows] == [-200]  # 'later' closes after the signal, henry not pooled
    assert rows[0]["execution"]["usdt"] == 800 and rows[0]["tenant_id"] == "default"

    pooled = sc.memory_history_rows(idx, probe, lookback_days=90, pool=("henry",))
    assert sorted(r["pnl"] for r in pooled) == [-900, -200]

    assert sc.memory_history_rows(idx, probe, lookback_days=3) == []  # outside the lookback


def test_harness_m_registered_and_trials_counted():
    from backtest.harness import scenarios as sc

    assert "m" in sc.SCENARIOS
    assert sc.N_TRIALS == 60
