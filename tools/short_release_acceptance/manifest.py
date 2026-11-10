"""Disabled manual-run manifest. No path in this module contacts an exchange."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from tools.short_release_acceptance.constants import (
    ALLOWED_ACTIONS,
    AUTHORIZATION_SENTINEL,
    FORBIDDEN_ACTIONS,
    FROZEN_AGGREGATE_STOP_RISK_PCT,
    FROZEN_CUTOFF,
    FROZEN_LEVERAGE,
    FROZEN_MAX_HOLDING_HOURS,
    FROZEN_NOTIONAL_PCT,
    FROZEN_ORIGIN,
    FROZEN_SAFETY_CAP_PCT,
    FROZEN_STOP_RISK_PCT,
    FROZEN_TIMEZONE,
    FROZEN_WEEKDAYS,
    REDACTION_FLAGS,
    RESERVED_TENANTS,
    SECRET_KEY_NAMES,
    SECRET_VALUE_MARKERS,
    to_decimal,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
_BERLIN = ZoneInfo(FROZEN_TIMEZONE)


class ManifestError(ValueError):
    """The caller asked this package to write a forbidden path."""


def parse_aware(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            return None
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        return None
    return parsed


def civil_window_reasons(
    entry_at: Any,
    exit_at: Any,
    settlement_at: Any = None,
    due_at: Any = None,
    *,
    require_funding: bool = False,
) -> list[str]:
    """Frozen paper/testnet window. Naive timestamps are not assumed to be Berlin."""
    reasons: list[str] = []
    entry = parse_aware(entry_at)
    exit_at_dt = parse_aware(exit_at)
    if entry is None or exit_at_dt is None:
        return ["naive_or_missing_timestamp"]
    local_entry = entry.astimezone(_BERLIN)
    local_exit = exit_at_dt.astimezone(_BERLIN)
    if local_entry.date() != local_exit.date() or local_entry.weekday() != local_exit.weekday():
        reasons.append("overnight")
    if local_entry.weekday() >= 5:
        reasons.append("weekend_entry")
    entry_cutoff = local_entry.replace(hour=20, minute=0, second=0, microsecond=0)
    exit_cutoff = local_exit.replace(hour=20, minute=0, second=0, microsecond=0)
    if local_entry >= entry_cutoff:
        reasons.append("entry_after_cutoff")
    if local_exit > exit_cutoff:
        reasons.append("exit_after_cutoff")
    elapsed_hours = Decimal(str((exit_at_dt - entry).total_seconds())) / Decimal("3600")
    if elapsed_hours <= 0 or elapsed_hours > FROZEN_MAX_HOLDING_HOURS:
        reasons.append("holding_cap")
    if require_funding or settlement_at is not None or due_at is not None:
        settlement = parse_aware(settlement_at)
        due = parse_aware(due_at)
        if settlement is None or due is None:
            reasons.append("funding_timestamp_invalid")
        elif not (entry < settlement < exit_at_dt <= due):
            reasons.append("funding_window")
    return reasons


def funding_proof_ok(proof: Any) -> bool:
    if not isinstance(proof, dict):
        return False
    if proof.get("source") != "venue_account_cashflow":
        return False
    if proof.get("observed") != "actual_settlement":
        return False
    if proof.get("synthetic") is not False:
        return False
    if proof.get("kind") != "actual":
        return False
    proof_sha = proof.get("proof_sha")
    if not isinstance(proof_sha, str) or len(proof_sha) != 64:
        return False
    if any(character not in "0123456789abcdef" for character in proof_sha):
        return False
    return not civil_window_reasons(
        proof.get("entry_at"),
        proof.get("exit_at"),
        proof.get("settlement_at"),
        proof.get("due_at"),
        require_funding=True,
    )


def _walk_secrets(value: Any, reasons: list[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).strip().lower() in SECRET_KEY_NAMES:
                reasons.append("secret_key")
            _walk_secrets(item, reasons)
        return
    if isinstance(value, list):
        for item in value:
            _walk_secrets(item, reasons)
        return
    if isinstance(value, str):
        for marker in SECRET_VALUE_MARKERS:
            if marker in value:
                reasons.append("secret_value")
                return


def _limits_reasons(limits: Any) -> list[str]:
    if not isinstance(limits, dict):
        return ["limits_missing"]
    reasons: list[str] = []
    leverage = to_decimal(limits.get("leverage"))
    notional = to_decimal(limits.get("notional_pct_of_virtual_nav"))
    stop_risk = to_decimal(limits.get("stop_risk_pct"))
    aggregate = to_decimal(limits.get("aggregate_stop_risk_pct"))
    safety_cap = to_decimal(limits.get("safety_cap_pct"))
    min_qty = to_decimal(limits.get("exchange_min_qty"))
    if leverage != FROZEN_LEVERAGE:
        reasons.append("leverage")
    if notional is None or notional <= 0 or notional > FROZEN_NOTIONAL_PCT:
        reasons.append("notional")
    if stop_risk is None or stop_risk <= 0 or stop_risk > FROZEN_STOP_RISK_PCT:
        reasons.append("stop_risk")
    if aggregate is None or aggregate <= 0 or aggregate > FROZEN_AGGREGATE_STOP_RISK_PCT:
        reasons.append("aggregate_stop_risk")
    if safety_cap is None or safety_cap <= 0 or safety_cap > FROZEN_SAFETY_CAP_PCT:
        reasons.append("safety_cap")
    if min_qty is None or min_qty <= 0:
        reasons.append("min_qty_unknown")
    if limits.get("min_qty_fits") is not True:
        reasons.append("min_qty_fits_unknown")
    return reasons


def _window_reasons(window: Any) -> list[str]:
    if not isinstance(window, dict):
        return ["window_missing"]
    reasons: list[str] = []
    if window.get("timezone") != FROZEN_TIMEZONE:
        reasons.append("timezone")
    if window.get("cutoff") != FROZEN_CUTOFF:
        reasons.append("cutoff")
    if tuple(window.get("weekdays") or ()) != FROZEN_WEEKDAYS:
        reasons.append("weekdays")
    if to_decimal(window.get("max_holding_hours")) != FROZEN_MAX_HOLDING_HOURS:
        reasons.append("max_holding")
    if window.get("same_calendar_day") is not True:
        reasons.append("same_day")
    if "execution_buffer_minutes" not in window:
        reasons.append("buffer_missing")
    elif window.get("execution_buffer_minutes") is not None:
        reasons.append("buffer_not_frozen")
    if any(key in window for key in ("entry_at", "exit_at", "settlement_at", "due_at")):
        reasons.extend(
            civil_window_reasons(
                window.get("entry_at"),
                window.get("exit_at"),
                window.get("settlement_at"),
                window.get("due_at"),
                require_funding="settlement_at" in window or "due_at" in window,
            )
        )
    return reasons


def _sha_reasons(value: Any) -> list[str]:
    if not isinstance(value, str) or len(value) != 40:
        return ["sha_invalid"]
    if any(character not in "0123456789abcdef" for character in value):
        return ["sha_invalid"]
    if value == "0" * 40:
        return ["placeholder_sha"]
    return []


def validate_manifest(document: Any) -> dict[str, Any]:
    """Structural check. ``executable`` is false for every document."""
    reasons: list[str] = []
    if not isinstance(document, dict):
        reasons.append("manifest_missing")
        return _validation(False, reasons)
    if document.get("enabled") is not False:
        reasons.append("enable_refused")
    if document.get("execution_authorized") is not False:
        reasons.append("execution_authorization_refused")
    if document.get("global_kill_switch") not in (None, False):
        reasons.append("kill_switch_refused")
    tenant = document.get("tenant_id")
    if not isinstance(tenant, str) or not tenant.strip():
        reasons.append("tenant_missing")
    elif tenant.strip().casefold() in RESERVED_TENANTS:
        reasons.append("reserved_tenant")
    origin = document.get("origin")
    if origin != FROZEN_ORIGIN:
        reasons.append("origin_rejected")
    if isinstance(origin, str) and ("fx-api" in origin or origin.startswith("http://")):
        reasons.append("origin_rejected")
    actions = document.get("actions")
    if not isinstance(actions, list) or not actions:
        reasons.append("actions_missing")
    else:
        for action in actions:
            if action in FORBIDDEN_ACTIONS:
                reasons.append("forbidden_action")
            elif action not in ALLOWED_ACTIONS:
                reasons.append("action_unknown")
    reasons.extend(_limits_reasons(document.get("limits")))
    reasons.extend(_window_reasons(document.get("window")))
    reasons.extend(_sha_reasons(document.get("candidate_sha")))
    for field in ("keys_authorization", "config_authorization", "orders_authorization"):
        if document.get(field) != AUTHORIZATION_SENTINEL:
            reasons.append(f"{field}_missing")
    redaction = document.get("redaction")
    if not isinstance(redaction, dict):
        reasons.append("redaction_missing")
    else:
        for flag in REDACTION_FLAGS:
            if redaction.get(flag) is not True:
                reasons.append("redaction_incomplete")
    shorts = document.get("shorts")
    if isinstance(shorts, dict) and shorts.get("allow_live") is True:
        reasons.append("allow_live_refused")
    _walk_secrets(document, reasons)
    # A prepared document still cannot run: the buffer is unmeasured and this
    # package has no execution branch.
    unique = list(dict.fromkeys(reasons))
    return _validation(not unique, unique)


def _validation(complete: bool, reasons: list[str]) -> dict[str, Any]:
    return {
        "structurally_complete": complete,
        "executable": False,
        "window_executable": False,
        "venue_calls": 0,
        "reasons": reasons,
    }


def _refuse_unsafe_path(destination: Path) -> Path:
    path = Path(destination)
    if not path.is_absolute():
        path = Path.cwd() / path
    resolved = path.resolve()
    data_dir = (REPO_ROOT / "data").resolve()
    try:
        resolved.relative_to(data_dir)
    except ValueError:
        pass
    else:
        raise ManifestError("data_dir_write_refused")
    name = resolved.name
    if name == "config.json" or ".env" in name or name.endswith(".live.json"):
        raise ManifestError("refused_path")
    return resolved


def prepare_disabled_manifest(document: dict[str, Any], destination: Path) -> dict[str, Any]:
    """Write a disabled copy. Forces enabled and execution_authorized off."""
    if not isinstance(document, dict):
        raise ManifestError("manifest_missing")
    written = dict(document)
    written["enabled"] = False
    written["execution_authorized"] = False
    written["executable"] = False
    written["venue_calls"] = 0
    checked = validate_manifest(written)
    if "secret_key" in checked["reasons"] or "secret_value" in checked["reasons"]:
        raise ManifestError("secret_refused")
    target = _refuse_unsafe_path(Path(destination))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(written, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "written": str(target),
        "validation": checked,
        "executed": False,
        "venue_calls": 0,
        "authenticated_testnet_pass": False,
    }


def attempt_acceptance(manifest: Any = None, *, transport: Any = None) -> dict[str, Any]:
    """Refuse every call. ``transport`` is accepted so tests can prove it is unused."""
    del transport
    reason = (
        "disabled_no_operation_without_manifest"
        if manifest is None
        else "disabled_manifest_cannot_operate"
    )
    return {
        "executed": False,
        "venue_calls": 0,
        "tenant_enabled": False,
        "account_mode_changed": False,
        "ledger_reset": False,
        "deployed": False,
        "secrets_discovered": False,
        "authenticated_testnet_pass": False,
        "acceptance_executed_by_this_package": False,
        "reason": reason,
    }
