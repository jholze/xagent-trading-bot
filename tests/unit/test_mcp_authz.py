import sys

from services.mcp.authz import (
    Actor,
    authorize,
    check_write_rate,
    mcp_allow_live,
    mcp_allowed_tenants,
    mcp_enabled,
    mcp_live_writes_blocked,
    mcp_tenant_allowed,
    mcp_write_rate_per_min,
    mcp_writes_enabled,
    reset_write_rate,
)


def test_mcp_disabled_by_default_without_flag():
    assert mcp_enabled({}) is False
    assert mcp_writes_enabled({}) is False


def test_mcp_flags():
    raw = {"mcp": {"enabled": True, "allow_writes": True}}
    assert mcp_enabled(raw) is True
    assert mcp_writes_enabled(raw) is True
    assert mcp_writes_enabled({"mcp": {"enabled": True, "allow_writes": False}}) is False


def test_mcp_non_dict_fail_closed():
    assert mcp_enabled({"mcp": True}) is False
    assert mcp_writes_enabled({"mcp": True}) is False


OWNER = Actor("jens", "owner", ("*",), ("read", "trade", "lock", "config_read", "kill"))
HENRY = Actor("henry-op", "operator", ("henry",), ("read", "trade", "lock"))
OBS = Actor("henry-obs", "observer", ("henry",), ("read",))


def test_owner_can_trade_ctexp():
    ok, err = authorize(OWNER, "trade", "ctexp", writes_enabled=True)
    assert ok and err == ""


def test_operator_cannot_read_default():
    ok, err = authorize(HENRY, "read", "default")
    assert ok is False and err == "tenant_forbidden"


def test_observer_cannot_buy():
    ok, err = authorize(OBS, "trade", "henry", writes_enabled=True)
    assert ok is False and err == "forbidden"


def test_writes_kill():
    ok, err = authorize(OWNER, "trade", "default", writes_enabled=False)
    assert ok is False and err == "writes_disabled"


def test_missing_actor():
    ok, err = authorize(None, "read", "default")
    assert ok is False and err == "unauthorized"


def test_disabled_gate():
    ok, err = authorize(OWNER, "read", "default", enabled=False)
    assert ok is False and err == "mcp_disabled"


def test_allowed_tenants_default_and_explicit():
    assert mcp_allowed_tenants({}) == ["default", "henry", "ctexp"]
    assert mcp_tenant_allowed("henry", {"mcp": {"tenants": ["henry"]}}) is True
    assert mcp_tenant_allowed("default", {"mcp": {"tenants": ["henry"]}}) is False


def test_allow_live_default_false():
    assert mcp_allow_live({}) is False
    assert mcp_allow_live({"mcp": {"enabled": True, "allow_writes": True}}) is False
    assert mcp_allow_live({"mcp": {"allow_live": True}}) is True


def test_live_writes_blocked_only_for_real_live(monkeypatch):
    # #410: real means live.execution resolves to real, not merely dry_run=false.
    monkeypatch.setenv("DEMO_MODE", "0")
    monkeypatch.setenv("GATE_API_KEY", "k")
    monkeypatch.setenv("GATE_API_SECRET", "s")
    paper = {"trading_mode": "live", "live_confirmed": True, "live": {"dry_run": True}}
    real = {
        "trading_mode": "live",
        "live_confirmed": True,
        "live": {"execution": "real", "dry_run": False},
    }
    testnet = {**real, "live": {"execution": "testnet", "dry_run": False}}
    assert mcp_live_writes_blocked(paper) is False
    assert mcp_live_writes_blocked(testnet) is False
    assert mcp_live_writes_blocked(real) is True
    assert mcp_live_writes_blocked({**real, "mcp": {"allow_live": True}}) is False


def _capture_warnings(monkeypatch):
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "services.mcp.authz.log", lambda msg, level="INFO": seen.append((str(msg), level))
    )
    return seen


def _real_live_cfg():
    return {
        "trading_mode": "live",
        "live_confirmed": True,
        "live": {"execution": "real", "dry_run": False},
        "mcp": {"enabled": True, "allow_writes": True, "allow_live": False},
    }


