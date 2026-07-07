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


def _short_sha(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    return raw[:7] if len(raw) > 12 else raw


def get_build_info() -> dict:
    railway_sha = (os.getenv("RAILWAY_GIT_COMMIT_SHA") or "").strip()
    commit = (
        _short_sha(os.getenv("GIT_COMMIT") or "")
        or _short_sha(railway_sha)
        or _git("rev-parse", "--short", "HEAD")
        or "unknown"
    )
    branch = (
        (os.getenv("GIT_BRANCH") or "").strip()
        or (os.getenv("RAILWAY_GIT_BRANCH") or "").strip()
        or _git("rev-parse", "--abbrev-ref", "HEAD")
        or "unknown"
    )
    if branch == "HEAD":
        branch = "unknown"
    on_railway = bool(os.getenv("RAILWAY_DEPLOY") or os.getenv("RAILWAY_ENVIRONMENT"))
    dirty = (
        not on_railway
        and (os.getenv("GIT_DIRTY") == "1" or bool(_git("status", "--porcelain")))
    )
    return {"commit": commit, "branch": branch, "dirty": dirty}


def format_build_line(html: bool = True) -> str:
    from core.runtime_identity import format_build_line as _identity_build_line

    return _identity_build_line(html=html)