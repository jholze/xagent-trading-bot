"""#831: ``authorize_route`` (read intent) before every recovery startup read.

Drives the production entry ``execution.recovery.reconcile_with_exchange``
with a real ``GateExecutionAdapter`` and a fake ccxt client that records each
read. Env values are dummy placeholders set with monkeypatch. No socket.
New #831 names are resolved lazily so the file collects on base 46ec20e7.
"""

from __future__ import annotations

import pytest

from core.config import BotConfig
from execution.gate_adapter import GateExecutionAdapter
from execution import recovery as _recovery
from execution.recovery import (
    RecoveryFailed,
    reconcile_with_exchange,
    reset_recovery_log_for_tests,
)

# Resolved lazily so this file still collects on the base commit (#831 adds
# the class); on base the guard is absent and the tests fail, not error.
RecoveryRouteDenied = getattr(_recovery, "RecoveryRouteDenied", RecoveryFailed)
from services.portfolio_service import PortfolioService

TENANT = "tenant-a"
TESTNET = "https://api-testnet.gateapi.io/api/v4"
_MISSING = object()
DUMMY = {
    "GATE_API_KEY": "dummy-live-key",
    "GATE_API_SECRET": "dummy-live-secret",
    "GATE_TESTNET_API_KEY": "dummy-testnet-key",
    "GATE_TESTNET_API_SECRET": "dummy-testnet-secret",
}
EXPECTED_READS = ["fetch_balance", "fetch_open_orders", "fetch_positions"]


def _cfg(*, strict_route=True, execution="testnet") -> BotConfig:
    testnet = {"enabled": True, "tenants": [TENANT]}
    if strict_route is not _MISSING:
        testnet["strict_route"] = strict_route
    return BotConfig(
        {
            "trading_mode": "live",
            "live_confirmed": True,
            "max_usdt_per_trade": 100,
            "shorts": {
                "enabled": True,
                "allow_live": False,
                "leverage_cap": 2,
                "testnet_futures": testnet,
            },
            "live": {
                "execution": execution,
                "dry_run": False,
                "max_usdt_per_trade": 100,
                "api_key_env": "GATE_API_KEY",
                "api_secret_env": "GATE_API_SECRET",
            },
        }
    )


class _Session:
    def __init__(self, follows_redirects=False):
        if follows_redirects is not _MISSING:
            self.follows_redirects = follows_redirects
        self.calls = []

    def request(self, *a, **k):  # never used by the read guard
        self.calls.append(a)
        raise AssertionError("read guard must not send a probe")


class _ReadGate:
    """Fake testnet ccxt.gate. Every read is recorded; nothing leaves the process."""

    def __init__(self, *, private=None, follows_redirects=False, sandbox=True):
        self.reads: list[str] = []
        self.apiKey = DUMMY["GATE_TESTNET_API_KEY"]
        self.session = _Session(follows_redirects)
        bases = private if private is not None else {
            "futures": TESTNET, "spot": TESTNET, "wallet": TESTNET, "margin": TESTNET,
        }
        self.urls = {"api": {"public": dict(bases), "private": dict(bases)}}
        if sandbox is not _MISSING:
            self.isSandboxModeEnabled = sandbox

    def fetch_balance(self, *a, **k):
        self.reads.append("fetch_balance")
        return {"total": {}, "free": {}, "used": {}}

    def fetch_open_orders(self, *a, **k):
        self.reads.append("fetch_open_orders")
        return []

    def fetch_positions(self, *a, **k):
        self.reads.append("fetch_positions")
        return []

    def fetch_position(self, *a, **k):
        self.reads.append("fetch_position")
        return None

    def fetch_order(self, *a, **k):
        self.reads.append("fetch_order")
        raise Exception("not found")

    def fetch_my_trades(self, *a, **k):
        self.reads.append("fetch_my_trades")
        return []


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    reset_recovery_log_for_tests()
    import services.architecture_runtime as rt
    from strategies import positions as pos
    from strategies.positions import clear_positions_memory

    monkeypatch.setenv("MULTI_TENANT_ENABLED", "0")
    for name, value in DUMMY.items():
        monkeypatch.setenv(name, value)
    notified: list[str] = []
    monkeypatch.setattr(
        "execution.recovery.notify_operator", lambda text, **k: notified.append(text) or True
    )
    rt.reset_recovery_state_for_tests()
    clear_positions_memory()
    pos._positions_state.clear()
    yield notified
    clear_positions_memory()
    pos._positions_state.clear()
    rt.reset_recovery_state_for_tests()
    reset_recovery_log_for_tests()


