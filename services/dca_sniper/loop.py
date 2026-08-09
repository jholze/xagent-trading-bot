"""Standalone poll loop + Redis/WS wake for dca sniper service."""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable

from logger import log
from services.dca_sniper.config import dca_sniper_config, dca_sniper_enabled
from services.dca_sniper.engine import run_cycle
from services.dca_sniper import state as sniper_state


class DcaSniperLoop:
    def __init__(self, *, config: dict | None = None):
        self._cfg_raw = config
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_audit: dict[str, Any] = {}
        self._on_cycle: Callable[[dict], None] | None = None
        self._ws = None
        self._redis_sub = None
        self._last_wake_reason = ""

    def request_wake(self, reason: str = "ws", extra: dict | None = None) -> None:
        self._last_wake_reason = reason
        log(f"dca_sniper wake: {reason}", "INFO")
        sniper_state.add_decision({"action": "WAKE", "reason": reason, **(extra or {})})
        try:
            from services.dca_sniper.redis_bus import publish_wake

            # avoid echo storm: only publish if not from redis already
            if not str(reason).startswith("redis"):
                publish_wake(reason, extra=extra)
        except Exception:
            pass
        self._wake.set()

    def last_audit(self) -> dict[str, Any]:
        return dict(self._last_audit)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        cfg = dca_sniper_config(self._cfg_raw)
        # Redis pub/sub wakes
        try:
            from services.dca_sniper.redis_bus import WakeSubscriber, redis_available

            if redis_available():
                self._redis_sub = WakeSubscriber(
                    lambda reason, data: self.request_wake(f"redis:{reason}", data),
                    watch_provider=self._watch_symbols,
                    price_move_pct=float(cfg.get("ws_move_pct") or 1.5),
                )
                self._redis_sub.start()
        except Exception as e:
            log(f"dca_sniper redis sub skip: {e}", "DEBUG")

        # Gate WS for focus/shortlist
        if cfg.get("ws_enabled", True):
            try:
                from services.dca_sniper.ws_watch import SniperWsWatch
                from services.dca_sniper.redis_bus import publish_price

                def on_tick(sym: str, px: float) -> None:
                    publish_price(sym, px, source="dca_sniper_ws")

                self._ws = SniperWsWatch(
                    symbols_provider=self._watch_symbols,
                    on_tick=on_tick,
                    on_wake=lambda r, d: self.request_wake(r, d),
                    move_pct_wake=float(cfg.get("ws_move_pct") or 1.5),
                )
                self._ws.start()
            except Exception as e:
                log(f"dca_sniper WS skip: {e}", "WARNING")

        self._thread = threading.Thread(target=self._run, name="dca-sniper-loop", daemon=True)
        self._thread.start()
        log("dca_sniper standalone loop started", "INFO")

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._ws:
            try:
                self._ws.stop()
            except Exception:
                pass
        if self._redis_sub:
            try:
                self._redis_sub.stop()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5)
        log("dca_sniper loop stopped", "INFO")

    def _watch_symbols(self) -> list[str]:
        """Focus + top ranked candidates from last audit if present."""
        syms = list(sniper_state.focus_symbols())
        audit = self._last_audit or {}
        for row in audit.get("ranked_top") or []:
            s = str(row.get("symbol") or "")
            if s and s not in syms:
                syms.append(s)
        # also pull Redis watch set (other writers)
        try:
            from services.dca_sniper.redis_bus import get_watch_symbols

            for s in get_watch_symbols():
                if s and s not in syms:
                    syms.append(s)
        except Exception:
            pass
        return syms[:40]

    def _interval(self) -> float:
        cfg = dca_sniper_config(self._cfg_raw)
        return max(15.0, float(cfg.get("poll_interval_sec") or 120))

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                from services.dca_sniper.redis_bus import beat

                beat(ttl_sec=max(60, int(self._interval()) + 60))
            except Exception:
                pass
            try:
                if dca_sniper_enabled(self._cfg_raw) or str(
                    os.environ.get("DCA_SNIPER_FORCE") or ""
                ).lower() in ("1", "true"):
                    audit = run_cycle(config=self._cfg_raw)
                    if self._last_wake_reason:
                        audit["wake_reason"] = self._last_wake_reason
                        self._last_wake_reason = ""
                    self._last_audit = audit
                    # refresh watch set after cycle
                    try:
                        from services.dca_sniper.redis_bus import set_watch_symbols, publish_event

                        ranked = [
                            str(r.get("symbol"))
                            for r in (audit.get("ranked_top") or [])
                            if r.get("symbol")
                        ]
                        set_watch_symbols(list(dict.fromkeys(sniper_state.focus_symbols() + ranked)))
                        publish_event({"type": "cycle", "audit": {
                            "n_candidates": audit.get("n_candidates"),
                            "n_focus": audit.get("n_focus"),
                            "actions": len(audit.get("actions") or []),
                            "sharp": audit.get("sharp"),
                        }})
                    except Exception:
                        pass
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
            self._wake.clear()
            self._wake.wait(timeout=self._interval())
