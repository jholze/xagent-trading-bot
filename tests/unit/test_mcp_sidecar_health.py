"""Sidecar health is always up. No live MCP handshake in these unit tests."""

from __future__ import annotations

import sys

from starlette.testclient import TestClient

_ENABLED = {"mcp": {"enabled": True, "allow_writes": True}}
_DISABLED = {"mcp": {"enabled": False, "allow_writes": True}}


def test_health_payload_shape():
    from services.mcp_sidecar.app import health_payload

    assert health_payload() == {"ok": True, "service": "xagent-mcp"}


def test_health_http_200(monkeypatch):
    monkeypatch.delenv("MCP_OWNER_TOKEN", raising=False)
    from services.mcp_sidecar.app import create_app

    with TestClient(create_app()) as client:
        rv = client.get("/health")
    assert rv.status_code == 200
    assert rv.json() == {"ok": True, "service": "xagent-mcp"}


def test_health_ok_when_mcp_disabled(monkeypatch):
    monkeypatch.setattr(
        "data_manager.get_config",
        lambda **_k: {"mcp": {"enabled": False, "allow_writes": False}},
    )
    from services.mcp_sidecar.app import create_app, health_payload

    assert health_payload() == {"ok": True, "service": "xagent-mcp"}
    with TestClient(create_app()) as client:
        rv = client.get("/health")
    assert rv.status_code == 200
    assert rv.json()["ok"] is True
    assert rv.json()["service"] == "xagent-mcp"


def test_mcp_path_and_health_routes_registered():
    from services.mcp_sidecar.app import MCP_PATH, create_app

    assert MCP_PATH == "/mcp"
    app = create_app()
    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/health" in paths
    assert "/mcp" in paths


def test_listen_port_defaults_to_8080(monkeypatch):
    monkeypatch.delenv("PORT", raising=False)
    from services.mcp_sidecar.app import listen_port

    assert listen_port() == 8080
    monkeypatch.setenv("PORT", "9099")
    assert listen_port() == 9099


def test_missing_token_is_unauthorized(monkeypatch):
    monkeypatch.setenv("MCP_OWNER_TOKEN", "owner-secret")
    monkeypatch.delenv("MCP_ACTORS_JSON", raising=False)
    from services.mcp_sidecar.app import invoke_tool

    out = invoke_tool("xagent_whoami", authorization="", config_raw=_ENABLED)
    assert out["ok"] is False
    assert out["error"] == "unauthorized"


def test_missing_bearer_header_is_unauthorized(monkeypatch):
    monkeypatch.setenv("MCP_OWNER_TOKEN", "owner-secret")
    monkeypatch.delenv("MCP_ACTORS_JSON", raising=False)
    from services.mcp_sidecar.app import invoke_tool

    out = invoke_tool("xagent_whoami", authorization=None, config_raw=_ENABLED)
    assert out["ok"] is False
    assert out["error"] == "unauthorized"


def test_owner_bearer_whoami(monkeypatch):
    monkeypatch.setenv("MCP_OWNER_TOKEN", "owner-secret")
    monkeypatch.delenv("MCP_ACTORS_JSON", raising=False)
    from services.mcp_sidecar.app import invoke_tool

    out = invoke_tool(
        "xagent_whoami",
        authorization="Bearer owner-secret",
        config_raw=_ENABLED,
    )
    assert out["actor_id"] == "owner"
    assert out["role"] == "owner"
    assert "*" in out["tenants"]


def test_tools_deaf_when_mcp_disabled(monkeypatch):
    monkeypatch.setenv("MCP_OWNER_TOKEN", "owner-secret")
    from services.mcp_sidecar.app import invoke_tool

    out = invoke_tool(
        "xagent_whoami",
        authorization="Bearer owner-secret",
        config_raw=_DISABLED,
    )
    assert out["ok"] is False
    assert out["error"] == "mcp_disabled"


def test_sidecar_does_not_import_aria_bot():
    sys.modules.pop("aria_bot", None)
    from services.mcp_sidecar import app as sidecar_app  # noqa: F401

    assert "aria_bot" not in sys.modules
    sidecar_app.create_app()
    assert "aria_bot" not in sys.modules
