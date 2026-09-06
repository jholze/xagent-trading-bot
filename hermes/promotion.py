"""Pending promotions, veto window, snapshots, rollback (#308 slice 2).

State machine:
  qualified → pending → applied | vetoed | suppressed
  applied → post-apply ok | rolled_back
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

from data_manager import atomic_write_json
from hermes.memory import store
from hermes.significance import format_win_probability
from logger import log

_DEFAULT_VETO_MIN = 10
_DEFAULT_MAX_PER_DAY = 1
_DEFAULT_POST_APPLY_HOURS = 24
_DEFAULT_POST_APPLY_MIN_TRADES = 5
_DEFAULT_WIN_RATE_GAP_PP = 20.0


def _now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now


def _parse(ts: str | datetime | None) -> datetime | None:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "")[:26])
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return _now(dt).isoformat()


def _promo_cfg(agent=None) -> dict:
    if agent is not None:
        hermes = getattr(agent, "hermes", None)
        if isinstance(hermes, dict):
            return hermes.get("promotion") or {}
        cfg = getattr(agent, "config", None)
        if cfg is not None:
            return (getattr(cfg, "hermes_config", None) or {}).get("promotion") or {}
    try:
        from core.config import get_bot_config

        return get_bot_config().hermes_config.get("promotion") or {}
    except Exception:
        return {}


def _day_key(now: datetime) -> str:
    return _now(now).date().isoformat()


def _ensure_daily(state: dict, now: datetime) -> dict:
    daily = state.setdefault("daily", {"date": "", "variables": [], "promotions_applied": 0})
    key = _day_key(now)
    if daily.get("date") != key:
        daily["date"] = key
        daily["variables"] = []
        daily["promotions_applied"] = 0
    return daily


def note_variable_tested(variable: str, now: datetime | None = None) -> int:
    """Record a distinct variable tested today; return today's n."""
    now = _now(now)
    state = store.load_promotion_state()
    daily = _ensure_daily(state, now)
    var = str(variable or "")
    variables = list(daily.get("variables") or [])
    if var and var not in variables:
        variables.append(var)
        daily["variables"] = variables
        store.save_promotion_state(state)
    return max(1, len(daily.get("variables") or []))


def variables_tested_today(now: datetime | None = None) -> int:
    now = _now(now)
    state = store.load_promotion_state()
    daily = _ensure_daily(state, now)
    n = len(daily.get("variables") or [])
    return max(1, n) if n else 1


def load_pending() -> list[dict]:
    return list(store.load_promotion_state().get("pending") or [])


def format_veto_message(record: dict, window_min: int = _DEFAULT_VETO_MIN) -> str:
    wp = format_win_probability(
        float(record.get("win_probability") or 0),
        int(record.get("total_trades") or 0),
    )
    var = record.get("variable", "?")
    old = record.get("old_value", "?")
    new = record.get("new_value", "?")
    exp_id = record.get("id") or record.get("experiment_id") or ""
    return (
        f"Hermes will {var} {old} → {new} setzen: {wp}, Hold-out ok. "
        f"Veto mit /hermes_veto {exp_id} innerhalb {int(window_min)} min."
    )


def write_snapshot(
    experiment_id: str,
    *,
    baseline: dict,
    strategy_slice: dict | None,
) -> str:
    path = store.snapshot_file(experiment_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "experiment_id": experiment_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "baseline": copy.deepcopy(baseline),
        "strategy_slice": copy.deepcopy(strategy_slice) if strategy_slice else None,
    }
    atomic_write_json(str(path), payload)
    return str(path)


def load_snapshot(experiment_id: str) -> dict | None:
    path = store.snapshot_file(experiment_id)
    if not path.exists():
        return None
    try:
        import json

        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"Hermes snapshot read failed ({path}): {e}", "WARNING")
        return None


def latest_snapshot_id() -> str | None:
    applied = store.load_promotion_state().get("applied") or []
    if applied:
        return applied[-1].get("experiment_id")
    folder = store.MEMORY_DIR / "snapshots"
    if not folder.exists():
        return None
    files = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime)
    return files[-1].stem if files else None


