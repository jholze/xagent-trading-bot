"""Hermes entry: sync macro calendar + sessions + Polymarket → memory + snapshot."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from intelligence.macro.btc_correlation import compute_btc_correlation, impact_score
from intelligence.macro.calendar import (
    EVENT_CODES,
    MacroEvent,
    active_windows,
    fetch_alpha_vantage_calendar_stub,
    fetch_fred_calendar_stub,
    load_fixture_events,
    normalize_event_code,
)
from intelligence.macro.config import (
    calendar_risk_config,
    macro_config,
    macro_enabled,
    polymarket_config,
    sessions_config,
)
from intelligence.macro.polymarket import (
    fetch_polymarket_live,
    load_fixture_markets,
    mispricing_score,
)
from intelligence.macro.regime_rules import apply_regime_rules
from intelligence.macro.session_clock import session_status
from intelligence.macro.snapshot import publish_macro_snapshot
from intelligence.memory.event_ingest import make_event_id
from intelligence.memory.embeddings import embed_event
from intelligence.memory.models import MarketEvent, utc_now_iso
from intelligence.memory.store import MemoryStore
from logger import log


def _ingest_event(
    store: MemoryStore,
    *,
    source: str,
    event_type: str,
    description: str,
    impact: float,
    symbols: list[str] | None = None,
    metadata: dict | None = None,
    stable_key: str = "",
    refresh: bool = False,
) -> bool:
    """Write event. If refresh=True, update description/embedding when card already exists."""
    eid = make_event_id(source, stable_key or f"{event_type}|{description[:60]}")
    desc = (description or "")[:500]
    existing = store.get_event(eid)
    if existing and not refresh:
        return False
    if existing and refresh and (existing.description or "") == desc:
        return False
    ev = MarketEvent(
        event_id=eid,
        timestamp=(existing.timestamp if existing and existing.timestamp else utc_now_iso()),
        event_type=event_type,
        symbols=symbols or (list(existing.symbols) if existing and existing.symbols else ["BTC/USDT"]),
        impact_score=max(-1.0, min(1.0, float(impact))),
        description=desc,
        source=source,
        metadata={**(existing.metadata if existing and existing.metadata else {}), **dict(metadata or {})},
        embedding=embed_event(desc, event_type=event_type),
    )
    return bool(store.upsert_event(ev))


def _load_calendar_events(cfg: dict, inject: list[MacroEvent] | None) -> list[MacroEvent]:
    if inject is not None:
        return list(inject)
    events: list[MacroEvent] = []
    # fixtures always available offline
    events.extend(load_fixture_events())
    try:
        events.extend(fetch_fred_calendar_stub())
    except Exception:
        pass
    try:
        events.extend(fetch_alpha_vantage_calendar_stub())
    except Exception:
        pass
    allow = cfg.get("events_allowlist") or ["FOMC", "NFP", "CPI"]
    allow_u = {normalize_event_code(x) for x in allow}
    out = []
    for e in events:
        code = normalize_event_code(e.event_code)
        if allow_u and code not in allow_u and e.event_code not in allow_u:
            # keep if code is in EVENT_CODES and allow empty
            if code not in EVENT_CODES:
                continue
            if allow_u and code not in allow_u:
                continue
        e.event_code = code
        out.append(e)
    return out


def _load_btc_bars_optional() -> list[dict]:
    """Best-effort BTC bars for correlation — fail-open empty."""
    try:
        from pathlib import Path
        import json

        p = Path(__file__).resolve().parent / "data" / "btc_bars.json"
        if not p.is_file():
            p = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "macro" / "btc_bars.json"
        if p.is_file():
            return list(json.loads(p.read_text()).get("bars") or [])
    except Exception:
        pass
    return []


def sync_macro_context(
    store: MemoryStore | None = None,
    *,
    config: dict | None = None,
    now: datetime | None = None,
    calendar_events: list[MacroEvent] | None = None,
    pm_markets: list | None = None,
    volume_proxy: float | None = None,
    volume_baseline: float | None = None,
    btc_bars: list[dict] | None = None,
    btc_ret_24h: float | None = None,
    fusion_regime: str | None = None,
) -> dict[str, Any]:
    """Full macro sync for Hermes cycle — ledger-safe, fail-open."""
    store = store or MemoryStore()
    if not macro_enabled(config):
        return {"enabled": False}

    now = now or datetime.now(timezone.utc)
    mcfg = macro_config(config)
    scfg = sessions_config(config)
    pcfg = polymarket_config(config)
    crcfg = calendar_risk_config(config)

    counts = {
        "macro_events": 0,
        "session_events": 0,
        "pm_events": 0,
        "corr_codes": 0,
        "windows_active": 0,
    }

    # --- sessions ---
    windows = {}
    for name in ("asia", "london", "ny"):
        if name in scfg and isinstance(scfg[name], (list, tuple)):
            windows[name] = scfg[name]
    sess = session_status(
        now,
        windows=windows or None,
        volume_proxy=volume_proxy,
        volume_baseline=volume_baseline,
        low_volume_pctile=float(scfg.get("low_volume_pctile", 30)),
        fakeout_size_hint=float(scfg.get("fakeout_size_mult", 0.5)),
    )
    if scfg.get("enabled", True):
        # session open edge: emit once per hour bucket when open
        for name, is_open in (
            ("asia", sess.asia_open),
            ("london", sess.london_open),
            ("ny", sess.ny_open),
        ):
            if not is_open:
                continue
            if _ingest_event(
                store,
                source="session",
                event_type="session_open",
                description=f"session_open {name} as_of={sess.as_of}",
                impact=0.0,
                metadata={"session": name, **sess.as_dict()},
                stable_key=f"open|{name}|{sess.as_of[:13]}",
            ):
                counts["session_events"] += 1

    # --- calendar ---
    events = _load_calendar_events(mcfg, calendar_events) if mcfg.get("enabled", True) else []
    pre_w = list(mcfg.get("pre_windows_min") or [1440, 240, 60, 15])
    post_w = list(mcfg.get("post_windows_min") or [5, 60])
    min_hist = int(crcfg.get("min_hist_samples", 8))

    # correlation per code (fixture bars)
    bars = btc_bars if btc_bars is not None else _load_btc_bars_optional()
    corr_by_code: dict[str, Any] = {}
    by_code_times: dict[str, list] = {}
    for e in events:
        by_code_times.setdefault(e.event_code, []).append(e.scheduled_at)
    for code, times in by_code_times.items():
        summary = compute_btc_correlation(
            code, times, bars, min_samples=1 if bars else 999
        )
        corr_by_code[code] = summary
        if summary.sample_n > 0:
            counts["corr_codes"] += 1

    next_event = None
    next_hours = None
    in_pre = False
    high_impact = False
    cal_summary = ""
    calendar_mult = 1.0

    for e in events:
        if mcfg.get("enabled", True) is False:
            break
        corr = corr_by_code.get(e.event_code)
        imp = impact_score(e, corr, min_samples=min_hist if corr else 999)
        # scheduled event card — rich text for retrieval (FOMC/Fed/calendar needles)
        code = str(e.event_code or "").upper()
        title = (e.title or "").strip()
        fed_hint = "Fed rate decision " if code in ("FOMC", "FED") else ""
        sched_desc = (
            f"{code} {fed_hint}macro calendar scheduled {e.scheduled_at} {title} "
            f"pre/post window high-impact"
        ).strip()
        if _ingest_event(
            store,
            source=e.source or "macro",
            event_type="macro_scheduled",
            description=sched_desc,
            impact=imp,
            metadata={
                "event_code": e.event_code,
                "scheduled_at": e.scheduled_at,
                "importance": e.importance,
                "corr": corr.as_dict() if corr else None,
            },
            stable_key=f"sched|{e.event_code}|{e.scheduled_at[:16]}",
            refresh=True,
        ):
            counts["macro_events"] += 1

        for w in active_windows(e, now, pre_windows_min=pre_w, post_windows_min=post_w):
            counts["windows_active"] += 1
            et = (
                "macro_print"
                if w.get("kind") == "print"
                else "macro_window"
            )
            kind = w.get("kind")
            win_desc = (
                f"{code} macro calendar {kind} window {w.get('bucket')} "
                f"{'in' if kind == 'pre' else 'ago'} "
                f"{w.get('minutes_to_event') or w.get('minutes_since_event')}m "
                f"{fed_hint}".strip()
            )
            if _ingest_event(
                store,
                source=e.source or "macro",
                event_type=et,
                description=win_desc,
                impact=imp,
                metadata={**w, "importance": e.importance},
                stable_key=f"win|{e.event_code}|{e.scheduled_at[:13]}|{w.get('bucket')}",
                refresh=True,
            ):
                counts["macro_events"] += 1
            if w.get("kind") == "pre":
                in_pre = True
                if e.importance == "high" or e.event_code in ("CPI", "NFP", "FOMC"):
                    high_impact = True
                    calendar_mult = min(
                        calendar_mult,
                        float(crcfg.get("size_mult_pre_high_impact", 0.5)),
                    )
                    cal_summary = (
                        f"pre {e.event_code} {w.get('minutes_to_event')}m "
                        f"imp={imp:.2f}"
                    )

        # track next future event
        from intelligence.macro.calendar import parse_iso

        sdt = parse_iso(e.scheduled_at)
        if sdt and sdt > now.replace(tzinfo=sdt.tzinfo):
            hrs = (sdt - now.replace(tzinfo=sdt.tzinfo)).total_seconds() / 3600.0
            if next_hours is None or hrs < next_hours:
                next_hours = hrs
                next_event = e.event_code

    # --- regime rules ---
    regime = apply_regime_rules(
        sess,
        in_macro_pre_window=in_pre,
        macro_event_code=next_event,
        high_impact=high_impact,
        config={**scfg, **crcfg},
    )
    # merge calendar mult from windows into regime
    regime["calendar_mult"] = min(
        float(regime.get("calendar_mult") or 1.0), calendar_mult
    )
    if regime.get("tags") and scfg.get("enabled", True):
        if _ingest_event(
            store,
            source="session",
            event_type="session_regime",
            description=f"session_regime {regime.get('regime')} tags={regime.get('tags')}",
            impact=-0.2 if regime.get("fakeout_risk", 0) >= 0.5 else 0.0,
            metadata=regime,
            stable_key=f"regime|{regime.get('regime')}|{sess.as_of[:13]}",
        ):
            counts["session_events"] += 1

    # --- polymarket ---
    pm_mult = 1.0
    pm_summary = ""
    pm_payload: dict[str, Any] = {"markets": [], "mispricing_score": 0.0}
    if pcfg.get("enabled", True):
        markets = pm_markets
        if markets is None:
            markets = load_fixture_markets()
            ids = list(pcfg.get("market_ids") or [])
            if ids:
                live = fetch_polymarket_live(ids)
                if live:
                    markets = live
        thr = float(pcfg.get("mispricing_delta_pp", 10))
        max_score = 0.0
        for m in markets or []:
            mid = getattr(m, "market_id", None) or (m.get("market_id") if isinstance(m, dict) else "")
            title = getattr(m, "title", None) or (m.get("title") if isinstance(m, dict) else "")
            prob = float(getattr(m, "prob", None) if not isinstance(m, dict) else m.get("prob") or 0.5)
            prev = getattr(m, "prev_prob", None) if not isinstance(m, dict) else m.get("prev_prob")
            ms = mispricing_score(
                prob,
                prev_prob=prev,
                btc_ret=btc_ret_24h,
                delta_pp_threshold=thr,
                fusion_regime=fusion_regime,
            )
            max_score = max(max_score, float(ms.get("score") or 0))
            pm_payload["markets"].append(
                {
                    "id": mid,
                    "title": title,
                    "prob": prob,
                    "delta_pp": ms.get("delta_pp"),
                    "mispricing": ms,
                }
            )
            if abs(float(ms.get("delta_pp") or 0)) >= thr:
                if _ingest_event(
                    store,
                    source="polymarket",
                    event_type="pm_prob_move",
                    description=f"PM {title[:80]} prob={prob:.2f} Δ={ms.get('delta_pp')}pp",
                    impact=-0.15 if ms.get("flag") else 0.05,
                    metadata={"market_id": mid, **ms},
                    stable_key=f"pm_move|{mid}|{utc_now_iso()[:13]}",
                ):
                    counts["pm_events"] += 1
            if ms.get("flag"):
                if _ingest_event(
                    store,
                    source="polymarket",
                    event_type="pm_mispricing",
                    description=f"PM mispricing {title[:60]} score={ms.get('score')}",
                    impact=-0.25,
                    metadata={"market_id": mid, **ms},
                    stable_key=f"pm_mis|{mid}|{utc_now_iso()[:10]}",
                ):
                    counts["pm_events"] += 1
                pm_mult = min(pm_mult, 0.85)
                pm_summary = f"mispricing score={ms.get('score')}"
        pm_payload["mispricing_score"] = max_score
        pm_payload["summary"] = pm_summary
        pm_payload["as_of"] = utc_now_iso()

    cal_mult_f = float(regime.get("calendar_mult", calendar_mult) or 1.0)
    sess_mult_f = float(regime.get("session_mult", 1.0) or 1.0)
    pm_mult_f = float(pm_mult or 1.0)

    # P3: when size mults deviate from 1.0, always write a fresh *pressure* event
    # (hour-bucketed) so retrieval/RAG see active risk even if scheduled cards
    # were already upserted earlier with the same stable key.
    hour_bucket = utc_now_iso()[:13]
    if abs(cal_mult_f - 1.0) > 0.02:
        if _ingest_event(
            store,
            source="macro",
            event_type="macro_pressure",
            description=(
                f"macro_pressure calendar_mult={cal_mult_f:.2f} "
                f"next={next_event} hours={round(next_hours, 2) if next_hours is not None else 'n/a'} "
                f"{cal_summary or ''}"
            ).strip(),
            impact=max(-1.0, min(1.0, (1.0 - cal_mult_f))),
            metadata={
                "calendar_mult": cal_mult_f,
                "next_event": next_event,
                "hours_to_event": next_hours,
                "high_impact": high_impact,
                "in_pre_window": in_pre,
            },
            stable_key=f"pressure|cal|{hour_bucket}|{next_event or 'none'}",
        ):
            counts["macro_events"] += 1
            counts["pressure_events"] = int(counts.get("pressure_events") or 0) + 1
    if abs(sess_mult_f - 1.0) > 0.02:
        if _ingest_event(
            store,
            source="session",
            event_type="session_pressure",
            description=(
                f"session_pressure session_mult={sess_mult_f:.2f} "
                f"regime={regime.get('regime')} tags={regime.get('tags')}"
            ),
            impact=max(-1.0, min(1.0, (1.0 - sess_mult_f))),
            metadata={
                "session_mult": sess_mult_f,
                "regime": regime,
                "session": sess.as_dict(),
            },
            stable_key=f"pressure|sess|{hour_bucket}|{regime.get('regime')}",
        ):
            counts["session_events"] += 1
            counts["pressure_events"] = int(counts.get("pressure_events") or 0) + 1
    if abs(pm_mult_f - 1.0) > 0.02:
        if _ingest_event(
            store,
            source="polymarket",
            event_type="pm_pressure",
            description=(
                f"pm_pressure pm_mult={pm_mult_f:.2f} {pm_summary or ''}"
            ).strip(),
            impact=max(-1.0, min(1.0, (1.0 - pm_mult_f))),
            metadata={"pm_mult": pm_mult_f, "pm": pm_payload},
            stable_key=f"pressure|pm|{hour_bucket}",
        ):
            counts["pm_events"] += 1
            counts["pressure_events"] = int(counts.get("pressure_events") or 0) + 1

    snap = {
        "session": sess.as_dict(),
        "calendar": {
            "next_event": next_event,
            "hours_to_event": round(next_hours, 3) if next_hours is not None else None,
            "in_pre_window": in_pre,
            "high_impact": high_impact,
            "summary": cal_summary,
            "corr": {k: v.as_dict() for k, v in corr_by_code.items()},
        },
        "regime": regime,
        "pm": pm_payload,
        "calendar_mult": cal_mult_f,
        "session_mult": sess_mult_f,
        "pm_mult": pm_mult_f,
        "fakeout_risk": regime.get("fakeout_risk", sess.fakeout_risk),
        "counts": counts,
        "as_of": utc_now_iso(),
    }
    publish_macro_snapshot(snap)
    log(
        f"memory macro sync: {counts} cal_mult={snap['calendar_mult']} "
        f"sess_mult={snap['session_mult']} pm_mult={snap['pm_mult']}",
        "INFO",
    )
    return {
        "enabled": True,
        **counts,
        "calendar_mult": snap["calendar_mult"],
        "session_mult": snap["session_mult"],
        "pm_mult": pm_mult_f,
        "next_event": next_event,
        "hours_to_event": snap["calendar"].get("hours_to_event"),
        "fakeout_risk": snap["fakeout_risk"],
        "regime": regime.get("regime"),
    }
