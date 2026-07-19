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

    # Pass 2: rebuild stamps soft_block/size_bias on the active scope (demo on staging).
    # Always use each profile's own ledger_scope (never hardcode live).
    profiles = store.list_profiles(tenant_id=tenant_id, limit=200)
    if not profiles:
        # tenant filter miss (legacy docs): try unscoped listing via known trade symbols
        for sym in list(by_sym.keys())[:50]:
            for sc in (default_scope, "demo", "live", "paper"):
                p = store.get_profile(sym, ledger_scope=sc, tenant_id=tenant_id)
                if p:
                    profiles.append(p)
                    break
    for prof in profiles:
        weak = (
            prof.entry_bias == "soft_block"
            or "weak" in (prof.rationale or "").lower()
            or float(prof.size_bias or 1.0) < 0.95
        )
        if not weak:
            continue
        enough = (prof.sells_30d or 0) >= 1 or len(by_sym.get(prof.symbol, [])) >= 1
        if not enough:
            continue
        changed = False
        if prof.size_bias > 0.7:
            prof.size_bias = 0.7
            changed = True
        stamp = f"reflect scope={prof.ledger_scope} n={prof.sells_30d or 0}"
        if stamp not in (prof.rationale or ""):
            base = (prof.rationale or "history").strip()
            prof.rationale = f"{base} | {stamp}"[:200]
            changed = True
        if changed and store.upsert_profile(prof):
            profile_updates += 1

        # Explicit soft_block lesson/event so RAG + similar_events can retrieve it.
        # Language follows evidence (gross_loss vs weak_history) — no fake sensor stamps.
        rat = (prof.rationale or "").lower()
        is_soft = prof.entry_bias == "soft_block" or "soft_block" in rat
        is_gross = "gross_loss" in rat or "gross loss" in rat
        if is_soft or is_gross:
            feat = getattr(prof, "features", None) or {}
            if not isinstance(feat, dict):
                feat = {}
            scope_lbl = str(feat.get("soft_block_scope") or "sensor_only")
            loss_src = str(feat.get("last_loss_source") or "").lower()
            sensorish = (
                "sensor" in scope_lbl
                or "sensor" in loss_src
                or "sensor" in rat
            )
            head_bits = [prof.symbol + ":"]
            if sensorish:
                head_bits.append("sensor entry")
            if is_gross:
                head_bits.append("gross loss")
            head_bits.append("soft_block rebuy cooloff")
            text = (
                f"{' '.join(head_bits)}. "
                f"entry_bias={prof.entry_bias or 'soft_block'} size_bias={prof.size_bias} "
                f"scope={scope_lbl}. "
                f"{(prof.rationale or '')[:120]} "
                "Avoid rebuy until cooloff TTL; prefer smaller size."
            ).strip()
            tags = [
                "soft_block",
                (prof.symbol or "").split("/")[0].lower(),
            ]
            if is_gross:
                tags.append("gross_loss")
            if sensorish:
                tags.append("sensor")
            lid = hashlib.sha256(
                f"soft_block|{prof.symbol}|{prof.ledger_scope}".encode()
            ).hexdigest()[:16]
            if store.upsert_lesson(
                Lesson(
                    lesson_id=f"les_sb_{lid}",
                    text=text[:500],
                    confidence=0.65,
                    tags=tags,
                    symbols=[prof.symbol],
                    sample_n=int(prof.sells_30d or 0) or 1,
                    embedding=embed_text(text),
                    tenant_id=tenant_id,
                    source="reflector_soft_block",
                )
            ):
                lessons += 1
            try:
                from intelligence.memory.embeddings import embed_event
                from intelligence.memory.event_ingest import make_event_id
                from intelligence.memory.models import MarketEvent

                eid = make_event_id(
                    "reflector",
                    f"soft_block|{prof.symbol}|{prof.ledger_scope}",
                )
                store.upsert_event(
                    MarketEvent(
                        event_id=eid,
                        timestamp=utc_now_iso(),
                        event_type="soft_block",
                        symbols=[prof.symbol],
                        impact_score=-0.6,
                        description=text[:500],
                        source="reflector",
                        metadata={
                            "entry_bias": prof.entry_bias or "soft_block",
                            "size_bias": prof.size_bias,
                            "ledger_scope": prof.ledger_scope,
                            "gross_loss": is_gross,
                            "sensorish": sensorish,
                        },
                        embedding=embed_event(text, event_type="soft_block"),
                    )
                )
            except Exception as e:
                log(f"memory reflect soft_block event: {e}", "DEBUG")

    log(
        f"memory reflect: lessons={lessons} profile_updates={profile_updates} "
        f"profiles_seen={len(profiles)} scope={default_scope}",
        "INFO",
    )
    return {
        "lessons": lessons,
        "profile_updates": profile_updates,
        "profiles_seen": len(profiles),
        "scope": default_scope,
    }
