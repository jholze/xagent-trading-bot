#!/usr/bin/env python3
"""Generate a daily trading-bot report under auswertungen/YYYY-MM-DD_tag.md."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path


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


def normalize_action(post: dict) -> str:
    act = (post.get("action") or post.get("signal") or post.get("recommendation") or "?").upper()
    if "BUY" in act:
        return "BUY"
    if "SELL" in act:
        return "SELL"
    if "HOLD" in act:
        return "HOLD"
    return act


def fmt_trade_row(trade: dict) -> str:
    ts = parse_ts(trade["timestamp"]).strftime("%d.%m. %H:%M")
    pnl = trade.get("pnl") or 0
    usdt = trade.get("usdt_amount") or trade.get("usdt_received") or 0
    return (
        f"| {ts} | {trade['type']} | {trade['symbol']} | "
        f"${usdt:,.2f} | {trade.get('source', '?')} | {pnl:+.2f} |"
    )


def open_positions_table(positions: dict) -> tuple[str, float]:
    rows = []
    total = 0.0
    open_items = [(k, v) for k, v in positions.items() if (v.get("amount") or 0) > 0]
    open_items.sort(key=lambda item: -(item[1]["amount"] * item[1].get("average_entry", 0)))
    for key, pos in open_items:
        symbol = key.replace("_USDT_4h", "")
        entry = pos.get("average_entry", 0)
        value = pos["amount"] * entry
        total += value
        sold = pos.get("sold_percent", 0)
        rsi = pos.get("last_rsi", 0)
        rows.append(
            f"| {symbol} | ${value:,.2f} | {sold:.0%} verkauft | RSI {rsi:.1f} |"
        )
    if not rows:
        rows.append("| — | — | — | — |")
    return "\n".join(rows), total


def pnl_by_source(trades: list) -> str:
    pnl = defaultdict(float)
    for trade in trades:
        if trade["type"] == "SELL":
            pnl[trade.get("source", "?")] += trade.get("pnl") or 0
    if not pnl:
        return "_Keine Verkäufe im Zeitraum._"
    lines = ["| Quelle | Realized PnL |", "|--------|--------------|"]
    for source, value in sorted(pnl.items(), key=lambda x: x[1]):
        lines.append(f"| {source} | {value:+.2f} |")
    return "\n".join(lines)


def hermes_section(bot_dir: Path, day_start: datetime, day_end: datetime) -> str:
    exp_path = bot_dir / "hermes/memory/experiments.json"
    skills_path = bot_dir / "hermes/memory/skills.json"
    baseline_path = bot_dir / "hermes/memory/baseline.json"
    if not exp_path.exists():
        return "_Hermes-Daten nicht gefunden._"

    experiments = load_json(exp_path).get("experiments", [])
    day_experiments = [
        e for e in experiments
        if "created_at" in e and day_start <= parse_ts(e["created_at"]) < day_end
    ]
    if not day_experiments:
        day_experiments = experiments[-5:]

    verdicts = Counter(e.get("verdict", "?") for e in experiments)
    sources = Counter(e.get("source", "?") for e in experiments)
    symbols = Counter(e.get("symbol", "?") for e in experiments)
    promoted = verdicts.get("promoted", 0)

    skills_count = 0
    if skills_path.exists():
        skills_count = len(load_json(skills_path).get("skills", []))

    active = "?"
    profiles = []
    active_pool = []
    pool_mode = "?"
    if baseline_path.exists():
        baseline = load_json(baseline_path)
        active = baseline.get("active_key", "?")
        profiles = list(baseline.get("profiles", {}).keys())
        pool_data = baseline.get("active_pool") or {}
        active_pool = pool_data.get("symbols") or []
        pool_mode = pool_data.get("sources", {}).get("mode", "?")

    config_path = bot_dir / "config.json"
    live_vetoes = 0
    if config_path.exists():
        hermes_cfg = load_json(config_path).get("hermes", {})
        pool_mode = hermes_cfg.get("symbols_mode", pool_mode)
        live_vetoes = sum(
            1 for e in experiments
            if e.get("live_veto") or "Live veto" in (e.get("verdict_reason") or "")
        )

    reject_reasons = Counter((e.get("verdict_reason") or "")[:45] for e in experiments)

    lines = [
        "| Metrik | Wert |",
        "|--------|------|",
        f"| Experimente gesamt | {len(experiments)} |",
        f"| Heute neu | {len([e for e in experiments if 'created_at' in e and day_start <= parse_ts(e['created_at']) < day_end])} |",
        f"| Promoted | **{promoted}** |",
        f"| Rejected | {verdicts.get('rejected', 0)} |",
        f"| Quellen | {', '.join(f'{k} {v}' for k, v in sources.items())} |",
        f"| Symbole | {', '.join(f'{k} {v}' for k, v in symbols.items())} |",
        f"| Skills | {skills_count} |",
        f"| Aktives Profil | {active} |",
        f"| Profile | {', '.join(profiles) or '—'} |",
        f"| Symbol-Pool ({pool_mode}) | {', '.join(active_pool) or '—'} |",
        f"| Live-Vetos (gesamt) | {live_vetoes} |",
    ]
    dual_promotes = sum(
        1 for e in experiments if e.get("verdict") == "promoted" and "Dual promote" in (e.get("verdict_reason") or "")
    )
    lines.append(f"| Dual-Promotes (CF) | {dual_promotes} |")
    last_cf = next(
        (e.get("counterfactual_metrics") for e in reversed(experiments) if e.get("counterfactual_metrics")),
        None,
    )
    if last_cf:
        lines.append(
            f"| Letztes CF-Delta | {last_cf.get('pnl_delta', 0):+.2f} USDT "
            f"(seeded={last_cf.get('seeded')}, sells={last_cf.get('variant_sells')}) |"
        )
    lines.extend([
        "",
        "**Häufigste Ablehnungsgründe:**",
    ])
    for reason, count in reject_reasons.most_common(3):
        lines.append(f"- {reason} ({count}×)")
    return "\n".join(lines)


def live_flags(config: dict) -> str:
    live = config.get("live", {})
    hermes = config.get("hermes", {})
    parts = [
        f"live.dry_run={live.get('dry_run')}",
        f"dry_run_enhanced={live.get('dry_run_enhanced')}",
        f"hermes.enabled={hermes.get('enabled')}",
    ]
    return ", ".join(parts)


def generate_report(bot_dir: Path, report_date: datetime | None = None) -> str:
    report_date = report_date or datetime.now()
    day_start = report_date.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)

    th = load_json(bot_dir / "live_trade_history.json")
    trades = th.get("trades", [])
    day_trades = [t for t in trades if day_start <= parse_ts(t["timestamp"]) < day_end]

    orders_raw = load_json(bot_dir / "orders.live.json")
    day_orders = [
        o for o in orders_raw.get("orders", [])
        if day_start <= parse_ts(o["timestamps"]["created"]) < day_end
    ]
    filled_orders = sum(1 for o in day_orders if o["status"] == "filled")
    rejected_orders = sum(1 for o in day_orders if o["status"] == "rejected")
    reject_coins = Counter(o["symbol"] for o in day_orders if o["status"] == "rejected")

    positions = load_json(bot_dir / "positions.live.json").get("positions", {})
    pos_table, pos_value = open_positions_table(positions)
    open_count = sum(1 for p in positions.values() if (p.get("amount") or 0) > 0)

    config = load_json(bot_dir / "config.json")
    cmc_raw = load_json(bot_dir / "cmc_posts.json")
    posts = cmc_posts(cmc_raw)
    day_posts = [p for p in posts if (ts := post_timestamp(p)) and day_start <= ts < day_end]
    cmc_actions = Counter(normalize_action(p) for p in day_posts)
    buy_coins = Counter()
    for post in day_posts:
        if normalize_action(post) == "BUY":
            sym = (post.get("symbol") or post.get("coin") or "").split("/")[0]
            if sym:
                buy_coins[sym] += 1

    sell_pnl_day = sum((t.get("pnl") or 0) for t in day_trades if t["type"] == "SELL")
    buys_day = sum(1 for t in day_trades if t["type"] == "BUY")
    sells_day = sum(1 for t in day_trades if t["type"] == "SELL")
    cash = th.get("virtual_balance", 0)
    realized_total = th.get("realized_pnl", 0)

    trade_rows = [fmt_trade_row(t) for t in sorted(day_trades, key=lambda x: x["timestamp"])]
    if not trade_rows:
        trade_rows = ["| — | — | — | — | — | — |"]

    date_str = day_start.strftime("%Y-%m-%d")
    created = report_date.strftime("%Y-%m-%d")

    summary_parts = []
    if day_trades:
        summary_parts.append(
            f"{len(day_trades)} Trades ({buys_day} BUY, {sells_day} SELL), "
            f"realized PnL Verkäufe: **{sell_pnl_day:+.2f} USDT**."
        )
    else:
        summary_parts.append("Keine Trades heute.")
    if rejected_orders:
        summary_parts.append(
            f"{rejected_orders} Orders wegen Cooldown abgelehnt."
        )
    summary_parts.append(
        f"Portfolio: ${cash:,.2f} Cash + ${pos_value:,.2f} Positionen, "
        f"realized gesamt {realized_total:+.2f} USDT."
    )

    return f"""# Tages-Auswertung Trading Bot

