"""Decision-time entry lineage (D-LIN Rev 1).

Copies observations the decision already has onto the first fill.
Does not fetch indicators, does not add #600 snapshot keys, and does not
change sizing. Schema ``d-lin-1`` is a sibling of ``entry_snapshot``.

RECALL_FAILED: Hindsight is not reachable from this process.
"""

from __future__ import annotations

import math
import uuid
from typing import Any

SCHEMA = "d-lin-1"
SCHEMA_VERSION = 1

# #600 keys this module is allowed to fill. Not a schema extension.
SNAPSHOT_FIELDS = (
    "source",
    "timeframe",
    "strategy_profile",
    "strategy_tier",
    "rationale_codes",
    "rsi",
    "volume_factor",
    "atr",
    "volatility_tier",
    "size_before_mult",
    "size_after_mult",
    "cap_applied",
)

_TRACKED_MISSING = (
    "source",
    "timeframe",
    "rsi",
    "atr",
    "volume_factor",
    "rationale",
    "size_before_mult",
    "size_after_mult",
    "cap_applied",
)

EXTENDED_KEYS = frozenset({
    "bb_lower",
    "bb_middle",
    "bb_upper",
    "relvol_rank",
    "social_score",
    "social_sources",
    "oracle_state",
    "memory_ids",
    "atr_pct",
})

# Fields risk's rebuilt TradeOrder drops. Copied back; sizing fields are not.
ORDER_CARRY_FIELDS = (
    "decision_rsi",
    "decision_atr",
    "decision_atr_pct",
    "decision_volume_factor",
    "decision_rationale",
    "decision_rationale_codes",
    "decision_volatility_tier",
    "decision_strategy_profile",
    "decision_timeframe",
    "decision_size_before_mult",
    "decision_size_after_mult",
    "decision_cap_applied",
    "decision_context",
    "prior_attempt_id",
)

# Lot ``entry_source`` tokens from AC-LIN-2. Anything else (manual, grid, x,
# dca_scheduled) stays untagged so it does not stamp ``entry_at``. Sensor and
# gainer tags still come from the existing ``tagged_source`` helper.
LOT_ENTRY_CHANNELS = frozenset({
    "lc",
    "cmc",
    "auto",
    "entry_sensor_15m",
    "gainer_relvol",
})

# Only fields whose dataclass default is "absent" (None). Numeric defaults
# such as BB 0.0 or sentiment 0.0 are not treated as a supplied measurement.
_ANALYSIS_EXTENDED = {
    "ctx_oracle_state": "oracle_state",
    "relvol_rank": "relvol_rank",
}

_SECRET_PARTS = ("secret", "token", "password", "embedding", "credential", "api_key")


def _measured_atr_pct(value: Any) -> float | None:
    """``atr_pct <= 0`` is the SignalAnalysis default, not a measurement."""
    number, reason = _finite(value)
    if reason is not None or number is None or number <= 0:
        return None
    return number


def _channel_token(value: Any) -> str | None:
    text = _text(value)
    if text in LOT_ENTRY_CHANNELS:
        return text
    return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _finite(value: Any) -> tuple[float | None, str | None]:
    """Return (number, missing_reason). Bool is not a measurement."""
    if value is None or isinstance(value, bool):
        return None, "not_supplied"
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None, "not_numeric"
    if math.isnan(number) or math.isinf(number):
        return None, "non_finite"
    return number, None


def _clean_extended(raw: Any) -> dict:
    """Whitelist only. Drop embeddings and secret-like keys."""
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for key, value in raw.items():
        name = str(key)
        if name not in EXTENDED_KEYS:
            continue
        if any(part in name.lower() for part in _SECRET_PARTS):
            continue
        if name in ("social_sources", "memory_ids"):
            if not isinstance(value, (list, tuple)):
                continue
            cleaned = []
            for item in list(value)[:20]:
                if isinstance(item, (dict, list)):
                    continue
                text = _text(item)
                if text and not any(part in text.lower() for part in _SECRET_PARTS):
                    cleaned.append(text[:80])
            if cleaned:
                out[name] = cleaned
            continue
        number, reason = _finite(value)
        if reason is None and number is not None:
            out[name] = number
        else:
            text = _text(value)
            if text and name == "oracle_state":
                out[name] = text[:80]
    return out


