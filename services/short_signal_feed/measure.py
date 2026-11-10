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

_LEGACY_LIVE_MODES = frozenset({"live", "mainnet", "real"})
_SANDBOX_TRADING_MODES = frozenset({"paper", "demo", "gate_testnet"})
_SANDBOX_LIVE_EXECUTION = frozenset({"shadow", "testnet"})
SPEC_STATUS = (
    "frozen 2026-11-10 by Kay (owner-delegated), scope raw-feature logging only; "
    "thresholds/lookback/divergence/grace remain open until measured"
)


def _shorts(config: dict | None) -> dict:
    if not isinstance(config, dict):
        return {}
    shorts = config.get("shorts")
    return shorts if isinstance(shorts, dict) else {}


def exhaustion_enabled(config: dict | None) -> bool:
    """True only for boolean true. climax_fade is not consulted."""
    block = _shorts(config).get("exhaustion_v220")
    return isinstance(block, dict) and block.get("enabled") is True


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip().lower()
    return stripped or None


def _trading_mode(config: dict | None) -> str | None:
    if not isinstance(config, dict):
        return None
    return _text(config.get("trading_mode"))


def _live_execution(config: dict | None) -> str | None:
    if not isinstance(config, dict):
        return None
    live = config.get("live")
    if not isinstance(live, dict) or "execution" not in live:
        return None
    return _text(live.get("execution"))


def _legacy_execution_mode(config: dict | None) -> str | None:
    """``execution.mode`` is not the repo key. A live value still fails closed."""
    if not isinstance(config, dict):
        return None
    execution = config.get("execution")
    if not isinstance(execution, dict):
        return None
    return _text(execution.get("mode"))


def path_reason(config: dict | None) -> str:
    """Sandbox only. Any live indicator denies, including a paper execution.mode."""
    if not exhaustion_enabled(config):
        return "feature_off"
    if _shorts(config).get("allow_live") is True:
        return "live_denied"
    trading_mode = _trading_mode(config)
    live_execution = _live_execution(config)
    legacy = _legacy_execution_mode(config)
    if trading_mode == "live" or live_execution == "real" or legacy in _LEGACY_LIVE_MODES:
        return "live_denied"
    if live_execution is not None and live_execution not in _SANDBOX_LIVE_EXECUTION:
        return "live_denied"
    if trading_mode not in _SANDBOX_TRADING_MODES:
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
    status: str = "not_measurable",
    holdout: bool = False,
) -> dict:
    return {
        "signal_key": signal_key,
        "symbol": symbol,
        "as_of": as_of,
        "status": status,
        "status_reason": status_reason,
        "detail": detail,
        "decision": None,
        "order_intent": None,
        "thresholds_applied": False,
        "grace_applied": False,
        "holdout": holdout,
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
    holdout = as_of >= HOLDOUT_START
    if now_dt is not None and as_of > now_dt:
        return _shell(
            signal_key=identity["signal_key"],
            symbol=identity["symbol"],
            as_of=identity["as_of_text"],
            status_reason="future_as_of",
            detail=None,
            holdout=holdout,
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
        "holdout": holdout,
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
        "reason": "definitions_open",
        "pending_owner_freeze": False,
        "spec_status": SPEC_STATUS,
        "open_definitions": dict(OPEN_DEFINITIONS),
        "dataset_sha256": digest,
        "pre_feature_hash": True,
        "holdout": "logged_not_for_fitting",
        "holdout_rows": sum(1 for row in observations if row.get("holdout") is True),
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
            "reason": "definitions_open",
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

    now_invalid = now is not None and parse_timestamp(now) is None
    now_dt = None if now is None or now_invalid else parse_timestamp(now)
    seen: set[str] = set()
    observations: list[dict] = []
    dropped = 0
    for record in signal_rows:
        if now_invalid:
            identity, _reason, _detail = validate_identity(record)
            if identity is None:
                observation = _shell(
                    signal_key=malformed_key(record),
                    symbol=None,
                    as_of=None,
                    status="invalid",
                    status_reason="malformed_now",
                    detail="now",
                )
            else:
                observation = _shell(
                    signal_key=identity["signal_key"],
                    symbol=identity["symbol"],
                    as_of=identity["as_of_text"],
                    status="invalid",
                    status_reason="malformed_now",
                    detail="now",
                    holdout=identity["as_of"] >= HOLDOUT_START,
                )
        else:
            observation = _observe(record, bar_rows, funding_rows, now_dt)
        key = observation["signal_key"]
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        observations.append(observation)
    return {
        "reason": "malformed_now" if now_invalid else "measured",
        "trading_effect": "none",
        "order_intents": [],
        "venue_calls": 0,
        "observations": observations,
        "log_lines": [log_line(row) for row in observations],
        "duplicate_dropped": dropped,
        "tape_rows": [tape_row(row, tenant=tenant) for row in observations],
        "report": _report(digest=digest, observations=observations, ran=True),
    }
