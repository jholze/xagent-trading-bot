"""Unconditional recency+impact aggregation of market-wide events.

Cheap: store.list_events only (no embeddings / similar_events). Fail-open to
a neutral zero dict on any error so callers never amplify from bad data.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

MACRO_EVENT_TYPES = ("macro_news", "structure_risk", "onchain_tvl_shock", "token_unlock")
_TYPE_MULT = {
    "macro_news": 1.0,
    "structure_risk": 1.3,
    "onchain_tvl_shock": 0.8,
    "token_unlock": 0.6,
}

_NEUTRAL: dict[str, Any] = {
    "bearish_score": 0.0,
    "confidence": 0.0,
    "event_count": 0,
    "top_events": [],
}

# In-process snapshot written by the background poller.
_CACHE: dict[str, Any] = {"result": None, "computed_at": 0.0}

_DEFAULT_MAX_AGE_SEC = 1800.0
_DEFAULT_SCALE = 2.5
_DEFAULT_CONFIDENCE_TARGET = 6.0


def _neutral() -> dict[str, Any]:
    return {
        "bearish_score": 0.0,
        "confidence": 0.0,
        "event_count": 0,
        "top_events": [],
    }


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        s = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s[:32] if len(s) > 32 and "+" not in s[19:25] else s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _field(ev: Any, name: str, default: Any = None) -> Any:
    if isinstance(ev, dict):
        return ev.get(name, default)
    return getattr(ev, name, default)


def _ct_section(config_raw: dict | None) -> dict:
    if not isinstance(config_raw, dict):
        return {}
    sp = config_raw.get("sell_policy")
    if isinstance(sp, dict) and isinstance(sp.get("correlated_tier"), dict):
        return sp["correlated_tier"]
    if isinstance(config_raw.get("correlated_tier"), dict):
        return config_raw["correlated_tier"]
    return config_raw


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def market_pulse_score(
    since_minutes: int = 30,
    config_raw: dict | None = None,
    store=None,
) -> dict:
    """Aggregate recent MACRO_EVENT_TYPES into a bearish-ness score.

    Returns bearish_score / confidence / event_count / top_events.
    Any exception (Mongo down, bad data, missing store) → neutral zeros.
    """
    try:
        since_minutes = max(1, int(since_minutes or 30))
        ct = _ct_section(config_raw)
        try:
            scale = float(ct.get("news_pulse_scale") if ct.get("news_pulse_scale") is not None else _DEFAULT_SCALE)
        except (TypeError, ValueError):
            scale = _DEFAULT_SCALE
        try:
            conf_target = float(
                ct.get("news_pulse_confidence_target_events")
                if ct.get("news_pulse_confidence_target_events") is not None
                else _DEFAULT_CONFIDENCE_TARGET
            )
        except (TypeError, ValueError):
            conf_target = _DEFAULT_CONFIDENCE_TARGET
        if conf_target <= 0:
            conf_target = _DEFAULT_CONFIDENCE_TARGET

        if store is None:
            from intelligence.memory.store import MemoryStore

            store = MemoryStore()

        now = datetime.now(timezone.utc)
        since_iso = (now - timedelta(minutes=since_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        half_life = float(since_minutes)

        events: list[Any] = []
        seen_ids: set[str] = set()
        for etype in MACRO_EVENT_TYPES:
            batch = store.list_events(
                symbol=None,
                event_type=etype,
                since_iso=since_iso,
                limit=50,
            )
            if not batch:
                continue
            for ev in batch:
                eid = str(_field(ev, "event_id", "") or "")
                if eid and eid in seen_ids:
                    continue
                if eid:
                    seen_ids.add(eid)
                events.append(ev)

        contribs: list[tuple[float, Any]] = []
        raw = 0.0
        for ev in events:
            ts = _parse_ts(_field(ev, "timestamp"))
            if ts is None:
                continue
            age_min = max(0.0, (now - ts.astimezone(timezone.utc)).total_seconds() / 60.0)
            decay = 0.5 ** (age_min / half_life)
            try:
                impact = float(_field(ev, "impact_score", 0.0) or 0.0)
            except (TypeError, ValueError):
                impact = 0.0
            etype = str(_field(ev, "event_type", "") or "")
            contrib = impact * decay * float(_TYPE_MULT.get(etype, 1.0))
            contribs.append((contrib, ev))
            raw += contrib

        n = len(contribs)
        avg = raw / max(1, n)
        bearish_score = _clamp(-avg * scale, 0.0, 1.0)
        confidence = min(1.0, n / conf_target)

        ranked = sorted(contribs, key=lambda pair: abs(pair[0]), reverse=True)[:3]
        top_events: list[dict[str, Any]] = []
        for _c, ev in ranked:
            top_events.append(
                {
                    "event_id": _field(ev, "event_id", ""),
                    "timestamp": _field(ev, "timestamp", ""),
                    "event_type": _field(ev, "event_type", ""),
                    "impact_score": _field(ev, "impact_score", 0.0),
                    "description": _field(ev, "description", ""),
                    "source": _field(ev, "source", ""),
                    "symbols": list(_field(ev, "symbols", []) or []),
                }
            )

        return {
            "bearish_score": float(bearish_score),
            "confidence": float(confidence),
            "event_count": int(n),
            "top_events": top_events,
        }
    except Exception:
        return _neutral()


def set_cached_market_pulse(result: dict | None) -> None:
    """Store a freshly computed pulse. Fail-open: never raise to callers."""
    try:
        _CACHE["result"] = dict(result) if isinstance(result, dict) else _neutral()
        _CACHE["computed_at"] = time.time()
    except Exception:
        _CACHE["result"] = _neutral()
        _CACHE["computed_at"] = time.time()


def get_cached_market_pulse(max_age_sec: float | None = None) -> dict:
    """Return cached pulse if fresh enough, else a neutral zero dict."""
    try:
        age_limit = _DEFAULT_MAX_AGE_SEC if max_age_sec is None else float(max_age_sec)
        result = _CACHE.get("result")
        computed_at = float(_CACHE.get("computed_at") or 0.0)
        if not isinstance(result, dict) or computed_at <= 0:
            return _neutral()
        if time.time() - computed_at > max(0.0, age_limit):
            return _neutral()
        return {
            "bearish_score": float(result.get("bearish_score") or 0.0),
            "confidence": float(result.get("confidence") or 0.0),
            "event_count": int(result.get("event_count") or 0),
            "top_events": list(result.get("top_events") or []),
        }
    except Exception:
        return _neutral()
