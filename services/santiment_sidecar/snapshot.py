"""Build versioned Santiment snapshots for the bot."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from services.santiment_sidecar.regime import RegimeDecision, decide_regime


def build_snapshot(
    features: dict[str, float],
    *,
    schema_version: int = 1,
    ttl_sec: int = 1800,
    build: str = "",
    decision: RegimeDecision | None = None,
) -> dict[str, Any]:
    dec = decision or decide_regime(features)
    return {
        "schema_version": schema_version,
        "source": "santiment",
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ttl_sec": int(ttl_sec),
        "regime": dec.regime,
        "confidence": round(float(dec.confidence), 4),
        "size_mult": round(float(dec.size_mult), 4),
        "sensor_policy": dec.sensor_policy,
        "max_new_entries_per_hour": int(dec.max_new_entries_per_hour),
        "features": {k: round(float(v), 6) for k, v in sorted(features.items())},
        "symbols": {},
        "rationale": dec.rationale,
        "sidecar_build": build or "santiment-sidecar-0.1",
    }
