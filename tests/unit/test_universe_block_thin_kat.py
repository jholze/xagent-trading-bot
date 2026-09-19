"""#492 universe: KAT / thin junk must not re-enter via gainer or CMC trending.

Config + existing helpers only. Does not write data/.
"""

from __future__ import annotations

import json
from pathlib import Path

from services.gainer_universe.filters import passes_spot_usdt_filter

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config.json"
EXPANSION_PATH = ROOT / "data" / "watchlist.dry_run_expansion.json"

STILL_ELIGIBLE = ("XRP", "ZEC", "ADA", "SUI", "LTC", "BNB", "BTC", "AAVE", "ARB")
BLOCKED = ("KAT",)


def _config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def test_kat_on_gainer_and_trending_excludes():
    cfg = _config()
    bases = [str(x).upper() for x in cfg["gainer_universe"]["blacklist_bases"]]
    live_ex = [str(x).upper() for x in cfg["live"]["trending_watchlist"]["exclude_symbols"]]
    cmc_ex = [str(x).upper() for x in cfg["cmc"]["trending_watchlist"]["exclude_symbols"]]
    for name in BLOCKED:
        assert name in bases
        assert name in live_ex
        assert name in cmc_ex
    for name in STILL_ELIGIBLE:
        assert name not in bases


def test_kat_fails_spot_usdt_filter_from_config_blacklist():
    cfg = _config()
    bases = cfg["gainer_universe"]["blacklist_bases"]
    assert not passes_spot_usdt_filter("KAT/USDT", blacklist_bases=bases)
    assert passes_spot_usdt_filter("ARB/USDT", blacklist_bases=bases)
    assert passes_spot_usdt_filter("XRP/USDT", blacklist_bases=bases)


def test_fusion_top_n_and_trending_confidence_tightened():
    fusion = _config()["cmc"]["cmc_trending_fusion"]
    assert int(fusion["allow_cmc_only_buy_top_n"]) <= 3
    assert float(fusion["min_confidence_trending"]) >= 60


def test_wqe_stays_soft():
    assert _config()["watchlist_quality"]["mode"] == "soft"


def test_expansion_json_has_no_kat():
    payload = json.loads(EXPANSION_PATH.read_text(encoding="utf-8"))
    tickers = {
        str(c.get("ticker") or c.get("symbol", "")).upper().replace("/USDT", "")
        for c in payload.get("coins", [])
        if c.get("active", True)
    }
    assert "KAT" not in tickers
