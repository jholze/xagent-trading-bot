"""#633 Slice B — log the existing cap order (observe only).

The file name includes "universe" so tests/conftest.py leaves
universe.split_enabled on. Other unit tests force that gate off.

B1: observe on does not change buy / skip / size.
B2: observe on does not widen a cap-less revise, open the universe, or bypass risk.
B3: the log has symbol, rank, cap name, and reject code, and places no order.
Fail-open: a write or read error does not change the decision and is not a gate.

No live HTTP. File writes need CAP_ORDER_OBSERVE_UNDER_TEST=1 and use
isolate_bot_logs. Nothing is written under data/.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from services.universe.cap_order_observe import (
    IST_CAP_NAME,
    ROW_KEYS,
    cap_order_fire_enabled,
    cap_order_observe_enabled,
    existing_cap_order,
    maybe_log_existing_cap_order,
)
from services.universe.membership_revise import ERSATZ_CAP_NAME, MembershipReviseRefused
from services.universe.split import is_trade_eligible, load_trade_universe
from tests.unit.test_universe_membership_revise_631 import (
    IN_SET,
    OUT_SET,
    _eval_env,
    _observe_ranked,
    _order,
    _patch_universe_io,
    _risk_raw,
    _rm,
    _universe,
)

REJECT = "universe_trade_cap"


def _with_observe(raw: dict, *, on: bool, fire: bool = False) -> dict:
    cfg = copy.deepcopy(raw)
    cfg.setdefault("universe", {})["cap_order_observe"] = {
        "observe_enabled": on,
        "fire_enabled": fire,
    }
    return cfg


def _money(dec, order) -> dict:
    attached = getattr(dec, "order", None)
    return {
        "approved": dec.approved,
        "code": dec.code,
        "message": dec.message,
        "size_multiplier": dec.size_multiplier,
        "decision_has_order": attached is not None,
        "decision_qty": None if attached is None else attached.qty,
        "decision_usdt": None if attached is None else attached.usdt_amount,
        "qty": order.qty,
        "usdt_amount": order.usdt_amount,
    }


def _eval(monkeypatch, symbol: str, *, source: str, signal: str, venue_ok: bool, mcap: float, raw: dict):
    _patch_universe_io(monkeypatch, _observe_ranked())
    rm = _rm(raw)
    order = _order(symbol, source=source, signal=signal)
    with _eval_env(rm, venue_ok=venue_ok, mcap=mcap):
        dec = rm.evaluate(order, timeframe="15m", source=source)
    return dec, order


def _pair(monkeypatch, symbol: str, **kwargs):
    base = _risk_raw()
    off, off_order = _eval(
        monkeypatch, symbol, raw=_with_observe(base, on=False), **kwargs
    )
    on, on_order = _eval(
        monkeypatch, symbol, raw=_with_observe(base, on=True, fire=True), **kwargs
    )
    return off, off_order, on, on_order


# --- flag -----------------------------------------------------------------


def test_observe_defaults_off_and_fire_stays_false():
    assert cap_order_observe_enabled({}) is False
    assert cap_order_observe_enabled({"universe": {}}) is False
    assert cap_order_observe_enabled({"universe": {"cap_order_observe": "shadow"}}) is False
    assert cap_order_fire_enabled({"universe": {"cap_order_observe": {"fire_enabled": True}}}) is False
    assert cap_order_fire_enabled({"fire_enabled": True, "allow_live": True}) is False


# --- B1 -------------------------------------------------------------------


def test_b1_buy_skip_and_size_match_with_observe_on(monkeypatch):
    """Flag on, including a config fire_enabled true, matches the flag-off decision."""
    # code None means "do not pin the code"; still require on == off.
    cases = [
        (OUT_SET, "entry_sensor_15m", "BUY", True, 50_000_000, "universe_trade_cap"),
        (IN_SET, "entry_sensor_15m", "BUY", False, 50_000_000, "venue_liquidity_block"),
        (IN_SET, "entry_sensor_15m", "BUY", True, 1.0, "long_mcap"),
        (IN_SET, "entry_sensor_15m", "BUY", True, 50_000_000, ""),
        (OUT_SET, "gainer_relvol", "GAINER_RELVOL", True, 1.0, "long_mcap"),
        (OUT_SET, "dca", "BUY_DCA", True, 50_000_000, None),
    ]
    saw_buy = False
    saw_skip = False
    for symbol, source, signal, venue_ok, mcap, code in cases:
        off, off_order, on, on_order = _pair(
            monkeypatch,
            symbol,
            source=source,
            signal=signal,
            venue_ok=venue_ok,
            mcap=mcap,
        )
        assert _money(on, on_order) == _money(off, off_order)
        if code is not None:
            assert off.code == code
            assert on.code == code
        if off.approved:
            saw_buy = True
            assert on.size_multiplier == off.size_multiplier
            assert on.order is not None
            assert on.order.usdt_amount == off.order.usdt_amount
            assert on.order.qty == off.order.qty
        else:
            saw_skip = True
            assert on.size_multiplier == 0.0
            assert on.order is None
    assert saw_buy and saw_skip


def test_b1_trade_universe_unchanged_when_observe_on(monkeypatch):
    observe = _observe_ranked()
    _patch_universe_io(monkeypatch, observe)
    off = _with_observe(
        _universe(mode="enforce", trade_max=40, rank="as_is", cap=2, staged_max=100),
        on=False,
    )
    on = _with_observe(off, on=True, fire=True)
    off_syms = [
        c["symbol"]
        for c in load_trade_universe(config=off, observe_coins=list(observe), open_symbols=set())
    ]
    on_syms = [
        c["symbol"]
        for c in load_trade_universe(config=on, observe_coins=list(observe), open_symbols=set())
    ]
    assert on_syms == off_syms
    assert on_syms == [IN_SET, "MID/USDT"]
    assert OUT_SET not in on_syms


# --- B2 -------------------------------------------------------------------


def test_b2_observe_does_not_open_capless_revise(monkeypatch):
    observe = _observe_ranked()
    _patch_universe_io(monkeypatch, observe)
    cfg = _with_observe(
        _universe(mode="enforce", include_cap=False, cap=None, trade_max=40),
        on=True,
        fire=True,
    )
    with pytest.raises(MembershipReviseRefused):
        load_trade_universe(config=cfg, observe_coins=list(observe), open_symbols=set())
    assert is_trade_eligible(IN_SET, config=cfg) is False
    assert is_trade_eligible(OUT_SET, config=cfg) is False
    assert maybe_log_existing_cap_order(cfg, rejected_symbol=OUT_SET) is None

    dec, order = _eval(
        monkeypatch,
        OUT_SET,
        source="entry_sensor_15m",
        signal="BUY",
        venue_ok=True,
        mcap=50_000_000,
        raw=cfg,
    )
    assert dec.approved is False
    assert dec.code == REJECT
    assert dec.size_multiplier == 0.0
    assert dec.order is None
    assert order.usdt_amount == 200.0


def test_b2_observe_does_not_open_all(monkeypatch):
    observe = _observe_ranked()
    _patch_universe_io(monkeypatch, observe)
    cfg = _with_observe(
        _universe(mode="enforce", trade_max=40, rank="as_is", cap=2, staged_max=10_000),
        on=True,
    )
    trade = load_trade_universe(config=cfg, observe_coins=list(observe), open_symbols=set())
    rows = existing_cap_order(cfg)
    logged = [row["symbol"] for row in rows]
    assert logged == [c["symbol"] for c in trade]
    assert len(logged) == 2
    assert len(logged) < len(observe)
    assert set(logged) != {c["symbol"] for c in observe}
    for row in rows:
        assert "order" not in row
        assert row["rank"] == logged.index(row["symbol"]) + 1


@pytest.mark.parametrize(
    "symbol,source,signal,venue_ok,mcap,chase,code",
    [
        (OUT_SET, "entry_sensor_15m", "BUY", True, 50_000_000, False, "universe_trade_cap"),
        (IN_SET, "entry_sensor_15m", "BUY", False, 50_000_000, False, "venue_liquidity_block"),
        (IN_SET, "entry_sensor_15m", "BUY", True, 1.0, False, "long_mcap"),
        (IN_SET, "entry_sensor_15m", "BUY", True, 50_000_000, True, "gainer_chase_guard"),
        (OUT_SET, "gainer_relvol", "GAINER_RELVOL", True, 1.0, False, "long_mcap"),
    ],
)
def test_b2_observe_does_not_bypass_risk_codes(
    monkeypatch, symbol, source, signal, venue_ok, mcap, chase, code
):
    raw = _with_observe(_risk_raw(), on=True, fire=True)
    _patch_universe_io(monkeypatch, _observe_ranked())
    rm = _rm(raw)
    order = _order(symbol, source=source, signal=signal)
    with _eval_env(rm, venue_ok=venue_ok, mcap=mcap):
        if chase:
            with patch(
                "services.gainer_universe.chase_guard.check_gainer_chase_guard",
                return_value=(True, "chasing"),
            ):
                dec = rm.evaluate(order, timeframe="15m", source=source)
        else:
            dec = rm.evaluate(order, timeframe="15m", source=source)
    assert dec.approved is False
    assert dec.code == code
    assert dec.size_multiplier == 0.0
    assert dec.order is None
    if code != REJECT:
        assert dec.code != REJECT


def test_b2_shadow_staged_widen_stays_off_the_live_order(monkeypatch):
    """Shadow Ersatz-Cap must not become the live order when observe is on."""
    observe = _observe_ranked()
    _patch_universe_io(monkeypatch, observe)
    cfg = _with_observe(
        _universe(
            mode="shadow",
            trade_max=1,
            rank="as_is",
            cap=2,
            staged_max=100,
            staged_rank="quality_score",
        ),
        on=True,
        fire=True,
    )
    trade = [
        c["symbol"]
        for c in load_trade_universe(config=cfg, observe_coins=list(observe), open_symbols=set())
    ]
    rows = maybe_log_existing_cap_order(cfg, rejected_symbol=IN_SET)
    assert trade == [OUT_SET]
    assert [row["symbol"] for row in rows] == [OUT_SET]
    assert rows[0]["cap_name"] == IST_CAP_NAME
    assert rows[0]["rank"] == 1
    assert IN_SET not in trade


# --- B3 -------------------------------------------------------------------


def test_b3_log_is_existing_order_and_places_no_order(monkeypatch):
    observe = _observe_ranked()
    _patch_universe_io(monkeypatch, observe)
    # as_is keeps OUTSET then MID. A new quality score would put INSET first.
    cfg = _with_observe(
        _universe(mode="off", trade_max=2, rank="as_is", include_cap=False, cap=None),
        on=True,
    )
    trade = load_trade_universe(config=cfg, observe_coins=list(observe), open_symbols=set())
    logged: list[str] = []

    def _capture(message, level="INFO"):
        logged.append(str(message))

    with patch("services.universe.cap_order_observe.log", side_effect=_capture):
        dec, order = _eval(
            monkeypatch,
            "TAIL/USDT",
            source="entry_sensor_15m",
            signal="BUY",
            venue_ok=True,
            mcap=50_000_000,
            raw=cfg,
        )
    assert dec.approved is False
    assert dec.code == REJECT
    assert dec.size_multiplier == 0.0
    assert dec.order is None
    assert order.qty == 0
    assert order.usdt_amount == 200.0

    lines = [line for line in logged if line.startswith("[cap_order_observe]")]
    assert len(lines) == 1
    line = lines[0]
    assert "rejected_symbol=TAIL/USDT" in line
    assert "symbol=OUTSET/USDT" in line
    assert "rank=1" in line
    assert f"cap_name={IST_CAP_NAME}" in line
    assert f"reject_code={REJECT}" in line
    assert "symbol=MID/USDT" in line
    assert "rank=2" in line
    assert "symbol=INSET/USDT" not in line
    assert "fire_enabled=False" in line
    assert "order=" not in line

    rows = existing_cap_order(cfg)
    assert [row["symbol"] for row in rows] == [c["symbol"] for c in trade]
    assert [row["symbol"] for row in rows] == [OUT_SET, "MID/USDT"]
    for row in rows:
        assert set(row) == {"symbol", "rank", "cap_name", "reject_code"}
        assert row["cap_name"] == IST_CAP_NAME
        assert row["reject_code"] == REJECT
        assert isinstance(row["rank"], int)


def test_b3_enforce_names_ersatz_cap_and_keeps_its_order(monkeypatch):
    observe = _observe_ranked()
    _patch_universe_io(monkeypatch, observe)
    cfg = _with_observe(
        _universe(
            mode="enforce",
            trade_max=40,
            rank="as_is",
            cap=2,
            staged_max=100,
            staged_rank="quality_score",
        ),
        on=True,
    )
    rows = maybe_log_existing_cap_order(cfg, rejected_symbol=OUT_SET)
    assert rows is not None
    assert [row["symbol"] for row in rows] == [IN_SET, "MID/USDT"]
    assert [row["rank"] for row in rows] == [1, 2]
    for row in rows:
        assert row["cap_name"] == ERSATZ_CAP_NAME
        assert row["reject_code"] == REJECT
        assert row["rejected_symbol"] == OUT_SET
        assert "order" not in row
        assert set(row) == set(ROW_KEYS)


def test_b3_jsonl_rows_have_fields_and_no_order(tmp_path, monkeypatch):
    monkeypatch.setenv("CAP_ORDER_OBSERVE_UNDER_TEST", "1")
    observe = _observe_ranked()
    _patch_universe_io(monkeypatch, observe)
    cfg = _with_observe(
        _universe(mode="enforce", trade_max=40, rank="quality_score", cap=2, staged_max=2),
        on=True,
    )
    rows = maybe_log_existing_cap_order(cfg, rejected_symbol=OUT_SET)
    path = tmp_path / "logs" / "cap_order_observe.jsonl"
    assert path.is_file()
    on_disk = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert on_disk == rows
    assert len(on_disk) == 2
    for row in on_disk:
        assert set(row) == set(ROW_KEYS)
        assert row["reject_code"] == REJECT
        assert row["cap_name"] == ERSATZ_CAP_NAME
        assert isinstance(row["rank"], int)
        assert "order" not in row
        assert "usdt_amount" not in row
        assert "qty" not in row


def test_b3_observe_off_does_not_log(monkeypatch):
    observe = _observe_ranked()
    _patch_universe_io(monkeypatch, observe)
    cfg = _with_observe(_universe(mode="off", trade_max=2, rank="as_is", include_cap=False, cap=None), on=False)
    logged: list[str] = []
    with patch("services.universe.cap_order_observe.log", side_effect=lambda *a, **k: logged.append(str(a[0]))):
        assert maybe_log_existing_cap_order(cfg, rejected_symbol=OUT_SET) is None
    assert logged == []


# --- fail-open ------------------------------------------------------------


def test_fail_open_write_error_keeps_decision(monkeypatch):
    monkeypatch.setenv("CAP_ORDER_OBSERVE_UNDER_TEST", "1")
    base = _risk_raw()
    off, off_order = _eval(
        monkeypatch,
        OUT_SET,
        source="entry_sensor_15m",
        signal="BUY",
        venue_ok=True,
        mcap=50_000_000,
        raw=_with_observe(base, on=False),
    )
    on_raw = _with_observe(base, on=True)
    _patch_universe_io(monkeypatch, _observe_ranked())
    rm = _rm(on_raw)
    order = _order(OUT_SET, source="entry_sensor_15m", signal="BUY")
    with patch("services.universe.cap_order_observe.log", side_effect=OSError("log down")):
        with patch(
            "services.observability_store.append_jsonl",
            side_effect=OSError("disk full"),
        ):
            with _eval_env(rm, venue_ok=True, mcap=50_000_000):
                dec = rm.evaluate(order, timeframe="15m", source="entry_sensor_15m")
    assert _money(dec, order) == _money(off, off_order)
    assert dec.approved is False
    assert dec.code == REJECT
    assert dec.size_multiplier == 0.0
    assert dec.order is None


def test_fail_open_read_error_is_not_a_gate(monkeypatch):
    observe = _observe_ranked()
    _patch_universe_io(monkeypatch, observe)
    cfg = _with_observe(
        _universe(mode="enforce", trade_max=40, rank="quality_score", cap=2),
        on=True,
    )
    real = load_trade_universe
    calls = {"n": 0}

    def _flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError("cap order unreadable")
        return real(*args, **kwargs)

    with patch("services.universe.split.load_trade_universe", side_effect=_flaky):
        dec, order = _eval(
            monkeypatch,
            OUT_SET,
            source="entry_sensor_15m",
            signal="BUY",
            venue_ok=True,
            mcap=50_000_000,
            raw=cfg,
        )
    assert calls["n"] >= 2
    assert dec.approved is False
    assert dec.code == REJECT
    assert dec.size_multiplier == 0.0
    assert dec.order is None
    assert order.usdt_amount == 200.0


def test_fail_open_maybe_log_swallows_builder_and_writer(monkeypatch):
    cfg = _with_observe(_universe(mode="enforce", cap=2), on=True)
    with patch(
        "services.universe.cap_order_observe.existing_cap_order",
        side_effect=OSError("boom"),
    ):
        assert maybe_log_existing_cap_order(cfg, rejected_symbol=OUT_SET) is None

    monkeypatch.setenv("CAP_ORDER_OBSERVE_UNDER_TEST", "1")
    observe = _observe_ranked()
    _patch_universe_io(monkeypatch, observe)
    with patch("services.universe.cap_order_observe.log", side_effect=OSError("log down")):
        with patch(
            "services.observability_store.append_jsonl",
            side_effect=OSError("disk full"),
        ):
            rows = maybe_log_existing_cap_order(cfg, rejected_symbol=OUT_SET)
    assert rows is not None
    assert rows[0]["reject_code"] == REJECT
    assert "order" not in rows[0]
