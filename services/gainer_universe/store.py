"""Persist gainer scan state (JSON). Safe no-op paths for tests."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from logger import log

_STATE_NAME = "gainer_universe_state.json"


def _state_path() -> Path:
    env = (os.environ.get("GAINER_UNIVERSE_STATE_PATH") or "").strip()
    if env:
        return Path(env)
    # prefer logs volume on Railway, else repo root data/
    for base in (
        Path("/app/logs"),
        Path(__file__).resolve().parents[2] / "data",
        Path(__file__).resolve().parents[2],
    ):
        try:
            base.mkdir(parents=True, exist_ok=True)
            return base / _STATE_NAME
        except Exception:
            continue
    return Path(_STATE_NAME)


def load_gainer_state() -> dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        log(f"gainer_universe load state failed: {e}", "WARNING")
        return {}


def save_gainer_state(state: dict[str, Any]) -> None:
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        state = dict(state)
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix=".gainer_", suffix=".json"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, default=str)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as e:
        log(f"gainer_universe save state failed: {e}", "WARNING")
