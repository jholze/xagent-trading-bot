"""Fetcher stubs for unit tests that must not hit Gate/CMC (#324)."""


def gate_prices_listed(symbols, *args, **kwargs):
    """Every requested symbol looks listed on Gate (dummy positive price)."""
    return {str(s): 1.0 for s in (symbols or [])}
