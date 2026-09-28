"""#554 — morning / Tages-Auswertung decision counts must be per acting tenant."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from core.tenant_context import tenant_context
from notifications.daily_stats import decision_highlights, decision_stats

TS = "2026-09-23T10:00:00"
SINCE = datetime(2026, 9, 23, 0, 0, 0)
UNTIL = datetime(2026, 9, 24, 0, 0, 0)


def _write_jsonl(bot_dir: Path, rows: list[dict]) -> None:
    log_dir = bot_dir / "logs"
    log_dir.mkdir(exist_ok=True)
    path = log_dir / "decisions.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _row(symbol: str, *, tenant_id=None, include_tenant: bool = True) -> dict:
    rec = {
        "timestamp": TS,
        "symbol": symbol,
        "action": "BUY_DCA",
        "normalized_action": "BUY_DCA",
        "executed": False,
        "sources": ["dca"],
        "rationale": f"{symbol} BUY_DCA",
    }
    if include_tenant:
        rec["tenant_id"] = tenant_id
    return rec


def _mixed_dca(bot_dir: Path) -> None:
    _write_jsonl(
        bot_dir,
        [
            _row("HENRY/USDT", tenant_id="henry"),
            _row("CTEXP/USDT", tenant_id="ctexp"),
        ],
    )


def test_decision_stats_henry_sees_only_henry_buy_dca(tmp_path):
    _mixed_dca(tmp_path)
    with tenant_context("henry", scope="demo"):
        stats = decision_stats(tmp_path, SINCE, UNTIL)
        highlights = decision_highlights(tmp_path, SINCE, UNTIL)
    assert stats["buy_dca"] == 1
    assert stats["total"] == 1
    assert [h["symbol"] for h in highlights] == ["HENRY/USDT"]


def test_decision_stats_ctexp_sees_only_ctexp_buy_dca(tmp_path):
    _mixed_dca(tmp_path)
    with tenant_context("ctexp", scope="demo"):
        stats = decision_stats(tmp_path, SINCE, UNTIL)
        highlights = decision_highlights(tmp_path, SINCE, UNTIL)
    assert stats["buy_dca"] == 1
    assert stats["total"] == 1
    assert [h["symbol"] for h in highlights] == ["CTEXP/USDT"]


def test_missing_tenant_id_counts_only_for_default(tmp_path):
    _write_jsonl(
        tmp_path,
        [
            _row("LEGACY/USDT", include_tenant=False),
            _row("HENRY/USDT", tenant_id="henry"),
        ],
    )
    with tenant_context("default", scope="demo"):
        default_stats = decision_stats(tmp_path, SINCE, UNTIL)
        default_highlights = decision_highlights(tmp_path, SINCE, UNTIL)
    with tenant_context("henry", scope="demo"):
        henry_stats = decision_stats(tmp_path, SINCE, UNTIL)
        henry_highlights = decision_highlights(tmp_path, SINCE, UNTIL)
    assert default_stats["buy_dca"] == 1
    assert default_stats["total"] == 1
    assert [h["symbol"] for h in default_highlights] == ["LEGACY/USDT"]
    assert henry_stats["buy_dca"] == 1
    assert henry_stats["total"] == 1
    assert [h["symbol"] for h in henry_highlights] == ["HENRY/USDT"]


def test_empty_tenant_id_counts_only_for_default(tmp_path):
    _write_jsonl(tmp_path, [_row("EMPTY/USDT", tenant_id="")])
    with tenant_context("default", scope="demo"):
        default_stats = decision_stats(tmp_path, SINCE, UNTIL)
    with tenant_context("henry", scope="demo"):
        henry_stats = decision_stats(tmp_path, SINCE, UNTIL)
    assert default_stats["buy_dca"] == 1
    assert default_stats["total"] == 1
    assert henry_stats["buy_dca"] == 0
    assert henry_stats["total"] == 0
