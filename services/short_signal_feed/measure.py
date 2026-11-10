"""Default-off measurement entry. Logs raw features and emits no order."""

from __future__ import annotations

from typing import Any

from tools.hypothesis_protocol import SLIPPAGE_PCT_PER_SIDE, TAKER_FEE_PCT_PER_SIDE

from services.short_signal_feed.features import raw_features
from services.short_signal_feed.reader import malformed_key, parse_timestamp, validate_identity
from services.short_signal_feed.schema import (
    HISTORICAL_CLIMAX_NO_GO,
    HOLDOUT_START,
    OPEN_DEFINITIONS,
    TRIAL_ID,
    dataset_sha256,
)
from services.short_signal_feed.trace import log_line, tape_row

_LIVE_MODES = frozenset({"live", "mainnet", "real"})
_SANDBOX_MODES = frozenset({"paper", "futures_testnet"})


def _shorts(config: dict | None) -> dict:
    if not isinstance(config, dict):
        return {}
    shorts = config.get("shorts")
    return shorts if isinstance(shorts, dict) else {}


def exhaustion_enabled(config: dict | None) -> bool:
    """True only for boolean true. climax_fade is not consulted."""
    block = _shorts(config).get("exhaustion_v220")
    return isinstance(block, dict) and block.get("enabled") is True


def _mode(config: dict | None) -> str | None:
    if not isinstance(config, dict):
        return None
    execution = config.get("execution")
    if not isinstance(execution, dict):
        return None
    mode = execution.get("mode")
    if not isinstance(mode, str):
        return None
    stripped = mode.strip().lower()
    return stripped or None


def path_reason(config: dict | None) -> str:
    if not exhaustion_enabled(config):
        return "feature_off"
    if _shorts(config).get("allow_live") is True:
        return "live_denied"
    mode = _mode(config)
    if mode in _LIVE_MODES:
        return "live_denied"
    if mode not in _SANDBOX_MODES:
        return "mode_not_sandbox"
    return "measured"


def _shell(
    *,
    signal_key: str,
    symbol: str | None,
    as_of: str | None,
    status_reason: str,
    detail: str | None,
    inputs: dict | None = None,
) -> dict:
    return {
        "signal_key": signal_key,
        "symbol": symbol,
        "as_of": as_of,
        "status": "not_measurable",
        "status_reason": status_reason,
        "detail": detail,
        "decision": None,
        "order_intent": None,
        "thresholds_applied": False,
        "grace_applied": False,
        "features": None,
        "inputs": inputs
        or {
            "instrument_kind": None,
            "listed_perp": None,
            "calendar_known": None,
            "depth_known": None,
        },
        "missing_reasons": [status_reason],
    }


def _observe(record: object, bars: list, funding: list, now_dt) -> dict:
    identity, reason, detail = validate_identity(record)
    if identity is None:
        return _shell(
            signal_key=malformed_key(record),
            symbol=None,
            as_of=None,
            status_reason=reason or "malformed",
            detail=detail,
        )
    if reason is not None:
        return _shell(
            signal_key=identity["signal_key"],
            symbol=identity["symbol"],
            as_of=identity["as_of_text"],
            status_reason=reason,
            detail=detail,
        )
    as_of = identity["as_of"]
    if as_of >= HOLDOUT_START:
        return _shell(
            signal_key=identity["signal_key"],
            symbol=identity["symbol"],
            as_of=identity["as_of_text"],
            status_reason="holdout_excluded",
            detail=None,
        )
    if now_dt is not None and as_of > now_dt:
        return _shell(
            signal_key=identity["signal_key"],
            symbol=identity["symbol"],
            as_of=identity["as_of_text"],
            status_reason="future_as_of",
            detail=None,
        )
    measured = raw_features(identity, bars, funding, now_dt)
    return {
        "signal_key": identity["signal_key"],
        "symbol": identity["symbol"],
        "as_of": identity["as_of_text"],
        "status": "observed",
        "status_reason": None,
        "detail": None,
        "decision": None,
        "order_intent": None,
        "thresholds_applied": False,
        "grace_applied": False,
        "features": measured["features"],
        "inputs": measured["inputs"],
        "missing_reasons": measured["missing_reasons"],
    }


