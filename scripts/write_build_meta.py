#!/usr/bin/env python3
"""Write core/build_meta.json for Docker/Railway builds (no .git in image).

Priority for commit/branch:
  1) RAILWAY_GIT_COMMIT_SHA / RAILWAY_GIT_BRANCH (GitHub deploys)
  2) GIT_COMMIT / GIT_BRANCH (manual)
  3) local git (when building from a checkout)
  4) keep existing build_meta.json if it already has a real commit

Never overwrite a known commit with "unknown" (that broke railway up deploys).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "core" / "build_meta.json"


def _git(*args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", *args],
            cwd=ROOT,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        return ""


def _short(sha: str) -> str:
    raw = (sha or "").strip()
    if not raw or raw == "unknown":
        return ""
    return raw[:7] if len(raw) > 12 else raw


def _read_existing() -> dict[str, str]:
    try:
        data = json.loads(OUT.read_text(encoding="utf-8"))
        return {
            "commit": _short(str(data.get("commit") or "")),
            "branch": str(data.get("branch") or "").strip(),
        }
    except Exception:
        return {"commit": "", "branch": ""}


def resolve_meta() -> dict[str, str]:
    existing = _read_existing()
    commit = (
        _short(os.getenv("RAILWAY_GIT_COMMIT_SHA") or "")
        or _short(os.getenv("GIT_COMMIT") or "")
        or _short(_git("rev-parse", "--short", "HEAD"))
        or existing["commit"]
        or "unknown"
    )
    branch = (
        (os.getenv("RAILWAY_GIT_BRANCH") or "").strip()
        or (os.getenv("GIT_BRANCH") or "").strip()
        or _git("rev-parse", "--abbrev-ref", "HEAD")
        or existing["branch"]
        or "unknown"
    )
    if branch == "HEAD":
        branch = existing["branch"] or "unknown"
    return {"commit": commit, "branch": branch}


def main() -> int:
    meta = resolve_meta()
    # Do not clobber a good baked file with unknown (container start has no .git)
    existing = _read_existing()
    if meta["commit"] == "unknown" and existing["commit"] and existing["commit"] != "unknown":
        print(
            f"Keep existing {OUT.relative_to(ROOT)}: "
            f"{existing['commit']} @ {existing['branch']} (would write unknown)"
        )
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {OUT.relative_to(ROOT)}: {meta['commit']} @ {meta['branch']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
