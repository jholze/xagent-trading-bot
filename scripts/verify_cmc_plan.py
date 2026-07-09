#!/usr/bin/env python3
"""Verify CMC API key plan tier and bot-relevant endpoints (read-only)."""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv

load_dotenv(os.path.join(ROOT, ".env"))

import requests

BASE = "https://pro-api.coinmarketcap.com/v1"

# Endpoints the bot actually uses (see data/cmc_*.py)
ENDPOINTS = [
    {
        "id": "trending/latest",
        "method": "GET",
        "path": "/cryptocurrency/trending/latest",
        "params": {"limit": 3},
        "min_plan": "Builder",
        "bot_use": "Trending-Watchlist (MAGMA, volatile Coins)",
    },
    {
        "id": "trending/gainers-losers",
        "method": "GET",
        "path": "/cryptocurrency/trending/gainers-losers",
        "params": {"time_period": "24h", "limit": 3},
        "min_plan": "Builder",
        "bot_use": "Watchlist-Fallback + Social-Signale",
    },
    {
        "id": "listings/latest",
        "method": "GET",
        "path": "/cryptocurrency/listings/latest",
        "params": {"limit": 3, "sort": "percent_change_24h"},
        "min_plan": "Basic",
        "bot_use": "Fallback wenn trending/* 403",
    },
    {
        "id": "community/trending/token",
        "method": "GET",
        "path": "/community/trending/token",
        "params": {"limit": 3},
        "min_plan": "Builder",
        "bot_use": "CMC Community BUY/SELL-Signale",
    },
    {
        "id": "quotes/latest",
        "method": "GET",
        "path": "/cryptocurrency/quotes/latest",
        "params": {"symbol": "BTC,ETH"},
        "min_plan": "Basic",
        "bot_use": "Quotes-Fallback-Signale",
    },
    {
        "id": "dex/tokens/trending/list",
        "method": "POST",
        "path": "/dex/tokens/trending/list",
        "json": {"limit": 3},
        "min_plan": "Startup",
        "bot_use": "DexScan-Alerts (/dexsignals)",
    },
]


def _mask_key(key: str) -> str:
    key = (key or "").strip()
    if len(key) <= 8:
        return "(nicht gesetzt oder zu kurz)"
    return f"{key[:4]}…{key[-4:]}"


def _probe(key: str, spec: dict) -> dict:
    headers = {"X-CMC_PRO_API_KEY": key, "Accept": "application/json"}
    url = f"{BASE}{spec['path']}"
    try:
        if spec["method"] == "POST":
            resp = requests.post(
                url,
                headers=headers,
                json=spec.get("json") or {},
                timeout=15,
            )
        else:
            resp = requests.get(
                url,
                headers=headers,
                params=spec.get("params") or {},
                timeout=15,
            )
    except Exception as exc:
        return {"ok": False, "status": 0, "error": str(exc), "items": 0}

    error = ""
    items = 0
    if resp.status_code == 200:
        data = resp.json().get("data")
        if isinstance(data, list):
            items = len(data)
        elif isinstance(data, dict):
            items = sum(len(data.get(k) or []) for k in ("gainers", "losers"))
            if not items:
                items = len(data)
        else:
            items = 1 if data else 0
    else:
        try:
            error = resp.json().get("status", {}).get("error_message", "") or resp.text[:120]
        except Exception:
            error = resp.text[:120]

    return {
        "ok": resp.status_code == 200,
        "status": resp.status_code,
        "error": error,
        "items": items,
    }


def _fetch_key_info(key: str) -> dict:
    headers = {"X-CMC_PRO_API_KEY": key, "Accept": "application/json"}
    try:
        resp = requests.get(f"{BASE}/key/info", headers=headers, timeout=15)
        if resp.status_code != 200:
            return {}
        data = resp.json().get("data", {}) or {}
        plan = data.get("plan", {}) or {}
        usage = data.get("usage", {}) or {}
        return {
            "plan_name": str(plan.get("plan_name") or plan.get("name") or ""),
            "credits_monthly": int(plan.get("credit_limit_monthly") or 0),
            "rate_limit_per_min": int(plan.get("rate_limit_minute") or 0),
            "credits_used": int(usage.get("current_month", {}).get("credits_used") or 0),
        }
    except Exception:
        return {}


def _infer_plan(results: list[tuple[dict, dict]], key_info: dict) -> str:
    name = (key_info.get("plan_name") or "").strip()
    if name:
        credits = key_info.get("credits_monthly") or 0
        rate = key_info.get("rate_limit_per_min") or 0
        extra = ""
        if credits:
            extra = f" — {credits:,} credits/mo"
        if rate:
            extra += f", {rate}/min"
        return f"{name}{extra}"

    ok_ids = {spec["id"] for spec, res in results if res["ok"]}
    if "dex/tokens/trending/list" in ok_ids:
        return "Startup oder höher (DexScan + vermutlich WebSocket-fähig)"
    if "trending/latest" in ok_ids and "community/trending/token" in ok_ids:
        return "Builder (oder höher) — Trending + Community OK"
    if "listings/latest" in ok_ids:
        return "Basic/Builder — listings/quotes OK, Trending/Community evtl. 403"
    return "Unbekannt — Key ungültig oder Plan nicht erkannt"


