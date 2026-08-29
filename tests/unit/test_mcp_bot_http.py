"""MCP bot execute route: token-gated POST on a tiny Flask app (never aria_bot.app)."""

from __future__ import annotations

from types import SimpleNamespace

from flask import Flask

_PATH = "/internal/mcp/execute"
_BUY_BODY = {
    "action": "buy",
    "tenant_id": "henry",
    "symbol": "LAB/USDT",
    "usdt": 50,
    "timeframe": "1h",
    "actor_id": "jens",
}
_MCP_CFG = {
    "trading_mode": "live",
    "live_confirmed": True,
    "live": {"dry_run": True},
    "mcp": {
        "enabled": True,
        "allow_writes": True,
        "allow_live": False,
        "tenants": ["default", "henry", "ctexp"],
    },
}


def _client(monkeypatch, *, patch_trading=True, config=None):
    """Register routes on a fresh Flask app. Stub TradingService so tests never hit Mongo/exchange."""
    cfg = dict(config) if isinstance(config, dict) else dict(_MCP_CFG)
    monkeypatch.setattr("data_manager.get_config", lambda **_k: cfg)
    if patch_trading:
        _install_trading_stubs(monkeypatch)
    from services.mcp.bot_http import register_mcp_bot_routes

    app = Flask(__name__)
    register_mcp_bot_routes(app)
    return app.test_client()


def _install_trading_stubs(monkeypatch, *, buy=None, sell=None, lock=None, get_pos=None):
    buy_fn = buy or (lambda self, *a, **k: SimpleNamespace(executed=True, message="ok"))
    sell_fn = sell or (lambda self, *a, **k: SimpleNamespace(executed=True, message="ok"))

    class _FakeTS:
        execute_buy = buy_fn
        execute_sell = sell_fn

    monkeypatch.setattr("services.trading_service.TradingService", _FakeTS)
    monkeypatch.setattr(
        "strategies.positions.set_position_lock",
        lock or (lambda *a, **k: {}),
    )
    monkeypatch.setattr(
        "strategies.positions.get_position",
        get_pos or (lambda *a, **k: {"amount": 10.0}),
    )


def test_execute_503_without_token(monkeypatch):
    monkeypatch.delenv("EXIT_WS_INTERNAL_TOKEN", raising=False)
    monkeypatch.delenv("MCP_BOT_TOKEN", raising=False)
    client = _client(monkeypatch, patch_trading=False)
    rv = client.post(_PATH, json=_BUY_BODY)
    assert rv.status_code == 503
    body = rv.get_json()
    assert body.get("ok") is False
    assert body.get("error") == "not_configured"


def test_execute_401_bad_token(monkeypatch):
    monkeypatch.delenv("MCP_BOT_TOKEN", raising=False)
    monkeypatch.setenv("EXIT_WS_INTERNAL_TOKEN", "secret")
    client = _client(monkeypatch, patch_trading=False)
    rv = client.post(
        _PATH,
        json=_BUY_BODY,
        headers={"X-Exit-Ws-Token": "nope"},
    )
    assert rv.status_code == 401


def test_execute_buy_calls_trading_service(monkeypatch):
    monkeypatch.delenv("MCP_BOT_TOKEN", raising=False)
    monkeypatch.setenv("EXIT_WS_INTERNAL_TOKEN", "secret")
    calls = []

    def fake_buy(self, symbol, timeframe, price=0, usdt=0, **kw):
        from core.tenant_context import tenant_snapshot

        calls.append(
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "price": price,
                "usdt": usdt,
                "tenant_id": tenant_snapshot()[0],
            }
        )
        return SimpleNamespace(executed=True, message="ok")

    _install_trading_stubs(monkeypatch, buy=fake_buy)
    client = _client(monkeypatch, patch_trading=False)
    rv = client.post(
        _PATH,
        headers={"X-Exit-Ws-Token": "secret"},
        json=_BUY_BODY,
    )
    assert rv.status_code == 200
    body = rv.get_json()
    assert body.get("executed") is True
    assert calls, "execute_buy was not called"
    assert calls[0]["tenant_id"] == "henry"
    assert calls[0]["symbol"] == "LAB/USDT"
    assert calls[0]["timeframe"] == "1h"
    assert float(calls[0]["usdt"]) == 50.0


