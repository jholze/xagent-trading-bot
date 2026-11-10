"""Parse scanner rows into measurement identities. No pass/fail decision."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from strategies.climax_fade import normalize_climax_symbol

from services.short_signal_feed.schema import SCHEMA_ID, SCHEMA_REVISION

_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


def load_jsonl(path: str | Path) -> list:
    """Read a caller-supplied JSONL file. Invalid lines stay as markers."""
    items: list = []
    text = Path(path).read_text(encoding="utf-8")
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            items.append({"_parse_error": True, "line_no": line_no, "raw": line})
            continue
        if isinstance(value, dict):
            value = dict(value)
            value["_line_no"] = line_no
        items.append(value)
    return items


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or _TS.fullmatch(value) is None:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc)


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def identity_key(record: dict) -> str:
    parts = [
        str(record.get("schema_id") or ""),
        str(record.get("schema_revision") or ""),
        str(record.get("source_commit") or ""),
        str(record.get("venue") or ""),
        str(record.get("market_type") or ""),
        normalize_climax_symbol(_text(record.get("symbol"))),
        str(record.get("as_of") or ""),
        str(record.get("timeframe") or ""),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def malformed_key(record: object) -> str:
    if isinstance(record, dict) and record.get("_parse_error") is True:
        raw = f"json|{record.get('line_no')}|{record.get('raw')}"
    else:
        raw = f"record|{record!r}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validate_identity(record: object) -> tuple[dict | None, str | None, str | None]:
    """Return ``(identity, status_reason, detail)``.

    ``status_reason`` is None when the row can be measured. A reason means
    the feature numbers stay null. It is not an order reject.
    """
    if isinstance(record, dict) and record.get("_parse_error") is True:
        return None, "malformed", "json"
    if not isinstance(record, dict):
        return None, "malformed", "record"

    symbol = normalize_climax_symbol(_text(record.get("symbol")))
    as_of_text = record.get("as_of")
    as_of = parse_timestamp(as_of_text)
    commit = record.get("source_commit")
    venue = _text(record.get("venue"))
    market_type = _text(record.get("market_type"))
    timeframe = record.get("timeframe")
    schema_id = record.get("schema_id")
    schema_revision = record.get("schema_revision")

    if not symbol:
        return None, "malformed", "symbol"
    if as_of is None:
        return None, "malformed", "as_of"
    if not isinstance(commit, str) or _COMMIT.fullmatch(commit) is None:
        return None, "malformed", "source_commit"
    if venue is None:
        return None, "malformed", "venue"
    if market_type is None:
        return None, "malformed", "market_type"
    if not isinstance(timeframe, str) or not timeframe.strip():
        return None, "malformed", "timeframe"
    if not isinstance(schema_id, str) or not isinstance(schema_revision, str):
        return None, "malformed", "schema"

    identity = {
        "signal_key": identity_key(record),
        "symbol": symbol,
        "as_of": as_of,
        "as_of_text": as_of_text,
        "source_commit": commit,
        "venue": venue,
        "market_type": market_type,
        "timeframe": timeframe.strip(),
        "schema_id": schema_id,
        "schema_revision": schema_revision,
        "record": record,
    }
    if schema_id != SCHEMA_ID or schema_revision != SCHEMA_REVISION:
        detail = "schema_id" if schema_id != SCHEMA_ID else "schema_revision"
        return identity, "schema_mismatch", detail
    if timeframe.strip() != "4h":
        return identity, "malformed", "timeframe"
    return identity, None, None