def _print_setup_guide(base_url: str | None) -> None:
    print("\n=== WAS DU MIR / DEM BOT GEBEN MUSST ===\n")
    print("1) CMC Pro API Key (Pflicht)")
    print("   Wo holen:")
    print("   • https://pro.coinmarketcap.com/login")
    print("   • Menü: API → Dashboard (oder Account → API)")
    print("   • Nach Builder-Upgrade: neuen Key erzeugen (alte Keys bleiben oft auf Free!)")
    print("   Wohin:")
    print("   • Lokal:  trading_bot/.env  →  CMC_API_KEY=dein_neuer_key")
    print("   • Railway Test:")
    print("     railway variables --service xagent-test --environment test \\")
    print("       --set \"CMC_API_KEY=dein_neuer_key\"")
    print("   • Danach Bot redeploy / restart")
    print()
    print("2) Signal-Webhook Token (optional, für CMC/TradingView-Alerts)")
    print("   Selbst wählen: langer Zufallsstring (z.B. openssl rand -hex 24)")
    print("   Wohin:")
    print("   • Railway: SIGNAL_WEBHOOK_TOKEN=...")
    print("   • Alert-Dienst sendet Header: X-Signal-Token: <token>")
    print(f"   • URL Test: {base_url or 'https://xagent-test-test.up.railway.app'}/api/signals/webhook?source=cmc")
    print()
    print("3) Was du NICHT an mich schicken musst")
    print("   • API-Key nicht in Chat/Git committen")
    print("   • Nur sagen: 'Key ist in Railway gesetzt' — ich teste per Script/Health")
    print()
    print("=== CMC ALERTS → BOT (kein nativer CMC-Webhook im Builder) ===\n")
    print("CMC Builder hat KEIN WebSocket und keinen direkten Custom-Webhook.")
    print("Optionen:")
    print("A) CMC App/Website Price Alerts → CryptocurrencyAlerting.com")
    print("   → Webhook URL auf unseren /api/signals/webhook zeigen")
    print("B) TradingView Alert → gleiche Webhook-URL mit ?source=tradingview")
    print("C) WebSocket-Preise: erst ab CMC Startup ($79) — oder Gate/ccxt für Exits")
    print()
    print("=== NACH KEY-UPDATE ERWARTUNG IN BOT-LOGS (Builder) ===\n")
    print("• Watchlist: 'CMC listings movers watchlist' (kein 403-Spam trending/*)")
    print("• Signale: listings → quotes/latest (8er-Batches)")
    print("• Keine DexScan/Community/Content-Calls wenn 403")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify CMC API plan vs bot endpoints")
    parser.add_argument(
        "--key",
        help="Override CMC_API_KEY (default: env / .env)",
    )
    parser.add_argument(
        "--health-url",
        default=os.getenv("BOT_HEALTH_URL", "https://xagent-test-test.up.railway.app/health/detail"),
        help="Optional remote health check (no CMC key sent)",
    )
    args = parser.parse_args()

    key = (args.key or os.getenv("CMC_API_KEY") or "").strip()
    print("=== CMC PLAN CHECK (read-only) ===\n")
    print(f"Key: {_mask_key(key)}")

    if not key:
        print("\nFEHLER: CMC_API_KEY nicht gesetzt.")
        _print_setup_guide(None)
        return 1

    key_info = _fetch_key_info(key)
    if key_info:
        used = key_info.get("credits_used", 0)
        monthly = key_info.get("credits_monthly", 0)
        print(
            f"key/info: plan={key_info.get('plan_name') or '?'}  "
            f"credits={used:,}/{monthly:,}  "
            f"rate={key_info.get('rate_limit_per_min')}/min"
        )

    results: list[tuple[dict, dict]] = []
    print(f"\n{'Endpoint':<28} {'HTTP':>4} {'Items':>5}  Status")
    print("-" * 72)
    for spec in ENDPOINTS:
        res = _probe(key, spec)
        results.append((spec, res))
        mark = "OK" if res["ok"] else "FAIL"
        err = f" — {res['error'][:50]}" if res.get("error") and not res["ok"] else ""
        print(f"{spec['id']:<28} {res['status']:>4} {res['items']:>5}  {mark}{err}")

    plan = _infer_plan(results, key_info)
    print(f"\nErkanntes Plan-Niveau: {plan}")

    listings_ok = any(res["ok"] for spec, res in results if spec["id"] == "listings/latest")
    quotes_ok = any(res["ok"] for spec, res in results if spec["id"] == "quotes/latest")
    trending_ok = any(res["ok"] for spec, res in results if spec["id"] == "trending/latest")
    if listings_ok and quotes_ok and not trending_ok:
        print("\n✓ Builder-typisch: listings + quotes nutzbar, Trending/Community/DexScan 403.")
        print("  Bot-Config: source_priority=[listings/latest], dexscan disabled.")
    elif not listings_ok:
        print("\n⚠️  listings/latest fehlgeschlagen — Key ungültig oder Plan gesperrt.")
    elif not trending_ok:
        print("\n⚠️  trending/latest 403 — für Builder normal; Bot nutzt listings/quotes.")

    if args.health_url:
        try:
            hr = requests.get(args.health_url, timeout=10)
            if hr.status_code == 200:
                body = hr.json()
                print(f"\nRemote {args.health_url}")
                print(f"  redis: {body.get('redis')}")
                print(f"  price_cache: {body.get('price_cache_last_refresh')}")
                print(f"  signal_webhooks: {len(body.get('signal_webhook_recent') or [])} recent")
            else:
                print(f"\nRemote health: HTTP {hr.status_code}")
        except Exception as exc:
            print(f"\nRemote health skipped: {exc}")

    _print_setup_guide("https://xagent-test-test.up.railway.app")
    return 0 if (listings_ok and quotes_ok) else 2


if __name__ == "__main__":
    raise SystemExit(main())