def _missingness(observations: list[dict]) -> dict[str, int]:
    unknown_funding = 0
    unknown_rsi = 0
    no_ratio = 0
    for row in observations:
        features = row.get("features") or {}
        funding = features.get("funding") or {}
        rsi = features.get("rsi") or {}
        ratios = (features.get("volume") or {}).get("consecutive_ratios") or []
        if row.get("status") != "observed" or funding.get("last") is None:
            unknown_funding += 1
        if row.get("status") != "observed" or rsi.get("value") is None:
            unknown_rsi += 1
        if row.get("status") != "observed" or not ratios:
            no_ratio += 1
    return {
        "observations_with_unknown_funding": unknown_funding,
        "observations_with_unknown_rsi": unknown_rsi,
        "observations_with_no_volume_ratio": no_ratio,
    }


def _report(
    *,
    digest: str,
    observations: list[dict],
    ran: bool,
) -> dict[str, Any]:
    observed = [row for row in observations if row.get("status") == "observed"]
    return {
        "trial_id": TRIAL_ID,
        "status": "insufficient_data",
        "reason": "definitions_pending_owner_freeze",
        "pending_owner_freeze": True,
        "open_definitions": dict(OPEN_DEFINITIONS),
        "dataset_sha256": digest,
        "pre_feature_hash": True,
        "holdout": "not_accessed",
        "cost_protocol": {
            "taker_fee_pct_per_side": TAKER_FEE_PCT_PER_SIDE,
            "slippage_pct_per_side": dict(SLIPPAGE_PCT_PER_SIDE),
            "applied_to_pnl": False,
            "spread": "unknown",
            "funding_net_cost": "unknown",
        },
        "net_expectancy": None,
        "profit_factor": None,
        "max_drawdown": None,
        "mfe": None,
        "mae": None,
        "historical_climax_nogo": dict(HISTORICAL_CLIMAX_NO_GO),
        "profitability_claim": False,
        "baselines": {
            "old_climax": {
                "status": "not_run",
                "reason": "historical_no_go_not_recomputed",
            },
            "unconditional_top_gainer_fade": {
                "status": "not_run",
                "reason": "thresholds_unclear",
            },
            "no_trade": {"status": "not_run", "reason": "no_orders_emitted"},
        },
        "n_observations": len(observations),
        "n_observed": len(observed),
        "n_not_measurable": len(observations) - len(observed),
        "n_orders": 0,
        "uncertainty": "not_estimated",
        "missingness": _missingness(observations) if ran else None,
        "trial_register": {
            "id": TRIAL_ID,
            "issue": 769,
            "protocol": "tools.hypothesis_protocol",
            "scenario_trials_py": "not_modified",
            "n_effect": "none",
            "status": "planned_not_run",
            "reason": "definitions_pending_owner_freeze",
        },
    }


def measure_exhaustion(
    *,
    config: dict | None,
    signals: list | None,
    bars: list | None = None,
    funding: list | None = None,
    now: str | None = None,
    tenant: str = "fixture",
) -> dict[str, Any]:
    """Log raw features for a sandbox fixture. Never returns an order intent."""
    signal_rows = list(signals or [])
    bar_rows = list(bars or [])
    funding_rows = list(funding or [])
    digest = dataset_sha256(signal_rows, bar_rows, funding_rows, now)
    reason = path_reason(config)
    if reason != "measured":
        return {
            "reason": reason,
            "trading_effect": "none",
            "order_intents": [],
            "venue_calls": 0,
            "observations": [],
            "log_lines": [],
            "duplicate_dropped": 0,
            "tape_rows": [],
            "report": _report(digest=digest, observations=[], ran=False),
        }

    now_dt = parse_timestamp(now) if now is not None else None
    seen: set[str] = set()
    observations: list[dict] = []
    dropped = 0
    for record in signal_rows:
        observation = _observe(record, bar_rows, funding_rows, now_dt)
        key = observation["signal_key"]
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        observations.append(observation)
    return {
        "reason": "measured",
        "trading_effect": "none",
        "order_intents": [],
        "venue_calls": 0,
        "observations": observations,
        "log_lines": [log_line(row) for row in observations],
        "duplicate_dropped": dropped,
        "tape_rows": [tape_row(row, tenant=tenant) for row in observations],
        "report": _report(digest=digest, observations=observations, ran=True),
    }
