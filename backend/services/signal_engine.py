from tradingview_ta import TA_Handler, Interval

INTERVAL_MAP = {
    "1m": Interval.INTERVAL_1_MINUTE,
    "5m": Interval.INTERVAL_5_MINUTES,
    "15m": Interval.INTERVAL_15_MINUTES,
    "30m": Interval.INTERVAL_30_MINUTES,
    "1h": Interval.INTERVAL_1_HOUR,
    "2h": Interval.INTERVAL_2_HOURS,
    "4h": Interval.INTERVAL_4_HOURS,
    "1d": Interval.INTERVAL_1_DAY,
    "1w": Interval.INTERVAL_1_WEEK,
    "1M": Interval.INTERVAL_1_MONTH,
}


def get_signals(symbol: str, exchange: str = "KUCOIN", interval: str = "15m") -> dict:
    """Get TradingView technical analysis signals for a symbol."""
    # TradingView expects symbol without separator, e.g. BTCUSDT
    # Accept inputs like BTC/USDT, BTC-USDT, or BTCUSDT
    tv_symbol = symbol.replace("/", "").replace("-", "").upper()
    # Normalize the returned display symbol to slash-form so UI is consistent
    display_symbol = symbol.replace("-", "/").upper() if "-" in symbol or "/" in symbol else symbol.upper()
    screener = "crypto"
    tv_interval = INTERVAL_MAP.get(interval, Interval.INTERVAL_15_MINUTES)

    try:
        handler = TA_Handler(
            symbol=tv_symbol,
            screener=screener,
            exchange=exchange,
            interval=tv_interval,
        )
        analysis = handler.get_analysis()

        return {
            "symbol": display_symbol,
            "interval": interval,
            "summary": {
                "recommendation": analysis.summary["RECOMMENDATION"],
                "buy": analysis.summary["BUY"],
                "sell": analysis.summary["SELL"],
                "neutral": analysis.summary["NEUTRAL"],
            },
            "oscillators": analysis.oscillators,
            "moving_averages": analysis.moving_averages,
            "indicators": {
                "rsi": analysis.indicators.get("RSI"),
                "macd": analysis.indicators.get("MACD.macd"),
                "macd_signal": analysis.indicators.get("MACD.signal"),
                "bb_upper": analysis.indicators.get("BB.upper"),
                "bb_lower": analysis.indicators.get("BB.lower"),
                "ema_20": analysis.indicators.get("EMA20"),
                "sma_50": analysis.indicators.get("SMA50"),
                "adx": analysis.indicators.get("ADX"),
                "atr": analysis.indicators.get("ATR"),
                "volume": analysis.indicators.get("volume"),
            },
        }
    except Exception as e:
        return {"symbol": display_symbol, "interval": interval, "error": str(e)}
