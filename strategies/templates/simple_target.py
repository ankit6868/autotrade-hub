"""
SimpleTargetStrategy — Buy on RSI dips, take profit at target.

Works in any market condition. No complex indicators needed.

Entry rules (either condition triggers a buy):
  1. RSI < 38  (strongly oversold — bounce expected)
  2. RSI < 55  AND  price ≤ EMA-20 × 1.005  (mild dip near fair value)

Exit rules:
  • Take-profit:  +1.5% above entry  ✅ automatic
  • Stop-loss:    -1.5% below entry  (configurable in Paper Trade settings)

Risk / Reward: 1:1 by default — quick cycles, frequent signals.
Timeframe: 15m  |  Pairs: any USDT pair  |  Wallet: any size
"""


class SimpleTargetStrategy:
    """
    SimpleTargetStrategy
    Buy when RSI is below 55 near EMA-20, or when RSI < 38 (oversold).
    Exit at +1.5% take-profit or -1.5% stop-loss.
    """

    timeframe  = "15m"
    stoploss   = -0.015          # -1.5%
    minimal_roi = {"0": 0.015}   # +1.5% take-profit

    # ── Entry signal ─────────────────────────────────────────────────────
    def populate_entry_trend(self, dataframe, metadata):
        oversold = dataframe["rsi"] < 38
        mild_dip = (dataframe["rsi"] < 55) & (dataframe["close"] <= dataframe["ema_20"] * 1.005)

        dataframe.loc[oversold | mild_dip, "enter_long"] = 1
        return dataframe

    # ── Exit signal (take-profit handled by minimal_roi) ─────────────────
    def populate_exit_trend(self, dataframe, metadata):
        # No manual exit signal — let the engine handle TP (+1.5%) and SL (-1.5%)
        overbought = dataframe["rsi"] > 75
        dataframe.loc[overbought, "exit_long"] = 1
        return dataframe

    # ── Indicators ────────────────────────────────────────────────────────
    def populate_indicators(self, dataframe, metadata):
        # RSI
        delta = dataframe["close"].diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rs    = gain / loss.replace(0, 1e-10)
        dataframe["rsi"] = 100 - (100 / (1 + rs))

        # EMA-20
        dataframe["ema_20"] = dataframe["close"].ewm(span=20, adjust=False).mean()

        return dataframe
