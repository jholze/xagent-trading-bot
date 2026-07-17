"""Optional Weaviate client — fail-open when WEAVIATE_URL unset or down.

Collections: MemoryEvent, MemoryCoinProfile, MemoryTrade, MemoryLesson.
BYO vectors (Hermes hash/MiniLM); vectorizer=none.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.request import Request, urlopen

from intelligence.memory.embeddings import embed_text
from logger import log

# GraphQL class names (Weaviate class must start with uppercase)
CLASS_EVENT = "MemoryEvent"
CLASS_PROFILE = "MemoryCoinProfile"
CLASS_TRADE = "MemoryTrade"
CLASS_LESSON = "MemoryLesson"


def weaviate_url() -> str:
    return (os.environ.get("WEAVIATE_URL") or "").strip().rstrip("/")


def weaviate_enabled() -> bool:
    return bool(weaviate_url())


def _uuid_from_str(s: str) -> str:
    import hashlib
    import uuid

    h = hashlib.md5(s.encode()).hexdigest()
    return str(uuid.UUID(h))


class WeaviateIndex:
    """Minimal REST client for Weaviate v1.x objects + nearVector search."""

    def __init__(self, base_url: str | None = None):
        self.base = (base_url or weaviate_url()).rstrip("/")

    def _req(self, method: str, path: str, body: dict | None = None) -> dict | list | None:
        if not self.base:
            return None
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = Request(
            f"{self.base}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urlopen(req, timeout=12) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except Exception as e:
            log(f"weaviate {method} {path}: {e}", "DEBUG")
            return None

    def ready(self) -> bool:
        r = self._req("GET", "/v1/.well-known/ready")
        return r is not None

    def ensure_schema(self) -> None:
        """Best-effort class creation for all memory collections."""
        if not self.base:
            return
        schemas = [
            {
                "class": CLASS_EVENT,
                "vectorizer": "none",
                "properties": [
                    {"name": "event_id", "dataType": ["text"]},
                    {"name": "event_type", "dataType": ["text"]},
                    {"name": "description", "dataType": ["text"]},
                    {"name": "source", "dataType": ["text"]},
                    {"name": "symbols", "dataType": ["text[]"]},
                    {"name": "impact_score", "dataType": ["number"]},
                    {"name": "timestamp", "dataType": ["text"]},
                ],
            },
            {
                "class": CLASS_PROFILE,
                "vectorizer": "none",
                "properties": [
                    {"name": "symbol", "dataType": ["text"]},
                    {"name": "ledger_scope", "dataType": ["text"]},
                    {"name": "risk_score", "dataType": ["number"]},
                    {"name": "size_bias", "dataType": ["number"]},
                    {"name": "entry_bias", "dataType": ["text"]},
                    {"name": "rationale", "dataType": ["text"]},
                    {"name": "as_of", "dataType": ["text"]},
                ],
            },
            {
                "class": CLASS_TRADE,
                "vectorizer": "none",
                "properties": [
                    {"name": "trade_id", "dataType": ["text"]},
                    {"name": "symbol", "dataType": ["text"]},
                    {"name": "outcome", "dataType": ["text"]},
                    {"name": "source", "dataType": ["text"]},
                    {"name": "pnl_usdt", "dataType": ["number"]},
                    {"name": "reason", "dataType": ["text"]},
                ],
            },
            {
                "class": CLASS_LESSON,
                "vectorizer": "none",
                "properties": [
                    {"name": "lesson_id", "dataType": ["text"]},
                    {"name": "text", "dataType": ["text"]},
                    {"name": "confidence", "dataType": ["number"]},
                    {"name": "tags", "dataType": ["text[]"]},
                    {"name": "symbols", "dataType": ["text[]"]},
                    {"name": "validated", "dataType": ["boolean"]},
                ],
            },
        ]
        for body in schemas:
            self._req("POST", "/v1/schema", body)

    def _upsert(
        self,
        class_name: str,
        object_id: str,
        properties: dict[str, Any],
        vector: list[float],
    ) -> bool:
        if not self.base:
            return False
        payload = {
            "class": class_name,
            "id": _uuid_from_str(object_id),
            "properties": properties,
            "vector": vector,
        }
        # Prefer PUT for idempotent re-index; fall back to POST
        r = self._req("PUT", f"/v1/objects/{class_name}/{_uuid_from_str(object_id)}", payload)
        if r is not None:
            return True
        r = self._req("POST", "/v1/objects", payload)
        return r is not None

    def upsert_event(
        self,
        event_id: str,
        description: str,
        event_type: str = "news",
        source: str = "",
        impact_score: float = 0.0,
        symbols: list[str] | None = None,
        timestamp: str = "",
        vector: list[float] | None = None,
    ) -> bool:
        vec = vector or embed_text(f"{event_type} {description}")
        return self._upsert(
            CLASS_EVENT,
            event_id,
            {
                "event_id": event_id,
                "event_type": event_type,
                "description": (description or "")[:500],
                "source": source or "",
                "symbols": list(symbols or []),
                "impact_score": float(impact_score),
                "timestamp": timestamp or "",
            },
            vec,
        )

    def upsert_profile(
        self,
        symbol: str,
        rationale: str = "",
        size_bias: float = 1.0,
        risk_score: float = 0.5,
        entry_bias: str = "neutral",
        ledger_scope: str = "live",
        as_of: str = "",
        vector: list[float] | None = None,
    ) -> bool:
        text = f"{symbol} bias={size_bias} entry={entry_bias} {rationale}"
        vec = vector or embed_text(text)
        return self._upsert(
            CLASS_PROFILE,
            f"profile:{ledger_scope}:{symbol}",
            {
                "symbol": symbol,
                "ledger_scope": ledger_scope,
                "risk_score": float(risk_score),
                "size_bias": float(size_bias),
                "entry_bias": entry_bias or "neutral",
                "rationale": (rationale or "")[:400],
                "as_of": as_of or "",
            },
            vec,
        )

    def upsert_trade(
        self,
        trade_id: str,
        symbol: str,
        outcome: str = "",
        source: str = "",
        pnl_usdt: float = 0.0,
        reason: str = "",
        vector: list[float] | None = None,
    ) -> bool:
        text = f"{symbol} {outcome} {reason} pnl={pnl_usdt}"
        vec = vector or embed_text(text)
        return self._upsert(
            CLASS_TRADE,
            trade_id,
            {
                "trade_id": trade_id,
                "symbol": symbol,
                "outcome": outcome or "",
                "source": source or "",
                "pnl_usdt": float(pnl_usdt or 0),
                "reason": (reason or "")[:300],
            },
            vec,
        )

    def upsert_lesson(
        self,
        lesson_id: str,
        text: str,
        confidence: float = 0.5,
        tags: list[str] | None = None,
        symbols: list[str] | None = None,
        validated: bool = False,
        vector: list[float] | None = None,
    ) -> bool:
        vec = vector or embed_text(text or "")
        return self._upsert(
            CLASS_LESSON,
            lesson_id,
            {
                "lesson_id": lesson_id,
                "text": (text or "")[:500],
                "confidence": float(confidence),
                "tags": list(tags or []),
                "symbols": list(symbols or []),
                "validated": bool(validated),
            },
            vec,
        )

    def _near_vector_search(
        self,
        class_name: str,
        query: str,
        *,
        fields: list[str],
        k: int = 8,
        where_clause: str = "",
    ) -> list[dict[str, Any]]:
        if not self.base:
            return []
        vec = embed_text(query)
        field_block = "\n".join(fields)
        where = f", where: {where_clause}" if where_clause else ""
        gql = {
            "query": """
            {
              Get {
                %s(nearVector: {vector: %s}, limit: %d%s) {
                  %s
                }
              }
            }
            """
            % (class_name, json.dumps(vec), int(k), where, field_block)
        }
        r = self._req("POST", "/v1/graphql", gql)
        if not isinstance(r, dict):
            return []
        try:
            rows = r.get("data", {}).get("Get", {}).get(class_name) or []
            return [row for row in rows if isinstance(row, dict)]
        except Exception:
            return []

    def search_events(
        self,
        query: str,
        *,
        symbol: str | None = None,
        event_type: str | None = None,
        k: int = 8,
    ) -> list[str]:
        where = ""
        operands = []
        if symbol:
            base = symbol.split("/")[0].upper()
            # ContainsAny on symbols text array
            operands.append(
                '{path: ["symbols"], operator: ContainsAny, valueTextArray: ["%s", "%s"]}'
                % (base, f"{base}/USDT")
            )
        if event_type:
            operands.append(
                '{path: ["event_type"], operator: Equal, valueText: "%s"}' % event_type
            )
        if len(operands) == 1:
            where = operands[0]
        elif len(operands) > 1:
            where = "{operator: And, operands: [%s]}" % ", ".join(operands)
        rows = self._near_vector_search(
            CLASS_EVENT,
            query,
            fields=["event_id", "description", "event_type"],
            k=k,
            where_clause=where,
        )
        return [row["event_id"] for row in rows if row.get("event_id")]

    def search_similar_profiles(
        self,
        query: str,
        *,
        k: int = 8,
        ledger_scope: str | None = None,
    ) -> list[dict[str, Any]]:
        where = ""
        if ledger_scope:
            where = (
                '{path: ["ledger_scope"], operator: Equal, valueText: "%s"}' % ledger_scope
            )
        return self._near_vector_search(
            CLASS_PROFILE,
            query,
            fields=["symbol", "size_bias", "entry_bias", "rationale", "risk_score"],
            k=k,
            where_clause=where,
        )

    def search_lessons(self, query: str, *, k: int = 5) -> list[dict[str, Any]]:
        return self._near_vector_search(
            CLASS_LESSON,
            query,
            fields=["lesson_id", "text", "confidence", "symbols"],
            k=k,
        )
