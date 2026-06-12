"""Sync CMC trending coins into the dry-run watchlist overlay."""

from __future__ import annotations

import os
from datetime import datetime, timedelta

from core.config import BotConfig, get_bot_config
from data.cmc_trending_provider import CMCTrendingProvider
from data_manager import (
    is_dry_run_enhanced,
    load_dry_run_overlay,
    load_live_trade_history,
    load_watchlist,
    save_dry_run_overlay,
)
from logger import log
from price_fetcher import get_prices_batch


class DryRunWatchlistSync:
    def __init__(self, config: BotConfig = None, provider: CMCTrendingProvider = None):
        self.config = config or get_bot_config()
        api_env = self.config.cmc_config.get("api_key_env", "CMC_API_KEY")
        self.provider = provider or CMCTrendingProvider(api_key=os.getenv(api_env, ""))

    def sync_if_needed(self, force: bool = False) -> dict:
        if not is_dry_run_enhanced(self.config.raw):
            return load_dry_run_overlay()

        tw_cfg = self.config.trending_watchlist_config
        if not tw_cfg.get("enabled", True):
            return load_dry_run_overlay()

        overlay = load_dry_run_overlay()
        refresh_hours = float(tw_cfg.get("refresh_hours", 6))
        refreshed_at = overlay.get("refreshed_at", "")
        if not force and refreshed_at:
            try:
                last = datetime.fromisoformat(str(refreshed_at).replace("Z", ""))
                if datetime.now() - last < timedelta(hours=refresh_hours):
                    return overlay
            except Exception:
                pass

        max_coins = int(tw_cfg.get("max_coins", 15))
        symbols, source = self.provider.fetch_trending_symbols(limit=max_coins)
        if not symbols:
            return overlay

        exclude = {s.upper() for s in tw_cfg.get("exclude_symbols", [])}
        base_symbols = {c.get("symbol", "").split("/")[0].upper() for c in load_watchlist()}
        candidates = []
        for sym in symbols:
            sym = sym.upper()
            if sym in exclude or sym in base_symbols:
                continue
            candidates.append(f"{sym}/USDT")

        if tw_cfg.get("gate_only", True) and candidates:
            prices = get_prices_batch(candidates)
            candidates = [sym for sym in candidates if float(prices.get(sym, 0) or 0) > 0]

        coins = []
        for sym in candidates[:max_coins]:
            ticker = sym.split("/")[0]
            coins.append({
                "symbol": sym,
                "ticker": ticker,
                "name": ticker,
                "timeframe": "4h",
                "active": True,
                "source": "cmc_trending",
            })

        overlay = {
            "refreshed_at": datetime.now().isoformat(),
            "source": source or "cmc_trending",
            "coins": coins,
        }
        save_dry_run_overlay(overlay)
        log(
            f"Dry-run watchlist sync: {len(coins)}/{max_coins} trending coins Gate-tradeable "
            f"(source: {source or 'unknown'})",
            "INFO",
        )
        return overlay

    def status(self) -> dict:
        overlay = load_dry_run_overlay()
        tw_cfg = self.config.trending_watchlist_config
        history = load_live_trade_history()
        return {
            "enabled": is_dry_run_enhanced(self.config.raw) and tw_cfg.get("enabled", True),
            "refreshed_at": overlay.get("refreshed_at", ""),
            "source": overlay.get("source", ""),
            "trending_count": len(overlay.get("coins", [])),
            "simulated_balance": float(
                history.get("virtual_balance", self.config.simulated_balance_usdt)
            ),
        }