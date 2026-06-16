# Strategy Test Scenarios for AutoTrade Hub

Copy each strategy below into the **Upload Strategy** page (Strategy Editor) to test
specific engine capabilities. Run each on Futures Backtest, then test in Futures Paper.

---

## 1. PureLongTrendFollow — Tests LONG-only flow
Tests: long entry, long SL/TP, long P&L calculation
Description: Buys when EMA9 crosses above EMA50 with rising volume. Exit on SL or TP.

```python
class PureLongTrendFollow:
    """
    LONG-only trend follower.
    Entry: EMA9 crosses ABOVE EMA50 (golden cross signal)
           AND volume > 20-bar avg × 1.2
           AND RSI(14) between 40-65 (not overbought, not extreme)
    No SHORT entries — pure long-only test.
    SL: 2.0% | TP: 4.0% (2:1 R:R)
    """
    minimal_roi = {"0": 0.04}
    stoploss = -0.02
    timeframe = "15m"
```

---

## 2. PureShortTrendFollow — Tests SHORT-only flow
Tests: short entry mechanics, short P&L (inverted), short SL/TP placement
Description: Sells when EMA9 crosses below EMA50 in downtrend.

```python
class PureShortTrendFollow:
    """
    SHORT-only trend follower.
    Entry: EMA9 crosses BELOW EMA50 (death cross signal)
           AND volume > 20-bar avg × 1.2
           AND RSI(14) between 35-60 (downside momentum)
    No LONG entries — pure short-only test.
    SL: 2.0% (above entry) | TP: 4.0% (below entry)
    """
    minimal_roi = {"0": 0.04}
    stoploss = -0.02
    timeframe = "15m"
```

---

## 3. ExtremeBreakout — Tests wide SL/TP & long-running positions
Tests: position holding logic, trail-stop, large P&L moves
Description: Enters on Donchian channel breakout with very wide stops.

```python
class ExtremeBreakout:
    """
    Donchian breakout — wide SL/TP for trend captures.
    LONG  : close > 20-bar high (new HH)
    SHORT : close < 20-bar low  (new LL)
    SL: 5.0% | TP: 15.0% (3:1 R:R, captures strong moves)
    """
    minimal_roi = {"0": 0.15}
    stoploss = -0.05
    timeframe = "1h"
```

---

## 4. ScalpingHighFreq — Tests engine throughput & precision
Tests: many trades in short time, tight SL/TP precision
Description: Bollinger Band touch with very tight SL/TP.

```python
class ScalpingHighFreq:
    """
    High-frequency BB scalper — tight SL/TP for many small wins.
    LONG  : close < BB lower (oversold)
    SHORT : close > BB upper (overbought)
    SL: 0.5% | TP: 1.0% (2:1 R:R)
    Generates many trades — tests engine speed and precision.
    """
    minimal_roi = {"0": 0.01}
    stoploss = -0.005
    timeframe = "5m"
```

---

## 5. HighLeverageLiquidationTest — Tests liquidation logic
Tests: liquidation price calculation, instant liq detection
Description: Use this with 50× leverage to verify liquidation triggers correctly.

```python
class HighLeverageLiquidationTest:
    """
    Designed to TEST liquidation engine.
    Run with 50x or 75x leverage — liquidation will fire often.
    Entry: simple RSI cross (RSI<30 long, RSI>70 short).
    SL: 3.0% (but liquidation at ~2% with 50x — liq comes first!)
    """
    minimal_roi = {"0": 0.04}
    stoploss = -0.03
    timeframe = "15m"
```

---

## 6. MeanReversionRSI — Tests bidirectional balance
Tests: equal long/short distribution, mean-reversion logic
Description: Pure RSI extreme reversal.

```python
class MeanReversionRSI:
    """
    Pure RSI mean-reversion — symmetric long/short triggers.
    LONG  : RSI(14) < 25 (extreme oversold)
    SHORT : RSI(14) > 75 (extreme overbought)
    SL: 1.5% | TP: 2.5% (~1.7:1 R:R)
    Should produce roughly equal long & short counts.
    """
    minimal_roi = {"0": 0.025}
    stoploss = -0.015
    timeframe = "15m"
```

---

## 7. MacdHistogramReversal — Tests MACD signal accuracy
Tests: MACD indicator computation, histogram reversal detection
Description: Trade on MACD histogram momentum shifts.

```python
class MacdHistogramReversal:
    """
    MACD histogram reversal scalper.
    LONG  : MACD histogram crosses from negative to positive
    SHORT : MACD histogram crosses from positive to negative
    SL: 1.0% | TP: 2.0% (2:1 R:R)
    """
    minimal_roi = {"0": 0.02}
    stoploss = -0.01
    timeframe = "15m"
```

---

## 8. VolatilitySpike — Tests volatility-based filtering
Tests: ATR computation, volatility-aware entries
Description: Only trade when volatility (ATR) is in a sweet spot.

```python
class VolatilitySpike:
    """
    Volatility-filtered EMA cross.
    LONG  : EMA9 > EMA21 AND ATR(14) > 1.5% of price
    SHORT : EMA9 < EMA21 AND ATR(14) > 1.5% of price
    Skips trades during low-volatility chop.
    SL: 2.0% | TP: 4.0%
    """
    minimal_roi = {"0": 0.04}
    stoploss = -0.02
    timeframe = "15m"
```

---

## How to Test

For each strategy:
1. **Upload** via Strategy Editor → paste code → Save
2. **Backtest** on Futures Backtest page (3M, 6M, 1Y periods)
3. **Verify**:
   - Trade count matches expectation (high-freq → many, wide SL → few)
   - Long/Short ratio is reasonable (depending on strategy direction bias)
   - 0 negative balance (engine never loses more than account)
   - Liquidations happen for high-leverage strategies (#5)
   - Math is correct (manually verify a few trades)
4. **Compare** with TradingView for similar strategies
5. **Paper Trade** for 1 hour to verify live signal generation works
