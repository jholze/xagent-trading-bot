"""Intelligent position capacity — dynamic max_open_eff (pure, no ledger writes).

Composes fusion regime, adaptive cash mode, spendable cash, open-book memory
quality, and restart warmup into one clamped slot ceiling.

Sells and DCA on existing positions are never gated by this module.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from risk.cash_policy import MODE_DEPLOY, MODE_HARVEST, MODE_STEADY


# Default regime deltas: risk-on freer, risk-off / crash tighter.
_DEFAULT_REGIME_ADJ = {
    "RISK_ON": 6,
    "NEUTRAL": 0,
    "RISK_OFF": -6,
    "CRASH": -12,
    "WARMUP": -8,
}

_DEFAULT_CASH_MODE_ADJ = {
    MODE_DEPLOY: 3,
    MODE_STEADY: 0,
    MODE_HARVEST: -4,
}


@dataclass(frozen=True)
class CapacitySnapshot:
    enabled: bool
    max_open_eff: int
    base: int
    min_floor: int
    max_ceiling: int
    regime: str = "NEUTRAL"
    cash_mode: str = MODE_STEADY
    size_mult: float = 1.0
    full_slots: int | None = None
    free_slots: int | None = None
    factors: dict[str, int] = field(default_factory=dict)
    reason_codes: tuple[str, ...] = field(default_factory=tuple)
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["reason_codes"] = list(self.reason_codes)
        return d


def position_capacity_section(risk_config: dict | None) -> dict[str, Any]:
    risk = risk_config if isinstance(risk_config, dict) else {}
    raw = risk.get("position_capacity")
    return dict(raw) if isinstance(raw, dict) else {}


def is_position_capacity_enabled(risk_config: dict | None) -> bool:
    return bool(position_capacity_section(risk_config).get("enabled"))


def _i(cfg: dict, key: str, default: int) -> int:
    try:
        v = cfg.get(key, default)
        if v is None:
            return int(default)
        return int(v)
    except (TypeError, ValueError):
        return int(default)


def _f(cfg: dict, key: str, default: float) -> float:
    try:
        v = cfg.get(key, default)
        if v is None:
            return float(default)
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _regime_adj(regime: str, cfg: dict) -> tuple[int, str]:
    raw_map = cfg.get("regime_adj") if isinstance(cfg.get("regime_adj"), dict) else {}
    merged = {**_DEFAULT_REGIME_ADJ, **{str(k).upper(): int(v) for k, v in raw_map.items()}}
    key = str(regime or "NEUTRAL").upper() or "NEUTRAL"
    if key not in merged:
        key = "NEUTRAL"
    return int(merged.get(key, 0)), key


def _cash_mode_adj(mode: str, cfg: dict) -> tuple[int, str]:
    raw_map = cfg.get("cash_mode_adj") if isinstance(cfg.get("cash_mode_adj"), dict) else {}
    merged = {**_DEFAULT_CASH_MODE_ADJ, **{str(k).upper(): int(v) for k, v in raw_map.items()}}
    key = str(mode or MODE_STEADY).upper() or MODE_STEADY
    if key not in merged:
        key = MODE_STEADY
    return int(merged.get(key, 0)), key


def _size_mult_adj(size_mult: float, cfg: dict) -> int:
    """Continuous fusion size_mult → slot nudge (link_fusion_size_mult)."""
    if not cfg.get("link_fusion_size_mult", True):
        return 0
    scale = _f(cfg, "size_mult_slot_scale", 8.0)
    try:
        sm = float(size_mult)
    except (TypeError, ValueError):
        sm = 1.0
    # 1.0 → 0; 1.25 → +2; 0.5 → -4
    return int(round((sm - 1.0) * scale))


def _cash_spendable_adj(spendable_new: float | None, cfg: dict) -> int:
    if spendable_new is None:
        return 0
    try:
        sn = float(spendable_new)
    except (TypeError, ValueError):
        return 0
    tight_th = _f(cfg, "cash_tight_threshold_usdt", 2000.0)
    loose_th = _f(cfg, "cash_loose_threshold_usdt", 12_000.0)
    tight_adj = _i(cfg, "cash_tight_adj", -4)
    loose_adj = _i(cfg, "cash_loose_adj", 2)
    # How many full entries cash could still fund (soft, not hard cap)
    avg_entry = _f(cfg, "avg_entry_usdt", 0.0)
    afford_adj = 0
    if avg_entry > 0:
        afford = int(sn // avg_entry)
        # If cash can only fund a few more full sizes, pull ceiling in slightly
        # when already "tight" territory; never expands via afford alone.
        if afford <= 1 and sn < loose_th:
            afford_adj = _i(cfg, "cash_low_afford_adj", -2)
        elif afford >= 6 and sn >= loose_th:
            afford_adj = _i(cfg, "cash_high_afford_adj", 1)
    if sn < tight_th:
        return int(tight_adj) + afford_adj
    if sn >= loose_th:
        return int(loose_adj) + afford_adj
    return afford_adj


def _memory_adj(soft_block_open: int, toxic_open: int, prefer_open: int, cfg: dict) -> int:
    per = max(1, _i(cfg, "memory_soft_block_per_n", 5))
    per_adj = _i(cfg, "memory_soft_block_per_adj", -1)
    toxic_per = max(1, _i(cfg, "memory_toxic_per_n", 3))
    toxic_adj = _i(cfg, "memory_toxic_per_adj", -1)
    prefer_per = max(1, _i(cfg, "memory_prefer_per_n", 8))
    prefer_adj = _i(cfg, "memory_prefer_per_adj", 1)
    adj = 0
    adj += (max(0, int(soft_block_open)) // per) * per_adj
    adj += (max(0, int(toxic_open)) // toxic_per) * toxic_adj
    adj += (max(0, int(prefer_open)) // prefer_per) * prefer_adj
    # Cap memory contribution so it cannot dominate regime
    cap = _i(cfg, "memory_adj_cap", 4)
    if adj > cap:
        return cap
    if adj < -cap:
        return -cap
    return adj


def _warmup_adj(process_uptime_sec: float | None, cfg: dict) -> int:
    if process_uptime_sec is None:
        return 0
    warmup_min = _f(cfg, "restart_warmup_min", 15.0)
    if warmup_min <= 0:
        return 0
    try:
        up = float(process_uptime_sec)
    except (TypeError, ValueError):
        return 0
    if up < warmup_min * 60.0:
        return _i(cfg, "restart_warmup_adj", -6)
    return 0


def resolve_max_open_eff(
    *,
    base: int,
    risk_config: dict | None = None,
    regime: str | None = None,
    size_mult: float = 1.0,
    block_buys: bool = False,
    cash_mode: str | None = None,
    spendable_new: float | None = None,
    soft_block_open: int = 0,
    toxic_open: int = 0,
    prefer_open: int = 0,
    process_uptime_sec: float | None = None,
    full_slots: int | None = None,
    drawdown_active: bool = False,
) -> CapacitySnapshot:
    """Compute clamped max_open_eff from injectable inputs (unit-test friendly)."""
    risk = risk_config if isinstance(risk_config, dict) else {}
    cfg = position_capacity_section(risk)
    base_i = max(1, int(base))

    if not cfg.get("enabled"):
        return CapacitySnapshot(
            enabled=False,
            max_open_eff=base_i,
            base=base_i,
            min_floor=base_i,
            max_ceiling=base_i,
            regime=str(regime or "NEUTRAL").upper() or "NEUTRAL",
            cash_mode=str(cash_mode or MODE_STEADY).upper() or MODE_STEADY,
            size_mult=float(size_mult or 1.0),
            full_slots=full_slots,
            free_slots=(
                max(0, base_i - int(full_slots)) if full_slots is not None else None
            ),
            factors={},
            reason_codes=("disabled",),
            rationale=f"static max_open={base_i}",
        )

    min_floor = max(1, _i(cfg, "min_floor", 12))
    max_ceiling = max(min_floor, _i(cfg, "max_ceiling", 36))
    # Optional override of base inside section
    if cfg.get("base") is not None:
        try:
            base_i = max(1, int(cfg["base"]))
        except (TypeError, ValueError):
            pass

    reasons: list[str] = []
    regime_adj, regime_key = _regime_adj(regime or "NEUTRAL", cfg)
    reasons.append(f"regime={regime_key}:{regime_adj:+d}")

    cash_adj_mode, cash_key = _cash_mode_adj(cash_mode or MODE_STEADY, cfg)
    reasons.append(f"cash_mode={cash_key}:{cash_adj_mode:+d}")

    sm_adj = _size_mult_adj(size_mult, cfg)
    if sm_adj:
        reasons.append(f"size_mult={float(size_mult):.2f}:{sm_adj:+d}")

    cash_sn_adj = _cash_spendable_adj(spendable_new, cfg)
    if cash_sn_adj:
        reasons.append(f"spendable:{cash_sn_adj:+d}")

    mem_adj = _memory_adj(soft_block_open, toxic_open, prefer_open, cfg)
    if mem_adj:
        reasons.append(
            f"memory(sb={soft_block_open},tox={toxic_open},pref={prefer_open}):{mem_adj:+d}"
        )

    warm_adj = _warmup_adj(process_uptime_sec, cfg)
    if warm_adj:
        reasons.append(f"warmup:{warm_adj:+d}")

    dd_adj = 0
    if drawdown_active:
        dd_adj = _i(cfg, "drawdown_adj", -3)
        reasons.append(f"drawdown:{dd_adj:+d}")

    raw = (
        base_i
        + regime_adj
        + cash_adj_mode
        + sm_adj
        + cash_sn_adj
        + mem_adj
        + warm_adj
        + dd_adj
    )
    factors = {
        "regime": regime_adj,
        "cash_mode": cash_adj_mode,
        "size_mult": sm_adj,
        "spendable": cash_sn_adj,
        "memory": mem_adj,
        "warmup": warm_adj,
        "drawdown": dd_adj,
    }

    # Hard tighten: crash / block_buys → at most floor (no new risk expansion)
    hard_floor = False
    if block_buys or regime_key == "CRASH":
        hard_floor = True
        reasons.append("hard_floor_crash_or_block")
    elif regime_key == "RISK_OFF" and cfg.get("risk_off_cap_to_base", True):
        # Risk-off: never expand above base (freedom only when risk-on)
        if raw > base_i:
            raw = base_i
            reasons.append("risk_off_no_expand")

    if hard_floor:
        # Pull toward floor; still respect min_floor as absolute minimum
        raw = min(raw, min_floor)

    clamped = max(min_floor, min(max_ceiling, int(round(raw))))
    if clamped != raw:
        reasons.append("clamped")

    free = None
    if full_slots is not None:
        free = max(0, clamped - int(full_slots))

    parts = [f"base{base_i}"]
    for k, v in factors.items():
        if v:
            parts.append(f"{k}{v:+d}")
    rationale = f"max_open_eff={clamped} ({' '.join(parts)})"

    return CapacitySnapshot(
        enabled=True,
        max_open_eff=clamped,
        base=base_i,
        min_floor=min_floor,
        max_ceiling=max_ceiling,
        regime=regime_key,
        cash_mode=cash_key,
        size_mult=float(size_mult or 1.0),
        full_slots=full_slots,
        free_slots=free,
        factors=factors,
        reason_codes=tuple(reasons),
        rationale=rationale,
    )


def count_open_book_memory_signals(
    positions: list[dict] | None,
    *,
    get_profile=None,
) -> tuple[int, int, int]:
    """Return (soft_block_count, toxic_count, prefer_count) for open book.

    get_profile(symbol) -> object with entry_bias and optional features dict.
    Fail-open: missing profile ignored.
    """
    soft_n = toxic_n = prefer_n = 0
    if not positions or get_profile is None:
        return 0, 0, 0
    for pos in positions:
        try:
            sym = pos.get("symbol") if isinstance(pos, dict) else None
            if not sym:
                continue
            prof = get_profile(sym)
            if not prof:
                continue
            bias = str(getattr(prof, "entry_bias", None) or "neutral").lower()
            if bias == "soft_block":
                soft_n += 1
            elif bias == "prefer":
                prefer_n += 1
            feats = getattr(prof, "features", None)
            if isinstance(feats, dict):
                if feats.get("structure_risk") or feats.get("hard_negative"):
                    toxic_n += 1
        except Exception:
            continue
    return soft_n, toxic_n, prefer_n


def format_capacity_reject_message(snap: CapacitySnapshot, open_slots: int) -> str:
    """Human-readable max-open reject for RiskDecision.message."""
    if not snap.enabled:
        return f"Max open positions reached ({snap.max_open_eff})"
    bits = [
        f"Max open positions reached ({open_slots}/{snap.max_open_eff} eff",
        f"base{snap.base}",
        f"regime={snap.regime}",
        f"cash={snap.cash_mode}",
    ]
    fac = snap.factors or {}
    adj_parts = [f"{k}{v:+d}" for k, v in fac.items() if v]
    if adj_parts:
        bits.append(" ".join(adj_parts))
    return ", ".join(bits) + ")"
