"""#630 Daily Gainer Desk (observe only).

Labels each monitored gainer/RelVol candidate:

- hit — accepted fill intent on the #628 tape
- late — tape rejected by the named Ist late-entry rule
  (``gainer_chase_guard`` in services/gainer_universe/chase_guard.py).
  No other late rule is named in Ist; none is invented here.
- missed — scanner or list had the coin and no tape row linked
- rejected — tape outcome ``rejected`` plus that row's filter codes

Sources are the #628 tape plus existing gainer lists: gainer-universe
live_top (scanner), eligible (list), and #208 GIS leaders (list).
#597-style symbol lists are accepted as ``source=list`` when a caller
passes them. This module does not revise membership (#631) and does
not add a soft-gate, score, size change, or buy filter.

fire_enabled is not a switch here. It stays false even if config sets
it. The order path is unchanged.

Durable file: logs/daily_gainer_desk.jsonl under logger.LOG_DIR.
Same Ist bar as #628: append_jsonl + maybe_rotate_jsonl at
max_bytes=8_000_000, keep_lines=20_000. Fail-open on emit. The row
whitelist drops secrets and any other extra fields.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from typing import Any

from logger import log
from services.gis_monitor.pure import is_gainer_source, normalize_symbol

DESK_FILENAME = "daily_gainer_desk.jsonl"
ROTATE_MAX_BYTES = 8_000_000
ROTATE_KEEP_LINES = 20_000
TAPE_TAIL_LIMIT = 5_000
_UNDER_TEST_ENV = "DAILY_GAINER_DESK_UNDER_TEST"
_CONFIG_KEY = "daily_gainer_desk"

BUCKETS = ("hit", "late", "missed", "rejected")
DESK_SOURCES = ("tape", "scanner", "list")

# Only named Ist late-entry rule. Absent code → not late (UNCLEAR, not invented).
LATE_ENTRY_CODES = frozenset({"gainer_chase_guard"})

ROW_KEYS = (
    "ts",
    "symbol",
    "bucket",
    "signal",
    "filter_codes",
    "source",
    "correlation_id",
)

_lock = threading.Lock()
_emitted: set[tuple] = set()


def desk_path() -> str:
    from logger import LOG_DIR

    return os.path.join(LOG_DIR, DESK_FILENAME)


def _now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def desk_fire_enabled(_config: dict | None = None) -> bool:
    """Money flag. Always false. Config cannot turn it on."""
    return False


def desk_enabled(config: dict | None = None) -> bool:
    """Env DAILY_GAINER_DESK=0/1/false/true overrides; else config; else True.

    Default on matches #628 so a staging soak records rows without a
    config edit. This flag only controls emit. It is not a buy gate.
    """
    try:
        env = (os.environ.get("DAILY_GAINER_DESK") or "").strip().lower()
        if env in ("0", "false"):
            return False
        if env in ("1", "true"):
            return True
        if config is None:
            try:
                from core.config import get_bot_config

                config = get_bot_config().raw
            except Exception:
                config = None
        sec = (config or {}).get(_CONFIG_KEY) if isinstance(config, dict) else None
        if isinstance(sec, dict) and "enabled" in sec:
            return bool(sec.get("enabled"))
        return True
    except Exception:
        return True


def _file_write_allowed() -> bool:
    if os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get(_UNDER_TEST_ENV) != "1":
        return False
    return True


def _reset_emit_cache_for_tests() -> None:
    with _lock:
        _emitted.clear()


def _copy_signal(signal: Any) -> dict[str, Any] | None:
    """Whitelist name/source. Missing parts are null. Empty → null."""
    name: str | None = None
    source: str | None = None
    if isinstance(signal, str):
        name = signal.strip() or None
    elif isinstance(signal, dict):
        raw_name = signal.get("name")
        raw_source = signal.get("source")
        if isinstance(raw_name, str) and raw_name.strip():
            name = raw_name.strip()
        if isinstance(raw_source, str) and raw_source.strip():
            source = raw_source.strip()
    else:
        return None
    if name is None and source is None:
        return None
    return {"name": name, "source": source}


def _is_gainer_signal(signal: Any) -> bool:
    copied = _copy_signal(signal)
    if not copied:
        return False
    name = str(copied.get("name") or "")
    source = str(copied.get("source") or "")
    upper = name.upper()
    if upper.startswith("GAINER_") or name.lower().startswith("gainer"):
        return True
    if source and (is_gainer_source(source) or source.lower().startswith("relvol")):
        return True
    return False


def _filter_codes(row: dict) -> list[str] | None:
    """Copy Ist codes. Missing or non-list → null, not an invented code."""
    if "filter_codes" not in row or row.get("filter_codes") is None:
        return None
    raw = row.get("filter_codes")
    if not isinstance(raw, list):
        return None
    out: list[str] = []
    for code in raw:
        if isinstance(code, str) and code.strip():
            out.append(code.strip())
    return out


def _correlation_id(row: dict) -> str | None:
    if "correlation_id" not in row or row.get("correlation_id") is None:
        return None
    raw = row.get("correlation_id")
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    return text or None


def _ts_key(row: dict, index: int) -> tuple:
    raw = row.get("ts")
    text = raw.strip() if isinstance(raw, str) else ""
    return (text, index)


def _latest(rows: list[tuple[int, dict]]) -> dict:
    return max(rows, key=lambda item: _ts_key(item[1], item[0]))[1]


class _Acc:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.origin: str | None = None
        self.candidate_signal: dict[str, Any] | None = None
        self.tapes: list[tuple[int, dict]] = []


def _note_origin(acc: _Acc, origin: Any) -> None:
    if origin not in ("scanner", "list"):
        return
    if acc.origin is None or origin == "scanner":
        acc.origin = origin


def classify_gainer_desk(
    candidates: list[dict] | None,
    tape_rows: list[dict] | None,
) -> list[dict]:
    """Join candidates to tape rows. Does not write and does not trade.

    A tape row links a candidate by normalized symbol. A gainer/RelVol
    tape row is itself a monitored candidate when no list row exists.
    Other tape rows are not a desk universe on their own.
    """
    by_symbol: dict[str, _Acc] = {}

    for cand in candidates or []:
        if not isinstance(cand, dict):
            continue
        sym = normalize_symbol(str(cand.get("symbol") or ""))
        if not sym:
            continue
        acc = by_symbol.setdefault(sym, _Acc(sym))
        _note_origin(acc, cand.get("source"))
        if acc.candidate_signal is None:
            copied = _copy_signal(cand.get("signal"))
            if copied is not None:
                acc.candidate_signal = copied

    for index, row in enumerate(tape_rows or []):
        if not isinstance(row, dict):
            continue
        sym = normalize_symbol(str(row.get("symbol") or ""))
        if not sym:
            continue
        outcome = str(row.get("outcome") or "").strip().lower()
        if outcome not in ("accepted", "rejected"):
            continue
        monitored = sym in by_symbol or _is_gainer_signal(row.get("signal"))
        if not monitored:
            continue
        acc = by_symbol.setdefault(sym, _Acc(sym))
        acc.tapes.append((index, row))

    out: list[dict] = []
    for sym in sorted(by_symbol):
        built = _build_row(by_symbol[sym])
        if built is not None:
            out.append(built)
    return out


def _build_row(acc: _Acc) -> dict | None:
    accepted = [item for item in acc.tapes if str(item[1].get("outcome") or "").lower() == "accepted"]
    rejected = [item for item in acc.tapes if str(item[1].get("outcome") or "").lower() == "rejected"]
    ts = _now_ts()
    if accepted:
        win = _latest(accepted)
        signal = _copy_signal(win.get("signal")) or acc.candidate_signal
        return _row(
            ts=ts,
            symbol=acc.symbol,
            bucket="hit",
            signal=signal,
            filter_codes=[],
            source="tape",
            correlation_id=_correlation_id(win),
        )
    if rejected:
        win = _latest(rejected)
        codes = _filter_codes(win)
        late = bool(codes) and any(code in LATE_ENTRY_CODES for code in codes)
        signal = _copy_signal(win.get("signal")) or acc.candidate_signal
        return _row(
            ts=ts,
            symbol=acc.symbol,
            bucket="late" if late else "rejected",
            signal=signal,
            filter_codes=codes,
            source="tape",
            correlation_id=_correlation_id(win),
        )
    if acc.origin not in ("scanner", "list"):
        return None
    return _row(
        ts=ts,
        symbol=acc.symbol,
        bucket="missed",
        signal=acc.candidate_signal,
        filter_codes=None,
        source=acc.origin,
        correlation_id=None,
    )


def _row(
    *,
    ts: str,
    symbol: str,
    bucket: str,
    signal: dict[str, Any] | None,
    filter_codes: list[str] | None,
    source: str,
    correlation_id: str | None,
) -> dict:
    return {
        "ts": ts,
        "symbol": symbol,
        "bucket": bucket,
        "signal": signal,
        "filter_codes": filter_codes,
        "source": source,
        "correlation_id": correlation_id,
    }


def _emit_key(row: dict) -> tuple:
    codes = row.get("filter_codes")
    code_key: tuple | None
    if codes is None:
        code_key = None
    else:
        code_key = tuple(codes)
    signal = row.get("signal")
    if isinstance(signal, dict):
        sig_key = (signal.get("name"), signal.get("source"))
    else:
        sig_key = None
    return (
        row.get("symbol"),
        row.get("bucket"),
        row.get("source"),
        row.get("correlation_id"),
        code_key,
        sig_key,
    )


def _emit_rows(rows: list[dict]) -> int:
    """Append new rows. Fail-open. Returns how many lines were written."""
    if not rows:
        return 0
    if not _file_write_allowed():
        return 0
    pending: list[tuple[tuple, dict]] = []
    with _lock:
        for row in rows:
            if set(row.keys()) != set(ROW_KEYS):
                continue
            if row.get("bucket") not in BUCKETS or row.get("source") not in DESK_SOURCES:
                continue
            key = _emit_key(row)
            if key in _emitted:
                continue
            pending.append((key, row))
        if not pending:
            return 0
        try:
            from services.observability_store import append_jsonl, maybe_rotate_jsonl

            path = desk_path()
            written = 0
            for key, row in pending:
                append_jsonl(path, row)
                _emitted.add(key)
                written += 1
            maybe_rotate_jsonl(path, max_bytes=ROTATE_MAX_BYTES, keep_lines=ROTATE_KEEP_LINES)
        except Exception as exc:
            log(f"daily_gainer_desk emit failed: {exc}", "WARNING")
            return 0
    if written:
        counts = {name: 0 for name in BUCKETS}
        for _key, row in pending[:written]:
            bucket = str(row.get("bucket") or "")
            if bucket in counts:
                counts[bucket] += 1
        log(
            "daily_gainer_desk wrote n={n} hit={hit} late={late} "
            "missed={missed} rejected={rejected}".format(n=written, **counts),
            "INFO",
        )
    return written


def observe_gainer_desk(
    candidates: list[dict] | None,
    tape_rows: list[dict] | None,
    *,
    config: dict | None = None,
) -> list[dict]:
    """Classify and emit. Returns the classified rows. Never raises."""
    try:
        if not desk_enabled(config):
            return []
        rows = classify_gainer_desk(candidates, tape_rows)
        _emit_rows(rows)
        return rows
    except Exception as exc:
        log(f"daily_gainer_desk observe failed: {exc}", "WARNING")
        return []


def read_buy_decision_tape(*, limit: int = TAPE_TAIL_LIMIT) -> list[dict]:
    """Recent #628 rows. Fail-open to an empty window."""
    try:
        from services.observability_store import tail_jsonl
        from strategies.buy_decision_tape import tape_path

        rows = tail_jsonl(tape_path(), limit=max(1, int(limit)))
        return [row for row in rows if isinstance(row, dict)]
    except Exception:
        return []


