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