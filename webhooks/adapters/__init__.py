from webhooks.adapters.cmc_alert import parse_cmc_alert
from webhooks.adapters.generic import parse_generic
from webhooks.adapters.tradingview import parse_tradingview

ADAPTERS = {
    "generic": parse_generic,
    "tradingview": parse_tradingview,
    "tv": parse_tradingview,
    "cmc": parse_cmc_alert,
    "coinmarketcap": parse_cmc_alert,
}


def parse_signal_payload(body: dict | str | None, source: str = "generic"):
    adapter = ADAPTERS.get(str(source or "generic").lower(), parse_generic)
    return adapter(body, source=source)