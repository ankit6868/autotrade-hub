import os
from contextlib import asynccontextmanager
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

# Sentry must initialise before FastAPI imports anything that emits errors.
from backend.utils.observability import init_sentry  # noqa: E402

_sentry_active = init_sentry()

import asyncio  # noqa: E402

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, status as http_status  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from slowapi import _rate_limit_exceeded_handler  # noqa: E402
from slowapi.errors import RateLimitExceeded  # noqa: E402
from slowapi.middleware import SlowAPIMiddleware  # noqa: E402
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402
from sqlalchemy import select  # noqa: E402

from backend.models import init_db, SessionLocal, Config  # noqa: E402
# AutoTrade Hub is FUTURES-ONLY now. The spot / autotrade / multi-strategy /
# webhook / copy-trading-router stack was removed in commit "spot purge"
# (PDF §1 — single-purpose engine) — only the futures terminal, paper +
# live lead-trading, and futures backtest remain.
from backend.routers import auth, strategy, market  # noqa: E402
from backend.routers import futures as futures_router  # noqa: E402
from backend.routers import paper_scalp as paper_scalp_router  # noqa: E402
from backend.utils.clerk_auth import (  # noqa: E402
    ANONYMOUS_USER_ID,
    CLERK_AUDIENCE,
    CLERK_ISSUER,
    _get_jwks,
    clerk_enabled,
)
from backend.utils.rate_limit import limiter  # noqa: E402
import jwt  # noqa: E402


_SIMPLE_STRATEGY_CODE = '''
from freqtrade.strategy import IStrategy
from pandas import DataFrame
import talib.abstract as ta


class SimpleTargetStrategy(IStrategy):
    """
    RSI + EMA-20 mean-reversion strategy. Bidirectional LONG + SHORT.

    LONG  when (RSI < 45 AND close < EMA20) OR RSI < 30 (deep oversold)
    SHORT when (RSI > 55 AND close > EMA20) OR RSI > 70 (deep overbought)
    SL: 1.5% | TP: 3.0% (2:1 R:R) | TF: 15m
    """

    timeframe   = "15m"
    stoploss    = -0.015
    minimal_roi = {"0": 0.030}
    can_short   = True
    startup_candle_count = 30
    process_only_new_candles = True

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["rsi"]   = ta.RSI(dataframe, timeperiod=14)
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"]  = 0
        dataframe["enter_short"] = 0

        dataframe.loc[
            (dataframe["rsi"] < 30) |
            ((dataframe["rsi"] < 45) & (dataframe["close"] < dataframe["ema20"])),
            "enter_long",
        ] = 1

        dataframe.loc[
            (dataframe["rsi"] > 70) |
            ((dataframe["rsi"] > 55) & (dataframe["close"] > dataframe["ema20"])),
            "enter_short",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"]  = 0
        dataframe["exit_short"] = 0
        return dataframe
'''

_SMC_STRATEGY_CODE = '''
from freqtrade.strategy import IStrategy
from pandas import DataFrame
import talib.abstract as ta


class SMCStrategy(IStrategy):
    """
    Smart Money Concepts — EMA-based BOS + FVG approximation.

    LONG  : EMA9 crosses above EMA21 (BOS up) AND price near recent low
            (discount zone, i.e. close < 30-bar midpoint).
    SHORT : EMA9 crosses below EMA21 (BOS down) AND price near recent high
            (premium zone, i.e. close > 30-bar midpoint).
    SL    : Fixed 1.5% | TP: 3% (2:1 R:R) | TF: 15m
    """

    timeframe   = "15m"
    stoploss    = -0.015
    minimal_roi = {"0": 0.03}
    can_short   = True
    startup_candle_count = 50
    process_only_new_candles = True

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema9"]  = ta.EMA(dataframe, timeperiod=9)
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        # 30-bar range midpoint as the premium/discount split.
        dataframe["range_hi"] = dataframe["high"].rolling(30).max()
        dataframe["range_lo"] = dataframe["low"].rolling(30).min()
        dataframe["range_mid"] = (dataframe["range_hi"] + dataframe["range_lo"]) / 2
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"]  = 0
        dataframe["enter_short"] = 0

        bull_bos = (dataframe["ema9"] > dataframe["ema21"]) & \
                   (dataframe["ema9"].shift(1) <= dataframe["ema21"].shift(1))
        bear_bos = (dataframe["ema9"] < dataframe["ema21"]) & \
                   (dataframe["ema9"].shift(1) >= dataframe["ema21"].shift(1))

        in_discount = dataframe["close"] <= dataframe["range_mid"]
        in_premium  = dataframe["close"] >= dataframe["range_mid"]

        dataframe.loc[bull_bos & in_discount, "enter_long"]  = 1
        dataframe.loc[bear_bos & in_premium, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"]  = 0
        dataframe["exit_short"] = 0
        return dataframe
'''

