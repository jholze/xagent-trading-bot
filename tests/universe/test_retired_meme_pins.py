"""#602 — retire ARIA, RAVE, HIGH, ZBT from the base seed.

Does not rewrite the #601 30-coin list. Tests must not write into data/.
"""

from __future__ import annotations

import json
from pathlib import Path

from services.universe.core_seed import (
    CORE_SEED_30,
    CORE_SEED_30_TICKERS,
    PIN_TICKERS,
    finalize_trade_members,
    merge_preserving_open_lots,
    prepare_watchlist_core_seed,
    ticker_of,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
WATCHLIST_PATH = REPO_ROOT / "data" / "watchlist.json"
EXPANSION_PATH = REPO_ROOT / "data" / "watchlist.dry_run_expansion.json"
CONFIG_PATH = REPO_ROOT / "config.json"

_RETIRED = ("ARIA", "RAVE", "HIGH", "ZBT")


def _seed_cfg(mode: str = "shadow", *, behavior_change: bool = False) -> dict:
    return {
        "universe_core_seed": {
            "mode": mode,
            "behavior_change": behavior_change,
            "quiet_profile": "stable_altcoin",
            "moving_profile": "mid_cap_defaults",
            "risk_profile": "volatile_altcoin",
        }
    }


def _repo_watchlist_coins() -> list[dict]:
    data = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
    return list(data.get("coins") or [])


def _repo_expansion_coins() -> list[dict]:
    data = json.loads(EXPANSION_PATH.read_text(encoding="utf-8"))
    return list(data.get("coins") or [])


def _by_ticker(coins: list[dict]) -> dict[str, dict]:
    return {ticker_of(c.get("symbol") or c.get("ticker") or ""): c for c in coins}


def _base_from_repo() -> tuple[list[dict], set[str]]:
    raw = _repo_watchlist_coins()
    loaded = prepare_watchlist_core_seed(raw, _seed_cfg("shadow"))
    base_syms = {c["symbol"] for c in loaded if c.get("symbol")}
    return loaded, base_syms


def test_four_pins_absent_from_base():
    loaded, _ = _base_from_repo()
    by_t = _by_ticker(loaded)
    for tick in _RETIRED:
        row = by_t.get(tick)
        assert row is None, tick
    for row in loaded:
        assert ticker_of(row.get("symbol") or "") not in PIN_TICKERS
        if str(row.get("source") or "") == "base":
            assert ticker_of(row.get("symbol") or "") not in PIN_TICKERS


def test_core_30_unchanged():
    coins = _repo_watchlist_coins()
    by_t = _by_ticker(coins)
    for tick in CORE_SEED_30_TICKERS:
        row = by_t.get(tick)
        assert row is not None, tick
        assert str(row.get("symbol") or "").endswith("/USDT")
        assert row.get("active") is True
    assert "DOT" in by_t
    assert "PEPE" in by_t
    names = {ticker_of(c.get("symbol") or "") for c in coins if c.get("active") is True}
    extra = names - CORE_SEED_30
    assert not extra, extra
    assert CORE_SEED_30 <= names
    assert len(CORE_SEED_30) == 30


def test_expansion_file_does_not_restore_pins():
    expansion = _repo_expansion_coins()
    exp_tickers = {ticker_of(c.get("symbol") or "") for c in expansion}
    assert not (PIN_TICKERS & exp_tickers)

    high = {
        "symbol": "HIGH/USDT",
        "ticker": "HIGH",
        "timeframe": "4h",
        "active": True,
        "source": "dry_run_expansion",
    }
    loaded, base_syms = _base_from_repo()
    assert "HIGH/USDT" not in base_syms
    merged = merge_preserving_open_lots(
        list(loaded),
        [[dict(high)]],
        open_symbols=set(),
        config=_seed_cfg("shadow"),
    )
    members = finalize_trade_members(
        merged,
        open_symbols=set(),
        base_symbols=base_syms,
        config=_seed_cfg("shadow"),
    )
    by_sym = {c["symbol"]: c for c in members}
    leaked = by_sym.get("HIGH/USDT")
    if leaked is not None:
        assert leaked.get("source") != "base"


def test_open_lot_survives_pin_removal(monkeypatch):
    logged: list[str] = []
    monkeypatch.setattr(
        "services.universe.core_seed.log",
        lambda msg, level="INFO": logged.append(str(msg)),
    )
    loaded, base_syms = _base_from_repo()
    assert "ZBT/USDT" not in base_syms
    members = finalize_trade_members(
        list(loaded),
        open_symbols={"ZBT/USDT"},
        open_lot_rows={
            "ZBT/USDT": {
                "symbol": "ZBT/USDT",
                "ticker": "ZBT",
                "timeframe": "4h",
                "active": True,
            }
        },
        base_symbols=base_syms,
        config=_seed_cfg("shadow"),
    )
    zbt = next(c for c in members if c["symbol"] == "ZBT/USDT")
    assert zbt["lane"] == "trade"
    assert zbt["source"] == "position"
    zbt_logs = [line for line in logged if "symbol=ZBT/USDT" in line]
    assert zbt_logs
    line = zbt_logs[0]
    assert line.startswith("universe_member ")
    assert "lane=trade" in line
    assert "source=position" in line
    assert line.endswith("pin_removed=true")
    assert "pin_removed=true" in line


def test_no_liquidation_on_retire():
    loaded, base_syms = _base_from_repo()
    lot = {
        "symbol": "ZBT/USDT",
        "ticker": "ZBT",
        "timeframe": "4h",
        "active": True,
        "amount": 12.5,
        "average_entry": 0.4,
    }
    members = finalize_trade_members(
        list(loaded),
        open_symbols={"ZBT/USDT"},
        open_lot_rows={"ZBT/USDT": dict(lot)},
        base_symbols=base_syms,
        config=_seed_cfg("shadow"),
    )
    zbt = next(c for c in members if c["symbol"] == "ZBT/USDT")
    assert zbt["source"] == "position"
    assert zbt["lane"] == "trade"
    for row in members:
        action = str(row.get("action") or row.get("intent") or "").upper()
        assert "SELL" not in action
        assert str(row.get("order_type") or "").upper() != "SELL"
        assert row.get("liquidate") is not True
        assert row.get("force_sell") is not True


def test_strategy_row_cannot_reseed():
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    assert cfg.get("live", {}).get("dry_run") is True
    assert str(cfg.get("universe_core_seed", {}).get("mode") or "") == "shadow"
    strategies = list(cfg.get("strategies") or [])
    by_key = {
        (e.get("symbol"), e.get("timeframe", "4h")): e
        for e in strategies
        if isinstance(e, dict)
    }
    assert by_key[("ARIA/USDT", "4h")].get("live_enabled") is False
    assert by_key[("RAVE/USDT", "4h")].get("live_enabled") is False
    assert by_key[("HIGH/USDT", "4h")].get("live_enabled") is False
    assert ("ZBT/USDT", "4h") not in by_key

    loaded, base_syms = _base_from_repo()
    for tick in _RETIRED:
        assert f"{tick}/USDT" not in base_syms
        assert tick not in _by_ticker(loaded)

    members = finalize_trade_members(
        list(loaded),
        open_symbols=set(),
        base_symbols=base_syms,
        config=cfg,
    )
    member_tickers = {ticker_of(c.get("symbol") or "") for c in members}
    for tick in _RETIRED:
        assert tick not in member_tickers
    for row in members:
        if ticker_of(row.get("symbol") or "") in PIN_TICKERS:
            assert row.get("source") != "base"
