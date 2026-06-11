"""Central order ledger — scope-isolated demo / paper / live."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Optional

from core.config import get_bot_config
from core.models import RiskDecision, TradeOrder, TradeResult
from data_manager import (
    get_config,
    load_orders,
    resolve_ledger_scope,
    resolve_orders_file,
    save_orders,
)

ORDERS_PER_PAGE = 5
PENDING_TTL_MINUTES = 10

STATUS_ICONS = {
    "pending_confirmation": "⏳",
    "cancelled": "🚫",
    "expired": "⌛",
    "rejected": "❌",
    "executing": "🔄",
    "filled": "✅",
    "failed": "⚠️",
}


def ledger_label(scope: str = None) -> str:
    scope = scope or resolve_ledger_scope()
    labels = {"demo": "DEMO", "paper": "PAPER", "live": "GATE/LIVE"}
    return labels.get(scope, scope.upper())


def _now() -> str:
    return datetime.now().isoformat()


def _parse_ts(value: str):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", ""))
    except Exception:
        return None


class OrderService:
    def __init__(self, scope: str = None):
        self.scope = scope or resolve_ledger_scope()
        self._path = resolve_orders_file(self.scope)  # noqa: F841 — reserved for diagnostics

    def _load(self) -> dict:
        data = load_orders(self.scope)
        if data.get("ledger_scope") != self.scope:
            data["ledger_scope"] = self.scope
        return data

    def _save(self, data: dict) -> bool:
        data["ledger_scope"] = self.scope
        return save_orders(data, self.scope)

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

    def create_from_request(
        self,
        order: TradeOrder,
        *,
        timeframe: str = "4h",
        status: str = "pending_confirmation",
        request_extra: dict = None,
        risk: RiskDecision = None,
        telegram_token: str = None,
    ) -> dict:
        data = self._load()
        cfg = get_bot_config()
        record = {
            "id": telegram_token or uuid.uuid4().hex[:12],
            "display_seq": self._next_seq(data),
            "status": status,
            "side": order.type.lower(),
            "symbol": order.symbol,
            "timeframe": timeframe,
            "order_type": "market",
            "source": order.source or "auto",
            "signal": order.signal or "",
            "trading_mode": cfg.trading_mode,
            "ledger_scope": self.scope,
            "request": {
                "price": float(order.price or 0),
                "amount": float(order.amount or 0) or None,
                "usdt": float(order.usdt_amount or 0) or None,
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
        record = {
            "id": uuid.uuid4().hex[:12],
            "display_seq": self._next_seq(data),
            "status": "rejected",
            "side": order.type.lower(),
            "symbol": order.symbol,
            "timeframe": timeframe,
            "order_type": "market",
            "source": order.source or "auto",
            "signal": order.signal or "",
            "trading_mode": get_bot_config().trading_mode,
            "ledger_scope": self.scope,
            "request": {
                "price": float(order.price or 0),
                "amount": float(order.amount or 0) or None,
                "usdt": float(order.usdt_amount or 0) or None,
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
        record["status"] = status
        record["timestamps"]["updated"] = _now()
        if status == "filled":
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
        return record

    def get_by_id(self, order_id: str) -> Optional[dict]:
        return self._find(self._load(), order_id=order_id)

    def get_by_display_seq(self, display_seq: int) -> Optional[dict]:
        return self._find(self._load(), display_seq=display_seq)

    def expire_stale_pending(self) -> int:
        data = self._load()
        cutoff = datetime.now() - timedelta(minutes=PENDING_TTL_MINUTES)
        count = 0
        for o in data.get("orders", []):
            if o.get("status") != "pending_confirmation":
                continue
            ts = _parse_ts(o.get("timestamps", {}).get("created"))
            if ts and ts < cutoff:
                o["status"] = "expired"
                o["timestamps"]["updated"] = _now()
                count += 1
        if count:
            self._save(data)
        return count

    def list_orders(
        self,
        *,
        status_filter: set = None,
        hours: float = None,
        page: int = 1,
        per_page: int = ORDERS_PER_PAGE,
    ) -> tuple[list, int]:
        self.expire_stale_pending()
        data = self._load()
        orders = [o for o in data.get("orders", []) if o.get("ledger_scope") == self.scope]
        orders = list(reversed(orders))
        if status_filter:
            orders = [o for o in orders if o.get("status") in status_filter]
        if hours is not None:
            cutoff = datetime.now() - timedelta(hours=hours)
            orders = [
                o for o in orders
                if (_parse_ts(o.get("timestamps", {}).get("created")) or datetime.min) >= cutoff
            ]
        total = len(orders)
        start = (max(1, page) - 1) * per_page
        return orders[start:start + per_page], max(1, (total + per_page - 1) // per_page)

    def stats_24h(self) -> dict:
        self.expire_stale_pending()
        data = self._load()
        cutoff = datetime.now() - timedelta(hours=24)
        counts = {"filled": 0, "rejected": 0, "cancelled": 0, "pending_confirmation": 0, "failed": 0}
        for o in data.get("orders", []):
            if o.get("ledger_scope") != self.scope:
                continue
            ts = _parse_ts(o.get("timestamps", {}).get("created"))
            if not ts or ts < cutoff:
                continue
            st = o.get("status", "")
            if st in counts:
                counts[st] += 1
        return counts

    def link_execution_result(self, order_id: str, result: TradeResult, approved_order: TradeOrder = None) -> None:
        if not order_id:
            return
        if result.executed:
            self.update_status(
                order_id,
                "filled",
                execution={
                    "price": float(result.price or 0),
                    "amount": float(result.amount or 0),
                    "usdt": float(result.usdt_amount or 0),
                    "exchange_order_id": getattr(result, "exchange_order_id", None),
                },
                pnl=float(result.pnl) if result.pnl else None,
            )
        else:
            self.update_status(order_id, "failed", error=result.message or "Execution failed")

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


def format_order_line(order: dict) -> str:
    icon = STATUS_ICONS.get(order.get("status", ""), "·")
    sym = (order.get("symbol") or "").replace("/USDT", "")
    side = (order.get("side") or "?").upper()
    seq = order.get("display_seq", "?")
    src = order.get("source", "auto")
    usdt = _order_usdt_display(order)
    return f"{icon} <b>#{seq}</b> {order.get('status', '').upper()}  {side}  <b>{sym}</b>  {usdt}  <i>{src}</i>"


def format_order_detail(order: dict) -> str:
    sym = (order.get("symbol") or "").replace("/USDT", "")
    req = order.get("request", {})
    risk = order.get("risk", {})
    exe = order.get("execution", {})
    ts = order.get("timestamps", {})
    lines = [
        f"<b>Order #{order.get('display_seq')} — {order.get('status', '').upper()}</b>",
        f"{order.get('side', '').upper()} <b>{sym}</b> · {order.get('source', '')} · {ledger_label(order.get('ledger_scope'))}",
        "",
        f"<b>Anfrage</b>  Kurs ${float(req.get('price', 0)):.4f}",
    ]
    if req.get("usdt"):
        lines.append(f"   USDT <b>${float(req['usdt']):.0f}</b>")
    if req.get("amount"):
        lines.append(f"   Menge <code>{float(req['amount']):.4f}</code>")
    if req.get("pct"):
        lines.append(f"   Anteil <b>{float(req['pct']) * 100:.0f}%</b>")
    lines.append(f"<b>Risk</b>  {risk.get('message') or '—'}")
    if risk.get("approved_usdt"):
        lines.append(f"   Freigegeben <b>${float(risk['approved_usdt']):.0f}</b>")
    if exe:
        lines.append(
            f"<b>Ausführung</b>  <code>{float(exe.get('amount', 0)):.4f}</code> @ "
            f"${float(exe.get('price', 0)):.4f} · <b>${float(exe.get('usdt', 0)):.0f}</b>"
        )
        if exe.get("exchange_order_id"):
            lines.append(f"   Exchange-ID <code>{exe['exchange_order_id']}</code>")
    if order.get("pnl") is not None:
        lines.append(f"   PnL <b>${float(order['pnl']):+.2f}</b>")
    if order.get("error"):
        lines.append(f"<b>Fehler</b>  {order['error']}")
    created = (ts.get("created") or "")[:16].replace("T", " ")
    filled = (ts.get("filled") or "")[:16].replace("T", " ")
    lines.append(f"<b>Zeit</b>  erstellt {created}" + (f" · filled {filled}" if filled.strip() else ""))
    return "\n".join(lines)


def _order_usdt_display(order: dict) -> str:
    exe = order.get("execution", {})
    req = order.get("request", {})
    if exe.get("usdt"):
        return f"${float(exe['usdt']):.0f}"
    if req.get("usdt"):
        return f"${float(req['usdt']):.0f}"
    if order.get("side") == "sell" and order.get("pnl") is not None:
        return f"PnL ${float(order['pnl']):+.1f}"
    return "—"