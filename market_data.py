import ccxt
import pandas as pd
import talib
from datetime import datetime
import time

class MarketData:
    def __init__(self):
        self.exchange = ccxt.gate({'enableRateLimit': True})
        self.cache = {}  # key: (symbol, timeframe) → (df, timestamp)

    def get_ohlcv(self, symbol, timeframe, limit=120):
        key = (symbol, timeframe)
        now = time.time()

        # Cache für 25 Sekunden (da 1h/4h Kerzen sich nicht so schnell ändern)
        if key in self.cache and now - self.cache[key][1] < 25:
            return self.cache[key][0]

        try:
            bars = self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            df = pd.DataFrame(bars, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
            df['ts'] = pd.to_datetime(df['ts'], unit='ms')

            # Indikatoren berechnen
            df['rsi'] = talib.RSI(df['close'], timeperiod=14)
            _, df['middle'], df['lower'] = talib.BBANDS(df['close'], timeperiod=20)
            df['vol_avg'] = df['volume'].rolling(window=20).mean()

            self.cache[key] = (df, now)
            return df
        except Exception as e:
            print(f"MarketData Fehler ({timeframe}): {e}")
            return None

# Globale Instanz
market_data = MarketData()