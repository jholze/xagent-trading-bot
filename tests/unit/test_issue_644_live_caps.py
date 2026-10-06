"""Issue #644 — live caps overlay, daily loss USDT, human-buy source.

T1–T15 plus R6a and the sell-lock regression (is_manual_source unchanged).
Tenant ids and symbols here are test data.
"""

from __future__ import annotations

import copy
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import data_manager
from core.config import BotConfig
from core.models import RiskDecision, TradeOrder
from core.tenant_context import tenant_context
from core.time_utils import operator_tz
from core.trading_profiles import apply_effective_config
from risk.risk_manager import RiskManager
from scripts.apply_live_caps_overlay import apply_live_caps, load_overlay, redact_secrets
from services.trading_service import DEFAULT_ORDER_SOURCE, TradingService, coerce_order_source
from services.venue_quality import VenueMetrics
from strategies.dca_policy import is_human_operator_buy
from strategies.position_lock import MODE_NO_AUTO_SELL, auto_sell_blocked, build_lock
from strategies.positions import (
    _activate,
    _ensure_store,
    _position_stores,
    _resolve_store_key,
    clear_positions_memory,
    count_open_full_slots,
    get_key,
)
from tests.unit.test_long_mcap_venue_563 import _eval_env

_REPO = Path(__file__).resolve().parents[2]
_OVERLAY = _REPO / "deploy" / "live_caps_overlay.json"
_DISK = _REPO / "config.json"

_LIQ = {
    "min_quote_volume_24h_usdt": 500000,
    "depth_window_pct": 0.5,
    "order_book_cache_ttl_sec": 15,
}
_CAP_KEYS = (
    "max_usdt_per_trade",
    "max_open_positions",
    "max_daily_loss_usdt",
)
_FALSE_HUMAN = (
    "manual_x",
    "manualbuy",
    "operator",
    "confirm",
    "user",
    "telegram",
    "mcp:manual",
    "",
    None,
)
_T8_SOURCES = (
    "entry_sensor_15m",
    "gainer_relvol",
    "cmc",
    "auto",
    "lc",
    "dca",
    "dca_sniper",
    "grid",
    "deploy_boost",
    "manual",
)

_THICK = VenueMetrics(
    symbol="AAA/USDT",
    quote_volume_24h_usdt=5_000_000.0,
    last=1.0,
    bid=0.999,
    ask=1.001,
    bid_size=100_000.0,
    ask_size=100_000.0,
    spread_pct=0.2,
    top_book_bid_usdt=100_000.0,
    top_book_ask_usdt=100_000.0,
    capture="ok",
    depth_bid_usdt=100_000.0,
    depth_ask_usdt=100_000.0,
    depth_parsed=True,
    quote_volume_present=True,
)
_THIN = VenueMetrics(
    symbol="AAA/USDT",
    quote_volume_24h_usdt=100_000.0,
    last=1.0,
    bid=0.999,
    ask=1.001,
    bid_size=100_000.0,
    ask_size=100_000.0,
    spread_pct=0.2,
    top_book_bid_usdt=100_000.0,
    top_book_ask_usdt=100_000.0,
    capture="ok",
    depth_bid_usdt=100_000.0,
    depth_ask_usdt=100_000.0,
    depth_parsed=True,
    quote_volume_present=True,
)


def _disk_config() -> dict:
    return json.loads(_DISK.read_text(encoding="utf-8"))


def _cfg(**over) -> BotConfig:
    raw = {
        "max_usdt_per_trade": 1000,
        "max_open_positions": 50,
        "max_position_percent": 100,
        "trade_cooldown_hours": 0,
        "max_daily_trades": 0,
        "trading_mode": "paper",
        "trading": {"entries_enabled": True, "exits_enabled": True},
        "live": {"dry_run": True, "execution": "shadow", "max_usdt_per_trade": 1000},
        "shorts": {"enabled": True, "allow_live": False, "max_open": 6, "max_margin_pct": 80},
        "risk": {
            "min_trade_usdt": 5,
            "max_daily_loss_pct": 0,
            "cash_floor_pct": 0,
            "cash_policy": {"enabled": False},
            "position_capacity": {"enabled": False},
            "liquidity_guard": dict(_LIQ),
        },
        "sell_policy": {"rotation": {"tail_exempt_notional_usdt": 0}},
        "observability": {"operator_timezone": _disk_config()["observability"]["operator_timezone"]},
    }
    raw.update(over)
    if "risk" in over:
        raw["risk"] = {**raw["risk"], **over["risk"]} if False else over["risk"]
    # ``over`` already replaced top-level keys. Nested risk must keep the guard
    # unless the caller passed a full risk block.
    return BotConfig(raw)


def _risk_cfg(usdt=None, pct=0.0, **over) -> BotConfig:
    risk = {
        "min_trade_usdt": 5,
        "max_daily_loss_pct": pct,
        "cash_floor_pct": 0,
        "cash_policy": {"enabled": False},
        "position_capacity": {"enabled": False},
        "liquidity_guard": dict(_LIQ),
        "fail_closed_guards": "log",
    }
    raw = {
        "max_usdt_per_trade": 1000,
        "max_open_positions": 50,
        "max_position_percent": 100,
        "trade_cooldown_hours": 0,
        "trading_mode": "paper",
        "trading": {"entries_enabled": True, "exits_enabled": True},
        "live": {"dry_run": True, "execution": "shadow"},
        "shorts": {"enabled": True, "allow_live": False, "max_open": 6, "max_margin_pct": 80},
        "risk": risk,
        "observability": {"operator_timezone": _disk_config()["observability"]["operator_timezone"]},
    }
    if usdt is not None:
        raw["max_daily_loss_usdt"] = usdt
    raw.update(over)
    return BotConfig(raw)


def _buy(usdt=100.0, source="auto", signal="BUY") -> TradeOrder:
    return TradeOrder(
        type="BUY",
        symbol="AAA/USDT",
        price=1.0,
        amount=0,
        usdt_amount=usdt,
        signal=signal,
        source=source or "",
    )


def _sell() -> TradeOrder:
    return TradeOrder(
        type="SELL", symbol="AAA/USDT", price=1.0, amount=100, signal="SELL", source="auto"
    )


@contextmanager
def _sell_passes(rm):
    """History and partial-sell gates stay out of the R2/R3 assertion."""
    empty = {"trades": [], "virtual_balance": 100_000.0}
    with patch("risk.risk_manager.load_trade_history", return_value=empty), patch(
        "risk.risk_manager.load_live_trade_history", return_value=empty
    ), patch("data_manager.load_orders", return_value={"orders": []}), patch(
        "data_manager.load_positions_document", return_value={"positions": {}}
    ), patch(
        "strategies.position_lock.attach_lock_from_ledger",
        side_effect=lambda pos, *args, **kwargs: pos,
    ), patch.object(rm, "_partial_sell_blocked", return_value=(False, "")), patch.object(
        rm, "_trade_cooldown_blocked", return_value=(False, "")
    ), patch("risk.risk_manager.get_position", return_value={"amount": 100, "average_entry": 1.0}), patch(
        "risk.risk_manager.find_open_position_for_symbol", return_value=None
    ):
        yield


def _short(source="auto") -> TradeOrder:
    return TradeOrder(
        type="SHORT",
        symbol="AAA/USDT",
        price=1.0,
        amount=0,
        usdt_amount=100.0,
        signal="SHORT",
        source=source if source is not None else "",
    )


@contextmanager
def _quiet_loss(rm, *, realized_24h=0.0, figure=0.0, history=None):
    hist = history if history is not None else {}
    with patch.object(rm, "_trailing_24h_realized_pnl", return_value=realized_24h), patch.object(
        rm, "_daily_loss_usdt_figure", return_value=figure
    ), patch.object(rm, "_risk_history_load", return_value=hist), patch.object(
        rm, "_risk_history_save", side_effect=lambda data: hist.update(data)
    ), patch("core.operator_notify.notify_operator", return_value=True), patch(
        "risk.risk_manager._DAILY_LOSS_HALT_UNTIL", None
    ):
        yield hist


def _filled_sell(pnl: float, when: datetime) -> dict:
    return {
        "status": "filled",
        "side": "sell",
        "pnl": pnl,
        "timestamps": {"filled": when.isoformat()},
        "execution": {"usdt": 10, "price": 1, "amount": 10},
        "request": {"usdt": 10, "price": 1, "amount": 10},
    }


