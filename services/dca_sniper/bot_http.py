"""Bot-side HTTP API for DCA sniper (#224 / #226).

Routes (token-gated):
  GET  /internal/dca-sniper/candidates
  GET  /internal/dca-sniper/cash
  GET  /internal/dca-sniper/status
  POST /internal/dca-sniper/execute
  POST /internal/dca-sniper/fund-sell
  POST /internal/dca-sniper/promote
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from flask import Flask, jsonify, request

from logger import log
from services.dca_sniper.config import dca_sniper_config, dca_sniper_enabled, internal_token


def _check_token() -> tuple[bool, Any]:
    expected = internal_token()
    if not expected:
        return False, (
            jsonify(
                {
                    "ok": False,
                    "message": "not_configured",
                    "error": "DCA_SNIPER_TOKEN unset",
                }
            ),
            503,
        )
    got = (
        request.headers.get("X-Dca-Sniper-Token")
        or request.headers.get("X-Exit-Ws-Token")
        or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        or ""
    )
    if got != expected:
        return False, (jsonify({"ok": False, "message": "unauthorized"}), 401)
    return True, None


def _snapshot_cash() -> dict[str, Any]:
    try:
        from core.config import get_bot_config
        from risk.risk_manager import RiskManager
        from services.market_service import MarketService

        cfg = get_bot_config()
        risk = RiskManager(cfg, MarketService())
        snap = risk.status_summary()
        bal = float(snap.get("virtual_balance") or snap.get("spendable_usdt") or 0)
        sdca = snap.get("spendable_dca")
        if sdca is None:
            sdca = snap.get("spendable_usdt") or bal
        snew = snap.get("spendable_new")
        if snew is None:
            snew = snap.get("spendable_usdt") or bal
        return {
            "spendable_dca": float(sdca or 0),
            "spendable_new": float(snew or 0),
            "cash_floor_abs": float(snap.get("cash_floor_abs") or 0),
            "cash_mode": str(snap.get("cash_mode") or ""),
            "balance": bal,
            "equity": float(snap.get("portfolio_equity") or bal),
        }
    except Exception as e:
        log(f"dca_sniper cash snapshot fail: {e}", "WARNING")
        return {
            "spendable_dca": 0.0,
            "spendable_new": 0.0,
            "cash_floor_abs": 0.0,
            "cash_mode": "error",
            "balance": 0.0,
            "equity": 0.0,
            "error": str(e)[:120],
        }


def _build_candidates() -> list[dict[str, Any]]:
    from strategies.positions import get_position, list_active_positions
    from strategies.registry import resolve_strategy_params

    cash = _snapshot_cash()
    out: list[dict[str, Any]] = []
    try:
        lots = list_active_positions()
    except Exception as e:
        log(f"dca_sniper list positions: {e}", "WARNING")
        lots = []

    # Bulk marks — list_active lots often lack current_price
    price_map: dict[str, float] = {}
    try:
        from price_fetcher import get_prices_batch

        syms = []
        for lot in lots:
            s = str(lot.get("symbol") or "")
            if s:
                syms.append(s)
        if syms:
            raw = get_prices_batch(syms) or {}
            for s, px in raw.items():
                try:
                    v = float(px or 0)
                except (TypeError, ValueError):
                    v = 0.0
                if v > 0:
                    price_map[str(s).upper()] = v
    except Exception as e:
        log(f"dca_sniper bulk prices skip: {e}", "DEBUG")

    skipped = {"no_mark": 0, "green": 0, "locked_dca": 0, "bad_lot": 0, "err": 0}
    for lot in lots:
        try:
            symbol = str(lot.get("symbol") or "")
            tf = str(lot.get("timeframe") or "1h")
            # Full position for lock / recovery_hold / dca meta (lot summary can be thin)
            pos = get_position(symbol, tf) or lot
            avg = float(pos.get("average_entry") or lot.get("average_entry") or 0)
            amount = float(pos.get("amount") or lot.get("amount") or 0)
            if not symbol or amount <= 0 or avg <= 0:
                skipped["bad_lot"] += 1
                continue
            mark = float(
                pos.get("current_price")
                or pos.get("mark")
                or pos.get("last_price")
                or lot.get("current_price")
                or lot.get("mark")
                or 0
            )
            if mark <= 0:
                mark = float(price_map.get(symbol.upper()) or 0)
            if mark <= 0:
                try:
                    from services.market_service import MarketService

                    mark = float(MarketService().get_price(symbol) or 0)
                except Exception:
                    mark = 0.0
            if mark <= 0:
                skipped["no_mark"] += 1
                continue
            loss = (mark / avg - 1.0) * 100.0
            if loss >= -1.0:  # only red-ish bags
                skipped["green"] += 1
                continue
            try:
                from strategies.position_lock import dca_blocked

                blocked, _why = dca_blocked(pos)
                if blocked:
                    skipped["locked_dca"] += 1
                    continue
            except Exception:
                pass
            notional = amount * mark
            params: dict = {}
            try:
                params = resolve_strategy_params(
                    {"symbol": symbol, "timeframe": tf},
                    has_position=True,
                    frozen_tier=pos.get("strategy_tier") or lot.get("strategy_tier"),
                )
            except Exception:
                params = {}
            profile = str(
                pos.get("strategy_profile")
                or lot.get("strategy_profile")
                or params.get("strategy_profile")
                or ""
            )
            sclass = str(
                pos.get("strategy_class")
                or lot.get("strategy_class")
                or params.get("strategy_class")
                or ""
            )
            has_grid = False
            try:
                from storage.grid_plan_store import load_grid_plan

                has_grid = bool(load_grid_plan(symbol, tf))
            except Exception:
                pass

            # soft indicators (fail-open)
            rsi = atr = funding = None
            try:
                from services.market_service import MarketService

                ind = MarketService().fetch_indicators(symbol, tf, mark) or {}
                rsi = ind.get("rsi")
                atr = ind.get("atr_pct")
            except Exception:
                pass
            try:
                from services.market_service import MarketService

                funding = MarketService().fetch_funding_rate(symbol)
            except Exception:
                pass

            entry_bias = "neutral"
            try:
                entry_bias = str(
                    (
                        params.get("entry_bias")
                        or pos.get("entry_bias")
                        or lot.get("entry_bias")
                        or "neutral"
                    )
                )
            except Exception:
                pass

            struct = {"free_fall": None, "reclaim_ok": None, "structure_ok": None}
            try:
                from services.dca_sniper.structure import structure_flags_for_symbol

                struct = structure_flags_for_symbol(symbol, tf)
            except Exception:
                pass

            row = {
                "symbol": symbol,
                "timeframe": tf,
                "average_entry": avg,
                "amount": amount,
                "mark": mark,
                "loss_pct": round(loss, 3),
                "notional": round(notional, 2),
                "dca_rounds": int(pos.get("dca_rounds") or lot.get("dca_rounds") or 0),
                "max_rounds": int(
                    (params.get("dca") or {}).get("max_rounds")
                    or pos.get("dca_max_rounds")
                    or lot.get("dca_max_rounds")
                    or 4
                ),
                "recovery_hold": bool(pos.get("recovery_hold") or lot.get("recovery_hold")),
                "sniper_focus": bool(pos.get("sniper_focus") or lot.get("sniper_focus")),
                "dca_heavy_used": bool(pos.get("dca_heavy_used") or lot.get("dca_heavy_used")),
                "strategy_profile": profile,
                "strategy_class": sclass,
                "has_grid_plan": has_grid,
                "rsi": rsi,
                "atr_pct": atr,
                "funding_rate_pct": funding,
                "entry_bias": entry_bias,
                "peak_epoch_high": pos.get("peak_epoch_high") or lot.get("peak_epoch_high"),
                "last_dca_at": pos.get("last_dca_at") or lot.get("last_dca_at"),
                "spendable_dca": cash.get("spendable_dca"),
                "free_fall": struct.get("free_fall"),
                "reclaim_ok": struct.get("reclaim_ok"),
                "structure_ok": struct.get("structure_ok"),
                "position_locked": bool((pos.get("lock") or {}).get("enabled")),
            }
            out.append(row)
        except Exception as e:
            skipped["err"] += 1
            log(f"dca_sniper candidate skip: {e}", "DEBUG")
            continue
    if not out:
        log(
            f"dca_sniper candidates empty lots={len(lots)} skip={skipped}",
            "INFO",
        )
    else:
        log(
            f"dca_sniper candidates n={len(out)} lots={len(lots)} skip={skipped}",
            "INFO",
        )
    return out


def execute_sniper_dca(data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Execute BUY_DCA with recovery_hold; Risk final."""
    from core.actions import BUY_DCA
    from core.models import TradeOrder
    from strategies.positions import get_position, flush_positions
    from strategies.recovery_hold import set_recovery_hold, stamp_peak_epoch_on_dca

    symbol = str(data.get("symbol") or "").strip()
    tf = str(data.get("timeframe") or "1h")
    try:
        usdt = float(data.get("usdt") or 0)
    except (TypeError, ValueError):
        usdt = 0.0
    if not symbol or usdt <= 0:
        return {"ok": False, "executed": False, "message": "bad_args"}, 400

    # Staging parity with #227 position lock (no_dca)
    try:
        from strategies.position_lock import dca_blocked

        pos0 = get_position(symbol, tf)
        blocked, why = dca_blocked(pos0)
        if blocked:
            return {
                "ok": False,
                "executed": False,
                "message": why or "position_locked_no_dca",
            }, 409
    except Exception:
        pass

    try:
        price = float(data.get("price") or 0)
    except (TypeError, ValueError):
        price = 0.0
    if price <= 0:
        try:
            from services.market_service import MarketService

            price = float(MarketService().get_price(symbol) or 0)
        except Exception:
            price = 0.0
    if price <= 0:
        return {"ok": False, "executed": False, "message": "no_price"}, 400

    set_hold = bool(data.get("set_recovery_hold", True))
    heavy = bool(data.get("heavy", True))
    reason = str(data.get("reason_code") or "DCA_HEAVY")

    order = TradeOrder(
        type="BUY",
        symbol=symbol,
        price=price,
        amount=0,
        usdt_amount=usdt,
        signal=BUY_DCA,
        source="dca_sniper",
    )
    try:
        from services.trading_service import TradingService

        trading = TradingService()
        trading.refresh()
        result = trading.execute_order(order, tf, source="dca_sniper")
        executed = bool(getattr(result, "executed", False))
        msg = str(getattr(result, "message", "") or "")
        if executed:
            pos = get_position(symbol, tf)
            if pos:
                if set_hold:
                    set_recovery_hold(pos, sniper_focus=True, heavy=heavy)
                try:
                    stamp_peak_epoch_on_dca(pos, float(getattr(result, "price", None) or price))
                except Exception:
                    pass
                # analysis meta
                if data.get("analysis_id"):
                    pos["last_sniper_analysis_id"] = str(data.get("analysis_id"))
                if data.get("score") is not None:
                    pos["last_sniper_score"] = float(data.get("score"))
                pos["last_sniper_reason"] = reason
                try:
                    flush_positions()
                except Exception:
                    pass
        return {
            "ok": True,
            "executed": executed,
            "message": msg,
            "symbol": symbol,
            "usdt": usdt,
            "reason_code": reason,
            "recovery_hold": set_hold and executed,
        }, 200 if executed else 409
    except Exception as e:
        log(f"dca_sniper execute fail {symbol}: {e}", "ERROR")
        return {"ok": False, "executed": False, "message": str(e)[:200]}, 500


