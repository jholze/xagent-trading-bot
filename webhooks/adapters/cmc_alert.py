from __future__ import annotations

from webhooks.adapters.generic import parse_generic
from webhooks.schemas import ExternalSignal, normalize_event_type


def parse_cmc_alert(body: dict | str | None, *, source: str = "cmc") -> ExternalSignal | None:
    signal = parse_generic(body, source="cmc")
    if signal is None:
        return None
    signal.source = "cmc"
    raw_type = ""
    if isinstance(body, dict):
        raw_type = str(body.get("alert_type") or body.get("type") or body.get("category") or "")
    signal.event_type = normalize_event_type(raw_type or signal.event_type or "news_alert")
    if signal.event_type == "generic":
        signal.event_type = "news_alert"
    return signal