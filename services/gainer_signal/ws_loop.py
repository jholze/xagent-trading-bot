"""REST seed + Gate public spot.tickers WS loop for leaders board."""

from __future__ import annotations

import json
import os
import ssl
import threading
import time
from typing import Any, Callable

from logger import log
from services.gainer_signal.atr_cache import AtrPctCache
from services.gainer_signal.board import LeadersBoard, get_board
from services.gainer_signal.pure import (
    DEFAULT_ELIGIBLE_MIN_VOL,
    DEFAULT_ENTRY_POLICY,
    DEFAULT_HARD_CEILING,
    DEFAULT_HEAT_MAX,
    DEFAULT_HEAT_MIN,
    DEFAULT_RECOGNIZE_TOP_N,
    DEFAULT_SIGNAL_MAX_RANK,
    normalize_symbol,
)
from services.gainer_signal.push import push_signal_to_bot

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


def fetch_gate_tickers() -> dict[str, Any]:
    import ccxt

    ex = ccxt.gate({"enableRateLimit": True, "options": {"defaultType": "spot"}})
    return ex.fetch_tickers() or {}


def to_gate_pair(symbol: str) -> str:
    s = normalize_symbol(symbol)
    if "/" in s:
        a, b = s.split("/", 1)
        return f"{a}_{b}"
    return s


