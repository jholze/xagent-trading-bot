"""FastMCP streamable HTTP + GET /health. Never imports aria_bot."""

from __future__ import annotations

import json
import os
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from services.mcp.authz import mcp_enabled, mcp_writes_enabled
from services.mcp.tokens import actor_from_bearer, bootstrap_from_env
from services.mcp.tools import (
    tool_buy,
    tool_cover,
    tool_lock,
    tool_lots,
    tool_memory,
    tool_orders,
    tool_sell,
    tool_short,
    tool_snapshot,
    tool_unlock,
    tool_whoami,
    tool_why,
)

SERVICE_NAME = "xagent-mcp"
MCP_PATH = "/mcp"


def health_payload() -> dict:
    return {"ok": True, "service": SERVICE_NAME}


def listen_port() -> int:
    raw = os.environ.get("PORT") or "8080"
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 8080


def bootstrap_env() -> None:
    os.environ.setdefault("DEMO_MODE", "1")


def parse_bearer(authorization: str | None) -> str:
    raw = (authorization or "").strip()
    if not raw:
        return ""
    scheme, _, rest = raw.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return rest.strip()


def load_actors():
    raw = os.environ.get("MCP_ACTORS_JSON") or "[]"
    try:
        extras = json.loads(raw)
    except json.JSONDecodeError:
        extras = []
    if not isinstance(extras, list):
        extras = []
    return bootstrap_from_env(
        owner_token=os.environ.get("MCP_OWNER_TOKEN") or "",
        extras=extras,
    )


def actor_from_authorization(authorization: str | None):
    return actor_from_bearer(parse_bearer(authorization), load_actors())


def current_authorization(ctx: Any = None) -> str:
    if ctx is None:
        return ""
    try:
        req = ctx.request_context.request
    except Exception:
        return ""
    if req is None:
        return ""
    headers = getattr(req, "headers", None)
    if headers is None:
        return ""
    try:
        return str(headers.get("authorization") or "")
    except Exception:
        return ""


def _load_config_raw() -> dict:
    try:
        from data_manager import get_config

        cfg = get_config()
    except Exception:
        return {}
    return cfg if isinstance(cfg, dict) else {}


def mcp_flags(config_raw: dict | None = None) -> tuple[bool, bool]:
    raw = _load_config_raw() if config_raw is None else config_raw
    return mcp_enabled(raw), mcp_writes_enabled(raw)


def invoke_tool(
    name: str,
    *,
    authorization: str | None = None,
    config_raw: dict | None = None,
    **kwargs,
) -> dict:
    enabled, writes = mcp_flags(config_raw)
    if not enabled:
        return {"ok": False, "error": "mcp_disabled"}
    actor = actor_from_authorization(authorization)
    if actor is None:
        return {"ok": False, "error": "unauthorized"}

    tenant = kwargs.get("tenant")
    symbol = kwargs.get("symbol")
    timeframe = kwargs.get("timeframe") or "1h"
    price = kwargs.get("price")

    if name == "xagent_whoami":
        return tool_whoami(actor)
    if name == "xagent_snapshot":
        return tool_snapshot(actor, tenant=tenant, symbol=symbol)
    if name == "xagent_lots":
        return tool_lots(actor, tenant=tenant, symbol=symbol)
    if name == "xagent_orders":
        return tool_orders(
            actor,
            tenant=tenant,
            symbol=symbol,
            hours=kwargs.get("hours"),
            limit=kwargs.get("limit"),
            statuses=kwargs.get("statuses"),
        )
    if name == "xagent_memory":
        return tool_memory(
            actor,
            tenant=tenant,
            symbol=symbol,
            query=kwargs.get("query"),
        )
    if name == "xagent_why":
        return tool_why(
            actor,
            tenant=tenant,
            symbol=symbol,
            query=kwargs.get("query"),
        )
    if name == "xagent_buy":
        return tool_buy(
            actor,
            tenant=tenant,
            symbol=symbol,
            usdt=kwargs.get("usdt"),
            timeframe=timeframe,
            price=price,
            enabled=enabled,
            writes_enabled=writes,
        )
    if name == "xagent_sell":
        return tool_sell(
            actor,
            tenant=tenant,
            symbol=symbol,
            pct=kwargs.get("pct"),
            amount=kwargs.get("amount"),
            timeframe=timeframe,
            price=price,
            enabled=enabled,
            writes_enabled=writes,
        )
    if name == "xagent_short":
        return tool_short(
            actor,
            tenant=tenant,
            symbol=symbol,
            usdt=kwargs.get("usdt"),
            leverage=kwargs.get("leverage"),
            timeframe=timeframe,
            price=price,
            enabled=enabled,
            writes_enabled=writes,
        )
    if name == "xagent_cover":
        return tool_cover(
            actor,
            tenant=tenant,
            symbol=symbol,
            pct=kwargs.get("pct"),
            amount=kwargs.get("amount"),
            timeframe=timeframe,
            price=price,
            enabled=enabled,
            writes_enabled=writes,
        )
    if name == "xagent_lock":
        return tool_lock(
            actor,
            tenant=tenant,
            symbol=symbol,
            reason=kwargs.get("reason"),
            timeframe=timeframe,
            enabled=enabled,
            writes_enabled=writes,
        )
    if name == "xagent_unlock":
        return tool_unlock(
            actor,
            tenant=tenant,
            symbol=symbol,
            timeframe=timeframe,
            enabled=enabled,
            writes_enabled=writes,
        )
    return {"ok": False, "error": "unknown_tool"}


def _call(name: str, ctx: Context, **kwargs) -> dict:
    return invoke_tool(name, authorization=current_authorization(ctx), **kwargs)


