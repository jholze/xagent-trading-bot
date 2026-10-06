#!/usr/bin/env python3
"""Apply the live-caps overlay through the existing tenant-config save path.

Reads one JSON file, deep-merges it into the named tenant's stored body, and
writes only that patch (``data_manager.patch_config`` → tenant meta ``$set``).
The operator ``config.json`` and profile presets are not written.

A config-history snapshot is taken only when the stored body actually changes.
A second run with the same overlay prints the same effective config and does
not add a snapshot.

The tenant id is an argument. This script does not name a tenant.

``trading_mode=live`` reads ``live.max_usdt_per_trade`` as the ticket cap
(``RiskManager._base_usdt_cap``), for buys and for short opens. The overlay
sets that key to the same number as top-level ``max_usdt_per_trade`` so the
existing reader binds. It does not set ``live_max_loss_usdt`` (nothing reads
that key). It must not set ``live.dry_run``, ``live.execution``,
``allow_live``, or ``fire_enabled``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

DEFAULT_OVERLAY = _REPO / "deploy" / "live_caps_overlay.json"

_SECRET_PARTS = (
    "secret",
    "password",
    "token",
    "api_key",
    "api_secret",
    "private",
    "credential",
)


def redact_secrets(value):
    """Copy ``value`` with secret-like keys replaced. Values are not logged."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            name = str(key).lower()
            if any(part in name for part in _SECRET_PARTS):
                out[key] = "[redacted]"
            else:
                out[key] = redact_secrets(item)
        return out
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    return value


def _forbidden_overlay_key(overlay: dict) -> str:
    """Flags this PR must not write. Presence in the overlay is a hard error."""
    live = overlay.get("live") if isinstance(overlay.get("live"), dict) else {}
    if "dry_run" in live:
        return "live.dry_run"
    if "execution" in live:
        return "live.execution"
    shorts = overlay.get("shorts") if isinstance(overlay.get("shorts"), dict) else {}
    if "allow_live" in overlay or "allow_live" in shorts:
        return "allow_live"
    mcp = overlay.get("mcp") if isinstance(overlay.get("mcp"), dict) else {}
    if "allow_live" in mcp:
        return "mcp.allow_live"

    def _walk(node, path: str) -> str:
        if isinstance(node, dict):
            for key, item in node.items():
                here = f"{path}.{key}" if path else str(key)
                if key == "fire_enabled":
                    return here
                found = _walk(item, here)
                if found:
                    return found
        return ""

    return _walk(overlay, "")


def load_overlay(path: str | Path | None = None) -> dict:
    overlay_path = Path(path) if path else DEFAULT_OVERLAY
    data = json.loads(overlay_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not data:
        raise ValueError(f"overlay {overlay_path} must be a non-empty JSON object")
    forbidden = _forbidden_overlay_key(data)
    if forbidden:
        raise ValueError(f"overlay must not set {forbidden}")
    return data


def apply_live_caps(tenant_id: str, overlay_path: str | Path | None = None) -> dict:
    """Merge the overlay into ``tenant_id`` and return the effective config.

    Raises if the tenant id is missing, the store is unavailable, or the
    effective config fails the existing save validation. Does not write when
    the stored body already contains the overlay.
    """
    from core.config_guardrails import validate_config_for_save
    from core.tenant_context import DEFAULT_TENANT
    from core.trading_profiles import deep_merge_dicts
    from data_manager import (
        _apply_trading_profile_merge,
        _load_default_config_from_disk,
        _load_tenant_config_body,
        _should_use_mongo_for_tenant_config,
        patch_config,
    )
    from storage.config_history import config_write_lock, record_snapshot

    tid = str(tenant_id or "").strip()
    if not tid or tid == DEFAULT_TENANT:
        raise ValueError("tenant id is required and must not be the default tenant")
    overlay = load_overlay(overlay_path)
    default_cfg = _load_default_config_from_disk()
    if not _should_use_mongo_for_tenant_config(default_cfg):
        raise RuntimeError("tenant config store unavailable")
    body = _load_tenant_config_body(tid, default_cfg) or {}
    if not isinstance(body, dict):
        body = {}
    merged_body = deep_merge_dicts(body, overlay)
    effective = _apply_trading_profile_merge(default_cfg, merged_body)
    validate_config_for_save(effective)
    if merged_body != body:
        with config_write_lock():
            # Empty first body is a real change, so snapshot {} (not None).
            record_snapshot(body if body else {}, tenant_id=tid)
            wrote = patch_config(overlay, tenant_id=tid)
            if not wrote:
                raise RuntimeError("tenant config patch was not persisted")
            stored = _load_tenant_config_body(tid, default_cfg) or {}
            effective = _apply_trading_profile_merge(default_cfg, stored)
    return effective


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply the live-caps tenant overlay")
    parser.add_argument("tenant_id", help="Tenant whose override body receives the overlay")
    parser.add_argument(
        "--overlay",
        default=str(DEFAULT_OVERLAY),
        help="Path to the overlay JSON (default: deploy/live_caps_overlay.json)",
    )
    args = parser.parse_args(argv)
    effective = apply_live_caps(args.tenant_id, args.overlay)
    json.dump(redact_secrets(effective), sys.stdout, indent=2, sort_keys=True, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"apply_live_caps_overlay: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
