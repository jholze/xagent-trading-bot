"""#631 long-buy membership revise — one Ersatz-Cap, no open-all.

Revise path (config-first):

* Flag ``universe.membership_revise.mode``: ``off`` | ``shadow`` | ``enforce``.
* Ersatz-Cap ``universe.membership_revise.ersatz_cap_trade_max`` (positive int).

``shadow`` (shipped default) records the staged split knobs and does not change
the live trade set. ``enforce`` applies staged ``trade_max`` / ``trade_rank_by``
clamped to the Ersatz-Cap, then drops non-forced names that would exceed it
(including gainer-expand extras). ``off`` is the Ist path.

Active modes (``shadow`` / ``enforce``) without a usable cap raise
:class:`core.config_guardrails.ConfigValidationError`. RelVol exemption from
``universe_trade_cap`` is not part of this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.config_guardrails import ConfigValidationError

# Documented names (PR / U2). Do not rename without a Spec change.
MEMBERSHIP_REVISE_FLAG = "universe.membership_revise.mode"
ERSATZ_CAP_PATH = "universe.membership_revise.ersatz_cap_trade_max"
ERSATZ_CAP_NAME = "ersatz_cap_trade_max"
STAGED_TRADE_MAX_PATH = "universe.membership_revise.staged_trade_max_coins"
STAGED_RANK_PATH = "universe.membership_revise.staged_trade_rank_by"

ALLOWED_MODES = frozenset({"off", "shadow", "enforce"})
ALLOWED_RANKS = frozenset({"quality_score", "trending_rank", "as_is"})

# Hard ceiling so a huge "cap" cannot act as open-all. Live config ships 72.
ERSATZ_CAP_MAX = 200
STAGED_TRADE_MAX_HI = 10_000

# U1 — Ist membership layers and the #628 tape codes they emit.
# gainer expand and CMC have no dedicated tape code; they only change who
# reaches the split gate or the WQE gate.
MEMBERSHIP_LAYERS: tuple[dict[str, str], ...] = (
    {
        "layer": "universe_split_observe_trade",
        "where": "services.universe.split / risk.risk_manager",
        "knobs": "universe.split_enabled, trade_max_coins, trade_rank_by",
        "tape_code": "universe_trade_cap",
        "emits": "universe_trade_cap",
        "note": (
            "New non-DCA, non-RelVol BUY outside the trade set. "
            "GAINER_RELVOL / source gainer_relvol stays exempt."
        ),
    },
    {
        "layer": "gainer_universe_expand_inject",
        "where": "services.gainer_universe.inject",
        "knobs": "gainer_universe.expand_inject_max, trade_max_with_expand, mode=trade_expand",
        "tape_code": "",
        "emits": "",
        "note": (
            "Injects names into the trade set. No dedicated #628 code. "
            "A name that is still outside the set is universe_trade_cap at the split gate."
        ),
    },
    {
        "layer": "gainer_chase_guard",
        "where": "services.gainer_universe.chase_guard / risk.risk_manager",
        "knobs": "chase_guard_enabled, chase_max_gain_from_prev_close_pct, chase_guard_sources",
        "tape_code": "gainer_chase_guard",
        "emits": "gainer_chase_guard",
        "note": "New entries only. DCA adds are not chase-checked.",
    },
    {
        "layer": "wqe_soft_ranking",
        "where": "services.watchlist_quality.soft / enforce / risk.risk_manager",
        "knobs": "watchlist_quality.mode soft|enforce, min_buy_score, ai.sort_by",
        "tape_code": "watchlist_quality",
        "emits": "watchlist_quality",
        "note": "soft and enforce can reject a new BUY. shadow scores only and does not emit this code.",
    },
    {
        "layer": "cmc_wqe",
        "where": "services.watchlist_quality.universe",
        "knobs": "rank_cmc_candidates_by_wqe, cmc_only_buy_allowed",
        "tape_code": "",
        "emits": "",
        "note": (
            "No dedicated #628 code. A CMC-only buy rejected by WQE soft/enforce "
            "emits watchlist_quality."
        ),
    },
)

TAPE_CODES_631 = ("universe_trade_cap", "gainer_chase_guard", "watchlist_quality")


class MembershipReviseRefused(ConfigValidationError):
    """Active membership revise without a usable Ersatz-Cap."""


@dataclass(frozen=True)
class MembershipRevise:
    mode: str
    cap: int | None
    staged_trade_max: int | None
    staged_rank_by: str | None

    def live_trade_max(self, ist_trade_max: int) -> int:
        """Trade-max passed to the split selector. Enforce never returns <= 0."""
        if self.mode != "enforce":
            return int(ist_trade_max)
        staged = self.staged_trade_max if self.staged_trade_max is not None else int(ist_trade_max)
        if staged <= 0:
            staged = int(self.cap or 0)
        cap = int(self.cap or 0)
        effective = min(int(staged), cap)
        if effective <= 0:
            raise MembershipReviseRefused(
                ERSATZ_CAP_PATH,
                self.cap,
                "enforce requires a positive Ersatz-Cap",
            )
        return effective

    def live_rank_by(self, ist_rank_by: str) -> str:
        if self.mode != "enforce":
            return ist_rank_by
        return self.staged_rank_by or ist_rank_by


def _section(config: dict | None) -> dict[str, Any] | None:
    """Return the revise section, or None when absent.

    A non-dict section is refused so a typo cannot fall through to open-all.
    """
    if not isinstance(config, dict):
        return None
    universe = config.get("universe")
    if not isinstance(universe, dict) or "membership_revise" not in universe:
        return None
    sec = universe.get("membership_revise")
    if not isinstance(sec, dict):
        raise MembershipReviseRefused(
            "universe.membership_revise",
            sec,
            "must be an object",
        )
    return sec


def _positive_int(path: str, value: Any, *, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MembershipReviseRefused(path, value, "must be an integer")
    if not (1 <= value <= hi):
        raise MembershipReviseRefused(path, value, f"must be in [1, {hi}]")
    return value


def parse_membership_revise(config: dict | None) -> MembershipRevise:
    """Parse revise config. ``shadow`` and ``enforce`` refuse a missing cap."""
    sec = _section(config)
    if sec is None:
        return MembershipRevise("off", None, None, None)
    raw_mode = sec.get("mode", "off")
    if not isinstance(raw_mode, str) or raw_mode not in ALLOWED_MODES:
        raise MembershipReviseRefused(
            MEMBERSHIP_REVISE_FLAG,
            raw_mode,
            "must be one of off|shadow|enforce",
        )
    if raw_mode == "off":
        return MembershipRevise("off", None, None, None)

    if ERSATZ_CAP_NAME not in sec or sec.get(ERSATZ_CAP_NAME) is None:
        raise MembershipReviseRefused(
            ERSATZ_CAP_PATH,
            sec.get(ERSATZ_CAP_NAME),
            "active membership revise requires ersatz_cap_trade_max",
        )
    cap = _positive_int(ERSATZ_CAP_PATH, sec.get(ERSATZ_CAP_NAME), hi=ERSATZ_CAP_MAX)

    staged_max: int | None = None
    if "staged_trade_max_coins" in sec and sec.get("staged_trade_max_coins") is not None:
        staged_max = _positive_int(
            STAGED_TRADE_MAX_PATH,
            sec.get("staged_trade_max_coins"),
            hi=STAGED_TRADE_MAX_HI,
        )
    staged_rank: str | None = None
    if "staged_trade_rank_by" in sec and sec.get("staged_trade_rank_by") is not None:
        rank = sec.get("staged_trade_rank_by")
        if not isinstance(rank, str) or rank not in ALLOWED_RANKS:
            raise MembershipReviseRefused(
                STAGED_RANK_PATH,
                rank,
                "must be one of quality_score|trending_rank|as_is",
            )
        staged_rank = rank
    return MembershipRevise(raw_mode, cap, staged_max, staged_rank)


def assert_membership_revise_config(config: dict | None) -> None:
    """Refuse an active revise that has no usable Ersatz-Cap.

    ``off`` and a missing section are valid. Called from config save and from
    ``load_trade_universe`` so a hand-edited config cannot widen without the cap.
    """
    parse_membership_revise(config)


def _sym(coin: dict | None) -> str:
    if not isinstance(coin, dict):
        return ""
    return str(coin.get("symbol") or "").strip()


def bind_ersatz_cap(
    coins: list[dict],
    *,
    cap: int,
    forced_symbols: set[str] | None = None,
) -> list[dict]:
    """Bound trade membership to the Ersatz-Cap.

    Forced symbols (open positions, and base when ``trade_include_base``) are
    kept even when they already exceed the cap — same overflow rule as Ist
    ``trade_max``. Non-forced names fill only the remaining slots, so discovery
    cannot grow past the cap and a forced overflow admits no extra names.
    """
    if isinstance(cap, bool) or not isinstance(cap, int) or cap <= 0:
        raise MembershipReviseRefused(
            ERSATZ_CAP_PATH,
            cap,
            "enforce requires a positive Ersatz-Cap",
        )
    forced = {str(s).strip() for s in (forced_symbols or set()) if s}
    kept_forced: list[dict] = []
    rest: list[dict] = []
    seen: set[str] = set()
    for coin in coins or []:
        sym = _sym(coin)
        if not sym or sym in seen:
            continue
        seen.add(sym)
        if sym in forced:
            kept_forced.append(coin)
        else:
            rest.append(coin)
    room = max(0, cap - len(kept_forced))
    return kept_forced + rest[:room]
