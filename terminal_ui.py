from datetime import datetime

def print_dashboard(data):
    """Simple text-based split UI with clear sections (no dependencies)."""
    print("\n" + "=" * 80)
    print(" " * 20 + "🧠 X-AGENT TRADING BOT" + " " * 20)
    print("=" * 80)

    # Left - Main Output (simulated as top section)
    print("\n📡 MAIN ACTIVITY")
    print("-" * 80)
    for line in data.get("signals", ["No signals yet..."]):
        print("  " + line)
    print("-" * 80)

    # Right - Sidebar info (as bottom section for simplicity)
    print("\n📊 PORTFOLIO & INFO")
    print("-" * 80)
    print(f"Balance          : {data.get('balance', '$0')}")
    print(f"Unrealized       : {data.get('unrealized', '$0')}")
    print(f"Realized PnL     : {data.get('realized_pnl', '$0')}")
    print(f"Total Value      : {data.get('total_value', '$0')}")
    print(f"Active Positions : {data.get('active_positions', 0)}")
    print(f"Win Rate         : {data.get('win_rate', '—')}")
    print(f"X Accounts       : {', '.join(data.get('x_accounts', []))}")
    print(f"Last Cycle       : {data.get('last_cycle', '—')}")
    print(f"Status           : {data.get('status', '🟢 Running')}")
    print("-" * 80)

    print(f"\nNext update in {data.get('next_update', 60)} seconds... (Press Ctrl+C to stop)\n")

# Test
if __name__ == "__main__":
    test_data = {
        "balance": "$5,234",
        "unrealized": "$187.4",
        "realized_pnl": "$92.1",
        "total_value": "$5,421",
        "active_positions": 3,
        "win_rate": "71%",
        "coins": ["ARIA", "RAVE", "HIGH"],
        "x_accounts": ["CryptoCapo_", "Pentosh1", "SmartContracter"],
        "signals": [
            "🟢 @CryptoCapo_ BUY ARIA | 84% - Strong breakout",
            "🔴 @Pentosh1 SELL RAVE | 76% - Resistance hit",
            "→ Technical: ARIA | Price: $0.0474",
            "→ Technical: RAVE | Price: $0.5737"
        ],
        "last_cycle": "10:25:12",
        "status": "🟢 Running",
        "next_update": 37
    }
    print_dashboard(test_data)
    print("Test completed - this is the new UI.")