class _FakeCollection:
    def __init__(self):
        self.docs: dict[str, dict] = {}

    def find_one(self, flt):
        doc = self.docs.get(flt["tenant_id"])
        return copy.deepcopy(doc) if doc else None

    def update_one(self, flt, update, upsert=False):
        tid = flt["tenant_id"]
        doc = self.docs.get(tid)
        if doc is None:
            if not upsert:
                return
            doc = {"tenant_id": tid}
            self.docs[tid] = doc
        for path, value in update.get("$set", {}).items():
            _set_path(doc, path, copy.deepcopy(value))

    def replace_one(self, flt, doc, upsert=False):
        self.docs[flt["tenant_id"]] = copy.deepcopy(doc)


class _FakeDb(dict):
    def __missing__(self, key):
        coll = _FakeCollection()
        self[key] = coll
        return coll


def _set_path(doc: dict, path: str, value) -> None:
    parts = path.split(".")
    cur = doc
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


@pytest.fixture
def caps_store(monkeypatch, tmp_path):
    """In-memory tenant config store. Disk ``config.json`` stays the base."""
    from storage import tenant_meta_store as tms

    db = _FakeDb()
    history = tmp_path / "config_history"
    orig_resolve = data_manager.resolve_data_path

    def _resolve(name):
        if name == "config_history":
            return str(history)
        return orig_resolve(name)

    monkeypatch.setattr(data_manager, "_should_use_mongo_for_tenant_config", lambda cfg=None: True)
    monkeypatch.setattr(data_manager, "_mongo_test_mode", lambda cfg=None: True)
    monkeypatch.setattr(tms, "get_database", lambda test=False, config=None: db)
    monkeypatch.setattr(data_manager, "resolve_data_path", _resolve)
    return db


def _body(db, tid) -> dict:
    from storage import tenant_meta_store as tms

    doc = db[tms.TENANT_CONFIGS_COLL].docs.get(tid) or {}
    return doc.get("body") or {}


# --- T1 / T2 / T6 -----------------------------------------------------------


def test_t1_overlay_binds_r1_and_leaves_runtime_flags(caps_store):
    before = _DISK.read_bytes()
    tid = "henry"
    effective = apply_live_caps(tid, _OVERLAY)
    assert _DISK.read_bytes() == before
    assert effective["max_usdt_per_trade"] == 100
    assert effective["max_open_positions"] == 4
    assert effective["max_daily_loss_usdt"] == 50
    assert "live_max_loss_usdt" not in effective
    assert "live_max_loss_usdt" not in load_overlay(_OVERLAY)
    assert effective["trading"]["entries_enabled"] is False
    assert effective["live"]["max_usdt_per_trade"] == 100
    assert effective["live"]["dry_run"] is True
    assert effective["live"]["execution"] == "shadow"
    assert effective["shorts"]["allow_live"] is False
    assert effective["hermes"]["enabled"] is False
    assert effective["exit_realtime"]["cascade"]["fire_enabled"] is False
    assert effective["mcp"]["allow_live"] is False
    assert effective["risk"]["liquidity_guard"]["min_quote_volume_24h_usdt"] == 500000
    assert effective["risk"]["position_capacity"]["enabled"] is False
    assert effective["risk"]["position_capacity"]["base"] == _disk_config()["risk"]["position_capacity"]["base"]
    assert effective["risk"]["slot_eviction"]["mode"] in ("shadow", "off")
    rm = RiskManager(BotConfig(effective))
    assert rm._base_usdt_cap() == 100
    stored = _body(caps_store, tid)
    for key in _CAP_KEYS:
        assert key in stored
    assert "dry_run" not in stored.get("live", {})
    assert "execution" not in stored.get("live", {})
    assert stored["risk"]["position_capacity"]["enabled"] is False
    assert stored["risk"]["slot_eviction"]["mode"] in ("shadow", "off")


def test_t2_default_and_ctexp_match_freeze_base():
    """No overlay and an empty body stay on the disk merge.

    A pinned hash of the whole file breaks when staging adds unrelated keys.
    The freeze is the operator document itself: no profile, no overlay keys,
    and the #645 liquidity floor left as it is.
    """
    disk = _disk_config()
    base = apply_effective_config(disk, None)
    assert not disk.get("trading_profile")
    assert base == disk
    assert apply_effective_config(disk, {}) == base
    assert apply_effective_config(disk, None) == base
    assert "max_daily_loss_usdt" not in disk
    assert "max_daily_loss_usdt" not in base
    assert disk["live"]["dry_run"] is True
    assert disk["live"]["execution"] == "shadow"
    assert disk["shorts"]["allow_live"] is False
    assert disk["hermes"]["enabled"] is False
    assert disk["exit_realtime"]["cascade"]["fire_enabled"] is False
    assert disk["mcp"]["allow_live"] is False
    assert disk["risk"]["liquidity_guard"]["min_quote_volume_24h_usdt"] == 500000
    overlay = load_overlay(_OVERLAY)
    merged = apply_effective_config(disk, overlay)
    assert merged["max_usdt_per_trade"] == overlay["max_usdt_per_trade"]
    assert merged["max_usdt_per_trade"] != disk["max_usdt_per_trade"]
    assert merged["live"]["dry_run"] is True
    assert merged["live"]["execution"] == "shadow"
    assert merged["risk"]["liquidity_guard"]["min_quote_volume_24h_usdt"] == 500000
    assert base["max_usdt_per_trade"] == disk["max_usdt_per_trade"]


def test_t6_apply_is_idempotent(caps_store):
    from storage.config_history import list_snapshots

    tid = "tenant-overlay"
    first = apply_live_caps(tid, _OVERLAY)
    snaps = list_snapshots(tid)
    assert len(snaps) == 1
    second = apply_live_caps(tid, _OVERLAY)
    assert redact_secrets(second) == redact_secrets(first)
    assert len(list_snapshots(tid)) == 1
    assert first["max_usdt_per_trade"] == 100
    assert first["max_daily_loss_usdt"] == 50
    assert "api_key" not in json.dumps(redact_secrets(first)).lower() or "[redacted]" in json.dumps(
        redact_secrets(first)
    )


def test_overlay_file_has_no_tenant_or_runtime_flags():
    text = _OVERLAY.read_text(encoding="utf-8")
    overlay = json.loads(text)
    assert "henry" not in text.lower()
    assert "dry_run" not in overlay.get("live", {})
    assert "execution" not in overlay.get("live", {})
    blob = json.dumps(overlay)
    assert "allow_live" not in blob
    assert "fire_enabled" not in blob


# --- T3 / T4 / T9 -----------------------------------------------------------


def test_t3_usdt_unset_keeps_pct_path_and_skips_the_new_figure():
    history = {}
    rm = RiskManager(_risk_cfg(usdt=None, pct=5.0))

    def _boom():
        raise AssertionError("USDT figure must not run when the key is unset")

    with patch.object(rm, "_trailing_24h_realized_pnl", return_value=-6_000.0), patch.object(
        rm, "_daily_loss_usdt_figure", side_effect=_boom
    ), patch.object(rm, "_portfolio_equity", return_value=100_000.0), patch.object(
        rm, "_initial_capital", return_value=100_000.0
    ), patch.object(rm, "_risk_history_load", return_value=history), patch.object(
        rm, "_risk_history_save", side_effect=lambda data: history.update(data)
    ), patch("core.operator_notify.notify_operator", return_value=True), patch(
        "risk.risk_manager._DAILY_LOSS_HALT_UNTIL", None
    ):
        denied = rm._daily_loss_limit_blocked(_buy())
    assert denied is not None
    assert denied.code == "daily_loss_limit"
    assert "realized 24h" in denied.message
    assert "day pnl" not in denied.message

    rm0 = RiskManager(_risk_cfg(usdt=None, pct=0))
    with patch.object(rm0, "_daily_loss_usdt_figure", side_effect=_boom), patch.object(
        rm0, "_trailing_24h_realized_pnl", side_effect=_boom
    ), patch("risk.risk_manager._DAILY_LOSS_HALT_UNTIL", None):
        assert rm0._daily_loss_limit_blocked(_buy()) is None