def _extended_from_analysis(analysis: Any) -> dict:
    bag = _clean_extended(getattr(analysis, "decision_context", None))
    bag.update(_clean_extended(getattr(analysis, "lineage_extended", None)))
    for attr, key in _ANALYSIS_EXTENDED.items():
        if key in bag:
            continue
        if not hasattr(analysis, attr):
            continue
        value = getattr(analysis, attr)
        if value is None:
            continue
        bag.update(_clean_extended({key: value}))
    if "atr_pct" not in bag and hasattr(analysis, "atr_pct"):
        measured = _measured_atr_pct(getattr(analysis, "atr_pct"))
        if measured is not None:
            bag["atr_pct"] = measured
    memory_ids = getattr(analysis, "memory_ids", None)
    if memory_ids is not None and "memory_ids" not in bag:
        bag.update(_clean_extended({"memory_ids": memory_ids}))
    return bag


def stamp_decision_on_order(
    order: Any,
    analysis: Any = None,
    *,
    timeframe: str | None = None,
    source: str | None = None,
) -> None:
    """Copy already-present decision fields onto the order. Never fetches."""
    if order is None:
        return
    analysis = analysis if analysis is not None else order
    channel = _text(source) or _text(getattr(order, "source", None))
    if _text(getattr(order, "source", None)) is None and channel:
        # Do not overwrite a source risk or the caller already resolved,
        # and do not invent one. Only fill an empty source from the argument.
        order.source = channel

    def _set(name: str, value: Any) -> None:
        current = getattr(order, name, None)
        if current not in (None, "", [], {}):
            return
        if value in (None, "", [], {}):
            return
        setattr(order, name, value)

    rsi, _ = _finite(getattr(analysis, "rsi", None) if hasattr(analysis, "rsi") else None)
    # A duck-typed object without rsi stays missing. Do not read a HUD.
    if hasattr(analysis, "rsi"):
        _set("decision_rsi", rsi)

    if hasattr(analysis, "atr"):
        atr, _ = _finite(getattr(analysis, "atr"))
        _set("decision_atr", atr)
    if hasattr(analysis, "atr_pct"):
        _set("decision_atr_pct", _measured_atr_pct(getattr(analysis, "atr_pct")))

    volume = None
    volume_field = None
    if hasattr(analysis, "volume_factor"):
        volume, _ = _finite(getattr(analysis, "volume_factor"))
        volume_field = "volume_factor"
    if volume is None and hasattr(analysis, "vol_multiplier"):
        volume, _ = _finite(getattr(analysis, "vol_multiplier"))
        volume_field = "vol_multiplier"
    if (
        hasattr(analysis, "volume_factor")
        and hasattr(analysis, "vol_multiplier")
    ):
        left, left_reason = _finite(getattr(analysis, "volume_factor"))
        right, right_reason = _finite(getattr(analysis, "vol_multiplier"))
        if (
            left_reason is None
            and right_reason is None
            and left is not None
            and right is not None
            and left != right
        ):
            volume = None
            volume_field = "conflict"
    _set("decision_volume_factor", volume)
    if volume_field:
        _set("decision_volume_field", volume_field)

    rationale = _text(getattr(analysis, "rationale", None)) if hasattr(analysis, "rationale") else None
    _set("decision_rationale", rationale)
    codes = getattr(analysis, "rationale_codes", None)
    if isinstance(codes, (list, tuple)) and codes:
        _set("decision_rationale_codes", [str(c) for c in codes if _text(c)])
    _set(
        "decision_volatility_tier",
        _text(getattr(analysis, "volatility_tier", None)) if hasattr(analysis, "volatility_tier") else None,
    )
    _set(
        "decision_strategy_profile",
        _text(getattr(analysis, "strategy_profile", None)) if hasattr(analysis, "strategy_profile") else None,
    )
    _set("decision_timeframe", _text(timeframe) or _text(getattr(analysis, "timeframe", None)))
    _set("decision_source", channel)

    extended = _extended_from_analysis(analysis)
    if extended:
        current = getattr(order, "decision_context", None)
        merged = _clean_extended(current)
        merged.update(extended)
        order.decision_context = merged


