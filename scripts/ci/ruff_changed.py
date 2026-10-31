"""Lint Python files changed relative to the PR base.

The repository is not clean under a broad Ruff rule set (unused imports and
similar). This job uses only rules that do not ask for a reformat:

- E9 syntax errors
- F821 undefined names
- F823 local variable referenced before assignment

Files that already fail those rules are left untouched. The job looks only
at added or modified Python files in the pull request, so introducing the
check does not reformat the tree. Outside Actions, with no base sha, it
skips.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SELECT = "E9,F821,F823"


def changed_python(base: str, head: str) -> list[str]:
    output = subprocess.check_output(
        ["git", "diff", "--name-only", "--diff-filter=ACMR", base, head],
        cwd=REPO_ROOT,
        text=True,
    )
    names = []
    for line in output.splitlines():
        name = line.strip().replace("\\", "/")
        if name.endswith(".py") and (REPO_ROOT / name).is_file():
            names.append(name)
    return names


def main() -> int:
    base = os.environ.get("REVIEW_GATE_BASE", "")
    head = os.environ.get("REVIEW_GATE_HEAD") or "HEAD"
    if not base:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print("ruff: missing REVIEW_GATE_BASE", file=sys.stderr)
            return 1
        print("ruff: no base sha; skipped")
        return 0
    files = changed_python(base, head)
    if not files:
        print("ruff: no changed Python files")
        return 0
    ruff = shutil.which("ruff")
    if ruff is None:
        print("ruff: executable not found", file=sys.stderr)
        return 1
    print(f"ruff: checking {len(files)} file(s) with {SELECT}")
    completed = subprocess.run(
        [ruff, "check", "--select", SELECT, *files],
        cwd=REPO_ROOT,
        check=False,
    )
    return completed.returncode


if __name__ == "__main__":
    sys.exit(main())