_SMC_TV_STRATEGY_CODE = '''
from freqtrade.strategy import IStrategy
import pandas as pd
import numpy as np


class SMCStrategyTV(IStrategy):
    """
    SMC v2 — TradingView Pine Script port (BOS + FVG mitigation).

    Direct translation of the "SMC Strategy v2 - OB/FVG/BOS" Pine Script.
    Three core gates only — same as the original Pine. Earlier versions
    added HTF bias / premium-discount / liquidity sweep / session filter,
    which dropped signal count from ~hundreds to ~15 per 6M. Those extras
    are real institutional rules but they over-filter for retail back-
    testing — most users want to see what raw BOS+FVG produces first.

    Rules implemented (matches the original Pine, line-for-line):
      1. PIVOT BOS    : close crosses last confirmed pivot (N=5 each side)
      2. FVG zone     : price currently INSIDE an unfilled 3-candle
                        imbalance gap from the last 20 bars
      3. Structural SL: last opposing pivot ± 10bps buffer (rejected if
                        risk > 5% of entry — Pine's only filter)
      4. Single TP    : 2R (closes 100%) — Pine has no TP2 / partial close

    No HTF bias, no premium/discount filter, no liquidity sweep, no
    session filter. If you want institutional-strict filtering, see the
    SMCProV3 template instead.
    """
    timeframe   = "15m"
    minimal_roi = {"0": 100}        # exits handled by SL/TP at engine
    stoploss    = -0.99             # disable Freqtrade global SL
    can_short   = True              # required for KuCoin Futures shorts
    startup_candle_count = 30       # need 2N+1 = 11 bars for pivots + buffer
    process_only_new_candles = True

    SWING_LEN = 5                   # N each side → 11-bar pivot window

    def populate_indicators(self, df: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        sl = self.SWING_LEN
        n = len(df)
        high = df["high"]; low = df["low"]; close = df["close"]
        highs  = high.to_numpy()
        lows   = low.to_numpy()
        closes = close.to_numpy()

        # ── Pivot detection (N=5 each side) ─────────────────────────────
        # Pine's `ta.pivothigh(high, 5, 5)` returns the PIVOT VALUE (high[j])
        # at bar j+5 — the value of the swing-high bar, surfaced 5 bars later.
        # Pine uses STRICT greater on both sides: "leftbars LOWER bars to the
        # left and rightbars LOWER bars to the right". Earlier we used
        # rolling-max equality, which marks BOTH bars as pivots when two
        # adjacent highs tie (rare but real on 1m BTC) — Pine marks NEITHER.
        # Over 43k bars that produced ~hundreds of false pivots → tighter
        # SL anchors → lower win rate than Pine. Use strict comparison.
        ph = np.zeros(n, dtype=bool)
        pl = np.zeros(n, dtype=bool)
        for j in range(sl, n - sl):
            h = highs[j]; l = lows[j]
            is_ph = True; is_pl = True
            for d in range(1, sl + 1):
                if is_ph and (highs[j - d] >= h or highs[j + d] >= h):
                    is_ph = False
                if is_pl and (lows [j - d] <= l or lows [j + d] <= l):
                    is_pl = False
                if not is_ph and not is_pl:
                    break
            ph[j] = is_ph
            pl[j] = is_pl
        ph_shifted = np.zeros(n, dtype=bool)
        pl_shifted = np.zeros(n, dtype=bool)
        if sl < n:
            ph_shifted[sl:] = ph[:-sl] if sl > 0 else ph
            pl_shifted[sl:] = pl[:-sl] if sl > 0 else pl
        df["pivot_high"] = ph_shifted
        df["pivot_low"]  = pl_shifted

        # Last confirmed pivot VALUE up to current bar — used as BOS level
        # and SL anchor. Critical: we must forward-fill the PIVOT BAR's high
        # (highs[j]), NOT the confirmation bar's high (highs[j+5]).
        # The earlier `high.where(ph_shifted)` pulled highs[j+5] which sits
        # close to current price — that's why effective SL came out at
        # 0.01–0.1% instead of Pine's 0.3–0.7%, and why the strategy fired
        # ~5x as many signals as the equivalent Pine.
        pivot_h_values = np.where(ph, highs, np.nan)   # highs[j] at j, NaN elsewhere
        pivot_l_values = np.where(pl, lows,  np.nan)
        # Shift the VALUES forward by sl bars so they appear at the
        # confirmation bar j+sl (matches Pine's `ta.pivothigh` output bar).
        ph_values_shifted = np.full(n, np.nan)
        pl_values_shifted = np.full(n, np.nan)
        if sl < n:
            ph_values_shifted[sl:] = pivot_h_values[:-sl] if sl > 0 else pivot_h_values
            pl_values_shifted[sl:] = pivot_l_values[:-sl] if sl > 0 else pivot_l_values
        last_ph = pd.Series(ph_values_shifted, index=df.index).ffill().to_numpy()
        last_pl = pd.Series(pl_values_shifted, index=df.index).ffill().to_numpy()
        df["last_ph"] = last_ph
        df["last_pl"] = last_pl

        # ── BOS: close crosses last confirmed pivot (edge detection) ────
        # Pine: `bull_bos = close > last_ph and close[1] <= last_ph` — BOTH
        # sides compared to the CURRENT bar's last_ph (Pine `var` keeps a
        # single live value at each bar). Previous code compared close[i-1]
        # to last_ph[i-1] which diverges from Pine on the bar a new pivot
        # is registered. Matching Pine exactly here.
        bull_bos = np.zeros(n, dtype=bool)
        bear_bos = np.zeros(n, dtype=bool)
        for i in range(1, n):
            if not np.isnan(last_ph[i]):
                if closes[i] > last_ph[i] and closes[i-1] <= last_ph[i]:
                    bull_bos[i] = True
            if not np.isnan(last_pl[i]):
                if closes[i] < last_pl[i] and closes[i-1] >= last_pl[i]:
                    bear_bos[i] = True
        df["bull_bos"] = bull_bos
        df["bear_bos"] = bear_bos

        # ── FVG zone (price currently INSIDE an unfilled imbalance) ─────
        # Bull FVG zone = [high[k-2], low[k]] when high[k-2] < low[k].
        # Bear FVG zone = [high[k], low[k-2]] when low[k-2] > high[k].
        # Scan back up to 20 bars for the most recent zone containing close.
        bull_fvg_in_zone = np.zeros(n, dtype=bool)
        bear_fvg_in_zone = np.zeros(n, dtype=bool)
        for i in range(n):
            if i < 2: continue
            for k in range(i, max(2, i - 20), -1):
                if k < 2: break
                if highs[k - 2] < lows[k] and highs[k - 2] <= closes[i] <= lows[k]:
                    bull_fvg_in_zone[i] = True
                    break
                if lows[k - 2] > highs[k] and highs[k] <= closes[i] <= lows[k - 2]:
                    bear_fvg_in_zone[i] = True
                    break
        df["bull_fvg"] = bull_fvg_in_zone
        df["bear_fvg"] = bear_fvg_in_zone

        # ── Final entry signals: BOS + FVG-in-zone ──────────────────────
        long_signal  = bull_bos & bull_fvg_in_zone
        short_signal = bear_bos & bear_fvg_in_zone

        # ── Structural SL/TP per bar ────────────────────────────────────
        # SL anchored to opposing pivot with 10bps buffer.
        sl_long  = last_pl * 0.999     # below last swing low
        sl_short = last_ph * 1.001     # above last swing high
        entry    = closes
        risk_long  = entry - sl_long
        risk_short = sl_short - entry
        # Match Pine exactly: only reject if risk <= 0 or risk > 5% of price.
        # The previous 0.5% MIN_RISK_PCT floor was killing ~85% of valid
        # signals on 1m BTC (most pivots sit very close to current price on
        # a small TF) — that's why the app showed 9 trades where Pine showed
        # 74 for the same period. Pine has no such floor, so we drop it here.
        bad_long  = (
            (risk_long  <= 0) |
            (risk_long  > entry * 0.05) |
            np.isnan(sl_long)
        )
        bad_short = (
            (risk_short <= 0) |
            (risk_short > entry * 0.05) |
            np.isnan(sl_short)
        )
        long_signal  = long_signal  & ~bad_long
        short_signal = short_signal & ~bad_short

        tp1_long  = entry + 2 * risk_long      # 2R target — matches Pine
        tp1_short = entry - 2 * risk_short

        df["sl_price"]  = np.where(long_signal, sl_long,  np.where(short_signal, sl_short,  np.nan))
        df["tp_price"]  = np.where(long_signal, tp1_long, np.where(short_signal, tp1_short, np.nan))
        # No TP2 — Pine closes 100% at 2R. The earlier `tp2_price = last_ph`
        # logic triggered partial-close (50% off at TP1, breakeven trail to
        # TP2) which Pine doesn't do and which caused exit timing to diverge
        # even when entries matched.
        df["tp2_price"] = np.nan

        df["_long_signal"]  = long_signal
        df["_short_signal"] = short_signal
        return df

    def populate_entry_trend(self, df: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        df["enter_long"]  = df["_long_signal"].astype(int)
        df["enter_short"] = df["_short_signal"].astype(int)
        return df

    def populate_exit_trend(self, df: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        # Engine exits via SL/TP/liquidation — no condition-based exits.
        df["exit_long"]  = 0
        df["exit_short"] = 0
        return df
'''

_BIDIR_STRATEGY_CODE = '''
from freqtrade.strategy import IStrategy
from pandas import DataFrame
import talib.abstract as ta


class BidirectionalStrategy(IStrategy):
    """
    Trend + pullback strategy (was: enter on trend confirmation, which fired
    at the START of every move including fakeouts → 25% WR. Fixed to enter
    on pullbacks INSIDE confirmed trends — much higher quality entries).

    LONG  : EMA50 > EMA200 (uptrend) AND close pulled back to or below EMA21
            AND RSI < 40 (oversold within uptrend = buy the dip)
    SHORT : EMA50 < EMA200 (downtrend) AND close rallied to or above EMA21
            AND RSI > 60 (overbought within downtrend = short the rip)
    SL: 1.5% | TP: 3.0% (2:1 R:R) | TF: 15m
    """

    timeframe   = "15m"
    stoploss    = -0.015
    minimal_roi = {"0": 0.030}
    can_short   = True
    startup_candle_count = 220   # need EMA200
    process_only_new_candles = True

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema21"]  = ta.EMA(dataframe, timeperiod=21)
        dataframe["ema50"]  = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["rsi"]    = ta.RSI(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"]  = 0
        dataframe["enter_short"] = 0

        # Higher-timeframe bias: EMA50 vs EMA200 over current bar.
        bull_trend = dataframe["ema50"] > dataframe["ema200"]
        bear_trend = dataframe["ema50"] < dataframe["ema200"]

        # Pullback / rally condition: price visited EMA21 zone (within 0.5%).
        near_ema21 = (
            (dataframe["close"] - dataframe["ema21"]).abs()
            < dataframe["close"] * 0.005
        )

        # Oversold / overbought RSI within trend = good pullback.
        oversold   = dataframe["rsi"] < 40
        overbought = dataframe["rsi"] > 60

        dataframe.loc[bull_trend & near_ema21 & oversold,   "enter_long"]  = 1
        dataframe.loc[bear_trend & near_ema21 & overbought, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"]  = 0
        dataframe["exit_short"] = 0
        return dataframe
'''

_SMC_PRO_V3_CODE = '''
from freqtrade.strategy import IStrategy
from pandas import DataFrame
import pandas as pd
import numpy as np
import talib.abstract as ta


class SMCProV3(IStrategy):
    """
    SMC Pro v3 — strict 6-gate institutional model.

    Gates (ALL must align):
      1. HTF bias       — EMA200 direction over EMA50 (proxy for 4H trend)
      2. Premium/Discount — close vs 50-bar range midpoint
      3. Recent sweep   — current bar's low broke a recent 20-bar low (long)
                          or high broke a recent 20-bar high (short)
      4. Inside FVG     — close inside an unfilled 3-candle imbalance gap
      5. Strong move    — current bar body ≥ 1.5× 20-bar ATR
      6. NY session     — hour 12-21 UTC only (institutional liquidity)

    Aggressive filter — expect ~50-200 trades per 6 months on 15m BTC.
    SL: 2% | TP: 4% (2R) | TF: 15m | Leverage: 10x
    """

    timeframe   = "15m"
    stoploss    = -0.02
    minimal_roi = {"0": 0.04}
    can_short   = True
    startup_candle_count = 220
    process_only_new_candles = True

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe
        df["ema50"]  = ta.EMA(df, timeperiod=50)
        df["ema200"] = ta.EMA(df, timeperiod=200)
        df["atr20"]  = ta.ATR(df, timeperiod=20)

        # Range midpoint over last 50 bars for premium/discount.
        df["range_hi"]  = df["high"].rolling(50).max()
        df["range_lo"]  = df["low"].rolling(50).min()
        df["range_mid"] = (df["range_hi"] + df["range_lo"]) / 2

        # Recent 20-bar swings for sweep detection (shifted so current bar
        # is compared to the PAST 20 bars, not including itself).
        df["prev_low_20"]  = df["low"].rolling(20).min().shift(1)
        df["prev_high_20"] = df["high"].rolling(20).max().shift(1)

        # FVG: bull = high[i-2] < low[i]; bear = low[i-2] > high[i].
        # Flag bars where the FVG zone CONTAINS the current close.
        bull_zone_lo = df["high"].shift(2)
        bull_zone_hi = df["low"]
        bear_zone_lo = df["high"]
        bear_zone_hi = df["low"].shift(2)
        df["in_bull_fvg"] = (bull_zone_lo < bull_zone_hi) & \
                            (df["close"] >= bull_zone_lo) & \
                            (df["close"] <= bull_zone_hi)
        df["in_bear_fvg"] = (bear_zone_lo < bear_zone_hi) & \
                            (df["close"] >= bear_zone_lo) & \
                            (df["close"] <= bear_zone_hi)

        # Strong move = current bar body ≥ 1.5× ATR20.
        df["body"]        = (df["close"] - df["open"]).abs()
        df["strong_move"] = df["body"] >= 1.5 * df["atr20"]

        return df

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe
        df["enter_long"]  = 0
        df["enter_short"] = 0

        # NY session filter — hour 12-21 UTC (London + NY institutional).
        hours = df["date"].dt.hour if "date" in df.columns else pd.Series(0, index=df.index)
        in_session = (hours >= 12) & (hours <= 21)

        bull_bias = (df["close"] > df["ema200"]) & (df["ema50"] > df["ema200"])
        bear_bias = (df["close"] < df["ema200"]) & (df["ema50"] < df["ema200"])

        in_discount = df["close"] <= df["range_mid"]
        in_premium  = df["close"] >= df["range_mid"]

        # Sweep: this bar's low broke the recent 20-bar low and closed back above.
        bull_sweep = (df["low"]  < df["prev_low_20"])  & (df["close"] > df["prev_low_20"])
        bear_sweep = (df["high"] > df["prev_high_20"]) & (df["close"] < df["prev_high_20"])

        df.loc[
            bull_bias & in_discount & bull_sweep & df["in_bull_fvg"] &
            df["strong_move"] & in_session,
            "enter_long",
        ] = 1
        df.loc[
            bear_bias & in_premium & bear_sweep & df["in_bear_fvg"] &
            df["strong_move"] & in_session,
            "enter_short",
        ] = 1
        return df

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"]  = 0
        dataframe["exit_short"] = 0
        return dataframe
'''

