"""Delta gating for cycle Telegram notifications (summary + hold explanations)."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any


# Balanced quiet defaults (staging): fewer Telegram pings, trades always surface.
_DEFAULTS = {
    "mode": "delta",
    "send_on_trade": True,
    "send_on_blocked": False,
    "send_on_nav_delta_pct": 2.0,
    "send_on_new_decision": False,
    "min_interval_sec": 900,
    "heartbeat_sec": 3600,
    "hold_explanation_max_per_cycle": 0,
    "hold_explanation_cooldown_hours": 6,
    "digest_merge": True,
    "notify_hermes_rejected": False,
    "summary_style": "compact",
    "social_digest_min_interval_sec": 1800,
}


def _cycle_notifications_config(config=None) -> dict:
    from core.config import get_bot_config

    cfg = config or get_bot_config()
    raw = cfg.observability_config.get("cycle_notifications", {})
    return {**_DEFAULTS, **raw}


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


def _has_executed_trade(coin_results: list | None) -> bool:
    return any(r.get("executed") for r in (coin_results or []))


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
    last_summary_at: float = 0.0
    last_digest_at: float = 0.0
    last_summary_reason: str = ""
    process_started_at: float = field(default_factory=time.time)
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
            self.last_summary_reason = "off"
            return False
        if mode == "always":
            self._record_summary_state(coin_results, total_value, reason="always")
            return True

        triggers: list[str] = []
        force = False

        if cnf.get("send_on_trade", True) and _has_executed_trade(coin_results):
            triggers.append("trade")
            force = True

        if cnf.get("send_on_blocked", False):
            for r in coin_results or []:
                if r.get("trade_message") and not r.get("executed"):
                    triggers.append("blocked")
                    break

        nav_delta_pct = float(cnf.get("send_on_nav_delta_pct", 2.0) or 0)
        if nav_delta_pct > 0 and self.last_nav is not None and self.last_nav > 0:
            delta_pct = abs(float(total_value) - self.last_nav) / self.last_nav * 100.0
            if delta_pct >= nav_delta_pct:
                triggers.append("nav")

        if cnf.get("send_on_new_decision", False):
            fp = decision_fingerprint(coin_results)
            if fp and fp != self.last_decision_fingerprint:
                triggers.append("decision")

        now = time.time()
        min_interval = float(cnf.get("min_interval_sec", 900) or 0)
        heartbeat = float(cnf.get("heartbeat_sec", 3600) or 0)

        # Heartbeat: at most once per heartbeat_sec of quiet time (from last
        # summary or process start). Does not fire on the very first quiet cycle.
        if not triggers and heartbeat > 0:
            anchor = self.last_summary_at or self.process_started_at
            if self.last_summary_at > 0 and (now - anchor) >= heartbeat:
                triggers.append("heartbeat")
            elif self.last_summary_at <= 0 and (now - self.process_started_at) >= heartbeat:
                triggers.append("heartbeat")

        if not triggers:
            self.last_summary_reason = "quiet"
            return False

        if not force and min_interval > 0 and self.last_summary_at > 0:
            if (now - self.last_summary_at) < min_interval:
                self.last_summary_reason = (
                    f"min_interval triggers={'+'.join(triggers)}"
                )
                return False

        reason = "+".join(triggers)
        self._record_summary_state(coin_results, total_value, reason=reason)
        return True

    def should_send_social_digest(self, config=None) -> bool:
        """Rate-limit merged/separate social digests."""
        cnf = _cycle_notifications_config(config)
        interval = float(cnf.get("social_digest_min_interval_sec", 1800) or 0)
        if interval <= 0:
            return True
        now = time.time()
        if self.last_digest_at > 0 and (now - self.last_digest_at) < interval:
            return False
        self.last_digest_at = now
        return True

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
        age = ""
        if self.last_summary_at > 0:
            age = f" since_summary={time.time() - self.last_summary_at:.0f}s"
        return (
            f"delta_skip mode={cnf.get('mode', 'delta')}"
            f"{nav_part} decisions={fp or 'none'}{age}"
            f" reason={self.last_summary_reason or 'n/a'}"
        )

    def _record_summary_state(
        self,
        coin_results: list | None,
        total_value: float,
        *,
        reason: str = "",
    ) -> None:
        self.last_nav = float(total_value)
        self.last_decision_fingerprint = decision_fingerprint(coin_results)
        self.last_summary_at = time.time()
        self.last_summary_reason = reason or "sent"

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
        max_per_cycle = int(cnf.get("hold_explanation_max_per_cycle", 0) or 0)
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
        max_per_cycle = int(cnf.get("hold_explanation_max_per_cycle", 0) or 0)
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
