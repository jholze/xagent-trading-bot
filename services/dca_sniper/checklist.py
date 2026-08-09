"""Deep-ish checklist builder (sync; uses bot snapshot + optional market fields)."""

from __future__ import annotations

from typing import Any

from services.dca_sniper.pure import score_checklist


def build_layers_from_snapshot(cand: dict[str, Any], cash: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build layer dicts from candidate snapshot fields (no external IO).

    Expects optional precomputed: rsi, atr_pct, funding_rate_pct, entry_bias,
    structure_ok, unlock_risk, social_block, etc.
    """
    from services.dca_sniper.policy import dd_band_ok, dd_pct_from_loss

    loss = float(cand.get("loss_pct") or 0)
    rounds = int(cand.get("dca_rounds") or 0)
    max_rounds = int(cand.get("max_rounds") or 4)
    remainder = float(cand.get("notional") or 0)
    rsi = cand.get("rsi")
    atr = cand.get("atr_pct")
    funding = cand.get("funding_rate_pct")
    structure_ok = cand.get("structure_ok")
    entry_bias = str(cand.get("entry_bias") or "neutral").lower()
    unlock_risk = bool(cand.get("unlock_risk") or cand.get("hard_negative"))
    social_block = bool(cand.get("social_block") or cand.get("block_buys"))
    spendable = float((cash or {}).get("spendable_dca") or cand.get("spendable_dca") or 0)
    sniper_cfg = cand.get("sniper_cfg") if isinstance(cand.get("sniper_cfg"), dict) else {}

    # Position layer — same DD band as sizer (policy.dd_band_ok)
    band_ok, band_why = dd_band_ok(loss, sniper_cfg, for_checklist=True)
    pos_pass = band_ok and rounds < max_rounds and remainder >= 150
    pos_score = 0.0
    if pos_pass:
        pos_score = min(5.0, dd_pct_from_loss(loss) / 8.0)
    layers: dict[str, Any] = {
        "position": {
            "pass": pos_pass,
            "hard": True,
            "score": pos_score,
            "reason": (
                f"loss={loss:.1f}% rounds={rounds}/{max_rounds} rem={remainder:.0f}"
                + (f" band={band_why}" if not band_ok else "")
            ),
        }
    }

    # TA + reclaim (60d replay: free-fall DCA destroys; reclaim is quality gate)
    free_fall = bool(cand.get("free_fall"))
    reclaim_ok = cand.get("reclaim_ok")
    ta_hard_fail = False
    ta_score = 2.0
    ta_reasons = []
    if structure_ok is False or free_fall:
        ta_hard_fail = True
        ta_reasons.append("structure_broken" if structure_ok is False else "free_fall")
    # Heavy path expects reclaim when explicitly provided (None = unknown, soft)
    if reclaim_ok is False:
        ta_hard_fail = True
        ta_reasons.append("no_reclaim")
    if reclaim_ok is True:
        ta_score += 1.5
        ta_reasons.append("reclaim_ok")
    if rsi is not None:
        try:
            r = float(rsi)
            if r <= 30:
                ta_score += 2.0
            elif r <= 40:
                ta_score += 1.0
            elif r >= 55 and loss < -10:
                ta_score -= 1.0
                ta_reasons.append("rsi_not_oversold")
        except (TypeError, ValueError):
            pass
    if atr is not None:
        try:
            a = float(atr)
            if a >= 2.0:
                ta_score += 0.5
        except (TypeError, ValueError):
            pass
    layers["ta"] = {
        "pass": not ta_hard_fail,
        "hard": bool(ta_hard_fail),
        "score": max(0.0, ta_score),
        "reason": ",".join(ta_reasons) or "ta_ok",
    }

    # Funding
    fund_score = 2.0
    if funding is not None:
        try:
            f = float(funding)
            if f > 0.05:
                fund_score = 0.5
            elif f < -0.01:
                fund_score = 3.5
        except (TypeError, ValueError):
            pass
    layers["funding"] = {
        "pass": True,
        "hard": False,
        "score": fund_score,
        "reason": f"funding={funding}",
    }

    # Facts / unlock
    layers["facts"] = {
        "pass": not unlock_risk,
        "hard": True,
        "score": 0.0 if unlock_risk else 3.0,
        "reason": "unlock_or_hard_neg" if unlock_risk else "facts_ok",
    }

    # Social
    layers["social"] = {
        "pass": not social_block,
        "hard": bool(social_block),
        "score": 0.0 if social_block else 2.0,
        "reason": "block_buys" if social_block else "social_ok",
    }

    # Memory
    mem_score = 2.0
    mem_hard = False
    if entry_bias == "soft_block":
        mem_score = 0.5
    elif entry_bias == "prefer":
        mem_score = 4.0
    layers["memory"] = {
        "pass": not mem_hard,
        "hard": mem_hard,
        "score": mem_score,
        "reason": f"entry_bias={entry_bias}",
    }

    # Portfolio / cash
    layers["portfolio"] = {
        "pass": spendable > 0 or spendable == 0,  # soft; size engine gates
        "hard": False,
        "score": 3.0 if spendable >= 200 else 1.0,
        "reason": f"spendable_dca={spendable:.0f}",
    }

    return layers


def analyze_candidate(cand: dict[str, Any], cash: dict[str, Any] | None = None) -> dict[str, Any]:
    layers = build_layers_from_snapshot(cand, cash)
    score, hard_fails, detail = score_checklist(layers)
    return {
        "score": score,
        "hard_fail": hard_fails,
        "checklist": detail,
        "heavy_ok": score > 0 and not hard_fails,
    }
