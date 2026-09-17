"""Retry policy for ccxt ``fetch_order`` on ``OrderNotFound`` (#481).

``ccxt.OrderNotFound`` is a subclass of ``ccxt.InvalidOrder``. Callers must
check ``OrderNotFound`` first; a bare ``isinstance(exc, InvalidOrder)`` would
skip the retries this module exists to perform.

Backoff is a short series (0.25 + 0.5 + 1.0 s), not Freqtrade's 1+2+5+10.
Never resends ``create_*``.
"""

from __future__ import annotations

from time import sleep
from typing import Any, Callable

import ccxt

from logger import log

# 1 initial call + 3 extras. Sleeps between attempts cap wall time at 1.75s
# plus call latency.
ORDER_NOT_FOUND_EXTRA_ATTEMPTS = 3
ORDER_NOT_FOUND_BACKOFF = (0.25, 0.5, 1.0)


def is_order_not_found(exc: BaseException) -> bool:
    cls = getattr(ccxt, "OrderNotFound", None)
    return isinstance(cls, type) and isinstance(exc, cls)


def call_fetch_order_retrying_not_found(
    fn: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Call ``fn(*args, **kwargs)``; retry only on ``ccxt.OrderNotFound``.

    Other exceptions — including ``InvalidOrder`` that is not ``OrderNotFound``
    — are re-raised immediately with no sleep. After the extra attempts the
    last ``OrderNotFound`` is re-raised.

    Logs WARNING on each OrderNotFound retry. Never resends create.
    """
    attempts = 1 + ORDER_NOT_FOUND_EXTRA_ATTEMPTS
    last_not_found: BaseException | None = None
    ident = args[0] if args else None
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            # OrderNotFound subclasses InvalidOrder — check it first.
            if not is_order_not_found(e):
                raise
            last_not_found = e
            remaining = attempts - 1 - i
            if remaining <= 0:
                break
            delay = ORDER_NOT_FOUND_BACKOFF[min(i, len(ORDER_NOT_FOUND_BACKOFF) - 1)]
            log(
                f"fetch_order({ident!r}) OrderNotFound ({e}); "
                f"retry {i + 1}/{ORDER_NOT_FOUND_EXTRA_ATTEMPTS} in {delay}s",
                "WARNING",
            )
            sleep(delay)
    assert last_not_found is not None
    raise last_not_found
