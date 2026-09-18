"""#489: overlay losers stay off gainer expand, CMC trending, and expansion JSON."""

from __future__ import annotations

import json
import os
import threading
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.config import BotConfig
from services import dry_run_watchlist as tw_module
from services.dry_run_watchlist import TrendingWatchlistSync
from services.gainer_universe.config import gainer_universe_config
from services.gainer_universe.filters import passes_spot_usdt_filter
from services.gainer_universe.scanner import filter_and_rank_live

REPO = Path(__file__).resolve().parents[2]
OVERLAY_LOSERS = ("BEAT", "SKYAI", "H", "PENGU", "SPK")
GAINER_KEEPERS = ("XRP", "ZEC", "ADA", "SUI", "LTC", "BNB", "BTC", "AAVE")
LEAVE_OFF_LISTS = ("VELVET", "LAB", "HANA")


def _load_repo_config() -> dict:
    with open(REPO / "config.json", encoding="utf-8") as fh:
        return json.load(fh)


@contextmanager
def _sync_patches(
    *,
    overlay_path: str,
    provider: MagicMock,
    gate_prices: dict,
    base_watchlist: list | None = None,
):
    base_watchlist = base_watchlist if base_watchlist is not None else []
    patches = [
        patch("data_manager.get_data_file", return_value=overlay_path),
        patch("data_manager.load_watchlist", return_value=base_watchlist),
        patch("data_manager.is_dry_run_enhanced", return_value=True),
        patch("services.dry_run_watchlist.CMCTrendingProvider", return_value=provider),
        patch("services.dry_run_watchlist.get_gate_prices_batch", return_value=gate_prices),
        patch("data_manager.prune_non_gate_watchlist_sources"),
    ]
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield


def test_config_lists_overlay_losers_and_keeps_wqe_soft():
    cfg = _load_repo_config()
    assert cfg["watchlist_quality"]["mode"] == "soft"
    assert cfg["gainer_universe"]["blacklist_bases"] == list(OVERLAY_LOSERS)
    for leave in LEAVE_OFF_LISTS:
        assert leave not in cfg["gainer_universe"]["blacklist_bases"]
    for exclude in (
        cfg["live"]["trending_watchlist"]["exclude_symbols"],
        cfg["cmc"]["trending_watchlist"]["exclude_symbols"],
    ):
        have = {str(s).upper() for s in exclude}
        for base in OVERLAY_LOSERS:
            assert base in have
        for keep in ("USDT", "USDC", "BTC", "ETH", "SOL"):
            assert keep in have
        for leave in LEAVE_OFF_LISTS:
            assert leave not in have


def test_gainer_filter_drops_overlay_losers_keeps_named_bases():
    cfg = gainer_universe_config(_load_repo_config())
    blacklist = cfg["blacklist_bases"]
    tickers: dict = {}
    for i, base in enumerate(OVERLAY_LOSERS):
        tickers[f"{base}/USDT"] = {
            "last": 1,
            "percentage": 90 - i,
            "quoteVolume": 2_000_000,
        }
    for i, base in enumerate(GAINER_KEEPERS):
        tickers[f"{base}/USDT"] = {
            "last": 1,
            "percentage": 20 - i,
            "quoteVolume": 2_000_000,
        }
    ranked = filter_and_rank_live(tickers, cfg)
    syms = {r["symbol"] for r in ranked}
    for base in OVERLAY_LOSERS:
        pair = f"{base}/USDT"
        assert pair not in syms
        assert not passes_spot_usdt_filter(pair, blacklist_bases=blacklist)
    for base in GAINER_KEEPERS:
        pair = f"{base}/USDT"
        assert pair in syms
        assert passes_spot_usdt_filter(pair, blacklist_bases=blacklist)


def test_trending_sync_skips_overlay_losers(tmp_path):
    tw_module._sync_lock = threading.Lock()
    exclude = ["USDT", "USDC", "BTC", "ETH", "SOL", *OVERLAY_LOSERS]
    cfg = BotConfig()
    cfg._raw = {
        "trading_mode": "live",
        "live": {
            "dry_run": True,
            "dry_run_enhanced": True,
            "simulated_balance_usdt": 5000,
            "trending_watchlist": {
                "enabled": True,
                "max_coins": 15,
                "refresh_hours": 0,
                "gate_only": True,
                "exclude_symbols": list(exclude),
            },
        },
        "cmc": {
            "api_key_env": "CMC_API_KEY",
            "trending_watchlist": {"exclude_symbols": list(exclude)},
        },
        "dry_run_defaults": {},
        "volatile_altcoin": {"timeframe": "1h"},
    }
    provider = MagicMock()
    provider.fetch_trending_symbols.return_value = (
        list(OVERLAY_LOSERS) + ["XRP", "ADA", "BTC", "ETH", "SOL"],
        "trending/latest",
    )
    gate = {
        f"{base}/USDT": 1.0
        for base in list(OVERLAY_LOSERS) + ["XRP", "ADA", "BTC", "ETH", "SOL"]
    }
    overlay_path = os.path.join(tmp_path, "watchlist.dry_run_overlay.json")
    with open(overlay_path, "w", encoding="utf-8") as fh:
        json.dump({"coins": [], "refreshed_at": ""}, fh)
    with _sync_patches(
        overlay_path=overlay_path,
        provider=provider,
        gate_prices=gate,
        base_watchlist=[],
    ):
        out = TrendingWatchlistSync(cfg).sync_if_needed(force=True)
    coins = {c["symbol"] for c in (out.get("coins") or [])}
    for base in OVERLAY_LOSERS:
        assert f"{base}/USDT" not in coins
    assert "XRP/USDT" in coins
    assert "ADA/USDT" in coins
    assert "BTC/USDT" not in coins
    assert "ETH/USDT" not in coins
    assert "SOL/USDT" not in coins


def _expansion_bases(payload: dict) -> set[str]:
    bases: set[str] = set()
    for coin in payload.get("coins") or []:
        ticker = str(coin.get("ticker") or "").upper()
        if ticker:
            bases.add(ticker)
        symbol = str(coin.get("symbol") or "")
        if "/" in symbol:
            bases.add(symbol.split("/", 1)[0].upper())
    return bases


def test_expansion_json_omits_overlay_losers():
    committed = REPO / "data" / "watchlist.dry_run_expansion.json"
    assert committed.is_file()
    payload = json.loads(committed.read_text(encoding="utf-8"))
    bases = _expansion_bases(payload)
    for loser in OVERLAY_LOSERS:
        assert loser not in bases, committed.name
    for keep in ("VELVET", "LAB"):
        assert keep in bases, committed.name
