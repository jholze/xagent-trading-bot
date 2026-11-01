import math
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from core.config import BotConfig, get_bot_config
from core.models import RiskDecision, TradeOrder
from data_manager import (
    is_demo_mode,
    is_dry_run_enhanced,
    is_live_dry_run,
    load_live_trade_history,
    load_trade_history,
    resolve_ledger_backend,
    simulated_balance_usdt,
    uses_exchange_ledger,
)
from services.gate_balance import fetch_portfolio_equity, fetch_usdt_balance
from services.market_service import MarketService
from services.portfolio_service import PortfolioService
from strategies.positions import (
    count_open_full_slots,
    count_open_positions,
    find_open_position_for_symbol,
    find_open_positions_for_symbol,
    get_position,
    is_open_position,
    is_positions_state_unknown,
    list_active_positions,
    position_notional_usdt,
    sell_fraction_for_signal,
)


def floor_usdt_cents(amount: float) -> float:
    """Round a notional down to cents so a later round() cannot exceed the cap."""
    from decimal import ROUND_DOWN, Decimal

    quantized = Decimal(str(float(amount))).quantize(
        Decimal("0.01"), rounding=ROUND_DOWN
    )
    return float(quantized)


def _fmt_liq(value) -> str:
    if value is None:
        return "None"
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return "None"


@dataclass
class BuyLockResult:
    """Lock outcome plus the liquidity room ``evaluate`` uses to cut a buy."""

    decision: RiskDecision | None = None
    apply_cap: bool = False
    liq_cap_usdt: float | None = None
    cap_depth: float | None = None
    cap_volume: float | None = None
    open_cost_usdt: float | None = None
    allowed_usdt: float | None = None
    band_bid_usdt: float | None = None
    band_ask_usdt: float | None = None
    qv24_usdt: float | None = None
    book_age_sec: float | None = None
    depth_window_pct: float | None = None
    planned_usdt: float | None = None


_EQUITY_MTM_UNAVAILABLE_LOGGED = False

# #304 slice 3: one orders-document load per evaluate() call. The scope lives
# in a per-instance threading.local so concurrent evaluate() calls on a shared
# RiskManager never see each other's snapshot. _EVAL_DOC_PENDING marks "load on
# first use" so early rejects (cooldown, no amount, ...) cost no load at all.
_EVAL_DOC_PENDING = object()


class _MissingMark(RuntimeError):
    """Open live lot has no mark. The USDT daily-loss check always denies."""


# #709: counted hard-stop rejections in this process, split by tenant. Only
# "already sold" and "position $… below minimum" increment the count. Inflight,
# exchange, cooldown, and lock rejects do not. In-memory. The exit-radar hub
# mirrors the fires it observes into stats()["rejected_stops"].
_REJECTED_BY_TENANT: dict[str, int] = {}
_SOLD_PERCENT_PLAUSIBILITY_TOLERANCE = 0.01
_reject_event = threading.local()
_ignore_unreliable_sold = threading.local()


def reset_risk_manager_globals_for_tests() -> None:
    global _EQUITY_MTM_UNAVAILABLE_LOGGED, _REJECTED_BY_TENANT
    _EQUITY_MTM_UNAVAILABLE_LOGGED = False
    _REJECTED_BY_TENANT = {}
    _reject_event.active = False
    _reject_event.counted = False
    _ignore_unreliable_sold.on = False


def rejected_stop_count(tenant: str | None = None) -> int:
    """Counted hard-stop rejections since start (or the last test reset).

    No tenant: every tenant. A tenant id: that tenant only.
    """
    if tenant is None:
        return sum(int(v) for v in _REJECTED_BY_TENANT.values())
    return int(_REJECTED_BY_TENANT.get(str(tenant), 0))


def rejected_stop_counts_by_tenant() -> dict[str, int]:
    return {str(k): int(v) for k, v in _REJECTED_BY_TENANT.items()}


def _counted_stop_reason(reason: str) -> bool:
    """The two #709 risk-guard rejects. Gate-minimum and inflight are not these."""
    msg = reason or ""
    if "already sold" in msg:
        return True
    if "position $" in msg and "below minimum" in msg:
        return True
    return False


@contextmanager
def rejected_stop_event():
    """One stop fire. The partial and its follow-up increment the counter once."""
    prev_active = bool(getattr(_reject_event, "active", False))
    prev_counted = bool(getattr(_reject_event, "counted", False))
    _reject_event.active = True
    _reject_event.counted = False
    try:
        yield
    finally:
        _reject_event.active = prev_active
        _reject_event.counted = prev_counted


@contextmanager
def ignoring_unreliable_sold_percent():
    """One partial check may ignore stored sold_percent when the peak is unusable.

    The lot dict is not modified. A finite peak is still a basis.
    """
    prev = bool(getattr(_ignore_unreliable_sold, "on", False))
    _ignore_unreliable_sold.on = True
    try:
        yield
    finally:
        _ignore_unreliable_sold.on = prev


def _ignoring_unreliable_sold_percent() -> bool:
    return bool(getattr(_ignore_unreliable_sold, "on", False))


def peak_amount_usable(pos: dict | None) -> float | None:
    """Finite peak above 0, or None when the lot has no usable peak."""
    if not isinstance(pos, dict):
        return None
    raw = pos.get("peak_amount")
    if raw is None or raw == "":
        return None
    try:
        peak = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(peak) or peak <= 0:
        return None
    return peak


def _lower_sold_percent_for_check(pos: dict | None, stored: float) -> float:
    """Sold fraction for the partial-sell check only (#709).

    ``peak_amount`` is the opening fill. DCA and add-on buys raise ``amount``
    and not peak, so the held basis is ``max(peak_amount, amount)``.
    ``implied = 1 - amount / basis``. A stored value clearly above ``implied``
    is stale and is lowered to ``implied``. The result is never higher than
    ``stored``, and ``pos`` is not modified.
    """
    if not isinstance(pos, dict):
        return stored
    try:
        amount_now = float(pos.get("amount") or 0)
        peak = float(pos.get("peak_amount") or 0)
    except (TypeError, ValueError):
        return stored
    # NaN/inf is not a held basis. It must not imply 0 sold.
    if not math.isfinite(amount_now) or not math.isfinite(peak):
        return stored
    # Without a peak there is no held basis. amount alone implies 0 sold and
    # would wipe a real sold_percent.
    if amount_now <= 0 or peak <= 0:
        return stored
    basis = max(peak, amount_now)
    implied = max(0.0, 1.0 - (amount_now / basis))
    if stored > implied + _SOLD_PERCENT_PLAUSIBILITY_TOLERANCE and implied < stored:
        return implied
    return stored


def _rejected_stop_signal_kind(signal: str) -> str:
    if str(signal or "") == "SELL_STOP_FULL":
        return "SELL_STOP_FULL"
    return "partial"


def _format_peak_amount(peak_amount: float | None) -> str:
    """Missing peak is ``null``. A real 0 stays ``0.0`` so it is not invented."""
    if peak_amount is None:
        return "null"
    return f"{peak_amount}"


def note_rejected_stop(
    *,
    tenant: str,
    symbol: str,
    timeframe: str,
    signal: str,
    reason: str,
    sold_percent: float,
    amount: float,
    peak_amount: float | None,
) -> None:
    """WARNING plus the process counter for one rejected hard stop (#709).

    The line is the log source for later measurement. ``sold_percent`` is the
    stored lot value, not a plausibility correction. ``peak_amount`` is None
    when the lot has no peak, and the line then says ``peak_amount=null``.
    ``reason`` is last so the earlier fields stay single tokens.

    The counter moves only for the two risk-guard reasons, and only once when
    a partial and its follow-up share ``rejected_stop_event``.
    """
    from core.time_utils import format_display_time
    from logger import log

    count_this = _counted_stop_reason(reason)
    if (
        count_this
        and getattr(_reject_event, "active", False)
        and getattr(_reject_event, "counted", False)
    ):
        count_this = False
    tenant_id = str(tenant or "")
    if count_this:
        _REJECTED_BY_TENANT[tenant_id] = int(_REJECTED_BY_TENANT.get(tenant_id, 0)) + 1
        if getattr(_reject_event, "active", False):
            _reject_event.counted = True
    kind = _rejected_stop_signal_kind(signal)
    log(
        "rejected_stop "
        f"timestamp={format_display_time()} tenant={tenant} symbol={symbol} "
        f"tf={timeframe} signal={kind} sold_percent={sold_percent} "
        f"amount={amount} peak_amount={_format_peak_amount(peak_amount)} "
        f"rejected_stops={rejected_stop_count()} reason={reason}",
        "WARNING",
    )


def _is_emergency_sell(signal: str) -> bool:
    signal = signal or ""
    return signal in (
        "SELL_STOP_FULL",
        "SELL_STOP_PARTIAL",
        "SELL_FULL",
        "COVER_FULL",
    ) or "STOP" in signal


def _is_stop_loss_sell(signal: str) -> bool:
    signal = signal or ""
    return signal in ("SELL_STOP_FULL", "SELL_STOP_PARTIAL") or "STOP" in signal


def _is_partial_sell(signal: str) -> bool:
    signal = signal or ""
    if _is_emergency_sell(signal):
        return False
    if "FULL" in signal:
        return False
    return "PARTIAL" in signal or signal in ("SELL", "SELL_10", "SELL_20", "SELL_30", "SELL_TP")


def _fail_closed_guards_mode(config) -> str:
    """Rollout switch: 'log' (default, old behaviour + ERROR) | 'deny'."""
    try:
        risk = None
        if config is None:
            return "log"
        if isinstance(config, dict):
            nested = config.get("risk")
            if isinstance(nested, dict):
                risk = nested
            elif "fail_closed_guards" in config:
                risk = config
        else:
            rc = getattr(config, "risk_config", None)
            if isinstance(rc, dict):
                risk = rc
            else:
                raw = getattr(config, "raw", None)
                if isinstance(raw, dict) and isinstance(raw.get("risk"), dict):
                    risk = raw.get("risk")
        if not isinstance(risk, dict):
            return "log"
        mode = str(risk.get("fail_closed_guards") or "log").strip().lower()
        return "deny" if mode == "deny" else "log"
    except Exception:
        return "log"


def guard_failed(guard: str, exc: BaseException, order, *, config=None) -> RiskDecision | None:
    """A guard raised. 'log' → ERROR + None. 'deny' → ERROR + RiskDecision deny."""
    try:
        from logger import log

        symbol = getattr(order, "symbol", "") or ""
        log(f"risk guard {guard} failed {symbol}: {exc}", "ERROR")
    except Exception:
        pass
    if _fail_closed_guards_mode(config) != "deny":
        return None
    return RiskDecision(
        approved=False,
        message=f"{guard}_error: {exc}"[:200],
        code=f"{guard}_error",
        size_multiplier=0.0,
    )


# #408: process-wide fallback for a daily-loss halt whose persist failed. The
# trade-history document is the durable truth; this only keeps the halt alive for
# the rest of the current process (RiskManager instances are short-lived — the
# signal orchestrator builds one per evaluation — so an instance attribute would
# not survive). It cannot survive a restart; that is why the persist failure is
# logged ERROR and pushed to the operator instead of being swallowed.
_DAILY_LOSS_HALT_UNTIL: datetime | None = None


def _daily_loss_halt_memory() -> datetime | None:
    return _DAILY_LOSS_HALT_UNTIL


def _remember_daily_loss_halt(until: datetime) -> None:
    global _DAILY_LOSS_HALT_UNTIL
    if _DAILY_LOSS_HALT_UNTIL is None or until > _DAILY_LOSS_HALT_UNTIL:
        _DAILY_LOSS_HALT_UNTIL = until


def _execution_places_real_orders(raw) -> bool:
    """True only when live.execution resolves to real (#410).

    Thin wrapper over the single source of truth
    ``core.execution_mode.places_real_orders`` — no second parser of
    ``live.execution`` / ``live.dry_run`` lives here. Shadow and testnet are
    both False; an unresolved mode is treated as real (fail-closed) so the
    live short gate stays in place.
    """
    from core.execution_mode import places_real_orders
    from logger import log

    try:
        return places_real_orders(raw)
    except Exception as e:  # pragma: no cover - defensive, helper already fails closed
        log(f"short gate: execution mode unresolved, treating as real: {e}", "WARNING")
        return True


_HARD_STOP_SIGNALS = frozenset({"SELL_STOP_FULL", "SELL_STOP_PARTIAL"})


HARD_STOP_ORIGINS = frozenset({"exit_ws", "long_hard_stop"})  # H2 M-ORIGIN


def explicit_hard_stop_origin(order, source: str | None = None) -> str | None:
    """H2 #735 (M-ORIGIN): the explicit hard-stop origin marker, fail-closed.

    Returns "exit_ws" / "long_hard_stop" only when is_hard_stop_order holds AND the order carries the dedicated
    marker TradeOrder.hard_stop_origin (set only in exit_realtime/execute.py and signal_orchestrator.py).
    The exit_source label alone (incl. the exit_attribution fallback) never counts.
    """
    if not is_hard_stop_order(order, source):
        return None
    marker = str(getattr(order, "hard_stop_origin", "") or "")
    return marker if marker in HARD_STOP_ORIGINS else None


def _h2_price_below_entry(order, timeframe: str) -> bool:  # H2 M-DAILY below-entry condition
    try:
        pos = get_position(order.symbol, timeframe) or {}
        entry = float(pos.get("average_entry") or 0)
        return entry > 0 and float(order.price or 0) < entry
    except (TypeError, ValueError):
        return False


def is_hard_stop_order(order, source: str | None = None) -> bool:
    """R11 hard stop. Fail closed when ``exit_source`` is not visible.

    All three must hold: signal in (SELL_STOP_FULL, SELL_STOP_PARTIAL),
    exit_source == "stop_loss", and neither the order source nor the
    evaluate source is "manual".
    """
    signal = str(getattr(order, "signal", "") or "")
    if signal not in _HARD_STOP_SIGNALS:
        return False
    if not hasattr(order, "exit_source"):
        return False
    exit_source = getattr(order, "exit_source")
    if exit_source is None or str(exit_source) != "stop_loss":
        return False
    order_source = str(getattr(order, "source", "") or "")
    call_source = str(source or "")
    if order_source == "manual" or call_source == "manual":
        return False
    return True


# H2 (from #723 annex A): partial_sell_guard reason codes (priority order = check order).
PARTIAL_SELL_BLOCK_SOLD_CAP = "sold_cap"
PARTIAL_SELL_BLOCK_MIN_POSITION = "min_position"
PARTIAL_SELL_BLOCK_MIN_NOTIONAL = "min_notional"
PARTIAL_SELL_BLOCK_CAP_MISSING = "sold_cap_unconfigured"
PARTIAL_SELL_SIZE_CODES = frozenset(
    {PARTIAL_SELL_BLOCK_MIN_POSITION, PARTIAL_SELL_BLOCK_MIN_NOTIONAL, PARTIAL_SELL_BLOCK_CAP_MISSING}
)  # H2-ALLOW (P723A-ALLOW + P723B-E1-ALLOW)
# H2 (from #723 annex B, Kay N4): the sold cap is always the configured key, never a default.
PARTIAL_SELL_CAP_KEY = "block_partial_sell_if_sold_percent_above"
_PARTIAL_SELL_CAP_LOGGED: set = set()


def _log_partial_sell_cap_missing(symbol, detail):
    from core.tenant_context import resolve_tenant_id
    from logger import log

    tid = resolve_tenant_id()
    key = (tid, symbol, detail)
    if key in _PARTIAL_SELL_CAP_LOGGED:
        return
    _PARTIAL_SELL_CAP_LOGGED.add(key)
    log(
        f"partial_sell_cap_missing key={PARTIAL_SELL_CAP_KEY} {detail} "
        f"symbol={symbol} tenant={tid} -> non-hard partial sells blocked; "
        "hard stop partials escalate to one SELL_STOP_FULL",
        "ERROR",
    )


def _resolve_partial_sell_cap(params, risk_cfg, symbol):  # P723B-CAP
    import math

    for scope, cfg in (("strategy", params), ("risk", risk_cfg)):
        raw = (cfg or {}).get(PARTIAL_SELL_CAP_KEY)
        if raw is None:
            continue
        ok = isinstance(raw, (int, float)) and not isinstance(raw, bool) and math.isfinite(raw) and 0 < raw <= 1
        if ok:
            return float(raw)
        _log_partial_sell_cap_missing(symbol, f"invalid {scope}={raw!r}")
        return None
    _log_partial_sell_cap_missing(symbol, "missing")
    return None


