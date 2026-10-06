"""#594 shadow tags: source + lane on observe/trade members.

Visibility only. Membership stays with the existing observe/trade builders
(``services.universe.split`` and ``services.universe.core_seed``). This module
does not add, drop, or reorder coins and does not place orders.

Config (``config.json``): ``universe_early_trend.mode`` is ``off`` | ``shadow``.
``behavior_change`` stays false; a true value is ignored.

Primary ``source`` plus optional ``also``:
position wins over every overlay; otherwise the first matching overlay in the
existing feeder order; an unmapped feeder becomes ``discovery`` (never blank).
Lane is ``observe`` or ``trade``, taken from the lists the builders already
returned.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Iterable

from logger import log

# Visibility enum. ``observe_sensor`` and ``missed`` are recognized if a feeder
# already labels them; this module does not create those memberships (#595/#597).
KNOWN_SOURCES = frozenset({
    "base",
    "position",
    "cmc_trending",
    "gainer_prev",
    "gainer_continuation",
    "discovery",
    "observe_sensor",
    "missed",
})
LANES = frozenset({"observe", "trade"})

# Existing builder labels → visibility source. Anything else is discovery.
_FEEDER_SOURCE = {
    "base": "base",
    "position": "position",
    "cmc_trending": "cmc_trending",
    "gate_prev_top": "gainer_prev",
    "gainer_prev": "gainer_prev",
    "gainer_continuation": "gainer_continuation",
    "discovery": "discovery",
    "observe_sensor": "observe_sensor",
    "missed": "missed",
}

_TENANT_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_MODES = frozenset({"off", "shadow"})


def _sym(coin: dict | None) -> str:
    if not isinstance(coin, dict):
        return ""
    return str(coin.get("symbol") or "").strip()


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def early_trend_config(config: dict | None = None) -> dict[str, Any]:
    """Parse ``universe_early_trend``. Missing section → mode off, no tags."""
    sec: dict[str, Any] = {}
    if isinstance(config, dict):
        raw = config.get("universe_early_trend")
        if isinstance(raw, dict):
            sec = raw
    mode = str(sec.get("mode") or "off").strip().lower()
    if mode not in _MODES:
        mode = "off"
    return {
        "mode": mode,
        "behavior_change": _as_bool(sec.get("behavior_change"), False),
    }


def canonical_source(raw: str | None) -> str:
    """Map a feeder label onto the visibility enum. Unknown → discovery."""
    key = str(raw or "").strip()
    if not key:
        return "discovery"
    return _FEEDER_SOURCE.get(key, "discovery")


def safe_tenant_id(tenant_id: str | None) -> str:
    raw = str(tenant_id or "default").strip() or "default"
    safe = _TENANT_SAFE.sub("_", raw)
    return safe or "default"


def members_artifact_path(tenant_id: str | None, *, data_root: str | None = None) -> str:
    """``data/{tenant_id}/universe_members.json`` (overwrite each cycle)."""
    if data_root is None:
        from data_manager import data_dir

        data_root = data_dir()
    return os.path.join(data_root, safe_tenant_id(tenant_id), "universe_members.json")


def index_overlay_sources(overlay_lists: Iterable[list] | None) -> dict[str, list[str]]:
    """symbol → raw feeder labels, first seen in list order wins the front slot."""
    out: dict[str, list[str]] = {}
    for lst in overlay_lists or []:
        for coin in lst or []:
            if not isinstance(coin, dict):
                continue
            sym = _sym(coin)
            if not sym:
                continue
            raw = str(coin.get("source") or "").strip()
            bucket = out.setdefault(sym, [])
            if raw not in bucket:
                bucket.append(raw)
    return out


def _canon_overlays(raws: list[str]) -> list[str]:
    """Overlay labels only. ``base`` and ``position`` are not overlay feeders."""
    out: list[str] = []
    for raw in raws:
        canon = canonical_source(raw)
        if canon in ("base", "position"):
            continue
        if canon not in out:
            out.append(canon)
    return out


def _fallback_raws(coins: list[dict]) -> list[str]:
    raws: list[str] = []
    for coin in coins:
        existing = str(coin.get("source") or "").strip()
        if not existing or existing in ("base", "position"):
            continue
        if existing not in raws:
            raws.append(existing)
    return raws


def tag_universe_members(
    observe: list[dict] | None,
    trade: list[dict] | None,
    *,
    base_symbols: set[str] | None = None,
    open_symbols: set[str] | None = None,
    overlays: list | None = None,
) -> list[dict]:
    """Tag the members already chosen by the observe/trade builders.

    Does not insert or remove symbols. ``source=base`` only when the symbol is
    in the base watchlist. An open position's primary source is ``position``.
    """
    base = {str(s).strip() for s in (base_symbols or set()) if str(s).strip()}
    opens = {str(s).strip() for s in (open_symbols or set()) if str(s).strip()}
    indexed = index_overlay_sources(overlays)
    trade_syms = {_sym(c) for c in (trade or []) if _sym(c)}

    grouped: dict[str, list[dict]] = {}
    order: list[str] = []
    for coin in list(observe or []) + list(trade or []):
        if not isinstance(coin, dict):
            continue
        sym = _sym(coin)
        if not sym:
            continue
        grouped.setdefault(sym, []).append(coin)
        if sym not in order:
            order.append(sym)

    members: list[dict] = []
    for sym in order:
        raws = list(indexed.get(sym) or [])
        if not raws:
            raws = _fallback_raws(grouped.get(sym) or [])
        mapped = _canon_overlays(raws)
        if sym in opens:
            primary = "position"
            also = list(mapped)
            if sym in base and "base" not in also:
                also.append("base")
        elif mapped:
            primary = mapped[0]
            also = mapped[1:]
            if sym in base and "base" not in also:
                also.append("base")
        elif sym in base:
            primary = "base"
            also = []
        else:
            primary = "discovery"
            also = []
        if primary not in KNOWN_SOURCES:
            primary = "discovery"
        lane = "trade" if sym in trade_syms else "observe"
        members.append({
            "symbol": sym,
            "lane": lane,
            "source": primary,
            "also": also,
        })
    return members


def member_counts(members: list[dict] | None) -> dict[str, int]:
    rows = list(members or [])
    return {
        "members": len(rows),
        "observe": sum(1 for m in rows if m.get("lane") == "observe"),
        "trade": sum(1 for m in rows if m.get("lane") == "trade"),
        "tagged": sum(
            1
            for m in rows
            if str(m.get("source") or "").strip() and str(m.get("lane") or "").strip()
        ),
    }


def format_cycle_log_line(member: dict) -> str:
    also = member.get("also") or []
    also_s = ",".join(str(s) for s in also if s) or "-"
    return (
        f"universe_early_trend symbol={member.get('symbol')} "
        f"lane={member.get('lane')} source={member.get('source')} also={also_s}"
    )


def format_counts_line(members: list[dict] | None, *, errors: int = 0) -> str:
    """One INFO line per cycle. ``errors`` counts feeder failures, not members."""
    counts = member_counts(members)
    return (
        "universe_early_trend counts "
        f"members={counts['members']} observe={counts['observe']} "
        f"trade={counts['trade']} tagged={counts['tagged']} "
        f"errors={int(errors)}"
    )


def collect_overlay_lists(config: dict | None) -> tuple[list[list[dict]], int]:
    """Overlay coin lists in the order the existing membership builders apply them.

    Watchlist merge order (``build_merged_watchlist_coins``): expansion, dry-run
    overlay, CMC trending. Gainer inject then writes eligible-expand ``source``
    over live-top, so expand is listed before live-top — that is the source the
    builder keeps, not a new ranking.

    These feeders are process-shared, same as the membership builders. They do
    not take a tenant id:

    * ``data/watchlist.dry_run_expansion.json`` (``load_dry_run_expansion``)
    * ``data/watchlist.dry_run_overlay.json`` (``load_dry_run_overlay``)
    * ``data/watchlist.cmc_trending_overlay.json`` (``load_cmc_trending_overlay``)
    * ``gainer_universe_state.json`` via ``load_gainer_state``
      (``GAINER_UNIVERSE_STATE_PATH``, else ``/app/logs/``, else ``data/``)

    The base watchlist is the tenant-scoped input (``load_watchlist(tenant_id)``).
    Returns ``(lists, error_count)``. A feeder exception is a WARNING and does
    not raise.
    """
    cfg = config if isinstance(config, dict) else {}
    lists: list[list[dict]] = []
    errors = 0
    try:
        from data_manager import (
            is_dry_run_enhanced,
            load_cmc_trending_overlay,
            load_dry_run_expansion,
            load_dry_run_overlay,
            trending_watchlist_live_enabled,
            uses_watchlist_expansion,
        )
        from core.coin_eligibility import should_include_trending_overlay

        if uses_watchlist_expansion(cfg):
            lists.append(list((load_dry_run_expansion() or {}).get("coins") or []))
        if is_dry_run_enhanced(cfg):
            lists.append(list((load_dry_run_overlay() or {}).get("coins") or []))
        if trending_watchlist_live_enabled(cfg) and should_include_trending_overlay(cfg):
            lists.append(list((load_cmc_trending_overlay() or {}).get("coins") or []))
    except Exception as e:
        errors += 1
        log(f"universe_early_trend watchlist feeders skip: {e}", "WARNING")

    try:
        from services.gainer_universe.config import gainer_universe_config, gainer_universe_enabled
        from services.gainer_universe.inject import expand_candidates_for_trade
        from services.gainer_universe.store import load_gainer_state

        if gainer_universe_enabled(cfg):
            state = load_gainer_state() or {}
            gcfg = gainer_universe_config(cfg)
            lists.append(list(expand_candidates_for_trade(state, gcfg) or []))
            live: list[dict] = []
            for row in state.get("live_top") or []:
                if not isinstance(row, dict) or not row.get("symbol"):
                    continue
                live.append({
                    "symbol": row.get("symbol"),
                    "source": row.get("source") or "gainer_live_top",
                })
            lists.append(live)
    except Exception as e:
        errors += 1
        log(f"universe_early_trend gainer feeders skip: {e}", "WARNING")
    return lists, errors


def _base_symbols_from_watchlist(tenant_id: str | None) -> tuple[set[str], int]:
    try:
        from data_manager import load_watchlist

        coins = load_watchlist(tenant_id=tenant_id) or []
    except Exception as e:
        log(f"universe_early_trend base set skip: {e}", "WARNING")
        return set(), 1
    out: set[str] = set()
    for coin in coins:
        if isinstance(coin, dict) and coin.get("symbol"):
            sym = str(coin.get("symbol") or "").strip()
            if sym:
                out.add(sym)
    return out, 0


def _open_symbols_from_positions(positions: Iterable | None) -> set[str]:
    out: set[str] = set()
    for pos in positions or []:
        if isinstance(pos, dict):
            raw = pos.get("symbol")
        else:
            raw = getattr(pos, "symbol", None)
        sym = str(raw or "").strip()
        if sym:
            out.add(sym)
    return out


def publish_cycle_tags(
    observe: list[dict] | None,
    trade: list[dict] | None,
    *,
    open_positions: list | None = None,
    open_symbols: set[str] | None = None,
    base_symbols: set[str] | None = None,
    config: dict | None = None,
    tenant_id: str | None = None,
    data_root: str | None = None,
    overlays: list | None = None,
) -> list[dict] | None:
    """Log tags and overwrite the tenant artifact. No-op when mode is not shadow.

    Never mutates ``observe`` or ``trade``. ``behavior_change`` does not filter
    or reorder; tags are visibility only.
    """
    cfg = early_trend_config(config)
    if cfg["mode"] != "shadow":
        return None
    try:
        from core.tenant_context import resolve_tenant_id

        tid = resolve_tenant_id(tenant_id)
        if cfg["behavior_change"]:
            log(
                "universe_early_trend behavior_change ignored; visibility only",
                "DEBUG",
            )
        opens = (
            {str(s).strip() for s in open_symbols if str(s).strip()}
            if open_symbols is not None
            else _open_symbols_from_positions(open_positions)
        )
        if base_symbols is not None:
            base = {str(s).strip() for s in base_symbols if str(s).strip()}
            base_errors = 0
        else:
            base, base_errors = _base_symbols_from_watchlist(tid)
        if overlays is not None:
            overlay_lists = overlays
            feeder_errors = 0
        else:
            overlay_lists, feeder_errors = collect_overlay_lists(config)
        errors = int(base_errors) + int(feeder_errors)
        members = tag_universe_members(
            observe,
            trade,
            base_symbols=base,
            open_symbols=opens,
            overlays=overlay_lists,
        )
        for member in members:
            log(format_cycle_log_line(member), "DEBUG")
        log(format_counts_line(members, errors=errors), "INFO")
        payload = {
            "tenant_id": tid,
            "mode": "shadow",
            "behavior_change": False,
            "members": members,
            "counts": {**member_counts(members), "errors": errors},
        }
        path = members_artifact_path(tid, data_root=data_root)
        from data_manager import atomic_write_json

        atomic_write_json(path, payload)
        return members
    except Exception as e:
        log(f"universe_early_trend tag skip: {e}", "WARNING")
        return None


def list_formatter_tags(
    config: dict | None = None,
    *,
    tenant_id: str | None = None,
    data_root: str | None = None,
) -> dict[str, dict] | None:
    """Optional ``/list`` lookup. ``None`` when mode is off or no artifact yet."""
    if config is None:
        try:
            from data_manager import get_config

            config = get_config()
        except Exception:
            return None
    if early_trend_config(config)["mode"] != "shadow":
        return None
    try:
        from core.tenant_context import resolve_tenant_id

        path = members_artifact_path(resolve_tenant_id(tenant_id), data_root=data_root)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return None
    out: dict[str, dict] = {}
    for member in (data or {}).get("members") or []:
        if not isinstance(member, dict):
            continue
        sym = str(member.get("symbol") or "").strip()
        source = str(member.get("source") or "").strip()
        lane = str(member.get("lane") or "").strip()
        if sym and source and lane in LANES:
            out[sym] = member
    return out or None