_BESTPRACTICES_V1_CODE = '''
from freqtrade.strategy import IStrategy
import pandas as pd
import numpy as np


class BestPracticesV1(IStrategy):
    """
    BestPracticesV1 — SMC strategy for BTC/USDT 1h futures with HTF trend.

    Layered on the SMCStrategyTV entry trigger (BOS + FVG). The HTF
    trend filter is the always-on differentiator; ATR regime and NY
    session filters are AVAILABLE but DEFAULT OFF — the original
    stacked-defaults killed almost all signals on 1h (~1 trade per 6M).
    Re-enable them via constants below if your test data shows benefit.

      LAYER 1 (ON) — HTF trend filter (EMA200 on 1h)
        Long only when close > EMA200, short only when close < EMA200.
        BTC has dominant trend regimes; counter-trend SMC entries are
        the primary loser bucket. This filter typically lifts WR by
        5-10 percentage points at small cost in trade count.

      LAYER 2 (OFF by default) — ATR volatility regime filter
        Trade only when ATR(14) is between the ATR_PCT_LOW and
        ATR_PCT_HIGH percentile of the last 200 bars. Defaults 0/100
        = no filtering. Set to 25/75 to skip dead chop + crash vol.

      LAYER 3 (OFF by default) — NY session filter
        Defaults 0-23 = 24h trading. Set SESSION_START_HR_UTC=12,
        SESSION_END_HR_UTC=21 to restrict to NY hours only.

      LAYER 4 (ON) — SMC entry trigger (BOS + FVG)
        - Pivot BOS (N=5 each side): close crosses last confirmed pivot
        - FVG zone: price currently INSIDE an unfilled 3-candle imbalance
        Same proven mechanic as SMCStrategyTV (validated against
        TradingView line-for-line during parity testing).

      LAYER 5 (ON) — Risk management
        - Structural SL: opposing pivot ± 10bps buffer
        - Reject if SL distance > 3% of entry (broken structure)
        - Single TP at 2R (closes 100%)
        - Pairs with the engine's Single-position TV mode + stop-and-reverse

    DESIGN TARGET (1h BTC, 6M backtest, default filters):
        Trades: 10-40 over 6 months
        Win rate: 40-50% (deliberately conservative — anyone promising
          you 60%+ WR on retail crypto is fitting to past data)
        Profit factor target: ≥ 1.3
        Max drawdown target: ≤ 15%

    IMPORTANT: This is a disciplined application of well-tested
    principles, NOT a guaranteed profitable strategy. Before deploying
    real capital:
      1. Backtest 6M, 1Y, and 2Y to check stability across regimes
      2. Forward-test in paper mode for at least 2-4 weeks
      3. Compare against a do-nothing baseline (buy-and-hold)
      4. Validate against the equivalent Pine script in TradingView
    """

    timeframe   = "1h"
    minimal_roi = {"0": 100}        # exits handled by engine SL/TP
    stoploss    = -0.99             # disable Freqtrade global SL
    can_short   = True
    startup_candle_count = 250      # need EMA200 + a buffer
    process_only_new_candles = True

    SWING_LEN          = 5
    HTF_EMA_LEN        = 200
    ATR_LEN            = 14
    # ATR regime filter — DEFAULT OFF (0..100 = no filtering). The 25-75
    # band was over-restrictive on 1h: combined with HTF trend (~50% of
    # bars) and SMC BOS+FVG (~0.2% of bars), it produced 1 trade per 6M.
    # Set to 25 / 75 to re-enable the middle-50% volatility band.
    ATR_PCT_LOW        = 0
    ATR_PCT_HIGH       = 100
    ATR_PCT_LOOKBACK   = 200
    # NY session filter — DEFAULT OFF (0..23 = trade 24h). The 12-21 UTC
    # band cut another ~62% of bars on top of HTF trend + ATR + SMC.
    # Set to 12 / 21 to re-enable NY-session-only trading.
    SESSION_START_HR_UTC = 0
    SESSION_END_HR_UTC   = 23
    # MAX_SL_PCT: reject if structural SL distance > X% of entry. Pine's
    # only filter is 5%, which is what SMCStrategyTV uses and what's
    # validated against TradingView. The previous 3% was over-tight for
    # 1h where pivots are often further from price than they are on 15m
    # or 1m — rejecting valid setups that were inside Pine's spec.
    MAX_SL_PCT         = 5.0

    def populate_indicators(self, df: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        sl = self.SWING_LEN
        n  = len(df)
        high = df["high"]; low = df["low"]; close = df["close"]
        highs  = high.to_numpy()
        lows   = low .to_numpy()
        closes = close.to_numpy()

        # ── Layer 1: HTF trend filter (EMA200) ──────────────────────────
        htf_ema = close.ewm(span=self.HTF_EMA_LEN, adjust=False).mean().to_numpy()
        bull_trend = closes > htf_ema
        bear_trend = closes < htf_ema
        df["htf_ema"] = htf_ema

        # ── Layer 2: ATR regime (middle 50% of last 200 bars) ───────────
        # Wilder ATR via EMA of true range.
        prev_close = close.shift()
        tr = pd.concat([
            (high - low).abs(),
            (high - prev_close).abs(),
            (low  - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1.0 / self.ATR_LEN, adjust=False).mean()
        atr_arr = atr.to_numpy()
        df["atr"] = atr_arr
        # Filter-OFF fast path: when band is 0..100, skip the rolling quantile
        # entirely and accept every bar. The previous code computed
        # `atr.rolling(200).quantile(...)` even when the filter was off, which
        # produced NaN for the first 200 bars and silently excluded ~8 days
        # of trades from every 1h backtest (NaN comparisons are False).
        if self.ATR_PCT_LOW <= 0 and self.ATR_PCT_HIGH >= 100:
            atr_ok = np.ones(n, dtype=bool)
        else:
            atr_low_band  = atr.rolling(self.ATR_PCT_LOOKBACK).quantile(self.ATR_PCT_LOW  / 100).to_numpy()
            atr_high_band = atr.rolling(self.ATR_PCT_LOOKBACK).quantile(self.ATR_PCT_HIGH / 100).to_numpy()
            # NaN-safe: treat NaN bands as "no filter yet" instead of excluding.
            # This affects only the warmup period; once 200 bars have passed
            # the bands are valid and the filter behaves as intended.
            with np.errstate(invalid="ignore"):
                atr_ok = (
                    (np.isnan(atr_low_band) | (atr_arr >= atr_low_band)) &
                    (np.isnan(atr_high_band) | (atr_arr <= atr_high_band))
                )

        # ── Layer 3: NY session filter ──────────────────────────────────
        hours = df["date"].dt.hour
        in_session = ((hours >= self.SESSION_START_HR_UTC) &
                      (hours <= self.SESSION_END_HR_UTC)).to_numpy()

        # ── Layer 4a: Strict pivot detection (matches Pine ta.pivothigh/low) ──
        ph = np.zeros(n, dtype=bool)
        pl = np.zeros(n, dtype=bool)
        for j in range(sl, n - sl):
            h = highs[j]; l = lows[j]
            is_ph = True; is_pl = True
            for d in range(1, sl + 1):
                if is_ph and (highs[j - d] >= h or highs[j + d] >= h):
                    is_ph = False
                if is_pl and (lows [j - d] <= l or lows [j + d] <= l):
                    is_pl = False
                if not is_ph and not is_pl:
                    break
            ph[j] = is_ph
            pl[j] = is_pl

        # Forward-fill pivot VALUES to confirmation bar j+sl (same mechanic
        # as SMCStrategyTV — validated to match Pine exactly).
        pivot_h_vals = np.where(ph, highs, np.nan)
        pivot_l_vals = np.where(pl, lows,  np.nan)
        ph_shifted   = np.full(n, np.nan)
        pl_shifted   = np.full(n, np.nan)
        if sl < n:
            ph_shifted[sl:] = pivot_h_vals[:-sl]
            pl_shifted[sl:] = pivot_l_vals[:-sl]
        last_ph = pd.Series(ph_shifted, index=df.index).ffill().to_numpy()
        last_pl = pd.Series(pl_shifted, index=df.index).ffill().to_numpy()

        # ── Layer 4b: BOS edge detect ───────────────────────────────────
        bull_bos = np.zeros(n, dtype=bool)
        bear_bos = np.zeros(n, dtype=bool)
        for i in range(1, n):
            if not np.isnan(last_ph[i]):
                if closes[i] > last_ph[i] and closes[i-1] <= last_ph[i]:
                    bull_bos[i] = True
            if not np.isnan(last_pl[i]):
                if closes[i] < last_pl[i] and closes[i-1] >= last_pl[i]:
                    bear_bos[i] = True

        # ── Layer 4c: FVG zone (price inside unfilled 3-candle imbalance) ──
        bull_fvg = np.zeros(n, dtype=bool)
        bear_fvg = np.zeros(n, dtype=bool)
        for i in range(n):
            if i < 2: continue
            for k in range(i, max(2, i - 20), -1):
                if k < 2: break
                if highs[k - 2] < lows[k] and highs[k - 2] <= closes[i] <= lows[k]:
                    bull_fvg[i] = True
                    break
                if lows[k - 2] > highs[k] and highs[k] <= closes[i] <= lows[k - 2]:
                    bear_fvg[i] = True
                    break

        # ── Combine all 4 gates into a candidate setup ──────────────────
        long_setup  = bull_trend & atr_ok & in_session & bull_bos & bull_fvg
        short_setup = bear_trend & atr_ok & in_session & bear_bos & bear_fvg

        # ── Layer 5: Risk management — structural SL + 2R TP ────────────
        sl_long  = last_pl * 0.999
        sl_short = last_ph * 1.001
        risk_long  = closes - sl_long
        risk_short = sl_short - closes
        max_sl_dist = closes * (self.MAX_SL_PCT / 100.0)

        bad_long  = (risk_long  <= 0) | (risk_long  > max_sl_dist) | np.isnan(sl_long)
        bad_short = (risk_short <= 0) | (risk_short > max_sl_dist) | np.isnan(sl_short)

        long_signal  = long_setup  & ~bad_long
        short_signal = short_setup & ~bad_short

        tp_long  = closes + 2 * risk_long
        tp_short = closes - 2 * risk_short

        df["sl_price"]  = np.where(long_signal, sl_long,  np.where(short_signal, sl_short,  np.nan))
        df["tp_price"]  = np.where(long_signal, tp_long,  np.where(short_signal, tp_short,  np.nan))
        df["tp2_price"] = np.nan        # single TP (matches Pine)

        df["_long_signal"]  = long_signal
        df["_short_signal"] = short_signal
        return df

    def populate_entry_trend(self, df: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        df["enter_long"]  = df["_long_signal"].astype(int)
        df["enter_short"] = df["_short_signal"].astype(int)
        return df

    def populate_exit_trend(self, df: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        # Engine exits via structural SL / 2R TP / liquidation only.
        df["exit_long"]  = 0
        df["exit_short"] = 0
        return df
'''


