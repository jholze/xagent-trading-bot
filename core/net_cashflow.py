"""Signed venue cash flow and the #659 replay-row predicate.

Pure functions. No exchange I/O, no ledger writes, and no second book.
``replay_simulated_ledger`` remains the only spot replay and still applies
the #655 cost basis. This module only decides which spot rows count, and
computes a separate venue net for a later portfolio hook.

Net formula, when every required leg is actual USDT:
``price_pnl - fees + signed_funding + signed_adl``.
A missing, non-USDT, or unknown fee or settlement does not become zero.

The reconciliation tolerance is the frozen paper/testnet start value
0.01 USDT (Kay, 2026-11-10, freeze item 4). It is not read from config
and it is not widened. Quantity precision is a different question and is
not approved, so a quantity comparison cannot pass.

Not wired. Package F still owns ``services/portfolio_service.py``,
``core/models.py``, and ``strategies/positions.py``.

Portfolio hook, later, flag off leaves today's cover path unchanged:
- Call site: ``PortfolioService.execute_cover``, after the price line
  ``pnl = unrealized_pnl("short", qty, entry, price)`` and beside the
  paper block ``_cover_funding_usdt`` (about lines 581–598). Do not edit
  ``is_executed_status``.
- When ``accounting.net_cashflow_v220.enabled`` is not boolean true, keep
  the existing paper funding fields and the #373 warning. Do not write
  this result into ``pnl``.
- When the flag is true and the mode is sandbox, call ``account`` and
  store the dict on the trade row as ``venue_cashflow``. ``actual_net``
  None stays None. Do not coerce it to 0. Do not copy the cover path's
  ``reported_fee = 0.0`` default into a fee leg: a missing fee is
  ``state="unknown"``. Paper ``funding_usdt`` stays labeled simulation
  and is not a ``venue_cashflow`` funding leg.
- Quantity is the delta ``execute_cover`` already computed from booked
  rows. Do not infer a cumulative fill a second time (#676 owns that).
- Window is ``entry_at`` through the cover time, timezone-aware, with
  the inclusive flags set by the caller. Do not extend the hold and do
  not fetch funding (#68).
- Replay hook: the cover branch in ``replay_simulated_ledger``
  (``pnl = cover_amt * (entry - price)``) stays price-only until that
  same flag-gated call is added. Short and cover rows still require
  status ``filled`` (#659).
- Daily-loss hook: ``OrderService._stats_filled_window`` and
  ``stats_from_filled_orders`` should sum sell pnl for rows
  ``row_counts_for_replay`` accepts. ``query_day(filled_only=True)``
  still uses ``is_executed_status`` and will miss remainder-canceled
  sells until that query grows the same predicate.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

FROZEN_RECONCILIATION_TOLERANCE_USDT = Decimal("0.01")
MONEY_QUANTUM_USDT = Decimal("0.00000001")
PAPER_LABEL = "simulation"
FLAG_KEY = "accounting.net_cashflow_v220.enabled"

_PARTIAL_STATUSES = frozenset({"partially_filled", "canceled", "cancelled"})
_SHORT_SIDES = frozenset({"short", "cover"})
_SPOT_SIDES = frozenset({"buy", "sell"})
_SANDBOX_TRADING_MODES = frozenset({
    "paper",
    "demo",
    "gate_testnet",
    "futures_testnet",
})
_LEGACY_LIVE_MODES = frozenset({"live", "mainnet", "real"})
_SANDBOX_LIVE_EXECUTION = frozenset({"shadow", "testnet"})
_INCLUDES = frozenset({"price", "fees", "funding", "adl"})
_METRICS = frozenset({"actual_net", "price_pnl", "fees", "funding", "adl"})


def _text(value: object) -> str:
    return str(value or "").strip()


def _norm_status(order: dict) -> str:
    return _text(order.get("status")).lower()


def _norm_side(order: dict) -> str:
    return _text(order.get("side")).lower()


def _execution(order: dict) -> dict:
    raw = order.get("execution")
    return raw if isinstance(raw, dict) else {}


def _execution_amount(order: dict) -> float | None:
    raw = _execution(order).get("amount")
    if raw is None or raw == "":
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(val):
        return None
    return val


def row_counts_for_replay(order: object) -> bool:
    """#659 predicate. Short and cover still count only when filled."""
    if not isinstance(order, dict):
        return False
    side = _norm_side(order)
    status = _norm_status(order)
    if side in _SHORT_SIDES or side not in _SPOT_SIDES:
        return status == "filled"
    if status == "filled":
        return True
    if status in _PARTIAL_STATUSES:
        amount = _execution_amount(order)
        return amount is not None and amount > 0
    return False