def xagent_whoami(ctx: Context) -> dict:
    """Current MCP actor (id, role, tenants, caps)."""
    return _call("xagent_whoami", ctx)


def xagent_snapshot(ctx: Context, tenant: str = "default", symbol: str | None = None) -> dict:
    """Desk snapshot for a tenant (lots, HUD, next_edge)."""
    return _call("xagent_snapshot", ctx, tenant=tenant, symbol=symbol)


def xagent_lots(ctx: Context, tenant: str = "default", symbol: str | None = None) -> dict:
    """Open lots for a tenant."""
    return _call("xagent_lots", ctx, tenant=tenant, symbol=symbol)


def xagent_orders(
    ctx: Context,
    tenant: str = "default",
    symbol: str | None = None,
    hours: float | None = 168,
    limit: int | None = 40,
    statuses: str | None = None,
) -> dict:
    """Filled/rejected/failed orders for a tenant. Includes source, signal, risk, size."""
    return _call(
        "xagent_orders",
        ctx,
        tenant=tenant,
        symbol=symbol,
        hours=hours,
        limit=limit,
        statuses=statuses,
    )


def xagent_memory(
    ctx: Context,
    tenant: str = "default",
    symbol: str | None = None,
    query: str | None = None,
) -> dict:
    """CoinProfile, fact flags, market events, trade memory, lessons, RAG hits. No embeddings."""
    return _call("xagent_memory", ctx, tenant=tenant, symbol=symbol, query=query)


def xagent_why(
    ctx: Context,
    tenant: str = "default",
    symbol: str | None = None,
    query: str | None = None,
) -> dict:
    """Why this coin was bought: lot, HUD, orders+signals, profile, facts, events, RAG."""
    return _call("xagent_why", ctx, tenant=tenant, symbol=symbol, query=query)


def xagent_buy(
    ctx: Context,
    symbol: str,
    usdt: float,
    tenant: str = "default",
    timeframe: str = "1h",
    price: float | None = None,
) -> dict:
    """Paper buy via TradingService (sized/blocked by RiskManager)."""
    return _call(
        "xagent_buy",
        ctx,
        symbol=symbol,
        usdt=usdt,
        tenant=tenant,
        timeframe=timeframe,
        price=price,
    )


def xagent_sell(
    ctx: Context,
    symbol: str,
    tenant: str = "default",
    pct: float | None = None,
    amount: float | None = None,
    timeframe: str = "1h",
    price: float | None = None,
) -> dict:
    """Paper sell via TradingService (pct 0–100 or amount)."""
    return _call(
        "xagent_sell",
        ctx,
        symbol=symbol,
        tenant=tenant,
        pct=pct,
        amount=amount,
        timeframe=timeframe,
        price=price,
    )


def xagent_short(
    ctx: Context,
    symbol: str,
    usdt: float,
    tenant: str = "default",
    timeframe: str = "1h",
    price: float | None = None,
    leverage: float | None = None,
) -> dict:
    """Paper short via TradingService (isolated, sized/blocked by RiskManager)."""
    return _call(
        "xagent_short",
        ctx,
        symbol=symbol,
        usdt=usdt,
        tenant=tenant,
        timeframe=timeframe,
        price=price,
        leverage=leverage,
    )


def xagent_cover(
    ctx: Context,
    symbol: str,
    tenant: str = "default",
    pct: float | None = None,
    amount: float | None = None,
    timeframe: str = "1h",
    price: float | None = None,
) -> dict:
    """Cover a paper short (pct 0–100 or amount)."""
    return _call(
        "xagent_cover",
        ctx,
        symbol=symbol,
        tenant=tenant,
        pct=pct,
        amount=amount,
        timeframe=timeframe,
        price=price,
    )


def xagent_lock(
    ctx: Context,
    symbol: str,
    tenant: str = "default",
    reason: str | None = None,
    timeframe: str = "1h",
) -> dict:
    """Lock a position (same path as Telegram /lock)."""
    return _call(
        "xagent_lock",
        ctx,
        symbol=symbol,
        tenant=tenant,
        reason=reason,
        timeframe=timeframe,
    )


def xagent_unlock(
    ctx: Context,
    symbol: str,
    tenant: str = "default",
    timeframe: str = "1h",
) -> dict:
    """Unlock a position."""
    return _call(
        "xagent_unlock",
        ctx,
        symbol=symbol,
        tenant=tenant,
        timeframe=timeframe,
    )


def _register_tools(mcp: FastMCP) -> None:
    mcp.tool()(xagent_whoami)
    mcp.tool()(xagent_snapshot)
    mcp.tool()(xagent_lots)
    mcp.tool()(xagent_orders)
    mcp.tool()(xagent_memory)
    mcp.tool()(xagent_why)
    mcp.tool()(xagent_buy)
    mcp.tool()(xagent_sell)
    mcp.tool()(xagent_short)
    mcp.tool()(xagent_cover)
    mcp.tool()(xagent_lock)
    mcp.tool()(xagent_unlock)


async def _health(_request: Request) -> JSONResponse:
    return JSONResponse(health_payload())


def build_mcp(*, host: str = "0.0.0.0", port: int | None = None) -> FastMCP:
    mcp = FastMCP(
        SERVICE_NAME,
        host=host,
        port=port if port is not None else listen_port(),
        streamable_http_path=MCP_PATH,
        stateless_http=True,
    )
    _register_tools(mcp)
    mcp.custom_route("/health", methods=["GET"])(_health)
    return mcp


def create_app(*, host: str = "0.0.0.0", port: int | None = None):
    bootstrap_env()
    return build_mcp(host=host, port=port).streamable_http_app()
