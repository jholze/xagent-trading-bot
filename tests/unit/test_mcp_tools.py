from services.mcp.authz import Actor, authorize, reset_write_rate
from services.mcp.tools import (
    tool_snapshot,
    tool_lots,
    tool_whoami,
    tool_buy,
    tool_cover,
    tool_sell,
    tool_short,
    tool_lock,
    tool_unlock,
    write_idempotency_key,
)

OWNER = Actor("jens", "owner", ("*",), ("read", "trade", "lock", "config_read", "kill"))
HENRY = Actor("henry-op", "operator", ("henry",), ("read", "trade", "lock"))
OBS = Actor("o", "observer", ("henry",), ("read",))


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


def test_buy_denied_for_observer():
    called = []
    out = tool_buy(OBS, tenant="henry", symbol="LAB/USDT", usdt=10, execute_fn=lambda **k: called.append(k) or {"ok": True})
    assert out["error"] == "forbidden" and called == []


def test_owner_buy_posts_tenant():
    reset_write_rate()
    called = []
    out = tool_buy(OWNER, tenant="henry", symbol="LAB/USDT", usdt=25, execute_fn=lambda **k: called.append(k) or {"ok": True, "executed": True})
    assert out["ok"] is True
    assert called[0]["tenant_id"] == "henry"
    assert called[0]["action"] == "buy"
    assert called[0]["actor_id"] == "jens"
    assert str(called[0].get("idempotency_key") or "").startswith("mcp:")


def test_operator_buy_forced_off_default():
    called = []
    out = tool_buy(HENRY, tenant="default", symbol="LAB/USDT", usdt=10, execute_fn=lambda **k: called.append(k) or {"ok": True, "executed": True})
    assert called[0]["tenant_id"] == "henry"


def test_buy_writes_disabled_does_not_call():
    called = []
    out = tool_buy(
        OWNER,
        tenant="henry",
        symbol="LAB/USDT",
        usdt=10,
        writes_enabled=False,
        execute_fn=lambda **k: called.append(k) or {"ok": True},
    )
    assert out["ok"] is False and out["error"] == "writes_disabled"
    assert called == []


def test_sell_denied_for_observer():
    called = []
    out = tool_sell(
        OBS,
        tenant="henry",
        symbol="LAB/USDT",
        pct=50,
        execute_fn=lambda **k: called.append(k) or {"ok": True},
    )
    assert out["error"] == "forbidden" and called == []


def test_owner_sell_posts():
    called = []
    out = tool_sell(
        OWNER,
        tenant="henry",
        symbol="LAB/USDT",
        pct=50,
        execute_fn=lambda **k: called.append(k) or {"ok": True, "executed": True},
    )
    assert out["ok"] is True
    assert called[0]["action"] == "sell"
    assert called[0]["tenant_id"] == "henry"
    assert called[0]["actor_id"] == "jens"
    assert called[0]["pct"] == 50


def test_short_denied_for_observer():
    called = []
    out = tool_short(
        OBS,
        tenant="henry",
        symbol="LAB/USDT",
        usdt=10,
        execute_fn=lambda **k: called.append(k) or {"ok": True},
    )
    assert out["error"] == "forbidden" and called == []


def test_owner_short_posts():
    reset_write_rate()
    called = []
    out = tool_short(
        OWNER,
        tenant="henry",
        symbol="LAB/USDT",
        usdt=25,
        leverage=2,
        execute_fn=lambda **k: called.append(k) or {"ok": True, "executed": True},
    )
    assert out["ok"] is True
    assert called[0]["action"] == "short"
    assert called[0]["tenant_id"] == "henry"


def test_cover_denied_for_observer():
    called = []
    out = tool_cover(
        OBS,
        tenant="henry",
        symbol="LAB/USDT",
        pct=100,
        execute_fn=lambda **k: called.append(k) or {"ok": True},
    )
    assert out["error"] == "forbidden" and called == []


def test_lock_denied_for_observer():
    called = []
    out = tool_lock(
        OBS,
        tenant="henry",
        symbol="LAB/USDT",
        execute_fn=lambda **k: called.append(k) or {"ok": True},
    )
    assert out["error"] == "forbidden" and called == []


