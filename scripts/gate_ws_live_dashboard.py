#!/usr/bin/env python3
"""Realtime Exit Radar GUI — Gate WS + trail proximity (probe UI).

UI: tools/exit_radar/static/index.html (Interior.dev-inspired motion patterns).
Backend: public Gate spot.tickers + open ledger positions. Also surfaces bot
exit_realtime hub stats when running in-process.

Usage:
  python3 scripts/gate_ws_live_dashboard.py --ledger-scope demo --port 8765

This process does not place orders. Live sells are handled by the bot hub
(exit_realtime); this GUI visualizes radar + optional hub counters.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import signal
import ssl
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

WS_URL = "wss://api.gateio.ws/ws/v4/"
CHANNEL = "spot.tickers"
DEFAULT_PAIRS = (
    "BTC/USDT",
    "ETH/USDT",
    "SOL/USDT",
    "DRAMG/USDT",
    "MUG/USDT",
    "UNI/USDT",
    "XPL/USDT",
    "AAVE/USDT",
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


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    try:
        import certifi

        ctx.load_verify_locations(certifi.where())
    except Exception:
        pass
    return ctx


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]


# Domain logic lives in services.exit_radar (canonical).
from services.exit_radar import (  # noqa: E402
    evaluate_position,
    fetch_dca_sniper_status,
    load_open_positions,
)
from services.exit_radar.eval import hours_since as _hours_since  # noqa: E402
from services.exit_radar.eval import resolve_trail_pct as _resolve_trail_pct  # noqa: E402


@dataclass
class PairState:
    last: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    change_24h: float = 0.0
    updates: int = 0
    last_delta_pct: float = 0.0
    last_ts: float = 0.0
    flash: int = 0


@dataclass
class Hub:
    pairs: list[str]
    positions: dict[str, dict[str, Any]] = field(default_factory=dict)
    min_delta_pct: float = 0.05
    enqueue_threshold_pct: float = 0.15
    event_q: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=2000))
    clients: list[queue.Queue] = field(default_factory=list)
    clients_lock: threading.Lock = field(default_factory=threading.Lock)
    by_pair: dict[str, PairState] = field(default_factory=dict)
    exit_cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    prev_would: dict[str, frozenset] = field(default_factory=dict)
    stats: dict[str, Any] = field(
        default_factory=lambda: {
            "messages": 0,
            "ticker_updates": 0,
            "subscribe_acks": 0,
            "errors": 0,
            "reconnects": 0,
            "would_enqueue": 0,
            "would_exit_fires": 0,
            "started_at": time.time(),
            "connected": False,
            "subscribed": False,
            "last_stage": "idle",
            "stages": {
                "connect": 0,
                "subscribe": 0,
                "tick_in": 0,
                "delta": 0,
                "enqueue": 0,
                "exit_eval": 0,
            },
        }
    )
    feed: list[dict] = field(default_factory=list)
    feed_lock: threading.Lock = field(default_factory=threading.Lock)
    stop: threading.Event = field(default_factory=threading.Event)
    pos_lock: threading.Lock = field(default_factory=threading.Lock)

    def broadcast(self, event: dict) -> None:
        event.setdefault("ts", _now_iso())
        event.setdefault("server_ts", time.time())
        with self.feed_lock:
            self.feed.append(event)
            if len(self.feed) > 200:
                self.feed = self.feed[-150:]
        dead: list[queue.Queue] = []
        with self.clients_lock:
            for cq in self.clients:
                try:
                    cq.put_nowait(event)
                except queue.Full:
                    dead.append(cq)
            for cq in dead:
                try:
                    self.clients.remove(cq)
                except ValueError:
                    pass

    def eval_symbol(self, symbol: str, price: float) -> dict[str, Any] | None:
        with self.pos_lock:
            pos = self.positions.get(symbol)
            if not pos:
                return None
            # atr estimate from 24h change magnitude if available
            pair = to_gate_pair(symbol)
            st = self.by_pair.get(pair)
            atr_est = 5.0
            if st and st.change_24h:
                atr_est = max(2.0, min(15.0, abs(float(st.change_24h)) * 0.35 + 2.0))
            row = evaluate_position(pos, price, atr_pct_est=atr_est)
            self.exit_cache[symbol] = row

            # transition alerts
            sources = frozenset(row.get("would_sources") or [])
            prev = self.prev_would.get(symbol, frozenset())
            new_hits = sources - prev
            if new_hits:
                self.stats["would_exit_fires"] += 1
                self.stats["last_stage"] = "exit_eval"
                self.stats["stages"]["exit_eval"] = int(time.time() * 1000)
                self.broadcast(
                    {
                        "type": "would_exit",
                        "stage": "exit_eval",
                        "symbol": symbol,
                        "sources": sorted(new_hits),
                        "gain_pct": row.get("gain_pct"),
                        "peak_gain_pct": row.get("peak_gain_pct"),
                        "drop_from_high_pct": row.get("drop_from_high_pct"),
                        "msg": (
                            f"WOULD EXIT {symbol} via {', '.join(sorted(new_hits))} "
                            f"(gain={row.get('gain_pct')}% peak={row.get('peak_gain_pct')}% "
                            f"drop={row.get('drop_from_high_pct')}%)"
                        ),
                    }
                )
            self.prev_would[symbol] = sources
            return row

    def snapshot(self) -> dict:
        elapsed = max(0.001, time.time() - float(self.stats["started_at"]))
        rows = []
        for pair, st in sorted(self.by_pair.items()):
            rows.append(
                {
                    "symbol": from_gate_pair(pair),
                    "pair": pair,
                    "last": st.last,
                    "bid": st.bid,
                    "ask": st.ask,
                    "change_24h": st.change_24h,
                    "updates": st.updates,
                    "last_delta_pct": st.last_delta_pct,
                    "age_ms": int((time.time() - st.last_ts) * 1000)
                    if st.last_ts
                    else None,
                    "flash": st.flash,
                }
            )
            if st.flash > 0:
                st.flash = max(0, st.flash - 1)

        # re-eval exits with last prices
        exits: list[dict] = []
        with self.pos_lock:
            for symbol, pos in self.positions.items():
                pair = to_gate_pair(symbol)
                st = self.by_pair.get(pair)
                price = st.last if st and st.last > 0 else 0.0
                if price <= 0:
                    # still show position with entry only
                    exits.append(
                        {
                            "ok": True,
                            "symbol": symbol,
                            "price": 0,
                            "entry": pos["entry"],
                            "gain_pct": None,
                            "status": "waiting_tick",
                            "urgency": 0,
                            "would_exit": False,
                            "near_exit": False,
                            "would_sources": [],
                            "near_sources": [],
                        }
                    )
                    continue
                atr_est = 5.0
                if st and st.change_24h:
                    atr_est = max(
                        2.0, min(15.0, abs(float(st.change_24h)) * 0.35 + 2.0)
                    )
                row = evaluate_position(pos, price, atr_pct_est=atr_est)
                self.exit_cache[symbol] = row
                exits.append(row)

        exits.sort(
            key=lambda r: (
                0 if r.get("would_exit") else 1 if r.get("near_exit") else 2,
                -float(r.get("urgency") or 0),
                str(r.get("symbol") or ""),
            )
        )

        n_would = sum(1 for e in exits if e.get("would_exit"))
        n_near = sum(1 for e in exits if e.get("near_exit") and not e.get("would_exit"))
        n_armed = sum(
            1
            for e in exits
            if (e.get("ttp") or {}).get("armed")
            or (e.get("trailing_stop") or {}).get("active")
        )
        n_profit = sum(
            1 for e in exits if (e.get("gain_pct") is not None and e["gain_pct"] > 0)
        )
        n_loss = sum(
            1 for e in exits if (e.get("gain_pct") is not None and e["gain_pct"] < 0)
        )
        total_pnl = sum(float(e.get("pnl_usdt") or 0) for e in exits if e.get("price"))
        total_notional = sum(
            float(e.get("notional_usdt") or 0) for e in exits if e.get("price")
        )

        bot_hub: dict[str, Any] = {"running": False, "disabled": True}
        try:
            from services.exit_realtime.config import (
                exit_realtime_enabled,
                exit_realtime_mode,
            )
            from services.exit_realtime.hub import get_hub

            if not exit_realtime_enabled():
                bot_hub = {"running": False, "disabled": True}
            else:
                h = get_hub()
                if h is None:
                    bot_hub = {
                        "running": False,
                        "disabled": False,
                        "mode": exit_realtime_mode(),
                    }
                else:
                    st = h.stats()
                    bot_hub = {
                        "running": True,
                        "disabled": False,
                        "mode": exit_realtime_mode(),
                        **st,
                    }
        except Exception:
            bot_hub = {"running": False, "disabled": True, "error": True}

        n_hold = sum(1 for e in exits if e.get("recovery_hold") or e.get("sniper_focus"))
        n_hold_block = sum(1 for e in exits if e.get("blocked_by_hold"))
        dca_sniper = fetch_dca_sniper_status()

        return {
            "type": "snapshot",
            "connected": self.stats["connected"],
            "subscribed": self.stats["subscribed"],
            "last_stage": self.stats["last_stage"],
            "stages": dict(self.stats["stages"]),
            "bot_hub": bot_hub,
            "dca_sniper": dca_sniper,
            "stats": {
                "messages": self.stats["messages"],
                "ticker_updates": self.stats["ticker_updates"],
                "subscribe_acks": self.stats["subscribe_acks"],
                "errors": self.stats["errors"],
                "would_enqueue": self.stats["would_enqueue"],
                "would_exit_fires": self.stats["would_exit_fires"],
                "updates_per_sec": round(self.stats["ticker_updates"] / elapsed, 2),
                "elapsed_sec": round(elapsed, 1),
                "clients": len(self.clients),
                "positions": len(self.positions),
            },
            "exit_summary": {
                "positions": len(exits),
                "would_exit": n_would,
                "near_exit": n_near,
                "armed": n_armed,
                "in_profit": n_profit,
                "in_loss": n_loss,
                "recovery_hold": n_hold,
                "hold_blocked": n_hold_block,
                "total_pnl_usdt": round(total_pnl, 2),
                "total_notional_usdt": round(total_notional, 2),
            },
            "pairs": rows,
            "exits": exits,
            "pipeline_pairs": [from_gate_pair(to_gate_pair(p)) for p in self.pairs],
            "thresholds": {
                "min_delta_pct": self.min_delta_pct,
                "enqueue_threshold_pct": self.enqueue_threshold_pct,
            },
        }


def start_gate_ws(hub: Hub) -> threading.Thread:
    try:
        import websocket
    except ImportError as e:
        raise SystemExit(
            "Need websocket-client: pip install websocket-client"
        ) from e

    gate_pairs = [to_gate_pair(p) for p in hub.pairs]

    def on_open(ws: Any) -> None:
        hub.stats["connected"] = True
        hub.stats["last_stage"] = "connect"
        hub.stats["stages"]["connect"] = int(time.time() * 1000)
        hub.broadcast(
            {"type": "stage", "stage": "connect", "msg": f"connected {WS_URL}"}
        )
        # One pair per subscribe: Gate rejects the whole batch if any pair is unknown.
        for gp in gate_pairs:
            sub = {
                "time": int(time.time()),
                "channel": CHANNEL,
                "event": "subscribe",
                "payload": [gp],
            }
            try:
                ws.send(json.dumps(sub))
            except Exception as e:
                hub.broadcast({"type": "error", "msg": f"subscribe send {gp}: {e}"})
        hub.stats["last_stage"] = "subscribe"
        hub.stats["stages"]["subscribe"] = int(time.time() * 1000)
        hub.broadcast(
            {
                "type": "stage",
                "stage": "subscribe",
                "msg": f"subscribe {len(gate_pairs)} pairs (1-by-1)",
                "pairs": gate_pairs,
            }
        )

    def on_message(ws: Any, message: str) -> None:
        hub.stats["messages"] += 1
        try:
            msg = json.loads(message)
        except Exception:
            hub.stats["errors"] += 1
            hub.broadcast({"type": "error", "msg": "non-json frame"})
            return

        channel = msg.get("channel")
        event = msg.get("event")
        if event == "subscribe":
            hub.stats["subscribe_acks"] += 1
            if msg.get("error"):
                hub.stats["errors"] += 1
                payload = msg.get("payload") or []
                hub.broadcast(
                    {
                        "type": "error",
                        "msg": f"subscribe fail {payload}: {msg.get('error')}",
                    }
                )
            else:
                # any successful pair is enough to mark stream live
                hub.stats["subscribed"] = True
            return
        if channel != CHANNEL or event != "update":
            return

        result = msg.get("result")
        items: list[dict] = []
        if isinstance(result, dict):
            items = [result]
        elif isinstance(result, list):
            items = [x for x in result if isinstance(x, dict)]

        for item in items:
            pair = str(item.get("currency_pair") or "")
            try:
                last = float(item.get("last") or 0)
            except (TypeError, ValueError):
                continue
            if not pair or last <= 0:
                continue
            try:
                bid = float(item.get("highest_bid") or 0)
            except (TypeError, ValueError):
                bid = 0.0
            try:
                ask = float(item.get("lowest_ask") or 0)
            except (TypeError, ValueError):
                ask = 0.0
            try:
                chg = float(item.get("change_percentage") or 0)
            except (TypeError, ValueError):
                chg = 0.0

            st = hub.by_pair.setdefault(pair, PairState())
            prev = st.last
            delta_pct = abs(last / prev - 1.0) * 100.0 if prev > 0 else 0.0
            st.last = last
            st.bid = bid
            st.ask = ask
            st.change_24h = chg
            st.updates += 1
            st.last_delta_pct = delta_pct
            st.last_ts = time.time()
            st.flash = 3
            hub.stats["ticker_updates"] += 1
            hub.stats["last_stage"] = "tick_in"
            hub.stats["stages"]["tick_in"] = int(time.time() * 1000)

            sym = from_gate_pair(pair)
            hub.broadcast(
                {
                    "type": "tick",
                    "stage": "tick_in",
                    "symbol": sym,
                    "pair": pair,
                    "last": last,
                    "bid": bid,
                    "ask": ask,
                    "change_24h": chg,
                    "delta_pct": round(delta_pct, 4),
                    "updates": st.updates,
                }
            )

            # Exit radar eval
            exit_row = hub.eval_symbol(sym, last)
            if exit_row and (
                exit_row.get("would_exit")
                or exit_row.get("near_exit")
                or delta_pct >= hub.min_delta_pct
            ):
                hub.stats["stages"]["exit_eval"] = int(time.time() * 1000)
                hub.broadcast(
                    {
                        "type": "exit_update",
                        "stage": "exit_eval",
                        "symbol": sym,
                        "exit": exit_row,
                    }
                )

            if prev > 0 and delta_pct >= hub.min_delta_pct:
                hub.stats["last_stage"] = "delta"
                hub.stats["stages"]["delta"] = int(time.time() * 1000)
                hub.broadcast(
                    {
                        "type": "delta",
                        "stage": "delta",
                        "symbol": sym,
                        "delta_pct": round(delta_pct, 4),
                        "last": last,
                        "msg": f"{sym} moved {delta_pct:+.3f}%",
                    }
                )

            if prev > 0 and delta_pct >= hub.enqueue_threshold_pct:
                hub.stats["would_enqueue"] += 1
                hub.stats["last_stage"] = "enqueue"
                hub.stats["stages"]["enqueue"] = int(time.time() * 1000)
                hub.broadcast(
                    {
                        "type": "enqueue",
                        "stage": "enqueue",
                        "symbol": sym,
                        "delta_pct": round(delta_pct, 4),
                        "priority": 20,
                        "reason": "position_delta",
                        "msg": (
                            f"WOULD enqueue {sym} priority=20 "
                            f"(|Δ|={delta_pct:.3f}% ≥ {hub.enqueue_threshold_pct}%)"
                        ),
                    }
                )

    def on_error(ws: Any, error: Any) -> None:
        hub.stats["errors"] += 1
        hub.stats["connected"] = False
        hub.broadcast({"type": "error", "msg": str(error)})

    def on_close(ws: Any, code: Any, msg: Any) -> None:
        hub.stats["connected"] = False
        hub.stats["subscribed"] = False
        hub.broadcast(
            {"type": "stage", "stage": "idle", "msg": f"ws closed code={code}"}
        )

    def _run() -> None:
        while not hub.stop.is_set():
            ws = websocket.WebSocketApp(
                WS_URL,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            try:
                ws.run_forever(
                    ping_interval=20,
                    ping_timeout=10,
                    sslopt={"context": _ssl_context()},
                )
            except Exception as e:
                hub.stats["errors"] += 1
                hub.broadcast({"type": "error", "msg": f"run_forever: {e}"})
            if hub.stop.is_set():
                break
            hub.stats["reconnects"] += 1
            hub.broadcast(
                {
                    "type": "stage",
                    "stage": "connect",
                    "msg": "reconnecting in 2s…",
                }
            )
            time.sleep(2.0)

    th = threading.Thread(target=_run, name="gate-ws", daemon=True)
    th.start()
    return th


def _load_dashboard_html() -> str:
    """Prefer Interior-inspired GUI; fallback to minimal inline page."""
    path = ROOT / "tools" / "exit_radar" / "static" / "index.html"
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return (
            "<!DOCTYPE html><html><body><h1>Exit Radar</h1>"
            "<p>Missing tools/exit_radar/static/index.html</p></body></html>"
        )



# UI: tools/exit_radar/static/index.html (via _load_dashboard_html)

def make_handler(hub: Hub):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            if args and str(args[0]).startswith("GET /events"):
                return
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                html = _load_dashboard_html()
                self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
                return
            if path == "/api/snapshot":
                body = json.dumps(hub.snapshot()).encode("utf-8")
                self._send(200, body, "application/json")
                return
            if path == "/api/health":
                body = json.dumps(
                    {
                        "ok": True,
                        "connected": hub.stats["connected"],
                        "updates": hub.stats["ticker_updates"],
                        "positions": len(hub.positions),
                        "gui": "tools/exit_radar/static/index.html",
                    }
                ).encode("utf-8")
                self._send(200, body, "application/json")
                return
            if path == "/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                cq: queue.Queue = queue.Queue(maxsize=500)
                with hub.clients_lock:
                    hub.clients.append(cq)
                try:
                    init = json.dumps(hub.snapshot())
                    self.wfile.write(f"data: {init}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except Exception:
                    return
                try:
                    while not hub.stop.is_set():
                        try:
                            ev = cq.get(timeout=15.0)
                            payload = json.dumps(ev)
                            self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                            self.wfile.flush()
                        except queue.Empty:
                            try:
                                self.wfile.write(b": ping\n\n")
                                self.wfile.flush()
                            except Exception:
                                break
                finally:
                    with hub.clients_lock:
                        try:
                            hub.clients.remove(cq)
                        except ValueError:
                            pass
                return
            self._send(404, b"not found", "text/plain")

    return Handler


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Live Gate WS + exit-rule radar dashboard"
    )
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument(
        "--pairs",
        default="",
        help="Comma-separated symbols (default: open ledger or built-in defaults)",
    )
    p.add_argument(
        "--ledger-scope",
        default="demo",
        help="Ledger scope for open positions (default: demo)",
    )
    p.add_argument("--no-ledger", action="store_true", help="Skip ledger load")
    p.add_argument("--min-delta-pct", type=float, default=0.05)
    p.add_argument("--enqueue-threshold-pct", type=float, default=0.12)
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args(argv)

    positions_list: list[dict[str, Any]] = []
    if not args.no_ledger:
        try:
            positions_list = load_open_positions(str(args.ledger_scope))
            print(
                f"[{_now_iso()}] loaded {len(positions_list)} open positions "
                f"(scope={args.ledger_scope})",
                flush=True,
            )
        except Exception as e:
            print(f"[{_now_iso()}] ledger load failed: {e}", flush=True)

    if args.pairs.strip():
        pairs = [x.strip() for x in str(args.pairs).split(",") if x.strip()]
    elif positions_list:
        pairs = sorted({r["symbol"] for r in positions_list})
    else:
        pairs = list(DEFAULT_PAIRS)

    if not pairs:
        print("No pairs", file=sys.stderr)
        return 2

    pos_map = {r["symbol"]: r for r in positions_list}
    hub = Hub(
        pairs=pairs,
        positions=pos_map,
        min_delta_pct=float(args.min_delta_pct),
        enqueue_threshold_pct=float(args.enqueue_threshold_pct),
    )
    start_gate_ws(hub)

    handler = make_handler(hub)
    server = ThreadingHTTPServer((args.host, int(args.port)), handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"[{_now_iso()}] dashboard {url}", flush=True)
    print(f"[{_now_iso()}] pairs ({len(pairs)}): {', '.join(pairs[:12])}"
          f"{'…' if len(pairs)>12 else ''}", flush=True)
    print(
        f"[{_now_iso()}] min_delta={args.min_delta_pct}%  "
        f"enqueue_sim≥{args.enqueue_threshold_pct}%  "
        f"positions={len(pos_map)}",
        flush=True,
    )
    print(f"[{_now_iso()}] Ctrl+C to stop", flush=True)

    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    def _stop(*_a: Any) -> None:
        hub.stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        hub.stop.set()
        server.server_close()
        print(
            f"[{_now_iso()}] stopped. updates={hub.stats['ticker_updates']} "
            f"would_enqueue={hub.stats['would_enqueue']} "
            f"exit_fires={hub.stats['would_exit_fires']}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
