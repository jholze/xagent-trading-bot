"""Local measurement contract for draft D-EXH Rev 1.

Thresholds, lookback, bearish volume divergence, and freshness grace are
pending owner freeze. This module records that fact. It does not choose them.

The 4h value below is the bar length used to tell a closed bar from an open
one. It is not a freshness grace.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

SCHEMA_ID = "d-exh-measurement-input"
SCHEMA_REVISION = "r1-raw-2026-11-10"
TRIAL_ID = "D-EXH-R1-RAW"

# Same period as MarketService._bars_to_dataframe (talib.RSI timeperiod=14).
# Not an exhaustion cutoff. No minimum is defined.
RSI_PERIOD = 14
RSI_PERIOD_SOURCE = (
    "services.market_service.MarketService._bars_to_dataframe "
    "talib.RSI timeperiod=14"
)

BAR_HOURS = 4
HOLDOUT_START = datetime(2026, 11, 6, 0, 0, tzinfo=ZoneInfo("Europe/Berlin"))

OPEN_DEFINITIONS = {
    "thresholds": "UNCLEAR",
    "lookback": "UNCLEAR",
    "bearish_volume_divergence": "UNCLEAR",
    "freshness_grace": "undefined",
    "freeze": "pending owner freeze",
}

# Citation only. Not recomputed, and not a result of D-EXH-R1-RAW.
HISTORICAL_CLIMAX_NO_GO = {
    "source": "trading-learning/analysen/2026-11-05_shorts/SUMMARY.md",
    "recipe": "climax_1x",
    "profit_factor": "0.9160",
    "net_usdt": "-3500.42",
    "n": 1016,
    "kind": "spot_proxy",
    "verdict": "NO-GO",
    "recomputed": False,
    "applies_to_new_recipe": False,
}


def dataset_sha256(
    signals: list[Any],
    bars: list[Any],
    funding: list[Any],
    now: str | None,
) -> str:
    """Hash caller inputs before any feature value is computed."""
    payload = {
        "bars": bars,
        "funding": funding,
        "now": now,
        "signals": signals,
    }
    blob = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()
