"""RAG enrich for slot eviction keep scores — fail-open, no ledger writes."""

from __future__ import annotations

from typing import Any, Callable

from risk.slot_eviction import apply_rag_keep, clamp01, slot_eviction_section


def hold_query(symbol: str) -> str:
    return (
        f"{symbol}: hold quality trade outcomes wins losses soft_block lessons "
        f"structure_risk sensor entry exit"
    )


def enrich_keeps_with_rag(
    symbols: list[str],
    keep_profile: dict[str, float],
    *,
    risk_config: dict | None = None,
    retrieve_fn: Callable[[str, str], list[Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Return per-symbol {keep_profile, keep_rag, evidence_delta, hit_count, error}.

    retrieve_fn(symbol, query) -> hits list. If None or raises → fail-open.
    """
    cfg = slot_eviction_section(risk_config)
    rag = cfg.get("rag") if isinstance(cfg.get("rag"), dict) else {}
    mode = str(rag.get("mode") or "off").lower()
    weight = float(rag.get("evidence_weight", 0.25) or 0.25)
    max_n = int(rag.get("max_candidates_for_rag", 5) or 5)

    out: dict[str, dict[str, Any]] = {}
    if mode in ("off", "", "false", "0"):
        for sym in symbols:
            kp = float(keep_profile.get(sym, 0.5))
            out[sym] = {
                "keep_profile": kp,
                "keep_rag": kp,
                "evidence_delta": 0.0,
                "hit_count": 0,
                "error": False,
            }
        return out

    for i, sym in enumerate(symbols):
        kp = clamp01(float(keep_profile.get(sym, 0.5)))
        if i >= max_n and sym not in keep_profile:
            out[sym] = {
                "keep_profile": kp,
                "keep_rag": kp,
                "evidence_delta": 0.0,
                "hit_count": 0,
                "error": False,
            }
            continue
        hits: list[Any] = []
        err = False
        if retrieve_fn is not None:
            try:
                hits = list(retrieve_fn(sym, hold_query(sym)) or [])
            except Exception:
                hits = []
                err = True
        kr, ed = apply_rag_keep(kp, hits, evidence_weight=weight, retrieve_error=err)
        out[sym] = {
            "keep_profile": kp,
            "keep_rag": kr,
            "evidence_delta": ed,
            "hit_count": len(hits),
            "error": err,
        }
    return out


def default_retrieve_fn(config_raw: dict | None = None):
    """Build a retrieve_fn using RagRetriever; fail-open empty list."""

    def _retrieve(symbol: str, query: str) -> list[Any]:
        try:
            from hermes.memory.rag_retriever import RagRetriever
            from intelligence.memory.rag_config import rag_enabled

            if not rag_enabled(config_raw):
                return []
            r = RagRetriever(config=config_raw)
            return list(r.retrieve(query, top_k=5, filters={"symbol": symbol}) or [])
        except Exception:
            return []

    return _retrieve
