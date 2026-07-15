"""Shared work hoisted once per price-loop iteration (multi-tenant)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SharedCycleSignals:
    x_signals: list
    cmc_signals: list
    lc_signals: list


def union_tenant_watchlists(*, test: bool = False) -> list[dict]:
    """Merge active coins from all tenant watchlists (dedupe by symbol)."""
    from core.tenant_routing import iter_price_cycle_tenants, tenant_cycle_context
    from data_manager import load_effective_watchlist

    merged: list[dict] = []
    seen: set[str] = set()
    for tenant_id in iter_price_cycle_tenants(test=test):
        with tenant_cycle_context(tenant_id, test=test):
            for coin in load_effective_watchlist():
                sym = str(coin.get("symbol") or "").strip()
                if not sym or sym in seen:
                    continue
                seen.add(sym)
                merged.append(dict(coin))
    return merged


def sync_global_watchlist_once(bot_config) -> None:
    """Dry-run / gate pruning once per loop (default tenant scope)."""
    from core.tenant_context import DEFAULT_TENANT
    from core.tenant_routing import tenant_cycle_context
    from data_manager import prune_non_gate_watchlist_sources
    from services.dry_run_watchlist import sync_trending_watchlist_once

    with tenant_cycle_context(DEFAULT_TENANT):
        prune_non_gate_watchlist_sources(bot_config.raw)
        sync_trending_watchlist_once(bot_config)


def prepare_shared_cycle_signals(
    *,
    bot_config,
    social_pipeline=None,
    analyzer=None,
) -> SharedCycleSignals:
    """Fetch social signals once using the union watchlist across tenants."""
    union_watchlist = union_tenant_watchlists()
    x_signals: list = []
    cmc_signals: list = []
    lc_signals: list = []

    if social_pipeline:
        from services.background_runtime import (
            get_last_accuracy,
            register_pipeline,
            request_social_fetch,
            run_social_cycle_sync,
            social_ever_fetched,
            social_fetch_fresh,
        )

        register_pipeline(social_pipeline)
        arch = bot_config.architecture_config
        bg_social = arch.get("background_social_enabled", True)
        max_age = float(arch.get("social_snapshot_max_age_sec", 300))

        if bg_social:
            if not social_ever_fetched():
                run_social_cycle_sync(union_watchlist)
            elif not social_fetch_fresh(max_age):
                request_social_fetch(union_watchlist)
            _ = get_last_accuracy()
        else:
            social_pipeline.run_cycle_fetches(union_watchlist)

        use_snapshot = arch.get("use_signal_snapshot")
        if use_snapshot:
            from bus.signals import signal_snapshot_store

            cached = signal_snapshot_store.get_signals(
                max_age_sec=float(arch.get("social_snapshot_max_age_sec", 300))
            )
            if cached:
                x_signals, cmc_signals, lc_signals = cached
            else:
                x_signals = social_pipeline.refresh_signals()
                cmc_signals = social_pipeline.refresh_cmc_signals()
                lc_signals = social_pipeline.refresh_lc_signals()
        else:
            x_signals = social_pipeline.refresh_signals()
            cmc_signals = social_pipeline.refresh_cmc_signals()
            lc_signals = social_pipeline.refresh_lc_signals()
    elif analyzer:
        x_signals = analyzer.get_top_signals() or []

    return SharedCycleSignals(
        x_signals=x_signals,
        cmc_signals=cmc_signals,
        lc_signals=lc_signals,
    )