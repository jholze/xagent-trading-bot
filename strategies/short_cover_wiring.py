"""Production inputs for ``evaluate_short_cover`` on sandbox-tagged lots (#826).

#809 added ``halted``, ``price_stale``, ``timed_out`` and ``sandbox_schedule``
to ``evaluate_short_cover``. Those inputs only act on lots tagged
``sandbox_path = v220_paper`` while ``shorts.sandbox_expiry_v220.enabled`` is
true. For every other lot this module returns no keyword arguments, so the
caller makes exactly the pre-#826 call.

Sources:

- halt: the shorts kill switch, ``shorts.enabled`` (``shorts_enabled``).
  Kill never blocks a cover; for a tagged lot it makes the lot overdue.
- stale: what the caller already knows (hub: tick age above
  ``exit_realtime.max_tick_age_sec``; cycle: the quote was served from the
  last-good fallback because the fresh fetch failed).
- timeout: neither caller has a separate timeout signal today. A fetch
  timeout in ``price_fetcher`` is caught and surfaces as a stale quote or a
  zero price. Callers pass False unless they know better.
- schedule: ``sandbox_cover_schedule`` (budgets from the flag block).

A failure while resolving an input never raises. Halt falls back to None
(not a halt), schedule to None (#809 fail-closed: ``buffer_unknown``).
"""

from __future__ import annotations

from typing import Any, Callable, Mapping


def _probe(value: bool | Callable[[], bool]) -> bool:
    if callable(value):
        try:
            return value() is True
        except Exception:
            return False
    return value is True


def sandbox_cover_inputs(
    pos: Mapping[str, Any] | None,
    config_raw: Mapping[str, Any] | None,
    *,
    price_stale: bool | Callable[[], bool] = False,
    timed_out: bool | Callable[[], bool] = False,
) -> dict[str, Any]:
    """Keyword arguments for ``evaluate_short_cover``. Empty for untagged lots.

    ``price_stale`` and ``timed_out`` may be callables so a caller only pays
    for the lookup when the lot is tagged.
    """
    try:
        from strategies.short_expiry import lot_uses_sandbox_expiry

        if not lot_uses_sandbox_expiry(pos, config_raw):
            return {}
    except Exception:
        return {}
    inputs: dict[str, Any] = {
        "price_stale": _probe(price_stale),
        "timed_out": _probe(timed_out),
        "halted": None,
        "sandbox_schedule": None,
    }
    try:
        from strategies.short_policy import shorts_enabled

        inputs["halted"] = not shorts_enabled(dict(config_raw) if config_raw is not None else None)
    except Exception:
        inputs["halted"] = None
    try:
        from strategies.short_expiry import sandbox_cover_schedule

        inputs["sandbox_schedule"] = sandbox_cover_schedule(config_raw)
    except Exception:
        inputs["sandbox_schedule"] = None
    return inputs


def cycle_quote_stale(symbol: str) -> bool:
    """True when the cycle price for ``symbol`` came from the last-good fallback."""
    try:
        from price_fetcher import last_quote_source

        return last_quote_source(symbol) == "stale"
    except Exception:
        return False


def wiring_log_line(
    symbol: str | None,
    inputs: Mapping[str, Any] | None,
    hit: Mapping[str, Any] | None,
) -> str | None:
    """One compact line when halt/stale/timeout/schedule shaped a tagged decision.

    None for untagged lots and for a tagged lot still inside its hold with no
    halt, stale or timeout, so a healthy lot does not log every tick.
    """
    if not inputs:
        return None
    halted = inputs.get("halted")
    stale = inputs.get("price_stale") is True
    timeout = inputs.get("timed_out") is True
    overdue = isinstance(hit, Mapping) and hit.get("state") == "overdue"
    if not (halted is True or stale or timeout or overdue):
        return None
    sched = "set" if inputs.get("sandbox_schedule") else "none"
    source = str(hit.get("source") or "cover") if isinstance(hit, Mapping) else "hold"
    why = str(hit.get("rationale") or "")[:80] if isinstance(hit, Mapping) else ""
    return (
        f"short_cover tagged {symbol or '?'} halt={halted} stale={stale} "
        f"timeout={timeout} sched={sched} -> {source} ({why})"
    )
