"""AI4 / W6: local soak metrics from persisted WQE scores (no multi-day requirement)."""

from __future__ import annotations

from typing import Any

from services.watchlist_quality.store import load_quality_scores


def compute_ai_agreement_metrics(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compare det quality vs AI-fused score and stance distribution.

    Pure analysis of score artifact — used for local soak / operator reports.
    """
    data = payload if isinstance(payload, dict) else load_quality_scores()
    coins = [c for c in (data.get("coins") or []) if isinstance(c, dict)]
    n = len(coins)
    if n == 0:
        return {
            "n": 0,
            "ai_ok": 0,
            "ai_error": 0,
            "ai_skipped": 0,
            "mean_abs_delta": None,
            "demote_n": 0,
            "boost_n": 0,
            "avoid_new_n": 0,
            "det_gt_ai_n": 0,
            "ai_gt_det_n": 0,
        }

    ai_ok = ai_err = ai_skip = 0
    deltas: list[float] = []
    demote = boost = avoid = 0
    det_gt = ai_gt = 0

    for c in coins:
        ai = c.get("ai") if isinstance(c.get("ai"), dict) else {}
        src = str(ai.get("source") or "")
        if src == "ok":
            ai_ok += 1
        elif src == "error":
            ai_err += 1
        else:
            ai_skip += 1

        stance = str(ai.get("stance") or "")
        if stance == "demote":
            demote += 1
        elif stance == "boost":
            boost += 1
        elif stance == "avoid_new":
            avoid += 1

        try:
            q = float(c.get("quality_score") or 0)
        except (TypeError, ValueError):
            q = 0.0
        try:
            qa = c.get("quality_shadow_ai")
            qa_f = float(qa) if qa is not None else q
        except (TypeError, ValueError):
            qa_f = q
        d = abs(qa_f - q)
        deltas.append(d)
        if q > qa_f + 1e-9:
            det_gt += 1
        elif qa_f > q + 1e-9:
            ai_gt += 1

    mean_delta = sum(deltas) / len(deltas) if deltas else None
    return {
        "n": n,
        "ai_ok": ai_ok,
        "ai_error": ai_err,
        "ai_skipped": ai_skip,
        "ai_success_rate": round(ai_ok / n, 4) if n else None,
        "mean_abs_delta": round(mean_delta, 4) if mean_delta is not None else None,
        "demote_n": demote,
        "boost_n": boost,
        "avoid_new_n": avoid,
        "det_gt_ai_n": det_gt,
        "ai_gt_det_n": ai_gt,
        "mode": data.get("mode"),
        "updated_at": data.get("updated_at"),
    }


def format_soak_report(metrics: dict[str, Any] | None = None) -> str:
    m = metrics if isinstance(metrics, dict) else compute_ai_agreement_metrics()
    if not m.get("n"):
        return "WQE soak: no scores yet (run shadow mode first)."
    return (
        f"WQE soak n={m['n']} mode={m.get('mode')} "
        f"ai_ok={m.get('ai_ok')} err={m.get('ai_error')} skip={m.get('ai_skipped')} "
        f"success={m.get('ai_success_rate')} "
        f"|Δ|={m.get('mean_abs_delta')} "
        f"demote={m.get('demote_n')} boost={m.get('boost_n')} avoid_new={m.get('avoid_new_n')} "
        f"as_of={m.get('updated_at')}"
    )
