"""Paper/testnet start values frozen 2026-11-10 18:40 Europe/Berlin.

Jens delegated the decision to Kay. These values are not a live approval,
not keys, and not a strategy result. Missing or looser config cannot widen
them. This module does not place orders and is not wired into the adapter.
"""

from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

FROZEN_AT = "2026-11-10T18:40+01:00"
FROZEN_CLEARANCE = Decimal("0.05")
FROZEN_MIN_TICKS = 1
FROZEN_HOST = "https://api-testnet.gateapi.io:443"
FROZEN_TOLERANCE = Decimal("0.01")
PINNED_HOST = "api-testnet.gateapi.io"
LEGACY_HOST = "fx-api-testnet.gateio.ws"
ADL_BLOCK = ("warning", "elevated", "unknown")
_PATH_SEGMENT = re.compile(r"[A-Za-z0-9._~-]+")


@dataclass(frozen=True)
class FreezeDecision:
    allowed: bool
    reason: str
    value: Decimal | None = None
    keep_protection: bool = True
    auto_close: bool = False


def frozen_paper_start(raw: dict | None = None) -> dict[str, Any] | None:
    """Frozen block, the floor when the block is missing, or None if it is looser.

    A present block that widens clearance, tolerance, the host list, ADL, or
    the emergency permit is rejected. Callers must treat None as deny.
    """
    if raw is None:
        raw = _load_committed_config()
    if raw is None:
        return None
    block = _paper_start(raw)
    if block is None:
        return _floor_block()
    if not isinstance(block, dict) or not _block_is_within_floor(block):
        return None
    return block


def clearance_stop(
    *,
    entry: Any,
    liquidation: Any,
    tick: Any,
    proposed_stop: Any = None,
    fraction: Any = None,
    min_ticks: Any = None,
) -> FreezeDecision:
    """Keep at least 5% of entry-to-liquidation, and at least one tick.

    A larger fraction or tick count is a tighten. A smaller one is rejected.
    Missing venue entry, liquidation, or tick does not open a new position.
    """
    used_fraction, fraction_reason = _tightened_decimal(
        fraction, FROZEN_CLEARANCE, "clearance_loosened", "unknown_clearance"
    )
    if fraction_reason:
        return FreezeDecision(False, fraction_reason, keep_protection=True)
    used_ticks, tick_reason = _tightened_int(min_ticks, FROZEN_MIN_TICKS, "ticks_loosened")
    if tick_reason:
        return FreezeDecision(False, tick_reason, keep_protection=True)
    entry_d = _decimal(entry)
    liq_d = _decimal(liquidation)
    tick_d = _decimal(tick)
    if entry_d is None or liq_d is None or tick_d is None or tick_d <= 0 or entry_d <= 0 or liq_d <= entry_d:
        return FreezeDecision(False, "no_exchange_data", keep_protection=True)
    distance = liq_d - entry_d
    required = max(tick_d * used_ticks, distance * used_fraction)
    if required <= 0 or required >= distance:
        return FreezeDecision(False, "clearance_infeasible", keep_protection=True)
    ceiling = _snap_toward_entry(liq_d - required, tick_d, entry_d)
    if ceiling is None or (liq_d - ceiling) < required:
        return FreezeDecision(False, "clearance_infeasible", keep_protection=True)
    proposed = _decimal(proposed_stop) if proposed_stop is not None else None
    if proposed is None:
        return FreezeDecision(True, "tightened", ceiling, keep_protection=True)
    if proposed >= liq_d or proposed <= entry_d:
        return FreezeDecision(False, "no_exchange_data", keep_protection=True)
    if proposed > ceiling:
        return FreezeDecision(True, "tightened", ceiling, keep_protection=True)
    return FreezeDecision(True, "kept", proposed, keep_protection=True)