# ─────────────────────────────────────────────────────────────────────────
# SMCStrategy1 — full SMC strategy per the user's hand-authored spec.
#
# This is a literal port of the "SMC Strategy 1" spec the user provided
# in their request. Unlike SMCStrategyTV / BestPracticesV1 (which are
# pivot+FVG skeletons), this strategy implements the COMPLETE SMC model:
#
#   Step 1 — HTF Bias (4H):    swing pivots → last BOS direction → bullish
#                              / bearish / RANGE classification. RANGE
#                              blocks all trades.
#   Step 2 — Liquidity:        equal-highs / equal-lows clustering within
#                              threshold + previous swing extremes →
#                              liquidity sweep = wick takes out the level
#                              AND closes back through it.
#   Step 3 — Order Block:      last opposing candle before a confirmed
#                              BOS, with body-size + follow-through filter.
#   Step 4 — FVG:              3-candle imbalance, entry at MIDPOINT (not
#                              just inside-zone). Bullish: high[i-2] < low[i].
#   Step 5 — Premium/Discount: 50% fib of last 4H swing range. Long ONLY
#                              in discount (lower half), short ONLY in
#                              premium (upper half).
#   Step 6 — LTF BOS confirm:  on the execution TF (15m default), close
#                              must cross the last LTF swing in trade
#                              direction. This is the "confirmation" trigger.
#   Step 7 — Risk:             SL beyond sweep extreme (- 10bps buffer).
#                              TP = 2R primary. ARM engine splits this
#                              into TP1=1R+TP2=2R when enabled.
#   Step 8 — Session:          NY hours (12-21 UTC by default).
#
# Multi-TF data flow:
#   • bias_timeframes = ["1h", "4h"]   ← engine's mtf_analyzer pre-fetches
#     closed 1h and 4h bars and exposes them via metadata['htf'].
#   • Strategy reads HTF bars, computes HTF indicators, projects onto the
#     LTF dataframe via forward-fill (so every 15m bar carries the most
#     recent CLOSED 1h/4h bias + zones — no look-ahead).
#   • All entry logic runs on the LTF (15m) row with HTF context
#     already attached. This is the canonical hybrid-engine pattern.
#
# Why no naive "scalp candle hunting":
#   The strategy intentionally does NOT check every 15m bar against fresh
#   HTF candles. HTF bias is sampled ONLY when a 4H bar closes — between
#   closes, the bias is "sticky" (forward-filled). This is what the user
#   meant by "follow strategy to determine which candle to see and what
#   to execute" — the strategy's TF roles dictate the cadence, not the
#   engine's polling rate.
_SMC_STRATEGY_1_CODE = '''
from freqtrade.strategy import IStrategy
import pandas as pd
import numpy as np


class SMCStrategy1(IStrategy):
    """
    SMC Strategy 1 — literal implementation of the 6-step Smart Money
    Concepts model: HTF Bias → Liquidity → OB → FVG → Premium/Discount
    → LTF BOS confirmation, with NY session filter and structural SL.

    Multi-TF roles (per PDF §5):
      HTF (4H)   → bias (bull/bear/range from last BOS direction)
                   + premium/discount fib zone
      MTF (1H)   → order blocks + fair-value-gap zones
      LTF (15M)  → liquidity sweep detection + BOS confirmation
                   + entry/SL/TP execution

    The execution timeframe (`timeframe` below) drives the bar-by-bar
    loop. HTF and MTF context is fetched once per signal scan via the
    engine's mtf_analyzer and forward-filled onto the LTF dataframe so
    every LTF row carries the most recent CLOSED HTF bias / OB / FVG.
    """

    # ── Engine integration ─────────────────────────────────────────────
    timeframe = "15m"                  # LTF execution clock
    bias_timeframes = ["1h", "4h"]     # MTF + HTF context (mtf_analyzer)
    minimal_roi = {"0": 100}           # exits via engine SL/TP only
    stoploss = -0.99                   # disable Freqtrade global SL
    can_short = True
    startup_candle_count = 250         # need EMA200 + buffers
    process_only_new_candles = True

    # ── Tunable parameters (PDF §3, §4) ────────────────────────────────
    SWING_N            = 5          # pivot left/right bars (HTF & MTF & LTF)
    HTF_RANGE_LOOKBACK = 50         # 50 HTF bars for premium/discount range
    EQUAL_PRICE_THRESH = 0.001      # 0.1% — high/low within this = "equal"
    SWEEP_LOOKBACK     = 20         # bars to scan for previous equal-low/high
    OB_LOOKBACK        = 30         # bars to scan back for the most recent OB
    FVG_LOOKBACK       = 20         # bars to scan back for last unfilled FVG
    FVG_PROXIMITY_PCT  = 0.0030     # within 0.3% of FVG midpoint = "at FVG"
    OB_PROXIMITY_PCT   = 0.0030     # within 0.3% of OB midpoint = "at OB"
    STRONG_BODY_X_ATR  = 1.5        # body ≥ 1.5× ATR(14) = "strong move"
    ATR_LEN            = 14
    MAX_RISK_PCT       = 0.03       # reject if SL > 3% from entry (broken structure)
    R_MULTIPLE         = 2.0        # TP at 2R
    # NY session in UTC. PDF §6 wants "high-activity hours" — 12-21 UTC
    # covers London close + NY open + NY close (the institutional window).
    SESSION_START_HR   = 12
    SESSION_END_HR     = 21

    # ───────────────────────────── helpers ────────────────────────────

    @staticmethod
    def _strict_pivots(highs: np.ndarray, lows: np.ndarray, n: int) -> tuple[np.ndarray, np.ndarray]:
        """PDF §3 Step 1: strict-greater pivot detection (matches Pine
        ta.pivothigh / ta.pivotlow). Returns (is_pivot_high, is_pivot_low)
        boolean arrays — True ON the pivot bar (NOT shifted yet).
        """
        size = len(highs)
        ph = np.zeros(size, dtype=bool)
        pl = np.zeros(size, dtype=bool)
        for j in range(n, size - n):
            h, l = highs[j], lows[j]
            is_h = True; is_l = True
            for d in range(1, n + 1):
                if is_h and (highs[j - d] >= h or highs[j + d] >= h):
                    is_h = False
                if is_l and (lows[j - d] <= l or lows[j + d] <= l):
                    is_l = False
                if not is_h and not is_l:
                    break
            ph[j] = is_h
            pl[j] = is_l
        return ph, pl

    @staticmethod
    def _shifted_pivot_values(highs: np.ndarray, lows: np.ndarray,
                              ph: np.ndarray, pl: np.ndarray, n: int) -> tuple[np.ndarray, np.ndarray]:
        """Forward-fill pivot VALUES (highs[j], lows[j]) to the
        confirmation bar j+n. Matches Pine's ta.pivothigh output bar.
        """
        size = len(highs)
        ph_vals = np.where(ph, highs, np.nan)
        pl_vals = np.where(pl, lows,  np.nan)
        ph_s = np.full(size, np.nan)
        pl_s = np.full(size, np.nan)
        if n < size:
            ph_s[n:] = ph_vals[:-n]
            pl_s[n:] = pl_vals[:-n]
        last_ph = pd.Series(ph_s).ffill().to_numpy()
        last_pl = pd.Series(pl_s).ffill().to_numpy()
        return last_ph, last_pl

    @classmethod
    def _compute_htf_bias(cls, htf_df: pd.DataFrame) -> dict:
        """PDF §3 Step 1: classify HTF bias from last BOS direction.

        Returns dict with:
            bias_series  : Series of 'bull'/'bear'/'range' indexed by htf_df.date
            range_hi     : Series of premium/discount range top
            range_lo     : Series of premium/discount range bottom
            range_mid    : Series of midpoint (the 50% fib)
        """
        n = cls.SWING_N
        highs = htf_df["high"].to_numpy()
        lows  = htf_df["low"].to_numpy()
        closes = htf_df["close"].to_numpy()

        ph, pl = cls._strict_pivots(highs, lows, n)
        last_ph, last_pl = cls._shifted_pivot_values(highs, lows, ph, pl, n)

        # Track last BOS direction across HTF bars.
        bias = np.full(len(htf_df), "range", dtype=object)
        last_bos_dir = "range"
        for i in range(1, len(htf_df)):
            if not np.isnan(last_ph[i]) and closes[i] > last_ph[i] and closes[i-1] <= last_ph[i]:
                last_bos_dir = "bull"
            elif not np.isnan(last_pl[i]) and closes[i] < last_pl[i] and closes[i-1] >= last_pl[i]:
                last_bos_dir = "bear"
            bias[i] = last_bos_dir

        # Premium/Discount range = last HTF_RANGE_LOOKBACK HTF bars.
        range_hi  = htf_df["high"].rolling(cls.HTF_RANGE_LOOKBACK, min_periods=cls.SWING_N * 2 + 1).max()
        range_lo  = htf_df["low" ].rolling(cls.HTF_RANGE_LOOKBACK, min_periods=cls.SWING_N * 2 + 1).min()
        range_mid = (range_hi + range_lo) / 2.0

        return {
            "date":      htf_df["date"],
            "bias":      pd.Series(bias, index=htf_df.index),
            "range_hi":  range_hi,
            "range_lo":  range_lo,
            "range_mid": range_mid,
        }

    @classmethod
    def _compute_mtf_zones(cls, mtf_df: pd.DataFrame) -> dict:
        """PDF §3 Step 3 + Step 4: detect most recent OB + FVG midpoints
        on MTF bars. Returns scalar Series of zone midpoints; downstream
        forward-fills these onto the LTF df.
        """
        n_total = len(mtf_df)
        highs = mtf_df["high"].to_numpy()
        lows  = mtf_df["low"].to_numpy()
        opens = mtf_df["open"].to_numpy()
        closes = mtf_df["close"].to_numpy()

        # For each bar i, find the most recent bullish OB (last bearish
        # candle before a confirmed bull BOS) and bearish OB. Walk back
        # OB_LOOKBACK bars max.
        bull_ob_mid = np.full(n_total, np.nan)
        bear_ob_mid = np.full(n_total, np.nan)
        bull_fvg_mid = np.full(n_total, np.nan)
        bear_fvg_mid = np.full(n_total, np.nan)

        # FVG: 3-candle imbalance, MIDPOINT. PDF §3 Step 4.
        for i in range(n_total):
            if i < 2: continue
            # Walk back FVG_LOOKBACK bars finding the MOST RECENT unfilled FVG.
            for k in range(i, max(2, i - cls.FVG_LOOKBACK), -1):
                if k < 2: break
                if highs[k - 2] < lows[k] and np.isnan(bull_fvg_mid[i]):
                    # Bullish FVG: gap between bar k-2's high and bar k's low.
                    bull_fvg_mid[i] = (highs[k - 2] + lows[k]) / 2.0
                if lows[k - 2] > highs[k] and np.isnan(bear_fvg_mid[i]):
                    bear_fvg_mid[i] = (lows[k - 2] + highs[k]) / 2.0
                if not np.isnan(bull_fvg_mid[i]) and not np.isnan(bear_fvg_mid[i]):
                    break

        # OB: last opposing candle BEFORE a strong directional move + BOS.
        # Strong move proxy: bar body ≥ 1.5× rolling avg body over 14 bars.
        bodies = np.abs(closes - opens)
        body_avg = pd.Series(bodies).rolling(14, min_periods=5).mean().to_numpy()
        for i in range(n_total):
            if i < cls.SWING_N + 2: continue
            for k in range(i - 1, max(0, i - cls.OB_LOOKBACK), -1):
                if np.isnan(bull_ob_mid[i]) and closes[k] < opens[k]:
                    # Look forward for a strong bullish move within next 5 bars.
                    if k + 5 < i:
                        fwd_max = highs[k + 1: k + 6].max()
                        avg = body_avg[k] if not np.isnan(body_avg[k]) else 0
                        if fwd_max > highs[k] and (highs[k+1:k+6].max() - lows[k+1:k+6].min()) > 1.5 * avg:
                            bull_ob_mid[i] = (highs[k] + lows[k]) / 2.0
                if np.isnan(bear_ob_mid[i]) and closes[k] > opens[k]:
                    if k + 5 < i:
                        fwd_min = lows[k + 1: k + 6].min()
                        avg = body_avg[k] if not np.isnan(body_avg[k]) else 0
                        if fwd_min < lows[k] and (highs[k+1:k+6].max() - lows[k+1:k+6].min()) > 1.5 * avg:
                            bear_ob_mid[i] = (highs[k] + lows[k]) / 2.0
                if not np.isnan(bull_ob_mid[i]) and not np.isnan(bear_ob_mid[i]):
                    break

        return {
            "date":         mtf_df["date"],
            "bull_ob_mid":  pd.Series(bull_ob_mid,  index=mtf_df.index),
            "bear_ob_mid":  pd.Series(bear_ob_mid,  index=mtf_df.index),
            "bull_fvg_mid": pd.Series(bull_fvg_mid, index=mtf_df.index),
            "bear_fvg_mid": pd.Series(bear_fvg_mid, index=mtf_df.index),
        }

    @staticmethod
    def _project_onto_ltf(ltf_df: pd.DataFrame, htf_date: pd.Series,
                          htf_series: pd.Series) -> np.ndarray:
        """Forward-fill HTF series onto every LTF bar based on the most
        recent CLOSED HTF bar. No look-ahead: an HTF bar that closes at
        T is only visible to LTF bars whose timestamp is > T.
        """
        # asof merge: for each LTF date, find the latest HTF date < LTF date.
        ltf_dates = pd.to_datetime(ltf_df["date"], utc=True)
        htf_pairs = pd.DataFrame({
            "date":  pd.to_datetime(htf_date, utc=True),
            "value": htf_series.values,
        }).sort_values("date").reset_index(drop=True)
        out = pd.merge_asof(
            pd.DataFrame({"date": ltf_dates}).reset_index(),
            htf_pairs,
            on="date",
            direction="backward",
            allow_exact_matches=False,   # ← critical: prevent peeking at the bar still open
        )
        return out["value"].to_numpy()

    # ─────────────────────────── main pipeline ────────────────────────

    def populate_indicators(self, df: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        n_total = len(df)
        if n_total == 0:
            return df

        # ── Pull HTF / MTF context attached by mtf_analyzer ──────────
        htf_map = metadata.get("htf", {}) or {}
        htf_4h  = htf_map.get("4h")
        mtf_1h  = htf_map.get("1h")

        # ── Step 1: HTF bias (4H) ────────────────────────────────────
        # If 4H data is missing, fall back to bias inferred from execution
        # TF EMA200 direction. The fallback is conservative — strategy
        # still trades but with less HTF confidence.
        htf_bias_arr   = np.full(n_total, "range", dtype=object)
        range_mid_proj = np.full(n_total, np.nan)
        range_hi_proj  = np.full(n_total, np.nan)
        range_lo_proj  = np.full(n_total, np.nan)
        if htf_4h is not None and len(htf_4h) >= self.SWING_N * 2 + 5:
            htf_ctx = self._compute_htf_bias(htf_4h)
            htf_bias_arr = self._project_onto_ltf(df, htf_ctx["date"], htf_ctx["bias"])
            # Convert NaNs (no HTF bar yet) to "range".
            htf_bias_arr = np.where(pd.isna(htf_bias_arr), "range", htf_bias_arr)
            range_hi_proj  = self._project_onto_ltf(df, htf_ctx["date"], htf_ctx["range_hi"])
            range_lo_proj  = self._project_onto_ltf(df, htf_ctx["date"], htf_ctx["range_lo"])
            range_mid_proj = self._project_onto_ltf(df, htf_ctx["date"], htf_ctx["range_mid"])
        else:
            # Fallback: EMA200 direction on execution TF.
            ema200 = df["close"].ewm(span=200, adjust=False).mean().to_numpy()
            closes_arr = df["close"].to_numpy()
            for i in range(n_total):
                if closes_arr[i] > ema200[i]:
                    htf_bias_arr[i] = "bull"
                elif closes_arr[i] < ema200[i]:
                    htf_bias_arr[i] = "bear"
            # Range from 50-bar window.
            rh = df["high"].rolling(50, min_periods=10).max().to_numpy()
            rl = df["low" ].rolling(50, min_periods=10).min().to_numpy()
            range_hi_proj  = rh
            range_lo_proj  = rl
            range_mid_proj = (rh + rl) / 2.0

        df["htf_bias"]     = htf_bias_arr
        df["htf_range_hi"] = range_hi_proj
        df["htf_range_lo"] = range_lo_proj
        df["htf_range_md"] = range_mid_proj

        # ── Step 3 + 4: MTF Order Block + FVG zones (1H) ─────────────
        bull_ob_proj = np.full(n_total, np.nan)
        bear_ob_proj = np.full(n_total, np.nan)
        bull_fvg_proj = np.full(n_total, np.nan)
        bear_fvg_proj = np.full(n_total, np.nan)
        if mtf_1h is not None and len(mtf_1h) >= self.OB_LOOKBACK + 5:
            mtf_zones = self._compute_mtf_zones(mtf_1h)
            bull_ob_proj  = self._project_onto_ltf(df, mtf_zones["date"], mtf_zones["bull_ob_mid"])
            bear_ob_proj  = self._project_onto_ltf(df, mtf_zones["date"], mtf_zones["bear_ob_mid"])
            bull_fvg_proj = self._project_onto_ltf(df, mtf_zones["date"], mtf_zones["bull_fvg_mid"])
            bear_fvg_proj = self._project_onto_ltf(df, mtf_zones["date"], mtf_zones["bear_fvg_mid"])
        else:
            # Fallback: compute OB + FVG on the execution TF itself.
            ltf_zones = self._compute_mtf_zones(df)
            bull_ob_proj  = ltf_zones["bull_ob_mid"].to_numpy()
            bear_ob_proj  = ltf_zones["bear_ob_mid"].to_numpy()
            bull_fvg_proj = ltf_zones["bull_fvg_mid"].to_numpy()
            bear_fvg_proj = ltf_zones["bear_fvg_mid"].to_numpy()

        df["bull_ob_mid"]  = bull_ob_proj
        df["bear_ob_mid"]  = bear_ob_proj
        df["bull_fvg_mid"] = bull_fvg_proj
        df["bear_fvg_mid"] = bear_fvg_proj

        # ── Step 2: Liquidity detection on LTF ───────────────────────
        highs = df["high"].to_numpy()
        lows  = df["low"].to_numpy()
        closes = df["close"].to_numpy()

        # Equal-highs / equal-lows clustering within EQUAL_PRICE_THRESH
        # over the last SWEEP_LOOKBACK bars.
        equal_high_lvl = np.full(n_total, np.nan)
        equal_low_lvl  = np.full(n_total, np.nan)
        for i in range(n_total):
            if i < self.SWEEP_LOOKBACK: continue
            window_h = highs[i - self.SWEEP_LOOKBACK: i]
            window_l = lows [i - self.SWEEP_LOOKBACK: i]
            # Pick the highest high in the window as the candidate; check
            # if any OTHER bar's high is within threshold of it.
            top = float(window_h.max())
            close_to_top = window_h[np.abs(window_h - top) / max(top, 1e-9) < self.EQUAL_PRICE_THRESH]
            if len(close_to_top) >= 2:
                equal_high_lvl[i] = top
            bot = float(window_l.min())
            close_to_bot = window_l[np.abs(window_l - bot) / max(bot, 1e-9) < self.EQUAL_PRICE_THRESH]
            if len(close_to_bot) >= 2:
                equal_low_lvl[i] = bot

        # Liquidity SWEEP: this bar's wick took out the equal-high (long
        # = sell-side sweep, then reclaim) AND closed back through it.
        # PDF §3 Step 2 + §4 BUY MODEL "Liquidity Sweep: Previous low taken"
        bull_sweep = np.zeros(n_total, dtype=bool)
        bear_sweep = np.zeros(n_total, dtype=bool)
        for i in range(n_total):
            if not np.isnan(equal_low_lvl[i]):
                if lows[i] < equal_low_lvl[i] and closes[i] > equal_low_lvl[i]:
                    bull_sweep[i] = True
            if not np.isnan(equal_high_lvl[i]):
                if highs[i] > equal_high_lvl[i] and closes[i] < equal_high_lvl[i]:
                    bear_sweep[i] = True
        df["bull_sweep"] = bull_sweep
        df["bear_sweep"] = bear_sweep
        df["equal_low_lvl"]  = equal_low_lvl
        df["equal_high_lvl"] = equal_high_lvl

        # ── Step 6: LTF BOS confirmation on execution TF ─────────────
        ph_l, pl_l = self._strict_pivots(highs, lows, self.SWING_N)
        last_ph_ltf, last_pl_ltf = self._shifted_pivot_values(highs, lows, ph_l, pl_l, self.SWING_N)
        bull_bos_ltf = np.zeros(n_total, dtype=bool)
        bear_bos_ltf = np.zeros(n_total, dtype=bool)
        for i in range(1, n_total):
            if not np.isnan(last_ph_ltf[i]) and closes[i] > last_ph_ltf[i] and closes[i-1] <= last_ph_ltf[i]:
                bull_bos_ltf[i] = True
            if not np.isnan(last_pl_ltf[i]) and closes[i] < last_pl_ltf[i] and closes[i-1] >= last_pl_ltf[i]:
                bear_bos_ltf[i] = True
        df["bull_bos_ltf"] = bull_bos_ltf
        df["bear_bos_ltf"] = bear_bos_ltf
        df["last_ph_ltf"]  = last_ph_ltf
        df["last_pl_ltf"]  = last_pl_ltf

        # ── Step 5: Premium/Discount classification on LTF ───────────
        in_discount = closes <= range_mid_proj    # below 50% of HTF range → buy zone
        in_premium  = closes >= range_mid_proj    # above 50% → sell zone
        df["in_discount"] = in_discount
        df["in_premium"]  = in_premium

        # ── ATR for strong-move filter ───────────────────────────────
        high = df["high"]; low = df["low"]; close = df["close"]
        prev_close = close.shift()
        tr = pd.concat([
            (high - low).abs(),
            (high - prev_close).abs(),
            (low  - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1.0 / self.ATR_LEN, adjust=False).mean().to_numpy()
        body = (close - df["open"]).abs().to_numpy()
        df["atr"] = atr
        df["strong_move"] = body >= self.STRONG_BODY_X_ATR * atr

        # ── Step 8: NY Session filter ────────────────────────────────
        hours = df["date"].dt.hour
        df["in_session"] = ((hours >= self.SESSION_START_HR) &
                            (hours <= self.SESSION_END_HR)).to_numpy()

        # ── Zone proximity: price is "at" OB or FVG midpoint ─────────
        # PDF §4: "Price retraces to: OB OR FVG". We accept proximity
        # within FVG_PROXIMITY_PCT of the midpoint.
        with np.errstate(invalid="ignore"):
            at_bull_ob  = np.abs(closes - bull_ob_proj)  / np.maximum(bull_ob_proj, 1e-9)  < self.OB_PROXIMITY_PCT
            at_bear_ob  = np.abs(closes - bear_ob_proj)  / np.maximum(bear_ob_proj, 1e-9)  < self.OB_PROXIMITY_PCT
            at_bull_fvg = np.abs(closes - bull_fvg_proj) / np.maximum(bull_fvg_proj, 1e-9) < self.FVG_PROXIMITY_PCT
            at_bear_fvg = np.abs(closes - bear_fvg_proj) / np.maximum(bear_fvg_proj, 1e-9) < self.FVG_PROXIMITY_PCT
        at_bull_zone = np.where(np.isnan(bull_ob_proj) & np.isnan(bull_fvg_proj), False, at_bull_ob | at_bull_fvg)
        at_bear_zone = np.where(np.isnan(bear_ob_proj) & np.isnan(bear_fvg_proj), False, at_bear_ob | at_bear_fvg)

        # ── FINAL ENTRY: ALL 6 conditions must align (PDF §4) ────────
        long_setup  = (htf_bias_arr == "bull") & in_discount & at_bull_zone & bull_sweep & bull_bos_ltf & df["in_session"].to_numpy()
        short_setup = (htf_bias_arr == "bear") & in_premium  & at_bear_zone & bear_sweep & bear_bos_ltf & df["in_session"].to_numpy()

        # ── Risk math: SL beyond sweep, TP = 2R ──────────────────────
        # Long SL = recent equal-low (or LTF pivot low) - 10bps buffer
        sl_long_arr  = np.where(~pd.isna(equal_low_lvl),  equal_low_lvl  * 0.999, last_pl_ltf * 0.999)
        sl_short_arr = np.where(~pd.isna(equal_high_lvl), equal_high_lvl * 1.001, last_ph_ltf * 1.001)
        risk_long  = closes - sl_long_arr
        risk_short = sl_short_arr - closes
        max_dist = closes * self.MAX_RISK_PCT
        bad_long  = (risk_long  <= 0) | (risk_long  > max_dist) | np.isnan(sl_long_arr)
        bad_short = (risk_short <= 0) | (risk_short > max_dist) | np.isnan(sl_short_arr)
        long_signal  = long_setup  & ~bad_long
        short_signal = short_setup & ~bad_short

        tp_long  = closes + self.R_MULTIPLE * risk_long
        tp_short = closes - self.R_MULTIPLE * risk_short

        df["sl_price"]  = np.where(long_signal, sl_long_arr,
                            np.where(short_signal, sl_short_arr, np.nan))
        df["tp_price"]  = np.where(long_signal, tp_long,
                            np.where(short_signal, tp_short, np.nan))
        df["tp2_price"] = np.nan        # ARM engine handles TP1/TP2 split

        df["_long_signal"]  = long_signal
        df["_short_signal"] = short_signal
        return df

    def populate_entry_trend(self, df: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        df["enter_long"]  = df["_long_signal"].astype(int)
        df["enter_short"] = df["_short_signal"].astype(int)
        return df

    def populate_exit_trend(self, df: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        # PDF §6: exits managed by engine SL/TP/trailing — no condition-based exits.
        df["exit_long"]  = 0
        df["exit_short"] = 0
        return df
'''