def copy_decision_lineage(src: Any, dst: Any) -> None:
    """Put decision observations back on an order object risk rebuilt.

    Does not copy price, qty, or usdt_amount.
    """
    if src is None or dst is None or src is dst:
        return
    for name in ORDER_CARRY_FIELDS:
        if not hasattr(src, name):
            continue
        incoming = getattr(src, name, None)
        if incoming in (None, "", [], {}):
            continue
        current = getattr(dst, name, None)
        if current not in (None, "", [], {}):
            continue
        try:
            setattr(dst, name, incoming)
        except Exception:
            continue
    volume_field = getattr(src, "decision_volume_field", None)
    if volume_field and not getattr(dst, "decision_volume_field", None):
        try:
            dst.decision_volume_field = volume_field
        except Exception:
            pass
    decision_source = _text(getattr(src, "decision_source", None))
    if decision_source and not _text(getattr(dst, "decision_source", None)):
        try:
            dst.decision_source = decision_source
        except Exception:
            pass


def stamp_size_from_decision(order: Any, decision: Any) -> None:
    """Copy size numbers the risk decision already exposes. Do not derive a cap."""
    if order is None or decision is None:
        return
    details = getattr(decision, "details", None)
    if isinstance(details, dict):
        if "planned_usdt" in details:
            number, reason = _finite(details.get("planned_usdt"))
            if reason is None:
                if getattr(order, "decision_size_before_mult", None) is None:
                    order.decision_size_before_mult = number
        if "final_usdt" in details:
            number, reason = _finite(details.get("final_usdt"))
            if reason is None and getattr(order, "decision_size_after_mult", None) is None:
                order.decision_size_after_mult = number
        if "cap_applied" in details and isinstance(details.get("cap_applied"), bool):
            if getattr(order, "decision_cap_applied", None) is None:
                order.decision_cap_applied = details["cap_applied"]
    if getattr(order, "decision_size_after_mult", None) is None:
        number, reason = _finite(getattr(order, "usdt_amount", None))
        if reason is None and number is not None and number > 0:
            order.decision_size_after_mult = number


