"""Desk HUD helpers. v0: kill-switch + LAB cards + next-edge."""

from __future__ import annotations

from services.desk.models import (
    DEFAULT_CMC_MIN,
    LC_BUYISH,
    MEMORY_BLOCK_FLAGS,
    OVERSOLD_RSI,
    RISK_OFF_REGIMES,
    Hud,
    MemoryCard,
    SocialCard,
    TaCard,
)


def desk_enabled(config_raw: dict | None) -> bool:
    return bool(((config_raw or {}).get("desk") or {}).get("enabled"))


def _num(facts: dict, key: str, default: float = 0.0) -> float:
    try:
        value = facts.get(key, default)
        if value is None:
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _int(facts: dict, key: str, default: int = 0) -> int:
    return int(_num(facts, key, default))


def _upper(value: object) -> str:
    return str(value or "").strip().upper()


def _ta_card(facts: dict) -> TaCard:
    rsi = _num(facts, "rsi", 50.0)
    oversold = _num(facts, "remaining_round_oversold_rsi", OVERSOLD_RSI)
    at_lower_bb = bool(facts.get("at_lower_bb"))
    dca_rounds = _int(facts, "dca_rounds")
    dca_max = _int(facts, "dca_max_rounds")
    relvol_open = _int(facts, "relvol_open")
    relvol_max = _int(facts, "relvol_max")

    dip = rsi < oversold
    if dip and not at_lower_bb:
        setup = "dip miss"
        stance = "MISS"
        blocker = "not at lower BB"
    elif dip:
        setup = "dip"
        stance = "ARMED"
        blocker = "—"
    elif not at_lower_bb:
        setup = "miss"
        stance = "MISS"
        blocker = "not at lower BB"
    else:
        setup = "—"
        stance = "IDLE"
        blocker = "—"

    # RelVol cap is a different path — never a TA blocker.
    if dca_rounds < dca_max:
        path = f"DCA {dca_rounds}/{dca_max}"
    elif relvol_max > 0 and relvol_open < relvol_max:
        path = "RelVol"
    else:
        path = "sensor"

    return {"setup": setup, "path": path, "blocker": blocker, "stance": stance}


def _social_card(facts: dict) -> SocialCard:
    conf = _num(facts, "cmc_confidence")
    trust = _num(facts, "cmc_trust")
    product = conf * (trust / 100.0)
    fusion = _upper(facts.get("fusion_regime"))
    cmc_min = _num(facts, "cmc_min_confidence", DEFAULT_CMC_MIN)

    lead = f"CMC {conf:.0f}×{trust:.0f} → {product:.0f}"

    chorus_parts: list[str] = []
    if fusion == "NEUTRAL":
        chorus_parts.append("Santiment muted (fusion NEUTRAL)")
    lc = facts.get("lc_action")
    lc_norm = str(lc).strip().lower() if lc is not None else ""
    if lc_norm in LC_BUYISH:
        chorus_parts.append("Lunar BUY")
    else:
        chorus_parts.append("Lunar thin")
    chorus = "; ".join(chorus_parts)

    ttl = "quotes fallback" if facts.get("cmc_quotes_fallback") else "—"

    if fusion in RISK_OFF_REGIMES:
        stance = "BLOCK"
    elif product >= cmc_min:
        stance = "ARMED"
    else:
        stance = "IDLE"

    return {"lead": lead, "chorus": chorus, "ttl": ttl, "stance": stance}


def _memory_card(facts: dict) -> MemoryCard:
    bias = str(facts.get("memory_bias") or "neutral")
    flag = facts.get("memory_flag")
    lesson = facts.get("memory_lesson") or facts.get("lesson") or "—"
    flag_norm = str(flag).strip().lower() if flag else ""
    if flag_norm in MEMORY_BLOCK_FLAGS:
        stance = "BLOCK"
    else:
        stance = "IDLE"
    return {"bias": bias, "flag": flag, "lesson": str(lesson), "stance": stance}


def build_hud(facts: dict) -> Hud:
    """≤4 fields per card. RelVol cap is a different path — not TA blocker."""
    facts = facts or {}
    return {
        "ta": _ta_card(facts),
        "social": _social_card(facts),
        "memory": _memory_card(facts),
    }


def next_edge(facts: dict, hud: dict) -> str:
    """One sentence. Precedence: Memory BLOCK > path/DCA gate > Social ARMED > TA miss > idle."""
    facts = facts or {}
    hud = hud or {}
    ta = hud.get("ta") or {}
    social = hud.get("social") or {}
    memory = hud.get("memory") or {}

    oversold = _num(facts, "remaining_round_oversold_rsi", OVERSOLD_RSI)
    rsi_gate = f"RSI<{oversold:.0f}"
    dca_remaining = _int(facts, "dca_rounds") < _int(facts, "dca_max_rounds")

    if memory.get("stance") == "BLOCK":
        flag = memory.get("flag") or "block"
        return f"MEMORY: {flag} → hold."

    # LAB lock: TA dip-miss names TA as SOURCE; next action is the remaining DCA round.
    # RelVol cap is mentioned only as a different path — never the named source.
    if ta.get("stance") == "MISS" and dca_remaining:
        return (
            f"TA: dip miss; next edge is DCA when {rsi_gate} "
            "(RelVol cap is a different path)."
        )

    if social.get("stance") == "ARMED":
        lead = social.get("lead") or "CMC"
        return f"SOCIAL: {lead} → add."

    if ta.get("stance") == "MISS":
        setup = ta.get("setup") or "miss"
        return f"TA: {setup} → wait for lower BB."

    if dca_remaining:
        return f"TA: dip → DCA when {rsi_gate}."

    return "IDLE: no edge → hold."
