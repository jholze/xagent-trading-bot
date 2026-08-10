"""Gate public WS hub: trail eval → live SELL via TradingService (staging-first)."""

from __future__ import annotations

import json
import queue
import ssl
import threading
import time
from collections import deque
from typing import Any, Callable

from logger import log

from services.exit_realtime.config import (
    exit_realtime_config,
    exit_realtime_enabled,
    exit_realtime_mode,
    exit_realtime_sources,
)
from services.exit_realtime.execute import try_execute_trail_exit
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


def _log_event(event: dict[str, Any], *, live: bool) -> None:
    name = "exit_ws_live" if live else "exit_ws_event"
    try:
        from logger import LOG_DIR
        import os

        path = os.path.join(LOG_DIR, "exit_ws_events.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass
    try:
        log(
            f"{name} symbol={event.get('symbol')} src={event.get('source')} "
            f"gain={event.get('gain_pct')} drop={event.get('drop_from_high_pct')} "
            f"executed={event.get('executed')} px={event.get('price')} "
            f":: {str(event.get('rationale') or event.get('message') or '')[:80]}",
            "INFO",
        )
    except Exception:
        pass


class ExitRealtimeHub:
    """Background Gate ticker stream → trail exits + optional gainer board identify.

    - OPEN positions: trail/TTP eval → SELL (unchanged)
    - Watch set (REST-seeded): ticks feed gainer_universe.ws_board (shadow log only)
    """

    def __init__(self, raw_config: dict | None = None) -> None:
        self._raw = raw_config
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pos_lock = threading.Lock()
        self._book: dict[str, dict[str, Any]] = {}
        self._gate_to_symbol: dict[str, str] = {}
        # Identify watch (non-position); unioned into subscribe set
        self._watch_symbols: set[str] = set()
        self._watch_gate: dict[str, str] = {}
        self._last_fire: dict[str, float] = {}
        self._last_prices: dict[str, float] = {}
        self._connected = False
        self._ws: Any = None
        self._subscribed: set[str] = set()
        self._event_q: deque[dict[str, Any]] = deque(maxlen=200)
        self._gui_clients: list[queue.Queue] = []
        self._gui_lock = threading.Lock()
        self._stats = {
            "ticks": 0,
            "fires": 0,
            "executed": 0,
            "blocked": 0,
            "reconnects": 0,
            "last_tick_at": 0.0,
            "symbols": 0,
            "watch": 0,
            "connected": False,
        }

    def stats(self) -> dict[str, Any]:
        out = dict(self._stats)
        out["connected"] = self._connected
        out["gui_clients"] = len(self._gui_clients)
        return out

    def last_prices(self) -> dict[str, float]:
        with self._pos_lock:
            return dict(self._last_prices)

    def book_snapshot(self) -> list[dict[str, Any]]:
        with self._pos_lock:
            rows = []
            for sym, row in self._book.items():
                r = {
                    "symbol": sym,
                    "timeframe": row.get("timeframe"),
                    "average_entry": row.get("average_entry")
                    or (row.get("position") or {}).get("average_entry"),
                    "recent_high": row.get("recent_high")
                    or (row.get("position") or {}).get("recent_high"),
                    "last_price": self._last_prices.get(sym),
                    "strategy_tier": row.get("strategy_tier"),
                    "position": dict(row.get("position") or {}),
                    "strategy_params": dict(row.get("strategy_params") or {}),
                    "atr_pct": row.get("atr_pct"),
                }
                rows.append(r)
            return rows

    def subscribe_gui(self) -> queue.Queue:
        cq: queue.Queue = queue.Queue(maxsize=300)
        with self._gui_lock:
            self._gui_clients.append(cq)
        return cq

    def unsubscribe_gui(self, cq: queue.Queue) -> None:
        with self._gui_lock:
            try:
                self._gui_clients.remove(cq)
            except ValueError:
                pass

    def _broadcast_gui(self, event: dict[str, Any]) -> None:
        self._event_q.appendleft(event)
        with self._gui_lock:
            dead: list[queue.Queue] = []
            for cq in self._gui_clients:
                try:
                    cq.put_nowait(event)
                except queue.Full:
                    try:
                        cq.get_nowait()
                    except Exception:
                        pass
                    try:
                        cq.put_nowait(event)
                    except Exception:
                        dead.append(cq)
            for cq in dead:
                try:
                    self._gui_clients.remove(cq)
                except ValueError:
                    pass

    def update_book(self, positions: list[dict[str, Any]]) -> list[str]:
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
        self._request_subscribe_sync()
        return sorted(gmap.keys())

    def update_watch_set(self, symbols: list[str]) -> list[str]:
        """REST-seeded identify universe (no exit eval). Empty clears watch."""
        wset: set[str] = set()
        wgate: dict[str, str] = {}
        for raw in symbols or []:
            sym = str(raw or "").strip().upper().replace("-", "/")
            if not sym:
                continue
            if "_" in sym and "/" not in sym:
                a, b = sym.rsplit("_", 1)
                sym = f"{a}/{b}"
            wset.add(sym)
            wgate[to_gate_pair(sym)] = sym
        with self._pos_lock:
            self._watch_symbols = wset
            self._watch_gate = wgate
            self._stats["watch"] = len(wset)
        self._request_subscribe_sync()
        return sorted(wset)

    def _desired_gate_pairs(self) -> list[str]:
        with self._pos_lock:
            pairs = set(self._gate_to_symbol.keys()) | set(self._watch_gate.keys())
        return sorted(pairs)

    def _request_subscribe_sync(self) -> None:
        """Best-effort subscribe new pairs on live WS (no-op if disconnected)."""
        ws = self._ws
        if ws is None or not self._connected:
            return
        try:
            desired = set(self._desired_gate_pairs())
            new = desired - self._subscribed
            for gp in sorted(new):
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
                    self._subscribed.add(gp)
                    time.sleep(0.03)
                except Exception as exc:
                    log(f"exit_realtime watch-sub {gp}: {exc}", "DEBUG")
        except Exception as exc:
            log(f"exit_realtime subscribe sync: {exc}", "DEBUG")

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="exit-realtime-ws", daemon=True
        )
        self._thread.start()
        mode = exit_realtime_mode(self._raw)
        log(f"exit_realtime hub started mode={mode}", "INFO")

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

    def on_ticker(
        self,
        gate_pair: str,
        price: float,
        *,
        pct_24h: float | None = None,
        quote_volume: float | None = None,
    ) -> None:
        if price <= 0:
            return
        self._stats["ticks"] += 1
        self._stats["last_tick_at"] = time.time()
        with self._pos_lock:
            sym = (
                self._gate_to_symbol.get(gate_pair)
                or self._watch_gate.get(gate_pair)
                or from_gate_pair(gate_pair)
            )
            self._last_prices[sym] = float(price)
            in_watch = sym in self._watch_symbols or gate_pair in self._watch_gate
            row = self._book.get(sym)
            snapshot = None
            if row:
                rh = float(row.get("recent_high") or 0)
                if price > rh:
                    row["recent_high"] = price
                    # keep nested position peak in sync for eval
                    pos0 = row.get("position")
                    if isinstance(pos0, dict):
                        pos0["recent_high"] = price
                snapshot = {
                    "symbol": row.get("symbol"),
                    "timeframe": row.get("timeframe"),
                    "strategy_params": dict(row.get("strategy_params") or {}),
                    "atr_pct": row.get("atr_pct"),
                    "position": dict(row.get("position") or {}),
                    "recent_high": row.get("recent_high"),
                }
                # ensure position has peak + entry
                pos = snapshot["position"]
                if snapshot.get("recent_high"):
                    pos["recent_high"] = snapshot["recent_high"]
                if not pos.get("average_entry") and row.get("average_entry"):
                    pos["average_entry"] = row["average_entry"]

        # Identify board (watch or any tick we track) — never places orders
        if in_watch or snapshot is None:
            try:
                from services.gainer_universe.ws_board import (
                    get_ws_board,
                    ws_board_enabled,
                )

                if ws_board_enabled(self._raw if isinstance(self._raw, dict) else None):
                    get_ws_board().on_tick(
                        sym,
                        last=float(price),
                        pct_24h=pct_24h,
                        quote_volume=quote_volume,
                    )
                    get_ws_board().maybe_log_board(
                        self._raw if isinstance(self._raw, dict) else None
                    )
            except Exception:
                pass

        # No open position → identify-only path done
        if snapshot is None:
            return

        # GUI tick stream (UI further samples feed lines)
        self._broadcast_gui(
            {
                "type": "tick",
                "stage": "tick_in",
                "symbol": sym,
                "last": price,
                "delta_pct": 0,
            }
        )

        cfg = exit_realtime_config(self._raw)
        mode = exit_realtime_mode(self._raw)
        sources = exit_realtime_sources(self._raw)
        cooldown = float(
            cfg.get("live_cooldown_sec")
            or cfg.get("shadow_cooldown_sec")
            or 15
            or 15
        )
        atr = float(snapshot.get("atr_pct") or cfg.get("default_atr_pct", 3.0) or 3.0)
        params = dict(snapshot.get("strategy_params") or {})
        tf = str(snapshot.get("timeframe") or "1h")
        pos = dict(snapshot.get("position") or {})

        # Position lock: skip trail eval noise for locked lots (execute/risk still hard-block)
        try:
            from strategies.position_lock import attach_lock_from_ledger, auto_sell_blocked
            from strategies.positions import get_position

            live_pos = get_position(sym, tf) or pos
            live_pos = attach_lock_from_ledger(live_pos, sym, tf) or live_pos
            locked, _ = auto_sell_blocked(live_pos, "exit_ws")
            if locked:
                return
        except Exception:
            # Fail-closed on lock-check errors: do not evaluate trail sells
            return

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
            # Skip pure strategy-shadow candidates (mode=shadow inside trail config)
            if ev.get("strategy_shadow") and mode == "live":
                # still fire if strategy is live; strategy_shadow means trail rule in shadow
                continue
            src = str(ev.get("source") or "")
            if not self._debounce_ok(sym, src, cooldown):
                continue

            ev["mode"] = mode
            ev["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self._stats["fires"] += 1

            if mode != "live":
                # log-only path (if someone leaves mode=shadow)
                _log_event(ev, live=False)
                continue

            result = try_execute_trail_exit(
                symbol=sym,
                timeframe=tf,
                price=price,
                action=str(ev.get("action") or "SELL_FULL"),
                exit_source=src,
                rationale=str(ev.get("rationale") or ""),
            )
            ev["executed"] = bool(result.get("executed"))
            ev["message"] = result.get("message")
            if result.get("executed"):
                self._stats["executed"] += 1
                # drop from book so we stop ticking this symbol until refresh
                with self._pos_lock:
                    self._book.pop(sym, None)
                    gp = to_gate_pair(sym)
                    self._gate_to_symbol.pop(gp, None)
                    self._stats["symbols"] = len(self._book)
                self._broadcast_gui(
                    {
                        "type": "would_exit",
                        "stage": "exit_eval",
                        "symbol": sym,
                        "msg": f"LIVE SELL {sym} {src} @ {price}",
                        "executed": True,
                    }
                )
            else:
                self._stats["blocked"] += 1
            _log_event(ev, live=True)
            self._broadcast_gui({**ev, "type": "hub", "msg": f"{src} executed={ev.get('executed')}"})

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
            pairs = self._desired_gate_pairs()
            if not pairs:
                time.sleep(5)
                continue

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
                pair = result.get("currency_pair") or result.get("s")
                last = result.get("last") or result.get("c")
                if not pair or last is None:
                    return
                try:
                    px = float(last)
                except (TypeError, ValueError):
                    return
                pct: float | None = None
                for k in ("change_percentage", "change_percent", "change"):
                    if result.get(k) is not None:
                        try:
                            pct = float(result.get(k))
                            break
                        except (TypeError, ValueError):
                            pass
                qv: float | None = None
                for k in ("quote_volume", "quoteVolume", "base_volume"):
                    if result.get(k) is not None:
                        try:
                            qv = float(result.get(k))
                            break
                        except (TypeError, ValueError):
                            pass
                self.on_ticker(
                    str(pair).upper(),
                    px,
                    pct_24h=pct,
                    quote_volume=qv,
                )

            def on_open(ws) -> None:
                self._ws = ws
                self._connected = True
                self._stats["connected"] = True
                self._subscribed = set()
                self._broadcast_gui(
                    {"type": "stage", "stage": "connect", "msg": "Gate WSS connected"}
                )
                live_pairs = self._desired_gate_pairs()
                for gp in live_pairs:
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
                        self._subscribed.add(gp)
                        time.sleep(0.05)
                    except Exception as exc:
                        log(f"exit_realtime subscribe {gp}: {exc}", "DEBUG")
                self._broadcast_gui(
                    {
                        "type": "stage",
                        "stage": "subscribe",
                        "msg": f"subscribed {len(live_pairs)} pairs",
                    }
                )

            def on_error(_ws, err) -> None:
                log(f"exit_realtime ws error: {err}", "WARNING")
                self._broadcast_gui({"type": "error", "msg": str(err)[:160]})

            def on_close(_ws, *_a) -> None:
                self._connected = False
                self._stats["connected"] = False
                self._ws = None
                self._subscribed = set()

            try:
                ws = websocket.WebSocketApp(
                    WS_URL,
                    on_open=on_open,
                    on_message=on_message,
                    on_error=on_error,
                    on_close=on_close,
                )
                self._stats["reconnects"] += 1
                ws.run_forever(sslopt={"context": _ssl_context()}, ping_interval=20)
            except Exception as exc:
                log(f"exit_realtime run_forever: {exc}", "WARNING")
            finally:
                self._connected = False
                self._stats["connected"] = False
                self._ws = None
                self._subscribed = set()

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


_book_io_lock = threading.Lock()


def _sync_positions_from_ledger() -> None:
    """Sidecar: read-only reload of open positions from Mongo into memory.

    Uses strategies.positions.load_positions (not rebuild_positions_from_orders)
    so we never write the ledger from the radar process and avoid thrashing
    concurrent Flask IO.
    """
    try:
        from data_manager import resolve_ledger_scope
        from strategies.positions import load_positions

        scope = str(resolve_ledger_scope() or "demo")
        with _book_io_lock:
            load_positions(scope)
    except Exception as exc:
        log(f"exit_realtime ledger sync: {exc}", "DEBUG")


def _refresh_loop(raw_getter: Callable[[], dict | None]) -> None:
    from services.exit_realtime.config import is_exit_radar_sidecar_process

    while not _refresh_stop.is_set():
        hub = get_hub()
        if hub is None:
            break
        try:
            raw = raw_getter()
            if not exit_realtime_enabled(raw) or exit_realtime_mode(raw) == "off":
                time.sleep(10)
                continue
            if is_exit_radar_sidecar_process():
                _sync_positions_from_ledger()
            rows = _load_open_book(raw)
            hub.update_book(rows)
        except Exception as exc:
            log(f"exit_realtime book refresh: {exc}", "WARNING")
        cfg = exit_realtime_config(raw_getter())
        time.sleep(float(cfg.get("book_refresh_sec", 30) or 30))


def ensure_started(raw: dict | None = None) -> ExitRealtimeHub | None:
    """Idempotent start when config enables exit_realtime and this process owns the hub."""
    global _hub, _refresh_thread
    if raw is None:
        try:
            from core.config import get_bot_config

            raw = get_bot_config().raw
        except Exception:
            raw = {}

    from services.exit_realtime.config import exit_realtime_should_run_hub

    if not exit_realtime_should_run_hub(raw):
        return None
    mode = exit_realtime_mode(raw)
    if mode == "off":
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
        return _hub
