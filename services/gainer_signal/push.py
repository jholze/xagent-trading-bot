"""Push EntrySignal payloads to bot POST /internal/gainer-signal."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from logger import log


def gainer_signal_token() -> str:
    return (
        os.environ.get("GAINER_SIGNAL_TOKEN")
        or os.environ.get("EXIT_WS_INTERNAL_TOKEN")
        or ""
    ).strip()


def bot_signal_url() -> str:
    """Bot consume endpoint."""
    explicit = (os.environ.get("GAINER_SIGNAL_BOT_URL") or "").strip().rstrip("/")
    if explicit:
        if explicit.endswith("/internal/gainer-signal"):
            return explicit
        return f"{explicit}/internal/gainer-signal"
    # Railway public bot host
    host = (os.environ.get("RAILWAY_SERVICE_XAGENT_TEST_URL") or "").strip()
    if host:
        base = host if host.startswith("http") else f"https://{host}"
        return f"{base.rstrip('/')}/internal/gainer-signal"
    # local default
    port = os.environ.get("PORT") or "5000"
    return f"http://127.0.0.1:{port}/internal/gainer-signal"


def push_signal_to_bot(
    signal: dict[str, Any],
    *,
    url: str | None = None,
    token: str | None = None,
    timeout_sec: float = 8.0,
) -> dict[str, Any]:
    """POST one signal. Returns response dict; ok=False on errors."""
    target = (url or bot_signal_url()).strip()
    tok = (token if token is not None else gainer_signal_token()).strip()
    if not tok:
        return {"ok": False, "message": "no_token"}
    body = dict(signal or {})
    body.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        target,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Gainer-Signal-Token": tok,
            "X-Exit-Ws-Token": tok,  # allow shared token header style
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                out = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                out = {"ok": False, "message": "bad_json", "raw": raw[:200]}
            if not isinstance(out, dict):
                return {"ok": False, "message": "bad_response"}
            out.setdefault("ok", resp.status == 200)
            out["http_status"] = resp.status
            return out
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            detail = str(e)
        return {"ok": False, "message": f"http_{e.code}", "detail": detail, "http_status": e.code}
    except Exception as e:
        log(f"gainer_signal push fail: {e}", "DEBUG")
        return {"ok": False, "message": str(e)[:160]}