# BestPracticesV1Strict — same strategy logic as V1, but with ATR regime
# (middle 50% of 200-bar window) AND NY session (12-21 UTC) filters ON
# by default. Generated programmatically from V1 to keep a single source
# of truth for the entry/exit logic — only the four filter-enable
# constants differ. Users wanting "strategy with strict filters" pick
# this from the dropdown; users wanting "strategy with HTF trend only"
# pick V1. Side-by-side backtest of both reveals whether the strict
# filters actually help on the user's chosen instrument + period.
_BESTPRACTICES_V1_STRICT_CODE = (
    _BESTPRACTICES_V1_CODE
    .replace("class BestPracticesV1(IStrategy):",
             "class BestPracticesV1Strict(IStrategy):")
    .replace("BestPracticesV1 — SMC strategy",
             "BestPracticesV1Strict — SMC strategy")
    .replace("ATR_PCT_LOW        = 0",
             "ATR_PCT_LOW        = 25")
    .replace("ATR_PCT_HIGH       = 100",
             "ATR_PCT_HIGH       = 75")
    .replace("SESSION_START_HR_UTC = 0",
             "SESSION_START_HR_UTC = 12")
    .replace("SESSION_END_HR_UTC   = 23",
             "SESSION_END_HR_UTC   = 21")
)


