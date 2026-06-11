"""Normalized trading actions and legacy execution mapping."""

# Normalized decision actions
HOLD = "HOLD"
IGNORE = "IGNORE"
BUY = "BUY"
BUY_STRONG = "BUY_STRONG"
SELL_PARTIAL_20 = "SELL_PARTIAL_20"
SELL_PARTIAL_30 = "SELL_PARTIAL_30"
SELL_PARTIAL_50 = "SELL_PARTIAL_50"
SELL_FULL = "SELL_FULL"
SELL_STOP = "SELL_STOP"
ADD_WATCHLIST = "ADD_WATCHLIST"

LEGACY_TO_NORMALIZED = {
    "HOLD": HOLD,
    "BUY": BUY,
    "SELL": SELL_PARTIAL_20,
    "SELL_20": SELL_PARTIAL_20,
    "SELL_30": SELL_PARTIAL_30,
    "SELL_TP": SELL_PARTIAL_30,
    "SELL_STOP_PARTIAL": SELL_PARTIAL_50,
    "SELL_STOP_FULL": SELL_FULL,
}

NORMALIZED_TO_LEGACY = {
    HOLD: "HOLD",
    IGNORE: "IGNORE",
    BUY: "BUY",
    BUY_STRONG: "BUY",
    SELL_PARTIAL_20: "SELL_20",
    SELL_PARTIAL_30: "SELL_30",
    SELL_PARTIAL_50: "SELL_STOP_PARTIAL",
    SELL_FULL: "SELL_STOP_FULL",
    SELL_STOP: "SELL_STOP_FULL",
    ADD_WATCHLIST: "ADD_WATCHLIST",
}

SELL_ACTIONS = {
    SELL_PARTIAL_20, SELL_PARTIAL_30, SELL_PARTIAL_50, SELL_FULL, SELL_STOP,
    "SELL", "SELL_20", "SELL_30", "SELL_STOP_PARTIAL", "SELL_STOP_FULL",
}


def normalize(action: str) -> str:
    return LEGACY_TO_NORMALIZED.get(action, action)


def to_execution_action(action: str) -> str:
    if action in NORMALIZED_TO_LEGACY:
        return NORMALIZED_TO_LEGACY[action]
    return action


def is_sell(action: str) -> bool:
    return action in SELL_ACTIONS or "SELL" in action


def is_buy(action: str) -> bool:
    return action in (BUY, BUY_STRONG) or action == "BUY"