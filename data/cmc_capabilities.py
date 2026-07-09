"""CMC API plan detection and endpoint availability cache."""

from __future__ import annotations

import os
import time
from typing import Dict, List, Optional

import requests

from logger import log

BASE_URL = "https://pro-api.coinmarketcap.com/v1"

_PROBE_SPECS: dict[str, dict] = {
    "trending/latest": {
        "method": "GET",
        "path": "/cryptocurrency/trending/latest",
        "params": {"limit": 1},
    },
    "trending/gainers-losers": {
        "method": "GET",
        "path": "/cryptocurrency/trending/gainers-losers",
        "params": {"time_period": "24h", "limit": 1},
    },
    "listings/latest": {
        "method": "GET",
        "path": "/cryptocurrency/listings/latest",
        "params": {"limit": 1},
    },
    "community/trending/token": {
        "method": "GET",
        "path": "/community/trending/token",
        "params": {"limit": 1},
    },
    "content/latest": {
        "method": "GET",
        "path": "/content/latest",
        "params": {"limit": 1},
    },
    "quotes/latest": {
        "method": "GET",
        "path": "/cryptocurrency/quotes/latest",
        "params": {"symbol": "BTC"},
    },
    "dex/tokens/trending/list": {
        "method": "POST",
        "path": "/dex/tokens/trending/list",
        "json": {"limit": 1},
    },
}

_CACHE: dict | None = None
_CACHE_AT: float = 0.0
_CACHE_TTL_SEC = 3600.0


def _headers(api_key: str) -> dict:
    return {"X-CMC_PRO_API_KEY": api_key, "Accept": "application/json"}


def _probe_endpoint(api_key: str, endpoint_id: str) -> bool:
    spec = _PROBE_SPECS.get(endpoint_id)
    if not spec:
        return False
    url = f"{BASE_URL}{spec['path']}"
    try:
        if spec["method"] == "POST":
            resp = requests.post(
                url,
                headers=_headers(api_key),
                json=spec.get("json") or {},
                timeout=12,
            )
        else:
            resp = requests.get(
                url,
                headers=_headers(api_key),
                params=spec.get("params") or {},
                timeout=12,
            )
        return resp.status_code == 200
    except Exception:
        return False


def _fetch_key_info(api_key: str) -> dict:
    try:
        resp = requests.get(
            f"{BASE_URL}/key/info",
            headers=_headers(api_key),
            timeout=12,
        )
        if resp.status_code != 200:
            return {}
        data = resp.json().get("data", {}) or {}
        plan = data.get("plan", {}) or {}
        usage = data.get("usage", {}) or {}
        return {
            "plan_name": str(plan.get("plan_name") or plan.get("name") or ""),
            "credits_monthly": int(plan.get("credit_limit_monthly") or 0),
            "rate_limit_per_min": int(plan.get("rate_limit_minute") or 0),
            "credits_used": int(usage.get("current_month", {}).get("credits_used") or 0),
        }
    except Exception:
        return {}


def _infer_plan_label(endpoints: dict[str, bool], key_info: dict) -> str:
    name = (key_info.get("plan_name") or "").strip()
    if name:
        return name
    if endpoints.get("dex/tokens/trending/list"):
        return "Startup+"
    if endpoints.get("trending/latest") and endpoints.get("community/trending/token"):
        return "Builder+"
    if endpoints.get("listings/latest"):
        return "Basic/Builder (listings only)"
    return "unknown"


def probe_capabilities(api_key: str | None = None, *, force: bool = False) -> dict:
    """Probe CMC key once per hour; returns endpoint map + plan metadata."""
    global _CACHE, _CACHE_AT

    key = (api_key or os.getenv("CMC_API_KEY") or "").strip()
    if not key:
        return {"api_key_set": False, "endpoints": {}, "plan_label": "no_key"}

    now = time.time()
    if not force and _CACHE and (now - _CACHE_AT) < _CACHE_TTL_SEC:
        return _CACHE

    key_info = _fetch_key_info(key)
    endpoints = {eid: _probe_endpoint(key, eid) for eid in _PROBE_SPECS}
    plan_label = _infer_plan_label(endpoints, key_info)

    _CACHE = {
        "api_key_set": True,
        "endpoints": endpoints,
        "plan_label": plan_label,
        "key_info": key_info,
    }
    _CACHE_AT = now

    ok = [e for e, v in endpoints.items() if v]
    log(
        f"CMC capabilities: plan={plan_label}, endpoints_ok={len(ok)}/{len(endpoints)}",
        "INFO",
    )
    return _CACHE


def reset_capabilities_cache() -> None:
    global _CACHE, _CACHE_AT
    _CACHE = None
    _CACHE_AT = 0.0


def endpoint_available(endpoint_id: str, caps: dict | None = None) -> bool:
    data = caps or probe_capabilities()
    return bool((data.get("endpoints") or {}).get(endpoint_id))


def filter_source_priority(
    priority: List[str],
    caps: dict | None = None,
    *,
    api_key: str | None = None,
) -> List[str]:
    """Drop endpoints known to 403 for this API key."""
    data = caps or probe_capabilities(api_key)
    endpoints = data.get("endpoints") or {}
    if not endpoints:
        return list(priority)
    filtered = [p for p in priority if endpoints.get(p, True)]
    return filtered or ["listings/latest"]


def quotes_batch_size(cmc_config: dict | None = None) -> int:
    cfg = cmc_config or {}
    return max(1, int(cfg.get("quotes_batch_size", 8)))