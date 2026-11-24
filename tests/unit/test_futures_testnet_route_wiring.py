"""#826: GateExecutionAdapter runs #804 ``authorize_route`` before testnet futures.

Fakes only. No keys are read (the fake exchange is injected, so
``_get_exchange`` never looks at the environment) and no socket is opened
(requests tests mount an in-process adapter).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from core.config import BotConfig
from core.models import OrderStatus, TradeOrder, TradeResult
from core.tenant_context import tenant_context
from execution.gate_adapter import GateExecutionAdapter

TENANT = "tenant-a"
SYMBOL = "BASE/USDT"
TESTNET_FUTURES = "https://api-testnet.gateapi.io/api/v4"
_MISSING = object()


def _cfg(*, strict_route=True) -> BotConfig:
    testnet = {"enabled": True, "tenants": [TENANT]}
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
    """Stands in for ccxt's ``exchange.session``. Records every request."""

    def __init__(self, follows_redirects=_MISSING, status=200, events=None):
        if follows_redirects is not _MISSING:
            self.follows_redirects = follows_redirects
        self.calls = []
        self._status = status
        self._events = events if events is not None else []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs.get("allow_redirects")))
        self._events.append("transport")
        return _Response(self._status)


def _exchange(session):
    ex = MagicMock(name="ccxt.gate")
    ex.session = session
    ex.urls = {"api": {"private": {"futures": TESTNET_FUTURES}}}
    return ex


def _adapter(monkeypatch, cfg, session, events, *, proof_ok=True, distinct_env=True):
    from execution.futures_testnet_route import ModeProof

    adapter = GateExecutionAdapter(cfg, MagicMock(name="portfolio"), mode="testnet")
    adapter._exchange = _exchange(session)
    if proof_ok:
        monkeypatch.setattr(
            adapter,
            "_futures_route_mode_proof",
            lambda exchange, order: ModeProof(True, "ok", "isolated", "one_way"),
            raising=False,
        )
    if distinct_env:
        monkeypatch.setattr(
            adapter,
            "_futures_route_credentials",
            lambda: ("GATE_API_KEY", "GATE_TESTNET_API_KEY"),
            raising=False,
        )

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


@pytest.mark.parametrize(
    "follows",
    [True, None, _MISSING, 1, "false", MagicMock()],
    ids=["true", "none", "missing", "one", "str_false", "mock"],
)
@pytest.mark.parametrize("kind", ["SHORT", "COVER"])
def test_redirect_capable_transport_denied_before_any_call(monkeypatch, follows, kind):
    events: list[str] = []
    session = _Session(follows_redirects=follows, events=events)
    adapter = _adapter(monkeypatch, _cfg(), session, events)
    with tenant_context(TENANT):
        result = adapter.execute(_order(kind), "4h")
    assert result.executed is False
    assert "futures testnet route denied: redirects_not_disabled" in result.message
    assert session.calls == []
    assert events == []


@pytest.mark.parametrize("kind", ["SHORT", "COVER"])
def test_literal_false_transport_probe_once_then_order(monkeypatch, kind):
    events: list[str] = []
    session = _Session(follows_redirects=False, events=events)
    adapter = _adapter(monkeypatch, _cfg(), session, events)
    with tenant_context(TENANT):
        result = adapter.execute(_order(kind), "4h")
    assert result.executed is True, result.message
    assert session.calls == [
        ("GET", f"{TESTNET_FUTURES}/futures/usdt/contracts/BASE_USDT", False)
    ]
    assert events == ["transport", "order"]


def test_redirect_response_denied_and_no_order(monkeypatch):
    events: list[str] = []
    session = _Session(follows_redirects=False, status=302, events=events)
    adapter = _adapter(monkeypatch, _cfg(), session, events)
    with tenant_context(TENANT):
        result = adapter.execute(_order(), "4h")
    assert result.executed is False
    assert "redirect_rejected" in result.message
    assert events == ["transport"]


def test_mainnet_url_denied_without_transport(monkeypatch):
    events: list[str] = []
    session = _Session(follows_redirects=False, events=events)
    adapter = _adapter(monkeypatch, _cfg(), session, events)
    adapter._exchange.urls = {"api": {"private": {"futures": "https://api.gateio.ws/api/v4"}}}
    with tenant_context(TENANT):
        result = adapter.execute(_order(), "4h")
    assert "mainnet_rejected" in result.message
    assert events == []


