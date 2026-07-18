"""Stream / queue payload schemas (in-memory and Redis-compatible)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
import uuid

PRIORITY_URGENT = 0
PRIORITY_COMMAND = 1
PRIORITY_CYCLE = 2
PRIORITY_DEBUG = 3


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class NotificationMessage:
    text: str
    priority: int = PRIORITY_URGENT
    chat_id: str | int | None = None
    reply_markup: Any = None
    parse_mode: str = "HTML"
    kind: str = "text"
    source: str = "monolith"
    enqueued_at: str = field(default_factory=utc_now_iso)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])


@dataclass
class RagQuery:
    """Bus contract: request retrieval (epic #72 C3). JSON-serializable."""

    query: str
    top_k: int = 5
    filters: dict[str, Any] = field(default_factory=dict)
    correlation_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    source: str = "hermes"
    enqueued_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "top_k": int(self.top_k),
            "filters": dict(self.filters or {}),
            "correlation_id": self.correlation_id,
            "source": self.source,
            "enqueued_at": self.enqueued_at,
        }


@dataclass
class RagResult:
    """Bus contract: retrieval response (hits are plain dicts)."""

    correlation_id: str
    hits: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    prompt: str = ""
    enqueued_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "correlation_id": self.correlation_id,
            "hits": list(self.hits or []),
            "error": self.error or "",
            "prompt": (self.prompt or "")[:8000],
            "enqueued_at": self.enqueued_at,
        }