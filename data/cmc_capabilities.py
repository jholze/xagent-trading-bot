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
    """Label plan from key/info name or endpoint/credit fingerprints."""
    name = (key_info.get("plan_name") or "").strip()
    credits = int(key_info.get("credits_monthly") or 0)
    rate = int(key_info.get("rate_limit_per_min") or 0)

    # API key/info often omits plan_name; use credits/rate + endpoints.
    if not name:
        if endpoints.get("dex/tokens/trending/list"):
            name = "Startup+ (Dex)"
        elif endpoints.get("trending/latest") and endpoints.get("community/trending/token"):
            name = "Builder+ (Community)"
        elif endpoints.get("trending/latest") or endpoints.get("trending/gainers-losers"):
            # Startup typically unlocks market trending; community/DEX may stay locked.
            if credits >= 300_000 or rate >= 500:
                name = "Startup"
            else:
                name = "Builder (trending)"
        elif endpoints.get("listings/latest"):
            name = "Basic (listings only)"
        else:
            name = "unknown"
    elif "startup" in name.lower() and not endpoints.get("trending/latest"):
        name = f"{name} (key may need re-issue for trending)"

    extras = []
    if credits:
        extras.append(f"{credits:,} cr/mo")
    if rate:
        extras.append(f"{rate}/min")
    if extras:
        return f"{name} · {' · '.join(extras)}"
    return name


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
    fail = [e for e, v in endpoints.items() if not v]
    log(
        f"CMC capabilities: plan={plan_label}, "
        f"ok=[{', '.join(ok) or 'none'}], "
        f"blocked=[{', '.join(fail) or 'none'}]",
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


def has_community_endpoints(caps: dict | None = None) -> bool:
    data = caps or probe_capabilities()
    return endpoint_available("community/trending/token", data) or endpoint_available(
        "content/latest", data
    )


def has_market_trending_endpoints(caps: dict | None = None) -> bool:
    """True when Startup/Builder market-trending APIs work (not listings-only)."""
    data = caps or probe_capabilities()
    return endpoint_available("trending/latest", data) or endpoint_available(
        "trending/gainers-losers", data
    )


def has_dexscan_endpoint(caps: dict | None = None) -> bool:
    data = caps or probe_capabilities()
    return endpoint_available("dex/tokens/trending/list", data)


def trade_path_mode(cmc_config: dict | None = None, caps: dict | None = None) -> str:
    """How CMC feeds the trade path.

    Modes: community | market_trending | quotes_fallback | quotes_blocked |
    disabled | no_key | empty
    """
    cfg = cmc_config or {}
    if not cfg.get("enabled", True):
        return "disabled"
    data = caps or probe_capabilities()
    if not data.get("api_key_set"):
        return "no_key"
    quotes_ok = bool(cfg.get("quotes_fallback_as_signal", False))
    community = has_community_endpoints(data)
    market = has_market_trending_endpoints(data)
    listings = endpoint_available("listings/latest", data)
    quotes = endpoint_available("quotes/latest", data)
    if community:
        return "community"
    if market:
        return "market_trending"
    if quotes_ok and (listings or quotes):
        return "quotes_fallback"
    if listings or quotes:
        return "quotes_blocked"
    return "empty"


def format_cmc_status_line(cmc_config: dict | None = None, caps: dict | None = None) -> str:
    """One-line operator status for /cmc, digests, startup."""
    cfg = cmc_config or {}
    data = caps or probe_capabilities()
    plan = data.get("plan_label") or "unknown"
    mode = trade_path_mode(cfg, data)
    mode_label = {
        "community": "community + market (full social)",
        "market_trending": "trending/latest (Startup market path)",
        "quotes_fallback": "quotes/listings only (trade-enabled)",
        "quotes_blocked": "quotes/listings only — trade filter OFF",
        "disabled": "disabled in config",
        "no_key": "no API key",
        "empty": "no usable endpoints",
    }.get(mode, mode)
    dex = "dex=on" if has_dexscan_endpoint(data) else "dex=off"
    return f"CMC plan={plan} · trade={mode_label} · {dex}"


def log_cmc_boot_status(cmc_config: dict | None = None) -> dict:
    """Probe once at process start and log plan + trade-path decision."""
    cfg = cmc_config or {}
    try:
        from core.config import get_bot_config

        if not cfg:
            cfg = get_bot_config().cmc_config
    except Exception:
        pass
    caps = probe_capabilities(force=False)
    line = format_cmc_status_line(cfg, caps)
    mode = trade_path_mode(cfg, caps)
    log(line, "INFO")
    if mode == "quotes_blocked":
        log(
            "CMC: only quotes/listings on this key — trade path off. "
            "Set cmc.quotes_fallback_as_signal=true to allow quote signals "
            "(with sell_requires_ta + churn guards), or unlock trending/community on the plan",
            "WARNING",
        )
    elif mode == "quotes_fallback":
        log(
            "CMC: trade path via quotes/listings only "
            "(no trending endpoints; lower trust, sell_requires_ta + churn guards apply)",
            "INFO",
        )
    elif mode == "market_trending":
        log(
            "CMC: Startup market-trending path active "
            "(trending/latest primary; quotes may enrich watchlist; "
            "community/DEX off-plan unless unlocked)",
            "INFO",
        )
    elif mode == "community":
        log("CMC: community + market endpoints active", "INFO")
    if not has_dexscan_endpoint(caps) and (cfg.get("dexscan_alerts") or {}).get("enabled", True):
        log(
            "CMC DexScan endpoint not on this plan — /dexsignals will stay empty until unlocked",
            "INFO",
        )
    return caps


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