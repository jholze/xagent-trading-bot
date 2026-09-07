"""Build MarketSnapshot v1 for bot ingest (compatible with santiment fields)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from services.market_oracle.regime import OracleDecision


def build_snapshot(
    features: dict[str, float],
    decision: OracleDecision,
    *,
    schema_version: int = 1,
    ttl_sec: int = 900,
    previous_state: str | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state = decision.state
    return {
        "schema_version": schema_version,
        "source": "market_oracle",
        "as_of": now,
        "ttl_sec": int(ttl_sec),
        # Dual keys: oracle plan uses state; bot fusion also accepts regime
        "state": state,
        "regime": state,
        "previous_state": previous_state,
        "confidence": round(float(decision.confidence), 4),
        "size_mult": round(float(decision.size_mult), 4),
        "sensor_policy": decision.sensor_policy,
        "block_new_entries": bool(decision.block_new_entries),
        "block_sensor_entries": bool(decision.block_sensor_entries),
        "max_new_buys_per_hour": int(decision.max_new_buys_per_hour),
        "max_new_entries_per_hour": int(decision.max_new_buys_per_hour),
        "policy": {
            "size_mult": round(float(decision.size_mult), 4),
            "block_new_entries": bool(decision.block_new_entries),
            "block_sensor_entries": bool(decision.block_sensor_entries),
            "sensor_mode": decision.sensor_policy,
            "max_new_buys_per_hour": int(decision.max_new_buys_per_hour),
        },
        "features": {k: round(float(v), 6) for k, v in sorted(features.items())},
        "hysteresis": {
            "bars_in_state": int(decision.bars_in_state),
        },
        "rationale": decision.rationale,
        "sidecar_build": "market-oracle-0.1",
    }
