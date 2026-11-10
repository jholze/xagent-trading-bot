"""Default-off sandbox admission plumbing for draft T220-03.

The flag is missing or not boolean true → no admission. A missing risk
profile denies entry and is not replaced with zeros or paper defaults.
Supplied profile slots are echoed and are not compared with a limit table.
Numeric AC-03a/b/c rules and the regime table stay unimplemented until the
owner freeze. This module does not exclude stock tokens and does not import
the risk manager.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from strategies.short_policy import shorts_allow_live, shorts_config

FLAG_KEY = "sandbox_admission_v220"
_PROFILE_SLOTS = (
    "profile_id",
    "leverage",
    "notional",
    "margin",
    "stop_risk",
    "nav",
    "metadata",
    "reservations",
)
_RESERVATION_KEYS = ("pending", "uncertain", "reserved")
_PAPER_MARKETS = frozenset({"paper", "futures_usdt_testnet"})


@dataclass(frozen=True)
class AdmissionDecision:
    admitted: bool
    action: str
    reasons: tuple[str, ...]
    parameters: tuple[tuple[str, str], ...]
    mode_parameters: tuple[tuple[str, str], ...] = ()
    regime_applied: bool = False
    stock_exclusion_applied: bool = False


def sandbox_admission_enabled(config_raw: Mapping[str, Any] | None) -> bool:
    """True only for boolean true. Missing, false, and other types stay off."""
    block = shorts_config(config_raw).get(FLAG_KEY)
    return isinstance(block, dict) and block.get("enabled") is True


def cover_permitted_when_entry_denied(
    *,
    position_side: str | None,
    reduce_only: bool,
) -> bool:
    """Reduce-only cover of an existing short stays allowed when entry is denied."""
    if reduce_only is not True or not isinstance(position_side, str):
        return False
    return position_side.strip().lower() == "short"


def evaluate_short_admission(
    candidate: Mapping[str, Any] | None = None,
    *,
    config_raw: Mapping[str, Any] | None = None,
    profile: Mapping[str, Any] | None = None,
    mode_proof: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> AdmissionDecision:
    """Deny unless a later frozen revision adds numeric and regime rules.

    ``now`` is accepted so callers can pass a clock. This revision does not
    invent a staleness limit from it.
    """
    del now
    if not sandbox_admission_enabled(config_raw):
        return AdmissionDecision(
            admitted=False,
            action="none",
            reasons=("sandbox_disabled",),
            parameters=(),
            mode_parameters=(),
            regime_applied=False,
            stock_exclusion_applied=False,
        )
    profile_params, profile_missing = _plumb_profile(profile)
    parameters = _plumb_candidate(candidate, profile_params)
    proof_params, proof_missing, proof_live = _parse_mode_proof(mode_proof)
    reasons: list[str] = []
    if shorts_allow_live(config_raw) or proof_live:
        reasons.append("live_denied")
    if profile_missing:
        reasons.append("risk_profile_missing")
    if proof_missing:
        reasons.append("mode_proof_missing")
    if _open_long(candidate):
        reasons.append("long_not_flipped")
    if not reasons:
        reasons.append("owner_freeze_pending")
    return AdmissionDecision(
        admitted=False,
        action="none",
        reasons=tuple(reasons),
        parameters=parameters,
        mode_parameters=proof_params,
        regime_applied=False,
        stock_exclusion_applied=False,
    )


def _exact_token(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() == "unknown":
            return None
        return text
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            return None
        return format(value, "f")
    return None


def _metadata_token(value: Any) -> str | None:
    if not isinstance(value, dict) or not value:
        return None
    parts: list[str] = []
    for key in sorted(value):
        if not isinstance(key, str):
            return None
        name = key.strip()
        if not name or name.lower() == "unknown":
            return None
        token = _exact_token(value[key])
        if token is None:
            return None
        parts.append(f"{name}={token}")
    return ",".join(parts)


def _reservations_token(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    parts: list[str] = []
    for key in _RESERVATION_KEYS:
        if key not in value:
            return None
        token = _exact_token(value[key])
        if token is None:
            return None
        parts.append(f"{key}={token}")
    return ",".join(parts)


def _slot_token(slot: str, value: Any) -> str | None:
    if slot == "metadata":
        return _metadata_token(value)
    if slot == "reservations":
        return _reservations_token(value)
    return _exact_token(value)


def _plumb_profile(profile: Mapping[str, Any] | None) -> tuple[tuple[tuple[str, str], ...], bool]:
    if not isinstance(profile, dict):
        return (), True
    echoed: list[tuple[str, str]] = []
    missing = False
    for slot in _PROFILE_SLOTS:
        if slot not in profile:
            missing = True
            continue
        token = _slot_token(slot, profile[slot])
        if token is None:
            missing = True
            continue
        echoed.append((slot, token))
    for key in sorted(profile):
        if key in _PROFILE_SLOTS or not isinstance(key, str):
            continue
        token = _exact_token(profile[key])
        if token is None:
            continue
        echoed.append((key, token))
    return tuple(echoed), missing


def _plumb_candidate(
    candidate: Mapping[str, Any] | None,
    profile_params: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    if not isinstance(candidate, dict):
        return profile_params
    seen = {key for key, _ in profile_params}
    extra: list[tuple[str, str]] = []
    for key in sorted(candidate):
        if not isinstance(key, str) or key in seen:
            continue
        token = _exact_token(candidate[key])
        if token is None:
            continue
        extra.append((key, token))
    return profile_params + tuple(extra)


def _open_long(candidate: Mapping[str, Any] | None) -> bool:
    if not isinstance(candidate, dict):
        return False
    side = candidate.get("existing_side")
    if not isinstance(side, str) or side.strip().lower() != "long":
        return False
    if "existing_qty" not in candidate:
        return True
    token = _exact_token(candidate.get("existing_qty"))
    if token is None:
        return True
    return Decimal(token) != 0


def _parse_mode_proof(
    proof: Mapping[str, Any] | None,
) -> tuple[tuple[tuple[str, str], ...], bool, bool]:
    if not isinstance(proof, dict):
        return (), True, False
    missing = False
    live = False
    params: list[tuple[str, str]] = []

    tenant = _exact_token(proof.get("tenant_id")) if "tenant_id" in proof else None
    if tenant is None:
        missing = True
    else:
        params.append(("tenant_id", tenant))

    if "market" not in proof:
        missing = True
    else:
        market = _exact_token(proof.get("market"))
        if market is None:
            missing = True
        else:
            params.append(("market", market))
            if market not in _PAPER_MARKETS:
                live = True

    if "live" not in proof or not isinstance(proof.get("live"), bool):
        missing = True
    elif proof.get("live") is True:
        live = True
        params.append(("live", "true"))
    else:
        params.append(("live", "false"))

    if proof.get("readback_ok") is True:
        params.append(("readback_ok", "true"))
    else:
        missing = True
        if proof.get("readback_ok") is False:
            params.append(("readback_ok", "false"))

    if "as_of" not in proof:
        missing = True
    else:
        as_of = proof.get("as_of")
        if isinstance(as_of, datetime):
            if as_of.tzinfo is None:
                missing = True
            else:
                params.append(("as_of", as_of.astimezone(timezone.utc).isoformat()))
        else:
            token = _exact_token(as_of)
            if token is None:
                missing = True
            else:
                params.append(("as_of", token))
    return tuple(params), missing, live