def decision_lineage_from_order(order: Any, *, timeframe: str | None = None) -> dict | None:
    """Versioned block for the order row. None when the order carries no decision fields."""
    if order is None:
        return None
    stamped_names = (
        "decision_rsi",
        "decision_atr",
        "decision_atr_pct",
        "decision_volume_factor",
        "decision_rationale",
        "decision_rationale_codes",
        "decision_context",
        "decision_source",
        "decision_timeframe",
        "decision_size_before_mult",
        "decision_size_after_mult",
        "decision_volatility_tier",
        "decision_strategy_profile",
        "decision_cap_applied",
    )
    if not any(getattr(order, name, None) not in (None, "", [], {}) for name in stamped_names):
        return None
    source = _text(getattr(order, "decision_source", None)) or _text(getattr(order, "source", None))
    tf = _text(getattr(order, "decision_timeframe", None)) or _text(timeframe)
    rsi = getattr(order, "decision_rsi", None)
    atr = getattr(order, "decision_atr", None)
    volume = getattr(order, "decision_volume_factor", None)
    rationale = _text(getattr(order, "decision_rationale", None))
    codes = getattr(order, "decision_rationale_codes", None)
    profile = _text(getattr(order, "decision_strategy_profile", None))
    vol_tier = _text(getattr(order, "decision_volatility_tier", None))
    size_before = getattr(order, "decision_size_before_mult", None)
    size_after = getattr(order, "decision_size_after_mult", None)
    cap = getattr(order, "decision_cap_applied", None)
    extended = _clean_extended(getattr(order, "decision_context", None))
    atr_pct = getattr(order, "decision_atr_pct", None)
    if atr_pct is not None and "atr_pct" not in extended:
        measured = _measured_atr_pct(atr_pct)
        if measured is not None:
            extended["atr_pct"] = measured
    block = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "source": source,
        "timeframe": tf,
        "rsi": rsi,
        "atr": atr,
        "volume_factor": volume,
        "rationale": rationale,
        "rationale_codes": list(codes) if isinstance(codes, (list, tuple)) else None,
        "strategy_profile": profile,
        "volatility_tier": vol_tier,
        "size_before_mult": size_before,
        "size_after_mult": size_after,
        "cap_applied": cap if isinstance(cap, bool) else None,
        "extended": extended,
    }
    volume_field = _text(getattr(order, "decision_volume_field", None))
    provenance = {}
    if volume_field:
        provenance["volume_factor"] = volume_field
    if "atr_pct" in extended and atr is None:
        provenance["atr"] = "atr_pct_kept_in_extended"
    if volume_field == "conflict":
        block["volume_factor"] = None
    if provenance:
        block["provenance"] = provenance
    block["missing"] = _missing_map(block)
    if volume_field == "conflict":
        block["missing"]["volume_factor"] = "volume_factor_conflict"
    return block


def lineage_from_mapping(raw: Any, *, timeframe: str | None = None, source: str | None = None) -> dict | None:
    """Normalize an explicit caller dict. Unknown keys are dropped."""
    if not isinstance(raw, dict):
        return None
    extended = _clean_extended(raw.get("extended") or raw.get("decision_context"))
    if raw.get("atr_pct") is not None and "atr_pct" not in extended:
        measured = _measured_atr_pct(raw.get("atr_pct"))
        if measured is not None:
            extended["atr_pct"] = measured
    codes = raw.get("rationale_codes")
    block = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "source": _text(raw.get("source")) or _text(source),
        "timeframe": _text(raw.get("timeframe")) or _text(timeframe),
        "rsi": _finite(raw.get("rsi"))[0],
        "atr": _finite(raw.get("atr"))[0],
        "volume_factor": _finite(raw.get("volume_factor"))[0],
        "rationale": _text(raw.get("rationale")),
        "rationale_codes": [str(c) for c in codes if _text(c)] if isinstance(codes, (list, tuple)) else None,
        "strategy_profile": _text(raw.get("strategy_profile")),
        "strategy_tier": _text(raw.get("strategy_tier")),
        "volatility_tier": _text(raw.get("volatility_tier")),
        "size_before_mult": _finite(raw.get("size_before_mult"))[0],
        "size_after_mult": _finite(raw.get("size_after_mult"))[0],
        "cap_applied": raw.get("cap_applied") if isinstance(raw.get("cap_applied"), bool) else None,
        "extended": extended,
    }
    if raw.get("entry_order_id"):
        block["entry_order_id"] = _text(raw.get("entry_order_id"))
    if raw.get("entry_allocation_id"):
        block["entry_allocation_id"] = _text(raw.get("entry_allocation_id"))
    provenance = {}
    if "atr_pct" in extended and block["atr"] is None:
        provenance["atr"] = "atr_pct_kept_in_extended"
    if provenance:
        block["provenance"] = provenance
    block["missing"] = _missing_map(block)
    return block