class RiskManager:
    """Central gate for trade sizing and portfolio limits."""

    def __init__(
        self,
        config: BotConfig = None,
        portfolio: PortfolioService = None,
        market_service: MarketService = None,
    ):
        self.config = config or get_bot_config()
        self.portfolio = portfolio or PortfolioService(self.config)
        self.market = market_service or MarketService()

    def _guard_failed(self, guard: str, exc: BaseException, order) -> "RiskDecision | None":
        """A guard raised. 'log' → ERROR + return None (caller continues, old behaviour, but visible).
        'deny' → ERROR + RiskDecision(approved=False, code=f"{guard}_error", size_multiplier=0.0)."""
        return guard_failed(guard, exc, order, config=self.config)

    def evaluate(
        self,
        order: TradeOrder,
        timeframe: str = "4h",
        source: str = "auto",
        trust_score: float = None,
        confidence: float = None,
        indicators: dict = None,
        locked_sell_checks: bool = False,
    ) -> RiskDecision:
        # One orders-document snapshot per evaluate() call, loaded lazily on the
        # first counter read. Nested evaluate() (auto-short) must not reuse the
        # outer snapshot — it starts pending again and the outer is restored.
        scope = self._eval_scope()
        previous = getattr(scope, "doc", None)
        scope.doc = _EVAL_DOC_PENDING
        try:
            decision = self._evaluate_impl(
                order,
                timeframe=timeframe,
                source=source,
                trust_score=trust_score,
                confidence=confidence,
                indicators=indicators,
                locked_sell_checks=locked_sell_checks,
            )
            # R15: durable risk_rejects.jsonl for all BUY denies (fail-open)
            try:
                if (
                    str(getattr(order, "type", "") or "").upper() == "BUY"
                    and not getattr(decision, "approved", True)
                ):
                    from services.watchlist_quality.soak_log import log_risk_reject

                    raw = self.config.raw if hasattr(self.config, "raw") else None
                    log_risk_reject(
                        symbol=getattr(order, "symbol", "") or "",
                        side="BUY",
                        source=str(source or ""),
                        code=getattr(decision, "code", "") or "",
                        message=getattr(decision, "message", "") or "",
                        config=raw,
                    )
            except Exception:
                pass
            # #628: observe-only buy decision tape (fail-open; never a gate)
            try:
                if str(getattr(order, "type", "") or "").upper() == "BUY":
                    from strategies.buy_decision_tape import emit_buy_decision_tape

                    raw = self.config.raw if hasattr(self.config, "raw") else None
                    emit_buy_decision_tape(
                        order,
                        decision,
                        source=source,
                        timeframe=timeframe,
                        config=raw,
                    )
            except Exception as exc:
                try:
                    from logger import log

                    log(f"buy_decision_tape emit failed: {exc}", "WARNING")
                except Exception:
                    pass
            # #630: observe-only daily gainer desk (fail-open; never a gate)
            try:
                if str(getattr(order, "type", "") or "").upper() == "BUY":
                    from strategies.daily_gainer_desk import emit_desk_from_decision

                    raw = self.config.raw if hasattr(self.config, "raw") else None
                    emit_desk_from_decision(
                        order,
                        decision,
                        source=source,
                        config=raw,
                    )
            except Exception as exc:
                try:
                    from logger import log

                    log(f"daily_gainer_desk emit failed: {exc}", "WARNING")
                except Exception:
                    pass
            try:
                self._record_rejected_stop(order, timeframe, decision)
            except Exception:
                pass
            return decision
        finally:
            scope.doc = previous

    def _record_rejected_stop(self, order: TradeOrder, timeframe: str, decision: RiskDecision) -> None:
        """Log and count a rejected hard stop. Does not change the decision or the lot."""
        if getattr(decision, "approved", True):
            return
        if str(getattr(order, "type", "") or "").upper() != "SELL":
            return
        signal = str(getattr(order, "signal", "") or "")
        if not _is_stop_loss_sell(signal):
            return
        pos = {}
        try:
            pos = get_position(order.symbol, timeframe) or {}
        except Exception:
            pos = {}

        def _num(key: str) -> float:
            try:
                return float(pos.get(key) or 0)
            except (TypeError, ValueError):
                return 0.0

        def _peak_for_log() -> float | None:
            if not isinstance(pos, dict) or "peak_amount" not in pos:
                return None
            raw = pos.get("peak_amount")
            if raw is None or raw == "":
                return None
            try:
                value = float(raw)
            except (TypeError, ValueError):
                return None
            if not math.isfinite(value):
                return None
            return value

        from core.tenant_context import resolve_tenant_id

        note_rejected_stop(
            tenant=resolve_tenant_id(),
            symbol=str(getattr(order, "symbol", "") or ""),
            timeframe=str(timeframe or ""),
            signal=signal,
            reason=str(getattr(decision, "message", "") or ""),
            sold_percent=_num("sold_percent"),
            amount=_num("amount"),
            peak_amount=_peak_for_log(),
        )

    def _eval_scope(self) -> threading.local:
        tls = self.__dict__.get("_eval_tls")
        if tls is None:
            tls = threading.local()
            self.__dict__["_eval_tls"] = tls
        return tls

    @property
    def _eval_orders_doc(self):
        """Scoped orders document of the running evaluate() on this thread (tests)."""
        return getattr(self._eval_scope(), "doc", None)

    def _evaluate_impl(
        self,
        order: TradeOrder,
        timeframe: str = "4h",
        source: str = "auto",
        trust_score: float = None,
        confidence: float = None,
        indicators: dict = None,
        locked_sell_checks: bool = False,
    ) -> RiskDecision:
        from core.test_symbols import is_phantom_test_symbol

        cfg = self.config.raw if hasattr(self.config, "raw") else self.config
        if (
            is_demo_mode()
            and resolve_ledger_backend("demo", cfg) == "mongo"
            and is_phantom_test_symbol(order.symbol)
        ):
            return RiskDecision(
                approved=False,
                message=f"Phantom test symbol blocked: {order.symbol}",
                code="phantom_symbol",
            )

        # Hard rail: new exposure only. SELL/COVER always pass this check.
        if order.type in ("BUY", "SHORT"):
            halt = self._daily_loss_limit_blocked(order)
            if halt:
                return halt

        # Live caps fail closed on the value the guard will trade. Sells and
        # covers never reach this check. Short opens do.
        if order.type in ("BUY", "SHORT"):
            missing_caps = self._live_caps_missing_blocked(order)
            if missing_caps:
                return missing_caps

        # #679 K2: a filled sell/cover with no pnl blocks new buys and shorts
        # until recovery writes a pnl. Sells, covers, and stops do not enter.
        if order.type in ("BUY", "SHORT"):
            missing_pnl = self._missing_pnl_entry_block(order)
            if missing_pnl:
                return missing_pnl

        if order.type in ("SHORT", "COVER"):
            return self._evaluate_short_or_cover(order, timeframe, source=source)

        # One-way: never BUY or SELL an open short (any TF — sell repair hops TFs).
        try:
            from strategies.short_math import is_short as _is_short

            side_pos = get_position(order.symbol, timeframe)
            if _is_short(side_pos) and float((side_pos or {}).get("amount") or 0) > 1e-12:
                return RiskDecision(
                    approved=False,
                    message="one-way: cover short before long buy/sell",
                    code="one_way",
                )
            found = find_open_position_for_symbol(
                order.symbol, preferred_timeframe=timeframe
            )
            if found:
                _, hop = found
                if _is_short(hop) and float((hop or {}).get("amount") or 0) > 1e-12:
                    return RiskDecision(
                        approved=False,
                        message="one-way: cover short before long buy/sell",
                        code="one_way",
                    )
        except Exception as exc:
            return RiskDecision(
                approved=False,
                message=f"side_check_error: {exc}"[:200],
                code="side_check_error",
            )

        if order.type == "SELL":
            blocked, reason = self._trade_cooldown_blocked(
                order, timeframe, source=source, confidence=confidence
            )
            if blocked:
                return RiskDecision(approved=False, message=reason, code="trade_cooldown")
            # Position lock: block auto exits (exit_ws/trail/TA/grid); manual can still sell.
            # Fail-closed: never approve auto-sell if the lock check itself breaks
            # (matches DCA lock policy; avoids trail re-selling after ops revert+lock).
            try:
                from strategies.position_lock import (
                    attach_lock_from_ledger,
                    auto_sell_blocked,
                    log_lock_block,
                )

                pos = get_position(order.symbol, timeframe)
                pos = attach_lock_from_ledger(pos, order.symbol, timeframe) or pos
                raw_cfg = self.config.raw if hasattr(self.config, "raw") else None
                sell_src = source or getattr(order, "source", None) or ""
                locked, lock_msg = auto_sell_blocked(pos, sell_src, config=raw_cfg)
                if locked:
                    log_lock_block(order.symbol, lock_msg, source=str(sell_src))
                    return RiskDecision(
                        approved=False,
                        message=lock_msg,
                        code="position_locked",
                    )
            except Exception as exc:
                try:
                    from logger import log

                    log(
                        f"position_lock sell check error {order.symbol}: {exc}",
                        "ERROR",
                    )
                except Exception:
                    pass
                return RiskDecision(
                    approved=False,
                    message=f"position_lock_check_error: {exc}"[:200],
                    code="position_lock_check_error",
                )
            order = self._resolve_sell_order(order, timeframe, source)
            if order.amount <= 0:
                order = self._fill_sell_amount_from_open_lot(order, timeframe)
            if locked_sell_checks:
                # R5: cap against the lot this sell reduces, under the lock only.
                # The unlocked pre-check (TradingService.evaluate_risk) does not.
                capped = self._cap_sell_to_open_lot(order, timeframe)
                if isinstance(capped, RiskDecision):
                    return capped
                order = capped
                order = self._upgrade_partial_stop_below_gate_min(order, timeframe)
            if order.amount <= 0:
                return RiskDecision(approved=False, message="No amount to sell", code="no_amount")
            self._refresh_venue_mark(order.symbol, timeframe, order.price)
            from strategies.dca import confirmed_full_long_hard_stop
            from strategies.positions import BELOW_VENUE_MINIMUM, is_below_venue_minimum

            below_min_pos = get_position(order.symbol, timeframe)
            if is_below_venue_minimum(below_min_pos):
                stop_params = getattr(order, "_full_hard_stop_params", None)
                full_stop_on_below_min = confirmed_full_long_hard_stop(
                    action=str(order.signal or ""),
                    exit_source=getattr(order, "exit_source", None),
                    source=getattr(order, "source", None),
                    call_source=source,
                    price=float(order.price or 0),
                    position=below_min_pos,
                    strategy_params=stop_params if isinstance(stop_params, dict) else None,
                )
                if full_stop_on_below_min:
                    # Ledger amount. A venue-minimum figure is not a size.
                    # An exchange reject still uses the existing dust path.
                    try:
                        held = float(below_min_pos.get("amount") or 0)
                    except (TypeError, ValueError):
                        held = 0.0
                    if held > 0:
                        order.amount = held
                else:
                    # The transition already logged once. Do not log again, and do not retry.
                    return RiskDecision(
                        approved=False,
                        message="Lot is below the venue minimum",
                        code=BELOW_VENUE_MINIMUM,
                    )
            venue_block, order = self._venue_partial_guard(order, timeframe)
            if venue_block is not None:
                return venue_block
            partial_block, partial_reason = self._partial_sell_blocked(order, timeframe, source)
            if partial_block:
                escalated = None
                if locked_sell_checks:  # H2-LOCKED (P723-LOCKED)
                    escalated = self._escalate_rejected_hard_partial(
                        order, timeframe, source, partial_reason,
                        code=getattr(order, "_partial_sell_block_code", None),
                    )
                if escalated is None:
                    return RiskDecision(approved=False, message=partial_reason, code="partial_sell_guard")
                order = escalated
            max_daily_sells = self._effective_max_daily_sells()
            daily_sells = self._daily_sells_count()
            if max_daily_sells > 0 and daily_sells >= max_daily_sells:
                if (
                    locked_sell_checks
                    and explicit_hard_stop_origin(order, source)
                    and _h2_price_below_entry(order, timeframe)
                ):  # H2 M-DAILY (Viktor: marker + locked + below entry)
                    self._log_hard_stop_daily_bypass(
                        order, source, daily_sells, max_daily_sells
                    )
                else:
                    return RiskDecision(
                        approved=False,
                        message=f"Daily sell limit reached ({daily_sells}/{max_daily_sells})",
                        code="max_daily_sells",
                    )
            return RiskDecision(approved=True, order=order, message="Sell approved")

        if order.price <= 0:
            return RiskDecision(approved=False, message="Invalid price")

        blocked, reason = self._trade_cooldown_blocked(
            order, timeframe, source=source, confidence=confidence
        )
        if blocked:
            return RiskDecision(approved=False, message=reason, code="trade_cooldown")

        # Permanent stablecoin buy rail (all buy paths: TA, grid, gainer, DCA, …)
        from core.stablecoins import (
            is_stablecoin_symbol,
            stablecoin_block_reason,
            stablecoin_buys_blocked,
        )

        raw_cfg = self.config.raw if hasattr(self.config, "raw") else None
        try:
            stablecoin_hit = stablecoin_buys_blocked(raw_cfg) and is_stablecoin_symbol(
                order.symbol
            )
        except Exception as e:
            dec = self._guard_failed("stablecoin_blocked", e, order)
            if dec:
                return dec
            stablecoin_hit = False
        if stablecoin_hit:
            return RiskDecision(
                approved=False,
                message=stablecoin_block_reason(order.symbol),
                code="stablecoin_blocked",
                size_multiplier=0.0,
            )

        pos = get_position(order.symbol, timeframe)
        self._refresh_venue_mark(order.symbol, timeframe, order.price)
        pos = get_position(order.symbol, timeframe)
        has_position = is_open_position(pos)
        if order.type == "BUY" and not has_position:
            found = find_open_position_for_symbol(
                order.symbol, preferred_timeframe=timeframe
            )
            if found:
                hop_tf, hop = found
                try:
                    from strategies.short_math import is_short as _is_short_hop

                    hop_short = _is_short_hop(hop) and float(hop.get("amount") or 0) > 1e-12
                except Exception:
                    hop_short = False
                if not hop_short and float(hop.get("amount") or 0) > 1e-12:
                    # Second timeframe on an open long is an add (#640 R2c).
                    timeframe = hop_tf
                    pos = hop
                    has_position = True

        # Lot the DCA lock sees. Later sizing may re-read the position;
        # this snapshot is the open lot (including a hopped timeframe).
        guard_pos = pos if isinstance(pos, dict) else {}
        guard_has_position = bool(has_position)

        # Global market bias (oracle + santiment): block new buys on CRASH / warmup / size 0.
        if not has_position:
            from services.correlated_tier.api import correlated_tier_selloff_active

            raw_cfg = self.config.raw if hasattr(self.config, "raw") else None
            try:
                correlated_hit = correlated_tier_selloff_active(order.symbol, raw_cfg)
            except Exception as e:
                dec = self._guard_failed("correlated_tier_selloff", e, order)
                if dec:
                    return dec
                correlated_hit = False
            if correlated_hit:
                return RiskDecision(
                    approved=False,
                    message=f"Correlated-tier selloff active for {order.symbol}",
                    code="correlated_tier_selloff",
                    size_multiplier=0.0,
                )
            # Universe split: new BUYs only on trade-eligible set (observe is broader).
            # RelVol ignition deliberately discovers thin/off-universe names — exempt.
            from services.universe.split import is_trade_eligible, universe_split_enabled

            raw_cfg = self.config.raw if hasattr(self.config, "raw") else None
            try:
                universe_hit = (
                    universe_split_enabled(raw_cfg)
                    and not self._is_dca_buy(source, order)
                    and not self._is_relvol_buy(source, order)
                    and not is_trade_eligible(
                        order.symbol,
                        config=raw_cfg,
                    )
                )
            except Exception as e:
                dec = self._guard_failed("universe_trade_cap", e, order)
                if dec:
                    return dec
                universe_hit = False
            if universe_hit:
                decision = RiskDecision(
                    approved=False,
                    message=(
                        f"Outside trade universe (observe-only): {order.symbol}"
                    ),
                    code="universe_trade_cap",
                    size_multiplier=0.0,
                )
                # #633 Slice B: log the existing cap order. Observe only.
                # A write error must not change this decision or become a gate.
                try:
                    from services.universe.cap_order_observe import (
                        maybe_log_existing_cap_order,
                    )

                    maybe_log_existing_cap_order(
                        raw_cfg,
                        rejected_symbol=str(order.symbol or ""),
                    )
                except Exception:
                    pass
                return decision

            # Issue #162: prev-day gainer chase guard (new entries only, not DCA add)
            if not self._is_dca_buy(source, order):
                from services.gainer_universe.chase_guard import check_gainer_chase_guard

                raw_cfg = self.config.raw if hasattr(self.config, "raw") else None
                px = float(getattr(order, "price", 0) or 0)
                try:
                    blocked, gmsg = check_gainer_chase_guard(
                        order.symbol, px, config=raw_cfg
                    )
                except Exception as e:
                    dec = self._guard_failed("gainer_chase_guard", e, order)
                    if dec:
                        return dec
                    blocked, gmsg = False, ""
                if blocked:
                    return RiskDecision(
                        approved=False,
                        message=gmsg,
                        code="gainer_chase_guard",
                        size_multiplier=0.0,
                    )

            from services.market_policy_fusion import get_global_market_bias

            bias = {}
            try:
                bias = get_global_market_bias(
                    self.config.raw if hasattr(self.config, "raw") else None
                )
                market_hit = bool(bias.get("block_buys"))
            except Exception as e:
                dec = self._guard_failed("market_block", e, order)
                if dec:
                    return dec
                market_hit = False
            if market_hit:
                try:
                    from services.market_context_observability import note_buy_blocked

                    note_buy_blocked(
                        regime=bias.get("regime"),
                        source=bias.get("source"),
                        rationale=str(bias.get("rationale") or ""),
                    )
                except Exception:
                    pass
                return RiskDecision(
                    approved=False,
                    message=(
                        f"Market {bias.get('regime') or 'block'} "
                        f"[{bias.get('source') or 'global'}]: "
                        f"no new entries ({bias.get('rationale') or 'policy'})"
                    ),
                    code="market_block",
                    size_multiplier=float(bias.get("size_mult") or 0.0),
                )
            if (
                _fail_closed_guards_mode(self.config) == "deny"
                and bool(bias.get("degraded"))
                and not self._is_dca_buy(source, order)
            ):
                try:
                    from services.market_context_observability import note_buy_blocked

                    note_buy_blocked(
                        regime="UNKNOWN",
                        source=bias.get("source"),
                        rationale="market_bias_degraded",
                    )
                except Exception:
                    pass
                return RiskDecision(
                    approved=False,
                    message="Market bias degraded: no new entries",
                    code="market_bias_degraded",
                    size_multiplier=0.0,
                )

            # Coin memory soft_block: skip *new* entries only (DCA / existing pos allowed)
            if not has_position and not self._is_dca_buy(source, order):
                from intelligence.memory.cache import get_entry_bias, get_coin_profile
                from strategies.sensor_entry_policy import is_sensor_source

                memory_hit = False
                memory_msg = ""
                try:
                    if get_entry_bias(order.symbol) == "soft_block":
                        prof = get_coin_profile(order.symbol)
                        feats = (prof.features if prof else None) or {}
                        # Legacy soft_block (no scope in features) blocks all new entries.
                        # Gross-loss sensor_only: only block sensor-family sources.
                        scope = str(feats.get("soft_block_scope") or "").lower()
                        block_this = True
                        if scope == "sensor_only" and not is_sensor_source(source):
                            block_this = False
                        # TTL soft_block_until in features
                        if block_this and isinstance(feats, dict):
                            until = feats.get("soft_block_until")
                            if until:
                                try:
                                    from datetime import datetime, timezone

                                    u = str(until).replace("Z", "+00:00")
                                    until_dt = datetime.fromisoformat(u)
                                    if until_dt.tzinfo is None:
                                        until_dt = until_dt.replace(tzinfo=timezone.utc)
                                    if datetime.now(timezone.utc) > until_dt:
                                        block_this = False
                                except Exception:
                                    pass
                        if block_this:
                            memory_hit = True
                            memory_msg = (
                                f"Coin memory soft_block {order.symbol}: "
                                f"{(prof.rationale if prof else 'weak history')}"
                            )
                except Exception as e:
                    dec = self._guard_failed("coin_memory_soft_block", e, order)
                    if dec:
                        return dec
                    memory_hit = False
                if memory_hit:
                    return RiskDecision(
                        approved=False,
                        message=memory_msg,
                        code="coin_memory_soft_block",
                        size_multiplier=0.0,
                    )
                # WQE-R1: quality gate for all new entries (soft/enforce); DCA/open exempt
                if not has_position and not self._is_dca_buy(source, order):
                    from services.watchlist_quality.config import wqe_mode
                    from services.watchlist_quality.enforce import buy_allowed
                    from services.watchlist_quality.store import load_quality_scores

                    raw = self.config.raw if hasattr(self.config, "raw") else None
                    wqe_hit = False
                    wqe_reason = ""
                    scored = None
                    try:
                        mode = wqe_mode(raw)
                        if mode in ("soft", "enforce"):
                            from core.tenant_context import resolve_tenant_id

                            tid = resolve_tenant_id()
                            data = load_quality_scores(tenant_id=tid)
                            scored = None
                            for c in data.get("coins") or []:
                                if isinstance(c, dict) and c.get("symbol") == order.symbol:
                                    scored = c
                                    break
                            ok, reason = buy_allowed(
                                order.symbol,
                                scored_row=scored,
                                config=raw,
                                source=str(source or ""),
                                is_new_add=True,
                                has_open_position=False,
                            )
                            if not ok:
                                wqe_hit = True
                                wqe_reason = reason
                    except Exception as e:
                        dec = self._guard_failed("watchlist_quality", e, order)
                        if dec:
                            return dec
                        wqe_hit = False
                    if wqe_hit:
                        try:
                            from services.watchlist_quality.metrics import note_buy_blocked
                            from services.watchlist_quality.event_log import log_buy_block

                            note_buy_blocked(wqe_reason)
                            q = None
                            if scored:
                                q = scored.get("quality_shadow_ai")
                                if q is None:
                                    q = scored.get("quality_score")
                            log_buy_block(
                                order.symbol,
                                wqe_reason,
                                source=str(source or ""),
                                mode=mode,
                                quality_score=q,
                                config=raw,
                            )
                        except Exception:
                            pass
                        return RiskDecision(
                            approved=False,
                            message=f"WQE block {order.symbol}: {wqe_reason}",
                            code="watchlist_quality",
                            size_multiplier=0.0,
                        )
                # Re-entry cooloff after gross loss (sensor-entry-guard)
                try:
                    cool = self._sensor_reentry_cooloff_blocked(order, source)
                except Exception as e:
                    dec = self._guard_failed("sensor_reentry_cooloff", e, order)
                    if dec:
                        return dec
                    cool = None
                if cool:
                    return cool
            # Liquidity lock (#641) runs once, after final sizing, for every
            # automatic buy including adds. The old pre-size call lived here
            # and skipped open lots, so DCA never saw it.
            # Long mcap floor after the new-entry gates, before size. New long BUY only.
            # Open lots skip via has_position only. No-lot DCA is not exempt.
            # Only manual is exempt by default. Sells returned above.
            if (
                order.type == "BUY"
                and not has_position
                and (source or getattr(order, "source", None) or "") != "manual"
            ):
                from strategies.short_policy import (
                    _tier_key,
                    resolve_long_market_cap_min_usd,
                )

                tier = _tier_key(
                    pos.get("strategy_tier") if isinstance(pos, dict) else None
                )
                min_mcap = float(
                    resolve_long_market_cap_min_usd(
                        tier,
                        self.config.raw if hasattr(self.config, "raw") else None,
                    )
                    or 0
                )
                if min_mcap > 0:
                    mcap = None
                    try:
                        from data.cmc_market_cap import resolve_market_cap_usd

                        mcap = resolve_market_cap_usd(order.symbol)
                    except Exception:
                        mcap = None
                    deny_mcap = True
                    if mcap is not None:
                        try:
                            mcap_num = float(mcap)
                        except (TypeError, ValueError):
                            mcap_num = None
                        deny_mcap = (
                            mcap_num is None
                            or mcap_num <= 0
                            or mcap_num < min_mcap
                        )
                    if deny_mcap:
                        return RiskDecision(
                            approved=False,
                            message=f"long mcap {mcap} < min {min_mcap:.0f}",
                            code="long_mcap",
                            size_multiplier=0.0,
                        )
            if not self._is_dca_buy(source, order):
                # Optional macro calendar hard block (default off — prefer size mult)
                from intelligence.macro.snapshot import get_risk_multipliers

                mm = {}
                try:
                    mm = get_risk_multipliers(
                        self.config.raw if hasattr(self.config, "raw") else None
                    )
                    macro_hit = bool(mm.get("block_new_entries"))
                except Exception as e:
                    dec = self._guard_failed("macro_calendar_block", e, order)
                    if dec:
                        return dec
                    macro_hit = False
                if macro_hit:
                    return RiskDecision(
                        approved=False,
                        message=(
                            f"Macro calendar pre-window block: "
                            f"{mm.get('calendar_risk') or mm.get('next_event') or 'high impact'}"
                        ),
                        code="macro_calendar_block",
                        size_multiplier=0.0,
                    )

        # Cash floor / daily buy limit reject with no side effects. Do not
        # evict (live sell) for an entry those guards would still deny (#334).
        is_dca = self._is_dca_buy(source, order)
        floor_block = self._cash_floor_blocked(is_dca=is_dca)
        if floor_block:
            return floor_block

        buy_limit = self._daily_buy_limit_blocked(is_dca)
        if buy_limit:
            return buy_limit

        if not has_position:
            slot_block = self._open_slot_cap_blocked(order, source)
            if slot_block:
                return slot_block

        # R4/R7 and the below-min merge run after the existing new-entry gates
        # (universe, capacity). They do not add a second slot cap.
        merge_plan = None
        pair_block = self._venue_pair_buy_block(order)
        if pair_block is not None:
            return pair_block
        merge_reject, merge_plan = self._venue_min_merge(order, timeframe)
        if merge_reject is not None:
            return merge_reject

        base_usdt = order.usdt_amount or self._base_usdt_cap()
        if source == "cmc":
            fusion = self.config.cmc_trending_fusion_config
            trending_syms = self._trending_overlay_symbols()
            if order.symbol in trending_syms:
                pct = float(fusion.get("trending_trade_size_pct", 50)) / 100.0
                base_usdt = base_usdt * pct
        if source in ("dca", "dca_recovery") or order.signal == "BUY_DCA":
            params = self.config.strategy_params(order.symbol, timeframe)
            try:
                from strategies.registry import resolve_strategy_params

                pos = get_position(order.symbol, timeframe)
                params = resolve_strategy_params(
                    {"symbol": order.symbol, "timeframe": timeframe},
                    has_position=True,
                    frozen_tier=pos.get("strategy_tier"),
                )
            except Exception:
                pass
            dca_cfg = dict(params.get("dca") or {})
            if order.usdt_amount:
                base_usdt = float(order.usdt_amount)
            elif source == "dca_recovery":
                from strategies.dca_recovery import recovery_config, recovery_usdt_amount

                rec_cfg = recovery_config(params)
                pos = get_position(order.symbol, timeframe)
                base_usdt = recovery_usdt_amount(rec_cfg, params, position=pos)
            elif dca_cfg.get("fixed_usdt"):
                base_usdt = float(dca_cfg["fixed_usdt"])
        # Merge buy is ticket − remainder, then the existing multipliers
        # (size_mult, drawdown) shrink that buy. They never size it back up.
        if merge_plan is not None:
            base_usdt = min(float(base_usdt), float(merge_plan["cap"]))
        if source == "manual":
            # Telegram /buy amounts are explicit user intent — don't shrink via auto-trade multipliers.
            sized = base_usdt
            factors = {
                "trust_factor": 1.0,
                "conf_factor": 1.0,
                "atr_factor": 1.0,
                "drawdown_pct": round(self._equity_drawdown_pct(), 2),
                "drawdown_multiplier": 1.0,
                "total_multiplier": 1.0,
            }
        elif is_dca:
            sized = base_usdt
            factors = {
                "trust_factor": 1.0,
                "conf_factor": 1.0,
                "atr_factor": 1.0,
                "drawdown_pct": round(self._equity_drawdown_pct(), 2),
                "drawdown_multiplier": 1.0,
                "total_multiplier": 1.0,
            }
            # Mild size lift under moderate_deploy (+ cash-rich boost)
            try:
                from risk.moderate_deploy import size_boost_for_regime
                from services.market_policy_fusion import get_global_market_bias

                raw_cfg = self.config.raw if hasattr(self.config, "raw") else None
                bias = get_global_market_bias(raw_cfg) or {}
                regime = bias.get("regime")
                cash_pct = None
                try:
                    eq = float(self._portfolio_equity(order.price, order.symbol) or 0)
                    cash = float(self._available_usdt(eq) or 0)
                    if eq > 0:
                        cash_pct = 100.0 * cash / eq
                except Exception:
                    cash_pct = None
                md_boost = size_boost_for_regime(
                    raw_cfg, regime, is_dca=True, cash_pct=cash_pct
                )
                if md_boost > 1.0:
                    sized = float(sized) * md_boost
                    factors["moderate_deploy_mult"] = round(md_boost, 3)
                    factors["total_multiplier"] = round(md_boost, 3)
                    factors["global_regime"] = regime
                    if cash_pct is not None:
                        factors["cash_pct"] = round(cash_pct, 1)
            except Exception:
                pass
        else:
            if indicators is None:
                indicators = self.market.fetch_indicators(order.symbol, timeframe, order.price)
            # Sensor-entry-guard: no aggression inflation on pure sensor size
            from strategies.sensor_entry_policy import is_sensor_source

            sensor_cfg = {}
            try:
                sensor_cfg = self.config.entry_sensor_15m_config
            except Exception:
                sensor_cfg = {}
            skip_dyn = is_sensor_source(source) and bool(
                sensor_cfg.get("ignore_aggression_boost", True)
            )
            if skip_dyn:
                sized = float(base_usdt)
                factors = {
                    "trust_factor": 1.0,
                    "conf_factor": 1.0,
                    "atr_factor": 1.0,
                    "drawdown_pct": round(self._equity_drawdown_pct(), 2),
                    "drawdown_multiplier": 1.0,
                    "total_multiplier": 1.0,
                    "sensor_size_locked": True,
                }
            else:
                sized, factors = self._dynamic_size(
                    base_usdt,
                    order,
                    timeframe,
                    source,
                    trust_score,
                    confidence,
                    indicators,
                )
            # Absolute sensor cap (defense-in-depth)
            if is_sensor_source(source):
                abs_cap = sensor_cfg.get("max_usdt_absolute")
                if abs_cap is not None and float(abs_cap) > 0:
                    sized = min(float(sized), float(abs_cap))
                    factors["sensor_max_usdt_absolute"] = float(abs_cap)

        # Hard per-ticket ceiling AFTER multipliers (moderate_deploy / cash-rich / DCA
        # boosts must not push above max_usdt_per_trade — e.g. 4500 * 1.38 was ~6.2k).
        # The cap is the final notional: original and capped size are logged.
        pre_ticket_usdt = float(sized)
        ticket_cap = float(self._base_usdt_cap() or 0)
        if ticket_cap > 0 and pre_ticket_usdt > ticket_cap:
            sized = ticket_cap
            factors["ticket_capped"] = True
            factors["ticket_cap_usdt"] = ticket_cap
            factors["ticket_original_usdt"] = pre_ticket_usdt
            try:
                from logger import log

                log(
                    f"ticket_cap {order.symbol} original={pre_ticket_usdt:.2f} "
                    f"capped={float(ticket_cap):.2f}",
                    "INFO",
                )
            except Exception:
                pass

        equity = self._equity_for_sizing(order.price, order.symbol)
        pos_value = float(pos.get("amount", 0)) * order.price
        max_position_value = equity * (self.config.max_position_percent / 100.0)
        room = max_position_value - pos_value

        if room <= 0:
            return RiskDecision(
                approved=False,
                message=f"Max position concentration ({self.config.max_position_percent}% of portfolio)",
                code="max_position_percent",
            )

        if sized > room:
            sized = room
            factors["concentration_capped"] = True

        balance = self._spendable_usdt(equity, is_dca=is_dca)
        if sized > balance:
            sized = balance
            factors["balance_capped"] = True
            factors["spendable_usdt"] = round(balance, 2)

        # Bound uses the merged cost after every reduction, including
        # size_mult and drawdown. A full ticket (multiplier 1) matches
        # (stop − buffer) / 100 × ticket. The size floor runs first: a
        # final buy below min_trade_usdt is size_too_small and never
        # reaches the bound (T9d-3b).
        if merge_plan is not None:
            cap = float(merge_plan["cap"])
            if float(sized) > cap:
                sized = cap
                factors["venue_min_merge_capped"] = True
            min_trade_merge = float(self.config.risk_config.get("min_trade_usdt", 5.0))
            if float(sized) < min_trade_merge:
                return RiskDecision(
                    approved=False,
                    message=(
                        f"Adjusted size ${float(sized):.2f} below minimum "
                        f"(${min_trade_merge:.0f})"
                    ),
                    code="size_too_small",
                    size_multiplier=factors.get("total_multiplier", 1.0),
                    drawdown_pct=factors.get("drawdown_pct", 0.0),
                    atr_factor=factors.get("atr_factor", 1.0),
                    trust_factor=factors.get("trust_factor", 1.0),
                )
            bound_reject = self._venue_merge_loss_reject(
                order, timeframe, merge_plan, float(sized)
            )
            if bound_reject is not None:
                return bound_reject

        min_trade = float(self.config.risk_config.get("min_trade_usdt", 5.0))
        if factors.get("ticket_capped") and sized < min_trade:
            return RiskDecision(
                approved=False,
                message=(
                    f"Capped size ${sized:.2f} below minimum (${min_trade:.0f})"
                ),
                code="ticket_below_min",
                size_multiplier=factors.get("total_multiplier", 1.0),
                drawdown_pct=factors.get("drawdown_pct", 0.0),
                atr_factor=factors.get("atr_factor", 1.0),
                trust_factor=factors.get("trust_factor", 1.0),
            )
        if sized < min_trade:
            floor_abs = self._cash_floor_abs()
            cash = self._available_usdt(equity)
            # Prefer clear cash_floor messaging when reserve/floor is the constraint
            if floor_abs > 0 and cash - floor_abs < min_trade:
                return RiskDecision(
                    approved=False,
                    message=(
                        f"Cash floor: free ${max(0.0, cash - floor_abs):.2f} "
                        f"(floor ${floor_abs:.0f}, cash ${cash:.2f})"
                    ),
                    code="cash_floor",
                    size_multiplier=factors.get("total_multiplier", 1.0),
                    drawdown_pct=factors.get("drawdown_pct", 0.0),
                    atr_factor=factors.get("atr_factor", 1.0),
                    trust_factor=factors.get("trust_factor", 1.0),
                )
            return RiskDecision(
                approved=False,
                message=f"Adjusted size ${sized:.2f} below minimum (${min_trade:.0f})",
                code="size_too_small",
                size_multiplier=factors.get("total_multiplier", 1.0),
                drawdown_pct=factors.get("drawdown_pct", 0.0),
                atr_factor=factors.get("atr_factor", 1.0),
                trust_factor=factors.get("trust_factor", 1.0),
            )

        # #640 then #641, then #671. After the final size, before RelVol,
        # so later checks see the cut size. DCA codes come first.
        locked = self._buy_lock_decision(
            order,
            source=source,
            pos=guard_pos,
            has_position=guard_has_position,
            planned_usdt=float(sized),
            indicators=indicators,
        )
        if locked is None:
            locked = BuyLockResult(decision=None, apply_cap=False)
        if locked.decision is not None:
            return locked.decision

        planned_liq = float(sized)
        # A cut is the cap binding, not the cents floor. Sub-cent planned
        # sizes still round the way staging does when allowed_usdt is at
        # least the planned size.
        cap_binds = False
        if locked.apply_cap:
            if locked.allowed_usdt is None:
                return self._liq_size_reject(
                    locked,
                    factors,
                    code="liq_guard_missing_input",
                    planned=planned_liq,
                    final=None,
                    message="liq_guard_missing_input",
                )
            if float(locked.allowed_usdt) <= 0:
                return self._liq_size_reject(
                    locked,
                    factors,
                    code="liq_guard_depth_lt_order",
                    planned=planned_liq,
                    final=None,
                    message="liq_guard_depth_lt_order",
                )
            cap_binds = float(locked.allowed_usdt) < planned_liq
            if cap_binds:
                sized = min(float(sized), float(locked.allowed_usdt))
                sized = floor_usdt_cents(sized)
            if sized < min_trade:
                return self._liq_size_reject(
                    locked,
                    factors,
                    code="liq_guard_depth_lt_order",
                    planned=planned_liq,
                    final=sized,
                    message=(
                        f"liq_guard_depth_lt_order planned={planned_liq:g} "
                        f"final={sized:g} min_trade={min_trade:g}"
                    ),
                )
            if cap_binds:
                self._log_liq_size_capped(order, locked, planned_liq, sized)

        # #662 R6 runs on the cut size. A bound that held before the cut
        # can fail once the buy is smaller.
        if merge_plan is not None:
            bound_reject = self._venue_merge_loss_reject(
                order, timeframe, merge_plan, float(sized)
            )
            if bound_reject is not None:
                return self._attach_liq_details(
                    bound_reject, locked, planned_liq, float(sized)
                )

        pair_min_reject = self._cut_below_pair_min(order, float(sized))
        if pair_min_reject is not None:
            return self._attach_liq_details(
                pair_min_reject, locked, planned_liq, float(sized)
            )

        # RelVol trade tickets: reject under the floor after multipliers /
        # shrink-only caps. Do not round up. Other sources keep min_trade_usdt.
        # The floor reads risk.relvol_min_trade_usdt (today 1000). Explicit 0
        # disables it. max_usdt_per_trade is applied above and is the submit
        # size. A ticket that was above the floor before that cap is still
        # submitted at the cap.
        relvol_floor = self._cfg_number("relvol_min_trade_usdt", 1000.0)
        # A ticket cap that stays above the floor still submits (#589).
        # A liquidity cut is a new size: the floor sees that smaller notional.
        relvol_basis = pre_ticket_usdt if factors.get("ticket_capped") else float(sized)
        if cap_binds:
            relvol_basis = float(sized)
        if (
            self._is_relvol_buy(source, order)
            and relvol_floor > 0
            and relvol_basis < relvol_floor
        ):
            return self._attach_liq_details(
                RiskDecision(
                    approved=False,
                    message=f"Adjusted size ${sized:.2f} below minimum (${relvol_floor:.0f})",
                    code="size_too_small",
                    size_multiplier=factors.get("total_multiplier", 1.0),
                    drawdown_pct=factors.get("drawdown_pct", 0.0),
                    atr_factor=factors.get("atr_factor", 1.0),
                    trust_factor=factors.get("trust_factor", 1.0),
                ),
                locked,
                planned_liq,
                float(sized),
            )

        if is_dca:
            dca_usdt_limit = self._daily_dca_usdt_limit_blocked(sized)
            if dca_usdt_limit:
                return self._attach_liq_details(
                    dca_usdt_limit, locked, planned_liq, float(sized)
                )

        resolved_source = order.source if order.source not in ("", "auto") else source
        usdt_places = 2
        if merge_plan is not None:
            from execution.gate_adapter import venue_limits_for

            raw_places = (venue_limits_for(order.symbol) or {}).get("price_places")
            if raw_places is not None:
                usdt_places = max(2, int(raw_places))
        approved = TradeOrder(
            type=order.type,
            symbol=order.symbol,
            price=order.price,
            amount=order.amount,
            usdt_amount=round(float(sized), usdt_places),
            signal=order.signal,
            source=resolved_source,
            order_id=order.order_id,
            timestamp=order.timestamp,
            exposure_multiplier=getattr(order, "exposure_multiplier", None),
            ctx_oracle_state=getattr(order, "ctx_oracle_state", None),
            ctx_coin_regime=getattr(order, "ctx_coin_regime", None),
            ctx_volume_rel=getattr(order, "ctx_volume_rel", None),
            ctx_volume_window_days=getattr(order, "ctx_volume_window_days", None),
        )
        approved_decision = RiskDecision(
            approved=True,
            order=approved,
            message="Approved",
            size_multiplier=factors.get("total_multiplier", 1.0),
            drawdown_pct=factors.get("drawdown_pct", 0.0),
            atr_factor=factors.get("atr_factor", 1.0),
            trust_factor=factors.get("trust_factor", 1.0),
        )
        if locked.apply_cap or locked.liq_cap_usdt is not None or locked.band_bid_usdt is not None:
            return self._attach_liq_details(
                approved_decision, locked, planned_liq, float(approved.usdt_amount)
            )
        return approved_decision

    def status_summary(self, current_price: float = None) -> dict:
        history = self._primary_history()
        mtm = self._portfolio_equity(current_price or 0)
        equity = float(mtm) if mtm is not None else 0.0
        initial = self._initial_capital()
        drawdown_pct = self._equity_drawdown_pct()
        throttle_at = float(self.config.risk_config.get("drawdown_throttle_pct", 10.0))
        cash = self._available_usdt()
        floor_abs = self._cash_floor_abs()
        spendable = self._spendable_usdt(equity, is_dca=False)
        full_slots = count_open_full_slots(self.config.raw)
        daily = self._daily_counters_from_orders(self._load_orders_document())
        out = {
            "open_positions": count_open_positions(),
            "open_full_slots": full_slots,
            "max_open_positions": self.config.max_open_positions,
            "daily_trades": daily["daily_trades"],
            "daily_buys": daily["daily_buys"],
            "daily_dca_buys": daily["daily_dca_buys"],
            "daily_dca_usdt": round(daily["daily_dca_usdt"], 2),
            "daily_sells": daily["daily_sells"],
            "max_daily_trades": self._effective_max_daily_buys(),
            "max_daily_buys": self._effective_max_daily_buys(),
            "max_daily_dca_buys": self._effective_max_daily_dca_buys(),
            "max_daily_dca_usdt": self._effective_max_daily_dca_usdt(),
            "max_daily_sells": self._effective_max_daily_sells(),
            "max_position_percent": self.config.max_position_percent,
            "base_usdt_per_trade": self._base_usdt_cap(),
            "portfolio_equity": round(equity, 2),
            "initial_capital": initial,
            "drawdown_pct": round(drawdown_pct, 2),
            "drawdown_throttle_active": drawdown_pct >= throttle_at,
            "virtual_balance": cash,
            "cash_floor_abs": round(floor_abs, 2),
            "cash_floor_pct": float(self.config.risk_config.get("cash_floor_pct", 0) or 0),
            "spendable_usdt": round(spendable, 2),
            "ledger_source": self._ledger_source_label(),
            "risk_halt_until": str((history or {}).get("risk_halt_until") or ""),
            "max_daily_loss_pct": float(
                self.config.risk_config.get("max_daily_loss_pct", 0) or 0
            ),
        }
        pol = self._evaluate_cash_policy(equity)
        if pol is not None and pol.enabled:
            out["cash_policy_enabled"] = True
            out["cash_mode"] = pol.mode
            out["cash_floor_pct_eff"] = round(pol.floor_pct_eff, 2)
            out["spendable_new"] = round(pol.spendable_new, 2)
            out["spendable_dca"] = round(pol.spendable_dca, 2)
            out["dca_buffer_target"] = round(pol.dca_buffer_target, 2)
            out["cash_policy_size_mult"] = round(pol.size_mult, 4)
        else:
            out["cash_policy_enabled"] = False
        try:
            cap = self._resolve_position_capacity(full_slots=full_slots, equity=equity)
            out["max_open_eff"] = cap.max_open_eff
            out["position_capacity_enabled"] = cap.enabled
            out["position_capacity_rationale"] = cap.rationale
            out["position_capacity_factors"] = dict(cap.factors or {})
            out["free_full_slots"] = cap.free_slots
            out["capacity_regime"] = cap.regime
            # Telegram /risk still uses max_open_positions as the live gate
            if cap.enabled:
                out["max_open_positions"] = cap.max_open_eff
        except Exception:
            out["max_open_eff"] = self.config.max_open_positions
            out["position_capacity_enabled"] = False
        return out


    def _sensor_reentry_cooloff_blocked(self, order: TradeOrder, source: str):
        """Block sensor-family re-entry after a recent gross loss on the symbol."""
        from strategies.sensor_entry_policy import is_sensor_source

        if not is_sensor_source(source):
            return None
        risk = self.config.risk_config or {}
        se = risk.get("sensor_entry") or {}
        hours = float(se.get("reentry_cooloff_hours_after_gross_loss") or 0)
        if hours <= 0:
            return None
        try:
            from intelligence.memory.cache import get_coin_profile

            prof = get_coin_profile(order.symbol)
            if not prof or not isinstance(prof.features, dict):
                return None
            last_loss = prof.features.get("last_loss_at") or prof.features.get("soft_block_until")
            if not last_loss:
                return None
            # only if last loss was large
            worst = float(prof.features.get("worst_loss_usdt") or 0)
            worst_pct = float(prof.features.get("worst_loss_pct") or 0)
            min_usdt = float(
                ((self.config.raw.get("memory") or {}).get("gross_loss") or {}).get(
                    "min_loss_usdt", 500
                )
            )
            min_pct = float(
                ((self.config.raw.get("memory") or {}).get("gross_loss") or {}).get(
                    "min_loss_pct", 25
                )
            )
            if abs(worst) < min_usdt and abs(worst_pct) < min_pct:
                return None
            from datetime import datetime, timezone, timedelta

            u = str(last_loss).replace("Z", "+00:00")
            loss_dt = datetime.fromisoformat(u)
            if loss_dt.tzinfo is None:
                loss_dt = loss_dt.replace(tzinfo=timezone.utc)
            # if soft_block_until is in future, use last_loss_at for cooloff start
            start = loss_dt
            if "last_loss_at" in prof.features:
                try:
                    s2 = str(prof.features["last_loss_at"]).replace("Z", "+00:00")
                    start = datetime.fromisoformat(s2)
                    if start.tzinfo is None:
                        start = start.replace(tzinfo=timezone.utc)
                except Exception:
                    pass
            elapsed_h = (datetime.now(timezone.utc) - start).total_seconds() / 3600.0
            if elapsed_h < hours:
                return RiskDecision(
                    approved=False,
                    message=(
                        f"Sensor re-entry cooloff {order.symbol}: "
                        f"{elapsed_h:.1f}h < {hours:.0f}h after gross loss"
                    ),
                    code="sensor_reentry_cooloff",
                    size_multiplier=0.0,
                )
        except Exception:
            return None
        return None

    def _ledger_source_label(self) -> str:
        if is_live_dry_run(self.config.raw):
            return "simulated"
        if uses_exchange_ledger(self.config.trading_mode):
            return "gate"
        return "paper"

    def _effective_max_daily_buys(self) -> int:
        if is_dry_run_enhanced(self.config.raw):
            defaults = self.config.dry_run_defaults
            if defaults.get("max_daily_buys") is not None:
                return int(defaults["max_daily_buys"])
            if defaults.get("max_daily_trades") is not None:
                return int(defaults["max_daily_trades"])
        risk_cfg = self.config.risk_config
        if risk_cfg.get("max_daily_buys") is not None:
            return int(risk_cfg["max_daily_buys"])
        return self.config.max_daily_trades

    def _effective_max_daily_sells(self) -> int:
        if is_dry_run_enhanced(self.config.raw):
            defaults = self.config.dry_run_defaults
            if defaults.get("max_daily_sells") is not None:
                return int(defaults["max_daily_sells"])
        risk_cfg = self.config.risk_config
        if risk_cfg.get("max_daily_sells") is not None:
            return int(risk_cfg["max_daily_sells"])
        return int(self.config.raw.get("max_daily_sells", 0))

    def _effective_max_daily_trades(self) -> int:
        """Backward-compatible alias for buy limit."""
        return self._effective_max_daily_buys()

    def _effective_max_daily_dca_buys(self) -> int:
        if is_dry_run_enhanced(self.config.raw):
            defaults = self.config.dry_run_defaults
            if defaults.get("max_daily_dca_buys") is not None:
                return int(defaults["max_daily_dca_buys"])
        risk_cfg = self.config.risk_config
        if risk_cfg.get("max_daily_dca_buys") is not None:
            return int(risk_cfg["max_daily_dca_buys"])
        return 0

    def _effective_max_daily_dca_usdt(self) -> float:
        if is_dry_run_enhanced(self.config.raw):
            defaults = self.config.dry_run_defaults
            if defaults.get("max_daily_dca_usdt") is not None:
                return float(defaults["max_daily_dca_usdt"])
        risk_cfg = self.config.risk_config
        if risk_cfg.get("max_daily_dca_usdt") is not None:
            return float(risk_cfg["max_daily_dca_usdt"])
        return 0.0

    def _dca_limits_enabled(self) -> bool:
        return self._effective_max_daily_dca_buys() > 0

    def _liq_lock_fields(self, liq, planned_usdt: float) -> dict:
        """Cap and book fields copied off a venue result. Missing stays None."""

        def _num(obj, name: str):
            if obj is None:
                return None
            raw = getattr(obj, name, None)
            if raw is None:
                return None
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None

        band_bid = _num(liq, "band_bid_usdt")
        if band_bid is None:
            band_bid = _num(liq, "depth_bid_usdt")
        band_ask = _num(liq, "band_ask_usdt")
        if band_ask is None:
            band_ask = _num(liq, "depth_ask_usdt")
        qv = _num(liq, "qv24_usdt")
        if qv is None:
            qv = _num(liq, "quote_volume_24h_usdt")
        if liq is not None and getattr(liq, "metrics", None) is not None:
            metrics = liq.metrics
            if qv is None:
                try:
                    qv = float(metrics.quote_volume_24h_usdt)
                except (TypeError, ValueError):
                    qv = None
            if band_bid is None:
                try:
                    band_bid = float(metrics.depth_bid_usdt)
                    band_ask = float(metrics.depth_ask_usdt)
                except (TypeError, ValueError):
                    pass
        return {
            "liq_cap_usdt": _num(liq, "liq_cap_usdt"),
            "cap_depth": _num(liq, "cap_depth"),
            "cap_volume": _num(liq, "cap_volume"),
            "open_cost_usdt": None,
            "allowed_usdt": None,
            "band_bid_usdt": band_bid,
            "band_ask_usdt": band_ask,
            "qv24_usdt": qv,
            "book_age_sec": _num(liq, "book_age_sec"),
            "depth_window_pct": _num(liq, "depth_window_pct"),
            "planned_usdt": float(planned_usdt or 0),
        }

    def _open_cost_usdt(self, symbol: str, *, side: str) -> tuple[float | None, str | None]:
        """Entry-cost room already used by open lots of one side.

        Longs are ``side != short``. Shorts are ``side == short``. Lots
        parked below the exchange minimum still count. A lot with amount
        > 0 whose notional is 0 or unreadable fails closed. Unknown
        positions state fails closed. No matching lot is 0.
        """
        from strategies.short_math import is_short

        try:
            if is_positions_state_unknown():
                return None, "liq_guard_missing_input"
        except Exception:
            return None, "liq_guard_missing_input"
        try:
            lots = find_open_positions_for_symbol(
                symbol, include_below_venue_minimum=True
            )
        except Exception:
            return None, "liq_guard_missing_input"
        want_short = str(side or "") == "short"
        total = 0.0
        for _tf, lot in lots:
            if not isinstance(lot, dict):
                return None, "liq_guard_missing_input"
            try:
                short = bool(is_short(lot))
            except Exception:
                return None, "liq_guard_missing_input"
            if want_short != short:
                continue
            try:
                amount = float(lot.get("amount", 0) or 0)
            except (TypeError, ValueError):
                return None, "liq_guard_missing_input"
            if amount <= 0:
                return None, "liq_guard_missing_input"
            try:
                notional = float(position_notional_usdt(lot))
            except Exception:
                return None, "liq_guard_missing_input"
            if notional <= 0:
                return None, "liq_guard_missing_input"
            total += notional
        return total, None

    def _liq_detail_dict(self, locked: BuyLockResult, planned, final) -> dict:
        return {
            "planned_usdt": None if planned is None else float(planned),
            "final_usdt": None if final is None else float(final),
            "liq_cap_usdt": locked.liq_cap_usdt,
            "cap_depth": locked.cap_depth,
            "cap_volume": locked.cap_volume,
            "open_cost_usdt": locked.open_cost_usdt,
            "allowed_usdt": locked.allowed_usdt,
            "band_bid_usdt": locked.band_bid_usdt,
            "band_ask_usdt": locked.band_ask_usdt,
            "depth_bid_usdt": locked.band_bid_usdt,
            "depth_ask_usdt": locked.band_ask_usdt,
            "qv24_usdt": locked.qv24_usdt,
            "quote_volume_24h_usdt": locked.qv24_usdt,
            "book_age_sec": locked.book_age_sec,
            "depth_window_pct": locked.depth_window_pct,
        }

    def _liq_size_reject(
        self,
        locked: BuyLockResult,
        factors: dict | None,
        *,
        code: str,
        planned,
        final,
        message: str,
    ) -> RiskDecision:
        factors = factors or {}
        details = self._liq_detail_dict(locked, planned, final)
        details["codes"] = [code]
        return RiskDecision(
            approved=False,
            message=message,
            code=code,
            size_multiplier=factors.get("total_multiplier", 1.0),
            drawdown_pct=factors.get("drawdown_pct", 0.0),
            atr_factor=factors.get("atr_factor", 1.0),
            trust_factor=factors.get("trust_factor", 1.0),
            details=details,
        )

    def _attach_liq_details(
        self,
        decision: RiskDecision,
        locked: BuyLockResult,
        planned,
        final,
    ) -> RiskDecision:
        """Merge tape fields. Leave a merge-bound's own numbers alone."""
        details = dict(getattr(decision, "details", None) or {})
        protected = {"reason", "loss", "bound", "final_buy", "basis", "stop_pct", "buffer_pct"}
        extra = self._liq_detail_dict(locked, planned, final)
        for key, value in extra.items():
            if key in protected and key in details:
                continue
            details[key] = value
        if "codes" not in details:
            code = str(getattr(decision, "code", "") or "").strip()
            details["codes"] = [code] if code else []
        decision.details = details
        return decision

    def _log_liq_size_capped(
        self,
        order: TradeOrder,
        locked: BuyLockResult,
        planned: float,
        final: float,
    ) -> None:
        try:
            from core.tenant_context import resolve_tenant_id
            from logger import log
            from services.venue_quality import LIQ_GUARD_SIZE_CAPPED

            log(
                f"{LIQ_GUARD_SIZE_CAPPED} tenant={resolve_tenant_id()} "
                f"symbol={getattr(order, 'symbol', '')} "
                f"planned={_fmt_liq(planned)} final={_fmt_liq(final)} "
                f"liq_cap_usdt={_fmt_liq(locked.liq_cap_usdt)} "
                f"cap_depth={_fmt_liq(locked.cap_depth)} "
                f"cap_volume={_fmt_liq(locked.cap_volume)} "
                f"open_cost_usdt={_fmt_liq(locked.open_cost_usdt)}",
                "INFO",
            )
        except Exception:
            pass

    def _cut_below_pair_min(self, order: TradeOrder, sized: float) -> RiskDecision | None:
        """Reject a cut that is still above min_trade but under the pair minimum."""
        from execution.gate_adapter import venue_limits_for

        limits = venue_limits_for(order.symbol) or {}
        if not limits.get("known"):
            return None
        try:
            min_cost = float(limits.get("min_cost") or 0)
        except (TypeError, ValueError):
            return None
        if min_cost <= 0 or float(sized) + 1e-9 >= min_cost:
            return None
        return RiskDecision(
            approved=False,
            message=(
                f"Adjusted size ${float(sized):.2f} below venue minimum ${min_cost:.4g}"
            ),
            code="venue_pair_blocked",
            size_multiplier=0.0,
        )

    def _short_liq_room_reject(
        self,
        order: TradeOrder,
        liq,
        usdt: float,
    ) -> RiskDecision | None:
        """Shorts compare against short room and never shrink."""
        fields = self._liq_lock_fields(liq, usdt)
        cap = fields.get("liq_cap_usdt")
        if cap is None:
            fields["planned_usdt"] = float(usdt)
            return self._room_reject(order, fields, "liq_guard_missing_input", usdt)
        cost, err = self._open_cost_usdt(getattr(order, "symbol", "") or "", side="short")
        if err:
            fields["planned_usdt"] = float(usdt)
            return self._room_reject(order, fields, err, usdt)
        allowed = float(cap) - float(cost)
        fields["open_cost_usdt"] = float(cost)
        fields["allowed_usdt"] = allowed
        fields["planned_usdt"] = float(usdt)
        if float(usdt) > allowed:
            return self._room_reject(order, fields, "liq_guard_depth_lt_order", usdt)
        return None

    def _room_reject(
        self,
        order: TradeOrder,
        fields: dict,
        code: str,
        usdt: float,
    ) -> RiskDecision:
        locked = BuyLockResult(**{k: fields.get(k) for k in (
            "liq_cap_usdt",
            "cap_depth",
            "cap_volume",
            "open_cost_usdt",
            "allowed_usdt",
            "band_bid_usdt",
            "band_ask_usdt",
            "qv24_usdt",
            "book_age_sec",
            "depth_window_pct",
            "planned_usdt",
        )})
        details = self._liq_detail_dict(locked, usdt, None)
        details["codes"] = [code]
        return RiskDecision(
            approved=False,
            message=code,
            code=code,
            size_multiplier=0.0,
            details=details,
        )

    def _buy_lock_decision(
        self,
        order: TradeOrder,
        *,
        source: str,
        pos: dict,
        has_position: bool,
        planned_usdt: float,
        indicators: dict | None,
    ) -> BuyLockResult | None:
        """DCA lock (#640) and liquidity lock (#641) at order submit.

        Callers that reach ``RiskManager.evaluate`` on a BUY all pass here:
        ``dca``, ``dca_sniper`` / ``dca_sniper_deep``, ``deploy_boost`` (a
        size reason on those orders, not a separate path), ``dca_recovery``,
        a second-timeframe entry, ``entry_sensor_15m`` and other new-entry
        sources, and ``mcp:{actor}`` bot buys. A human operator source is
        the exact string ``manual`` (never a prefix, never ``mcp:``). It is
        the only exemption, and it does not skip the live caps.
        Sells, stops, covers and kill-switch flattens do not call this.
        """
        if str(getattr(order, "type", "") or "").upper() != "BUY":
            return None
        from strategies.dca_policy import evaluate_dca_guard, is_human_operator_buy

        src = source or getattr(order, "source", None) or ""
        # A human manual buy is not auto-blocked. The checks still run and
        # the result is logged. No config list can skip a bot source.
        human = is_human_operator_buy(src)

        codes: list[str] = []
        dca_result = None
        try:
            dca_result = evaluate_dca_guard(
                pos,
                price=getattr(order, "price", None),
                source=src,
                has_open_lot=bool(has_position),
                symbol=getattr(order, "symbol", "") or "",
                indicators=indicators,
            )
        except Exception as exc:
            dca_result = None
            if has_position:
                codes.append("dca_guard_missing_input")
                try:
                    from logger import log

                    log(f"dca guard failed {order.symbol}: {exc}", "ERROR")
                except Exception:
                    pass
        if dca_result is not None and dca_result.blocked:
            codes.extend(dca_result.codes)

        liq = None
        liq_exc = None
        try:
            from services.venue_quality import check_venue_for_buy

            liq = check_venue_for_buy(
                order.symbol,
                source=str(src),
                planned_usdt=float(planned_usdt or 0),
                config_raw=self.config.raw if hasattr(self.config, "raw") else None,
            )
        except Exception as exc:
            liq_exc = exc
            codes.append("liq_guard_missing_input")
            try:
                from logger import log

                log(f"liquidity guard failed {order.symbol}: {exc}", "ERROR")
            except Exception:
                pass
        if liq is not None and not liq.ok:
            extra = list(getattr(liq, "guard_codes", None) or [])
            if not extra and liq.code:
                extra = [liq.code]
            # #563 callers (and the #631 membership test) reject with
            # venue_liquidity_block when the result is not ok and carries
            # no code. An empty code must not fall through to a later gate.
            if not any(extra):
                extra = ["venue_liquidity_block"]
            for code in extra:
                if code and code not in codes:
                    codes.append(code)
            if liq.code and liq.code not in codes:
                codes.append(liq.code)

        fields = self._liq_lock_fields(liq, planned_usdt)
        if human:
            # Logged only. No cap, and the lot sum stays below this return
            # so a bot buy still pays for open cost.
            try:
                from logger import log

                log(
                    f"manual_buy_guard {getattr(order, 'symbol', '')} "
                    f"blocked={bool(codes)} codes={','.join(codes) or '-'}",
                    "INFO",
                )
            except Exception:
                pass
            return BuyLockResult(decision=None, apply_cap=False, **fields)

        cost, cost_err = self._open_cost_usdt(
            getattr(order, "symbol", "") or "", side="long"
        )
        if cost_err:
            if cost_err not in codes:
                codes.append(cost_err)
        else:
            fields["open_cost_usdt"] = float(cost)
            if fields.get("liq_cap_usdt") is not None:
                fields["allowed_usdt"] = float(fields["liq_cap_usdt"]) - float(cost)

        if not codes and (
            fields.get("liq_cap_usdt") is None or fields.get("allowed_usdt") is None
        ):
            codes.append("liq_guard_missing_input")

        if not codes:
            return BuyLockResult(decision=None, apply_cap=True, **fields)

        price = getattr(order, "price", None)
        avg = dca_result.avg if dca_result is not None else None
        rounds = dca_result.dca_rounds if dca_result is not None else None
        locked_flag = dca_result.locked if dca_result is not None else None
        snapshot = BuyLockResult(**fields)
        details = self._liq_detail_dict(snapshot, fields.get("planned_usdt"), None)
        details.update(
            {
                "codes": list(codes),
                "price": price,
                "avg": avg,
                "dca_rounds": rounds,
                "locked": locked_flag,
            }
        )
        primary = codes[0]
        bits = [primary]
        if price is not None:
            bits.append(f"price={price}")
        if avg is not None:
            bits.append(f"avg={avg}")
        if rounds is not None:
            bits.append(f"dca_rounds={rounds}")
        if locked_flag is not None:
            bits.append(f"locked={locked_flag}")
        if details.get("planned_usdt"):
            bits.append(f"planned={details['planned_usdt']}")
        if liq_exc is not None:
            bits.append("gate_error")
        if liq is not None and liq.reasons:
            bits.append("; ".join(str(r) for r in liq.reasons)[:240])
        decision = RiskDecision(
            approved=False,
            message=" ".join(str(b) for b in bits)[:400],
            code=primary,
            size_multiplier=0.0,
            details=details,
        )
        return BuyLockResult(decision=decision, apply_cap=False, **fields)

    @staticmethod
    def _is_dca_buy(source: str, order: TradeOrder) -> bool:
        src = str(source or "").strip().lower()
        if src in ("dca", "dca_recovery", "dca_sniper", "dca_scheduled"):
            return True
        return str(getattr(order, "signal", "") or "").upper() == "BUY_DCA"

    @staticmethod
    def _is_relvol_buy(source: str, order: TradeOrder) -> bool:
        """RelVol discovery path — allowed outside the 500k trade universe."""
        src = str(source or "").strip().lower()
        if src == "gainer_relvol" or src.startswith("relvol"):
            return True
        sig = str(getattr(order, "signal", "") or "").upper()
        return sig == "GAINER_RELVOL"

    @staticmethod
    def _order_is_dca(order: dict) -> bool:
        src = str(order.get("source", "")).lower()
        if src in ("dca", "dca_recovery", "dca_sniper", "dca_scheduled"):
            return True
        return str(order.get("signal", "")).upper() == "BUY_DCA"

    @staticmethod
    def _filled_order_usdt(order: dict) -> float:
        for key_path in (
            ("risk", "approved_usdt"),
            ("execution", "usdt"),
            ("request", "usdt"),
        ):
            section, field = key_path
            raw = (order.get(section) or {}).get(field)
            if raw is not None:
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    continue
        return 0.0

    def _daily_buy_limit_blocked(self, is_dca: bool) -> RiskDecision | None:
        if is_dca and self._dca_limits_enabled():
            daily_dca = self._daily_dca_buys_count()
            max_dca = self._effective_max_daily_dca_buys()
            if daily_dca >= max_dca:
                return RiskDecision(
                    approved=False,
                    message=f"Daily DCA limit reached ({daily_dca}/{max_dca})",
                    code="max_daily_dca_buys",
                )
            return None

        daily_buys = self._daily_buys_count(
            dca_only=False if self._dca_limits_enabled() else None,
        )
        max_daily_buys = self._effective_max_daily_buys()
        if max_daily_buys > 0 and daily_buys >= max_daily_buys:
            return RiskDecision(
                approved=False,
                message=f"Daily buy limit reached ({daily_buys}/{max_daily_buys})",
                code="max_daily_trades",
            )
        return None

    def _daily_dca_usdt_limit_blocked(self, sized_usdt: float) -> RiskDecision | None:
        max_usdt = self._effective_max_daily_dca_usdt()
        if max_usdt <= 0:
            return None
        spent = self._daily_dca_usdt_sum()
        if spent + sized_usdt > max_usdt:
            return RiskDecision(
                approved=False,
                message=(
                    f"Daily DCA USDT limit reached "
                    f"(${spent:.0f}+${sized_usdt:.0f}>${max_usdt:.0f})"
                ),
                code="max_daily_dca_usdt",
            )
        return None

    @staticmethod
    def _parse_iso_dt(raw) -> datetime | None:
        if not raw:
            return None
        try:
            ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return ts.astimezone(timezone.utc)
        except Exception:
            return None

    def _risk_history_load(self) -> dict:
        if is_live_dry_run(self.config.raw):
            return load_live_trade_history() or {}
        return load_trade_history() or {}

    def _risk_history_save(self, history: dict) -> None:
        from data_manager import save_live_trade_history, save_trade_history

        if is_live_dry_run(self.config.raw):
            save_live_trade_history(history)
        else:
            save_trade_history(history)

    def _trailing_24h_realized_pnl(self) -> float:
        """Filled-order realized PnL over the trailing 24h (order-service window)."""
        from data_manager import resolve_ledger_scope
        from services.order_service import OrderService, _display_now_naive

        # _stats_filled_window filters through order_event_ts, which is naive in
        # the DISPLAY timezone. A naive datetime.now() is the process clock (UTC
        # on Railway), so the window was shifted by the UTC offset: the last two
        # hours of fills were missing and fills from 24-26 h ago counted instead
        # -- exactly the wrong window for a kill switch (review of PR #322).
        now = _display_now_naive()
        start = now - timedelta(hours=24)
        stats = OrderService(resolve_ledger_scope())._stats_filled_window(start, now)
        raw_pnl = stats.get("realized_pnl") if isinstance(stats, dict) else None
        if raw_pnl is None:
            from logger import log

            log("trailing 24h realized pnl missing", "ERROR")
            return 0.0
        return float(raw_pnl)

    def _configured_max_daily_loss_usdt(self) -> float | None:
        """Optional USDT daily-loss cap. Missing key → unset (pct path only)."""
        raw = self.config.raw if hasattr(self.config, "raw") else None
        if not isinstance(raw, dict) or "max_daily_loss_usdt" not in raw:
            return None
        val = raw.get("max_daily_loss_usdt")
        if val is None or val == "":
            return None
        try:
            num = float(val)
        except (TypeError, ValueError):
            return None
        if num <= 0:
            return None
        return num

    def _operator_zone(self):
        """Zone from ``observability.operator_timezone``. No zone name in this module."""
        from zoneinfo import ZoneInfo

        from core.time_utils import operator_tz

        raw = self.config.raw if hasattr(self.config, "raw") else None
        obs = raw.get("observability") if isinstance(raw, dict) else None
        name = ""
        if isinstance(obs, dict):
            name = str(obs.get("operator_timezone") or "").strip()
        if name:
            try:
                return ZoneInfo(name)
            except Exception:
                pass
        return operator_tz()

    def _realized_pnl_operator_day(self, now: datetime | None = None) -> float:
        """Realized PnL of filled sells/covers since 00:00 in the operator zone."""
        from data_manager import resolve_ledger_scope
        from services.order_service import OrderService, is_executed_status, order_event_ts_utc
        from storage.order_ledger_v2 import stats_from_filled_orders

        tz = self._operator_zone()
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        local = moment.astimezone(tz)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        start_utc = start.astimezone(timezone.utc)
        end_utc = end.astimezone(timezone.utc)
        scope = resolve_ledger_scope(self.config.trading_mode)
        orders = OrderService(scope)._scoped_orders_newest_first(mutate=False)
        chosen = []
        for row in orders or []:
            if not isinstance(row, dict):
                continue
            if not is_executed_status(row.get("status")):
                continue
            ts = order_event_ts_utc(row)
            if ts is None:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if start_utc <= ts.astimezone(timezone.utc) < end_utc:
                chosen.append(row)
        raw_pnl = stats_from_filled_orders(chosen).get("realized_pnl")
        if raw_pnl is None:
            from logger import log

            log("operator day realized pnl missing", "ERROR")
            return 0.0
        return float(raw_pnl)

    def _open_live_unrealized_pnl(self) -> float:
        """Unrealized PnL of this tenant's open live lots at mark. Paper is excluded."""
        from core.tenant_context import resolve_tenant_id
        from strategies.positions import counts_toward_open_cap, list_active_positions
        from strategies.short_math import is_short, unrealized_pnl

        lots = list_active_positions(tenant_id=resolve_tenant_id(), scope="live")
        parsed: list[tuple] = []
        need: list[str] = []
        for lot in lots or []:
            if not counts_toward_open_cap(lot):
                continue
            try:
                amount = float(lot.get("amount") or 0)
                entry = float(lot.get("average_entry") or lot.get("entry_price") or 0)
            except (TypeError, ValueError):
                continue
            if amount <= 1e-12 or entry <= 0:
                continue
            mark_raw = lot.get("current_price")
            try:
                mark = float(mark_raw) if mark_raw not in (None, "") else 0.0
            except (TypeError, ValueError):
                mark = 0.0
            sym = str(lot.get("symbol") or "")
            if sym and "/" not in sym:
                sym = f"{sym}/USDT"
            parsed.append((lot, amount, entry, mark, sym))
            if mark <= 0 and sym:
                need.append(sym)
        prices: dict = {}
        if need:
            from price_fetcher import get_prices_batch

            try:
                prices = get_prices_batch(list(dict.fromkeys(need))) or {}
            except Exception as exc:
                # A lookup that raises is the same as an empty book: no mark.
                # Deny even when fail_closed_guards is log. Do not halt 24h.
                raise _MissingMark(
                    f"mark lookup failed for open live lot: {exc}"
                ) from exc
        total = 0.0
        for lot, amount, entry, mark, sym in parsed:
            if mark <= 0:
                try:
                    mark = float((prices or {}).get(sym) or 0)
                except (TypeError, ValueError):
                    mark = 0.0
            if mark <= 0:
                raise _MissingMark(f"mark missing for open live lot {sym}")
            side = "short" if is_short(lot) else "long"
            total += float(unrealized_pnl(side, amount, entry, mark))
        return total

    def _daily_loss_usdt_figure(self, now: datetime | None = None) -> float:
        """Operator-day realized PnL plus unrealized PnL of open live lots."""
        return float(self._realized_pnl_operator_day(now)) + float(
            self._open_live_unrealized_pnl()
        )

    def _filled_pnl_missing_count(self) -> int:
        """Marked sell/cover rows whose pnl is still missing.

        Legacy rows without the marker are counted in a log line and do not
        block. This check does not read ``fail_closed_guards``.
        """
        from data_manager import resolve_ledger_scope
        from logger import log
        from services.order_service import OrderService
        from storage.order_ledger_v2 import split_missing_pnl

        scope = resolve_ledger_scope(getattr(self.config, "trading_mode", None))
        orders = OrderService(scope)._scoped_orders_newest_first(mutate=False)
        tracked, legacy = split_missing_pnl(orders or [])
        if legacy > 0:
            log(f"pnl_missing_legacy count={legacy}", "WARNING")
        return int(tracked)

    def _missing_pnl_entry_block(self, order=None) -> RiskDecision | None:
        """Block new exposure while a marked fill's pnl is unresolved (#679 K2).

        Buys and shorts only. A raise blocks those entries even when
        ``fail_closed_guards`` is ``log``. Sells, covers, and stops do not
        enter this function from ``_evaluate_impl``.
        """
        kind = str(getattr(order, "type", "") or "").upper()
        if kind not in ("BUY", "SHORT"):
            return None
        try:
            missing = self._filled_pnl_missing_count()
        except Exception as exc:
            from logger import log

            symbol = getattr(order, "symbol", "") or ""
            log(f"risk guard pnl_missing failed {symbol}: {exc}", "ERROR")
            return RiskDecision(
                approved=False,
                message=f"pnl_missing check failed: {exc}"[:200],
                code="pnl_missing",
                size_multiplier=0.0,
            )
        if missing <= 0:
            return None
        return RiskDecision(
            approved=False,
            message=(
                f"pnl_missing={missing}: new exposure blocked until recovery "
                "resolves the row"
            ),
            code="pnl_missing",
            size_multiplier=0.0,
        )

    def _daily_loss_limit_blocked(self, order=None) -> RiskDecision | None:
        """Block new BUY/SHORT on the pct rail, and on optional USDT loss.

        ``max_daily_loss_usdt`` unset → the pct check only (same figure, same
        24h window, same halt). Set → also stop new exposure when operator-day
        realized PnL plus open-live unrealized PnL reaches the USDT value.
        The stricter of the two wins. Sells and exits do not call this.
        """
        try:
            pct = float(self.config.risk_config.get("max_daily_loss_pct", 0) or 0)
        except (TypeError, ValueError):
            pct = 0.0
        usdt_limit = self._configured_max_daily_loss_usdt()
        if pct <= 0 and usdt_limit is None:
            return None
        history = self._risk_history_load()
        if not isinstance(history, dict):
            history = {}
        until = self._parse_iso_dt(history.get("risk_halt_until"))
        now = datetime.now(timezone.utc)
        if until is not None and until > now:
            return RiskDecision(
                approved=False,
                message=f"Daily loss limit: halted until {history.get('risk_halt_until')}",
                code="daily_loss_limit",
                size_multiplier=0.0,
            )
        # #408: a halt this process computed but could not persist. The reloaded
        # history has no risk_halt_until, yet the halt is still in force — do not
        # fail-open just because the save raised.
        mem_until = _daily_loss_halt_memory()
        if mem_until is not None and mem_until > now:
            return RiskDecision(
                approved=False,
                message=(
                    f"Daily loss limit: halted until {mem_until.isoformat()} "
                    "(in-memory; persist failed)"
                ),
                code="daily_loss_limit",
                size_multiplier=0.0,
            )
        pct_breach = False
        realized = 0.0
        nav = 0.0
        if pct > 0:
            try:
                # Below-min open loss is measured separately. It is not fed into
                # this halt: v1.3 has no brake and no daily-loss cap.
                realized = float(self._trailing_24h_realized_pnl())
            except Exception as e:
                if usdt_limit is None:
                    # Same rollout switch as every other guard: 'log' -> ERROR + allow
                    # (old behaviour, visible), 'deny' -> block new exposure (#302 audit).
                    return self._guard_failed("daily_loss_limit", e, order)
                # Both limits are set and the pct figure failed. Do not pretend
                # the pct check ran; the USDT check still decides.
                try:
                    from logger import log

                    log(
                        "daily_loss_limit pct figure failed "
                        f"({type(e).__name__}: {e}); USDT check still runs",
                        "ERROR",
                    )
                except Exception:
                    pass
                realized = 0.0
            else:
                nav = self._portfolio_equity()
                if nav is None or float(nav) <= 0:
                    nav = float(self._initial_capital() or 0)
                if usdt_limit is None and (nav is None or float(nav) <= 0):
                    return None
                if nav is not None and float(nav) > 0:
                    threshold = -(pct / 100.0) * float(nav)
                    pct_breach = realized <= threshold
                elif usdt_limit is None:
                    return None
        usdt_figure = None
        usdt_breach = False
        if usdt_limit is not None:
            try:
                usdt_figure = float(self._daily_loss_usdt_figure())
            except _MissingMark as e:
                # A missing mark must deny even when fail_closed_guards is log.
                return self._daily_loss_mark_denied(e, order)
            except Exception as e:
                return self._guard_failed("daily_loss_limit", e, order)
            usdt_breach = usdt_figure <= -float(usdt_limit)
        if not pct_breach and not usdt_breach:
            return None
        if pct_breach:
            detail = (
                f"Daily loss limit: realized 24h ${realized:.0f} "
                f"≤ -{pct:g}% of NAV ${float(nav):.0f}"
            )
            notify = (
                f"🛑 Daily loss limit: realized 24h ${realized:.0f} "
                f"≤ -{pct:g}% of NAV ${float(nav):.0f}. "
            )
        else:
            detail = (
                f"Daily loss limit: day pnl ${float(usdt_figure):.2f} "
                f"≤ -{usdt_limit:g} USDT"
            )
            notify = (
                f"🛑 Daily loss limit: day pnl ${float(usdt_figure):.2f} "
                f"≤ -{usdt_limit:g} USDT. "
            )
        if pct_breach and usdt_breach:
            detail += (
                f"; day pnl ${float(usdt_figure):.2f} ≤ -{usdt_limit:g} USDT"
            )
        halt_until = now + timedelta(hours=24)
        halt_iso = halt_until.isoformat()
        history["risk_halt_until"] = halt_iso
        persist_error: Exception | None = None
        try:
            self._risk_history_save(history)
        except Exception as e:
            # #408: never swallow this. A lost risk_halt_until fails open on the
            # next restart. Keep the halt alive in-process and make the failure
            # visible (ERROR + operator).
            persist_error = e
            _remember_daily_loss_halt(halt_until)
            try:
                from logger import log

                log(
                    f"daily_loss_limit: persisting risk_halt_until={halt_iso} failed "
                    f"({type(e).__name__}: {e}); halt kept in-memory for this process "
                    "only and will NOT survive a restart",
                    "ERROR",
                )
            except Exception:
                pass
        try:
            from core.operator_notify import notify_operator

            text = notify + f"New buys/shorts halted until {halt_iso}."
            if persist_error is not None:
                text += (
                    f"\n⚠️ Halt NOT persisted ({type(persist_error).__name__}: "
                    f"{persist_error}) — it is lost on restart."
                )
            notify_operator(text)
        except Exception:
            pass
        return RiskDecision(
            approved=False,
            message=detail,
            code="daily_loss_limit",
            size_multiplier=0.0,
        )

    def _daily_loss_mark_denied(self, exc: BaseException, order=None) -> RiskDecision:
        """Missing mark on the USDT loss figure. Deny regardless of the log/deny switch."""
        symbol = getattr(order, "symbol", "") or ""
        try:
            from logger import log

            log(
                f"daily_loss_limit mark missing {symbol} ({exc}); denying new exposure",
                "ERROR",
            )
        except Exception:
            pass
        try:
            from core.operator_notify import notify_operator

            notify_operator(
                f"🛑 Daily loss limit: mark missing ({exc}). New buys/shorts blocked."
            )
        except Exception:
            pass
        return RiskDecision(
            approved=False,
            message=f"Daily loss limit: {exc}"[:200],
            code="daily_loss_limit",
            size_multiplier=0.0,
        )

    _LIVE_CAP_BODY_KEYS = (
        "max_usdt_per_trade",
    )

    def _live_real_money_buys(self) -> bool:
        """R3 applies when fills are real money, or a live book sets dry_run false.

        ``places_real_orders`` is the accounting truth (#410) and is enough on
        its own, including a fail-closed unresolvable real mode. ``dry_run``
        false on ``trading_mode=live`` stays on too, so a shadow execution
        alias does not drop the check. An unset trading mode stays off:
        that helper returns shadow before it reads ``live.execution``.
        """
        raw = self.config.raw if hasattr(self.config, "raw") else None
        if not isinstance(raw, dict):
            return False
        from core.execution_mode import places_real_orders

        mode = str(raw.get("trading_mode") or "").strip().lower()
        live = raw.get("live") if isinstance(raw.get("live"), dict) else {}
        if places_real_orders(raw):
            return True
        return mode == "live" and live.get("dry_run") is False

    def _tenant_override_for_caps(self) -> tuple[dict | None, str]:
        """Tenant override body, or (None, reason) when it did not load."""
        from core.tenant_context import DEFAULT_TENANT, multi_tenant_enabled, resolve_tenant_id

        if not multi_tenant_enabled():
            return None, "multi_tenant_off"
        tid = resolve_tenant_id()
        if not tid or tid == DEFAULT_TENANT:
            return None, "tenant_unknown"
        try:
            from data_manager import (
                _load_default_config_from_disk,
                _load_tenant_config_body,
                _should_use_mongo_for_tenant_config,
            )

            default_cfg = _load_default_config_from_disk()
            if not _should_use_mongo_for_tenant_config(default_cfg):
                return None, "tenant_store_unavailable"
            body = _load_tenant_config_body(tid, default_cfg)
        except Exception as exc:
            return None, f"tenant_body_load_error:{type(exc).__name__}"
        if not isinstance(body, dict) or not body:
            return None, "tenant_body_empty"
        return body, ""

    @staticmethod
    def _positive_cap_value(val) -> float | None:
        """A cap the guard can enforce. 0, blank, None, and non-numbers are not."""
        if val is None or isinstance(val, bool):
            return None
        if isinstance(val, str) and not val.strip():
            return None
        try:
            num = float(val)
        except (TypeError, ValueError):
            return None
        if num != num or num <= 0 or num == float("inf"):
            return None
        return num

    def _effective_cap_problems(self) -> list[str]:
        """Names of caps whose value the guard would actually use is not > 0.

        ``trading_mode=live`` prefers ``live.max_usdt_per_trade`` inside this
        class. Other sizing paths read the top-level key. Either number above
        the other is a size that would be sent, so both are rejected here.
        """
        raw = self.config.raw if isinstance(getattr(self.config, "raw", None), dict) else {}
        problems: list[str] = []
        top = self._positive_cap_value(raw.get("max_usdt_per_trade"))
        live = raw.get("live") if isinstance(raw.get("live"), dict) else {}
        live_has_ticket = isinstance(live, dict) and "max_usdt_per_trade" in live
        live_num = (
            self._positive_cap_value(live.get("max_usdt_per_trade")) if live_has_ticket else None
        )
        try:
            used = float(self._base_usdt_cap())
        except (TypeError, ValueError):
            used = 0.0
        if self._positive_cap_value(used) is None or top is None:
            problems.append("max_usdt_per_trade")
        if str(raw.get("trading_mode") or "").strip().lower() == "live" and live_has_ticket:
            if live_num is None or (top is not None and live_num > top):
                problems.append("live.max_usdt_per_trade")
        # Portfolio, gainer, and Telegram defaults read the top-level ticket.
        # A top-level number above the live cap is the size those paths would send.
        if (
            live_num is not None
            and top is not None
            and top > live_num
            and "max_usdt_per_trade" not in problems
        ):
            problems.append("max_usdt_per_trade")
        try:
            from risk.slot_eviction import eviction_mode

            if eviction_mode(self.config.risk_config) == "live":
                problems.append("slot_eviction")
        except Exception:
            problems.append("slot_eviction")
        # allow_live stays false, so a short already on the live book cannot
        # be opened or covered. Do not start real-money trading on that book.
        if self._live_scope_has_open_short():
            problems.append("open_short")
        return problems

    def _live_scope_has_open_short(self) -> bool:
        """True when this tenant's live book has an open short, or cannot be read."""
        try:
            from core.tenant_context import resolve_tenant_id
            from strategies.positions import counts_toward_open_cap, list_active_positions
            from strategies.short_math import is_short

            lots = list_active_positions(tenant_id=resolve_tenant_id(), scope="live") or []
        except Exception:
            return True
        for lot in lots:
            if not isinstance(lot, dict) or not counts_toward_open_cap(lot):
                continue
            if is_short(lot):
                return True
        return False

    def _own_tail_baseline_problems(self, body: dict) -> tuple[list[str], list[str]]:
        """Own-body tail keys and baseline. Inherited config does not count.

        ``rotation_config`` reads ``sell_policy.rotation.tail_exempt_notional_usdt``
        and ``tail_exempt_sold_pct``. ``live_sim_initial_capital`` reads top-level
        ``initial_capital_usdt``. ``0 < T < risk.min_trade_usdt`` is also below
        the live ticket. ``S > 0.5``. Baseline is a number > 0.
        """
        missing: list[str] = []
        bad: list[str] = []
        sell = body.get("sell_policy") if isinstance(body.get("sell_policy"), dict) else {}
        rotation = sell.get("rotation") if isinstance(sell.get("rotation"), dict) else {}
        notional_key = "tail_exempt_notional_usdt"
        if notional_key not in rotation:
            missing.append(notional_key)
        else:
            try:
                min_trade = float(self.config.risk_config.get("min_trade_usdt", 5.0))
            except (TypeError, ValueError):
                min_trade = 0.0
            t = self._positive_cap_value(rotation.get(notional_key))
            if t is None or not (t < min_trade):
                bad.append(notional_key)
        sold_key = "tail_exempt_sold_pct"
        if sold_key not in rotation:
            missing.append(sold_key)
        else:
            sold = self._positive_cap_value(rotation.get(sold_key))
            if sold is None or not (sold > 0.5):
                bad.append(sold_key)
        capital_key = "initial_capital_usdt"
        if capital_key not in body:
            missing.append(capital_key)
        elif self._positive_cap_value(body.get(capital_key)) is None:
            bad.append(capital_key)
        return missing, bad

    def _live_caps_missing_blocked(self, order=None) -> RiskDecision | None:
        """Fail closed when a real-money tenant cannot trade under its caps.

        The tenant body must load and carry the ticket. It must also set the
        tail keys and ``initial_capital_usdt`` itself; config.json and presets
        do not count. The ticket the guard reads must be a number > 0. A
        live-block ticket above the top-level ticket is the same failure.
        Sells and covers never call this. Short opens do.
        """
        if not self._live_real_money_buys():
            return None
        kind = str(getattr(order, "type", "") or "").upper() if order is not None else "BUY"
        if kind not in ("BUY", "SHORT"):
            return None
        body, err = self._tenant_override_for_caps()
        parts: list[str] = []
        if body is None:
            parts.append(err or "tenant_body_missing")
        else:
            missing = [key for key in self._LIVE_CAP_BODY_KEYS if key not in body]
            own_missing, own_bad = self._own_tail_baseline_problems(body)
            missing.extend(own_missing)
            if missing:
                parts.append("missing:" + ",".join(missing))
            bad = self._effective_cap_problems()
            bad.extend(own_bad)
            if bad:
                parts.append("effective:" + ",".join(bad))
        reason = " ".join(parts)
        if not reason:
            return None
        symbol = getattr(order, "symbol", "") or ""
        try:
            from logger import log

            log(f"live_caps_missing {symbol} {reason}", "ERROR")
        except Exception:
            pass
        try:
            from core.operator_notify import notify_operator

            notify_operator(
                f"live_caps_missing {symbol} {reason}. "
                "New buys and shorts blocked; sells still run."
            )
        except Exception:
            pass
        return RiskDecision(
            approved=False,
            message=f"live_caps_missing {reason}"[:200],
            code="live_caps_missing",
            size_multiplier=0.0,
        )

    def _mark_is_fresh(self, symbol: str, price: float) -> bool:
        """True only when the order price matches a non-expired cached mark."""
        if price <= 0:
            return False
        try:
            from price_fetcher import _cache_get, stale_expired_symbols

            if symbol in stale_expired_symbols():
                return False
            cached = _cache_get(symbol)
            if cached is None:
                return False
            return abs(float(cached) - float(price)) <= 1e-8 * max(1.0, float(price))
        except Exception:
            return False

    def _refresh_venue_mark(self, symbol: str, timeframe: str, price: float) -> None:
        from strategies.positions import refresh_venue_min_mark

        try:
            px = float(price or 0)
        except (TypeError, ValueError):
            px = 0.0
        refresh_venue_min_mark(
            symbol,
            timeframe,
            mark=px if px > 0 else None,
            mark_fresh=self._mark_is_fresh(symbol, px),
        )

    def _cfg_number(self, key: str, default: float) -> float:
        """Dry-run block first when enhanced, then risk. Explicit 0 is kept."""
        from core.simulated_trading import is_dry_run_enhanced

        raw = self.config.raw if hasattr(self.config, "raw") else {}
        if is_dry_run_enhanced(raw):
            defaults = self.config.dry_run_defaults or {}
            if key in defaults and defaults.get(key) is not None:
                return float(defaults[key])
        risk = self.config.risk_config or {}
        if key in risk and risk.get(key) is not None:
            return float(risk[key])
        return float(default)

    def _base_usdt_cap(self) -> float:
        if self.config.trading_mode == "live":
            return float(
                self.config.live_config.get("max_usdt_per_trade", self.config.max_usdt_per_trade)
            )
        return self.config.max_usdt_per_trade

    def _initial_capital(self) -> float:
        from core.portfolio_baseline import initial_capital
        from data_manager import resolve_ledger_scope

        return initial_capital(
            scope=resolve_ledger_scope(self.config.trading_mode),
            config=self.config.raw,
            trading_mode=self.config.trading_mode,
        )

    def _primary_history(self) -> dict:
        if uses_exchange_ledger(self.config.trading_mode):
            return load_live_trade_history()
        return load_trade_history()

    def _available_usdt(self, fallback: float = 0) -> float:
        from core.simulated_trading import is_simulated_trading, uses_order_ledger_cash
        from data_manager import resolve_sim_cash_balance

        if is_simulated_trading(self.config.raw):
            if uses_order_ledger_cash(self.config.raw):
                return resolve_sim_cash_balance(config=self.config.raw)
            history = load_live_trade_history()
            return float(history.get("virtual_balance", simulated_balance_usdt(self.config.raw)))
        if is_live_dry_run(self.config.raw):
            history = load_live_trade_history()
            return float(history.get("virtual_balance", simulated_balance_usdt(self.config.raw)))
        if uses_exchange_ledger(self.config.trading_mode):
            return fetch_usdt_balance(self.config)
        return float(load_trade_history().get("virtual_balance", fallback))

    def _cash_floor_basis_ref(
        self, equity: float | None = None, *, adaptive: bool = False
    ) -> float:
        """Reference capital for floor % → absolute conversion."""
        risk = self.config.risk_config
        pol = risk.get("cash_policy") if isinstance(risk.get("cash_policy"), dict) else {}
        # Adaptive floor_basis only when policy is active; else legacy cash_floor_basis
        if adaptive and pol:
            basis = str(pol.get("floor_basis") or risk.get("cash_floor_basis", "initial") or "initial")
        else:
            basis = str(risk.get("cash_floor_basis", "initial") or "initial")
        basis = basis.lower()
        if basis == "nav":
            if equity is not None:
                return float(equity)
            return self._equity_for_sizing()
        return float(self._initial_capital())

    def _market_bias_for_cash(self) -> dict:
        """Fusion/global bias for cash policy; fail-open to neutral unless deny."""
        from services.market_policy_fusion import get_global_market_bias

        try:
            out = dict(get_global_market_bias(self.config.raw) or {})
            if _fail_closed_guards_mode(self.config) == "deny" and out.get("degraded"):
                r = str(out.get("regime") or "").upper()
                if r in ("", "RISK_ON", "UNKNOWN"):
                    out["regime"] = "NEUTRAL"
            return out
        except Exception as e:
            dec = self._guard_failed("market_bias_for_cash", e, None)
            if dec is not None:
                return {"size_mult": 0.0, "block_buys": True, "regime": None}
            return {"size_mult": 1.0, "block_buys": False, "regime": None}

    def _process_uptime_sec(self) -> float | None:
        try:
            from services.market_oracle_store import process_uptime_sec

            return float(process_uptime_sec())
        except Exception:
            return None

    def _open_book_memory_counts(self) -> tuple[int, int, int]:
        """soft_block / toxic / prefer among open positions — fail-open (0,0,0)."""
        try:
            from intelligence.memory.cache import get_coin_profile
            from risk.position_capacity import count_open_book_memory_signals

            return count_open_book_memory_signals(
                list_active_positions(),
                get_profile=lambda sym: get_coin_profile(
                    sym, config=self.config.raw if hasattr(self.config, "raw") else None
                ),
            )
        except Exception:
            return 0, 0, 0

    def _open_slot_cap_blocked(self, order, source: str) -> RiskDecision | None:
        """Reject a new buy or short open when the book has no free slot.

        Buys and short opens share this check. Live caps count every open
        long and short, including rotation tails. A freed slot (eviction
        sell filled) returns None so the caller continues.
        """
        open_slots = count_open_full_slots(self.config.raw)
        cap = self._resolve_position_capacity(full_slots=open_slots)
        if open_slots < cap.max_open_eff:
            return None
        from risk.position_capacity import format_capacity_reject_message

        msg = format_capacity_reject_message(cap, open_slots)
        free = max(0, int(cap.max_open_eff) - int(open_slots))
        evicted_ok = False
        try:
            from risk.slot_eviction_runtime import try_slot_eviction_on_max_open

            plan, suffix = try_slot_eviction_on_max_open(
                order=order,
                source=source,
                free_full_slots=free,
                config=self.config,
                risk_config=self.config.risk_config,
                config_raw=self.config.raw if hasattr(self.config, "raw") else None,
                spike_multiple=float(
                    getattr(order, "entry_15m_vol_ratio", None) or 0
                ),
                risk_manager=self,
            )
            if suffix:
                msg = f"{msg}{suffix}"
            veto = str(getattr(plan, "veto_reason", "") or "") if plan is not None else ""
            if veto == "no_positive_price":
                return RiskDecision(
                    approved=False,
                    message=msg or "slot eviction aborted: no positive price",
                    code="slot_eviction_no_price",
                )
            # Structured flag set by the runtime after the eviction sell filled —
            # never infer execution from the human-readable suffix (#300 audit).
            sell_executed = bool(getattr(plan, "sell_executed", False))
            if sell_executed:
                open_slots = count_open_full_slots(self.config.raw)
                if open_slots < cap.max_open_eff:
                    evicted_ok = True
        except Exception:
            pass
        if not evicted_ok:
            return RiskDecision(
                approved=False,
                message=msg,
                code="max_open_positions",
            )
        return None

    def _resolve_position_capacity(
        self,
        *,
        full_slots: int | None = None,
        equity: float | None = None,
    ):
        """Fusion + cash + memory → CapacitySnapshot (fail-open to static base)."""
        from risk.cash_policy import MODE_STEADY
        from risk.position_capacity import resolve_max_open_eff

        base = int(self.config.max_open_positions)
        risk = self.config.risk_config
        bias = self._market_bias_for_cash()
        try:
            size_mult = float(bias.get("size_mult", 1.0) or 1.0)
        except (TypeError, ValueError):
            size_mult = 1.0
        block_buys = bool(bias.get("block_buys"))
        regime = bias.get("regime")

        cash_mode = MODE_STEADY
        spendable_new = None
        pol = self._evaluate_cash_policy(equity)
        if pol is not None:
            cash_mode = pol.mode
            spendable_new = float(pol.spendable_new)
            # Prefer policy-linked fusion signals when cash policy is on
            if pol.enabled:
                size_mult = float(pol.size_mult)
                block_buys = bool(pol.block_buys)

        throttle_at = float(risk.get("drawdown_throttle_pct", 10.0) or 10.0)
        drawdown_active = float(self._equity_drawdown_pct()) >= throttle_at
        soft_n, toxic_n, prefer_n = self._open_book_memory_counts()
        if _fail_closed_guards_mode(self.config) == "deny" and bias.get("degraded"):
            prefer_n = 0
            r = str(regime or "").upper()
            if r in ("", "RISK_ON", "UNKNOWN"):
                regime = "NEUTRAL"

        # Inject avg_entry into capacity section from bot trade size (no hardcode)
        risk_for_cap = dict(risk) if isinstance(risk, dict) else {}
        pc = dict(risk_for_cap.get("position_capacity") or {})
        if pc.get("enabled") and not pc.get("avg_entry_usdt"):
            try:
                pc["avg_entry_usdt"] = float(self._base_usdt_cap() or 0)
            except Exception:
                pass
            risk_for_cap["position_capacity"] = pc

        return resolve_max_open_eff(
            base=base,
            risk_config=risk_for_cap,
            regime=regime,
            size_mult=size_mult,
            block_buys=block_buys,
            cash_mode=cash_mode,
            spendable_new=spendable_new,
            soft_block_open=soft_n,
            toxic_open=toxic_n,
            prefer_open=prefer_n,
            process_uptime_sec=self._process_uptime_sec(),
            full_slots=full_slots,
            drawdown_active=drawdown_active,
        )

    def _evaluate_cash_policy(self, equity: float | None = None):
        """Adaptive cash evaluation when enabled; None when legacy path."""
        from risk.cash_policy import evaluate_cash_policy, is_cash_policy_enabled

        risk = self.config.risk_config
        if not is_cash_policy_enabled(risk):
            return None
        eq = float(equity) if equity is not None else self._equity_for_sizing()
        cash = float(self._available_usdt(eq))
        bias = self._market_bias_for_cash()
        try:
            size_mult = float(bias.get("size_mult", 1.0) or 1.0)
        except (TypeError, ValueError):
            size_mult = 1.0
        block_buys = bool(bias.get("block_buys"))
        throttle_at = float(risk.get("drawdown_throttle_pct", 10.0) or 10.0)
        dd_pct = float(self._equity_drawdown_pct())
        drawdown_active = dd_pct >= throttle_at
        basis = self._cash_floor_basis_ref(eq, adaptive=True)
        return evaluate_cash_policy(
            cash_total=cash,
            basis_for_floor=basis,
            equity=eq,
            size_mult=size_mult,
            block_buys=block_buys,
            drawdown_active=drawdown_active,
            risk_config=risk,
        )

    def _cash_floor_abs(self) -> float:
        """Absolute min free cash. Adaptive floor when cash_policy.enabled."""
        pol = self._evaluate_cash_policy()
        if pol is not None and pol.enabled:
            return float(pol.floor_abs)
        risk = self.config.risk_config
        pct = float(risk.get("cash_floor_pct", 0) or 0)
        if pct <= 0:
            return 0.0
        ref = self._cash_floor_basis_ref(adaptive=False)
        return max(0.0, float(ref) * (pct / 100.0))

    def _cash_floor_blocked(self, *, is_dca: bool = False) -> "RiskDecision | None":
        """Block buys when spendable for that bucket is below min trade."""
        min_trade = float(self.config.risk_config.get("min_trade_usdt", 5.0))
        pol = self._evaluate_cash_policy()
        if pol is not None and pol.enabled:
            free = float(pol.spendable_dca if is_dca else pol.spendable_new)
            if free >= min_trade:
                return None
            cash = float(self._available_usdt())
            return RiskDecision(
                approved=False,
                message=(
                    f"Cash floor ({pol.mode}): free ${max(0.0, free):.2f} "
                    f"({'dca' if is_dca else 'new'}) "
                    f"(floor ${pol.floor_abs:.0f} / {pol.floor_pct_eff:.1f}%, "
                    f"cash ${cash:.2f})"
                ),
                code="cash_floor",
            )

        floor_abs = self._cash_floor_abs()
        if floor_abs <= 0:
            return None
        cash = self._available_usdt()
        free = cash - floor_abs
        if free >= min_trade:
            return None
        return RiskDecision(
            approved=False,
            message=(
                f"Cash floor: free ${max(0.0, free):.2f} "
                f"(floor ${floor_abs:.0f}, cash ${cash:.2f})"
            ),
            code="cash_floor",
        )

    def _spendable_usdt(self, equity: float, *, is_dca: bool) -> float:
        pol = self._evaluate_cash_policy(equity)
        if pol is not None and pol.enabled:
            return float(pol.spendable_dca if is_dca else pol.spendable_new)

        balance = self._available_usdt(equity)
        floor_abs = self._cash_floor_abs()
        # Absolute floor first (initial capital %)
        if floor_abs > 0:
            balance = max(0.0, balance - floor_abs)
        # Optional extra % reserve of *remaining* cash (legacy dca_reserve_pct)
        if not is_dca:
            reserve_pct = float(self.config.risk_config.get("dca_reserve_pct", 0) or 0)
            if reserve_pct > 0:
                balance = max(0.0, balance * (1.0 - reserve_pct / 100.0))
        return balance

    def _dry_run_reference_prices(self, reference_price: float = 0, symbol: str = None) -> dict:
        ref_prices = {}
        for pos in list_active_positions():
            sym = pos["symbol"] if "/" in pos["symbol"] else f"{pos['symbol']}/USDT"
            ref_prices[sym] = float(pos.get("average_entry", pos.get("entry_price", 0)) or 0)
        if reference_price > 0 and symbol:
            ref_prices[symbol] = reference_price
        return ref_prices

    def _log_equity_mtm_unavailable_once(self, reason: str) -> None:
        global _EQUITY_MTM_UNAVAILABLE_LOGGED
        if _EQUITY_MTM_UNAVAILABLE_LOGGED:
            return
        _EQUITY_MTM_UNAVAILABLE_LOGGED = True
        try:
            from logger import log

            log(
                f"portfolio MTM equity unavailable ({reason}); "
                "drawdown treated as unknown → size throttle",
                "WARNING",
            )
        except Exception:
            pass

    def _mark_to_market_equity(
        self, reference_price: float = 0, symbol: str = None
    ) -> float | None:
        """Cash + positions at live prices. None if any open lot lacks a quote."""
        try:
            from price_fetcher import get_prices_batch
            from strategies.positions import list_active_positions

            cash = float(self._available_usdt() or 0)
            active = list_active_positions()
            if not active:
                return cash
            symbols: list[str] = []
            for pos in active:
                raw_sym = pos.get("symbol") or ""
                symbols.append(raw_sym if "/" in raw_sym else f"{raw_sym}/USDT")
            live: dict[str, float] = {}
            if symbol and float(reference_price or 0) > 0:
                ref_sym = symbol if "/" in symbol else f"{symbol}/USDT"
                live[ref_sym] = float(reference_price)
            need = [s for s in dict.fromkeys(symbols) if s not in live]
            if need:
                batch = get_prices_batch(need) or {}
                for s, px in batch.items():
                    try:
                        val = float(px or 0)
                    except (TypeError, ValueError):
                        val = 0.0
                    if val > 0:
                        live[s] = val
            total = cash
            for pos in active:
                raw_sym = pos.get("symbol") or ""
                sym = raw_sym if "/" in raw_sym else f"{raw_sym}/USDT"
                px = float(live.get(sym) or 0)
                amount = float(pos.get("amount") or 0)
                if amount <= 1e-12:
                    continue
                if px <= 0:
                    return None
                try:
                    from strategies.short_math import is_short, snapshot

                    if is_short(pos):
                        snap = snapshot(pos, px)
                        from storage.order_ledger_v2 import mark_pnl_or_exclude

                        total += float(snap.get("margin") or 0) + mark_pnl_or_exclude(
                            snap.get("pnl"), where="risk_equity_short",
                        )
                        continue
                except Exception:
                    pass
                total += amount * px
            return total
        except Exception:
            return None

    def _portfolio_equity(self, reference_price: float = 0, symbol: str = None) -> float | None:
        """Mark-to-market NAV. None when live prices and a fresh snapshot are missing."""
        try:
            from services.portfolio_nav_history import latest_fresh_nav

            interval = float(getattr(self.config, "update_interval", 600) or 600)
            snap = latest_fresh_nav(max_age_sec=2.0 * interval)
            if snap is not None:
                return float(snap)
        except Exception:
            pass
        mtm = self._mark_to_market_equity(reference_price, symbol)
        if mtm is None:
            self._log_equity_mtm_unavailable_once("live prices/NAV missing")
        return mtm

    def _equity_for_sizing(self, reference_price: float = 0, symbol: str = None) -> float:
        """Sizing/concentration fallback — never pretends cost-basis is MTM."""
        mtm = self._portfolio_equity(reference_price, symbol)
        if mtm is not None:
            return float(mtm)
        return float(self._initial_capital() or 0)

    def _equity_drawdown_pct(self, reference_price: float = 0, symbol: str = None) -> float:
        risk = self.config.risk_config if hasattr(self.config, "risk_config") else {}
        throttle_at = float((risk or {}).get("drawdown_throttle_pct", 10.0) or 10.0)
        if is_live_dry_run(self.config.raw):
            history = load_live_trade_history()
            initial = simulated_balance_usdt(self.config.raw)
            equity = self._portfolio_equity(reference_price, symbol)
        else:
            history = load_trade_history()
            initial = self._initial_capital()
            equity = self._portfolio_equity(reference_price, symbol)
        if equity is None:
            # Unknown drawdown → arm the size throttle, never report 1.0.
            return float(throttle_at)
        try:
            peak = float(history.get("peak_equity", initial))
        except (TypeError, ValueError):
            peak = float(initial or 0)
        peak = max(peak, equity, initial)
        if peak <= 0:
            return 0.0
        return max(0.0, (peak - equity) / peak * 100.0)

    def _most_restrictive_coin_size_bias(self) -> float:
        """Min of the configured size-bias range; 0.5 if not derivable."""
        try:
            raw = self.config.raw if hasattr(self.config, "raw") else {}
            gl = ((raw or {}).get("memory") or {}).get("gross_loss") or {}
            cap = gl.get("size_bias_cap")
            if cap is not None:
                return float(cap)
        except Exception:
            pass
        return 0.5

    def _dynamic_size(
        self,
        base_usdt: float,
        order: TradeOrder,
        timeframe: str,
        source: str,
        trust_score: float,
        confidence: float,
        indicators: dict,
    ) -> tuple[float, dict]:
        aggression = self.config.aggression_config
        risk = self.config.risk_config

        trust = trust_score if trust_score is not None else 70.0
        conf = confidence if confidence is not None else 50.0

        trust_delta = (trust - 70.0) / 10.0
        trust_factor = 1.0 + trust_delta * 0.1
        if source == "x" and trust < aggression.get("min_trust_for_live", 70):
            trust_factor *= 0.85

        conf_factor = 0.8 + (conf / 100.0) * 0.4

        atr_pct = float(indicators.get("atr_pct", risk.get("atr_reference_pct", 3.0)))
        ref_atr = float(risk.get("atr_reference_pct", 3.0))
        atr_factor = min(1.5, max(0.5, ref_atr / max(atr_pct, 0.5)))

        drawdown_pct = self._equity_drawdown_pct()
        throttle_at = float(risk.get("drawdown_throttle_pct", 10.0))
        dd_mult = float(risk.get("drawdown_size_multiplier", 0.5)) if drawdown_pct >= throttle_at else 1.0

        global_mult = 1.0
        global_regime = None
        global_source = None
        from services.market_policy_fusion import get_global_market_bias

        try:
            bias = get_global_market_bias(
                self.config.raw if hasattr(self.config, "raw") else None
            )
            if bias.get("apply_size_mult") and bias.get("active"):
                global_mult = max(0.0, min(1.5, float(bias.get("size_mult") or 1.0)))
                global_regime = bias.get("regime")
                global_source = bias.get("source")
                if global_mult < 0.999:
                    try:
                        from services.market_context_observability import note_size_cut

                        note_size_cut(mult=global_mult, regime=global_regime)
                    except Exception:
                        pass
            if _fail_closed_guards_mode(self.config) == "deny" and bias.get("degraded"):
                global_mult = min(1.0, float(global_mult))
                global_regime = "UNKNOWN"
                global_source = bias.get("source") or global_source
        except Exception as e:
            dec = self._guard_failed("global_market_bias", e, order)
            if dec is not None:
                global_mult = 0.0
                global_regime = "UNKNOWN"

        coin_bias = 1.0
        coin_entry = "neutral"
        coin_rationale = ""
        social_summary = ""
        from intelligence.memory.cache import get_coin_profile, get_size_bias

        try:
            coin_bias = float(
                get_size_bias(
                    order.symbol,
                    config=self.config.raw if hasattr(self.config, "raw") else None,
                )
            )
            prof = get_coin_profile(
                order.symbol,
                config=self.config.raw if hasattr(self.config, "raw") else None,
            )
            if prof:
                coin_entry = prof.entry_bias or "neutral"
                coin_rationale = (prof.rationale or "")[:120]
                feats = prof.features or {}
                social_summary = str((feats.get("social_summary") or ""))[:80]
        except Exception as e:
            dec = self._guard_failed("coin_memory_size_bias", e, order)
            if dec is not None:
                coin_bias = self._most_restrictive_coin_size_bias()
            else:
                coin_bias = 1.0
            social_summary = ""

        calendar_mult = 1.0
        session_mult = 1.0
        pm_mult = 1.0
        calendar_risk = ""
        session_risk = ""
        pm_risk = ""
        try:
            from intelligence.macro.snapshot import get_risk_multipliers

            mm = get_risk_multipliers(
                self.config.raw if hasattr(self.config, "raw") else None
            )
            calendar_mult = float(mm.get("calendar_mult") or 1.0)
            session_mult = float(mm.get("session_mult") or 1.0)
            pm_mult = float(mm.get("pm_mult") or 1.0)
            calendar_risk = str(mm.get("calendar_risk") or "")[:100]
            session_risk = str(mm.get("session_risk") or "")[:80]
            pm_risk = str(mm.get("pm_risk") or "")[:80]
        except Exception:
            pass

        total = (
            trust_factor
            * conf_factor
            * atr_factor
            * dd_mult
            * global_mult
            * coin_bias
            * calendar_mult
            * session_mult
            * pm_mult
        )
        max_mult = float(aggression.get("max_position_multiplier", 2.0))
        min_mult = float(risk.get("min_size_multiplier", 0.25))
        md_boost = 1.0
        # Moderate deploy: lift size when not CRASH; extra when cash-rich
        try:
            from risk.moderate_deploy import (
                effective_max_total_multiplier,
                size_boost_for_regime,
            )

            raw_cfg = self.config.raw if hasattr(self.config, "raw") else None
            cash_pct = None
            try:
                eq = float(self._portfolio_equity(order.price, order.symbol) or 0)
                cash = float(self._available_usdt(eq) or 0)
                if eq > 0:
                    cash_pct = 100.0 * cash / eq
            except Exception:
                cash_pct = None
            md_boost = size_boost_for_regime(
                raw_cfg,
                global_regime,
                is_dca=False,
                cash_pct=cash_pct,
            )
            if md_boost > 1.0 and global_mult > 0:
                total *= md_boost
                max_mult = effective_max_total_multiplier(
                    raw_cfg, base_max=max_mult, boost=md_boost
                )
        except Exception:
            md_boost = 1.0
        # Allow CRASH/warmup (0) to zero out size; otherwise keep floor.
        if global_mult <= 0:
            total = 0.0
        else:
            total = max(min_mult, min(max_mult, total))

        # Allocator de-risking: never a boost, applied once after other multipliers.
        raw_exp = getattr(order, "exposure_multiplier", None)
        try:
            exp_mult = 1.0 if raw_exp is None else float(raw_exp)
        except (TypeError, ValueError):
            exp_mult = 1.0
        exp_mult = max(0.0, min(1.0, exp_mult))
        if exp_mult < 1.0:
            total *= exp_mult
            try:
                from logger import log

                log(
                    f"exposure_multiplier={exp_mult:.2f} applied {order.symbol} "
                    f"total={total:.3f}",
                    "INFO",
                )
            except Exception:
                pass

        factors = {
            "trust_factor": round(trust_factor, 3),
            "conf_factor": round(conf_factor, 3),
            "atr_factor": round(atr_factor, 3),
            "drawdown_pct": round(drawdown_pct, 2),
            "drawdown_multiplier": dd_mult,
            "global_size_mult": round(global_mult, 3),
            "global_regime": global_regime,
            "global_bias_source": global_source,
            "coin_size_bias": round(coin_bias, 3),
            "coin_entry_bias": coin_entry,
            "coin_memory": coin_rationale,
            "coin_social": social_summary,
            "calendar_mult": round(calendar_mult, 3),
            "session_mult": round(session_mult, 3),
            "pm_mult": round(pm_mult, 3),
            "calendar_risk": calendar_risk,
            "session_risk": session_risk,
            "pm_risk": pm_risk,
            "moderate_deploy_mult": round(md_boost, 3),
            "exposure_multiplier": round(exp_mult, 3),
            "total_multiplier": round(total, 3),
        }
        return base_usdt * total, factors

    def _venue_pair_buy_block(self, order: TradeOrder) -> RiskDecision | None:
        """R4 missing minimum data and R7 pair rule. Stops are not buys."""
        from execution.gate_adapter import ensure_venue_limits_loaded, venue_limits_for

        if str(order.type or "").upper() != "BUY":
            return None
        ensure_venue_limits_loaded(self.config)
        limits = venue_limits_for(order.symbol)
        if not limits.get("known") or not limits.get("min_cost") or not limits.get("amount_step"):
            return RiskDecision(
                approved=False,
                message=f"Venue minimum unknown for {order.symbol}",
                code="venue_min_missing",
                size_multiplier=0.0,
            )
        ticket = float(self._base_usdt_cap() or 0)
        if ticket <= 0:
            return RiskDecision(
                approved=False,
                message="Configured ticket is not positive",
                code="venue_pair_blocked",
                size_multiplier=0.0,
            )
        min_cost = float(limits["min_cost"])
        max_min_pct = self._cfg_number("venue_min_max_pct_of_ticket", 0.20)
        step_pct = self._cfg_number("venue_step_max_pct_of_ticket", 0.02)
        if min_cost > max_min_pct * ticket:
            self._log_venue_once(
                order.symbol,
                "venue_pair_blocked",
                f"min={min_cost} pct={max_min_pct} ticket={ticket}",
            )
            return RiskDecision(
                approved=False,
                message=(
                    f"Pair minimum {min_cost:.4g} is above "
                    f"{max_min_pct:.0%} of the ticket"
                ),
                code="venue_pair_blocked",
                size_multiplier=0.0,
            )
        step = float(limits["amount_step"])
        price = float(order.price or 0)
        if price <= 0 or step * price > step_pct * ticket:
            self._log_venue_once(
                order.symbol,
                "venue_pair_blocked",
                f"step={step} price={price} pct={step_pct}",
            )
            return RiskDecision(
                approved=False,
                message="Amount step is above the configured share of the ticket",
                code="venue_pair_blocked",
                size_multiplier=0.0,
            )
        return None

    def _log_venue_once(self, symbol: str, code: str, extra: str) -> None:
        from core.tenant_context import resolve_tenant_id
        from logger import log

        seen = getattr(self, "_venue_pair_logged", None)
        if seen is None:
            seen = set()
            self._venue_pair_logged = seen
        key = (resolve_tenant_id(), symbol, code)
        if key in seen:
            return
        seen.add(key)
        log(f"{code} tenant={key[0]} symbol={symbol} {extra}", "WARNING")

    def _effective_stop_loss_pct(self, symbol: str, timeframe: str, position: dict | None = None) -> float:
        """First stop the exit path fires, in percent.

        Same resolver as the stop path (``effective_stop_loss_thresholds``):
        profile ``partial_stop_pct`` when the params carry it, otherwise the
        configured partial ratio, and DCA widening on the full stop. A merge
        is a new cycle, so remainder DCA rounds do not widen this stop.
        """
        from strategies.dca import effective_stop_loss_thresholds

        params: dict = {}
        try:
            params = dict(self.config.strategy_params(symbol, timeframe) or {})
        except Exception:
            params = {}
        try:
            profile = self.config.volatile_altcoin_config or {}
        except Exception:
            profile = {}
        if isinstance(profile, dict):
            if params.get("partial_stop_pct") is None and profile.get("partial_stop_pct") is not None:
                params["partial_stop_pct"] = profile["partial_stop_pct"]
            if "dca" not in params and isinstance(profile.get("dca"), dict):
                params["dca"] = profile["dca"]
        raw = params.get("stop_loss_pct")
        if raw is None:
            raw = self.config.stop_loss_pct
        try:
            base = float(raw or 0)
        except (TypeError, ValueError):
            base = 0.0
        fresh = {
            "dca_rounds": 0,
            "dca_recovery_rounds": 0,
            "last_dca_at": None,
            "last_dca_recovery_at": None,
        }
        full, partial, in_grace = effective_stop_loss_thresholds(fresh, params, base)
        if in_grace or partial is None:
            return float(full)
        return min(float(full), float(partial))

    def _stop_gap_buffer_pct(self) -> float | None:
        """Percent gap buffer. None when the key is absent (fail closed, no default)."""
        from core.simulated_trading import is_dry_run_enhanced

        raw = self.config.raw if hasattr(self.config, "raw") else {}
        if is_dry_run_enhanced(raw):
            defaults = self.config.dry_run_defaults or {}
            if "stop_gap_buffer_pct" in defaults and defaults.get("stop_gap_buffer_pct") is not None:
                return float(defaults["stop_gap_buffer_pct"])
        risk = self.config.risk_config or {}
        if "stop_gap_buffer_pct" in risk and risk.get("stop_gap_buffer_pct") is not None:
            return float(risk["stop_gap_buffer_pct"])
        return None

    def _log_merge_once(self, order: TradeOrder, timeframe: str, loss: float, bound) -> None:
        from core.tenant_context import resolve_tenant_id
        from logger import log
        from strategies.positions import VENUE_MIN_MERGE_LOSS_EXCEEDED, get_position

        pos = get_position(order.symbol, timeframe)
        if pos.get("venue_min_merge_logged"):
            return
        log(
            f"{VENUE_MIN_MERGE_LOSS_EXCEEDED} tenant={resolve_tenant_id()} "
            f"symbol={order.symbol} loss={loss} bound={bound}",
            "WARNING",
        )
        pos["venue_min_merge_logged"] = True

    def _venue_min_merge(self, order: TradeOrder, timeframe: str):
        """Plan a below-min merge. The loss bound is applied after final sizing.

        Returns (reject, plan). plan carries the reduced-buy cap and the
        remainder cost and loss. A missing buffer, or a stop that is not
        above the buffer, rejects here and a merge never happens.
        """
        from strategies.positions import (
            VENUE_MIN_MERGE_LOSS_EXCEEDED,
            get_position,
            is_below_venue_minimum,
        )

        if str(order.type or "").upper() != "BUY":
            return None, None
        pos = get_position(order.symbol, timeframe)
        if not is_below_venue_minimum(pos):
            return None, None
        price = float(order.price or 0)
        if price <= 0:
            self._log_merge_once(order, timeframe, 0.0, None)
            return (
                RiskDecision(
                    approved=False,
                    message="Mark missing; below-minimum lot is unchanged",
                    code=VENUE_MIN_MERGE_LOSS_EXCEEDED,
                    size_multiplier=0.0,
                    details={"reason": "mark_missing", "loss": None, "bound": None},
                ),
                None,
            )
        amount = float(pos.get("amount") or 0)
        entry = float(pos.get("average_entry") or 0)
        cost = amount * entry
        loss = cost - amount * price
        buffer = self._stop_gap_buffer_pct()
        if buffer is None:
            self._log_merge_once(order, timeframe, loss, None)
            return (
                RiskDecision(
                    approved=False,
                    message="stop_gap_buffer_pct missing; merge refused",
                    code=VENUE_MIN_MERGE_LOSS_EXCEEDED,
                    size_multiplier=0.0,
                    details={"reason": "missing_key", "loss": loss, "bound": None},
                ),
                None,
            )
        stop_pct = self._effective_stop_loss_pct(order.symbol, timeframe, pos)
        if stop_pct <= float(buffer):
            self._log_merge_once(order, timeframe, loss, 0.0)
            return (
                RiskDecision(
                    approved=False,
                    message=(
                        "stop_loss_pct is not above stop_gap_buffer_pct; "
                        "merge refused"
                    ),
                    code=VENUE_MIN_MERGE_LOSS_EXCEEDED,
                    size_multiplier=0.0,
                    details={
                        "reason": "stop_not_above_buffer",
                        "loss": loss,
                        "bound": 0.0,
                        "stop_pct": stop_pct,
                        "buffer_pct": float(buffer),
                    },
                ),
                None,
            )
        ticket = float(self._base_usdt_cap() or 0)
        buy_usdt = ticket - cost
        # Pre-multiplier basis is the looser one (a later size_mult can only
        # shrink it). A loss that already exceeds that basis is the bound
        # reject (T9b/T9c). The size floor runs only when this bound holds,
        # and again after final sizing.
        plan = {
            "cap": max(buy_usdt, 0.0),
            "cost": cost,
            "loss": loss,
            "stop_pct": stop_pct,
            "buffer_pct": float(buffer),
        }
        bound_reject = self._venue_merge_loss_reject(
            order, timeframe, plan, max(buy_usdt, 0.0)
        )
        if bound_reject is not None:
            return bound_reject, None
        min_trade = float(self.config.risk_config.get("min_trade_usdt", 5.0))
        if buy_usdt < min_trade:
            return (
                RiskDecision(
                    approved=False,
                    message=f"Adjusted size ${buy_usdt:.2f} below minimum (${min_trade:.0f})",
                    code="size_too_small",
                    size_multiplier=0.0,
                ),
                None,
            )
        return None, plan

    def _venue_merge_loss_reject(
        self, order: TradeOrder, timeframe: str, plan: dict, final_buy: float
    ) -> RiskDecision | None:
        """Reject when the remainder loss exceeds the bound on the sized basis."""
        from execution.gate_adapter import round_usdt_to_price_precision, venue_limits_for
        from strategies.positions import VENUE_MIN_MERGE_LOSS_EXCEEDED

        loss = float(plan["loss"])
        basis = float(plan["cost"]) + float(final_buy)
        bound = (float(plan["stop_pct"]) - float(plan["buffer_pct"])) / 100.0 * basis
        limits = venue_limits_for(order.symbol)
        rounded_loss = round_usdt_to_price_precision(loss, limits)
        rounded_bound = round_usdt_to_price_precision(bound, limits)
        if rounded_loss <= rounded_bound:
            return None
        self._log_merge_once(order, timeframe, loss, bound)
        return RiskDecision(
            approved=False,
            message=(
                f"Below-minimum merge loss {loss:.6g} exceeds bound {bound:.6g}"
            ),
            code=VENUE_MIN_MERGE_LOSS_EXCEEDED,
            size_multiplier=0.0,
            details={
                "reason": "loss_bound",
                "loss": loss,
                "bound": bound,
                "basis": basis,
                "final_buy": float(final_buy),
                "stop_pct": float(plan["stop_pct"]),
                "buffer_pct": float(plan["buffer_pct"]),
            },
        )

    def _venue_partial_guard(self, order: TradeOrder, timeframe: str):
        """Skip a partial below the pair minimum. Sell the whole lot if the remainder would be.

        Missing pair limits never block a sell. Load them on the same adapter
        path as a buy; if they are still unknown, keep today's partial path.
        """
        from execution import gate_adapter
        from execution.gate_adapter import ensure_venue_limits_loaded, venue_limits_for
        from logger import log
        from strategies.positions import get_position

        if not _is_partial_sell(order.signal):
            return None, order
        ensure_venue_limits_loaded(self.config)
        limits = venue_limits_for(order.symbol)
        if not limits.get("known") or not limits.get("min_cost"):
            # The loader already warned on a failed load. Log once only when
            # the load succeeded and this pair is still unknown.
            if not gate_adapter._VENUE_WARNED:
                self._log_venue_once(
                    order.symbol,
                    "venue_limits_unavailable",
                    "partial continues",
                )
            return None, order
        pos = get_position(order.symbol, timeframe)
        held = float(pos.get("amount") or 0)
        price = float(order.price or 0)
        notional = float(order.amount or 0) * price
        remainder = held * price - notional
        min_cost = float(limits["min_cost"])
        if notional < min_cost:
            log(
                f"partial skipped: value {notional:.4g} below venue minimum "
                f"{min_cost:.4g} symbol={order.symbol}",
                "WARNING",
            )
            return (
                RiskDecision(
                    approved=False,
                    message=(
                        f"Partial skipped: value {notional:.2f} below venue "
                        f"minimum {min_cost:.2f}"
                    ),
                    code="venue_min_partial_skipped",
                ),
                order,
            )
        if remainder < min_cost and held > 0:
            return None, TradeOrder(
                type=order.type,
                symbol=order.symbol,
                price=order.price,
                amount=held,
                signal="SELL_FULL",
                source=order.source,
                order_id=order.order_id,
                timestamp=order.timestamp,
                ctx_oracle_state=getattr(order, "ctx_oracle_state", None),
                ctx_coin_regime=getattr(order, "ctx_coin_regime", None),
                ctx_volume_rel=getattr(order, "ctx_volume_rel", None),
                ctx_volume_window_days=getattr(order, "ctx_volume_window_days", None),
            )
        return None, order

    def _partial_sell_limits(self, symbol: str, timeframe: str) -> dict:
        params = self.config.strategy_params(symbol, timeframe)
        cmc_cfg = self.config.cmc_config
        risk_cfg = self.config.risk_config
        return {
            "min_position_usdt": float(
                params.get("min_position_usdt_for_partial_sell")
                or params.get("min_position_usdt_for_social_sell")
                or risk_cfg.get("min_position_usdt_for_partial_sell")
                or cmc_cfg.get("min_position_usdt_for_social_sell", 25)
            ),
            "min_notional_usdt": float(
                params.get("min_sell_notional_usdt")
                or risk_cfg.get("min_sell_notional_usdt")
                or cmc_cfg.get("min_sell_notional_usdt", 15)
            ),
            "max_sold_percent": _resolve_partial_sell_cap(params, risk_cfg, symbol),  # P723B-CAP
            "dust_sweep_max_position_usdt": float(
                risk_cfg.get("dust_sweep_max_position_usdt", 15)
            ),
            "dust_sweep_sold_percent_min": float(
                risk_cfg.get("dust_sweep_sold_percent_min", 0.70)
            ),
            "dust_sweep_min_remainder_usdt": float(
                risk_cfg.get("dust_sweep_min_remainder_usdt", 10)
            ),
        }

    def _cap_sell_to_open_lot(self, order: TradeOrder, timeframe: str):
        """Re-read the order's (symbol, timeframe) lot and cap the sell.

        Returns the order, or a rejecting RiskDecision when the lot is closed.
        """
        from core.tenant_context import resolve_tenant_id
        from logger import log

        pos = get_position(order.symbol, timeframe) or {}
        try:
            held = float(pos.get("amount") or 0)
        except (TypeError, ValueError):
            held = 0.0
        if held <= 0:
            return RiskDecision(
                approved=False,
                message=f"sell_lot_closed {order.symbol} {timeframe}",
                code="sell_lot_closed",
                order=order,
            )
        try:
            asked = float(order.amount or 0)
        except (TypeError, ValueError):
            asked = 0.0
        if asked > held:
            order.amount = held
            log(
                f"sell_amount_capped symbol={order.symbol} timeframe={timeframe} "
                f"tenant={resolve_tenant_id()} old_amount={asked} new_amount={held}",
                "WARNING",
            )
        return order

    def _escalate_rejected_hard_partial(
        self, order: TradeOrder, timeframe: str, source: str, reason: str,
        code: str | None = None,
    ):
        """H2: a marked hard partial the guard rejects for size becomes one SELL_STOP_FULL.

        The lot is ``get_position(symbol, timeframe)`` (symbol + timeframe, not a 1h default).
        Escalation requires an explicit hard-stop origin (``exit_ws`` or ``long_hard_stop``),
        a size code, held > 0, and price strictly below that lot's average_entry.
        A missing or zero entry fails closed. Returns the escalated order, or None
        (the rejection stands).
        """
        if str(order.signal or "") != "SELL_STOP_PARTIAL":
            return None
        if not is_hard_stop_order(order, source):  # H2-HARD (signal + exit_source stop_loss + not manual)
            return None
        origin = explicit_hard_stop_origin(order, source)
        if origin is None:  # M-ESC fail-closed: no explicit hard-stop origin marker -> the rejection stands
            return None
        if code not in PARTIAL_SELL_SIZE_CODES:  # H2-CODE allowlist (sold_cap / unknown keep blocking)
            return None
        pos = get_position(order.symbol, timeframe) or {}
        try:
            held = float(pos.get("amount") or 0)
            entry = float(pos.get("average_entry") or 0)
        except (TypeError, ValueError):
            return None
        if held <= 0:
            return None
        if not (entry > 0 and float(order.price or 0) < entry):  # H2-BELOW-ENTRY sanity barrier
            return None
        order.amount = held  # H2-AMOUNT
        order.signal = "SELL_STOP_FULL"
        order._h2_escalated = True  # M-ESC (daily-cap log escalated=1)
        from core.tenant_context import resolve_tenant_id
        from logger import log

        log(
            "partial_stop_upgraded_to_full "
            f"symbol={order.symbol} timeframe={timeframe} "
            f"tenant={resolve_tenant_id()} "
            f"source={getattr(order, 'source', None) or source} amount={held:.10g} "
            f"reason=partial_sell_guard block={code} origin={origin}",  # H2-LOG M-ESC
            "ERROR" if code == PARTIAL_SELL_BLOCK_CAP_MISSING else "INFO",  # P723B-E1-LEVEL
        )
        return order

    def _upgrade_partial_stop_below_gate_min(
        self, order: TradeOrder, timeframe: str
    ) -> TradeOrder:
        """Sell the whole lot when a hard partial would leave a sub-minimum remainder.

        Same remainder rule as #662 R5: the pair minimum comes from the loaded
        Gate market. Unknown minimums do not upgrade and are not treated as zero.
        """
        if str(order.signal or "") != "SELL_STOP_PARTIAL":
            return order
        if str(getattr(order, "exit_source", "") or "") != "stop_loss":
            return order
        try:
            price = float(order.price or 0)
            sell_amt = float(order.amount or 0)
        except (TypeError, ValueError):
            return order
        if price <= 0 or sell_amt <= 0:
            return order
        from execution.gate_adapter import gate_pair_minimums

        mins = gate_pair_minimums(order.symbol)
        if mins is None:
            return order
        min_amount, min_cost = mins
        pos = get_position(order.symbol, timeframe) or {}
        try:
            held = float(pos.get("amount") or 0)
        except (TypeError, ValueError):
            return order
        if held <= sell_amt:
            return order
        remainder = held - sell_amt
        below_amount = min_amount > 0 and remainder < min_amount
        below_cost = min_cost > 0 and remainder * price < min_cost
        if not (below_amount or below_cost):
            return order
        order.amount = held
        order.signal = "SELL_STOP_FULL"
        from core.tenant_context import resolve_tenant_id
        from logger import log

        log(
            "partial_stop_upgraded_to_full "
            f"symbol={order.symbol} timeframe={timeframe} "
            f"tenant={resolve_tenant_id()} "
            f"reason=remainder_below_gate_minimum",
            "INFO",
        )
        return order

    def _log_hard_stop_daily_bypass(
        self,
        order: TradeOrder,
        source: str,
        daily_sells: int,
        max_daily_sells: int,
    ) -> None:
        from core.tenant_context import resolve_tenant_id
        from logger import log

        log(
            "daily_sells_limit_bypassed_hard_stop "
            f"symbol={order.symbol} tenant={resolve_tenant_id()} "
            f"signal={getattr(order, 'signal', '')} "
            f"source={getattr(order, 'source', None) or source} "
            f"exit_source={getattr(order, 'exit_source', '')} "
            f"count={daily_sells} limit={max_daily_sells} "
            f"origin={getattr(order, 'hard_stop_origin', '')} "
            f"escalated={1 if getattr(order, '_h2_escalated', False) else 0}",  # H2 M-DAILY
            "INFO",
        )

    def _fill_sell_amount_from_open_lot(
        self, order: TradeOrder, timeframe: str
    ) -> TradeOrder:
        """
        If sell amount is missing/zero, size from any open lot for the symbol.

        Guards against analysis TF ≠ position TF (e.g. 4h signal vs 1h volatile lot).
        """
        found = find_open_position_for_symbol(
            order.symbol, preferred_timeframe=timeframe
        )
        if not found:
            return order
        pos_tf, pos = found
        try:
            from strategies.short_math import is_short as _is_short

            if _is_short(pos) and float(pos.get("amount") or 0) > 1e-12:
                return order
        except Exception:
            return order
        held = float(pos.get("amount", 0) or 0)
        if held <= 0:
            return order
        try:
            fraction = sell_fraction_for_signal(
                order.signal or "SELL_FULL",
                order.symbol,
                pos_tf,
                float(order.price or 0),
                None,
            )
        except Exception as e:
            # Never full-sell on a sizing error — skip the partial instead.
            self._guard_failed("sell_fraction", e, order)
            fraction = 0.0
        amount = held * float(fraction or 0)
        if amount <= 0:
            return order
        return TradeOrder(
            type=order.type,
            symbol=order.symbol,
            price=order.price,
            amount=amount,
            usdt_amount=order.usdt_amount,
            signal=order.signal,
            source=order.source,
            order_id=order.order_id,
            timestamp=order.timestamp,
            exit_source=getattr(order, "exit_source", None),
            exit_rationale=getattr(order, "exit_rationale", None),
            idempotency_key=getattr(order, "idempotency_key", None),
            ctx_oracle_state=getattr(order, "ctx_oracle_state", None),
            ctx_coin_regime=getattr(order, "ctx_coin_regime", None),
            ctx_volume_rel=getattr(order, "ctx_volume_rel", None),
            ctx_volume_window_days=getattr(order, "ctx_volume_window_days", None),
        )

    def _resolve_sell_order(self, order: TradeOrder, timeframe: str, source: str) -> TradeOrder:
        """Upgrade partial sells to full close when the lot is dust or nearly exited."""
        if source == "manual" or _is_emergency_sell(order.signal):
            return order
        if not _is_partial_sell(order.signal) or order.price <= 0:
            return order

        pos = get_position(order.symbol, timeframe)
        amount = float(pos.get("amount", 0))
        if amount <= 0:
            found = find_open_position_for_symbol(
                order.symbol, preferred_timeframe=timeframe
            )
            if found:
                pos = found[1]
                amount = float(pos.get("amount", 0) or 0)
        if amount <= 0:
            return order

        pos_value = amount * order.price
        sold_pct = float(pos.get("sold_percent", 0))
        limits = self._partial_sell_limits(order.symbol, timeframe)
        notional = float(order.amount) * order.price
        remainder = pos_value - notional

        dust_min = limits["dust_sweep_min_remainder_usdt"]

        sweep = (
            pos_value <= limits["dust_sweep_max_position_usdt"]
            or (
                sold_pct >= limits["dust_sweep_sold_percent_min"]
                and pos_value <= limits["min_position_usdt"]
            )
            or (0 < remainder < dust_min)
        )
        if not sweep:
            return order

        from core.models import MarketContext
        from strategies.registry import resolve_strategy_params
        from strategies.sell_rotation_policy import can_rotation_evict, rotation_config

        sparams = None
        try:
            sparams = resolve_strategy_params(
                {"symbol": order.symbol, "timeframe": timeframe},
                has_position=True,
                frozen_tier=pos.get("strategy_tier"),
            )
        except Exception:
            pass
        cfg = rotation_config(self.config.raw, sparams)
        entry = float(pos.get("average_entry", 0) or order.price)
        market = MarketContext(
            symbol=order.symbol,
            timeframe=timeframe,
            current_price=order.price,
            has_position=True,
            average_entry=entry,
        )
        if not can_rotation_evict(market, pos, cfg):
            return order

        return TradeOrder(
            type=order.type,
            symbol=order.symbol,
            price=order.price,
            amount=amount,
            signal="SELL_FULL",
            source=order.source,
            order_id=order.order_id,
            timestamp=order.timestamp,
            ctx_oracle_state=getattr(order, "ctx_oracle_state", None),
            ctx_coin_regime=getattr(order, "ctx_coin_regime", None),
            ctx_volume_rel=getattr(order, "ctx_volume_rel", None),
            ctx_volume_window_days=getattr(order, "ctx_volume_window_days", None),
        )

    def _partial_sell_blocked(self, order: TradeOrder, timeframe: str, source: str) -> tuple[bool, str]:
        signal = str(order.signal or "")
        if source == "manual":
            return False, ""
        # The full hard stop skips this guard. A hard partial does not:
        # ``_is_emergency_sell`` matches it via the STOP substring, and that
        # match must not exempt SELL_STOP_PARTIAL (#663 B3, spec R11).
        if signal == "SELL_STOP_FULL":
            return False, ""
        if _is_emergency_sell(signal) and signal != "SELL_STOP_PARTIAL":
            return False, ""
        if order.price <= 0:
            return False, ""
        if signal != "SELL_STOP_PARTIAL" and not _is_partial_sell(signal):
            return False, ""

        params = self.config.strategy_params(order.symbol, timeframe)
        try:
            from strategies.registry import resolve_strategy_params

            pos = get_position(order.symbol, timeframe)
            params = resolve_strategy_params(
                {"symbol": order.symbol, "timeframe": timeframe},
                has_position=is_open_position(pos),
                frozen_tier=pos.get("strategy_tier"),
            )
        except Exception:
            pass
        try:
            from strategies.exit_ladder import ladder_enabled

            if ladder_enabled(params):
                return False, ""
        except Exception:
            pass

        limits = self._partial_sell_limits(order.symbol, timeframe)
        pos = get_position(order.symbol, timeframe)
        pos_value = float(pos.get("amount", 0)) * order.price
        notional = float(order.amount) * order.price
        sold_pct = float(pos.get("sold_percent", 0) or 0)
        # #709: lower a stale sold_percent for this check only. Not written back.
        sold_pct = _lower_sold_percent_for_check(pos, sold_pct)
        # No usable peak: stored sold_percent is not a basis for this decision.
        # Only the no-peak retry sets the flag. The lot is not written.
        if _ignoring_unreliable_sold_percent() and peak_amount_usable(pos) is None:
            sold_pct = 0.0

        if limits["max_sold_percent"] is None:  # P723B-MISSING block; only a hard partial escalates (allowlist)
            try:
                order._partial_sell_block_code = PARTIAL_SELL_BLOCK_CAP_MISSING
            except Exception:
                pass
            return True, (
                f"Partial sell blocked: {PARTIAL_SELL_CAP_KEY} not configured "
                "(non-hard partial blocked; hard stop partial escalates to SELL_STOP_FULL)"
            )
        code = None
        message = ""
        if sold_pct >= limits["max_sold_percent"]:  # P723A-PRIO-1 sold cap wins (Kay)
            code = PARTIAL_SELL_BLOCK_SOLD_CAP
            message = (
                f"Partial sell blocked: already sold {sold_pct * 100:.0f}% of position "
                f"(max {limits['max_sold_percent'] * 100:.0f}%)"
            )
        elif pos_value < limits["min_position_usdt"]:  # P723A-PRIO-2
            code = PARTIAL_SELL_BLOCK_MIN_POSITION
            message = (
                f"Partial sell blocked: position ${pos_value:.2f} "
                f"below minimum ${limits['min_position_usdt']:.0f} "
                f"(use full close or manual sell)"
            )
        elif notional < limits["min_notional_usdt"]:  # P723A-PRIO-3
            code = PARTIAL_SELL_BLOCK_MIN_NOTIONAL
            message = (
                f"Partial sell blocked: notional ${notional:.2f} "
                f"below minimum ${limits['min_notional_usdt']:.0f}"
            )
        try:
            order._partial_sell_block_code = code  # P723A-RECORD
        except Exception:
            pass
        if code is None:
            return False, ""
        return True, message

    def _social_sell_blocked(self, order: TradeOrder, timeframe: str, source: str) -> tuple[bool, str]:
        """Backward-compatible alias for tests and callers."""
        return self._partial_sell_blocked(order, timeframe, source)

    def _evaluate_short_or_cover(
        self,
        order: TradeOrder,
        timeframe: str,
        source: str = "auto",
    ) -> RiskDecision:
        import math

        from core.simulated_trading import is_real_live_trading
        from strategies.short_math import clamp_leverage, is_short, margin_usdt
        from strategies.short_policy import resolve_short_params, shorts_allow_live, shorts_enabled

        raw = self.config.raw if hasattr(self.config, "raw") else {}
        if (
            is_real_live_trading(raw)
            and not shorts_allow_live(raw)
            and _execution_places_real_orders(raw)
        ):
            return RiskDecision(
                approved=False,
                message="shorts.allow_live=false (no Gate futures in v0)",
                code="shorts_live_blocked",
            )
        pos = get_position(order.symbol, timeframe)
        params = resolve_short_params(
            symbol=order.symbol,
            tier=pos.get("strategy_tier") if isinstance(pos, dict) else None,
            lot=pos if isinstance(pos, dict) else None,
            config_raw=raw,
        )
        if order.type == "COVER":
            if not is_short(pos) or float((pos or {}).get("amount") or 0) <= 0:
                return RiskDecision(approved=False, message="no short to cover", code="no_short")
            cover_src = str(source or getattr(order, "source", None) or "").strip().lower()
            cover_exit = str(getattr(order, "exit_source", "") or "").strip().lower()
            if cover_src == "liq_cascade" or cover_exit == "liq_cascade":
                try:
                    from strategies.position_lock import (
                        attach_lock_from_ledger,
                        auto_sell_blocked,
                        log_lock_block,
                    )

                    locked_pos = attach_lock_from_ledger(pos, order.symbol, timeframe) or pos
                    locked, lock_msg = auto_sell_blocked(
                        locked_pos, "liq_cascade", config=raw
                    )
                    if locked:
                        log_lock_block(order.symbol, lock_msg, source="liq_cascade")
                        return RiskDecision(
                            approved=False,
                            message=lock_msg,
                            code="position_locked",
                        )
                except Exception as exc:
                    try:
                        from logger import log as _log

                        _log(
                            f"position_lock cover check error {order.symbol}: {exc}",
                            "ERROR",
                        )
                    except Exception:
                        pass
                    return RiskDecision(
                        approved=False,
                        message=f"position_lock_check_error: {exc}"[:200],
                        code="position_lock_check_error",
                    )
            out = TradeOrder(
                type="COVER",
                symbol=order.symbol,
                price=order.price,
                amount=order.amount or float(pos.get("amount") or 0),
                signal=order.signal or "COVER",
                source=source or order.source,
                exit_source=getattr(order, "exit_source", "") or "",
                exit_rationale=getattr(order, "exit_rationale", "") or "",
                ctx_oracle_state=getattr(order, "ctx_oracle_state", None),
                ctx_coin_regime=getattr(order, "ctx_coin_regime", None),
                ctx_volume_rel=getattr(order, "ctx_volume_rel", None),
                ctx_volume_window_days=getattr(order, "ctx_volume_window_days", None),
            )
            return RiskDecision(approved=True, order=out, message="ok")

        if not shorts_enabled(raw):
            return RiskDecision(approved=False, message="shorts disabled", code="shorts_disabled")

        # SHORT open — same material-long test as the flip gate (notional ≥ 1 USDT).
        adding_short = is_short(pos) and float((pos or {}).get("amount") or 0) > 0
        if adding_short:
            pass  # add to existing short
        elif is_open_position(pos or {}):
            return RiskDecision(
                approved=False,
                message="one-way: close long before short",
                code="one_way",
            )
        else:
            # Same slot check as a new buy. Covers never reach this branch.
            slot_block = self._open_slot_cap_blocked(order, source)
            if slot_block:
                return slot_block
        n_short = 0
        open_margin = 0.0
        invalid_lots = []
        try:
            for p in list_active_positions():
                if is_short(p) and float(p.get("amount") or 0) > 0:
                    n_short += 1
                    raw_lev = p.get("leverage") or params.get("leverage") or 2
                    # float() first: non-numeric stored lev (e.g. "x") still fail-closed via shorts_slots.
                    lot_lev = clamp_leverage(float(raw_lev), cap=params["leverage_cap"])
                    contrib = margin_usdt(
                        float(p.get("amount") or 0),
                        float(p.get("average_entry") or 0),
                        lot_lev,
                    )
                    if math.isnan(contrib) or math.isinf(contrib) or contrib < 0:
                        invalid_lots.append(
                            f"{p.get('symbol') or '?'} raw_leverage={raw_lev!r}"
                        )
                    open_margin += contrib
        except Exception as exc:
            return RiskDecision(
                approved=False,
                message=f"short book check failed: {exc}"[:200],
                code="shorts_slots",
            )
        if math.isnan(open_margin) or math.isinf(open_margin) or open_margin < 0:
            from logger import log

            detail = ", ".join(invalid_lots) or "unknown"
            log(
                f"short margin cap: open margin invalid ({open_margin!r}); lots: {detail}",
                "ERROR",
            )
            return RiskDecision(
                approved=False,
                code="short_margin_pct",
                message="short margin cap: open margin invalid",
            )
        if n_short >= int(params.get("max_open") or 6) and not (
            is_short(pos) and float((pos or {}).get("amount") or 0) > 0
        ):
            return RiskDecision(approved=False, message="shorts.max_open reached", code="shorts_slots")
        from strategies.dca_policy import is_human_operator_buy

        if not is_human_operator_buy(source or getattr(order, "source", None)):
            min_mcap = float(params.get("market_cap_min_usd") or 0)
            if min_mcap > 0:
                mcap = None
                try:
                    from data.cmc_market_cap import resolve_market_cap_usd

                    mcap = resolve_market_cap_usd(order.symbol)
                except Exception:
                    mcap = None
                if mcap is None or float(mcap) < min_mcap:
                    return RiskDecision(
                        approved=False,
                        message=f"short mcap {mcap} < min {min_mcap:.0f}",
                        code="short_mcap",
                    )
        else:
            min_mcap = float(params.get("market_cap_min_usd") or 0)
            if min_mcap > 0:
                try:
                    from data.cmc_market_cap import resolve_market_cap_usd

                    mcap = resolve_market_cap_usd(order.symbol)
                except Exception:
                    mcap = None
                if mcap is not None and float(mcap) < min_mcap:
                    return RiskDecision(
                        approved=False,
                        message=f"short mcap {mcap} < min {min_mcap:.0f}",
                        code="short_mcap",
                    )
        lev = clamp_leverage(order.leverage or params["leverage"], cap=params["leverage_cap"])
        usdt = float(order.usdt_amount or 0) or float(self.config.max_usdt_per_trade)
        # Same final-notional cap as buys, every source including manual.
        try:
            ticket_cap = float(self._base_usdt_cap() or 0)
        except (TypeError, ValueError):
            ticket_cap = 0.0
        if ticket_cap > 0 and usdt > ticket_cap:
            try:
                from logger import log

                log(
                    f"ticket_cap {order.symbol} original={usdt:.2f} "
                    f"capped={ticket_cap:.2f}",
                    "INFO",
                )
            except Exception:
                pass
            usdt = ticket_cap
            try:
                min_trade = float(self.config.risk_config.get("min_trade_usdt", 5.0))
            except (TypeError, ValueError):
                min_trade = 5.0
            if usdt < min_trade:
                return RiskDecision(
                    approved=False,
                    message=f"Capped size ${usdt:.2f} below minimum (${min_trade:.0f})",
                    code="ticket_below_min",
                )
        if order.price <= 0:
            return RiskDecision(approved=False, message="invalid price", code="bad_price")
        qty = usdt / order.price
        margin = margin_usdt(qty, order.price, lev)
        try:
            cash = float(self._available_usdt(fallback=0))
        except Exception as exc:
            return RiskDecision(
                approved=False,
                message=f"short cash unknown: {exc}"[:200],
                code="short_margin",
            )
        if margin > cash + 1e-6:
            return RiskDecision(
                approved=False,
                message=f"short margin {margin:.0f} > cash {cash:.0f}",
                code="short_margin",
            )
        max_pct = float(params.get("max_margin_pct") or 0)
        if max_pct > 0:
            try:
                nav = float(self._portfolio_equity(order.price, order.symbol) or 0)
            except Exception:
                nav = 0.0
            if nav <= 0:
                return RiskDecision(
                    approved=False,
                    message="short margin cap: NAV unknown",
                    code="short_margin_pct",
                )
            limit = nav * (max_pct / 100.0)
            if open_margin + margin > limit + 1e-6:
                return RiskDecision(
                    approved=False,
                    message=f"short margin {open_margin + margin:.0f} > {max_pct:g}% NAV",
                    code="short_margin_pct",
                )
        src = str(source or order.source or "")
        if not is_human_operator_buy(src):
            try:
                from services.venue_quality import check_venue_for_buy

                liq = check_venue_for_buy(
                    order.symbol,
                    source=src,
                    planned_usdt=float(usdt or 0),
                    config_raw=raw if isinstance(raw, dict) else None,
                )
            except Exception as exc:
                return RiskDecision(
                    approved=False,
                    message=f"liq_guard_missing_input {exc}"[:200],
                    code="liq_guard_missing_input",
                )
            if not liq.ok:
                extra = list(getattr(liq, "guard_codes", None) or [])
                if liq.code and liq.code not in extra:
                    extra.append(liq.code)
                return RiskDecision(
                    approved=False,
                    message=(" ".join(str(r) for r in (liq.reasons or [])) or liq.code)[:400],
                    code=(extra[0] if extra else liq.code) or "liq_guard_missing_input",
                    size_multiplier=0.0,
                    details={"codes": extra, "planned_usdt": float(usdt or 0)},
                )
            room = self._short_liq_room_reject(order, liq, float(usdt or 0))
            if room is not None:
                return room
        out = TradeOrder(
            type="SHORT",
            symbol=order.symbol,
            price=order.price,
            amount=qty,
            usdt_amount=usdt,
            signal=order.signal or "SHORT",
            source=source or order.source,
            leverage=lev,
            ctx_oracle_state=getattr(order, "ctx_oracle_state", None),
            ctx_coin_regime=getattr(order, "ctx_coin_regime", None),
            ctx_volume_rel=getattr(order, "ctx_volume_rel", None),
            ctx_volume_window_days=getattr(order, "ctx_volume_window_days", None),
        )
        return RiskDecision(approved=True, order=out, message="ok")

    def _trade_cooldown_blocked(
        self,
        order: TradeOrder,
        timeframe: str,
        source: str = "auto",
        confidence: float | None = None,
    ) -> tuple:
        if source == "manual":
            return False, ""
        signal = order.signal or ""
        if order.type == "SELL" and signal in ("SELL_STOP_FULL", "SELL_STOP_PARTIAL", "SELL_FULL"):
            return False, ""
        if order.type == "SELL" and "FULL" in signal:
            return False, ""

        pos = get_position(order.symbol, timeframe)
        params = self.config.strategy_params(order.symbol, timeframe)
        defaults = self.config.dry_run_defaults if is_dry_run_enhanced(self.config.raw) else {}
        cmc_cfg = self.config.cmc_config

        if order.type == "SELL" and source == "cmc":
            last_cmc = pos.get("last_cmc_sell_at")
            if last_cmc:
                try:
                    last_ts = datetime.fromisoformat(str(last_cmc).replace("Z", ""))
                    min_hours = float(
                        params.get("cmc_min_hours_between_sells")
                        or defaults.get("cmc_min_hours_between_sells")
                        or cmc_cfg.get("cmc_min_hours_between_sells", 6)
                    )
                    elapsed = (datetime.now() - last_ts).total_seconds() / 3600.0
                    if elapsed < min_hours:
                        return True, (
                            f"CMC sell cooldown: {elapsed:.1f}h since last CMC sell "
                            f"(min {min_hours:.1f}h)"
                        )
                except Exception:
                    pass

        last_at = pos.get("last_trade_at")
        last_type = pos.get("last_trade_type")
        if not last_at:
            return False, ""

        try:
            last_ts = datetime.fromisoformat(str(last_at).replace("Z", ""))
        except Exception as e:
            dec = self._guard_failed("trade_cooldown", e, order)
            if dec is not None:
                return True, "cooldown_timestamp_unparsable"
            return False, ""

        pos_amount = float(pos.get("amount", 0) or 0)
        is_dca = order.signal == "BUY_DCA" or source in ("dca", "dca_recovery")

        if order.type == "BUY" and source == "cmc":
            blocked, reason = self._trending_position_cap_blocked(order, timeframe)
            if blocked:
                return True, reason

        if order.type == "BUY" and last_type == "SELL" and not (is_dca and pos_amount > 0):
            blocked, reason = self._rebuy_after_sell_blocked(
                order, timeframe, source, last_ts, pos, params, defaults,
                confidence=confidence,
            )
            if blocked:
                return True, reason

        if order.type == "BUY" and is_dca and pos_amount > 0:
            blocked, reason = self._dca_interval_blocked(pos, params, defaults, source=source)
            if blocked:
                return True, reason
            return False, ""

        if last_type != order.type:
            return False, ""

        if order.type == "BUY":
            min_hours = float(
                params.get("min_hours_between_buys")
                or defaults.get("min_hours_between_buys")
                or defaults.get("trade_cooldown_hours")
                or self.config.trade_cooldown_hours
            )
        else:
            min_hours = float(
                params.get("min_hours_between_sells")
                or defaults.get("min_hours_between_sells")
                or defaults.get("trade_cooldown_hours")
                or self.config.trade_cooldown_hours
            )

        elapsed = (datetime.now() - last_ts).total_seconds() / 3600.0
        if elapsed < min_hours:
            return True, (
                f"Trade cooldown: {elapsed:.1f}h since last {order.type} "
                f"(min {min_hours:.1f}h)"
            )
        return False, ""

    def _rebuy_after_sell_blocked(
        self,
        order: TradeOrder,
        timeframe: str,
        source: str,
        last_ts: datetime,
        pos: dict,
        params: dict,
        defaults: dict,
        confidence: float | None = None,
    ) -> tuple[bool, str]:
        if source == "manual":
            return False, ""

        from risk.rebuy_cooldown import (
            format_rebuy_reject_message,
            prepare_dynamic_config,
            rebuy_cooldown_config,
            rebuy_cooldown_enabled,
            resolve_rebuy_cooldown_hours,
            signal_quality_from_confidence,
        )

        arch = self.config.architecture_config
        last_signal = str(pos.get("last_sell_signal") or "")
        fallback_hours = float(
            params.get("min_hours_after_sell_before_rebuy")
            or defaults.get("min_hours_after_sell_before_rebuy")
            or self.config.min_hours_after_sell_before_rebuy
        )
        risk_cfg = self.config.risk_config or {}
        try:
            elapsed = (datetime.now() - last_ts).total_seconds() / 3600.0
        except TypeError:
            # aware/naive mix — treat as no cooldown rather than crash
            return False, ""

        if not rebuy_cooldown_enabled(risk_cfg, self.config.raw):
            return self._legacy_rebuy_after_sell_blocked(
                last_signal, arch, fallback_hours, elapsed
            )

        cfg = prepare_dynamic_config(
            rebuy_cooldown_config(risk_cfg, self.config.raw), arch
        )
        regime = None
        try:
            from services.market_policy_fusion import get_global_market_bias

            regime = (get_global_market_bias() or {}).get("regime")
        except Exception:
            pass
        profile = None
        try:
            from intelligence.memory.cache import get_coin_profile

            profile = get_coin_profile(order.symbol, config=self.config.raw)
        except Exception:
            pass

        last_exit = (
            pos.get("last_exit_source")
            or pos.get("exit_source")
            or pos.get("last_sell_exit_source")
        )
        vol_tier = pos.get("strategy_tier") or params.get("volatility_tier")
        result = resolve_rebuy_cooldown_hours(
            regime=regime,
            last_sell_signal=last_signal,
            last_exit_source=str(last_exit) if last_exit else None,
            order_signal=str(getattr(order, "signal", None) or "") or None,
            volatility_tier=str(vol_tier) if vol_tier else None,
            # #414: TradeOrder has no `confidence`; use the value evaluate() received.
            signal_quality=signal_quality_from_confidence(confidence),
            profile=profile,
            config=cfg,
            fallback_hours=fallback_hours,
        )
        if cfg.get("log"):
            try:
                from logger import log

                log(
                    f"rebuy_cooldown symbol={order.symbol} hours={result.hours:.2f} "
                    f"elapsed={elapsed:.2f} regime={regime} "
                    f"factors={result.factors} reasons={result.reasons}",
                    "DEBUG",
                )
            except Exception:
                pass
        if result.hours <= 0:
            return False, ""
        if elapsed < result.hours:
            return True, format_rebuy_reject_message(elapsed_h=elapsed, result=result)
        return False, ""

    def _legacy_rebuy_after_sell_blocked(
        self,
        last_signal: str,
        arch: dict,
        fallback_hours: float,
        elapsed: float,
    ) -> tuple[bool, str]:
        """Flat hours when risk.rebuy_cooldown.enabled is false."""
        min_hours = fallback_hours
        stop_sell = _is_stop_loss_sell(last_signal)
        if arch.get("block_rebuy_if_last_sell_was_stop", False) and stop_sell:
            min_hours = float(arch.get("rebuy_after_stop_loss_hours", 24.0))
        if min_hours <= 0:
            return False, ""
        if elapsed < min_hours:
            label = "Stop-loss rebuy cooldown" if stop_sell else "Rebuy cooldown"
            return True, (
                f"{label}: {elapsed:.1f}h since last SELL "
                f"(min {min_hours:.1f}h after sell)"
            )
        return False, ""

    def _trending_overlay_symbols(self) -> set:
        """Symbols currently on the trending overlay the runtime actually writes.

        Enhanced dry-run (#416) persists the trending coins to
        ``watchlist.dry_run_overlay.json`` and never populates the CMC overlay
        (``services/dry_run_watchlist.py::_save_overlay``). Mirror the branch
        ``data_manager`` uses so the size haircut and the open-position cap see the
        same coins as the watchlist. ``trending_watchlist_live_enabled`` stays the
        on/off switch; ``is_dry_run_enhanced`` is False whenever live execution
        resolves to real orders, so the real-live path keeps reading the CMC overlay.
        """
        from data_manager import (
            load_cmc_trending_overlay,
            load_dry_run_overlay,
            trending_watchlist_live_enabled,
        )

        raw = self.config.raw
        if not trending_watchlist_live_enabled(raw):
            return set()
        if is_dry_run_enhanced(raw):
            overlay = load_dry_run_overlay()
        else:
            overlay = load_cmc_trending_overlay()
        return {
            c.get("symbol") for c in (overlay.get("coins") or []) if isinstance(c, dict)
        }

    def _trending_position_cap_blocked(
        self,
        order: TradeOrder,
        timeframe: str,
    ) -> tuple[bool, str]:
        tw = self.config.trending_watchlist_config
        cap = int(tw.get("max_open_from_trending", 0))
        if cap <= 0 or order.type != "BUY":
            return False, ""
        from strategies.positions import list_active_positions

        trending_syms = self._trending_overlay_symbols()
        if not trending_syms:
            return False, ""
        trending_open = sum(
            1 for pos in list_active_positions() if pos.get("symbol") in trending_syms
        )
        if trending_open >= cap:
            return True, (
                f"Trending position cap: {trending_open}/{cap} open from CMC trending"
            )
        return False, ""

    def _dca_interval_blocked(
        self,
        pos: dict,
        params: dict,
        defaults: dict,
        *,
        source: str = "dca",
    ) -> tuple[bool, str]:
        from strategies.dca import _hours_since_last_dca

        dca_cfg = dict(params.get("dca") or {})
        interval_hours = float(dca_cfg.get("interval_hours", 12))
        elapsed = _hours_since_last_dca(pos)
        if elapsed is None:
            return False, ""
        if elapsed < interval_hours:
            return True, (
                f"DCA interval: {elapsed:.1f}h since last DCA "
                f"(min {interval_hours:.1f}h)"
            )
        return False, ""

    @staticmethod
    def _order_side(order: dict) -> str:
        side = str(order.get("side") or order.get("type") or "").lower()
        if side in ("buy", "sell"):
            return side
        return ""

    def _load_orders_document(self) -> dict:
        scope = self._eval_scope()
        scoped = getattr(scope, "doc", None)
        if scoped is not None and scoped is not _EVAL_DOC_PENDING:
            return scoped
        from data_manager import load_orders, resolve_ledger_scope

        doc = load_orders(resolve_ledger_scope(self.config.trading_mode)) or {}
        if scoped is _EVAL_DOC_PENDING:
            scope.doc = doc
        return doc

    def _daily_trades_count(
        self,
        side: str | None = None,
        *,
        dca_only: bool | None = None,
    ) -> int:
        """Count filled ledger orders in the last 24h (optional buy/sell + DCA filter)."""
        return sum(1 for _ in self._iter_daily_filled_orders(side=side, dca_only=dca_only))

    def _iter_daily_filled_orders(
        self,
        *,
        side: str | None = None,
        dca_only: bool | None = None,
        orders_doc: dict | None = None,
        cutoff: datetime | None = None,
    ):
        if cutoff is None:
            cutoff = datetime.now() - timedelta(hours=24)
        if orders_doc is None:
            orders_doc = self._load_orders_document()
        orders = orders_doc.get("orders", []) if isinstance(orders_doc, dict) else []
        want = (side or "").lower() or None
        for order in orders:
            if order.get("status") != "filled":
                continue
            order_side = self._order_side(order)
            if want and order_side != want:
                continue
            is_dca = self._order_is_dca(order)
            if dca_only is True and not is_dca:
                continue
            if dca_only is False and is_dca:
                continue
            ts_raw = (
                order.get("timestamps", {}).get("filled")
                or order.get("timestamps", {}).get("created")
                or ""
            )
            try:
                ts = datetime.fromisoformat(str(ts_raw).replace("Z", ""))
            except Exception:
                continue
            if ts >= cutoff:
                yield order

    def _daily_counters_from_orders(
        self,
        orders_doc: dict | None,
        *,
        cutoff: datetime | None = None,
        dca_limits: bool | None = None,
    ) -> dict:
        """Derive the five daily counters from one already-loaded orders document."""
        if cutoff is None:
            cutoff = datetime.now() - timedelta(hours=24)
        if dca_limits is None:
            dca_limits = self._dca_limits_enabled()
        filled = list(
            self._iter_daily_filled_orders(orders_doc=orders_doc or {}, cutoff=cutoff)
        )
        buys = [o for o in filled if self._order_side(o) == "buy"]
        dca_buys = [o for o in buys if self._order_is_dca(o)]
        if dca_limits:
            daily_buys = sum(1 for o in buys if not self._order_is_dca(o))
        else:
            daily_buys = len(buys)
        return {
            "daily_trades": len(filled),
            "daily_buys": daily_buys,
            "daily_dca_buys": len(dca_buys),
            "daily_dca_usdt": sum(self._filled_order_usdt(o) for o in dca_buys),
            "daily_sells": sum(1 for o in filled if self._order_side(o) == "sell"),
        }

    def _daily_buys_count(self, *, dca_only: bool | None = None) -> int:
        return self._daily_trades_count("buy", dca_only=dca_only)

    def _daily_dca_buys_count(self) -> int:
        return self._daily_buys_count(dca_only=True)

    def _daily_dca_usdt_sum(self) -> float:
        return sum(self._filled_order_usdt(order) for order in self._iter_daily_filled_orders(
            side="buy", dca_only=True,
        ))

    def _daily_sells_count(self) -> int:
        return self._daily_trades_count("sell")