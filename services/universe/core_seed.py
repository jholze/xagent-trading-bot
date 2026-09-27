"""Gate 90d core seed (#601): bucket mapping, membership, shadow vs enforce.

Shadow (default) logs the intended profile and does not switch RSI or size.
Enforce exists so tests can turn it on; default config must stay shadow.
"""

from __future__ import annotations

from typing import Any, Iterable

from logger import log

PIN_TICKERS = frozenset({"ARIA", "RAVE", "HIGH", "ZBT"})
LEGACY_SIX_TICKERS = frozenset({"ARIA", "RAVE", "HIGH", "SOL", "BTC", "ZBT"})

QUIET_TICKERS = (
    "TRX",
    "BNB",
    "BTC",
    "OKB",
    "ASTER",
    "ETH",
    "LTC",
    "SOL",
    "DOGE",
    "LINK",
    "XLM",
    "XRP",
)
MOVING_TICKERS = (
    "HYPE",
    "AVAX",
    "ADA",
    "TAO",
    "SUI",
    "AAVE",
    "BCH",
    "ONDO",
    "INJ",
    "PEPE",
)
RISK_TICKERS = (
    "DOT",
    "FIL",
    "NEAR",
    "UNI",
    "WLD",
    "ENA",
    "ZEC",
    "ARB",
)
CORE_SEED_30_TICKERS = QUIET_TICKERS + MOVING_TICKERS + RISK_TICKERS
CORE_SEED_30 = frozenset(CORE_SEED_30_TICKERS)

ALLOWED_BUCKETS = frozenset({"quiet", "moving", "risk"})
ALLOWED_MODES = frozenset({"off", "shadow", "enforce"})

_DEFAULT_PROFILES = {
    "quiet": "stable_altcoin",
    "moving": "mid_cap_defaults",
    "risk": "volatile_altcoin",
}
_PROFILE_CONFIG_KEYS = {
    "quiet": "quiet_profile",
    "moving": "moving_profile",
    "risk": "risk_profile",
}

# Overlay sources that selected RSI / tier before this seed. Open lots in
# shadow/off must keep these rows; source=base must not replace them.
_PRE_SEED_SOURCES = frozenset({
    "dry_run_expansion",
    "cmc_trending",
    "gainer_live_heat",
    "gate_prev_top",
})

# RSI block only — never copy size / DCA / max_usdt from the profile.
_RSI_BLOCK_KEYS = (
    "rsi_buy_low",
    "rsi_buy_high",
    "rsi_sell_mode",
    "rsi_sell_30",
    "rsi_sell_20",
    "rsi_sell_min_gain_pct",
)


class CoreSeedLoadError(ValueError):
    """A core seed row is missing a required bucket or has an invalid one."""


def ticker_of(symbol: str) -> str:
    raw = str(symbol or "").strip().upper()
    if not raw:
        return ""
    return raw.split("/", 1)[0]


def _sym(coin: dict | None) -> str:
    if not isinstance(coin, dict):
        return ""
    return str(coin.get("symbol") or "").strip()


def core_seed_config(config: dict | None = None) -> dict[str, Any]:
    raw: dict[str, Any] = {}
    if isinstance(config, dict):
        sec = config.get("universe_core_seed")
        if isinstance(sec, dict):
            raw = sec
    mode = str(raw.get("mode") or "shadow").strip().lower()
    if mode not in ALLOWED_MODES:
        mode = "shadow"
    quiet = str(raw.get("quiet_profile") or _DEFAULT_PROFILES["quiet"]).strip()
    moving = str(raw.get("moving_profile") or _DEFAULT_PROFILES["moving"]).strip()
    risk = str(raw.get("risk_profile") or _DEFAULT_PROFILES["risk"]).strip()
    return {
        "mode": mode,
        "behavior_change": bool(raw.get("behavior_change", False)),
        "quiet_profile": quiet or _DEFAULT_PROFILES["quiet"],
        "moving_profile": moving or _DEFAULT_PROFILES["moving"],
        "risk_profile": risk or _DEFAULT_PROFILES["risk"],
    }


def map_bucket_to_profile(bucket: str, config: dict | None = None) -> str:
    b = str(bucket or "").strip().lower()
    if b not in ALLOWED_BUCKETS:
        raise CoreSeedLoadError(f"invalid core seed bucket {bucket!r}")
    cfg = core_seed_config(config)
    return str(cfg[_PROFILE_CONFIG_KEYS[b]])


def _bucket_of(coin: dict) -> str:
    raw = coin.get("bucket")
    if raw is None:
        return ""
    return str(raw).strip().lower()


def _is_pin(coin: dict) -> bool:
    return ticker_of(_sym(coin) or str(coin.get("ticker") or "")) in PIN_TICKERS


def _is_core_row(coin: dict) -> bool:
    """True for the 30 Gate-USDT seed names (BTC/SOL included). Pins are not core."""
    if _is_pin(coin):
        return False
    tick = ticker_of(_sym(coin) or str(coin.get("ticker") or ""))
    return tick in CORE_SEED_30


