"""Sync CMC trending coins into the watchlist overlay (live + enhanced dry-run)."""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta

from core.config import BotConfig, get_bot_config
from data.cmc_trending_provider import CMCTrendingProvider
from data_manager import (
    is_dry_run_enhanced,
    load_cmc_trending_overlay,
    load_dry_run_overlay,
    load_live_trade_history,
    load_watchlist,
    save_cmc_trending_overlay,
    save_dry_run_overlay,
    trending_watchlist_live_enabled,
)
from logger import log
from price_fetcher import get_gate_prices_batch

_sync_lock = threading.Lock()


def sync_trending_watchlist_once(bot_config: BotConfig = None, *, force: bool = False) -> dict:
    """Single entry point for CMC trending overlay sync (thread-safe)."""
    with _sync_lock:
        return TrendingWatchlistSync(bot_config).sync_if_needed(force=force)


class TrendingWatchlistSync:
    """Keeps CMC trending symbols on the effective watchlist."""

    def __init__(self, config: BotConfig = None, provider: CMCTrendingProvider = None):
        self.config = config or get_bot_config()
        api_env = self.config.cmc_config.get("api_key_env", "CMC_API_KEY")
        self.provider = provider or CMCTrendingProvider(api_key=os.getenv(api_env, ""))

    def _should_sync(self) -> bool:
        tw_cfg = self.config.trending_watchlist_config
        if not tw_cfg.get("enabled", True):
            return False
        return trending_watchlist_live_enabled(self.config.raw) or is_dry_run_enhanced(self.config.raw)

    def _use_live_overlay(self) -> bool:
        return trending_watchlist_live_enabled(self.config.raw)

    def _load_overlay(self) -> dict:
        if self._use_live_overlay() and not is_dry_run_enhanced(self.config.raw):
            return load_cmc_trending_overlay()
        return load_dry_run_overlay()

    def _save_overlay(self, data: dict) -> None:
        if self._use_live_overlay() and not is_dry_run_enhanced(self.config.raw):
            save_cmc_trending_overlay(data)
        else:
            save_dry_run_overlay(data)

    def sync_if_needed(self, force: bool = False) -> dict:
        if not self._should_sync():
            return self._load_overlay()

        tw_cfg = self.config.trending_watchlist_config
        overlay = self._load_overlay()
        refresh_hours = float(tw_cfg.get("refresh_hours", 4))
        refreshed_at = overlay.get("refreshed_at", "")
        if not force and refreshed_at:
            try:
                last = datetime.fromisoformat(str(refreshed_at).replace("Z", ""))
                if datetime.now() - last < timedelta(hours=refresh_hours):
                    return overlay
            except Exception:
                pass

        max_coins = int(tw_cfg.get("max_coins", 15))
        source_priority = tw_cfg.get("source_priority") or ["trending/latest"]
        symbols, source = self.provider.fetch_trending_symbols(
            limit=max_coins,
            source_priority=source_priority,
        )
        if not symbols:
            return overlay

        exclude = {s.upper() for s in tw_cfg.get("exclude_symbols", [])}
        base_symbols = {c.get("symbol", "").split("/")[0].upper() for c in load_watchlist()}
        candidates = []
        for rank, sym in enumerate(symbols, start=1):
            sym = sym.upper()
            if sym in exclude or sym in base_symbols:
                continue
            candidates.append((f"{sym}/USDT", rank))

        if tw_cfg.get("gate_only", tw_cfg.get("exchange_only", True)) and candidates:
            from core.config import get_bot_config
            ex = get_bot_config().exchange
            syms = [sym for sym, _ in candidates]
            prices = get_gate_prices_batch(syms)  # TODO: generalize to exchange
            candidates = [(sym, rank) for sym, rank in candidates if float(prices.get(sym, 0) or 0) > 0]

        volatile_tf = str(
            self.config.volatile_altcoin_config.get("timeframe") or "1h"
        ).strip() or "1h"
        old_syms = {c.get("symbol") for c in overlay.get("coins", [])}
        coins = []
        for sym, rank in candidates[:max_coins]:
            ticker = sym.split("/")[0]
            coins.append({
                "symbol": sym,
                "ticker": ticker,
                "name": ticker,
                "timeframe": volatile_tf,
                "active": True,
                "source": "cmc_trending",
                "trending_rank": rank,
            })

        new_syms = {c.get("symbol") for c in coins}
        added = [c for c in coins if c.get("symbol") not in old_syms]
        removed = [s for s in old_syms if s and s not in new_syms]

        overlay = {
            "refreshed_at": datetime.now().isoformat(),
            "source": source or "cmc_trending",
            "coins": coins,
            "added": [{"symbol": c["symbol"], "trending_rank": c.get("trending_rank")} for c in added],
            "removed": list(removed),
        }
        self._save_overlay(overlay)
        log(
            f"CMC trending watchlist sync: {len(coins)}/{max_coins} Gate-tradeable "
            f"(source: {source or 'unknown'}, +{len(added)} -{len(removed)})",
            "INFO",
        )
        if not old_syms:
            log(
                f"CMC trending bootstrap: {len(coins)} coins seeded "
                f"(source: {source or 'unknown'}, no Telegram on cold start)",
                "INFO",
            )
        else:
            self._notify_added(added, source)
        return overlay

    def _notify_added(self, added: list, source: str) -> None:
        if not added:
            return
        try:
            from telegram_notifier import send_telegram_message
            from notifications.coin_links import format_ticker_html

            lines = [
                "<b>📈 Watchlist+ CMC Trending</b>",
                "<i>Beobachtung — kein automatischer Kauf.</i>",
                "",
            ]
            for c in added[:8]:
                sym = c.get("symbol", "").split("/")[0]
                rank = c.get("trending_rank", "?")
                lines.append(f"• {format_ticker_html(sym)} — Trending #{rank} ({source or 'cmc'})")
            if len(added) > 8:
                lines.append(f"… +{len(added) - 8} weitere — /trending")
            send_telegram_message("\n".join(lines))
        except Exception as e:
            log(f"Trending add notification failed: {e}", "WARNING")

    def status(self) -> dict:
        overlay = self._load_overlay()
        tw_cfg = self.config.trending_watchlist_config
        history = load_live_trade_history()
        return {
            "enabled": self._should_sync(),
            "live_overlay": self._use_live_overlay(),
            "refreshed_at": overlay.get("refreshed_at", ""),
            "source": overlay.get("source", ""),
            "trending_count": len(overlay.get("coins", [])),
            "added_last": overlay.get("added", []),
            "removed_last": overlay.get("removed", []),
            "simulated_balance": float(
                history.get("virtual_balance", self.config.simulated_balance_usdt)
            ),
        }


DryRunWatchlistSync = TrendingWatchlistSync