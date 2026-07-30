#!/usr/bin/env python3
"""Probe Gate.io public Spot WebSocket tickers (terminal stream).

Feature branch helper — no trading, no ledger writes.

Usage:
  /usr/local/bin/python3.13 scripts/probe_gate_ws_tickers.py
  /usr/local/bin/python3.13 scripts/probe_gate_ws_tickers.py --pairs BTC/USDT,ETH/USDT,SOL/USDT
  /usr/local/bin/python3.13 scripts/probe_gate_ws_tickers.py --seconds 30 --min-delta-pct 0.05

Endpoint: wss://api.gateio.ws/ws/v4/
Channel:  spot.tickers  (public, no auth)
"""

from __future__ import annotations

import argparse
import json
import signal
import ssl
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


WS_URL = "wss://api.gateio.ws/ws/v4/"
CHANNEL = "spot.tickers"


def _ssl_context() -> ssl.SSLContext:
    """Prefer certifi CA bundle (macOS Python often lacks system roots)."""
    ctx = ssl.create_default_context()
    try:
        import certifi

        ctx.load_verify_locations(certifi.where())
    except Exception:
        pass
    return ctx
DEFAULT_PAIRS = (
    "BTC/USDT",
    "ETH/USDT",
    "SOL/USDT",
    "XRP/USDT",
    "BNB/USDT",
)


def to_gate_pair(symbol: str) -> str:
    s = str(symbol or "").strip().upper().replace("-", "/")
    if "/" in s:
        base, quote = s.split("/", 1)
        return f"{base}_{quote}"
    if s.endswith("USDT"):
        return f"{s[:-4]}_USDT"
    return s


def from_gate_pair(pair: str) -> str:
    p = str(pair or "").strip().upper()
    if "_" in p:
        return p.replace("_", "/", 1)
    return p


@dataclass
class TickerState:
    last: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    change_pct: float = 0.0
    updates: int = 0
    last_print_at: float = 0.0
    last_printed_px: float = 0.0


@dataclass
class StreamStats:
    messages: int = 0
    ticker_updates: int = 0
    subscribe_acks: int = 0
    errors: int = 0
    reconnects: int = 0
    started_at: float = field(default_factory=time.time)
    by_pair: dict[str, TickerState] = field(default_factory=dict)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def _parse_ticker_result(result: dict[str, Any]) -> tuple[str, float, float, float, float] | None:
    """Return (currency_pair, last, bid, ask, change_pct) or None."""
    if not isinstance(result, dict):
        return None
    pair = str(result.get("currency_pair") or result.get("s") or "")
    if not pair:
        return None
    try:
        last = float(result.get("last") or 0)
    except (TypeError, ValueError):
        last = 0.0
    if last <= 0:
        return None
    try:
        bid = float(result.get("highest_bid") or result.get("bid") or 0)
    except (TypeError, ValueError):
        bid = 0.0
    try:
        ask = float(result.get("lowest_ask") or result.get("ask") or 0)
    except (TypeError, ValueError):
        ask = 0.0
    try:
        chg = float(result.get("change_percentage") or result.get("change") or 0)
    except (TypeError, ValueError):
        chg = 0.0
    return pair, last, bid, ask, chg


