"""Pure metadata → lobe + neon color (no I/O)."""

from __future__ import annotations

from typing import Any

# Neon night-desk palette (RGB 0–1 for Three.js)
LOBE_COLORS: dict[str, tuple[float, float, float]] = {
    "coin_facts": (0.13, 0.83, 0.93),  # cyan
    "trades": (0.96, 0.45, 0.71),  # pink
    "lessons": (0.66, 0.55, 0.98),  # violet
    "events": (0.98, 0.75, 0.14),  # amber
    "social": (0.29, 0.87, 0.50),  # green
    "other": (0.39, 0.45, 0.55),  # slate
}

LOBE_ORDER = ("coin_facts", "trades", "lessons", "events", "social", "other")

_COIN_FACT_SOURCES = (
    "cmc_pro_",
    "cmc_mcp_",
    "cmc_ai_",
    "coin_fact",
)
_TRADE_KEYS = ("trade", "fill", "pnl", "position_close", "execution")
_LESSON_KEYS = ("lesson", "dca_lesson", "reflector", "reflection")
_EVENT_KEYS = ("event", "regime", "fusion", "market_context", "macro")
_SOCIAL_KEYS = ("social", "community", "twitter", "telegram_post")


def _haystack(metadata: dict[str, Any] | None) -> str:
    meta = metadata or {}
    parts = [
        str(meta.get("lobe") or ""),
        str(meta.get("type") or ""),
        str(meta.get("kind") or ""),
        str(meta.get("source") or ""),
        str(meta.get("event_type") or ""),
        str(meta.get("category") or ""),
    ]
    return " ".join(parts).lower()


def classify_lobe(metadata: dict[str, Any] | None) -> str:
    """Map chunk metadata to a visual lobe key."""
    meta = metadata or {}
    explicit = str(meta.get("lobe") or "").strip().lower()
    if explicit in LOBE_COLORS:
        return explicit

    h = _haystack(meta)
    source = str(meta.get("source") or "").lower()
    kind = str(meta.get("kind") or meta.get("type") or "").lower()

    if kind in ("coin_fact", "coin_facts") or any(source.startswith(p) for p in _COIN_FACT_SOURCES):
        return "coin_facts"
    if any(k in h for k in _TRADE_KEYS):
        return "trades"
    if any(k in h for k in _LESSON_KEYS):
        return "lessons"
    if any(k in h for k in _EVENT_KEYS):
        return "events"
    if any(k in h for k in _SOCIAL_KEYS):
        return "social"
    return "other"


def lobe_color(lobe: str) -> list[float]:
    rgb = LOBE_COLORS.get(lobe) or LOBE_COLORS["other"]
    return [float(rgb[0]), float(rgb[1]), float(rgb[2])]


def lobe_legend() -> list[dict[str, Any]]:
    return [
        {"id": k, "label": k.replace("_", " "), "color": lobe_color(k)}
        for k in LOBE_ORDER
    ]