def origin_allowed(url: str, allowlist: Any = None) -> FreezeDecision:
    """Allow only the frozen testnet origin. No legacy fx-api fallback."""
    hosts = _allowlist(allowlist)
    if hosts is None:
        return FreezeDecision(False, "allowlist_not_frozen")
    if LEGACY_HOST in url or "fx-api" in url:
        return FreezeDecision(False, "legacy_host_rejected")
    parsed = _origin(url)
    if parsed is None:
        return FreezeDecision(False, "origin_rejected")
    if parsed != FROZEN_HOST:
        return FreezeDecision(False, "origin_rejected")
    return FreezeDecision(True, "ok")


def failover_hosts(_failed_host: str = "") -> tuple[str, ...]:
    """The frozen allowlist has no fallback host."""
    return ()


def futures_order_url_allowed(url: str, allowlist: Any = None) -> FreezeDecision:
    """Origin allowlist plus a normalized futures-USDT path. No transport."""
    decision = origin_allowed(url, allowlist)
    if not decision.allowed:
        return decision
    parts = urlsplit(url.strip())
    path = parts.path or ""
    lowered = path.lower()
    if ".." in path or "\\" in path or "//" in path or "%2e" in lowered or "%2f" in lowered or "%5c" in lowered:
        return FreezeDecision(False, "path_escape")
    normalized = posixpath.normpath(path or "/")
    segments = normalized.split("/")
    if segments[:5] != ["", "api", "v4", "futures", "usdt"]:
        return FreezeDecision(False, "path_not_futures_usdt")
    for segment in segments[5:]:
        if not segment or _PATH_SEGMENT.fullmatch(segment) is None:
            return FreezeDecision(False, "path_escape")
    return FreezeDecision(True, "ok")


def reconcile_usdt(
    ledger: Any,
    venue: Any,
    *,
    tolerance: Any = None,
    quantum: Any = None,
) -> FreezeDecision:
    """|ledger-venue| <= T passes. T is 0.01 USDT and cannot be widened.

    Unknown amounts are not treated as zero. A tighter tolerance is kept.
    """
    if tolerance is None:
        used = FROZEN_TOLERANCE
    else:
        used = _decimal(tolerance)
        if used is None or used <= 0:
            return FreezeDecision(False, "unknown_tolerance")
        if used > FROZEN_TOLERANCE:
            return FreezeDecision(False, "tolerance_widened")
    step = _decimal(quantum) if quantum is not None else Decimal("0.000001")
    if step is None or step <= 0:
        return FreezeDecision(False, "unknown_quantum")
    left = _decimal(ledger)
    right = _decimal(venue)
    if left is None or right is None:
        return FreezeDecision(False, "unknown_amount")
    gap = abs(left - right)
    if gap <= used:
        return FreezeDecision(True, "within_tolerance", gap)
    return FreezeDecision(False, "above_tolerance", gap)


def adl_new_short(state: Any) -> FreezeDecision:
    """Warning, elevated, unknown, stale, or unrecognized ADL blocks a new short.

    Protection is kept. This does not cancel or flatten.
    """
    token = "" if state is None else str(state).strip().lower()
    if token not in ADL_BLOCK:
        token = "unknown"
    return FreezeDecision(False, f"adl_{token}", keep_protection=True, auto_close=False)


def emergency_close(config: dict | None = None, permit: Any = None) -> FreezeDecision:
    """No automatic emergency close. Limits are not given default minutes or amounts."""
    block = frozen_paper_start(config)
    if not isinstance(block, dict):
        return FreezeDecision(False, "paper_start_rejected", auto_close=False, keep_protection=True)
    if block.get("emergency_auto_close") is True:
        return FreezeDecision(False, "auto_close_forbidden", auto_close=False, keep_protection=True)
    if permit is not None:
        return FreezeDecision(False, "permit_not_an_auto_close", auto_close=False, keep_protection=True)
    return FreezeDecision(False, "emergency_off", auto_close=False, keep_protection=True)


