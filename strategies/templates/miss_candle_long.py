from freqtrade.strategy import IStrategy
from pandas import DataFrame
import talib.abstract as ta


class MissCandleLongStrategy(IStrategy):
    """
    Miss-Candle Long Strategy - the LONG mirror of the user's sell-only playbook.

    This exists so the rules can be backtested on KuCoin SPOT, where shorting
    is not possible. Logic is identical but flipped:

    Entry (LONG):
      - MACD histogram > 0 AND magnitude rising (bullish momentum, "color 1/2/3").
      - PREVIOUS bar is a bullish miss candle: its LOW > EMA5 (whole bar above EMA).
      - CURRENT bar is bullish (close > open) and does NOT break prev LOW.
      - Price breaks the miss-candle's HIGH.

    Exit:
      - Fixed 1:3 R:R (handled via minimal_roi proxied by custom_exit).
      - Static SL at the miss-candle LOW.
      - At 1:1.5 R, SL trails to lock 50 % of the favourable excursion.
    """

    INTERFACE_VERSION = 3

    can_short = False
    timeframe = "5m"
    stoploss = -0.02
    minimal_roi = {"0": 0.06}
    use_custom_stoploss = True
    process_only_new_candles = True
    startup_candle_count = 50

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe
        df["ema5"] = ta.EMA(df, timeperiod=5)

        macd = ta.MACD(df, fastperiod=12, slowperiod=26, signalperiod=9)
        df["macd"] = macd["macd"]
        df["macdsignal"] = macd["macdsignal"]
        df["macdhist"] = macd["macdhist"]

        df["hist_abs"] = df["macdhist"].abs()
        df["hist_rising"] = df["hist_abs"] > df["hist_abs"].shift(1)
        df["macd_long_ok"] = (df["macdhist"] > 0) & df["hist_rising"]

        df["prev_high"] = df["high"].shift(1)
        df["prev_low"] = df["low"].shift(1)
        df["prev_open"] = df["open"].shift(1)
        df["prev_close"] = df["close"].shift(1)
        df["prev_ema5"] = df["ema5"].shift(1)

        # Bullish miss-candle: previous bar's LOW is above the 5-EMA
        df["miss_long"] = df["prev_low"] > df["prev_ema5"]

        df["curr_bullish"] = df["close"] > df["open"]
        df["no_break_low"] = df["low"] >= df["prev_low"]
        df["break_high"] = df["high"] >= df["prev_high"]

        return df

    def populate_entry_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        long_cond = (
            df["macd_long_ok"]
            & df["miss_long"]
            & df["curr_bullish"]
            & df["no_break_low"]
            & df["break_high"]
        )
        df.loc[long_cond, ["enter_long", "enter_tag"]] = (1, "miss_candle_long")
        return df

    def populate_exit_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        return df

    def custom_exit(self, pair, trade, current_time, current_rate, current_profit, **kw):
        # 1:3 R:R on long side.
        # SL distance % = (entry - miss_low) / entry approximated by entry * |stoploss|
        # at open-time; use current_profit vs |stoploss| as R-multiple proxy.
        sl_pct = abs(self.stoploss)
        if sl_pct <= 0:
            return None
        r_multiple = current_profit / sl_pct
        if r_multiple >= 3.0:
            return "tp_1to3"
        return None

    def custom_stoploss(self, pair, trade, current_time, current_rate, current_profit, **kw):
        sl_pct = abs(self.stoploss)
        if sl_pct <= 0 or current_profit <= 0:
            return self.stoploss
        r_multiple = current_profit / sl_pct
        if r_multiple >= 1.5:
            # Lock 50% of favourable excursion
            return -(current_profit * 0.5)
        return self.stoploss