def candidates_from_gainer_state(state: dict | None) -> list[dict]:
    """live_top and streaks → scanner. eligible → list. Dedupe, scanner wins."""
    if not isinstance(state, dict):
        return []
    out: list[dict] = []
    seen: set[str] = set()

    def _add(rows: Any, source: str) -> None:
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            sym = normalize_symbol(str(row.get("symbol") or ""))
            if not sym or sym in seen:
                continue
            seen.add(sym)
            out.append({"symbol": sym, "source": source, "signal": None})

    _add(state.get("live_top"), "scanner")
    _add(state.get("streaks"), "scanner")
    _add(state.get("eligible"), "list")
    return out


def candidates_from_gis_leaders(leaders: list | None) -> list[dict]:
    """#208 GIS leader rows → source list. Extra leader fields are dropped."""
    return candidates_from_symbol_list(leaders, source="list")


def candidates_from_symbol_list(symbols: list | None, *, source: str = "list") -> list[dict]:
    """#597-style names or ``{symbol, signal}`` rows. source is scanner|list."""
    if source not in ("scanner", "list"):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for item in symbols or []:
        signal = None
        if isinstance(item, dict):
            raw = item.get("symbol")
            signal = _copy_signal(item.get("signal"))
        else:
            raw = item
        sym = normalize_symbol(str(raw or ""))
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append({"symbol": sym, "source": source, "signal": signal})
    return out


