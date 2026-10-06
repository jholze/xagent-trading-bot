"""#594 source + lane tags (shadow). Units 1–7, S1–S4, T1–T5.

``PYTEST_DB_SUFFIX=sUnivSrc``. Writes artifacts only under tmp paths — nothing
under checkout ``data/``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from notifications.telegram_commands.watchlist_commands import format_watchlist_message
from services.universe.source_tags import (
    canonical_source,
    collect_overlay_lists,
    early_trend_config,
    format_counts_line,
    format_cycle_log_line,
    list_formatter_tags,
    members_artifact_path,
    publish_cycle_tags,
    tag_universe_members,
)
from services.universe.split import load_trade_universe, select_trade_universe

REPO = Path(__file__).resolve().parents[2]
_ARTIFACT = "universe_members.json"


@pytest.fixture(autouse=True)
def _pytest_db_suffix(monkeypatch):
    monkeypatch.setenv("PYTEST_DB_SUFFIX", "sUnivSrc")


@pytest.fixture(autouse=True)
def _no_checkout_universe_members():
    root = REPO / "data"
    before = set(root.rglob(_ARTIFACT)) if root.exists() else set()
    yield
    after = set(root.rglob(_ARTIFACT)) if root.exists() else set()
    assert after == before


def _shadow(**extra) -> dict:
    cfg = {
        "universe_early_trend": {"mode": "shadow", "behavior_change": False},
        "universe": {
            "split_enabled": True,
            "observe_max_coins": 100,
            "trade_max_coins": 40,
            "trade_include_open_positions": True,
            "trade_include_base": True,
            "trade_rank_by": "as_is",
        },
        "universe_core_seed": {"mode": "off", "behavior_change": False},
    }
    cfg.update(extra)
    return cfg


def _row(symbol: str, source: str) -> dict:
    return {"symbol": symbol, "source": source, "active": True}


def _by_symbol(members: list[dict]) -> dict[str, dict]:
    return {m["symbol"]: m for m in members}


# --- Unit 1 ---


def test_tag_base_seed():
    """Coin only in the base watchlist. Lane follows trade_include_base via the existing selector."""
    base_sym = "AAA/USDT"
    base = _row(base_sym, "base")
    trade_on = select_trade_universe(
        [base],
        base_symbols={base_sym},
        include_base=True,
        include_open_positions=False,
        trade_max_coins=1,
        rank_by="as_is",
    )
    tagged = tag_universe_members(
        [base],
        trade_on,
        base_symbols={base_sym},
        open_symbols=set(),
        overlays=[],
    )
    assert tagged[0]["source"] == "base"
    assert tagged[0]["lane"] == "trade"

    filler = _row("BBB/USDT", "manual")
    observe = [filler, base]
    trade_off = select_trade_universe(
        observe,
        base_symbols={base_sym},
        include_base=False,
        include_open_positions=False,
        trade_max_coins=1,
        rank_by="as_is",
    )
    assert base_sym not in {c["symbol"] for c in trade_off}
    tagged_off = _by_symbol(
        tag_universe_members(
            observe,
            trade_off,
            base_symbols={base_sym},
            open_symbols=set(),
            overlays=[],
        )
    )
    assert tagged_off[base_sym]["source"] == "base"
    assert tagged_off[base_sym]["lane"] == "observe"


# --- Unit 2 ---


def test_tag_open_position_forced():
    sym = "CCC/USDT"
    coin = {"symbol": sym, "active": True}
    tagged = tag_universe_members(
        [],
        [coin],
        base_symbols=set(),
        open_symbols={sym},
        overlays=[],
    )
    assert tagged[0]["source"] == "position"
    assert tagged[0]["lane"] == "trade"
    assert tagged[0]["also"] == []


# --- Unit 3 ---


def test_tag_cmc_trending_observe():
    cmc = _row("DDD/USDT", "cmc_trending")
    other = _row("EEE/USDT", "manual")
    observe = [other, cmc]
    trade = select_trade_universe(
        observe,
        base_symbols=set(),
        include_base=False,
        include_open_positions=False,
        trade_max_coins=1,
        rank_by="as_is",
    )
    assert cmc["symbol"] not in {c["symbol"] for c in trade}
    tagged = _by_symbol(
        tag_universe_members(
            observe,
            trade,
            base_symbols=set(),
            open_symbols=set(),
            overlays=[[cmc]],
        )
    )
    assert tagged[cmc["symbol"]]["lane"] == "observe"
    assert tagged[cmc["symbol"]]["source"] == "cmc_trending"


# --- Unit 4 ---


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("gate_prev_top", "gainer_prev"),
        ("gainer_continuation", "gainer_continuation"),
    ],
)
def test_tag_gainer_prev_and_continuation(raw, expected):
    coin = _row("FFF/USDT", raw)
    tagged = tag_universe_members(
        [coin],
        [coin],
        base_symbols=set(),
        open_symbols=set(),
        overlays=[[coin]],
    )
    assert tagged[0]["source"] == expected
    assert tagged[0]["lane"] == "trade"
    assert canonical_source(raw) == expected


# --- Unit 5 ---


def test_position_wins_source_but_keeps_overlay_as_secondary():
    sym = "GGG/USDT"
    coin = _row(sym, "cmc_trending")
    tagged = tag_universe_members(
        [coin],
        [coin],
        base_symbols=set(),
        open_symbols={sym},
        overlays=[[coin]],
    )
    assert tagged[0]["source"] == "position"
    assert tagged[0]["source"]
    assert tagged[0]["also"] == ["cmc_trending"]
    assert tagged[0]["lane"] == "trade"


# --- Unit 6 ---


def test_unknown_overlay_fails_closed_to_discovery():
    coin = _row("HHH/USDT", "not_a_feeder")
    tagged = tag_universe_members(
        [coin],
        [coin],
        base_symbols=set(),
        open_symbols=set(),
        overlays=[[coin]],
    )
    assert tagged[0]["source"] == "discovery"
    assert tagged[0]["lane"] in {"observe", "trade"}
    assert tagged[0]["source"]


# --- Unit 7 / T2 ---


def test_shadow_does_not_change_membership(monkeypatch):
    observe = [
        _row("AAA/USDT", "base"),
        _row("BBB/USDT", "cmc_trending"),
        _row("CCC/USDT", "gate_prev_top"),
    ]
    monkeypatch.setattr(
        "data_manager.load_watchlist",
        lambda tenant_id=None: [_row("AAA/USDT", "base")],
    )

    def run(mode: str, *, behavior_change: bool = False):
        cfg = _shadow()
        cfg["universe"]["trade_max_coins"] = 2
        cfg["universe_early_trend"] = {
            "mode": mode,
            "behavior_change": behavior_change,
        }
        snapshot = json.dumps(observe, sort_keys=True)
        trade = load_trade_universe(
            config=cfg,
            observe_coins=[dict(c) for c in observe],
            open_symbols=set(),
        )
        assert json.dumps(observe, sort_keys=True) == snapshot
        return [(c["symbol"], c.get("source")) for c in trade]

    off = run("off")
    shadow = run("shadow")
    forced_true = run("shadow", behavior_change=True)
    assert off == shadow == forced_true
    assert {sym for sym, _ in off} == {sym for sym, _ in shadow}

    trade_coins = [{"symbol": sym, "source": src} for sym, src in shadow]
    tags_off_inputs = tag_universe_members(
        observe,
        trade_coins,
        base_symbols={"AAA/USDT"},
        open_symbols=set(),
        overlays=[[observe[1]], [observe[2]]],
    )
    assert {t["symbol"] for t in tags_off_inputs} == {c["symbol"] for c in observe}


# --- S1 ---


def test_s1_qnt_gainer_prev_trade_no_extra_buy(tmp_path, monkeypatch):
    qnt = _row("QNT/USDT", "gate_prev_top")
    before = json.dumps(qnt, sort_keys=True)
    logged: list[str] = []
    monkeypatch.setattr(
        "services.universe.source_tags.log",
        lambda msg, level="INFO": logged.append(msg),
    )
    buys: list[str] = []

    class _NoBuy:
        def evaluate(self, *args, **kwargs):
            buys.append("evaluate")

    monkeypatch.setattr("risk.risk_manager.RiskManager", _NoBuy, raising=False)
    members = publish_cycle_tags(
        [qnt],
        [qnt],
        open_symbols=set(),
        base_symbols=set(),
        config=_shadow(),
        tenant_id="sUnivSrc",
        data_root=str(tmp_path),
        overlays=[[qnt]],
    )
    assert json.dumps(qnt, sort_keys=True) == before
    assert qnt["source"] == "gate_prev_top"
    assert members is not None
    assert members[0]["lane"] == "trade"
    assert members[0]["source"] == "gainer_prev"
    assert buys == []
    assert any("source=gainer_prev" in line for line in logged)


# --- S2 ---


def test_s2_soon_not_in_builder_is_absent_never_base():
    soon = "SOON/USDT"
    eth = _row("ETH/USDT", "base")
    # On a CMC 24h list the observe builder did not ingest → absent, never base.
    cmc_24h_only = {soon}
    tagged = tag_universe_members(
        [eth],
        [eth],
        base_symbols={"ETH/USDT"},
        open_symbols=set(),
        overlays=[],
    )
    symbols = {t["symbol"] for t in tagged}
    assert cmc_24h_only.isdisjoint(symbols)
    assert all(t["source"] != "base" or t["symbol"] == "ETH/USDT" for t in tagged)

    # If the observe builder did include it, the tag is cmc_trending, not base.
    soon_coin = _row(soon, "cmc_trending")
    included = tag_universe_members(
        [soon_coin],
        [],
        base_symbols=set(),
        open_symbols=set(),
        overlays=[[soon_coin]],
    )
    assert included[0]["lane"] == "observe"
    assert included[0]["source"] == "cmc_trending"
    assert included[0]["source"] != "base"


# --- S3 ---


def test_s3_lab_open_lot_still_forced(monkeypatch):
    cfg = _shadow()
    cfg["universe"]["trade_max_coins"] = 1
    cfg["universe"]["trade_include_base"] = False
    filler = _row("OTHER/USDT", "manual")
    monkeypatch.setattr("data_manager.load_watchlist", lambda tenant_id=None: [])
    trade = load_trade_universe(
        config=cfg,
        observe_coins=[filler],
        open_symbols={"LAB/USDT"},
    )
    assert "LAB/USDT" in {c["symbol"] for c in trade}
    tagged = _by_symbol(
        tag_universe_members(
            [filler],
            trade,
            base_symbols=set(),
            open_symbols={"LAB/USDT"},
            overlays=[],
        )
    )
    assert tagged["LAB/USDT"]["lane"] == "trade"
    assert tagged["LAB/USDT"]["source"] == "position"


# --- S4 ---


def test_s4_mode_off_skips_tags_and_list_unchanged(tmp_path):
    coins = [{"symbol": "AAA/USDT", "name": "Alpha", "active": True, "source": "base"}]
    plain = format_watchlist_message(coins)
    off = {"universe_early_trend": {"mode": "off", "behavior_change": False}}
    assert early_trend_config(off)["mode"] == "off"
    assert list_formatter_tags(off, tenant_id="sUnivSrc", data_root=str(tmp_path)) is None
    assert format_watchlist_message(coins, source_tags=None) == plain
    assert format_watchlist_message(coins) == plain

    publish_cycle_tags(
        coins,
        coins,
        open_symbols=set(),
        base_symbols={"AAA/USDT"},
        config=_shadow(),
        tenant_id="sUnivSrc",
        data_root=str(tmp_path),
        overlays=[],
    )
    assert list_formatter_tags(off, tenant_id="sUnivSrc", data_root=str(tmp_path)) is None
    assert format_watchlist_message(coins) == plain

    fresh = tmp_path / "offmode"
    assert publish_cycle_tags(
        coins,
        coins,
        config=off,
        tenant_id="sUnivSrc",
        data_root=str(fresh),
        base_symbols={"AAA/USDT"},
        open_symbols=set(),
        overlays=[],
    ) is None
    assert not any(fresh.rglob(_ARTIFACT))


# --- T1 ---


def test_t1_every_member_has_source_and_lane(tmp_path, monkeypatch):
    observe = [
        _row("AAA/USDT", "base"),
        _row("DDD/USDT", "cmc_trending"),
    ]
    trade = [observe[0]]
    logged: list[str] = []
    monkeypatch.setattr(
        "services.universe.source_tags.log",
        lambda msg, level="INFO": logged.append(msg),
    )
    members = publish_cycle_tags(
        observe,
        trade,
        open_symbols=set(),
        base_symbols={"AAA/USDT"},
        config=_shadow(),
        tenant_id="tenant/../x",
        data_root=str(tmp_path),
        overlays=[[observe[1]]],
    )
    assert members is not None
    assert len(members) == 2
    for member in members:
        assert str(member["source"]).strip()
        assert member["lane"] in {"observe", "trade"}
        assert member["lane"]
    path = Path(members_artifact_path("tenant/../x", data_root=str(tmp_path)))
    assert path.is_file()
    assert ".." not in path.parts
    resolved = path.resolve()
    assert str(tmp_path.resolve()) in str(resolved)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["behavior_change"] is False
    assert payload["mode"] == "shadow"
    assert payload["counts"]["tagged"] == payload["counts"]["members"] == 2
    assert payload["counts"]["errors"] == 0
    assert "rate" not in payload["counts"]
    for member in payload["members"]:
        assert member["source"]
        assert member["lane"] in {"observe", "trade"}

    # Overwrite, do not append.
    other = _row("JJJ/USDT", "cmc_trending")
    publish_cycle_tags(
        [other],
        [],
        open_symbols=set(),
        base_symbols=set(),
        config=_shadow(),
        tenant_id="tenant/../x",
        data_root=str(tmp_path),
        overlays=[[other]],
    )
    again = json.loads(path.read_text(encoding="utf-8"))
    assert [m["symbol"] for m in again["members"]] == ["JJJ/USDT"]
    assert any(
        line.startswith("universe_early_trend counts ") and "errors=0" in line
        for line in logged
    )


# --- T3 ---


def test_t3_formatter_and_log_show_source_without_telegram(monkeypatch):
    coin = _row("QNT/USDT", "gate_prev_top")
    tags = {
        coin["symbol"]: {
            "symbol": coin["symbol"],
            "lane": "trade",
            "source": "gainer_prev",
            "also": [],
        }
    }
    from unittest.mock import patch

    with patch(
        "notifications.telegram_commands.watchlist_commands.send_telegram_message"
    ) as send:
        msg = format_watchlist_message(
            [{"symbol": coin["symbol"], "name": "Quant", "active": True}],
            source_tags=tags,
        )
        send.assert_not_called()
    assert "gainer_prev" in msg
    assert "trade/gainer_prev" in msg
    line = format_cycle_log_line(tags[coin["symbol"]])
    assert "source=gainer_prev" in line
    assert "lane=trade" in line
    counts = format_counts_line([tags[coin["symbol"]]])
    assert "%" not in counts
    assert "rate" not in counts
    assert "members=1" in counts
    assert "errors=0" in counts


# --- T4 ---


def test_t4_no_risk_manager_or_fill_path():
    text = (REPO / "services" / "universe" / "source_tags.py").read_text(encoding="utf-8")
    for banned in (
        "RiskManager",
        "risk_manager",
        "place_order",
        "allow_live",
        "live.execution",
    ):
        assert banned not in text
    bot = (REPO / "aria_bot.py").read_text(encoding="utf-8")
    assert "publish_cycle_tags(" in bot
    assert "trade_watchlist = publish_cycle_tags" not in bot
    assert "observe_watchlist = publish_cycle_tags" not in bot


# --- T5 ---


def test_t5_safety_config_pins():
    cfg = json.loads((REPO / "config.json").read_text(encoding="utf-8"))
    section = cfg["universe_early_trend"]
    assert section["mode"] == "shadow"
    assert section["behavior_change"] is False
    assert cfg["live"]["execution"] == "shadow"
    assert cfg["live"]["dry_run"] is True
    assert cfg["shorts"]["allow_live"] is False
    # #614 climax-fade stays configured; #637 cascade fire stays off.
    assert cfg["shorts"]["climax_fade"]["enabled"] is True
    assert cfg["exit_realtime"]["cascade"]["fire_enabled"] is False
    assert "api_key" not in cfg["universe_early_trend"]
    assert "api_secret" not in cfg["universe_early_trend"]


def test_overlay_order_follows_existing_builders(monkeypatch):
    monkeypatch.setattr("data_manager.uses_watchlist_expansion", lambda config=None: True)
    monkeypatch.setattr("data_manager.is_dry_run_enhanced", lambda config=None: True)
    monkeypatch.setattr(
        "data_manager.trending_watchlist_live_enabled", lambda config=None: True
    )
    monkeypatch.setattr(
        "core.coin_eligibility.should_include_trending_overlay", lambda cfg: True
    )
    monkeypatch.setattr(
        "data_manager.load_dry_run_expansion",
        lambda: {"coins": [_row("A/USDT", "dry_run_expansion")]},
    )
    monkeypatch.setattr(
        "data_manager.load_dry_run_overlay",
        lambda: {"coins": [_row("D/USDT", "dry_run_overlay")]},
    )
    monkeypatch.setattr(
        "data_manager.load_cmc_trending_overlay",
        lambda: {"coins": [_row("C/USDT", "cmc_trending")]},
    )
    monkeypatch.setattr(
        "services.gainer_universe.config.gainer_universe_enabled",
        lambda config=None: True,
    )
    monkeypatch.setattr(
        "services.gainer_universe.config.gainer_universe_config",
        lambda config=None: {},
    )
    monkeypatch.setattr(
        "services.gainer_universe.store.load_gainer_state",
        lambda: {"live_top": [{"symbol": "L/USDT"}], "eligible": []},
    )
    monkeypatch.setattr(
        "services.gainer_universe.inject.expand_candidates_for_trade",
        lambda state, cfg: [_row("G/USDT", "gate_prev_top")],
    )
    lists, errors = collect_overlay_lists(
        {"gainer_universe": {"enabled": True, "mode": "shadow"}}
    )
    assert errors == 0
    symbols = [[c["symbol"] for c in lst] for lst in lists]
    assert symbols == [
        ["A/USDT"],
        ["D/USDT"],
        ["C/USDT"],
        ["G/USDT"],
        ["L/USDT"],
    ]


def test_expand_source_kept_ahead_of_live_top():
    """Gainer inject overwrites live-top source with the eligible expand source."""
    prev = _row("FFF/USDT", "gate_prev_top")
    live = {"symbol": "FFF/USDT", "source": "gainer_live_top"}
    tagged = tag_universe_members(
        [prev],
        [prev],
        base_symbols=set(),
        open_symbols=set(),
        overlays=[[prev], [live]],
    )
    assert tagged[0]["source"] == "gainer_prev"
    assert "discovery" in tagged[0]["also"]


def _capture_logs(monkeypatch) -> list[tuple[str, str]]:
    logged: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "services.universe.source_tags.log",
        lambda msg, level="INFO": logged.append((level, msg)),
    )
    return logged


def test_counts_line_is_info_member_lines_are_debug(tmp_path, monkeypatch):
    """Per-cycle count stays INFO. Per-coin lines are DEBUG; the JSON has them."""
    logged = _capture_logs(monkeypatch)
    base = _row("AAA/USDT", "base")
    cmc = _row("BBB/USDT", "cmc_trending")
    members = publish_cycle_tags(
        [base, cmc],
        [base],
        open_symbols=set(),
        base_symbols={"AAA/USDT"},
        config=_shadow(),
        tenant_id="sUnivSrc",
        data_root=str(tmp_path),
        overlays=[[cmc]],
    )
    assert members is not None and len(members) == 2
    info = [msg for level, msg in logged if level == "INFO"]
    debug = [msg for level, msg in logged if level == "DEBUG"]
    assert info == [
        "universe_early_trend counts members=2 observe=1 trade=1 tagged=2 errors=0"
    ]
    assert any("symbol=AAA/USDT" in line and "source=base" in line for line in debug)
    assert any("symbol=BBB/USDT" in line and "source=cmc_trending" in line for line in debug)
    assert not any("symbol=" in line for line in info)


def test_publish_failure_is_warning_not_debug(tmp_path, monkeypatch):
    logged = _capture_logs(monkeypatch)

    def _boom(*args, **kwargs):
        raise RuntimeError("tagger down")

    monkeypatch.setattr("services.universe.source_tags.tag_universe_members", _boom)
    result = publish_cycle_tags(
        [_row("AAA/USDT", "base")],
        [_row("AAA/USDT", "base")],
        open_symbols=set(),
        base_symbols={"AAA/USDT"},
        config=_shadow(),
        tenant_id="sUnivSrc",
        data_root=str(tmp_path),
        overlays=[],
    )
    assert result is None
    warnings = [msg for level, msg in logged if level == "WARNING"]
    assert any("universe_early_trend tag skip:" in msg for msg in warnings)
    assert not any(
        level == "DEBUG" and "tag skip:" in msg for level, msg in logged
    )
    assert not any(level == "INFO" and msg.startswith("universe_early_trend counts ") for level, msg in logged)
    assert not any(Path(tmp_path).rglob(_ARTIFACT))


def test_feeder_failures_warn_and_count_errors(tmp_path, monkeypatch):
    logged = _capture_logs(monkeypatch)

    def _boom(*args, **kwargs):
        raise RuntimeError("feeder down")

    monkeypatch.setattr("data_manager.uses_watchlist_expansion", lambda config=None: True)
    monkeypatch.setattr("data_manager.is_dry_run_enhanced", lambda config=None: False)
    monkeypatch.setattr(
        "data_manager.trending_watchlist_live_enabled", lambda config=None: False
    )
    monkeypatch.setattr("data_manager.load_dry_run_expansion", _boom)
    monkeypatch.setattr(
        "services.gainer_universe.config.gainer_universe_enabled",
        lambda config=None: True,
    )
    monkeypatch.setattr("services.gainer_universe.store.load_gainer_state", _boom)

    lists, errors = collect_overlay_lists({})
    assert lists == []
    assert errors == 2
    assert any(
        level == "WARNING" and "watchlist feeders skip:" in msg for level, msg in logged
    )
    assert any(
        level == "WARNING" and "gainer feeders skip:" in msg for level, msg in logged
    )
    assert not any(level == "DEBUG" and "feeders skip:" in msg for level, msg in logged)

    coin = _row("AAA/USDT", "base")
    members = publish_cycle_tags(
        [coin],
        [coin],
        open_symbols=set(),
        base_symbols={"AAA/USDT"},
        config=_shadow(),
        tenant_id="sUnivSrc",
        data_root=str(tmp_path),
        overlays=None,
    )
    assert members is not None
    assert members[0]["source"] == "base"
    info = [msg for level, msg in logged if level == "INFO"]
    assert len(info) == 1
    assert "errors=2" in info[0]
    path = Path(members_artifact_path("sUnivSrc", data_root=str(tmp_path)))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["counts"]["errors"] == 2


def test_base_watchlist_failure_warns_and_counts_errors(tmp_path, monkeypatch):
    logged = _capture_logs(monkeypatch)

    def _boom(*args, **kwargs):
        raise RuntimeError("watchlist down")

    monkeypatch.setattr("data_manager.load_watchlist", _boom)
    monkeypatch.setattr("data_manager.uses_watchlist_expansion", lambda config=None: False)
    monkeypatch.setattr("data_manager.is_dry_run_enhanced", lambda config=None: False)
    monkeypatch.setattr(
        "data_manager.trending_watchlist_live_enabled", lambda config=None: False
    )
    monkeypatch.setattr(
        "services.gainer_universe.config.gainer_universe_enabled",
        lambda config=None: False,
    )
    coin = _row("AAA/USDT", "manual")
    members = publish_cycle_tags(
        [coin],
        [],
        open_symbols=set(),
        base_symbols=None,
        config=_shadow(),
        tenant_id="henry",
        data_root=str(tmp_path),
        overlays=None,
    )
    assert members is not None
    assert members[0]["source"] == "discovery"
    assert any(
        level == "WARNING" and "base set skip:" in msg for level, msg in logged
    )
    info = [msg for level, msg in logged if level == "INFO"]
    assert len(info) == 1 and "errors=1" in info[0]


def test_aria_tag_skip_is_warning():
    bot = (REPO / "aria_bot.py").read_text(encoding="utf-8")
    assert 'log(f"universe_early_trend tag skip: {e}", "WARNING")' in bot
    text = (REPO / "services" / "universe" / "source_tags.py").read_text(encoding="utf-8")
    assert 'tag skip: {e}", "DEBUG"' not in text
    assert 'feeders skip: {e}", "DEBUG"' not in text
    assert 'base set skip: {e}", "DEBUG"' not in text


_DECISION_ROOTS = (
    REPO / "risk",
    REPO / "strategies",
    REPO / "execution",
    REPO / "services" / "venue_quality.py",
)
_TAG_MARKERS = (
    "services.universe.source_tags",
    "universe.source_tags",
    "universe_members",
    "universe_early_trend",
    "publish_cycle_tags",
    "list_formatter_tags",
    "tag_universe_members",
)


def test_risk_and_buy_code_do_not_read_source_lane_tags():
    """Tags are display-only. Risk, strategy, and execution code must not read them.

    Operational coin[\"source\"] (chase guard and the like) is a different field
    and is not this artifact.
    """
    offenders: list[str] = []
    files: list[Path] = []
    for root in _DECISION_ROOTS:
        if root.is_file():
            files.append(root)
        else:
            files.extend(p for p in root.rglob("*.py") if p.is_file())
    assert files, "decision trees missing"
    for path in files:
        text = path.read_text(encoding="utf-8")
        for marker in _TAG_MARKERS:
            if marker in text:
                offenders.append(f"{path.relative_to(REPO)} contains {marker}")
    assert offenders == []


def test_overlay_loaders_shared_base_watchlist_is_tenant_scoped(tmp_path, monkeypatch):
    """Overlay files are process-shared, same as build_merged_watchlist_coins.

    The base watchlist is tenant-scoped, so henry's tags load henry's watchlist.
    """
    import inspect

    import data_manager
    from services.gainer_universe.store import load_gainer_state

    doc = collect_overlay_lists.__doc__ or ""
    for snippet in (
        "data/watchlist.dry_run_expansion.json",
        "data/watchlist.dry_run_overlay.json",
        "data/watchlist.cmc_trending_overlay.json",
        "gainer_universe_state.json",
        "load_watchlist(tenant_id)",
    ):
        assert snippet in doc
    for fn in (
        data_manager.load_dry_run_expansion,
        data_manager.load_dry_run_overlay,
        data_manager.load_cmc_trending_overlay,
        load_gainer_state,
    ):
        assert "tenant_id" not in inspect.signature(fn).parameters

    seen: dict[str, str | None] = {}

    def _load(tenant_id=None):
        seen["tenant_id"] = tenant_id
        return [{"symbol": "AAA/USDT", "source": "base"}]

    monkeypatch.setattr("data_manager.load_watchlist", _load)
    members = publish_cycle_tags(
        [_row("AAA/USDT", "base")],
        [_row("AAA/USDT", "base")],
        open_symbols=set(),
        base_symbols=None,
        config=_shadow(),
        tenant_id="henry",
        data_root=str(tmp_path),
        overlays=[],
    )
    assert seen["tenant_id"] == "henry"
    assert members is not None
    assert members[0]["source"] == "base"
    path = Path(members_artifact_path("henry", data_root=str(tmp_path)))
    assert path.is_file()
    assert path.parent.name == "henry"
