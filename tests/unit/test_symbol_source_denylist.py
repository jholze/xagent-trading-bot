"""Prevention denylist: config lists, fail-closed parse, tape codes.

Coin names and the blocked source live in tests/fixtures/prevention_denylist.json
and in config.json. This file reads those lists. It does not add a second copy
in strategy code.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from core.config import BotConfig
from core.models import TradeOrder
from core.trading_profiles import apply_effective_config
from risk.denylist import (
    CODE_CONFIG,
    CODE_SOURCE,
    CODE_SYMBOL,
    evaluate_denylist,
)
from risk.risk_manager import RiskManager

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "prevention_denylist.json"
LOSS_CASES = ROOT / "tests" / "fixtures" / "loss_cases_641.csv"
FIXTURE_DIR = ROOT / "tests" / "fixtures"


def _lists() -> tuple[list[str], list[str]]:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return list(raw["symbol_denylist"]), list(raw["source_denylist"])


def _risk(**extra) -> dict:
    symbols, sources = _lists()
    risk = {"symbol_denylist": list(symbols), "source_denylist": list(sources)}
    risk.update(extra)
    return risk


def _pair(base: str) -> str:
    return f"{base}/USDT"


def test_shipped_config_matches_the_fixture():
    disk = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    symbols, sources = _lists()
    assert disk["risk"]["symbol_denylist"] == symbols
    assert disk["risk"]["source_denylist"] == sources


def test_blocks_each_configured_base_on_buy_and_add():
    symbols, _sources = _lists()
    for base in symbols:
        fresh = evaluate_denylist(_pair(base), ["technical"], _risk())
        add = evaluate_denylist(_pair(base), ["dca"], _risk())
        assert fresh.codes == (CODE_SYMBOL,)
        assert add.codes == (CODE_SYMBOL,)
        assert fresh.blocked and add.blocked


def test_blocks_configured_source_even_when_the_base_is_allowed():
    _symbols, sources = _lists()
    for source in sources:
        verdict = evaluate_denylist("TAG/USDT", [source], _risk())
        assert verdict.codes == (CODE_SOURCE,)
    other_field = evaluate_denylist(
        "TAG/USDT",
        ["technical", sources[0]],
        _risk(),
    )
    assert CODE_SOURCE in other_field.codes


def test_symbol_and_source_codes_both_show():
    symbols, sources = _lists()
    verdict = evaluate_denylist(_pair(symbols[0]), [sources[0]], _risk())
    assert verdict.codes == (CODE_SYMBOL, CODE_SOURCE)


def test_case_and_pair_shapes():
    symbols, sources = _lists()
    base = symbols[0]
    assert evaluate_denylist(base.lower(), [sources[0].upper()], _risk()).codes == (
        CODE_SYMBOL,
        CODE_SOURCE,
    )
    assert CODE_SYMBOL in evaluate_denylist(f"{base}_USDT_1h", ["dca"], _risk()).codes
    assert evaluate_denylist("UNAVAIL/USDT", ["dca"], _risk()).blocked is False


def test_absent_keys_do_not_block():
    symbols, sources = _lists()
    verdict = evaluate_denylist(_pair(symbols[0]), [sources[0]], {})
    assert verdict.blocked is False
    assert verdict.malformed is False


@pytest.mark.parametrize(
    "bad",
    ["nope", {"a": 1}, None, [""], ["  "], [1], ["ok", None]],
)
def test_malformed_list_fail_closes(bad):
    verdict = evaluate_denylist("TAG/USDT", ["dca"], {"symbol_denylist": bad})
    assert verdict.malformed is True
    assert verdict.codes == (CODE_CONFIG,)
    source_bad = evaluate_denylist("TAG/USDT", ["dca"], {"source_denylist": bad})
    assert source_bad.malformed is True


def test_risk_section_not_a_dict_fail_closes():
    assert evaluate_denylist("TAG/USDT", ["dca"], ["nope"]).malformed is True


def test_empty_lists_allow():
    verdict = evaluate_denylist("TAG/USDT", ["dca"], {"symbol_denylist": [], "source_denylist": []})
    assert verdict.blocked is False


def test_tenant_override_replaces_the_list():
    symbols, sources = _lists()
    base = {"risk": _risk()}
    cleared = apply_effective_config(base, {"risk": {"symbol_denylist": []}})
    assert cleared["risk"]["symbol_denylist"] == []
    assert cleared["risk"]["source_denylist"] == sources
    assert evaluate_denylist(_pair(symbols[0]), ["dca"], cleared["risk"]).codes == ()
    replaced = apply_effective_config(
        base,
        {"risk": {"source_denylist": ["fixture_source"]}},
    )
    assert evaluate_denylist("TAG/USDT", ["fixture_source"], replaced["risk"]).codes == (
        CODE_SOURCE,
    )
    assert evaluate_denylist("TAG/USDT", [sources[0]], replaced["risk"]).blocked is False


def _manager(risk: dict) -> RiskManager:
    return RiskManager(
        BotConfig(
            {
                "max_usdt_per_trade": 100,
                "max_open_positions": 10,
                "trade_cooldown_hours": 0,
                "risk": risk,
                "live": {"execution": "shadow", "dry_run": True},
            }
        )
    )


def test_risk_manager_rejects_buy_and_add_and_keeps_sell():
    symbols, sources = _lists()
    rm = _manager(_risk())
    buy = rm.evaluate(
        TradeOrder(type="BUY", symbol=_pair(symbols[0]), price=1.0, amount=0, usdt_amount=25),
        "1h",
        source="technical",
    )
    assert buy.approved is False
    assert buy.details["codes"] == [CODE_SYMBOL]
    add = rm.evaluate(
        TradeOrder(
            type="BUY",
            symbol="TAG/USDT",
            price=1.0,
            amount=0,
            usdt_amount=25,
            signal="BUY_DCA",
            source="dca",
        ),
        "1h",
        source=sources[0],
    )
    assert add.approved is False
    assert add.code == CODE_SOURCE
    sell = rm._symbol_source_denylist(
        TradeOrder(type="SELL", symbol=_pair(symbols[0]), price=1.0, amount=1),
        "auto",
    )
    assert sell is None
    short = rm._symbol_source_denylist(
        TradeOrder(type="SHORT", symbol=_pair(symbols[0]), price=1.0, amount=0, usdt_amount=25),
        "auto",
    )
    assert short is None


def test_manual_buy_is_not_exempt():
    symbols, _sources = _lists()
    rm = _manager(_risk())
    decision = rm.evaluate(
        TradeOrder(
            type="BUY",
            symbol=_pair(symbols[-1]),
            price=1.0,
            amount=0,
            usdt_amount=10,
            source="manual",
        ),
        "1h",
        source="manual",
    )
    assert decision.approved is False
    assert decision.code == CODE_SYMBOL


def test_tape_row_shows_the_reject_codes(monkeypatch):
    symbols, _sources = _lists()
    rm = _manager(_risk())
    order = TradeOrder(
        type="BUY",
        symbol=_pair(symbols[0]),
        price=1.0,
        amount=0,
        usdt_amount=25,
        source="dca",
    )
    decision = rm.evaluate(order, "1h", source="dca")
    captured: dict = {}

    def _append(_path, row):
        captured["row"] = row

    monkeypatch.setenv("BUY_DECISION_TAPE_UNDER_TEST", "1")
    monkeypatch.setattr(
        "strategies.buy_decision_tape._lookup_position",
        lambda symbol, timeframe: {"amount": 1, "average_entry": 2},
    )
    monkeypatch.setattr("services.observability_store.append_jsonl", _append)
    monkeypatch.setattr("services.observability_store.maybe_rotate_jsonl", lambda *a, **k: None)
    from strategies.buy_decision_tape import emit_buy_decision_tape

    emit_buy_decision_tape(order, decision, source="dca", timeframe="1h", config={})
    assert captured["row"]["outcome"] == "rejected"
    assert captured["row"]["filter_codes"] == [CODE_SYMBOL]


def _is_buy_or_add(kind: str, side: str) -> bool:
    kind_l = kind.strip().lower()
    side_l = side.strip().lower()
    if kind_l == "entry" or kind_l.startswith("add"):
        return True
    return side_l in {"buy", "buy_dca"}


def scan_fixture_buys(symbols: set[str], sources: set[str]) -> dict:
    """Count buy/add rows in tests/fixtures that the denylist would reject."""
    rows = []
    with LOSS_CASES.open(encoding="utf-8", newline="") as handle:
        for record in csv.DictReader(handle):
            kind = str(record.get("kind") or "")
            if not _is_buy_or_add(kind, ""):
                continue
            base = str(record.get("symbol") or "").strip().upper()
            source = str(record.get("buy_source") or "").strip().lower()
            symbol_hit = base in symbols
            source_hit = source in sources
            if not symbol_hit and not source_hit:
                continue
            rows.append(
                {
                    "file": "tests/fixtures/loss_cases_641.csv",
                    "base": base,
                    "source": source,
                    "kind": kind,
                    "expected": str(record.get("expected") or ""),
                    "symbol_hit": symbol_hit,
                    "source_hit": source_hit,
                }
            )
    extra = _scan_other_fixtures(symbols, sources)
    return {"loss_cases": rows, "other": extra}


def _scan_other_fixtures(symbols: set[str], sources: set[str]) -> list[dict]:
    found: list[dict] = []
    for path in sorted(FIXTURE_DIR.rglob("*")):
        if not path.is_file() or path.name == "loss_cases_641.csv":
            continue
        if path.suffix == ".csv":
            found.extend(_scan_csv(path, symbols, sources))
        elif path.suffix == ".json":
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            _walk_json(payload, path, symbols, sources, found)
    return found


def _scan_csv(path: Path, symbols: set[str], sources: set[str]) -> list[dict]:
    hits = []
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                return []
            for record in reader:
                kind = str(record.get("kind") or record.get("type") or record.get("side") or "")
                source = str(
                    record.get("buy_source") or record.get("source") or record.get("entry_source") or ""
                )
                base = str(record.get("symbol") or record.get("base") or "").split("/")[0].upper()
                if not _is_buy_or_add(kind, kind):
                    continue
                if base in symbols or source.strip().lower() in sources:
                    hits.append({"file": str(path.relative_to(ROOT)), "base": base, "source": source})
    except (OSError, csv.Error, UnicodeDecodeError):
        return []
    return hits


def _walk_json(node, path: Path, symbols: set[str], sources: set[str], found: list[dict]) -> None:
    if isinstance(node, dict):
        kind = str(node.get("kind") or node.get("type") or node.get("side") or "")
        source = str(node.get("buy_source") or node.get("source") or node.get("entry_source") or "")
        symbol = str(node.get("symbol") or "")
        base = symbol.split("/")[0].upper() if symbol else str(node.get("base") or "").upper()
        if _is_buy_or_add(kind, kind) and (base in symbols or source.strip().lower() in sources):
            found.append(
                {
                    "file": str(path.relative_to(ROOT)),
                    "base": base,
                    "source": source.strip().lower(),
                    "kind": kind,
                }
            )
        for value in node.values():
            _walk_json(value, path, symbols, sources, found)
    elif isinstance(node, list):
        for item in node:
            _walk_json(item, path, symbols, sources, found)


def test_fixture_buy_block_count_is_stable():
    symbols, sources = _lists()
    scanned = scan_fixture_buys(set(symbols), {item.lower() for item in sources})
    loss = scanned["loss_cases"]
    # Measured on tests/fixtures/loss_cases_641.csv. A fixture edit must update
    # this pin and the PR evidence table together.
    assert len(loss) == 33
    assert sum(1 for row in loss if str(row["kind"]).startswith("add")) == 20
    assert sum(1 for row in loss if row["kind"] == "entry") == 13
    assert sum(1 for row in loss if row["expected"] == "PASS") == 5
    assert sum(1 for row in loss if row["expected"] == "BLOCK") == 20
    assert sum(1 for row in loss if row["expected"] == "NOT_PROVABLE") == 8
    assert sum(1 for row in loss if row["symbol_hit"] and row["source_hit"]) == 1
    assert sum(1 for row in loss if row["source_hit"] and not row["symbol_hit"]) == 2
    assert scanned["other"] == []
