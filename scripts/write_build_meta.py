#!/usr/bin/env python3
"""Write core/build_meta.json for Docker/Railway builds (no .git in image)."""

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


def resolve_meta() -> dict[str, str]:
    commit = (
        (os.getenv("GIT_COMMIT") or "").strip()
        or _git("rev-parse", "--short", "HEAD")
        or "unknown"
    )
    branch = (
        (os.getenv("GIT_BRANCH") or "").strip()
        or _git("rev-parse", "--abbrev-ref", "HEAD")
        or "unknown"
    )
    if branch == "HEAD":
        branch = "unknown"
    return {"commit": commit, "branch": branch}


def main() -> int:
    meta = resolve_meta()
    OUT.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {OUT.relative_to(ROOT)}: {meta['commit']} @ {meta['branch']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())