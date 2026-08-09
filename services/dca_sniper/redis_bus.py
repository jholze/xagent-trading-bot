"""Redis integration for standalone DCA sniper service.

Keys (prefix default ``aria:``):
  ``{prefix}dca_sniper:state``     — JSON focus/decisions/metrics (TTL refreshed)
  ``{prefix}health:dca_sniper``    — heartbeat ISO timestamp
  ``{prefix}dca_sniper:watch``     — JSON list of symbols to WS-watch

Channels (pub/sub):
  ``{prefix}dca_sniper:wake``      — wake loop (reason payload)
  ``{prefix}dca_sniper:events``    — decision/audit fan-out
  ``{prefix}price:tick``           — optional shared price ticks ``{symbol,price}``
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Callable

from bus.redis_client import get_redis, resolve_redis_url
from logger import log

DEFAULT_PREFIX = "aria:"
CH_WAKE = "dca_sniper:wake"
CH_EVENTS = "dca_sniper:events"
CH_PRICE = "price:tick"
KEY_STATE = "dca_sniper:state"
KEY_WATCH = "dca_sniper:watch"
KEY_HEALTH = "health:dca_sniper"


def key_prefix() -> str:
    return (os.environ.get("DCA_SNIPER_REDIS_PREFIX") or os.environ.get("REDIS_KEY_PREFIX") or DEFAULT_PREFIX)


def _k(suffix: str) -> str:
    p = key_prefix()
    if not p.endswith(":"):
        p = p + ":"
    return f"{p}{suffix}"


def redis_available() -> bool:
    c = get_redis(resolve_redis_url())
    if not c:
        return False
    try:
        return bool(c.ping())
    except Exception:
        return False


def beat(*, ttl_sec: int = 120) -> None:
    from datetime import datetime, timezone

    c = get_redis()
    if not c:
        return
    try:
        now = datetime.now(timezone.utc).isoformat()
        c.setex(_k(KEY_HEALTH), int(ttl_sec), now)
    except Exception as e:
        log(f"dca_sniper redis beat fail: {e}", "DEBUG")


def save_state_redis(state: dict[str, Any], *, ttl_sec: int = 86400) -> bool:
    c = get_redis()
    if not c:
        return False
    try:
        c.setex(_k(KEY_STATE), int(ttl_sec), json.dumps(state, default=str))
        return True
    except Exception as e:
        log(f"dca_sniper redis save_state fail: {e}", "DEBUG")
        return False


def load_state_redis() -> dict[str, Any] | None:
    c = get_redis()
    if not c:
        return None
    try:
        raw = c.get(_k(KEY_STATE))
        if not raw:
            return None
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def set_watch_symbols(symbols: list[str], *, ttl_sec: int = 3600) -> None:
    c = get_redis()
    if not c:
        return
    try:
        uniq = sorted({str(s).upper().replace("_", "/") for s in symbols if s})
        c.setex(_k(KEY_WATCH), int(ttl_sec), json.dumps(uniq))
    except Exception as e:
        log(f"dca_sniper redis watch fail: {e}", "DEBUG")


def get_watch_symbols() -> list[str]:
    c = get_redis()
    if not c:
        return []
    try:
        raw = c.get(_k(KEY_WATCH))
        if not raw:
            return []
        data = json.loads(raw)
        return list(data) if isinstance(data, list) else []
    except Exception:
        return []


def publish_wake(reason: str = "manual", *, extra: dict | None = None) -> None:
    c = get_redis()
    if not c:
        return
    payload = {"reason": reason, "ts": time.time(), **(extra or {})}
    try:
        c.publish(_k(CH_WAKE), json.dumps(payload))
    except Exception as e:
        log(f"dca_sniper publish_wake fail: {e}", "DEBUG")


def publish_event(event: dict[str, Any]) -> None:
    c = get_redis()
    if not c:
        return
    try:
        body = dict(event)
        body.setdefault("ts", time.time())
        body.setdefault("service", "dca_sniper")
        c.publish(_k(CH_EVENTS), json.dumps(body, default=str))
    except Exception as e:
        log(f"dca_sniper publish_event fail: {e}", "DEBUG")


def publish_price(symbol: str, price: float, *, source: str = "sniper_ws") -> None:
    """Write price cache + optional tick channel for other services."""
    c = get_redis()
    if not c or price <= 0:
        return
    try:
        from bus.price_cache import RedisPriceCache

        cache = RedisPriceCache(key_prefix=key_prefix())
        cache.set_many({symbol: float(price)}, sources={symbol: source})
    except Exception:
        pass
    try:
        c.publish(
            _k(CH_PRICE),
            json.dumps({"symbol": symbol, "price": float(price), "source": source, "ts": time.time()}),
        )
    except Exception:
        pass


class WakeSubscriber:
    """Background Redis pub/sub → callback on wake / price ticks for watched symbols."""

    def __init__(
        self,
        on_wake: Callable[[str, dict], None],
        *,
        watch_provider: Callable[[], list[str]] | None = None,
        price_move_pct: float = 1.5,
    ):
        self._on_wake = on_wake
        self._watch_provider = watch_provider or get_watch_symbols
        self._price_move_pct = float(price_move_pct)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_px: dict[str, float] = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="dca-sniper-redis-sub", daemon=True)
        self._thread.start()
        log("dca_sniper redis wake subscriber started", "INFO")

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            c = get_redis()
            if not c:
                time.sleep(5)
                continue
            pubsub = None
            try:
                pubsub = c.pubsub(ignore_subscribe_messages=True)
                pubsub.subscribe(_k(CH_WAKE), _k(CH_PRICE))
                while not self._stop.is_set():
                    msg = pubsub.get_message(timeout=1.0)
                    if not msg or msg.get("type") != "message":
                        continue
                    ch = str(msg.get("channel") or "")
                    try:
                        data = json.loads(msg.get("data") or "{}")
                    except Exception:
                        data = {}
                    if not isinstance(data, dict):
                        data = {}
                    if ch.endswith(CH_WAKE):
                        self._on_wake(str(data.get("reason") or "redis_wake"), data)
                    elif ch.endswith(CH_PRICE):
                        self._handle_price(data)
            except Exception as e:
                log(f"dca_sniper redis sub error: {e}", "WARNING")
                time.sleep(3)
            finally:
                try:
                    if pubsub:
                        pubsub.close()
                except Exception:
                    pass

    def _handle_price(self, data: dict) -> None:
        sym = str(data.get("symbol") or "").upper()
        try:
            px = float(data.get("price") or 0)
        except (TypeError, ValueError):
            return
        if not sym or px <= 0:
            return
        watch = {s.upper() for s in (self._watch_provider() or [])}
        if watch and sym not in watch and sym.replace("/", "_") not in {
            w.replace("/", "_") for w in watch
        }:
            return
        prev = self._last_px.get(sym)
        self._last_px[sym] = px
        if prev and prev > 0:
            move = abs(px / prev - 1.0) * 100.0
            if move >= self._price_move_pct:
                self._on_wake(
                    "price_move",
                    {"symbol": sym, "price": px, "move_pct": round(move, 3)},
                )