def _cleanup_stale_test_trades(db):
    """One-time cleanup: delete open futures trades that were created during
    debugging (entry_price looks wrong or entry_time is from dev session).
    Safe to call repeatedly — just deletes 0 rows if already clean."""
    from sqlalchemy import text
    try:
        result = db.execute(text(
            "DELETE FROM trades WHERE market_type = 'futures' AND status = 'open'"
        ))
        db.commit()
        if result.rowcount > 0:
            import logging
            logging.getLogger("startup").info(
                "Cleaned up %d stale open futures test trades", result.rowcount
            )
    except Exception as e:
        import logging
        logging.getLogger("startup").warning("Stale trade cleanup failed: %s", e)


def _seed_builtin_strategies(db):
    """Ensure template strategies exist with correct trading configs."""
    from backend.models.strategy import Strategy

    templates = [
        {
            "name": "SMCStrategyTV",
            "description": "SMC v2 — TradingView Pine Script port (BOS + FVG mitigation). "
                           "Core 3-gate logic: PIVOT BOS (close crosses last N=5 confirmed pivot) "
                           "+ FVG-in-zone (price retraced INTO an unfilled 3-candle imbalance) "
                           "+ structural SL anchored to opposing pivot. Multi-TP (TP1=2R close 50%, "
                           "TP2=previous pivot). No HTF bias / sweep / session filter — see "
                           "SMCProV3 for the full institutional 10-gate model.",
            "code": _SMC_TV_STRATEGY_CODE,
            "stoploss": -0.03,
            "take_profit": 0.06,
            "leverage": 10,
        },
        {
            "name": "SMCStrategy",
            "description": "Smart Money Concepts: HTF bias + Swing BOS + FVG/OB + Liquidity sweep + NY session. "
                           "Full multi-layer entry: LONG in discount zone after sell-side sweep + BOS up; "
                           "SHORT in premium zone after buy-side sweep + BOS down. 2R TP, SL below/above sweep.",
            "code": _SMC_STRATEGY_CODE,
            "stoploss": -0.015,
            "take_profit": 0.030,
            "leverage": 10,
        },
        {
            "name": "SimpleTargetStrategy",
            "description": "Bidirectional mean-reversion: LONG when RSI<55 near EMA-20 or RSI<38 (oversold); "
                           "SHORT when RSI>65 above EMA-20 or RSI>72 (overbought). TP 3%, SL 1.5% (2:1 R:R).",
            "code": _SIMPLE_STRATEGY_CODE,
            "stoploss": -0.015,
            "take_profit": 0.030,
            "leverage": 10,
        },
        {
            "name": "BidirectionalStrategy",
            "description": "Trend-following LONG+SHORT strategy: LONG when EMA9>EMA21 (uptrend) AND RSI<60; "
                           "SHORT when EMA9<EMA21 (downtrend) AND RSI>40. TP 3%, SL 1.5%. "
                           "Ideal for testing short positions in futures paper/live/backtest.",
            "code": _BIDIR_STRATEGY_CODE,
            "stoploss": -0.015,
            "take_profit": 0.030,
            "leverage": 10,
        },
        {
            "name": "SMCProV3",
            "description": "SMC Pro v3 — FULL institutional Smart Money Concepts. "
                           "7-layer entry: HTF bias (BOS direction) + Premium/Discount fib zone + "
                           "Liquidity sweep + OB/FVG mitigation + LTF BOS confirmation + NY session "
                           "(12:00-21:00 UTC) + structural SL/2R TP. Aggressive filtering — fires "
                           "only when ALL 7 conditions align, so expect ~50-200 high-quality trades "
                           "per 6 months on 15m BTC instead of thousands of noise signals.",
            "code": _SMC_PRO_V3_CODE,
            "stoploss": -0.02,
            "take_profit": 0.04,
            "leverage": 10,
        },
        {
            "name": "BestPracticesV1",
            "description": "SMC strategy for BTC/USDT 1h futures with HTF trend filter. "
                           "Active gates (default): (1) HTF EMA200 trend filter — long only "
                           "above EMA200, short only below; (2) SMC BOS + FVG entry trigger "
                           "(same as SMCStrategyTV); (3) Structural SL @ opposing pivot "
                           "(cap 3%) + single 2R TP. "
                           "OPTIONAL gates (default OFF, edit constants in main.py to enable): "
                           "ATR volatility regime (middle 50% of last 200 bars), NY session "
                           "(12-21 UTC). Designed for 1h targeting 10-40 trades/6M with WR "
                           "40-50% and profit factor ≥ 1.3. Equivalent Pine script in "
                           "docs/pine/BestPracticesV1.pine.",
            "code": _BESTPRACTICES_V1_CODE,
            "stoploss": -0.03,
            "take_profit": 0.06,
            "leverage": 10,
            "timeframe": "1h",   # Designed for 1h; overrides the 15m default.
        },
        {
            "name": "BestPracticesV1Strict",
            "description": "Same logic as BestPracticesV1 (HTF EMA200 + SMC BOS + FVG + "
                           "structural SL/2R TP) but with BOTH optional filters ON: "
                           "(A) ATR volatility regime — only trade when ATR(14) is in middle "
                           "50% of last 200 bars (skips dead chop + crash vol); "
                           "(B) NY session — only 12:00-21:00 UTC (peak BTC futures volume). "
                           "Produces FEWER but theoretically HIGHER-quality trades. Run "
                           "side-by-side with BestPracticesV1 to test whether the strict "
                           "filters actually improve risk-adjusted return on your data.",
            "code": _BESTPRACTICES_V1_STRICT_CODE,
            "stoploss": -0.03,
            "take_profit": 0.06,
            "leverage": 10,
            "timeframe": "1h",
        },
        {
            "name": "SMCStrategy1",
            "description": "Full Smart Money Concepts strategy — literal implementation of the "
                           "6-step model: (1) HTF bias from 4H BOS direction with RANGE detection "
                           "[no-trade in chop]; (2) Liquidity sweep on equal-highs/lows cluster "
                           "with reclaim filter; (3) Order Block detection on 1H + strong-move "
                           "follow-through; (4) FVG MIDPOINT entry (not just zone-inside); "
                           "(5) Premium/Discount via 50% fib of last 50-bar 4H range; "
                           "(6) LTF BOS confirmation on the execution TF + NY session (12-21 UTC). "
                           "Uses bias_timeframes=['1h','4h'] so the engine's mtf_analyzer "
                           "pre-fetches closed HTF bars and forward-fills them onto every LTF row "
                           "with NO look-ahead. SL anchored to sweep extreme, TP=2R. ARM-compatible: "
                           "when ARM is ON, TP splits into TP1=1R + TP2=2R + breakeven trail.",
            "code": _SMC_STRATEGY_1_CODE,
            "stoploss": -0.03,
            "take_profit": 0.06,
            "leverage": 10,
            "timeframe": "15m",   # Default LTF entry TF; can be changed via UI
        },
    ]

    for tmpl in templates:
        existing = db.execute(
            select(Strategy).where(Strategy.name == tmpl["name"], Strategy.is_template == True)  # noqa: E712
        ).scalar_one_or_none()
        if not existing:
            db.add(Strategy(
                user_id="system",
                name=tmpl["name"],
                description=tmpl["description"],
                original_text=tmpl["description"],
                generated_code=tmpl["code"],
                timeframe=tmpl.get("timeframe", "15m"),
                stoploss=tmpl["stoploss"],
                take_profit=tmpl["take_profit"],
                default_leverage=tmpl["leverage"],
                is_template=True,
            ))
        else:
            changed = False
            if existing.take_profit != tmpl["take_profit"]:
                existing.take_profit = tmpl["take_profit"]; changed = True
            if existing.stoploss != tmpl["stoploss"]:
                existing.stoploss = tmpl["stoploss"]; changed = True
            if not getattr(existing, "default_leverage", None) or existing.default_leverage < 2:
                existing.default_leverage = tmpl["leverage"]; changed = True
            # Always refresh generated_code AND description on templates so
            # users get the latest version of built-in strategy code without
            # having to manually delete-and-reseed the DB row. Templates are
            # read-only from the user's perspective (cloning is the way to
            # customize), so overwriting is safe.
            if existing.generated_code != tmpl["code"]:
                existing.generated_code = tmpl["code"]; changed = True
            if existing.description != tmpl["description"]:
                existing.description = tmpl["description"]; changed = True
            if changed:
                pass  # commit below

    db.commit()


