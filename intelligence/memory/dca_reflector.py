"""DCA policy reflection (#100 D5) — rules first, optional Grok offline.

Reads memory_market_events (event_type=dca_decision) + memory trades.
Writes Lessons only (memory_lessons). Never touches ledger / never changes policy mults.
"""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from intelligence.memory.embeddings import embed_text
from intelligence.memory.models import Lesson, TradeMemory, utc_now_iso
from intelligence.memory.store import MemoryStore
from logger import log


def dca_reflect_config(config_raw: dict | None = None) -> dict[str, Any]:
    """Resolve dca.policy.reflect + sensible defaults."""
    defaults = {
        "enabled": True,
        "min_events": 5,
        "event_limit": 300,
        "trade_limit": 400,
        "outcome_window_hours": 72,
        "loss_usdt_threshold": 50.0,
        "win_usdt_threshold": 20.0,
        "reflect_grok": False,
        "index_rag": True,
    }
    try:
        if config_raw is None:
            from core.config import get_bot_config

            config_raw = get_bot_config().raw
        pol: dict[str, Any] = {}
        strategies = (config_raw or {}).get("strategies") or {}
        candidates: list[dict] = []
        if isinstance(strategies, dict):
            for block in strategies.values():
                if isinstance(block, dict):
                    candidates.append(block)
        # also scan top-level nested strategy packs if any
        for block in candidates:
            dca_cfg = block.get("dca") if isinstance(block.get("dca"), dict) else {}
            p = dca_cfg.get("policy") if isinstance(dca_cfg.get("policy"), dict) else {}
            if p:
                pol = p
                break
        mem = ((config_raw or {}).get("memory") or {}).get("dca_reflect") or {}
        if not isinstance(mem, dict):
            mem = {}
        nested = pol.get("reflect") if isinstance(pol.get("reflect"), dict) else {}
        ref = {**defaults, **nested, **mem}
        if "reflect_grok" in pol:
            ref["reflect_grok"] = bool(pol.get("reflect_grok"))
        return ref
    except Exception:
        return dict(defaults)


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        s = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s[:32] if len(s) > 32 else s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _lesson_id(key: str) -> str:
    return "les_dca_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def derive_dca_lesson_specs(
    events: list[Any],
    sells: list[TradeMemory],
    *,
    min_events: int = 5,
    outcome_window_hours: float = 72,
    loss_usdt_threshold: float = 50.0,
    win_usdt_threshold: float = 20.0,
) -> list[dict[str, Any]]:
    """Pure rules → lesson specs {key, text, conf, tags, symbols, sample_n}.

    events: MarketEvent-like with event_type, description, symbols, metadata, timestamp
    """
    specs: list[dict[str, Any]] = []
    dca_evs = [
        e
        for e in events or []
        if getattr(e, "event_type", "") == "dca_decision"
        or (getattr(e, "metadata", None) or {}).get("kind") == "dca_decision"
    ]
    if not dca_evs:
        return []

    # --- Rule A: global harvest_skip dominance ---
    reason_counter: Counter[str] = Counter()
    mode_counter: Counter[str] = Counter()
    skip_n = 0
    allow_n = 0
    by_sym_skip: dict[str, int] = defaultdict(int)
    by_sym_allow: dict[str, int] = defaultdict(int)
    allow_events: list[Any] = []

    for e in dca_evs:
        meta = getattr(e, "metadata", None) or {}
        codes = meta.get("reason_codes") or []
        if isinstance(codes, str):
            codes = [codes]
        for c in codes:
            reason_counter[str(c)] += 1
        mode = str(meta.get("cash_mode") or "")
        if mode:
            mode_counter[mode] += 1
        syms = list(getattr(e, "symbols", None) or [])
        sym = syms[0] if syms else ""
        is_skip = bool(meta.get("skip"))
        if is_skip:
            skip_n += 1
            if sym:
                by_sym_skip[sym] += 1
        else:
            allow_n += 1
            if sym:
                by_sym_allow[sym] += 1
            allow_events.append(e)

    n = len(dca_evs)
    if reason_counter.get("harvest_skip", 0) >= min_events:
        hs = reason_counter["harvest_skip"]
        specs.append(
            {
                "key": "global|harvest_skip",
                "text": (
                    f"DCA policy: harvest_skip dominated recent decisions "
                    f"({hs}/{n} events). In HARVEST/RISK_OFF prefer no DCA add-ons; "
                    f"wait for STEADY/DEPLOY before averaging down."
                ),
                "confidence": min(0.75, 0.45 + 0.03 * min(hs, 10)),
                "tags": ["dca_policy", "harvest", "risk_off"],
                "symbols": ["BTC/USDT"],  # global market lesson anchor
                "sample_n": hs,
            }
        )

    if reason_counter.get("deploy_boost", 0) >= min_events and allow_n >= min_events:
        db = reason_counter["deploy_boost"]
        specs.append(
            {
                "key": "global|deploy_boost",
                "text": (
                    f"DCA policy: deploy_boost common ({db}/{n}). "
                    f"In DEPLOY/RISK_ON sized add-ons were favored; still respect hard gates."
                ),
                "confidence": 0.5,
                "tags": ["dca_policy", "deploy", "risk_on"],
                "symbols": ["BTC/USDT"],
                "sample_n": db,
            }
        )

    # --- Rule B: per-symbol skip density ---
    for sym, cnt in by_sym_skip.items():
        if cnt >= min_events:
            specs.append(
                {
                    "key": f"{sym}|skip_heavy",
                    "text": (
                        f"{sym}: DCA policy skipped add-ons often ({cnt} recent decisions). "
                        f"Avoid chasing DCA on this pair while harvest/block signals persist."
                    ),
                    "confidence": min(0.7, 0.5 + 0.02 * cnt),
                    "tags": ["dca_policy", "skip", sym.split("/")[0].lower()],
                    "symbols": [sym],
                    "sample_n": cnt,
                }
            )

    # --- Rule C: outcome link allow → later sell ---
    sells = [t for t in (sells or []) if t.direction == "sell" and t.pnl_usdt is not None]
    sells_by_sym: dict[str, list[TradeMemory]] = defaultdict(list)
    for t in sells:
        sells_by_sym[t.symbol].append(t)
    for tlist in sells_by_sym.values():
        tlist.sort(key=lambda x: x.exit_time or x.entry_time or "")

    window = timedelta(hours=float(outcome_window_hours))
    loss_hits: list[tuple[str, float]] = []
    win_hits: list[tuple[str, float]] = []

    for e in allow_events:
        meta = getattr(e, "metadata", None) or {}
        if meta.get("skip"):
            continue
        # shadow allows still count as "would have DCA'd" for learning
        syms = list(getattr(e, "symbols", None) or [])
        if not syms:
            continue
        sym = syms[0]
        et = _parse_ts(getattr(e, "timestamp", "") or "")
        if et is None:
            continue
        end = et + window
        for t in sells_by_sym.get(sym, []):
            tt = _parse_ts(t.exit_time or t.entry_time)
            if tt is None or tt < et or tt > end:
                continue
            pnl = float(t.pnl_usdt or 0)
            if pnl <= -abs(loss_usdt_threshold):
                loss_hits.append((sym, pnl))
            elif pnl >= abs(win_usdt_threshold):
                win_hits.append((sym, pnl))
            break  # first sell in window

    if len(loss_hits) >= max(2, min_events // 2):
        by: dict[str, list[float]] = defaultdict(list)
        for s, p in loss_hits:
            by[s].append(p)
        for sym, pnls in by.items():
            if len(pnls) < 2:
                continue
            avg = sum(pnls) / len(pnls)
            specs.append(
                {
                    "key": f"{sym}|allow_then_loss",
                    "text": (
                        f"{sym}: after DCA-policy ALLOW, subsequent sells often lost "
                        f"(n={len(pnls)}, avg_pnl={avg:.1f} USDT within "
                        f"{int(outcome_window_hours)}h). Prefer smaller mult or skip on weak structure."
                    ),
                    "confidence": min(0.72, 0.52 + 0.04 * len(pnls)),
                    "tags": ["dca_policy", "outcome_loss", sym.split("/")[0].lower()],
                    "symbols": [sym],
                    "sample_n": len(pnls),
                }
            )

    if len(win_hits) >= max(2, min_events // 2):
        by_w: dict[str, list[float]] = defaultdict(list)
        for s, p in win_hits:
            by_w[s].append(p)
        for sym, pnls in by_w.items():
            if len(pnls) < 2:
                continue
            avg = sum(pnls) / len(pnls)
            specs.append(
                {
                    "key": f"{sym}|allow_then_win",
                    "text": (
                        f"{sym}: after DCA-policy ALLOW, later sells were often green "
                        f"(n={len(pnls)}, avg_pnl={avg:.1f} USDT). Measured add-ons can work here."
                    ),
                    "confidence": 0.55,
                    "tags": ["dca_policy", "outcome_win", sym.split("/")[0].lower()],
                    "symbols": [sym],
                    "sample_n": len(pnls),
                }
            )

    return specs


def _optional_grok_summary(specs: list[dict[str, Any]], cfg: dict) -> str | None:
    if not cfg.get("reflect_grok"):
        return None
    if not specs:
        return None
    try:
        from intelligence.llm_client import grok_agent  # type: ignore
    except Exception:
        try:
            from grok_agent import ask_grok as grok_agent  # type: ignore
        except Exception:
            return None
    try:
        blob = "\n".join(f"- {s['text']}" for s in specs[:12])
        prompt = (
            "Fasse die folgenden DCA-Policy-Lessons in max 5 deutschen Stichpunkten zusammen. "
            "Keine Order-Empfehlung, nur Erkenntnisse:\n"
            f"{blob}"
        )
        if callable(grok_agent):
            # ask_grok style
            try:
                out = grok_agent(prompt, temperature=0.2)
            except TypeError:
                out = grok_agent(prompt)
            text = str(out or "").strip()
            return text[:1500] if text and not text.startswith("API-Fehler") else None
    except Exception as e:
        log(f"dca reflect grok skipped: {e}", "DEBUG")
    return None


def reflect_dca_policy(
    store: MemoryStore | None = None,
    *,
    config_raw: dict | None = None,
    tenant_id: str = "default",
) -> dict[str, Any]:
    """Run DCA policy reflection once. Fail-open."""
    cfg = dca_reflect_config(config_raw)
    out: dict[str, Any] = {
        "enabled": bool(cfg.get("enabled")),
        "lessons": 0,
        "events_read": 0,
        "sells_read": 0,
        "specs": 0,
        "grok": False,
    }
    if not cfg.get("enabled", True):
        return out

    store = store or MemoryStore()
    try:
        events = store.list_events(
            event_type="dca_decision",
            limit=int(cfg.get("event_limit") or 300),
        )
        # fallback: untyped filter if store ignores event_type filter edge cases
        if not events:
            events = [
                e
                for e in store.list_events(limit=int(cfg.get("event_limit") or 300))
                if getattr(e, "event_type", "") == "dca_decision"
            ]
        out["events_read"] = len(events)
        sells = [
            t
            for t in store.list_trades(tenant_id=tenant_id, limit=int(cfg.get("trade_limit") or 400))
            if t.direction == "sell"
        ]
        out["sells_read"] = len(sells)

        specs = derive_dca_lesson_specs(
            events,
            sells,
            min_events=int(cfg.get("min_events") or 5),
            outcome_window_hours=float(cfg.get("outcome_window_hours") or 72),
            loss_usdt_threshold=float(cfg.get("loss_usdt_threshold") or 50),
            win_usdt_threshold=float(cfg.get("win_usdt_threshold") or 20),
        )
        out["specs"] = len(specs)

        grok_text = _optional_grok_summary(specs, cfg)
        if grok_text:
            out["grok"] = True
            specs.append(
                {
                    "key": "global|grok_summary",
                    "text": f"DCA policy Grok summary: {grok_text}",
                    "confidence": 0.4,
                    "tags": ["dca_policy", "grok_summary"],
                    "symbols": ["BTC/USDT"],
                    "sample_n": len(specs),
                }
            )

        lessons_n = 0
        for s in specs:
            text = str(s.get("text") or "").strip()
            if not text:
                continue
            lid = _lesson_id(str(s.get("key") or text[:80]))
            lesson = Lesson(
                lesson_id=lid,
                text=text[:2000],
                confidence=float(s.get("confidence") or 0.5),
                tags=list(s.get("tags") or ["dca_policy"]),
                symbols=list(s.get("symbols") or []),
                sample_n=int(s.get("sample_n") or 0),
                validated=int(s.get("sample_n") or 0) >= int(cfg.get("min_events") or 5),
                created_at=utc_now_iso(),
                source="dca_reflector",
                embedding=embed_text(text),
                tenant_id=tenant_id,
            )
            if store.upsert_lesson(lesson):
                lessons_n += 1
                if cfg.get("index_rag", True):
                    try:
                        from hermes.memory.rag_retriever import RagRetriever
                        from intelligence.memory.rag_config import rag_enabled

                        if rag_enabled(config_raw):
                            RagRetriever(config=config_raw).add_to_memory(
                                text,
                                {
                                    "type": "lesson",
                                    "symbol": (lesson.symbols[0] if lesson.symbols else ""),
                                    "source": "dca_reflector",
                                    "source_id": lid,
                                },
                            )
                    except Exception:
                        pass
        out["lessons"] = lessons_n
        log(
            f"dca reflect: events={out['events_read']} sells={out['sells_read']} "
            f"specs={out['specs']} lessons={lessons_n} grok={out['grok']}",
            "INFO",
        )
    except Exception as e:
        log(f"dca reflect failed (fail-open): {e}", "WARNING")
        out["error"] = str(e)[:200]
    return out
