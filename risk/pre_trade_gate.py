"""Pre-buy memory gate: one check before every bot BUY (plan ``memory_gate`` §2).

Called once from ``RiskManager._evaluate_impl`` for every non-manual BUY: new
entries from every source (main/lc/cmc/auto/oracle, gainer_relvol,
entry_sensor_15m, sniper) and DCA adds. Sells are never evaluated.

Inputs (all known before the buy):

1. Per-tenant coin history from ``orders_v2`` (one document per order, never
   touched by a ledger reset), so archived / pre-epoch fills still count.
   Optional pooling across tenants via ``pool_tenants``.
2. Coin-fact flags (``intelligence.memory.coin_facts.flags_from_events``) from
   market events of this symbol only. Facts are market-wide; tenant-specific
   event types (``dca_decision``, ``soft_block``) are excluded by the type
   filter (#708).
3. Optional DCA trend rule (price vs. daily SMA), off unless a threshold is set.

Verdict: ``allow`` | ``reduce`` (size multiplier) | ``block``. A coin without
history and without flags is ``allow``. A read error is ``block`` with code
``db_error`` (fail-closed); it is only enforced in ``enforce`` mode.

Mode switch ``rule_switches.pre_trade_gate.mode``: ``off`` | ``shadow`` |
``enforce``. Missing block = ``off``. A malformed value maps to ``enforce``
(same convention as ``elig_*``, #643 R4). Shadow decides and logs, never
changes the order.

No coin names and no thresholds live in this module: every numeric rule is
inactive while its threshold is ``None``; values come from ``config.json`` or
tenant overrides.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ENFORCE = "enforce"
MODES = (MODE_OFF, MODE_SHADOW, MODE_ENFORCE)

VERDICT_ALLOW = "allow"
VERDICT_REDUCE = "reduce"
VERDICT_BLOCK = "block"

CODE_DB_ERROR = "db_error"
CODE_NO_HISTORY = "no_history"
DECISION_CODE_BLOCK = "pre_trade_gate_block"
OBSERVE_COLLECTION = "obs_pre_trade_gate"

DCA_SOURCES = frozenset({"dca", "dca_recovery", "dca_sniper", "dca_scheduled"})

# Structural defaults only. Every threshold is None (rule inactive) until the
# config sets it. Shipped values live in config.json rule_switches.pre_trade_gate.
_DEFAULTS: dict[str, Any] = {
    "mode": MODE_OFF,
    "lookback_days": None,
    "pool_tenants": [],
    "loss_min_usdt": None,
    "half_life_days": None,
    "block_min_losses": None,
    "block_net_loss_usdt": None,
    "recent_days": None,
    "recent_loss_pct": None,
    "recent_loss_usdt": None,
    "reduce_min_losses": None,
    "reduce_net_loss_usdt": None,
    "reduce_mult": None,
    "min_mult": None,
    "max_rows": 2000,
    "cache_ttl_sec": 0,
    "facts": {
        "enabled": False,
        "lookback_hours": None,
        "max_events": 40,
        "block_flags": [],
        "reduce_mults": {},
    },
    "dca": {
        "sma_days": None,
        "max_below_sma_pct": None,
    },
}

_FACT_FLAG_NAMES = (
    "hard_negative",
    "unlock",
    "profit_taking",
    "flow_only",
    "structure_risk",
    "volume_breakout",
    "catalyst",
    "utility",
    "noise_only",
)


class GateDataError(RuntimeError):
    """History or facts could not be read. The gate turns this into block."""


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def _raw_of(config: Any) -> dict:
    if isinstance(config, dict):
        return config
    raw = getattr(config, "raw", None)
    return raw if isinstance(raw, dict) else {}


def _block(config: Any) -> dict | None:
    rs = _raw_of(config).get("rule_switches")
    sub = rs.get("pre_trade_gate") if isinstance(rs, dict) else None
    return sub if isinstance(sub, dict) else None


def gate_mode(config: Any) -> str:
    """``off`` | ``shadow`` | ``enforce``. Missing = off; malformed = enforce."""
    sub = _block(config)
    if sub is None or "mode" not in sub or sub.get("mode") is None:
        return MODE_OFF
    value = sub.get("mode")
    if isinstance(value, bool):
        return MODE_ENFORCE if value else MODE_OFF
    text = str(value).strip().lower()
    return text if text in MODES else MODE_ENFORCE


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def gate_settings(config: Any) -> dict[str, Any]:
    """Defaults <- ``rule_switches.pre_trade_gate`` (one level deep for sub-blocks)."""
    out: dict[str, Any] = {
        k: (dict(v) if isinstance(v, dict) else (list(v) if isinstance(v, list) else v))
        for k, v in _DEFAULTS.items()
    }
    sub = _block(config) or {}
    for k, v in sub.items():
        if str(k).startswith("_"):
            continue
        if isinstance(out.get(k), dict) and isinstance(v, dict):
            merged = dict(out[k])
            merged.update({kk: vv for kk, vv in v.items() if not str(kk).startswith("_")})
            out[k] = merged
        else:
            out[k] = v
    out["mode"] = gate_mode(config)
    return out


# ---------------------------------------------------------------------------
# result
# ---------------------------------------------------------------------------


@dataclass
class HistoryStats:
    n_fills: int = 0
    n_closed: int = 0
    wins: int = 0
    losses: int = 0
    net_pnl_usdt: float = 0.0
    weighted_pnl_usdt: float = 0.0
    win_rate: float | None = None
    worst_loss_usdt: float = 0.0
    worst_loss_pct: float = 0.0
    last_loss_ts: str | None = None
    recent_big_loss: bool = False
    dca_buys: int = 0
    tenants: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_fills": self.n_fills,
            "n_closed": self.n_closed,
            "wins": self.wins,
            "losses": self.losses,
            "net_pnl_usdt": round(self.net_pnl_usdt, 2),
            "weighted_pnl_usdt": round(self.weighted_pnl_usdt, 2),
            "win_rate": None if self.win_rate is None else round(self.win_rate, 3),
            "worst_loss_usdt": round(self.worst_loss_usdt, 2),
            "worst_loss_pct": round(self.worst_loss_pct, 2),
            "last_loss_ts": self.last_loss_ts,
            "recent_big_loss": self.recent_big_loss,
            "dca_buys": self.dca_buys,
            "tenants": list(self.tenants),
        }


@dataclass
class GateResult:
    verdict: str = VERDICT_ALLOW
    codes: list[str] = field(default_factory=list)
    size_mult: float = 1.0
    mode: str = MODE_OFF
    history: dict[str, Any] = field(default_factory=dict)
    flags: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def blocks(self) -> bool:
        return self.verdict == VERDICT_BLOCK

    @property
    def enforced(self) -> bool:
        return self.mode == MODE_ENFORCE

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "codes": list(self.codes),
            "size_mult": round(float(self.size_mult), 4),
            "mode": self.mode,
            "history": dict(self.history),
            "flags": dict(self.flags),
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# pure history math
# ---------------------------------------------------------------------------


def _parse_ts(row: dict) -> datetime | None:
    cand = row.get("ts_event")
    if not cand:
        ts = row.get("timestamps") or {}
        cand = ts.get("filled") or ts.get("updated") or ts.get("created")
    if isinstance(cand, datetime):
        dt = cand
    elif cand:
        text = str(cand).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            dt = None
    else:
        dt = None
    if dt is None:
        day = str(row.get("day_key") or "")
        try:
            dt = datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _usdt(row: dict) -> float | None:
    ex = row.get("execution") or {}
    return _num(ex.get("usdt") if isinstance(ex, dict) else None)


def summarize_history(
    rows: list[dict], *, now: datetime, settings: dict[str, Any]
) -> HistoryStats:
    """Per-coin metrics from filled orders. *now* is naive UTC."""
    st = HistoryStats()
    loss_min = _num(settings.get("loss_min_usdt")) or 0.0
    half_life = _num(settings.get("half_life_days"))
    recent_days = _num(settings.get("recent_days"))
    recent_pct = _num(settings.get("recent_loss_pct"))
    recent_usdt = _num(settings.get("recent_loss_usdt"))
    tenants: set[str] = set()
    last_loss: datetime | None = None
    for row in rows or []:
        st.n_fills += 1
        tenants.add(str(row.get("tenant_id") or ""))
        side = str(row.get("side") or "").lower()
        if side == "buy":
            src = str(row.get("source") or "").lower()
            if src in DCA_SOURCES or str(row.get("signal") or "").upper() == "BUY_DCA":
                st.dca_buys += 1
            continue
        if side != "sell":
            continue
        pnl = _num(row.get("pnl"))
        if pnl is None:
            continue
        st.n_closed += 1
        st.net_pnl_usdt += pnl
        ts = _parse_ts(row)
        age_days = max(0.0, (now - ts).total_seconds() / 86400.0) if ts else None
        weight = 1.0
        if half_life and half_life > 0 and age_days is not None:
            weight = 0.5 ** (age_days / half_life)
        st.weighted_pnl_usdt += pnl * weight
        proceeds = _usdt(row)
        pct = None
        if proceeds is not None and proceeds - pnl > 0:
            pct = pnl / (proceeds - pnl) * 100.0
        if pnl > loss_min:
            st.wins += 1
        elif pnl < -loss_min:
            st.losses += 1
            if ts and (last_loss is None or ts > last_loss):
                last_loss = ts
        st.worst_loss_usdt = min(st.worst_loss_usdt, pnl)
        if pct is not None:
            st.worst_loss_pct = min(st.worst_loss_pct, pct)
        if recent_days is not None and age_days is not None and age_days <= recent_days:
            big_usdt = recent_usdt is not None and pnl <= -abs(recent_usdt)
            big_pct = recent_pct is not None and pct is not None and pct <= -abs(recent_pct)
            if big_usdt or big_pct:
                st.recent_big_loss = True
    if st.n_closed:
        st.win_rate = st.wins / st.n_closed
    st.last_loss_ts = last_loss.isoformat() if last_loss else None
    st.tenants = sorted(t for t in tenants if t)
    return st


def flags_to_dict(flags: Any) -> dict[str, Any]:
    if flags is None:
        return {}
    out = {name: bool(getattr(flags, name, False)) for name in _FACT_FLAG_NAMES}
    out["event_count"] = int(getattr(flags, "event_count", 0) or 0)
    return out


def decide(
    history: HistoryStats | None,
    flags: dict[str, Any] | None,
    settings: dict[str, Any],
    *,
    is_dca: bool = False,
    dca_sma_ext_pct: float | None = None,
) -> GateResult:
    """Pure verdict from metrics, flags and settings. No I/O."""
    res = GateResult(mode=str(settings.get("mode") or MODE_OFF))
    block_codes: list[str] = []
    reduce_codes: list[str] = []
    mult = 1.0
    h = history or HistoryStats()
    res.history = h.to_dict()
    res.flags = dict(flags or {})

    basis = h.weighted_pnl_usdt if _num(settings.get("half_life_days")) else h.net_pnl_usdt
    b_losses = _num(settings.get("block_min_losses"))
    b_net = _num(settings.get("block_net_loss_usdt"))
    if b_losses is not None and b_net is not None:
        if h.losses >= b_losses and basis <= -abs(b_net):
            block_codes.append("hist_losses")
    if h.recent_big_loss:
        block_codes.append("hist_recent_big_loss")

    r_losses = _num(settings.get("reduce_min_losses"))
    r_net = _num(settings.get("reduce_net_loss_usdt"))
    r_mult = _num(settings.get("reduce_mult"))
    if r_losses is not None and r_net is not None and r_mult is not None:
        if h.losses >= r_losses and basis < -abs(r_net):
            reduce_codes.append("hist_mixed")
            mult *= max(0.0, min(1.0, r_mult))

    facts_cfg = settings.get("facts") or {}
    if facts_cfg.get("enabled") and flags:
        for name in facts_cfg.get("block_flags") or []:
            if flags.get(str(name)):
                block_codes.append(f"facts_{name}")
        for name, fm in (facts_cfg.get("reduce_mults") or {}).items():
            fmv = _num(fm)
            if fmv is not None and flags.get(str(name)):
                reduce_codes.append(f"facts_{name}")
                mult *= max(0.0, min(1.0, fmv))

    dca_cfg = settings.get("dca") or {}
    max_below = _num(dca_cfg.get("max_below_sma_pct"))
    if is_dca and max_below is not None and dca_sma_ext_pct is not None:
        if dca_sma_ext_pct <= -abs(max_below):
            block_codes.append("dca_below_trend")

    if block_codes:
        res.verdict = VERDICT_BLOCK
        res.codes = block_codes + reduce_codes
        res.size_mult = 0.0
        return res
    if reduce_codes and mult < 1.0:
        floor = _num(settings.get("min_mult"))
        if floor is not None:
            mult = max(mult, max(0.0, min(1.0, floor)))
        res.verdict = VERDICT_REDUCE
        res.codes = reduce_codes
        res.size_mult = mult
        return res
    res.verdict = VERDICT_ALLOW
    res.codes = [] if h.n_closed or (flags or {}).get("event_count") else [CODE_NO_HISTORY]
    res.size_mult = 1.0
    return res


# ---------------------------------------------------------------------------
# loaders (read-only; errors propagate as GateDataError)
# ---------------------------------------------------------------------------


def load_history_rows(
    *, tenant_ids: list[str], scope: str, symbol: str, since_day: str, limit: int
) -> list[dict]:
    from storage.order_ledger_v2 import get_order_ledger_v2

    store = get_order_ledger_v2()
    if store is None:
        raise GateDataError("orders_v2 disabled")
    fn = getattr(store, "query_symbol_fills", None)
    if fn is None:
        raise GateDataError("orders_v2 store has no query_symbol_fills")
    return list(fn(tenant_ids, scope, symbol, since_day, limit=limit))


def load_fact_events(*, symbol: str, since_iso: str, limit: int, db: Any = None) -> list[Any]:
    """Recent market events of *symbol* restricted to the coin-fact taxonomy."""
    from intelligence.memory.coin_facts import CMC_SOURCES, EVENT_TYPES
    from intelligence.memory.models import MarketEvent
    from intelligence.memory.store import COL_EVENTS, MemoryStore

    sym = str(symbol or "").upper()
    base = sym.split("/")[0]
    q = {
        "symbols": {"$in": sorted({sym, base, f"{base}/USDT"})},
        "timestamp": {"$gte": since_iso},
        "$or": [
            {"event_type": {"$in": sorted(EVENT_TYPES)}},
            {"source": {"$in": sorted(CMC_SOURCES)}},
        ],
    }
    col = MemoryStore(db=db)._col(COL_EVENTS)
    cur = col.find(q).sort("timestamp", -1).limit(max(1, int(limit)))
    out = []
    for doc in cur:
        ev = MarketEvent.from_doc(doc)
        if ev is not None:
            out.append(ev)
    return out


# ---------------------------------------------------------------------------
# gate
# ---------------------------------------------------------------------------

_CACHE_LOCK = threading.Lock()
_CACHE: dict[tuple, tuple[float, Any]] = {}


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def _cached(key: tuple, ttl: float, fn: Callable[[], Any]) -> Any:
    if ttl > 0:
        now = time.monotonic()
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
            if hit and now - hit[0] <= ttl:
                return hit[1]
    value = fn()
    if ttl > 0:
        with _CACHE_LOCK:
            _CACHE[key] = (time.monotonic(), value)
    return value


def evaluate_gate(
    *,
    symbol: str,
    source: str,
    tenant_id: str,
    scope: str,
    config: Any,
    is_dca: bool = False,
    dca_sma_ext_pct: float | None = None,
    now: datetime | None = None,
    history_loader: Callable[..., list[dict]] | None = None,
    facts_loader: Callable[..., list[Any]] | None = None,
) -> GateResult:
    """Full gate for one BUY. Never raises: read errors become ``block/db_error``."""
    settings = gate_settings(config)
    mode = settings["mode"]
    if mode == MODE_OFF:
        return GateResult(mode=MODE_OFF)
    now_utc = now or datetime.now(timezone.utc).replace(tzinfo=None)
    if now_utc.tzinfo is not None:
        now_utc = now_utc.astimezone(timezone.utc).replace(tzinfo=None)
    ttl = _num(settings.get("cache_ttl_sec")) or 0.0
    history_loader = history_loader or load_history_rows
    facts_loader = facts_loader or load_fact_events

    try:
        history: HistoryStats | None = None
        lookback = _num(settings.get("lookback_days"))
        if lookback is not None and lookback > 0:
            pool = settings.get("pool_tenants") or []
            tids = sorted({str(tenant_id)} | {str(t) for t in pool if t})
            since_day = (now_utc - timedelta(days=lookback)).strftime("%Y-%m-%d")
            max_rows = int(_num(settings.get("max_rows")) or 2000)
            key = ("h", tuple(tids), scope, symbol, since_day)
            rows = _cached(
                key,
                ttl,
                lambda: history_loader(
                    tenant_ids=tids, scope=scope, symbol=symbol, since_day=since_day, limit=max_rows
                ),
            )
            history = summarize_history(rows, now=now_utc, settings=settings)

        flags: dict[str, Any] = {}
        facts_cfg = settings.get("facts") or {}
        fl_hours = _num(facts_cfg.get("lookback_hours"))
        if facts_cfg.get("enabled") and fl_hours is not None and fl_hours > 0:
            from intelligence.memory.coin_facts import flags_from_events

            since_iso = (now_utc - timedelta(hours=fl_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
            limit = int(_num(facts_cfg.get("max_events")) or 40)
            key = ("f", symbol, since_iso[:13])
            events = _cached(
                key, ttl, lambda: facts_loader(symbol=symbol, since_iso=since_iso, limit=limit)
            )
            flags = flags_to_dict(flags_from_events(list(events or [])))
    except Exception as exc:  # fail-closed: unknown history must not look clean
        return GateResult(
            verdict=VERDICT_BLOCK,
            codes=[CODE_DB_ERROR],
            size_mult=0.0,
            mode=mode,
            error=f"{type(exc).__name__}: {str(exc)[:200]}",
        )

    return decide(
        history, flags, settings, is_dca=is_dca, dca_sma_ext_pct=dca_sma_ext_pct
    )


def log_line(result: GateResult, *, tenant_id: str, symbol: str, source: str) -> str:
    codes = ",".join(result.codes) or "-"
    return (
        f"pre_trade_gate tenant={tenant_id} symbol={symbol} source={source} "
        f"verdict={result.verdict} codes={codes} mult={result.size_mult:.2f} "
        f"mode={result.mode}"
    )


def record_decision(
    result: GateResult,
    *,
    tenant_id: str,
    symbol: str,
    source: str,
    is_dca: bool,
    order_usdt: float | None,
) -> None:
    """Log line + best-effort audit row (``obs_pre_trade_gate``). Never raises."""
    try:
        from logger import log

        level = "WARNING" if result.verdict == VERDICT_BLOCK else "INFO"
        log(log_line(result, tenant_id=tenant_id, symbol=symbol, source=source), level)
    except Exception:
        pass
    try:
        from services.observe_streams import mirror_record

        row = {
            "kind": "pre_trade_gate",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tenant_id": tenant_id,
            "symbol": symbol,
            "source": source,
            "is_dca": bool(is_dca),
            "order_usdt": order_usdt,
            **result.to_dict(),
        }
        mirror_record(OBSERVE_COLLECTION, row)
    except Exception:
        pass