def test_production_defaults_deny_mode_proof_missing_without_transport(monkeypatch):
    """No ModeProof producer exists yet: strict_route on fails closed, no call."""
    events: list[str] = []
    session = _Session(follows_redirects=False, events=events)
    adapter = _adapter(monkeypatch, _cfg(), session, events, proof_ok=False, distinct_env=False)
    with tenant_context(TENANT):
        result = adapter.execute(_order(), "4h")
    assert "mode_proof_missing" in result.message
    assert events == []


@pytest.mark.parametrize("strict", [_MISSING, False, "true", 1])
def test_strict_route_off_keeps_existing_adapter_path(monkeypatch, strict):
    """RouteDecision(strict_route_off).inhibits_existing_adapter is False."""
    events: list[str] = []
    session = _Session(follows_redirects=True, events=events)
    adapter = _adapter(monkeypatch, _cfg(strict_route=strict), session, events)
    with tenant_context(TENANT):
        result = adapter.execute(_order(), "4h")
    assert result.executed is True
    assert events == ["order"]
    assert adapter._exchange.session is session


def test_route_check_error_is_clean_denial(monkeypatch):
    events: list[str] = []
    session = _Session(follows_redirects=False, events=events)
    adapter = _adapter(monkeypatch, _cfg(), session, events)

    def _boom(exchange, order):
        raise RuntimeError("bad url")

    monkeypatch.setattr(adapter, "_futures_route_url", _boom, raising=False)
    with tenant_context(TENANT):
        result = adapter.execute(_order(), "4h")
    assert result.executed is False
    assert "route_check_error" in result.message
    assert events == []


# --- the production transport really disables redirects --------------------
# The suite's network guard wraps requests.Session.send for every non-loopback
# host, so these use loopback URLs served by an in-process adapter.

LOCAL = "https://127.0.0.1/api/v4/futures/usdt/orders"
LOCAL_TARGET = "https://127.0.0.2/api/v4/futures/usdt/orders"


class _RedirectingHTTP(requests.adapters.BaseAdapter):
    """In-process HTTP: always answers 302 elsewhere. Counts sends, no socket."""

    def __init__(self):
        super().__init__()
        self.sent: list[str] = []

    def send(self, request, **kwargs):
        self.sent.append(request.url)
        response = requests.Response()
        response.status_code = 302
        response.headers["Location"] = LOCAL_TARGET
        response.url = request.url
        response.request = request
        response._content = b""
        return response

    def close(self):
        pass


def test_no_redirect_session_returns_3xx_without_second_request():
    from execution.futures_testnet_route import NoRedirectSession

    session = NoRedirectSession()
    http = _RedirectingHTTP()
    session.mount("https://", http)
    assert session.follows_redirects is False
    response = session.request("POST", LOCAL, allow_redirects=True)
    assert response.status_code == 302
    assert http.sent == [LOCAL]


def test_plain_requests_session_would_follow_the_redirect():
    """Control: the default ccxt session follows, which is why it is swapped."""
    session = requests.Session()
    session.max_redirects = 1
    http = _RedirectingHTTP()
    session.mount("https://", http)
    with pytest.raises(requests.TooManyRedirects):
        session.request("POST", LOCAL)
    assert LOCAL_TARGET in http.sent


def test_adapter_swaps_real_ccxt_session_and_probes_once(monkeypatch):
    from execution.futures_testnet_route import NoRedirectSession

    events: list[str] = []
    adapter = _adapter(monkeypatch, _cfg(), _Session(), events)
    adapter._exchange.session = requests.Session()
    sent = []

    def _send(self, request, **kwargs):
        sent.append((request.method, request.url, kwargs.get("allow_redirects")))
        response = requests.Response()
        response.status_code = 200
        response.request = request
        response._content = b"{}"
        return response

    def _base_request(self, method, url, **kwargs):
        # Stand-in for the guarded requests.Session.request: prepare + send.
        prepared = requests.Request(method, url).prepare()
        return self.send(prepared, allow_redirects=kwargs.get("allow_redirects"))

    monkeypatch.setattr(requests.Session, "request", _base_request)
    monkeypatch.setattr(NoRedirectSession, "send", _send)
    with tenant_context(TENANT):
        result = adapter.execute(_order(), "4h")
    assert result.executed is True, result.message
    assert isinstance(adapter._exchange.session, NoRedirectSession)
    assert sent == [("GET", f"{TESTNET_FUTURES}/futures/usdt/contracts/BASE_USDT", False)]
    assert events == ["order"]