def execute_fund_sell(data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Sell a winner to free cash for DCA (rotation-safe)."""
    from core.actions import SELL_FULL, SELL_PARTIAL_50
    from core.models import TradeOrder
    from strategies.positions import get_position, is_open_position
    from strategies.recovery_hold import is_recovery_hold_active

    symbol = str(data.get("symbol") or "").strip()
    tf = str(data.get("timeframe") or "1h")
    if not symbol:
        return {"ok": False, "executed": False, "message": "bad_symbol"}, 400

    pos = get_position(symbol, tf)
    if not is_open_position(pos):
        return {"ok": False, "executed": False, "message": "no_position"}, 404
    if is_recovery_hold_active(pos):
        return {
            "ok": False,
            "executed": False,
            "message": "cannot_fund_sell_recovery_hold",
        }, 409

    # Position lock no_auto_sell: fund-from-winner is auto, not manual
    try:
        from strategies.position_lock import auto_sell_blocked

        locked, why = auto_sell_blocked(pos, "dca_sniper_fund")
        if locked:
            return {
                "ok": False,
                "executed": False,
                "message": why or "position_locked_no_auto_sell",
            }, 409
    except Exception:
        pass

    try:
        price = float(data.get("price") or 0)
    except (TypeError, ValueError):
        price = 0.0
    if price <= 0:
        try:
            from services.market_service import MarketService

            price = float(MarketService().get_price(symbol) or 0)
        except Exception:
            price = 0.0
    amount = float(pos.get("amount") or 0)
    partial = bool(data.get("partial", False))
    if partial:
        amount = amount * 0.5
    if amount <= 0 or price <= 0:
        return {"ok": False, "executed": False, "message": "bad_amount_or_price"}, 400

    signal = SELL_PARTIAL_50 if partial else SELL_FULL
    order = TradeOrder(
        type="SELL",
        symbol=symbol,
        price=price,
        amount=amount,
        signal=signal,
        source="dca_sniper_fund",
    )
    try:
        from services.trading_service import TradingService

        trading = TradingService()
        trading.refresh()
        result = trading.execute_order(order, tf, source="dca_sniper_fund")
        return {
            "ok": True,
            "executed": bool(getattr(result, "executed", False)),
            "message": str(getattr(result, "message", "") or ""),
            "symbol": symbol,
            "partial": partial,
        }, 200 if getattr(result, "executed", False) else 409
    except Exception as e:
        return {"ok": False, "executed": False, "message": str(e)[:200]}, 500


def promote_position(data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    from strategies.positions import get_position, flush_positions
    from strategies.recovery_hold import maybe_promote_recovery_hold, set_recovery_hold

    symbol = str(data.get("symbol") or "").strip()
    tf = str(data.get("timeframe") or "1h")
    force_clear = bool(data.get("force_clear"))
    pos = get_position(symbol, tf)
    if not pos:
        return {"ok": False, "message": "no_position"}, 404
    if force_clear:
        pos["recovery_hold"] = False
        pos["sniper_focus"] = False
        pos["recovery_hold_clear_reason"] = "force"
        pos["recovery_hold_cleared_at"] = datetime.now().isoformat()
        try:
            flush_positions()
        except Exception:
            pass
        return {"ok": True, "cleared": True, "reason": "force"}, 200
    try:
        mark = float(data.get("price") or 0)
    except (TypeError, ValueError):
        mark = 0.0
    if mark <= 0:
        mark = float(pos.get("average_entry") or 0)
    cleared = maybe_promote_recovery_hold(pos, mark)
    if cleared:
        try:
            flush_positions()
        except Exception:
            pass
    return {"ok": True, "cleared": cleared}, 200


def register_dca_sniper_routes(app: Flask) -> None:
    @app.route("/internal/dca-sniper/candidates", methods=["GET"])
    def dca_sniper_candidates():
        ok, err = _check_token()
        if not ok:
            return err
        cands = _build_candidates()
        return jsonify({"ok": True, "candidates": cands, "n": len(cands)}), 200

    @app.route("/internal/dca-sniper/cash", methods=["GET"])
    def dca_sniper_cash():
        ok, err = _check_token()
        if not ok:
            return err
        body = {"ok": True, **_snapshot_cash()}
        # Funding candidates: green open lots (not recovery_hold)
        winners = []
        try:
            from strategies.positions import get_position, list_active_positions
            from price_fetcher import get_prices_batch

            lots = list_active_positions()
            price_map: dict[str, float] = {}
            try:
                raw = get_prices_batch(
                    [str(l.get("symbol") or "") for l in lots if l.get("symbol")]
                ) or {}
                for s, px in raw.items():
                    try:
                        v = float(px or 0)
                    except (TypeError, ValueError):
                        v = 0.0
                    if v > 0:
                        price_map[str(s).upper()] = v
            except Exception:
                pass
            for lot in lots:
                symbol = str(lot.get("symbol") or "")
                tf = str(lot.get("timeframe") or "1h")
                pos = get_position(symbol, tf) or lot
                avg = float(pos.get("average_entry") or lot.get("average_entry") or 0)
                amount = float(pos.get("amount") or lot.get("amount") or 0)
                if avg <= 0 or amount <= 0 or not symbol:
                    continue
                if pos.get("recovery_hold") or pos.get("sniper_focus"):
                    continue
                # fund-from-winner is auto-sell: skip no_auto_sell locks
                try:
                    from strategies.position_lock import auto_sell_blocked

                    locked, _ = auto_sell_blocked(pos, "dca_sniper_fund")
                    if locked:
                        continue
                except Exception:
                    pass
                mark = float(
                    pos.get("current_price")
                    or lot.get("current_price")
                    or lot.get("mark")
                    or price_map.get(symbol.upper())
                    or 0
                )
                if mark <= 0:
                    continue
                gain = (mark / avg - 1.0) * 100.0
                if gain < 3.0:
                    continue
                winners.append(
                    {
                        "symbol": symbol,
                        "timeframe": tf,
                        "gain_pct": round(gain, 2),
                        "notional": round(amount * mark, 2),
                        "mark": mark,
                    }
                )
            winners.sort(key=lambda w: float(w.get("gain_pct") or 0), reverse=True)
        except Exception:
            pass
        body["winners"] = winners[:10]
        return jsonify(body), 200

    @app.route("/internal/dca-sniper/status", methods=["GET"])
    def dca_sniper_status():
        ok, err = _check_token()
        if not ok:
            return err
        cfg = dca_sniper_config()
        holds = 0
        try:
            from strategies.positions import list_active_positions

            for p in list_active_positions():
                if p.get("recovery_hold") or p.get("sniper_focus"):
                    holds += 1
        except Exception:
            pass
        return jsonify(
            {
                "ok": True,
                "enabled": dca_sniper_enabled(),
                "config": {
                    "max_focus_slots": cfg["max_focus_slots"],
                    "exclude_grid": cfg["exclude_grid"],
                    "disable_cycle_dca_when_enabled": cfg[
                        "disable_cycle_dca_when_enabled"
                    ],
                },
                "open_focus_holds": holds,
            }
        ), 200

    @app.route("/internal/dca-sniper/execute", methods=["POST"])
    def dca_sniper_execute():
        ok, err = _check_token()
        if not ok:
            return err
        data = request.get_json(silent=True) or {}
        body, status = execute_sniper_dca(data)
        log(
            f"dca_sniper execute {data.get('symbol')} usdt={data.get('usdt')} "
            f"exec={body.get('executed')} msg={str(body.get('message') or '')[:80]}",
            "INFO",
        )
        return jsonify(body), status

    @app.route("/internal/dca-sniper/fund-sell", methods=["POST"])
    def dca_sniper_fund_sell():
        ok, err = _check_token()
        if not ok:
            return err
        data = request.get_json(silent=True) or {}
        body, status = execute_fund_sell(data)
        return jsonify(body), status

    @app.route("/internal/dca-sniper/promote", methods=["POST"])
    def dca_sniper_promote():
        ok, err = _check_token()
        if not ok:
            return err
        data = request.get_json(silent=True) or {}
        body, status = promote_position(data)
        return jsonify(body), status

    log(
        "dca_sniper routes registered "
        "(/internal/dca-sniper/{candidates,cash,status,execute,fund-sell,promote})",
        "INFO",
    )
