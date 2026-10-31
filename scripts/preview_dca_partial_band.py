#!/usr/bin/env python3
"""Read-only: which exported lots sit in the partial band after the grace deploy.

The file is a lots export (the bot lot model, or ``{"lots": [...], "as_of": ...}``).
Nothing is written, no orders are sent, and no exchange is called. The stop,
the grace window, and the partial fraction are the live helpers. Risk floors
come from the loaded config.

``as_of`` (document field or ``--as-of``) freezes the grace clock so a snapshot
can be replayed. Naive timestamps stay naive, matching the lot writer.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


_PRICE_KEYS = ("current_price", "mark", "last_price")
_SELL_PARTIAL = "SELL_STOP_PARTIAL"
_SELL_FULL = "SELL_STOP_FULL"


def load_export(path: Path) -> tuple[list[dict], datetime | None, dict[str, float]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    prices: dict[str, float] = {}
    as_of = None
    if isinstance(payload, list):
        lots = payload
    elif isinstance(payload, dict) and isinstance(payload.get("lots"), list):
        lots = payload["lots"]
        as_of = _parse_as_of(payload.get("as_of"))
        raw_prices = payload.get("prices") or {}
        if isinstance(raw_prices, dict):
            for symbol, value in raw_prices.items():
                try:
                    prices[str(symbol)] = float(value)
                except (TypeError, ValueError):
                    continue
    elif isinstance(payload, dict):
        lots = []
        for key, value in payload.items():
            if isinstance(value, dict) and (
                "average_entry" in value or "amount" in value
            ):
                row = dict(value)
                row.setdefault("store_key", key)
                lots.append(row)
    else:
        raise SystemExit("lots export must be a list or an object with 'lots'")
    return [row for row in lots if isinstance(row, dict)], as_of, prices


def _parse_as_of(raw: object) -> datetime | None:
    if not raw:
        return None
    text = str(raw).strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SystemExit(f"as_of is not a timestamp: {raw!r}") from exc
    if parsed.tzinfo is not None:
        return parsed.replace(tzinfo=None)
    return parsed


def _price(lot: dict, prices: dict[str, float]) -> float | None:
    for key in _PRICE_KEYS:
        if lot.get(key) in (None, ""):
            continue
        try:
            value = float(lot[key])
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    symbol = str(lot.get("symbol") or "")
    if symbol in prices and prices[symbol] > 0:
        return float(prices[symbol])
    return None


def _params_for(lot: dict) -> dict:
    explicit = lot.get("strategy_params")
    if isinstance(explicit, dict) and explicit:
        return explicit
    from strategies.registry import resolve_strategy_params

    coin = {
        "symbol": lot.get("symbol") or "",
        "timeframe": lot.get("timeframe") or "4h",
    }
    for key in ("source", "market_cap_tier"):
        if lot.get(key):
            coin[key] = lot[key]
    return resolve_strategy_params(
        coin,
        has_position=True,
        frozen_tier=lot.get("strategy_tier"),
    )


def _base_stop(params: dict) -> float:
    from core.config import get_bot_config

    raw = params.get("stop_loss_pct")
    if raw is not None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    return float(get_bot_config().stop_loss_pct)


def _floors(params: dict) -> dict[str, float]:
    from core.config import get_bot_config

    cfg = get_bot_config()
    risk = cfg.risk_config if isinstance(cfg.risk_config, dict) else {}
    cmc = cfg.cmc_config if isinstance(getattr(cfg, "cmc_config", None), dict) else {}

    def _pick(*values: object, default: float) -> float:
        for value in values:
            if value is None or value == "":
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return default

    return {
        "min_position_usdt": _pick(
            params.get("min_position_usdt_for_partial_sell"),
            params.get("min_position_usdt_for_social_sell"),
            risk.get("min_position_usdt_for_partial_sell"),
            cmc.get("min_position_usdt_for_social_sell"),
            default=0.0,
        ),
        "min_notional_usdt": _pick(
            params.get("min_sell_notional_usdt"),
            risk.get("min_sell_notional_usdt"),
            cmc.get("min_sell_notional_usdt"),
            default=0.0,
        ),
        "max_sold_percent": _pick(
            params.get("block_partial_sell_if_sold_percent_above"),
            risk.get("block_partial_sell_if_sold_percent_above"),
            default=1.0,
        ),
    }


def _before_deploy_signal(
    loss: float | None, full: float, partial: float | None, rounds: int
) -> str | None:
    """Partial pause while any DCA round was open, which this deploy removes."""
    if loss is None:
        return None
    if loss > full:
        return _SELL_FULL
    if rounds > 0:
        return "HOLD"
    if partial is not None and loss > partial:
        return _SELL_PARTIAL
    return "HOLD"


@contextmanager
def _clock(as_of: datetime | None) -> Iterator[None]:
    if as_of is None:
        yield
        return
    from datetime import datetime as _datetime
    from unittest.mock import patch

    fixed = as_of

    class _Fixed(_datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fixed
            return fixed.replace(tzinfo=tz)

    with patch("strategies.dca.datetime", _Fixed):
        yield


def classify_lots(
    lots: list[dict],
    *,
    as_of: datetime | None = None,
    prices: dict[str, float] | None = None,
) -> dict[str, Any]:
    from strategies.dca import (
        _dca_rounds_used,
        effective_stop_loss_thresholds,
        evaluate_long_hard_stop,
        grace_window_hours,
    )
    from strategies.position_lock import auto_sell_blocked
    from strategies.positions import sell_fraction_for_signal
    from strategies.short_math import is_short

    from core.config import get_bot_config

    price_map = prices or {}
    cfg = get_bot_config()
    rows: list[dict] = []
    with _clock(as_of):
        for lot in lots:
            rows.append(
                _classify_one(
                    lot,
                    price_map=price_map,
                    config_raw=cfg.raw if isinstance(cfg.raw, dict) else {},
                    effective_stop_loss_thresholds=effective_stop_loss_thresholds,
                    evaluate_long_hard_stop=evaluate_long_hard_stop,
                    grace_window_hours=grace_window_hours,
                    rounds_used=_dca_rounds_used,
                    auto_sell_blocked=auto_sell_blocked,
                    sell_fraction_for_signal=sell_fraction_for_signal,
                    is_short=is_short,
                )
            )
    band = [
        row
        for row in rows
        if row.get("in_partial_band") and row.get("unlocked") and not row.get("in_grace")
    ]
    return {
        "read_only": True,
        "as_of": as_of.isoformat() if as_of else None,
        "summary": {
            "lots": len(rows),
            "in_partial_band_unlocked_outside_grace": len(band),
            "would_partial_fill": sum(
                1 for row in rows if row.get("disposition") == "partial_would_fill"
            ),
            "would_partial_reject": sum(
                1
                for row in rows
                if str(row.get("disposition") or "").startswith("partial_rejected")
            ),
            "partial_paused_grace": sum(
                1 for row in rows if row.get("disposition") == "partial_paused_grace"
            ),
            "would_full_fill": sum(
                1 for row in rows if row.get("disposition") == "full_would_fill"
            ),
            "locked": sum(1 for row in rows if row.get("disposition") == "locked"),
            "price_missing": sum(
                1 for row in rows if row.get("disposition") == "price_missing"
            ),
        },
        "partial_band_after_deploy": band,
        "lots": rows,
    }


def _classify_one(
    lot: dict,
    *,
    price_map: dict[str, float],
    config_raw: dict,
    effective_stop_loss_thresholds,
    evaluate_long_hard_stop,
    grace_window_hours,
    rounds_used,
    auto_sell_blocked,
    sell_fraction_for_signal,
    is_short,
) -> dict:
    symbol = str(lot.get("symbol") or "")
    timeframe = str(lot.get("timeframe") or "4h")
    try:
        amount = float(lot.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0.0
    try:
        entry = float(lot.get("average_entry") or 0)
    except (TypeError, ValueError):
        entry = 0.0
    row: dict[str, Any] = {
        "symbol": symbol,
        "timeframe": timeframe,
        "tenant_id": lot.get("tenant_id") or "",
        "amount": amount,
        "average_entry": entry,
        "dca_rounds": lot.get("dca_rounds"),
        "dca_recovery_rounds": lot.get("dca_recovery_rounds"),
        "last_dca_at": lot.get("last_dca_at"),
        "last_dca_recovery_at": lot.get("last_dca_recovery_at"),
    }
    if str(lot.get("side") or "long").lower() == "short" or is_short(lot):
        row["disposition"] = "not_long"
        return row
    if amount <= 0 or entry <= 0:
        row["disposition"] = "flat"
        return row
    price = _price(lot, price_map)
    row["price"] = price
    if price is None:
        row["disposition"] = "price_missing"
        return row

    params = _params_for(lot)
    base = _base_stop(params)
    full, _paused_partial, in_grace = effective_stop_loss_thresholds(lot, params, base)
    _bare_full, open_partial, _bare_grace = effective_stop_loss_thresholds(
        {"average_entry": entry}, params, base
    )
    loss = (price / entry - 1.0) * -100.0
    try:
        rounds = int(rounds_used(lot))
    except (TypeError, ValueError):
        rounds = 0
    hit = evaluate_long_hard_stop(
        price=price,
        entry=entry,
        position=lot,
        strategy_params=params,
        base_stop_loss_pct=base,
    )
    signal = hit[0] if hit else "HOLD"
    in_band = (
        open_partial is not None and loss > open_partial and not (loss > full)
    )
    fraction = (
        sell_fraction_for_signal(signal, symbol, timeframe, price, params)
        if signal != "HOLD"
        else None
    )
    position_usdt = amount * price
    sell_notional = position_usdt * fraction if fraction is not None else None
    locked, lock_reason = auto_sell_blocked(lot, "auto", config=config_raw)
    floors = _floors(params)
    disposition = "hold"
    note = ""
    if locked and signal != "HOLD":
        disposition = "locked"
        note = lock_reason
    elif signal == _SELL_FULL:
        disposition = "full_would_fill"
    elif signal == _SELL_PARTIAL:
        if position_usdt < floors["min_position_usdt"]:
            disposition = "partial_rejected_position_floor"
        elif sell_notional is not None and sell_notional < floors["min_notional_usdt"]:
            disposition = "partial_rejected_notional_floor"
        elif float(lot.get("sold_percent") or 0) >= floors["max_sold_percent"]:
            disposition = "partial_rejected_sold_percent"
        else:
            disposition = "partial_would_fill"
    elif in_grace and in_band:
        disposition = "partial_paused_grace"

    row.update(
        {
            "price": price,
            "loss_pct": loss,
            "full_stop_pct": full,
            "partial_stop_pct": open_partial,
            "grace_hours": grace_window_hours(params),
            "in_grace": bool(in_grace),
            "rounds_counted": rounds,
            "in_partial_band": bool(in_band),
            "unlocked": not locked,
            "signal": signal,
            "fraction": fraction,
            "position_usdt": position_usdt,
            "sell_notional_usdt": sell_notional,
            "disposition": disposition,
            "lock_reason": lock_reason if locked else "",
            "before_deploy_signal": _before_deploy_signal(
                loss, full, open_partial, rounds
            ),
            "note": note,
        }
    )
    return row


def _text(report: dict) -> str:
    lines = [
        "read-only partial-band preview (after deploy)",
        (
            "lots={lots} partial_band_unlocked_outside_grace={band} "
            "would_fill={fill} would_reject={reject} paused_grace={grace} "
            "full={full} locked={locked} price_missing={missing}"
        ).format(
            lots=report["summary"]["lots"],
            band=report["summary"]["in_partial_band_unlocked_outside_grace"],
            fill=report["summary"]["would_partial_fill"],
            reject=report["summary"]["would_partial_reject"],
            grace=report["summary"]["partial_paused_grace"],
            full=report["summary"]["would_full_fill"],
            locked=report["summary"]["locked"],
            missing=report["summary"]["price_missing"],
        ),
    ]
    for row in report["partial_band_after_deploy"]:
        lines.append(
            "{symbol} {timeframe} loss={loss:.2f} signal={signal} "
            "fraction={fraction} disposition={disposition} "
            "before={before}".format(
                symbol=row.get("symbol") or "",
                timeframe=row.get("timeframe") or "",
                loss=float(row.get("loss_pct") or 0),
                signal=row.get("signal"),
                fraction=row.get("fraction"),
                disposition=row.get("disposition"),
                before=row.get("before_deploy_signal"),
            )
        )
    if not report["partial_band_after_deploy"]:
        lines.append("no unlocked lot is in the partial band outside grace")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lots_json", type=Path)
    parser.add_argument("--as-of", default=None, help="Grace clock, ISO timestamp")
    parser.add_argument("--format", choices=("json", "text"), default="json")
    args = parser.parse_args(argv)
    os.chdir(ROOT)
    lots, file_as_of, prices = load_export(args.lots_json)
    as_of = _parse_as_of(args.as_of) if args.as_of else file_as_of
    report = classify_lots(lots, as_of=as_of, prices=prices)
    if args.format == "text":
        sys.stdout.write(_text(report) + "\n")
    else:
        json.dump(report, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
