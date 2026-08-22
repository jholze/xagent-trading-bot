"""Desk HTTP routes: token-gated GETs on a tiny Flask app (never aria_bot.app)."""

from __future__ import annotations

from flask import Flask

_SNAP_PATH = "/internal/desk/snapshot?tenant=default&symbol=LAB/USDT"
_OHLCV_PATH = "/internal/desk/ohlcv?symbol=LAB/USDT&tf=1h"

_FAKE_SNAP = {
    "ok": True,
    "tenant_id": "default",
    "symbol": "LAB/USDT",
    "hud": {"ta": {"stance": "MISS"}},
    "badges": {},
    "lots": [],
    "conflict": None,
    "next_edge": "TA: dip miss",
    "partial_stop_paused": True,
}

_FAKE_OHLCV = {
    "ok": True,
    "closes": [1.0],
    "rsi": [37.7],
    "bb_upper": [None],
    "bb_middle": [None],
    "bb_lower": [None],
    "bars": [{"ts": 1, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0}],
    "last_rsi": 37.7,
    "at_lower_bb": False,
}

_OHLCV_UNAVAIL = {"ok": False, "error": "ohlcv_unavailable", "bars": []}


def _patch_live(monkeypatch, *, snapshot=None, ohlcv=None):
    """Never hit Mongo/exchange from HTTP tests."""
    monkeypatch.setattr(
        "services.desk.snapshot.build_snapshot",
        snapshot
        or (
            lambda **kw: {
                **_FAKE_SNAP,
                "symbol": kw.get("symbol") or _FAKE_SNAP["symbol"],
                "tenant_id": kw.get("tenant_id") or _FAKE_SNAP["tenant_id"],
            }
        ),
    )
    monkeypatch.setattr(
        "services.desk.ohlcv.load_ohlcv",
        ohlcv or (lambda *a, **k: dict(_OHLCV_UNAVAIL)),
    )


def _client(
    monkeypatch,
    *,
    exit_token="secret",
    desk_token=None,
    snapshot=None,
    ohlcv=None,
):
    monkeypatch.delenv("DESK_TOKEN", raising=False)
    monkeypatch.delenv("EXIT_WS_INTERNAL_TOKEN", raising=False)
    if exit_token is not None:
        monkeypatch.setenv("EXIT_WS_INTERNAL_TOKEN", exit_token)
    if desk_token is not None:
        monkeypatch.setenv("DESK_TOKEN", desk_token)
    _patch_live(monkeypatch, snapshot=snapshot, ohlcv=ohlcv)
    from services.desk_http import register_desk_routes

    app = Flask(__name__)
    register_desk_routes(app)
    return app.test_client()


def test_desk_snapshot_requires_token_when_set(monkeypatch):
    monkeypatch.setenv("EXIT_WS_INTERNAL_TOKEN", "secret")
    client = _client(monkeypatch, exit_token="secret")
    rv = client.get("/internal/desk/snapshot?tenant=default&symbol=LAB/USDT")
    assert rv.status_code == 401


def test_desk_snapshot_ok_with_header(monkeypatch):
    monkeypatch.setenv("EXIT_WS_INTERNAL_TOKEN", "secret")
    client = _client(monkeypatch, exit_token="secret")
    rv = client.get(
        "/internal/desk/snapshot?tenant=default&symbol=LAB/USDT",
        headers={"X-Exit-Ws-Token": "secret"},
    )
    assert rv.status_code == 200
    body = rv.get_json()
    assert "hud" in body or body.get("ok") is False  # fixture-less live may fail-open


def test_desk_not_configured_without_token(monkeypatch):
    client = _client(monkeypatch, exit_token=None, desk_token=None)
    rv = client.get(
        _SNAP_PATH,
        headers={"X-Exit-Ws-Token": "anything"},
    )
    assert rv.status_code == 503
    body = rv.get_json()
    assert body.get("ok") is False
    assert body.get("error") == "not_configured"


def test_desk_token_preferred_over_exit_ws(monkeypatch):
    client = _client(monkeypatch, exit_token="ws-secret", desk_token="desk-secret")
    bad = client.get(_SNAP_PATH, headers={"X-Exit-Ws-Token": "ws-secret"})
    assert bad.status_code == 401
    good = client.get(_SNAP_PATH, headers={"X-Exit-Ws-Token": "desk-secret"})
    assert good.status_code == 200


def test_desk_token_alone_is_accepted(monkeypatch):
    client = _client(monkeypatch, exit_token=None, desk_token="desk-only")
    rv = client.get(_SNAP_PATH, headers={"X-Exit-Ws-Token": "desk-only"})
    assert rv.status_code == 200


def test_desk_snapshot_ok_with_query_token(monkeypatch):
    client = _client(monkeypatch, exit_token="secret")
    rv = client.get(_SNAP_PATH + "&token=secret")
    assert rv.status_code == 200
    body = rv.get_json()
    assert "hud" in body or body.get("ok") is False


def test_desk_snapshot_ok_with_bearer(monkeypatch):
    client = _client(monkeypatch, exit_token="secret")
    rv = client.get(_SNAP_PATH, headers={"Authorization": "Bearer secret"})
    assert rv.status_code == 200


