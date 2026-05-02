from freqtrade.strategy import IStrategy
from pandas import DataFrame
import talib.abstract as ta


class MacdCrossoverStrategy(IStrategy):
    """
    MACD Crossover Strategy

    Buy: MACD crosses above signal AND histogram turning positive
    Sell: MACD crosses below signal
    Stop-loss: 2.5% | Timeframe: 1h
    """

    INTERFACE_VERSION = 3

    timeframe = "1h"
    stoploss = -0.025
    minimal_roi = {"0": 0.04, "60": 0.02, "120": 0.01, "240": 0}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        macd = ta.MACD(dataframe, fastperiod=12, slowperiod=26, signalperiod=9)
        dataframe["macd"] = macd["macd"]
        dataframe["macdsignal"] = macd["macdsignal"]
        dataframe["macdhist"] = macd["macdhist"]

        # Previous values for crossover detection
        dataframe["macd_prev"] = dataframe["macd"].shift(1)
        dataframe["macdsignal_prev"] = dataframe["macdsignal"].shift(1)

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["macd_prev"] < dataframe["macdsignal_prev"])
            & (dataframe["macd"] > dataframe["macdsignal"])
            & (dataframe["macdhist"] > 0),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["macd_prev"] > dataframe["macdsignal_prev"])
            & (dataframe["macd"] < dataframe["macdsignal"]),
            "exit_long",
        ] = 1
        return dataframe