def test_live_and_shadow_modes_never_reach_the_route(monkeypatch):
    for mode in ("real", "shadow"):
        adapter = GateExecutionAdapter(_cfg(), MagicMock(name="portfolio"), mode=mode)
        called = []
        monkeypatch.setattr(
            adapter, "_futures_route_denial", lambda *a: called.append(a), raising=False
        )
        adapter._exchange = _exchange(_Session(follows_redirects=False))
        monkeypatch.setattr(adapter, "_execute_short_shadow", lambda *a: None)
        with tenant_context(TENANT):
            adapter.execute(_order(), "4h")
        assert called == []


def test_route_check_error_with_strict_route_off_keeps_existing_path(monkeypatch):
    events: list[str] = []
    session = _Session(follows_redirects=True, events=events)
    adapter = _adapter(monkeypatch, _cfg(strict_route=_MISSING), session, events)

    def _boom(exchange, order):
        raise RuntimeError("bad url")

    monkeypatch.setattr(adapter, "_futures_route_url", _boom, raising=False)
    with tenant_context(TENANT):
        result = adapter.execute(_order(), "4h")
    assert result.executed is True
    assert events == ["order"]


def test_transport_independent_denial_leaves_ccxt_session_untouched(monkeypatch):
    """Item 3: production defaults deny before the probe transport is built."""
    events: list[str] = []
    adapter = _adapter(monkeypatch, _cfg(), _Session(), events, proof_ok=False, distinct_env=False)
    plain = requests.Session()
    adapter._exchange.session = plain
    with tenant_context(TENANT):
        result = adapter.execute(_order(), "4h")
    assert "mode_proof_missing" in result.message
    assert adapter._exchange.session is plain
    assert events == []


# --- item 4: every testnet-mode ccxt client gets the no-redirect session ----


class _FakeGate:
    """Stands in for ccxt.gate: no network, keeps the params it was built with."""

    def __init__(self, params):
        self.params = params
        self.session = requests.Session()
        self.urls = {"api": {"private": {"futures": "https://api.gateio.ws/api/v4"}}}
        self.sandbox = False

    def set_sandbox_mode(self, enabled):
        self.sandbox = enabled


@pytest.mark.parametrize(
    "mode, no_redirect",
    [("testnet", True), ("real", False), ("shadow", False)],
)
def test_get_exchange_installs_no_redirect_session_only_in_testnet(monkeypatch, mode, no_redirect):
    from execution.futures_testnet_route import NoRedirectSession

    # Dummy placeholders so _get_exchange builds a client; not credentials.
    monkeypatch.setenv("GATE_API_KEY", "dummy-not-a-key")
    monkeypatch.setenv("GATE_API_SECRET", "dummy-not-a-secret")
    monkeypatch.setattr("execution.gate_adapter.ccxt.gate", _FakeGate)
    adapter = GateExecutionAdapter(_cfg(strict_route=_MISSING), MagicMock(name="portfolio"), mode=mode)
    exchange = adapter._get_exchange()
    assert isinstance(exchange, _FakeGate)
    assert isinstance(exchange.session, NoRedirectSession) is no_redirect
    if not no_redirect:
        assert type(exchange.session) is requests.Session
    assert exchange.sandbox is (mode == "testnet")


def test_recovery_client_in_testnet_uses_no_redirect_session(monkeypatch):
    """execution/recovery.py takes its client from adapter._get_exchange."""
    from execution import recovery
    from execution.futures_testnet_route import NoRedirectSession

    monkeypatch.setenv("GATE_API_KEY", "dummy-not-a-key")
    monkeypatch.setenv("GATE_API_SECRET", "dummy-not-a-secret")
    monkeypatch.setattr("execution.gate_adapter.ccxt.gate", _FakeGate)
    adapter = GateExecutionAdapter(_cfg(strict_route=_MISSING), MagicMock(name="portfolio"), mode="testnet")
    client = recovery._ccxt_client(adapter)
    assert isinstance(client.session, NoRedirectSession)
