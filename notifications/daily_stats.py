"""Shared trade/decision/social stats for daily and morning reports."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

BOT_ROOT = Path(__file__).resolve().parents[1]


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "")[:26])


def load_json(path: Path) -> dict | list:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


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
    root = bot_dir or BOT_ROOT
    positions = load_json(root / "positions.live.json").get("positions", {})
    open_count = sum(1 for p in positions.values() if (p.get("amount") or 0) > 0)
    total = 0.0
    for pos in positions.values():
        amt = float(pos.get("amount") or 0)
        if amt <= 0:
            continue
        total += amt * float(pos.get("average_entry") or 0)
    return open_count, total


def trades_in_window(
    bot_dir: Path,
    since: datetime,
    until: datetime,
) -> list[dict]:
    th = load_json(bot_dir / "live_trade_history.json")
    trades = th.get("trades", [])
    return [t for t in trades if since <= parse_ts(t["timestamp"]) < until]


def orders_in_window(
    bot_dir: Path,
    since: datetime,
    until: datetime,
) -> list[dict]:
    orders_raw = load_json(bot_dir / "orders.live.json")
    return [
        o for o in orders_raw.get("orders", [])
        if since <= parse_ts(o["timestamps"]["created"]) < until
    ]


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
        f"letztes {sym} → {verdict}"
        f"{f' ({reason})' if reason else ''}"
    )


def window_stats(bot_dir: Path, since: datetime, until: datetime) -> dict:
    window_trades = trades_in_window(bot_dir, since, until)
    window_orders = orders_in_window(bot_dir, since, until)
    th = load_json(bot_dir / "live_trade_history.json")
    buys = sum(1 for t in window_trades if t["type"] == "BUY")
    sells = sum(1 for t in window_trades if t["type"] == "SELL")
    dca_buys = sum(
        1 for t in window_trades
        if t["type"] == "BUY" and str(t.get("source", "")).lower() == "dca"
    )
    sell_pnl = sum((t.get("pnl") or 0) for t in window_trades if t["type"] == "SELL")
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