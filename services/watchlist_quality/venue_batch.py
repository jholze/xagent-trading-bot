"""WQE-R12: attach Gate 24h quote volumes to candidates (fail-open)."""

from __future__ import annotations

from typing import Any


def attach_quote_volumes(
    coins: list[dict[str, Any]],
    *,
    batch_fn=None,
    config: dict | None = None,
) -> list[dict[str, Any]]:
    """Return copies of coins with quote_vol_24h filled when fetchable."""
    if not coins:
        return []
    symbols = [str(c["symbol"]) for c in coins if isinstance(c, dict) and c.get("symbol")]
    vol_map: dict[str, float] = {}
    try:
        if batch_fn is None:
            from services.venue_quality import fetch_gate_venue_metrics

            metrics = fetch_gate_venue_metrics(symbols, config_raw=config)
        else:
            metrics = batch_fn(symbols) or {}
        for sym, m in (metrics or {}).items():
            if m is None:
                continue
            qv = getattr(m, "quote_volume_24h_usdt", None)
            if qv is None and isinstance(m, dict):
                qv = m.get("quote_volume_24h_usdt")
            if qv is not None:
                try:
                    vol_map[str(sym)] = float(qv)
                except (TypeError, ValueError):
                    pass
    except Exception:
        pass

    out = []
    for c in coins:
        if not isinstance(c, dict):
            continue
        row = dict(c)
        sym = str(row.get("symbol") or "")
        if row.get("quote_vol_24h") is None and sym in vol_map:
            row["quote_vol_24h"] = vol_map[sym]
        out.append(row)
    return out
