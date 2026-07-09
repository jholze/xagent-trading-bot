"""Delta gating for cycle Telegram notifications (summary + hold explanations)."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any


def _cycle_notifications_config(config=None) -> dict:
    from core.config import get_bot_config

    cfg = config or get_bot_config()
    defaults = {
        "mode": "delta",
        "send_on_trade": True,
        "send_on_blocked": True,
        "send_on_nav_delta_pct": 0.5,
        "send_on_new_decision": True,
        "hold_explanation_max_per_cycle": 1,
        "hold_explanation_cooldown_hours": 6,
        "digest_merge": True,
        "notify_hermes_rejected": False,
    }
    raw = cfg.observability_config.get("cycle_notifications", {})
    return {**defaults, **raw}


def decision_fingerprint(coin_results: list | None) -> str:
    """Stable fingerprint of non-HOLD decisions in a cycle."""
    parts = []
    for r in coin_results or []:
        action = (r.get("normalized_action") or r.get("action") or "HOLD").upper()
        if action == "HOLD":
            continue
        sym = r.get("symbol") or ""
        parts.append(f"{sym}:{action}")
    return "|".join(sorted(parts))


def _reason_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]


@dataclass
class _HoldCandidate:
    symbol: str
    why_de: str
    tech_line: str
    confidence: float


@dataclass
class CycleNotificationPolicy:
    last_nav: float | None = None
    last_decision_fingerprint: str = ""
    hold_cooldown: dict[str, float] = field(default_factory=dict)
    _hold_candidates: list[_HoldCandidate] = field(default_factory=list)

    def reset_cycle(self) -> None:
        self._hold_candidates.clear()

    def should_send_summary(
        self,
        *,
        coin_results: list | None,
        total_value: float,
        config=None,
    ) -> bool:
        cnf = _cycle_notifications_config(config)
        mode = (cnf.get("mode") or "delta").lower()
        if mode == "off":
            return False
        if mode == "always":
            self._record_summary_state(coin_results, total_value)
            return True

        triggers: list[str] = []

        if cnf.get("send_on_trade", True):
            if any(r.get("executed") for r in (coin_results or [])):
                triggers.append("trade")

        if cnf.get("send_on_blocked", True):
            for r in coin_results or []:
                if r.get("trade_message") and not r.get("executed"):
                    triggers.append("blocked")
                    break

        nav_delta_pct = float(cnf.get("send_on_nav_delta_pct", 0.5) or 0)
        if nav_delta_pct > 0 and self.last_nav is not None and self.last_nav > 0:
            delta_pct = abs(float(total_value) - self.last_nav) / self.last_nav * 100.0
            if delta_pct >= nav_delta_pct:
                triggers.append("nav")

        if cnf.get("send_on_new_decision", True):
            fp = decision_fingerprint(coin_results)
            if fp and fp != self.last_decision_fingerprint:
                triggers.append("decision")

        if triggers:
            self._record_summary_state(coin_results, total_value)
            return True
        return False

    def skip_reason(
        self,
        *,
        coin_results: list | None,
        total_value: float,
        config=None,
    ) -> str:
        cnf = _cycle_notifications_config(config)
        fp = decision_fingerprint(coin_results)
        nav_part = ""
        if self.last_nav is not None and self.last_nav > 0:
            delta_pct = abs(float(total_value) - self.last_nav) / self.last_nav * 100.0
            nav_part = f" nav_delta={delta_pct:.2f}%"
        return (
            f"delta_skip mode={cnf.get('mode', 'delta')}"
            f"{nav_part} decisions={fp or 'none'}"
        )

    def _record_summary_state(self, coin_results: list | None, total_value: float) -> None:
        self.last_nav = float(total_value)
        self.last_decision_fingerprint = decision_fingerprint(coin_results)

    def offer_hold_explanation(
        self,
        symbol: str,
        why_de: str,
        *,
        tech_line: str = "",
        confidence: float = 0.0,
        config=None,
    ) -> None:
        cnf = _cycle_notifications_config(config)
        max_per_cycle = int(cnf.get("hold_explanation_max_per_cycle", 1) or 0)
        if max_per_cycle <= 0:
            return
        cooldown_h = float(cnf.get("hold_explanation_cooldown_hours", 6) or 0)
        key = f"{symbol}:{_reason_hash(why_de)}"
        if cooldown_h > 0:
            last = self.hold_cooldown.get(key)
            if last and (time.time() - last) < cooldown_h * 3600:
                return
        self._hold_candidates.append(
            _HoldCandidate(
                symbol=symbol,
                why_de=why_de,
                tech_line=tech_line,
                confidence=float(confidence or 0),
            )
        )

    def flush_hold_explanations(self, config=None) -> int:
        cnf = _cycle_notifications_config(config)
        max_per_cycle = int(cnf.get("hold_explanation_max_per_cycle", 1) or 0)
        if max_per_cycle <= 0 or not self._hold_candidates:
            self._hold_candidates.clear()
            return 0

        best = max(self._hold_candidates, key=lambda c: c.confidence)
        self._hold_candidates.clear()

        from telegram_notifier import send_hold_explanation_message

        sent = send_hold_explanation_message(best.symbol, best.why_de, best.tech_line)
        if sent:
            cooldown_h = float(cnf.get("hold_explanation_cooldown_hours", 6) or 0)
            if cooldown_h > 0:
                key = f"{best.symbol}:{_reason_hash(best.why_de)}"
                self.hold_cooldown[key] = time.time()
            return 1
        return 0

    @staticmethod
    def social_confidence_from_context(social_ctx: dict | None) -> float:
        if not social_ctx:
            return 0.0
        best = 0.0
        x = social_ctx.get("x") or {}
        if x:
            best = max(
                best,
                float(x.get("effective_confidence", x.get("confidence", 0)) or 0),
            )
        cmc = social_ctx.get("cmc") or {}
        if cmc:
            best = max(best, float(cmc.get("confidence", 0) or 0))
        lc = social_ctx.get("lc") or {}
        if lc:
            best = max(best, float(lc.get("confidence", 0) or 0))
        return best


cycle_notification_policy = CycleNotificationPolicy()