"""#631 U1–U3: membership revise with one Ersatz-Cap. No network.

Ersatz-Cap: ``universe.membership_revise.ersatz_cap_trade_max``
Flag: ``universe.membership_revise.mode`` (off|shadow|enforce). Shadow does not
change the live trade set. Enforce clamps staged trade_max/rank to the cap.
RelVol stays exempt from ``universe_trade_cap``.
"""

from __future__ import annotations

import json
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.config import BotConfig
from core.config_guardrails import ConfigValidationError, validate_config_for_save
from core.models import TradeOrder
from risk.risk_manager import RiskManager
from services.universe.membership_revise import (
    ERSATZ_CAP_MAX,
    ERSATZ_CAP_NAME,
    ERSATZ_CAP_PATH,
    MEMBERSHIP_LAYERS,
    MEMBERSHIP_REVISE_FLAG,
    TAPE_CODES_631,
    MembershipReviseRefused,
    bind_ersatz_cap,
    parse_membership_revise,
)
from services.universe.split import load_trade_universe
from services.venue_quality import VenueQualityResult

_ROOT = Path(__file__).resolve().parents[2]
_DOC = _ROOT / "docs" / "universe_membership_631.md"

IN_SET = "INSET/USDT"
OUT_SET = "OUTSET/USDT"


def _coin(sym: str, **extra):
    row = {"symbol": sym, "active": True, "ticker": sym.split("/")[0]}
    row.update(extra)
    return row


def _revise(
    mode: str,
    *,
    cap: int | None = 5,
    staged_max: int | None = 100,
    rank: str | None = "quality_score",
    include_cap: bool = True,
) -> dict:
    sec: dict = {"mode": mode}
    if include_cap and cap is not None:
        sec[ERSATZ_CAP_NAME] = cap
    if staged_max is not None:
        sec["staged_trade_max_coins"] = staged_max
    if rank is not None:
        sec["staged_trade_rank_by"] = rank
    return sec


def _universe(
    *,
    mode: str = "enforce",
    trade_max: int = 40,
    rank: str = "as_is",
    cap: int | None = 2,
    staged_max: int | None = 100,
    staged_rank: str | None = "quality_score",
    include_cap: bool = True,
    split: bool = True,
) -> dict:
    uni = {
        "split_enabled": split,
        "observe_max_coins": 100,
        "trade_max_coins": trade_max,
        "trade_include_open_positions": True,
        "trade_include_base": False,
        "trade_rank_by": rank,
        "membership_revise": _revise(
            mode,
            cap=cap,
            staged_max=staged_max,
            rank=staged_rank,
            include_cap=include_cap,
        ),
    }
    return {"universe": uni, "watchlist_quality": {"mode": "off"}}


def _patch_universe_io(monkeypatch, observe: list[dict]):
    monkeypatch.setattr("data_manager.load_watchlist", lambda tenant_id=None: [])
    monkeypatch.setattr(
        "services.universe.split._quality_lookup",
        lambda tenant_id="default", use_ai_score=True: {},
    )
    monkeypatch.setattr("services.universe.split._open_symbols_live", lambda: set())
    monkeypatch.setattr(
        "services.universe.split.load_observe_universe",
        lambda tenant_id=None, **kwargs: list(observe),
    )


def _observe_ranked() -> list[dict]:
    """as_is order is worst-first so Ist rank would keep OUTSET, quality rank keeps INSET."""
    return [
        _coin(OUT_SET, quality_score=0.1),
        _coin("MID/USDT", quality_score=0.5),
        _coin(IN_SET, quality_score=0.9),
        _coin("TAIL/USDT", quality_score=0.05),
    ]


# --- U1 -----------------------------------------------------------------


def test_u1_layers_name_628_codes():
    emitted = {row["emits"] for row in MEMBERSHIP_LAYERS if row["emits"]}
    assert emitted == set(TAPE_CODES_631)
    by_layer = {row["layer"]: row for row in MEMBERSHIP_LAYERS}
    assert by_layer["universe_split_observe_trade"]["emits"] == "universe_trade_cap"
    assert by_layer["gainer_chase_guard"]["emits"] == "gainer_chase_guard"
    assert by_layer["wqe_soft_ranking"]["emits"] == "watchlist_quality"
    assert by_layer["gainer_universe_expand_inject"]["emits"] == ""
    assert by_layer["cmc_wqe"]["emits"] == ""
    text = _DOC.read_text(encoding="utf-8")
    for code in TAPE_CODES_631:
        assert code in text
    assert ERSATZ_CAP_PATH in text
    assert MEMBERSHIP_REVISE_FLAG in text
    assert "GAINER_RELVOL" in text