def execution_only_view(order: dict) -> dict:
    """Partial and canceled spot rows must not fall back to the request size."""
    if (
        _norm_side(order) in _SPOT_SIDES
        and _norm_status(order) in _PARTIAL_STATUSES
    ):
        cloned = dict(order)
        cloned["request"] = {}
        cloned["execution"] = dict(_execution(order))
        return cloned
    return order


def _replay_time(order: dict) -> str:
    raw = order.get("timestamps")
    ts = raw if isinstance(raw, dict) else {}
    return _text(ts.get("filled") or ts.get("created"))


def _replay_identity(order: dict) -> str | None:
    side = _norm_side(order)
    exchange_id = _text(order.get("exchange_order_id"))
    if exchange_id:
        return f"{side}|ex:{exchange_id}"
    for key in ("id", "order_id", "client_order_id"):
        token = _text(order.get(key))
        if token:
            return f"{side}|{key}:{token}"
    return None


def _replaces(new_idx: int, new: dict, old_idx: int, old: dict) -> bool:
    new_time = _replay_time(new)
    old_time = _replay_time(old)
    if new_time != old_time:
        return new_time > old_time
    new_qty = _execution_amount(new) or 0.0
    old_qty = _execution_amount(old) or 0.0
    if new_qty != old_qty:
        return new_qty > old_qty
    return new_idx > old_idx


def select_replay_rows(orders: list | None) -> list:
    """One current cumulative row per order id, then chronological order."""
    chosen: dict[str, tuple[int, dict]] = {}
    anonymous: list[tuple[int, dict]] = []
    for idx, order in enumerate(orders or []):
        if not row_counts_for_replay(order):
            continue
        identity = _replay_identity(order)
        if identity is None:
            anonymous.append((idx, order))
            continue
        prev = chosen.get(identity)
        if prev is None or _replaces(idx, order, prev[0], prev[1]):
            chosen[identity] = (idx, order)
    rows = list(chosen.values()) + anonymous
    rows.sort(key=lambda pair: (_replay_time(pair[1]), pair[0]))
    return [order for _, order in rows]


def _money(value: object) -> Decimal | None:
    if isinstance(value, bool) or isinstance(value, float) or value is None:
        return None
    if isinstance(value, Decimal):
        dec = value
    elif isinstance(value, int):
        dec = Decimal(value)
    elif isinstance(value, str):
        try:
            dec = Decimal(value.strip())
        except (InvalidOperation, ValueError):
            return None
    else:
        return None
    if not dec.is_finite():
        return None
    return dec