def test_desk_snapshot_missing_symbol_is_400(monkeypatch):
    client = _client(monkeypatch, exit_token="secret")
    rv = client.get(
        "/internal/desk/snapshot?tenant=default",
        headers={"X-Exit-Ws-Token": "secret"},
    )
    assert rv.status_code == 400
    body = rv.get_json()
    assert body.get("ok") is False


def test_desk_snapshot_overlays_last_bar_ta(monkeypatch):
    captured = {}

    def fake_snap(**kwargs):
        captured.update(kwargs)
        return dict(_FAKE_SNAP)

    def fake_ohlcv(symbol, tf, limit=120):
        captured["ohlcv_symbol"] = symbol
        captured["ohlcv_tf"] = tf
        return dict(_FAKE_OHLCV)

    client = _client(
        monkeypatch,
        exit_token="secret",
        snapshot=fake_snap,
        ohlcv=fake_ohlcv,
    )
    rv = client.get(_SNAP_PATH, headers={"X-Exit-Ws-Token": "secret"})
    assert rv.status_code == 200
    assert captured.get("symbol") == "LAB/USDT"
    assert captured.get("tenant_id") == "default"
    facts = captured.get("facts") or {}
    assert facts.get("rsi") == 37.7
    assert facts.get("at_lower_bb") is False
    assert captured.get("ohlcv_tf") == "1h"


def test_desk_snapshot_fail_open_on_builder_error(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("mongo down")

    client = _client(monkeypatch, exit_token="secret", snapshot=boom)
    rv = client.get(_SNAP_PATH, headers={"X-Exit-Ws-Token": "secret"})
    assert rv.status_code in (200, 503)
    assert rv.status_code != 500
    body = rv.get_json()
    assert body.get("ok") is False
    assert body.get("error") == "snapshot_failed"


def test_desk_ohlcv_requires_token_when_set(monkeypatch):
    client = _client(monkeypatch, exit_token="secret")
    rv = client.get(_OHLCV_PATH)
    assert rv.status_code == 401


def test_desk_ohlcv_ok_with_header(monkeypatch):
    client = _client(
        monkeypatch,
        exit_token="secret",
        ohlcv=lambda *a, **k: dict(_FAKE_OHLCV),
    )
    rv = client.get(
        "/internal/desk/ohlcv?symbol=LAB/USDT&tf=1h",
        headers={"X-Exit-Ws-Token": "secret"},
    )
    assert rv.status_code == 200
    body = rv.get_json()
    assert body.get("ok") is True or body.get("error") == "ohlcv_unavailable"
    if body.get("ok"):
        assert "bars" in body
        assert body.get("last_rsi") == 37.7


def test_desk_ohlcv_unavailable_is_200(monkeypatch):
    client = _client(
        monkeypatch,
        exit_token="secret",
        ohlcv=lambda *a, **k: dict(_OHLCV_UNAVAIL),
    )
    rv = client.get(_OHLCV_PATH, headers={"X-Exit-Ws-Token": "secret"})
    assert rv.status_code == 200
    body = rv.get_json()
    assert body.get("ok") is False
    assert body.get("error") == "ohlcv_unavailable"


def test_desk_ohlcv_query_token_and_default_tf(monkeypatch):
    captured = {}

    def fake_ohlcv(symbol, tf, limit=120):
        captured["symbol"] = symbol
        captured["tf"] = tf
        return dict(_FAKE_OHLCV)

    client = _client(monkeypatch, exit_token="secret", ohlcv=fake_ohlcv)
    rv = client.get(
        "/internal/desk/ohlcv?symbol=LAB/USDT&token=secret",
    )
    assert rv.status_code == 200
    assert captured.get("symbol") == "LAB/USDT"
    assert captured.get("tf") == "1h"


def test_desk_spa_missing_build_is_404(monkeypatch):
    client = _client(monkeypatch, exit_token="secret")
    rv = client.get("/desk")
    assert rv.status_code == 404
    body = rv.get_data(as_text=True)
    assert "npm run build" in body


def test_desk_spa_serves_index_and_assets_not_listing(monkeypatch, tmp_path):
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text("<html>desk</html>", encoding="utf-8")
    (assets / "app.js").write_text("console.log(1)", encoding="utf-8")

    client = _client(monkeypatch, exit_token="secret")
    import services.desk_http as desk_http

    monkeypatch.setattr(desk_http, "_DIST", dist)

    index = client.get("/desk")
    assert index.status_code == 200
    assert b"desk" in index.data

    js = client.get("/desk/assets/app.js")
    assert js.status_code == 200
    assert b"console.log" in js.data

    listing = client.get("/desk/assets")
    assert listing.status_code == 404
    listing_slash = client.get("/desk/assets/")
    assert listing_slash.status_code == 404


def test_desk_no_post_routes(monkeypatch):
    client = _client(monkeypatch, exit_token="secret")
    for path in (
        "/internal/desk/snapshot",
        "/internal/desk/ohlcv",
        "/desk",
    ):
        rv = client.post(path, headers={"X-Exit-Ws-Token": "secret"})
        assert rv.status_code == 405, path
