"""Skip LLM calls after a run of failures so logs and credits are not burned.

Defaults: 5 consecutive failures open the breaker for 10 minutes
(``LLM_CIRCUIT_FAILURES``, ``LLM_CIRCUIT_OPEN_MINUTES``). ``LLM_CIRCUIT_FAILURES=0``
disables it. One log line when it opens, one when a later call succeeds and
closes it. While it is open, callers fail the same way they already do
(``LlmError``, ``API-Fehler``, empty x_search list) and do not hit the network.

The clock is ``time.monotonic``. Tests replace ``now``.
"""

from __future__ import annotations

import os
import threading
import time

from logger import log

FAILURES_VAR = "LLM_CIRCUIT_FAILURES"
OPEN_MINUTES_VAR = "LLM_CIRCUIT_OPEN_MINUTES"
DEFAULT_FAILURES = 5
DEFAULT_OPEN_MINUTES = 10.0

OPEN_LOG = "LLM circuit open"
CLOSED_LOG = "LLM circuit closed"

_lock = threading.Lock()
_consecutive = 0
_open = False
_open_until = 0.0
_announced_open = False


def now() -> float:
    return time.monotonic()


def reset_llm_circuit() -> None:
    """Test helper: breaker closed, no consecutive failures."""
    global _consecutive, _open, _open_until, _announced_open
    with _lock:
        _consecutive = 0
        _open = False
        _open_until = 0.0
        _announced_open = False


def failure_limit() -> int:
    """Consecutive failures that open the breaker. ``0`` disables it."""
    raw = os.environ.get(FAILURES_VAR)
    if raw is None or not str(raw).strip():
        return DEFAULT_FAILURES
    try:
        n = int(str(raw).strip())
    except ValueError:
        return DEFAULT_FAILURES
    return n if n > 0 else 0


def open_minutes() -> float:
    raw = os.environ.get(OPEN_MINUTES_VAR)
    if raw is None or not str(raw).strip():
        return DEFAULT_OPEN_MINUTES
    try:
        minutes = float(str(raw).strip())
    except ValueError:
        return DEFAULT_OPEN_MINUTES
    return minutes if minutes > 0 else DEFAULT_OPEN_MINUTES


def before_llm_call() -> str | None:
    """``None`` when the call may proceed. A reason string when it must be skipped."""
    if failure_limit() <= 0:
        return None
    with _lock:
        if _open and now() < _open_until:
            remain = max(0, int(_open_until - now()))
            return f"{OPEN_LOG}: skipping LLM calls for {remain}s"
        return None


def record_llm_success() -> None:
    """A completed LLM call. Closes the breaker and logs that once."""
    global _consecutive, _open, _open_until, _announced_open
    if failure_limit() <= 0:
        return
    with _lock:
        was_open = _open
        _consecutive = 0
        _open = False
        _open_until = 0.0
        _announced_open = False
        if was_open:
            log(CLOSED_LOG, "INFO")


def record_llm_failure() -> None:
    """One logical LLM call failed (retries already spent). Opens once, logs once."""
    global _consecutive, _open, _open_until, _announced_open
    limit = failure_limit()
    if limit <= 0:
        return
    minutes = open_minutes()
    with _lock:
        _consecutive += 1
        if _consecutive < limit:
            return
        _open = True
        _open_until = now() + minutes * 60
        if _announced_open:
            return
        _announced_open = True
        log(
            f"{OPEN_LOG}: {_consecutive} consecutive failures, skipping LLM calls for {minutes:g} minutes",
            "WARNING",
        )
