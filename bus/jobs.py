"""In-process heavy job queue — one job at a time, integrates with command sessions."""

from __future__ import annotations

import queue
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from bus.sessions import session_manager
from logger import log


@dataclass
class HeavyJob:
    job_id: str
    kind: str
    chat_id: str
    params: dict
    run: Callable[[], None]
    session_id: str = ""


class HeavyJobQueue:
    def __init__(self):
        self._queue: queue.Queue[HeavyJob | None] = queue.Queue()
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._running

    def depth(self) -> int:
        return self._queue.qsize()

    def start(self):
        with self._lock:
            if self._running:
                return
            self._running = True
            self._thread = threading.Thread(target=self._loop, daemon=True, name="heavy-job-worker")
            self._thread.start()
            log("Heavy job worker started", "INFO")

    def stop(self):
        self._running = False
        self._queue.put(None)

    def enqueue(
        self,
        kind: str,
        chat_id: str | int,
        run: Callable[[], None],
        *,
        params: dict | None = None,
        ttl_minutes: int = 60,
    ) -> tuple[Optional[str], Optional[str]]:
        """Returns (job_id, error_message). error_message set when rejected."""
        cid = str(chat_id or "").strip()
        if not cid:
            return None, "Kein chat_id"
        if session_manager.has_heavy_session():
            return None, session_manager.busy_message()
        session = session_manager.start(cid, kind, ttl_minutes=ttl_minutes)
        if not session:
            return None, session_manager.busy_message()
        job_id = session.job_id
        job = HeavyJob(
            job_id=job_id,
            kind=kind,
            chat_id=cid,
            params=params or {},
            run=run,
            session_id=session.session_id,
        )
        self._queue.put(job)
        return job_id, None

    def _finish_session(self, job: HeavyJob):
        session_manager.end(session_id=job.session_id, chat_id=job.chat_id)
        try:
            from bus.notifications import notification_publisher

            notification_publisher.flush_deferred()
        except Exception as e:
            log(f"Deferred notification flush failed: {e}", "WARNING")

    def _loop(self):
        while self._running:
            try:
                job = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if job is None:
                break
            try:
                try:
                    from bus.heartbeats import heartbeat_registry
                    from core.config import get_bot_config

                    arch = get_bot_config().architecture_config
                    heartbeat_registry.beat(
                        "heavy_job_worker",
                        meta={"job": job.kind},
                        ttl_sec=int(arch.get("heartbeat_ttl_sec", 120)),
                        key_prefix=arch.get("key_prefix", "aria:"),
                    )
                except Exception:
                    pass
                job.run()
            except Exception as e:
                log(f"Heavy job {job.kind} ({job.job_id}) failed: {e}", "ERROR")
            finally:
                self._finish_session(job)
                self._queue.task_done()


heavy_job_queue = HeavyJobQueue()