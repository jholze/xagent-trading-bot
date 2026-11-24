"""#830: production ModeProof producer and dedicated testnet key names.

Drives ``GateExecutionAdapter.execute`` (testnet SHORT/COVER) with a fake ccxt
client. The real ``_futures_route_mode_proof`` and ``_futures_route_credentials``
run. Env values are dummy placeholders set with monkeypatch, not credentials.
No socket is opened.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.config import BotConfig
from core.models import OrderStatus, TradeOrder, TradeResult
from core.tenant_context import tenant_context
from execution.gate_adapter import GateExecutionAdapter

TENANT = "tenant-a"
SYMBOL = "BASE/USDT"
SWAP = "BASE/USDT:USDT"
TESTNET = "https://api-testnet.gateapi.io/api/v4"
PROBE = ("GET", f"{TESTNET}/futures/usdt/contracts/BASE_USDT", False)

DUMMY = {
    "GATE_API_KEY": "dummy-live-key",
    "GATE_API_SECRET": "dummy-live-secret",
    "GATE_TESTNET_API_KEY": "dummy-testnet-key",
    "GATE_TESTNET_API_SECRET": "dummy-testnet-secret",
}
_MISSING = object()

# Gate answers set_leverage with the position (raw), fetch_position via ccxt.
SET_OK = {"leverage": "2", "mode": "single", "contract": "BASE_USDT"}
READ_OK = {"symbol": SWAP, "marginMode": "isolated", "leverage": 2, "info": {"mode": "single", "leverage": "2"}}


def _cfg(*, strict_route=True, **testnet_extra) -> BotConfig:
    testnet = {"enabled": True, "tenants": [TENANT], **testnet_extra}
    if strict_route is not _MISSING:
        testnet["strict_route"] = strict_route
    return BotConfig(
        {
            "trading_mode": "live",
            "live_confirmed": True,
            "max_usdt_per_trade": 1000,
            "shorts": {
                "enabled": True,
                "allow_live": False,
                "leverage_default": 2,
                "leverage_cap": 2,
                "testnet_futures": testnet,
            },
            "live": {
                "execution": "testnet",
                "dry_run": False,
                "api_key_env": "GATE_API_KEY",
                "api_secret_env": "GATE_API_SECRET",
            },
        }
    )


class _Response:
    def __init__(self, status):
        self.status_code = status


class _Session:
    follows_redirects = False

    def __init__(self, events):
        self.calls = []
        self._events = events

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs.get("allow_redirects")))
        self._events.append("probe")
        return _Response(200)


class _FakeGate:
    """Fake ccxt.gate: records set_leverage / fetch_position, no network."""

    def __init__(self, events, *, set_response=SET_OK, readback=READ_OK):
        self.events = events
        self.apiKey = DUMMY["GATE_TESTNET_API_KEY"]
        self.session = _Session(events)
        self.urls = {"api": {"private": {"futures": TESTNET}}}
        self.set_response = set_response
        self.readback = readback

    def set_leverage(self, lev, symbol, params=None):
        self.events.append(("set_leverage", lev, symbol, dict(params or {})))
        if isinstance(self.set_response, Exception):
            raise self.set_response
        return dict(self.set_response) if isinstance(self.set_response, dict) else self.set_response

    def fetch_position(self, symbol, params=None):
        self.events.append(("fetch_position", symbol))
        if isinstance(self.readback, Exception):
            raise self.readback
        if isinstance(self.readback, dict):
            out = dict(self.readback)
            out["info"] = dict(self.readback.get("info") or {})
            return out
        return self.readback


@pytest.fixture
def env(monkeypatch):
    for name, value in DUMMY.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


def _adapter(monkeypatch, cfg, events, **gate_kw):
    adapter = GateExecutionAdapter(cfg, MagicMock(name="portfolio"), mode="testnet")
    adapter._exchange = _FakeGate(events, **gate_kw)

    def _order_path(exchange, order, timeframe):
        events.append("order")
        return TradeResult(
            True, order.type, order.symbol, 1.0, 100.0, "ok",
            order_status=OrderStatus.EXECUTED,
        )

    monkeypatch.setattr(adapter, "_execute_short", _order_path)
    monkeypatch.setattr(adapter, "_execute_cover", _order_path)
    return adapter


def _order(kind="SHORT") -> TradeOrder:
    return TradeOrder(kind, SYMBOL, 100.0, 1.0, usdt_amount=100.0, leverage=2.0, signal=kind, source="manual")


def _run(adapter, kind):
    with tenant_context(TENANT):
        return adapter.execute(_order(kind), "4h")


def _exchange_calls(events):
    return [e for e in events if isinstance(e, tuple)]


# --- valid proof + dedicated names: SHORT and COVER pass --------------------


def test_short_passes_with_set_proof_and_fresh_readback(env):
    events: list = []
    adapter = _adapter(env, _cfg(), events)
    result = _run(adapter, "SHORT")
    assert result.executed is True, result.message
    assert events == [
        ("set_leverage", 2, SWAP, {"marginMode": "isolated"}),
        ("fetch_position", SWAP),
        "probe",
        "order",
    ]
    assert adapter._exchange.session.calls == [PROBE]


def test_cover_re_sets_own_leverage_then_reads_back(env):
    events: list = []
    adapter = _adapter(env, _cfg(), events)
    result = _run(adapter, "COVER")
    assert result.executed is True, result.message
    assert events == [
        ("fetch_position", SWAP),
        ("set_leverage", 2, SWAP, {"marginMode": "isolated"}),
        ("fetch_position", SWAP),
        "probe",
        "order",
    ]


def test_production_producer_returns_allowed_mode_proof(env):
    events: list = []
    adapter = _adapter(env, _cfg(), events)
    with tenant_context(TENANT):
        proof = adapter._futures_route_mode_proof(adapter._exchange, _order())
    assert proof is not None
    assert (proof.allowed, proof.reason, proof.margin_mode, proof.position_mode) == (
        True, "ok", "isolated", "one_way",
    )
    assert proof.set_invoked is False  # set comes from the adapter, not collect_mode_proof


# --- proof missing / mismatch / stale / error: denied, no probe, no order ---


@pytest.mark.parametrize("kind", ["SHORT", "COVER"])
@pytest.mark.parametrize(
    "gate_kw, reason",
    [
        ({"set_response": None}, "set_proof_missing"),
        ({"set_response": "not-a-dict"}, "set_proof_missing"),
        ({"set_response": {"leverage": "0", "mode": "single"}}, "margin_cross"),
        ({"set_response": RuntimeError("set failed")}, "mode_proof_error"),
    ],
    ids=["set_none", "set_garbage", "set_cross", "set_raises"],
)
def test_bad_set_proof_denied_without_probe_or_order(env, kind, gate_kw, reason):
    events: list = []
    adapter = _adapter(env, _cfg(), events, **gate_kw)
    result = _run(adapter, kind)
    assert result.executed is False
    assert f"futures testnet route denied: {reason}" in result.message
    assert "probe" not in events and "order" not in events


@pytest.mark.parametrize(
    "readback, reason",
    [
        ({"symbol": SWAP, "marginMode": "cross", "leverage": 0, "info": {"mode": "single"}}, "margin_cross"),
        ({"symbol": SWAP, "marginMode": "isolated", "leverage": 2, "info": {"mode": "dual_long"}}, "unknown_position"),
        ({"symbol": SWAP, "marginMode": "isolated", "leverage": 2, "hedged": True, "info": {}}, "position_mode_denied"),
        ({"symbol": SWAP, "marginMode": "isolated", "leverage": 2, "info": {}}, "unknown_position"),
    ],
    ids=["readback_cross", "readback_dual", "readback_hedged", "readback_no_mode"],
)
def test_readback_mismatch_denies_short(env, readback, reason):
    events: list = []
    adapter = _adapter(env, _cfg(), events, readback=readback)
    result = _run(adapter, "SHORT")
    assert result.executed is False
    assert f"futures testnet route denied: {reason}" in result.message
    assert "probe" not in events and "order" not in events


@pytest.mark.parametrize("readback", [None, RuntimeError("boom")], ids=["none", "raises"])
def test_readback_missing_denies_short(env, readback):
    events: list = []
    adapter = _adapter(env, _cfg(), events, readback=readback)
    result = _run(adapter, "SHORT")
    assert result.executed is False
    assert "futures testnet route denied: readback_missing" in result.message
    assert "probe" not in events and "order" not in events


@pytest.mark.parametrize("readback", [None, {"symbol": SWAP, "leverage": 0, "info": {"mode": "single"}}])
def test_cover_never_sets_on_unknown_or_cross_position(env, readback):
    events: list = []
    adapter = _adapter(env, _cfg(), events, readback=readback)
    result = _run(adapter, "COVER")
    assert result.executed is False
    assert "set_skipped_position_not_isolated" in result.message
    assert [e for e in events if isinstance(e, tuple) and e[0] == "set_leverage"] == []
    assert "probe" not in events and "order" not in events


@pytest.mark.parametrize(
    "stamps, allowed",
    [((1_000, 1_000, 1_060), True), ((1_000, 1_000, 1_061), False), ((1_000, 1_001, 1_000), False)],
    ids=["age_60_ok", "age_61_stale", "readback_after_observed"],
)
def test_freshness_limit_is_mode_proof_max_age(env, stamps, allowed):
    """Stamps are (set as_of, readback as_of, observed_at); limit is 60 s."""
    events: list = []
    adapter = _adapter(env, _cfg(), events)
    clock = iter(stamps)
    env.setattr(adapter, "_route_now", lambda: next(clock))
    result = _run(adapter, "SHORT")
    if allowed:
        assert result.executed is True, result.message
    else:
        assert result.executed is False
        assert "futures testnet route denied: stale_or_unknown" in result.message
        assert "probe" not in events and "order" not in events


def test_set_proof_older_than_limit_is_stale(env):
    events: list = []
    adapter = _adapter(env, _cfg(), events)
    clock = iter((1_000, 1_070, 1_070))
    env.setattr(adapter, "_route_now", lambda: next(clock))
    result = _run(adapter, "SHORT")
    assert result.executed is False
    assert "futures testnet route denied: stale_or_unknown" in result.message


def test_paper_start_can_only_tighten_max_age(env):
    events: list = []
    adapter = _adapter(env, _cfg(paper_start={"mode_proof_max_age_seconds": 5}), events)
    clock = iter((1_000, 1_000, 1_006))
    env.setattr(adapter, "_route_now", lambda: next(clock))
    result = _run(adapter, "SHORT")
    assert result.executed is False
    assert "stale_or_unknown" in result.message


# --- dedicated testnet key names -------------------------------------------


@pytest.mark.parametrize("kind", ["SHORT", "COVER"])
@pytest.mark.parametrize(
    "cfg_extra, env_change, reason",
    [
        ({"api_key_env": "GATE_API_KEY"}, {}, "testnet_env_aliases_live"),
        ({"api_secret_env": "GATE_API_SECRET"}, {}, "testnet_env_aliases_live"),
        ({"api_key_env": "GATE_API_SECRET"}, {}, "testnet_env_aliases_live"),
        ({}, {"GATE_TESTNET_API_KEY": DUMMY["GATE_API_KEY"]}, "testnet_env_aliases_live"),
        ({}, {"GATE_TESTNET_API_SECRET": DUMMY["GATE_API_SECRET"]}, "testnet_env_aliases_live"),
        ({}, {"GATE_TESTNET_API_KEY": None}, "testnet_credentials_unknown"),
        ({}, {"GATE_TESTNET_API_SECRET": ""}, "testnet_credentials_unknown"),
        ({"api_key_env": ""}, {}, "testnet_credentials_unknown"),
        ({"api_key_env": 7}, {}, "testnet_credentials_unknown"),
        ({"api_key_env": "X_TN", "api_secret_env": "X_TN"}, {"X_TN": "dummy"}, "testnet_credentials_unknown"),
    ],
    ids=[
        "name_aliases_live_key", "secret_name_aliases_live_secret", "key_name_aliases_live_secret",
        "value_aliases_live_key", "value_aliases_live_secret", "testnet_key_unset",
        "testnet_secret_empty", "name_empty", "name_not_str", "key_and_secret_same_name",
    ],
)
def test_testnet_key_names_must_be_dedicated(env, kind, cfg_extra, env_change, reason):
    for name, value in env_change.items():
        if value is None:
            env.delenv(name, raising=False)
        else:
            env.setenv(name, value)
    events: list = []
    adapter = _adapter(env, _cfg(**cfg_extra), events)
    logged: list[str] = []
    env.setattr("execution.gate_adapter.log", lambda msg, *a, **k: logged.append(str(msg)))
    result = _run(adapter, kind)
    assert result.executed is False
    assert "futures testnet route denied: mode_proof_missing" in result.message
    assert reason in result.message
    # No exchange call before the credentials are proven distinct.
    assert events == []
    text = result.message + " ".join(logged)
    for value in DUMMY.values():
        assert value not in text


def test_custom_testnet_names_are_honoured(env):
    env.setenv("TN_KEY_FIXTURE", "dummy-tn-k")
    env.setenv("TN_SECRET_FIXTURE", "dummy-tn-s")
    env.delenv("GATE_TESTNET_API_KEY")
    events: list = []
    adapter = _adapter(
        env, _cfg(api_key_env="TN_KEY_FIXTURE", api_secret_env="TN_SECRET_FIXTURE"), events
    )
    adapter._exchange.apiKey = "dummy-tn-k"
    assert _run(adapter, "SHORT").executed is True


def test_check_testnet_credentials_never_returns_values(env):
    from execution.futures_testnet_route import check_testnet_credentials
    import os

    ok = check_testnet_credentials(
        _cfg().raw, live_key_env="GATE_API_KEY", live_secret_env="GATE_API_SECRET", environ=os.environ
    )
    assert (ok.allowed, ok.credential_env) == (True, "GATE_TESTNET_API_KEY")
    for value in DUMMY.values():
        assert value not in repr(ok)


@pytest.mark.parametrize(
    "mode, strict, key_env",
    [
        ("testnet", True, "GATE_TESTNET_API_KEY"),
        ("testnet", _MISSING, "GATE_API_KEY"),
        ("testnet", False, "GATE_API_KEY"),
        ("real", True, "GATE_API_KEY"),
        ("shadow", True, "GATE_API_KEY"),
    ],
)
def test_client_signs_with_testnet_names_only_in_strict_testnet(env, mode, strict, key_env):
    import requests

    class _Gate:
        def __init__(self, params):
            self.params = params
            self.session = requests.Session()
            self.urls = {"api": {"private": {"futures": TESTNET}}}

        def set_sandbox_mode(self, enabled):
            self.sandbox = enabled

    env.setattr("execution.gate_adapter.ccxt.gate", _Gate)
    adapter = GateExecutionAdapter(_cfg(strict_route=strict), MagicMock(name="portfolio"), mode=mode)
    exchange = adapter._get_exchange()
    assert exchange.params["apiKey"] == DUMMY[key_env]


def test_strict_testnet_without_testnet_env_has_no_client(env):
    env.delenv("GATE_TESTNET_API_KEY")
    adapter = GateExecutionAdapter(_cfg(), MagicMock(name="portfolio"), mode="testnet")
    assert adapter._get_exchange() is None
    with tenant_context(TENANT):
        result = adapter.execute(_order(), "4h")
    assert result.executed is False
    assert "GATE_TESTNET_API_KEY" in result.message
    for value in DUMMY.values():
        assert value not in result.message


# --- strict_route off: producer never runs, existing path unchanged ---------


@pytest.mark.parametrize("strict", [_MISSING, False, "true", 1])
@pytest.mark.parametrize("kind", ["SHORT", "COVER"])
def test_strict_route_off_never_runs_the_producer(env, strict, kind):
    events: list = []
    adapter = _adapter(env, _cfg(strict_route=strict), events)
    result = _run(adapter, kind)
    assert result.executed is True
    assert events == ["order"]


# --- review round: prechecks stop the set_leverage write -------------------


@pytest.mark.parametrize("kind", ["SHORT", "COVER"])
@pytest.mark.parametrize(
    "mutate, reason",
    [
        (lambda g: setattr(g, "urls", {"api": {"private": {"futures": "https://api.gateio.ws/api/v4"}}}), "mainnet_rejected"),
        (lambda g: setattr(g.session, "follows_redirects", True), "redirects_not_disabled"),
        (lambda g: setattr(g, "apiKey", DUMMY["GATE_API_KEY"]), "client_not_testnet_signed"),
    ],
    ids=["mainnet_host", "redirect_session", "client_signed_with_live_key"],
)
def test_precheck_failure_makes_no_exchange_call(env, kind, mutate, reason):
    events: list = []
    adapter = _adapter(env, _cfg(), events)
    mutate(adapter._exchange)
    result = _run(adapter, kind)
    assert result.executed is False
    assert reason in result.message
    assert events == []


def test_mode_proof_error_message_has_no_exception_text(env):
    events: list = []
    adapter = _adapter(env, _cfg(), events, set_response=RuntimeError("body with secret-ish text"))
    result = _run(adapter, "SHORT")
    assert "mode_proof_error: RuntimeError" in result.message
    assert "secret-ish" not in result.message


# --- fix round 1: the set step is gated by authorize_route itself -----------


@pytest.mark.parametrize("kind", ["SHORT", "COVER"])
def test_denied_route_never_calls_set_leverage(env, kind):
    """Legacy testnet host: authorize_route denies before any exchange call."""
    events: list = []
    adapter = _adapter(env, _cfg(), events)
    adapter._exchange.urls = {"api": {"private": {"futures": "https://fx-api-testnet.gateio.ws/api/v4"}}}
    result = _run(adapter, kind)
    assert result.executed is False
    assert "futures testnet route denied: legacy_host_rejected" in result.message
    assert events == []


@pytest.mark.parametrize("kind", ["SHORT", "COVER"])
def test_future_authorize_route_check_also_protects_the_set_call(env, kind):
    """Any denial added to authorize_route later stops set_leverage too."""
    import execution.futures_testnet_route as route

    real = route.authorize_route
    gate_calls: list = []

    def _stricter(raw, **kw):
        if kw.get("mode_proof") is not None and kw["mode_proof"].reason == "pre_set":
            gate_calls.append(kw["method"])
            return route.RouteDecision(False, "future_check_failed")
        return real(raw, **kw)

    env.setattr(route, "authorize_route", _stricter)
    events: list = []
    adapter = _adapter(env, _cfg(), events)
    result = _run(adapter, kind)
    assert gate_calls == ["POST"]
    assert result.executed is False
    assert "mode_proof_missing: future_check_failed" in result.message
    assert events == []


@pytest.mark.parametrize("leverage", [2.5, 0.5, "1.9"])
def test_cover_denies_non_integral_position_leverage(env, leverage):
    events: list = []
    readback = {"symbol": SWAP, "marginMode": "isolated", "leverage": leverage, "info": {"mode": "single"}}
    adapter = _adapter(env, _cfg(), events, readback=readback)
    result = _run(adapter, "COVER")
    assert result.executed is False
    assert "set_skipped_leverage_not_integral" in result.message
    assert [e for e in events if isinstance(e, tuple) and e[0] == "set_leverage"] == []
    assert "probe" not in events and "order" not in events


def test_cover_accepts_whole_number_float_leverage(env):
    events: list = []
    readback = {"symbol": SWAP, "marginMode": "isolated", "leverage": 2.0, "info": {"mode": "single"}}
    adapter = _adapter(env, _cfg(), events, readback=readback)
    assert _run(adapter, "COVER").executed is True
    assert ("set_leverage", 2, SWAP, {"marginMode": "isolated"}) in events
