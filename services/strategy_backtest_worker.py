"""Background worker: staggered adaptive strategy backtests for all coins."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

from core.config import get_bot_config
from data_manager import (
    get_strategy_backtest_entry,
    list_strategy_targets,
    save_strategy_backtest_entry,
)
from intelligence.strategy_backtest import StrategyBacktester, classify_coin, coin_key
from logger import log
from services.strategy_auto_tuner import StrategyAutoTuner
from services.strategy_review_scheduler import StrategyReviewScheduler
from telegram_notifier import send_telegram_message


class StrategyBacktestWorker:
    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        self._worker_lock = threading.Lock()
        self._running = False
        self._last_job_finished: datetime | None = None
        self._force_queue: list[tuple[str, str]] = []

    @classmethod
    def get(cls) -> "StrategyBacktestWorker":
        with cls._lock:
            if cls._instance is None:
                cls._instance = StrategyBacktestWorker()
            return cls._instance

    def tick(self):
        cfg = get_bot_config()
        bt_cfg = cfg.raw.get("strategy_backtest", {})
        if not bt_cfg.get("enabled", True) or not bt_cfg.get("auto_run", True):
            return
        if self._running:
            return
        if not self._stagger_elapsed(bt_cfg):
            return

        target = self._next_due_target()
        if not target:
            return

        symbol, timeframe, params = target
        thread = threading.Thread(
            target=self._run_job,
            args=(symbol, timeframe, params),
            daemon=True,
        )
        thread.start()

    def force_enqueue(self, symbol: str, timeframe: str = "4h"):
        self._force_queue.insert(0, (symbol, timeframe))

    def status_lines(self) -> list[str]:
        lines = []
        now = datetime.now()
        for entry in list_strategy_targets():
            symbol = entry["symbol"]
            tf = entry.get("timeframe", "4h")
            key = coin_key(symbol, tf)
            stored = get_strategy_backtest_entry(key)
            if stored.get("locked"):
                lines.append(f"<b>{symbol}</b> {tf} — <i>locked</i>")
                continue
            nxt = stored.get("next_review_at", "")
            reason = stored.get("review_reason", "—")
            if nxt:
                try:
                    due = datetime.fromisoformat(str(nxt).replace("Z", ""))
                    label = due.strftime("%a %H:%M")
                    if due <= now:
                        label += " (fällig)"
                except Exception:
                    label = nxt
            else:
                label = "bald (neu)"
            lines.append(f"<b>{symbol}</b> {tf} — nächster Check: <code>{label}</code>\n  <i>{reason}</i>")
        return lines

    def _stagger_elapsed(self, bt_cfg: dict) -> bool:
        stagger = float(bt_cfg.get("stagger_minutes", 15))
        if self._last_job_finished is None:
            return True
        return datetime.now() - self._last_job_finished >= timedelta(minutes=stagger)

    def _next_due_target(self):
        if self._force_queue:
            symbol, tf = self._force_queue.pop(0)
            for entry in list_strategy_targets():
                if entry["symbol"] == symbol and entry.get("timeframe", "4h") == tf:
                    return entry["symbol"], entry.get("timeframe", "4h"), entry
            return None

        now = datetime.now()
        due = []
        for entry in list_strategy_targets():
            symbol = entry["symbol"]
            tf = entry.get("timeframe", "4h")
            key = coin_key(symbol, tf)
            stored = get_strategy_backtest_entry(key)
            if stored.get("locked"):
                continue
            nxt_raw = stored.get("next_review_at")
            if not nxt_raw:
                due.append((datetime.min, entry))
                continue
            try:
                nxt = datetime.fromisoformat(str(nxt_raw).replace("Z", ""))
            except Exception:
                due.append((datetime.min, entry))
                continue
            if nxt <= now:
                due.append((nxt, entry))

        if not due:
            return None
        due.sort(key=lambda x: x[0])
        entry = due[0][1]
        return entry["symbol"], entry.get("timeframe", "4h"), entry

    def _run_job(self, symbol: str, timeframe: str, strategy_entry: dict):
        with self._worker_lock:
            if self._running:
                return
            self._running = True
        try:
            cfg = get_bot_config()
            backtester = StrategyBacktester(cfg.raw)
            result = backtester.compare_variants(symbol, timeframe, strategy_entry)
            key = result.coin_key
            previous = get_strategy_backtest_entry(key)

            tuner = StrategyAutoTuner(cfg.raw)
            applied = {}
            apply_reason = ""
            apply_ok = False
            best = result.best_variant
            if best:
                should, apply_reason = tuner.should_apply(
                    result.improvement_pct,
                    best,
                    result.metrics.to_dict(),
                )
                if should:
                    ok, applied, msg = tuner.apply(symbol, timeframe, best)
                    apply_ok = ok
                    apply_reason = msg

            scheduler = StrategyReviewScheduler(cfg.raw)
            updated_entry = strategy_entry if not applied else self._fresh_strategy_entry(symbol, timeframe)
            next_at, hours, reason = scheduler.compute_next_review(
                result,
                updated_entry or strategy_entry,
                param_applied=bool(applied),
                previous_entry=previous,
            )

            record = {
                "symbol": symbol,
                "timeframe": timeframe,
                "last_run": datetime.now().isoformat(),
                "next_review_at": next_at.isoformat(),
                "review_interval_hours": round(hours, 1),
                "review_reason": reason,
                "coin_class": classify_coin(symbol, updated_entry or strategy_entry),
                "metrics": result.metrics.to_dict(),
                "improvement_pct": result.improvement_pct,
                "applied_params": applied,
                "skipped_reason": "" if apply_ok or applied else apply_reason,
            }
            if previous.get("locked"):
                record["locked"] = True
            save_strategy_backtest_entry(key, record)

            if applied and cfg.raw.get("strategy_backtest", {}).get("telegram_on_apply", True):
                from notifications.user_explain import describe_param_change
                changes = "\n".join(f"• {describe_param_change(k, v)}" for k, v in applied.items())
                send_telegram_message(
                    f"🔧 <b>Strategie angepasst</b> — {symbol} {timeframe}\n"
                    f"<b>Warum:</b> Backtest ({result.days} Tage) war mit diesen Werten besser.\n"
                    f"{changes}\n"
                    f"Sim-PnL: {result.metrics.pnl_sim:.1f} USDT | Nächster Check: {next_at.strftime('%a %H:%M')}"
                )
            log(
                f"Strategy backtest {symbol} {timeframe}: churn={result.metrics.signal_churn} "
                f"pnl={result.metrics.pnl_sim} next={next_at.isoformat()}",
                "INFO",
            )
        except Exception as e:
            log(f"Strategy backtest job failed for {symbol}: {e}", "WARNING")
        finally:
            self._last_job_finished = datetime.now()
            self._running = False

    def _fresh_strategy_entry(self, symbol: str, timeframe: str) -> dict | None:
        from data_manager import reload_config

        reload_config()
        for entry in get_bot_config().raw.get("strategies", []):
            if entry.get("symbol") == symbol and entry.get("timeframe", "4h") == timeframe:
                return entry
        return None


def tick_strategy_backtest():
    StrategyBacktestWorker.get().tick()