def _recover(cfg, gate, mode="testnet", logs=None, monkeypatch=None):
    adapter = GateExecutionAdapter(cfg, PortfolioService(cfg), mode=mode)
    adapter._exchange = gate
    if logs is not None:
        monkeypatch.setattr("execution.gate_adapter.log", lambda m, *a, **k: logs.append(str(m)))
    return reconcile_with_exchange(tenant_id=TENANT, scope="demo", adapter=adapter, config=cfg)


def test_correct_route_performs_exactly_the_expected_reads(monkeypatch):
    gate = _ReadGate()
    logs: list[str] = []
    report = _recover(_cfg(), gate, logs=logs, monkeypatch=monkeypatch)
    assert report.skipped is False
    assert gate.reads == EXPECTED_READS
    authorized = [m for m in logs if m.startswith("futures testnet route authorized read")]
    assert authorized == [f"futures testnet route authorized read {r}" for r in EXPECTED_READS]
    assert gate.session.calls == []


@pytest.mark.parametrize(
    "gate_kw, reason",
    [
        ({"private": {"futures": TESTNET, "spot": "https://api.gateio.ws/api/v4"}}, "mainnet_rejected"),
        ({"private": {"futures": "https://evil.example/api/v4"}}, "host_rejected"),
        ({"private": {"futures": "https://fx-api-testnet.gateio.ws/api/v4"}}, "legacy_host_rejected"),
        ({"private": {"futures": "http://api-testnet.gateapi.io/api/v4"}}, "scheme_rejected"),
        ({"private": {}}, "url_missing"),
        ({"follows_redirects": True}, "redirects_not_disabled"),
        ({"follows_redirects": None}, "redirects_not_disabled"),
        ({"follows_redirects": _MISSING}, "redirects_not_disabled"),
        ({"follows_redirects": "false"}, "redirects_not_disabled"),
        ({"sandbox": _MISSING}, "read_sandbox_not_enabled"),
        ({"sandbox": False}, "read_sandbox_not_enabled"),
    ],
    ids=[
        "spot_base_mainnet", "bad_host", "legacy_host", "http", "no_private_base",
        "redirects_true", "redirects_none", "redirects_missing", "redirects_str", "no_proof", "proof_false",
    ],
)
def test_wrong_route_rejected_at_startup_before_any_read(_env, gate_kw, reason):
    gate = _ReadGate(**gate_kw)
    with pytest.raises(RecoveryRouteDenied, match=f"route denied read fetch_balance: {reason}"):
        _recover(_cfg(), gate)
    assert gate.reads == []
    assert gate.session.calls == []
    assert any("RecoveryFailed" in text for text in _env)


@pytest.mark.parametrize(
    "env_change, reason",
    [
        ({"GATE_TESTNET_API_KEY": None}, "testnet_credentials_unknown"),
        ({"GATE_TESTNET_API_KEY": DUMMY["GATE_API_KEY"]}, "testnet_env_aliases_live"),
    ],
)
def test_credentials_not_dedicated_rejected_before_any_read(monkeypatch, env_change, reason):
    for name, value in env_change.items():
        if value is None:
            monkeypatch.delenv(name)
        else:
            monkeypatch.setenv(name, value)
    gate = _ReadGate()
    with pytest.raises(RecoveryRouteDenied, match=reason) as excinfo:
        _recover(_cfg(), gate)
    assert gate.reads == []
    for value in DUMMY.values():
        assert value not in str(excinfo.value)


def test_tenant_not_named_is_rejected(monkeypatch):
    gate = _ReadGate()
    cfg = _cfg()
    cfg.raw["shorts"]["testnet_futures"]["tenants"] = ["someone-else"]
    with pytest.raises(RecoveryRouteDenied, match="tenant_mismatch"):
        _recover(cfg, gate)
    assert gate.reads == []


