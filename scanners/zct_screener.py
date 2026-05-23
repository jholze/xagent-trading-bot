import ccxt
import pandas as pd
from datetime import datetime
from typing import List, Dict

from data_manager import load_config


class ZCTAltcoinScanner:
    def __init__(self, min_volume_5m: float = 500_000):
        self.config = load_config()
        self.min_volume_5m = min_volume_5m
        self.exchange = ccxt.binance({
            'enableRateLimit': True,
            'timeout': 10000,
        })

    def get_all_tickers(self) -> pd.DataFrame:
        """Get 24h ticker data for all USDT pairs using CCXT."""
        print("📡 Fetching market data from Binance...")
        tickers = self.exchange.fetch_tickers()
        data = []
        for symbol, ticker in tickers.items():
            if symbol.endswith('/USDT'):
                data.append({
                    'symbol': symbol,
                    'priceChangePercent': ticker.get('percentage', 0),
                    'quoteVolume': ticker.get('quoteVolume', 0),
                    'lastPrice': ticker.get('last', 0)
                })
        df = pd.DataFrame(data)
        df = df[df['quoteVolume'] > 1_000_000]  # Minimum liquidity
        print(f"✅ Loaded {len(df)} USDT trading pairs")
        return df

    def get_5m_momentum(self, symbol: str) -> float:
        """Calculate 5-minute price change using CCXT."""
        try:
            bars = self.exchange.fetch_ohlcv(symbol, timeframe='5m', limit=2)
            if len(bars) < 2:
                return 0.0
            open_price = bars[0][1]
            close_price = bars[-1][4]
            return ((close_price - open_price) / open_price) * 100
        except:
            return 0.0

    def apply_screener(self, df: pd.DataFrame) -> pd.DataFrame:
        """Apply ZCT Screener Filters."""
        print("🔎 Applying ZCT Screener...")

        # Activity filter
        df = df[df['quoteVolume'] > df['quoteVolume'].quantile(0.7)]

        # Daily heat + momentum
        df = df[(df['priceChangePercent'].abs() >= 8.0) | (df['priceChangePercent'] >= 5.0)]

        # Liquidity filter
        df = df[df['quoteVolume'] > self.min_volume_5m * 12]

        return df

    def run_full_scan(self, target_coins: int = 6) -> List[Dict]:
        """Main ZCT Discovery Pipeline - returns structured data for the bot."""
        print(f"\n🚀 ZCT ALTCOIN DISCOVERY SCAN - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        
        df = self.get_all_tickers()
        filtered = self.apply_screener(df)

        if filtered.empty:
            print("❌ No coins passed initial filters")
            return []

        # Calculate 5m momentum for top candidates
        print("📊 Calculating short-term momentum...")
        candidates = filtered.head(30).copy()
        
        results = []
        for _, row in candidates.iterrows():
            symbol = row['symbol']
            change_5m = self.get_5m_momentum(symbol)
            
            coin_data = {
                'symbol': symbol,
                'change_5m': round(change_5m, 2),
                'change_1d': round(float(row['priceChangePercent']), 2),
                'volume_24h': round(float(row['quoteVolume']), 0),
                'price': float(row['lastPrice']),
                'regime': "BREAKOUT" if 1 <= change_5m <= 5 and row['priceChangePercent'] > 0 else "REVERSAL"
            }
            results.append(coin_data)

        result_df = pd.DataFrame(results)
        result_df['extreme_score'] = result_df['change_5m'].abs() + result_df['change_1d'].abs()
        result_df = result_df.sort_values('extreme_score', ascending=False)
        
        final_coins = result_df.head(target_coins).to_dict('records')

        print("🏆 TOP ALTCOINS RECOMMENDED:")
        for coin in final_coins:
            print(f"• {coin['symbol']:12} | 5m: {coin['change_5m']:+6.2f}% | 1D: {coin['change_1d']:+6.2f}% | "
                  f"Vol24h: ${coin['volume_24h']:,.0f} | → {coin['regime']}")

        return final_coins


if __name__ == "__main__":
    scanner = ZCTAltcoinScanner()
    scanner.run_full_scan(target_coins=6)
