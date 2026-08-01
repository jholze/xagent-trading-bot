"""Gate public WS hub for shadow exit evaluation (no orders)."""

from __future__ import annotations

import json
import ssl
import threading
import time
from typing import Any, Callable

from logger import log

from services.exit_realtime.config import (
    exit_realtime_config,
    exit_realtime_enabled,
    exit_realtime_mode,
    exit_realtime_sources,
)
from services.exit_realtime.shadow_eval import (
    evaluate_would_sells,
    from_gate_pair,
    to_gate_pair,
)

WS_URL = "wss://api.gateio.ws/ws/v4/"
CHANNEL = "spot.tickers"


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    try:
        import certifi

        ctx.load_verify_locations(certifi.where())
    except Exception:
        pass
    return ctx


def _log_shadow_event(event: dict[str, Any]) -> None:
    try:
        from logger import LOG_DIR
        import os

        path = os.path.join(LOG_DIR, "exit_ws_shadow.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass
    try:
        log(
            f"exit_ws_shadow symbol={event.get('symbol')} src={event.get('source')} "
            f"gain={event.get('gain_pct')} drop={event.get('drop_from_high_pct')} "
            f"px={event.get('price')} :: {event.get('rationale', '')[:80]}",
            "INFO",
        )
    except Exception:
        pass


class ExitRealtimeHub:
    """Background Gate ticker stream → pure trail evaluators → shadow logs."""

    def __init__(self, raw_config: dict | None = None) -> None:
        self._raw = raw_config
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pos_lock = threading.Lock()
        # symbol -> position snapshot + strategy_params + timeframe
        self._book: dict[str, dict[str, Any]] = {}
        self._gate_to_symbol: dict[str, str] = {}
        self._last_fire: dict[str, float] = {}  # key symbol|source -> mono time
        self._stats = {
            "ticks": 0,
            "shadow_fires": 0,
            "reconnects": 0,
            "last_tick_at": 0.0,
            "symbols": 0,
        }

    def stats(self) -> dict[str, Any]:
        return dict(self._stats)

    def update_book(self, positions: list[dict[str, Any]]) -> list[str]:
        """Replace open-position book. Returns gate pairs to subscribe."""
        book: dict[str, dict[str, Any]] = {}
        gmap: dict[str, str] = {}
        for row in positions:
            sym = str(row.get("symbol") or "")
            if not sym:
                continue
            book[sym] = row
            gmap[to_gate_pair(sym)] = sym
        with self._pos_lock:
            self._book = book
            self._gate_to_symbol = gmap
            self._stats["symbols"] = len(book)
        return sorted(gmap.keys())

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="exit-realtime-ws", daemon=True
        )
        self._thread.start()
        log("exit_realtime hub started (shadow)", "INFO")

    def stop(self) -> None:
        self._stop.set()

    def _debounce_ok(self, symbol: str, source: str, cooldown_sec: float) -> bool:
        key = f"{symbol}|{source}"
        now = time.monotonic()
        last = self._last_fire.get(key, 0.0)
        if now - last < cooldown_sec:
            return False
        self._last_fire[key] = now
        return True

    def on_ticker(self, gate_pair: str, price: float) -> None:
        if price <= 0:
            return
        self._stats["ticks"] += 1
        self._stats["last_tick_at"] = time.time()
        with self._pos_lock:
            sym = self._gate_to_symbol.get(gate_pair) or from_gate_pair(gate_pair)
            row = self._book.get(sym)
            if not row:
                return
            # bump in-memory peak
            rh = float(row.get("recent_high") or 0)
            if price > rh:
                row["recent_high"] = price
            snapshot = dict(row)

        cfg = exit_realtime_config(self._raw)
        sources = exit_realtime_sources(self._raw)
        cooldown = float(cfg.get("shadow_cooldown_sec", 30) or 30)
        atr = float(snapshot.get("atr_pct") or cfg.get("default_atr_pct", 3.0) or 3.0)
        params = dict(snapshot.get("strategy_params") or {})
        tf = str(snapshot.get("timeframe") or "1h")
        pos = dict(snapshot.get("position") or snapshot)

        events = evaluate_would_sells(
            symbol=sym,
            timeframe=tf,
            price=price,
            position=pos,
            strategy_params=params,
            sources=sources,
            atr_pct=atr,
        )
        for ev in events:
            if ev.get("error") or not ev.get("action"):
                continue
            src = str(ev.get("source") or "")
            if not self._debounce_ok(sym, src, cooldown):
                continue
            ev["mode"] = exit_realtime_mode(self._raw)
            ev["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _log_shadow_event(ev)
            self._stats["shadow_fires"] += 1

    def _run_loop(self) -> None:
        try:
            import websocket
        except ImportError:
            log(
                "exit_realtime: websocket-client missing — hub idle "
                "(pip install websocket-client)",
                "WARNING",
            )
            return

        cfg = exit_realtime_config(self._raw)
        backoff = float(cfg.get("reconnect_backoff_sec", 3) or 3)
        max_backoff = float(cfg.get("reconnect_max_sec", 60) or 60)

        while not self._stop.is_set():
            pairs: list[str] = []
            with self._pos_lock:
                pairs = sorted(self._gate_to_symbol.keys())
            if not pairs:
                time.sleep(5)
                continue

            ws_holder: dict[str, Any] = {"ws": None}

            def on_message(_ws, message: str) -> None:
                try:
                    data = json.loads(message)
                except Exception:
                    return
                if data.get("event") in ("subscribe", "unsubscribe"):
                    return
                result = data.get("result")
                if not isinstance(result, dict):
                    return
                # spot.tickers push: currency_pair, last, ...
                pair = result.get("currency_pair") or result.get("s")
                last = result.get("last") or result.get("c")
                if not pair or last is None:
                    return
                try:
                    px = float(last)
                except (TypeError, ValueError):
                    return
                self.on_ticker(str(pair).upper(), px)

            def on_open(ws) -> None:
                # Gate: subscribe one-by-one (batch fails on unknown pairs)
                for gp in pairs:
                    try:
                        ws.send(
                            json.dumps(
                                {
                                    "time": int(time.time()),
                                    "channel": CHANNEL,
                                    "event": "subscribe",
                                    "payload": [gp],
                                }
                            )
                        )
                        time.sleep(0.05)
                    except Exception as exc:
                        log(f"exit_realtime subscribe {gp}: {exc}", "DEBUG")

            def on_error(_ws, err) -> None:
                log(f"exit_realtime ws error: {err}", "WARNING")

            def on_close(_ws, *_a) -> None:
                pass

            try:
                ws = websocket.WebSocketApp(
                    WS_URL,
                    on_open=on_open,
                    on_message=on_message,
                    on_error=on_error,
                    on_close=on_close,
                )
                ws_holder["ws"] = ws
                self._stats["reconnects"] += 1
                ws.run_forever(sslopt={"context": _ssl_context()}, ping_interval=20)
            except Exception as exc:
                log(f"exit_realtime run_forever: {exc}", "WARNING")

            if self._stop.is_set():
                break
            time.sleep(backoff)
            backoff = min(max_backoff, backoff * 1.5)


_hub: ExitRealtimeHub | None = None
_hub_lock = threading.Lock()
_refresh_thread: threading.Thread | None = None
_refresh_stop = threading.Event()


def get_hub() -> ExitRealtimeHub | None:
    return _hub


def _load_open_book(raw: dict | None) -> list[dict[str, Any]]:
    """Build position rows for hub from in-memory positions."""
    from core.config import get_bot_config
    from strategies.positions import is_open_position, parse_position_key, positions

    cfg = get_bot_config()
    rows: list[dict[str, Any]] = []
    for key, pos in list(positions.items()):
        if not is_open_position(pos):
            continue
        sym, tf = parse_position_key(key)
        if not sym:
            sym = str(pos.get("symbol") or "").replace("_", "/")
            tf = str(pos.get("timeframe") or "1h")
        if not sym:
            continue
        if not tf:
            tf = "1h"

        try:
            from strategies.registry import resolve_strategy_params

            params = resolve_strategy_params(
                {"symbol": sym, "timeframe": tf},
                has_position=True,
                frozen_tier=pos.get("strategy_tier"),
            )
        except Exception:
            try:
                params = cfg.strategy_params(sym, tf)
            except Exception:
                params = {}

        rows.append(
            {
                "symbol": sym,
                "timeframe": tf,
                "position": dict(pos),
                "average_entry": float(pos.get("average_entry") or 0),
                "recent_high": float(pos.get("recent_high") or 0),
                "strategy_params": params,
                "atr_pct": float(
                    (params or {}).get("atr_reference_pct")
                    or (cfg.risk_config or {}).get("atr_reference_pct")
                    or 3.0
                ),
                "strategy_tier": pos.get("strategy_tier"),
            }
        )
    return rows


def _refresh_loop(raw_getter: Callable[[], dict | None]) -> None:
    while not _refresh_stop.is_set():
        hub = get_hub()
        if hub is None:
            break
        try:
            raw = raw_getter()
            if not exit_realtime_enabled(raw) or exit_realtime_mode(raw) == "off":
                time.sleep(10)
                continue
            rows = _load_open_book(raw)
            hub.update_book(rows)
        except Exception as exc:
            log(f"exit_realtime book refresh: {exc}", "WARNING")
        cfg = exit_realtime_config(raw_getter())
        time.sleep(float(cfg.get("book_refresh_sec", 30) or 30))


def ensure_started(raw: dict | None = None) -> ExitRealtimeHub | None:
    """Idempotent start of shadow hub when config enables it."""
    global _hub, _refresh_thread
    if raw is None:
        try:
            from core.config import get_bot_config

            raw = get_bot_config().raw
        except Exception:
            raw = {}

    if not exit_realtime_enabled(raw):
        return None
    mode = exit_realtime_mode(raw)
    if mode == "off":
        return None
    # Phase 1: shadow only — live is a no-op for orders but hub may still log
    if mode not in ("shadow", "live"):
        return None

    with _hub_lock:
        if _hub is None:
            _hub = ExitRealtimeHub(raw)
            try:
                _hub.update_book(_load_open_book(raw))
            except Exception as exc:
                log(f"exit_realtime initial book: {exc}", "WARNING")
            _hub.start()
        if _refresh_thread is None or not _refresh_thread.is_alive():
            _refresh_stop.clear()

            def _getter() -> dict | None:
                try:
                    from core.config import get_bot_config

                    return get_bot_config().raw
                except Exception:
                    return raw

            _refresh_thread = threading.Thread(
                target=_refresh_loop, args=(_getter,), daemon=True, name="exit-rt-book"
            )
            _refresh_thread.start()
        if mode == "live":
            log(
                "exit_realtime mode=live but order path not wired yet (shadow logs only)",
                "INFO",
            )
        return _hub
