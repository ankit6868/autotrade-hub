from freqtrade.strategy import IStrategy
from pandas import DataFrame
import talib.abstract as ta


class EmaScalpingStrategy(IStrategy):
    """
    EMA Scalping Strategy

    Buy: EMA(9) crosses above EMA(21) AND volume > 1.5x 20-period average
    Sell: EMA(9) crosses below EMA(21)
    Stop-loss: 1.5% | Timeframe: 5m
    """

    INTERFACE_VERSION = 3

    timeframe = "5m"
    stoploss = -0.015
    minimal_roi = {"0": 0.02, "15": 0.01, "30": 0.005, "60": 0}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod=9)
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=21)

        # Previous EMAs for crossover detection
        dataframe["ema_fast_prev"] = dataframe["ema_fast"].shift(1)
        dataframe["ema_slow_prev"] = dataframe["ema_slow"].shift(1)

        # Volume analysis
        dataframe["volume_mean"] = dataframe["volume"].rolling(window=20).mean()

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["ema_fast_prev"] < dataframe["ema_slow_prev"])
            & (dataframe["ema_fast"] > dataframe["ema_slow"])
            & (dataframe["volume"] > dataframe["volume_mean"] * 1.5),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["ema_fast_prev"] > dataframe["ema_slow_prev"])
            & (dataframe["ema_fast"] < dataframe["ema_slow"]),
            "exit_long",
        ] = 1
        return dataframe
