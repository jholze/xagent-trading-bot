"""Persist WQE shadow scores (demo-aware, optional tenant_id)."""

from __future__ import annotations

from typing import Any

SCORES_FILE = "watchlist_quality_scores.json"


def _scores_basename(tenant_id: str | None = None) -> str:
    tid = (tenant_id or "default").strip() or "default"
    if tid == "default":
        return SCORES_FILE
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in tid)[:64]
    return f"watchlist_quality_scores.{safe}.json"


def load_quality_scores(tenant_id: str | None = None) -> dict[str, Any]:
    try:
        from data_manager import get_data_file
        import json
        import os

        path = get_data_file(_scores_basename(tenant_id))
        if not os.path.exists(path) and tenant_id and tenant_id != "default":
            # fallback legacy global file
            path = get_data_file(SCORES_FILE)
        if not os.path.exists(path):
            return {"updated_at": "", "mode": "off", "coins": [], "tenant_id": tenant_id or "default"}
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"updated_at": "", "mode": "off", "coins": [], "tenant_id": tenant_id or "default"}
        data.setdefault("coins", [])
        data.setdefault("tenant_id", tenant_id or "default")
        return data
    except Exception:
        return {"updated_at": "", "mode": "off", "coins": [], "tenant_id": tenant_id or "default"}


def save_quality_scores(payload: dict[str, Any], tenant_id: str | None = None) -> bool:
    try:
        from data_manager import atomic_write_json, get_data_file

        tid = tenant_id or payload.get("tenant_id") or "default"
        payload = dict(payload)
        payload["tenant_id"] = tid
        path = get_data_file(_scores_basename(str(tid)))
        atomic_write_json(path, payload)
        return True
    except Exception:
        return False


def score_age_seconds(tenant_id: str | None = None) -> float | None:
    """Seconds since scores updated_at; None if missing/unparseable."""
    from datetime import datetime, timezone

    data = load_quality_scores(tenant_id=tenant_id)
    raw = data.get("updated_at") or ""
    if not raw:
        return None
    try:
        u = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(u)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
    except Exception:
        return None
