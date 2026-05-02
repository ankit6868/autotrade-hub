from freqtrade.strategy import IStrategy
from pandas import DataFrame


class DcaAccumulationStrategy(IStrategy):
    """
    DCA Accumulation Strategy

    Buy: Every 4 hours regardless of price (time-based DCA)
    Sell: Total position profit > 10%
    No stop-loss | Timeframe: 4h
    """

    INTERFACE_VERSION = 3

    timeframe = "4h"
    stoploss = -0.99  # Effectively no stop-loss
    minimal_roi = {"0": 0.10}  # Sell at 10% profit

    # Allow multiple buys (DCA)
    position_adjustment_enable = True
    max_entry_position_adjustment = 10

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Simple moving average for reference
        dataframe["sma_50"] = dataframe["close"].rolling(window=50).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Buy on every candle (DCA - buy at regular intervals)
        dataframe.loc[dataframe["volume"] > 0, "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Exit is handled by minimal_roi (10% profit target)
        # No manual exit signal
        return dataframe

    def adjust_trade_position(self, trade, current_time, current_rate, current_profit, min_stake, max_stake, **kwargs):
        """DCA: Add to position on each interval."""
        if current_profit > -0.05:
            return None  # Only DCA if position is down
        return min_stake  # Add minimum stake amount
