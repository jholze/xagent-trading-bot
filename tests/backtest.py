from datetime import datetime, timedelta

import ccxt
import pandas as pd
import talib

# === EINSTELLUNGEN (genau wie in deinem Bot) ===
exchange = ccxt.gate(
    {"enableRateLimit": True}
)  # oder ccxt.binance() falls du wechselst
symbol = "ARIA/USDT"
timeframe = "4h"  # oder "1h" zum Testen
days_back = 10

print(f"🔍 Backtest für {symbol} auf {timeframe} – letzte {days_back} Tage")
print("=" * 70)

# Daten holen
since = int((datetime.now() - timedelta(days=days_back)).timestamp() * 1000)
bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=500)

df = pd.DataFrame(bars, columns=["timestamp", "open", "high", "low", "close", "volume"])
df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
df = df.set_index("timestamp")

# Indikatoren berechnen
df["rsi"] = talib.RSI(df["close"], timeperiod=14)
df["upper"], df["middle"], df["lower"] = talib.BBANDS(df["close"], timeperiod=20)
df["vol_avg"] = df["volume"].rolling(window=20).mean()

# Kauf-Signale zählen
buy_signals = 0
signal_dates = []

for i in range(20, len(df)):  # erst ab Index 20, weil Indikatoren "warm" laufen müssen
    row = df.iloc[i]
    prev_row = df.iloc[i - 1]  # wir checken das Signal am Ende der Kerze

    if (
        row["close"] <= row["lower"]
        and 30 <= row["rsi"] <= 45
        and row["volume"] > 1.5 * row["vol_avg"]
    ):
        buy_signals += 1
        signal_dates.append(
            {
                "time": row.name,
                "price": row["close"],
                "rsi": round(row["rsi"], 2),
                "volume_mult": round(row["volume"] / row["vol_avg"], 2),
            }
        )

print(
    f"\n✅ In den letzten {days_back} Tagen gab es **{buy_signals} Kauf-Signale** auf {timeframe}."
)
if buy_signals > 0:
    print("\nDie Signale waren an folgenden Kerzen:")
    for s in signal_dates:
        print(
            f"   • {s['time'].strftime('%d.%m. %H:%M')} | Preis {s['price']:.4f} | RSI {s['rsi']} | Volumen {s['volume_mult']}×"
        )
else:
    print("   → Kein einziges Mal haben alle drei Bedingungen gleichzeitig gepasst.")

print("\nFazit: Die Regeln sind aktuell sehr streng.")


print("\n--- Virtual Trading Unit Test ---")
try:
    from data_manager import load_config, load_trade_history, record_trade
    from strategies.positions import get_position, update_position
    from strategies.core_strategy import check_signal
    config = load_config()
    print("✅ Config loaded")
    history = load_trade_history()
    print(f"Virtual balance: ${history.get('virtual_balance', 0):.0f} | Realized PnL: ${history.get('realized_pnl', 0):.1f}")
    pos = get_position("TEST/USDT", "4h")
    print(f"Test position: amount={float(pos['amount']):.2f}, entry={pos.get('entry_price', 0)}")
    print("✅ Virtual trading components load and run successfully")
except Exception as e:
    print(f"❌ Test failed: {e}")