def _missing_map(block: dict) -> dict:
    missing = {}
    for name in _TRACKED_MISSING:
        value = block.get(name)
        if name == "cap_applied":
            if not isinstance(value, bool):
                missing[name] = "not_supplied"
            continue
        if value in (None, "", [], {}):
            if name == "atr" and isinstance(block.get("extended"), dict) and "atr_pct" in block["extended"]:
                missing[name] = "atr_pct_not_atr"
            elif name == "rationale" and block.get("rationale_codes"):
                missing[name] = "codes_only"
            else:
                missing[name] = "not_supplied"
    codes = block.get("rationale_codes")
    if not codes and block.get("rationale"):
        missing["rationale_codes"] = "text_only"
    elif not codes:
        missing["rationale_codes"] = "not_supplied"
    return missing


def snapshot_update_kwargs(lineage: dict | None) -> dict:
    """#600 kwargs only. Omits nulls so the snapshot default stays null."""
    if not isinstance(lineage, dict):
        return {}
    out = {}
    for name in SNAPSHOT_FIELDS:
        # ``timeframe`` is positional on update_position. The snapshot uses
        # that argument. Passing it again raises TypeError.
        if name == "timeframe":
            continue
        if name not in lineage:
            continue
        value = lineage.get(name)
        if name == "rationale_codes":
            if isinstance(value, (list, tuple)):
                out[name] = [str(c) for c in value if _text(c)]
            continue
        if name == "cap_applied":
            if isinstance(value, bool):
                out[name] = value
            continue
        if value in (None, "", [], {}):
            continue
        out[name] = value
    return out


def allocation_id_for(order_id: str | None) -> str:
    text = _text(order_id)
    if text:
        return f"ord:{text}"
    return f"alloc:{uuid.uuid4().hex}"


def lot_lineage_record(lineage: dict | None, *, order_id: str | None = None) -> dict | None:
    """First-fill lot block. Does not mutate the caller's dict."""
    if not isinstance(lineage, dict):
        return None
    record = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "source": lineage.get("source"),
        "timeframe": lineage.get("timeframe"),
        "rsi": lineage.get("rsi"),
        "atr": lineage.get("atr"),
        "volume_factor": lineage.get("volume_factor"),
        "rationale": lineage.get("rationale"),
        "rationale_codes": list(lineage["rationale_codes"]) if isinstance(lineage.get("rationale_codes"), list) else lineage.get("rationale_codes"),
        "strategy_profile": lineage.get("strategy_profile"),
        "volatility_tier": lineage.get("volatility_tier"),
        "size_before_mult": lineage.get("size_before_mult"),
        "size_after_mult": lineage.get("size_after_mult"),
        "cap_applied": lineage.get("cap_applied") if isinstance(lineage.get("cap_applied"), bool) else None,
        "extended": _clean_extended(lineage.get("extended")),
        "missing": dict(lineage.get("missing") or {}),
        "entry_order_id": _text(lineage.get("entry_order_id")) or _text(order_id),
        "entry_allocation_id": _text(lineage.get("entry_allocation_id")) or allocation_id_for(
            _text(lineage.get("entry_order_id")) or _text(order_id)
        ),
    }
    if isinstance(lineage.get("provenance"), dict):
        record["provenance"] = {
            str(k): v for k, v in lineage["provenance"].items()
            if not any(part in str(k).lower() for part in _SECRET_PARTS)
        }
    if not record["missing"]:
        record["missing"] = _missing_map(record)
    return record


def new_lot_entry_source(
    *,
    entry_source: str | None,
    source: str | None,
    lineage: dict | None,
    new_lot: bool,
    tagged_source: str | None = None,
) -> str | None:
    """Channel token for a new lot. An open lot is not backfilled.

    ``tagged_source`` is the existing guard/cap tag (sensor, gainer). The
    caller computes it so this module does not import the portfolio service.
    """
    explicit = _text(entry_source)
    if explicit:
        return explicit
    if _text(tagged_source):
        return _text(tagged_source)
    if not new_lot:
        return None
    if isinstance(lineage, dict):
        channel = _channel_token(lineage.get("source"))
        if channel:
            return channel
    return _channel_token(source)


