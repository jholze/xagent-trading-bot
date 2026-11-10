"""Default-off sandbox admission for draft T220-03.

The flag is missing or not boolean true → no admission. A missing risk
profile denies entry and is not replaced with zeros. When the flag is on,
paper/testnet start defaults from the 2026-11-10 owner freeze apply:
1x leverage, 10% virtual-equity exposure, 0.25% stop loss per position,
0.75% across short stops, and the existing 20% margin cap is not raised.
Every regime cell starts closed. Tokenized stocks and unclear assets get
no new short; that deferral is not a global product ban.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from core.time_utils import ledger_datetime_utc
from strategies.short_mode_proof import opened_regimes, parse_sandbox_mode_proof, regime_max_age_seconds
from strategies.short_policy import shorts_allow_live, shorts_config

FLAG_KEY = "sandbox_admission_v220"
_PROFILE_SLOTS = (
    "profile_id",
    "notional",
    "open_short_notional",
    "stop_risk",
    "existing_short_stop_risk",
    "nav",
    "quote_quantum",
    "metadata",
    "reservations",
)
_OPTIONAL_DEFAULTS = (
    "max_exposure_pct",
    "max_stop_loss_pct",
    "max_stop_loss_pct_all",
    "legacy_margin_cap_pct",
)
_RESERVATION_KEYS = ("pending", "uncertain", "reserved")


@dataclass(frozen=True)
class AdmissionDecision:
    admitted: bool
    action: str
    reasons: tuple[str, ...]
    parameters: tuple[tuple[str, str], ...]
    mode_parameters: tuple[tuple[str, str], ...] = ()
    regime_applied: bool = False
    stock_exclusion_applied: bool = False
    short_path_deferred: bool = False


def sandbox_start_defaults() -> dict[str, str]:
    """Paper/testnet start configuration. Unused while the flag is off.

    Percents match the existing ``max_margin_pct`` unit (20 means 20%).
    The legacy 20% cap is a ceiling this profile does not raise.
    """
    return {
        "leverage": "1",
        "max_exposure_pct": "10",
        "max_stop_loss_pct": "0.25",
        "max_stop_loss_pct_all": "0.75",
        "legacy_margin_cap_pct": "20",
        "opened_regimes": "",
        "stock_phase": "defer_new_shorts",
        "min_qty_policy": "exchange_min",
    }


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
    """Apply the frozen start profile only when the sandbox flag is on."""
    if not sandbox_admission_enabled(config_raw):
        return AdmissionDecision(
            admitted=False,
            action="none",
            reasons=("sandbox_disabled",),
            parameters=(),
            mode_parameters=(),
            regime_applied=False,
            stock_exclusion_applied=False,
            short_path_deferred=False,
        )
    profile_params, profile_missing = _plumb_profile(profile)
    parameters = _with_defaults(profile_params, profile)
    parameters = _plumb_candidate(candidate, parameters)
    proof = parse_sandbox_mode_proof(mode_proof, now=now, config_raw=config_raw)
    reasons: list[str] = []
    if shorts_allow_live(config_raw) or proof.live or proof.reason == "live_denied":
        reasons.append("live_denied")
    elif not proof.allowed:
        reasons.append(proof.reason)
    if profile_missing:
        reasons.append("risk_profile_missing")
    else:
        reasons.extend(_frozen_limits(profile, candidate, config_raw=config_raw, now=now))
    reasons.extend(_position_reasons(candidate))
    ordered = tuple(_dedupe(reasons))
    admitted = not ordered
    return AdmissionDecision(
        admitted=admitted,
        action="sandbox_short_candidate" if admitted else "none",
        reasons=ordered,
        parameters=parameters,
        mode_parameters=_proof_echo(proof),
        regime_applied=not profile_missing,
        stock_exclusion_applied=False,
        short_path_deferred="short_path_deferred" in ordered,
    )


def _with_defaults(
    params: tuple[tuple[str, str], ...],
    profile: Mapping[str, Any] | None,
) -> tuple[tuple[str, str], ...]:
    if not isinstance(profile, dict):
        return params
    echoed = dict(params)
    defaults = sandbox_start_defaults()
    if "leverage" not in echoed and "leverage" not in profile:
        echoed["leverage"] = defaults["leverage"]
    for key in _OPTIONAL_DEFAULTS:
        if key not in echoed and key not in profile:
            echoed[key] = defaults[key]
    return tuple(echoed.items())


def _dedupe(reasons: list[str]) -> list[str]:
    seen: list[str] = []
    for reason in reasons:
        if reason not in seen:
            seen.append(reason)
    return seen


def _number(value: Any) -> Decimal | None:
    token = _exact_token(value)
    if token is None:
        return None
    try:
        number = Decimal(token)
    except Exception:
        return None
    if not number.is_finite():
        return None
    return number


def _ceiling(profile: Mapping[str, Any], key: str, default: Decimal) -> tuple[Decimal | None, str | None]:
    if key not in profile:
        return default, None
    value = _number(profile.get(key))
    if value is None:
        return None, "risk_profile_missing"
    if value > default:
        return None, "cap_loosened"
    return value, None


def _frozen_limits(
    profile: Mapping[str, Any] | None,
    candidate: Mapping[str, Any] | None,
    *,
    config_raw: Mapping[str, Any] | None,
    now: datetime | None,
) -> list[str]:
    """Paper/testnet start limits. A looser figure is rejected, not applied."""
    if not isinstance(profile, dict):
        return ["risk_profile_missing"]
    reasons: list[str] = []
    defaults = sandbox_start_defaults()
    if "leverage" in profile:
        leverage = _number(profile.get("leverage"))
        if leverage is None:
            reasons.append("risk_profile_missing")
            leverage = None
        elif leverage != Decimal(defaults["leverage"]):
            reasons.append("leverage_above_start")
    else:
        leverage = Decimal(defaults["leverage"])
    nav = _number(profile.get("nav")) if "nav" in profile else None
    if nav is None or nav <= 0:
        reasons.append("nav_unknown")
    quantum = _number(profile.get("quote_quantum")) if "quote_quantum" in profile else None
    if quantum is None or quantum <= 0:
        reasons.append("quantum_unknown")
    notional = _number(profile.get("notional")) if "notional" in profile else None
    if notional is None or notional < 0:
        reasons.append("notional_unknown")
    stop_risk = _number(profile.get("stop_risk")) if "stop_risk" in profile else None
    if stop_risk is None or stop_risk < 0:
        reasons.append("stop_risk_unknown")
    booked = _number(profile.get("existing_short_stop_risk")) if "existing_short_stop_risk" in profile else None
    if booked is None or booked < 0:
        reasons.append("stop_book_unknown")
    reserve = _reservation_sum(profile.get("reservations") if "reservations" in profile else None)
    if reserve is None:
        reasons.append("reservation_unknown")
    exposure_cap, cap_reason = _ceiling(profile, "max_exposure_pct", Decimal(defaults["max_exposure_pct"]))
    if cap_reason:
        reasons.append(cap_reason)
    stop_cap, stop_reason = _ceiling(profile, "max_stop_loss_pct", Decimal(defaults["max_stop_loss_pct"]))
    if stop_reason:
        reasons.append(stop_reason)
    book_cap, book_reason = _ceiling(
        profile, "max_stop_loss_pct_all", Decimal(defaults["max_stop_loss_pct_all"])
    )
    if book_reason:
        reasons.append(book_reason)
    legacy_cap, legacy_reason = _ceiling(
        profile, "legacy_margin_cap_pct", Decimal(defaults["legacy_margin_cap_pct"])
    )
    if legacy_reason:
        reasons.append(legacy_reason)
    configured = shorts_config(config_raw).get("max_margin_pct", defaults["legacy_margin_cap_pct"])
    configured_cap = _number(configured)
    if configured_cap is None:
        reasons.append("legacy_cap_unknown")
    elif configured_cap > Decimal(defaults["legacy_margin_cap_pct"]):
        reasons.append("legacy_cap_raised")
    elif legacy_cap is not None:
        legacy_cap = min(legacy_cap, configured_cap)
    open_notional = _number(profile.get("open_short_notional")) if "open_short_notional" in profile else None
    if open_notional is None or open_notional < 0:
        reasons.append("open_notional_unknown")
    if (
        nav is not None
        and nav > 0
        and notional is not None
        and reserve is not None
        and open_notional is not None
        and exposure_cap is not None
        and quantum is not None
    ):
        exposure = open_notional + reserve + notional
        limit = nav * exposure_cap / Decimal("100")
        if exposure > limit:
            reasons.append("exposure_cap")
    if nav is not None and nav > 0 and stop_risk is not None and stop_cap is not None:
        if stop_risk > nav * stop_cap / Decimal("100"):
            reasons.append("stop_loss_cap")
    if (
        nav is not None
        and nav > 0
        and stop_risk is not None
        and booked is not None
        and book_cap is not None
    ):
        if stop_risk + booked > nav * book_cap / Decimal("100"):
            reasons.append("stop_loss_book_cap")
    if (
        nav is not None
        and nav > 0
        and notional is not None
        and leverage is not None
        and leverage > 0
        and legacy_cap is not None
    ):
        margin = notional / leverage
        if "margin" in profile:
            supplied = _number(profile.get("margin"))
            if supplied is None or (quantum is not None and abs(supplied - margin) > 0):
                if supplied is None or supplied != margin:
                    reasons.append("margin_notional_mismatch")
        if margin / nav * Decimal("100") > legacy_cap:
            reasons.append("legacy_margin_cap")
    reasons.extend(_regime_reasons(profile, candidate, config_raw=config_raw, now=now))
    reasons.extend(_product_reasons(candidate))
    reasons.extend(_qty_reasons(candidate))
    return reasons


def _reservation_sum(value: Any) -> Decimal | None:
    if _reservations_token(value) is None or not isinstance(value, dict):
        return None
    total = Decimal("0")
    for key in _RESERVATION_KEYS:
        number = _number(value.get(key))
        if number is None or number < 0:
            return None
        total += number
    return total


def _caller_tries_to_open(profile: Mapping[str, Any]) -> bool:
    if "opened_regimes" not in profile:
        return False
    raw = profile.get("opened_regimes")
    if raw is None or raw == "":
        return False
    if isinstance(raw, str):
        return any(part.strip() and part.strip().lower() != "unknown" for part in raw.split(","))
    if isinstance(raw, (list, tuple)):
        return any(isinstance(item, str) and item.strip() for item in raw)
    return True


def _regime_reasons(
    profile: Mapping[str, Any],
    candidate: Mapping[str, Any] | None,
    *,
    config_raw: Mapping[str, Any] | None,
    now: datetime | None,
) -> list[str]:
    reasons: list[str] = []
    if now is None:
        reasons.append("now_missing")
    if _caller_tries_to_open(profile):
        reasons.append("regime_opened_without_freeze")
    opened = opened_regimes(config_raw)
    regime = None
    if isinstance(candidate, dict) and "regime" in candidate:
        regime = _exact_token(candidate.get("regime"))
    if regime is None:
        reasons.append("regime_unknown")
    elif regime not in opened:
        reasons.append("regime_closed")
    if not isinstance(candidate, dict) or "regime_as_of" not in candidate:
        reasons.append("regime_stale")
        return reasons
    if now is None:
        return reasons
    as_of = ledger_datetime_utc(candidate.get("regime_as_of"))
    now_utc = ledger_datetime_utc(now)
    limit = regime_max_age_seconds(config_raw)
    if as_of is None or now_utc is None or as_of > now_utc or (now_utc - as_of).total_seconds() > limit:
        reasons.append("regime_stale")
    return reasons


def _product_reasons(candidate: Mapping[str, Any] | None) -> list[str]:
    kind = None
    if isinstance(candidate, dict) and "product_kind" in candidate:
        kind = _exact_token(candidate.get("product_kind"))
    if kind != "perp":
        return ["short_path_deferred"]
    return []


def _qty_reasons(candidate: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(candidate, dict):
        return ["min_qty_unknown"]
    if "qty" not in candidate or "exchange_min_qty" not in candidate:
        return ["min_qty_unknown"]
    qty = _number(candidate.get("qty"))
    minimum = _number(candidate.get("exchange_min_qty"))
    if qty is None or minimum is None or minimum <= 0 or qty <= 0:
        return ["min_qty_unknown"]
    if qty < minimum:
        return ["below_min_qty"]
    if qty != minimum:
        return ["above_min_qty"]
    return []


def _position_reasons(candidate: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(candidate, dict) or "existing_side" not in candidate:
        return ["existing_side_unknown"]
    side_raw = candidate.get("existing_side")
    if not isinstance(side_raw, str) or _exact_token(side_raw) is None:
        return ["existing_side_unknown"]
    side = side_raw.strip().lower()
    if side not in {"flat", "short", "long"}:
        return ["existing_side_unknown"]
    if "existing_qty" not in candidate:
        if side == "long":
            return ["long_not_flipped"]
        return ["existing_qty_invalid"]
    qty = _number(candidate.get("existing_qty"))
    if qty is None:
        return ["existing_qty_invalid"]
    if side == "long" and qty != 0:
        return ["long_not_flipped"]
    return []


def _proof_echo(proof) -> tuple[tuple[str, str], ...]:
    return (
        ("allowed", "true" if proof.allowed else "false"),
        ("reason", proof.reason),
        ("margin_mode", proof.margin_mode),
        ("position_mode", proof.position_mode),
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

