"""Poll loop (+ optional WS wake hook) for dca sniper service."""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable

from logger import log
from services.dca_sniper.config import dca_sniper_config, dca_sniper_enabled
from services.dca_sniper.engine import run_cycle


class DcaSniperLoop:
    def __init__(self, *, config: dict | None = None):
        self._cfg_raw = config
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_audit: dict[str, Any] = {}
        self._on_cycle: Callable[[dict], None] | None = None

    def request_wake(self, reason: str = "ws") -> None:
        log(f"dca_sniper wake: {reason}", "DEBUG")
        self._wake.set()

    def last_audit(self) -> dict[str, Any]:
        return dict(self._last_audit)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="dca-sniper-loop", daemon=True)
        self._thread.start()
        log("dca_sniper loop started", "INFO")

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)
        log("dca_sniper loop stopped", "INFO")

    def _interval(self) -> float:
        cfg = dca_sniper_config(self._cfg_raw)
        return max(15.0, float(cfg.get("poll_interval_sec") or 120))

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if dca_sniper_enabled(self._cfg_raw) or str(
                    os.environ.get("DCA_SNIPER_FORCE") or ""
                ).lower() in ("1", "true"):
                    audit = run_cycle(config=self._cfg_raw)
                    self._last_audit = audit
                    if self._on_cycle:
                        try:
                            self._on_cycle(audit)
                        except Exception:
                            pass
                else:
                    self._last_audit = {"skipped": "disabled"}
            except Exception as e:
                log(f"dca_sniper cycle error: {e}", "ERROR")
                self._last_audit = {"error": str(e)[:200]}
            # wait interval or wake
            self._wake.clear()
            self._wake.wait(timeout=self._interval())