def _floor_block() -> dict[str, Any]:
    return {
        "_frozen_at": FROZEN_AT,
        "liq_clearance_fraction": format(FROZEN_CLEARANCE, "f"),
        "liq_clearance_min_ticks": FROZEN_MIN_TICKS,
        "host_allowlist": [FROZEN_HOST],
        "legacy_host_fallback": False,
        "reconciliation_tolerance_usdt": format(FROZEN_TOLERANCE, "f"),
        "reconciliation_tolerance_may_widen": False,
        "adl_block_new_short": list(ADL_BLOCK),
        "adl_keep_protection": True,
        "emergency_permit_default": False,
        "emergency_auto_close": False,
    }


def _paper_start(raw: Any) -> Any:
    if not isinstance(raw, dict):
        return None
    shorts = raw.get("shorts")
    if not isinstance(shorts, dict):
        return None
    testnet = shorts.get("testnet_futures")
    if not isinstance(testnet, dict):
        return None
    return testnet.get("paper_start")


def _allowlist(allowlist: Any) -> tuple[str, ...] | None:
    if allowlist is None:
        try:
            block = frozen_paper_start()
        except (OSError, json.JSONDecodeError, UnicodeError):
            return None
        if not isinstance(block, dict):
            return None
        allowlist = block.get("host_allowlist")
        if block.get("legacy_host_fallback") is not False:
            return None
    if isinstance(allowlist, dict):
        if allowlist.get("legacy_host_fallback") is not False:
            return None
        allowlist = allowlist.get("host_allowlist")
    if not isinstance(allowlist, (list, tuple)) or len(allowlist) != 1:
        return None
    if allowlist[0] != FROZEN_HOST:
        return None
    return (FROZEN_HOST,)


def _origin(url: str) -> str | None:
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or parts.username or parts.password or parts.fragment:
        return None
    if host != PINNED_HOST or port not in (None, 443):
        return None
    return FROZEN_HOST


def _snap_toward_entry(ceiling: Decimal, tick: Decimal, entry: Decimal) -> Decimal | None:
    """Round the short stop down onto the tick grid, toward entry."""
    steps = (ceiling / tick).to_integral_value(rounding=ROUND_DOWN)
    snapped = steps * tick
    if snapped <= entry:
        return None
    return snapped


def _block_is_within_floor(block: dict) -> bool:
    fraction = _decimal(block.get("liq_clearance_fraction"))
    if fraction is None or fraction < FROZEN_CLEARANCE:
        return False
    ticks, tick_reason = _tightened_int(
        block.get("liq_clearance_min_ticks"), FROZEN_MIN_TICKS, "ticks_loosened"
    )
    if tick_reason or ticks is None:
        return False
    hosts = block.get("host_allowlist")
    if not isinstance(hosts, list) or hosts != [FROZEN_HOST]:
        return False
    if block.get("legacy_host_fallback") is not False:
        return False
    tolerance = _decimal(block.get("reconciliation_tolerance_usdt"))
    if tolerance is None or tolerance <= 0 or tolerance > FROZEN_TOLERANCE:
        return False
    if block.get("reconciliation_tolerance_may_widen") is not False:
        return False
    adl = block.get("adl_block_new_short")
    if not isinstance(adl, list) or any(name not in adl for name in ADL_BLOCK):
        return False
    if block.get("adl_keep_protection") is not True:
        return False
    if block.get("emergency_permit_default") is not False:
        return False
    if block.get("emergency_auto_close") is not False:
        return False
    return True


def _tightened_decimal(
    value: Any,
    floor: Decimal,
    loosened: str,
    unknown: str,
) -> tuple[Decimal | None, str]:
    if value is None:
        return floor, ""
    parsed = _decimal(value)
    if parsed is None or parsed <= 0:
        return None, unknown
    if parsed < floor:
        return None, loosened
    return parsed, ""


def _tightened_int(value: Any, floor: int, loosened: str) -> tuple[int | None, str]:
    if value is None:
        return floor, ""
    if isinstance(value, bool):
        return None, loosened
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None, loosened
    if parsed < floor:
        return None, loosened
    return parsed, ""


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError, ArithmeticError):
        return None
    if not number.is_finite():
        return None
    return number


def _load_committed_config() -> dict | None:
    path = Path(__file__).resolve().parents[1] / "config.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