def test_t4_usdt_threshold_and_stricter_of_pct_and_usdt():
    rm = RiskManager(_risk_cfg(usdt=50, pct=0))
    with _quiet_loss(rm, figure=-49.99), _eval_env(rm, metrics=_THICK, mcap=50_000_000):
        allowed = rm.evaluate(_buy(), "4h", source="auto")
    assert allowed.approved is True, allowed.message
    assert allowed.code != "daily_loss_limit"

    with _quiet_loss(rm, figure=-50.0):
        blocked = rm.evaluate(_buy(), "4h", source="auto")
    assert blocked.approved is False
    assert blocked.code == "daily_loss_limit"
    sell_rm = RiskManager(_risk_cfg(usdt=50, pct=0))
    with _quiet_loss(sell_rm, figure=-50.0), _sell_passes(sell_rm):
        sold = sell_rm.evaluate(_sell(), "4h", source="auto")
    assert sold.approved is True, sold.message
    assert sold.code != "daily_loss_limit"

    # Pct does not breach; USDT does. USDT wins.
    usdt_wins = RiskManager(_risk_cfg(usdt=50, pct=5.0))
    with _quiet_loss(usdt_wins, realized_24h=-1_000.0, figure=-50.0), patch.object(
        usdt_wins, "_portfolio_equity", return_value=100_000.0
    ), patch.object(usdt_wins, "_initial_capital", return_value=100_000.0):
        dec = usdt_wins._daily_loss_limit_blocked(_buy())
    assert dec is not None and dec.code == "daily_loss_limit"
    assert "day pnl" in dec.message

    # USDT does not breach; pct does. Pct wins, and the old message stays.
    pct_wins = RiskManager(_risk_cfg(usdt=50, pct=5.0))
    with _quiet_loss(pct_wins, realized_24h=-6_000.0, figure=-10.0), patch.object(
        pct_wins, "_portfolio_equity", return_value=100_000.0
    ), patch.object(pct_wins, "_initial_capital", return_value=100_000.0):
        dec = pct_wins._daily_loss_limit_blocked(_buy())
    assert dec is not None and dec.code == "daily_loss_limit"
    assert "realized 24h" in dec.message


def test_t9_open_loss_counts_and_operator_day_boundary():
    rm = RiskManager(_risk_cfg(usdt=50, pct=0))
    with _quiet_loss(rm, figure=-50.0):
        blocked = rm.evaluate(_buy(), "4h", source="auto")
    assert blocked.code == "daily_loss_limit"
    with _quiet_loss(rm, figure=-49.99), _eval_env(rm, metrics=_THICK, mcap=50_000_000):
        allowed = rm.evaluate(_buy(), "4h", source="auto")
    assert allowed.approved is True, allowed.message

    zone = operator_tz()
    day = datetime(2026, 1, 15, 12, 0, tzinfo=zone)
    late = datetime(2026, 1, 15, 23, 59, tzinfo=zone)
    midnight = datetime(2026, 1, 16, 0, 0, tzinfo=zone)
    orders = [_filled_sell(-30.0, late), _filled_sell(-70.0, midnight)]
    with patch(
        "services.order_service.OrderService._scoped_orders_newest_first",
        return_value=orders,
    ):
        same_day = rm._realized_pnl_operator_day(day.astimezone(timezone.utc))
        assert same_day == pytest.approx(-30.0)
        next_day = rm._realized_pnl_operator_day(midnight.astimezone(timezone.utc))
        assert next_day == pytest.approx(-70.0)

    calls = []

    def _lots(tenant_id=None, scope=None):
        calls.append((tenant_id, scope))
        return [
            {
                "symbol": "AAA/USDT",
                "amount": 10.0,
                "average_entry": 10.0,
                "current_price": 5.0,
                "side": "long",
            },
            {
                "symbol": "BBB/USDT",
                "amount": 10.0,
                "average_entry": 10.0,
                "current_price": 1.0,
                "shadow": True,
            },
        ]

    with patch("strategies.positions.list_active_positions", side_effect=_lots):
        unreal = rm._open_live_unrealized_pnl()
    assert calls and calls[0][1] == "live"
    assert unreal == pytest.approx(-50.0)


# --- T5 ---------------------------------------------------------------------


def _live_cfg(dry_run):
    return _risk_cfg(
        usdt=None,
        pct=0,
        trading_mode="live",
        live={"dry_run": dry_run, "execution": "shadow", "max_usdt_per_trade": 100},
        max_usdt_per_trade=100,
        max_open_positions=4,
        max_daily_loss_usdt=50,
    )


def _arm_tenant_body(monkeypatch, body, *, tenant="tenant-a", multi=True, boom=False):
    import inspect

    monkeypatch.setattr("core.tenant_context.multi_tenant_enabled", lambda: multi)
    monkeypatch.setattr("core.tenant_context.resolve_tenant_id", lambda tenant_id=None: tenant)

    def _load(tid, default_cfg):
        if boom and any(fr.function == "_tenant_override_for_caps" for fr in inspect.stack()):
            raise RuntimeError("store down")
        return body

    empty_hist = {"trades": [], "virtual_balance": 100_000.0}

    class _NoNetColl:
        def find_one(self, *args, **kwargs):
            return None

        def find(self, *args, **kwargs):
            return []

        def update_one(self, *args, **kwargs):
            return None

        def insert_one(self, *args, **kwargs):
            return None

        def replace_one(self, *args, **kwargs):
            return None

    class _NoNetDb(dict):
        def __missing__(self, key):
            coll = _NoNetColl()
            self[key] = coll
            return coll

        def command(self, *args, **kwargs):
            return {"ok": 1}

    monkeypatch.setattr("storage.mongo_client.get_database", lambda *a, **k: _NoNetDb())
    monkeypatch.setattr(data_manager, "_load_default_config_from_disk", lambda: {"demo": {"backend": "mongo"}})
    monkeypatch.setattr(data_manager, "_should_use_mongo_for_tenant_config", lambda cfg=None: True)
    monkeypatch.setattr(data_manager, "_load_tenant_config_body", _load)
    monkeypatch.setattr(data_manager, "load_trade_history_document", lambda *a, **k: dict(empty_hist))
    monkeypatch.setattr(data_manager, "load_orders", lambda *a, **k: {"orders": []})
    monkeypatch.setattr(data_manager, "load_positions_document", lambda *a, **k: {"positions": {}})
    monkeypatch.setattr("risk.risk_manager.load_trade_history", lambda *a, **k: dict(empty_hist))
    monkeypatch.setattr("risk.risk_manager.load_live_trade_history", lambda *a, **k: dict(empty_hist))


@pytest.mark.parametrize("missing", _CAP_KEYS)
def test_t5_missing_cap_blocks_buy_only(monkeypatch, missing):
    body = {key: 1 for key in _CAP_KEYS}
    body.pop(missing)
    _arm_tenant_body(monkeypatch, body)
    rm = RiskManager(_live_cfg(False))
    notes = []
    with patch("core.operator_notify.notify_operator", side_effect=lambda text: notes.append(text)), patch(
        "logger.log"
    ), _sell_passes(rm):
        buy = rm.evaluate(_buy(), "4h", source="manual")
        sell = rm.evaluate(_sell(), "4h", source="auto")
    assert buy.approved is False
    assert buy.code == "live_caps_missing"
    assert missing in buy.message
    assert notes and "live_caps_missing" in notes[0]
    assert sell.approved is True, sell.message


@pytest.mark.parametrize(
    "reason",
    ["store", "unknown", "multi_off", "empty"],
)
def test_t5_body_does_not_load_blocks_buy_not_sell(monkeypatch, reason):
    if reason == "store":
        _arm_tenant_body(monkeypatch, None, boom=True)
    elif reason == "unknown":
        _arm_tenant_body(monkeypatch, {"max_usdt_per_trade": 1}, tenant="default")
    elif reason == "multi_off":
        _arm_tenant_body(monkeypatch, {key: 1 for key in _CAP_KEYS}, multi=False)
    else:
        _arm_tenant_body(monkeypatch, {})
    rm = RiskManager(_live_cfg(False))
    with patch("core.operator_notify.notify_operator", return_value=True), patch("logger.log"), _sell_passes(rm):
        buy = rm.evaluate(_buy(), "4h", source="auto")
        sell = rm.evaluate(_sell(), "4h", source="auto")
    assert buy.code == "live_caps_missing"
    assert sell.approved is True, sell.message