def allocation_from_lot(pos: dict | None, *, exit_order_id: str | None, qty: float | None) -> dict | None:
    """One exit leg from the lot's stored id. No symbol/time lookup."""
    if not isinstance(pos, dict):
        return None
    lineage = pos.get("entry_lineage")
    if not isinstance(lineage, dict):
        return None
    alloc = _text(lineage.get("entry_allocation_id"))
    if not alloc:
        return {
            "schema": SCHEMA,
            "linked": False,
            "reason": "no_entry_allocation_id",
            "exit_order_id": _text(exit_order_id),
        }
    snap = pos.get("entry_snapshot") if isinstance(pos.get("entry_snapshot"), dict) else {}
    return {
        "schema": SCHEMA,
        "linked": True,
        "entry_allocation_id": alloc,
        "entry_order_id": _text(lineage.get("entry_order_id")),
        "entry_source": _text(lineage.get("source")) or _text(snap.get("source")) or _text(pos.get("entry_source")),
        "exit_order_id": _text(exit_order_id),
        "qty": qty,
    }


def bind_exit_allocations(
    exit_order_id: str | None,
    lots: list[dict] | None,
    *,
    qty_by_index: list[float] | None = None,
) -> dict:
    """Keep every supplied lot's stored id. Do not search other lots."""
    allocations = []
    unlinked = 0
    for index, lot in enumerate(list(lots or [])):
        qty = None
        if qty_by_index is not None and index < len(qty_by_index):
            qty = qty_by_index[index]
        leg = allocation_from_lot(lot, exit_order_id=exit_order_id, qty=qty)
        if leg is None or not leg.get("linked"):
            unlinked += 1
            if isinstance(leg, dict):
                allocations.append(leg)
            else:
                allocations.append({
                    "schema": SCHEMA,
                    "linked": False,
                    "reason": "no_entry_allocation_id",
                    "exit_order_id": _text(exit_order_id),
                })
            continue
        allocations.append(leg)
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "exit_order_id": _text(exit_order_id),
        "allocations": allocations,
        "unlinked": unlinked,
    }


def order_is_fill(record: dict | None) -> bool:
    """Executed status only. An attempt_only row is never a fill."""
    if not isinstance(record, dict):
        return False
    if record.get("attempt_only") is True:
        return False
    try:
        from core.models import OrderStatus, is_executed_status

        status = record.get("status")
        if OrderStatus.try_legacy(status) in {
            OrderStatus.REJECTED,
            OrderStatus.CANCELED,
            OrderStatus.QUEUED,
            OrderStatus.ACTIVE,
            OrderStatus.PARTIALLY_FILLED,
        }:
            return False
        return bool(is_executed_status(status))
    except Exception:
        return False


def attempt_fields(record_id: str, *, idempotency_key: str | None, prior_attempt_id: str | None, rejected: bool) -> dict:
    attempt_id = _text(record_id) or ""
    correlation = _text(idempotency_key) or attempt_id
    out = {
        "attempt_id": attempt_id,
        "correlation_id": correlation,
    }
    prior = _text(prior_attempt_id)
    if prior:
        out["prior_attempt_id"] = prior
    if rejected:
        out["attempt_only"] = True
    return out


def project_lot(lot: Any) -> dict:
    """Read projection. Drops secret-like keys. Does not recompute indicators."""
    if not isinstance(lot, dict):
        return {}
    out: dict = {}
    for key, value in lot.items():
        if any(part in str(key).lower() for part in _SECRET_PARTS):
            continue
        if key == "entry_snapshot" and isinstance(value, dict):
            out[key] = {
                sk: sv
                for sk, sv in value.items()
                if not any(part in str(sk).lower() for part in _SECRET_PARTS)
            }
            continue
        if key == "entry_lineage":
            projected = public_lineage(value)
            if projected is not None:
                out[key] = projected
            continue
        out[key] = value
    return out


