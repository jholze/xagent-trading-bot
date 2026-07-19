"""Slot eviction for high-conviction entry — pure plan (no ledger writes).

When free full slots == 0 and entry demand is high enough, rank open full-slot
bags by memory keep_score (+ optional RAG keep_final) and pick a victim sell.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from strategies.sensor_entry_policy import is_sensor_source

EXIT_SOURCE_SLOT_EVICT = "slot_evict_for_entry"

ACTION_PARTIAL = "SELL_PARTIAL"
ACTION_PARTIAL_40 = "SELL_PARTIAL_40"  # legacy alias in logs
ACTION_FULL = "SELL_FULL"
ACTION_REDUCE_TAIL = "SELL_REDUCE_TO_TAIL"


def fraction_to_free_full_slot(
    *,
    sold_percent: float,
    notional_usdt: float,
    tail_sold_pct: float = 0.55,
    tail_notional_usdt: float = 800.0,
) -> tuple[float, str, bool]:
    """Compute sell fraction so remaining bag is a *tail* (frees a full slot).

    Matches ``is_tail_position`` rules: sold >= tail_sold_pct OR remaining notional < cap.

    Returns (fraction, action_label, already_tail).
    """
    sold = max(0.0, min(1.0, float(sold_percent or 0.0)))
    notional = max(0.0, float(notional_usdt or 0.0))
    target_sold = max(0.0, min(1.0, float(tail_sold_pct or 0.55)))
    tail_cap = max(0.0, float(tail_notional_usdt or 800.0))

    if sold >= target_sold or (0 < notional < tail_cap):
        return 0.0, ACTION_PARTIAL, True

    frac_sold = 0.0
    if sold < target_sold and sold < 1.0:
        # new_sold = sold + (1-sold)*f >= target_sold
        frac_sold = (target_sold - sold) / max(1e-9, 1.0 - sold)

    frac_notional = 0.0
    if notional > 0 and tail_cap > 0 and notional >= tail_cap:
        # remaining = notional*(1-f) < tail_cap  → f > 1 - tail_cap/notional
        frac_notional = 1.0 - (tail_cap / notional) + 1e-6

    frac = max(frac_sold, frac_notional)
    frac = max(0.0, min(1.0, frac))
    # Small bags: full close cleaner than dust partial
    if notional > 0 and notional * (1.0 - frac) < max(50.0, tail_cap * 0.15):
        frac = 1.0
    if frac >= 0.99:
        return 1.0, ACTION_FULL, False
    if frac <= 0:
        return 0.0, ACTION_PARTIAL, True
    label = ACTION_REDUCE_TAIL if sold < 0.05 else ACTION_PARTIAL
    return frac, label, False


def would_be_tail_after_sell(
    *,
    sold_percent: float,
    notional_usdt: float,
    sell_fraction: float,
    tail_sold_pct: float = 0.55,
    tail_notional_usdt: float = 800.0,
) -> bool:
    """True if after selling ``sell_fraction`` of current amount the bag is tail-class."""
    f = max(0.0, min(1.0, float(sell_fraction or 0.0)))
    sold = max(0.0, min(1.0, float(sold_percent or 0.0)))
    notional = max(0.0, float(notional_usdt or 0.0))
    new_sold = sold + (1.0 - sold) * f
    new_notional = notional * (1.0 - f)
    if new_notional <= 0 or f >= 0.99:
        return True  # fully closed → free slot
    if new_sold >= float(tail_sold_pct):
        return True
    if 0 < new_notional < float(tail_notional_usdt):
        return True
    return False


@dataclass(frozen=True)
class EntryDemand:
    symbol: str
    source: str
    score: float
    spike_multiple: float = 0.0
    venue_ok: bool = True
    soft_block: bool = False
    structure_risk: bool = False
    block_buys: bool = False
    regime: str = "NEUTRAL"
    spendable_ok: bool = True
    free_full_slots: int = 0
    must_fail_reasons: tuple[str, ...] = field(default_factory=tuple)
    passed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class VictimCandidate:
    symbol: str
    timeframe: str
    gain_pct: float
    peak_gain_pct: float
    idle_hours: float
    sold_percent: float
    notional_usdt: float
    amount: float
    price: float
    keep_profile: float
    keep_rag: float | None
    keep_final: float
    trail_armed: bool
    rotation_eligible: bool  # gain>=0 or realized>0
    prefer: bool
    age_hours: float
    free_score: float = 0.0
    class_name: str = "A"  # A green/flat, B underwater, C toxic
    veto: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EvictionPlan:
    ok: bool
    mode: str  # off | shadow | live
    entry_symbol: str
    victim_symbol: str = ""
    victim_timeframe: str = "4h"
    action: str = ""
    sell_fraction: float = 0.0
    exit_source: str = EXIT_SOURCE_SLOT_EVICT
    exit_rationale: str = ""
    demand_score: float = 0.0
    keep_entry: float = 0.0
    keep_victim: float = 0.0
    swap_edge: float = 0.0
    profile_victim: str = ""
    rag_victim: str = ""
    applied_victim: str = ""
    apply_to_plan: bool = False
    rag_mode: str = "off"
    veto_reason: str = ""
    reason_codes: tuple[str, ...] = field(default_factory=tuple)
    candidates: tuple[dict, ...] = field(default_factory=tuple)
    ab: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["reason_codes"] = list(self.reason_codes)
        d["candidates"] = list(self.candidates)
        return d


def slot_eviction_section(risk_config: dict | None) -> dict[str, Any]:
    risk = risk_config if isinstance(risk_config, dict) else {}
    raw = risk.get("slot_eviction")
    return dict(raw) if isinstance(raw, dict) else {}


def eviction_mode(risk_config: dict | None) -> str:
    cfg = slot_eviction_section(risk_config)
    if not cfg.get("enabled", False):
        return "off"
    mode = str(cfg.get("mode") or "off").strip().lower()
    if mode in ("off", "disabled", "false", "0"):
        return "off"
    if mode in ("shadow", "log", "dry"):
        return "shadow"
    if mode in ("live", "active", "on", "true", "1"):
        return "live"
    return "off"


def _f(cfg: dict, key: str, default: float) -> float:
    try:
        v = cfg.get(key, default)
        return float(default if v is None else v)
    except (TypeError, ValueError):
        return float(default)


def _i(cfg: dict, key: str, default: int) -> int:
    try:
        v = cfg.get(key, default)
        return int(default if v is None else v)
    except (TypeError, ValueError):
        return int(default)


def _mem(cfg: dict) -> dict:
    m = cfg.get("memory")
    return dict(m) if isinstance(m, dict) else {}


def _rag(cfg: dict) -> dict:
    r = cfg.get("rag")
    return dict(r) if isinstance(r, dict) else {}


def score_entry_demand(
    *,
    symbol: str,
    source: str,
    free_full_slots: int,
    spike_multiple: float = 0.0,
    venue_ok: bool = True,
    soft_block: bool = False,
    structure_risk: bool = False,
    block_buys: bool = False,
    regime: str | None = None,
    spendable_ok: bool = True,
    risk_config: dict | None = None,
    prefer_entry: bool = False,
) -> EntryDemand:
    """Score whether this new entry is worth freeing a slot for."""
    cfg = slot_eviction_section(risk_config)
    sources = cfg.get("sources") or [
        "entry_sensor_15m",
        "vol_spike_15m",
        "entry_sensor",
        "15m_sensor",
    ]
    source_l = (source or "").lower()
    allowed = {str(s).lower() for s in sources}
    sensor_ok = is_sensor_source(source) or source_l in allowed

    must: list[str] = []
    if free_full_slots > 0:
        must.append("free_slots_available")
    if not venue_ok:
        must.append("venue_fail")
    if soft_block:
        must.append("soft_block")
    if structure_risk:
        must.append("structure_risk")
    if block_buys:
        must.append("block_buys")
    reg = str(regime or "NEUTRAL").upper() or "NEUTRAL"
    if reg == "CRASH" or (cfg.get("skip_if_crash", True) and reg == "CRASH"):
        must.append("crash")
    if cfg.get("skip_if_block_buys", True) and block_buys:
        if "block_buys" not in must:
            must.append("block_buys")
    if not spendable_ok and cfg.get("require_spendable_for_entry", True):
        must.append("spendable")
    if not sensor_ok and cfg.get("require_sensor_source", True):
        must.append("source_not_allowed")

    score = 0.0
    if sensor_ok:
        score += 2.0
    try:
        spike = float(spike_multiple or 0)
    except (TypeError, ValueError):
        spike = 0.0
    if spike >= 5.0:
        score += 2.0
    elif spike >= 3.0:
        score += 1.0
    if prefer_entry:
        score += 1.0

    mode = eviction_mode(risk_config)
    min_live = _f(cfg, "min_entry_score", 4.0)
    min_shadow = _f(cfg, "min_entry_score_shadow", 3.0)
    thr = min_shadow if mode == "shadow" else min_live
    passed = not must and score >= thr and free_full_slots <= 0

    return EntryDemand(
        symbol=symbol,
        source=source or "",
        score=score,
        spike_multiple=spike,
        venue_ok=venue_ok,
        soft_block=soft_block,
        structure_risk=structure_risk,
        block_buys=block_buys,
        regime=reg,
        spendable_ok=spendable_ok,
        free_full_slots=int(free_full_slots),
        must_fail_reasons=tuple(must),
        passed=passed,
    )


def memory_keep_score(
    profile: Any | None,
    *,
    risk_config: dict | None = None,
    rotation_urgency: float = 0.0,
) -> float:
    """Deterministic keep_score in [0, 1] from CoinProfile-like object."""
    cfg = slot_eviction_section(risk_config)
    mem = _mem(cfg)
    missing = _f(mem, "missing_profile_keep", 0.5)
    if profile is None:
        return max(0.0, min(1.0, missing))

    score = 0.5
    bias = str(getattr(profile, "entry_bias", None) or "neutral").lower()
    if bias == "prefer":
        score += 0.22
    elif bias == "soft_block":
        score -= 0.28

    try:
        size_bias = float(getattr(profile, "size_bias", 1.0) or 1.0)
    except (TypeError, ValueError):
        size_bias = 1.0
    score += (size_bias - 1.0) * 0.35  # 1.2 → +0.07; 0.7 → -0.105

    try:
        wr = float(getattr(profile, "win_rate", 0.0) or 0.0)
    except (TypeError, ValueError):
        wr = 0.0
    try:
        n = int(getattr(profile, "sells_30d", 0) or getattr(profile, "trades_30d", 0) or 0)
    except (TypeError, ValueError):
        n = 0
    min_n = _i(mem, "min_samples_for_win_rate", 3)
    if n >= min_n:
        score += (wr - 0.5) * 0.3

    try:
        risk_s = float(getattr(profile, "risk_score", 0.5) or 0.5)
    except (TypeError, ValueError):
        risk_s = 0.5
    score += (0.5 - risk_s) * 0.2

    try:
        total_pnl = float(getattr(profile, "total_pnl_usdt", 0.0) or 0.0)
    except (TypeError, ValueError):
        total_pnl = 0.0
    if total_pnl > 0:
        score += min(0.08, total_pnl / 5000.0 * 0.08)
    elif total_pnl < 0:
        score -= min(0.12, abs(total_pnl) / 2000.0 * 0.12)

    feats = getattr(profile, "features", None)
    if isinstance(feats, dict):
        if feats.get("structure_risk") or feats.get("hard_negative"):
            score -= 0.18
        if feats.get("soft_block_until") or bias == "soft_block":
            score -= 0.05

    # higher urgency → lower keep (prefer free sooner when green)
    urg = max(0.0, min(1.0, float(rotation_urgency or 0.0)))
    score -= urg * 0.12

    return max(0.0, min(1.0, score))


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def evidence_delta_from_hits(hits: list[Any] | None) -> float:
    """Map RAG hit texts to keep boost in roughly [-0.35, +0.25]. Pure."""
    if not hits:
        return 0.0
    delta = 0.0
    for h in hits:
        text = ""
        score = 0.5
        if hasattr(h, "text"):
            text = str(getattr(h, "text", "") or "").lower()
            try:
                score = float(getattr(h, "score", 0.5) or 0.5)
            except (TypeError, ValueError):
                score = 0.5
        elif isinstance(h, dict):
            text = str(h.get("text") or "").lower()
            try:
                score = float(h.get("score") or 0.5)
            except (TypeError, ValueError):
                score = 0.5
        else:
            text = str(h).lower()
        w = max(0.15, min(1.0, score))
        neg = (
            "soft_block",
            "gross loss",
            "gross_loss",
            "stop",
            "blowup",
            "structure_risk",
            "hard_neg",
            "weak history",
            "loss",
            "dump",
        )
        pos = (
            "prefer",
            "solid",
            "win_rate",
            "winner",
            "strong history",
            "lesson keep",
            "hold quality good",
        )
        for n in neg:
            if n in text:
                delta -= 0.06 * w
        for p in pos:
            if p in text:
                delta += 0.04 * w
    return max(-0.35, min(0.25, delta))


def apply_rag_keep(
    keep_profile: float,
    hits: list[Any] | None,
    *,
    evidence_weight: float = 0.25,
    retrieve_error: bool = False,
) -> tuple[float, float]:
    """Return (keep_rag, evidence_delta). On error, keep_rag == keep_profile."""
    if retrieve_error:
        return clamp01(keep_profile), 0.0
    ed = evidence_delta_from_hits(hits)
    kr = clamp01(float(keep_profile) + ed * float(evidence_weight))
    return kr, ed


def _weights(cfg: dict) -> dict[str, float]:
    w = cfg.get("weights") if isinstance(cfg.get("weights"), dict) else {}
    return {
        "memory": float(w.get("memory", 0.55)),
        "idle": float(w.get("idle", 0.2)),
        "pnl_flat": float(w.get("pnl_flat", 0.1)),
        "tail_ready": float(w.get("tail_ready", 0.15)),
    }


def free_score_for_candidate(
    c: VictimCandidate,
    *,
    risk_config: dict | None = None,
) -> float:
    """Higher = more free-able. Memory (low keep) dominates for Class A."""
    cfg = slot_eviction_section(risk_config)
    w = _weights(cfg)
    keep = c.keep_final if c.keep_final is not None else c.keep_profile
    mem_term = (1.0 - clamp01(keep)) * w["memory"]
    idle_term = min(1.0, max(0.0, c.idle_hours) / 72.0) * w["idle"]
    # flatness: small |gain| preferred among greens
    flat = 1.0 - min(1.0, abs(c.gain_pct) / 20.0)
    pnl_term = flat * w["pnl_flat"]
    sold = min(1.0, max(0.0, c.sold_percent))
    tail_term = sold * w["tail_ready"]
    return mem_term + idle_term + pnl_term + tail_term


def plan_slot_eviction(
    *,
    demand: EntryDemand,
    candidates: list[VictimCandidate],
    risk_config: dict | None = None,
    rate_limit_blocked: bool = False,
    rate_limit_reason: str = "",
    warmup_active: bool = False,
) -> EvictionPlan:
    """Pick victim among pre-built candidates (hard vetos already partially applied)."""
    mode = eviction_mode(risk_config)
    cfg = slot_eviction_section(risk_config)
    rag_cfg = _rag(cfg)
    rag_mode = str(rag_cfg.get("mode") or "off").lower()
    apply_to_plan = bool(rag_cfg.get("apply_to_plan", False))
    mem = _mem(cfg)
    min_edge = _f(mem, "min_entry_keep_edge", 0.12)
    prefer_hard = bool(mem.get("prefer_is_hard_keep", True))
    prefer_floor = _f(mem, "prefer_keep_floor", 0.7)
    protect_peak = _f(cfg, "protect_peak_gain_pct", 12.0)
    min_hold = _f(cfg, "min_hold_hours", 3.0)
    max_notional = _f(cfg, "max_evict_notional_usdt", 8000.0)
    min_victim = _f(cfg, "min_victim_score", 0.15)

    if mode == "off":
        return EvictionPlan(
            ok=False,
            mode="off",
            entry_symbol=demand.symbol,
            demand_score=demand.score,
            veto_reason="mode_off",
            reason_codes=("mode_off",),
        )

    if not demand.passed:
        reasons = demand.must_fail_reasons or ("demand_failed",)
        return EvictionPlan(
            ok=False,
            mode=mode,
            entry_symbol=demand.symbol,
            demand_score=demand.score,
            apply_to_plan=apply_to_plan,
            rag_mode=rag_mode,
            veto_reason=reasons[0],
            reason_codes=reasons,
        )

    if warmup_active and cfg.get("skip_if_warmup", True):
        return EvictionPlan(
            ok=False,
            mode=mode,
            entry_symbol=demand.symbol,
            demand_score=demand.score,
            apply_to_plan=apply_to_plan,
            rag_mode=rag_mode,
            veto_reason="warmup",
            reason_codes=("warmup",),
        )

    if rate_limit_blocked:
        return EvictionPlan(
            ok=False,
            mode=mode,
            entry_symbol=demand.symbol,
            demand_score=demand.score,
            apply_to_plan=apply_to_plan,
            rag_mode=rag_mode,
            veto_reason=rate_limit_reason or "rate_limit",
            reason_codes=("rate_limit",),
        )

    # Resolve keep_final per candidate for ranking
    ranked: list[VictimCandidate] = []
    for c in candidates:
        if c.symbol == demand.symbol:
            continue
        veto = c.veto
        if c.trail_armed:
            veto = veto or "trail_armed"
        if c.peak_gain_pct >= protect_peak:
            veto = veto or "peak_protected"
        if c.age_hours < min_hold:
            veto = veto or "min_hold"
        if c.notional_usdt > max_notional:
            veto = veto or "max_notional"
        if prefer_hard and c.prefer and c.keep_final >= prefer_floor and c.gain_pct >= 0:
            veto = veto or "prefer_hard_keep"
        if veto:
            ranked.append(
                VictimCandidate(
                    **{**c.to_dict(), "veto": veto, "free_score": -1.0}  # type: ignore[arg-type]
                )
            )
            continue
        # Class A needs rotation eligibility (no full loss dump by default)
        if c.gain_pct < 0 and not c.rotation_eligible:
            if not cfg.get("prefer_reduce_to_tail", True):
                ranked.append(
                    VictimCandidate(
                        **{**c.to_dict(), "veto": "underwater_no_reduce", "free_score": -1.0}
                    )
                )
                continue
        fs = free_score_for_candidate(c, risk_config=risk_config)
        ranked.append(VictimCandidate(**{**c.to_dict(), "free_score": fs, "veto": ""}))

    eligible = [c for c in ranked if not c.veto]
    # Prefer Class A first
    class_a = [c for c in eligible if c.class_name == "A" or c.gain_pct >= 0]
    pool = class_a if class_a else eligible

    def _pick(use_rag: bool) -> VictimCandidate | None:
        best = None
        best_fs = -1.0
        for c in pool:
            keep = (
                c.keep_rag
                if use_rag and c.keep_rag is not None
                else c.keep_profile
            )
            # recompute free with that keep
            tmp = VictimCandidate(**{**c.to_dict(), "keep_final": float(keep)})
            fs = free_score_for_candidate(tmp, risk_config=risk_config)
            if fs > best_fs:
                best_fs = fs
                best = tmp
        if best is None or best_fs < min_victim:
            return None
        return best

    profile_pick = _pick(use_rag=False)
    rag_pick = _pick(use_rag=True) if rag_mode not in ("off", "") else profile_pick

    if apply_to_plan and rag_mode not in ("off", "") and rag_pick is not None:
        applied = rag_pick
    else:
        applied = profile_pick

    cand_dicts = tuple(c.to_dict() for c in ranked)
    ab = {
        "profile_victim": profile_pick.symbol if profile_pick else "",
        "rag_victim": rag_pick.symbol if rag_pick else "",
        "applied_victim": applied.symbol if applied else "",
        "profile_vs_rag_agree": bool(
            profile_pick and rag_pick and profile_pick.symbol == rag_pick.symbol
        ),
        "rag_mode": rag_mode,
        "apply_to_plan": apply_to_plan,
    }

    if applied is None:
        return EvictionPlan(
            ok=False,
            mode=mode,
            entry_symbol=demand.symbol,
            demand_score=demand.score,
            profile_victim=ab["profile_victim"],
            rag_victim=ab["rag_victim"],
            applied_victim="",
            apply_to_plan=apply_to_plan,
            rag_mode=rag_mode,
            veto_reason="no_candidate",
            reason_codes=("no_candidate",),
            candidates=cand_dicts,
            ab=ab,
        )

    # Swap gate for Class A (green)
    entry_keep = 0.55  # proxy unless provided via candidate list later
    # Prefer demand prefer flag
    if demand.score >= 5:
        entry_keep = 0.62
    if demand.soft_block:
        entry_keep = 0.2

    # If we have entry keep on a synthetic cand — scan
    for c in candidates:
        if c.symbol == demand.symbol:
            entry_keep = c.keep_final
            break

    victim_keep = float(applied.keep_final)
    edge = entry_keep - victim_keep
    if applied.gain_pct >= 0 and edge < min_edge:
        return EvictionPlan(
            ok=False,
            mode=mode,
            entry_symbol=demand.symbol,
            demand_score=demand.score,
            keep_entry=entry_keep,
            keep_victim=victim_keep,
            swap_edge=edge,
            profile_victim=ab["profile_victim"],
            rag_victim=ab["rag_victim"],
            applied_victim=applied.symbol,
            apply_to_plan=apply_to_plan,
            rag_mode=rag_mode,
            veto_reason="memory_swap_not_worth_it",
            reason_codes=("memory_swap_not_worth_it",),
            candidates=cand_dicts,
            ab=ab,
        )

    # Action: always size so remaining is tail (frees full slot) — not a fixed 40%.
    tail_sold = _f(cfg, "tail_target_sold_pct", 0.55)
    tail_notional = _f(cfg, "tail_target_max_notional_usdt", 800.0)
    # Align with sell_policy.rotation defaults when present on parent risk
    if isinstance(risk_config, dict):
        sp = risk_config.get("sell_policy") if isinstance(risk_config.get("sell_policy"), dict) else {}
        rot = sp.get("rotation") if isinstance(sp.get("rotation"), dict) else {}
        if rot.get("tail_exempt_sold_pct") is not None:
            try:
                tail_sold = max(tail_sold, float(rot["tail_exempt_sold_pct"]))
            except (TypeError, ValueError):
                pass
        if rot.get("tail_exempt_notional_usdt") is not None:
            try:
                tail_notional = min(tail_notional, float(rot["tail_exempt_notional_usdt"]))
            except (TypeError, ValueError):
                pass

    frac, action, already_tail = fraction_to_free_full_slot(
        sold_percent=applied.sold_percent,
        notional_usdt=applied.notional_usdt,
        tail_sold_pct=tail_sold,
        tail_notional_usdt=tail_notional,
    )
    if already_tail:
        # Should not be a full-slot candidate; fail closed
        return EvictionPlan(
            ok=False,
            mode=mode,
            entry_symbol=demand.symbol,
            demand_score=demand.score,
            apply_to_plan=apply_to_plan,
            rag_mode=rag_mode,
            veto_reason="victim_already_tail",
            reason_codes=("victim_already_tail",),
            candidates=cand_dicts,
            ab=ab,
        )
    if applied.gain_pct < 0 and cfg.get("prefer_reduce_to_tail", True):
        action = ACTION_REDUCE_TAIL
    if not would_be_tail_after_sell(
        sold_percent=applied.sold_percent,
        notional_usdt=applied.notional_usdt,
        sell_fraction=frac,
        tail_sold_pct=tail_sold,
        tail_notional_usdt=tail_notional,
    ):
        # Safety: force full if math edge-case left a full bag
        frac, action = 1.0, ACTION_FULL

    rationale = (
        f"for={demand.symbol} demand={demand.score:.0f} "
        f"keep_v={victim_keep:.2f} keep_e={entry_keep:.2f} edge={edge:.2f} "
        f"rag={rag_mode} action={action} frac={frac:.2f}"
    )

    return EvictionPlan(
        ok=True,
        mode=mode,
        entry_symbol=demand.symbol,
        victim_symbol=applied.symbol,
        victim_timeframe=applied.timeframe or "4h",
        action=action,
        sell_fraction=frac,
        exit_source=EXIT_SOURCE_SLOT_EVICT,
        exit_rationale=rationale,
        demand_score=demand.score,
        keep_entry=entry_keep,
        keep_victim=victim_keep,
        swap_edge=edge,
        profile_victim=ab["profile_victim"],
        rag_victim=ab["rag_victim"],
        applied_victim=applied.symbol,
        apply_to_plan=apply_to_plan,
        rag_mode=rag_mode,
        reason_codes=("plan_ok",),
        candidates=cand_dicts,
        ab=ab,
    )


def format_eviction_reject_suffix(plan: EvictionPlan | None) -> str:
    if plan is None:
        return ""
    if plan.ok and plan.mode == "live":
        return (
            f" · eviction: sold/plan {plan.victim_symbol} for {plan.entry_symbol} "
            f"({plan.action})"
        )
    if plan.ok and plan.mode == "shadow":
        return (
            f" · eviction shadow would {plan.victim_symbol} for {plan.entry_symbol}"
        )
    if plan.veto_reason:
        return f" · eviction: {plan.veto_reason}"
    return ""
