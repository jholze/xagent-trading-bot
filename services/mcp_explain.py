"""Tenant-scoped MCP read packs: orders, memory, why (signals + facts).

Reads fail-open (empty lists + errors[]). Never returns embeddings.
Does not touch the bot price loop. Live I/O only when live=True.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any, Callable

DEFAULT_ORDER_HOURS = 168.0
WHY_ORDER_HOURS = 720.0
DEFAULT_LIMIT = 40
MAX_LIMIT = 200
MAX_HOURS = 24.0 * 90.0
DEFAULT_STATUSES = frozenset({"filled", "rejected", "failed", "cancelled"})

_ORDER_KEYS = (
    "id",
    "display_seq",
    "status",
    "side",
    "symbol",
    "timeframe",
    "source",
    "signal",
    "exit_source",
    "exit_rationale",
    "tenant_id",
    "error",
    "pnl",
    "timestamps",
)
_REQUEST_KEYS = ("price", "amount", "usdt")
_RISK_KEYS = ("approved", "message", "code", "size_multiplier", "approved_usdt")
_EXEC_KEYS = (
    "filled_price",
    "filled_amount",
    "filled_usdt",
    "avg_price",
    "price",
    "amount",
    "usdt",
)
_PROFILE_KEYS = (
    "symbol",
    "ledger_scope",
    "tenant_id",
    "as_of",
    "version",
    "trades_30d",
    "sells_30d",
    "buys_30d",
    "win_rate",
    "total_pnl_usdt",
    "avg_pnl_usdt",
    "dca_count_30d",
    "size_bias",
    "entry_bias",
    "risk_score",
    "rationale",
    "features",
)
_EVENT_KEYS = (
    "event_id",
    "timestamp",
    "event_type",
    "symbols",
    "impact_score",
    "description",
    "source",
    "url",
    "metadata",
    "tenant_id",
)
_TRADE_KEYS = (
    "trade_id",
    "symbol",
    "entry_time",
    "exit_time",
    "direction",
    "entry_price",
    "exit_price",
    "pnl_usdt",
    "pnl_pct",
    "source",
    "outcome",
    "reason",
    "related_event_ids",
    "ledger_scope",
    "tenant_id",
    "metadata",
)
_LESSON_KEYS = (
    "lesson_id",
    "text",
    "confidence",
    "tags",
    "symbols",
    "sample_n",
    "validated",
    "created_at",
    "source",
    "tenant_id",
)


def normalize_symbol(symbol: str | None) -> str:
    s = str(symbol or "").strip().upper().replace("-", "/")
    if not s:
        return ""
    if "/" not in s:
        s = f"{s}/USDT"
    return s


def _symbol_match(have: str | None, want: str | None) -> bool:
    b = normalize_symbol(want)
    if not b:
        return True
    a = normalize_symbol(have)
    if not a:
        return False
    return a == b or a.split("/")[0] == b.split("/")[0]


def _as_mapping(obj: Any) -> dict:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return dict(obj)
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    if hasattr(obj, "to_doc"):
        try:
            d = obj.to_doc()
            if isinstance(d, dict):
                d = dict(d)
                d.pop("_id", None)
                return d
        except Exception:
            pass
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in vars(obj).items() if not str(k).startswith("_")}
    return {}


def _pick(src: dict, keys: tuple[str, ...]) -> dict:
    out = {}
    for key in keys:
        if key in src and src[key] is not None:
            out[key] = src[key]
    return out


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    if depth > 6:
        return None
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    try:
        from decimal import Decimal

        if isinstance(value, Decimal):
            return float(value)
    except Exception:
        pass
    if isinstance(value, dict):
        return {str(k): _json_safe(v, depth=depth + 1) for k, v in value.items() if k != "embedding"}
    if isinstance(value, (list, tuple)):
        if value and all(isinstance(x, (int, float)) for x in value) and len(value) > 24:
            return []
        return [_json_safe(v, depth=depth + 1) for v in value]
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe(asdict(value), depth=depth + 1)
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)


def _clamp_limit(limit: Any) -> int:
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = DEFAULT_LIMIT
    if n < 1:
        return DEFAULT_LIMIT
    return min(n, MAX_LIMIT)


def _clamp_hours(hours: Any, default: float = DEFAULT_ORDER_HOURS) -> float:
    if hours is None:
        return default
    try:
        n = float(hours)
    except (TypeError, ValueError):
        return default
    if n < 1:
        return default
    return min(n, MAX_HOURS)


def _statuses(raw: Any) -> set[str]:
    if raw is None:
        return set(DEFAULT_STATUSES)
    if isinstance(raw, str):
        parts = [p.strip().lower() for p in raw.replace(";", ",").split(",") if p.strip()]
        return set(parts) or set(DEFAULT_STATUSES)
    if isinstance(raw, (list, tuple, set, frozenset)):
        parts = [str(p).strip().lower() for p in raw if str(p).strip()]
        return set(parts) or set(DEFAULT_STATUSES)
    return set(DEFAULT_STATUSES)


def sanitize_order(order: Any) -> dict:
    src = _as_mapping(order)
    out = _pick(src, _ORDER_KEYS)
    req = src.get("request") if isinstance(src.get("request"), dict) else {}
    out["request"] = _pick(req, _REQUEST_KEYS)
    risk = src.get("risk") if isinstance(src.get("risk"), dict) else {}
    out["risk"] = _pick(risk, _RISK_KEYS)
    exe = src.get("execution") if isinstance(src.get("execution"), dict) else {}
    out["execution"] = _pick(exe, _EXEC_KEYS)
    return _json_safe(out)


def sanitize_profile(profile: Any) -> dict | None:
    if profile is None:
        return None
    src = _as_mapping(profile)
    if not src:
        return None
    out = _pick(src, _PROFILE_KEYS)
    feats = out.get("features")
    if isinstance(feats, dict):
        out["features"] = {k: v for k, v in feats.items() if k != "embedding"}
    return _json_safe(out)


def sanitize_event(event: Any) -> dict:
    src = _as_mapping(event)
    return _json_safe(_pick(src, _EVENT_KEYS))


def sanitize_trade(trade: Any) -> dict:
    src = _as_mapping(trade)
    return _json_safe(_pick(src, _TRADE_KEYS))


def _same_tenant(obj: Any, tenant_id: str) -> bool:
    src = _as_mapping(obj)
    tid = str(src.get("tenant_id") or "").strip()
    if not tid:
        return True
    return tid == str(tenant_id or "").strip()


def sanitize_lesson(lesson: Any) -> dict:
    src = _as_mapping(lesson)
    out = _pick(src, _LESSON_KEYS)
    text = str(out.get("text") or "")
    if len(text) > 800:
        out["text"] = text[:799] + "…"
    return _json_safe(out)


def sanitize_facts(flags: Any) -> dict:
    if flags is None:
        return {}
    src = _as_mapping(flags)
    src.pop("embedding", None)
    return _json_safe(src)


def sanitize_rag_hit(hit: Any) -> dict:
    src = _as_mapping(hit)
    if hasattr(hit, "to_dict"):
        try:
            d = hit.to_dict()
            if isinstance(d, dict):
                src = d
        except Exception:
            pass
    meta = src.get("metadata") if isinstance(src.get("metadata"), dict) else {}
    meta = {k: v for k, v in meta.items() if k != "embedding"}
    return _json_safe(
        {
            "text": str(src.get("text") or "")[:1200],
            "score": src.get("score"),
            "metadata": meta,
            "chunk_id": src.get("chunk_id") or "",
        }
    )


def _err(errors: list[str], prefix: str, exc: BaseException) -> None:
    msg = f"{prefix}: {type(exc).__name__}"
    errors.append(msg)


def _live_list_orders(
    tenant_id: str,
    *,
    symbol: str | None,
    hours: float,
    limit: int,
    statuses: set[str],
) -> list:
    from core.tenant_context import tenant_context
    from services.order_service import ORDERS_LIST_HARD_CAP, OrderService

    per_page = ORDERS_LIST_HARD_CAP if symbol else min(limit, ORDERS_LIST_HARD_CAP)
    with tenant_context(tenant_id):
        svc = OrderService()
        orders, _pages = svc.list_orders(
            status_filter=set(statuses),
            hours=hours,
            page=1,
            per_page=per_page,
        )
    return list(orders or [])


def list_orders_public(
    tenant_id: str,
    *,
    symbol: str | None = None,
    hours: float | None = DEFAULT_ORDER_HOURS,
    limit: int = DEFAULT_LIMIT,
    statuses: Any = None,
    list_fn: Callable[..., list] | None = None,
    live: bool = False,
) -> dict:
    errors: list[str] = []
    cap = _clamp_limit(limit)
    window = _clamp_hours(hours)
    want_status = _statuses(statuses)
    raw: list = []
    try:
        if list_fn is not None:
            raw = list(
                list_fn(
                    tenant_id=tenant_id,
                    symbol=symbol,
                    hours=window,
                    limit=cap,
                    statuses=want_status,
                )
                or []
            )
        elif live:
            raw = _live_list_orders(
                tenant_id,
                symbol=symbol,
                hours=window,
                limit=cap,
                statuses=want_status,
            )
    except Exception as exc:
        _err(errors, "orders", exc)
        raw = []

    orders = []
    for item in raw:
        if not isinstance(item, dict) and not hasattr(item, "__dict__"):
            continue
        mapped = item if isinstance(item, dict) else _as_mapping(item)
        if not _symbol_match(mapped.get("symbol"), symbol):
            continue
        orders.append(sanitize_order(mapped))
        if len(orders) >= cap:
            break
    return {
        "ok": True,
        "tenant_id": tenant_id,
        "symbol": normalize_symbol(symbol) or None,
        "hours": window,
        "orders": orders,
        "n": len(orders),
        "errors": errors,
    }


def _live_facts(symbol: str, *, store: Any, config_raw: dict | None) -> dict:
    from intelligence.memory.coin_facts import summarize_facts_for_symbol

    flags = summarize_facts_for_symbol(symbol, store=store, config_raw=config_raw)
    return sanitize_facts(flags)


def _live_rag(symbol: str, *, query: str | None, top_k: int = 5) -> list[dict]:
    from hermes.memory.rag_retriever import RagRetriever

    q = (query or "").strip() or f"{symbol} why buy signals memory lesson"
    retriever = RagRetriever()
    hits = retriever.retrieve(q, top_k=top_k, filters={"symbol": symbol} if symbol else None)
    return [sanitize_rag_hit(h) for h in (hits or [])]


def _call_section(errors: list[str], prefix: str, fn: Callable, default):
    try:
        return fn()
    except Exception as exc:
        _err(errors, prefix, exc)
        return default


def memory_pack(
    tenant_id: str,
    symbol: str | None = None,
    *,
    query: str | None = None,
    store: Any = None,
    facts_fn: Callable[..., dict] | None = None,
    rag_fn: Callable[..., list] | None = None,
    config_raw: dict | None = None,
    event_limit: int = 20,
    trade_limit: int = 20,
    lesson_limit: int = 10,
    rag_k: int = 5,
    live: bool = False,
) -> dict:
    errors: list[str] = []
    sym = normalize_symbol(symbol) or None
    if store is None and live:
        try:
            from intelligence.memory.store import MemoryStore

            store = MemoryStore()
        except Exception as exc:
            _err(errors, "store", exc)
            store = None

    profile = None
    profiles: list[dict] = []
    events: list[dict] = []
    trades: list[dict] = []
    lessons: list[dict] = []
    facts: dict = {}
    rag: list[dict] = []

    if store is not None and sym:
        profile = _call_section(
            errors,
            "profile",
            lambda: sanitize_profile(store.get_profile(sym, tenant_id=tenant_id)),
            None,
        )
        events = _call_section(
            errors,
            "events",
            lambda: [sanitize_event(e) for e in (store.list_events(symbol=sym, limit=event_limit) or [])],
            [],
        )
        trades = _call_section(
            errors,
            "trades",
            lambda: [
                sanitize_trade(t)
                for t in (store.list_trades(symbol=sym, tenant_id=tenant_id, limit=trade_limit) or [])
            ],
            [],
        )
        lessons = _call_section(
            errors,
            "lessons",
            lambda: [
                sanitize_lesson(L)
                for L in (store.list_lessons(symbol=sym, limit=lesson_limit) or [])
                if _same_tenant(L, tenant_id)
            ],
            [],
        )
    elif store is not None:
        profiles = _call_section(
            errors,
            "profiles",
            lambda: [
                sanitize_profile(p)
                for p in (store.list_profiles(tenant_id=tenant_id, limit=50) or [])
                if sanitize_profile(p)
            ],
            [],
        )
        events = _call_section(
            errors,
            "events",
            lambda: [sanitize_event(e) for e in (store.list_events(limit=event_limit) or [])],
            [],
        )

    if facts_fn is not None:
        facts = _call_section(errors, "facts", lambda: sanitize_facts(facts_fn(symbol=sym, tenant_id=tenant_id)), {})
    elif live and sym:
        facts = _call_section(
            errors,
            "facts",
            lambda: _live_facts(sym, store=store, config_raw=config_raw),
            {},
        )

    if rag_fn is not None:
        rag = _call_section(
            errors,
            "rag",
            lambda: [sanitize_rag_hit(h) for h in (rag_fn(tenant_id=tenant_id, symbol=sym, query=query) or [])],
            [],
        )
    elif live and (sym or query):
        rag = _call_section(errors, "rag", lambda: _live_rag(sym or "", query=query, top_k=rag_k), [])

    return {
        "ok": True,
        "tenant_id": tenant_id,
        "symbol": sym,
        "profile": profile,
        "profiles": profiles,
        "facts": facts or {},
        "events": events,
        "trades": trades,
        "lessons": lessons,
        "rag": rag,
        "errors": errors,
    }


def _pick_lot(lots: list | None, symbol: str | None) -> dict | None:
    want = normalize_symbol(symbol)
    if not want:
        return None
    for lot in lots or []:
        if not isinstance(lot, dict):
            continue
        if _symbol_match(lot.get("symbol"), want):
            return _json_safe(lot)
    return None


def why_pack(
    tenant_id: str,
    symbol: str | None,
    *,
    query: str | None = None,
    store: Any = None,
    facts_fn: Callable[..., dict] | None = None,
    rag_fn: Callable[..., list] | None = None,
    list_fn: Callable[..., list] | None = None,
    snapshot_fn: Callable[..., dict] | None = None,
    config_raw: dict | None = None,
    live: bool = False,
) -> dict:
    sym = normalize_symbol(symbol)
    if not sym:
        return {"ok": False, "error": "missing_symbol"}

    errors: list[str] = []
    snap: dict = {}
    if snapshot_fn is not None:
        snap = _call_section(
            errors,
            "snapshot",
            lambda: snapshot_fn(tenant_id=tenant_id, symbol=sym) or {},
            {},
        )
    elif live:
        def _snap():
            from services.desk.snapshot import build_snapshot

            return build_snapshot(tenant_id=tenant_id, symbol=sym) or {}

        snap = _call_section(errors, "snapshot", _snap, {})
    if not isinstance(snap, dict):
        snap = {}
    if snap.get("ok") is False and snap.get("error"):
        errors.append(f"snapshot: {snap.get('error')}")

    lot = _pick_lot(snap.get("lots") if isinstance(snap.get("lots"), list) else [], sym)
    hud = snap.get("hud") if isinstance(snap.get("hud"), dict) else None
    badges = snap.get("badges") if isinstance(snap.get("badges"), dict) else None
    next_edge = snap.get("next_edge")
    conflict = snap.get("conflict")

    orders_out = list_orders_public(
        tenant_id,
        symbol=sym,
        hours=WHY_ORDER_HOURS,
        limit=DEFAULT_LIMIT,
        list_fn=list_fn,
        live=live,
    )
    if orders_out.get("errors"):
        errors.extend(orders_out["errors"])

    mem = memory_pack(
        tenant_id,
        sym,
        query=query,
        store=store,
        facts_fn=facts_fn,
        rag_fn=rag_fn,
        config_raw=config_raw,
        live=live,
    )
    if mem.get("errors"):
        errors.extend(mem["errors"])

    return {
        "ok": True,
        "tenant_id": tenant_id,
        "symbol": sym,
        "lot": lot,
        "hud": hud,
        "badges": badges,
        "next_edge": next_edge,
        "conflict": conflict,
        "orders": orders_out.get("orders") or [],
        "profile": mem.get("profile"),
        "facts": mem.get("facts") or {},
        "events": mem.get("events") or [],
        "trades": mem.get("trades") or [],
        "lessons": mem.get("lessons") or [],
        "rag": mem.get("rag") or [],
        "errors": errors,
    }
