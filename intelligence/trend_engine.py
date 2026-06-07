from logger import log


class TrendEngine:
    """Cross-validates scanner momentum candidates with X social mentions."""

    def __init__(self, scanner=None):
        self._scanner = scanner

    @property
    def scanner(self):
        if self._scanner is None:
            from scanners.zct_screener import ZCTAltcoinScanner
            self._scanner = ZCTAltcoinScanner()
        return self._scanner

    def _x_coin_set(self, x_signals: list) -> set:
        coins = set()
        for signal in x_signals or []:
            if isinstance(signal, dict):
                coin = signal.get("coin")
            else:
                coin = getattr(signal, "coin", None)
            if coin and coin != "UNKNOWN":
                coins.add(str(coin).upper())
        return coins

    def cross_validate(self, x_signals: list, scanner_results: list = None, run_scan: bool = False) -> list:
        x_coins = self._x_coin_set(x_signals)
        if not x_coins:
            return []

        if scanner_results is None and run_scan:
            try:
                scanner_results = self.scanner.run_full_scan(target_coins=12)
            except Exception as e:
                log(f"TrendEngine scanner failed: {e}", "WARNING")
                return []

        if not scanner_results:
            return []

        candidates = []
        for row in scanner_results:
            symbol = row.get("symbol", "")
            base = symbol.split("/")[0].upper() if symbol else ""
            if base in x_coins:
                candidates.append({
                    "symbol": symbol,
                    "base": base,
                    "change_5m": row.get("change_5m", 0),
                    "change_1d": row.get("change_1d", 0),
                    "regime": row.get("regime", "UNKNOWN"),
                    "volume_24h": row.get("volume_24h", 0),
                    "consensus": "x_scanner",
                    "score": abs(row.get("change_5m", 0)) + abs(row.get("change_1d", 0)),
                })

        return sorted(candidates, key=lambda c: c["score"], reverse=True)

    def suggest_watchlist_additions(self, x_signals: list, scanner_results: list = None, limit: int = 3) -> list:
        validated = self.cross_validate(x_signals, scanner_results=scanner_results)
        return [c["symbol"] for c in validated[:limit]]