def run_stream(
    pairs: list[str],
    *,
    seconds: float = 45.0,
    min_delta_pct: float = 0.02,
    print_all: bool = False,
    quiet_ping: bool = True,
) -> StreamStats:
    try:
        import websocket  # websocket-client
    except ImportError as e:
        raise SystemExit(
            "Missing dependency: websocket-client\n"
            "  pip install websocket-client\n"
            "  or use: /usr/local/bin/python3.13 (project env)"
        ) from e

    gate_pairs = [to_gate_pair(p) for p in pairs]
    stats = StreamStats()
    stop = threading.Event()
    ws_holder: dict[str, Any] = {}

    def on_open(ws: Any) -> None:
        sub = {
            "time": int(time.time()),
            "channel": CHANNEL,
            "event": "subscribe",
            "payload": gate_pairs,
        }
        ws.send(json.dumps(sub))
        print(
            f"[{_now_iso()}] subscribed {CHANNEL} → {', '.join(gate_pairs)}",
            flush=True,
        )

    def on_message(ws: Any, message: str) -> None:
        stats.messages += 1
        try:
            msg = json.loads(message)
        except Exception:
            stats.errors += 1
            print(f"[{_now_iso()}] non-json: {message[:120]!r}", flush=True)
            return

        channel = msg.get("channel")
        event = msg.get("event")
        # subscribe ack
        if event == "subscribe":
            stats.subscribe_acks += 1
            err = msg.get("error")
            if err:
                stats.errors += 1
                print(f"[{_now_iso()}] subscribe error: {err}", flush=True)
            else:
                print(f"[{_now_iso()}] subscribe ok channel={channel}", flush=True)
            return
        if event == "unsubscribe":
            return
        # pong / ping noise
        if channel in ("spot.pong", "spot.ping") or event in ("pong", "ping"):
            if not quiet_ping:
                print(f"[{_now_iso()}] {event or channel}", flush=True)
            return

        if channel != CHANNEL or event != "update":
            # unexpected but useful once
            if stats.messages <= 5:
                print(f"[{_now_iso()}] other msg: {json.dumps(msg)[:200]}", flush=True)
            return

        result = msg.get("result")
        # result may be dict or list
        items: list[dict] = []
        if isinstance(result, dict):
            items = [result]
        elif isinstance(result, list):
            items = [x for x in result if isinstance(x, dict)]
        else:
            return

        for item in items:
            parsed = _parse_ticker_result(item)
            if not parsed:
                continue
            pair, last, bid, ask, chg = parsed
            st = stats.by_pair.setdefault(pair, TickerState())
            prev = st.last
            st.last = last
            st.bid = bid
            st.ask = ask
            st.change_pct = chg
            st.updates += 1
            stats.ticker_updates += 1

            now = time.time()
            delta_pct = 0.0
            if prev > 0:
                delta_pct = abs(last / prev - 1.0) * 100.0
            should_print = print_all or st.updates == 1
            if not should_print and prev > 0 and delta_pct >= min_delta_pct:
                should_print = True
            # rate-limit identical prints
            if should_print and (now - st.last_print_at) < 0.15 and not print_all:
                should_print = False
            if should_print:
                st.last_print_at = now
                st.last_printed_px = last
                sym = from_gate_pair(pair)
                d_str = f"{delta_pct:+.3f}%" if prev > 0 else "  first"
                spread = ""
                if bid > 0 and ask > 0:
                    spread = f"  bid={bid:.8g} ask={ask:.8g}"
                print(
                    f"[{_now_iso()}] {sym:12} last={last:.8g}  "
                    f"Δtick={d_str:>9}  24h={chg:+.2f}%{spread}",
                    flush=True,
                )

    def on_error(ws: Any, error: Any) -> None:
        stats.errors += 1
        print(f"[{_now_iso()}] error: {error}", flush=True)

    def on_close(ws: Any, status_code: Any, msg: Any) -> None:
        print(f"[{_now_iso()}] closed code={status_code} msg={msg}", flush=True)

    def on_ping(ws: Any, data: Any) -> None:
        if not quiet_ping:
            print(f"[{_now_iso()}] ping", flush=True)

    def on_pong(ws: Any, data: Any) -> None:
        if not quiet_ping:
            print(f"[{_now_iso()}] pong", flush=True)

    def _stop(*_args: Any) -> None:
        stop.set()
        w = ws_holder.get("ws")
        if w is not None:
            try:
                w.close()
            except Exception:
                pass

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    print(f"[{_now_iso()}] connecting {WS_URL}", flush=True)
    print(
        f"[{_now_iso()}] run {seconds:.0f}s | min_delta_pct={min_delta_pct} | "
        f"print_all={print_all}",
        flush=True,
    )

    ws = websocket.WebSocketApp(
        WS_URL,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
        on_ping=on_ping,
        on_pong=on_pong,
    )
    ws_holder["ws"] = ws

    def _run() -> None:
        # ping every 20s keeps connection alive (Gate recommends client ping)
        ws.run_forever(
            ping_interval=20,
            ping_timeout=10,
            sslopt={"context": _ssl_context()},
        )

    th = threading.Thread(target=_run, name="gate-ws", daemon=True)
    th.start()

    deadline = time.time() + max(3.0, float(seconds))
    try:
        while time.time() < deadline and not stop.is_set():
            time.sleep(0.2)
    finally:
        stop.set()
        try:
            ws.close()
        except Exception:
            pass
        th.join(timeout=3.0)

    elapsed = max(0.001, time.time() - stats.started_at)
    print(flush=True)
    print("======== SUMMARY ========", flush=True)
    print(f"elapsed_sec     {elapsed:.1f}", flush=True)
    print(f"ws_messages     {stats.messages}", flush=True)
    print(f"ticker_updates  {stats.ticker_updates}", flush=True)
    print(f"subscribe_acks  {stats.subscribe_acks}", flush=True)
    print(f"errors          {stats.errors}", flush=True)
    print(f"updates/sec     {stats.ticker_updates / elapsed:.2f}", flush=True)
    print("per pair:", flush=True)
    for pair in sorted(stats.by_pair.keys()):
        st = stats.by_pair[pair]
        print(
            f"  {from_gate_pair(pair):12} updates={st.updates:4d}  "
            f"last={st.last:.8g}  24h={st.change_pct:+.2f}%",
            flush=True,
        )
    if stats.ticker_updates == 0:
        print(
            "NOTE: no ticker updates — check network/firewall or pair names.",
            flush=True,
        )
    else:
        print("OK: Gate public WS tickers stream works.", flush=True)
    return stats


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Probe Gate spot.tickers WebSocket")
    p.add_argument(
        "--pairs",
        default=",".join(DEFAULT_PAIRS),
        help="Comma-separated symbols (BTC/USDT or BTC_USDT)",
    )
    p.add_argument(
        "--seconds",
        type=float,
        default=45.0,
        help="How long to stream (default 45)",
    )
    p.add_argument(
        "--min-delta-pct",
        type=float,
        default=0.02,
        help="Print only if |Δ| from last tick >= this %% (default 0.02)",
    )
    p.add_argument(
        "--print-all",
        action="store_true",
        help="Print every ticker update (noisy)",
    )
    p.add_argument(
        "--verbose-ping",
        action="store_true",
        help="Log ping/pong frames",
    )
    args = p.parse_args(argv)
    pairs = [x.strip() for x in str(args.pairs).split(",") if x.strip()]
    if not pairs:
        print("No pairs given", file=sys.stderr)
        return 2
    try:
        stats = run_stream(
            pairs,
            seconds=args.seconds,
            min_delta_pct=args.min_delta_pct,
            print_all=args.print_all,
            quiet_ping=not args.verbose_ping,
        )
    except SystemExit:
        raise
    except Exception as e:
        print(f"FATAL: {e}", file=sys.stderr)
        return 1
    return 0 if stats.ticker_updates > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
