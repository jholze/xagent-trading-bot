#!/usr/bin/env python3
"""Railway entry for GIS daily monitor (one-shot + /health for deploy checks).

Starts a tiny Flask /health on $PORT so Railway healthchecks pass while the
monitor runs, then exits 0. Durable output goes to Mongo (gis_daily_monitor).
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _serve_health() -> None:
    try:
        from flask import Flask

        app = Flask("gis_monitor_health")

        @app.route("/health")
        def health():
            return "OK", 200

        port = int(os.environ.get("PORT") or "8080")
        app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)
    except Exception as e:
        print(f"gis_monitor health server failed: {e}", flush=True)


def main() -> int:
    t = threading.Thread(target=_serve_health, name="gis-health", daemon=True)
    t.start()

    day = os.environ.get("GIS_MONITOR_DAY") or "yesterday"
    top = os.environ.get("GIS_MONITOR_TOP") or "20"
    scope = os.environ.get("GIS_MONITOR_SCOPE") or "demo"
    out = os.environ.get("GIS_MONITOR_OUT_DIR") or "/tmp/gis_monitor"
    Path(out).mkdir(parents=True, exist_ok=True)
    argv = [
        "--day",
        day,
        "--top",
        str(top),
        "--scope",
        scope,
        "--out-dir",
        out,
        "--persist-mongo",
    ]
    print(f"=== GIS monitor railway entry argv={argv} ===", flush=True)
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "gis_daily_monitor", ROOT / "scripts" / "gis_daily_monitor.py"
    )
    if spec is None or spec.loader is None:
        print("cannot load gis_daily_monitor", flush=True)
        return 2
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    code = int(mod.main(argv) or 0)
    print(f"=== GIS monitor exit code={code} ===", flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
