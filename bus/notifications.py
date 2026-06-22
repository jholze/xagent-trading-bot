"""Priority notification queue — non-blocking Telegram sends for cycle digests."""

from __future__ import annotations

import heapq
import itertools
import threading
import time
from typing import Any, Callable, Optional

from bus.schemas import PRIORITY_CYCLE, PRIORITY_URGENT, NotificationMessage
from logger import log

_send_fn: Optional[Callable[..., bool]] = None
_counter = itertools.count()


def _should_defer(priority: int) -> bool:
    if priority < PRIORITY_CYCLE:
        return False
    try:
        from bus.sessions import session_manager

        return session_manager.has_heavy_session()
    except Exception:
        return False


class NotificationPublisher:
    """Min-heap priority queue + background sender thread."""

    def __init__(self, rate_limit_sec: float = 1.0):
        self._heap: list[tuple[int, int, NotificationMessage]] = []
        self._deferred: list[tuple[int, int, NotificationMessage]] = []
        self._lock = threading.Lock()
        self._not_empty = threading.Condition(self._lock)
        self._running = False
        self._thread: threading.Thread | None = None
        self._rate_limit_sec = max(0.0, float(rate_limit_sec))
        self._last_send_at = 0.0

    @property
    def running(self) -> bool:
        return self._running

    def queue_depth(self) -> int:
        with self._lock:
            return len(self._heap)

    def enqueue(
        self,
        text: str,
        *,
        priority: int = PRIORITY_URGENT,
        chat_id: str | int | None = None,
        reply_markup: Any = None,
        parse_mode: str = "HTML",
        kind: str = "text",
        source: str = "monolith",
    ) -> str:
        msg = NotificationMessage(
            text=text,
            priority=priority,
            chat_id=chat_id,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            kind=kind,
            source=source,
        )
        with self._not_empty:
            target = self._deferred if _should_defer(priority) else self._heap
            heapq.heappush(target, (priority, next(_counter), msg))
            if target is self._heap:
                self._not_empty.notify()
        try:
            from bus.publisher import publish_notification
            from core.config import get_bot_config

            arch = get_bot_config().architecture_config
            publish_notification(msg, key_prefix=arch.get("key_prefix", "aria:"), redis_url=arch.get("redis_url"))
        except Exception:
            pass
        return msg.id

    def deferred_count(self) -> int:
        with self._lock:
            return len(self._deferred)

    def flush_deferred(self) -> int:
        moved = 0
        with self._not_empty:
            while self._deferred:
                item = heapq.heappop(self._deferred)
                heapq.heappush(self._heap, item)
                moved += 1
            if moved:
                self._not_empty.notify_all()
        if moved:
            log(f"Flushed {moved} deferred notification(s) after session end", "INFO")
        return moved

    def start(self, send_fn: Callable[..., bool]):
        global _send_fn
        _send_fn = send_fn
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="notification-worker")
        self._thread.start()
        log(f"Notification worker started (rate_limit={self._rate_limit_sec}s)", "INFO")
        try:
            from bus.heartbeats import heartbeat_registry
            from core.config import get_bot_config

            arch = get_bot_config().architecture_config
            heartbeat_registry.beat(
                "notification_worker",
                ttl_sec=int(arch.get("heartbeat_ttl_sec", 120)),
                key_prefix=arch.get("key_prefix", "aria:"),
            )
        except Exception:
            pass

    def stop(self):
        self._running = False
        with self._not_empty:
            self._not_empty.notify_all()

    def _loop(self):
        while self._running:
            with self._not_empty:
                while self._running and not self._heap:
                    self._not_empty.wait(timeout=1.0)
                if not self._running:
                    break
                _prio, _seq, msg = heapq.heappop(self._heap)
            self._dispatch(msg)

    def _dispatch(self, msg: NotificationMessage):
        if _send_fn is None:
            return
        if self._rate_limit_sec > 0 and msg.priority >= PRIORITY_CYCLE:
            elapsed = time.time() - self._last_send_at
            if elapsed < self._rate_limit_sec:
                time.sleep(self._rate_limit_sec - elapsed)
        try:
            ok = _send_fn(
                msg.text,
                reply_markup=msg.reply_markup,
                chat_id=msg.chat_id,
                parse_mode=msg.parse_mode,
            )
            if not ok:
                log(f"Notification delivery failed ({msg.kind}, prio={msg.priority})", "WARNING")
        except Exception as e:
            log(f"Notification worker error: {e}", "WARNING")
        finally:
            self._last_send_at = time.time()


notification_publisher = NotificationPublisher()