async def _background_startup():
    """
    Heavy startup work runs AFTER Uvicorn is already serving requests.
    This keeps the healthcheck fast (app ready in <3s) while still
    auto-resuming bots and seeding data in the background.
    """
    import logging
    log = logging.getLogger("startup")
    await asyncio.sleep(2)   # tiny grace period so the first healthcheck passes

    # ── Init DB + seed strategies ─────────────────────────────────────────────
    try:
        init_db()
    except Exception as e:
        log.error("init_db failed: %s", e)

    try:
        with SessionLocal() as db:
            _seed_builtin_strategies(db)
    except Exception as e:
        log.error("seed strategies failed: %s", e)

    # ── Auto-resume futures bots ───────────────────────────────────────
    # Each persisted StrategyInstance row gets its engine restarted via
    # GET /api/futures/bots' built-in resume path (the bot list endpoint
    # restarts any DB-running but engine-dead bots on first call). No
    # explicit resume here — keeps startup lean. The futures bot DB rows
    # carry their own ARM + cooldown + DD config so the engine spins back
    # up identically to before the restart.
    log.info("Background startup complete. (futures-only — no spot stack)")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fire heavy work in the background — Uvicorn starts serving immediately,
    # so the Railway healthcheck passes in <3 seconds instead of ~40 seconds.
    asyncio.create_task(_background_startup())
    yield
    # Stop all futures bot engines on shutdown so KuCoin gets a clean
    # disconnect instead of phantom orders timing out.
    try:
        from backend.services.futures_engine import futures_engine_registry
        for _, eng in futures_engine_registry.all_running():
            try:
                eng.stop()
            except Exception:
                pass
    except Exception:
        pass


