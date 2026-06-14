"""CMC, Gate, and TradingView links for Telegram coin mentions."""

from __future__ import annotations

import json
import os
import re
from html import escape
from pathlib import Path

import requests

from logger import log

_CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "cmc_slug_cache.json"
_CMC_BASE = "https://coinmarketcap.com"
_GATE_BASE = "https://www.gate.io/trade"
_TV_BASE = "https://www.tradingview.com/chart/"


def coin_links_config(config=None) -> dict:
    from core.config import get_bot_config

    cfg = config or get_bot_config()
    defaults = {
        "enabled": True,
        "show_cmc": True,
        "show_gate": True,
        "show_tradingview": True,
        "inline_buttons_on_signals": True,
        "chart_image_on_executed_trades": True,
        "chart_bars": 48,
        "chart_timeframe": "4h",
    }
    raw = cfg.observability_config.get("coin_links", {})
    return {**defaults, **raw}


def coin_links_enabled(config=None) -> bool:
    return bool(coin_links_config(config).get("enabled", True))


def normalize_ticker(symbol: str) -> str:
    if not symbol:
        return ""
    s = str(symbol).strip().upper()
    if "/" in s:
        s = s.split("/")[0]
    return s.replace("USDT", "").strip()


def _load_cache() -> dict:
    if not _CACHE_PATH.is_file():
        return {}
    try:
        with open(_CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(cache: dict):
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, sort_keys=True)


def _watchlist_name_for_ticker(ticker: str) -> str:
    try:
        from data_manager import load_effective_watchlist

        for coin in load_effective_watchlist():
            if normalize_ticker(coin.get("symbol", "")) == ticker:
                if coin.get("cmc_slug"):
                    return coin["cmc_slug"]
                return coin.get("name") or ""
    except Exception:
        pass
    return ""


def _name_match_score(coin_name: str, candidate_name: str) -> float:
    if not coin_name or not candidate_name:
        return 0.0
    a = re.sub(r"[^a-z0-9]+", "", coin_name.lower())
    b = re.sub(r"[^a-z0-9]+", "", candidate_name.lower())
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return 0.0


def _resolve_slug_from_api(ticker: str, name_hint: str = "") -> str:
    api_key = os.getenv("CMC_API_KEY", "")
    if not api_key:
        return ""
    try:
        resp = requests.get(
            "https://pro-api.coinmarketcap.com/v1/cryptocurrency/info",
            headers={"X-CMC_PRO_API_KEY": api_key, "Accept": "application/json"},
            params={"symbol": ticker},
            timeout=12,
        )
        if resp.status_code != 200:
            return ""
        data = resp.json().get("data") or {}
        if not data:
            return ""
        candidates = []
        for entry in data.values():
            slug = entry.get("slug") or ""
            sym = (entry.get("symbol") or "").upper()
            if slug and sym == ticker:
                candidates.append((slug, entry.get("name") or ""))
        if not candidates:
            return ""
        if len(candidates) == 1:
            return candidates[0][0]
        if name_hint:
            best = max(candidates, key=lambda c: _name_match_score(name_hint, c[1]))
            if _name_match_score(name_hint, best[1]) > 0:
                return best[0]
        return candidates[0][0]
    except Exception as e:
        log(f"CMC slug resolve failed for {ticker}: {e}", "WARNING")
        return ""


def resolve_cmc_slug(ticker: str, name: str = None) -> str:
    ticker = normalize_ticker(ticker)
    if not ticker:
        return ""
    cache = _load_cache()
    if ticker in cache and cache[ticker]:
        return cache[ticker]

    name_hint = name or _watchlist_name_for_ticker(ticker)
    if name_hint and "/" not in name_hint and " " not in name_hint and name_hint.islower():
        slug = name_hint
    else:
        slug = _resolve_slug_from_api(ticker, name_hint if " " in str(name_hint) else name_hint)

    if slug:
        cache[ticker] = slug
        _save_cache(cache)
    return slug or ""


def cmc_coin_url(ticker: str, name: str = None) -> str:
    ticker = normalize_ticker(ticker)
    if not ticker:
        return f"{_CMC_BASE}/"
    slug = resolve_cmc_slug(ticker, name=name)
    if slug:
        return f"{_CMC_BASE}/currencies/{slug}/"
    return f"{_CMC_BASE}/search/?q={ticker}"


def gate_trade_url(ticker: str) -> str:
    ticker = normalize_ticker(ticker)
    return f"{_GATE_BASE}/{ticker}_USDT" if ticker else _GATE_BASE


def tradingview_chart_url(ticker: str) -> str:
    ticker = normalize_ticker(ticker)
    if not ticker:
        return _TV_BASE
    return f"{_TV_BASE}?symbol=GATEIO%3A{ticker}USDT"


def format_link_html(label: str, url: str) -> str:
    return f'<a href="{escape(url, quote=True)}">{escape(label)}</a>'


def format_ticker_html(ticker: str, name: str = None, *, symbol_suffix: str = "/USDT") -> str:
    if not coin_links_enabled():
        t = normalize_ticker(ticker) or escape(str(ticker))
        return f"{t}{symbol_suffix}" if symbol_suffix else t
    t = normalize_ticker(ticker) or str(ticker)
    url = cmc_coin_url(t, name=name)
    inner = escape(t)
    if symbol_suffix:
        return f'<a href="{escape(url, quote=True)}">{inner}</a>{symbol_suffix}'
    return f'<a href="{escape(url, quote=True)}">{inner}</a>'


def format_links_line(ticker: str, name: str = None) -> str:
    cfg = coin_links_config()
    if not cfg.get("enabled", True):
        return ""
    parts = []
    if cfg.get("show_cmc", True):
        parts.append(format_link_html("CMC", cmc_coin_url(ticker, name=name)))
    if cfg.get("show_gate", True):
        parts.append(format_link_html("Gate", gate_trade_url(ticker)))
    if cfg.get("show_tradingview", True):
        parts.append(format_link_html("Chart", tradingview_chart_url(ticker)))
    if not parts:
        return ""
    return "<b>Links:</b> " + " · ".join(parts)


def inline_link_buttons(ticker: str, name: str = None) -> list:
    """Return inline_keyboard rows for Telegram (url buttons)."""
    cfg = coin_links_config()
    if not cfg.get("enabled", True) or not cfg.get("inline_buttons_on_signals", True):
        return []
    row = []
    if cfg.get("show_cmc", True):
        row.append({"text": "CMC", "url": cmc_coin_url(ticker, name=name)})
    if cfg.get("show_gate", True):
        row.append({"text": "Gate", "url": gate_trade_url(ticker)})
    if cfg.get("show_tradingview", True):
        row.append({"text": "Chart", "url": tradingview_chart_url(ticker)})
    return [row] if row else []


def prefetch_watchlist_slugs():
    """Warm slug cache for active watchlist (best-effort)."""
    try:
        from data_manager import load_effective_watchlist

        for coin in load_effective_watchlist():
            if not coin.get("active", True):
                continue
            ticker = normalize_ticker(coin.get("symbol", ""))
            if ticker:
                resolve_cmc_slug(ticker, name=coin.get("name"))
    except Exception as e:
        log(f"Slug prefetch skipped: {e}", "WARNING")