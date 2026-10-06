"""#636 Per-coin indicator compare (observe only).

Current read: ``technical_rsi_bb`` entry predicate with ``buy_regime=both``.
Alternate read: the same predicate and the same parameters, ``buy_regime=dip``.
A row diverges only when the current read is ``BUY`` and the alternate is ``HOLD``.

The row is appended to ``data/{tenant_id}/indicator_compare.jsonl``. It is not
an accept, reject, or size input. Nothing here places an order.

Flag: ``indicator_compare.enabled`` (bool, default false). Missing or empty
config is a no-op. Scope is characteristic, not a name list: Gate spot, 4h,
``technical_rsi_bb``, and an explicit ``strategies[]`` row whose ``buy_regime``
is ``both``. A write error is fail-open and is not a gate.
"""

from __future__ import annotations

import math
import os
import re
from datetime import datetime, timezone
from typing import Any

IST_REGIME = "both"
ALT_REGIME = "dip"
STRATEGY_CLASS = "technical_rsi_bb"
TIMEFRAME = "4h"
VENUE_EXCHANGE = "gate"
VENUE_MARKET = "spot"
VERDICT_BUY = "BUY"
VERDICT_HOLD = "HOLD"
LOG_FILENAME = "indicator_compare.jsonl"
ROTATE_MAX_BYTES = 8_000_000
ROTATE_KEEP_LINES = 20_000
_UNDER_TEST_ENV = "INDICATOR_COMPARE_UNDER_TEST"
_TENANT_SAFE = re.compile(r"[^A-Za-z0-9_-]+")
_SWAP_MARKETS = frozenset({"swap", "perp", "perpetual", "future", "futures", "margin"})

ROW_KEYS = (
    "symbol",
    "bar_time",
    "rsi",
    "last_rsi",
    "volume_factor",
    "dist_lower_bb",
    "ist_read",
    "alt_read",
    "diverge",
)


def indicator_compare_enabled(config: dict | None) -> bool:
    """True only when the flag is boolean true. Missing section stays off."""
    try:
        if not isinstance(config, dict):
            return False
        section = config.get("indicator_compare")
        if not isinstance(section, dict):
            return False
        return section.get("enabled") is True
    except Exception:
        return False


def _text(value: object) -> str:
    return str(value or "").strip().lower()


def _exchange(coin: dict | None, params: dict | None, config: dict | None) -> str:
    for src in (coin, params):
        if isinstance(src, dict) and src.get("exchange"):
            return _text(src.get("exchange"))
    if isinstance(config, dict):
        live = config.get("live")
        if isinstance(live, dict) and live.get("exchange"):
            return _text(live.get("exchange"))
        if config.get("exchange"):
            return _text(config.get("exchange"))
    # Same default as BotConfig.exchange when live.exchange is unset.
    return VENUE_EXCHANGE


def _market_kind(coin: dict | None, params: dict | None) -> str:
    for src in (coin, params):
        if not isinstance(src, dict):
            continue
        for key in ("market", "market_type", "instrument_type"):
            raw = src.get(key)
            if raw:
                return _text(raw)
    return VENUE_MARKET


def _is_gate_spot(coin: dict | None, params: dict | None, config: dict | None) -> bool:
    kind = _market_kind(coin, params)
    if kind in _SWAP_MARKETS or kind != VENUE_MARKET:
        return False
    return _exchange(coin, params, config) == VENUE_EXCHANGE


def _explicit_both_row(
    config: dict | None,
    *,
    symbol: str,
    timeframe: str,
) -> dict | None:
    """Strategy row that already says ``buy_regime=both``.

    Resolved volatile defaults are not a row. A coin enters scope only when
    ``strategies[]`` lists that symbol and timeframe with regime ``both``.
    """
    if not isinstance(config, dict):
        return None
    rows = config.get("strategies")
    if not isinstance(rows, list):
        return None
    tf = _text(timeframe)
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("symbol") or "") != symbol:
            continue
        if _text(row.get("timeframe") or TIMEFRAME) != tf:
            continue
        if row.get("buy_regime") != IST_REGIME:
            continue
        klass = _text(row.get("strategy_class") or STRATEGY_CLASS)
        if klass != STRATEGY_CLASS:
            continue
        if not _is_gate_spot(row, row, config):
            continue
        return row
    return None


def in_compare_scope(
    *,
    coin: dict | None,
    params: dict | None,
    config: dict | None,
    market,
    strategy_name: str | None = None,
) -> bool:
    """Gate spot, 4h, technical RSI, explicit ``buy_regime=both`` row."""
    name = _text(strategy_name) if strategy_name else ""
    if name and name != STRATEGY_CLASS:
        return False
    if not isinstance(params, dict) or params.get("buy_regime") != IST_REGIME:
        return False
    tf = _text(getattr(market, "timeframe", None) or (coin or {}).get("timeframe"))
    if tf != TIMEFRAME:
        return False
    if not _is_gate_spot(coin, params, config):
        return False
    symbol = str(getattr(market, "symbol", None) or (coin or {}).get("symbol") or "")
    if not symbol:
        return False
    return _explicit_both_row(config, symbol=symbol, timeframe=tf) is not None


