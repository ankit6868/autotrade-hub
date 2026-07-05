from freqtrade.strategy import IStrategy
from pandas import DataFrame
import numpy as np
import talib.abstract as ta


class Ema5GapSweep(IStrategy):
    """
    EMA5 Gap + Sweep — translated from the TradingView Pine "EMA5 Gap+Sweep
    Strategy (Visual)". Every Pine chart-drawing / stats / array construct is
    dropped; only the trading logic remains.

    Idea:
      * ema5  = 5-EMA of close.
      * ltEma = 50-EMA trend filter. (Pine used a 60-min HTF 50-EMA; here it is
        approximated on the execution timeframe. Run this on a higher TF, or
        raise lt_ema_len, to mirror the original HTF filter more closely.)
      * GAP   - the PREVIOUS candle sits entirely on one side of the 5-EMA:
                  gapUp   = close[1] > ema5[1] and low[1]  > ema5[1]
                  gapDown = close[1] < ema5[1] and high[1] < ema5[1]
      * SWEEP - the CURRENT bar takes out the prior bar's extreme:
                  long  = gapDown and high >= high[1]   (sweeps prior high)
                  short = gapUp   and low  <= low[1]     (sweeps prior low)
      * TREND FILTER - long only if high[1] >= ltEma; short only if low[1] <= ltEma.
      * ENTRY  = prior-bar extreme (high[1] long / low[1] short) - a breakout fill.
      * STOP   = structural: min(low[2],low[1],low) long / max(high[2],high[1],high) short.
      * TARGET = entry +/- risk * 6  (Pine rrTarget = 6.0).

    Exits are handled by the structural sl_price / tp_price levels the engine
    reads per signal. The Pine "move to break-even at 1R" is not embedded here -
    enable ARM (Advanced Risk Management -> break-even trail) on the bot/backtest
    to reproduce it. The Pine IST session filter (09:15-15:30) is an Indian-equity
    window and is omitted for 24/7 crypto; add a session window on the bot to
    restrict hours.
    """

    can_short = True
    timeframe = "5m"
    stoploss = -0.05                 # fallback only; real SL is structural (sl_price)
    minimal_roi = {"0": 0.30}        # fallback only; real TP is structural (tp_price)
    process_only_new_candles = True
    startup_candle_count = 60

    ema5_len = 5
    lt_ema_len = 50
    rr_target = 6.0
    sl_buffer = 0.0001               # 0.01%

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema5"] = ta.EMA(dataframe, timeperiod=self.ema5_len)
        dataframe["ltema"] = ta.EMA(dataframe, timeperiod=self.lt_ema_len)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe
        df["enter_long"] = 0
        df["enter_short"] = 0
        df["entry_price"] = np.nan
        df["sl_price"] = np.nan
        df["tp_price"] = np.nan

        c1 = df["close"].shift(1)
        h1 = df["high"].shift(1)
        l1 = df["low"].shift(1)
        h2 = df["high"].shift(2)
        l2 = df["low"].shift(2)
        e1 = df["ema5"].shift(1)
        lt = df["ltema"]
        high = df["high"]
        low = df["low"]

        gap_up = (c1 > e1) & (l1 > e1)        # prev candle fully above ema5
        gap_down = (c1 < e1) & (h1 < e1)      # prev candle fully below ema5

        sweep_bull = gap_down & (high >= h1)  # long: sweeps prior high
        sweep_bear = gap_up & (low <= l1)     # short: sweeps prior low

        # Structural entry / stop / target (absolute prices).
        long_entry = h1
        long_sl = np.minimum(np.minimum(l2, l1), low) * (1.0 - self.sl_buffer)
        long_risk = long_entry - long_sl
        long_tp = long_entry + long_risk * self.rr_target

        short_entry = l1
        short_sl = np.maximum(np.maximum(h2, h1), high) * (1.0 + self.sl_buffer)
        short_risk = short_sl - short_entry
        short_tp = short_entry - short_risk * self.rr_target

        long_cond = sweep_bull & (h1 >= lt) & (long_risk > 0)
        short_cond = sweep_bear & (l1 <= lt) & (short_risk > 0)

        df.loc[long_cond, "enter_long"] = 1
        df.loc[long_cond, "entry_price"] = long_entry[long_cond]
        df.loc[long_cond, "sl_price"] = long_sl[long_cond]
        df.loc[long_cond, "tp_price"] = long_tp[long_cond]

        df.loc[short_cond, "enter_short"] = 1
        df.loc[short_cond, "entry_price"] = short_entry[short_cond]
        df.loc[short_cond, "sl_price"] = short_sl[short_cond]
        df.loc[short_cond, "tp_price"] = short_tp[short_cond]

        return df

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Exits are fully handled by the structural sl_price / tp_price levels.
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0
        return dataframe