**Zeitraum:** {date_str} 00:00 → {date_str} 23:59  
**Modus:** Live Dry-Run ({live_flags(config)})  
**Erstellt:** {created} (`scripts/daily_auswertung.py`)

---

## Kurzfassung

{' '.join(summary_parts)}

---

## Trades & Portfolio

| Metrik | Wert |
|--------|------|
| Cash (Sim) | ${cash:,.2f} |
| Offene Positionen | {open_count} (~${pos_value:,.2f} Buchwert) |
| Gesamtwert (Entry-Basis) | ~${cash + pos_value:,.2f} |
| Realized PnL gesamt | {realized_total:+.2f} USDT |
| Realized PnL heute (Verkäufe) | {sell_pnl_day:+.2f} USDT |
| Trades heute | {len(day_trades)} ({buys_day} BUY / {sells_day} SELL) |
| Orders heute | {len(day_orders)} ({filled_orders} filled / {rejected_orders} rejected) |

### Trades heute

| Zeit | Typ | Coin | Betrag | Quelle | PnL |
|------|-----|------|--------|--------|-----|
{chr(10).join(trade_rows)}

### PnL nach Quelle (heute)

{pnl_by_source(day_trades)}

### Offene Positionen

| Coin | Buchwert | Status | RSI |
|------|----------|--------|-----|
{pos_table}

