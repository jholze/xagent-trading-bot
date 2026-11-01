"""Load the bot's real stop/DCA code, from the working tree or a git ref, on a sim clock.

``strategies.dca`` reads the wall clock (``datetime.now()``) for the DCA grace.
For a replay the clock must be the bar time, so each loaded module gets its
own ``datetime`` name rebound to a subclass whose ``now()`` returns the
harness clock. The source file is not edited. A module from another git ref
(for example the parent of a merge, to replay the code before a PR) is read
with ``git show`` and executed under a private module name; nothing on disk
changes.
"""

from __future__ import annotations

import importlib
import importlib.util
import subprocess
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[2]
BERLIN = ZoneInfo("Europe/Berlin")


class SimClock:
    """Shared mutable clock. ``now`` is an aware UTC datetime."""

    def __init__(self, now: datetime | None = None):
        self.now = now or datetime(2000, 1, 1, tzinfo=timezone.utc)

    def set(self, moment: datetime) -> None:
        self.now = moment


def _clock_datetime(clock: SimClock) -> type:
    class ClockDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            moment = clock.now
            if tz is None:
                # The live writer stores naive Europe/Berlin wall time.
                return moment.astimezone(BERLIN).replace(tzinfo=None)
            return moment.astimezone(tz)

        @classmethod
        def utcnow(cls):  # type: ignore[override]
            return clock.now.astimezone(timezone.utc).replace(tzinfo=None)

    return ClockDatetime


def git_blob(ref: str, path: str) -> str:
    return subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def load_module(
    dotted: str,
    *,
    ref: str | None = None,
    clock: SimClock | None = None,
    alias: str | None = None,
) -> types.ModuleType:
    """Return ``dotted`` (e.g. ``strategies.dca``) as a fresh module.

    ``ref=None`` executes the working-tree source; otherwise the file at that
    git ref. The module is never registered under its real name, so the bot's
    own import of ``strategies.dca`` is untouched.
    """
    rel = dotted.replace(".", "/") + ".py"
    name = alias or f"_bt_{dotted.replace('.', '_')}_{(ref or 'wt').replace('/', '_').replace('-', '_')}_{id(clock)}"
    if ref is None:
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
    else:
        source = git_blob(ref, rel)
    module = types.ModuleType(name)
    module.__file__ = str(REPO_ROOT / rel)
    module.__package__ = dotted.rpartition(".")[0]
    code = compile(source, f"<{ref or 'worktree'}:{rel}>", "exec")
    sys.modules[name] = module  # dataclasses resolve their module during exec
    exec(code, module.__dict__)  # noqa: S102 - repo source, offline
    if clock is not None and "datetime" in module.__dict__:
        module.__dict__["datetime"] = _clock_datetime(clock)
    return module


def resolve_ref(ref: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--short=8", ref], cwd=REPO_ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()


def import_real(dotted: str) -> types.ModuleType:
    """Plain import of a pure helper (no clock use), e.g. ``strategies.positions``."""
    return importlib.import_module(dotted)