# --- U2 -----------------------------------------------------------------


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_u2_refuses_config_without_ersatz_cap(mode):
    cfg = _universe(mode=mode, include_cap=False, cap=None)
    with pytest.raises(MembershipReviseRefused) as exc:
        validate_config_for_save(cfg)
    assert exc.value.path == ERSATZ_CAP_PATH


@pytest.mark.parametrize(
    "cap",
    [None, 0, -1, True, False, 1.5, "72", ERSATZ_CAP_MAX + 1],
)
def test_u2_refuses_unusable_cap(cap):
    uni = _universe(mode="enforce", cap=5)
    uni["universe"]["membership_revise"][ERSATZ_CAP_NAME] = cap
    with pytest.raises(ConfigValidationError) as exc:
        validate_config_for_save(uni)
    assert exc.value.path == ERSATZ_CAP_PATH


def test_u2_off_and_absent_do_not_require_cap():
    validate_config_for_save({})
    validate_config_for_save({"universe": {}})
    validate_config_for_save(_universe(mode="off", include_cap=False, cap=None))


def test_u2_load_refuses_enforce_without_cap(monkeypatch):
    _patch_universe_io(monkeypatch, _observe_ranked())
    cfg = _universe(mode="enforce", include_cap=False, cap=None)
    with pytest.raises(MembershipReviseRefused):
        load_trade_universe(config=cfg, observe_coins=_observe_ranked(), open_symbols=set())


def test_u2_missing_cap_does_not_open_eligibility(monkeypatch):
    """Gate must fail closed, not treat a cap-less revise as eligible."""
    from services.universe import split

    _patch_universe_io(monkeypatch, _observe_ranked())
    cfg = _universe(mode="enforce", include_cap=False, cap=None)
    assert split.is_trade_eligible(IN_SET, config=cfg) is False
    assert split.is_trade_eligible(OUT_SET, config=cfg) is False


def test_u2_enforce_eligibility_bounded_by_cap(monkeypatch):
    _patch_universe_io(monkeypatch, _observe_ranked())
    cfg = _universe(
        mode="enforce",
        trade_max=40,
        rank="as_is",
        cap=2,
        staged_max=10_000,
        staged_rank="quality_score",
    )
    trade = load_trade_universe(
        config=cfg, observe_coins=_observe_ranked(), open_symbols=set()
    )
    syms = [c["symbol"] for c in trade]
    assert len(syms) <= 2
    assert len(syms) == 2
    assert IN_SET in syms
    assert OUT_SET not in syms
    # Staged max above the cap must not widen.
    assert "TAIL/USDT" not in syms


def test_u2_shadow_does_not_apply_staged_widen(monkeypatch):
    observe = _observe_ranked()
    _patch_universe_io(monkeypatch, observe)
    shadow = _universe(
        mode="shadow",
        trade_max=1,
        rank="as_is",
        cap=2,
        staged_max=100,
        staged_rank="quality_score",
    )
    off = _universe(mode="off", trade_max=1, rank="as_is", include_cap=False, cap=None)
    shadow_syms = [
        c["symbol"]
        for c in load_trade_universe(
            config=shadow, observe_coins=list(observe), open_symbols=set()
        )
    ]
    off_syms = [
        c["symbol"]
        for c in load_trade_universe(
            config=off, observe_coins=list(observe), open_symbols=set()
        )
    ]
    assert shadow_syms == off_syms
    assert shadow_syms == [OUT_SET]
    assert IN_SET not in shadow_syms


def test_u2_enforce_trims_expand_extras_to_cap(monkeypatch):
    observe = [_coin(IN_SET, quality_score=0.9), _coin(OUT_SET, quality_score=0.1)]
    _patch_universe_io(monkeypatch, observe)

    def _expand(trade, *_a, **_k):
        extras = [_coin(f"G{i}/USDT", quality_score=0.01) for i in range(15)]
        return list(trade) + extras

    monkeypatch.setattr(
        "services.gainer_universe.inject.merge_expand_into_trade",
        _expand,
    )
    cfg = _universe(mode="enforce", trade_max=2, rank="quality_score", cap=2, staged_max=50)
    cfg["gainer_universe"] = {"enabled": True, "mode": "trade_expand"}
    trade = load_trade_universe(config=cfg, observe_coins=observe, open_symbols=set())
    syms = [c["symbol"] for c in trade]
    assert len(syms) <= 2
    assert all(not s.startswith("G") for s in syms)


