"""Compat: ``python -m services.mcp_sidecar`` → ``services.mcp.sidecar``."""

from services.mcp.sidecar.__main__ import main

if __name__ == "__main__":
    main()
