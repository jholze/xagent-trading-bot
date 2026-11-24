"""Production inputs for ``evaluate_short_cover`` on sandbox-tagged lots (#826).

#809 added ``halted``, ``price_stale``, ``timed_out`` and ``sandbox_schedule``
to ``evaluate_short_cover``. Those inputs only act on lots tagged
``sandbox_path = v220_paper`` while ``shorts.sandbox_expiry_v220.enabled`` is
true. For every other lot this module returns no keyword arguments, so the
caller makes exactly the pre-#826 call.

Sources:

- halt: the shorts kill switch, ``shorts.enabled`` (``shorts_enabled``).
  Kill never blocks a cover; for a tagged lot it makes the lot overdue
  (pending owner confirmation, spec Rev 2).
- stale: cycle only. The source label ``get_prices_batch`` returned for this
  cycle's quote is ``stale`` (last-good fallback after a failed fetch). The
  hub never evaluates a stale tick (#710 skip), so it passes False.
- timeout: neither caller has a separate timeout signal today. A fetch
  timeout in ``price_fetcher`` is caught and surfaces as a stale quote or a
  zero price. Callers pass False.
- schedule: ``sandbox_cover_schedule`` (both budgets from the flag block).

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


def wiring_log_line(
    symbol: str | None,
    inputs: Mapping[str, Any] | None,
    hit: Mapping[str, Any] | None,
    *,
    executed: bool | None = None,
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
        f"timeout={timeout} sched={sched} -> {source} ({why}) executed={executed}"
    )


# #832: stale-quote deferral for the aria_bot queue-off loop. Same name and
# meaning as the hub stat (+1 each time a stale price reaches an open,
# sandbox-tagged, non-climax short, which is then not evaluated).
STALE_TAGGED_SHORT_DEFERRED = "stale_tagged_short_deferred"
_cycle_stats: dict[str, int] = {STALE_TAGGED_SHORT_DEFERRED: 0}


def stale_defers_tagged_short(
    pos: Mapping[str, Any] | None, config_raw: Mapping[str, Any] | None
) -> bool:
    """True for an open sandbox-tagged, non-climax short. Any error is False.

    Shared by the hub stale-tick guard and the aria_bot queue-off loop.
    """
    try:
        from strategies.climax_fade import is_climax_lot
        from strategies.short_expiry import lot_uses_sandbox_expiry
        from strategies.short_math import is_short

        lot = dict(pos or {})
        return bool(
            is_short(lot)
            and float(lot.get("amount") or 0) > 0
            and not is_climax_lot(lot)
            and lot_uses_sandbox_expiry(lot, config_raw)
        )
    except Exception:
        return False


def defer_stale_cycle_quote(
    symbol: str, price_source: Any, config_raw: Mapping[str, Any] | None
) -> bool:
    """aria_bot queue-off loop (#832): skip this coin on a stale quote.

    True only when the cycle's quote label is ``stale`` and every open lot of
    the symbol is a sandbox-tagged, non-climax short. The cover is then not evaluated at
    the stale price; ``stale_tagged_short_deferred`` counts it. Untagged,
    long and climax lots, fresh quotes, the flag off, and any error return
    False, so the caller makes the unchanged ``process_coin`` call.
    """
    if price_source != "stale":
        return False
    try:
        from strategies.positions import find_open_positions_for_symbol
        from strategies.short_expiry import sandbox_expiry_enabled

        raw = dict(config_raw) if config_raw is not None else None
        if not sandbox_expiry_enabled(raw):
            return False
        lots = [lot for _tf, lot in find_open_positions_for_symbol(symbol)]
        # Every open lot of the symbol must be a tagged short: a mixed symbol
        # (e.g. an untagged lot on another timeframe) keeps the base call.
        if not lots or not all(stale_defers_tagged_short(lot, raw) for lot in lots):
            return False
        _cycle_stats[STALE_TAGGED_SHORT_DEFERRED] = (
            int(_cycle_stats.get(STALE_TAGGED_SHORT_DEFERRED) or 0) + 1
        )
        from logger import log

        log(
            f"short_cover stale deferred {symbol} (queue-off cycle) "
            f"{STALE_TAGGED_SHORT_DEFERRED}={_cycle_stats[STALE_TAGGED_SHORT_DEFERRED]}",
            "INFO",
        )
        return True
    except Exception:
        return False


def cycle_stale_stats() -> dict[str, int]:
    return dict(_cycle_stats)


def reset_cycle_stale_stats_for_tests() -> None:
    _cycle_stats[STALE_TAGGED_SHORT_DEFERRED] = 0
