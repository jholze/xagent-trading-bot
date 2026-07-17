"""Push market oracle snapshots to the trading bot."""

from __future__ import annotations

import logging
from typing import Any

import requests

log = logging.getLogger("market_oracle.publisher")


def publish_snapshot(
    snapshot: dict[str, Any],
    *,
    url: str,
    token: str,
    dry_run: bool = False,
    timeout: float = 20.0,
) -> tuple[bool, str]:
    if dry_run:
        log.info("DRY_RUN publish skipped state=%s", snapshot.get("state"))
        return True, "dry_run"
    if not url:
        return False, "missing_bot_ingest_url"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["X-Market-Oracle-Token"] = token
        # accept same header style as santiment for shared token setups
        headers["X-Oracle-Token"] = token
    try:
        resp = requests.post(url, json=snapshot, headers=headers, timeout=timeout)
        if resp.status_code >= 400:
            return False, f"http_{resp.status_code}:{resp.text[:160]}"
        return True, "ok"
    except Exception as e:
        log.warning("publish failed: %s", e)
        return False, str(e)
