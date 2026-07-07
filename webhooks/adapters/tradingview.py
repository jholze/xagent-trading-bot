from __future__ import annotations

from webhooks.adapters.generic import parse_generic
from webhooks.schemas import ExternalSignal, normalize_event_type, normalize_symbol, utc_now_iso


def parse_tradingview(body: dict | str | None, *, source: str = "tradingview") -> ExternalSignal | None:
    if isinstance(body, str):
        text = body.strip()
        upper = text.upper()
        event_type = "generic"
        if "VOLUME" in upper:
            event_type = "volume_spike"
        elif "BREAK" in upper or "BREAKOUT" in upper:
            event_type = "price_breakout"
        token = text.split()[0] if text else ""
        symbol = normalize_symbol(token.replace("USDT.P", "USDT").replace(".P", ""))
        if not symbol:
            return None
        return ExternalSignal(
            source="tradingview",
            symbol=symbol,
            event_type=event_type,
            strength=0.7,
            timestamp=utc_now_iso(),
            raw={"text": text},
        )

    signal = parse_generic(body, source="tradingview")
    if signal is None:
        return None

    msg = str((body or {}).get("message") or (body or {}).get("comment") or "").upper()
    if "VOLUME" in msg:
        signal.event_type = "volume_spike"
    elif "BREAK" in msg:
        signal.event_type = "price_breakout"
    else:
        signal.event_type = normalize_event_type(signal.event_type)
    signal.source = "tradingview"
    return signal