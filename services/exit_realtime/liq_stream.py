"""Gate public futures liquidations WS (second connection in the exit-realtime hub)."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from logger import log

SIDE_LONG = "long"  # position size > 0 → long liquidated → dump
SIDE_SHORT = "short"  # position size < 0 → short liquidated → pump

BTC_CONTRACT = "BTC_USDT"


@dataclass(frozen=True)
class ParsedLiq:
    ts_ms: int
    side: str
    usd: Decimal
    contract: str
    size: Decimal
    price: Decimal


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    if d.is_nan() or d.is_infinite():
        return None
    return d


def quanto_for_contract(contract: str, *, quanto_btc: Decimal | float) -> Decimal:
    """Known quanto is BTC_USDT only. Unknown or non-positive → 0 (caller drops)."""
    c = str(contract or "").strip().upper()
    if c != BTC_CONTRACT:
        return Decimal("0")
    q = _decimal(quanto_btc)
    if q is None or q <= 0:
        return Decimal("0")
    return q


def parse_liq_event(
    item: dict[str, Any],
    *,
    quanto_btc: Decimal | float = Decimal("0.0001"),
) -> ParsedLiq | None:
    """Parse one public_liquidates row. Position size > 0 = long liq (dump)."""
    if not isinstance(item, dict):
        return None
    contract = str(item.get("contract") or item.get("s") or "").strip().upper()
    size = _decimal(item.get("size"))
    price = _decimal(item.get("price") or item.get("p"))
    if size is None or price is None or price <= 0:
        return None
    if size == 0:
        return None
    ts_raw = item.get("time") or item.get("t") or item.get("create_time")
    ts = _decimal(ts_raw)
    if ts is None:
        return None
    ts_ms = int(ts)
    if ts_ms < 10_000_000_000:
        ts_ms *= 1000
    quanto = quanto_for_contract(contract, quanto_btc=quanto_btc)
    if quanto <= 0:
        log(f"liq_stream drop unknown quanto contract={contract}", "DEBUG")
        return None
    usd = abs(size) * quanto * price
    if usd <= 0:
        return None
    side = SIDE_LONG if size > 0 else SIDE_SHORT
    return ParsedLiq(
        ts_ms=ts_ms,
        side=side,
        usd=usd,
        contract=contract or "UNKNOWN",
        size=size,
        price=price,
    )


def parse_liq_batch(
    payload: Any,
    *,
    quanto_btc: Decimal | float = Decimal("0.0001"),
) -> list[ParsedLiq]:
    """Parse a WS result array (or wrapped message). Dedup (contract, time, size)."""
    rows: list[Any]
    if isinstance(payload, dict):
        if payload.get("event") in ("subscribe", "unsubscribe"):
            return []
        result = payload.get("result", payload)
        if isinstance(result, list):
            rows = result
        elif isinstance(result, dict):
            rows = [result]
        else:
            return []
    elif isinstance(payload, list):
        rows = payload
    else:
        return []

    out: list[ParsedLiq] = []
    seen: set[tuple[str, int, str]] = set()
    for item in rows:
        parsed = parse_liq_event(item, quanto_btc=quanto_btc)
        if parsed is None:
            continue
        key = (parsed.contract, parsed.ts_ms, str(parsed.size))
        if key in seen:
            continue
        seen.add(key)
        out.append(parsed)
    return out


class LiqStream:
    """Second public WS. Own reconnect loop, same process as the ticker hub."""

    def __init__(
        self,
        *,
        stop_event: threading.Event,
        config: dict[str, Any],
        on_batch: Callable[[Any], None],
        ssl_context_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._stop = stop_event
        self._cfg = dict(config or {})
        self._on_batch = on_batch
        self._ssl_context_factory = ssl_context_factory
        self._thread: threading.Thread | None = None
        self._app: Any = None
        self._ws: Any = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run_loop, name="exit-realtime-liq", daemon=True
        )
        self._thread.start()
        log(
            f"liq_stream started url={self._cfg.get('ws_url')} "
            f"channel={self._cfg.get('channel')} payload={self._cfg.get('payload')}",
            "INFO",
        )

    def stop(self) -> None:
        app = self._app
        if app is not None:
            try:
                app.keep_running = False
            except Exception:
                pass
            try:
                app.close()
            except Exception:
                pass
        ws = self._ws
        if ws is not None and ws is not app:
            try:
                ws.close()
            except Exception:
                pass
        thread = self._thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=2.0)

    def _run_loop(self) -> None:
        try:
            import websocket
        except ImportError:
            log(
                "liq_stream: websocket-client missing — cascade idle "
                "(pip install websocket-client)",
                "WARNING",
            )
            return

        url = str(self._cfg.get("ws_url") or "wss://fx-ws.gateio.ws/v4/ws/usdt")
        channel = str(self._cfg.get("channel") or "futures.public_liquidates")
        payload = str(self._cfg.get("payload") or "!all")
        backoff = 3.0
        max_backoff = 60.0

        while not self._stop.is_set():

            def on_message(_ws, message: str) -> None:
                try:
                    data = json.loads(message)
                except Exception:
                    return
                try:
                    self._on_batch(data)
                except Exception as exc:
                    log(f"liq_stream on_batch: {exc}", "DEBUG")

            def on_open(ws) -> None:
                self._ws = ws
                try:
                    ws.send(
                        json.dumps(
                            {
                                "time": int(time.time()),
                                "channel": channel,
                                "event": "subscribe",
                                "payload": [payload],
                            }
                        )
                    )
                except Exception as exc:
                    log(f"liq_stream subscribe: {exc}", "WARNING")

            def on_error(_ws, err) -> None:
                log(f"liq_stream ws error: {err}", "WARNING")

            def on_close(_ws, *_a) -> None:
                self._ws = None

            sslopt = {}
            if self._ssl_context_factory is not None:
                try:
                    sslopt = {"context": self._ssl_context_factory()}
                except Exception:
                    sslopt = {}

            try:
                ws = websocket.WebSocketApp(
                    url,
                    on_open=on_open,
                    on_message=on_message,
                    on_error=on_error,
                    on_close=on_close,
                )
                self._app = ws
                ws.run_forever(sslopt=sslopt, ping_interval=20)
            except Exception as exc:
                log(f"liq_stream run_forever: {exc}", "WARNING")
            finally:
                self._ws = None
                self._app = None

            if self._stop.is_set():
                break
            if self._stop.wait(backoff):
                break
            backoff = min(max_backoff, backoff * 1.5)