def observe_scanner_state(state: dict | None, *, config: dict | None = None) -> list[dict]:
    """Join a gainer-universe snapshot to the tape. Fail-open."""
    try:
        if not desk_enabled(config):
            return []
        return observe_gainer_desk(
            candidates_from_gainer_state(state),
            read_buy_decision_tape(),
            config=config,
        )
    except Exception as exc:
        log(f"daily_gainer_desk scan observe failed: {exc}", "WARNING")
        return []


def observe_gis_leaders(
    leaders: list | None,
    *,
    config: dict | None = None,
    tape_rows: list[dict] | None = None,
) -> list[dict]:
    """Join #208 leaders to the tape. Fail-open. Does not edit the report."""
    try:
        if not desk_enabled(config):
            return []
        rows = tape_rows if tape_rows is not None else read_buy_decision_tape()
        return observe_gainer_desk(
            candidates_from_gis_leaders(leaders),
            rows,
            config=config,
        )
    except Exception as exc:
        log(f"daily_gainer_desk list observe failed: {exc}", "WARNING")
        return []


def _order_correlation(order: Any) -> str | None:
    for attr in ("idempotency_key", "client_order_id", "order_id"):
        val = str(getattr(order, attr, "") or "").strip()
        if val:
            return val
    return None


def emit_desk_from_decision(
    order: Any,
    decision: Any,
    *,
    source: str | None = None,
    config: dict | None = None,
) -> None:
    """One observe row for a gainer/RelVol BUY that reached evaluate.

    Never mutates *order* or *decision*. Non-gainer buys are ignored.
    """
    try:
        if not desk_enabled(config):
            return
        if str(getattr(order, "type", "") or "").upper() != "BUY":
            return
        name = str(getattr(order, "signal", "") or "").strip()
        src = source if source is not None else str(getattr(order, "source", "") or "")
        src = str(src or "").strip()
        signal = {"name": name or None, "source": src or None}
        if not _is_gainer_signal(signal):
            return
        approved = bool(getattr(decision, "approved", False))
        if approved:
            outcome = "accepted"
            codes: list[str] | None = []
        else:
            outcome = "rejected"
            code = str(getattr(decision, "code", "") or "").strip()
            codes = [code] if code else None
        tape_row = {
            "ts": _now_ts(),
            "symbol": getattr(order, "symbol", "") or "",
            "outcome": outcome,
            "signal": signal,
            "filter_codes": codes,
            "correlation_id": _order_correlation(order),
        }
        observe_gainer_desk([], [tape_row], config=config)
    except Exception as exc:
        log(f"daily_gainer_desk decision emit failed: {exc}", "WARNING")
