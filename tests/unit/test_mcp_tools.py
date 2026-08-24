from services.mcp_authz import Actor, authorize
from services.mcp_tools import tool_snapshot, tool_lots, tool_whoami

OWNER = Actor("jens", "owner", ("*",), ("read", "trade", "lock", "config_read", "kill"))
HENRY = Actor("henry-op", "operator", ("henry",), ("read", "trade", "lock"))


def test_whoami():
    w = tool_whoami(HENRY)
    assert w["actor_id"] == "henry-op" and list(w["tenants"]) == ["henry"]


def test_operator_forced_tenant():
    calls = []

    def fake_snap(**kw):
        calls.append(kw)
        return {"ok": True, "tenant_id": kw["tenant_id"], "lots": [{"symbol": "AAA/USDT"}]}

    out = tool_snapshot(HENRY, tenant="default", symbol="LAB/USDT", snapshot_fn=fake_snap)
    assert out["ok"] is True
    assert out["tenant_id"] == "henry"  # forced
    assert calls[0]["tenant_id"] == "henry"


def test_operator_cannot_see_default():
    ok, err = authorize(HENRY, "read", "default")
    assert not ok and err == "tenant_forbidden"


def test_snapshot_deny_does_not_call():
    called = []

    def fake(**kw):
        called.append(1)
        return {"ok": True}

    out = tool_snapshot(None, tenant="henry", symbol="X", snapshot_fn=fake)
    assert out["ok"] is False and out["error"] == "unauthorized"
    assert called == []


def test_lots_from_snapshot():
    def fake_snap(**kw):
        return {"ok": True, "tenant_id": kw["tenant_id"], "lots": [{"symbol": "AAA/USDT"}]}

    out = tool_lots(HENRY, tenant="default", symbol="LAB/USDT", snapshot_fn=fake_snap)
    assert out["ok"] is True
    assert out["tenant_id"] == "henry"
    assert out["lots"] == [{"symbol": "AAA/USDT"}]


def test_snapshot_exception_is_snapshot_failed():
    def boom(**kw):
        raise RuntimeError("mongo down")

    out = tool_snapshot(HENRY, tenant="henry", symbol="X", snapshot_fn=boom)
    assert out["ok"] is False and out["error"] == "snapshot_failed"


def test_owner_uses_requested_tenant():
    calls = []

    def fake_snap(**kw):
        calls.append(kw)
        return {"ok": True, "tenant_id": kw["tenant_id"], "lots": []}

    out = tool_snapshot(OWNER, tenant="ctexp", symbol="LAB/USDT", snapshot_fn=fake_snap)
    assert out["ok"] is True
    assert out["tenant_id"] == "ctexp"
    assert calls[0]["tenant_id"] == "ctexp"