def test_u2_bind_keeps_forced_overflow_and_blocks_discovery():
    coins = [_coin(f"P{i}/USDT") for i in range(4)] + [_coin(f"D{i}/USDT") for i in range(6)]
    forced = {f"P{i}/USDT" for i in range(4)}
    bound = bind_ersatz_cap(coins, cap=2, forced_symbols=forced)
    syms = [c["symbol"] for c in bound]
    assert set(syms) == forced
    assert all(not s.startswith("D") for s in syms)


def test_u2_enforce_never_treats_non_positive_max_as_unlimited():
    parsed = parse_membership_revise(
        _universe(mode="enforce", cap=4, staged_max=9)
    )
    assert parsed.live_trade_max(0) == 4
    assert parsed.live_trade_max(40) == 4
    parsed_staged = parse_membership_revise(
        _universe(mode="enforce", cap=4, staged_max=3)
    )
    assert parsed_staged.live_trade_max(40) == 3


def test_shipped_config_is_shadow_with_cap_and_money_flags_off():
    raw = json.loads((_ROOT / "config.json").read_text(encoding="utf-8"))
    revise = raw["universe"]["membership_revise"]
    assert revise["mode"] == "shadow"
    assert revise[ERSATZ_CAP_NAME] == 72
    assert raw["exit_realtime"]["cascade"]["fire_enabled"] is False
    assert raw["shorts"]["allow_live"] is False
    assert raw["mcp"]["allow_live"] is False
    validate_config_for_save(raw)


# --- U3 -----------------------------------------------------------------


def _risk_raw(**over) -> dict:
    raw = {
        "max_usdt_per_trade": 500,
        "max_position_percent": 80,
        "max_open_positions": 50,
        "trading_mode": "paper",
        "paper": {"initial_capital_usdt": 100_000},
        "risk": {
            "min_trade_usdt": 1,
            "cash_floor_pct": 0,
            "cash_policy": {"enabled": False},
            "position_capacity": {"enabled": False},
            "moderate_deploy": {"enabled": False},
            "slot_eviction": {"enabled": False},
            "max_daily_loss_pct": 0,
            "venue_quality": {"enabled": True},
        },
        "shorts": {
            "enabled": True,
            "allow_live": False,
            "volatile": {"market_cap_min_usd": 5_000_000},
        },
        "watchlist_quality": {"mode": "off"},
    }
    raw.update(_universe(mode="enforce", cap=2, staged_max=100, staged_rank="quality_score"))
    raw.update(over)
    return raw


def _rm(raw: dict | None = None) -> RiskManager:
    cfg = BotConfig()
    cfg._raw = raw if raw is not None else _risk_raw()
    return RiskManager(cfg)


def _order(symbol: str, *, source: str, signal: str) -> TradeOrder:
    return TradeOrder(
        type="BUY",
        symbol=symbol,
        price=1.0,
        amount=0,
        usdt_amount=200.0,
        signal=signal,
        source=source,
    )