def _strategy_slice(symbol: str, timeframe: str) -> dict | None:
    try:
        from data_manager import get_config

        cfg = get_config()
        if not isinstance(cfg, dict):
            cfg = getattr(cfg, "raw", None) or {}
        for entry in cfg.get("strategies") or []:
            if entry.get("symbol") == symbol and entry.get("timeframe", "4h") == timeframe:
                return copy.deepcopy(entry)
    except Exception:
        return None
    return None


def _open_pending(state: dict) -> list[dict]:
    return [p for p in (state.get("pending") or []) if p.get("status") == "pending"]


def can_queue_today(now: datetime | None = None, agent=None) -> bool:
    now = _now(now)
    cfg = _promo_cfg(agent)
    cap = int(cfg.get("max_promotions_per_day", _DEFAULT_MAX_PER_DAY))
    state = store.load_promotion_state()
    daily = _ensure_daily(state, now)
    used = int(daily.get("promotions_applied") or 0) + len(_open_pending(state))
    return used < cap


def queue_or_suppress(agent, record: dict, *, observe: bool, now: datetime | None = None) -> dict:
    """Store a pending promotion. Observe → status suppressed, never applied."""
    now = _now(now)
    cfg = _promo_cfg(agent)
    window = int(cfg.get("veto_window_min", _DEFAULT_VETO_MIN))
    exp_id = record.get("id") or ""
    item = {
        "experiment_id": exp_id,
        "variable": record.get("variable"),
        "old_value": record.get("old_value"),
        "new_value": record.get("new_value"),
        "params": record.get("params") or {},
        "baseline_params": record.get("baseline_params") or {},
        "symbol": record.get("symbol"),
        "timeframe": record.get("timeframe") or "4h",
        "win_probability": record.get("win_probability"),
        "total_trades": record.get("total_trades"),
        "threshold_used": record.get("threshold_used"),
        "variant_metrics": record.get("variant_metrics") or {},
        "apply_after": _iso(now + timedelta(minutes=window)),
        "created_at": _iso(now),
        "status": "suppressed" if observe else "pending",
    }
    if observe:
        state = store.load_promotion_state()
        state.setdefault("pending", []).append(item)
        store.save_promotion_state(state)
        store.update_experiment(exp_id, verdict="suppressed")
        log("hermes observe: promotion suppressed", "INFO")
        return item

    if not can_queue_today(now, agent=agent):
        reason = "max_promotions_per_day"
        store.update_experiment(exp_id, verdict="rejected", verdict_reason=reason)
        item["status"] = "rejected"
        item["reason"] = reason
        return item

    state = store.load_promotion_state()
    _ensure_daily(state, now)
    state.setdefault("pending", []).append(item)
    store.save_promotion_state(state)
    store.update_experiment(exp_id, verdict="pending", apply_after=item["apply_after"])
    _notify_veto_window(record, window)
    return item


def _notify_veto_window(record: dict, window_min: int) -> None:
    try:
        from telegram_notifier import send_telegram_message

        send_telegram_message(format_veto_message(record, window_min))
    except Exception as e:
        log(f"Hermes veto-window notify failed: {e}", "WARNING")


def veto(experiment_id: str, now: datetime | None = None) -> dict:
    now = _now(now)
    state = store.load_promotion_state()
    found = None
    for item in state.get("pending") or []:
        if item.get("experiment_id") == experiment_id and item.get("status") == "pending":
            item["status"] = "vetoed"
            item["vetoed_at"] = _iso(now)
            found = item
            break
    if found is None:
        return {"status": "not_found", "experiment_id": experiment_id}
    store.save_promotion_state(state)
    store.update_experiment(experiment_id, verdict="vetoed")
    try:
        from telegram_notifier import send_telegram_message

        send_telegram_message(
            f"🧠 <b>Hermes — Veto</b>\nPromotion {experiment_id} wurde storniert."
        )
    except Exception:
        pass
    return found


