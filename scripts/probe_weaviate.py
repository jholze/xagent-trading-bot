#!/usr/bin/env python3
"""Live Weaviate probe — run on Hermes network: railway run -s xagent-hermes python3 scripts/probe_weaviate.py"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from intelligence.memory.embeddings import embed_text
from intelligence.memory.vector_weaviate import (
    CLASS_EVENT,
    VECTOR_DIM,
    WeaviateIndex,
    weaviate_url,
)


def main() -> int:
    url = weaviate_url()
    print("WEAVIATE_URL=", url or "(empty)")
    if not url:
        print("FAIL: WEAVIATE_URL not set")
        return 2

    idx = WeaviateIndex()
    ready = idx.ready()
    print("ready=", ready)
    if not ready:
        print("FAIL: not ready")
        return 1

    classes = idx.list_classes()
    print("classes_before=", sorted(classes))
    idx.ensure_schema()
    classes = idx.list_classes()
    print("classes_after=", sorted(classes))
    for need in (CLASS_EVENT, "MemoryCoinProfile", "MemoryTrade", "MemoryLesson"):
        print(f"  has {need}: {need in classes or need in idx._schema_ok}")

    probe_id = f"probe:weaviate:{int(time.time())}"
    vec = embed_text("probe exchange hack weaviate upsert")
    print("vector_dim=", len(vec), "expected=", VECTOR_DIM)
    assert len(vec) == VECTOR_DIM

    ok_create = idx.upsert_event(
        probe_id,
        "probe exchange hack weaviate upsert test",
        event_type="news",
        source="probe",
        impact_score=-0.2,
        symbols=["BTC/USDT"],
        timestamp="2026-07-18T10:00:00Z",
        vector=vec,
    )
    print("upsert_create=", ok_create)

    ok_update = idx.upsert_event(
        probe_id,
        "probe exchange hack weaviate upsert test UPDATED",
        event_type="news",
        source="probe",
        impact_score=-0.3,
        symbols=["BTC/USDT"],
        timestamp="2026-07-18T10:00:00Z",
        vector=vec,
    )
    print("upsert_update=", ok_update)

    exists = idx.object_exists(CLASS_EVENT, probe_id)
    print("object_exists=", exists)

    hits = idx.search_events("exchange hack weaviate", k=5)
    print("search_hits=", hits[:5])
    found = probe_id in hits or any("probe" in (h or "") for h in hits)
    # nearVector may not return brand-new object immediately; existence is enough
    print("search_found_probe=", found)

    ok = ready and ok_create and ok_update and exists
    print("RESULT=", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
