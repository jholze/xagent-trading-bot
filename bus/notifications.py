"""Priority notification queue — two-lane Telegram delivery with 429 retry.

Lanes
-----
- **Urgent** (priority < PRIORITY_CYCLE: fills, stops, operator alerts): at most
  1 msg/s, independent of the normal lane so cycle digests cannot stall a fill.
- **Normal** (PRIORITY_CYCLE and below): existing ``rate_limit_sec`` (default 1s).

HTTP 429
--------
``TelegramRateLimited(retry_after)`` from the send function is honoured: wait
``min(60, retry_after * 2**(attempts-1))`` seconds (exponential backoff, cap
60s). The failed payload is stored in a bounded retry buffer (max 200, oldest
dropped) and persisted via ``data_manager.resolve_data_path("telegram_retry_buffer.json")``
so a process restart does not lose pending alerts.

Fallback (notifier, not this module)
------------------------------------
When this publisher is not running, ``telegram_notifier.send_telegram_message``
sends synchronously via ``_send_telegram_direct`` — CLI scripts, direct test
calls, and process shutdown must not drop messages on the floor.
"""

from __future__ import annotations

import heapq
import itertools
import json
import os
import threading
import time
from typing import Any, Callable, Optional

from bus.schemas import PRIORITY_CYCLE, PRIORITY_URGENT, NotificationMessage
from logger import log

_send_fn: Optional[Callable[..., bool]] = None
_counter = itertools.count()

URGENT_RATE_LIMIT_SEC = 1.0
RETRY_BUFFER_MAX = 200
RETRY_BACKOFF_CAP_SEC = 60.0
RETRY_BUFFER_NAME = "telegram_retry_buffer.json"


class TelegramRateLimited(Exception):
    """Telegram HTTP 429 — ``retry_after`` is seconds from ``parameters.retry_after``."""

    def __init__(self, retry_after: float = 1.0):
        self.retry_after = max(0.0, float(retry_after))
        super().__init__(f"retry_after={self.retry_after}")


def _should_defer(priority: int) -> bool:
    if priority < PRIORITY_CYCLE:
        return False
    try:
        from bus.sessions import session_manager

        return session_manager.has_heavy_session()
    except Exception:
        return False


def _retry_buffer_path() -> str:
    from data_manager import resolve_data_path

    return resolve_data_path(RETRY_BUFFER_NAME)


def compute_retry_wait(attempts: int, retry_after: float) -> float:
    """Exponential backoff honoring Telegram's retry_after, capped at 60s."""
    base = max(0.0, float(retry_after))
    exp = max(0, int(attempts) - 1)
    return min(RETRY_BACKOFF_CAP_SEC, base * (2 ** exp))


def _msg_to_dict(msg: NotificationMessage) -> dict[str, Any]:
    return {
        "text": msg.text,
        "priority": int(msg.priority),
        "chat_id": msg.chat_id,
        "reply_markup": msg.reply_markup,
        "parse_mode": msg.parse_mode,
        "kind": msg.kind,
        "source": msg.source,
        "enqueued_at": msg.enqueued_at,
        "id": msg.id,
    }


def _msg_from_dict(data: dict[str, Any]) -> NotificationMessage:
    kwargs: dict[str, Any] = {
        "text": str(data.get("text") or ""),
        "priority": int(data.get("priority", PRIORITY_URGENT)),
        "chat_id": data.get("chat_id"),
        "reply_markup": data.get("reply_markup"),
        "parse_mode": str(data.get("parse_mode") or "HTML"),
        "kind": str(data.get("kind") or "text"),
        "source": str(data.get("source") or "monolith"),
    }
    enqueued = str(data.get("enqueued_at") or "").strip()
    if enqueued:
        kwargs["enqueued_at"] = enqueued
    mid = str(data.get("id") or "").strip()
    if mid:
        kwargs["id"] = mid
    return NotificationMessage(**kwargs)


