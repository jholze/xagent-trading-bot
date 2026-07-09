#!/usr/bin/env python3
"""Print the Telegram /mode message (for deploy verification)."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.runtime_identity import format_identity_section
from data_manager import is_demo_mode
from services.trading_service import TradingService


def main() -> int:
    service = TradingService()
    demo = " | Demo: ON" if is_demo_mode() else ""
    msg = f"""<b>Trading Mode</b>

Current: <b>{service.mode_label()}</b>{demo}

{format_identity_section()}

<b>Commands:</b>
/mode paper — Local paper trading (virtual ledger)
/mode live — Live Gate.io mainnet (requires /live_confirm)
/mode off — Analysis only, no execution
/live_confirm — Confirm live trading
/live_cancel — Revoke live confirmation
/gate — Gate.io API status + Balance
/maxpositions — Max. offene Positionen anzeigen/setzen
"""
    print(msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())