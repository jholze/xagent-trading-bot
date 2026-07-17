"""Read Santiment sidecar snapshot and expose bot risk/sensor policy.

Mode/grid fusion lives in ``market_policy_fusion`` (avoids DEFENSIVE churn).
"""

from __future__ import annotations

from typing import Any

from services.santiment_store import get_latest_snapshot, snapshot_is_fresh


def _arch(config_raw: dict | None) -> dict:
    if config_raw is None:
        try:
            from core.config import get_bot_config

            config_raw = get_bot_config().raw
        except Exception:
            config_raw = {}
    return (config_raw or {}).get("architecture") or {}


def santiment_risk_config(config_raw: dict | None = None) -> dict:
    arch = _arch(config_raw)
    return {
        "enabled": bool(arch.get("santiment_risk_enabled", True)),
        "fail_open": bool(arch.get("santiment_risk_fail_open", True)),
        "apply_size_mult": bool(arch.get("santiment_apply_size_mult", True)),
        "apply_sensor_policy": bool(arch.get("santiment_apply_sensor_policy", True)),
        "block_buys_on_crash": bool(arch.get("santiment_block_buys_on_crash", True)),
        "apply_mode_bias": bool(arch.get("santiment_apply_mode_bias", True)),
        "apply_grid_spacing": bool(arch.get("santiment_apply_grid_spacing", True)),
        "inject_regime_sentiment": bool(arch.get("santiment_inject_regime_sentiment", True)),
    }


def get_santiment_policy(config_raw: dict | None = None) -> dict[str, Any]:
    """Effective policy for RiskManager / entry sensor.

    When disabled, missing, or stale + fail_open: no effect (size_mult=1, sensor active).
    """
    cfg = santiment_risk_config(config_raw)
    neutral = {
        "active": False,
        "fresh": False,
        "regime": None,
        "size_mult": 1.0,
        "sensor_policy": "active",
        "max_new_entries_per_hour": None,
        "rationale": "",
        "apply_size_mult": False,
        "apply_sensor_policy": False,
        "block_buys": False,
    }
    if not cfg["enabled"]:
        return neutral

    snap = get_latest_snapshot()
    fresh = snapshot_is_fresh(snap)
    if not snap or not fresh:
        if cfg["fail_open"]:
            return {**neutral, "fresh": False, "rationale": "santiment missing/stale fail-open"}
        # fail closed: treat as risk-off
        return {
            "active": True,
            "fresh": False,
            "regime": "RISK_OFF",
            "size_mult": 0.35,
            "sensor_policy": "shadow",
            "max_new_entries_per_hour": 2,
            "rationale": "santiment missing/stale fail-closed",
            "apply_size_mult": cfg["apply_size_mult"],
            "apply_sensor_policy": cfg["apply_sensor_policy"],
            "block_buys": False,
        }

    regime = str(snap.get("regime") or "NEUTRAL").upper()
    try:
        size_mult = float(snap.get("size_mult") if snap.get("size_mult") is not None else 1.0)
    except Exception:
        size_mult = 1.0
    size_mult = max(0.0, min(1.5, size_mult))
    sensor = str(snap.get("sensor_policy") or "active").lower()
    if sensor not in ("active", "shadow", "block"):
        sensor = "active"

    block_buys = bool(
        cfg["block_buys_on_crash"]
        and (regime == "CRASH" or size_mult <= 0 or sensor == "block")
    )

    return {
        "active": True,
        "fresh": True,
        "regime": regime,
        "size_mult": size_mult,
        "sensor_policy": sensor,
        "max_new_entries_per_hour": snap.get("max_new_entries_per_hour"),
        "rationale": str(snap.get("rationale") or ""),
        "as_of": snap.get("as_of"),
        "apply_size_mult": cfg["apply_size_mult"],
        "apply_sensor_policy": cfg["apply_sensor_policy"],
        "apply_mode_bias": cfg["apply_mode_bias"],
        "apply_grid_spacing": cfg["apply_grid_spacing"],
        "inject_regime_sentiment": cfg["inject_regime_sentiment"],
        "block_buys": block_buys,
    }
