"""Complete venue_quality blocks for tests that mean a valid config.

Production has no fallback. These values are the old code defaults the
spec moved into config, plus ``book_unavailable_policy`` ``volume_ok``
which the deleted ``_DEFAULTS`` supplied when a fixture omitted it.
"""

from __future__ import annotations

from services.venue_quality import VenueQualityConfig


def venue_bounds() -> dict:
    """The five bound paths #692 reads from base config. Not a code fallback."""
    return {
        "order_book_timeout_sec_max": 10,
        "ticker_timeout_sec_max": 15,
        "cache_ttl_sec_min": 5,
        "cache_ttl_sec_max": 120,
        "order_book_limit_max": 100,
    }


def venue_block(**overrides) -> dict:
    block = {
        "enabled": True,
        "exchange": "gate",
        "min_quote_volume_24h_usdt": 50_000.0,
        "max_spread_pct": 1.5,
        "min_top_book_usdt_per_side": 200.0,
        "order_book_limit": 100,
        "order_book_timeout_sec": 5.0,
        "ticker_timeout_sec": 12,
        "cache_ttl_sec": 90.0,
        "book_unavailable_policy": "volume_ok",
        "on_fetch_error": "block_sensor",
        "min_volume_to_order_multiple": 20,
    }
    block.update(overrides)
    return block


def as_venue_config(block: dict | None = None) -> VenueQualityConfig:
    """Build a config object for ``evaluate_venue_quality``. Not a production fallback."""
    data = venue_block(**(block or {}))
    return VenueQualityConfig(
        enabled=bool(data["enabled"]),
        exchange=str(data["exchange"]),
        min_quote_volume_24h_usdt=float(data["min_quote_volume_24h_usdt"]),
        max_spread_pct=float(data["max_spread_pct"]),
        min_top_book_usdt_per_side=float(data["min_top_book_usdt_per_side"]),
        order_book_limit=int(data["order_book_limit"]),
        order_book_timeout_sec=float(data["order_book_timeout_sec"]),
        ticker_timeout_sec=float(data["ticker_timeout_sec"]),
        cache_ttl_sec=float(data["cache_ttl_sec"]),
        book_unavailable_policy=str(data["book_unavailable_policy"]),
        on_fetch_error=str(data["on_fetch_error"]),
        min_volume_to_order_multiple=float(data["min_volume_to_order_multiple"]),
    )