@contextmanager
def _eval_env(rm: RiskManager, *, venue_ok: bool, mcap: float):
    cap = SimpleNamespace(
        max_open_eff=100,
        enabled=False,
        rationale="",
        factors={},
        free_slots=100,
        regime=None,
    )
    venue = (
        VenueQualityResult(ok=True, reasons=["ok"])
        if venue_ok
        else VenueQualityResult(ok=False, reasons=["thin market"], code="")
    )
    with ExitStack() as stack:
        stack.enter_context(patch("risk.risk_manager.get_position", return_value={"amount": 0}))
        stack.enter_context(
            patch("risk.risk_manager.find_open_position_for_symbol", return_value=None)
        )
        stack.enter_context(patch("risk.risk_manager.count_open_full_slots", return_value=0))
        stack.enter_context(patch("risk.risk_manager.count_open_positions", return_value=0))
        stack.enter_context(patch.object(rm, "_trade_cooldown_blocked", return_value=(False, "")))
        stack.enter_context(patch.object(rm, "_daily_loss_limit_blocked", return_value=None))
        stack.enter_context(patch.object(rm, "_cash_floor_blocked", return_value=None))
        stack.enter_context(patch.object(rm, "_daily_buy_limit_blocked", return_value=None))
        stack.enter_context(
            patch.object(rm, "_dynamic_size", return_value=(200.0, {"total_multiplier": 1.0}))
        )
        stack.enter_context(patch.object(rm, "_portfolio_equity", return_value=100_000.0))
        stack.enter_context(patch.object(rm, "_spendable_usdt", return_value=50_000.0))
        stack.enter_context(patch.object(rm, "_available_usdt", return_value=50_000.0))
        stack.enter_context(patch.object(rm, "_initial_capital", return_value=100_000.0))
        stack.enter_context(patch.object(rm, "_equity_drawdown_pct", return_value=0.0))
        stack.enter_context(patch.object(rm, "_resolve_position_capacity", return_value=cap))
        stack.enter_context(patch.object(rm, "_daily_dca_usdt_limit_blocked", return_value=None))
        stack.enter_context(patch.object(rm, "_sensor_reentry_cooloff_blocked", return_value=None))
        stack.enter_context(
            patch("services.correlated_tier.api.correlated_tier_selloff_active", return_value=False)
        )
        stack.enter_context(
            patch(
                "services.gainer_universe.chase_guard.check_gainer_chase_guard",
                return_value=(False, ""),
            )
        )
        stack.enter_context(
            patch(
                "services.market_policy_fusion.get_global_market_bias",
                return_value={"block_buys": False, "apply_size_mult": False, "active": False},
            )
        )
        stack.enter_context(patch("intelligence.memory.cache.get_entry_bias", return_value="neutral"))
        stack.enter_context(patch("intelligence.memory.cache.get_coin_profile", return_value=None))
        stack.enter_context(patch("intelligence.macro.snapshot.get_risk_multipliers", return_value={}))
        stack.enter_context(patch("core.stablecoins.is_stablecoin_symbol", return_value=False))
        stack.enter_context(patch("core.stablecoins.stablecoin_buys_blocked", return_value=True))
        stack.enter_context(patch("services.watchlist_quality.config.wqe_mode", return_value="off"))
        stack.enter_context(
            patch("services.venue_quality.check_venue_for_buy", return_value=venue)
        )
        stack.enter_context(
            patch("data.cmc_market_cap.resolve_market_cap_usd", return_value=mcap)
        )
        stack.enter_context(
            patch("strategies.buy_decision_tape.emit_buy_decision_tape", return_value=None)
        )
        stack.enter_context(
            patch("services.watchlist_quality.soak_log.log_risk_reject", return_value=None)
        )
        yield


def _decide(monkeypatch, symbol: str, *, source: str, signal: str, venue_ok: bool, mcap: float):
    _patch_universe_io(monkeypatch, _observe_ranked())
    rm = _rm()
    order = _order(symbol, source=source, signal=signal)
    with _eval_env(rm, venue_ok=venue_ok, mcap=mcap):
        return rm.evaluate(order, timeframe="15m", source=source)


def test_u3_outside_trade_set_is_universe_trade_cap(monkeypatch):
    dec = _decide(
        monkeypatch,
        OUT_SET,
        source="entry_sensor_15m",
        signal="BUY",
        venue_ok=True,
        mcap=50_000_000,
    )
    assert dec.approved is False
    assert dec.code == "universe_trade_cap"


def test_u3_in_set_passes_membership_and_still_hits_venue_and_mcap(monkeypatch):
    venue_dec = _decide(
        monkeypatch,
        IN_SET,
        source="entry_sensor_15m",
        signal="BUY",
        venue_ok=False,
        mcap=50_000_000,
    )
    assert venue_dec.approved is False
    assert venue_dec.code == "venue_liquidity_block"
    assert venue_dec.code != "universe_trade_cap"

    mcap_dec = _decide(
        monkeypatch,
        IN_SET,
        source="entry_sensor_15m",
        signal="BUY",
        venue_ok=True,
        mcap=1.0,
    )
    assert mcap_dec.approved is False
    assert mcap_dec.code == "long_mcap"
    assert mcap_dec.code != "universe_trade_cap"


def test_u3_relvol_exempt_unchanged_and_mcap_still_applies(monkeypatch):
    """Default RelVol exemption: outside the trade set, not universe_trade_cap."""
    blocked = _decide(
        monkeypatch,
        OUT_SET,
        source="gainer_relvol",
        signal="GAINER_RELVOL",
        venue_ok=True,
        mcap=1.0,
    )
    assert blocked.code != "universe_trade_cap"
    assert blocked.code == "long_mcap"

    venue = _decide(
        monkeypatch,
        OUT_SET,
        source="gainer_relvol",
        signal="GAINER_RELVOL",
        venue_ok=False,
        mcap=50_000_000,
    )
    assert venue.code == "venue_liquidity_block"

    # Same symbol, non-RelVol, still capped — exemption is not a general bypass.
    plain = _decide(
        monkeypatch,
        OUT_SET,
        source="entry_sensor_15m",
        signal="BUY",
        venue_ok=True,
        mcap=50_000_000,
    )
    assert plain.code == "universe_trade_cap"