def _finite(value: object) -> float | None:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num):
        return None
    return num


def _bar_time(market) -> str | None:
    df = getattr(market, "ohlcv_df", None)
    if df is None:
        return None
    try:
        if bool(getattr(df, "empty", False)):
            return None
        columns = getattr(df, "columns", None)
        if columns is not None and "ts" not in list(columns):
            return None
        raw = df["ts"].iloc[-1]
        ms = int(raw)
        if ms < 10_000_000_000:
            ms *= 1000
        stamp = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
        return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return None


def _dist_lower_bb(market) -> float | None:
    lower = _finite(getattr(market, "lower_bb", None))
    price = _finite(getattr(market, "current_price", None))
    if lower is None or price is None or lower <= 0:
        return None
    return (price - lower) / lower


def _verdict(value: str) -> str:
    return VERDICT_BUY if value == VERDICT_BUY else VERDICT_HOLD


def _max_open_positions(config: dict | None) -> int:
    try:
        from core.config import get_bot_config

        return int(get_bot_config().max_open_positions)
    except Exception:
        raw = config if isinstance(config, dict) else {}
        try:
            return int(raw.get("max_open_positions", 5))
        except (TypeError, ValueError):
            return 5


def _build_row(
    config: dict | None,
    *,
    coin: dict | None,
    market,
    params: dict | None,
    strategy_name: str | None,
) -> dict[str, Any] | None:
    if not in_compare_scope(
        coin=coin,
        params=params,
        config=config,
        market=market,
        strategy_name=strategy_name,
    ):
        return None
    if getattr(market, "has_position", False):
        return None
    max_open = _max_open_positions(config)
    if int(getattr(market, "open_positions", 0) or 0) >= max_open:
        return None

    from strategies.technical_rsi_bb import _entry_last_rsi, entry_read

    symbol = str(getattr(market, "symbol", None) or (coin or {}).get("symbol") or "")
    tf = str(getattr(market, "timeframe", None) or TIMEFRAME)
    last_rsi = _entry_last_rsi(market, symbol, tf)
    ist = _verdict(
        entry_read(
            market,
            params,
            symbol=symbol,
            tf=tf,
            max_open_positions=max_open,
            last_rsi=last_rsi,
        )
    )
    alt_params = dict(params or {})
    alt_params["buy_regime"] = ALT_REGIME
    alt = _verdict(
        entry_read(
            market,
            alt_params,
            symbol=symbol,
            tf=tf,
            max_open_positions=max_open,
            last_rsi=last_rsi,
        )
    )
    rsi = _finite(getattr(market, "rsi", None))
    volume = _finite(getattr(market, "vol_multiplier", None))
    return {
        "symbol": symbol,
        "bar_time": _bar_time(market),
        "rsi": rsi,
        "last_rsi": _finite(last_rsi),
        "volume_factor": volume,
        "dist_lower_bb": _dist_lower_bb(market),
        "ist_read": ist,
        "alt_read": alt,
        "diverge": ist == VERDICT_BUY and alt == VERDICT_HOLD,
    }


def _safe_tenant_id(tenant_id: str) -> str:
    safe = _TENANT_SAFE.sub("_", str(tenant_id or "").strip())
    return safe or "tenant"


def compare_log_path(tenant_id: str | None = None) -> str:
    from core.tenant_context import resolve_tenant_id
    from data_manager import data_dir

    tid = _safe_tenant_id(resolve_tenant_id(tenant_id))
    return os.path.join(data_dir(), tid, LOG_FILENAME)


def _file_write_allowed() -> bool:
    if os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get(_UNDER_TEST_ENV) != "1":
        return False
    return True


def _warn(detail: object) -> None:
    try:
        from logger import log

        log(f"[indicator_compare] fail-open: {detail}", "WARNING")
    except Exception:
        pass


def _write_row(row: dict[str, Any], tenant_id: str | None) -> None:
    if not _file_write_allowed():
        return
    from services.observability_store import append_jsonl, maybe_rotate_jsonl

    path = compare_log_path(tenant_id)
    append_jsonl(path, {key: row.get(key) for key in ROW_KEYS})
    maybe_rotate_jsonl(path, max_bytes=ROTATE_MAX_BYTES, keep_lines=ROTATE_KEEP_LINES)


def maybe_log_indicator_compare(
    config: dict | None,
    *,
    coin: dict | None = None,
    market=None,
    params: dict | None = None,
    strategy_name: str | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any] | None:
    """Append one observe row, or return None when the flag or scope is empty.

    Never raises. The returned row is not a trading decision.
    """
    if not indicator_compare_enabled(config):
        return None
    if market is None:
        return None
    try:
        row = _build_row(
            config,
            coin=coin,
            market=market,
            params=params,
            strategy_name=strategy_name,
        )
    except Exception:
        return None
    if row is None:
        return None
    try:
        _write_row(row, tenant_id)
    except Exception as exc:
        _warn(exc)
    return row
