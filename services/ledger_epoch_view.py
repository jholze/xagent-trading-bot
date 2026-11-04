"""Read-only view of the current ledger epoch (``scripts/ledger_epoch.py``).

Same source order as the MCP ``xagent_nav`` fix (PR #768):

1. ``ledger_epoch`` marker in the tenant's ``trade_history`` document
   (written by ``ledger_epoch.py reset``/``restore``),
2. else the newest ``ledger_epochs`` doc of the tenant.

The newest ``started_at`` of both wins. Nothing here writes. Reports use this
to start their windows (plan start, BTC HODL baseline, KPI windows) at the
current epoch while keeping older rows available as ``pre`` data.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Iterable

EPOCH_COLLECTION = "ledger_epochs"
_FIELDS = ("epoch_id", "started_at", "initial_capital", "kind")


def parse_ts(raw: Any) -> datetime | None:
    """ISO timestamp → aware UTC datetime. Naive values are read as UTC."""
    if raw in (None, ""):
        return None
    if isinstance(raw, datetime):
        ts = raw
    else:
        try:
            ts = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _pick(doc: Any) -> dict:
    if not isinstance(doc, dict):
        return {}
    out = {k: doc.get(k) for k in _FIELDS if doc.get(k) is not None}
    return out if parse_ts(out.get("started_at")) else {}


def pick_current_epoch(trade_history_doc: dict | None, epoch_docs: Iterable[dict] | None) -> dict:
    """Newest valid epoch marker from the trade_history doc and ``ledger_epochs`` docs."""
    cands = [_pick((trade_history_doc or {}).get("ledger_epoch"))]
    cands += [_pick(d) for d in (epoch_docs or [])]
    cands = [c for c in cands if c]
    if not cands:
        return {}
    return max(cands, key=lambda c: parse_ts(c["started_at"]))


def load_current_epoch(tenant_id: str | None = None, scope: str | None = None) -> dict:
    """Current epoch of the active (or given) tenant. ``{}`` when none or unreadable."""
    try:
        from core.tenant_context import resolve_tenant_id
        from data_manager import resolve_ledger_scope

        tenant_id = str(resolve_tenant_id(tenant_id) or "default")
        scope = scope or str(resolve_ledger_scope() or "demo")
    except Exception:
        tenant_id, scope = tenant_id or "default", scope or "demo"
    th: dict = {}
    try:
        from data_manager import load_trade_history_document

        th = load_trade_history_document(scope, tenant_id=tenant_id) or {}
    except Exception:
        th = {}
    docs: list[dict] = []
    try:
        from data_manager import resolve_ledger_backend

        if resolve_ledger_backend() == "mongo":
            from storage.mongo_client import get_database

            docs = list(
                get_database()[EPOCH_COLLECTION].find(
                    {"tenant_id": tenant_id}, {"_id": 0, **{k: 1 for k in _FIELDS}}
                )
            )
    except Exception:
        docs = []
    return pick_current_epoch(th, docs)


def epoch_start(epoch: dict | None) -> datetime | None:
    return parse_ts((epoch or {}).get("started_at"))


def epoch_start_date(epoch: dict | None, tz=None) -> date | None:
    """Epoch start as a calendar day in the display time zone (Berlin)."""
    start = epoch_start(epoch)
    if start is None:
        return None
    if tz is None:
        try:
            from core.time_utils import display_tz

            tz = display_tz()
        except Exception:
            tz = timezone.utc
    return start.astimezone(tz).date()


def point_in_epoch(point: dict, start: datetime | None) -> bool:
    """A daily NAV point belongs to the epoch when last written at/after the start.

    The bot overwrites today's point every cycle, so the reset day's point holds
    post-reset NAV once ``as_of`` is past the start. Without ``as_of`` fall back
    to the day (strictly after the start day). Same rule as PR #768.
    """
    if start is None:
        return True
    ts = parse_ts(point.get("as_of"))
    if ts is not None:
        return ts >= start
    try:
        day = date.fromisoformat(str(point.get("date") or "")[:10])
    except ValueError:
        return False
    return day > start.date()


def split_points(points: Iterable[dict] | None, start: datetime | None) -> tuple[list[dict], list[dict]]:
    """``(current_epoch_points, pre_epoch_points)``, input order kept."""
    cur: list[dict] = []
    pre: list[dict] = []
    for p in points or []:
        if isinstance(p, dict):
            (cur if point_in_epoch(p, start) else pre).append(p)
    return cur, pre


def trade_in_epoch(trade: dict, start: datetime | None, tz=None) -> bool:
    """Closed-trade records carry naive display-time (Berlin) timestamps."""
    if start is None:
        return True
    raw = trade.get("timestamp")
    if raw in (None, ""):
        return False
    try:
        ts = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    if ts.tzinfo is None:
        if tz is None:
            try:
                from core.time_utils import display_tz

                tz = display_tz()
            except Exception:
                tz = timezone.utc
        ts = ts.replace(tzinfo=tz)
    return ts >= start
