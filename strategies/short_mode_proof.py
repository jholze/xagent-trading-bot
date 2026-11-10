"""Shared paper/testnet mode proof for the v2.2.0 sandbox.

PR #804's ``ModeProof`` (allowed, reason, margin_mode, position_mode) is not
on staging. This module is the shared contract for admission and expiry.
Numeric ceilings are code constants. ``shorts.testnet_futures.paper_start``
(owned by #807) may only tighten them. A missing profile fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal
from typing import Any, Mapping

from core.time_utils import ledger_datetime_utc
from strategies.short_policy import shorts_config

DEFAULT_MAX_AGE_SECONDS = 60
MAX_CLOCK_SKEW_SECONDS = 5
MAX_EXPOSURE_PCT = Decimal("10")
MAX_STOP_LOSS_PCT = Decimal("0.25")
MAX_STOP_LOSS_PCT_ALL = Decimal("0.75")
LEGACY_MARGIN_CAP_PCT = Decimal("20")
MAX_HOLD_HOURS = Decimal("4")
CUTOFF_LOCAL = time(20, 0, 0)
PAPER_MARKETS = frozenset({"paper", "futures_usdt_testnet"})
ACCEPTED_MARGIN = frozenset({"isolated"})
ACCEPTED_POSITION = frozenset({"one_way"})


@dataclass(frozen=True)
class SandboxModeProof:
    allowed: bool
    reason: str
    margin_mode: str = ""
    position_mode: str = ""
    tenant_id: str = ""
    market: str = ""
    live: bool = False


def paper_start(config_raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Read #807's profile. Absent means fail closed, not a looser default."""
    block = shorts_config(config_raw).get("testnet_futures")
    if isinstance(block, dict) and isinstance(block.get("paper_start"), dict):
        return dict(block["paper_start"])
    return {}


def tightened_int(config_raw: Mapping[str, Any] | None, key: str, ceiling: int) -> int:
    raw = paper_start(config_raw).get(key)
    if isinstance(raw, bool) or raw is None:
        return ceiling
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return ceiling
    if value < 0 or value > ceiling:
        return ceiling
    return value


def tightened_decimal(config_raw: Mapping[str, Any] | None, key: str, ceiling: Decimal) -> Decimal:
    raw = paper_start(config_raw).get(key)
    if raw is None or isinstance(raw, bool):
        return ceiling
    try:
        value = Decimal(str(raw).strip())
    except Exception:
        return ceiling
    if not value.is_finite() or value <= 0 or value > ceiling:
        return ceiling
    return value


def tightened_cutoff(config_raw: Mapping[str, Any] | None) -> time:
    raw = paper_start(config_raw).get("cutoff_local")
    if not isinstance(raw, str):
        return CUTOFF_LOCAL
    parts = raw.strip().split(":")
    if len(parts) < 2:
        return CUTOFF_LOCAL
    try:
        hour = int(parts[0])
        minute = int(parts[1])
        second = int(parts[2]) if len(parts) > 2 else 0
        candidate = time(hour, minute, second)
    except (TypeError, ValueError):
        return CUTOFF_LOCAL
    if candidate > CUTOFF_LOCAL:
        return CUTOFF_LOCAL
    return candidate


def mode_proof_max_age_seconds(config_raw: Mapping[str, Any] | None) -> int:
    return tightened_int(config_raw, "mode_proof_max_age_seconds", DEFAULT_MAX_AGE_SECONDS)


def regime_max_age_seconds(config_raw: Mapping[str, Any] | None) -> int:
    return tightened_int(config_raw, "regime_max_age_seconds", DEFAULT_MAX_AGE_SECONDS)


def clock_skew_seconds(config_raw: Mapping[str, Any] | None) -> int:
    return tightened_int(config_raw, "clock_skew_seconds", MAX_CLOCK_SKEW_SECONDS)


def opened_regimes(config_raw: Mapping[str, Any] | None) -> frozenset[str]:
    """Cells opened by the committed paper_start profile. Default is none."""
    raw = paper_start(config_raw).get("opened_regimes", [])
    if raw is None or raw == "":
        return frozenset()
    if isinstance(raw, str):
        parts = [part.strip() for part in raw.split(",")]
        return frozenset(part for part in parts if part and part.lower() != "unknown")
    if isinstance(raw, (list, tuple)):
        opened: set[str] = set()
        for item in raw:
            if not isinstance(item, str):
                continue
            name = item.strip()
            if name and name.lower() != "unknown":
                opened.add(name)
        return frozenset(opened)
    return frozenset()


def _as_of_unix(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return None
    if isinstance(value, datetime):
        stamped = ledger_datetime_utc(value)
        if stamped is None:
            return None
        return int(stamped.timestamp())
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() == "unknown":
            return None
        if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
            return int(text)
        stamped = ledger_datetime_utc(text)
        if stamped is None:
            return None
        return int(stamped.timestamp())
    return None


def parse_sandbox_mode_proof(
    raw: Mapping[str, Any] | None,
    *,
    now: datetime | None,
    config_raw: Mapping[str, Any] | None = None,
) -> SandboxModeProof:
    """Deny a missing, live, cross, or stale proof. Callers cannot widen 60s."""
    if not isinstance(raw, dict):
        return SandboxModeProof(False, "mode_proof_missing")
    if now is None:
        return SandboxModeProof(False, "mode_proof_stale")
    observed = ledger_datetime_utc(now)
    if observed is None:
        return SandboxModeProof(False, "mode_proof_stale")
    tenant = raw.get("tenant_id")
    if not isinstance(tenant, str) or not tenant.strip() or tenant.strip().lower() == "unknown":
        return SandboxModeProof(False, "mode_proof_missing")
    market = raw.get("market")
    if not isinstance(market, str) or not market.strip():
        return SandboxModeProof(False, "mode_proof_missing")
    market_name = market.strip()
    live = raw.get("live") is True or market_name not in PAPER_MARKETS
    if live:
        return SandboxModeProof(
            False,
            "live_denied",
            tenant_id=tenant.strip(),
            market=market_name,
            live=True,
        )
    if raw.get("live") is not False or raw.get("readback_ok") is not True:
        return SandboxModeProof(False, "mode_proof_missing", tenant_id=tenant.strip(), market=market_name)
    margin = str(raw.get("margin_mode") or "").strip().lower()
    position = str(raw.get("position_mode") or "").strip().lower()
    if margin not in ACCEPTED_MARGIN or position not in ACCEPTED_POSITION:
        return SandboxModeProof(
            False,
            "mode_proof_missing",
            margin_mode=margin,
            position_mode=position,
            tenant_id=tenant.strip(),
            market=market_name,
        )
    stamped = _as_of_unix(raw.get("as_of"))
    if stamped is None:
        return SandboxModeProof(False, "mode_proof_stale", margin, position, tenant.strip(), market_name)
    age = int(observed.timestamp()) - stamped
    limit = mode_proof_max_age_seconds(config_raw)
    if age < 0 or age > limit:
        return SandboxModeProof(False, "mode_proof_stale", margin, position, tenant.strip(), market_name)
    return SandboxModeProof(True, "ok", margin, position, tenant.strip(), market_name, False)
