"""Runtime build metadata from git (commit, branch, dirty state)."""

from __future__ import annotations

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
    commit = _git("rev-parse", "--short", "HEAD") or "unknown"
    branch = _git("rev-parse", "--abbrev-ref", "HEAD") or "unknown"
    dirty = bool(_git("status", "--porcelain"))
    return {"commit": commit, "branch": branch, "dirty": dirty}


def format_build_line(html: bool = True) -> str:
    info = get_build_info()
    dirty = " *" if info["dirty"] else ""
    if html:
        return f"Version: <code>{info['commit']}{dirty}</code> · Branch: <code>{info['branch']}</code>"
    return f"Version: {info['commit']}{dirty} · Branch: {info['branch']}"