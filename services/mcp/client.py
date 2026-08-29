"""HTTP write client: MCP sidecar → bot POST /internal/mcp/execute."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

EXECUTE_PATH = "/internal/mcp/execute"
TIMEOUT_SEC = 45


def _timeout_sec() -> float:
    raw = (os.environ.get("MCP_BOT_TIMEOUT_SEC") or "").strip()
    try:
        n = float(raw)
    except (TypeError, ValueError):
        n = TIMEOUT_SEC
    if n < 5:
        return float(TIMEOUT_SEC)
    return n


def _bot_base_url() -> str:
    return (os.environ.get("MCP_BOT_URL") or "").strip().rstrip("/")


def _bot_token() -> str:
    return (
        os.environ.get("MCP_BOT_TOKEN") or os.environ.get("EXIT_WS_INTERNAL_TOKEN") or ""
    ).strip()


def _execute_url(base: str | None = None) -> str:
    raw = (base if base is not None else _bot_base_url()).strip().rstrip("/")
    if not raw:
        return ""
    if raw.endswith(EXECUTE_PATH):
        return raw
    return f"{raw}{EXECUTE_PATH}"


def execute(**body: Any) -> dict[str, Any]:
    """POST JSON body to the bot execute route. Never raises to callers."""
    url = _execute_url()
    if not url:
        return {"ok": False, "error": "bot_unreachable"}
    tok = _bot_token()
    try:
        data = json.dumps(body).encode("utf-8")
    except (TypeError, ValueError):
        return {"ok": False, "error": "bot_unreachable"}
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if tok:
        headers["X-Exit-Ws-Token"] = tok
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_timeout_sec()) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return _parse_body(raw)
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", errors="replace")
        except Exception:
            return {"ok": False, "error": "bot_unreachable"}
        return _parse_body(raw)
    except Exception:
        return {"ok": False, "error": "bot_unreachable"}


def _parse_body(raw: str) -> dict[str, Any]:
    if not raw:
        return {"ok": False, "error": "bot_unreachable"}
    try:
        out = json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "error": "bot_unreachable"}
    if not isinstance(out, dict):
        return {"ok": False, "error": "bot_unreachable"}
    return out
