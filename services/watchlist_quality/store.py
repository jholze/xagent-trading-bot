"""Persist WQE shadow scores (demo-aware, optional tenant_id).

On Railway staging, scores live under ``logs/`` so they land on the
``xagent-test-volume`` mount at ``/app/logs`` and survive redeploys.
Locally, same ``logs/`` path keeps soak artifacts next to wqe_events.jsonl.
"""

from __future__ import annotations

import json
import os
from typing import Any

from logger import LOG_DIR

SCORES_FILE = "watchlist_quality_scores.json"


def _scores_basename(tenant_id: str | None = None) -> str:
    tid = (tenant_id or "default").strip() or "default"
    if tid == "default":
        return SCORES_FILE
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in tid)[:64]
    return f"watchlist_quality_scores.{safe}.json"


def _scores_path(tenant_id: str | None = None) -> str:
    """Prefer LOG_DIR (volume-backed on Railway); optional WQE_DATA_DIR override."""
    base = (os.environ.get("WQE_DATA_DIR") or "").strip() or LOG_DIR
    os.makedirs(base, exist_ok=True)
    name = _scores_basename(tenant_id)
    # demo mode: keep .demo suffix for isolation
    try:
        from data_manager import is_demo_mode

        if is_demo_mode() and name.endswith(".json") and not name.endswith(".demo.json"):
            name = name.replace(".json", ".demo.json")
    except Exception:
        pass
    return os.path.join(base, name)


def load_quality_scores(tenant_id: str | None = None) -> dict[str, Any]:
    try:
        path = _scores_path(tenant_id)
        # legacy CWD fallback
        if not os.path.exists(path):
            try:
                from data_manager import get_data_file

                legacy = get_data_file(_scores_basename(tenant_id))
                if os.path.exists(legacy):
                    path = legacy
            except Exception:
                pass
        # Non-default tenants must not inherit default-tenant scores when their file is missing.
        if not os.path.exists(path):
            return {
                "updated_at": "",
                "mode": "off",
                "coins": [],
                "tenant_id": tenant_id or "default",
            }
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {
                "updated_at": "",
                "mode": "off",
                "coins": [],
                "tenant_id": tenant_id or "default",
            }
        data.setdefault("coins", [])
        data.setdefault("tenant_id", tenant_id or "default")
        return data
    except Exception:
        return {
            "updated_at": "",
            "mode": "off",
            "coins": [],
            "tenant_id": tenant_id or "default",
        }


def save_quality_scores(payload: dict[str, Any], tenant_id: str | None = None) -> bool:
    try:
        from data_manager import atomic_write_json

        tid = tenant_id or payload.get("tenant_id") or "default"
        payload = dict(payload)
        payload["tenant_id"] = tid
        path = _scores_path(str(tid))
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


def scores_path(tenant_id: str | None = None) -> str:
    return _scores_path(tenant_id)