def test_t5_all_required_caps_present_and_dry_run_skips_r3(monkeypatch):
    body = {
        "max_usdt_per_trade": 100,
        "max_open_positions": 4,
        "max_daily_loss_usdt": 50,
    }
    _arm_tenant_body(monkeypatch, body)
    rm = RiskManager(_live_cfg(False))
    # _eval_env stubs the capacity snapshot at 100 slots. The live check reads
    # that snapshot, so this case pins it to the configured open cap.
    capped = SimpleNamespace(
        max_open_eff=4, enabled=False, rationale="", factors={}, free_slots=4, regime="NEUTRAL"
    )
    with _eval_env(rm, metrics=_THICK, mcap=50_000_000), patch.object(
        rm, "_resolve_position_capacity", return_value=capped
    ), patch(
        "core.operator_notify.notify_operator", return_value=True
    ) as notify:
        buy = rm.evaluate(_buy(usdt=50), "4h", source="auto")
    assert buy.code != "live_caps_missing"
    assert buy.approved is True, buy.message
    notify.assert_not_called()

    shadow = RiskManager(_live_cfg(True))
    with _eval_env(shadow, metrics=_THICK, mcap=50_000_000):
        ok = shadow.evaluate(_buy(usdt=50), "4h", source="auto")
    assert ok.approved is True, ok.message
    assert ok.code != "live_caps_missing"


def test_t5_inherited_caps_do_not_count(monkeypatch):
    """Effective config has the three keys; the tenant body does not."""
    _arm_tenant_body(monkeypatch, {"trading": {"entries_enabled": False}})
    rm = RiskManager(_live_cfg(False))
    with patch("core.operator_notify.notify_operator", return_value=True), patch("logger.log"):
        buy = rm.evaluate(_buy(), "4h", source="manual")
    assert buy.code == "live_caps_missing"


def _good_cap_body() -> dict:
    return {
        "max_usdt_per_trade": 100,
        "max_open_positions": 4,
        "max_daily_loss_usdt": 50,
    }


def test_b_t7_manual_short_above_cap_is_capped_and_missing_caps_block_shorts(monkeypatch):
    """#642 B T7. A 500 USDT short, including source manual, is not submitted at 500.

    The same ticket cap as buys binds the final notional. The live-cap check
    runs before the short open and blocks when the tenant body has no caps.
    """
    _arm_tenant_body(monkeypatch, _good_cap_body())
    rm = RiskManager(_live_cfg(False))
    order = _short("manual")
    order.usdt_amount = 500
    capped = SimpleNamespace(
        max_open_eff=4, enabled=False, rationale="", factors={}, free_slots=4, regime="NEUTRAL"
    )
    with _eval_env(rm, metrics=_THICK, mcap=80_000_000), patch.object(
        rm, "_resolve_position_capacity", return_value=capped
    ), patch(
        "strategies.positions.list_active_positions", return_value=[]
    ), patch("core.operator_notify.notify_operator", return_value=True):
        dec = rm.evaluate(order, "15m", source="manual")
    assert dec.approved is True, f"{dec.code} {dec.message}"
    assert dec.order.usdt_amount <= 100
    assert dec.order.usdt_amount != pytest.approx(500)

    bare = RiskManager(_live_cfg(False))
    _arm_tenant_body(monkeypatch, {"trading": {"entries_enabled": False}})
    with patch("core.operator_notify.notify_operator", return_value=True), patch("logger.log"):
        blocked = bare.evaluate(_short("manual"), "15m", source="manual")
        cover = TradeOrder(
            type="COVER", symbol="AAA/USDT", price=1.0, amount=1, signal="COVER", source="manual"
        )
        exited = bare.evaluate(cover, "15m", source="manual")
    assert blocked.approved is False
    assert blocked.code == "live_caps_missing"
    assert exited.code != "live_caps_missing"


@pytest.mark.parametrize(
    "key,bad",
    [
        ("max_usdt_per_trade", 0),
        ("max_usdt_per_trade", ""),
        ("max_usdt_per_trade", None),
        ("max_open_positions", 0),
        ("max_open_positions", ""),
        ("max_open_positions", None),
        ("max_daily_loss_usdt", 0),
        ("max_daily_loss_usdt", ""),
        ("max_daily_loss_usdt", None),
    ],
)
def test_b_t8_non_positive_effective_cap_blocks(monkeypatch, key, bad):
    """#642 B T8. 0, empty, and None do not switch the cap off."""
    body = _good_cap_body()
    body[key] = bad
    _arm_tenant_body(monkeypatch, body)
    cfg = _live_cfg(False)
    cfg.raw[key] = bad
    if key == "max_usdt_per_trade":
        cfg.raw["live"]["max_usdt_per_trade"] = bad
    rm = RiskManager(cfg)
    with patch("core.operator_notify.notify_operator", return_value=True), patch("logger.log"):
        dec = rm.evaluate(_buy(usdt=50), "4h", source="manual")
    assert dec.approved is False
    assert dec.code == "live_caps_missing"
    assert key in dec.message


def test_b_t8_live_block_above_top_level_never_trades_the_higher_cap(monkeypatch):
    """#642 B T8. Top-level 100 with 4500 in the live block is not a 4500 ticket."""
    body = _good_cap_body()
    body["live"] = {"max_usdt_per_trade": 4500}
    _arm_tenant_body(monkeypatch, body)
    cfg = _live_cfg(False)
    cfg.raw["max_usdt_per_trade"] = 100
    cfg.raw["live"]["max_usdt_per_trade"] = 4500
    rm = RiskManager(cfg)
    with patch("core.operator_notify.notify_operator", return_value=True), patch("logger.log"):
        buy = rm.evaluate(_buy(usdt=500, source="manual"), "4h", source="manual")
        short = rm.evaluate(_short("manual"), "15m", source="manual")
    assert buy.approved is False
    assert buy.code == "live_caps_missing"
    assert "live.max_usdt_per_trade" in buy.message
    assert short.code == "live_caps_missing"
    assert getattr(buy, "order", None) is None or buy.order.usdt_amount != pytest.approx(4500)


def test_t8_top_level_above_live_cap_blocks(monkeypatch):
    """Top-level 4500 with live cap 100 is the size other paths would send."""
    body = _good_cap_body()
    body["max_usdt_per_trade"] = 4500
    body["live"] = {"max_usdt_per_trade": 100}
    _arm_tenant_body(monkeypatch, body)
    cfg = _live_cfg(False)
    cfg.raw["max_usdt_per_trade"] = 4500
    cfg.raw["live"]["max_usdt_per_trade"] = 100
    rm = RiskManager(cfg)
    with patch("core.operator_notify.notify_operator", return_value=True), patch("logger.log"):
        buy = rm.evaluate(_buy(usdt=100, source="manual"), "4h", source="manual")
        short = rm.evaluate(_short("manual"), "15m", source="manual")
    assert buy.approved is False
    assert buy.code == "live_caps_missing"
    assert "max_usdt_per_trade" in buy.message
    assert "live.max_usdt_per_trade" not in buy.message
    assert short.code == "live_caps_missing"
    assert getattr(buy, "order", None) is None


def test_b_t9_missing_mark_blocks_even_when_switch_is_log():
    """#642 B T9. No mark on an open live lot denies, including fail_closed_guards=log."""
    rm = RiskManager(_risk_cfg(usdt=50, pct=5.0))
    assert rm.config.risk_config.get("fail_closed_guards") == "log"
    lot = {
        "symbol": "AAA/USDT",
        "amount": 2.0,
        "average_entry": 10.0,
        "side": "long",
    }
    with patch.object(rm, "_trailing_24h_realized_pnl", return_value=0.0), patch.object(
        rm, "_risk_history_load", return_value={}
    ), patch.object(rm, "_risk_history_save"), patch(
        "strategies.positions.list_active_positions", return_value=[lot]
    ), patch(
        "price_fetcher.get_prices_batch", return_value={}
    ), patch(
        "core.operator_notify.notify_operator", return_value=True
    ), patch(
        "risk.risk_manager._DAILY_LOSS_HALT_UNTIL", None
    ):
        dec = rm.evaluate(_buy(usdt=50), "4h", source="auto")
    assert dec.approved is False
    assert dec.code == "daily_loss_limit"


