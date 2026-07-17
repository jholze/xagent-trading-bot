"""Export memory collections to JSONL (ops / backup)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from intelligence.memory.store import (
    COL_EVENTS,
    COL_LESSONS,
    COL_PROFILES,
    COL_TRADES,
    MemoryStore,
)
from logger import log


def export_jsonl(out_dir: str | Path | None = None, store: MemoryStore | None = None) -> str:
    store = store or MemoryStore()
    root = Path(out_dir or os.environ.get("MEMORY_EXPORT_DIR") or "logs/memory_export")
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = root / f"memory_{stamp}.jsonl"
    n = 0
    try:
        db = store.db
        with out_path.open("w", encoding="utf-8") as f:
            for col in (COL_PROFILES, COL_EVENTS, COL_TRADES, COL_LESSONS):
                for doc in db[col].find({}).limit(5000):
                    doc["_collection"] = col
                    if "_id" in doc:
                        doc["_id"] = str(doc["_id"])
                    f.write(json.dumps(doc, default=str) + "\n")
                    n += 1
        log(f"memory export {n} docs → {out_path}", "INFO")
    except Exception as e:
        log(f"memory export failed: {e}", "WARNING")
        # still create empty marker
        out_path.write_text(f'{{"error": "{e}"}}\n', encoding="utf-8")
    return str(out_path)
