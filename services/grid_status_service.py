"""Grid mode status for active watchlist coins (Telegram /grid)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from data_manager import get_config, load_effective_watchlist
from logger import DECISIONS_LOG_FILE
from services.observability_store import tail_jsonl
from strategies.registry import resolve_coin_config

_REGIME_RE = re.compile(r"regime=([A-Z_]+)")


@dataclass
class GridCoinStatus:
    symbol: str
    timeframe: str
    mode: str  # active | tracking | partial | forced | off | unknown
    regime: str | None = None
    last_action: str | None = None
    last_at: str | None = None
    center_price: float | None = None
    spacing: float | None = None
    open_levels: int = 0
    has_position: bool = False
    rationale_snip: str = ""


def _grid_state_key(symbol: str, tf: str) -> str:
    return f"{symbol}_{tf}"


def _parse_regime(rationale: str | None) -> str | None:
    if not rationale:
        return None
    m = _REGIME_RE.search(rationale)
    return m.group(1) if m else None


def _level_stats(state: dict | None) -> tuple[float | None, float | None, int]:
    if not state or not isinstance(state, dict):
        return None, None, 0
    center = state.get("center_price")
    spacing = state.get("spacing")
    levels = state.get("levels") or []
    open_ct = sum(1 for lv in levels if isinstance(lv, dict) and not lv.get("filled"))
    try:
        center_f = float(center) if center is not None else None
    except (TypeError, ValueError):
        center_f = None
    try:
        spacing_f = float(spacing) if spacing is not None else None
    except (TypeError, ValueError):
        spacing_f = None
    return center_f, spacing_f, open_ct


def _classify_mode(
    *,
    forced: bool,
    state: dict | None,
    last: dict | None,
) -> str:
    if forced:
        return "forced"
    if last and str(last.get("strategy_profile") or "") == "grid":
        return "active"
    if state:
        return "tracking"
    if last and "grid" in (last.get("sources") or []):
        return "partial"
    if last:
        return "off"
    return "unknown"


def _latest_decisions_by_symbol(entries: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for entry in entries:
        sym = str(entry.get("symbol") or "").upper()
        if not sym or sym in out:
            continue
        out[sym] = entry
    return out


def build_grid_status_report(
    *,
    symbol_filter: str | None = None,
    decisions_limit: int = 800,
) -> dict[str, Any]:
    """Summarize grid mode per active watchlist coin."""
    cfg = get_config() or {}
    grid_states = dict(cfg.get("grid_states") or {})
    # Phase B rest: merge tenant Mongo plans (source of truth when present)
    try:
        from storage.grid_plan_store import load_grid_plans_document

        mongo_plans = (load_grid_plans_document().get("plans") or {})
        for k, v in mongo_plans.items():
            if isinstance(v, dict):
                grid_states[k] = {**grid_states.get(k, {}), **v}
    except Exception:
        pass
    grid_cfg = (cfg.get("grid") or {})
    allocator_cfg = (cfg.get("strategy_allocator") or {})
    regime_cfg = (cfg.get("regime_detector") or {})

    watchlist = [c for c in load_effective_watchlist() if c.get("active", True)]
    if symbol_filter:
        base = symbol_filter.upper().replace("/USDT", "").strip()
        sym = f"{base}/USDT" if "/" not in base else base
        watchlist = [c for c in watchlist if str(c.get("symbol", "")).upper() == sym]

    entries = tail_jsonl(DECISIONS_LOG_FILE, decisions_limit)
    latest = _latest_decisions_by_symbol(entries)

    rows: list[GridCoinStatus] = []
    for coin in watchlist:
        resolved = resolve_coin_config(coin)
        symbol = str(resolved.get("symbol") or "")
        tf = str(resolved.get("timeframe") or "4h")
        if not symbol:
            continue
        key = _grid_state_key(symbol, tf)
        state = grid_states.get(key)
        last = latest.get(symbol.upper())
        forced = resolved.get("strategy_class") == "grid"
        center, spacing, open_lv = _level_stats(state)
        mode = _classify_mode(forced=forced, state=state, last=last)
        rationale = str((last or {}).get("rationale") or "")
        rows.append(
            GridCoinStatus(
                symbol=symbol,
                timeframe=tf,
                mode=mode,
                regime=_parse_regime(rationale),
                last_action=(last or {}).get("normalized_action") or (last or {}).get("action"),
                last_at=str((last or {}).get("timestamp") or "")[:16] or None,
                center_price=center,
                spacing=spacing,
                open_levels=open_lv,
                has_position=bool((last or {}).get("has_position")),
                rationale_snip=rationale[:80],
            )
        )

    by_mode: dict[str, list[GridCoinStatus]] = {}
    for row in rows:
        by_mode.setdefault(row.mode, []).append(row)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "watchlist_active": len(rows),
        "grid_enabled": bool(grid_cfg.get("enabled", True)),
        "allocator_enabled": bool(allocator_cfg.get("enabled", True)),
        "regime_enabled": bool(regime_cfg.get("enabled", True)),
        "rows": rows,
        "by_mode": by_mode,
        "symbol_filter": symbol_filter,
    }


def _mode_label_de(mode: str) -> str:
    return {
        "active": "aktiv",
        "tracking": "Tracking",
        "partial": "Grid-Anteil",
        "forced": "erzwungen",
        "off": "aus",
        "unknown": "unbekannt",
    }.get(mode, mode)


def _format_coin_line(row: GridCoinStatus, *, detailed: bool = False) -> str:
    parts = [f"<b>{row.symbol}</b> {row.timeframe}"]
    if row.regime:
        parts.append(row.regime)
    if row.center_price is not None:
        parts.append(f"Center {row.center_price:,.2f}")
    if row.spacing is not None:
        parts.append(f"±{row.spacing:,.2f}")
    if row.open_levels:
        parts.append(f"{row.open_levels} Levels")
    if row.last_action:
        parts.append(f"letzte: {row.last_action}")
    if row.has_position:
        parts.append("📦 Pos")
    line = " — ".join(parts)
    if detailed and row.rationale_snip:
        line += f"\n  <i>{row.rationale_snip}</i>"
    return line


def format_grid_status_telegram(report: dict[str, Any], *, lang: str = "de") -> str:
    rows: list[GridCoinStatus] = report.get("rows") or []
    by_mode: dict[str, list[GridCoinStatus]] = report.get("by_mode") or {}
    filt = report.get("symbol_filter")
    detailed = bool(filt)

    if lang == "en":
        title = "🔲 <b>Grid mode</b>"
        empty = "No active watchlist coins."
        footer = (
            "<i>Active = GridStrategy in last decision · Tracking = saved grid_states · "
            "Off = Allocator favors momentum or grid weight ≤ 5%</i>"
        )
    else:
        title = "🔲 <b>Grid-Modus</b>"
        empty = "Keine aktiven Watchlist-Coins."
        footer = (
            "<i>Aktiv = GridStrategy in letzter Entscheidung · Tracking = gespeichertes grid_states · "
            "Aus = Allocator bevorzugt Momentum oder Grid-Gewicht ≤ 5%</i>"
        )

    lines = [title, ""]
    if filt:
        lines.append(f"<code>{filt}</code>")
        lines.append("")

    if not rows:
        lines.append(empty)
        return "\n".join(lines)

    show_order = ("forced", "active", "tracking", "partial", "unknown", "off")
    section_titles_de = {
        "forced": "Erzwungen (strategy_class=grid)",
        "active": "Aktiv — GridStrategy",
        "tracking": "Tracking — Grid-State gespeichert",
        "partial": "Grid-Anteil in letzter Entscheidung",
        "unknown": "Noch keine Entscheidung im Log",
        "off": "Aus — Momentum / kein Grid",
    }
    section_titles_en = {
        "forced": "Forced (strategy_class=grid)",
        "active": "Active — GridStrategy",
        "tracking": "Tracking — persisted grid state",
        "partial": "Grid contributed in last decision",
        "unknown": "No decision in log yet",
        "off": "Off — momentum / no grid",
    }
    section_titles = section_titles_en if lang == "en" else section_titles_de

    active_ct = sum(len(by_mode.get(m, [])) for m in ("forced", "active", "tracking", "partial"))
    if lang == "en":
        lines.append(f"Watchlist: <b>{len(rows)}</b> active · <b>{active_ct}</b> with grid involvement")
    else:
        lines.append(f"Watchlist: <b>{len(rows)}</b> aktiv · <b>{active_ct}</b> mit Grid-Bezug")
    lines.append(
        f"Config: grid={'on' if report.get('grid_enabled') else 'off'} · "
        f"allocator={'on' if report.get('allocator_enabled') else 'off'} · "
        f"regime={'on' if report.get('regime_enabled') else 'off'}"
    )
    lines.append("")

    max_per_section = 15 if detailed else 8
    for mode in show_order:
        group = by_mode.get(mode) or []
        if not group:
            continue
        lines.append(f"<b>{section_titles.get(mode, mode)} ({len(group)})</b>")
        for row in group[:max_per_section]:
            lines.append(f"• {_format_coin_line(row, detailed=detailed)}")
        if len(group) > max_per_section:
            rest = len(group) - max_per_section
            lines.append(f"  <i>… +{rest} weitere</i>" if lang == "de" else f"  <i>… +{rest} more</i>")
        lines.append("")

    lines.append(footer)
    return "\n".join(lines).rstrip()