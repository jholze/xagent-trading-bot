"""Gainer Signal Service (WS-1) + bot consume helpers (WS-2)."""

__all__ = ["main"]


def main() -> None:
    from services.gainer_signal.__main__ import main as _main

    _main()