def test_live_writes_blocked_when_live_section_is_none(monkeypatch):
    # #426: cfg['live']=None is undeterminable → fail-closed, never open.
    seen = _capture_warnings(monkeypatch)
    cfg = {"trading_mode": "live", "live_confirmed": True, "live": None}
    assert mcp_live_writes_blocked(cfg) is True
    assert seen and seen[-1][1] == "WARNING"
    assert "malformed live section" in seen[-1][0]


def test_live_writes_blocked_when_live_section_not_dict(monkeypatch):
    seen = _capture_warnings(monkeypatch)
    for bad in ("real", 1, ["execution", "real"], True):
        seen.clear()
        cfg = {"trading_mode": "live", "live_confirmed": True, "live": bad}
        assert mcp_live_writes_blocked(cfg) is True, bad
        assert seen and seen[-1][1] == "WARNING", bad


def test_live_writes_blocked_when_config_not_dict(monkeypatch):
    seen = _capture_warnings(monkeypatch)
    for bad in (None, "live", 42, ["live"]):
        seen.clear()
        assert mcp_live_writes_blocked(bad) is True, bad
        assert seen and seen[-1][1] == "WARNING", bad


def test_live_writes_blocked_on_import_error(monkeypatch):
    # #426: a failing import of the live resolver must block, not silently allow.
    seen = _capture_warnings(monkeypatch)
    monkeypatch.setitem(sys.modules, "core.simulated_trading", None)
    cfg = {"trading_mode": "live", "live_confirmed": True, "live": {"dry_run": True}}
    assert mcp_live_writes_blocked(cfg) is True
    assert seen and seen[-1][1] == "WARNING"
    assert "cannot determine live state" in seen[-1][0]


def test_live_writes_blocked_on_resolver_exception(monkeypatch):
    seen = _capture_warnings(monkeypatch)
    import core.simulated_trading as sim

    def boom(_cfg=None):
        raise ValueError("resolver broken")

    monkeypatch.setattr(sim, "is_real_live_trading", boom)
    cfg = {"trading_mode": "live", "live_confirmed": True, "live": {"dry_run": True}}
    assert mcp_live_writes_blocked(cfg) is True
    assert seen and seen[-1][1] == "WARNING"
    assert "resolver broken" in seen[-1][0]


def test_live_writes_only_allow_live_true_opens_gate(monkeypatch):
    # #426: only a literal True on mcp.allow_live opens the gate on real live.
    monkeypatch.setenv("DEMO_MODE", "0")
    monkeypatch.setenv("GATE_API_KEY", "k")
    monkeypatch.setenv("GATE_API_SECRET", "s")
    real = _real_live_cfg()
    assert mcp_live_writes_blocked(real) is True
    for not_true in (False, None, 0, ""):
        cfg = {**real, "mcp": {**real["mcp"], "allow_live": not_true}}
        assert mcp_live_writes_blocked(cfg) is True, not_true
    assert mcp_live_writes_blocked({**real, "mcp": {**real["mcp"], "allow_live": True}}) is False


def test_live_writes_allow_live_opens_gate_even_when_undeterminable(monkeypatch):
    # allow_live is the operator's explicit override; it wins before any probing.
    seen = _capture_warnings(monkeypatch)
    cfg = {"trading_mode": "live", "live_confirmed": True, "live": None, "mcp": {"allow_live": True}}
    assert mcp_live_writes_blocked(cfg) is False
    assert seen == []


def test_write_rate_per_min_default():
    assert mcp_write_rate_per_min({}) == 20
    assert mcp_write_rate_per_min({"mcp": {"write_rate_per_min": 5}}) == 5


def test_check_write_rate_limits_actor():
    reset_write_rate()
    now = 1_000_000.0
    assert check_write_rate("jens", per_min=2, now=now)[0] is True
    assert check_write_rate("jens", per_min=2, now=now + 1)[0] is True
    ok, err = check_write_rate("jens", per_min=2, now=now + 2)
    assert ok is False and err == "rate_limited"
    assert check_write_rate("henry-op", per_min=2, now=now + 2)[0] is True
    assert check_write_rate("jens", per_min=2, now=now + 61)[0] is True
