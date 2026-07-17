"""Rule-based reflection → Lessons + profile bias tweaks."""

from __future__ import annotations

import hashlib
from collections import defaultdict

from intelligence.memory.embeddings import embed_text
from intelligence.memory.models import Lesson, utc_now_iso
from intelligence.memory.store import MemoryStore, resolve_memory_scope
from logger import log


def reflect(
    store: MemoryStore | None = None,
    *,
    tenant_id: str = "default",
    ledger_scope: str | None = None,
    min_samples: int = 3,
) -> dict[str, int]:
    store = store or MemoryStore()
    trades = store.list_trades(tenant_id=tenant_id, limit=500)
    events = store.list_events(limit=100)
    lessons = 0
    profile_updates = 0
    default_scope = resolve_memory_scope(ledger_scope)

    # Aggregate by symbol
    by_sym: dict[str, list] = defaultdict(list)
    for t in trades:
        by_sym[t.symbol].append(t)

    for symbol, tlist in by_sym.items():
        sells = [t for t in tlist if t.direction == "sell" and t.pnl_usdt is not None]
        if len(sells) < min_samples:
            continue
        # Prefer scope from TradeMemory (rebuild stamps demo|live on staging)
        trade_scope = next(
            (t.ledger_scope for t in tlist if getattr(t, "ledger_scope", None)),
            None,
        )
        scope_candidates: list[str] = []
        for sc in (trade_scope, default_scope, "demo", "live", "paper"):
            if sc and sc not in scope_candidates:
                scope_candidates.append(sc)
        pnls = [float(t.pnl_usdt) for t in sells]
        wr = sum(1 for p in pnls if p > 0) / len(pnls)
        total = sum(pnls)
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

        # reinforce profile: try trade scope first, then active env, then common scopes
        prof = None
        for sc in scope_candidates:
            prof = store.get_profile(symbol, ledger_scope=sc, tenant_id=tenant_id)
            if prof:
                break
        if prof and "weak_history" in tags:
            prof.rationale = text[:200]
            if prof.size_bias > 0.7:
                prof.size_bias = 0.7
            # keep profile.ledger_scope so _id stays tenant|scope|symbol
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

    # Pass 2: rebuild already stamps soft_block/size_bias on the active scope (demo on
    # staging). Reinforce those profiles even when the sell-count loop above missed them
    # (e.g. sparse TradeMemory rows) — always use profile.ledger_scope, never live-only.
    for prof in store.list_profiles(tenant_id=tenant_id, limit=200):
        if prof.entry_bias != "soft_block" and "weak" not in (prof.rationale or "").lower():
            continue
        if (prof.sells_30d or 0) < min_samples and len(by_sym.get(prof.symbol, [])) < min_samples:
            continue
        changed = False
        if prof.size_bias > 0.7:
            prof.size_bias = 0.7
            changed = True
        stamp = f"reflect soft_block n={prof.sells_30d or 0}"
        if stamp not in (prof.rationale or ""):
            base = (prof.rationale or "weak history").strip()
            prof.rationale = f"{base} | {stamp}"[:200]
            changed = True
        if changed and store.upsert_profile(prof):
            profile_updates += 1

    log(f"memory reflect: lessons={lessons} profile_updates={profile_updates}", "INFO")
    return {"lessons": lessons, "profile_updates": profile_updates}
