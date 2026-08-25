"""xagent-mcp FastMCP sidecar (paper reads/writes, no price loop)."""

__all__ = ["main"]


def main() -> None:
    from services.mcp_sidecar.__main__ import main as _main

    _main()
