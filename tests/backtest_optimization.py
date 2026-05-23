import itertools
from datetime import datetime, timedelta

import ccxt
import pandas as pd
import talib

# ================== EINSTELLUNGEN ==================
symbol = "ARIA/USDT"
days_back = 10
timeframes = ["4h", "1h"]  # beide Zeitrahmen testen

# Parameter, die wir automatisch testen
rsi_low_options = [25, 28, 30, 32]
rsi_high_options = [42, 45, 48, 50, 52]
volume_mult_options = [1.2, 1.3, 1.5, 1.8]

print(f"🔍 Optimierungs-Backtest für {symbol} – letzte {days_back} Tage\n")
print("Kombinationen werden getestet... (kann 20–40 Sekunden dauern)\n")

results = []

for timeframe in timeframes:
    # Daten einmal pro Timeframe holen
    exchange = ccxt.gate({"enableRateLimit": True})
    since = int((datetime.now() - timedelta(days=days_back)).timestamp() * 1000)
    bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=500)
    df = pd.DataFrame(
        bars, columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.set_index("timestamp")

    df["rsi"] = talib.RSI(df["close"], timeperiod=14)
    df["upper"], df["middle"], df["lower"] = talib.BBANDS(df["close"], timeperiod=20)
    df["vol_avg"] = df["volume"].rolling(window=20).mean()

    print(f"→ {timeframe} Kerzen geladen: {len(df)}")

    # Alle Kombinationen testen
    for low, high, vol_mult in itertools.product(
        rsi_low_options, rsi_high_options, volume_mult_options
    ):
        signals = 0
        for i in range(20, len(df)):
            row = df.iloc[i]
            if (
                row["close"] <= row["lower"]
                and low <= row["rsi"] <= high
                and row["volume"] > vol_mult * row["vol_avg"]
            ):
                signals += 1

        results.append(
            {
                "timeframe": timeframe,
                "rsi_low": low,
                "rsi_high": high,
                "vol_mult": vol_mult,
                "signals": signals,
            }
        )

# Ergebnisse sortieren und anzeigen
df_results = pd.DataFrame(results)
df_results = df_results.sort_values(
    by=["timeframe", "signals"], ascending=[True, False]
)

print("\n" + "=" * 80)
print(
    "📊 ERGEBNISSE – Wie viele Signale hätten die verschiedenen Einstellungen erzeugt?"
)
print("=" * 80)
print(df_results.to_string(index=False))

# Top-Empfehlungen
print("\n" + "=" * 80)
print("💡 MEINE EMPFEHLUNGEN für dich (letzte 10 Tage):")
print("=" * 80)

top = df_results[df_results["signals"] > 0].head(8)
for _, row in top.iterrows():
    print(
        f"• {row['timeframe']:>3} | RSI {row['rsi_low']}-{row['rsi_high']} | Volumen ≥ {row['vol_mult']}× → "
        f"{row['signals']} Signale"
    )

print(
    "\nTipp: Mit RSI 30–48 + Volumen 1.3× hättest du wahrscheinlich 3–6 realistische Signale gehabt."
)
print("     Das wäre ein guter Kompromiss zwischen Strenge und Aktivität.")
