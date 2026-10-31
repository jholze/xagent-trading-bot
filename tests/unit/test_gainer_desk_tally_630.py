"""#630 desk tally: hit + late + missed + rejected == rows appended.

The 4-week scrape had days where rejected alone exceeded the row count.
A tally the desk publishes has to match the lines that landed, including
when a later append fails and when the same key appears twice in one call.
"""

from __future__ import annotations

import json

import pytest

from strategies.daily_gainer_desk import (
    _emit_rows,
    _reset_emit_cache_for_tests,
    format_desk_tally,
    parse_desk_tally,
    tally_is_consistent,
)


_RELVOL = {"name": "GAINER_RELVOL", "source": "gainer_relvol"}

# 2026-10-25 from shadow630_gainer_desk_daily.csv. rejected > rows.
_SCRAPE_LINE = (
    "daily_gainer_desk wrote n=660 hit=1 late=0 missed=2 rejected=1756 sum=1759"
)


@pytest.fixture(autouse=True)
def _desk_under_test(monkeypatch):
    monkeypatch.setenv("DAILY_GAINER_DESK_UNDER_TEST", "1")
    _reset_emit_cache_for_tests()


def _row(symbol: str, bucket: str, *, code: str = "long_mcap", corr: str = "c1") -> dict:
    return {
        "ts": "2026-10-25T00:00:00Z",
        "symbol": symbol,
        "bucket": bucket,
        "signal": dict(_RELVOL),
        "filter_codes": [] if bucket == "hit" else [code],
        "source": "tape" if bucket != "missed" else "scanner",
        "correlation_id": None if bucket == "missed" else corr,
    }


def _read(tmp_path) -> list[dict]:
    path = tmp_path / "logs" / "daily_gainer_desk.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _capture(monkeypatch) -> list[str]:
    found: list[str] = []

    def _log(message, level="INFO"):
        found.append(str(message))

    monkeypatch.setattr("strategies.daily_gainer_desk.log", _log)
    return found


def _tallies(lines: list[str]) -> list[dict[str, int]]:
    return [parsed for line in lines if (parsed := parse_desk_tally(line))]


def test_scrape_shape_is_an_invariant_violation():
    parsed = parse_desk_tally(_SCRAPE_LINE)
    assert parsed is not None
    assert parsed["rejected"] > parsed["n"]
    assert tally_is_consistent(parsed) is False


def test_emit_tally_matches_rows_including_a_failed_later_append(tmp_path, monkeypatch):
    logs = _capture(monkeypatch)
    rows = [
        _row("AAA/USDT", "missed"),
        _row("BBB/USDT", "missed"),
        _row("CCC/USDT", "hit", corr="hit-1"),
        _row("DDD/USDT", "late", code="gainer_chase_guard", corr="late-1"),
        _row("EEE/USDT", "rejected", corr="rej-1"),
    ]
    calls = {"n": 0}
    real = __import__(
        "services.observability_store", fromlist=["append_jsonl"]
    ).append_jsonl

    def flaky(path, record):
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError("disk")
        real(path, record)

    monkeypatch.setattr("services.observability_store.append_jsonl", flaky)
    written = _emit_rows(rows)
    landed = _read(tmp_path)
    parsed = _tallies(logs)
    assert written == len(landed) == 2
    assert len(parsed) == 1
    assert tally_is_consistent(parsed[0]) is True
    assert parsed[0]["n"] == 2
    assert parsed[0]["missed"] == 2
    assert parsed[0]["hit"] + parsed[0]["late"] + parsed[0]["rejected"] == 0
    buckets = {"hit": 0, "late": 0, "missed": 0, "rejected": 0}
    for row in landed:
        buckets[row["bucket"]] += 1
    assert buckets["hit"] == parsed[0]["hit"]
    assert buckets["late"] == parsed[0]["late"]
    assert buckets["missed"] == parsed[0]["missed"]
    assert buckets["rejected"] == parsed[0]["rejected"]


def test_duplicate_key_in_one_call_is_one_row(tmp_path, monkeypatch):
    logs = _capture(monkeypatch)
    row = _row("EEE/USDT", "rejected", corr="rej-1")
    written = _emit_rows([row, dict(row)])
    landed = _read(tmp_path)
    parsed = _tallies(logs)
    assert written == 1
    assert len(landed) == 1
    assert len(parsed) == 1
    assert tally_is_consistent(parsed[0]) is True
    assert parsed[0]["rejected"] == 1
    assert parsed[0]["n"] == 1


def test_mixed_batch_buckets_sum_to_rows_written(tmp_path, monkeypatch):
    logs = _capture(monkeypatch)
    rows = [
        _row("AAA/USDT", "missed"),
        _row("CCC/USDT", "hit", corr="hit-1"),
        _row("DDD/USDT", "late", code="gainer_chase_guard", corr="late-1"),
        _row("EEE/USDT", "rejected", corr="rej-1"),
    ]
    written = _emit_rows(rows)
    landed = _read(tmp_path)
    parsed = _tallies(logs)
    assert written == len(landed) == 4
    assert len(parsed) == 1
    assert tally_is_consistent(parsed[0]) is True
    assert parsed[0] == {
        "n": 4,
        "hit": 1,
        "late": 1,
        "missed": 1,
        "rejected": 1,
        "sum": 4,
    }
    again = format_desk_tally(
        {"hit": 1, "late": 1, "missed": 1, "rejected": 1},
        4,
    )
    assert tally_is_consistent(parse_desk_tally(again) or {})