class NotificationPublisher:
    """Two-lane min-heap + retry buffer + background sender thread."""

    def __init__(
        self,
        rate_limit_sec: float = 1.0,
        *,
        urgent_rate_limit_sec: float | None = None,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ):
        self._heap: list[tuple[int, int, NotificationMessage]] = []
        self._urgent: list[tuple[int, int, NotificationMessage]] = []
        self._deferred: list[tuple[int, int, NotificationMessage]] = []
        self._retry: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._not_empty = threading.Condition(self._lock)
        self._running = False
        self._thread: threading.Thread | None = None
        self._rate_limit_sec = max(0.0, float(rate_limit_sec))
        if urgent_rate_limit_sec is None:
            self._urgent_rate_limit_sec = (
                0.0 if self._rate_limit_sec == 0.0 else URGENT_RATE_LIMIT_SEC
            )
        else:
            self._urgent_rate_limit_sec = max(0.0, float(urgent_rate_limit_sec))
        self._last_send_at = 0.0
        self._last_urgent_send_at = 0.0
        self._last_normal_send_at = 0.0
        self._clock = clock or time.time
        self._sleep = sleeper or time.sleep

    def _now(self) -> float:
        return float(self._clock())

    @property
    def running(self) -> bool:
        return self._running

    def queue_depth(self) -> int:
        with self._lock:
            return len(self._heap) + len(self._urgent)

    def retry_buffer_depth(self) -> int:
        with self._lock:
            return len(self._retry)

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
            if _should_defer(priority):
                target = self._deferred
            elif int(priority) < PRIORITY_CYCLE:
                target = self._urgent
            else:
                target = self._heap
            heapq.heappush(target, (priority, next(_counter), msg))
            if target is not self._deferred:
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
        self._load_retry_buffer()
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="notification-worker")
        self._thread.start()
        log(
            f"Notification worker started (rate_limit={self._rate_limit_sec}s, "
            f"urgent_rate_limit={self._urgent_rate_limit_sec}s)",
            "INFO",
        )
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

    def stop(self, *, persist: bool = True):
        self._running = False
        with self._not_empty:
            self._not_empty.notify_all()
        if persist:
            with self._lock:
                self._absorb_heaps_into_retry_locked()
            self._save_retry_buffer()

    def drain(self) -> None:
        """Drop in-memory queues (tests / reset). Does not write the retry file."""
        with self._lock:
            self._heap.clear()
            self._urgent.clear()
            self._deferred.clear()
            self._retry.clear()
            self._last_send_at = 0.0
            self._last_urgent_send_at = 0.0
            self._last_normal_send_at = 0.0

    def _loop(self):
        while self._running:
            item: tuple[NotificationMessage, int] | None = None
            with self._not_empty:
                while self._running:
                    ready = self._pop_ready_retry_locked()
                    if ready is not None:
                        item = ready
                        break
                    if self._urgent:
                        _prio, _seq, msg = heapq.heappop(self._urgent)
                        item = (msg, 0)
                        break
                    if self._heap:
                        _prio, _seq, msg = heapq.heappop(self._heap)
                        item = (msg, 0)
                        break
                    timeout = self._retry_wait_locked()
                    if timeout is None:
                        timeout = 1.0
                    else:
                        timeout = min(max(timeout, 0.0), 1.0)
                    self._not_empty.wait(timeout=timeout)
                if not self._running:
                    break
            if item is not None:
                msg, attempts = item
                self._dispatch(msg, attempts=attempts)

    def _retry_wait_locked(self) -> float | None:
        if not self._retry:
            return None
        soonest = min(float(e.get("ready_at") or 0.0) for e in self._retry)
        return soonest - self._now()

    def _pop_ready_retry_locked(self) -> tuple[NotificationMessage, int] | None:
        now = self._now()
        for i, entry in enumerate(self._retry):
            if float(entry.get("ready_at") or 0.0) <= now:
                popped = self._retry.pop(i)
                msg = popped.get("msg")
                if not isinstance(msg, NotificationMessage):
                    return None
                return msg, int(popped.get("attempts") or 0)
        return None

    def _wait_lane(self, msg: NotificationMessage) -> None:
        is_urgent = int(msg.priority) < PRIORITY_CYCLE
        limit = self._urgent_rate_limit_sec if is_urgent else self._rate_limit_sec
        if limit <= 0:
            return
        last = self._last_urgent_send_at if is_urgent else self._last_normal_send_at
        elapsed = self._now() - last
        if elapsed < limit:
            self._sleep(limit - elapsed)

    def _mark_sent(self, msg: NotificationMessage) -> None:
        now = self._now()
        self._last_send_at = now
        if int(msg.priority) < PRIORITY_CYCLE:
            self._last_urgent_send_at = now
        else:
            self._last_normal_send_at = now

    def _dispatch(self, msg: NotificationMessage, attempts: int = 0):
        if _send_fn is None:
            return
        if attempts <= 0:
            self._wait_lane(msg)
        try:
            while True:
                if not self._running:
                    self._buffer_retry(msg, max(attempts, 1), wait=0.0)
                    return
                try:
                    ok = _send_fn(
                        msg.text,
                        reply_markup=msg.reply_markup,
                        chat_id=msg.chat_id,
                        parse_mode=msg.parse_mode,
                    )
                except TelegramRateLimited as e:
                    attempts += 1
                    wait = compute_retry_wait(attempts, e.retry_after)
                    self._buffer_retry(msg, attempts, wait)
                    self._mark_sent(msg)
                    if not self._running:
                        return
                    self._sleep(wait)
                    continue
                self._mark_sent(msg)
                if ok:
                    self._drop_retry(msg.id)
                    return
                log(f"Notification delivery failed ({msg.kind}, prio={msg.priority})", "WARNING")
                return
        except Exception as e:
            log(f"Notification worker error: {e}", "WARNING")
            self._mark_sent(msg)

    def _buffer_retry(self, msg: NotificationMessage, attempts: int, wait: float) -> None:
        ready_at = self._now() + max(0.0, float(wait))
        with self._lock:
            self._retry = [e for e in self._retry if getattr(e.get("msg"), "id", None) != msg.id]
            self._retry.append({"msg": msg, "attempts": int(attempts), "ready_at": ready_at})
            dropped = 0
            while len(self._retry) > RETRY_BUFFER_MAX:
                self._retry.pop(0)
                dropped += 1
            if dropped:
                log(
                    f"Telegram retry buffer full — dropped {dropped} oldest pending alert(s)",
                    "WARNING",
                )
        self._save_retry_buffer()

    def _drop_retry(self, msg_id: str) -> None:
        with self._lock:
            before = len(self._retry)
            self._retry = [e for e in self._retry if getattr(e.get("msg"), "id", None) != msg_id]
            changed = len(self._retry) != before
        if changed:
            self._save_retry_buffer()

    def _absorb_heaps_into_retry_locked(self) -> None:
        pending: list[NotificationMessage] = []
        while self._urgent:
            pending.append(heapq.heappop(self._urgent)[2])
        while self._heap:
            pending.append(heapq.heappop(self._heap)[2])
        while self._deferred:
            pending.append(heapq.heappop(self._deferred)[2])
        now = self._now()
        known = {getattr(e.get("msg"), "id", None) for e in self._retry}
        for msg in pending:
            if msg.id in known:
                continue
            self._retry.append({"msg": msg, "attempts": 0, "ready_at": now})
            known.add(msg.id)
            while len(self._retry) > RETRY_BUFFER_MAX:
                self._retry.pop(0)

    def _save_retry_buffer(self) -> None:
        try:
            from data_manager import atomic_write_json

            with self._lock:
                entries = []
                for e in self._retry:
                    msg = e.get("msg")
                    if not isinstance(msg, NotificationMessage):
                        continue
                    entries.append(
                        {
                            "message": _msg_to_dict(msg),
                            "attempts": int(e.get("attempts") or 0),
                            "ready_at": float(e.get("ready_at") or 0.0),
                        }
                    )
            atomic_write_json(_retry_buffer_path(), {"entries": entries})
        except Exception as exc:
            log(f"Telegram retry buffer save failed: {exc}", "WARNING")

    def _load_retry_buffer(self) -> None:
        path = _retry_buffer_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh) or {}
        except Exception as exc:
            log(f"Telegram retry buffer load failed: {exc}", "WARNING")
            return
        raw_entries = payload.get("entries") if isinstance(payload, dict) else None
        if not isinstance(raw_entries, list):
            return
        loaded: list[dict[str, Any]] = []
        for raw in raw_entries:
            if not isinstance(raw, dict):
                continue
            msg_data = raw.get("message")
            if not isinstance(msg_data, dict):
                continue
            try:
                msg = _msg_from_dict(msg_data)
            except Exception:
                continue
            if not msg.id:
                continue
            loaded.append(
                {
                    "msg": msg,
                    "attempts": int(raw.get("attempts") or 0),
                    "ready_at": float(raw.get("ready_at") or 0.0),
                }
            )
        if not loaded:
            return
        with self._lock:
            have = {getattr(e.get("msg"), "id", None) for e in self._retry}
            for entry in loaded:
                mid = entry["msg"].id
                if mid in have:
                    continue
                self._retry.append(entry)
                have.add(mid)
                while len(self._retry) > RETRY_BUFFER_MAX:
                    self._retry.pop(0)
            if loaded:
                self._not_empty.notify()
        log(f"Loaded {len(loaded)} Telegram alert(s) from retry buffer", "INFO")

    def _clear_retry_file(self) -> None:
        path = _retry_buffer_path()
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass


notification_publisher = NotificationPublisher()


def reset_notification_publisher_for_tests() -> None:
    """Stop and drain the singleton notification worker so it cannot outlive a pytest test (#329)."""
    global _send_fn
    pub = notification_publisher
    if pub.running:
        pub.stop(persist=False)
    thread = pub._thread
    if thread is not None and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=2.0)
    pub._thread = None
    pub.drain()
    pub._clear_retry_file()
    _send_fn = None
    pub._clock = time.time
    pub._sleep = time.sleep
    pub._rate_limit_sec = 1.0
    pub._urgent_rate_limit_sec = URGENT_RATE_LIMIT_SEC
