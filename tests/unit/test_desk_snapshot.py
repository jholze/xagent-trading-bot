import json
from pathlib import Path

from services.desk.snapshot import build_snapshot

_LAB_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "desk" / "lab_snapshot.json"
LAB = json.loads(_LAB_PATH.read_text(encoding="utf-8"))

_LAB_NEXT_EDGE = (
    "TA: dip miss; next edge is DCA when RSI<40 (RelVol cap is a different path)."
)

_DESK_ON = {"desk": {"enabled": True, "tenants": ["default", "henry"]}}
_LAB_LOT = {
    "symbol": "LAB/USDT",
    "timeframe": "1h",
    "amount": 1,
    "average_entry": 0.132,
    "pnl_pct": -40.0,
    "dca_rounds": 1,
    "dca_max_rounds": 2,
    "source": "grid",
}


def _ok_kwargs(**overrides):
    kwargs = {
        "tenant_id": "default",
        "symbol": "LAB/USDT",
        "config_raw": dict(_DESK_ON),
        "lots": [dict(_LAB_LOT)],
        "fusion": {"regime": "NEUTRAL", "size_mult": 0.85},
        "cash_mode": "DEPLOY",
        "relvol_open": 8,
        "relvol_max": 8,
        "facts": dict(LAB),
    }
    kwargs.update(overrides)
    return kwargs


def test_snapshot_lists_open_lots_and_selects_lab():
    snap = build_snapshot(
        tenant_id="default",
        symbol="LAB/USDT",
        config_raw={"desk": {"enabled": True, "tenants": ["default", "henry"]}},
        lots=[
            {
                "symbol": "LAB/USDT",
                "timeframe": "1h",
                "amount": 1,
                "average_entry": 0.132,
                "pnl_pct": -40.0,
                "dca_rounds": 1,
                "dca_max_rounds": 2,
                "source": "grid",
            }
        ],
        fusion={"regime": "NEUTRAL", "size_mult": 0.85},
        cash_mode="DEPLOY",
        relvol_open=8,
        relvol_max=8,
        facts=LAB,
    )
    assert snap["ok"] is True
    assert snap["tenant_id"] == "default"
    assert snap["badges"]["relvol"] == "8 / 8"
    assert snap["hud"]["ta"]["stance"] == "MISS"
    assert "lots" in snap
    assert snap["symbol"] == "LAB/USDT"
    assert snap["badges"] == {
        "fusion": "NEUTRAL",
        "size_mult": 0.85,
        "cash": "DEPLOY",
        "relvol": "8 / 8",
    }
    assert snap["conflict"] is None
    assert snap["next_edge"] == _LAB_NEXT_EDGE
    assert snap["partial_stop_paused"] is True
    assert snap["lots"][0]["symbol"] == "LAB/USDT"
    assert snap["hud"]["social"]["stance"] == "ARMED"
    assert snap["hud"]["memory"]["stance"] == "IDLE"


def test_snapshot_short_lot_skips_dca_path():
    snap = build_snapshot(
        tenant_id="default",
        symbol="H/USDT",
        config_raw={"desk": {"enabled": True, "tenants": ["default", "henry"]}},
        lots=[
            {
                "symbol": "H/USDT",
                "timeframe": "4h",
                "amount": 100,
                "average_entry": 2.0,
                "side": "short",
                "leverage": 2,
                "dca_rounds": 0,
                "dca_max_rounds": 3,
            }
        ],
        fusion={"regime": "NEUTRAL", "size_mult": 1.0},
        cash_mode="DEPLOY",
        relvol_open=0,
        relvol_max=8,
        facts={"rsi": 55, "at_lower_bb": False},
    )
    assert snap["ok"] is True
    assert snap["hud"]["ta"]["path"] != "DCA 0/3"
    assert "DCA" not in str(snap["hud"]["ta"].get("path") or "")
    assert snap["partial_stop_paused"] is False