---

## Bot-Aktivität

| Metrik | Wert |
|--------|------|
| CMC-Posts heute | {len(day_posts)} |
| CMC BUY / SELL / HOLD | {cmc_actions.get('BUY', 0)} / {cmc_actions.get('SELL', 0)} / {cmc_actions.get('HOLD', 0)} |
| Top CMC-BUY-Coins | {', '.join(f'{c} ({n}×)' for c, n in buy_coins.most_common(5)) or '—'} |
| Abgelehnte Orders (Cooldown) | {rejected_orders}{f' — {dict(reject_coins)}' if reject_coins else ''} |

---

## Hermes

{hermes_section(bot_dir, day_start, day_end)}

**Was „Beobachter, 0 Live-Änderungen" bedeutet:** Hermes testet alle ~30 Min. eine Parameter-Änderung im **Backtest** (Walk-Forward). Nur bei `verdict=promoted` schreibt er in `hermes/memory/baseline.json` und optional `config.json`. Bisher scheitern alle Varianten an der Fold-Validierung (typisch 0/4 Folds gewonnen) — der **Live-Bot handelt weiter mit den festen Config-Parametern**.

---

## Auffälligkeiten

- CAT-Bestand wirkt extrem hoch, ist aber korrekt ($500 ÷ ~0,0000015 USDT).
- X-Live-Suche aus (kein Bearer Token).
- CMC Trending/Content-API nicht im Plan → Fallback-Endpoints.

---

## Referenz-Dateien

- `live_trade_history.json`
- `orders.live.json`
- `positions.live.json`
- `cmc_posts.json`
- `config.json`
- `hermes/memory/experiments.json`
- `hermes/memory/baseline.json`
- `logs/aria_log.txt`
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate daily bot report")
    parser.add_argument(
        "--bot-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Trading bot root directory (default: repo root)",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Report date YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print report to stdout instead of writing file",
    )
    args = parser.parse_args()

    bot_dir = args.bot_dir.resolve()
    report_date = datetime.strptime(args.date, "%Y-%m-%d") if args.date else datetime.now()
    report = generate_report(bot_dir, report_date)

    if args.stdout:
        print(report)
        return

    out_dir = bot_dir / "auswertungen"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"{report_date.strftime('%Y-%m-%d')}_tag.md"
    out_path.write_text(report, encoding="utf-8")
    print(f"Written: {out_path}")


if __name__ == "__main__":
    main()