"""Central order ledger — scope-isolated demo / paper / live."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta
from typing import Optional

from core.config import get_bot_config
from core.models import (
    OrderStatus,
    RiskDecision,
    TradeOrder,
    TradeResult,
    is_executed_status,
    stored_status,
)
from core.tenant_context import resolve_tenant_id, resolve_tenant_scope
from core.time_utils import (
    format_operator_time,
    ledger_datetime_utc,
    process_local_tz,
    utc_now,
)
from data_manager import (
    get_config,
    load_orders,
    resolve_orders_file,
    save_orders,
)
from logger import log

ORDERS_PER_PAGE = 5
# Hard cap for full-list views (/orders, /orders_blocked, /orders_month) — no pager.
ORDERS_LIST_HARD_CAP = 500
PENDING_TTL_MINUTES = 10
EXECUTED_STATUSES = frozenset({OrderStatus.EXECUTED.value})
TRADE_BOOK_STATUSES = frozenset({OrderStatus.EXECUTED.value})
# Non-executed attempts shown under /orders_blocked.
# Enum values plus legacy tokens still present in the live Mongo ledger.
BLOCKED_STATUSES = frozenset({
    OrderStatus.REJECTED.value,
    OrderStatus.CANCELED.value,
    OrderStatus.ACTIVE.value,
    OrderStatus.QUEUED.value,
    OrderStatus.PARTIALLY_FILLED.value,
    "cancelled",  # legacy British spelling
    "failed",  # legacy write; from_legacy → REJECTED
    "expired",
    "pending_confirmation",  # pre-submission, outside OrderStatus
})

STATUS_ICONS = {
    "pending_confirmation": "⏳",
    "cancelled": "🚫",
    OrderStatus.CANCELED.value: "🚫",
    "expired": "⌛",
    OrderStatus.REJECTED.value: "❌",
    OrderStatus.ACTIVE.value: "🔄",
    OrderStatus.EXECUTED.value: "✅",
    "failed": "⚠️",
    OrderStatus.QUEUED.value: "📥",
    OrderStatus.PARTIALLY_FILLED.value: "◐",
}

_ORDERS_READ_CACHE: dict[str, tuple[float, dict]] = {}
# Command read path: longer TTL; writes still invalidate via _save.
# 90s cuts repeat /orders cold blob cost without feeling stale for operators.
_ORDERS_READ_CACHE_TTL = 90.0
# Stop reverse day/month scans after this many consecutive stamps before window start.
_WINDOW_EARLY_STOP_STREAK = 12
# Honest instrumentation: full-history blob loads via OrderService._load
_BLOB_LOAD_COUNT = 0


def reset_blob_load_count() -> None:
    global _BLOB_LOAD_COUNT
    _BLOB_LOAD_COUNT = 0


def blob_load_count() -> int:
    """How many times OrderService loaded the legacy full orders document."""
    return _BLOB_LOAD_COUNT

SOURCE_LABELS = {
    "auto": "Auto",
    "manual": "Manuell",
    "x": "X",
    "cmc": "CMC",
}


def source_label(source: str) -> str:
    return SOURCE_LABELS.get(source or "auto", source or "Auto")


def infer_manual_source(order: dict) -> Optional[str]:
    """Heuristic for legacy orders saved before source was propagated."""
    if order.get("source") not in (None, "", "auto"):
        return None
    side = (order.get("side") or "").lower()
    signal = (order.get("signal") or "").strip()
    if side == "buy" and not signal:
        return "manual"
    if side == "sell" and signal == "SELL":
        return "manual"
    return None


def ledger_label(scope: str = None) -> str:
    scope = scope or resolve_tenant_scope()
    labels = {"demo": "DEMO", "paper": "PAPER", "live": "GATE/LIVE"}
    return labels.get(scope, scope.upper())


def _orders_header_label(scope: str | None = None) -> str:
    """Ledger label including tenant id for multi-tenant Telegram views."""
    from core.tenant_context import multi_tenant_enabled

    scope = scope or resolve_tenant_scope()
    base = ledger_label(scope)
    if not multi_tenant_enabled():
        return base
    tid = resolve_tenant_id()
    if tid == "default":
        return base
    return f"{tid.upper()} · {base}"


def _now() -> str:
    # Writer unchanged (#320): naive process-local wall clock.
    return datetime.now().isoformat()


def _parse_ts(value: str):
    """Parse a ledger stamp to naive *display* clock (calendar windows).

    Naive values are process-local (``_now()`` / ``datetime.now()``), then
    converted to the operator/display zone. Duration logic must not use
    this — see ``ledger_datetime_utc``.
    """
    if not value:
        return None
    try:
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        return _as_display_naive(dt)
    except Exception:
        return None


def _process_local_tz():
    """Timezone of naive datetime.now() stamps (UTC on Railway, local on Mac)."""
    return process_local_tz()


def _as_display_naive(dt: datetime) -> datetime:
    """Normalize to naive *display* clock for calendar day/month windows.

    Naive ledger timestamps come from ``datetime.now()`` (process-local wall
    clock — typically UTC on Railway). They are NOT already Europe/Berlin;
    attach process-local tzinfo first, then convert to display_tz.
    Telegram rendering (``_format_ts_short``) goes through
    ``format_operator_time``, which applies the same rule.
    """
    try:
        from core.time_utils import display_tz

        target = display_tz()
    except Exception:
        target = None
    if dt.tzinfo is None:
        local_tz = _process_local_tz()
        if local_tz is not None:
            dt = dt.replace(tzinfo=local_tz)
        elif target is not None:
            # Fallback: assume display tz if process tz unknown
            dt = dt.replace(tzinfo=target)
        else:
            return dt
    if target is None:
        return dt.replace(tzinfo=None)
    try:
        return dt.astimezone(target).replace(tzinfo=None)
    except Exception:
        return dt.replace(tzinfo=None)


def _display_now_naive() -> datetime:
    try:
        from core.time_utils import now_display

        n = now_display()
        # now_display is already in display tz; strip tz for window math
        if n.tzinfo is not None:
            return n.replace(tzinfo=None)
        return _as_display_naive(n)
    except Exception:
        return _as_display_naive(datetime.now())


def calendar_day_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    """[start, end) for the current calendar day in display timezone."""
    n = _as_display_naive(now) if now is not None else _display_now_naive()
    start = n.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def calendar_month_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    """[start, end) for the current calendar month in display timezone."""
    n = _as_display_naive(now) if now is not None else _display_now_naive()
    start = n.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def _order_event_raw(order: dict) -> str | None:
    ts = order.get("timestamps") or {}
    if is_executed_status(order.get("status")):
        return ts.get("filled") or ts.get("created") or ts.get("updated")
    return ts.get("created") or ts.get("updated") or ts.get("filled")


def order_event_ts(order: dict) -> datetime | None:
    """Prefer fill time for executed trades, else created/updated (display naive)."""
    return _parse_ts(_order_event_raw(order) or "")


def order_event_ts_utc(order: dict) -> datetime | None:
    """Same stamp as ``order_event_ts``, interpreted as aware UTC for durations."""
    return ledger_datetime_utc(_order_event_raw(order))


def order_in_window(order: dict, start: datetime, end: datetime) -> bool:
    ts = order_event_ts(order)
    if ts is None:
        return False
    return start <= ts < end


def _format_ts_short(value: str) -> str:
    return format_operator_time(value, "%d.%m.%Y %H:%M")


def _trade_date_label(side: str) -> str:
    raw = (side or "").lower()
    if raw == "buy":
        return "Kaufdatum"
    if raw == "sell":
        return "Verkaufdatum"
    if raw == "short":
        return "Short-Datum"
    if raw == "cover":
        return "Cover-Datum"
    return "Datum"


def order_side_glyph(side: str) -> str:
    return {
        "buy": "🟢",
        "sell": "🔴",
        "short": "🔻",
        "cover": "🔺",
    }.get((side or "").strip().lower(), "")


def _order_trade_ts(order: dict) -> str:
    ts = order.get("timestamps", {})
    return _format_ts_short(ts.get("filled") or ts.get("created") or "")


class OrderService:
    def __init__(self, scope: str = None):
        self.scope = scope or resolve_tenant_scope()
        self._path = resolve_orders_file(self.scope)  # noqa: F841 — reserved for diagnostics

    def _cache_key(self) -> str:
        from core.tenant_context import resolve_tenant_id

        return f"{resolve_tenant_id()}:{self.scope}"

    def _load(self) -> dict:
        global _BLOB_LOAD_COUNT
        now = time.time()
        key = self._cache_key()
        cached = _ORDERS_READ_CACHE.get(key)
        if cached and now - cached[0] < _ORDERS_READ_CACHE_TTL:
            data = cached[1]
        else:
            _BLOB_LOAD_COUNT += 1
            data = load_orders(self.scope)
            if data.get("ledger_scope") != self.scope:
                data["ledger_scope"] = self.scope
            _ORDERS_READ_CACHE[key] = (now, data)
        return data

    def _save(self, data: dict) -> bool:
        data["ledger_scope"] = self.scope
        ok = save_orders(data, self.scope)
        if ok:
            _ORDERS_READ_CACHE[self._cache_key()] = (time.time(), data)
        return ok

    def _next_seq(self, data: dict) -> int:
        orders = data.get("orders", [])
        if not orders:
            return 1
        return max(int(o.get("display_seq", 0)) for o in orders) + 1

    def _find(self, data: dict, order_id: str = None, display_seq: int = None) -> Optional[dict]:
        for o in data.get("orders", []):
            if order_id and o.get("id") == order_id:
                return o
            if display_seq is not None and int(o.get("display_seq", -1)) == display_seq:
                return o
        return None

    def find_by_idempotency_key(self, key: str) -> Optional[dict]:
        if not key:
            return None
        data = self._load()
        for order in reversed(data.get("orders", [])):
            if order.get("idempotency_key") == key:
                return order
        return None

    def create_from_request(
        self,
        order: TradeOrder,
        *,
        timeframe: str = "4h",
        status: str = "pending_confirmation",
        request_extra: dict = None,
        risk: RiskDecision = None,
        telegram_token: str = None,
        idempotency_key: str = None,
    ) -> dict:
        data = self._load()
        cfg = get_bot_config()
        from core.tenant_context import resolve_tenant_id

        idem = (
            idempotency_key
            or getattr(order, "client_order_id", "")
            or getattr(order, "idempotency_key", "")
            or ""
        )
        record = {
            "id": telegram_token or uuid.uuid4().hex[:12],
            "display_seq": self._next_seq(data),
            "status": stored_status(status) or status,
            "side": order.type.lower(),
            "symbol": order.symbol,
            "timeframe": timeframe,
            "order_type": "market",
            "source": order.source or "auto",
            "signal": order.signal or "",
            "exit_source": (getattr(order, "exit_source", None) or "") or None,
            "exit_rationale": (getattr(order, "exit_rationale", None) or "") or None,
            "idempotency_key": idem or None,
            "client_order_id": idem or None,
            "exchange_order_id": getattr(order, "exchange_order_id", "") or None,
            "order_exist_in_exchange": bool(
                getattr(order, "order_exist_in_exchange", False)
            ),
            "qty": float(order.qty or 0) or None,
            "filled_qty": float(getattr(order, "filled_qty", 0) or 0),
            "reduce_only": bool(getattr(order, "reduce_only", False)),
            "tenant_id": resolve_tenant_id(),
            "trading_mode": cfg.trading_mode,
            "ledger_scope": self.scope,
            **_leverage_payload(order),
            "request": {
                "price": float(order.price or 0),
                "amount": float(order.qty or 0) or None,
                "usdt": float(order.usdt_amount or 0) or None,
                **_leverage_payload(order),
                **(request_extra or {}),
            },
            "risk": self._risk_snapshot(risk),
            "execution": {},
            "pnl": None,
            "error": None,
            "timestamps": {"created": _now(), "updated": _now()},
        }
        data.setdefault("orders", []).append(record)
        self._save(data)
        self._dual_write_v2(record)
        return record

    def record_rejected(
        self,
        order: TradeOrder,
        decision: RiskDecision,
        *,
        timeframe: str = "4h",
        request_extra: dict = None,
    ) -> dict:
        data = self._load()
        from core.tenant_context import resolve_tenant_id

        record = {
            "id": uuid.uuid4().hex[:12],
            "display_seq": self._next_seq(data),
            "status": OrderStatus.REJECTED.value,
            "side": order.type.lower(),
            "symbol": order.symbol,
            "timeframe": timeframe,
            "order_type": "market",
            "tenant_id": resolve_tenant_id(),
            "source": order.source or "auto",
            "signal": order.signal or "",
            "exit_source": (getattr(order, "exit_source", None) or "") or None,
            "exit_rationale": (getattr(order, "exit_rationale", None) or "") or None,
            "trading_mode": get_bot_config().trading_mode,
            "ledger_scope": self.scope,
            **_leverage_payload(order),
            "request": {
                "price": float(order.price or 0),
                "amount": float(order.amount or 0) or None,
                "usdt": float(order.usdt_amount or 0) or None,
                **_leverage_payload(order),
                **(request_extra or {}),
            },
            "risk": self._risk_snapshot(decision, approved=False),
            "execution": {},
            "pnl": None,
            "error": decision.message,
            "timestamps": {"created": _now(), "updated": _now()},
        }
        data.setdefault("orders", []).append(record)
        self._save(data)
        self._dual_write_v2(record)
        return record

    def update_status(
        self,
        order_id: str,
        status: str,
        *,
        execution: dict = None,
        error: str = None,
        pnl: float = None,
        risk: dict = None,
    ) -> Optional[dict]:
        data = self._load()
        record = self._find(data, order_id=order_id)
        if not record or record.get("ledger_scope") != self.scope:
            return None
        stored = stored_status(status) or status
        record["status"] = stored
        record["timestamps"]["updated"] = _now()
        if is_executed_status(stored):
            record["timestamps"]["filled"] = _now()
        if execution:
            record["execution"] = {**record.get("execution", {}), **execution}
        if error is not None:
            record["error"] = error
        if pnl is not None:
            record["pnl"] = pnl
        if risk:
            record["risk"] = {**record.get("risk", {}), **risk}
        self._save(data)
        self._dual_write_v2(record)
        return record

    def _dual_write_v2(self, record: dict) -> None:
        """Per-order upsert into ledger v2 (no full-history rewrite on v2 path)."""
        try:
            from storage.order_ledger_v2 import get_order_ledger_v2

            store = get_order_ledger_v2()
            if store is None:
                return
            store.upsert_order(record)
        except Exception as e:
            # Fail-open: v2 is still a shadow; legacy blob remains source of truth.
            log(f"order ledger v2 dual-write failed: {e}", "WARNING")

    def _v2_day_key(self, now: datetime | None = None) -> str:
        """Display-calendar day key YYYY-MM-DD for v2 day queries."""
        from storage.order_ledger_v2 import display_day_key_now

        if now is None:
            return display_day_key_now()
        n = _as_display_naive(now)
        return n.strftime("%Y-%m-%d")

    def get_by_id(self, order_id: str) -> Optional[dict]:
        """Lookup single order; prefer v2 when reads enabled (no full-blob load)."""
        try:
            from storage.order_ledger_v2 import get_order_ledger_v2, order_ledger_v2_reads_enabled

            store = get_order_ledger_v2()
            if store is not None and order_ledger_v2_reads_enabled():
                hit = store.get_by_id(resolve_tenant_id(), self.scope, order_id)
                if hit is not None:
                    return hit
        except Exception:
            pass
        return self._find(self._load(), order_id=order_id)

    def get_by_display_seq(self, display_seq: int) -> Optional[dict]:
        """Lookup by display_seq; prefer v2 when reads enabled (no full-blob load)."""
        try:
            from storage.order_ledger_v2 import get_order_ledger_v2, order_ledger_v2_reads_enabled

            store = get_order_ledger_v2()
            if store is not None and order_ledger_v2_reads_enabled():
                hit = store.get_by_display_seq(
                    resolve_tenant_id(), self.scope, int(display_seq),
                )
                if hit is not None:
                    return hit
        except Exception:
            pass
        return self._find(self._load(), display_seq=display_seq)

    def expire_stale_pending(self) -> int:
        data = self._load()
        cutoff = utc_now() - timedelta(minutes=PENDING_TTL_MINUTES)
        count = 0
        touched: list[dict] = []
        for o in data.get("orders", []):
            if o.get("status") != "pending_confirmation":
                continue
            ts = ledger_datetime_utc(o.get("timestamps", {}).get("created"))
            if ts and ts < cutoff:
                o["status"] = "expired"
                o["timestamps"]["updated"] = _now()
                count += 1
                touched.append(o)
        if count:
            self._save(data)
            for o in touched:
                self._dual_write_v2(o)
        return count

    def reconcile_legacy_sources(self) -> int:
        data = self._load()
        changed = 0
        touched: list[dict] = []
        for order in data.get("orders", []):
            if order.get("ledger_scope") != self.scope:
                continue
            inferred = infer_manual_source(order)
            if inferred:
                order["source"] = inferred
                changed += 1
                touched.append(order)
        if changed:
            self._save(data)
            for o in touched:
                self._dual_write_v2(o)
        return changed

    def list_recent_rejected(self, *, hours: float = 24, limit: int = 5) -> list:
        """Recent rejected orders (legacy helper; prefer list_blocked_orders)."""
        data = self._load()
        cutoff = utc_now() - timedelta(hours=hours)
        rejected = []
        for order in reversed(data.get("orders", [])):
            if order.get("ledger_scope") != self.scope:
                continue
            if OrderStatus.try_legacy(order.get("status")) is not OrderStatus.REJECTED:
                continue
            ts = order_event_ts_utc(order)
            if not ts or ts < cutoff:
                continue
            rejected.append(order)
            if len(rejected) >= limit:
                break
        return rejected

    def _scoped_orders_newest_first(self, *, mutate: bool = False) -> list:
        """Newest-first scoped orders.

        ``mutate=False`` (default for list/stats): pure read — no expire/reconcile.
        ``mutate=True``: maintenance side-effects (writes) before read.
        """
        if mutate:
            self.expire_stale_pending()
            self.reconcile_legacy_sources()
        data = self._load()
        orders = [o for o in data.get("orders", []) if o.get("ledger_scope") == self.scope]
        return list(reversed(orders))

    @staticmethod
    def _filter_window_newest_first(
        orders: list,
        start: datetime,
        end: datetime,
        *,
        early_stop: bool = True,
        stop_streak: int = _WINDOW_EARLY_STOP_STREAK,
    ) -> list:
        """Keep orders in [start, end). ``orders`` must be newest-first.

        When *early_stop* is set, stop after *stop_streak* consecutive stamps
        strictly before *start* (append-only ledgers are roughly chronological).
        """
        out: list = []
        streak = 0
        for o in orders:
            ts = order_event_ts(o)
            if ts is None:
                continue
            if ts >= end:
                streak = 0
                continue
            if ts < start:
                streak += 1
                if early_stop and streak >= max(1, stop_streak):
                    break
                continue
            streak = 0
            out.append(o)
        return out

    def list_orders(
        self,
        *,
        status_filter: set = None,
        trade_book_only: bool = False,
        hours: float = None,
        since: datetime | None = None,
        until: datetime | None = None,
        page: int = 1,
        per_page: int = ORDERS_PER_PAGE,
        mutate: bool = False,
    ) -> tuple[list, int]:
        orders = self._scoped_orders_newest_first(mutate=mutate)
        if trade_book_only:
            orders = [o for o in orders if o.get("status") in TRADE_BOOK_STATUSES]
        if status_filter:
            orders = [o for o in orders if o.get("status") in status_filter]
        if hours is not None:
            cutoff = utc_now() - timedelta(hours=hours)
            orders = [
                o for o in orders
                if (ts := order_event_ts_utc(o)) is not None and ts >= cutoff
            ]
        if since is not None or until is not None:
            start = since or datetime.min
            end = until or datetime.max
            # Early-stop only when lower bound is real (day/month windows).
            orders = self._filter_window_newest_first(
                orders,
                start,
                end,
                early_stop=since is not None,
            )
        total = len(orders)
        start_i = (max(1, page) - 1) * per_page
        return orders[start_i:start_i + per_page], max(1, (total + per_page - 1) // per_page)

    def list_day_filled(
        self,
        *,
        page: int = 1,
        per_page: int = ORDERS_PER_PAGE,
        now: datetime | None = None,
    ) -> tuple[list, int]:
        """Executed (filled) trades for the current calendar day."""
        start, end = calendar_day_bounds(now)
        return self.list_orders(
            trade_book_only=True,
            since=start,
            until=end,
            page=page,
            per_page=per_page,
        )

    @staticmethod
    def _merge_orders_by_id(*lists: list) -> list:
        """Union order lists by id (first occurrence wins; preserve order)."""
        seen: set[str] = set()
        out: list = []
        for lst in lists:
            for o in lst or []:
                oid = str(o.get("id") or "")
                if not oid or oid in seen:
                    continue
                seen.add(oid)
                out.append(o)
        return out

    def list_day_filled_all(self, *, now: datetime | None = None) -> list:
        """All filled trades for the display calendar day (no pager).

        Prefer order-ledger v2 day index. Until backfill is complete, union with
        legacy day window so blob-only same-day fills are not dropped.
        """
        v2_day: list = []
        use_v2 = False
        try:
            from storage.order_ledger_v2 import (
                get_order_ledger_v2,
                order_ledger_v2_backfill_complete,
                order_ledger_v2_reads_enabled,
            )

            store = get_order_ledger_v2()
            tid = resolve_tenant_id()
            if store is not None and order_ledger_v2_reads_enabled():
                use_v2 = True
                v2_day = store.query_day(
                    tid,
                    self.scope,
                    self._v2_day_key(now),
                    filled_only=True,
                    limit=ORDERS_LIST_HARD_CAP,
                )
                if order_ledger_v2_backfill_complete() and store.has_tenant_orders(
                    tid, self.scope
                ):
                    return v2_day
        except Exception:
            use_v2 = False

        # Legacy day window (full blob) — always when v2 off; union when partial dual-write
        orders, _ = self.list_day_filled(
            page=1, per_page=ORDERS_LIST_HARD_CAP, now=now,
        )
        if use_v2 and v2_day:
            return self._merge_orders_by_id(v2_day, orders)[:ORDERS_LIST_HARD_CAP]
        if use_v2 and not orders:
            return v2_day
        return orders

    def list_month_filled(
        self,
        *,
        page: int = 1,
        per_page: int = ORDERS_PER_PAGE,
        now: datetime | None = None,
    ) -> tuple[list, int]:
        """Executed (filled) trades for the current calendar month."""
        start, end = calendar_month_bounds(now)
        return self.list_orders(
            trade_book_only=True,
            since=start,
            until=end,
            page=page,
            per_page=per_page,
        )

    def list_month_filled_all(self, *, now: datetime | None = None) -> list:
        """All filled trades for the display calendar month (no pager)."""
        try:
            from storage.order_ledger_v2 import (
                get_order_ledger_v2,
                order_ledger_v2_reads_enabled,
            )

            store = get_order_ledger_v2()
            tid = resolve_tenant_id()
            if store is not None and order_ledger_v2_reads_enabled():
                from storage.order_ledger_v2 import order_ledger_v2_backfill_complete

                start, end = calendar_month_bounds(now)
                out: list = []
                day = start
                while day < end and len(out) < ORDERS_LIST_HARD_CAP:
                    day_key = day.strftime("%Y-%m-%d")
                    chunk = store.query_day(
                        tid,
                        self.scope,
                        day_key,
                        filled_only=True,
                        limit=ORDERS_LIST_HARD_CAP - len(out),
                    )
                    out.extend(chunk)
                    day = day + timedelta(days=1)
                out.sort(
                    key=lambda o: str(
                        (o.get("timestamps") or {}).get("filled")
                        or (o.get("timestamps") or {}).get("created")
                        or ""
                    ),
                    reverse=True,
                )
                if order_ledger_v2_backfill_complete() and store.has_tenant_orders(
                    tid, self.scope
                ):
                    return out[:ORDERS_LIST_HARD_CAP]
                # Partial: union with legacy month window
                legacy, _ = self.list_month_filled(
                    page=1, per_page=ORDERS_LIST_HARD_CAP, now=now,
                )
                return self._merge_orders_by_id(out, legacy)[:ORDERS_LIST_HARD_CAP]
        except Exception:
            pass
        orders, _ = self.list_month_filled(
            page=1, per_page=ORDERS_LIST_HARD_CAP, now=now,
        )
        return orders

    def list_blocked_orders(
        self,
        *,
        page: int = 1,
        per_page: int = ORDERS_PER_PAGE,
        now: datetime | None = None,
        day_only: bool = True,
    ) -> tuple[list, int]:
        """Blocked/non-executed order attempts (rejected, cancelled, failed, …)."""
        kwargs: dict = {
            "status_filter": set(BLOCKED_STATUSES),
            "page": page,
            "per_page": per_page,
        }
        if day_only:
            start, end = calendar_day_bounds(now)
            kwargs["since"] = start
            kwargs["until"] = end
        return self.list_orders(**kwargs)

    def list_blocked_day_all(self, *, now: datetime | None = None) -> list:
        """All blocked attempts for the display calendar day (no pager)."""
        try:
            from storage.order_ledger_v2 import (
                get_order_ledger_v2,
                order_ledger_v2_reads_enabled,
            )

            store = get_order_ledger_v2()
            tid = resolve_tenant_id()
            if store is not None and order_ledger_v2_reads_enabled():
                from storage.order_ledger_v2 import order_ledger_v2_backfill_complete

                v2_b = store.query_day(
                    tid,
                    self.scope,
                    self._v2_day_key(now),
                    blocked_only=True,
                    limit=ORDERS_LIST_HARD_CAP,
                )
                if order_ledger_v2_backfill_complete() and store.has_tenant_orders(
                    tid, self.scope
                ):
                    return v2_b
                orders, _ = self.list_blocked_orders(
                    page=1, per_page=ORDERS_LIST_HARD_CAP, now=now, day_only=True,
                )
                if v2_b:
                    return self._merge_orders_by_id(v2_b, orders)[:ORDERS_LIST_HARD_CAP]
                return orders
        except Exception:
            pass
        orders, _ = self.list_blocked_orders(
            page=1, per_page=ORDERS_LIST_HARD_CAP, now=now, day_only=True,
        )
        return orders

    @staticmethod
    def _order_notional_usdt(order: dict) -> float:
        exe = order.get("execution") or {}
        req = order.get("request") or {}
        for bag in (exe, req):
            try:
                usdt = float(bag.get("usdt") or 0)
            except (TypeError, ValueError):
                usdt = 0.0
            if usdt > 0:
                return usdt
        try:
            price = float(exe.get("price") or req.get("price") or 0)
            amount = float(exe.get("amount") or req.get("amount") or 0)
        except (TypeError, ValueError):
            return 0.0
        if price > 0 and amount > 0:
            return price * amount
        return 0.0

    @classmethod
    def stats_from_filled_orders(cls, orders: list) -> dict:
        """Pure stats from an already-filtered filled order list (no I/O).

        Implementation lives in ``storage.order_ledger_v2.stats_from_filled_orders``
        so blob and v2 day-stats stay on one code path.
        """
        from storage.order_ledger_v2 import stats_from_filled_orders as _stats

        return _stats(orders)

    @staticmethod
    def stats_blocked_from_orders(orders: list) -> dict:
        """Status counts from an already-filtered blocked order list (no I/O)."""
        counts = {st: 0 for st in sorted(BLOCKED_STATUSES)}
        for o in orders:
            st = (o.get("status") or "").lower()
            if st in counts:
                counts[st] = counts.get(st, 0) + 1
        return counts

    @staticmethod
    def blocked_codes_from_orders(orders: list, *, top: int = 3) -> list[tuple[str, int]]:
        code_counts: dict[str, int] = {}
        for o in orders:
            code = str((o.get("risk") or {}).get("code") or "").strip()
            if not code:
                continue
            code_counts[code] = code_counts.get(code, 0) + 1
        ranked = sorted(code_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        return ranked[: max(0, int(top or 0))]

    def _stats_filled_window(self, start: datetime, end: datetime) -> dict:
        """Buy/sell counts, volume and realized PnL for filled trades in [start, end)."""
        orders = self._scoped_orders_newest_first(mutate=False)
        filled = [
            o for o in orders
            if is_executed_status(o.get("status"))
        ]
        in_window = self._filter_window_newest_first(filled, start, end, early_stop=True)
        return self.stats_from_filled_orders(in_window)

    def stats_day_filled(self, now: datetime | None = None) -> dict:
        """Buy/sell counts + volume + realized PnL for filled trades today.

        After backfill: pure v2 day_stats (no blob). Before backfill with
        READS=1: recompute from unioned day list so blob-only fills count.
        """
        try:
            from storage.order_ledger_v2 import (
                get_order_ledger_v2,
                order_ledger_v2_backfill_complete,
                order_ledger_v2_reads_enabled,
            )

            store = get_order_ledger_v2()
            tid = resolve_tenant_id()
            if store is not None and order_ledger_v2_reads_enabled():
                if order_ledger_v2_backfill_complete() and store.has_tenant_orders(
                    tid, self.scope
                ):
                    stats = store.get_day_stats(
                        tid, self.scope, self._v2_day_key(now),
                    )
                    return {
                        "filled": int(stats.get("filled") or 0),
                        "buys": int(stats.get("buys") or 0),
                        "sells": int(stats.get("sells") or 0),
                        "shorts": int(stats.get("shorts") or 0),
                        "covers": int(stats.get("covers") or 0),
                        "buy_usdt": float(stats.get("buy_usdt") or 0),
                        "sell_usdt": float(stats.get("sell_usdt") or 0),
                        "short_usdt": float(stats.get("short_usdt") or 0),
                        "cover_usdt": float(stats.get("cover_usdt") or 0),
                        "realized_pnl": float(stats.get("realized_pnl") or 0),
                        "sell_wins": int(stats.get("sell_wins") or 0),
                        "sell_losses": int(stats.get("sell_losses") or 0),
                        "wins": int(stats.get("wins") or 0),
                        "losses": int(stats.get("losses") or 0),
                        "unknown_side": int(stats.get("unknown_side") or 0),
                    }
                # Partial dual-write: parity with unioned day list
                return self.stats_from_filled_orders(self.list_day_filled_all(now=now))
        except Exception:
            pass
        start, end = calendar_day_bounds(now)
        return self._stats_filled_window(start, end)

    def stats_day_filled_fast(self, now: datetime | None = None) -> dict:
        """Day stats for interactive /positions — never load the legacy blob.

        Prefers v2 ``query_day(filled_only)`` (indexed). If the blob is already
        in the 90s read cache, use that. Otherwise return empty stats so the
        Heute-line is omitted instead of blocking Telegram on a multi-MB Mongo
        document (gainer_relvol rejects inflate the blob).
        """
        empty = {
            "filled": 0,
            "buys": 0,
            "sells": 0,
            "shorts": 0,
            "covers": 0,
            "buy_usdt": 0.0,
            "sell_usdt": 0.0,
            "short_usdt": 0.0,
            "cover_usdt": 0.0,
            "realized_pnl": 0.0,
            "sell_wins": 0,
            "sell_losses": 0,
            "wins": 0,
            "losses": 0,
            "unknown_side": 0,
        }
        try:
            from storage.order_ledger_v2 import (
                get_order_ledger_v2,
                order_ledger_v2_is_degraded,
            )

            if order_ledger_v2_is_degraded():
                store = None
            else:
                store = get_order_ledger_v2()
            if store is not None:
                tid = resolve_tenant_id()
                filled = store.query_day(
                    tid,
                    self.scope,
                    self._v2_day_key(now),
                    filled_only=True,
                    limit=ORDERS_LIST_HARD_CAP,
                )
                return self.stats_from_filled_orders(filled)
        except Exception:
            pass
        cached = _ORDERS_READ_CACHE.get(self._cache_key())
        if cached and (time.time() - cached[0]) < _ORDERS_READ_CACHE_TTL:
            start, end = calendar_day_bounds(now)
            return self._stats_filled_window(start, end)
        return empty

    def stats_month_filled(self, now: datetime | None = None) -> dict:
        """Buy/sell counts + volume + realized PnL for filled trades this month."""
        start, end = calendar_month_bounds(now)
        return self._stats_filled_window(start, end)

    def stats_blocked_day(self, now: datetime | None = None) -> dict:
        """Counts of blocked statuses for today."""
        orders = self.list_blocked_day_all(now=now)
        return self.stats_blocked_from_orders(orders)

    def stats_blocked_day_codes(self, now: datetime | None = None, *, top: int = 3) -> list[tuple[str, int]]:
        """Top risk.code values among blocked orders today (for header)."""
        orders = self.list_blocked_day_all(now=now)
        return self.blocked_codes_from_orders(orders, top=top)

    def stats_24h(self) -> dict:
        """Count all ledger entries in the last 24h (including blocked / pending)."""
        self.expire_stale_pending()
        data = self._load()
        cutoff = utc_now() - timedelta(hours=24)
        counts = {
            "filled": 0,
            "rejected": 0,
            "cancelled": 0,
            "pending_confirmation": 0,
            "failed": 0,
            "expired": 0,
            "executing": 0,
        }
        for o in data.get("orders", []):
            if o.get("ledger_scope") != self.scope:
                continue
            ts = ledger_datetime_utc(o.get("timestamps", {}).get("created"))
            if not ts or ts < cutoff:
                continue
            st = o.get("status", "")
            if st in counts:
                counts[st] += 1
        return counts

    def stats_executed_24h(self) -> dict:
        """Filled buy/sell counts for the classic order book view."""
        self.expire_stale_pending()
        data = self._load()
        cutoff = utc_now() - timedelta(hours=24)
        counts = {"filled": 0, "buys": 0, "sells": 0, "shorts": 0, "covers": 0}
        for o in data.get("orders", []):
            if o.get("ledger_scope") != self.scope:
                continue
            if not is_executed_status(o.get("status")):
                continue
            ts = ledger_datetime_utc(
                o.get("timestamps", {}).get("filled") or o.get("timestamps", {}).get("created")
            )
            if not ts or ts < cutoff:
                continue
            counts["filled"] += 1
            side = (o.get("side") or "").lower()
            if side == "buy":
                counts["buys"] += 1
            elif side == "sell":
                counts["sells"] += 1
            elif side == "short":
                counts["shorts"] += 1
            elif side == "cover":
                counts["covers"] += 1
        return counts

    def link_execution_result(self, order_id: str, result: TradeResult, approved_order: TradeOrder = None) -> None:
        if not order_id:
            return
        if result.executed and approved_order:
            data = self._load()
            record = self._find(data, order_id=order_id)
            dirty = False
            if record and approved_order.source and record.get("source") != approved_order.source:
                record["source"] = approved_order.source
                dirty = True
            if record and getattr(approved_order, "exit_source", None):
                if record.get("exit_source") != approved_order.exit_source:
                    record["exit_source"] = approved_order.exit_source
                    dirty = True
                if approved_order.exit_rationale and record.get("exit_rationale") != approved_order.exit_rationale:
                    record["exit_rationale"] = approved_order.exit_rationale
                    dirty = True
            if dirty:
                self._save(data)
        st = getattr(result, "order_status", None)
        if not isinstance(st, OrderStatus):
            st = OrderStatus.try_legacy(st)

        exist = bool(getattr(result, "order_exist_in_exchange", False))
        if approved_order is not None:
            exist = exist or bool(getattr(approved_order, "order_exist_in_exchange", False))

        extra_fields = {
            "order_exist_in_exchange": exist,
            "exchange_order_id": getattr(result, "exchange_order_id", None)
            or (getattr(approved_order, "exchange_order_id", None) if approved_order else None),
            "filled_qty": float(
                getattr(result, "filled_qty", 0) or getattr(result, "amount", 0) or 0
            ),
            "fee_unknown": bool(getattr(result, "fee_unknown", False)),
            "needs_reconcile": bool(getattr(result, "needs_reconcile", False)),
        }
        if extra_fields["exchange_order_id"] in (None, ""):
            extra_fields["exchange_order_id"] = None

        if result.executed:
            if st is None:
                st = OrderStatus.EXECUTED
                req_qty = float(getattr(approved_order, "qty", 0) or 0) if approved_order else 0.0
                filled = float(getattr(result, "filled_qty", 0) or result.amount or 0)
                if req_qty > 0 and 0 < filled < req_qty:
                    st = OrderStatus.PARTIALLY_FILLED
            execution = {
                "price": float(result.price or 0),
                "amount": float(result.amount or 0),
                "usdt": float(result.usdt_amount or 0),
                "exchange_order_id": extra_fields["exchange_order_id"],
                "fee": float(getattr(result, "fee", 0) or 0) or None,
                "fee_unknown": extra_fields["fee_unknown"],
            }
            filled_gross = float(getattr(result, "filled_qty", 0) or 0)
            if filled_gross > 0:
                execution["filled_qty_gross"] = filled_gross
            lev_pay = _leverage_payload(approved_order)
            if lev_pay:
                execution.update(lev_pay)
            # Sensor-entry-guard: stamp Gate venue metrics at fill for memory learning
            try:
                side = (approved_order.type if approved_order else "") or ""
                if str(side).upper() == "BUY" and approved_order:
                    from services.venue_quality import stamp_venue_for_fill

                    planned = float(
                        result.usdt_amount
                        or getattr(approved_order, "usdt_amount", 0)
                        or 0
                    )
                    execution["venue"] = stamp_venue_for_fill(
                        approved_order.symbol,
                        planned_usdt=planned,
                    )
            except Exception:
                execution["venue"] = {"capture": "missing"}
            record = self.update_status(
                order_id,
                st,
                execution=execution,
                pnl=float(result.pnl) if result.pnl is not None else None,
            )
        elif getattr(result, "pending", False) or getattr(result, "needs_reconcile", False):
            record = self.update_status(
                order_id,
                st or OrderStatus.ACTIVE,
                error=result.message or "pending reconcile",
                execution={
                    "exchange_order_id": extra_fields["exchange_order_id"],
                    "fee_unknown": extra_fields["fee_unknown"],
                },
            )
        elif st is OrderStatus.CANCELED:
            record = self.update_status(
                order_id, OrderStatus.CANCELED, error=result.message or "canceled"
            )
        elif st is OrderStatus.REJECTED:
            record = self.update_status(
                order_id, OrderStatus.REJECTED, error=result.message or "rejected"
            )
        else:
            # Last resort: never write legacy "failed". ACTIVE + reconcile (#314).
            record = self.update_status(
                order_id,
                OrderStatus.ACTIVE,
                error=result.message or "unknown execution state",
            )
        if record is not None:
            dirty = False
            for key, val in extra_fields.items():
                if val is None and key == "exchange_order_id":
                    continue
                if record.get(key) != val:
                    record[key] = val
                    dirty = True
            if dirty:
                data = self._load()
                found = self._find(data, order_id=order_id)
                if found is not None:
                    found.update({k: record[k] for k in extra_fields if k in record})
                    self._save(data)
                    self._dual_write_v2(found)

    @staticmethod
    def _risk_snapshot(decision: RiskDecision = None, approved: bool = True) -> dict:
        if not decision:
            return {"approved": approved, "message": "", "code": "", "size_multiplier": 1.0}
        approved_usdt = None
        if decision.order:
            approved_usdt = float(decision.order.usdt_amount or 0) or None
        return {
            "approved": decision.approved if decision else approved,
            "message": decision.message or "",
            "code": decision.code or "",
            "size_multiplier": float(decision.size_multiplier or 1.0),
            "approved_usdt": approved_usdt,
            "checked_at": _now(),
        }


def _leverage_payload(order: TradeOrder | None) -> dict:
    lev = getattr(order, "leverage", None) if order is not None else None
    try:
        val = float(lev) if lev is not None else 0.0
    except (TypeError, ValueError):
        return {}
    if val <= 0:
        return {}
    return {"leverage": val}


def _fmt_price(price) -> str:
    from price_fetcher import format_usdt_price

    return format_usdt_price(float(price or 0))


def _order_pnl_part(order: dict) -> str:
    """Realized trade PnL for list lines (sells; rarely set on buys)."""
    if order.get("pnl") is None:
        return ""
    try:
        return f"  PnL <b>${float(order['pnl']):+.1f}</b>"
    except (TypeError, ValueError):
        return ""


def _block_reason_parts(order: dict, *, msg_max: int = 48) -> tuple[str, str]:
    """Return (risk_code, short_message) for blocked list lines."""
    risk = order.get("risk") or {}
    code = str(risk.get("code") or "").strip()
    msg = str(risk.get("message") or order.get("error") or "").strip()
    if msg and code and msg.lower() == code.lower():
        msg = ""
    if len(msg) > msg_max:
        msg = msg[: max(1, msg_max - 1)].rstrip() + "…"
    return code, msg


def format_order_line(order: dict, *, show_block_reason: bool = False) -> str:
    from notifications.coin_links import format_ticker_html
    from notifications.telegram_i18n import t

    icon = STATUS_ICONS.get(order.get("status", ""), "·")
    sym = (order.get("symbol") or "").replace("/USDT", "")
    # List hot path: never CMC API / effective-watchlist (was multi-second per line)
    sym_html = format_ticker_html(sym, symbol_suffix="", allow_network=False)
    status_raw = (order.get("status") or "").lower()
    status_key = f"order_status_{status_raw}"
    status_label = t(status_key) if status_raw in (
        "filled", "rejected", "pending", "cancelled", "pending_confirmation",
    ) else (order.get("status") or "").upper()
    if status_raw == "pending_confirmation":
        status_label = t("order_status_pending")
    side_raw = (order.get("side") or "").lower()
    if side_raw == "buy":
        side = t("order_side_buy")
    elif side_raw == "sell":
        side = t("order_side_sell")
    elif side_raw == "short":
        side = t("order_side_short")
    elif side_raw == "cover":
        side = t("order_side_cover")
    else:
        side = (order.get("side") or "?").upper()
    glyph = order_side_glyph(side_raw)
    if glyph:
        side = f"{glyph} {side}"
    seq = order.get("display_seq", "?")
    src = source_label(order.get("source", "auto"))
    usdt = _order_usdt_display(order)
    pnl_part = _order_pnl_part(order)
    trade_ts = _order_trade_ts(order)
    date_part = f"  <i>{trade_ts}</i>" if trade_ts else ""
    reason_part = ""
    if show_block_reason:
        code, msg = _block_reason_parts(order)
        bits = []
        if code:
            bits.append(f"<code>{code}</code>")
        if msg:
            bits.append(msg)
        if bits:
            reason_part = "  · " + " · ".join(bits)
    return (
        f"{icon} <b>#{seq}</b> {status_label}  {side}  "
        f"<b>{sym_html}</b>  {usdt}{pnl_part}{reason_part}  <i>{src}</i>{date_part}"
    )


def format_order_detail(order: dict) -> str:
    from notifications.coin_links import format_links_line, format_ticker_html

    sym = (order.get("symbol") or "").replace("/USDT", "")
    sym_html = format_ticker_html(sym, symbol_suffix="")
    links = format_links_line(sym)
    req = order.get("request", {})
    risk = order.get("risk", {})
    exe = order.get("execution", {})
    ts = order.get("timestamps", {})
    side_raw = order.get("side", "")
    side_glyph = order_side_glyph(side_raw)
    side_txt = f"{side_glyph} {str(side_raw).upper()}" if side_glyph else str(side_raw).upper()
    lines = [
        f"<b>Order #{order.get('display_seq')} — {order.get('status', '').upper()}</b>",
        f"{side_txt} <b>{sym_html}</b> · {source_label(order.get('source', 'auto'))} · {ledger_label(order.get('ledger_scope'))}",
    ]
    if order.get("exit_source"):
        lines.append(f"<b>Exit</b>  <code>{order.get('exit_source')}</code>")
        if order.get("exit_rationale"):
            rat = str(order.get("exit_rationale") or "")[:180]
            lines.append(f"   <i>{rat}</i>")
    if links:
        lines.append(links)
    lines.extend([
        "",
        f"<b>Anfrage</b>  Kurs {_fmt_price(req.get('price', 0))}",
    ])
    if req.get("usdt"):
        lines.append(f"   USDT <b>${float(req['usdt']):.0f}</b>")
    if req.get("amount"):
        lines.append(f"   Menge <code>{float(req['amount']):.4f}</code>")
    if req.get("pct"):
        lines.append(f"   Anteil <b>{float(req['pct']) * 100:.0f}%</b>")
    if order.get("source") == "hermes" and req.get("hermes_experiment_id"):
        lines.append(f"<b>Hermes</b>  Experiment <code>{req['hermes_experiment_id']}</code>")
    lines.append(f"<b>Risk</b>  {risk.get('message') or '—'}")
    if risk.get("approved_usdt"):
        lines.append(f"   Freigegeben <b>${float(risk['approved_usdt']):.0f}</b>")
    if exe:
        lines.append(
            f"<b>Ausführung</b>  <code>{float(exe.get('amount', 0)):.4f}</code> @ "
            f"{_fmt_price(exe.get('price', 0))} · <b>${float(exe.get('usdt', 0)):.0f}</b>"
        )
        if exe.get("exchange_order_id"):
            lines.append(f"   Exchange-ID <code>{exe['exchange_order_id']}</code>")
        if exe.get("fee"):
            lines.append(f"   Fee <b>${float(exe['fee']):.4f}</b>")
    if order.get("pnl") is not None:
        lines.append(f"   PnL <b>${float(order['pnl']):+.2f}</b>")
    if order.get("error"):
        lines.append(f"<b>Fehler</b>  {order['error']}")
    trade_ts = _format_ts_short(ts.get("filled") or ts.get("created") or "")
    if trade_ts:
        lines.append(f"<b>{_trade_date_label(order.get('side'))}</b>  {trade_ts}")
    created = _format_ts_short(ts.get("created") or "")
    filled = _format_ts_short(ts.get("filled") or "")
    if created and filled and created != filled:
        lines.append(f"<b>Angelegt</b>  {created}")
    return "\n".join(lines)


def _order_usdt_display(order: dict) -> str:
    """Notional USDT only — PnL is appended separately via _order_pnl_part."""
    exe = order.get("execution", {})
    req = order.get("request", {})
    if exe.get("usdt"):
        return f"${float(exe['usdt']):.0f}"
    if req.get("usdt"):
        return f"${float(req['usdt']):.0f}"
    # Fallback: price × amount when execution notional missing
    price = float(exe.get("price") or req.get("price") or 0)
    amount = float(exe.get("amount") or req.get("amount") or 0)
    if price > 0 and amount > 0:
        return f"${price * amount:.0f}"
    return "—"