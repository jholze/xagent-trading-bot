"""Adaptive per-coin next_review_at scheduling after strategy backtests."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from intelligence.strategy_backtest import BacktestRunResult, classify_coin


class StrategyReviewScheduler:
    def __init__(self, config: dict = None):
        from core.config import get_bot_config

        self.cfg = config or get_bot_config().raw
        self.bt_cfg = self.cfg.get("strategy_backtest", {})

    def compute_next_review(
        self,
        result: BacktestRunResult,
        strategy_entry: dict,
        *,
        param_applied: bool = False,
        previous_entry: dict = None,
    ) -> tuple[datetime, float, str]:
        now = datetime.now()
        if param_applied:
            hours = float(self.bt_cfg.get("post_apply_validation_hours", 36))
            return now + timedelta(hours=hours), hours, "post-apply validation"

        coin_class = classify_coin(result.symbol, strategy_entry)
        base_map = self.bt_cfg.get("base_review_hours", {})
        base_hours = float(base_map.get(coin_class, base_map.get("default", 48)))

        metrics = result.metrics
        vol_factor = 0.5 if metrics.atr_pct > 5 else (1.5 if metrics.atr_pct < 2 else 1.0)
        churn = metrics.signal_churn
        churn_factor = 0.6 if churn > 8 else (1.4 if churn < 2 else 1.0)

        vp = metrics.volume_profile
        us_factor = 0.7 if vp.us_session_volume_ratio > 0.6 else 1.0

        stability_factor = 1.0
        if previous_entry:
            prev_metrics = previous_entry.get("metrics", {})
            prev_pnl = float(prev_metrics.get("pnl_sim", 0))
            if abs(prev_pnl - metrics.pnl_sim) < 1.0 and not result.best_variant:
                stability_factor = 1.5

        if not result.best_variant and result.improvement_pct < float(self.bt_cfg.get("min_improvement_pct", 10)):
            base_hours *= 1.3

        hours = base_hours * vol_factor * churn_factor * us_factor * stability_factor
        min_h = float(self.bt_cfg.get("min_review_hours", 12))
        max_h = float(self.bt_cfg.get("max_review_hours", 336))
        hours = max(min_h, min(max_h, hours))

        next_at = now + timedelta(hours=hours)
        reason_parts = [f"class={coin_class}", f"atr={metrics.atr_pct:.1f}%", f"churn={churn}"]
        if vp.dominant_session == "us":
            reason_parts.append("US-session volume")

        us_cfg = self.bt_cfg.get("us_market", {})
        if us_cfg.get("prefer_review_after_close") and vp.us_session_volume_ratio > 0.6:
            next_at = self._align_after_us_close(next_at, us_cfg)
            reason_parts.append("aligned after US close")

        return next_at, hours, "; ".join(reason_parts)

    def _align_after_us_close(self, dt: datetime, us_cfg: dict) -> datetime:
        try:
            tz = ZoneInfo(us_cfg.get("timezone", "Europe/Berlin"))
        except Exception:
            return dt
        close_parts = str(us_cfg.get("close", "22:00")).split(":")
        close_h, close_m = int(close_parts[0]), int(close_parts[1])
        local = dt.astimezone(tz)
        target = local.replace(hour=close_h, minute=close_m, second=0, microsecond=0)
        if local >= target:
            target += timedelta(days=1)
        target += timedelta(minutes=30)
        return target.astimezone(dt.tzinfo) if dt.tzinfo else target.replace(tzinfo=None)