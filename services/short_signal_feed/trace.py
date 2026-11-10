"""Project a feature row onto the existing #628 tape keys. Does not write."""

from __future__ import annotations

from services.short_signal_feed.schema import TRIAL_ID
from strategies.buy_decision_tape import ROW_KEYS


def tape_row(
    observation: dict,
    *,
    tenant: str,
    entry_snapshot_ref: str | None = None,
    entry_snapshot: dict | None = None,
) -> dict:
    """One row shaped like ``ROW_KEYS``. The snapshot body is not copied."""
    rejected: list[str] = []
    if isinstance(entry_snapshot, dict):
        from strategies.positions import _ENTRY_SNAPSHOT_KEYS

        allowed = set(_ENTRY_SNAPSHOT_KEYS)
        rejected = [key for key in entry_snapshot if key not in allowed]
    eligibility = {
        "features": observation.get("features"),
        "missing_reasons": list(observation.get("missing_reasons") or []),
        "status_reason": observation.get("status_reason"),
        "entry_snapshot_ref": entry_snapshot_ref,
        "entry_snapshot_rejected_keys": rejected,
    }
    row = {
        "ts": observation.get("as_of") or "unknown",
        "symbol": observation.get("symbol") or "",
        "tenant": tenant,
        "outcome": observation.get("status") or "not_measurable",
        "signal": {
            "name": TRIAL_ID,
            "source": observation.get("signal_key"),
        },
        "filter_codes": [],
        "book": {
            "thresholds_applied": False,
            "grace_applied": False,
            "order_intent": None,
            "decision": None,
        },
        "macro_stress": None,
        "eligibility": eligibility,
        "correlation_id": observation.get("signal_key"),
    }
    return {key: row[key] for key in ROW_KEYS}


def log_line(observation: dict) -> str:
    features = observation.get("features") or {}
    rsi = (features.get("rsi") or {}).get("value")
    last = (features.get("funding") or {}).get("last") or {}
    ratios = (features.get("volume") or {}).get("consecutive_ratios") or []
    age = (features.get("age") or {}).get("seconds")
    rate = last.get("rate") if last else None
    return (
        f"exhaustion_features symbol={observation.get('symbol') or '-'} "
        f"key={observation.get('signal_key')} status={observation.get('status')} "
        f"rsi={rsi if rsi is not None else 'unknown'} "
        f"funding_last={rate if rate is not None else 'unknown'} "
        f"volume_ratios={len(ratios)} "
        f"age_seconds={age if age is not None else 'unknown'} "
        f"gate=none order_intent=none"
    )