def test_snapshot_rejects_ctexp_tenant():
    snap = build_snapshot(
        tenant_id="ctexp",
        symbol="LAB/USDT",
        config_raw={"desk": {"enabled": True, "tenants": ["default", "henry"]}},
    )
    assert snap["ok"] is False
    assert snap["error"] == "tenant_not_allowed"


def test_snapshot_disabled_when_flag_off():
    snap = build_snapshot(
        tenant_id="default",
        symbol="LAB/USDT",
        config_raw={"desk": {"enabled": False, "tenants": ["default", "henry"]}},
        facts=LAB,
    )
    assert snap["ok"] is False
    assert snap["error"] == "desk_disabled"


def test_snapshot_conflict_when_social_armed_and_memory_block():
    facts = {**LAB, "memory_flag": "structure_risk"}  # live FactFlags name, not invented
    snap = build_snapshot(
        tenant_id="default",
        symbol="LAB/USDT",
        config_raw={"desk": {"enabled": True, "tenants": ["default", "henry"]}},
        lots=[
            {
                "symbol": "LAB/USDT",
                "timeframe": "1h",
                "amount": 1,
                "average_entry": 0.132,
                "pnl_pct": -40.0,
                "dca_rounds": 1,
                "dca_max_rounds": 2,
                "source": "grid",
            }
        ],
        fusion={"regime": "NEUTRAL", "size_mult": 0.85},
        cash_mode="DEPLOY",
        relvol_open=8,
        relvol_max=8,
        facts=facts,
    )
    assert snap["hud"]["memory"]["stance"] == "BLOCK"
    assert snap["hud"]["social"]["stance"] == "ARMED"
    assert snap["conflict"] == "SOCIAL ARMED · MEMORY BLOCK"


def test_snapshot_non_dict_desk_is_disabled():
    for desk in (True, "on", ["default"], 1):
        snap = build_snapshot(
            tenant_id="default",
            symbol="LAB/USDT",
            config_raw={"desk": desk},
        )
        assert snap["ok"] is False
        assert snap["error"] == "desk_disabled"


def test_snapshot_missing_tenants_defaults_allow_henry():
    snap = build_snapshot(**_ok_kwargs(tenant_id="henry", config_raw={"desk": {"enabled": True}}))
    assert snap["ok"] is True
    assert snap["tenant_id"] == "henry"


def test_snapshot_unknown_tenant_rejected():
    snap = build_snapshot(
        tenant_id="alice",
        symbol="LAB/USDT",
        config_raw=_DESK_ON,
    )
    assert snap["ok"] is False
    assert snap["error"] == "tenant_not_allowed"


def test_facts_overlay_keeps_lot_dca_and_adds_ta():
    snap = build_snapshot(
        tenant_id="default",
        symbol="LAB/USDT",
        config_raw={
            "desk": {"enabled": True, "tenants": ["default", "henry"]},
            "dca": {"max_rounds": 2, "pause_partial_stop_during_dca": True},
        },
        lots=[
            {
                "symbol": "LAB/USDT",
                "timeframe": "1h",
                "amount": 1,
                "average_entry": 0.132,
                "dca_rounds": 1,
                "source": "grid",
                # real list_active_positions DTO: NO dca_max_rounds, NO partial_stop_paused
            }
        ],
        fusion={"regime": "NEUTRAL", "size_mult": 0.85},
        cash_mode="DEPLOY",
        relvol_open=8,
        relvol_max=8,
        facts={
            "rsi": 37.7,
            "at_lower_bb": False,
            "cmc_confidence": 83.0,
            "cmc_trust": 72.0,
        },
    )
    assert snap["ok"] is True
    assert snap["hud"]["ta"]["stance"] == "MISS"
    assert snap["hud"]["ta"]["path"] == "DCA 1/2"
    assert snap["partial_stop_paused"] is True
    assert "DCA" in snap["next_edge"]
    assert "RSI" in snap["next_edge"]