def test_b_t9_price_lookup_exception_denies_even_when_switch_is_log():
    """A raising mark lookup is a missing mark: deny on log, and do not halt."""
    rm = RiskManager(_risk_cfg(usdt=50, pct=5.0))
    assert rm.config.risk_config.get("fail_closed_guards") == "log"
    lot = {
        "symbol": "AAA/USDT",
        "amount": 2.0,
        "average_entry": 10.0,
        "side": "long",
    }
    saved = []

    def _save(data):
        saved.append(data)

    with patch.object(rm, "_trailing_24h_realized_pnl", return_value=0.0), patch.object(
        rm, "_risk_history_load", return_value={}
    ), patch.object(rm, "_risk_history_save", side_effect=_save), patch(
        "strategies.positions.list_active_positions", return_value=[lot]
    ), patch(
        "price_fetcher.get_prices_batch", side_effect=RuntimeError("ticker down")
    ), patch(
        "core.operator_notify.notify_operator", return_value=True
    ), patch(
        "risk.risk_manager._DAILY_LOSS_HALT_UNTIL", None
    ):
        buy = rm.evaluate(_buy(usdt=50), "4h", source="auto")
        short = rm.evaluate(_short("auto"), "15m", source="auto")
    assert buy.approved is False
    assert buy.code == "daily_loss_limit"
    assert "lookup failed" in buy.message
    assert short.approved is False
    assert short.code == "daily_loss_limit"
    assert "lookup failed" in short.message
    assert saved == []


# --- T8 / T10 / T12 ---------------------------------------------------------


@pytest.mark.parametrize("source", _T8_SOURCES)
def test_t8_final_size_is_capped_for_every_source(source):
    raw_over = {
        "max_usdt_per_trade": 100,
        "trading_mode": "paper",
        "live": {"dry_run": True, "execution": "shadow", "max_usdt_per_trade": 4500},
    }
    rm = RiskManager(_risk_cfg(**raw_over))
    signal = "GAINER_RELVOL" if source == "gainer_relvol" else "BUY"
    order = _buy(usdt=1520, source=source, signal=signal)
    with _eval_env(rm, metrics=_THICK, mcap=50_000_000), patch.object(
        rm, "_dynamic_size", return_value=(1520.0, {"total_multiplier": 1.9})
    ):
        dec = rm.evaluate(order, "15m", source=source)
    assert dec.approved is True, f"{source}: {dec.code} {dec.message}"
    assert dec.order.usdt_amount <= 100

    same = RiskManager(_risk_cfg(**raw_over))
    small = _buy(usdt=80, source=source, signal=signal)
    with _eval_env(same, metrics=_THICK, mcap=50_000_000), patch.object(
        same, "_dynamic_size", return_value=(80.0, {"total_multiplier": 1.0})
    ):
        kept = same.evaluate(small, "15m", source=source)
    if source == "gainer_relvol":
        # The relvol floor still applies when the ticket cap did not shrink the order.
        assert kept.approved is False
        assert kept.code == "size_too_small"
    else:
        assert kept.approved is True, kept.message
        assert kept.order.usdt_amount == pytest.approx(80.0)


def test_t8_capped_below_minimum_is_rejected():
    rm = RiskManager(
        _risk_cfg(
            max_usdt_per_trade=3,
            risk={
                "min_trade_usdt": 5,
                "max_daily_loss_pct": 0,
                "cash_floor_pct": 0,
                "cash_policy": {"enabled": False},
                "position_capacity": {"enabled": False},
                "liquidity_guard": dict(_LIQ),
            },
        )
    )
    with _eval_env(rm, metrics=_THICK, mcap=50_000_000), patch.object(
        rm, "_dynamic_size", return_value=(1520.0, {"total_multiplier": 1.0})
    ):
        dec = rm.evaluate(_buy(usdt=1520, source="dca", signal="BUY_DCA"), "15m", source="dca")
    assert dec.approved is False
    assert dec.code == "ticket_below_min"


def test_t8_live_reader_uses_overlay_ticket():
    disk = _disk_config()
    effective = apply_effective_config(disk, load_overlay(_OVERLAY))
    assert RiskManager(BotConfig(effective))._base_usdt_cap() == 100


def _seed_lot(symbol: str, *, scope: str, tenant: str, **extra) -> None:
    key = _resolve_store_key(scope, tenant)
    _activate(key)
    store = _ensure_store(key)
    row = {
        "amount": Decimal("100"),
        "peak_amount": 100.0,
        "sold_percent": 0.0,
        "average_entry": 10.0,
        "realized_pnl": 0.0,
        "last_buy_price": 10.0,
        "dca_rounds": 0,
    }
    row.update(extra)
    store[get_key(symbol, "1h")] = row


def test_t10_open_cap_counts_only_this_tenants_live_lots():
    tid = "tenant-live"
    other = "tenant-other"
    cfg = _risk_cfg(
        max_open_positions=4,
        trading_mode="live",
        live={"dry_run": True, "execution": "shadow"},
        sell_policy={"rotation": {"tail_exempt_notional_usdt": 0}},
    )
    clear_positions_memory()
    clear_positions_memory(tenant_id=tid)
    clear_positions_memory(tenant_id=other)
    empty_hist = {"trades": [], "virtual_balance": 100_000.0}
    ledger_patches = (
        patch("data_manager.load_orders", return_value={"orders": []}),
        patch("data_manager.load_positions_document", return_value={"positions": {}}),
        patch("data_manager.load_trade_history_document", return_value=empty_hist),
        patch("risk.risk_manager.load_trade_history", return_value=empty_hist),
        patch("risk.risk_manager.load_live_trade_history", return_value=empty_hist),
    )
    try:
        with tenant_context(other, scope="live"):
            for name in ("EEE", "FFF", "GGG", "HHH", "III"):
                _seed_lot(f"{name}/USDT", scope="live", tenant=other)
        with tenant_context(tid, scope="paper"):
            for name in ("JJJ", "KKK", "LLL"):
                _seed_lot(f"{name}/USDT", scope="paper", tenant=tid)
        with tenant_context(tid, scope="live"):
            for name in ("AAA", "BBB", "CCC"):
                _seed_lot(f"{name}/USDT", scope="live", tenant=tid)
            _seed_lot("DDD/USDT", scope="live", tenant=tid, shadow=True, mode="shadow")
        # Leave the tenant context before evaluate. logger.get_config loads the
        # active tenant body, and a live tenant context would open Mongo.
        # _active_key survives the context exit, so the live store still counts.
        _activate(_resolve_store_key("live", tid))
        assert count_open_full_slots(cfg.raw) == 3
        rm = RiskManager(cfg)
        indicators = {"rsi": 50, "atr": 0.01, "close": 1.0}
        full_cap = SimpleNamespace(
            max_open_eff=4, enabled=False, rationale="", factors={}, free_slots=0, regime=None
        )
        with ledger_patches[0], ledger_patches[1], ledger_patches[2], ledger_patches[3], ledger_patches[4]:
            with _eval_env(rm, metrics=_THICK, mcap=50_000_000), patch(
                "risk.risk_manager.count_open_full_slots", wraps=count_open_full_slots
            ), patch.object(rm.market, "fetch_indicators", return_value=indicators):
                fourth = rm.evaluate(_buy(usdt=50, source="auto"), "1h", source="auto")
            assert fourth.approved is True, fourth.message
            _seed_lot("MMM/USDT", scope="live", tenant=tid)
            _activate(_resolve_store_key("live", tid))
            assert count_open_full_slots(cfg.raw) == 4
            order = TradeOrder(
                type="BUY", symbol="NNN/USDT", price=1.0, amount=0, usdt_amount=50, signal="BUY", source="auto"
            )
            with _eval_env(rm, metrics=_THICK, mcap=50_000_000), patch(
                "risk.risk_manager.count_open_full_slots", return_value=4
            ), patch(
                "risk.slot_eviction_runtime.try_slot_eviction_on_max_open",
                return_value=(None, ""),
            ), patch.object(rm, "_resolve_position_capacity", return_value=full_cap), patch.object(
                rm.market, "fetch_indicators", return_value=indicators
            ):
                fifth = rm.evaluate(order, "1h", source="auto")
            assert fifth.approved is False
            assert fifth.code == "max_open_positions"
    finally:
        clear_positions_memory()
        clear_positions_memory(tenant_id=tid)
        clear_positions_memory(tenant_id=other)
        _position_stores.clear()


_SLOT_REGIMES = ("NEUTRAL", "RISK_ON", "RISK_OFF", "CRASH")


def _staging_slot_pieces() -> tuple[dict, dict, dict]:
    disk = _disk_config()
    capacity = copy.deepcopy(disk["risk"]["position_capacity"])
    eviction = copy.deepcopy(disk["risk"]["slot_eviction"])
    rotation = copy.deepcopy(disk["sell_policy"]["rotation"])
    assert capacity.get("enabled") is True
    assert float(rotation["tail_exempt_notional_usdt"]) > 100
    assert float(rotation["tail_exempt_sold_pct"]) <= 0.30
    return capacity, eviction, rotation


