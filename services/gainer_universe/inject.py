"""Merge gainer candidates into observe/trade lists (no orders)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from services.gainer_universe.config import gainer_trade_expand_enabled, gainer_universe_config
from services.gainer_universe.filters import normalize_symbol
from services.gainer_universe.store import load_gainer_state


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        t = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _active_eligible(state: dict, cfg: dict, *, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    rows = list(state.get("eligible") or [])
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        sym = normalize_symbol(r.get("symbol") or "")
        if not sym:
            continue
        until = _parse_ts(r.get("eligible_until"))
        if until and until < now:
            continue
        out.append({**r, "symbol": sym})
    cap = int(cfg.get("expand_inject_max") or 40)
    return out[: max(1, cap)] if out else []


def expand_candidates_for_trade(
    state: dict | None = None,
    cfg: dict | None = None,
    *,
    now: datetime | None = None,
) -> list[dict]:
    """Coin dicts ready for trade universe merge."""
    cfg = cfg or gainer_universe_config()
    state = state if state is not None else load_gainer_state()
    coins = []
    for r in _active_eligible(state, cfg, now=now):
        coins.append(
            {
                "symbol": r["symbol"],
                "ticker": r["symbol"].split("/")[0],
                "active": True,
                "timeframe": "1h",
                "source": r.get("source") or "gate_prev_top",
                "gainer_rank": r.get("rank"),
                "gainer_day_ret": r.get("day_ret"),
                "gainer_day": r.get("day"),
                "eligible_until": r.get("eligible_until"),
                "strategy": "gainer_expand",
            }
        )
    return coins


def merge_gainers_into_observe(
    observe: list[dict],
    state: dict | None = None,
    cfg: dict | None = None,
) -> list[dict]:
    """Union live_top + eligible into observe (dedupe). Never drops existing."""
    cfg = cfg or gainer_universe_config()
    state = state if state is not None else load_gainer_state()
    by_sym: dict[str, dict] = {}
    for c in observe or []:
        if not isinstance(c, dict):
            continue
        s = normalize_symbol(c.get("symbol") or "")
        if s:
            by_sym[s] = dict(c)
            by_sym[s]["symbol"] = s

    # live top for observe visibility
    for r in state.get("live_top") or []:
        if not isinstance(r, dict):
            continue
        s = normalize_symbol(r.get("symbol") or "")
        if not s:
            continue
        if s not in by_sym:
            by_sym[s] = {
                "symbol": s,
                "ticker": s.split("/")[0],
                "active": True,
                "timeframe": "1h",
                "source": "gainer_live_top",
                "gainer_rank": r.get("rank"),
                "gainer_pct_24h": r.get("pct_24h"),
            }
        else:
            by_sym[s].setdefault("source", by_sym[s].get("source") or "gainer_live_top")
            by_sym[s]["gainer_pct_24h"] = r.get("pct_24h")

    for c in expand_candidates_for_trade(state, cfg):
        s = c["symbol"]
        if s not in by_sym:
            by_sym[s] = c
        else:
            by_sym[s]["source"] = c.get("source") or by_sym[s].get("source")
            by_sym[s]["gainer_rank"] = c.get("gainer_rank")
            by_sym[s]["gainer_day_ret"] = c.get("gainer_day_ret")
            by_sym[s]["eligible_until"] = c.get("eligible_until")

    return list(by_sym.values())


def merge_expand_into_trade(
    trade: list[dict],
    state: dict | None = None,
    cfg: dict | None = None,
    *,
    root_config: dict | None = None,
) -> list[dict]:
    """Force-include expand candidates when mode=trade_expand."""
    if root_config is not None:
        cfg = gainer_universe_config(root_config)
    else:
        cfg = cfg or gainer_universe_config()
    if not cfg.get("enabled") or str(cfg.get("mode") or "") != "trade_expand":
        return list(trade or [])

    state = state if state is not None else load_gainer_state()
    expand = expand_candidates_for_trade(state, cfg)
    if not expand:
        return list(trade or [])

    by_sym: dict[str, dict] = {}
    order: list[str] = []
    for c in trade or []:
        if not isinstance(c, dict):
            continue
        s = normalize_symbol(c.get("symbol") or "")
        if not s:
            continue
        if s not in by_sym:
            order.append(s)
        by_sym[s] = dict(c)
        by_sym[s]["symbol"] = s

    for c in expand:
        s = c["symbol"]
        if s not in by_sym:
            order.append(s)
            by_sym[s] = c
        else:
            # tag existing membership
            by_sym[s]["gainer_rank"] = c.get("gainer_rank")
            by_sym[s]["gainer_day_ret"] = c.get("gainer_day_ret")
            by_sym[s]["eligible_until"] = c.get("eligible_until")
            if not by_sym[s].get("source") or by_sym[s].get("source") in (
                "discovery",
                "trending",
            ):
                by_sym[s]["source"] = c.get("source")

    max_total = int(cfg.get("trade_max_with_expand") or 80)
    # keep all original trade first, then expand extras, then cap
    # Prefer never dropping pre-existing trade symbols under cap pressure:
    original = [normalize_symbol(c.get("symbol")) for c in (trade or []) if c.get("symbol")]
    original_set = set(original)
    kept = [by_sym[s] for s in order if s in original_set and s in by_sym]
    extras = [by_sym[s] for s in order if s not in original_set and s in by_sym]
    room = max(0, max_total - len(kept))
    result = kept + extras[:room]
    return result