def public_lineage(block: Any) -> dict | None:
    """MCP-safe lineage. Stored numbers only. No secret-like keys."""
    if not isinstance(block, dict):
        return None
    extended = _clean_extended(block.get("extended"))
    missing = block.get("missing") if isinstance(block.get("missing"), dict) else {}
    out = {
        "schema": SCHEMA if block.get("schema") else None,
        "schema_version": block.get("schema_version"),
        "source": block.get("source"),
        "timeframe": block.get("timeframe"),
        "rsi": block.get("rsi"),
        "atr": block.get("atr"),
        "volume_factor": block.get("volume_factor"),
        "rationale": _text(block.get("rationale")),
        "rationale_codes": list(block["rationale_codes"]) if isinstance(block.get("rationale_codes"), list) else None,
        "strategy_profile": block.get("strategy_profile"),
        "volatility_tier": block.get("volatility_tier"),
        "size_before_mult": block.get("size_before_mult"),
        "size_after_mult": block.get("size_after_mult"),
        "cap_applied": block.get("cap_applied") if isinstance(block.get("cap_applied"), bool) else None,
        "missing": {str(k): str(v) for k, v in missing.items() if not any(part in str(k).lower() for part in _SECRET_PARTS)},
        "extended": extended,
        "entry_order_id": _text(block.get("entry_order_id")),
        "entry_allocation_id": _text(block.get("entry_allocation_id")),
        "provenance": {},
    }
    provenance = block.get("provenance") if isinstance(block.get("provenance"), dict) else {}
    for key, value in provenance.items():
        if any(part in str(key).lower() for part in _SECRET_PARTS):
            continue
        if any(part in str(value).lower() for part in _SECRET_PARTS):
            continue
        out["provenance"][str(key)] = value
    if not out["provenance"]:
        out.pop("provenance")
    return out


def public_exit(block: Any) -> dict | None:
    if not isinstance(block, dict):
        return None
    allocations = []
    for leg in block.get("allocations") or []:
        if not isinstance(leg, dict):
            continue
        allocations.append({
            "schema": SCHEMA,
            "linked": bool(leg.get("linked")),
            "reason": _text(leg.get("reason")),
            "entry_allocation_id": _text(leg.get("entry_allocation_id")),
            "entry_order_id": _text(leg.get("entry_order_id")),
            "entry_source": _text(leg.get("entry_source")),
            "exit_order_id": _text(leg.get("exit_order_id")),
            "qty": leg.get("qty"),
        })
    return {
        "schema": SCHEMA,
        "exit_order_id": _text(block.get("exit_order_id")),
        "allocations": allocations,
        "unlinked": block.get("unlinked"),
    }


def public_order_fields(record: dict | None) -> dict:
    if not isinstance(record, dict):
        return {}
    out = {
        "attempt_id": _text(record.get("attempt_id")),
        "correlation_id": _text(record.get("correlation_id")),
        "prior_attempt_id": _text(record.get("prior_attempt_id")),
        "is_fill": order_is_fill(record),
    }
    lineage = public_lineage(record.get("entry_lineage"))
    if lineage is not None:
        out["entry_lineage"] = lineage
    exit_block = public_exit(record.get("exit_lineage"))
    if exit_block is not None:
        out["exit_lineage"] = exit_block
    return out


def _lot_source(lot: dict) -> str:
    snap = lot.get("entry_snapshot") if isinstance(lot.get("entry_snapshot"), dict) else {}
    lineage = lot.get("entry_lineage") if isinstance(lot.get("entry_lineage"), dict) else {}
    return _text(snap.get("source")) or _text(lineage.get("source")) or _text(lot.get("entry_source")) or "missing"


