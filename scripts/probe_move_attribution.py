#!/usr/bin/env python3
"""Local probe for 1h screen + 15m drill move attribution (no Hermes deploy needed).

Default is DRY-RUN: in-memory store only, never writes Mongo.

  # Pure offline (synthetic candles) — always safe
  python3 scripts/probe_move_attribution.py --offline

  # Live OHLCV (Gate/Binance via MarketService), still dry-run store
  python3 scripts/probe_move_attribution.py --symbols BTC/USDT,ETH/USDT,SOL/USDT

  # Include watchlist top N if config/watchlist available
  python3 scripts/probe_move_attribution.py --from-watchlist --top 15

  # Actually write to Mongo (only after dry-run looks good)
  python3 scripts/probe_move_attribution.py --symbols SOL/USDT --write
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _offline_demo() -> dict:
    """Synthetic 1h large move + 15m impulse + unlock trigger."""
    from datetime import datetime, timedelta, timezone

    from intelligence.memory.models import MarketEvent
    from intelligence.memory.move_attribution import (
        MoveSnap,
        build_attribution_event,
        find_triggers,
        is_large_move,
        strongest_bar_impulse,
        sync_move_attribution,
    )

    class MemStore:
        def __init__(self):
            self.events = {}

        def get_event(self, eid):
            return self.events.get(eid)

        def upsert_event(self, ev):
            self.events[ev.event_id] = ev
            return True

        def list_events(self, symbol=None, event_type=None, since_iso=None, limit=50):
            out = list(self.events.values())
            if symbol:
                b = symbol.split("/")[0]
                out = [e for e in out if symbol in (e.symbols or []) or any(b in s for s in (e.symbols or []))]
            return out[:limit]

    store = MemStore()
    now = datetime.now(timezone.utc)
    store.upsert_event(
        MarketEvent(
            event_id="probe_unlock",
            timestamp=(now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            event_type="token_unlock",
            symbols=["SOL/USDT"],
            impact_score=-0.3,
            description="Probe SOL unlock narrative",
            source="probe",
        )
    )
    # candle math smoke
    imp, volx = strongest_bar_impulse(
        [100, 101, 102, 108, 107],
        [1, 1, 1, 6, 1],
        window=4,
    )
    print(f"offline candle math: impulse={imp} volx={volx}")

    move = MoveSnap(
        "SOL/USDT",
        chg_1h=5.2,
        chg_24h=5.2,
        screen_tf="1h",
        source="offline_synthetic",
        fine_tf="15m",
        fine_impulse_pct=2.8,
        fine_impulse_vol_x=2.1,
        fine_bars_scanned=8,
        vs_btc=3.5,
    )
    print(f"is_large_move(1h 5.2% thr=4)={is_large_move(move, abs_chg=4.0, rel_btc=3.0)}")
    triggers = find_triggers(store, move, lookback_hours=72, max_triggers=5)
    print(f"triggers={len(triggers)} top={triggers[0].event_type if triggers else None}")
    ev = build_attribution_event(move, triggers)
    print(f"event_desc={ev.description}")
    print(f"metadata_keys={sorted(ev.metadata.keys())}")

    out = sync_move_attribution(
        store,
        config_raw={
            "memory": {
                "enabled": True,
                "move_attribution": {"enabled": True, "abs_chg_1h_pct": 4, "index_rag": False},
            }
        },
        symbols=["SOL/USDT"],
        moves=[move],
    )
    print("sync:", json.dumps(out, indent=2))
    return out


def _live_dry(symbols: list[str], *, write: bool) -> dict:
    from intelligence.memory.move_attribution import (
        apply_15m_drill,
        fetch_move_snaps,
        is_large_move,
        move_attribution_config,
        sync_move_attribution,
    )

    cfg = move_attribution_config()
    print(f"config: abs_1h={cfg['abs_chg_1h_pct']} rel_btc={cfg['rel_btc_1h_pct']} fine_bars={cfg['fine_bars']}")
    print(f"symbols ({len(symbols)}): {symbols[:12]}{'...' if len(symbols)>12 else ''}")

    snaps = fetch_move_snaps(symbols)
    print(f"1h snaps fetched: {len(snaps)}")
    if not snaps:
        print("WARN: no OHLCV snaps — check network / exchange (Gate). Falling back offline demo.")
        return _offline_demo()

    abs_thr = float(cfg["abs_chg_1h_pct"])
    rel_thr = float(cfg["rel_btc_1h_pct"])
    large = []
    for s in sorted(snaps, key=lambda x: -abs(x.chg_pct)):
        ok = is_large_move(s, abs_chg=abs_thr, rel_btc=rel_thr)
        flag = "LARGE" if ok else "  ok "
        print(
            f"  {flag} {s.symbol:12} 1h={s.chg_pct:+6.2f}% "
            f"vsBTC={s.vs_btc if s.vs_btc is not None else float('nan'):+6.2f} "
            f"src={s.source} px={s.price:.6g}"
        )
        if ok:
            large.append(s)

    print(f"large candidates: {len(large)}")
    for s in large[:10]:
        apply_15m_drill(s, cfg=cfg)
        print(
            f"  15m drill {s.symbol}: impulse={s.fine_impulse_pct} "
            f"volx={s.fine_impulse_vol_x} bars={s.fine_bars_scanned}"
        )

    if not write:
        # dry-run attribution with in-memory store (still runs find_triggers if Mongo available)
        class MemStore:
            def __init__(self):
                self.events = {}

            def get_event(self, eid):
                return self.events.get(eid)

            def upsert_event(self, ev):
                self.events[ev.event_id] = ev
                print(f"  [dry-write] {ev.event_type} {ev.symbols} {ev.description[:100]}")
                return True

            def list_events(self, **kw):
                # try real mongo for trigger search if available
                try:
                    from intelligence.memory.store import MemoryStore

                    return MemoryStore().list_events(**kw)
                except Exception:
                    return list(self.events.values())

        store = MemStore()
        out = sync_move_attribution(
            store,
            symbols=symbols,
            moves=large if large else snaps[:0],  # only large
            config_raw={
                "memory": {
                    "enabled": True,
                    "move_attribution": {
                        **{k: cfg[k] for k in cfg},
                        "index_rag": False,
                    },
                }
            },
        )
        # if no large, still show we would skip
        if not large:
            out = {
                "enabled": True,
                "dry_run": True,
                "moves_seen": len(snaps),
                "moves_large": 0,
                "note": "no 1h moves above threshold — try more volatile symbols or lower abs_chg_1h_pct",
            }
        else:
            out["dry_run"] = True
            out["would_write"] = out.get("attributions_written", 0)
        print("result:", json.dumps(out, indent=2, default=str))
        return out

    # LIVE write path
    print("WRITE MODE: persisting to Mongo memory_* ...")
    out = sync_move_attribution(symbols=symbols)
    print("result:", json.dumps(out, indent=2, default=str))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--offline", action="store_true", help="Synthetic candles only, no network")
    ap.add_argument("--symbols", default="", help="Comma list e.g. BTC/USDT,ETH/USDT")
    ap.add_argument("--from-watchlist", action="store_true")
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument(
        "--write",
        action="store_true",
        help="Write attribution events to Mongo (default: dry-run in-memory)",
    )
    args = ap.parse_args()

    if args.offline:
        out = _offline_demo()
        ok = bool(out.get("attributions_written") or out.get("moves_large") is not None)
        print("OFFLINE_OK" if ok else "OFFLINE_FAIL")
        return 0 if ok else 1

    symbols: list[str] = []
    if args.symbols:
        for s in args.symbols.split(","):
            s = s.strip().upper()
            if s and "/" not in s:
                s = f"{s}/USDT"
            if s:
                symbols.append(s)
    if args.from_watchlist or not symbols:
        try:
            from data_manager import load_effective_watchlist

            for c in load_effective_watchlist() or []:
                if not (c or {}).get("active", True):
                    continue
                s = str((c or {}).get("symbol") or "").upper()
                if s and "/" not in s:
                    s = f"{s}/USDT"
                if s and s not in symbols:
                    symbols.append(s)
        except Exception as e:
            print(f"watchlist skip: {e}")
        if not symbols:
            symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT"]
    symbols = symbols[: max(1, int(args.top))]

    out = _live_dry(symbols, write=bool(args.write))
    if out.get("enabled") is False and out.get("reason"):
        print("PROBE_FAIL", out)
        return 1
    print("PROBE_OK dry_run=" + str(not args.write))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
