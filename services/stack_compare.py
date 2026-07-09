"""Aggregate prod vs staging observability data."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from core.actions import is_sell
from services.observability_store import load_decisions, load_snapshots


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "")[:26])
    except Exception:
        return None


def _decision_stack_stats(rows: list[dict]) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "evals": 0,
        "with_position": 0,
        "sell_signals": 0,
        "executed_sells": 0,
        "hold": 0,
        "sources": Counter(),
        "would_sources": Counter(),
        "trail_exclusive_blocked": Counter(),
        "entry_guard_blocks": 0,
        "trail_armed_evals": 0,
    }
    for rec in rows:
        stats["evals"] += 1
        if rec.get("has_position"):
            stats["with_position"] += 1
        action = str(rec.get("normalized_action") or rec.get("action") or "").upper()
        if action == "HOLD":
            stats["hold"] += 1
        if is_sell(action):
            stats["sell_signals"] += 1
        if rec.get("executed") and is_sell(action):
            stats["executed_sells"] += 1
        for src in rec.get("sources") or []:
            stats["sources"][str(src).lower()] += 1
        ws = rec.get("would_source")
        if ws:
            stats["would_sources"][str(ws).lower()] += 1
        for blocked in rec.get("trail_exclusive_blocked") or []:
            stats["trail_exclusive_blocked"][str(blocked).lower()] += 1
        if rec.get("trail_armed"):
            stats["trail_armed_evals"] += 1
        rm = str(rec.get("risk_message") or "").lower()
        if "entry_guard" in rm or "entry_guard" in str(rec.get("sources") or []):
            stats["entry_guard_blocks"] += 1
    return stats


def _latest_snapshot_by_symbol(snaps: list[dict]) -> dict[str, dict]:
    latest: dict[str, tuple[datetime | None, dict]] = {}
    for snap in snaps:
        ts = _parse_ts(snap.get("ts") or snap.get("recorded_at"))
        for pos in snap.get("positions") or []:
            key = pos.get("key") or f"{pos.get('symbol')}_{pos.get('timeframe')}"
            prev = latest.get(key)
            if prev is None or (ts and (prev[0] is None or ts > prev[0])):
                latest[key] = (ts, pos)
    return {k: v[1] for k, v in latest.items()}


def would_sell_divergences(
    staging_positions: dict[str, dict],
    prod_positions: dict[str, dict],
    *,
    top_n: int = 25,
) -> list[dict]:
    divergent: list[dict] = []
    all_keys = sorted(set(staging_positions) | set(prod_positions))
    for key in all_keys:
        s = staging_positions.get(key) or {}
        p = prod_positions.get(key) or {}
        s_act = s.get("would_action") or "—"
        p_act = p.get("would_action") or "—"
        if s_act == p_act:
            continue
        divergent.append({
            "key": key,
            "symbol": s.get("symbol") or p.get("symbol") or key,
            "staging_would": s_act,
            "staging_source": s.get("would_source") or "",
            "prod_would": p_act,
            "prod_source": p.get("would_source") or "",
            "staging_peak": s.get("peak_gain_pct"),
            "prod_peak": p.get("peak_gain_pct"),
        })
    divergent.sort(
        key=lambda r: abs((r.get("staging_peak") or 0) - (r.get("prod_peak") or 0)),
        reverse=True,
    )
    return divergent[:top_n]


def build_stack_compare_report(
    *,
    since: datetime,
    until: datetime | None = None,
    staging_decision_paths: list[Path] | None = None,
    prod_decision_paths: list[Path] | None = None,
    staging_snapshot_paths: list[Path] | None = None,
    prod_snapshot_paths: list[Path] | None = None,
    top_n: int = 25,
) -> dict[str, Any]:
    staging_dec = load_decisions(
        since=since, until=until, bot_stack="staging", paths=staging_decision_paths,
    )
    prod_dec = load_decisions(
        since=since, until=until, bot_stack="production", paths=prod_decision_paths,
    )
    if not staging_dec and staging_decision_paths is None:
        staging_dec = load_decisions(since=since, until=until, paths=None)
        staging_dec = [r for r in staging_dec if r.get("bot_stack") in (None, "staging", "local")]
    if not prod_dec and prod_decision_paths is None:
        prod_dec = load_decisions(since=since, until=until, paths=None)
        prod_dec = [r for r in prod_dec if r.get("bot_stack") == "production"]

    staging_snaps = load_snapshots(
        since=since, bot_stack="staging", paths=staging_snapshot_paths,
    )
    prod_snaps = load_snapshots(
        since=since, bot_stack="production", paths=prod_snapshot_paths,
    )

    st_pos = _latest_snapshot_by_symbol(staging_snaps)
    pr_pos = _latest_snapshot_by_symbol(prod_snaps)

    return {
        "since": since.isoformat(),
        "until": (until or datetime.now()).isoformat(),
        "staging": {
            "decisions": _decision_stack_stats(staging_dec),
            "snapshot_count": len(staging_snaps),
            "open_positions_latest": len(st_pos),
            "build_commits": sorted({r.get("build_commit", "") for r in staging_dec if r.get("build_commit")}),
            "config_fingerprints": sorted({r.get("config_fingerprint", "") for r in staging_dec if r.get("config_fingerprint")}),
        },
        "production": {
            "decisions": _decision_stack_stats(prod_dec),
            "snapshot_count": len(prod_snaps),
            "open_positions_latest": len(pr_pos),
            "build_commits": sorted({r.get("build_commit", "") for r in prod_dec if r.get("build_commit")}),
            "config_fingerprints": sorted({r.get("config_fingerprint", "") for r in prod_dec if r.get("config_fingerprint")}),
        },
        "divergences": would_sell_divergences(st_pos, pr_pos, top_n=top_n),
    }


def format_stack_compare_markdown(report: dict[str, Any]) -> str:
    st = report["staging"]["decisions"]
    pr = report["production"]["decisions"]

    def _src_table(stats: dict, key: str) -> str:
        ctr: Counter = stats.get(key) or Counter()
        if not ctr:
            return "_keine_"
        lines = ["| Source | Count |", "|--------|------:|"]
        for name, count in ctr.most_common(15):
            lines.append(f"| {name} | {count} |")
        return "\n".join(lines)

    lines = [
        f"# Stack Compare — {report['since'][:10]}",
        "",
        f"Zeitraum: `{report['since']}` → `{report['until']}`",
        "",
        "## KPIs",
        "",
        "| Metrik | Staging | Production |",
        "|--------|--------:|-----------:|",
        f"| Evals | {st['evals']} | {pr['evals']} |",
        f"| Mit Position | {st['with_position']} | {pr['with_position']} |",
        f"| Sell-Signale | {st['sell_signals']} | {pr['sell_signals']} |",
        f"| Executed Sells | {st['executed_sells']} | {pr['executed_sells']} |",
        f"| Trail armed evals | {st['trail_armed_evals']} | {pr['trail_armed_evals']} |",
        f"| Open (letzter Snapshot) | {report['staging']['open_positions_latest']} | {report['production']['open_positions_latest']} |",
        "",
        "### Staging Builds",
        ", ".join(report["staging"]["build_commits"]) or "—",
        "",
        "### Production Builds",
        ", ".join(report["production"]["build_commits"]) or "—",
        "",
        "## Sell Sources (Staging)",
        "",
        _src_table(st, "sources"),
        "",
        "## Would-Sell Sources (Staging)",
        "",
        _src_table(st, "would_sources"),
        "",
        "## Trail-Exclusive Blocks (Staging)",
        "",
        _src_table(st, "trail_exclusive_blocked"),
        "",
        "## Would-Sell Divergenzen (Staging vs Production)",
        "",
    ]
    divs = report.get("divergences") or []
    if not divs:
        lines.append("_Keine Divergenzen in gemeinsamen Snapshot-Keys._")
    else:
        lines.extend([
            "| Symbol | Staging | Prod | Staging Src | Prod Src |",
            "|--------|---------|------|-------------|----------|",
        ])
        for d in divs:
            lines.append(
                f"| {d['symbol']} | {d['staging_would']} | {d['prod_would']} | "
                f"{d['staging_source']} | {d['prod_source']} |"
            )
    lines.append("")
    return "\n".join(lines)


def _format_counter_lines(ctr: Counter, *, limit: int = 8) -> list[str]:
    if not ctr:
        return ["<i>keine</i>"]
    lines: list[str] = []
    for name, count in ctr.most_common(limit):
        lines.append(f"• <code>{name}</code> — {count}")
    return lines


def format_stack_compare_telegram(
    report: dict[str, Any],
    *,
    local_stack: str | None = None,
    max_divergences: int = 12,
) -> list[str]:
    """Compact HTML report for Telegram (may return multiple chunks)."""
    st = report["staging"]["decisions"]
    pr = report["production"]["decisions"]
    since = str(report.get("since") or "")[:16].replace("T", " ")
    until = str(report.get("until") or "")[:16].replace("T", " ")

    lines = [
        "<b>📊 Stack Compare</b>",
        f"<i>{since} → {until}</i>",
    ]
    if local_stack:
        lines.append(f"Lokale Instanz: <code>{local_stack}</code>")
    lines.extend([
        "",
        "<b>KPIs</b>",
        f"Staging — Evals: {st['evals']} | Positionen: {st['with_position']} | "
        f"Sells: {st['sell_signals']} ({st['executed_sells']} exec) | Trail: {st['trail_armed_evals']}",
        f"Prod — Evals: {pr['evals']} | Positionen: {pr['with_position']} | "
        f"Sells: {pr['sell_signals']} ({pr['executed_sells']} exec) | Trail: {pr['trail_armed_evals']}",
        f"Open (Snapshot): Staging {report['staging']['open_positions_latest']} | "
        f"Prod {report['production']['open_positions_latest']}",
    ])

    st_builds = report["staging"]["build_commits"]
    pr_builds = report["production"]["build_commits"]
    if st_builds or pr_builds:
        lines.extend([
            "",
            "<b>Builds</b>",
            f"Staging: <code>{', '.join(st_builds) or '—'}</code>",
            f"Prod: <code>{', '.join(pr_builds) or '—'}</code>",
        ])

    lines.extend([
        "",
        "<b>Sell Sources (Staging)</b>",
        *_format_counter_lines(st.get("sources") or Counter()),
        "",
        "<b>Would-Sell (Staging)</b>",
        *_format_counter_lines(st.get("would_sources") or Counter()),
    ])

    trail_blocks = st.get("trail_exclusive_blocked") or Counter()
    if trail_blocks:
        lines.extend([
            "",
            "<b>Trail-Exclusive Blocks</b>",
            *_format_counter_lines(trail_blocks),
        ])

    divs = report.get("divergences") or []
    lines.extend(["", "<b>Divergenzen (Would-Sell)</b>"])
    if not divs:
        lines.append("<i>Keine in gemeinsamen Snapshot-Keys.</i>")
    else:
        for d in divs[:max_divergences]:
            sym = str(d.get("symbol") or d.get("key") or "?")
            lines.append(
                f"• <b>{sym}</b> — Stg <code>{d.get('staging_would') or '—'}</code> "
                f"({d.get('staging_source') or '—'}) vs Prod <code>{d.get('prod_would') or '—'}</code> "
                f"({d.get('prod_source') or '—'})"
            )
        if len(divs) > max_divergences:
            lines.append(f"<i>… +{len(divs) - max_divergences} weitere</i>")

    if st["evals"] == 0 and pr["evals"] == 0:
        lines.extend([
            "",
            "<i>Hinweis: Wenig lokale Daten — voller Vergleich braucht Mongo-Sync "
            "oder <code>pull_stack_observability.sh</code>.</i>",
        ])

    text = "\n".join(lines)
    limit = 3900
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    while text:
        chunks.append(text[:limit])
        text = text[limit:]
    return chunks