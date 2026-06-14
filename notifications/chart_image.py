"""Mini price charts for Telegram sendPhoto."""

from __future__ import annotations

import tempfile
from pathlib import Path

from logger import log
from notifications.coin_links import coin_links_config, normalize_ticker


def render_ohlcv_chart_png(
    symbol: str,
    timeframe: str = "4h",
    bars: int = 48,
    current_price: float = None,
) -> str | None:
    """Return path to a temporary PNG file, or None on failure."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        log("matplotlib not installed — chart image skipped", "WARNING")
        return None

    from services.market_service import MarketService

    tf = timeframe or coin_links_config().get("chart_timeframe", "4h")
    limit = int(bars or coin_links_config().get("chart_bars", 48))
    market = MarketService()
    df = market._fetch_ohlcv(symbol, tf, limit)
    if df is None or df.empty or len(df) < 5:
        return None

    ticker = normalize_ticker(symbol)
    fig, ax = plt.subplots(figsize=(8, 3), dpi=100)
    try:
        xs = range(len(df))
        ax.plot(xs, df["close"], color="#2ecc71", linewidth=1.5, label="Close")
        ax.fill_between(xs, df["low"], df["high"], alpha=0.15, color="#3498db")

        if current_price and current_price > 0:
            ax.axhline(current_price, color="#e74c3c", linestyle="--", linewidth=1, alpha=0.8)

        ax.set_title(f"{ticker}/USDT · {tf}", fontsize=11)
        ax.set_ylabel("USDT", fontsize=9)
        ax.grid(True, alpha=0.25)
        ax.tick_params(labelsize=8)
        fig.tight_layout()

        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        path = tmp.name
        tmp.close()
        fig.savefig(path, format="png", facecolor="white")
        return path
    except Exception as e:
        log(f"Chart render failed for {symbol}: {e}", "WARNING")
        return None
    finally:
        plt.close(fig)


def send_trade_chart_if_enabled(
    symbol: str,
    *,
    executed: bool,
    current_price: float = None,
    caption: str = "",
    reply_markup: dict = None,
) -> bool:
    """Send chart photo for executed trades when configured."""
    cfg = coin_links_config()
    if not executed or not cfg.get("chart_image_on_executed_trades", True):
        return False

    path = render_ohlcv_chart_png(
        symbol,
        timeframe=cfg.get("chart_timeframe", "4h"),
        bars=int(cfg.get("chart_bars", 48)),
        current_price=current_price,
    )
    if not path:
        return False

    try:
        from telegram_notifier import send_telegram_photo

        ticker = normalize_ticker(symbol)
        cap = caption or f"📈 {ticker}/USDT — {cfg.get('chart_timeframe', '4h')} Verlauf"
        ok = send_telegram_photo(cap, path, reply_markup=reply_markup)
        return ok
    finally:
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:
            pass