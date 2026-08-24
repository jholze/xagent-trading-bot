from services.mcp_authz import Actor, authorize, mcp_enabled, mcp_writes_enabled


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