def _t10b_raw(*, overlay: bool) -> dict:
    """Disk capacity and rotation, then the live-caps overlay (or without its disable)."""
    from core.trading_profiles import deep_merge_dicts

    capacity, eviction, rotation = _staging_slot_pieces()
    disk = _disk_config()
    base = {
        "trading_mode": "live",
        "max_usdt_per_trade": 4500,
        "max_open_positions": int(disk["max_open_positions"]),
        "max_position_percent": 100,
        "trade_cooldown_hours": 0,
        "max_daily_trades": 0,
        "trading": {"entries_enabled": True, "exits_enabled": True},
        "live": {"dry_run": False, "execution": "shadow", "max_usdt_per_trade": 4500},
        "shorts": {
            "enabled": True,
            "allow_live": False,
            "max_open": 6,
            "max_margin_pct": 80,
            "volatile": {"market_cap_min_usd": 0},
        },
        "risk": {
            "min_trade_usdt": 5,
            "max_daily_loss_pct": 0,
            "cash_floor_pct": 0,
            "cash_policy": {"enabled": False},
            "position_capacity": capacity,
            "slot_eviction": eviction,
            "liquidity_guard": dict(_LIQ),
            "fail_closed_guards": "log",
        },
        "sell_policy": {"rotation": rotation},
        "observability": {
            "operator_timezone": disk["observability"]["operator_timezone"]
        },
    }
    patch_overlay = copy.deepcopy(load_overlay(_OVERLAY))
    if not overlay:
        patch_overlay.get("risk", {}).pop("position_capacity", None)
    return deep_merge_dicts(base, patch_overlay)


@contextmanager
def _reach_slot(rm, regime: str):
    """Stub gates that are not the slot cap. Capacity and tail counting stay real."""
    from contextlib import ExitStack

    bias = {
        "block_buys": False,
        "apply_size_mult": False,
        "active": True,
        "size_mult": 1.0,
        "regime": regime,
        "degraded": False,
    }
    with ExitStack() as stack:
        stack.enter_context(patch.object(rm, "_trade_cooldown_blocked", return_value=(False, "")))
        stack.enter_context(patch.object(rm, "_cash_floor_blocked", return_value=None))
        stack.enter_context(patch.object(rm, "_daily_buy_limit_blocked", return_value=None))
        stack.enter_context(patch.object(rm, "_portfolio_equity", return_value=100_000.0))
        stack.enter_context(patch.object(rm, "_spendable_usdt", return_value=50_000.0))
        stack.enter_context(patch.object(rm, "_available_usdt", return_value=50_000.0))
        stack.enter_context(patch.object(rm, "_initial_capital", return_value=100_000.0))
        stack.enter_context(patch.object(rm, "_equity_drawdown_pct", return_value=0.0))
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
            patch("services.market_policy_fusion.get_global_market_bias", return_value=bias)
        )
        stack.enter_context(patch("intelligence.memory.cache.get_entry_bias", return_value="neutral"))
        stack.enter_context(patch("intelligence.memory.cache.get_coin_profile", return_value=None))
        stack.enter_context(patch("intelligence.macro.snapshot.get_risk_multipliers", return_value={}))
        stack.enter_context(patch("services.universe.split.universe_split_enabled", return_value=False))
        stack.enter_context(patch("services.universe.split.is_trade_eligible", return_value=True))
        stack.enter_context(patch("core.stablecoins.is_stablecoin_symbol", return_value=False))
        stack.enter_context(patch("services.watchlist_quality.config.wqe_mode", return_value="off"))
        stack.enter_context(patch("price_fetcher.get_prices_batch", return_value={}))
        stack.enter_context(patch("core.operator_notify.notify_operator", return_value=True))
        stack.enter_context(
            patch.object(
                rm.market,
                "fetch_indicators",
                return_value={"rsi": 50, "atr": 0.01, "close": 1.0},
            )
        )
        stack.enter_context(patch("data.cmc_market_cap.resolve_market_cap_usd", return_value=None))
        stack.enter_context(patch("strategies.positions.resolve_tenant_scope", lambda: "live"))
        stack.enter_context(
            patch("strategies.positions.resolve_tenant_id", lambda tenant_id=None: "tenant-a")
        )
        yield


def _seed_tail_lot(symbol: str, tenant: str, **extra) -> None:
    row = {
        "amount": Decimal("100"),
        "peak_amount": 100.0,
        "sold_percent": 0.0,
        "average_entry": 1.0,
        "last_buy_price": 1.0,
        "current_price": 1.0,
        "realized_pnl": 0.0,
        "dca_rounds": 0,
    }
    row.update(extra)
    _seed_lot(symbol, scope="live", tenant=tenant, **row)


def test_t10b_fifth_buy_and_short_rejected_in_every_regime(monkeypatch):
    """Real capacity and tail rules. Four ~100 USDT lots fill the cap of 4.

    One lot is partly sold. A short is counted, then removed so the live-start
    check does not hide the slot rejection. No patch of the capacity resolver
    or the tail counter.
    """
    tid = "tenant-a"
    _arm_tenant_body(monkeypatch, _good_cap_body())
    merged = _t10b_raw(overlay=True)
    assert merged["risk"]["position_capacity"]["enabled"] is False
    assert merged["max_open_positions"] == 4
    assert merged["max_usdt_per_trade"] == 100
    assert merged["live"]["max_usdt_per_trade"] == 100
    assert merged["risk"]["slot_eviction"]["mode"] in ("shadow", "off")
    clear_positions_memory()
    clear_positions_memory(tenant_id=tid)
    try:
        for name in ("AAA", "BBB", "CCC"):
            _seed_tail_lot(f"{name}/USDT", tid)
        _seed_tail_lot(
            "DDD/USDT",
            tid,
            amount=Decimal("70"),
            peak_amount=100.0,
            sold_percent=0.30,
        )
        _seed_tail_lot("EEE/USDT", tid, side="short")
        _activate(_resolve_store_key("live", tid))
        assert count_open_full_slots(merged) == 5
        dry = copy.deepcopy(merged)
        dry["live"] = {**dry["live"], "dry_run": True}
        assert count_open_full_slots(dry) < 5
        store = _ensure_store(_resolve_store_key("live", tid))
        del store[get_key("EEE/USDT", "1h")]
        _activate(_resolve_store_key("live", tid))
        assert count_open_full_slots(merged) == 4
        rm = RiskManager(BotConfig(merged))
        for regime in _SLOT_REGIMES:
            with _reach_slot(rm, regime):
                buy = rm.evaluate(
                    TradeOrder(
                        type="BUY",
                        symbol="FFF/USDT",
                        price=1.0,
                        amount=0,
                        usdt_amount=100,
                        signal="BUY",
                        source="manual",
                    ),
                    "1h",
                    source="manual",
                )
                short = rm.evaluate(
                    TradeOrder(
                        type="SHORT",
                        symbol="GGG/USDT",
                        price=1.0,
                        amount=0,
                        usdt_amount=100,
                        signal="SHORT",
                        source="manual",
                    ),
                    "1h",
                    source="manual",
                )
            assert buy.approved is False, regime
            assert buy.code == "max_open_positions", f"{regime} buy {buy.code} {buy.message}"
            assert short.approved is False, regime
            assert short.code == "max_open_positions", f"{regime} short {short.code} {short.message}"
        assert count_open_full_slots(merged) == 4
    finally:
        clear_positions_memory()
        clear_positions_memory(tenant_id=tid)
        _position_stores.clear()


