"""Persist WQE shadow scores to JSON (demo-aware via get_data_file)."""

from __future__ import annotations

from typing import Any

SCORES_FILE = "watchlist_quality_scores.json"


def load_quality_scores() -> dict[str, Any]:
    try:
        from data_manager import get_data_file
        import json
        import os

        path = get_data_file(SCORES_FILE)
        if not os.path.exists(path):
            return {"updated_at": "", "mode": "off", "coins": []}
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"updated_at": "", "mode": "off", "coins": []}
        data.setdefault("coins", [])
        return data
    except Exception:
        return {"updated_at": "", "mode": "off", "coins": []}


def save_quality_scores(payload: dict[str, Any]) -> bool:
    try:
        from data_manager import atomic_write_json, get_data_file

        path = get_data_file(SCORES_FILE)
        atomic_write_json(path, payload)
        return True
    except Exception:
        return False
