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

    # Facts / unlock (from deep context or bot snapshot)
    fact_summary = str(cand.get("fact_summary") or "")[:80]
    fact_n = int(cand.get("fact_event_count") or 0)
    layers["facts"] = {
        "pass": not unlock_risk,
        "hard": True,
        "score": 0.0 if unlock_risk else (3.0 if fact_n == 0 else min(4.0, 2.5 + 0.2 * fact_n)),
        "reason": (
            "unlock_or_hard_neg"
            if unlock_risk
            else (f"facts_ok n={fact_n}" + (f" {fact_summary}" if fact_summary else ""))
        ),
    }

    # Social / block_buys (fusion or explicit)
    layers["social"] = {
        "pass": not social_block,
        "hard": bool(social_block),
        "score": 0.0 if social_block else 2.0,
        "reason": "block_buys" if social_block else "social_ok",
    }

    # Memory — entry_bias + RAG hits + DCA lessons (deep path fills these)
    mem_score = 2.0
    mem_hard = False
    mem_bits = [f"entry_bias={entry_bias}"]
    if entry_bias == "soft_block":
        mem_score = 0.5
        mem_bits.append("soft_block")
    elif entry_bias == "prefer":
        mem_score = 4.0
        mem_bits.append("prefer")
    # RAG hits and DCA lessons count by direction (lesson_polarity): a warning
    # ("Avoid chasing DCA", "weak live history", "sells often lost") lowers
    # the score, a measured win raises it, unknown direction is neutral.
    # Before this fix every hit and every lesson added score (sign error).
    def _int(key: str) -> int:
        try:
            return max(0, int(cand.get(key) or 0))
        except (TypeError, ValueError):
            return 0

    rag_hits = _int("rag_hit_count")
    rag_neg = _int("rag_negative_count")
    rag_pos = _int("rag_positive_count")
    if rag_hits:
        rag_neg = min(rag_hits, rag_neg)
        rag_pos = min(rag_hits - rag_neg, rag_pos)
    if rag_hits > 0 or rag_neg or rag_pos:
        mem_bits.append(f"rag_hits={rag_hits}")
    if rag_neg:
        mem_score -= min(1.5, 0.4 * rag_neg)
        mem_bits.append(f"rag_neg={rag_neg}")
    if rag_pos:
        mem_score += min(1.0, 0.3 * rag_pos)
        mem_bits.append(f"rag_pos={rag_pos}")
    lesson_n = _int("dca_lesson_count")
    les_neg = _int("dca_lesson_negative_count")
    les_pos = _int("dca_lesson_positive_count")
    if lesson_n:
        les_neg = min(lesson_n, les_neg)
        les_pos = min(lesson_n - les_neg, les_pos)
    if lesson_n > 0 or les_neg or les_pos:
        mem_bits.append(f"dca_lessons={max(lesson_n, les_neg + les_pos)}")
    if les_neg:
        mem_score -= min(1.5, 0.5 * les_neg)
        mem_bits.append(f"lessons_neg={les_neg}")
    if les_pos:
        mem_score += min(1.0, 0.25 * les_pos)
        mem_bits.append(f"lessons_pos={les_pos}")
    if lesson_n > 0 or les_neg or les_pos:
        summary = str(cand.get("dca_lesson_summary") or "").strip()
        if summary:
            mem_bits.append(summary[:40])
    mem_score = max(0.0, min(5.0, mem_score))
    layers["memory"] = {
        "pass": not mem_hard,
        "hard": mem_hard,
        "score": mem_score,
        "reason": ",".join(mem_bits),
    }

    # Portfolio / cash
    layers["portfolio"] = {
        "pass": spendable > 0 or spendable == 0,  # soft; size engine gates
        "hard": False,
        "score": 3.0 if spendable >= 200 else 1.0,
        "reason": f"spendable_dca={spendable:.0f}",
    }

    # News / evidence brief (deep path)
    news_n = 0
    try:
        news_n = int(cand.get("news_count") or 0)
    except (TypeError, ValueError):
        news_n = 0
    hard_news = bool(cand.get("hard_news"))
    news_score = 2.0
    news_pass = True
    news_hard = False
    news_reason = "no_news"
    if hard_news:
        news_score = 0.0
        news_pass = False
        news_hard = True
        news_reason = "hard_news"
    elif news_n > 0:
        news_score = min(4.0, 2.0 + 0.3 * news_n)
        brief = cand.get("news_brief") or []
        news_reason = f"news_n={news_n}"
        if isinstance(brief, list) and brief:
            news_reason += f" top={str(brief[0])[:50]}"
        if cand.get("facts_fresh"):
            news_reason += ",fresh"
    layers["news"] = {
        "pass": news_pass,
        "hard": news_hard,
        "score": news_score,
        "reason": news_reason,
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