def test_execute_missing_fields_400(monkeypatch):
    monkeypatch.delenv("MCP_BOT_TOKEN", raising=False)
    monkeypatch.setenv("EXIT_WS_INTERNAL_TOKEN", "secret")
    client = _client(monkeypatch)
    headers = {"X-Exit-Ws-Token": "secret"}
    for body in (
        {"tenant_id": "default", "symbol": "LAB/USDT"},
        {"action": "buy", "tenant_id": "default"},
        {"action": "buy", "symbol": "LAB/USDT"},
    ):
        rv = client.post(_PATH, json=body, headers=headers)
        assert rv.status_code == 400, body


def test_execute_sell_pct_uses_position_amount(monkeypatch):
    monkeypatch.delenv("MCP_BOT_TOKEN", raising=False)
    monkeypatch.setenv("EXIT_WS_INTERNAL_TOKEN", "secret")
    calls = []

    def fake_sell(self, symbol, timeframe, price, signal, amount, **kw):
        calls.append(
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "price": price,
                "signal": signal,
                "amount": amount,
            }
        )
        return SimpleNamespace(executed=True, message="ok")

    _install_trading_stubs(
        monkeypatch,
        sell=fake_sell,
        get_pos=lambda symbol, timeframe: {"amount": 8.0},
    )
    client = _client(monkeypatch, patch_trading=False)
    rv = client.post(
        _PATH,
        headers={"X-Exit-Ws-Token": "secret"},
        json={
            "action": "sell",
            "tenant_id": "henry",
            "symbol": "LAB/USDT",
            "timeframe": "1h",
            "pct": 50,
            "actor_id": "jens",
        },
    )
    assert rv.status_code == 200
    assert rv.get_json().get("executed") is True
    assert calls[0]["amount"] == 4.0
    assert calls[0]["signal"] == "mcp:jens"


def test_execute_risk_reject_is_200(monkeypatch):
    monkeypatch.delenv("MCP_BOT_TOKEN", raising=False)
    monkeypatch.setenv("EXIT_WS_INTERNAL_TOKEN", "secret")

    def fake_buy(self, *a, **k):
        return SimpleNamespace(executed=False, message="size_capped")

    _install_trading_stubs(monkeypatch, buy=fake_buy)
    client = _client(monkeypatch, patch_trading=False)
    rv = client.post(
        _PATH,
        headers={"X-Exit-Ws-Token": "secret"},
        json=_BUY_BODY,
    )
    assert rv.status_code == 200
    body = rv.get_json()
    assert body.get("ok") is False
    assert body.get("executed") is False
    assert body.get("message") == "size_capped"


def test_execute_lock_calls_set_position_lock(monkeypatch):
    monkeypatch.delenv("MCP_BOT_TOKEN", raising=False)
    monkeypatch.setenv("EXIT_WS_INTERNAL_TOKEN", "secret")
    locks = []

    def fake_lock(symbol, timeframe, lock, persist=True):
        locks.append(
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "lock": lock,
                "persist": persist,
            }
        )
        return dict(lock or {})

    _install_trading_stubs(monkeypatch, lock=fake_lock)
    client = _client(monkeypatch, patch_trading=False)
    rv = client.post(
        _PATH,
        headers={"Authorization": "Bearer secret"},
        json={
            "action": "lock",
            "tenant_id": "henry",
            "symbol": "LAB/USDT",
            "timeframe": "1h",
            "actor_id": "jens",
            "reason": "hold",
        },
    )
    assert rv.status_code == 200
    body = rv.get_json()
    assert body.get("ok") is True
    assert locks, "set_position_lock was not called"
    assert locks[0]["symbol"] == "LAB/USDT"
    assert locks[0]["lock"] is not None
    assert locks[0]["persist"] is True


