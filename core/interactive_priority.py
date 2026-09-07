"""Cooperative yield so Telegram interactive commands can preempt eval/cycle.

Flask is threaded, but one process + a busy eval queue still starves
``/positions``: ACK is instant, the snapshot thread waits on the GIL.

Hold ``interactive_priority`` for the duration of the snapshot. Cycle and
eval-worker call ``yield_to_interactive()`` between units of work; that
``time.sleep`` releases the GIL so the snapshot can run.
"""

from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_depth = 0
_pending = threading.Event()
_expires_at = 0.0
_DEFAULT_TTL_SEC = 45.0


def _expired() -> bool:
    return _expires_at > 0.0 and time.monotonic() >= _expires_at


def _clear_if_idle_or_expired() -> None:
    global _depth
    with _lock:
        if _depth <= 0 or _expired():
            _depth = 0
            _pending.clear()


class interactive_priority:
    def __init__(self, ttl_sec: float = _DEFAULT_TTL_SEC):
        self.ttl_sec = float(ttl_sec)

    def __enter__(self):
        global _depth, _expires_at
        with _lock:
            _depth += 1
            _expires_at = time.monotonic() + max(0.05, self.ttl_sec)
            _pending.set()
        return self

    def __exit__(self, exc_type, exc, tb):
        global _depth
        with _lock:
            _depth = max(0, _depth - 1)
            if _depth == 0:
                _pending.clear()
        return False


def interactive_pending() -> bool:
    if not _pending.is_set():
        return False
    if _expired():
        _clear_if_idle_or_expired()
        return False
    return True


def yield_to_interactive(*, poll: float = 0.02, max_wait: float = 5.0) -> None:
    """Block the caller (releasing GIL) while an interactive command runs."""
    if not interactive_pending():
        return
    deadline = time.monotonic() + max(0.0, float(max_wait))
    while interactive_pending() and time.monotonic() < deadline:
        time.sleep(poll)


def reset_interactive_priority_for_tests() -> None:
    global _depth, _expires_at
    with _lock:
        _depth = 0
        _expires_at = 0.0
        _pending.clear()