def _should_validate_buckets(coins: Iterable[dict], config: dict | None = None) -> bool:
    """Require buckets on the operator seed, not on 1-coin test fixtures.

    A file with any ``bucket`` field, or with 10+ of the 30 Gate names, is the
    core seed. Synthetic BTC-only watchlists used by existing tests are not.
    """
    if core_seed_config(config)["mode"] == "off":
        return False
    rows = [c for c in (coins or []) if isinstance(c, dict)]
    if any(_bucket_of(c) for c in rows):
        return True
    return sum(1 for c in rows if _is_core_row(c)) >= 10


def validate_core_seed_rows(coins: Iterable[dict], config: dict | None = None) -> None:
    """Fail the load when a core row has no bucket / an invalid bucket.

    Pins (ARIA, RAVE, HIGH, ZBT) may omit bucket. mode=off does not require
    buckets because membership is the legacy six.
    """
    cfg = core_seed_config(config)
    if cfg["mode"] == "off":
        return
    for coin in coins or []:
        if not isinstance(coin, dict):
            continue
        if not _is_core_row(coin):
            continue
        bucket = _bucket_of(coin)
        if not bucket:
            raise CoreSeedLoadError(
                f"core seed row {_sym(coin) or coin.get('ticker')} is missing bucket"
            )
        if bucket not in ALLOWED_BUCKETS:
            raise CoreSeedLoadError(
                f"core seed row {_sym(coin)} has invalid bucket {bucket!r}"
            )


def _is_pre_seed_source(row: dict | None) -> bool:
    if not isinstance(row, dict):
        return False
    return str(row.get("source") or "").strip() in _PRE_SEED_SOURCES


def _lot_rows_by_symbol(open_lot_rows: dict | None) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for key, row in (open_lot_rows or {}).items():
        if not key or not isinstance(row, dict):
            continue
        s = str(key).strip() or _sym(row)
        if s:
            out[s] = dict(row)
    return out


def merge_preserving_open_lots(
    base_coins: list[dict],
    overlay_lists: list[list[dict]] | None = None,
    *,
    open_symbols: Iterable[str] | None = None,
    open_lot_rows: dict | None = None,
    open_set_unknown: bool = False,
    config: dict | None = None,
) -> list[dict]:
    """Base-first merge, except open lots in shadow/off keep the overlay row.

    Before this seed, ADA/NEAR/… lived on ``dry_run_expansion`` (or trending /
    gainer). First-wins dedupe would let the new ``source=base`` row replace
    that and flip RSI while mode=shadow. Open lots keep the pre-seed row.

    The trending overlay is rewritten every refresh and skips seed tickers, so
    the surviving pre-seed row may come from the open lot itself. If the open
    set cannot be read, keep every pre-seed overlay row rather than falling
    back to base.
    """
    extras: list[dict] = []
    overlay_first: dict[str, dict] = {}
    for lst in overlay_lists or []:
        for coin in lst or []:
            if not isinstance(coin, dict):
                continue
            s = _sym(coin)
            if not s:
                continue
            extras.append(dict(coin))
            overlay_first.setdefault(s, dict(coin))

    mode = core_seed_config(config)["mode"]
    shadow_like = mode in ("shadow", "off")
    open_syms = {str(s).strip() for s in (open_symbols or set()) if s}
    lot_rows = _lot_rows_by_symbol(open_lot_rows)
    keep_all_overlay = shadow_like and open_set_unknown

    out: list[dict] = []
    seen: set[str] = set()
    for coin in list(base_coins or []) + extras:
        if not isinstance(coin, dict):
            continue
        s = _sym(coin)
        if not s or s in seen:
            continue
        if shadow_like and (keep_all_overlay or s in open_syms):
            ov = overlay_first.get(s)
            if ov and _is_pre_seed_source(ov):
                out.append(ov)
                seen.add(s)
                continue
            lot = lot_rows.get(s)
            if s in open_syms and lot and _is_pre_seed_source(lot):
                out.append(lot)
                seen.add(s)
                continue
        out.append(dict(coin))
        seen.add(s)
    return out


def filter_watchlist_for_mode(
    coins: list[dict],
    config: dict | None = None,
) -> list[dict]:
    """mode=off keeps the old six pins; shadow/enforce keep 30 + four pins."""
    cfg = core_seed_config(config)
    out: list[dict] = []
    seen: set[str] = set()
    for coin in coins or []:
        if not isinstance(coin, dict):
            continue
        sym = _sym(coin)
        if not sym or sym in seen:
            continue
        tick = ticker_of(sym)
        if cfg["mode"] == "off":
            if tick not in LEGACY_SIX_TICKERS:
                continue
        else:
            if tick not in PIN_TICKERS and tick not in CORE_SEED_30:
                # Operator-added names outside the seed stay; they are not
                # core rows so a missing bucket does not fail the load.
                pass
        seen.add(sym)
        row = dict(coin)
        row["source"] = "base"
        out.append(row)
    return out


def prepare_watchlist_core_seed(
    coins: list[dict],
    config: dict | None = None,
) -> list[dict]:
    """Validate buckets and apply mode membership. Called from load_watchlist."""
    cfg = config
    if cfg is None:
        cfg = _load_config_safe()
    if _should_validate_buckets(coins, cfg):
        validate_core_seed_rows(coins, cfg)
    return filter_watchlist_for_mode(coins, cfg)


