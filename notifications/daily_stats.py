"""Shared trade/decision/social stats for daily and morning reports."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from logger import log

BOT_ROOT = Path(__file__).resolve().parents[1]


def parse_ts(value: str) -> datetime:
    """Parse ISO timestamp to naive UTC (comparable across sources)."""
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        dt = datetime.fromisoformat(raw.replace("Z", "")[:26])
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


_MISSING = object()


def load_json(path: Path, default=_MISSING) -> dict | list:
    """Load a JSON file.

    Required files (no ``default``) raise ``FileNotFoundError`` as before —
    a missing config or ledger must surface, not read as empty. Optional
    files pass an explicit ``default`` and get it back with a WARNING.
    """
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        if default is _MISSING:
            raise
        log(f"JSON file not found, using default: {path}", "WARNING")
        return default


def _stats_ledger_scope() -> str:
    """Same scope the bot uses for orders/positions (not hardcoded demo)."""
    from data_manager import resolve_ledger_scope

    return resolve_ledger_scope()


def load_trade_history_doc() -> dict:
    """Trade history from active ledger scope (Mongo or JSON)."""
    try:
        from data_manager import load_trade_history_document

        return load_trade_history_document(_stats_ledger_scope()) or {}
    except Exception:
        pass
    for name in (
        "live_trade_history.json",
        "live_trade_history.demo.json",
        "trade_history.json",
        "trade_history.demo.json",
    ):
        path = BOT_ROOT / name
        if not path.exists():
            continue
        try:
            return load_json(path)
        except Exception:
            continue
    return {"trades": [], "virtual_balance": 0.0, "realized_pnl": 0.0, "total_pnl": 0.0}


def load_orders_doc() -> dict:
    """Orders from active ledger scope, with legacy JSON fallback."""
    try:
        from data_manager import load_orders

        return load_orders(_stats_ledger_scope()) or {"orders": []}
    except Exception:
        pass
    scope = _stats_ledger_scope()
    candidates = {
        "demo": ("orders.demo.json",),
        "paper": ("orders.paper.json",),
        "live": ("orders.live.json",),
    }.get(scope, ("orders.live.json", "orders.paper.json", "orders.demo.json"))
    for name in candidates:
        for path in (BOT_ROOT / "data" / name, BOT_ROOT / name):
            if not path.exists():
                continue
            try:
                return load_json(path)
            except Exception:
                continue
    return {"orders": []}


def _order_window_ts(order: dict) -> datetime | None:
    ts = order.get("timestamps") or {}
    # Prefer fill time for activity windows
    raw = ts.get("filled") or ts.get("created") or ts.get("updated")
    if not raw:
        return None
    try:
        return parse_ts(str(raw))
    except Exception:
        return None


def filled_order_to_trade(order: dict) -> dict | None:
    """Map a filled order record to the trade-history shape used by briefings."""
    if str(order.get("status") or "").lower() != "filled":
        return None
    side = str(order.get("side") or "").upper()
    if side not in ("BUY", "SELL", "SHORT", "COVER"):
        return None
    ts = _order_window_ts(order)
    if ts is None:
        return None
    req = order.get("request") or {}
    ex = order.get("execution") or {}
    usdt = ex.get("usdt") or ex.get("usdt_amount") or req.get("usdt") or req.get("usdt_amount") or 0
    try:
        usdt_f = float(usdt or 0)
    except Exception:
        usdt_f = 0.0
    pnl = order.get("pnl")
    if isinstance(pnl, dict):
        pnl = pnl.get("usdt") or pnl.get("realized") or pnl.get("pnl")
    ts_raw = (order.get("timestamps") or {}).get("filled") or (
        order.get("timestamps") or {}
    ).get("created")
    return {
        "type": side,
        "symbol": order.get("symbol") or "?",
        "timestamp": ts_raw or ts.isoformat(),
        "usdt_amount": usdt_f,
        "usdt_received": usdt_f if side in ("SELL", "COVER") else None,
        "source": order.get("source") or "order",
        "pnl": pnl,
        "_from_order": True,
        "_order_id": order.get("id") or order.get("display_seq"),
    }


def cmc_posts(raw) -> list:
    if isinstance(raw, list):
        return raw
    return raw.get("posts", raw.get("signals", []))


def post_timestamp(post: dict) -> datetime | None:
    for key in ("timestamp", "created_at", "fetched_at", "time"):
        if key in post and post[key]:
            return parse_ts(str(post[key]))
    return None


def normalize_social_action(post: dict) -> str:
    act = (post.get("action") or post.get("signal") or post.get("recommendation") or "?").upper()
    if "BUY" in act:
        return "BUY"
    if "SELL" in act:
        return "SELL"
    if "HOLD" in act:
        return "HOLD"
    return act


def open_positions_summary(bot_dir: Path | None = None) -> tuple[int, float]:
    try:
        from strategies.positions import bootstrap_positions, list_active_positions

        scope = _stats_ledger_scope()
        bootstrap_positions(scope=scope)
        active = list_active_positions()
        total = sum(
            float(p.get("amount") or 0)
            * float(p.get("average_entry") or p.get("entry_price") or 0)
            for p in active
        )
        return len(active), total
    except Exception:
        pass
    root = bot_dir or BOT_ROOT
    for name in ("positions.live.json", "positions.demo.json", "positions.json"):
        path = None
        for candidate in (root / "data" / name, root / name):
            if candidate.exists():
                path = candidate
                break
        if path is None:
            continue
        try:
            positions = load_json(path).get("positions", {})
        except Exception:
            continue
        open_count = sum(1 for p in positions.values() if (p.get("amount") or 0) > 0)
        total = 0.0
        for pos in positions.values():
            amt = float(pos.get("amount") or 0)
            if amt <= 0:
                continue
            total += amt * float(pos.get("average_entry") or 0)
        return open_count, total
    return 0, 0.0


def trades_in_window(
    bot_dir: Path,
    since: datetime,
    until: datetime,
) -> list[dict]:
    """Trades in [since, until). Prefer trade_history; fall back to filled orders.

    Railway / Mongo often has fills only in the order ledger.
    """
    th = load_trade_history_doc()
    trades = th.get("trades", []) or []
    out: list[dict] = []
    for trade in trades:
        ts_raw = trade.get("timestamp")
        if not ts_raw:
            continue
        try:
            ts = parse_ts(str(ts_raw))
        except Exception:
            continue
        if since <= ts < until:
            out.append(trade)

    # Always merge filled orders so morning briefing works when history is empty
    # or only partially synced.
    seen = set()
    for t in out:
        key = (
            str(t.get("type") or "").upper(),
            str(t.get("symbol") or ""),
            str(t.get("timestamp") or "")[:19],
            round(float(t.get("usdt_amount") or t.get("usdt_received") or 0), 2),
        )
        seen.add(key)

    for order in orders_in_window(bot_dir, since, until):
        row = filled_order_to_trade(order)
        if not row:
            continue
        try:
            ts = parse_ts(str(row["timestamp"]))
        except Exception:
            continue
        if not (since <= ts < until):
            continue
        key = (
            row["type"],
            str(row.get("symbol") or ""),
            str(row.get("timestamp") or "")[:19],
            round(float(row.get("usdt_amount") or 0), 2),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(row)

    out.sort(key=lambda t: str(t.get("timestamp") or ""))
    return out


def orders_in_window(
    bot_dir: Path,
    since: datetime,
    until: datetime,
) -> list[dict]:
    orders_raw = load_orders_doc()
    out = []
    for order in orders_raw.get("orders", []):
        ts = _order_window_ts(order)
        if ts and since <= ts < until:
            out.append(order)
    return out


def decision_stats(bot_dir: Path, since: datetime, until: datetime) -> dict:
    path = bot_dir / "logs/decisions.jsonl"
    stats = {
        "total": 0,
        "buy": 0,
        "sell": 0,
        "buy_dca": 0,
        "buy_dca_executed": 0,
        "buy_dca_shadow": 0,
        "hold": 0,
        "executed": 0,
    }
    if not path.exists():
        return stats
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts_raw = rec.get("timestamp")
            if not ts_raw:
                continue
            ts = parse_ts(str(ts_raw))
            if not (since <= ts < until):
                continue
            stats["total"] += 1
            action = str(rec.get("normalized_action") or rec.get("action") or "").upper()
            shadow = str(rec.get("shadow_action") or "").upper()
            sources = [str(s).lower() for s in (rec.get("sources") or [])]
            if rec.get("executed"):
                stats["executed"] += 1
            if action == "BUY_DCA" or "dca" in sources:
                stats["buy_dca"] += 1
                if rec.get("executed"):
                    stats["buy_dca_executed"] += 1
            elif shadow == "BUY_DCA":
                stats["buy_dca_shadow"] += 1
            elif action.startswith("BUY"):
                stats["buy"] += 1
            elif action.startswith("SELL"):
                stats["sell"] += 1
            elif action == "HOLD":
                stats["hold"] += 1
    return stats


def decision_highlights(
    bot_dir: Path,
    since: datetime,
    until: datetime,
    limit: int = 5,
) -> list[dict]:
    path = bot_dir / "logs/decisions.jsonl"
    if not path.exists():
        return []
    interesting = set()
    entries: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts_raw = rec.get("timestamp")
            if not ts_raw:
                continue
            ts = parse_ts(str(ts_raw))
            if not (since <= ts < until):
                continue
            action = str(rec.get("normalized_action") or rec.get("action") or "").upper()
            shadow = str(rec.get("shadow_action") or "").upper()
            sources = [str(s).lower() for s in (rec.get("sources") or [])]
            notable = (
                rec.get("executed")
                or action in ("BUY", "BUY_DCA", "BUY_STRONG")
                or action.startswith("SELL")
                or shadow == "BUY_DCA"
                or "dca" in sources
            )
            if not notable:
                continue
            entries.append({**rec, "_ts": ts})
    entries.sort(key=lambda e: e["_ts"], reverse=True)
    out = []
    for rec in entries:
        sym = rec.get("symbol", "?")
        action = str(rec.get("normalized_action") or rec.get("action") or "HOLD")
        shadow = str(rec.get("shadow_action") or "")
        if shadow and action == "HOLD":
            action = f"HOLD→{shadow}"
        key = (sym, action, rec.get("executed"))
        if key in interesting:
            continue
        interesting.add(key)
        rationale = str(rec.get("rationale") or "")[:80]
        out.append({
            "time": rec["_ts"].strftime("%d.%m. %H:%M"),
            "symbol": sym,
            "action": action,
            "executed": bool(rec.get("executed")),
            "rationale": rationale,
        })
        if len(out) >= limit:
            break
    return out


def social_highlights(
    bot_dir: Path,
    since: datetime,
    until: datetime,
    limit: int = 3,
) -> list[str]:
    lines: list[str] = []
    cmc_path = bot_dir / "cmc_posts.json"
    if cmc_path.exists():
        posts = cmc_posts(load_json(cmc_path))
        for post in sorted(
            [p for p in posts if (ts := post_timestamp(p)) and since <= ts < until],
            key=lambda p: post_timestamp(p) or datetime.min,
            reverse=True,
        ):
            act = normalize_social_action(post)
            if act not in ("BUY", "SELL"):
                continue
            sym = post.get("symbol") or post.get("coin") or "?"
            conf = post.get("confidence") or post.get("score") or ""
            ts = post_timestamp(post)
            time_s = ts.strftime("%H:%M") if ts else "?"
            conf_s = f" {conf}%" if conf != "" else ""
            lines.append(f"• CMC {act} {sym}{conf_s} ({time_s})")
            if len(lines) >= limit:
                return lines
    lc_path = bot_dir / "data/lc_signals.json"
    if lc_path.exists() and len(lines) < limit:
        try:
            lc_raw = load_json(lc_path)
            signals = lc_raw if isinstance(lc_raw, list) else lc_raw.get("signals", [])
            for sig in sorted(signals, key=lambda s: str(s.get("timestamp", "")), reverse=True):
                ts_raw = sig.get("timestamp") or sig.get("created_at")
                if not ts_raw:
                    continue
                ts = parse_ts(str(ts_raw))
                if not (since <= ts < until):
                    continue
                act = normalize_social_action(sig)
                if act not in ("BUY", "SELL"):
                    continue
                sym = sig.get("symbol") or sig.get("coin") or "?"
                lines.append(f"• LC {act} {sym} ({ts.strftime('%H:%M')})")
                if len(lines) >= limit:
                    break
        except Exception:
            pass
    return lines


def hermes_brief_line(bot_dir: Path) -> str:
    exp_path = bot_dir / "hermes/memory/experiments.json"
    if not exp_path.exists():
        return "Hermes: keine Daten"
    experiments = load_json(exp_path).get("experiments", [])
    if not experiments:
        return "Hermes: keine Experimente"
    verdicts = Counter(e.get("verdict", "?") for e in experiments)
    last = experiments[-1]
    sym = last.get("symbol", "?")
    verdict = last.get("verdict", "?")
    reason = str(last.get("verdict_reason") or "")[:40]
    return (
        f"Hermes: {len(experiments)} Experimente, "
        f"{verdicts.get('promoted', 0)} promoted · "
        f"{verdicts.get('inconclusive', 0)} inconclusive · "
        f"letztes {sym} → {verdict}"
        f"{f' ({reason})' if reason else ''}"
    )


def window_stats(bot_dir: Path, since: datetime, until: datetime) -> dict:
    window_trades = trades_in_window(bot_dir, since, until)
    window_orders = orders_in_window(bot_dir, since, until)
    th = load_trade_history_doc()
    buys = sum(1 for t in window_trades if t["type"] == "BUY")
    sells = sum(1 for t in window_trades if t["type"] == "SELL")
    shorts = sum(1 for t in window_trades if t["type"] == "SHORT")
    covers = sum(1 for t in window_trades if t["type"] == "COVER")
    dca_buys = sum(
        1 for t in window_trades
        if t["type"] == "BUY" and str(t.get("source", "")).lower() == "dca"
    )
    sell_pnl = sum((t.get("pnl") or 0) for t in window_trades if t["type"] in ("SELL", "COVER"))
    filled_orders = sum(1 for o in window_orders if o["status"] == "filled")
    rejected_orders = sum(1 for o in window_orders if o["status"] == "rejected")
    open_count, pos_value = open_positions_summary(bot_dir)
    return {
        "since": since,
        "until": until,
        "trades": window_trades,
        "orders": window_orders,
        "buys": buys,
        "sells": sells,
        "shorts": shorts,
        "covers": covers,
        "dca_buys": dca_buys,
        "sell_pnl": sell_pnl,
        "filled_orders": filled_orders,
        "rejected_orders": rejected_orders,
        "cash": float(th.get("virtual_balance", 0) or 0),
        "realized_total": float(th.get("realized_pnl", 0) or 0),
        "open_count": open_count,
        "pos_value": pos_value,
        "decisions": decision_stats(bot_dir, since, until),
        "highlights": decision_highlights(bot_dir, since, until),
        "social": social_highlights(bot_dir, since, until),
        "hermes": hermes_brief_line(bot_dir),
    }