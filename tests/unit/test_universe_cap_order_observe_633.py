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

from core.tenant_context import DEFAULT_TENANT, context_tenant_id, resolve_tenant_id, tenant_context
from services.universe.cap_order_observe import (
    IST_CAP_NAME,
    LOG_FILENAME,
    ROW_KEYS,
    cap_order_fire_enabled,
    cap_order_observe_enabled,
    existing_cap_order,
    maybe_log_existing_cap_order,
    observe_log_path,
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
    assert rows[0]["tenant"] is None
    assert "order" not in rows[0]


# --- tenant on the observe row --------------------------------------------

# Synthetic ids and symbols. Not production tenants or coins.
_REJECTED = "TREJECT/USDT"
_CAP_ORDERS = {
    None: ("TUNSCOPED/USDT",),
    DEFAULT_TENANT: ("TDEFA/USDT", "TDEFB/USDT"),
    "tenant_alpha": ("TALPHA/USDT", "TALPHB/USDT"),
    "tenant_beta": ("TBETA/USDT",),
}


def _member(symbol: str) -> dict:
    return {"symbol": symbol, "active": True, "ticker": symbol.split("/")[0]}


def _install_cap_orders(monkeypatch) -> list:
    """Trade list depends on the tenant id passed in, including None."""
    seen: list = []

    def _observe(tenant_id=None, **kwargs):
        seen.append(tenant_id)
        symbols = _CAP_ORDERS.get(tenant_id, _CAP_ORDERS[None])
        return [_member(sym) for sym in symbols]

    monkeypatch.setattr("data_manager.load_watchlist", lambda tenant_id=None: [])
    monkeypatch.setattr(
        "services.universe.split._quality_lookup",
        lambda tenant_id=DEFAULT_TENANT, use_ai_score=True: {},
    )
    monkeypatch.setattr("services.universe.split._open_symbols_live", lambda: set())
    monkeypatch.setattr("services.universe.split.load_observe_universe", _observe)
    return seen


def _tenant_cfg(*, on: bool) -> dict:
    base = _risk_raw()
    uni = _universe(
        mode="off",
        trade_max=10,
        rank="as_is",
        include_cap=False,
        cap=None,
    )
    base["universe"] = uni["universe"]
    return _with_observe(base, on=on, fire=True)


def _evaluate(raw: dict, rm=None):
    # Build the manager outside any tenant context. BotConfig() with no raw
    # calls get_config(), which follows the context and must not run here.
    if rm is None:
        rm = _rm(raw)
    order = _order(_REJECTED, source="entry_sensor_15m", signal="BUY")
    with _eval_env(rm, venue_ok=True, mcap=50_000_000):
        dec = rm.evaluate(order, timeframe="15m", source="entry_sensor_15m")
    return dec, order


def _cap_lines(logged: list[str]) -> list[str]:
    return [line for line in logged if line.startswith("[cap_order_observe]")]


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _files_under(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()}


def test_context_tenant_id_reads_contextvar_and_resolve_still_falls_back():
    """Helper returns None with no context. resolve_tenant_id is unchanged."""
    assert context_tenant_id() is None
    assert resolve_tenant_id(None) == DEFAULT_TENANT
    with tenant_context("tenant_alpha", scope="paper"):
        assert context_tenant_id() == "tenant_alpha"
        assert resolve_tenant_id(None) == "tenant_alpha"
    with tenant_context(DEFAULT_TENANT, scope="paper"):
        assert context_tenant_id() == DEFAULT_TENANT
    assert context_tenant_id() is None


def test_two_tenants_log_their_own_cap_order(tmp_path, monkeypatch):
    """Each context logs its own tenant id and its own cap list."""
    monkeypatch.setenv("CAP_ORDER_OBSERVE_UNDER_TEST", "1")
    seen = _install_cap_orders(monkeypatch)
    logged: list[str] = []
    data_root = tmp_path / "data"
    before_default = _files_under(data_root / DEFAULT_TENANT)
    off_cfg = _tenant_cfg(on=False)
    on_cfg = _tenant_cfg(on=True)

    def _capture(message, level="INFO"):
        logged.append(str(message))

    off_rm = _rm(off_cfg)
    on_rm = _rm(on_cfg)
    with patch("services.universe.cap_order_observe.log", side_effect=_capture):
        for tid in ("tenant_alpha", "tenant_beta"):
            with tenant_context(tid, scope="paper"):
                off_dec, off_order = _evaluate(off_cfg, off_rm)
                on_dec, on_order = _evaluate(on_cfg, on_rm)
            assert _money(off_dec, off_order) == _money(on_dec, on_order)
            assert on_dec.approved is False
            assert on_dec.code == REJECT
            assert on_dec.size_multiplier == 0.0
            assert on_dec.order is None

    lines = _cap_lines(logged)
    assert len(lines) == 2
    path = tmp_path / "logs" / LOG_FILENAME
    rows = _read_jsonl(path)
    assert [row["tenant"] for row in rows] == [
        "tenant_alpha",
        "tenant_alpha",
        "tenant_beta",
    ]
    by_tenant: dict[str, list[str]] = {}
    for row in rows:
        assert set(row) == set(ROW_KEYS)
        assert row["rejected_symbol"] == _REJECTED
        assert row["reject_code"] == REJECT
        assert "order" not in row
        by_tenant.setdefault(row["tenant"], []).append(row["symbol"])
    assert by_tenant["tenant_alpha"] == list(_CAP_ORDERS["tenant_alpha"])
    assert by_tenant["tenant_beta"] == list(_CAP_ORDERS["tenant_beta"])
    assert DEFAULT_TENANT not in by_tenant

    alpha_line = next(line for line in lines if "tenant=tenant_alpha" in line)
    beta_line = next(line for line in lines if "tenant=tenant_beta" in line)
    assert "symbol=TALPHA/USDT" in alpha_line and "rank=1" in alpha_line
    assert "symbol=TALPHB/USDT" in alpha_line and "rank=2" in alpha_line
    assert "symbol=TBETA/USDT" not in alpha_line
    assert "symbol=TUNSCOPED/USDT" not in alpha_line
    assert f"symbol={_CAP_ORDERS[DEFAULT_TENANT][0]}" not in alpha_line
    assert "symbol=TBETA/USDT" in beta_line and "rank=1" in beta_line
    assert "symbol=TALPHA/USDT" not in beta_line
    assert "fire_enabled=False" in alpha_line
    assert "fire_enabled=False" in beta_line
    assert "order=" not in alpha_line and "order=" not in beta_line
    # Gate reads with no id; the log read uses the context id.
    assert seen.count("tenant_alpha") == 1
    assert seen.count("tenant_beta") == 1
    assert DEFAULT_TENANT not in seen
    assert _files_under(data_root / DEFAULT_TENANT) == before_default


def test_default_tenant_context_logs_its_own_list(tmp_path, monkeypatch):
    monkeypatch.setenv("CAP_ORDER_OBSERVE_UNDER_TEST", "1")
    seen = _install_cap_orders(monkeypatch)
    logged: list[str] = []
    off_cfg = _tenant_cfg(on=False)
    on_cfg = _tenant_cfg(on=True)

    def _capture(message, level="INFO"):
        logged.append(str(message))

    off_rm = _rm(off_cfg)
    on_rm = _rm(on_cfg)
    with patch("services.universe.cap_order_observe.log", side_effect=_capture):
        with tenant_context(DEFAULT_TENANT, scope="paper"):
            off_dec, off_order = _evaluate(off_cfg, off_rm)
            on_dec, on_order = _evaluate(on_cfg, on_rm)
    assert _money(off_dec, off_order) == _money(on_dec, on_order)
    assert on_dec.code == REJECT
    lines = _cap_lines(logged)
    assert len(lines) == 1
    assert f"tenant={DEFAULT_TENANT}" in lines[0]
    assert "tenant=null" not in lines[0]
    assert "symbol=TDEFA/USDT" in lines[0] and "rank=1" in lines[0]
    assert "symbol=TDEFB/USDT" in lines[0] and "rank=2" in lines[0]
    assert "symbol=TUNSCOPED/USDT" not in lines[0]
    assert "fire_enabled=False" in lines[0]
    rows = _read_jsonl(tmp_path / "logs" / LOG_FILENAME)
    assert [row["symbol"] for row in rows] == list(_CAP_ORDERS[DEFAULT_TENANT])
    assert {row["tenant"] for row in rows} == {DEFAULT_TENANT}
    assert seen.count(DEFAULT_TENANT) == 1


def test_no_tenant_context_logs_null_and_does_not_write_default(tmp_path, monkeypatch):
    """No context: JSON null, and no data/default file or default-attributed row."""
    monkeypatch.setenv("CAP_ORDER_OBSERVE_UNDER_TEST", "1")
    assert context_tenant_id() is None
    seen = _install_cap_orders(monkeypatch)
    logged: list[str] = []
    data_root = tmp_path / "data"
    before = _files_under(data_root)
    before_default = _files_under(data_root / DEFAULT_TENANT)
    off_cfg = _tenant_cfg(on=False)
    on_cfg = _tenant_cfg(on=True)

    def _capture(message, level="INFO"):
        logged.append(str(message))

    off_rm = _rm(off_cfg)
    on_rm = _rm(on_cfg)
    with patch("services.universe.cap_order_observe.log", side_effect=_capture):
        off_dec, off_order = _evaluate(off_cfg, off_rm)
        assert _cap_lines(logged) == []
        assert not (tmp_path / "logs" / LOG_FILENAME).exists()
        on_dec, on_order = _evaluate(on_cfg, on_rm)

    assert _money(off_dec, off_order) == _money(on_dec, on_order)
    assert on_dec.approved is False
    assert on_dec.code == REJECT
    assert on_dec.size_multiplier == 0.0
    assert on_dec.order is None

    lines = _cap_lines(logged)
    assert len(lines) == 1
    assert "tenant=null" in lines[0]
    assert f"tenant={DEFAULT_TENANT}" not in lines[0]
    assert "tenant=None" not in lines[0]
    assert "symbol=TUNSCOPED/USDT" in lines[0]
    assert "symbol=TDEFA/USDT" not in lines[0]
    assert "symbol=TDEFB/USDT" not in lines[0]
    assert "fire_enabled=False" in lines[0]
    assert "order=" not in lines[0]

    path = Path(observe_log_path())
    assert path == tmp_path / "logs" / LOG_FILENAME
    assert DEFAULT_TENANT not in path.parts
    raw = path.read_text(encoding="utf-8")
    assert '"tenant": null' in raw
    assert f'"tenant": "{DEFAULT_TENANT}"' not in raw
    assert '"tenant": "null"' not in raw
    rows = _read_jsonl(path)
    assert rows
    for row in rows:
        assert row["tenant"] is None
        assert row["symbol"] in _CAP_ORDERS[None]
        assert set(row) == set(ROW_KEYS)
    assert DEFAULT_TENANT not in seen
    assert seen.count(None) >= 1
    assert _files_under(data_root) == before
    assert _files_under(data_root / DEFAULT_TENANT) == before_default
    if not before_default:
        assert not (data_root / DEFAULT_TENANT).exists()


def test_flag_off_writes_no_log_or_default_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CAP_ORDER_OBSERVE_UNDER_TEST", "1")
    _install_cap_orders(monkeypatch)
    logged: list[str] = []
    data_root = tmp_path / "data"
    before = _files_under(data_root)
    cfg = _tenant_cfg(on=False)

    rm = _rm(cfg)
    with patch(
        "services.universe.cap_order_observe.log",
        side_effect=lambda *a, **k: logged.append(str(a[0])),
    ):
        with tenant_context("tenant_alpha", scope="paper"):
            dec, order = _evaluate(cfg, rm)
        bare_dec, bare_order = _evaluate(cfg, rm)
    assert dec.code == REJECT
    assert bare_dec.code == REJECT
    assert dec.order is None and bare_order.usdt_amount == order.usdt_amount
    assert _cap_lines(logged) == []
    assert not (tmp_path / "logs" / LOG_FILENAME).exists()
    assert _files_under(data_root) == before
    assert not (data_root / DEFAULT_TENANT).exists()
