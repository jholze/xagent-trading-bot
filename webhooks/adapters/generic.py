from __future__ import annotations

from typing import Any

from webhooks.schemas import (
    ExternalSignal,
    clamp_strength,
    normalize_event_type,
    normalize_symbol,
    utc_now_iso,
)


def _first_symbol(data: dict) -> str:
    for key in ("symbol", "ticker", "pair", "coin", "asset"):
        if data.get(key):
            return normalize_symbol(str(data[key]))
    symbols = data.get("symbols")
    if isinstance(symbols, list) and symbols:
        return normalize_symbol(str(symbols[0]))
    if isinstance(symbols, str):
        return normalize_symbol(symbols.split(",")[0])
    return ""


def parse_generic(body: dict | str | None, *, source: str = "generic") -> ExternalSignal | None:
    if body is None:
        return None
    if isinstance(body, str):
        sym = normalize_symbol(body.split()[0] if body.split() else body)
        if not sym:
            return None
        return ExternalSignal(
            source=source or "generic",
            symbol=sym,
            event_type="generic",
            strength=0.5,
            timestamp=utc_now_iso(),
            raw={"text": body},
        )
    if not isinstance(body, dict):
        return None

    data: dict[str, Any] = dict(body)
    nested = data.get("data") or data.get("payload") or data.get("alert")
    if isinstance(nested, dict):
        data = {**nested, **{k: v for k, v in data.items() if k not in nested}}

    symbol = _first_symbol(data)
    if not symbol:
        return None

    event_type = normalize_event_type(
        data.get("event_type") or data.get("event") or data.get("type") or data.get("alert_type")
    )
    raw_strength = data.get("strength")
    if raw_strength is None and data.get("score") is not None:
        raw_strength = data.get("score")
    if raw_strength is None and data.get("confidence") is not None:
        try:
            raw_strength = float(data.get("confidence")) / 100.0
        except (TypeError, ValueError):
            raw_strength = 0.5
    strength = clamp_strength(raw_strength if raw_strength is not None else 0.5)
    ts = str(data.get("timestamp") or data.get("time") or utc_now_iso())
    src = str(data.get("source") or source or "generic").lower()

    return ExternalSignal(
        source=src,
        symbol=symbol,
        event_type=event_type,
        strength=strength,
        timestamp=ts,
        raw=data,
    )