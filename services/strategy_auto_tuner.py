"""Apply strategy backtest suggestions to config.strategies with guardrails."""

from __future__ import annotations

from copy import deepcopy

from data_manager import get_config, reload_config, save_config
from logger import log


TUNABLE_KEYS = (
    "rsi_buy_low",
    "rsi_buy_high",
    "volume_multiplier",
    "min_hours_between_buys",
    "min_hours_between_sells",
)


class StrategyAutoTuner:
    def __init__(self, config: dict = None):
        from core.config import get_bot_config

        self.cfg = config or get_bot_config().raw
        self.bt_cfg = self.cfg.get("strategy_backtest", {})
        self.guardrails = self.bt_cfg.get("guardrails", {})

    def should_apply(self, improvement_pct: float, best_variant: dict, metrics: dict) -> tuple[bool, str]:
        if not self.bt_cfg.get("auto_apply", True):
            return False, "auto_apply disabled"
        if not best_variant:
            return False, "no better variant"
        min_sig = int(self.bt_cfg.get("min_signals_for_valid", 3))
        if int(metrics.get("signal_churn", 0)) < min_sig:
            return False, f"insufficient signals ({metrics.get('signal_churn', 0)} < {min_sig})"

        min_imp = float(self.bt_cfg.get("min_improvement_pct", 10))
        live = self.cfg.get("live", {})
        if self.cfg.get("trading_mode") == "live" and not live.get("dry_run", True):
            min_imp = max(min_imp, 15.0)
        if improvement_pct < min_imp:
            return False, f"improvement {improvement_pct:.1f}% < {min_imp}%"
        return True, "approved"

    def apply(self, symbol: str, timeframe: str, new_params: dict) -> tuple[bool, dict, str]:
        cfg = deepcopy(get_config())
        strategies = cfg.setdefault("strategies", [])
        target = None
        for entry in strategies:
            if entry.get("symbol") == symbol and entry.get("timeframe", "4h") == timeframe:
                target = entry
                break
        if not target:
            return False, {}, f"No strategy entry for {symbol} {timeframe}"

        applied = {}
        for key in TUNABLE_KEYS:
            if key not in new_params:
                continue
            old_val = target.get(key)
            new_val = self._clamp_param(key, old_val, new_params[key])
            if new_val is None:
                continue
            if old_val != new_val:
                target[key] = new_val
                applied[key] = new_val

        if not applied:
            return False, {}, "no param changes within guardrails"

        if save_config(cfg):
            reload_config()
            log(f"Strategy auto-tuned {symbol} {timeframe}: {applied}", "INFO")
            return True, applied, "applied"
        return False, {}, "config save failed"

    def _clamp_param(self, key: str, old_val, new_val):
        rules = self.guardrails.get(key, {})
        if rules:
            try:
                new_val = float(new_val) if key == "volume_multiplier" else int(new_val)
            except (TypeError, ValueError):
                return None
            lo = rules.get("min")
            hi = rules.get("max")
            max_delta = rules.get("max_delta")
            if lo is not None:
                new_val = max(lo, new_val)
            if hi is not None:
                new_val = min(hi, new_val)
            if max_delta is not None and old_val is not None:
                try:
                    old_num = float(old_val)
                    delta = float(max_delta)
                    new_val = max(old_num - delta, min(old_num + delta, float(new_val)))
                    if key != "volume_multiplier":
                        new_val = int(round(new_val))
                    else:
                        new_val = round(float(new_val), 2)
                except (TypeError, ValueError):
                    pass
            return new_val
        return new_val