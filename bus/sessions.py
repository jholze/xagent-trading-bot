"""Command session tracking — one global HEAVY job; defers cycle notifications."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

HEAVY_KINDS = frozenset({
    "testaccount",
    "backtest",
    "hermes_run",
    "churn_replay",
    "counterfactual",
})


@dataclass
class CommandSession:
    session_id: str
    chat_id: str
    kind: str
    job_id: str
    started_at: datetime
    ttl_minutes: int = 60
    progress_message_id: int | None = None

    def expired(self) -> bool:
        return datetime.now() - self.started_at > timedelta(minutes=self.ttl_minutes)


class CommandSessionManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._global: CommandSession | None = None

    def has_heavy_session(self) -> bool:
        with self._lock:
            if self._global and self._global.expired():
                self._global = None
            return self._global is not None

    def current(self) -> CommandSession | None:
        with self._lock:
            if self._global and self._global.expired():
                self._global = None
            return self._global

    def start(
        self,
        chat_id: str | int,
        kind: str,
        *,
        job_id: str | None = None,
        ttl_minutes: int = 60,
    ) -> CommandSession | None:
        cid = str(chat_id or "").strip()
        if not cid or kind not in HEAVY_KINDS:
            return None
        with self._lock:
            if self._global and not self._global.expired():
                return None
            session = CommandSession(
                session_id=uuid.uuid4().hex[:12],
                chat_id=cid,
                kind=kind,
                job_id=job_id or uuid.uuid4().hex[:12],
                started_at=datetime.now(),
                ttl_minutes=ttl_minutes,
            )
            self._global = session
            return session

    def end(self, *, session_id: str | None = None, chat_id: str | int | None = None) -> bool:
        with self._lock:
            if not self._global:
                return False
            if session_id and self._global.session_id != session_id:
                return False
            if chat_id and str(chat_id) != self._global.chat_id:
                return False
            self._global = None
            return True

    def busy_message(self) -> str:
        cur = self.current()
        if not cur:
            return ""
        return (
            f"⏳ Ein schwerer Job läuft bereits "
            f"(<code>{cur.kind}</code>, seit "
            f"{(datetime.now() - cur.started_at).seconds // 60} Min). "
            f"Bitte warten oder <code>/session_cancel</code>."
        )


session_manager = CommandSessionManager()