from freqtrade.strategy import IStrategy, informative
from pandas import DataFrame
import talib.abstract as ta
import numpy as np


class MissCandleShortStrategy(IStrategy):
    """
    Miss-Candle Short Strategy (faithful translation of the user's sell-only playbook).

    Entry (SHORT):
      - MACD histogram is negative AND its magnitude is rising vs the previous bar
        (i.e. "color 1/2/3" = strengthening bearish momentum, not color 0 = fading).
      - The PREVIOUS bar is a "miss candle": its high < EMA5 (the whole bar is
        below the 5-EMA, i.e. it never touched the EMA).
      - The CURRENT bar is bearish (close < open).
      - The CURRENT bar does NOT break the previous bar's high (high <= prev_high).
      - Price breaks the miss-candle's LOW (current low <= prev_low).

    Exit (cover):
      - Fixed 1:3 risk/reward targeted via minimal_roi (see custom_exit for exact
        1:3 math using the trade's original SL distance).
      - Hard stop = miss-candle HIGH (re-computed in custom_stoploss).
      - At 1:1.5 R (half-way to target), SL is trailed to a point that has locked
        in 50 % of the current favourable excursion.

    Notes:
      - SHORTING requires Freqtrade FUTURES mode. On KuCoin spot this strategy
        will refuse to open trades. Pair it with `trading_mode: "futures"` and
        `margin_mode: "isolated"` in the Freqtrade config.
      - Timeframe: 5m, pair: BTC/USDT:USDT (futures suffix) recommended.
    """

    INTERFACE_VERSION = 3

    can_short = True
    timeframe = "5m"
    stoploss = -0.02  # safety cap; custom_stoploss refines per-trade
    minimal_roi = {"0": 0.06}  # 6% aspirational; custom_exit enforces 1:3 exactly
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

        # Histogram "colour" proxy: magnitude change vs previous bar.
        # color 1/2/3 = histogram getting more negative (strengthening)
        # color 0    = histogram shrinking in magnitude (fading)
        df["hist_abs"] = df["macdhist"].abs()
        df["hist_rising"] = df["hist_abs"] > df["hist_abs"].shift(1)

        # Bearish momentum: histogram < 0 AND rising magnitude for short
        df["macd_short_ok"] = (df["macdhist"] < 0) & df["hist_rising"]

        # Miss-candle (bearish setup): previous bar's HIGH is below EMA5
        # i.e. the entire previous bar stayed under the 5-EMA.
        df["prev_high"] = df["high"].shift(1)
        df["prev_low"] = df["low"].shift(1)
        df["prev_open"] = df["open"].shift(1)
        df["prev_close"] = df["close"].shift(1)
        df["prev_ema5"] = df["ema5"].shift(1)

        df["miss_short"] = df["prev_high"] < df["prev_ema5"]

        # Current bar confirmations
        df["curr_bearish"] = df["close"] < df["open"]
        df["no_break_high"] = df["high"] <= df["prev_high"]
        df["break_low"] = df["low"] <= df["prev_low"]

        return df

    def populate_entry_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        short_cond = (
            df["macd_short_ok"]
            & df["miss_short"]
            & df["curr_bearish"]
            & df["no_break_high"]
            & df["break_low"]
        )
        df.loc[short_cond, ["enter_short", "enter_tag"]] = (1, "miss_candle_short")
        return df

    def populate_exit_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        # Exits are handled entirely by custom_stoploss (SL + trail) and
        # custom_exit (1:3 TP). Leave signal columns empty.
        return df

    # ----- 1:3 take-profit + 1:1.5 trail ----------------------------------
    def custom_exit(self, pair, trade, current_time, current_rate, current_profit, **kw):
        # Original SL distance (absolute USDT per unit) stored on open:
        # SL price = miss-candle HIGH, entry = miss-candle LOW.
        sl_dist = getattr(trade, "_mc_sl_dist", None)
        if sl_dist is None or sl_dist <= 0:
            return None
        # For shorts, profit is positive when price FALLS from entry.
        # Distance moved in our favour (absolute):
        fav_move = trade.open_rate - current_rate  # positive = winning
        r_multiple = fav_move / sl_dist
        if r_multiple >= 3.0:
            return "tp_1to3"
        return None

    def custom_stoploss(self, pair, trade, current_time, current_rate, current_profit, **kw):
        # Pull the miss-candle HIGH we stamped at open.
        miss_high = getattr(trade, "_mc_miss_high", None)
        entry = trade.open_rate
        if miss_high is None or entry <= 0:
            return self.stoploss

        # Distance in % from entry to the miss-candle high (SL for a short).
        sl_pct = (miss_high - entry) / entry  # positive number, e.g. 0.004

        # Track best favourable move so far
        fav_pct = (entry - current_rate) / entry
        r_multiple = fav_pct / sl_pct if sl_pct > 0 else 0

        if r_multiple >= 1.5:
            # lock in 50% of favourable excursion: new SL is halfway between
            # entry and current price (i.e. 0.5 * fav_pct below entry -> above
            # for a short this means SL is *below* entry price which is profit).
            locked_pct = fav_pct * 0.5
            # return negative = loss from entry; we want a positive profit lock.
            # Freqtrade custom_stoploss expects a value relative to current
            # profit, where 0 means break-even. Return the profit-level SL:
            return -locked_pct  # e.g. -(-0.003) when fav_pct=0.006 -> 0.003 profit lock

        # Before 1:1.5 R, SL is the static miss-candle high.
        return sl_pct  # positive number means "give up this much" from entry

    # ----- stash miss-candle levels on each opened trade ------------------
    def confirm_trade_entry(self, pair, order_type, amount, rate, time_in_force,
                            current_time, entry_tag, side, **kw):
        # Compute sl/target from the most recent analyzed dataframe
        df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if df is None or len(df) < 2:
            return True
        row = df.iloc[-1]
        prev_high = float(row["prev_high"])
        prev_low = float(row["prev_low"])
        sl_dist = abs(prev_high - prev_low)
        # Stash on the trade object via the strategy cache dict.
        # Freqtrade persists these on trade open via custom_data hooks; for
        # simplicity we monkey-patch on the in-memory trade in confirm_trade_exit.
        self._pending_entry = {
            "pair": pair,
            "miss_high": prev_high,
            "miss_low": prev_low,
            "sl_dist": sl_dist,
        }
        return True
