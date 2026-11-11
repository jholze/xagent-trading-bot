"""Frozen paper/testnet start values for the D-E2E acceptance judge.

The 2026-11-10 18:40 Europe/Berlin freeze is the source of these numbers.
Items that stayed open (time buffer, permit minutes/amounts, exhaustion
thresholds) have no numeric stand-in. This module does not place orders.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

SCHEMA_VERSION = "d-e2e-evidence-1"
DRAFT_ID = "D-E2E"
AC_REVISION = "rev-1-2026-11-10"
ISSUE_412_STATE = "closed_not_planned"

FROZEN_AT = "2026-11-10T18:40+01:00"
FROZEN_CLEARANCE = Decimal("0.05")
FROZEN_TOLERANCE = Decimal("0.01")
FROZEN_MAX_HOLDING_HOURS = Decimal("4")
FROZEN_ABSOLUTE_HOLDING_HOURS = Decimal("24")
FROZEN_ORIGIN = "https://api-testnet.gateapi.io:443"
LEGACY_TESTNET_ORIGIN = "https://fx-api-testnet.gateio.ws:443"
FROZEN_CUTOFF = "20:00:00"
FROZEN_TIMEZONE = "Europe/Berlin"
FROZEN_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri")
FROZEN_LEVERAGE = Decimal("1")
FROZEN_NOTIONAL_PCT = Decimal("10")
FROZEN_STOP_RISK_PCT = Decimal("0.25")
FROZEN_AGGREGATE_STOP_RISK_PCT = Decimal("0.75")
FROZEN_SAFETY_CAP_PCT = Decimal("20")

RELEASE_FLAG_KEY = "short_release_acceptance"
CODER_MODEL = "grok-4.7"
REQUIRED_REVIEWERS = ("deepseek-v4-pro", "claude-opus-5-5")
# Exact producer ids that may classify as actual acceptance. No trimming.
ACCEPTED_PRODUCERS = frozenset({"separate_authorized_run"})
ISSUE_412_ACTIONS = frozenset({"leave_closed_not_planned"})
ALLOWED_ASSET_CLASSES = frozenset({"perp"})
ALLOWED_MANIFEST_MODES = frozenset({"paper", "testnet"})
REQUIRED_AC_IDS = ("AC-E2E-1", "AC-E2E-2", "AC-E2E-3", "AC-E2E-4")
VERDICT_NOT_VERIFIED = "NOT_VERIFIED"
VERDICT_NO_GO = "NO-GO"
VERDICT_ACCEPTED = "ACCEPTED"
EVIDENCE_KINDS = ("fixture", "manual", "actual")
FIXTURE_MODES = frozenset({"paper", "futures_testnet", "testnet"})
RESERVED_TENANTS = frozenset({"default", "henry", "all", "*"})
ALLOWED_ACTIONS = frozenset({"SHORT", "protective_stop", "COVER"})
FORBIDDEN_ACTIONS = frozenset(
    {
        "ADL",
        "adl_experiment",
        "liquidation",
        "liquidation_experiment",
        "flatten",
        "deploy",
        "reset_ledger",
        "set_account_mode",
        "flip_tenant",
        "kill_switch",
    }
)
OPEN_PARAMETERS = (
    "execution_buffer_minutes",
    "permit_minutes",
    "permit_amount",
    "exhaustion_thresholds",
)

# Cited from strategies/positions.py ``_ENTRY_SNAPSHOT_KEYS`` (#600).
# A test asserts equality. Do not add keys here.
ENTRY_SNAPSHOT_KEYS = (
    "captured_at",
    "fill_price",
    "fee",
    "source",
    "timeframe",
    "strategy_profile",
    "strategy_tier",
    "rationale_codes",
    "rsi",
    "volume_factor",
    "atr",
    "volatility_tier",
    "size_before_mult",
    "size_after_mult",
    "cap_applied",
)

REQUIRED_STAGES = (
    "scanner",
    "decision",
    "short_intent",
    "fill",
    "protection",
    "cover",
    "ledger",
    "trace",
)

OWNER_BY_STAGE = {
    "scanner": "D-EXH",
    "decision": "existing-decision-risk",
    "short_intent": "T220-01",
    "fill": "676",
    "protection": "T220-02",
    "cover": "T220-05",
    "ledger": "T220-04",
    "trace": "D-LIN",
}

# Module names are documentation only. This package must not import them.
OWNER_MODULES = {
    "D-EXH": "services.short_signal_feed",
    "T220-01": "execution.futures_testnet_route",
    "676": "execution.partial_fill_delta",
    "T220-02": "execution.short_protection",
    "FREEZE": "execution.paper_testnet_freeze",
    "T220-03": "strategies.short_admission",
    "T220-05": "strategies.short_cover",
    "T220-04": "services.portfolio_service",
    "D-LIN": "strategies.positions",
    "existing-decision-risk": "risk.risk_manager",
}

FAULT_OWNERS = {
    "disabled_flag": "D-E2E",
    "missing_flag": "D-E2E",
    "live_mode": "D-E2E",
    "wrong_tenant": "T220-01",
    "stale_source": "D-EXH",
    "unknown_cost": "T220-04",
    "redis_timeout": "765",
    "malformed_mode": "T220-01",
    "uncertain_create": "T220-02",
    "restart_orphan": "T220-02",
    "stop_rejection": "T220-02",
    "stop_cover_race": "T220-02",
}

CANONICAL_AC_TEXT = """\
AC-E2E-1: Given scanner candidate fixture, enabled paper/testnet fixture tenant and valid mode proof, run normal risk→SHORT→confirmed partial/full fill→protective stop→time-cap COVER→ledger/MCP trace. Refer to owner contracts, assert causal IDs and net arithmetic, no external network or data/ writes.
AC-E2E-2: Given disabled flag/live/wrong tenant/stale data/Redis timeout/unknown fee/stop reject/uncertain create/restart/orphan/partial cover race, assert contract-owned failure result, no duplicate/reversal or unauthorized emergency flatten; regular authorized reduce-only management remains possible.
AC-E2E-3: Given evidence rows classified fixture/manual/actual with candidate SHA, AC revision/hash, model and test status, any stale SHA, missing real funding proof or missing required review yields NOT VERIFIED/NO-GO. Passing offline harness cannot output authenticated success.
AC-E2E-4: Given no explicit operation manifest, acceptance command cannot contact an exchange or enable a tenant. Prepare only a disabled manual runbook/manifest for a future exact tenant/origin/actions/budget/time/SHA authorization and evidence-redaction checklist; no automatic authentication/credential discovery.
"""

# Applied to a key after lowercasing and removing "-" and "_".
SECRET_KEY_PATTERN = r"api.?key|secret|private.?key|token|bearer|passphrase|password"
REDACTION_FLAGS = (
    "secrets_redacted",
    "account_ids_redacted",
    "raw_venue_payloads_redacted",
    "no_credentials_in_evidence",
)
AUTHORIZATION_SENTINEL = "separately_authorized_not_in_repo"


def to_decimal(value: Any) -> Decimal | None:
    """Parse a finite decimal. Floats are rejected so binary noise cannot widen T."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, int):
        number = Decimal(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text or text.lower() in {"unknown", "nan", "inf", "+inf", "-inf"}:
            return None
        try:
            number = Decimal(text)
        except InvalidOperation:
            return None
    else:
        return None
    if not number.is_finite():
        return None
    return number
