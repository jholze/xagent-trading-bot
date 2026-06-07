from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.columns import Columns
from rich.text import Text
from rich import print as rprint
from datetime import datetime

console = Console()

def print_dashboard(data):
    """Simple split view: Left = Main output, Right = Sidebar with portfolio info."""
    # Left - Main Output
    main_text = Text()
    main_text.append("Latest Signals & Analysis\n\n", style="bold cyan")
    for line in data.get("signals", ["No signals yet..."]):
        main_text.append(str(line) + "\n", style="white")

    left = Panel(
        main_text,
        title="📡 Main Activity",
        border_style="green",
        padding=(1, 2)
    )

    # Right - Sidebar with rich portfolio
    sidebar_table = Table(box=None, expand=True, show_header=False, padding=(0, 1))
    sidebar_table.add_column("Metric", style="dim", width=16)
    sidebar_table.add_column("Value", style="bold")

    p = data
    sidebar_table.add_row("Mode", str(p.get("trading_mode", "PAPER")))
    sidebar_table.add_row("Balance", str(p.get("balance", "$0")))
    sidebar_table.add_row("Unrealized", str(p.get("unrealized", "$0")))
    sidebar_table.add_row("Realized PnL", str(p.get("realized_pnl", "$0")))
    sidebar_table.add_row("Total Value", str(p.get("total_value", "$0")))
    sidebar_table.add_row("Active Pos.", str(p.get("active_positions", 0)))
    sidebar_table.add_row("Win Rate", str(p.get("win_rate", "—")))
    watch = ", ".join([str(c) for c in p.get("coins", [])[:5]])
    sidebar_table.add_row("Watchlist", watch or "—")
    sidebar_table.add_row("X Accounts", ", ".join([str(a) for a in p.get("x_accounts", [])[:3]]))
    board = p.get("trust_leaderboard") or []
    if board:
        top = board[0]
        sidebar_table.add_row("Top Trust", f"@{top.get('handle', '?')} ({top.get('trust_score', 0):.0f})")
    sidebar_table.add_row("Last Cycle", str(p.get("last_cycle", "—")))
    sidebar_table.add_row("Status", str(p.get("status", "🟢 Running")))

    right = Panel(
        sidebar_table,
        title="📊 Portfolio & Info",
        border_style="blue",
        padding=(1, 2)
    )

    # Split Layout
    layout = Columns([left, right], expand=True)

    console.clear()
    rprint(layout)
    rprint(f"\n[bold yellow]Next update in {data.get('next_update', 60)}s...[/bold yellow]  (Press Ctrl+C to stop)")

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
            "🟢 @CryptoCapo_ BUY ARIA | 84%",
            "🔴 @Pentosh1 SELL RAVE | 76%",
            "→ Technical: ARIA | Price: $0.0474",
            "→ Technical: RAVE | Price: $0.5737"
        ],
        "last_cycle": "10:25:12",
        "status": "🟢 Running",
        "next_update": 42
    }
    print_dashboard(test_data)
    print("\nUI test completed successfully.")
