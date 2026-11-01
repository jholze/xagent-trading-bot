"""Observe-only shadow evaluation of three candidate loser-coin rules.

Nothing in this module places, sizes, skips, delays or cancels an order. It
only records what a rule WOULD have done, in Mongo collection
``obs_shadow_rules`` (same mirror as the PR #743 ``obs_*`` streams: async
queue, 120-day TTL on ``timestamp``, index on ``tenant_id``).

Rules (thresholds are fixed code constants, not config keys):

``g_no_chase``           entry candidate whose price is more than the limit
                         above the lowest closed 1h low of the 48 h before the
                         entry. Limit by Market Oracle state: 10 % in
                         RISK_OFF/CRASH, 20 % in RISK_ON, 15 % otherwise
                         (NEUTRAL or unknown). -> ``would_skip_entry``.
``h_oracle_early_exit``  open long lot at <= -10 % vs average entry within the
                         first 72 h after entry AND oracle RISK_OFF/CRASH.
                         -> ``would_full_exit``.
``e_age_limit``          open long lot older than 14 days and <= -15 % vs
                         average entry. -> ``would_close_aged``.

The pure helpers (``pump_pct_48h``, ``no_chase_limit_pct``) are shared with the
backtest harness (``backtest/harness/scenarios.py`` scenarios g/h and the
regime variants) so backtest and live shadow use one definition.

Failure isolation: the two hooks called from the trading path
(``observe_entry_candidate``, ``observe_lot``) only take a snapshot and put it
on a bounded queue. They never raise and never wait. Candle fetches, the
oracle read, rule evaluation and the Mongo insert run on a daemon worker
thread. Any exception there is logged and dropped.

Kill switch: env ``SHADOW_RULES=0`` (also ``false``/``off``). Under pytest the
hooks are off unless ``SHADOW_RULES_UNDER_TEST=1``.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

# ---------------------------------------------------------------------------
# Fixed rule parameters (2026-11-01, /workspace/umbau/loser_coins_analysis.md).

COLLECTION = "obs_shadow_rules"

RULE_NO_CHASE = "g_no_chase"
RULE_ORACLE_EARLY_EXIT = "h_oracle_early_exit"
RULE_AGE_LIMIT = "e_age_limit"

ACTION_SKIP_ENTRY = "would_skip_entry"
ACTION_ALLOW_ENTRY = "would_allow_entry"
ACTION_NO_DATA = "no_data"
ACTION_FULL_EXIT = "would_full_exit"
ACTION_CLOSE_AGED = "would_close_aged"

NO_CHASE_LOOKBACK_HOURS = 48
# Fewer closed 1h bars than this in the 48 h window -> no verdict (harness: keep).
NO_CHASE_MIN_BARS = 36
NO_CHASE_LIMIT_RISK_OFF_PCT = 10.0
NO_CHASE_LIMIT_NEUTRAL_PCT = 15.0
NO_CHASE_LIMIT_RISK_ON_PCT = 20.0

EARLY_EXIT_LOSS_PCT = 10.0
EARLY_EXIT_WINDOW_HOURS = 72.0

AGE_LIMIT_DAYS = 14.0
AGE_LIMIT_LOSS_PCT = 15.0

RISK_OFF_STATES = frozenset({"RISK_OFF", "CRASH"})
RISK_ON_STATES = frozenset({"RISK_ON"})

HOUR = 3600

_QUEUE_MAX = 2048
_SEEN_MAX = 50_000

# ---------------------------------------------------------------------------
# Pure helpers (no I/O). Shared with backtest/harness.


def _norm_state(state: Any) -> str | None:
    if state is None:
        return None
    text = str(state).strip().upper()
    return text or None


def is_risk_off(state: Any) -> bool:
    """RISK_OFF / CRASH (and the harness ``OFF`` label of sma/ret series)."""
    s = _norm_state(state)
    return s in RISK_OFF_STATES or s == "OFF"


def no_chase_limit_pct(state: Any) -> float:
    """48 h pump limit for ``g_no_chase`` by regime label.

    RISK_OFF/CRASH (or ``OFF``) -> 10 %, RISK_ON (or ``ON``) -> 20 %,
    anything else including NEUTRAL and unknown -> 15 %.
    """
    s = _norm_state(state)
    if s is None:
        return NO_CHASE_LIMIT_NEUTRAL_PCT
    if is_risk_off(s):
        return NO_CHASE_LIMIT_RISK_OFF_PCT
    if s in RISK_ON_STATES or s == "ON":
        return NO_CHASE_LIMIT_RISK_ON_PCT
    return NO_CHASE_LIMIT_NEUTRAL_PCT


def _bar_open_unix(bar: Any) -> int:
    ts = int(bar[0])
    return ts // 1000 if ts > 10_000_000_000 else ts


def window_lows_48h(bars_1h: Iterable[Any], signal_unix: int) -> list[float]:
    """Lows of 1h bars that opened in the 48 h before the signal and closed by it."""
    lo = signal_unix - NO_CHASE_LOOKBACK_HOURS * HOUR
    out: list[float] = []
    for b in bars_1h or ():
        t = _bar_open_unix(b)
        if lo <= t and t + HOUR <= signal_unix:
            out.append(float(b[3]))
    return out


def pump_pct_from_lows(lows: list[float], price: float) -> float | None:
    """(price / min(low) - 1) * 100, or None with too little history."""
    if len(lows) < NO_CHASE_MIN_BARS or min(lows) <= 0 or not price or price <= 0:
        return None
    return (float(price) / min(lows) - 1.0) * 100.0


def pump_pct_48h(bars_1h: Iterable[Any], price: float, signal_unix: int) -> float | None:
    return pump_pct_from_lows(window_lows_48h(bars_1h, signal_unix), price)


def evaluate_no_chase(pump_pct: float | None, oracle_state: Any) -> tuple[str, float]:
    """(action, limit_pct). Skip when the pump is strictly above the limit."""
    limit = no_chase_limit_pct(oracle_state)
    if pump_pct is None:
        return ACTION_NO_DATA, limit
    if pump_pct > limit:
        return ACTION_SKIP_ENTRY, limit
    return ACTION_ALLOW_ENTRY, limit


def lot_pnl_pct(average_entry: float, price: float) -> float | None:
    try:
        avg = float(average_entry or 0)
        px = float(price or 0)
    except (TypeError, ValueError):
        return None
    if avg <= 0 or px <= 0:
        return None
    return (px / avg - 1.0) * 100.0


def early_exit_triggered(pnl_pct: float | None, age_hours: float | None, oracle_state: Any) -> bool:
    """h_oracle: <= -10 % within the first 72 h and oracle RISK_OFF/CRASH."""
    if pnl_pct is None or age_hours is None or age_hours < 0:
        return False
    return (
        pnl_pct <= -EARLY_EXIT_LOSS_PCT
        and age_hours <= EARLY_EXIT_WINDOW_HOURS
        and _norm_state(oracle_state) in RISK_OFF_STATES
    )


def age_limit_triggered(pnl_pct: float | None, age_hours: float | None) -> bool:
    """e: lot older than 14 days and <= -15 %."""
    if pnl_pct is None or age_hours is None:
        return False
    return age_hours > AGE_LIMIT_DAYS * 24.0 and pnl_pct <= -AGE_LIMIT_LOSS_PCT


def rule_params() -> dict[str, dict]:
    return {
        RULE_NO_CHASE: {
            "lookback_hours": NO_CHASE_LOOKBACK_HOURS,
            "min_bars": NO_CHASE_MIN_BARS,
            "limit_risk_off_pct": NO_CHASE_LIMIT_RISK_OFF_PCT,
            "limit_neutral_pct": NO_CHASE_LIMIT_NEUTRAL_PCT,
            "limit_risk_on_pct": NO_CHASE_LIMIT_RISK_ON_PCT,
            "reference": "min 1h low of closed bars in window",
        },
        RULE_ORACLE_EARLY_EXIT: {
            "loss_pct": EARLY_EXIT_LOSS_PCT,
            "window_hours": EARLY_EXIT_WINDOW_HOURS,
            "oracle_states": sorted(RISK_OFF_STATES),
        },
        RULE_AGE_LIMIT: {
            "min_age_days": AGE_LIMIT_DAYS,
            "loss_pct": AGE_LIMIT_LOSS_PCT,
        },
    }


def _parse_ts(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 10_000_000_000:
            v /= 1000.0
        return datetime.fromtimestamp(v, tz=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def lot_id(symbol: str, timeframe: str, entry_at: Any) -> str:
    return f"{symbol}|{timeframe}|{entry_at or ''}"


# ---------------------------------------------------------------------------
# Runtime (worker thread). Injectable for tests.

_queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX)
_worker_lock = threading.Lock()
_worker_started = False
_seen_lock = threading.Lock()
_seen: set[tuple] = set()

_sink: Callable[[dict], None] | None = None
_candle_fetcher: Callable[[str, int], list] | None = None
_oracle_reader: Callable[[], str | None] | None = None
_inline = False  # tests: process on the caller thread instead of the worker


def _log(msg: str, level: str = "INFO") -> None:
    try:
        from logger import log

        log(msg, level)
    except Exception:
        pass


def enabled() -> bool:
    flag = (os.environ.get("SHADOW_RULES") or "").strip().lower()
    if flag in ("0", "false", "off", "no"):
        return False
    if os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get("SHADOW_RULES_UNDER_TEST") != "1":
        return False
    return True


def _tenant() -> str:
    try:
        from core.tenant_context import resolve_tenant_id

        return str(resolve_tenant_id() or "default")
    except Exception:
        return "default"


def _read_oracle() -> str | None:
    reader = _oracle_reader
    if reader is not None:
        return _norm_state(reader())
    from strategies.ctx_axes import read_oracle_state

    return _norm_state(read_oracle_state())


def _fetch_1h(symbol: str, limit: int) -> list:
    fetcher = _candle_fetcher
    if fetcher is not None:
        return list(fetcher(symbol, limit) or [])
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return []  # unit tests never reach the network
    import ccxt

    ex = ccxt.gate({"enableRateLimit": True, "timeout": 10_000, "options": {"defaultType": "spot"}})
    bars = ex.fetch_ohlcv(symbol, timeframe="1h", limit=limit) or []
    return [list(b) for b in bars if len(b) >= 6]


def _first_time(key: tuple) -> bool:
    with _seen_lock:
        if key in _seen:
            return False
        if len(_seen) >= _SEEN_MAX:
            _seen.clear()
        _seen.add(key)
        return True


def _emit(doc: dict) -> None:
    sink = _sink
    if sink is not None:
        sink(doc)
        return
    from services.observe_streams import mirror_record

    mirror_record(COLLECTION, doc)


def _base_doc(job: dict, *, rule: str, action: str, oracle_state: str | None) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "type": "shadow_rule",
        "observe_only": True,
        "timestamp": now,
        "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tenant_id": job.get("tenant_id") or "default",
        "symbol": job.get("symbol"),
        "timeframe": job.get("timeframe"),
        "lot_id": job.get("lot_id"),
        "rule": rule,
        "action": action,
        "rule_params": rule_params()[rule],
        "oracle_state": oracle_state,
        "price": job.get("price"),
    }


def process_entry_job(job: dict) -> dict | None:
    """Evaluate ``g_no_chase`` for one entry candidate and emit one row."""
    oracle = _read_oracle()
    sig = int(job.get("signal_unix") or time.time())
    price = float(job.get("price") or 0)
    bars = _fetch_1h(str(job.get("symbol")), NO_CHASE_LOOKBACK_HOURS + 12)
    pump = pump_pct_48h(bars, price, sig)
    action, limit = evaluate_no_chase(pump, oracle)
    doc = _base_doc(job, rule=RULE_NO_CHASE, action=action, oracle_state=oracle)
    doc.update(
        {
            "pnl_pct": None,
            "pump_48h_pct": None if pump is None else round(pump, 4),
            "limit_pct": limit,
            "bars_in_window": len(window_lows_48h(bars, sig)),
            "order_source": job.get("source"),
            "idempotency_key": job.get("idempotency_key"),
        }
    )
    _emit(doc)
    if action == ACTION_SKIP_ENTRY:
        _log(
            f"shadow_rule {RULE_NO_CHASE} {action} {doc['symbol']} pump48h={pump:.1f}% "
            f"limit={limit:.0f}% oracle={oracle} (observe only)",
            "INFO",
        )
    return doc


def process_lot_job(job: dict) -> list[dict]:
    """Evaluate ``h_oracle_early_exit`` and ``e_age_limit`` for one lot.

    Emits one row per rule the first time it would fire for that lot.
    """
    price = float(job.get("price") or 0)
    pnl = lot_pnl_pct(job.get("average_entry") or 0, price)
    entered = _parse_ts(job.get("entry_at"))
    now = _parse_ts(job.get("observed_at")) or datetime.now(timezone.utc)
    age_h = None if entered is None else (now - entered).total_seconds() / 3600.0
    if pnl is None or age_h is None:
        return []
    want_age = age_limit_triggered(pnl, age_h)
    # The oracle is only read when the early-exit price/age part holds.
    maybe_early = pnl <= -EARLY_EXIT_LOSS_PCT and 0 <= age_h <= EARLY_EXIT_WINDOW_HOURS
    if not want_age and not maybe_early:
        return []
    oracle = _read_oracle()
    out: list[dict] = []
    hits = []
    if maybe_early and early_exit_triggered(pnl, age_h, oracle):
        hits.append((RULE_ORACLE_EARLY_EXIT, ACTION_FULL_EXIT))
    if want_age:
        hits.append((RULE_AGE_LIMIT, ACTION_CLOSE_AGED))
    for rule, action in hits:
        if not _first_time((job.get("tenant_id"), job.get("lot_id"), rule)):
            continue
        doc = _base_doc(job, rule=rule, action=action, oracle_state=oracle)
        doc.update(
            {
                "pnl_pct": round(pnl, 4),
                "average_entry": job.get("average_entry"),
                "entry_at": job.get("entry_at"),
                "lot_age_hours": round(age_h, 2),
                "amount": job.get("amount"),
            }
        )
        _emit(doc)
        _log(
            f"shadow_rule {rule} {action} {doc['symbol']} pnl={pnl:.1f}% age={age_h:.0f}h "
            f"oracle={oracle} (observe only)",
            "INFO",
        )
        out.append(doc)
    return out


def process_job(job: dict) -> Any:
    kind = job.get("kind")
    if kind == "entry":
        return process_entry_job(job)
    if kind == "lot":
        return process_lot_job(job)
    return None


_STOP_KIND = "__stop__"
_worker_thread: threading.Thread | None = None


def _worker() -> None:
    global _worker_started
    while True:
        try:
            job = _queue.get(timeout=0.5)
        except queue.Empty:
            continue
        if job.get("kind") == _STOP_KIND:
            with _worker_lock:
                _worker_started = False
            return
        try:
            process_job(job)
        except Exception as exc:
            _log(f"shadow_rules worker error ({job.get('kind')} {job.get('symbol')}): {exc}", "WARNING")


def _ensure_worker() -> None:
    global _worker_started, _worker_thread
    with _worker_lock:
        if _worker_started:
            return
        _worker_thread = threading.Thread(target=_worker, name="shadow-rules", daemon=True)
        _worker_thread.start()
        _worker_started = True


def _stop_worker(timeout: float = 2.0) -> None:
    """Tests only: end the worker thread so no thread outlives the test."""
    thread = _worker_thread
    if thread is None or not thread.is_alive():
        return
    drain_for_tests()
    try:
        _queue.put({"kind": _STOP_KIND}, timeout=timeout)
    except queue.Full:
        return
    thread.join(timeout)


def _enqueue(job: dict) -> None:
    if _inline:
        try:
            process_job(job)
        except Exception as exc:
            _log(f"shadow_rules inline error ({job.get('kind')} {job.get('symbol')}): {exc}", "WARNING")
        return
    _ensure_worker()
    try:
        _queue.put_nowait(job)
    except queue.Full:
        return


# ---------------------------------------------------------------------------
# Hooks called from the trading path. Never raise, never block.


def observe_entry_candidate(order: Any, *, timeframe: str = "", source: str = "") -> None:
    """Snapshot a BUY that would open a new long lot. Observe only."""
    try:
        if not enabled():
            return
        if str(getattr(order, "type", "") or "").upper() != "BUY":
            return
        symbol = str(getattr(order, "symbol", "") or "")
        price = float(getattr(order, "price", 0) or 0)
        if not symbol or price <= 0:
            return
        from strategies.positions import find_open_position_for_symbol

        if find_open_position_for_symbol(symbol) is not None:
            return  # add to an existing lot, not an entry
        tenant = _tenant()
        idem = str(getattr(order, "idempotency_key", "") or "")
        dedupe = (tenant, "entry", symbol, idem or int(time.time()) // 60)
        if not _first_time(dedupe):
            return
        _enqueue(
            {
                "kind": "entry",
                "tenant_id": tenant,
                "symbol": symbol,
                "timeframe": timeframe,
                "lot_id": None,
                "price": price,
                "signal_unix": int(time.time()),
                "source": source,
                "idempotency_key": idem or None,
            }
        )
    except Exception as exc:
        _log(f"shadow_rules entry hook error: {exc}", "WARNING")


def observe_lot(symbol: str, timeframe: str, pos: Any, price: float) -> None:
    """Snapshot one open long lot at the cycle price. Observe only."""
    try:
        if not enabled():
            return
        if not isinstance(pos, dict) or not symbol or not price or float(price) <= 0:
            return
        if str(pos.get("side") or "long").strip().lower() == "short":
            return
        if float(pos.get("amount") or 0) <= 0 or float(pos.get("average_entry") or 0) <= 0:
            return
        entry_at = pos.get("entry_at") or pos.get("first_buy_at")
        if not entry_at:
            return
        tf = str(pos.get("timeframe") or timeframe or "")
        _enqueue(
            {
                "kind": "lot",
                "tenant_id": _tenant(),
                "symbol": symbol,
                "timeframe": tf,
                "lot_id": lot_id(symbol, tf, entry_at),
                "price": float(price),
                "average_entry": float(pos.get("average_entry") or 0),
                "amount": float(pos.get("amount") or 0),
                "entry_at": str(entry_at),
                "observed_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    except Exception as exc:
        _log(f"shadow_rules lot hook error: {exc}", "WARNING")


def observe_symbol_lot(symbol: str, price: float, preferred_timeframe: str = "") -> None:
    """Look up the open long lot for ``symbol`` (any timeframe) and observe it."""
    try:
        if not enabled():
            return
        from strategies.positions import find_open_position_for_symbol

        found = find_open_position_for_symbol(symbol, preferred_timeframe=preferred_timeframe or None)
        if not found:
            return
        lot_tf, pos = found
        observe_lot(symbol, lot_tf, dict(pos, timeframe=lot_tf), price)
    except Exception as exc:
        _log(f"shadow_rules lot lookup error: {exc}", "WARNING")


# ---------------------------------------------------------------------------
# Test helpers.


def set_hooks_for_tests(
    *,
    sink: Callable[[dict], None] | None = None,
    candle_fetcher: Callable[[str, int], list] | None = None,
    oracle_reader: Callable[[], str | None] | None = None,
    inline: bool = False,
) -> None:
    global _sink, _candle_fetcher, _oracle_reader, _inline
    _sink = sink
    _candle_fetcher = candle_fetcher
    _oracle_reader = oracle_reader
    _inline = inline


def drain_for_tests() -> list[dict]:
    """Pop queued jobs without processing them (worker may already hold one)."""
    jobs = []
    while True:
        try:
            jobs.append(_queue.get_nowait())
        except queue.Empty:
            return jobs


def reset_for_tests() -> None:
    set_hooks_for_tests()
    with _seen_lock:
        _seen.clear()
    drain_for_tests()
    _stop_worker()
