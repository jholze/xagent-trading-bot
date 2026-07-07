"""Runtime build metadata from git (commit, branch, dirty state)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _git(*args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", *args],
            cwd=_REPO_ROOT,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        return ""


def get_build_info() -> dict:
    commit = (
        (os.getenv("GIT_COMMIT") or "").strip()
        or (os.getenv("RAILWAY_GIT_COMMIT_SHA") or "")[:7]
        or _git("rev-parse", "--short", "HEAD")
        or "unknown"
    )
    branch = (
        (os.getenv("GIT_BRANCH") or "").strip()
        or (os.getenv("RAILWAY_GIT_BRANCH") or "").strip()
        or _git("rev-parse", "--abbrev-ref", "HEAD")
        or "unknown"
    )
    dirty = os.getenv("GIT_DIRTY") == "1" or bool(_git("status", "--porcelain"))
    return {"commit": commit, "branch": branch, "dirty": dirty}


def format_build_line(html: bool = True) -> str:
    from core.runtime_identity import format_build_line as _identity_build_line

    return _identity_build_line(html=html)