def test_owner_lock_and_unlock_post():
    called = []
    out = tool_lock(
        OWNER,
        tenant="henry",
        symbol="LAB/USDT",
        reason="hold",
        execute_fn=lambda **k: called.append(k) or {"ok": True, "executed": True},
    )
    assert out["ok"] is True
    assert called[0]["action"] == "lock"
    assert called[0]["tenant_id"] == "henry"
    assert called[0]["actor_id"] == "jens"
    called.clear()
    out = tool_unlock(
        OWNER,
        tenant="henry",
        symbol="LAB/USDT",
        execute_fn=lambda **k: called.append(k) or {"ok": True, "executed": True},
    )
    assert out["ok"] is True
    assert called[0]["action"] == "unlock"
    assert called[0]["tenant_id"] == "henry"


def test_operator_lock_forced_off_default():
    called = []
    tool_lock(
        HENRY,
        tenant="default",
        symbol="LAB/USDT",
        execute_fn=lambda **k: called.append(k) or {"ok": True, "executed": True},
    )
    assert called[0]["tenant_id"] == "henry"


def test_execute_network_error_is_bot_unreachable(monkeypatch):
    import urllib.error

    monkeypatch.setenv("MCP_BOT_URL", "https://bot.example")
    monkeypatch.setenv("EXIT_WS_INTERNAL_TOKEN", "secret")
    monkeypatch.delenv("MCP_BOT_TOKEN", raising=False)

    def boom(*_a, **_k):
        raise urllib.error.URLError("down")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    from services.mcp.client import execute

    out = execute(action="buy", tenant_id="henry", symbol="LAB/USDT", usdt=10)
    assert out == {"ok": False, "error": "bot_unreachable"}


def test_execute_posts_json_with_token(monkeypatch):
    captured = {}

    class _Resp:
        def read(self):
            return b'{"ok": true, "executed": true}'

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        captured["method"] = req.get_method()
        captured["data"] = req.data
        captured["token"] = req.get_header("X-exit-ws-token")
        return _Resp()

    monkeypatch.setenv("MCP_BOT_URL", "https://bot.example")
    monkeypatch.setenv("EXIT_WS_INTERNAL_TOKEN", "secret")
    monkeypatch.delenv("MCP_BOT_TOKEN", raising=False)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    from services.mcp.client import execute

    out = execute(action="buy", tenant_id="henry", symbol="LAB/USDT", usdt=25, actor_id="jens")
    assert out.get("ok") is True
    assert captured["url"] == "https://bot.example/internal/mcp/execute"
    assert captured["timeout"] == 45
    assert captured["method"] == "POST"
    assert captured["token"] == "secret"
    import json

    body = json.loads(captured["data"].decode("utf-8"))
    assert body["action"] == "buy"
    assert body["tenant_id"] == "henry"


def test_write_idempotency_stable_in_same_bucket():
    a = write_idempotency_key(
        actor_id="jens",
        action="buy",
        tenant_id="default",
        symbol="BLESS/USDT",
        usdt=2500,
        now=90.0,
    )
    b = write_idempotency_key(
        actor_id="jens",
        action="buy",
        tenant_id="default",
        symbol="BLESS/USDT",
        usdt=2500,
        now=110.0,
    )
    c = write_idempotency_key(
        actor_id="jens",
        action="buy",
        tenant_id="default",
        symbol="BLESS/USDT",
        usdt=2500,
        now=120.0,
    )
    assert a == b
    assert a != c
    assert a.startswith("mcp:")


def test_buy_rate_limited_does_not_call():
    reset_write_rate()
    called = []

    def exec_fn(**k):
        called.append(k)
        return {"ok": True, "executed": True}

    kwargs = dict(
        actor=OWNER,
        tenant="henry",
        symbol="LAB/USDT",
        usdt=10,
        execute_fn=exec_fn,
        rate_per_min=1,
        now=2_000_000.0,
    )
    assert tool_buy(**kwargs)["ok"] is True
    out = tool_buy(**kwargs)
    assert out["ok"] is False and out["error"] == "rate_limited"
    assert len(called) == 1


def test_execute_timeout_env_override(monkeypatch):
    captured = {}

    class _Resp:
        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    def fake_urlopen(req, timeout=None):
        captured["timeout"] = timeout
        return _Resp()

    monkeypatch.setenv("MCP_BOT_URL", "https://bot.example")
    monkeypatch.setenv("EXIT_WS_INTERNAL_TOKEN", "secret")
    monkeypatch.setenv("MCP_BOT_TIMEOUT_SEC", "60")
    monkeypatch.delenv("MCP_BOT_TOKEN", raising=False)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    from services.mcp.client import execute

    execute(action="buy", tenant_id="default", symbol="BLESS/USDT", usdt=2500)
    assert captured["timeout"] == 60