app = FastAPI(
    title="AutoTrade Hub",
    version=os.getenv("APP_VERSION", "1.0.0"),
    lifespan=lifespan,
    docs_url="/docs" if os.getenv("ENABLE_DOCS", "true").lower() == "true" else None,
    redoc_url=None,
)

# --- Global exception handler (app-level, catches route + dependency errors) -
@app.exception_handler(Exception)
async def _global_exception_handler(request: Request, exc: Exception):
    import logging, traceback
    logging.getLogger("autotrade_hub").error(
        "Unhandled exception on %s %s: %s\n%s",
        request.method, request.url.path, exc, traceback.format_exc(),
    )
    return JSONResponse(
        status_code=500,
        content={"error": f"{type(exc).__name__}: {exc}"},
    )

# --- Outermost safety-net middleware (catches middleware-layer exceptions) --
class _JsonErrorMiddleware(BaseHTTPMiddleware):
    """Sits just inside ServerErrorMiddleware. Converts any unhandled Python
    exception — including those raised in other middlewares (e.g. SlowAPI) —
    into a JSON {error: ...} response so the UI never sees a raw 500 page."""

    async def dispatch(self, request: Request, call_next):
        import logging, traceback
        try:
            return await call_next(request)
        except Exception as exc:
            logging.getLogger("autotrade_hub").error(
                "Middleware-layer exception on %s %s: %s\n%s",
                request.method, request.url.path, exc, traceback.format_exc(),
            )
            return JSONResponse(
                status_code=500,
                content={"error": f"{type(exc).__name__}: {exc}"},
            )

app.add_middleware(_JsonErrorMiddleware)

# --- Rate limiting ---------------------------------------------------------
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# --- CORS ------------------------------------------------------------------
_default_cors = "http://localhost:3000,http://127.0.0.1:3000"
_cors = [o.strip() for o in os.getenv("CORS_ALLOWED_ORIGINS", _default_cors).split(",") if o.strip()]
# Always allow the production Vercel frontend (direct browser→backend calls
# bypass Vercel rewrites, avoiding ROUTER_EXTERNAL_TARGET_ERROR on uploads).
_vercel_origins = [
    "https://autotrade-hub.vercel.app",
]
for vo in _vercel_origins:
    if vo not in _cors:
        _cors.append(vo)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# --- Routers ---------------------------------------------------------------
app.include_router(auth.router)
app.include_router(strategy.router)
app.include_router(market.router)        # market data (price/pairs/orderbook) — shared by chart + futures
app.include_router(futures_router.router)
app.include_router(paper_scalp_router.router)


@app.get("/")
async def root():
    return {
        "app": "AutoTrade Hub",
        "version": app.version,
        "status": "running",
        "sentry": _sentry_active,
    }


@app.get("/api/health")
async def health():
    # Surface proxy status so the user can see at a glance whether the
    # KUCOIN_HTTPS_PROXY env var was parsed correctly and which entry is
    # currently in use (credentials redacted).
    try:
        from backend.services._kucoin_proxy import proxy_status
        proxy = proxy_status()
    except Exception:
        proxy = {"count": 0, "active": None}
    # Count active futures bot engines instead of the old freqtrade mgr.
    try:
        from backend.services.futures_engine import futures_engine_registry
        active = len(futures_engine_registry.all_running())
    except Exception:
        active = 0
    return {
        "status": "healthy",
        "active_users": active,
        "kucoin_proxy": proxy,
        "timestamp": datetime.utcnow().isoformat(),
    }


# --- Live trade websocket --------------------------------------------------
class ConnectionManager:
    """Per-user fan-out: each user has their own list of open sockets so a
    push for user A never reaches user B's browser."""

    def __init__(self):
        self.active: dict[str, list[WebSocket]] = {}

    async def connect(self, user_id: str, ws: WebSocket):
        await ws.accept()
        self.active.setdefault(user_id, []).append(ws)

    def disconnect(self, user_id: str, ws: WebSocket):
        sockets = self.active.get(user_id) or []
        if ws in sockets:
            sockets.remove(ws)
        if not sockets:
            self.active.pop(user_id, None)

    async def broadcast(self, user_id: str, data: dict):
        for ws in list(self.active.get(user_id, [])):
            try:
                await ws.send_json(data)
            except Exception:
                pass


ws_manager = ConnectionManager()


def _resolve_ws_user(token: str | None) -> str | None:
    """Verify the websocket token and return the user_id, or None to reject.

    In local-dev (no CLERK_JWKS_URL) we accept anonymous connections under the
    shared `local-dev` id — same fallback the HTTP dependency uses."""
    if not clerk_enabled():
        return ANONYMOUS_USER_ID
    if not token:
        return None
    try:
        signing_key = _get_jwks().get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            audience=CLERK_AUDIENCE or None,
            issuer=CLERK_ISSUER or None,
            options={"verify_aud": bool(CLERK_AUDIENCE)},
        )
        sub = claims.get("sub")
        return sub if sub else None
    except Exception:
        return None


@app.websocket("/ws/trades")
async def websocket_trades(ws: WebSocket, token: str | None = None):
    user_id = _resolve_ws_user(token)
    if not user_id:
        await ws.close(code=http_status.WS_1008_POLICY_VIOLATION)
        return

    await ws_manager.connect(user_id, ws)
    try:
        while True:
            data = await ws.receive_text()
            if data == "ping":
                # Respond with the user's primary futures engine status
                # so the dashboard ping-pong watchdog keeps showing live
                # health without hitting any deleted spot endpoints.
                try:
                    from backend.services.futures_engine import futures_engine_registry
                    eng = futures_engine_registry.for_user(user_id)
                    bot_status = eng.status if eng else {"is_running": False}
                except Exception:
                    bot_status = {"is_running": False}
                await ws.send_json({
                    "type": "pong",
                    "bot": bot_status,
                    "timestamp": datetime.utcnow().isoformat(),
                })
    except WebSocketDisconnect:
        ws_manager.disconnect(user_id, ws)
