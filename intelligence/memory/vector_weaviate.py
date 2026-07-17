"""Optional Weaviate client — fail-open when WEAVIATE_URL unset or down."""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from intelligence.memory.embeddings import embed_text
from logger import log


def weaviate_url() -> str:
    return (os.environ.get("WEAVIATE_URL") or "").strip().rstrip("/")


def weaviate_enabled() -> bool:
    return bool(weaviate_url())


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
            with urlopen(req, timeout=8) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except Exception as e:
            log(f"weaviate {method} {path}: {e}", "DEBUG")
            return None

    def ready(self) -> bool:
        r = self._req("GET", "/v1/.well-known/ready")
        return r is not None

    def ensure_schema(self) -> None:
        """Best-effort class creation for MarketEvent."""
        if not self.base:
            return
        body = {
            "class": "MemoryEvent",
            "vectorizer": "none",
            "properties": [
                {"name": "event_id", "dataType": ["text"]},
                {"name": "event_type", "dataType": ["text"]},
                {"name": "description", "dataType": ["text"]},
                {"name": "source", "dataType": ["text"]},
                {"name": "impact_score", "dataType": ["number"]},
            ],
        }
        self._req("POST", "/v1/schema", body)

    def upsert_event(
        self,
        event_id: str,
        description: str,
        event_type: str = "news",
        source: str = "",
        impact_score: float = 0.0,
        vector: list[float] | None = None,
    ) -> bool:
        if not self.base:
            return False
        vec = vector or embed_text(f"{event_type} {description}")
        payload = {
            "class": "MemoryEvent",
            "id": _uuid_from_str(event_id),
            "properties": {
                "event_id": event_id,
                "event_type": event_type,
                "description": description[:500],
                "source": source,
                "impact_score": impact_score,
            },
            "vector": vec,
        }
        r = self._req("POST", "/v1/objects", payload)
        return r is not None

    def search_events(self, query: str, *, symbol: str | None = None, k: int = 8) -> list[str]:
        if not self.base:
            return []
        vec = embed_text(query)
        gql = {
            "query": """
            {
              Get {
                MemoryEvent(nearVector: {vector: %s}, limit: %d) {
                  event_id
                  description
                }
              }
            }
            """
            % (json.dumps(vec), int(k))
        }
        # Use REST graphql
        r = self._req("POST", "/v1/graphql", gql)
        if not isinstance(r, dict):
            return []
        try:
            rows = r["data"]["Get"]["MemoryEvent"]
            return [row.get("event_id") for row in rows if row.get("event_id")]
        except Exception:
            return []


def _uuid_from_str(s: str) -> str:
    import hashlib
    import uuid

    h = hashlib.md5(s.encode()).hexdigest()
    return str(uuid.UUID(h))
