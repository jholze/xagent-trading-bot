"""HTTP handshake on /mcp requires a valid owner/operator token."""

from __future__ import annotations

from starlette.testclient import TestClient

_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
_ACCEPT = {"Accept": "application/json, text/event-stream"}


def _app(monkeypatch):
    monkeypatch.setenv("MCP_OWNER_TOKEN", "owner-secret")
    monkeypatch.delenv("MCP_ACTORS_JSON", raising=False)
    from services.mcp.sidecar.app import create_app

    return create_app()


def test_health_stays_public_without_token(monkeypatch):
    with TestClient(_app(monkeypatch)) as client:
        rv = client.get("/health")
    assert rv.status_code == 200
    assert rv.json()["ok"] is True


def test_mcp_initialize_without_token_is_401(monkeypatch):
    with TestClient(_app(monkeypatch)) as client:
        rv = client.post("/mcp", json=_INIT, headers=_ACCEPT)
    assert rv.status_code == 401
    assert rv.json()["error"] == "unauthorized"
    assert "Bearer" in (rv.headers.get("www-authenticate") or "")


def test_mcp_initialize_wrong_token_is_401(monkeypatch):
    with TestClient(_app(monkeypatch)) as client:
        rv = client.post(
            "/mcp",
            json=_INIT,
            headers={**_ACCEPT, "Authorization": "Bearer nope"},
        )
    assert rv.status_code == 401


def test_mcp_initialize_bearer_header_ok(monkeypatch):
    with TestClient(_app(monkeypatch)) as client:
        rv = client.post(
            "/mcp",
            json=_INIT,
            headers={**_ACCEPT, "Authorization": "Bearer owner-secret"},
        )
    assert rv.status_code == 200
    assert "xagent-mcp" in rv.text


def test_mcp_initialize_query_token_ok(monkeypatch):
    """Grok Web/iOS custom connector has no header field — token on the URL."""
    with TestClient(_app(monkeypatch)) as client:
        rv = client.post(
            "/mcp?token=owner-secret",
            json=_INIT,
            headers=_ACCEPT,
        )
    assert rv.status_code == 200
    assert "xagent-mcp" in rv.text


def test_mcp_initialize_access_token_query_ok(monkeypatch):
    with TestClient(_app(monkeypatch)) as client:
        rv = client.post(
            "/mcp?access_token=owner-secret",
            json=_INIT,
            headers=_ACCEPT,
        )
    assert rv.status_code == 200


def test_parse_bearer_accepts_bare_token():
    from services.mcp.sidecar.app import parse_bearer

    assert parse_bearer("Bearer abc") == "abc"
    assert parse_bearer("abc") == "abc"
    assert parse_bearer("Basic abc") == ""
    assert parse_bearer("") == ""
    assert parse_bearer(None) == ""
