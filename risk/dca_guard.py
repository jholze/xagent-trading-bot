"""Hard DCA lock (#640). No off-switch.

An add is any buy against an open long lot, including a second timeframe
and a bot call over MCP. Only a human operator source is exempt. MCP
sources are never exempt: the execute path only receives ``mcp:{actor}``
and cannot tell a human from a bot, so every MCP add is guarded.

Compared price is ``order.price`` — the same mark the order is submitted
at (cycle ``current_price`` or the MCP price). ``live == avg`` passes.
Staleness uses ``architecture.stale_price_max_age_sec`` (price_fetcher),
not a new threshold.

``dca_rounds`` lives on the open lot. It increments when a DCA fill is
applied and resets to 0 only when that lot is fully flat (amount ~ 0).
A partial sell does not reset it. A lock does not reset it. A genuinely
new lot (buy after a full close) therefore starts at 0.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# First match is the primary tape code. Later codes are still recorded.
_CODE_MISSING = "dca_guard_missing_input"
_CODE_LOCKED = "dca_guard_locked"
_CODE_BELOW = "dca_guard_below_avg"
_CODE_ROUNDS = "dca_guard_max_rounds"


@dataclass
class DcaGuardResult:
    blocked: bool
    codes: list[str] = field(default_factory=list)
    price: float | None = None
    avg: float | None = None
    dca_rounds: int | None = None
    locked: bool | None = None

    @property
    def code(self) -> str:
        return self.codes[0] if self.codes else ""


def is_human_operator_buy(source: str | None) -> bool:
    """Human operator only. Every ``mcp*`` source is automatic."""
    src = str(source or "").strip().lower()
    if not src or src.startswith("mcp"):
        return False
    from strategies.position_lock import is_manual_source

    return is_manual_source(src)


def _price_is_stale(symbol: str, indicators: dict | None) -> bool:
    """Existing quote-freshness rule. Unknown age is not stale by itself;
    a non-positive price is missing and handled by the caller. An expired
    last-good quote, or an explicit age above the configured max, is stale.
    """
    from price_fetcher import _stale_price_max_age_sec, stale_expired_symbols

    max_age = float(_stale_price_max_age_sec())
    if isinstance(indicators, dict) and indicators.get("price_age_sec") is not None:
        try:
            age = float(indicators.get("price_age_sec"))
        except (TypeError, ValueError):
            return True
        if age > max_age:
            return True
    try:
        expired = stale_expired_symbols()
    except Exception:
        return True
    return str(symbol or "") in expired


def _lock_active(pos: dict) -> bool | None:
    """True/False when the lock document can be read. None if it cannot.

    ``risk.position_locks.enabled`` is not an allowance: an active lock
    document blocks adds even when that kill switch is off.
    """
    try:
        from strategies.position_lock import get_lock, lock_is_active

        if "lock" not in pos:
            return False
        raw = pos.get("lock")
        if raw is None:
            return False
        if not isinstance(raw, dict):
            return None
        return bool(lock_is_active(get_lock(pos)))
    except Exception:
        return None


def evaluate_dca_guard(
    pos: dict | None,
    *,
    price: float | None,
    source: str | None,
    has_open_lot: bool,
    symbol: str = "",
    indicators: dict | None = None,
) -> DcaGuardResult:
    """Pure add-on lock. New entries (no open lot) are not this guard."""
    if not has_open_lot:
        return DcaGuardResult(blocked=False, price=_as_float(price))
    if is_human_operator_buy(source):
        return DcaGuardResult(blocked=False, price=_as_float(price))

    if not isinstance(pos, dict):
        return DcaGuardResult(
            blocked=True,
            codes=[_CODE_MISSING],
            price=_as_float(price),
            locked=None,
        )

    live = _as_float(price)
    avg = _as_float(pos.get("average_entry"))
    if avg is None:
        avg = _as_float(pos.get("entry_price"))
    rounds_raw = pos.get("dca_rounds") if "dca_rounds" in pos else None
    rounds: int | None
    if rounds_raw is None:
        rounds = None
    else:
        try:
            rounds = int(rounds_raw)
        except (TypeError, ValueError):
            rounds = None
    locked = _lock_active(pos)
    stale = False
    try:
        stale = _price_is_stale(symbol or str(pos.get("symbol") or ""), indicators)
    except Exception:
        stale = True

    missing = (
        live is None
        or live <= 0
        or avg is None
        or avg <= 0
        or rounds is None
        or rounds < 0
        or locked is None
        or stale
    )
    if missing:
        return DcaGuardResult(
            blocked=True,
            codes=[_CODE_MISSING],
            price=live,
            avg=avg,
            dca_rounds=rounds,
            locked=locked,
        )

    codes: list[str] = []
    if locked:
        codes.append(_CODE_LOCKED)
    if live < avg:
        codes.append(_CODE_BELOW)
    if rounds >= 1:
        codes.append(_CODE_ROUNDS)
    return DcaGuardResult(
        blocked=bool(codes),
        codes=codes,
        price=live,
        avg=avg,
        dca_rounds=rounds,
        locked=bool(locked),
    )


def policy_skip_for_guard(
    pos: dict | None,
    price: float | None,
    *,
    symbol: str = "",
    indicators: dict | None = None,
) -> Any | None:
    """Return a skip policy result when the lock would block this add.

    The decision text then says ``action=skip`` with the ``dca_guard_*``
    code, not ``action=buy_dca``. None means the policy result is unchanged.
    """
    result = evaluate_dca_guard(
        pos,
        price=price,
        source="dca",
        has_open_lot=True,
        symbol=symbol,
        indicators=indicators,
    )
    if not result.blocked:
        return None
    from strategies.dca_policy import DcaPolicyResult

    return DcaPolicyResult(
        size_mult=1.0,
        skip=True,
        reason_codes=tuple(result.codes),
    )


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out
