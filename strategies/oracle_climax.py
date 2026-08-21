"""Two-stage Oracle RISK_ON overlay: hold runners in the grind, tighten/harvest at climax.

Idle: overlay off, oracle not RISK_ON, or Fusion RISK_OFF/CRASH (18 Aug false alarm).
Grind: Oracle RISK_ON, not yet climax — suppress BB/TTP full-close so runners can extend.
Tighten: climax armed (extension + hot breadth) and 1h stall — tighter TTP trail.
Harvest: climax armed and 1h rolling over (or 15m dump if the feature exists) —
         SELL_FULL on lots with gain ≥ harvest_min.

Fusion NEUTRAL is allowed (19–20 Aug tape). Disk default enabled=false.
Enable per tenant overlay (Henry first). Kill: sell_policy.oracle_climax.enabled=false.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.actions import SELL_FULL

MODE_IDLE = "idle"
MODE_GRIND = "grind"
MODE_TIGHTEN = "tighten"
MODE_HARVEST = "harvest"

GRIND_BLOCK_SOURCES = frozenset(
    {
        "bb_upper",
        "trailing_take_profit",
        "trailing_stop",
        "profit_max_lifetime",
        "time_profit_exit",
        "exit_volume_climax",
        "vol_exhaustion",
    }
)

HARVEST_SOURCE = "oracle_climax_harvest"
HARVEST_PRIORITY = 8

_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "min_btc_24h_pct": 6.0,
    "min_eth_24h_pct": 10.0,
    "min_breadth_green": 0.70,
    "min_btc_4h_pct": 1.0,
    "min_trend_4h": 0.0,
    "stall_1h_max_pct": 0.40,
    "stall_1h_min_pct": -0.80,
    "harvest_1h_max_pct": -0.30,
    "dump_15m_max_pct": -0.80,
    "harvest_min_gain_pct": 12.0,
    "harvest_priority": HARVEST_PRIORITY,
    "tighten_trail_pct": 1.5,
}

# Per-tenant-cycle cache so DE + TTP + WS eval share one resolve.
_cycle: tuple["ClimaxDecision", dict] | None = None


@dataclass(frozen=True)
class ClimaxDecision:
    mode: str
    reasons: tuple[str, ...]
    features: dict[str, float]


def oracle_climax_config(config_raw: dict | None) -> dict:
    root = dict((config_raw or {}).get("sell_policy") or {})
    block = dict(root.get("oracle_climax") or {})
    return {**_DEFAULTS, **block}


def _f(features: dict, *keys: str, default: float | None = None) -> float | None:
    for k in keys:
        if k in features and features[k] is not None:
            try:
                return float(features[k])
            except (TypeError, ValueError):
                continue
    return default


def _oracle_state(snap: dict | None) -> str:
    if not snap:
        return ""
    return str(snap.get("state") or snap.get("regime") or "").upper()


def _fusion_blocked(fusion_regime: str | None) -> bool:
    r = str(fusion_regime or "").upper()
    return r in {"RISK_OFF", "CRASH"}


def climax_armed(features: dict, cfg: dict) -> tuple[bool, list[str]]:
    """Extension + breadth + 4h still bid. Does not include 1h stall (that's tighten/harvest)."""
    why: list[str] = []
    btc_24 = _f(features, "btc_ret_24h_pct")
    eth_24 = _f(features, "eth_ret_24h_pct")
    ext = False
    if btc_24 is not None and btc_24 >= float(cfg["min_btc_24h_pct"]):
        ext = True
    if eth_24 is not None and eth_24 >= float(cfg["min_eth_24h_pct"]):
        ext = True
    if not ext:
        why.append("no_extension")
    breadth = _f(features, "breadth_pct_green")
    if breadth is None or breadth < float(cfg["min_breadth_green"]):
        why.append("breadth_cold")
    btc_4h = _f(features, "btc_ret_4h_pct")
    if btc_4h is not None and btc_4h < float(cfg["min_btc_4h_pct"]):
        why.append("btc_4h_soft")
    trend = _f(features, "btc_trend_4h")
    if trend is not None and trend < float(cfg["min_trend_4h"]):
        why.append("trend_4h_down")
    return (not why), why


def evaluate_climax_mode(
    *,
    oracle_snap: dict | None,
    fusion_regime: str | None,
    cfg: dict | None = None,
) -> ClimaxDecision:
    cfg = {**_DEFAULTS, **(cfg or {})}
    feats = dict((oracle_snap or {}).get("features") or {})
    if not bool(cfg.get("enabled")):
        return ClimaxDecision(MODE_IDLE, ("disabled",), feats)
    if _fusion_blocked(fusion_regime):
        return ClimaxDecision(MODE_IDLE, ("fusion_risk_off",), feats)
    ora = _oracle_state(oracle_snap)
    if ora == "WARMUP":
        return ClimaxDecision(MODE_IDLE, ("oracle_warmup",), feats)
    if ora != "RISK_ON":
        return ClimaxDecision(MODE_IDLE, ("oracle_not_risk_on",), feats)

    armed, arm_why = climax_armed(feats, cfg)
    if not armed:
        return ClimaxDecision(MODE_GRIND, tuple(["grind"] + arm_why), feats)

    # Armed. 15m dump (optional feature) or 1h dump → harvest.
    # Stall → tighten. 1h still ripping → keep grinding (20 Aug continuation).
    # No lower bound on 1h dump: hysteresis can keep RISK_ON through a real dump.
    btc_15m = _f(feats, "btc_ret_15m_pct")
    dump_15m = float(cfg.get("dump_15m_max_pct", -0.80))
    if btc_15m is not None and btc_15m <= dump_15m:
        return ClimaxDecision(MODE_HARVEST, ("armed", "dump_15m"), feats)

    btc_1h = _f(feats, "btc_ret_1h_pct")
    if btc_1h is None:
        return ClimaxDecision(MODE_TIGHTEN, ("armed", "no_1h"), feats)

    harvest_cap = float(cfg["harvest_1h_max_pct"])
    stall_hi = float(cfg["stall_1h_max_pct"])
    if btc_1h <= harvest_cap:
        return ClimaxDecision(MODE_HARVEST, ("armed", "dump_1h"), feats)
    if btc_1h <= stall_hi:
        return ClimaxDecision(MODE_TIGHTEN, ("armed", "stall_1h"), feats)
    return ClimaxDecision(MODE_GRIND, ("armed", "1h_still_green"), feats)


def filter_grind_candidates(
    candidates: list[tuple],
    decision: ClimaxDecision,
) -> tuple[list[tuple], list[str]]:
    if decision.mode != MODE_GRIND:
        return candidates, []
    kept = []
    blocked = []
    for item in candidates:
        src = str(item[2] if len(item) > 2 else "").lower()
        if src in GRIND_BLOCK_SOURCES:
            blocked.append(src)
            continue
        kept.append(item)
    return kept, blocked


def harvest_eligible(
    *,
    gain_pct: float,
    decision: ClimaxDecision,
    cfg: dict,
    locked: bool = False,
) -> bool:
    if decision.mode != MODE_HARVEST:
        return False
    if locked:
        return False
    if gain_pct < float(cfg.get("harvest_min_gain_pct", 12.0)):
        return False
    if gain_pct < 0:
        return False
    return True


def harvest_candidate(
    *,
    gain_pct: float,
    decision: ClimaxDecision,
    cfg: dict,
    locked: bool = False,
) -> tuple | None:
    if not harvest_eligible(
        gain_pct=gain_pct, decision=decision, cfg=cfg, locked=locked
    ):
        return None
    pri = int(cfg.get("harvest_priority", HARVEST_PRIORITY))
    return (SELL_FULL, pri, HARVEST_SOURCE)


def position_blocked_from_harvest(
    position: dict | None,
    config_raw: dict | None = None,
) -> bool:
    """Locks, recovery_hold, missing pos: do not harvest. Fail-closed on lock errors."""
    if not isinstance(position, dict):
        return True
    if position.get("recovery_hold") or position.get("sniper_focus"):
        return True
    try:
        from strategies.position_lock import MODE_NO_AUTO_SELL, is_position_locked

        return bool(
            is_position_locked(
                position, mode=MODE_NO_AUTO_SELL, config=config_raw
            )
        )
    except Exception:
        return True


def apply_ttp_climax_overlay(ttp_cfg: dict, decision: ClimaxDecision, cfg: dict) -> dict:
    """Tighten trail knobs when latched; grind does not rewrite rot_mid (TTP is skipped)."""
    out = dict(ttp_cfg)
    if decision.mode not in (MODE_TIGHTEN, MODE_HARVEST):
        return out
    tight = float(cfg.get("tighten_trail_pct", 1.5))
    out["trail_pct"] = min(float(out.get("trail_pct") or tight), tight)
    out["trail_pct_min"] = min(float(out.get("trail_pct_min") or tight), tight)
    return out


def _raw_or_empty(config_raw: dict | None) -> dict:
    if config_raw is not None:
        return config_raw
    try:
        from core.config import get_bot_config

        return get_bot_config().raw or {}
    except Exception:
        return {}


def resolve_climax_decision(
    config_raw: dict | None = None,
    *,
    cfg: dict | None = None,
) -> ClimaxDecision:
    raw = _raw_or_empty(config_raw)
    cfg = {**_DEFAULTS, **(cfg or oracle_climax_config(raw))}
    if not bool(cfg.get("enabled")):
        return ClimaxDecision(MODE_IDLE, ("disabled",), {})
    snap = None
    fusion = None
    try:
        from services.market_oracle_store import get_latest_snapshot

        snap = get_latest_snapshot()
    except Exception:
        snap = None
    try:
        from services.market_policy_fusion import get_global_market_bias

        fusion = (get_global_market_bias(raw) or {}).get("regime")
    except Exception:
        fusion = None
    return evaluate_climax_mode(oracle_snap=snap, fusion_regime=fusion, cfg=cfg)


def begin_cycle(config_raw: dict | None) -> ClimaxDecision:
    """Resolve once per tenant cycle (DE.begin_tenant_cycle)."""
    global _cycle
    cfg = oracle_climax_config(config_raw)
    dec = resolve_climax_decision(config_raw, cfg=cfg)
    _cycle = (dec, cfg)
    return dec


def reset_cycle() -> None:
    global _cycle
    _cycle = None


def current_climax(
    config_raw: dict | None = None,
    *,
    climax_decision: ClimaxDecision | None = None,
) -> tuple[ClimaxDecision, dict]:
    if climax_decision is not None:
        return climax_decision, oracle_climax_config(_raw_or_empty(config_raw))
    if _cycle is not None:
        return _cycle
    raw = _raw_or_empty(config_raw)
    cfg = oracle_climax_config(raw)
    if not bool(cfg.get("enabled")):
        return ClimaxDecision(MODE_IDLE, ("disabled",), {}), cfg
    return resolve_climax_decision(raw, cfg=cfg), cfg


def climax_ttp_adjust(
    ttp_cfg: dict,
    *,
    config_raw: dict | None = None,
    climax_decision: ClimaxDecision | None = None,
) -> tuple[dict, bool]:
    """Return (ttp_cfg, skip). Grind → skip TTP; tighten/harvest → tighter trail."""
    dec, oc = current_climax(config_raw, climax_decision=climax_decision)
    if not oc.get("enabled") or dec.mode == MODE_IDLE:
        return ttp_cfg, False
    if dec.mode == MODE_GRIND:
        return ttp_cfg, True
    return apply_ttp_climax_overlay(ttp_cfg, dec, oc), False
