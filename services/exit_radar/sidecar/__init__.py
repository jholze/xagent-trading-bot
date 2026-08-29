"""Exit radar + Gate WS hub as a dedicated Railway process (no bot cycles)."""

__all__ = ["main"]


def main() -> None:
    from services.exit_radar.sidecar.__main__ import main as _main

    _main()
