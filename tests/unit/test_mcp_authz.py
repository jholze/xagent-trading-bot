from services.mcp_authz import mcp_enabled, mcp_writes_enabled


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
