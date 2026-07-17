"""CMC DexScan smart-money alerts — observability only (no auto-trade)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime

import requests

from logger import log


@dataclass
class DexScanAlert:
    symbol: str
    platform: str
    signal_type: str
    rationale: str
    gate_tradeable: bool = False
    alert_id: str = ""


class CMCDexScanAlertProvider:
    """Fetch trending DEX tokens and flag Gate-listable symbols."""

    BASE_URL = "https://pro-api.coinmarketcap.com/v1"

    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.getenv("CMC_API_KEY", "")

    def _headers(self) -> dict:
        return {"X-CMC_PRO_API_KEY": self.api_key, "Accept": "application/json"}

    def fetch_alerts(self, limit: int = 10) -> list[DexScanAlert]:
        if not self.api_key:
            return []
        try:
            from core.config import get_bot_config
            from data.cmc_capabilities import has_dexscan_endpoint, probe_capabilities

            dex_cfg = (get_bot_config().cmc_config.get("dexscan_alerts") or {})
            if dex_cfg.get("require_endpoint", True):
                caps = probe_capabilities(self.api_key)
                if not has_dexscan_endpoint(caps):
                    log(
                        "DexScan skipped — endpoint not on this CMC plan "
                        f"({caps.get('plan_label', '?')})",
                        "INFO",
                    )
                    return []
        except Exception:
            pass
        alerts = []
        try:
            url = f"{self.BASE_URL}/dex/tokens/trending/list"
            resp = requests.post(
                url,
                headers=self._headers(),
                json={"limit": limit},
                timeout=15,
            )
            if resp.status_code != 200:
                err = resp.json().get("status", {}).get("error_message", resp.status_code)
                log(f"DexScan trending unavailable: {err}", "WARNING")
                return []
            for item in resp.json().get("data", [])[:limit]:
                sym = (item.get("sym") or item.get("symbol") or "").upper()
                if not sym:
                    continue
                platform = str(item.get("plt", item.get("platform", "?")))
                sig = item.get("sig") or {}
                mtp = sig.get("mtp", "")
                alerts.append(DexScanAlert(
                    symbol=sym,
                    platform=platform,
                    signal_type=str(mtp or "trending"),
                    rationale=f"DexScan {mtp or 'trending'} on {platform}",
                    gate_tradeable=False,
                    alert_id=f"dex_{platform}_{sym}_{datetime.now().strftime('%Y%m%d')}",
                ))
        except Exception as e:
            log(f"DexScan fetch error: {e}", "WARNING")
            return alerts

        if alerts:
            try:
                from price_fetcher import get_prices_batch

                candidates = [f"{a.symbol}/USDT" for a in alerts]
                prices = get_prices_batch(candidates)
                for alert in alerts:
                    sym = f"{alert.symbol}/USDT"
                    alert.gate_tradeable = float(prices.get(sym, 0) or 0) > 0
            except Exception:
                pass
        return alerts


def get_dexscan_provider() -> CMCDexScanAlertProvider:
    return CMCDexScanAlertProvider()