def test_t10b_missing_capacity_disable_is_live_caps_missing(monkeypatch):
    """Without position_capacity.enabled false, the guard's ceiling is above 4."""
    tid = "tenant-a"
    _arm_tenant_body(monkeypatch, _good_cap_body())
    merged = _t10b_raw(overlay=False)
    assert merged["risk"]["position_capacity"]["enabled"] is True
    assert merged["max_open_positions"] == 4
    clear_positions_memory()
    clear_positions_memory(tenant_id=tid)
    try:
        for name in ("AAA", "BBB", "CCC"):
            _seed_tail_lot(f"{name}/USDT", tid)
        _seed_tail_lot("DDD/USDT", tid, sold_percent=0.30, amount=Decimal("70"), peak_amount=100.0)
        _activate(_resolve_store_key("live", tid))
        rm = RiskManager(BotConfig(merged))
        for regime in _SLOT_REGIMES:
            with _reach_slot(rm, regime):
                buy = rm.evaluate(_buy(usdt=100, source="manual"), "1h", source="manual")
                short = rm.evaluate(_short("manual"), "1h", source="manual")
            assert buy.code == "live_caps_missing", f"{regime} {buy.code} {buy.message}"
            assert "max_open_eff" in buy.message
            assert short.code == "live_caps_missing", f"{regime} {short.code} {short.message}"
            assert "max_open_eff" in short.message
    finally:
        clear_positions_memory()
        clear_positions_memory(tenant_id=tid)
        _position_stores.clear()


def test_b3_slot_eviction_live_blocks(monkeypatch):
    _arm_tenant_body(monkeypatch, _good_cap_body())
    cfg = _live_cfg(False)
    cfg.raw["risk"]["slot_eviction"] = {"enabled": True, "mode": "live"}
    rm = RiskManager(cfg)
    with patch("core.operator_notify.notify_operator", return_value=True), patch("logger.log"):
        dec = rm.evaluate(_buy(usdt=50, source="manual"), "4h", source="manual")
    assert dec.approved is False
    assert dec.code == "live_caps_missing"
    assert "slot_eviction" in dec.message


def test_b3_open_live_short_blocks_live_start(monkeypatch):
    """An open short on the live book cannot be covered while allow_live is false."""
    tid = "tenant-a"
    _arm_tenant_body(monkeypatch, _good_cap_body())
    cfg = _live_cfg(False)
    clear_positions_memory()
    clear_positions_memory(tenant_id=tid)
    try:
        _seed_tail_lot("SSS/USDT", tid, side="short", amount=Decimal("10"), average_entry=2.0)
        _activate(_resolve_store_key("live", tid))
        rm = RiskManager(cfg)
        with patch("core.operator_notify.notify_operator", return_value=True), patch("logger.log"):
            buy = rm.evaluate(_buy(usdt=50, source="manual"), "4h", source="manual")
        assert buy.approved is False
        assert buy.code == "live_caps_missing"
        assert "open_short" in buy.message
        with _sell_passes(rm), patch("core.operator_notify.notify_operator", return_value=True), patch(
            "logger.log"
        ):
            sell = rm.evaluate(_sell(), "4h", source="auto")
        assert sell.code != "live_caps_missing"
        assert sell.approved is True, sell.message

        clear_positions_memory(tenant_id=tid, scope="live")
        _seed_tail_lot("LLL/USDT", tid, side="long")
        _activate(_resolve_store_key("live", tid))
        assert "open_short" not in rm._effective_cap_problems()

        clear_positions_memory(tenant_id=tid, scope="live")
        _seed_lot(
            "PPP/USDT",
            scope="paper",
            tenant=tid,
            side="short",
            amount=Decimal("10"),
            average_entry=2.0,
        )
        _activate(_resolve_store_key("live", tid))
        assert "open_short" not in rm._effective_cap_problems()
    finally:
        clear_positions_memory()
        clear_positions_memory(tenant_id=tid)
        _position_stores.clear()


def test_b3_uncounted_tails_are_live_caps_missing(monkeypatch):
    """The slot counter must include tails. A smaller count fails the live check."""
    _arm_tenant_body(monkeypatch, _good_cap_body())
    rm = RiskManager(_live_cfg(False))

    def _count(raw, include_tails=None):
        return 4 if include_tails else 0

    with patch("risk.risk_manager.count_open_full_slots", side_effect=_count), patch(
        "core.operator_notify.notify_operator", return_value=True
    ), patch("logger.log"):
        dec = rm.evaluate(_buy(usdt=50, source="manual"), "4h", source="manual")
    assert dec.code == "live_caps_missing"
    assert "open_slots" in dec.message


def test_t12_telegram_buy_stays_manual_capped_and_r3_still_applies(monkeypatch):
    from notifications.telegram_commands import manual_order_flow as flow

    # R1 ticket on a shadow book. The full disk merge is T1; this path is the
    # Telegram confirm, which must stay source=manual and still hit R3/R4.
    rm = RiskManager(
        _risk_cfg(
            max_usdt_per_trade=100,
            live={"dry_run": True, "execution": "shadow", "max_usdt_per_trade": 100},
        )
    )
    svc = TradingService(rm.config, risk_manager=rm)
    logged = []

    def _log(msg, level="INFO"):
        logged.append(str(msg))

    with _eval_env(rm, metrics=_THIN, mcap=50_000_000), patch.object(
        rm, "_dynamic_size", return_value=(1520.0, {"total_multiplier": 1.9})
    ), patch.object(svc, "refresh", return_value=svc), patch.object(
        rm, "status_summary", return_value={"drawdown_pct": 0}
    ), patch(
        "notifications.telegram_commands.manual_order_flow.OrderService"
    ) as order_cls, patch(
        "notifications.telegram_commands.manual_order_flow.send_telegram_message"
    ), patch(
        "notifications.telegram_commands.manual_order_flow.send_telegram_buttons"
    ), patch(
        "notifications.telegram_commands.manual_order_flow._store_pending", return_value="ord-1"
    ), patch("logger.log", side_effect=_log):
        order_cls.return_value = SimpleNamespace()
        assert flow.request_buy_confirmation(
            svc, symbol="AAA/USDT", timeframe="15m", price=1.0, usdt=1520
        )
    assert any("manual_buy_guard" in line for line in logged), logged
    with _eval_env(rm, metrics=_THIN, mcap=50_000_000), patch.object(
        rm, "_dynamic_size", return_value=(1520.0, {"total_multiplier": 1.9})
    ):
        decision = rm.evaluate(
            TradeOrder(
                type="BUY",
                symbol="AAA/USDT",
                price=1.0,
                amount=0,
                usdt_amount=1520,
                signal="BUY",
                source="manual",
            ),
            "15m",
            source="manual",
        )
    assert decision.approved is True, decision.message
    assert decision.code != "liq_guard_volume_low"
    assert not str(decision.code).startswith("liq_guard_")
    assert decision.order.usdt_amount <= 100

    _arm_tenant_body(monkeypatch, {"trading": {"entries_enabled": False}})
    live_rm = RiskManager(_live_cfg(False))
    with patch("core.operator_notify.notify_operator", return_value=True), patch("logger.log"):
        missing = live_rm.evaluate(
            TradeOrder(
                type="BUY", symbol="AAA/USDT", price=1.0, amount=0, usdt_amount=50, source="manual"
            ),
            "15m",
            source="manual",
        )
    assert missing.code == "live_caps_missing"


# --- T11 / T13 / T14 / R6a / sell lock --------------------------------------


def test_t11_only_exact_manual_is_human_and_others_hit_liquidity():
    assert is_human_operator_buy("manual") is True
    assert is_human_operator_buy("  Manual  ") is True
    for source in _FALSE_HUMAN:
        assert is_human_operator_buy(source) is False
        rm = RiskManager(_risk_cfg())
        src = source if source is not None else ""
        order = _buy(usdt=100, source=src or "auto")
        order.source = src
        with _eval_env(rm, metrics=_THIN, mcap=50_000_000):
            dec = rm.evaluate(order, "15m", source=src)
        assert dec.approved is False
        assert str(dec.code).startswith("liq_guard_"), f"{source!r} -> {dec.code} {dec.message}"


def test_t13_missing_source_is_unspecified_and_liquidity_blocks():
    assert DEFAULT_ORDER_SOURCE == "unspecified"
    assert coerce_order_source(None) == "unspecified"
    assert coerce_order_source("") == "unspecified"
    assert coerce_order_source("manual") == "manual"
    rm = RiskManager(_risk_cfg())
    svc = TradingService(rm.config, risk_manager=rm)
    seen = []

    def _spy(order, timeframe="4h", source="auto", **kwargs):
        seen.append(source)
        return RiskManager.evaluate(rm, order, timeframe, source=source, **kwargs)

    with _eval_env(rm, metrics=_THIN, mcap=50_000_000), patch.object(svc.risk, "evaluate", _spy):
        for source in (None, ""):
            dec = svc.evaluate_risk(_buy(usdt=100, source="auto"), "15m", source=source)
            assert dec.approved is False
            assert str(dec.code).startswith("liq_guard_")
    assert seen == ["unspecified", "unspecified"]