def _supplied(lot: dict, name: str) -> bool:
    snap = lot.get("entry_snapshot") if isinstance(lot.get("entry_snapshot"), dict) else {}
    lineage = lot.get("entry_lineage") if isinstance(lot.get("entry_lineage"), dict) else {}
    value = snap.get(name) if name in snap else lineage.get(name)
    return value not in (None, "", [], {})


def lineage_coverage(lots: list[dict] | None, orders: list[dict] | None = None) -> dict:
    """Offline completeness. Does not measure the operational 90% target."""
    rows = [lot for lot in (lots or []) if isinstance(lot, dict)]
    by_source: dict[str, dict] = {}
    missing_reasons: dict[str, int] = {}
    supplied_kept = 0
    supplied_seen = 0
    indicator_complete = 0
    for lot in rows:
        source = _lot_source(lot)
        bucket = by_source.setdefault(source, {
            "lots": 0,
            "with_rsi": 0,
            "with_atr": 0,
            "with_volume_factor": 0,
            "with_source": 0,
            "indicator_complete": 0,
        })
        bucket["lots"] += 1
        has_rsi = _supplied(lot, "rsi")
        has_atr = _supplied(lot, "atr")
        has_vol = _supplied(lot, "volume_factor")
        has_source = source != "missing"
        bucket["with_rsi"] += int(has_rsi)
        bucket["with_atr"] += int(has_atr)
        bucket["with_volume_factor"] += int(has_vol)
        bucket["with_source"] += int(has_source)
        if has_rsi and has_atr and has_vol and has_source:
            indicator_complete += 1
            bucket["indicator_complete"] += 1
        lineage = lot.get("entry_lineage") if isinstance(lot.get("entry_lineage"), dict) else {}
        missing = lineage.get("missing") if isinstance(lineage.get("missing"), dict) else {}
        for key, reason in missing.items():
            label = f"{key}:{reason}"
            missing_reasons[label] = missing_reasons.get(label, 0) + 1
        # A fixture lot is "fully supplied" when it has no missing map entries
        # for rsi/atr/volume/source, or when the caller set fully_supplied.
        marked = lot.get("fully_supplied") is True
        fields_ok = has_rsi and has_atr and has_vol and has_source
        if marked or (fields_ok and not any(k in missing for k in ("rsi", "atr", "volume_factor", "source"))):
            supplied_seen += 1
            snap = lot.get("entry_snapshot") if isinstance(lot.get("entry_snapshot"), dict) else {}
            if fields_ok and snap.get("rsi") == lineage.get("rsi") and snap.get("atr") == lineage.get("atr"):
                supplied_kept += 1
            elif fields_ok and not lineage:
                supplied_kept += 1
            elif fields_ok and snap.get("rsi") == lineage.get("rsi", snap.get("rsi")) and snap.get("volume_factor") == lineage.get("volume_factor", snap.get("volume_factor")):
                supplied_kept += 1

    exits = []
    for order in orders or []:
        if not isinstance(order, dict):
            continue
        if not order_is_fill(order):
            continue
        side = str(order.get("side") or order.get("type") or "").lower()
        if side not in {"sell", "cover"}:
            continue
        exits.append(order)
    linked = 0
    for order in exits:
        block = order.get("exit_lineage") if isinstance(order.get("exit_lineage"), dict) else {}
        legs = [leg for leg in (block.get("allocations") or []) if isinstance(leg, dict) and leg.get("linked") and _text(leg.get("entry_allocation_id"))]
        if legs:
            linked += 1
    total = len(rows)
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "runtime_target_measured": False,
        "lots_total": total,
        "missing_denominator": total,
        "indicator_complete": indicator_complete,
        "by_source": by_source,
        "missing_reasons": missing_reasons,
        "fully_supplied_lots": supplied_seen,
        "fully_supplied_retained": supplied_kept,
        "exits_total": len(exits),
        "exits_linked": linked,
    }
