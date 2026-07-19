#!/usr/bin/env python3
"""Probe coin-fact layer end-to-end (memory + policy). No ledger writes.

  # Offline unit-style (fixtures → fake store path via real persist if Mongo up)
  python3 scripts/probe_coin_facts.py --seed-historical --verify

  # Live cycle with fixtures as fetch (no CMC scrape)
  python3 scripts/probe_coin_facts.py --sync-fixtures --symbols ALLO/USDT

  # Enable-config simulation + context via build_dca_context
  python3 scripts/probe_coin_facts.py --verify --symbol ALLO/USDT

Staging (Hermes / Railway test), after coin_facts.enabled=true:
  railway ssh -s xagent-hermes -e test -- python3 scripts/seed_coin_facts.py --verify
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

FIX = _ROOT / "tests" / "fixtures" / "cmc_ai"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed-historical", action="store_true")
    ap.add_argument("--sync-fixtures", action="store_true")
    ap.add_argument("--symbols", default="ALLO/USDT")
    ap.add_argument("--symbol", default="ALLO/USDT")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    report: dict = {"steps": []}

    import importlib.util

    def _seed_mod():
        p = _ROOT / "scripts" / "seed_coin_facts.py"
        spec = importlib.util.spec_from_file_location("seed_coin_facts", p)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        return mod

    seed = _seed_mod()

    if args.seed_historical:
        out = seed.seed_from_jsonl(seed.DEFAULT_SEED, dry_run=False)
        report["steps"].append({"seed_historical": out})
        print("seed:", json.dumps({k: v for k, v in out.items() if k != "ids"}, indent=2))

    if args.sync_fixtures:
        from intelligence.memory.coin_facts_ingest import sync_coin_facts

        def fetch(url: str) -> str:
            if "latest-updates" in url:
                return (FIX / "allora_latest_updates.html").read_text()
            if "price-analysis" in url:
                return (FIX / "allora_price_analysis.html").read_text()
            if "price-prediction" in url:
                return (FIX / "allora_price_prediction.html").read_text()
            return ""

        raw = {
            "memory": {
                "enabled": True,
                "coin_facts": {
                    "enabled": True,
                    "sources": {"cmc_ai": {"enabled": True, "max_coins_per_cycle": 5}},
                },
            }
        }
        syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
        out = sync_coin_facts(fetch_fn=fetch, config_raw=raw, symbols=syms)
        report["steps"].append({"sync_fixtures": out})
        print("sync:", json.dumps(out, indent=2))

    if args.list or args.verify or args.seed_historical or args.sync_fixtures:
        events = seed.list_facts(args.symbol, limit=40)
        print(f"\nlist {args.symbol}: n={len(events)}")
        for e in events[:15]:
            print(f"  {e.timestamp} {e.event_type:28} {e.impact_score:+.2f} {e.description[:65]}")

        if args.verify or args.seed_historical:
            v = seed.verify_policy(args.symbol)
            report["verify"] = v
            print("\nverify:", json.dumps(v, indent=2))
            if v.get("fact_event_count", 0) < 1:
                print("FAIL: no facts in context", file=sys.stderr)
                return 1
            codes = v.get("policy", {}).get("reason_codes") or []
            if not any(str(c).startswith("fact_") for c in codes):
                print("NOTE: no fact_* reason_codes (flags may be soft-only)")
            else:
                print("OK: policy emitted fact_* codes")

    if not any([args.seed_historical, args.sync_fixtures, args.verify, args.list]):
        ap.print_help()
        print("\nQuick start:\n  python3 scripts/probe_coin_facts.py --seed-historical --verify")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
