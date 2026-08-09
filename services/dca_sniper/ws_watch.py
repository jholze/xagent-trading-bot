"""Gate spot.tickers WS for sniper focus/shortlist — wakes cycle on moves."""

from __future__ import annotations

import json
import ssl
import threading
import time
from typing import Any, Callable

from logger import log

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


def to_gate_pair(symbol: str) -> str:
    s = str(symbol or "").upper().replace("-", "/")
    if "/" in s:
        a, b = s.split("/", 1)
        return f"{a}_{b}"
    return s


def from_gate_pair(pair: str) -> str:
    p = str(pair or "").upper()
    if "_" in p:
        a, b = p.split("_", 1)
        return f"{a}/{b}"
    return p


class SniperWsWatch:
    """Subscribe Gate public tickers for a dynamic symbol set."""

    def __init__(
        self,
        *,
        symbols_provider: Callable[[], list[str]],
        on_tick: Callable[[str, float], None] | None = None,
        on_wake: Callable[[str, dict], None] | None = None,
        move_pct_wake: float = 1.5,
        resync_sec: float = 45.0,
        max_subs: int = 40,
    ):
        self._symbols_provider = symbols_provider
        self._on_tick = on_tick
        self._on_wake = on_wake
        self._move_pct = float(move_pct_wake)
        self._resync_sec = float(resync_sec)
        self._max_subs = int(max_subs)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_px: dict[str, float] = {}
        self._last_wake_at: dict[str, float] = {}
        self._wake_cooldown = 30.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="dca-sniper-ws", daemon=True)
        self._thread.start()
        log("dca_sniper Gate WS watch started", "INFO")

    def stop(self) -> None:
        self._stop.set()

    def _desired_pairs(self) -> list[str]:
        raw = self._symbols_provider() or []
        pairs = []
        seen = set()
        for s in raw:
            p = to_gate_pair(s)
            if p and p not in seen:
                seen.add(p)
                pairs.append(p)
            if len(pairs) >= self._max_subs:
                break
        return pairs

    def _run(self) -> None:
        while not self._stop.is_set():
            pairs = self._desired_pairs()
            if not pairs:
                time.sleep(5)
                continue
            try:
                self._session(pairs)
            except Exception as e:
                log(f"dca_sniper WS session error: {e}", "WARNING")
                time.sleep(3)

    def _session(self, pairs: list[str]) -> None:
        try:
            import websocket  # type: ignore
        except ImportError:
            log("dca_sniper WS: websocket-client not installed — poll only", "WARNING")
            self._stop.wait(60)
            return

        last_sub = time.time()
        subscribed: set[str] = set()

        def on_message(_ws, message: str) -> None:
            try:
                msg = json.loads(message)
            except Exception:
                return
            if msg.get("event") == "update" and msg.get("channel") == CHANNEL:
                result = msg.get("result") or {}
                if isinstance(result, dict):
                    self._handle_ticker(result)
            elif msg.get("channel") == CHANNEL and isinstance(msg.get("result"), dict):
                # some gate payloads nest differently
                self._handle_ticker(msg["result"])

        def on_error(_ws, err) -> None:
            log(f"dca_sniper WS error: {err}", "DEBUG")

        def on_open(ws) -> None:
            nonlocal subscribed, last_sub
            for p in pairs:
                try:
                    ws.send(
                        json.dumps(
                            {
                                "time": int(time.time()),
                                "channel": CHANNEL,
                                "event": "subscribe",
                                "payload": [p],
                            }
                        )
                    )
                    subscribed.add(p)
                except Exception as e:
                    log(f"dca_sniper WS sub {p}: {e}", "DEBUG")
            last_sub = time.time()
            log(f"dca_sniper WS subscribed n={len(subscribed)}", "INFO")

        def on_close(_ws, *_a) -> None:
            log("dca_sniper WS closed", "DEBUG")

        ws_app = websocket.WebSocketApp(
            WS_URL,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )

        # run with periodic resync: stop if symbols change
        def runner():
            ws_app.run_forever(sslopt={"context": _ssl_context()}, ping_interval=20, ping_timeout=10)

        t = threading.Thread(target=runner, daemon=True)
        t.start()
        try:
            while not self._stop.is_set() and t.is_alive():
                time.sleep(2)
                if time.time() - last_sub >= self._resync_sec:
                    new_pairs = set(self._desired_pairs())
                    if new_pairs != subscribed:
                        try:
                            ws_app.close()
                        except Exception:
                            pass
                        break
        finally:
            try:
                ws_app.close()
            except Exception:
                pass
            t.join(timeout=3)

    def _handle_ticker(self, result: dict[str, Any]) -> None:
        pair = str(result.get("currency_pair") or result.get("symbol") or "")
        if not pair:
            return
        sym = from_gate_pair(pair)
        try:
            last = float(result.get("last") or result.get("close") or 0)
        except (TypeError, ValueError):
            last = 0.0
        if last <= 0:
            return
        if self._on_tick:
            try:
                self._on_tick(sym, last)
            except Exception:
                pass
        prev = self._last_px.get(sym)
        self._last_px[sym] = last
        if prev and prev > 0 and self._on_wake:
            move = abs(last / prev - 1.0) * 100.0
            if move >= self._move_pct:
                now = time.time()
                if now - self._last_wake_at.get(sym, 0) >= self._wake_cooldown:
                    self._last_wake_at[sym] = now
                    try:
                        self._on_wake(
                            "ws_price_move",
                            {"symbol": sym, "price": last, "move_pct": round(move, 3)},
                        )
                    except Exception:
                        pass
