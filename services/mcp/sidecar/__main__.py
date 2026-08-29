"""Entrypoint: FastMCP sidecar + /health (no trading bot cycles).

  python -m services.mcp.sidecar

MCP streamable HTTP: /mcp
Health: GET /health
Port: PORT (default 8080)
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def main() -> None:
    from logger import log
    from services.mcp.sidecar.app import MCP_PATH, bootstrap_env, create_app, listen_port

    bootstrap_env()
    port = listen_port()
    app = create_app(host="0.0.0.0", port=port)
    log(
        f"=== xagent-mcp sidecar start host=0.0.0.0 port={port} mcp={MCP_PATH} ===",
        "INFO",
    )
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
