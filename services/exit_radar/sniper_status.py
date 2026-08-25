"""Best-effort DCA sniper health for live board / radar."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]


def fetch_dca_sniper_status() -> dict[str, Any]:
    """Best-effort DCA sniper health for the live board.

    Order: HTTP (DCA_SNIPER_URL) → Redis heartbeat/state → local state file
    → bot config flag only. Never raises; board must stay up if sniper is down.
    """
    out: dict[str, Any] = {
        "ok": False,
        "source": None,
        "enabled": None,
        "standalone": None,
        "redis": None,
        "healthy": False,
        "heartbeat": None,
        "focus": [],
        "open_focus_holds": None,
        "last_cycle_at": None,
        "error": None,
    }

    def _focus_symbols(raw: Any) -> list[str]:
        if isinstance(raw, dict):
            return [str(k) for k in raw.keys() if k]
        if not isinstance(raw, list):
            return []
        out: list[str] = []
        for x in raw:
            if isinstance(x, dict) and x.get("symbol"):
                out.append(str(x["symbol"]))
            elif isinstance(x, str) and x and not x.startswith("{"):
                out.append(x)
        return out

    def _normalize(payload: dict[str, Any], *, source: str) -> dict[str, Any]:
        st = payload.get("state") if isinstance(payload.get("state"), dict) else {}
        focus = _focus_symbols(payload.get("focus") or st.get("focus") or [])
        last_audit = payload.get("last_audit") if isinstance(payload.get("last_audit"), dict) else {}
        return {
            "ok": bool(payload.get("ok", True)),
            "source": source,
            "enabled": payload.get("enabled"),
            "standalone": payload.get("standalone"),
            "redis": payload.get("redis"),
            "healthy": True,
            "heartbeat": payload.get("heartbeat") or st.get("updated_at") or st.get("last_cycle_at"),
            "focus": focus[:12],
            "open_focus_holds": payload.get("open_focus_holds")
            if payload.get("open_focus_holds") is not None
            else (len(focus) if focus else None),
            "last_cycle_at": last_audit.get("ts")
            or st.get("last_cycle_at")
            or payload.get("last_cycle_at"),
            "watch": payload.get("watch") or [],
            "config": payload.get("config") if isinstance(payload.get("config"), dict) else None,
            "error": None,
        }

    # 1) HTTP: standalone service
    base = (
        os.environ.get("DCA_SNIPER_URL")
        or os.environ.get("DCA_SNIPER_PUBLIC_URL")
        or ""
    ).strip().rstrip("/")
    if base:
        try:
            import urllib.request

            req = urllib.request.Request(
                f"{base}/status",
                headers={"Accept": "application/json", "User-Agent": "exit-radar/1"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                body = json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
            if isinstance(body, dict):
                return _normalize(body, source="http_status")
        except Exception as e:
            out["error"] = f"http:{type(e).__name__}"
            try:
                import urllib.request

                req = urllib.request.Request(
                    f"{base}/health",
                    headers={"Accept": "application/json", "User-Agent": "exit-radar/1"},
                    method="GET",
                )
                with urllib.request.urlopen(req, timeout=1.2) as resp:
                    body = json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
                if isinstance(body, dict) and body.get("ok"):
                    return _normalize({**body, "ok": True}, source="http_health")
            except Exception as e2:
                out["error"] = f"http:{type(e2).__name__}"

    # 2) Redis heartbeat + state
    try:
        from services.dca_sniper.redis_bus import (
            KEY_HEALTH,
            key_prefix,
            load_state_redis,
            redis_available,
        )
        from bus.redis_client import get_redis

        if redis_available():
            c = get_redis()
            hb = None
            if c is not None:
                try:
                    pfx = key_prefix()
                    if not pfx.endswith(":"):
                        pfx = pfx + ":"
                    raw_hb = c.get(f"{pfx}{KEY_HEALTH}")
                    if raw_hb:
                        hb = (
                            raw_hb.decode()
                            if isinstance(raw_hb, (bytes, bytearray))
                            else str(raw_hb)
                        )
                except Exception:
                    pass
            st = load_state_redis() or {}
            focus = _focus_symbols(st.get("focus") or [])
            healthy = bool(hb)
            if healthy:
                return {
                    "ok": True,
                    "source": "redis",
                    "enabled": True,
                    "standalone": True,
                    "redis": True,
                    "healthy": True,
                    "heartbeat": hb or st.get("updated_at"),
                    "focus": focus[:12],
                    "open_focus_holds": len(focus) if focus else None,
                    "last_cycle_at": st.get("last_cycle_at"),
                    "watch": st.get("watch") or [],
                    "config": None,
                    "error": None,
                }
            out["redis"] = True
            out["source"] = "redis"
            out["healthy"] = False
            out["error"] = out.get("error") or "redis_no_heartbeat"
    except Exception as e:
        out["error"] = out.get("error") or f"redis:{type(e).__name__}"

    # 3) Local state file (operator / same host)
    try:
        p = _REPO_ROOT / "data" / "dca_sniper_state.json"
        if p.is_file():
            st = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(st, dict):
                focus = _focus_symbols(st.get("focus") or [])
                return {
                    "ok": True,
                    "source": "local_file",
                    "enabled": None,
                    "standalone": None,
                    "redis": None,
                    "healthy": True,
                    "heartbeat": st.get("updated_at") or st.get("last_cycle_at"),
                    "focus": focus[:12],
                    "open_focus_holds": len(focus),
                    "last_cycle_at": st.get("last_cycle_at"),
                    "watch": [],
                    "config": None,
                    "error": None,
                }
    except Exception as e:
        out["error"] = out.get("error") or f"file:{type(e).__name__}"

    # 4) Config flag only (bot process may know enabled)
    try:
        from services.dca_sniper.config import dca_sniper_config, dca_sniper_enabled

        cfg = dca_sniper_config()
        out["enabled"] = dca_sniper_enabled()
        out["source"] = "config"
        out["config"] = {
            "max_focus_slots": cfg.get("max_focus_slots"),
            "in_process_tick": cfg.get("in_process_tick"),
            "ws_enabled": cfg.get("ws_enabled"),
        }
        out["ok"] = True
        out["healthy"] = False  # service not confirmed online
    except Exception:
        pass

    return out