class GainerWsRuntime:
    """Background REST seed + optional WS subscribe on top symbols."""

    def __init__(
        self,
        board: LeadersBoard | None = None,
        *,
        top_n: int = DEFAULT_RECOGNIZE_TOP_N,
        min_vol: float = DEFAULT_ELIGIBLE_MIN_VOL,
        rest_seed_sec: float = 60.0,
        ws_max_subscriptions: int = 120,
        push_enabled: bool = True,
        signal_cooldown_sec: float = 300.0,
        heat_min: float = DEFAULT_HEAT_MIN,
        heat_max: float = DEFAULT_HEAT_MAX,
        signal_max_rank: int = DEFAULT_SIGNAL_MAX_RANK,
        entry_policy: str = DEFAULT_ENTRY_POLICY,
        hard_ceiling: float = DEFAULT_HARD_CEILING,
        atr_ttl_sec: float = 600.0,
    ) -> None:
        self.board = board or get_board()
        self.top_n = int(top_n)
        self.min_vol = float(min_vol)
        self.rest_seed_sec = float(rest_seed_sec)
        self.ws_max_subscriptions = int(ws_max_subscriptions)
        self.push_enabled = bool(push_enabled)
        self.signal_cooldown_sec = float(signal_cooldown_sec)
        self.heat_min = float(heat_min)
        self.heat_max = float(heat_max)
        self.signal_max_rank = int(signal_max_rank)
        self.entry_policy = str(entry_policy or DEFAULT_ENTRY_POLICY)
        self.hard_ceiling = float(hard_ceiling)
        self.atr_cache = AtrPctCache(ttl_sec=atr_ttl_sec)
        self._stop = threading.Event()
        self._rest_thread: threading.Thread | None = None
        self._ws_thread: threading.Thread | None = None
        self._last_signal_at: dict[str, float] = {}
        self._tickers_live: dict[str, dict[str, Any]] = {}
        self._tick_lock = threading.Lock()
        # RelVol on ticker stream (REST seed + WS) — not bot OHLCV mass-scan.
        # Kill: GAINER_RELVOL_ENABLED=0 OR (when bot rejects) mode!=trade in config.
        env_off = str(os.environ.get("GAINER_RELVOL_ENABLED") or "1").strip().lower() in (
            "0",
            "false",
            "no",
            "off",
        )
        self._relvol_enabled = not env_off
        self._relvol = None
        self._relvol_mode = (
            str(os.environ.get("GAINER_RELVOL_MODE") or "trade").strip().lower()
        )
        if self._relvol_enabled:
            try:
                from services.gainer_signal.relvol_tracker import RelvolTracker

                self._relvol = RelvolTracker(
                    mult=float(os.environ.get("GAINER_RELVOL_MULT") or 10),
                    baseline_hours=float(os.environ.get("GAINER_RELVOL_BASELINE_H") or 12),
                    min_ign_qvol=float(os.environ.get("GAINER_RELVOL_MIN_QVOL") or 5000),
                    cooldown_sec=float(
                        os.environ.get("GAINER_RELVOL_COOLDOWN_SEC") or 8 * 3600
                    ),
                )
            except Exception as e:
                log(f"gainer_signal relvol init skip: {e}", "WARNING")
                self._relvol = None

    def stop(self) -> None:
        self._stop.set()

    def start(self) -> None:
        self._stop.clear()
        if not self._rest_thread or not self._rest_thread.is_alive():
            self._rest_thread = threading.Thread(
                target=self._rest_loop, name="gainer-signal-rest", daemon=True
            )
            self._rest_thread.start()
        if not self._ws_thread or not self._ws_thread.is_alive():
            self._ws_thread = threading.Thread(
                target=self._ws_loop, name="gainer-signal-ws", daemon=True
            )
            self._ws_thread.start()

    def seed_once(self, tickers: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Apply one REST (or provided) ticker book and maybe emit signals."""
        data = tickers if tickers is not None else fetch_gate_tickers()
        with self._tick_lock:
            # merge
            for k, v in (data or {}).items():
                if isinstance(v, dict):
                    self._tickers_live[normalize_symbol(k) or k] = v
            merged = dict(self._tickers_live)
        leaders, _ = self.board.apply_tickers(
            merged,
            top_n=self.top_n,
            min_vol=self.min_vol,
            from_rest=True,
            max_rank_track=self.signal_max_rank,
        )
        self._maybe_emit_signals()
        self._maybe_emit_relvol(merged)
        return leaders

    def _fetch_atr_pct(self, symbol: str) -> float | None:
        try:
            from services.market_service import MarketService

            ms = MarketService()
            last = 0.0
            with self._tick_lock:
                t = self._tickers_live.get(symbol) or {}
                try:
                    last = float(t.get("last") or 0)
                except (TypeError, ValueError):
                    last = 0.0
            ind = ms.fetch_indicators(symbol, "1h", last or 1.0, limit=100)
            if not ind:
                return None
            atr_pct = ind.get("atr_pct")
            if atr_pct is None:
                return None
            return float(atr_pct)
        except Exception as e:
            log(f"gainer_signal atr fetch {symbol}: {e}", "DEBUG")
            return None

    def _atr_map_for_candidates(self) -> dict[str, float]:
        leaders = self.board.leaders()
        syms = [
            str(r.get("symbol"))
            for r in leaders
            if r.get("eligible") and int(r.get("rank") or 999) <= self.signal_max_rank
        ]
        if self.entry_policy.strip().lower() not in (
            "coin_aware_v1",
            "coin_aware",
            "v1",
            "bucket",
        ):
            # fixed_v0: ATR optional (meta only)
            return self.atr_cache.ensure_many(syms[: self.signal_max_rank], fetch_fn=self._fetch_atr_pct)
        return self.atr_cache.ensure_many(syms, fetch_fn=self._fetch_atr_pct)

    def _rest_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.seed_once()
                log(
                    f"gainer_signal rest seed recognized={self.board.stats().get('n_recognized')} "
                    f"eligible={self.board.stats().get('n_eligible')} policy={self.entry_policy}",
                    "INFO",
                )
            except Exception as e:
                log(f"gainer_signal rest seed failed: {e}", "WARNING")
            self._stop.wait(self.rest_seed_sec)

    def _maybe_emit_signals(self) -> None:
        atr_map = {}
        try:
            atr_map = self._atr_map_for_candidates()
        except Exception as e:
            log(f"gainer_signal atr map: {e}", "WARNING")
        signals = self.board.select_signals(
            heat_min=self.heat_min,
            heat_max=self.heat_max,
            max_rank=self.signal_max_rank,
            entry_policy=self.entry_policy,
            hard_ceiling=self.hard_ceiling,
            atr_by_symbol=atr_map,
        )
        self.board.note_prev_after_signals()
        now = time.time()
        for sig in signals:
            sym = sig["symbol"]
            last = self._last_signal_at.get(sym, 0)
            if now - last < self.signal_cooldown_sec:
                continue
            self._last_signal_at[sym] = now
            self.board.record_signal_emit(1)
            if not self.push_enabled:
                continue
            result = push_signal_to_bot(sig)
            self.board.record_push(bool(result.get("ok")))
            if result.get("ok"):
                log(
                    f"gainer_signal pushed {sym} trigger={sig.get('trigger')} "
                    f"rank={sig.get('rank')}",
                    "INFO",
                )
            else:
                log(
                    f"gainer_signal push skip {sym}: {result.get('message')}",
                    "DEBUG",
                )

    def _maybe_emit_relvol(self, tickers: dict[str, Any]) -> None:
        """Sample ticker book; only push when mode=trade (real kill switch)."""
        if not self._relvol or not self.push_enabled:
            return
        # mode: env GAINER_RELVOL_MODE overrides; trade|shadow|off
        mode = self._relvol_mode
        if mode in ("off", "disabled"):
            return
        # Always sample so baselines warm up even in shadow
        try:
            n = self._relvol.sample_tickers(tickers)
            fires = self._relvol.evaluate()
            if mode == "shadow":
                if fires:
                    log(
                        f"gainer_signal relvol SHADOW fires={len(fires)} sampled={n} "
                        f"(no push; mode=shadow)",
                        "INFO",
                    )
                return
            if mode != "trade":
                return
            if fires:
                log(
                    f"gainer_signal relvol fires={len(fires)} sampled={n}",
                    "INFO",
                )
            # tenants: env CSV or default single bot (bot fans out via body tenant_id)
            tenants_env = (os.environ.get("GAINER_RELVOL_TENANTS") or "default,henry").strip()
            tenants = [t.strip() for t in tenants_env.split(",") if t.strip()] or [
                "default"
            ]
            for sig in fires:
                sym = sig.get("symbol") or ""
                with self._tick_lock:
                    t = self._tickers_live.get(sym) or tickers.get(sym) or {}
                try:
                    from services.gainer_signal.pure import parse_pct_24h

                    sig["pct_24h"] = parse_pct_24h(t) if isinstance(t, dict) else 0.0
                except Exception:
                    pass
                # Extension pre-filter on signal service (bot also enforces)
                max_ext = float(os.environ.get("GAINER_RELVOL_MAX_PCT_24H") or 40)
                try:
                    if float(sig.get("pct_24h") or 0) > max_ext:
                        log(
                            f"gainer_signal RELVOL skip {sym}: extension "
                            f"pct={sig.get('pct_24h')}>{max_ext}",
                            "INFO",
                        )
                        continue
                except (TypeError, ValueError):
                    pass
                for tid in tenants:
                    payload = dict(sig)
                    payload["tenant_id"] = tid
                    result = push_signal_to_bot(payload)
                    self.board.record_push(bool(result.get("ok")))
                    if result.get("ok") and result.get("executed"):
                        log(
                            f"gainer_signal RELVOL BUY tenant={tid} {sym} "
                            f"factor={sig.get('factor')} qvol_1h={sig.get('qvol_1h')}",
                            "INFO",
                        )
                    elif result.get("ok"):
                        log(
                            f"gainer_signal RELVOL push ok no-fill tenant={tid} {sym}: "
                            f"{result.get('message')}",
                            "INFO",
                        )
                    else:
                        log(
                            f"gainer_signal RELVOL push skip tenant={tid} {sym}: "
                            f"{result.get('message')}",
                            "INFO",
                        )
        except Exception as e:
            log(f"gainer_signal relvol emit: {e}", "WARNING")

    def _ws_loop(self) -> None:
        try:
            import websocket
        except ImportError:
            log(
                "gainer_signal: websocket-client missing — REST-only mode "
                "(pip install websocket-client)",
                "WARNING",
            )
            return

        backoff = 3.0
        while not self._stop.is_set():
            # subscribe top symbols from current board + some volume leaders
            leaders = self.board.leaders()
            pairs = [to_gate_pair(r["symbol"]) for r in leaders[: self.ws_max_subscriptions]]
            if not pairs:
                self._stop.wait(5)
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
                if not pair:
                    return
                sym = normalize_symbol(str(pair).replace("_", "/"))
                # build ticker-like dict
                t: dict[str, Any] = {"info": result}
                if result.get("last") is not None:
                    t["last"] = result.get("last")
                if result.get("change_percentage") is not None:
                    t["percentage"] = result.get("change_percentage")
                # quote volume fields vary
                for k in ("quote_volume", "base_volume"):
                    if result.get(k) is not None:
                        t[k if k != "base_volume" else "baseVolume"] = result.get(k)
                with self._tick_lock:
                    self._tickers_live[sym] = {**self._tickers_live.get(sym, {}), **t}
                    merged = dict(self._tickers_live)
                self.board.bump_tick()
                # light re-rank every N ticks would be expensive; rely on REST seed
                # but update board periodically from merged
                if self.board.stats().get("ticks", 0) % 50 == 0:
                    self.board.apply_tickers(
                        merged, top_n=self.top_n, min_vol=self.min_vol, from_rest=False
                    )

            def on_open(ws) -> None:
                self.board.set_connected(True)
                self.board.bump_reconnect()
                n = 0
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
                        n += 1
                        time.sleep(0.03)
                    except Exception as exc:
                        log(f"gainer_signal subscribe {gp}: {exc}", "DEBUG")
                self.board.set_subscribed(n)
                log(f"gainer_signal WS subscribed n={n}", "INFO")

            def on_error(_ws, err) -> None:
                log(f"gainer_signal ws error: {err}", "WARNING")

            def on_close(_ws, *_a) -> None:
                self.board.set_connected(False)
                self.board.set_subscribed(0)

            try:
                ws = websocket.WebSocketApp(
                    WS_URL,
                    on_open=on_open,
                    on_message=on_message,
                    on_error=on_error,
                    on_close=on_close,
                )
                ws.run_forever(sslopt={"context": _ssl_context()}, ping_interval=20)
            except Exception as exc:
                log(f"gainer_signal run_forever: {exc}", "WARNING")
            finally:
                self.board.set_connected(False)

            if self._stop.is_set():
                break
            time.sleep(backoff)
            backoff = min(60.0, backoff * 1.5)
