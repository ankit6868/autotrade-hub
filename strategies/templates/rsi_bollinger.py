from freqtrade.strategy import IStrategy, IntParameter, DecimalParameter
from pandas import DataFrame
import talib.abstract as ta


class RsiBollingerStrategy(IStrategy):
    """
    RSI + Bollinger Band Reversal Strategy

    Buy: RSI(14) < 30 AND price < lower Bollinger Band(20, 2)
    Sell: RSI > 70 OR price > upper Bollinger Band
    Stop-loss: 3% | Timeframe: 15m
    """

    INTERFACE_VERSION = 3

    timeframe = "15m"
    stoploss = -0.03
    minimal_roi = {"0": 0.05, "30": 0.025, "60": 0.01, "120": 0}

    # Indicator parameters
    rsi_period = IntParameter(10, 20, default=14, space="buy")
    rsi_buy_threshold = IntParameter(25, 35, default=30, space="buy")
    rsi_sell_threshold = IntParameter(65, 80, default=70, space="sell")
    bb_period = IntParameter(15, 25, default=20, space="buy")
    bb_std = DecimalParameter(1.5, 3.0, default=2.0, space="buy")

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # RSI
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=self.rsi_period.value)

        # Bollinger Bands
        bollinger = ta.BBANDS(
            dataframe, timeperiod=self.bb_period.value, nbdevup=self.bb_std.value, nbdevdn=self.bb_std.value
        )
        dataframe["bb_upper"] = bollinger["upperband"]
        dataframe["bb_middle"] = bollinger["middleband"]
        dataframe["bb_lower"] = bollinger["lowerband"]

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["rsi"] < self.rsi_buy_threshold.value) & (dataframe["close"] < dataframe["bb_lower"]),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["rsi"] > self.rsi_sell_threshold.value) | (dataframe["close"] > dataframe["bb_upper"]),
            "exit_long",
        ] = 1
        return dataframe
