"""RAG kill-switches and defaults (memory.rag.*). Never touches ledger."""

from __future__ import annotations

import os
from typing import Any


def _raw_config(config: dict | None = None) -> dict:
    if config is not None:
        return config
    try:
        from core.config import get_bot_config

        return get_bot_config().raw or {}
    except Exception:
        return {}


def rag_config(config: dict | None = None) -> dict[str, Any]:
    raw = _raw_config(config)
    mem = (raw.get("memory") or {}) if isinstance(raw, dict) else {}
    rag = dict(mem.get("rag") or {})
    env_off = os.environ.get("HERMES_RAG", "").strip().lower() in ("0", "false", "no", "off")
    env_on = os.environ.get("HERMES_RAG", "").strip().lower() in ("1", "true", "yes", "on")
    enabled = bool(rag.get("enabled", True))
    if env_off:
        enabled = False
    elif env_on:
        enabled = True
    use_wv = rag.get("use_weaviate_rag")
    if use_wv is None:
        use_wv = True  # prefer Weaviate when URL set; still fail-open
    return {
        "enabled": enabled,
        "embedding_backend": str(
            os.environ.get("MEMORY_EMBEDDING_BACKEND")
            or rag.get("embedding_backend")
            or "hash"
        ).lower(),
        "top_k": int(rag.get("top_k", 5) or 5),
        "mongo_scan_limit": int(rag.get("mongo_scan_limit", 500) or 500),
        "index_on_cycle": bool(rag.get("index_on_cycle", True)),
        "use_bus": bool(rag.get("use_bus", False)),
        "use_weaviate_rag": bool(use_wv),
        "index_market_context": bool(rag.get("index_market_context", False)),
        # Shadow: attach top-k RAG hits to decision audit only (never changes actions)
        "enrich_decision_audit": bool(rag.get("enrich_decision_audit", True)),
        "prefer_minilm": bool(rag.get("prefer_minilm", True)),
        "max_prompt_chars": int(rag.get("max_prompt_chars", 6000) or 6000),
        # Index breadth — keep coin-facts/portfolio visible next to RSS flood
        "event_index_limit": int(rag.get("event_index_limit", 200) or 200),
        "trade_index_limit": int(rag.get("trade_index_limit", 80) or 80),
        "lesson_index_limit": int(rag.get("lesson_index_limit", 40) or 40),
    }


def rag_enabled(config: dict | None = None) -> bool:
    return bool(rag_config(config).get("enabled"))
