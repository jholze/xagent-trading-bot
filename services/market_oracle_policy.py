"""Bot-side read of market oracle snapshot (+ process warm-up)."""

from __future__ import annotations

from typing import Any

from services.market_oracle_store import (
    get_latest_snapshot,
    process_uptime_sec,
    snapshot_is_fresh,
)


def _arch(config_raw: dict | None) -> dict:
    if config_raw is None:
        try:
            from core.config import get_bot_config

            config_raw = get_bot_config().raw
        except Exception:
            config_raw = {}
    raw = config_raw or {}
    arch = dict(raw.get("architecture") or {})
    # also allow top-level market_context block
    mc = dict(raw.get("market_context") or {})
    arch.update({k: v for k, v in mc.items() if k not in arch})
    return arch


def market_oracle_risk_config(config_raw: dict | None = None) -> dict:
    arch = _arch(config_raw)
    return {
        "enabled": bool(arch.get("market_oracle_risk_enabled", True)),
        "fail_open": bool(arch.get("market_oracle_risk_fail_open", True)),
        "apply_size_mult": bool(arch.get("market_oracle_apply_size_mult", True)),
        "apply_sensor_policy": bool(arch.get("market_oracle_apply_sensor_policy", True)),
        "block_buys_on_crash": bool(arch.get("market_oracle_block_buys_on_crash", True)),
        "warmup_sec": int(arch.get("warmup_sec") or arch.get("market_oracle_warmup_sec") or 0),
        "warmup_max_new_buys": int(
            arch.get("warmup_max_new_buys") or arch.get("market_oracle_warmup_max_new_buys") or 0
        ),
    }


def get_market_oracle_policy(config_raw: dict | None = None) -> dict[str, Any]:
    cfg = market_oracle_risk_config(config_raw)
    neutral = {
        "active": False,
        "fresh": False,
        "regime": None,
        "state": None,
        "size_mult": 1.0,
        "sensor_policy": "active",
        "block_buys": False,
        "apply_size_mult": False,
        "apply_sensor_policy": False,
        "warmup_active": False,
        "rationale": "",
    }
    if not cfg["enabled"]:
        return neutral

    # Process warm-up: block new entries after deploy
    uptime = process_uptime_sec()
    warmup_active = cfg["warmup_sec"] > 0 and uptime < cfg["warmup_sec"]

    snap = get_latest_snapshot()
    fresh = snapshot_is_fresh(snap)
    if not snap or not fresh:
        if warmup_active:
            return {
                **neutral,
                "active": True,
                "regime": "WARMUP",
                "state": "WARMUP",
                "size_mult": 0.0 if cfg["warmup_max_new_buys"] <= 0 else 0.25,
                "sensor_policy": "block" if cfg["warmup_max_new_buys"] <= 0 else "shadow",
                "block_buys": cfg["warmup_max_new_buys"] <= 0,
                "apply_size_mult": True,
                "apply_sensor_policy": True,
                "warmup_active": True,
                "rationale": f"process warm-up {uptime:.0f}/{cfg['warmup_sec']}s",
            }
        if cfg["fail_open"]:
            return {**neutral, "rationale": "oracle missing/stale fail-open"}
        return {
            **neutral,
            "active": True,
            "regime": "RISK_OFF",
            "state": "RISK_OFF",
            "size_mult": 0.35,
            "sensor_policy": "shadow",
            "apply_size_mult": cfg["apply_size_mult"],
            "apply_sensor_policy": cfg["apply_sensor_policy"],
            "rationale": "oracle missing/stale fail-closed",
        }

    state = str(snap.get("state") or snap.get("regime") or "NEUTRAL").upper()
    try:
        size_mult = float(snap.get("size_mult") if snap.get("size_mult") is not None else 1.0)
    except Exception:
        size_mult = 1.0
    size_mult = max(0.0, min(1.5, size_mult))
    sensor = str(snap.get("sensor_policy") or (snap.get("policy") or {}).get("sensor_mode") or "active")
    sensor = sensor.lower()
    if sensor not in ("active", "shadow", "block"):
        sensor = "active"

    block_buys = bool(
        cfg["block_buys_on_crash"]
        and (
            state == "CRASH"
            or size_mult <= 0
            or sensor == "block"
            or bool(snap.get("block_new_entries"))
        )
    )
    if warmup_active and cfg["warmup_max_new_buys"] <= 0:
        block_buys = True
        sensor = "block" if sensor == "active" else sensor
        size_mult = min(size_mult, 0.0)

    return {
        "active": True,
        "fresh": True,
        "regime": state,
        "state": state,
        "size_mult": size_mult,
        "sensor_policy": sensor,
        "block_buys": block_buys,
        "apply_size_mult": cfg["apply_size_mult"],
        "apply_sensor_policy": cfg["apply_sensor_policy"],
        "warmup_active": warmup_active,
        "rationale": str(snap.get("rationale") or ""),
        "as_of": snap.get("as_of"),
        "features": snap.get("features") or {},
    }