def test_t13_execute_order_without_source_gets_idempotency_key_and_liquidity_block():
    rm = RiskManager(_risk_cfg())
    svc = TradingService(rm.config, risk_manager=rm)
    seen = []
    real = rm.evaluate

    def _spy(order, timeframe="4h", source="auto", **kwargs):
        seen.append(source)
        return real(order, timeframe, source=source, **kwargs)

    from contextlib import nullcontext

    class _Ledger:
        def record_rejected(self, *args, **kwargs):
            return None

        def find_by_idempotency_key(self, *args, **kwargs):
            return None

        def update_status(self, *args, **kwargs):
            return None

        def create_from_request(self, *args, **kwargs):
            return {"id": "ord-1"}

    with _eval_env(rm, metrics=_THIN, mcap=50_000_000), patch.object(svc.risk, "evaluate", _spy), patch(
        "bus.writer_lease.require_lease_for_order"
    ), patch("services.trading_engine_runtime.should_queue_intent", return_value=False), patch(
        "bus.locks.ledger_lock", return_value=nullcontext()
    ), patch.object(svc, "can_execute", return_value=(True, "")), patch(
        "services.trading_service.OrderService", _Ledger
    ), patch("strategies.positions.bind_buy_timeframe", side_effect=lambda symbol, tf: tf):
        for source in (None, ""):
            order = _buy(usdt=100, source="auto")
            result = svc.execute_order(order, "15m", source=source)
            assert result.executed is False
            assert str(getattr(result, "code", "")).startswith("liq_guard_")
            assert order.idempotency_key
            assert order.source != "manual"
    assert seen == ["unspecified", "unspecified"]


@pytest.mark.parametrize("kind", ["BUY", "SHORT"])
@pytest.mark.parametrize("source", ("manual",) + _FALSE_HUMAN)
def test_t14_buy_and_short_classify_sources_the_same(kind, source):
    human = is_human_operator_buy(source)
    assert human is (str(source or "").strip().lower() == "manual")
    rm = RiskManager(_risk_cfg())
    src = source if isinstance(source, str) else ""
    if kind == "BUY":
        order = _buy(usdt=100, source=src or "auto")
        order.source = src
        with _eval_env(rm, metrics=_THIN, mcap=80_000_000):
            dec = rm.evaluate(order, "15m", source=src)
        if human:
            assert dec.approved is True, dec.message
            assert not str(dec.code).startswith("liq_guard_")
        else:
            assert str(dec.code).startswith("liq_guard_")
    else:
        order = _short(src)
        order.source = src
        with _eval_env(rm, metrics=_THIN, mcap=80_000_000), patch(
            "strategies.positions.list_active_positions", return_value=[]
        ):
            dec = rm.evaluate(order, "15m", source=src)
        if human:
            assert dec.approved is True, f"{dec.code} {dec.message}"
            assert not str(dec.code).startswith("liq_guard_")
        else:
            assert str(dec.code).startswith("liq_guard_"), f"{source!r} {dec.code} {dec.message}"


def test_r6a_mcp_tool_and_http_buy_never_evaluate_as_manual(monkeypatch):
    from services.mcp.authz import Actor
    from services.mcp.tools import tool_buy

    captured = []

    def _execute(**body):
        captured.append(body)
        return {"ok": True, "executed": False}

    actor = Actor("op", "owner", ("*",), ("read", "trade", "lock", "config_read", "kill"))
    out = tool_buy(
        actor,
        tenant="tenant-a",
        symbol="AAA/USDT",
        usdt=25,
        execute_fn=_execute,
        price=1.0,
    )
    assert out.get("ok") is True
    assert captured
    assert captured[0].get("source", "") != "manual"
    assert "source" not in captured[0]

    from flask import Flask
    from services.mcp.bot_http import register_mcp_bot_routes

    seen = []

    def _spy(self, order, timeframe="4h", source="auto", **kwargs):
        seen.append(source)
        return RiskDecision(approved=False, message="stop", code="test_stop")

    monkeypatch.setenv("EXIT_WS_INTERNAL_TOKEN", "secret")
    monkeypatch.delenv("MCP_BOT_TOKEN", raising=False)
    cfg = _risk_cfg(
        trading_mode="paper",
        live={"dry_run": True, "execution": "shadow"},
        mcp={
            "enabled": True,
            "allow_writes": True,
            "allow_live": False,
            "tenants": ["tenant-a"],
        },
    )

    class _TS(TradingService):
        def __init__(self, *args, **kwargs):
            super().__init__(cfg)

    class _Ledger:
        def record_rejected(self, *args, **kwargs):
            return None

        def find_by_idempotency_key(self, *args, **kwargs):
            return None

        def update_status(self, *args, **kwargs):
            return None

    monkeypatch.setattr("data_manager.get_config", lambda **_k: cfg.raw)
    monkeypatch.setattr("core.config.reload_config", lambda tenant_id=None: cfg.raw)
    monkeypatch.setattr(data_manager, "_load_tenant_config_body", lambda *a, **k: {})
    monkeypatch.setattr("services.trading_service.TradingService", _TS)
    monkeypatch.setattr("services.trading_service.OrderService", _Ledger)
    monkeypatch.setattr(RiskManager, "evaluate", _spy)
    from contextlib import nullcontext

    monkeypatch.setattr("bus.writer_lease.require_lease_for_order", lambda: None)
    monkeypatch.setattr("services.trading_engine_runtime.should_queue_intent", lambda *a, **k: False)
    monkeypatch.setattr("bus.locks.ledger_lock", lambda *a, **k: nullcontext())
    app = Flask(__name__)
    register_mcp_bot_routes(app)
    client = app.test_client()
    rv = client.post(
        "/internal/mcp/execute",
        headers={"X-Exit-Ws-Token": "secret"},
        json={
            "action": "buy",
            "tenant_id": "tenant-a",
            "symbol": "AAA/USDT",
            "usdt": 25,
            "timeframe": "1h",
            "actor_id": "op",
            "price": 1,
        },
    )
    assert rv.status_code == 200
    assert seen, "HTTP buy did not reach RiskManager.evaluate"
    assert all(src != "manual" for src in seen)
    assert all(str(src).startswith("mcp:") for src in seen)


def test_auto_sell_blocked_unchanged_for_manual_aliases():
    locked = {"amount": 1.0, "average_entry": 1.0, "lock": build_lock(reason="hold", locked_by="op")}
    assert auto_sell_blocked(locked, "manual")[0] is False
    assert auto_sell_blocked(locked, "manual_sell")[0] is False
    assert auto_sell_blocked(locked, "telegram")[0] is False
    blocked, _msg = auto_sell_blocked(locked, "mcp:op")
    assert blocked is True
    assert MODE_NO_AUTO_SELL


# --- T15 --------------------------------------------------------------------


def test_t15_missing_dca_rounds_blocks_addon_present_rounds_do_not():
    lot = {
        "amount": 100.0,
        "average_entry": 1.0,
        "symbol": "AAA/USDT",
        "entry_price": 1.0,
    }
    assert "dca_rounds" not in lot
    rm = RiskManager(_risk_cfg())
    fresh = {"price_age_sec": 0}
    with _eval_env(rm, position=lot, metrics=_THICK, mcap=50_000_000), patch(
        "price_fetcher.stale_expired_symbols", return_value=set()
    ):
        missing = rm.evaluate(
            _buy(usdt=100, source="dca", signal="BUY_DCA"),
            "15m",
            source="dca",
            indicators=fresh,
        )
    assert missing.approved is False
    assert missing.code == "dca_guard_missing_input"

    present = dict(lot)
    present["dca_rounds"] = 0
    with _eval_env(rm, position=present, metrics=_THICK, mcap=50_000_000), patch(
        "price_fetcher.stale_expired_symbols", return_value=set()
    ):
        allowed = rm.evaluate(
            _buy(usdt=100, source="dca", signal="BUY_DCA"),
            "15m",
            source="dca",
            indicators=fresh,
        )
    # price on the order is 1.0, average is 1.0: not below average, rounds 0.
    assert allowed.code != "dca_guard_missing_input"
    assert allowed.approved is True, f"{allowed.code} {allowed.message}"


def test_t12_confirm_passes_manual_source():
    text = (_REPO / "notifications" / "telegram_commands" / "manual_order_flow.py").read_text(
        encoding="utf-8"
    )
    assert 'source="manual"' in text