def test_execute_mcp_disabled(monkeypatch):
    monkeypatch.setenv("EXIT_WS_INTERNAL_TOKEN", "secret")
    monkeypatch.delenv("MCP_BOT_TOKEN", raising=False)
    cfg = dict(_MCP_CFG)
    cfg["mcp"] = {"enabled": False, "allow_writes": True, "tenants": ["henry"]}
    client = _client(monkeypatch, config=cfg)
    rv = client.post(_PATH, json=_BUY_BODY, headers={"X-Exit-Ws-Token": "secret"})
    assert rv.status_code == 503
    assert rv.get_json().get("error") == "mcp_disabled"


def test_execute_writes_disabled(monkeypatch):
    monkeypatch.setenv("EXIT_WS_INTERNAL_TOKEN", "secret")
    monkeypatch.delenv("MCP_BOT_TOKEN", raising=False)
    cfg = dict(_MCP_CFG)
    cfg["mcp"] = {"enabled": True, "allow_writes": False, "tenants": ["henry"]}
    client = _client(monkeypatch, config=cfg)
    rv = client.post(_PATH, json=_BUY_BODY, headers={"X-Exit-Ws-Token": "secret"})
    assert rv.status_code == 403
    assert rv.get_json().get("error") == "writes_disabled"


def test_execute_tenant_not_allowed(monkeypatch):
    monkeypatch.setenv("EXIT_WS_INTERNAL_TOKEN", "secret")
    monkeypatch.delenv("MCP_BOT_TOKEN", raising=False)
    cfg = dict(_MCP_CFG)
    cfg["mcp"] = {"enabled": True, "allow_writes": True, "tenants": ["default"]}
    client = _client(monkeypatch, config=cfg)
    rv = client.post(_PATH, json=_BUY_BODY, headers={"X-Exit-Ws-Token": "secret"})
    assert rv.status_code == 403
    assert rv.get_json().get("error") == "tenant_forbidden"


def test_execute_real_live_forbidden(monkeypatch):
    monkeypatch.setenv("EXIT_WS_INTERNAL_TOKEN", "secret")
    monkeypatch.delenv("MCP_BOT_TOKEN", raising=False)
    cfg = {
        "trading_mode": "live",
        "live_confirmed": True,
        "live": {"dry_run": False},
        "mcp": {"enabled": True, "allow_writes": True, "allow_live": False, "tenants": ["henry"]},
    }
    client = _client(monkeypatch, config=cfg)
    rv = client.post(_PATH, json=_BUY_BODY, headers={"X-Exit-Ws-Token": "secret"})
    assert rv.status_code == 403
    assert rv.get_json().get("error") == "live_forbidden"


def test_execute_buy_passes_source_and_idempotency(monkeypatch):
    monkeypatch.setenv("EXIT_WS_INTERNAL_TOKEN", "secret")
    monkeypatch.delenv("MCP_BOT_TOKEN", raising=False)
    calls = []

    def fake_buy(self, symbol, timeframe, price=0, usdt=0, **kw):
        calls.append({"source": kw.get("source"), "idempotency_key": kw.get("idempotency_key")})
        return SimpleNamespace(executed=True, message="ok")

    _install_trading_stubs(monkeypatch, buy=fake_buy)
    client = _client(monkeypatch, patch_trading=False)
    rv = client.post(
        _PATH,
        headers={"X-Exit-Ws-Token": "secret"},
        json={**_BUY_BODY, "idempotency_key": "mcp:abc"},
    )
    assert rv.status_code == 200
    assert calls[0]["source"] == "mcp:jens"
    assert calls[0]["idempotency_key"] == "mcp:abc"


def test_mcp_bot_token_wins_over_exit_ws(monkeypatch):
    monkeypatch.setenv("MCP_BOT_TOKEN", "mcp-secret")
    monkeypatch.setenv("EXIT_WS_INTERNAL_TOKEN", "exit-secret")
    client = _client(monkeypatch)
    bad = client.post(_PATH, json=_BUY_BODY, headers={"X-Exit-Ws-Token": "exit-secret"})
    assert bad.status_code == 401
    ok = client.post(_PATH, json=_BUY_BODY, headers={"X-Exit-Ws-Token": "mcp-secret"})
    assert ok.status_code == 200
