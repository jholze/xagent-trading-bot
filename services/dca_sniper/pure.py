"""Pure ranking / sizing / focus-slot math for DCA sniper (no I/O)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class CandidateView:
    symbol: str
    timeframe: str = "1h"
    average_entry: float = 0.0
    amount: float = 0.0
    mark: float = 0.0
    loss_pct: float = 0.0
    notional: float = 0.0
    dca_rounds: int = 0
    recovery_hold: bool = False
    sniper_focus: bool = False
    strategy_profile: str = ""
    strategy_class: str = ""
    has_grid_plan: bool = False
    score: float = 0.0
    checklist: dict[str, Any] = field(default_factory=dict)
    hard_fail: list[str] = field(default_factory=list)
    usdt_suggest: float = 0.0
    rank_priority: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def is_grid_excluded(
    *,
    strategy_profile: str = "",
    strategy_class: str = "",
    has_grid_plan: bool = False,
    exclude_grid: bool = True,
) -> bool:
    if not exclude_grid:
        return False
    sp = str(strategy_profile or "").strip().lower()
    sc = str(strategy_class or "").strip().lower()
    if sp == "grid" or sc == "grid":
        return True
    if has_grid_plan and sp in ("", "grid", "hybrid"):
        # active grid plan → skip heavy unless explicitly mid without grid profile
        if sp in ("grid",) or sc == "grid" or has_grid_plan and sp == "grid":
            return True
    if sc == "grid" or sp == "grid":
        return True
    # pure grid only when profile/class says grid
    return sc == "grid" or sp == "grid"


def loss_pct(avg: float, mark: float) -> float:
    if avg <= 0 or mark <= 0:
        return 0.0
    return (mark / avg - 1.0) * 100.0


def profile_key(strategy_profile: str = "", strategy_class: str = "", symbol: str = "") -> str:
    sp = str(strategy_profile or "").lower()
    sc = str(strategy_class or "").lower()
    if "meme" in sp or "meme" in sc:
        return "meme"
    if "stable" in sp or "large" in sc or "major" in sp:
        return "major"
    if "volatile" in sp or "alt" in sp:
        return "volatile"
    return "default"


def compute_heavy_size(
    *,
    rest_notional: float,
    score: float,
    heavy_min_score: float,
    profile: str,
    profile_f: dict[str, float],
    spendable_dca: float,
    max_single_add_usdt: float,
    max_bag_pct_equity: float,
    equity: float,
    bag_now: float,
    min_meaningful_usdt: float,
    daily_room: float | None = None,
    liq_cap: float | None = None,
) -> float:
    """Individual heavy size — never fixed. Returns 0 if not meaningful."""
    if rest_notional <= 0 or spendable_dca <= 0:
        return 0.0
    if score < float(heavy_min_score):
        return 0.0
    f_base = float((profile_f or {}).get(profile) or (profile_f or {}).get("default") or 0.75)
    # score quality boost: at min → 0.85×f, at min+4 → ~1.15×f
    span = 4.0
    t = max(0.0, min(1.0, (float(score) - float(heavy_min_score)) / span))
    f = f_base * (0.85 + 0.30 * t)
    raw = rest_notional * f
    max_bag = float(equity) * (float(max_bag_pct_equity) / 100.0) if equity > 0 else raw
    room_bag = max(0.0, max_bag - float(bag_now or 0))
    add = min(raw, float(max_single_add_usdt), float(spendable_dca), room_bag if room_bag > 0 else raw)
    if daily_room is not None:
        add = min(add, max(0.0, float(daily_room)))
    if liq_cap is not None and liq_cap > 0:
        add = min(add, float(liq_cap))
    if add < float(min_meaningful_usdt):
        return 0.0
    return round(add, 2)


def dynamic_focus_slots(
    *,
    candidates_yes: list[CandidateView],
    spendable_dca: float,
    max_focus_slots: int,
    min_cash_after_focus: float,
    open_focus_count: int = 0,
) -> int:
    """N_eff from quality list + cash room (1…max)."""
    cap = max(0, int(max_focus_slots))
    free_slots = max(0, cap - int(open_focus_count or 0))
    if free_slots <= 0 or not candidates_yes:
        return 0
    cash = max(0.0, float(spendable_dca))
    if cash <= float(min_cash_after_focus):
        return 0
    n = 0
    left = cash
    for c in candidates_yes:
        if n >= free_slots:
            break
        need = float(c.usdt_suggest or 0)
        if need <= 0:
            continue
        if left - need < float(min_cash_after_focus) and n > 0:
            break
        if left < need and n == 0:
            # first focus: allow if any meaningful spendable
            if left < need * 0.5:
                break
            need = left
        left -= need
        n += 1
    return n


def rank_priority(score: float, loss_pct_val: float, notional: float) -> float:
    """Higher = better focus candidate."""
    loss_u = min(3.0, abs(min(0.0, loss_pct_val)) / 10.0)
    size_u = min(2.0, max(0.0, notional) / 5000.0)
    return float(score) * 2.0 + loss_u + size_u


def cash_plan(
    *,
    need_usdt: float,
    spendable_dca: float,
    free_cash_above_floor: float,
    soft_claim_enabled: bool,
    soft_claim_max_usdt: float,
) -> dict[str, Any]:
    """Order: spendable_dca → soft claim → need fund → wait."""
    need = max(0.0, float(need_usdt))
    sd = max(0.0, float(spendable_dca))
    free = max(0.0, float(free_cash_above_floor))
    if need <= 0:
        return {"action": "WAIT", "need": 0.0, "available": sd, "claim": 0.0, "shortfall": 0.0}
    if sd >= need:
        return {"action": "DCA_HEAVY", "need": need, "available": sd, "claim": 0.0, "shortfall": 0.0}
    claim = 0.0
    if soft_claim_enabled:
        # claim from free cash beyond current spendable (floor-safe free pool)
        claimable = max(0.0, free - sd)
        claim = min(need - sd, claimable, max(0.0, float(soft_claim_max_usdt)))
    if sd + claim >= need:
        return {
            "action": "DCA_HEAVY",
            "need": need,
            "available": sd + claim,
            "claim": claim,
            "shortfall": 0.0,
        }
    shortfall = need - (sd + claim)
    return {
        "action": "NEED_CASH",
        "need": need,
        "available": sd + claim,
        "claim": claim,
        "shortfall": round(shortfall, 2),
    }


def score_checklist(layers: dict[str, Any]) -> tuple[float, list[str], dict[str, Any]]:
    """Aggregate checklist layers → score, hard_fails, detail.

    layers keys optional: position, ta, funding, facts, social, memory, portfolio
    each value: {pass: bool, hard: bool, score: float, reason: str}
    """
    hard_fails: list[str] = []
    total = 0.0
    weight_sum = 0.0
    detail: dict[str, Any] = {}
    weights = {
        "position": 1.5,
        "ta": 2.0,
        "funding": 1.0,
        "facts": 1.5,
        "social": 0.8,
        "memory": 1.0,
        "portfolio": 1.2,
    }
    for key, w in weights.items():
        layer = layers.get(key) if isinstance(layers, dict) else None
        if not isinstance(layer, dict):
            continue
        detail[key] = layer
        if layer.get("hard") and not layer.get("pass", True):
            hard_fails.append(f"{key}:{layer.get('reason') or 'fail'}")
            continue
        if layer.get("pass") is False and layer.get("hard"):
            hard_fails.append(f"{key}:{layer.get('reason') or 'fail'}")
            continue
        s = float(layer.get("score") or 0)
        if layer.get("pass") is False:
            s = min(s, 0.0)
        total += s * w
        weight_sum += w
    if hard_fails:
        return 0.0, hard_fails, detail
    if weight_sum <= 0:
        return 0.0, [], detail
    # normalize to ~0–10
    score = max(0.0, min(10.0, (total / weight_sum) * 2.0))
    return round(score, 2), [], detail


def select_focus_batch(
    ranked: list[CandidateView],
    *,
    spendable_dca: float,
    max_focus_slots: int,
    min_cash_after_focus: float,
    open_focus_count: int,
    heavy_min_score: float,
) -> list[CandidateView]:
    yes = [
        c
        for c in ranked
        if not c.hard_fail
        and c.score >= heavy_min_score
        and c.usdt_suggest > 0
        and not c.recovery_hold
        and not c.sniper_focus
    ]
    n = dynamic_focus_slots(
        candidates_yes=yes,
        spendable_dca=spendable_dca,
        max_focus_slots=max_focus_slots,
        min_cash_after_focus=min_cash_after_focus,
        open_focus_count=open_focus_count,
    )
    if n <= 0:
        return []
    out: list[CandidateView] = []
    left = float(spendable_dca)
    for c in yes:
        if len(out) >= n:
            break
        need = float(c.usdt_suggest)
        if need > left and out:
            break
        if need > left:
            c.usdt_suggest = round(left, 2)
            if c.usdt_suggest <= 0:
                break
        left -= float(c.usdt_suggest)
        out.append(c)
    return out
