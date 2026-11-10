"""Shared paper/testnet mode proof for the v2.2.0 sandbox.

PR #804's ``ModeProof`` (allowed, reason, margin_mode, position_mode) is not
on staging. This module is the shared contract for admission and expiry.
The age ceiling comes from ``shorts.sandbox_v220.paper_start`` and is never
raised by a caller. Missing config uses 60 seconds, which is also the
hard maximum.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from core.time_utils import ledger_datetime_utc
from strategies.short_policy import shorts_config

DEFAULT_MAX_AGE_SECONDS = 60
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
    block = shorts_config(config_raw).get("sandbox_v220")
    if isinstance(block, dict) and isinstance(block.get("paper_start"), dict):
        return dict(block["paper_start"])
    return {}


def _bounded_seconds(config_raw: Mapping[str, Any] | None, key: str) -> int:
    raw = paper_start(config_raw).get(key, DEFAULT_MAX_AGE_SECONDS)
    if isinstance(raw, bool):
        return DEFAULT_MAX_AGE_SECONDS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_AGE_SECONDS
    if value < 0:
        return DEFAULT_MAX_AGE_SECONDS
    return min(value, DEFAULT_MAX_AGE_SECONDS)


def mode_proof_max_age_seconds(config_raw: Mapping[str, Any] | None) -> int:
    return _bounded_seconds(config_raw, "mode_proof_max_age_seconds")


def regime_max_age_seconds(config_raw: Mapping[str, Any] | None) -> int:
    return _bounded_seconds(config_raw, "regime_max_age_seconds")


def clock_skew_seconds(config_raw: Mapping[str, Any] | None) -> int:
    raw = paper_start(config_raw).get("clock_skew_seconds", 5)
    if isinstance(raw, bool):
        return 5
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 5
    if value < 0:
        return 5
    return min(value, 5)


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
