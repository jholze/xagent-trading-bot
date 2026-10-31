"""Reconstruct per-day, per-tenant price-cycle duration from Railway logs.

The xagent-test export is JSONL (optionally gzipped). Each line has
``message`` and ``timestamp``. Cycle lines are ``Cycle completed in Ns``
and ``cycle_summary ... dur=Ns``. Log lines are sometimes concatenated, so
the timestamp used is the one immediately before the matched phrase.

The price loop stamps one ``cycle_started`` and then runs every tenant
(``aria_bot.price_loop``). Each tenant's logged duration is cumulative.
Own time for tenant slot k is the difference from the previous slot.
Loops are almost always length 3. Slot 0 is default
(``iter_price_cycle_tenants`` puts it first). Slots 1 and 2 follow Mongo
active-tenant order. ``--lots`` is an open-lot CSV with a ``tenant``
column; the script prints those counts so the slots can be matched to
tenants by position count. It does not join the log to a tenant id.

Usage:
    python3 scripts/analysis/cycle_duration.py /path/to/cycle_logs.jsonl.gz
    python3 scripts/analysis/cycle_duration.py logs.jsonl --lots open_lots_now.csv
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


_TS = re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")
_SUMMARY = re.compile(
    r"cycle_summary mode=\S+ wl=(\d+) pos=(\d+) dur=([0-9.]+)s"
)
_COMPLETED = re.compile(
    r"Cycle completed in (\d+)s \([^,]+, (\d+) positions first\)"
)


def _open_text(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", errors="replace")
    return path.open("r", errors="replace")


def _stamp(msg: str, key: str, railway_ts: str) -> str:
    idx = msg.find(key)
    stamps = _TS.findall(msg[:idx] if idx >= 0 else msg)
    if stamps:
        return stamps[-1]
    return (railway_ts or "")[:19].replace("T", " ")


def load_events(path: Path) -> tuple[list[dict], list[dict]]:
    """Return (cycle_summary rows, Cycle completed rows), each sorted by time."""
    summaries: list[dict] = []
    completed: list[dict] = []
    with _open_text(path) as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            msg = obj.get("message") or ""
            railway = obj.get("timestamp") or ""
            found = _SUMMARY.search(msg)
            if found:
                stamp = _stamp(msg, "cycle_summary", railway)
                summaries.append(
                    {
                        "t": datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S"),
                        "wl": int(found.group(1)),
                        "pos": int(found.group(2)),
                        "dur": float(found.group(3)),
                    }
                )
            found_c = _COMPLETED.search(msg)
            if found_c:
                stamp = _stamp(msg, "Cycle completed", railway)
                completed.append(
                    {
                        "t": datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S"),
                        "dur": int(found_c.group(1)),
                        "pos": int(found_c.group(2)),
                    }
                )
    summaries.sort(key=lambda row: row["t"])
    completed.sort(key=lambda row: row["t"])
    return summaries, completed


def segment_loops(summaries: list[dict]) -> list[list[dict]]:
    """Group summaries that share one price-loop ``cycle_started``.

    A continuation has a higher cumulative duration and a wall gap equal to
    that increase. Anything else starts a new loop.
    """
    loops: list[list[dict]] = []
    current: list[dict] = []
    for row in summaries:
        if not current:
            current = [row]
            continue
        prev = current[-1]
        gap = (row["t"] - prev["t"]).total_seconds()
        delta = row["dur"] - prev["dur"]
        continues = (
            delta > -5
            and abs(gap - max(delta, 0)) < max(20, 0.25 * max(gap, 1))
            and row["dur"] + 5 >= prev["dur"]
        )
        if continues:
            current.append(row)
        else:
            loops.append(current)
            current = [row]
    if current:
        loops.append(current)
    return loops


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * p
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (rank - lo)


def own_times(loop: list[dict]) -> list[float]:
    prev = 0.0
    out: list[float] = []
    for row in loop:
        out.append(row["dur"] - prev)
        prev = row["dur"]
    return out


def daily_report(loops: list[list[dict]], completed: list[dict]) -> list[dict]:
    slots: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    sizes: dict[str, Counter] = defaultdict(Counter)
    for loop in loops:
        day = loop[0]["t"].strftime("%Y-%m-%d")
        sizes[day][len(loop)] += 1
        for index, own in enumerate(own_times(loop)):
            if index > 2:
                continue
            slots[day][index].append(own)
    raw: dict[str, list[float]] = defaultdict(list)
    for row in completed:
        raw[row["t"].strftime("%Y-%m-%d")].append(float(row["dur"]))
    days = sorted(set(slots) | set(raw))
    rows = []
    for day in days:
        row = {
            "day": day,
            "loops": sum(sizes[day].values()),
            "loop_size_3": sizes[day].get(3, 0),
            "completed_n": len(raw[day]),
            "completed_p50": _percentile(raw[day], 0.5),
            "completed_p90": _percentile(raw[day], 0.9),
        }
        for index in range(3):
            row[f"slot{index}_n"] = len(slots[day][index])
            row[f"slot{index}_p50"] = _percentile(slots[day][index], 0.5)
            row[f"slot{index}_p90"] = _percentile(slots[day][index], 0.9)
        rows.append(row)
    return rows


def lot_counts(path: Path) -> Counter:
    counts: Counter = Counter()
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            tenant = (row.get("tenant") or "").strip()
            if tenant:
                counts[tenant] += 1
    return counts


def _fmt(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.0f}"


def render_markdown(rows: list[dict], lots: Counter | None) -> str:
    lines = [
        "| day | completed n | completed p50 | completed p90 | slot0 p50 | slot1 p50 | slot2 p50 | slot0 p90 | slot2 p90 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            "| {day} | {completed_n} | {cp50} | {cp90} | {s0} | {s1} | {s2} | {s0p90} | {s2p90} |".format(
                day=row["day"],
                completed_n=row["completed_n"],
                cp50=_fmt(row["completed_p50"]),
                cp90=_fmt(row["completed_p90"]),
                s0=_fmt(row["slot0_p50"]),
                s1=_fmt(row["slot1_p50"]),
                s2=_fmt(row["slot2_p50"]),
                s0p90=_fmt(row["slot0_p90"]),
                s2p90=_fmt(row["slot2_p90"]),
            )
        )
    if lots:
        rendered = ", ".join(f"{name}={count}" for name, count in lots.most_common())
        lines.append("")
        lines.append(f"Open-lot snapshot counts: {rendered}.")
        lines.append(
            "Slot 0 is the default tenant (it runs first). "
            "Slot 1 and slot 2 follow Mongo active-tenant order. "
            "On the 2026-10-31 snapshot, default has the high lot count "
            "(slot 0) and the other two tenants match slot 1 and slot 2."
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("--lots", type=Path, default=None)
    args = parser.parse_args(argv)
    summaries, completed = load_events(args.log)
    loops = segment_loops(summaries)
    rows = daily_report(loops, completed)
    lots = lot_counts(args.lots) if args.lots else None
    sys.stdout.write(render_markdown(rows, lots))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
