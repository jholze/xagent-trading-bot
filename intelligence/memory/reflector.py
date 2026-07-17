"""Rule-based reflection → Lessons + profile bias tweaks."""

from __future__ import annotations

import hashlib
from collections import defaultdict

from intelligence.memory.embeddings import embed_text
from intelligence.memory.models import Lesson, utc_now_iso
from intelligence.memory.store import MemoryStore
from logger import log


def reflect(
    store: MemoryStore | None = None,
    *,
    tenant_id: str = "default",
    min_samples: int = 3,
) -> dict[str, int]:
    store = store or MemoryStore()
    trades = store.list_trades(tenant_id=tenant_id, limit=500)
    events = store.list_events(limit=100)
    lessons = 0
    profile_updates = 0

    # Aggregate by symbol
    by_sym: dict[str, list] = defaultdict(list)
    for t in trades:
        by_sym[t.symbol].append(t)

    for symbol, tlist in by_sym.items():
        sells = [t for t in tlist if t.direction == "sell" and t.pnl_usdt is not None]
        if len(sells) < min_samples:
            continue
        pnls = [float(t.pnl_usdt) for t in sells]
        wr = sum(1 for p in pnls if p > 0) / len(pnls)
        total = sum(pnls)
        # negative events near losses
        loss_trades = [t for t in sells if float(t.pnl_usdt or 0) < 0]
        neg_events = [e for e in events if e.impact_score < -0.3]
        text = ""
        conf = 0.45
        tags = [symbol.split("/")[0].lower(), "history"]
        if wr < 0.4 and total < 0:
            text = (
                f"{symbol}: weak live history (win_rate={wr:.0%}, pnl={total:.1f} USDT, n={len(sells)}). "
                "Prefer smaller size and avoid chasing DCA."
            )
            conf = 0.55 + (0.1 if neg_events else 0)
            tags.append("weak_history")
        elif wr >= 0.55 and total > 0:
            text = (
                f"{symbol}: solid live history (win_rate={wr:.0%}, pnl={total:.1f} USDT, n={len(sells)})."
            )
            conf = 0.5
            tags.append("strong_history")
        else:
            continue

        lid = hashlib.sha256(f"{symbol}|{text[:80]}".encode()).hexdigest()[:16]
        lesson = Lesson(
            lesson_id=f"les_{lid}",
            text=text,
            confidence=conf,
            tags=tags,
            symbols=[symbol],
            sample_n=len(sells),
            validated=len(sells) >= 5,
            created_at=utc_now_iso(),
            source="reflector",
            embedding=embed_text(text),
            tenant_id=tenant_id,
        )
        if store.upsert_lesson(lesson):
            lessons += 1

        # reinforce profile rationale if exists
        prof = store.get_profile(symbol, tenant_id=tenant_id)
        if prof and "weak_history" in tags:
            prof.rationale = text[:200]
            if prof.size_bias > 0.7:
                prof.size_bias = 0.7
            if store.upsert_profile(prof):
                profile_updates += 1

    # Global lesson from many RISK_OFF events
    risk_off = [e for e in events if e.event_type == "regime_change" and "RISK_OFF" in e.description]
    if len(risk_off) >= 3:
        text = (
            f"Market saw {len(risk_off)} recent RISK_OFF regime events — "
            "global fusion size cuts apply; avoid aggressive new entries."
        )
        lid = hashlib.sha256(text.encode()).hexdigest()[:16]
        if store.upsert_lesson(
            Lesson(
                lesson_id=f"les_{lid}",
                text=text,
                confidence=0.6,
                tags=["regime", "risk_off"],
                symbols=["BTC/USDT"],
                sample_n=len(risk_off),
                embedding=embed_text(text),
                tenant_id=tenant_id,
            )
        ):
            lessons += 1

    log(f"memory reflect: lessons={lessons} profile_updates={profile_updates}", "INFO")
    return {"lessons": lessons, "profile_updates": profile_updates}