def test_denial_is_sticky_and_never_swallowed_by_lenient_reads(monkeypatch):
    """Lenient helpers (fetch_order, fetch_my_trades, fetch_position) re-raise it."""
    from execution import recovery

    gate = _ReadGate()
    denials = iter([None, "host_rejected"])
    adapter = GateExecutionAdapter(_cfg(), PortfolioService(_cfg()), mode="testnet")
    monkeypatch.setattr(adapter, "_read_route_denial", lambda ex, m: next(denials, "host_rejected"))
    guarded = recovery._route_guarded(gate, adapter=adapter, config=_cfg(), mode="testnet", tid=TENANT, sc="demo")
    assert guarded.fetch_balance() == {"total": {}, "free": {}, "used": {}}
    with pytest.raises(RecoveryRouteDenied):
        recovery._vwap_from_my_trades(guarded, "BASE/USDT")
    with pytest.raises(RecoveryRouteDenied):
        recovery._fetch_order_raw(guarded, {"exchange_order_id": "ex-1", "symbol": "BASE/USDT"})
    with pytest.raises(RecoveryRouteDenied):
        recovery._read_margin_mode(guarded, "BASE/USDT")
    assert gate.reads == ["fetch_balance"]
    # Non-read attributes pass straight through.
    assert guarded.urls is gate.urls


def test_route_check_exception_is_a_clean_denial(monkeypatch):
    gate = _ReadGate()
    adapter = GateExecutionAdapter(_cfg(), PortfolioService(_cfg()), mode="testnet")
    adapter._exchange = gate

    def _boom(*a, **k):
        raise RuntimeError("bad urls")

    monkeypatch.setattr(adapter, "_private_api_bases", _boom)
    with pytest.raises(RecoveryRouteDenied, match="route_check_error"):
        reconcile_with_exchange(tenant_id=TENANT, scope="demo", adapter=adapter, config=_cfg())
    assert gate.reads == []


def test_denied_recovery_skips_the_cycle_without_crashing(monkeypatch):
    """RecoveryRouteDenied is a RecoveryFailed: ensure_started raises it, the
    aria_bot cycle logs and returns (existing #314 handling)."""
    import aria_bot

    calls: list[str] = []
    monkeypatch.setattr(
        "services.architecture_runtime.ensure_started",
        lambda *a, **k: (_ for _ in ()).throw(RecoveryRouteDenied("route denied read fetch_balance: host_rejected")),
    )
    monkeypatch.setattr(aria_bot, "load_effective_watchlist", lambda *a, **k: calls.append("watchlist") or [])
    logged: list[tuple] = []
    monkeypatch.setattr(aria_bot, "log", lambda *a, **k: logged.append(a))
    aria_bot._run_tenant_price_cycle(
        cycle_started=0.0, use_dashboard=False,
        analyzer=None, orchestrator=None, social_pipeline=None, sandbox=None, trend_engine=None,
    )
    assert calls == []
    assert any("exchange recovery failed" in str(a[0]) for a in logged)


# --- unchanged paths ---------------------------------------------------------


@pytest.mark.parametrize("strict", [_MISSING, False, "true", 1])
def test_strict_route_off_keeps_todays_reads_without_route_check(monkeypatch, strict):
    gate = _ReadGate(private={"futures": "https://api.gateio.ws/api/v4"}, follows_redirects=True, sandbox=_MISSING)
    called: list = []
    monkeypatch.setattr(
        GateExecutionAdapter, "_read_route_denial", lambda self, ex, m: called.append(m) or "denied"
    )
    _recover(_cfg(strict_route=strict), gate)
    assert gate.reads == EXPECTED_READS
    assert called == []


def test_real_mode_never_route_checks_reads(monkeypatch):
    gate = _ReadGate(private={"futures": "https://api.gateio.ws/api/v4"}, follows_redirects=True)
    called: list = []
    monkeypatch.setattr(
        GateExecutionAdapter, "_read_route_denial", lambda self, ex, m: called.append(m) or "denied"
    )
    _recover(_cfg(execution="real"), gate, mode="real")
    assert gate.reads == EXPECTED_READS
    assert called == []


def test_shadow_mode_still_skips_without_reads():
    gate = _ReadGate()
    report = _recover(_cfg(execution="shadow"), gate, mode="shadow")
    assert report.skipped is True
    assert gate.reads == []


def test_client_signed_with_live_key_is_rejected_before_any_read():
    gate = _ReadGate()
    gate.apiKey = DUMMY["GATE_API_KEY"]
    with pytest.raises(RecoveryRouteDenied, match="read_client_not_testnet_signed"):
        _recover(_cfg(), gate)
    assert gate.reads == []