def _apply_one(agent, item: dict, now: datetime) -> dict:
    exp_id = item["experiment_id"]
    symbol = item.get("symbol")
    timeframe = item.get("timeframe") or "4h"
    params = item.get("params") or {}
    current = store.load_profile(symbol, timeframe)
    baseline_before = {
        "symbol": symbol,
        "timeframe": timeframe,
        "params": copy.deepcopy(current.get("params") or item.get("baseline_params") or {}),
        "metrics": copy.deepcopy(current.get("metrics") or {}),
        "updated_at": current.get("updated_at"),
    }
    slice_before = _strategy_slice(symbol, timeframe)
    snap_path = write_snapshot(exp_id, baseline=baseline_before, strategy_slice=slice_before)

    new_baseline = {
        "symbol": symbol,
        "timeframe": timeframe,
        "params": dict(params),
        "metrics": item.get("variant_metrics") or {},
        "hermes_experiment_id": exp_id,
    }
    new_baseline["params"]["hermes_experiment_id"] = exp_id
    store.save_baseline(new_baseline)
    profile = store.load_profile(symbol, timeframe)
    profile["hermes_experiment_id"] = exp_id
    profile["hermes_updated_at"] = _iso(now)
    store.save_profile(symbol, timeframe, profile)

    if agent is not None and hasattr(agent, "_sync_to_config"):
        agent._sync_to_config(new_baseline, exp_id)

    record = store.find_experiment(exp_id) or item
    record = {
        **record,
        "verdict": "promoted",
        "snapshot_path": snap_path,
        "id": exp_id,
    }
    if agent is not None and hasattr(agent, "_notify_promotion"):
        proposal = type("P", (), {
            "variable": item.get("variable"),
            "old_value": item.get("old_value"),
            "new_value": item.get("new_value"),
        })()
        try:
            agent._notify_promotion(record, proposal, item.get("variant_metrics") or {}, symbol)
        except Exception as e:
            log(f"Hermes promotion notify failed: {e}", "WARNING")

    hours = float(_promo_cfg(agent).get("post_apply_validation_hours", _DEFAULT_POST_APPLY_HOURS))
    applied_at = _iso(now)
    store.update_experiment(
        exp_id,
        verdict="promoted",
        snapshot_path=snap_path,
        applied_at=applied_at,
        post_apply_after=_iso(now + timedelta(hours=hours)),
    )
    item["status"] = "applied"
    item["applied_at"] = applied_at
    item["snapshot_path"] = snap_path
    item["post_apply_after"] = _iso(now + timedelta(hours=hours))
    item["post_apply_status"] = "pending"
    return item


def rollback(experiment_id: str | None = None, *, agent=None, now: datetime | None = None) -> dict:
    now = _now(now)
    exp_id = experiment_id or latest_snapshot_id()
    if not exp_id:
        return {"verdict": "not_found", "reason": "no snapshot"}
    snap = load_snapshot(exp_id)
    if not snap:
        return {"verdict": "not_found", "experiment_id": exp_id, "reason": "snapshot missing"}
    baseline = snap.get("baseline") or {}
    symbol = baseline.get("symbol")
    timeframe = baseline.get("timeframe", "4h")
    params = dict(baseline.get("params") or {})
    params.pop("hermes_experiment_id", None)
    restored = {
        "symbol": symbol,
        "timeframe": timeframe,
        "params": params,
        "metrics": baseline.get("metrics") or {},
    }
    store.save_baseline(restored)
    profile = store.load_profile(symbol, timeframe)
    profile.pop("hermes_experiment_id", None)
    profile["params"] = params
    store.save_profile(symbol, timeframe, profile)
    if agent is not None and hasattr(agent, "_sync_to_config"):
        agent._sync_to_config(restored, exp_id)
    store.update_experiment(exp_id, verdict="rolled_back", rolled_back_at=_iso(now))
    state = store.load_promotion_state()
    for item in state.get("applied") or []:
        if item.get("experiment_id") == exp_id:
            item["post_apply_status"] = "reverted"
            item["rolled_back_at"] = _iso(now)
    for item in state.get("pending") or []:
        if item.get("experiment_id") == exp_id and item.get("status") == "applied":
            item["post_apply_status"] = "reverted"
    store.save_promotion_state(state)
    try:
        from telegram_notifier import send_telegram_message

        send_telegram_message(
            f"🧠 <b>Hermes — Rollback</b>\n"
            f"Experiment {exp_id} auf den Snapshot vor der Promotion zurückgesetzt."
        )
    except Exception:
        pass
    return {"verdict": "rolled_back", "experiment_id": exp_id, "snapshot_path": str(store.snapshot_file(exp_id))}