def _fmt(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _aware(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def settlement_in_window(settlement_time: object, window: object) -> str:
    """Attribute on the settlement instant. Publication time is not read."""
    if not isinstance(window, dict):
        return "window_unspecified"
    if not isinstance(window.get("inclusive_start"), bool):
        return "window_unspecified"
    if not isinstance(window.get("inclusive_end"), bool):
        return "window_unspecified"
    start = _aware(window.get("start"))
    end = _aware(window.get("end"))
    instant = _aware(settlement_time)
    if start is None or end is None or instant is None or end < start:
        return "naive_or_bad_timestamp"
    if instant < start or (instant == start and not window["inclusive_start"]):
        return "outside"
    if instant > end or (instant == end and not window["inclusive_end"]):
        return "outside"
    return "inside"


def _enabled(config: dict | None) -> bool:
    if not isinstance(config, dict):
        return False
    block = config.get("accounting")
    if not isinstance(block, dict):
        return False
    nested = block.get("net_cashflow_v220")
    return isinstance(nested, dict) and nested.get("enabled") is True


def _mode_text(config: dict, *path: str) -> str | None:
    cursor: object = config
    for key in path:
        if not isinstance(cursor, dict) or key not in cursor:
            return None
        cursor = cursor[key]
    if not isinstance(cursor, str):
        return None
    stripped = cursor.strip().lower()
    return stripped or None


def path_reason(config: dict | None) -> str:
    """Sandbox only. A missing flag is off. Live is denied."""
    if not _enabled(config):
        return "feature_off"
    assert isinstance(config, dict)
    shorts = config.get("shorts")
    if isinstance(shorts, dict) and shorts.get("allow_live") is True:
        return "live_denied"
    trading_mode = _mode_text(config, "trading_mode")
    live_execution = _mode_text(config, "live", "execution")
    legacy = _mode_text(config, "execution", "mode")
    if trading_mode == "live" or live_execution == "real" or legacy in _LEGACY_LIVE_MODES:
        return "live_denied"
    if live_execution is not None and live_execution not in _SANDBOX_LIVE_EXECUTION:
        return "live_denied"
    if trading_mode not in _SANDBOX_TRADING_MODES:
        return "mode_not_sandbox"
    return "open"


def _closed(status: str) -> dict:
    return {
        "status": status,
        "actual_net": None,
        "price_pnl": None,
        "fees": None,
        "funding": None,
        "adl": None,
        "paper_funding": None,
        "paper_label": None,
        "qty": None,
        "reasons": [status],
        "booked_ids": [],
        "unattributed_ids": [],
        "venue_figures": [],
        "live_pnl": False,
        "venue_calls": 0,
        "reconcile": None,
    }


class _Ids:
    def __init__(self) -> None:
        self.reasons: list[str] = []
        self.ids: list[str] = []
        self.block = False
        self._seen: dict[str, Decimal | None] = {}

    def add(self, kind: str, record_id: object, amount: Decimal | None) -> bool:
        token = _text(record_id)
        if not token:
            self.block = True
            self.reasons.append("missing_record_id")
            return False
        key = f"{kind}:{token}"
        if key in self._seen:
            if self._seen[key] != amount:
                self.block = True
                self.reasons.append("record_id_conflict")
            return False
        self._seen[key] = amount
        self.ids.append(key)
        return True


def _fill_rank(idx: int, fill: dict) -> tuple:
    stamp = _aware(fill.get("as_of"))
    qty = _money(fill.get("filled_qty")) or Decimal(0)
    stamp_key = stamp.isoformat() if stamp is not None else ""
    return (stamp_key, qty, idx)


def replayed_fill_qty(fills: list) -> tuple[Decimal | None, list[str]]:
    """Incremental trade ids sum. Cumulative snapshots keep the latest qty."""
    if not isinstance(fills, list):
        return None, ["malformed_fill"]
    groups: dict[str, list[tuple[int, dict]]] = {}
    for idx, fill in enumerate(fills):
        if not isinstance(fill, dict):
            return None, ["malformed_fill"]
        order_id = _text(fill.get("order_id"))
        if not order_id:
            return None, ["missing_order_id"]
        groups.setdefault(order_id, []).append((idx, fill))
    total = Decimal(0)
    for rows in groups.values():
        semantics = {row[1].get("semantics") for row in rows}
        if semantics != {"incremental"} and semantics != {"cumulative"}:
            if any(item not in {"incremental", "cumulative"} for item in semantics):
                return None, ["missing_quantity_semantics"]
            return None, ["mixed_quantity_semantics"]
        semantic = next(iter(semantics))
        chosen: dict[str, tuple[int, dict, Decimal]] = {}
        for idx, fill in rows:
            record_id = _text(fill.get("record_id"))
            if not record_id:
                return None, ["missing_record_id"]
            qty = _money(fill.get("filled_qty"))
            if qty is None or qty < 0:
                return None, ["missing_filled_qty"]
            prev = chosen.get(record_id)
            if prev is not None and semantic == "incremental" and prev[2] != qty:
                return None, ["record_id_conflict"]
            if prev is None or _fill_rank(idx, fill) >= _fill_rank(prev[0], prev[1]):
                chosen[record_id] = (idx, fill, qty)
        if semantic == "incremental":
            for item in chosen.values():
                total += item[2]
            continue
        latest = max(chosen.values(), key=lambda item: _fill_rank(item[0], item[1]))
        total += latest[2]
    return total, []


def reconcile_quantity(*, tolerance: object = None, **_ignored: object) -> dict:
    """Quantity precision is not the money tolerance and is not approved."""
    if tolerance is None:
        status = "blocked_missing_quantity_precision"
    else:
        status = "blocked_unapproved_quantity_precision"
    return {
        "status": status,
        "pass": False,
        "uses_money_tolerance": False,
        "frozen_money_t": _fmt(FROZEN_RECONCILIATION_TOLERANCE_USDT),
    }


def _includes(value: object) -> set[str] | None:
    if not isinstance(value, (list, tuple, set)) or not value:
        return None
    found: set[str] = set()
    for item in value:
        token = _text(item)
        if token not in _INCLUDES:
            return None
        found.add(token)
    return found or None


def reconcile_money(
    *,
    ledger: object,
    venue: object,
    tolerance: object,
    metric: object,
    horizon: object,
    ledger_includes: object,
    venue_includes: object,
) -> dict:
    """Like-for-like money compare. Exact T passes. T plus one quantum fails."""
    frozen = _fmt(FROZEN_RECONCILIATION_TOLERANCE_USDT)
    blocked = {
        "pass": False,
        "frozen_t": frozen,
        "abs_diff": None,
    }
    if tolerance is None:
        return {"status": "blocked_missing_tolerance", **blocked}
    tol = _money(tolerance)
    if tol is None or tol != FROZEN_RECONCILIATION_TOLERANCE_USDT:
        return {"status": "blocked_unapproved_tolerance", **blocked}
    if not isinstance(metric, str) or metric not in _METRICS:
        return {"status": "blocked_mapping", **blocked}
    if not isinstance(horizon, str) or not horizon.strip():
        return {"status": "blocked_mapping", **blocked}
    left = _includes(ledger_includes)
    right = _includes(venue_includes)
    if left is None or right is None or left != right:
        return {"status": "blocked_mapping", **blocked}
    ledger_amount = _money(ledger)
    venue_amount = _money(venue)
    if ledger_amount is None or venue_amount is None:
        return {"status": "blocked_unknown", **blocked}
    diff = abs(ledger_amount - venue_amount)
    passed = diff <= FROZEN_RECONCILIATION_TOLERANCE_USDT
    return {
        "status": "pass" if passed else "fail",
        "pass": passed,
        "frozen_t": frozen,
        "abs_diff": _fmt(diff),
        "metric": metric,
        "horizon": horizon.strip(),
    }


def _quote_usdt(record: dict) -> bool:
    return _text(record.get("quote")).upper() == "USDT"


def _fee_sum(records: object, book: _Ids) -> Decimal | None:
    if not isinstance(records, list) or not records:
        book.block = True
        book.reasons.append("fee_unknown")
        return None
    total = Decimal(0)
    for record in records:
        if not isinstance(record, dict):
            book.block = True
            book.reasons.append("fee_unknown")
            continue
        amount = _money(record.get("amount")) if "amount" in record else None
        actual = (
            record.get("state") == "actual"
            and _quote_usdt(record)
            and amount is not None
            and amount >= 0
        )
        if not actual:
            book.block = True
            if not _quote_usdt(record) and _text(record.get("quote")):
                book.reasons.append("non_usdt")
            else:
                book.reasons.append("fee_unknown")
        if book.add("fee", record.get("record_id"), amount if actual else None) and actual:
            total += amount
    if book.block:
        return None
    return total


def _funding_parts(
    records: object,
    window: object,
    book: _Ids,
) -> tuple[Decimal | None, Decimal | None]:
    """Return (actual funding, paper funding). Paper is never actual."""
    if not isinstance(records, list):
        book.block = True
        book.reasons.append("no_settlement_observed")
        return None, None
    actual = Decimal(0)
    paper = Decimal(0)
    paper_n = 0
    actual_n = 0
    saw_indicative = False
    saw_absent = False
    saw_outside = False
    saw_bad = False
    for record in records:
        if not isinstance(record, dict):
            book.block = True
            book.reasons.append("funding_provenance_unknown")
            saw_bad = True
            continue
        provenance = record.get("provenance")
        amount = _money(record.get("signed_amount")) if "signed_amount" in record else None
        if provenance == "paper_simulation":
            if amount is None or not _quote_usdt(record):
                book.block = True
                book.reasons.append("funding_unknown")
                saw_bad = True
                continue
            if book.add("paper_funding", record.get("record_id"), amount):
                paper += amount
                paper_n += 1
            continue
        if provenance == "indicative":
            saw_indicative = True
            book.add("indicative", record.get("record_id"), None)
            continue
        if provenance in {"absent", "no_settlement_observed"}:
            saw_absent = True
            book.add("absent", record.get("record_id"), None)
            continue
        if provenance != "venue_cashflow":
            book.block = True
            book.reasons.append("funding_provenance_unknown")
            saw_bad = True
            continue
        verdict = settlement_in_window(record.get("settlement_time"), window)
        usable = (
            verdict == "inside"
            and _quote_usdt(record)
            and amount is not None
        )
        if verdict in {"window_unspecified", "naive_or_bad_timestamp"}:
            book.block = True
            book.reasons.append(verdict)
            saw_bad = True
        elif not _quote_usdt(record) or amount is None:
            book.block = True
            book.reasons.append("non_usdt" if not _quote_usdt(record) and _text(record.get("quote")) else "funding_unknown")
            saw_bad = True
        elif verdict == "outside":
            saw_outside = True
            token = _text(record.get("record_id"))
            if token:
                book.ids.append(f"unattributed:{token}")
        if usable and book.add("funding", record.get("record_id"), amount):
            actual += amount
            actual_n += 1
    paper_total = paper if paper_n else None
    if actual_n == 0:
        book.block = True
        if saw_bad:
            pass
        elif saw_outside:
            book.reasons.append("outside_window")
        elif saw_indicative and not saw_absent:
            book.reasons.append("indicative_not_settlement")
        else:
            book.reasons.append("no_settlement_observed")
        return None, paper_total
    if book.block:
        return None, paper_total
    return actual, paper_total


def _adl_sum(records: object, book: _Ids) -> tuple[Decimal, list[dict]]:
    extra = Decimal(0)
    figures: list[dict] = []
    if records is None:
        return extra, figures
    if not isinstance(records, list):
        book.block = True
        book.reasons.append("funding_unknown")
        return extra, figures
    for record in records:
        if not isinstance(record, dict):
            book.block = True
            book.reasons.append("funding_unknown")
            continue
        amount = _money(record.get("signed_amount")) if "signed_amount" in record else None
        includes = record.get("includes")
        if includes not in (None, []):
            parsed = _includes(includes)
            if parsed is None:
                book.block = True
                book.reasons.append("funding_unknown")
                continue
            if parsed & {"price", "fees", "funding"}:
                if amount is not None and book.add("venue_figure", record.get("record_id"), amount):
                    figures.append({
                        "record_id": _text(record.get("record_id")),
                        "signed_amount": _fmt(amount),
                        "includes": sorted(parsed),
                    })
                elif amount is None:
                    book.block = True
                    book.reasons.append("funding_unknown")
                continue
        if amount is None or not _quote_usdt(record):
            book.block = True
            book.reasons.append("non_usdt" if not _quote_usdt(record) and _text(record.get("quote")) else "funding_unknown")
            continue
        if book.add("adl", record.get("record_id"), amount):
            extra += amount
    return extra, figures


def _resolve_qty(payload: dict, book: _Ids) -> Decimal | None:
    has_fills = "fills" in payload and payload.get("fills") is not None
    given = _money(payload.get("qty")) if "qty" in payload else None
    if has_fills:
        filled, reasons = replayed_fill_qty(payload.get("fills"))
        book.reasons.extend(reasons)
        if reasons or filled is None or filled <= 0:
            book.block = True
            if not reasons and filled is not None and filled <= 0:
                book.reasons.append("zero_qty")
            return None
        if "qty" in payload and (given is None or given != filled):
            book.block = True
            book.reasons.append("qty_conflict")
        return filled
    if given is None or given <= 0:
        book.block = True
        book.reasons.append("missing_qty" if given is None else "zero_qty")
        return None
    return given


def _price(side: object, qty: Decimal | None, entry: object, exit_px: object) -> Decimal | None:
    if qty is None:
        return None
    left = _money(entry)
    right = _money(exit_px)
    if left is None or right is None or left <= 0 or right <= 0 or qty <= 0:
        return None
    if side == "short":
        return qty * (left - right)
    if side == "long":
        return qty * (right - left)
    return None


def _uniq(items: list[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


def account(config: dict | None, payload: dict | None = None) -> dict:
    """Flagged entry. Missing flag returns no actual cash flow and no zero."""
    reason = path_reason(config)
    if reason != "open":
        return _closed(reason)
    if not isinstance(payload, dict):
        closed = _closed("unknown")
        closed["reasons"] = ["malformed_payload"]
        return closed
    book = _Ids()
    qty = _resolve_qty(payload, book)
    side = payload.get("side")
    price = _price(side, qty, payload.get("entry"), payload.get("exit"))
    if side not in {"short", "long"}:
        book.block = True
        book.reasons.append("unknown_side")
    elif price is None:
        book.block = True
        book.reasons.append("bad_price")
    fees = _fee_sum(payload.get("fees"), book)
    funding, paper = _funding_parts(payload.get("funding"), payload.get("window"), book)
    adl, figures = _adl_sum(payload.get("adl"), book)
    reasons = _uniq(book.reasons)
    actual: Decimal | None = None
    if not book.block and price is not None and fees is not None and funding is not None:
        actual = price - fees + funding + adl
        status = "accounted"
    else:
        status = "unknown"
        fees = None if book.block else fees
        funding = None if book.block else funding
    reconcile = None
    if isinstance(payload.get("reconcile"), dict):
        block = payload["reconcile"]
        metric = block.get("metric")
        computed = {
            "actual_net": actual,
            "price_pnl": price,
            "fees": fees,
            "funding": funding,
            "adl": adl if not book.block else None,
        }.get(metric if isinstance(metric, str) else "")
        supplied = block.get("ledger", computed)
        supplied_amount = _money(supplied)
        mismatched = (
            computed is not None
            and supplied_amount is not None
            and supplied_amount != computed
        )
        if computed is None or mismatched:
            reconcile = {
                "status": "blocked_unknown" if computed is None else "blocked_mapping",
                "pass": False,
                "frozen_t": _fmt(FROZEN_RECONCILIATION_TOLERANCE_USDT),
                "abs_diff": None,
            }
        else:
            reconcile = reconcile_money(
                ledger=computed,
                venue=block.get("venue"),
                tolerance=block.get("tolerance"),
                metric=metric,
                horizon=block.get("horizon"),
                ledger_includes=block.get("ledger_includes"),
                venue_includes=block.get("venue_includes"),
            )
    unattributed = [item.split(":", 1)[1] for item in book.ids if item.startswith("unattributed:")]
    booked = [item for item in book.ids if not item.startswith("unattributed:")]
    return {
        "status": status,
        "actual_net": None if actual is None else _fmt(actual),
        "price_pnl": None if price is None else _fmt(price),
        "fees": None if fees is None else _fmt(fees),
        "funding": None if funding is None else _fmt(funding),
        "adl": None if book.block else _fmt(adl),
        "paper_funding": None if paper is None else _fmt(paper),
        "paper_label": None if paper is None else PAPER_LABEL,
        "qty": None if qty is None else _fmt(qty),
        "reasons": reasons,
        "booked_ids": booked,
        "unattributed_ids": unattributed,
        "venue_figures": figures,
        "live_pnl": False,
        "venue_calls": 0,
        "reconcile": reconcile,
    }