def _load_config_safe() -> dict:
    try:
        from data_manager import get_config

        cfg = get_config()
        if isinstance(cfg, dict):
            return cfg
    except Exception:
        pass
    try:
        from data_manager import _load_default_config_from_disk

        cfg = _load_default_config_from_disk()
        if isinstance(cfg, dict):
            return cfg
    except Exception:
        pass
    return {}


def _profile_block(config: dict | None, profile_name: str) -> dict:
    if not isinstance(config, dict) or not profile_name:
        return {}
    block = config.get(profile_name)
    return dict(block) if isinstance(block, dict) else {}


def _apply_enforce_rsi(row: dict, profile: str, config: dict | None) -> None:
    """Route the bucket to the profile's RSI block. Does not copy size/DCA."""
    params = dict(row.get("strategy_params") or {})
    params["strategy_profile"] = profile
    block = _profile_block(config, profile)
    for key in _RSI_BLOCK_KEYS:
        if key in block:
            params[key] = block[key]
    row["strategy_params"] = params
    row["strategy_profile"] = profile


def _member_log_line(row: dict) -> str:
    bucket = row.get("bucket") or "-"
    profile = row.get("profile") or "-"
    applied = row.get("profile_applied")
    applied_s = "true" if applied else "false"
    return (
        f"universe_member symbol={row.get('symbol')} "
        f"lane={row.get('lane') or 'trade'} "
        f"source={row.get('source') or '-'} "
        f"bucket={bucket} profile={profile} profile_applied={applied_s}"
    )


def finalize_trade_members(
    coins: list[dict],
    *,
    open_symbols: Iterable[str] | None = None,
    open_lot_rows: dict | None = None,
    open_set_unknown: bool = False,
    base_symbols: Iterable[str] | None = None,
    config: dict | None = None,
) -> list[dict]:
    """Tag trade-lane members, inject open lots, log intended profile.

    Open lots that are not in the seed are never dropped, even when seed +
    trending exceeds the watch cap. In shadow/off, an open lot keeps the
    pre-seed overlay row (expansion, trending, gainer) — source=base must
    not overwrite it. source=position is only set when the row has no source.
    When the overlay no longer holds that row, the open lot itself is the
    surviving source. If the open set cannot be read, pre-seed overlay rows
    are not overwritten with source=base.
    """
    cfg = core_seed_config(config)
    mode = cfg["mode"]
    base_syms = {str(s).strip() for s in (base_symbols or set()) if s}
    open_syms = {str(s).strip() for s in (open_symbols or set()) if s}
    lot_rows = _lot_rows_by_symbol(open_lot_rows)
    keep_pre_seed = mode in ("shadow", "off")

    by_sym: dict[str, dict] = {}
    order: list[str] = []
    for coin in coins or []:
        if not isinstance(coin, dict):
            continue
        sym = _sym(coin)
        if not sym:
            continue
        if sym in by_sym:
            if (
                keep_pre_seed
                and (sym in open_syms or open_set_unknown)
                and _is_pre_seed_source(coin)
                and not _is_pre_seed_source(by_sym[sym])
            ):
                by_sym[sym] = dict(coin)
            continue
        by_sym[sym] = dict(coin)
        order.append(sym)

    for sym in open_syms:
        lot = lot_rows.get(sym)
        if (
            keep_pre_seed
            and lot
            and _is_pre_seed_source(lot)
            and not _is_pre_seed_source(by_sym.get(sym) or {})
        ):
            if sym not in by_sym:
                order.append(sym)
            by_sym[sym] = dict(lot)
            continue
        if sym not in by_sym:
            by_sym[sym] = {
                "symbol": sym,
                "ticker": ticker_of(sym),
                "timeframe": "4h",
                "active": True,
            }
            order.append(sym)

    out: list[dict] = []
    for sym in order:
        row = by_sym[sym]
        row["lane"] = "trade"
        bucket = _bucket_of(row)
        intended = ""
        if bucket in ALLOWED_BUCKETS:
            intended = map_bucket_to_profile(bucket, config)

        pre_seed = _is_pre_seed_source(row)
        keep_open_source = keep_pre_seed and (
            sym in open_syms or (open_set_unknown and pre_seed)
        )
        if keep_open_source:
            if not str(row.get("source") or "").strip():
                row["source"] = "position"
            row["profile_applied"] = False
        elif sym in base_syms:
            row["source"] = "base"
            if intended:
                row["bucket"] = bucket
                row["profile"] = intended
            if mode == "enforce" and intended:
                _apply_enforce_rsi(row, intended, config)
                row["profile_applied"] = True
            else:
                row["profile_applied"] = False
        elif sym in open_syms:
            if not str(row.get("source") or "").strip():
                row["source"] = "position"
            row["profile_applied"] = False
        else:
            row.setdefault("source", row.get("source") or "discovery")
            row["profile_applied"] = False

        out.append(row)
        if mode != "off":
            log(_member_log_line(row), "INFO")
    return out
