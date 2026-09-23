"""Fetcher stubs for unit tests that must not hit Gate/CMC (#324, #563)."""

# Above the 20M stable long floor and the 100M short floor.
OFFLINE_MARKET_CAP_USD = 1_000_000_000

# Clears min_volume_to_order_multiple 20 for any order size the unit suite uses.
OFFLINE_VENUE_QUOTE_VOLUME_USDT = 1e12


def gate_prices_listed(symbols, *args, **kwargs):
    """Every requested symbol looks listed on Gate (dummy positive price)."""
    return {str(s): 1.0 for s in (symbols or [])}


def healthy_offline_venue_metrics(symbol="OFFLINE/USDT", *args, **kwargs):
    """Thick Gate book: capture ok, spread under 1.5%, both sides above $200.

    ``quote_volume_24h_usdt`` is at least 1e12 so ``min_volume_to_order_multiple``
    20 passes. Signature matches ``get_venue_metrics(symbol, *, config_raw, force)``.
    """
    from services.venue_quality import VenueMetrics

    return VenueMetrics(
        symbol=str(symbol or "OFFLINE/USDT"),
        quote_volume_24h_usdt=OFFLINE_VENUE_QUOTE_VOLUME_USDT,
        last=1.0,
        bid=0.9995,
        ask=1.0005,
        bid_size=1_000_000.0,
        ask_size=1_000_000.0,
        spread_pct=0.1,
        top_book_bid_usdt=1_000_000.0,
        top_book_ask_usdt=1_000_000.0,
        exchange="gate",
        capture="ok",
    )
