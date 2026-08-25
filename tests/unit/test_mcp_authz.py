from services.mcp_authz import (
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


def test_live_writes_blocked_only_for_real_live():
    paper = {"trading_mode": "live", "live_confirmed": True, "live": {"dry_run": True}}
    real = {"trading_mode": "live", "live_confirmed": True, "live": {"dry_run": False}}
    assert mcp_live_writes_blocked(paper) is False
    assert mcp_live_writes_blocked(real) is True
    assert mcp_live_writes_blocked({**real, "mcp": {"allow_live": True}}) is False


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