def _notify_auto_revert(item: dict, decision) -> None:
    try:
        from telegram_notifier import send_telegram_message

        wp = format_win_probability(
            float(item.get("win_probability") or 0),
            int(item.get("total_trades") or 0),
        )
        send_telegram_message(
            f"🧠 <b>Hermes — Auto-Revert</b>\n"
            f"{item.get('symbol')} Experiment {item.get('experiment_id')}: "
            f"{decision.reason}. {wp}. "
            f"Realized PnL {decision.realized_pnl:.2f} USDT, "
            f"Win-Rate {decision.win_rate:.1f}% vs Backtest {decision.backtest_win_rate:.1f}% "
            f"({decision.n_trades} Trades)."
        )
    except Exception as e:
        log(f"Hermes auto-revert notify failed: {e}", "WARNING")


def tick(agent=None, now: datetime | None = None, trades: list[dict] | None = None) -> dict:
    """Apply due pending promotions and run due post-apply checks."""
    now = _now(now)
    if agent is not None and getattr(agent, "_is_observe_mode", lambda: False)():
        return {"applied": [], "reverted": []}

    state = store.load_promotion_state()
    _ensure_daily(state, now)
    applied: list[dict] = []
    reverted: list[dict] = []
    dirty = False

    for item in list(state.get("pending") or []):
        if item.get("status") != "pending":
            continue
        apply_after = _parse(item.get("apply_after"))
        if apply_after is None or now < apply_after:
            continue
        try:
            done = _apply_one(agent, item, now)
        except Exception as e:
            log(f"Hermes apply pending {item.get('experiment_id')} failed: {e}", "ERROR")
            continue
        daily = _ensure_daily(state, now)
        daily["promotions_applied"] = int(daily.get("promotions_applied") or 0) + 1
        state.setdefault("applied", []).append(copy.deepcopy(done))
        applied.append(done)
        dirty = True

    if dirty:
        store.save_promotion_state(state)

    from hermes.post_apply import evaluate as eval_post, load_ledger_trades

    cfg = _promo_cfg(agent)
    min_trades = int(cfg.get("post_apply_min_trades", _DEFAULT_POST_APPLY_MIN_TRADES))
    gap = float(cfg.get("post_apply_win_rate_gap_pp", _DEFAULT_WIN_RATE_GAP_PP))
    ledger = trades
    post_dirty = False
    for item in list(state.get("applied") or []):
        if item.get("post_apply_status") not in (None, "pending", "no_verdict_yet"):
            continue
        due = _parse(item.get("post_apply_after"))
        if due is None or now < due:
            continue
        if ledger is None:
            ledger = load_ledger_trades(item.get("symbol"))
        decision = eval_post(
            experiment_id=item.get("experiment_id"),
            symbol=item.get("symbol"),
            applied_at=item.get("applied_at"),
            variant_metrics=item.get("variant_metrics") or {},
            trades=ledger,
            min_trades=min_trades,
            win_rate_gap_pp=gap,
        )
        if decision.action == "no_verdict_yet":
            item["post_apply_status"] = "no_verdict_yet"
            post_dirty = True
            continue
        if decision.action == "ok":
            item["post_apply_status"] = "ok"
            post_dirty = True
            continue
        if decision.action == "revert":
            rb = rollback(item.get("experiment_id"), agent=agent, now=now)
            item["post_apply_status"] = "reverted"
            reverted.append(rb)
            _notify_auto_revert(item, decision)
            post_dirty = True

    if post_dirty:
        store.save_promotion_state(state)
    return {"applied": applied, "reverted": reverted}


def tick_hermes_promotions(now: datetime | None = None) -> dict:
    """Entry point for the background runtime / Hermes cycle tick."""
    try:
        state = store.load_promotion_state()
        pending = [p for p in (state.get("pending") or []) if p.get("status") == "pending"]
        applied = [
            a for a in (state.get("applied") or [])
            if a.get("post_apply_status") in (None, "pending", "no_verdict_yet")
        ]
        if not pending and not applied:
            return {"applied": [], "reverted": []}
        from core.config import get_bot_config
        from hermes.agent import HermesAgent

        return tick(HermesAgent(get_bot_config()), now=now)
    except Exception as e:
        log(f"Hermes promotion tick failed: {e}", "WARNING")
        return {"applied": [], "reverted": [], "error": str(e)}
