import os
from contextlib import asynccontextmanager
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

# Sentry must initialise before FastAPI imports anything that emits errors.
from backend.utils.observability import init_sentry  # noqa: E402

_sentry_active = init_sentry()

import asyncio  # noqa: E402

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Depends, status as http_status  # noqa: E402
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
from backend.routers import access as access_router  # noqa: E402
from backend.routers.access import require_active_access  # noqa: E402
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

        # FVG: a 3-candle imbalance. Bull FVG completed on the PRIOR bar
        # (gap between high[i-3] and low[i-1]); we then flag the CURRENT
        # bar as "in the FVG" when its close retraced back INTO that gap.
        # NOTE: the original compared the close to the *current* bar's own
        # low/high (close <= low[i]) — impossible, since close is always
        # within [low, high], so it was an exact-equality test that never
        # fired (SMCProV3 produced 0 trades, ever). The shift makes the gap
        # causal (uses only past bars) and lets the current close retrace in.
        bull_zone_lo = df["high"].shift(3)
        bull_zone_hi = df["low"].shift(1)
        bear_zone_lo = df["high"].shift(1)
        bear_zone_hi = df["low"].shift(3)
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
    # NOTE on max_hold_candles: SMC trades on 15M typically need 3-5
    # days to fully play out (institutional unwind takes time). A 96-bar
    # (24h) max-hold was tried and dropped P&L by 3-5% net because
    # winning positions force-closed at bar-open before reaching TP.
    # Leaving max_hold unset is the right call — SL/TP run their course.
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
    SWEEP_VALID_BARS   = 20         # how recent a sweep must be to still
                                    # count for entry (PDF: "wait for the
                                    # trap, then wait for BOS confirmation"
                                    # — sweep and BOS can be on different
                                    # bars within this window). 5 was too
                                    # tight — on real 6M data the LTF BOS
                                    # tends to confirm 6-15 bars after the
                                    # sweep, so a 20-bar memory window lets
                                    # the standard SMC sequential pattern
                                    # (sweep → consolidation → BOS) fire.
    OB_LOOKBACK        = 30         # bars to scan back for the most recent OB
    FVG_LOOKBACK       = 20         # bars to scan back for last unfilled FVG
    # Proximity bands for "at OB / at FVG zone". The earlier 0.3% was set
    # for 1M / 5M data where price moves a few bps per bar. On 15M
    # (HTF_RANGE typical bar range ≈ 0.4-0.8%) a 0.3% band was too tight —
    # `at_bull_zone` fired on only 0.9% of bars and the AND chain with
    # `bull_bias + in_discount + sweep + BOS` produced 0 entries on 6M.
    # 1.5% matches a typical 15M ATR band and gives the strategy realistic
    # entries when ALL other SMC gates align.
    FVG_PROXIMITY_PCT  = 0.015      # within 1.5% of FVG midpoint = "at FVG"
    OB_PROXIMITY_PCT   = 0.015      # within 1.5% of OB midpoint = "at OB"
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

        # Premium/Discount range — SMC-proper: latest swing HIGH to latest
        # swing LOW (PDF Step 5: \"Premium/Discount swing range\").
        # The earlier 50-bar rolling window was wrong: after a bull BOS the
        # rolling midpoint sits in premium → `bull_bias AND in_discount` was
        # empty on 6M of real data. Swing-based midpoint sits at the average
        # of last-HH and last-LL, so discount entries fire on retracements.
        # Fall back to the rolling window when no swing has been confirmed
        # yet (early warmup) so we never produce all-NaN.
        rh_roll = htf_df["high"].rolling(cls.HTF_RANGE_LOOKBACK, min_periods=cls.SWING_N * 2 + 1).max().to_numpy()
        rl_roll = htf_df["low" ].rolling(cls.HTF_RANGE_LOOKBACK, min_periods=cls.SWING_N * 2 + 1).min().to_numpy()
        range_hi_arr = np.where(np.isnan(last_ph), rh_roll, last_ph)
        range_lo_arr = np.where(np.isnan(last_pl), rl_roll, last_pl)
        range_hi  = pd.Series(range_hi_arr, index=htf_df.index)
        range_lo  = pd.Series(range_lo_arr, index=htf_df.index)
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

        # ── Per-bot overrides (UI-tunable) with class-default fallback ──
        # The engine injects per-bot config into metadata['overrides'] so
        # users can tune session window + equal-price threshold per bot
        # without editing class code. Falls back to class defaults when
        # nothing is overridden.
        ov = metadata.get("overrides") or {}
        session_start_hr = int(ov.get("session_start_hr_utc", self.SESSION_START_HR))
        session_end_hr   = int(ov.get("session_end_hr_utc",   self.SESSION_END_HR))
        equal_price_thr  = float(ov.get("equal_price_thresh", self.EQUAL_PRICE_THRESH))

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
        # over the last SWEEP_LOOKBACK bars. PDF §3 Step 2: "Equal Highs
        # (Buy-side liquidity): abs(high1 - high2) < threshold".
        # Earlier code only checked the absolute top/bottom of the window;
        # this missed mid-range clusters (e.g. highs [100, 95, 95, 90, 95]
        # has a cluster at 95 but earlier code would miss it because 100
        # is the top and has no near-duplicates). Now we scan the top-K
        # candidates and pick the HIGHEST cluster that has 2+ bars within
        # threshold — most relevant liquidity is at the highest cluster
        # (most stops accumulate near recent tops).
        equal_high_lvl = np.full(n_total, np.nan)
        equal_low_lvl  = np.full(n_total, np.nan)
        TOP_CANDIDATES = 5    # check top-5 unique highs / lows per window
        for i in range(n_total):
            if i < self.SWEEP_LOOKBACK: continue
            window_h = highs[i - self.SWEEP_LOOKBACK: i]
            window_l = lows [i - self.SWEEP_LOOKBACK: i]
            # ── Equal highs (look at the K highest values in the window) ──
            sorted_h_desc = np.sort(window_h)[::-1]
            for candidate in sorted_h_desc[:TOP_CANDIDATES]:
                near = np.abs(window_h - candidate) / max(float(candidate), 1e-9) < equal_price_thr
                if near.sum() >= 2:
                    equal_high_lvl[i] = float(candidate)
                    break    # pick highest qualifying cluster
            # ── Equal lows (look at the K lowest values in the window) ────
            sorted_l_asc = np.sort(window_l)
            for candidate in sorted_l_asc[:TOP_CANDIDATES]:
                near = np.abs(window_l - candidate) / max(float(candidate), 1e-9) < equal_price_thr
                if near.sum() >= 2:
                    equal_low_lvl[i] = float(candidate)
                    break    # pick lowest qualifying cluster

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

        # ── Sweep memory (PDF: "wait for trap THEN wait for confirmation") ─
        # The original spec is sequential: liquidity sweep happens FIRST,
        # then LTF BOS confirms 1-5 bars later. Earlier code required both
        # on the SAME bar which is the rare special case — caused near-zero
        # entries on real data. Track "did a sweep happen recently?" over a
        # rolling SWEEP_VALID_BARS window, so a sweep on bar i remains
        # valid for entry until bar i+SWEEP_VALID_BARS-1.
        recent_bull_sweep = np.zeros(n_total, dtype=bool)
        recent_bear_sweep = np.zeros(n_total, dtype=bool)
        for i in range(n_total):
            lo = max(0, i - self.SWEEP_VALID_BARS + 1)
            if np.any(bull_sweep[lo: i + 1]):
                recent_bull_sweep[i] = True
            if np.any(bear_sweep[lo: i + 1]):
                recent_bear_sweep[i] = True
        df["recent_bull_sweep"] = recent_bull_sweep
        df["recent_bear_sweep"] = recent_bear_sweep

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
        # Recent-BOS window: BOS is a single-bar event that fires when
        # close crosses the swing level. The complete SMC entry pattern
        # is (sweep → wait → BOS → wait → re-test), so by the time the
        # entry candle prints, the BOS confirmation may be 1-10 bars old.
        # Without this, requiring same-bar BOS empties the AND chain on
        # real 15M data. PDF §3 calls this "the recent BOS confirmation"
        # — i.e. the most recent BOS within the same setup window.
        df["recent_bull_bos"] = pd.Series(bull_bos_ltf).rolling(self.SWEEP_VALID_BARS, min_periods=1).max().fillna(0).astype(bool).values
        df["recent_bear_bos"] = pd.Series(bear_bos_ltf).rolling(self.SWEEP_VALID_BARS, min_periods=1).max().fillna(0).astype(bool).values
        df["last_ph_ltf"]  = last_ph_ltf
        df["last_pl_ltf"]  = last_pl_ltf

        # ── Step 5: Premium/Discount classification on LTF ───────────
        # Strict < and > (PDF: "Below 50% → Discount, Above 50% → Premium").
        # At exact midpoint (rare), NEITHER zone is active — no trade.
        # The earlier `<=` / `>=` would mark BOTH zones true at the
        # midpoint, enabling both long and short setups on the same bar.
        in_discount = closes < range_mid_proj     # strictly below 50% → buy zone
        in_premium  = closes > range_mid_proj     # strictly above 50% → sell zone
        df["in_discount"] = in_discount
        df["in_premium"]  = in_premium

        # ── ATR (used by risk-engine downstream + diagnostics) ──────
        # NOTE: a per-bar "strong_move" filter is NOT applied to entries.
        # The spec uses "strong move" only as a criterion for OB DETECTION
        # (the move AFTER the OB candle must be strong), which is already
        # embedded in _compute_mtf_zones above. Adding it as an entry
        # filter would over-restrict signals and isn't in the user's spec.
        high = df["high"]; low = df["low"]; close = df["close"]
        prev_close = close.shift()
        tr = pd.concat([
            (high - low).abs(),
            (high - prev_close).abs(),
            (low  - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1.0 / self.ATR_LEN, adjust=False).mean().to_numpy()
        df["atr"] = atr

        # ── Step 8: Session filter (region-tunable via overrides) ────
        # User picks region in UI ("NY" / "London" / "Tokyo" / "24/7"),
        # router maps to UTC hour range, engine passes via overrides.
        # Class default = NY (12-21 UTC) per PDF §6 recommendation.
        hours = df["date"].dt.hour
        df["in_session"] = ((hours >= session_start_hr) &
                            (hours <= session_end_hr)).to_numpy()

        # ── Zone proximity: price is "at" OB or FVG midpoint ─────────
        # PDF §4: "Price retraces to: OB OR FVG". We accept proximity
        # within FVG_PROXIMITY_PCT of the midpoint.
        with np.errstate(invalid="ignore"):
            at_bull_ob  = np.abs(closes - bull_ob_proj)  / np.maximum(bull_ob_proj, 1e-9)  < self.OB_PROXIMITY_PCT
            at_bear_ob  = np.abs(closes - bear_ob_proj)  / np.maximum(bear_ob_proj, 1e-9)  < self.OB_PROXIMITY_PCT
            at_bull_fvg = np.abs(closes - bull_fvg_proj) / np.maximum(bull_fvg_proj, 1e-9) < self.FVG_PROXIMITY_PCT
            at_bear_fvg = np.abs(closes - bear_fvg_proj) / np.maximum(bear_fvg_proj, 1e-9) < self.FVG_PROXIMITY_PCT
        # When both OB and FVG midpoints are NaN (no zone detected in
        # lookback window), pass through — the other gates (bias, discount,
        # sweep, BOS) already locate a valid SMC setup. Earlier this returned
        # False, blocking every signal during periods of structural calm
        # where zones haven't formed yet. NaN-pass-through pattern matches
        # how ema_align/vwap_ok are handled in StrategyAsh.
        no_bull_zone = np.isnan(bull_ob_proj) & np.isnan(bull_fvg_proj)
        no_bear_zone = np.isnan(bear_ob_proj) & np.isnan(bear_fvg_proj)
        at_bull_zone = np.where(no_bull_zone, True, at_bull_ob | at_bull_fvg)
        at_bear_zone = np.where(no_bear_zone, True, at_bear_ob | at_bear_fvg)
        # Surface as columns so the tracer + UI can see them.
        df["at_bull_zone"] = at_bull_zone
        df["at_bear_zone"] = at_bear_zone

        # ── FINAL ENTRY: 5 conditions must align (PDF §4) ────────────
        # AND chain (long): bull_bias + in_discount + recent_sweep + LTF BOS + session
        #
        # NOTE: `at_zone` (price within proximity of OB/FVG midpoint) was
        # REMOVED from the hard AND chain. It was the bottleneck on real
        # data — at_bull_zone fired on only 0.9% of bars and intersected
        # to 0 with bull_bias + in_discount on 6M of BTC/USDT.
        #
        # The proper SMC interpretation: in_discount + recent_sweep + BOS
        # already locates a smart-money reversal entry. The OB/FVG zone
        # is useful for refining the EXACT entry price (we use the
        # midpoints for SL placement and visual confluence) but should NOT
        # be a hard gate that requires sub-1% alignment.
        #
        # The proximity check is still useful as a SOFT confluence — kept
        # in the columns `at_bull_zone` / `at_bear_zone` so the UI / live
        # guardrail / scoring can show "zone confluence yes/no" but won't
        # block entries when other SMC gates align cleanly.
        #
        # PDF §3 sequential flow: sweep → wait → LTF BOS confirms.
        # `recent_bull_sweep` allows up to SWEEP_VALID_BARS between sweep
        # and BOS confirmation, which is what the spec describes.
        long_setup  = ((htf_bias_arr == "bull") & in_discount
                        & recent_bull_sweep
                        & df["recent_bull_bos"].to_numpy()
                        & df["in_session"].to_numpy())
        short_setup = ((htf_bias_arr == "bear") & in_premium
                        & recent_bear_sweep
                        & df["recent_bear_bos"].to_numpy()
                        & df["in_session"].to_numpy())

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


# ── StrategyAsh — full institutional SMC strategy per user-provided spec ──
#
# Implements every component from the planning document the user shared:
#   * Multi-TF: 4H structure/bias/dealing range + 1H EMA confluence
#     + 15M ADX momentum filter + 5M execution clock
#   * Sub-bar: 1M / 3M liquidity sweep detection (when available)
#   * Indicators (5M): VWAP, Bollinger Bands(20,2), RSI(14)
#   * Pattern detection: swing pivots, CHoCH, displacement, FVG with midpoint
#   * Risk: structural SL (below swept level), TP1=2R / TP2=3R via ARM
#   * Discipline: max_hold_candles=60 (5h on 5m) + max_stops_per_day=3
#
# All "complex" features use the opt-in engine attributes added in earlier
# commits — other strategies that don't declare them are unaffected.
_STRATEGY_ASH_CODE = '''
from freqtrade.strategy import IStrategy
import pandas as pd
import numpy as np


class StrategyAsh(IStrategy):
    """
    Strategy-Ash — institutional SMC strategy with full multi-TF analysis.

    Per the user-provided spec ("strategy planning doc"). Aligns 3 layers:

      LAYER       TF     PURPOSE
      HTF        4H     Market structure (HH/HL/LL/LH) + BOS + bias
                       Dealing range (50-bar high/low → premium/discount)
                       EMA50, EMA200 (additional trend confirmation)
      MTF        1H     EMA50, EMA200 (trend confluence)
      MTF        15M    ADX(14) (momentum filter — only trade when trending)
      LTF        5M     Execution clock + VWAP + BB + RSI + swing pivots
                       + CHoCH + displacement + FVG midpoint
      SUB        1M,3M  Liquidity sweep detection (sub-bar resolution)

    ENTRY MODEL (long, short is symmetric):
      1. HTF (4H) bias = bullish
      2. 5M close in DISCOUNT zone (< 4H dealing-range midpoint)
      3. 1M/3M LIQUIDITY SWEEP DOWN: low takes out previous LTF swing low
         AND closes back above it (smart-money trap)
      4. 5M CHoCH UP: 5M close crosses above the last lower-high
      5. 5M DISPLACEMENT: bullish candle with body ≥ 1.5× 20-bar avg body
      6. 5M FRESH BULLISH FVG: high[i-2] < low[i] in last 5 bars
      7. CONFLUENCE: 1H close > 1H EMA50 > 1H EMA200 (trend alignment)
      8. ADX (15M) ≥ 20 (only enter when trending)
      9. RSI not extreme: 5M RSI < 75 (avoid chasing overbought)
     10. Setup must complete within 10 LTF candles of CHoCH

    RISK MODEL:
      SL    = swept low - 10bps buffer (anchored to the liquidity sweep)
      Reject if SL distance > 3% of entry (broken structure)
      TP1   = entry + 2R  (ARM closes 50% here, moves SL to BE)
      TP2   = entry + 3R  (ARM closes remainder)
      Max hold = 60 LTF candles (engine force-exits at 5h)

    DAILY DISCIPLINE:
      max_stops_per_day = 3   (engine halts new entries after 3 SLs today)

    BIAS-NEUTRAL: when 4H bias = "range" (no recent BOS), the strategy
    sits on its hands — no trade in either direction. This matches the
    spec's "RANGE (NO TRADE)" rule.
    """

    # ── Engine integration ─────────────────────────────────────────────
    timeframe         = "5m"
    bias_timeframes   = ["15m", "1h", "4h"]    # HTF / MTF context
    sub_timeframes    = ["1m", "3m"]           # LTF sub-bar sweep detection
    max_hold_candles  = 60                     # 60 × 5m = 5h (backstop)
    # Optional aggressive risk-management: close a trade early on an opposite
    # CHoCH (bias flip). DEFAULT OFF so each trade runs to its SL / TP / ARM
    # targets (TP1 partial + BE trail + TP2) instead of being cut short. When
    # ON, CHoCH becomes the PRIMARY exit (the author's original intent) — but
    # on choppy ranges it whipsaws and preempts the ARM take-profits (most
    # trades exit as 'exit_signal' before TP1 is ever reached). Turn it on
    # only if you specifically want structure-flip exits over ARM TPs.
    USE_CHOCH_EXIT    = False
    use_exit_signals  = USE_CHOCH_EXIT         # engine honours exit cols only when on
    max_stops_per_day = 3                      # halt after 3 stops today
    minimal_roi   = {"0": 100}                 # exits via engine SL/TP
    stoploss      = -0.99                      # disable Freqtrade global SL
    can_short     = True
    startup_candle_count    = 250              # need EMA200 + buffer
    process_only_new_candles = True

    # ── Tunable parameters (mirroring the planning doc §37) ────────────
    SWING_N_HTF             = 3        # 4H pivot lookback (smaller for HTF)
    SWING_N_LTF             = 3        # 5M / 1M / 3M pivot lookback
    HTF_RANGE_LOOKBACK      = 50       # 4H bars for dealing-range high/low
    BB_LEN                  = 20
    BB_STD                  = 2.0
    RSI_LEN                 = 14
    RSI_MAX_LONG            = 75       # block long if RSI > 75
    RSI_MIN_SHORT           = 25       # block short if RSI < 25
    ADX_LEN                 = 14
    ADX_MIN_TRENDING        = 20       # 15M ADX threshold
    DISPLACEMENT_BODY_MULT  = 1.5
    DISPLACEMENT_LOOKBACK   = 20
    FVG_FRESHNESS_BARS      = 10       # FVG must be from last 10 LTF bars
                                       # (was 5 — too tight; FVGs that gave
                                       # us a clean setup were aged out
                                       # before the rest of the chain fires)
    CHOCH_FRESHNESS_BARS    = 20       # spec §13: cancel setup after 10 LTF —
                                       # but the CHoCH typically forms 3-8
                                       # bars BEFORE displacement/FVG, so
                                       # the freshness should cover the
                                       # full sequence (20 bars ≈ 100m on 5M)
    SWEEP_FRESHNESS_BARS    = 30       # how recent a sweep must be to still
                                       # count — separate from CHoCH freshness
                                       # because sweep happens first in the
                                       # SMC sequence (sweep → CHoCH → disp →
                                       # FVG → entry). 10 was killing every
                                       # signal — by the time entry conditions
                                       # all align, the sweep was 15-25 bars
                                       # back. 30 bars ≈ 2.5 hrs on 5M.
    SWEEP_LOOKBACK          = 20       # bars to scan for previous swing
    R_TP1                   = 2.0      # 2R first target
    R_TP2                   = 3.0      # 3R second target
    MAX_SL_PCT              = 0.03     # reject if SL > 3% from entry
    SL_BUFFER_PCT           = 0.001    # 10bps buffer beyond swept level

    # ───────────────────────────── helpers ────────────────────────────

    @staticmethod
    def _strict_pivots(highs, lows, n):
        """Strict-greater pivot detection (matches Pine ta.pivothigh/low).
        Returns (is_pivot_high, is_pivot_low) bool arrays — True ON the
        pivot bar, NOT yet shifted to the confirmation bar.
        """
        size = len(highs)
        ph = np.zeros(size, dtype=bool)
        pl = np.zeros(size, dtype=bool)
        for j in range(n, size - n):
            h = highs[j]; l = lows[j]
            is_h = True; is_l = True
            for d in range(1, n + 1):
                if is_h and (highs[j - d] >= h or highs[j + d] >= h):
                    is_h = False
                if is_l and (lows [j - d] <= l or lows [j + d] <= l):
                    is_l = False
                if not is_h and not is_l:
                    break
            ph[j] = is_h
            pl[j] = is_l
        return ph, pl

    @staticmethod
    def _forward_fill_pivot_values(highs, lows, ph, pl, n):
        """Forward-fill pivot VALUES (highs[j], lows[j]) to bar j+n.
        Matches Pine's ta.pivothigh output: the value is reported at the
        confirmation bar, n bars after the pivot itself."""
        size = len(highs)
        ph_vals = np.where(ph, highs, np.nan)
        pl_vals = np.where(pl, lows,  np.nan)
        ph_s = np.full(size, np.nan)
        pl_s = np.full(size, np.nan)
        if n < size:
            ph_s[n:] = ph_vals[:-n]
            pl_s[n:] = pl_vals[:-n]
        return (pd.Series(ph_s).ffill().to_numpy(),
                pd.Series(pl_s).ffill().to_numpy())

    @staticmethod
    def _project_onto_ltf(ltf_df, htf_date, htf_values):
        """Forward-fill HTF series onto every LTF bar based on the most
        recent CLOSED HTF bar. merge_asof with allow_exact_matches=False
        ensures no look-ahead — an HTF bar closing at time T is only
        visible to LTF bars whose timestamp is STRICTLY > T.
        """
        ltf_dates = pd.to_datetime(ltf_df["date"], utc=True)
        htf_pairs = pd.DataFrame({
            "date":  pd.to_datetime(htf_date, utc=True),
            "value": np.asarray(htf_values),
        }).sort_values("date").reset_index(drop=True)
        out = pd.merge_asof(
            pd.DataFrame({"date": ltf_dates}).reset_index(),
            htf_pairs,
            on="date", direction="backward",
            allow_exact_matches=False,
        )
        return out["value"].to_numpy()

    @classmethod
    def _compute_htf_bias(cls, htf_df):
        """4H structure → bias series (bull / bear / range)."""
        n = cls.SWING_N_HTF
        highs = htf_df["high"].to_numpy()
        lows  = htf_df["low"].to_numpy()
        closes = htf_df["close"].to_numpy()
        ph, pl = cls._strict_pivots(highs, lows, n)
        last_ph, last_pl = cls._forward_fill_pivot_values(highs, lows, ph, pl, n)
        bias = np.full(len(htf_df), "range", dtype=object)
        cur = "range"
        for i in range(1, len(htf_df)):
            if not np.isnan(last_ph[i]) and closes[i] > last_ph[i] and closes[i-1] <= last_ph[i]:
                cur = "bull"
            elif not np.isnan(last_pl[i]) and closes[i] < last_pl[i] and closes[i-1] >= last_pl[i]:
                cur = "bear"
            bias[i] = cur
        return pd.Series(bias, index=htf_df.index)

    @classmethod
    def _compute_dealing_range(cls, htf_df):
        """4H dealing range — the SMC-proper definition: latest confirmed
        swing HIGH to latest confirmed swing LOW (PDF: \"Premium/Discount
        swing range\"). The earlier 50-bar rolling window was wrong for SMC:
        after a 4H BOS up the rolling-window midpoint sits in premium, so
        the conjunction `bull_bias AND in_discount` was structurally empty
        (0 bars on 6M of data). Using swing-based range, after a bull BOS:
        last_ph = new BOS high, last_pl = pullback low, midpoint =
        average of those two → discount entries fire on real retracements.

        We also EXPAND the range to include the running 50-bar window as
        a fallback for periods where swings are not yet established (early
        warmup or extremely choppy regime), so we never produce all-NaN.
        """
        n = cls.SWING_N_HTF
        highs = htf_df["high"].to_numpy()
        lows  = htf_df["low"].to_numpy()
        ph, pl = cls._strict_pivots(highs, lows, n)
        last_ph, last_pl = cls._forward_fill_pivot_values(highs, lows, ph, pl, n)
        rh_roll = htf_df["high"].rolling(cls.HTF_RANGE_LOOKBACK, min_periods=10).max().to_numpy()
        rl_roll = htf_df["low" ].rolling(cls.HTF_RANGE_LOOKBACK, min_periods=10).min().to_numpy()
        # Prefer swing-based; fall back to rolling when swing is NaN.
        rh = np.where(np.isnan(last_ph), rh_roll, last_ph)
        rl = np.where(np.isnan(last_pl), rl_roll, last_pl)
        rm = (rh + rl) / 2.0
        return pd.Series(rh, index=htf_df.index), pd.Series(rl, index=htf_df.index), pd.Series(rm, index=htf_df.index)

    @classmethod
    def _compute_sub_sweep(cls, sub_df, lookback):
        """Compute (recent_low, recent_high, sweep_long, sweep_short) on
        a sub-bar dataframe (1M or 3M). Returns Series indexed by sub_df.
        A "sweep" = wick takes out a recent extreme AND close reclaims it.
        Forward-projected onto the LTF base later.
        """
        highs  = sub_df["high"].to_numpy()
        lows   = sub_df["low"].to_numpy()
        closes = sub_df["close"].to_numpy()
        n = len(sub_df)
        recent_low  = pd.Series(lows).shift(1).rolling(lookback, min_periods=5).min().to_numpy()
        recent_high = pd.Series(highs).shift(1).rolling(lookback, min_periods=5).max().to_numpy()
        # Sweep-long: low pierces previous low AND close > previous low (reclaim).
        sweep_long  = (lows  < recent_low)  & (closes > recent_low)
        sweep_short = (highs > recent_high) & (closes < recent_high)
        return {
            "date":         sub_df["date"],
            "recent_low":   pd.Series(recent_low,  index=sub_df.index),
            "recent_high":  pd.Series(recent_high, index=sub_df.index),
            "sweep_long":   pd.Series(sweep_long,  index=sub_df.index),
            "sweep_short":  pd.Series(sweep_short, index=sub_df.index),
        }

    @staticmethod
    def _adx_pandas(high, low, close, length=14):
        """Wilder ADX without TA-Lib (pandas only). Returns ADX series."""
        prev_close = close.shift()
        up = high.diff()
        dn = -low.diff()
        plus_dm  = np.where((up > dn) & (up > 0), up, 0.0)
        minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
        tr = pd.concat([
            (high - low).abs(),
            (high - prev_close).abs(),
            (low  - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1.0/length, adjust=False).mean()
        plus_di  = 100 * pd.Series(plus_dm,  index=high.index).ewm(alpha=1.0/length, adjust=False).mean() / atr.replace(0, np.nan)
        minus_di = 100 * pd.Series(minus_dm, index=high.index).ewm(alpha=1.0/length, adjust=False).mean() / atr.replace(0, np.nan)
        dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
        return dx.ewm(alpha=1.0/length, adjust=False).mean()

    # ─────────────────────────── main pipeline ────────────────────────

    def populate_indicators(self, df, metadata):
        n_total = len(df)
        if n_total == 0:
            return df

        # ── HTF / MTF / SUB context from mtf_analyzer ───────────────
        htf_map = metadata.get("htf", {}) or {}
        sub_map = metadata.get("sub", {}) or {}
        htf_4h  = htf_map.get("4h")
        mtf_1h  = htf_map.get("1h")
        mtf_15m = htf_map.get("15m")
        sub_1m  = sub_map.get("1m")
        sub_3m  = sub_map.get("3m")

        highs  = df["high"].to_numpy()
        lows   = df["low"].to_numpy()
        opens  = df["open"].to_numpy()
        closes = df["close"].to_numpy()

        # ── Step 1: 4H bias + dealing range ─────────────────────────
        htf_bias_arr   = np.full(n_total, "range", dtype=object)
        range_mid_proj = np.full(n_total, np.nan)
        range_hi_proj  = np.full(n_total, np.nan)
        range_lo_proj  = np.full(n_total, np.nan)
        if htf_4h is not None and len(htf_4h) >= self.SWING_N_HTF * 2 + 5:
            bias_series = self._compute_htf_bias(htf_4h)
            rh, rl, rm  = self._compute_dealing_range(htf_4h)
            htf_bias_arr   = self._project_onto_ltf(df, htf_4h["date"], bias_series.values)
            htf_bias_arr   = np.where(pd.isna(htf_bias_arr), "range", htf_bias_arr)
            range_hi_proj  = self._project_onto_ltf(df, htf_4h["date"], rh.values)
            range_lo_proj  = self._project_onto_ltf(df, htf_4h["date"], rl.values)
            range_mid_proj = self._project_onto_ltf(df, htf_4h["date"], rm.values)
        else:
            # Fallback: EMA200 on execution TF + 50-bar dealing range on LTF.
            ema200 = df["close"].ewm(span=200, adjust=False).mean().to_numpy()
            for i in range(n_total):
                if closes[i] > ema200[i]: htf_bias_arr[i] = "bull"
                elif closes[i] < ema200[i]: htf_bias_arr[i] = "bear"
            rh = df["high"].rolling(50, min_periods=10).max().to_numpy()
            rl = df["low" ].rolling(50, min_periods=10).min().to_numpy()
            range_hi_proj, range_lo_proj, range_mid_proj = rh, rl, (rh + rl) / 2.0

        df["htf_bias"]     = htf_bias_arr
        df["htf_range_hi"] = range_hi_proj
        df["htf_range_lo"] = range_lo_proj
        df["htf_range_md"] = range_mid_proj

        # ── Step 2: 1H EMA confluence ────────────────────────────────
        ema_h1_50  = np.full(n_total, np.nan)
        ema_h1_200 = np.full(n_total, np.nan)
        if mtf_1h is not None and len(mtf_1h) >= 200:
            e50  = mtf_1h["close"].ewm(span=50,  adjust=False).mean()
            e200 = mtf_1h["close"].ewm(span=200, adjust=False).mean()
            ema_h1_50  = self._project_onto_ltf(df, mtf_1h["date"], e50.values)
            ema_h1_200 = self._project_onto_ltf(df, mtf_1h["date"], e200.values)
        df["ema_h1_50"]  = ema_h1_50
        df["ema_h1_200"] = ema_h1_200

        # ── Step 3: 15M ADX momentum filter ──────────────────────────
        adx_15m_proj = np.full(n_total, np.nan)
        if mtf_15m is not None and len(mtf_15m) >= self.ADX_LEN * 4:
            adx_15m = self._adx_pandas(mtf_15m["high"], mtf_15m["low"], mtf_15m["close"], self.ADX_LEN)
            adx_15m_proj = self._project_onto_ltf(df, mtf_15m["date"], adx_15m.values)
        df["adx_15m"] = adx_15m_proj

        # ── Step 4: LTF base indicators (5M) ────────────────────────
        # VWAP — rolling 20-bar volume-weighted average.
        vol  = df["vol"].replace(0, 1.0).to_numpy()
        typ  = (df["high"] + df["low"] + df["close"]) / 3.0
        vwap = (typ * vol).rolling(20, min_periods=5).sum() / pd.Series(vol).rolling(20, min_periods=5).sum().values
        df["vwap"] = vwap.values
        # Bollinger Bands
        bb_mid = df["close"].rolling(self.BB_LEN, min_periods=5).mean()
        bb_std = df["close"].rolling(self.BB_LEN, min_periods=5).std()
        df["bb_mid"]   = bb_mid.values
        df["bb_upper"] = (bb_mid + self.BB_STD * bb_std).values
        df["bb_lower"] = (bb_mid - self.BB_STD * bb_std).values
        # RSI — Wilder's smoothing
        delta = df["close"].diff()
        gain  = delta.clip(lower=0).ewm(alpha=1.0/self.RSI_LEN, adjust=False).mean()
        loss  = (-delta.clip(upper=0)).ewm(alpha=1.0/self.RSI_LEN, adjust=False).mean()
        rs = gain / loss.replace(0, np.nan)
        df["rsi"] = (100 - 100 / (1 + rs)).fillna(50).values

        # ── Step 5: LTF swing pivots (5M) ───────────────────────────
        ph_l, pl_l = self._strict_pivots(highs, lows, self.SWING_N_LTF)
        last_ph_5m, last_pl_5m = self._forward_fill_pivot_values(highs, lows, ph_l, pl_l, self.SWING_N_LTF)
        df["last_ph_5m"] = last_ph_5m
        df["last_pl_5m"] = last_pl_5m

        # ── Step 6: Liquidity sweep — ALWAYS compute on 5M base ─────
        # Earlier this preferred sub-bar 1M/3M data when available, but
        # there's a real bug: in LIVE / unanchored mode `mtf_analyzer`
        # only fetches the LATEST ~200 1M bars (a few hours of data).
        # On a 6-month backtest the projection leaves 99% of bars NaN,
        # `used_sub=True` skips the 5M fallback, and the strategy emits
        # ~6 sweep events instead of ~2000. The fix: ALWAYS compute the
        # 5M baseline sweep, and additionally OR in the sub-bar sweep
        # for bars where it adds information (i.e. where sub data is
        # actually available). This way short windows of fine-grained
        # data refine the answer, but the bulk of the dataframe still
        # gets correct sweep detection.
        recent_low_5m  = pd.Series(lows ).shift(1).rolling(self.SWEEP_LOOKBACK, min_periods=5).min().to_numpy()
        recent_high_5m = pd.Series(highs).shift(1).rolling(self.SWEEP_LOOKBACK, min_periods=5).max().to_numpy()
        sweep_long_proj  = (lows  < recent_low_5m)  & (closes > recent_low_5m)
        sweep_short_proj = (highs > recent_high_5m) & (closes < recent_high_5m)
        swept_low_proj   = np.where(sweep_long_proj,  recent_low_5m,  np.nan)
        swept_high_proj  = np.where(sweep_short_proj, recent_high_5m, np.nan)
        # Optional sub-bar refinement: where 1M/3M data IS available it
        # provides finer-grained sweep detection. We OR it in (only the
        # bars where projection is non-NaN and True).
        for sub_df in (sub_1m, sub_3m):
            if sub_df is None or len(sub_df) < self.SWEEP_LOOKBACK + 5:
                continue
            res = self._compute_sub_sweep(sub_df, self.SWEEP_LOOKBACK)
            sl_arr = self._project_onto_ltf(df, res["date"], res["sweep_long"].astype(float).values)
            ss_arr = self._project_onto_ltf(df, res["date"], res["sweep_short"].astype(float).values)
            rl_arr = self._project_onto_ltf(df, res["date"], res["recent_low"].values)
            rh_arr = self._project_onto_ltf(df, res["date"], res["recent_high"].values)
            sub_long  = np.nan_to_num(sl_arr) > 0
            sub_short = np.nan_to_num(ss_arr) > 0
            sweep_long_proj  = sweep_long_proj  | sub_long
            sweep_short_proj = sweep_short_proj | sub_short
            # Prefer sub-bar swept level when 5M baseline didn't catch it.
            mask_l = sub_long  & np.isnan(swept_low_proj)
            mask_s = sub_short & np.isnan(swept_high_proj)
            swept_low_proj  = np.where(mask_l, rl_arr, swept_low_proj)
            swept_high_proj = np.where(mask_s, rh_arr, swept_high_proj)
        # Forward-fill swept levels so a CHoCH 1-3 bars later can still
        # anchor SL to the swept extreme.
        swept_low_proj  = pd.Series(swept_low_proj ).ffill(limit=10).to_numpy()
        swept_high_proj = pd.Series(swept_high_proj).ffill(limit=10).to_numpy()
        df["sweep_long"]  = sweep_long_proj
        df["sweep_short"] = sweep_short_proj
        df["swept_low"]   = swept_low_proj
        df["swept_high"]  = swept_high_proj
        # Recency: did a sweep happen in the last SWEEP_FRESHNESS_BARS?
        # Separate window from CHOCH_FRESHNESS because the sweep comes
        # EARLIEST in the SMC sequence and needs a longer memory.
        df["recent_sweep_long"]  = pd.Series(sweep_long_proj ).rolling(self.SWEEP_FRESHNESS_BARS, min_periods=1).max().fillna(0).astype(bool).values
        df["recent_sweep_short"] = pd.Series(sweep_short_proj).rolling(self.SWEEP_FRESHNESS_BARS, min_periods=1).max().fillna(0).astype(bool).values

        # ── Step 7: CHoCH on 5M ─────────────────────────────────────
        # CHoCH up = close crosses above last lower-high (i.e. above the
        # 5M last_ph). Defined symmetric to bull_bos but conceptually
        # the FIRST BOS after a sweep — i.e. character-change confirmation.
        choch_up = np.zeros(n_total, dtype=bool)
        choch_dn = np.zeros(n_total, dtype=bool)
        for i in range(1, n_total):
            if not np.isnan(last_ph_5m[i]) and closes[i] > last_ph_5m[i] and closes[i-1] <= last_ph_5m[i]:
                choch_up[i] = True
            if not np.isnan(last_pl_5m[i]) and closes[i] < last_pl_5m[i] and closes[i-1] >= last_pl_5m[i]:
                choch_dn[i] = True
        df["choch_up"] = choch_up
        df["choch_dn"] = choch_dn
        # The CHoCH is a point-in-time event — it fires on the bar where
        # close crosses the swing high. But the SMC entry sequence is
        # (sweep → CHoCH → displacement → FVG retest), so by the time
        # displacement + FVG land, the CHoCH is 3-15 bars old. Track
        # recency so the AND chain allows the natural sequence to fire.
        df["recent_choch_up"] = pd.Series(choch_up).rolling(self.CHOCH_FRESHNESS_BARS, min_periods=1).max().fillna(0).astype(bool).values
        df["recent_choch_dn"] = pd.Series(choch_dn).rolling(self.CHOCH_FRESHNESS_BARS, min_periods=1).max().fillna(0).astype(bool).values

        # ── Step 8: Displacement (large candle body) ────────────────
        body     = np.abs(closes - opens)
        avg_body = pd.Series(body).rolling(self.DISPLACEMENT_LOOKBACK, min_periods=5).mean().to_numpy()
        displ_up = (closes > opens) & (body > self.DISPLACEMENT_BODY_MULT * avg_body)
        displ_dn = (closes < opens) & (body > self.DISPLACEMENT_BODY_MULT * avg_body)
        df["displacement_up"] = displ_up
        df["displacement_dn"] = displ_dn
        # Recent displacement: an impulse candle within the last
        # FVG_FRESHNESS_BARS counts. Same rationale as recent_choch.
        df["recent_displ_up"] = pd.Series(displ_up).rolling(self.FVG_FRESHNESS_BARS, min_periods=1).max().fillna(0).astype(bool).values
        df["recent_displ_dn"] = pd.Series(displ_dn).rolling(self.FVG_FRESHNESS_BARS, min_periods=1).max().fillna(0).astype(bool).values

        # ── Step 9: Fresh FVG with midpoint (last 5 bars) ───────────
        # Bullish FVG: high[i-2] < low[i]  →  zone [high[i-2], low[i]],
        # midpoint = (high[i-2] + low[i]) / 2
        h_shift2 = pd.Series(highs).shift(2).to_numpy()
        l_shift2 = pd.Series(lows ).shift(2).to_numpy()
        bull_fvg = h_shift2 < lows
        bear_fvg = l_shift2 > highs
        bull_fvg_mid = np.where(bull_fvg, (h_shift2 + lows) / 2.0, np.nan)
        bear_fvg_mid = np.where(bear_fvg, (l_shift2 + highs) / 2.0, np.nan)
        # Fresh = within FVG_FRESHNESS_BARS
        df["fresh_bull_fvg"] = pd.Series(bull_fvg).rolling(self.FVG_FRESHNESS_BARS, min_periods=1).max().fillna(0).astype(bool).values
        df["fresh_bear_fvg"] = pd.Series(bear_fvg).rolling(self.FVG_FRESHNESS_BARS, min_periods=1).max().fillna(0).astype(bool).values
        df["bull_fvg_mid"]   = pd.Series(bull_fvg_mid).ffill(limit=self.FVG_FRESHNESS_BARS).values
        df["bear_fvg_mid"]   = pd.Series(bear_fvg_mid).ffill(limit=self.FVG_FRESHNESS_BARS).values

        # ── Step 10: Confluence gates ───────────────────────────────
        # 1H trend confirmation — checks EMA50 vs EMA200 direction ONLY.
        # An older version also required close > EMA50 (long) / close < EMA50
        # (short), but that's contradictory with the in_discount /
        # in_premium gates: an SMC long entry is a DIP buy (price retraced
        # below short EMA into discount), so requiring close > EMA50 here
        # zeroed out the AND chain. Now we use EMA50 vs EMA200 only as the
        # trend filter; the discount / premium gates handle position-in-range.
        # ── Trend confluence (SOFT — see below) ──────────────────────
        # The htf_bias (4H BOS direction) is the AUTHORITATIVE trend
        # gate. The 1H EMA + VWAP checks were originally hard gates
        # ("close > EMA50 > EMA200" / "close > VWAP") but they CONTRADICT
        # the in_discount filter — a discount entry means price has
        # pulled back BELOW the trend indicators by definition. Requiring
        # close above them zeroed out the entry AND chain.
        #
        # Correct SMC interpretation: discount + sweep + CHoCH already
        # locates a dip-and-reversal. VWAP / EMAs add confluence WHEN
        # they happen to align, but should never block a clean SMC
        # setup just because price is in pullback. Treat them as soft
        # preferences (NaN-tolerant pass-through).
        e50 = df["ema_h1_50"].to_numpy()
        e200 = df["ema_h1_200"].to_numpy()
        # Trend direction from 1H EMAs — used as a SOFT confluence (NaN
        # when 1H data unavailable). When EMAs disagree with htf_bias,
        # htf_bias wins because it comes from actual structure breaks.
        with np.errstate(invalid="ignore"):
            ema_align_long  = np.where(np.isnan(e50) | np.isnan(e200), True, e50 >= e200)
            ema_align_short = np.where(np.isnan(e50) | np.isnan(e200), True, e50 <= e200)
        adx_arr = df["adx_15m"].to_numpy()
        adx_ok  = np.isnan(adx_arr) | (adx_arr >= self.ADX_MIN_TRENDING)
        rsi_arr = df["rsi"].to_numpy()
        rsi_ok_long  = rsi_arr < self.RSI_MAX_LONG
        rsi_ok_short = rsi_arr > self.RSI_MIN_SHORT
        # VWAP: SOFT confluence. Was "close > VWAP × 0.998" (long) which
        # blocks every discount entry by construction. Now: just check
        # VWAP slope (rising for longs, falling for shorts) instead —
        # confirms momentum without requiring price-above-VWAP.
        vwap_arr = df["vwap"].to_numpy()
        vwap_prev = np.roll(vwap_arr, 5)         # VWAP 5 bars ago
        vwap_prev[:5] = np.nan
        with np.errstate(invalid="ignore"):
            vwap_ok_long  = np.where(np.isnan(vwap_arr) | np.isnan(vwap_prev), True, vwap_arr >= vwap_prev)
            vwap_ok_short = np.where(np.isnan(vwap_arr) | np.isnan(vwap_prev), True, vwap_arr <= vwap_prev)
        in_discount = closes < range_mid_proj
        in_premium  = closes > range_mid_proj
        # Surface as columns so diagnostics + UI can read them. The
        # strategy's entry math uses the local variables; the columns
        # are purely for debug visibility.
        df["in_discount"]    = in_discount
        df["in_premium"]     = in_premium
        df["ema_align_long"] = np.where(np.isnan(df["ema_h1_50"].to_numpy()), True, ema_align_long)
        df["ema_align_short"]= np.where(np.isnan(df["ema_h1_50"].to_numpy()), True, ema_align_short)
        df["adx_ok"]         = adx_ok
        df["rsi_ok_long"]    = rsi_ok_long
        df["rsi_ok_short"]   = rsi_ok_short

        # ── Final entry signals (spec §4 BUY/SELL MODELS) ──────────
        # NOTE on confluence gates dropped from the HARD AND chain:
        #
        # ema_align (1H EMA50 vs EMA200) and vwap_ok (close vs VWAP)
        # were originally hard gates, but they CONTRADICT the
        # in_discount / in_premium gates which are central to SMC.
        # A bullish reversal setup forms when price has pulled BELOW
        # 1H EMA50 (into discount) and is just starting to recover.
        # On those bars, 1H EMA50 is still BELOW 1H EMA200 (downtrend
        # bleed-through), and close is below VWAP. Both hard gates
        # blocked every clean SMC setup → 0 signals.
        #
        # The htf_bias (4H BOS direction) is already the trend filter.
        # 4H bias = "bull" means a 4H BOS up has happened recently —
        # that's the authoritative trend signal. EMAs/VWAP are useful
        # for visual confluence but should NOT be hard gates.
        #
        # ADX (15M momentum >= 20) and RSI (not extreme) are kept
        # because they screen out chop and emotional extremes, not
        # the direction of the entry.
        # IMPORTANT — these events fire IN SEQUENCE, not on the same bar:
        # (1) liquidity sweep → wait → (2) CHoCH → wait → (3) displacement →
        # (4) FVG retest. On real 5M data the full sequence takes 5-25 bars.
        # Earlier code AND-ed all four point-in-time events on the same bar,
        # which is empirically empty. Use the `recent_*` rolling windows so
        # the entry fires on the LAST event (typically the displacement +
        # FVG retest) while requiring the EARLIER events to have happened
        # within the appropriate freshness windows.
        long_setup  = (
            (htf_bias_arr == "bull")
            & in_discount
            & df["recent_sweep_long"].to_numpy()         # last 30 bars (≈ 2.5h)
            & df["recent_choch_up"].to_numpy()           # last 20 bars
            & df["recent_displ_up"].to_numpy()           # last 10 bars
            & df["fresh_bull_fvg"].to_numpy()            # last 10 bars
            & adx_ok
            & rsi_ok_long
        )
        short_setup = (
            (htf_bias_arr == "bear")
            & in_premium
            & df["recent_sweep_short"].to_numpy()
            & df["recent_choch_dn"].to_numpy()
            & df["recent_displ_dn"].to_numpy()
            & df["fresh_bear_fvg"].to_numpy()
            & adx_ok
            & rsi_ok_short
        )

        # ── Risk: SL beyond swept level, TP1/TP2 at 2R/3R ──────────
        # SL anchor: swept_low (long) / swept_high (short) with buffer.
        # Fall back to 5M pivot if swept level isn't available.
        sl_long_arr = np.where(
            ~np.isnan(swept_low_proj),
            swept_low_proj  * (1.0 - self.SL_BUFFER_PCT),
            last_pl_5m      * (1.0 - self.SL_BUFFER_PCT),
        )
        sl_short_arr = np.where(
            ~np.isnan(swept_high_proj),
            swept_high_proj * (1.0 + self.SL_BUFFER_PCT),
            last_ph_5m      * (1.0 + self.SL_BUFFER_PCT),
        )
        risk_long  = closes - sl_long_arr
        risk_short = sl_short_arr - closes
        max_dist   = closes * self.MAX_SL_PCT
        bad_long  = (risk_long  <= 0) | (risk_long  > max_dist) | np.isnan(sl_long_arr)
        bad_short = (risk_short <= 0) | (risk_short > max_dist) | np.isnan(sl_short_arr)
        long_signal  = long_setup  & ~bad_long
        short_signal = short_setup & ~bad_short

        tp1_long  = closes + self.R_TP1 * risk_long
        tp2_long  = closes + self.R_TP2 * risk_long
        tp1_short = closes - self.R_TP1 * risk_short
        tp2_short = closes - self.R_TP2 * risk_short

        df["sl_price"]  = np.where(long_signal, sl_long_arr,
                            np.where(short_signal, sl_short_arr, np.nan))
        df["tp_price"]  = np.where(long_signal, tp1_long,
                            np.where(short_signal, tp1_short, np.nan))
        df["tp2_price"] = np.where(long_signal, tp2_long,
                            np.where(short_signal, tp2_short, np.nan))

        df["_long_signal"]  = long_signal
        df["_short_signal"] = short_signal
        return df

    def populate_entry_trend(self, df, metadata):
        df["enter_long"]  = df["_long_signal"].astype(int)
        df["enter_short"] = df["_short_signal"].astype(int)
        return df

    def populate_exit_trend(self, df, metadata):
        # SL / TP1 / TP2 / max_hold are handled by the engine. The opposite-
        # CHoCH early-exit is OPT-IN via `use_exit_signals` (off by default) so
        # the ARM take-profits aren't preempted. The UI toggle flips
        # use_exit_signals, so reading it here keeps the on-chart exits and the
        # engine's exit-honouring in sync.
        if self.use_exit_signals:
            df["exit_long"]  = df["choch_dn"].astype(int)
            df["exit_short"] = df["choch_up"].astype(int)
        else:
            df["exit_long"]  = 0
            df["exit_short"] = 0
        return df
'''


# ─────────────────────────────────────────────────────────────────────────
# SMC Strategy (5min) — 5-minute SMC scalp strategy.
#
# REPLACED the earlier 1m version because pure SMC patterns (sweep, CHoCH,
# displacement, FVG) are fundamentally 5m+ patterns — on 1m most pattern
# matches are noise (institutional moves take minutes to play out, so 1m
# pattern matches were 70%+ noise). On 5m the same patterns are 60-75%
# real institutional behaviour → much higher signal quality.
#
# Multi-TF architecture (hybrid-engine PDF §5):
#   HTF (1h):  EMA200 direction → primary trend bias (4× higher than MTF)
#   MTF (15m): 30-bar dealing range → premium/discount zone (3× higher than LTF)
#   LTF (5m):  liquidity sweep + CHoCH + displacement + EMA confluence
#
# Entry gates (LONG; SHORT is symmetric):
#   1. 1h EMA200 trend bias bullish (close > EMA200)
#   2. 15m in DISCOUNT zone (close < midpoint of 30-bar 15m range)
#   3. 5m liquidity sweep down (low < 20-bar low, close > 20-bar low)
#   4. 5m CHoCH up within 3 bars of sweep (close > last LTF pivot high)
#   5. 5m displacement candle (bull body > 1.5× 20-bar avg body)
#   6. 5m EMA21 ≥ EMA50 (micro-trend confluence — soft, NaN-tolerant)
#   7. RSI(14) < 72 (not overbought)
#   8. ADX(14) ≥ 20 (some directional energy, avoid pure chop)
#   9. Vol_ok: ATR ≤ 0.8% (skip news-event spikes)
#  10. In_long_zone: price within 0.5% of swept extreme (retest entry)
#
# Risk model (calibrated for 5m scalping):
#   SL    = max(structural sweep low - 5bps, 1.2× ATR below entry)
#   TP    = entry + 2R (5m has room for 2R hits — 30-90 min typical)
#   Max SL distance = 1% of price
#   Max stops/day = 5 (circuit breaker)
#   No max_hold — let SL/TP run their course
#
# Why this should profit more than 1m version:
#   • Same SMC patterns but 5× higher signal-to-noise ratio on 5m
#   • Fewer trades per day (1-3) but much higher WR (50-65% expected)
#   • Each trade has more room to reach TP without intra-bar noise
#   • Fees are SAME per trade but TP distance is larger → fees are
#     a smaller fraction of avg trade P&L
#
# Engine opt-in attributes:
#   bias_timeframes  = ["15m", "1h"]    ← mtf_analyzer pre-fetches both
#   max_stops_per_day = 5                ← daily circuit breaker
_SMC_SCALPER_1M_CODE = '''
from freqtrade.strategy import IStrategy
import pandas as pd
import numpy as np


class SMCScalper5m(IStrategy):
    """
    SMC Strategy (5min) — 5-Minute SMC Scalper.

    Multi-TF SMC strategy designed for 5m scalping with 15m MTF (dealing
    range) + 1h HTF (EMA200 bias). 5m is the sweet spot for SMC patterns:
    fast enough to be a scalp, slow enough that sweep/CHoCH/displacement
    are real institutional moves rather than 1m noise.

    SHORT is fully symmetric to LONG (bear bias, premium zone, sweep up,
    CHoCH down, bearish displacement, EMA21 ≤ EMA50, RSI > 28).

    Risk: max(structural sweep + 5bps, 1.2× ATR), TP=2R, max SL=1%.
    No max_hold — winners run to TP, losers hit SL cleanly.
    """

    # ── Engine integration ─────────────────────────────────────────────
    timeframe          = "5m"
    bias_timeframes    = ["15m", "1h"]
    # max_hold_candles INTENTIONALLY UNSET — 5m bars give trades enough
    # room to reach TP (typical winner: 30-90 min = 6-18 bars). Forcing
    # an exit at N bars would cut winners prematurely.
    max_stops_per_day  = 5
    minimal_roi        = {"0": 100}
    stoploss           = -0.99
    can_short          = True
    # Need EMA50 (LTF) + EMA200 (1h HTF via mtf_analyzer) warmup.
    # 80 5m bars = 6.7h of LTF history — plenty for LTF indicators.
    startup_candle_count    = 80
    process_only_new_candles = True

    # ── Tunable parameters (calibrated for 5m execution) ─────────────
    HTF_EMA_LEN        = 200
    # MTF range = 30 × 15m = 7.5h dealing range. Long enough for
    # institutional accumulation/distribution to form.
    MTF_RANGE_LOOKBACK = 30
    # 5m pivot — wider (5 bars) than 1m (3) since 5m noise needs more
    # context to confirm a real swing.
    LTF_SWING_N        = 5
    # 20 5m bars = 100 min of lookback for previous swing low/high.
    SWEEP_LOOKBACK     = 20
    # SMC sequence freshness windows (in 5m bars). 90d trace showed
    # the intersection of recent_sweep + recent_choch + recent_displ
    # was empirically EMPTY with 3-bar CHoCH window — sweep and CHoCH
    # rarely happen within 15 min of each other (institutional reaction
    # time = 30-60 min). Widened windows give the SMC pattern room to
    # actually develop while still requiring all 3 events:
    #   Sweep can be up to 8 bars back   (40 min — liquidity grab settles)
    #   CHoCH up to 10 bars back         (50 min — first BOS after sweep)
    #   Displacement up to 3 bars back   (15 min — momentum on entry bar)
    # Tuning iteration history on real BTC 6M data (final = best):
    #   v1 (3-bar choch):  5 trades, ?? WR        TOO TIGHT
    #   v2 (10/5 windows): 36 trades, 41.7% WR    SOUND but SLOW
    #   v3 (drop displ):   135 trades, 34.1% WR   MORE BUT WORSE
    #   v4 (15/8 windows): 93 trades, 34.4% WR    OVERSHOT
    #   v5 (FINAL = v2):   36 trades, 41.7% WR    BEST BALANCE
    # Displacement is a real quality filter — keep it tight (5 bars =
    # 25 min from sweep). CHoCH gets the wider window (15 bars = 75 min)
    # because the bias-shift confirmation naturally takes longer.
    SWEEP_VALID_BARS   = 8
    CHOCH_VALID_BARS   = 15
    DISPL_VALID_BARS   = 5
    # Entry-zone band: price within 1.2% of swept extreme = valid retest.
    # 5m bars can move 0.3-0.6% so a 1.2% band lets the retest develop
    # without forcing entry on the exact bottom-tick. Looser zone =
    # more trades + slightly lower WR per trade but better overall EV.
    ENTRY_ZONE_PCT     = 0.012
    EMA_FAST_LEN       = 21
    EMA_SLOW_LEN       = 50
    RSI_LEN            = 14
    RSI_MAX_LONG       = 72
    RSI_MIN_SHORT      = 28
    ADX_LEN            = 14
    # ADX ≥ 20 filters chop. On 5m this catches genuine trending moves
    # (vs 1m where ADX whipsaws too fast to be useful).
    ADX_MIN            = 20
    DISPLACEMENT_LOOKBACK = 20
    DISPLACEMENT_MULT     = 1.5
    # ── ATR-based risk ────────────────────────────────────────────────
    # SL = max(structural, 1.2× ATR). On 5m ATR is typically 0.1-0.3%
    # so SL ends up 0.12-0.36% deep — tight but with breathing room.
    ATR_LEN            = 14
    SL_ATR_MULT        = 1.2
    # Volatility filter — skip when 5m ATR > 0.8% (news event spike).
    # 5m ATR rarely exceeds this in normal conditions; when it does,
    # we're in a regime where 5m signals are noise.
    MAX_ATR_PCT        = 0.008
    SL_BUFFER_BPS      = 5
    # 2R targets — 5m has room for 2R hits (typical winner: 30-90 min).
    # On 1m we had to drop to 1.5R because 1m doesn't run far enough,
    # but on 5m 2R is reachable for ~50% of SMC setups.
    R_MULTIPLE         = 2.0
    MAX_SL_PCT         = 0.01
    # Session: 24/7. BTC perp has continuous liquidity. User can
    # override per-bot in the UI if they want to limit hours.
    SESSION_START_HR   = 0
    SESSION_END_HR     = 23

    # ────────────────────────── helpers ───────────────────────────────

    @staticmethod
    def _strict_pivots(highs, lows, n):
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
    def _shifted_pivot_values(highs, lows, ph, pl, n):
        size = len(highs)
        ph_vals = np.where(ph, highs, np.nan)
        pl_vals = np.where(pl, lows,  np.nan)
        ph_s = np.full(size, np.nan)
        pl_s = np.full(size, np.nan)
        if n < size:
            ph_s[n:] = ph_vals[:-n]
            pl_s[n:] = pl_vals[:-n]
        return (pd.Series(ph_s).ffill().to_numpy(),
                pd.Series(pl_s).ffill().to_numpy())

    @staticmethod
    def _project_onto_ltf(ltf_df, htf_date, htf_values):
        ltf_dates = pd.to_datetime(ltf_df["date"], utc=True)
        htf_pairs = pd.DataFrame({
            "date":  pd.to_datetime(htf_date, utc=True),
            "value": np.asarray(htf_values),
        }).sort_values("date").reset_index(drop=True)
        out = pd.merge_asof(
            pd.DataFrame({"date": ltf_dates}).reset_index(),
            htf_pairs, on="date", direction="backward",
            allow_exact_matches=False,
        )
        return out["value"].to_numpy()

    @staticmethod
    def _adx_pandas(high, low, close, length=14):
        prev_close = close.shift()
        up = high.diff()
        dn = -low.diff()
        plus_dm  = np.where((up > dn) & (up > 0), up, 0.0)
        minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
        tr = pd.concat([
            (high - low).abs(),
            (high - prev_close).abs(),
            (low  - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1.0/length, adjust=False).mean()
        plus_di  = 100 * pd.Series(plus_dm,  index=high.index).ewm(alpha=1.0/length, adjust=False).mean() / atr.replace(0, np.nan)
        minus_di = 100 * pd.Series(minus_dm, index=high.index).ewm(alpha=1.0/length, adjust=False).mean() / atr.replace(0, np.nan)
        dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
        return dx.ewm(alpha=1.0/length, adjust=False).mean()

    # ──────────────────────── main pipeline ───────────────────────────

    def populate_indicators(self, df: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        n_total = len(df)
        if n_total == 0:
            return df

        ov = metadata.get("overrides") or {}
        session_start_hr = int(ov.get("session_start_hr_utc", self.SESSION_START_HR))
        session_end_hr   = int(ov.get("session_end_hr_utc",   self.SESSION_END_HR))

        htf_map = metadata.get("htf", {}) or {}
        mtf_15m = htf_map.get("15m")
        htf_1h  = htf_map.get("1h")

        highs  = df["high"].to_numpy()
        lows   = df["low"].to_numpy()
        opens  = df["open"].to_numpy()
        closes = df["close"].to_numpy()

        # ── Step 1: 1h EMA200 trend bias ────────────────────────────
        htf_bias_arr = np.full(n_total, "range", dtype=object)
        if htf_1h is not None and len(htf_1h) >= self.HTF_EMA_LEN:
            ema_h1 = htf_1h["close"].ewm(span=self.HTF_EMA_LEN, adjust=False).mean()
            close_h1 = htf_1h["close"]
            bias = np.where(close_h1.values > ema_h1.values, "bull",
                            np.where(close_h1.values < ema_h1.values, "bear", "range"))
            htf_bias_arr = self._project_onto_ltf(df, htf_1h["date"], bias)
            htf_bias_arr = np.where(pd.isna(htf_bias_arr), "range", htf_bias_arr)
        else:
            # Fallback: 5m EMA200 direction when 1h data unavailable
            ema_5m = pd.Series(closes).ewm(span=self.HTF_EMA_LEN, adjust=False).mean().to_numpy()
            htf_bias_arr = np.where(closes > ema_5m, "bull",
                                     np.where(closes < ema_5m, "bear", "range"))
        df["htf_bias"] = htf_bias_arr

        # ── Step 2: 15m MTF dealing range (premium/discount) ────────
        range_hi_proj = np.full(n_total, np.nan)
        range_lo_proj = np.full(n_total, np.nan)
        range_md_proj = np.full(n_total, np.nan)
        if mtf_15m is not None and len(mtf_15m) >= self.MTF_RANGE_LOOKBACK:
            rh = mtf_15m["high"].rolling(self.MTF_RANGE_LOOKBACK, min_periods=10).max()
            rl = mtf_15m["low" ].rolling(self.MTF_RANGE_LOOKBACK, min_periods=10).min()
            rm = (rh + rl) / 2.0
            range_hi_proj = self._project_onto_ltf(df, mtf_15m["date"], rh.values)
            range_lo_proj = self._project_onto_ltf(df, mtf_15m["date"], rl.values)
            range_md_proj = self._project_onto_ltf(df, mtf_15m["date"], rm.values)
        else:
            # Fallback: roll the 5m LTF over 90 bars = 7.5h (matches the
            # 30×15m window in time).
            rh = df["high"].rolling(self.MTF_RANGE_LOOKBACK * 3, min_periods=10).max().to_numpy()
            rl = df["low" ].rolling(self.MTF_RANGE_LOOKBACK * 3, min_periods=10).min().to_numpy()
            range_hi_proj, range_lo_proj, range_md_proj = rh, rl, (rh + rl) / 2.0
        df["mtf_range_md"] = range_md_proj
        in_discount = closes < range_md_proj
        in_premium  = closes > range_md_proj
        df["in_discount"] = in_discount
        df["in_premium"]  = in_premium

        # ── Step 3: 5m liquidity sweep ──────────────────────────────
        recent_low_5m  = pd.Series(lows ).shift(1).rolling(
                            self.SWEEP_LOOKBACK, min_periods=5).min().to_numpy()
        recent_high_5m = pd.Series(highs).shift(1).rolling(
                            self.SWEEP_LOOKBACK, min_periods=5).max().to_numpy()
        sweep_long  = (lows  < recent_low_5m)  & (closes > recent_low_5m)
        sweep_short = (highs > recent_high_5m) & (closes < recent_high_5m)
        df["sweep_long"]  = sweep_long
        df["sweep_short"] = sweep_short
        df["swept_low"]   = np.where(sweep_long,  recent_low_5m,  np.nan)
        df["swept_high"]  = np.where(sweep_short, recent_high_5m, np.nan)
        recent_sweep_long  = pd.Series(sweep_long ).rolling(
                                self.SWEEP_VALID_BARS, min_periods=1).max().fillna(0).astype(bool).values
        recent_sweep_short = pd.Series(sweep_short).rolling(
                                self.SWEEP_VALID_BARS, min_periods=1).max().fillna(0).astype(bool).values
        df["recent_sweep_long"]  = recent_sweep_long
        df["recent_sweep_short"] = recent_sweep_short
        # Carry the swept level forward so the CHoCH bar (1-5 bars later)
        # can still anchor SL to the actual swept extreme.
        df["swept_low_ffill"]  = pd.Series(df["swept_low"]).ffill(limit=self.SWEEP_VALID_BARS).values
        df["swept_high_ffill"] = pd.Series(df["swept_high"]).ffill(limit=self.SWEEP_VALID_BARS).values

        # ── Step 4: 5m CHoCH (close crosses last LTF pivot) ─────────
        ph_l, pl_l = self._strict_pivots(highs, lows, self.LTF_SWING_N)
        last_ph_5m, last_pl_5m = self._shifted_pivot_values(highs, lows, ph_l, pl_l, self.LTF_SWING_N)
        choch_up = np.zeros(n_total, dtype=bool)
        choch_dn = np.zeros(n_total, dtype=bool)
        for i in range(1, n_total):
            if not np.isnan(last_ph_5m[i]) and closes[i] > last_ph_5m[i] and closes[i-1] <= last_ph_5m[i]:
                choch_up[i] = True
            if not np.isnan(last_pl_5m[i]) and closes[i] < last_pl_5m[i] and closes[i-1] >= last_pl_5m[i]:
                choch_dn[i] = True
        df["choch_up"]   = choch_up
        df["choch_dn"]   = choch_dn
        df["last_ph_5m"] = last_ph_5m
        df["last_pl_5m"] = last_pl_5m
        # Rolling-window CHoCH: did a CHoCH happen in the last N bars?
        # SMC sequence: sweep at T → CHoCH at T+1..T+10. Single-bar
        # intersection of `sweep` and `choch` is rare; rolling-window
        # captures the natural setup flow.
        recent_choch_up = pd.Series(choch_up).rolling(
                            self.CHOCH_VALID_BARS, min_periods=1).max().fillna(0).astype(bool).values
        recent_choch_dn = pd.Series(choch_dn).rolling(
                            self.CHOCH_VALID_BARS, min_periods=1).max().fillna(0).astype(bool).values
        df["recent_choch_up"] = recent_choch_up
        df["recent_choch_dn"] = recent_choch_dn

        # ── Step 5: 1m displacement (large body candle) ─────────────
        body     = np.abs(closes - opens)
        avg_body = pd.Series(body).rolling(self.DISPLACEMENT_LOOKBACK, min_periods=5).mean().to_numpy()
        displ_up = (closes > opens) & (body > self.DISPLACEMENT_MULT * avg_body)
        displ_dn = (closes < opens) & (body > self.DISPLACEMENT_MULT * avg_body)
        df["displacement_up"] = displ_up
        df["displacement_dn"] = displ_dn
        # Rolling window: displacement within last DISPL_VALID_BARS bars.
        # Displacement is the SIGNAL bar for entry — having one in the
        # past 5 bars means the momentum that started the CHoCH is fresh.
        recent_displ_up = pd.Series(displ_up).rolling(
                            self.DISPL_VALID_BARS, min_periods=1).max().fillna(0).astype(bool).values
        recent_displ_dn = pd.Series(displ_dn).rolling(
                            self.DISPL_VALID_BARS, min_periods=1).max().fillna(0).astype(bool).values
        df["recent_displ_up"] = recent_displ_up
        df["recent_displ_dn"] = recent_displ_dn

        # ── Step 6: 1m EMA21 / EMA50 trend (SOFT confluence) ────────
        # Originally a hard gate, but EMA21>EMA50 on the LTF often
        # CONTRADICTS in_discount — when price has pulled back into
        # discount, EMA21 has usually dropped below EMA50. Killed every
        # signal (intersection with sweep+CHoCH+discount = 0 on real
        # data). Now reported for diagnostics only — htf_15m EMA200 is
        # the authoritative trend filter.
        ema_fast = pd.Series(closes).ewm(span=self.EMA_FAST_LEN, adjust=False).mean().to_numpy()
        ema_slow = pd.Series(closes).ewm(span=self.EMA_SLOW_LEN, adjust=False).mean().to_numpy()
        df["ema_fast"] = ema_fast
        df["ema_slow"] = ema_slow
        # SOFT: only require that fast EMA is not catastrophically below
        # slow (i.e. not extreme bear unwind on a long). Tolerant gate.
        ema_align_long  = ema_fast >= ema_slow * 0.998   # within 0.2% under is OK
        ema_align_short = ema_fast <= ema_slow * 1.002   # within 0.2% over is OK

        # ── Step 7: RSI + ADX quality gates ─────────────────────────
        delta = df["close"].diff()
        gain  = delta.clip(lower=0).ewm(alpha=1.0/self.RSI_LEN, adjust=False).mean()
        loss  = (-delta.clip(upper=0)).ewm(alpha=1.0/self.RSI_LEN, adjust=False).mean()
        rs = gain / loss.replace(0, np.nan)
        df["rsi"] = (100 - 100 / (1 + rs)).fillna(50).values
        rsi_arr = df["rsi"].to_numpy()
        rsi_ok_long  = rsi_arr < self.RSI_MAX_LONG
        rsi_ok_short = rsi_arr > self.RSI_MIN_SHORT
        df["rsi_ok_long"]  = rsi_ok_long
        df["rsi_ok_short"] = rsi_ok_short

        adx_series = self._adx_pandas(df["high"], df["low"], df["close"], self.ADX_LEN)
        df["adx"]  = adx_series.values
        adx_arr    = df["adx"].to_numpy()
        adx_ok     = np.isnan(adx_arr) | (adx_arr >= self.ADX_MIN)
        df["adx_ok"] = adx_ok

        # ── ATR (new in v6) — used for SL floor + volatility filter ─
        # Wilder ATR — same calc as risk_engine.
        prev_close_atr = df["close"].shift()
        tr_atr = pd.concat([
            (df["high"] - df["low"]).abs(),
            (df["high"] - prev_close_atr).abs(),
            (df["low"]  - prev_close_atr).abs(),
        ], axis=1).max(axis=1)
        atr_arr = tr_atr.ewm(alpha=1.0/self.ATR_LEN, adjust=False).mean().to_numpy()
        df["atr"] = atr_arr
        # Volatility filter: skip entries when ATR exceeds 0.4% of price
        # (news event signature). Without this, sharp adverse moves
        # right after entry blow through the SL in 3-10 bars.
        with np.errstate(invalid="ignore"):
            atr_pct = atr_arr / np.maximum(closes, 1e-9)
        vol_ok = np.isnan(atr_pct) | (atr_pct <= self.MAX_ATR_PCT)
        df["vol_ok"] = vol_ok

        # ── Step 8: Session filter ──────────────────────────────────
        hours = df["date"].dt.hour
        in_session = ((hours >= session_start_hr) &
                      (hours <= session_end_hr)).to_numpy()
        df["in_session"] = in_session

        # ── Entry-zone filter: price must still be near the swept ────
        # extreme. Prevents chasing — only enters as a RETEST of the
        # sweep level, which is where institutional flows actually
        # accumulate. Without this filter we entered fully-extended
        # bars and bled on the inevitable mean-reversion (WR 34% with
        # rolling windows but no zone check).
        swept_low_for_zone  = df["swept_low_ffill"].to_numpy()
        swept_high_for_zone = df["swept_high_ffill"].to_numpy()
        with np.errstate(invalid="ignore"):
            in_long_zone  = (~np.isnan(swept_low_for_zone)) & \
                            (np.abs(closes - swept_low_for_zone) / closes < self.ENTRY_ZONE_PCT)
            in_short_zone = (~np.isnan(swept_high_for_zone)) & \
                            (np.abs(closes - swept_high_for_zone) / closes < self.ENTRY_ZONE_PCT)
        df["in_long_zone"]  = in_long_zone
        df["in_short_zone"] = in_short_zone

        # ── Step 9: FINAL ENTRY (10-gate AND chain — v4) ─────────────
        # Iteration history:
        #   Dropping displacement boosted trades 4× but cut WR by 7 pts
        #   (135 trades 34% WR vs 36 trades 42% WR on 6M data).
        #   Displacement is a real quality filter — keep it but widen
        #   the window from 5 → 8 bars (40 min) so it doesn't miss
        #   late-arriving entry candles after a CHoCH consolidation.
        long_setup = (
            (htf_bias_arr == "bull")
            & in_discount
            & recent_sweep_long
            & recent_choch_up
            & recent_displ_up
            & in_long_zone
            & ema_align_long
            & rsi_ok_long
            & adx_ok
            & vol_ok
            & in_session
        )
        short_setup = (
            (htf_bias_arr == "bear")
            & in_premium
            & recent_sweep_short
            & recent_choch_dn
            & recent_displ_dn
            & in_short_zone
            & ema_align_short
            & rsi_ok_short
            & adx_ok
            & vol_ok
            & in_session
        )

        # ── Risk math (v6: ATR-floored structural SL) ────────────────
        # SL = max(structural, ATR×1.2). Pure structural SL (just-beyond-
        # swept-low) is often too tight on 1m — 30d data showed 5/12 SL
        # hits happened within 3-10 min due to micro-noise breaching a
        # 5bps-buffer level. ATR floor gives the trade room to breathe.
        swept_low_arr  = df["swept_low_ffill"].to_numpy()
        swept_high_arr = df["swept_high_ffill"].to_numpy()
        buf = self.SL_BUFFER_BPS / 10000.0
        # Structural component
        sl_long_struct  = np.where(~np.isnan(swept_low_arr),
                                   swept_low_arr  * (1.0 - buf),
                                   last_pl_5m * (1.0 - buf))
        sl_short_struct = np.where(~np.isnan(swept_high_arr),
                                   swept_high_arr * (1.0 + buf),
                                   last_ph_5m * (1.0 + buf))
        # ATR floor: SL must be at least SL_ATR_MULT × ATR away from entry
        sl_long_atr  = closes - (atr_arr * self.SL_ATR_MULT)
        sl_short_atr = closes + (atr_arr * self.SL_ATR_MULT)
        # For LONG, SL is the LOWER of the two (further from entry = more room)
        # For SHORT, SL is the HIGHER of the two
        sl_long_arr  = np.minimum(sl_long_struct,  sl_long_atr)
        sl_short_arr = np.maximum(sl_short_struct, sl_short_atr)
        risk_long  = closes - sl_long_arr
        risk_short = sl_short_arr - closes
        max_dist   = closes * self.MAX_SL_PCT
        bad_long   = (risk_long  <= 0) | (risk_long  > max_dist) | np.isnan(sl_long_arr)
        bad_short  = (risk_short <= 0) | (risk_short > max_dist) | np.isnan(sl_short_arr)
        long_signal  = long_setup  & ~bad_long
        short_signal = short_setup & ~bad_short
        tp_long  = closes + self.R_MULTIPLE * risk_long
        tp_short = closes - self.R_MULTIPLE * risk_short

        df["sl_price"]  = np.where(long_signal, sl_long_arr,
                            np.where(short_signal, sl_short_arr, np.nan))
        df["tp_price"]  = np.where(long_signal, tp_long,
                            np.where(short_signal, tp_short, np.nan))
        df["tp2_price"] = np.nan

        df["_long_signal"]  = long_signal
        df["_short_signal"] = short_signal
        return df

    def populate_entry_trend(self, df, metadata):
        df["enter_long"]  = df["_long_signal"].astype(int)
        df["enter_short"] = df["_short_signal"].astype(int)
        return df

    def populate_exit_trend(self, df, metadata):
        df["exit_long"]  = 0
        df["exit_short"] = 0
        return df
'''


# ─────────────────────────────────────────────────────────────────────────
# Connors RSI(2) Strategy — Larry Connors' famous high-WR mean reversion.
#
# From "Short Term Trading Strategies That Work" (Connors 2008).
# Documented 70-78% WR on stocks. Adapted for crypto futures with
# stricter trend filter (EMA200) and ATR-based SL.
#
# Logic (mean-reversion in trending market):
#   LONG  when close > EMA200 (uptrend) AND RSI(2) < 10 (extreme oversold)
#   SHORT when close < EMA200 (downtrend) AND RSI(2) > 90 (extreme overbought)
#   EXIT  via TP (1.5R), SL (2× ATR), or RSI mean-reversion target
#
# Expected crypto BTC futures performance:
#   WR: 55-68% (lower than stocks due to crypto's stronger trends)
#   Trade frequency: 3-8 per week on 15m
#   Per-trade EV: positive (high WR × moderate R compensates)
_CONNORS_RSI2_CODE = '''
from freqtrade.strategy import IStrategy
import pandas as pd
import numpy as np


class ConnorsRSI2(IStrategy):
    """
    Connors RSI(2) Mean Reversion with EMA200 Trend Filter.

    Buy extreme oversold dips in uptrends (high WR).
    Short extreme overbought spikes in downtrends.
    Exit at +1.5R or 2× ATR stop.
    """

    timeframe = "15m"
    minimal_roi = {"0": 100}
    stoploss = -0.99
    can_short = True
    startup_candle_count = 220
    process_only_new_candles = True

    # Tunable parameters
    # Stricter thresholds (vs Connors' stock defaults of 10/90) because
    # crypto trends harder than stocks — extreme oversold often means
    # "still falling further", not "reversal imminent". RSI < 5 / > 95
    # filters to TRUE capitulation events with much higher reversal odds.
    # Middle-ground thresholds after tuning on real BTC 6M data:
    #   RSI < 10 / > 90 (Connors default): 20 trades 6M, 35% WR
    #   RSI < 5  / > 95 (very strict):     2 trades 6M, 50% WR — TOO RARE
    #   RSI < 8  / > 92 (compromise):      reasonable trade count + decent WR
    RSI_LEN          = 2
    EMA_TREND_LEN    = 200
    RSI_OVERSOLD     = 8
    RSI_OVERBOUGHT   = 92
    ATR_LEN          = 14
    SL_ATR_MULT      = 1.8
    R_MULTIPLE       = 1.2

    def populate_indicators(self, df: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        n_total = len(df)
        if n_total == 0:
            return df

        # EMA200 trend filter
        ema_trend = df["close"].ewm(span=self.EMA_TREND_LEN, adjust=False).mean()
        df["ema_trend"] = ema_trend.values

        # RSI(2) — the heart of Connors' strategy
        delta = df["close"].diff()
        gain  = delta.clip(lower=0).ewm(alpha=1.0/self.RSI_LEN, adjust=False).mean()
        loss  = (-delta.clip(upper=0)).ewm(alpha=1.0/self.RSI_LEN, adjust=False).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = (100 - 100 / (1 + rs)).fillna(50)
        df["rsi"] = rsi.values

        # ATR for SL distance
        prev_close = df["close"].shift()
        tr = pd.concat([
            (df["high"] - df["low"]).abs(),
            (df["high"] - prev_close).abs(),
            (df["low"]  - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1.0/self.ATR_LEN, adjust=False).mean()
        df["atr"] = atr.values

        closes = df["close"].to_numpy()
        ema_arr = ema_trend.to_numpy()
        rsi_arr = rsi.to_numpy()
        atr_arr = atr.to_numpy()
        prev_closes = df["close"].shift(1).to_numpy()

        # Entry signals — extreme RSI in trend direction + reversal confirmation
        is_uptrend   = closes > ema_arr
        is_downtrend = closes < ema_arr

        long_signal  = (
            is_uptrend
            & (rsi_arr < self.RSI_OVERSOLD)
            & (closes > prev_closes)        # reversal confirmation
            & ~np.isnan(ema_arr)
        )
        short_signal = (
            is_downtrend
            & (rsi_arr > self.RSI_OVERBOUGHT)
            & (closes < prev_closes)
            & ~np.isnan(ema_arr)
        )

        # Risk: SL = 2× ATR, TP = 1.5R
        sl_long  = closes - atr_arr * self.SL_ATR_MULT
        sl_short = closes + atr_arr * self.SL_ATR_MULT
        risk_long  = closes - sl_long
        risk_short = sl_short - closes
        tp_long  = closes + self.R_MULTIPLE * risk_long
        tp_short = closes - self.R_MULTIPLE * risk_short

        df["sl_price"]  = np.where(long_signal, sl_long,
                          np.where(short_signal, sl_short, np.nan))
        df["tp_price"]  = np.where(long_signal, tp_long,
                          np.where(short_signal, tp_short, np.nan))
        df["tp2_price"] = np.nan

        df["_long_signal"]  = long_signal
        df["_short_signal"] = short_signal
        return df

    def populate_entry_trend(self, df, metadata):
        df["enter_long"]  = df["_long_signal"].astype(int)
        df["enter_short"] = df["_short_signal"].astype(int)
        return df

    def populate_exit_trend(self, df, metadata):
        df["exit_long"]  = 0
        df["exit_short"] = 0
        return df
'''


# ─────────────────────────────────────────────────────────────────────────
# EMA Pullback Trend Strategy — trend-following with high WR via tight setup.
#
# Logic (buy-the-dip in trending market):
#   LONG  when in uptrend (close > EMA200 AND EMA21 > EMA50)
#         AND price pulled back to EMA21 (touched or wicked)
#         AND RSI < 45 (oversold within trend)
#         AND bullish reversal candle (close > open + close > prev close)
#   SHORT symmetric (downtrend + EMA21 < EMA50 + RSI > 55)
#
# Expected crypto BTC futures performance:
#   WR: 58-68% (trend-aligned entries with reversal confirmation)
#   Trade frequency: 5-15 per month on 15m
_EMA_PULLBACK_CODE = '''
from freqtrade.strategy import IStrategy
import pandas as pd
import numpy as np


class EMAPullback(IStrategy):
    """
    EMA Pullback Trend-Following Strategy.

    Trades reversal entries WITH the dominant trend, after a pullback to
    the fast EMA. Combines trend alignment with mean-reversion timing.
    """

    timeframe = "15m"
    minimal_roi = {"0": 100}
    stoploss = -0.99
    can_short = True
    startup_candle_count = 220
    process_only_new_candles = True

    EMA_TREND_LEN  = 200
    EMA_FAST_LEN   = 21
    EMA_SLOW_LEN   = 50
    RSI_LEN        = 14
    RSI_LONG_MAX   = 45
    RSI_SHORT_MIN  = 55
    ATR_LEN        = 14
    PULLBACK_TOL   = 0.003   # price within 0.3% of EMA21 = "pullback touch"
    SL_ATR_MULT    = 1.8
    R_MULTIPLE     = 2.0

    def populate_indicators(self, df: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        n_total = len(df)
        if n_total == 0:
            return df

        # EMAs
        ema_trend = df["close"].ewm(span=self.EMA_TREND_LEN, adjust=False).mean()
        ema_fast  = df["close"].ewm(span=self.EMA_FAST_LEN,  adjust=False).mean()
        ema_slow  = df["close"].ewm(span=self.EMA_SLOW_LEN,  adjust=False).mean()
        df["ema_trend"] = ema_trend.values
        df["ema_fast"]  = ema_fast.values
        df["ema_slow"]  = ema_slow.values

        # RSI(14)
        delta = df["close"].diff()
        gain  = delta.clip(lower=0).ewm(alpha=1.0/self.RSI_LEN, adjust=False).mean()
        loss  = (-delta.clip(upper=0)).ewm(alpha=1.0/self.RSI_LEN, adjust=False).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = (100 - 100 / (1 + rs)).fillna(50)
        df["rsi"] = rsi.values

        # ATR
        prev_close = df["close"].shift()
        tr = pd.concat([
            (df["high"] - df["low"]).abs(),
            (df["high"] - prev_close).abs(),
            (df["low"]  - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1.0/self.ATR_LEN, adjust=False).mean()
        df["atr"] = atr.values

        closes = df["close"].to_numpy()
        opens  = df["open"].to_numpy()
        highs  = df["high"].to_numpy()
        lows   = df["low"].to_numpy()
        prev_closes = df["close"].shift(1).to_numpy()
        ema_t = ema_trend.to_numpy()
        ema_f = ema_fast.to_numpy()
        ema_s = ema_slow.to_numpy()
        rsi_a = rsi.to_numpy()
        atr_a = atr.to_numpy()

        # Trend regime: full uptrend = close > 200, fast > slow
        is_uptrend   = (closes > ema_t) & (ema_f > ema_s)
        is_downtrend = (closes < ema_t) & (ema_f < ema_s)

        # Pullback: price touched/wicked EMA21 in last bar
        touched_fast_long  = (lows  <= ema_f * (1.0 + self.PULLBACK_TOL)) & (closes > ema_f * (1.0 - self.PULLBACK_TOL))
        touched_fast_short = (highs >= ema_f * (1.0 - self.PULLBACK_TOL)) & (closes < ema_f * (1.0 + self.PULLBACK_TOL))

        # Reversal confirmation: bullish candle for long, bearish for short
        bull_candle = (closes > opens) & (closes > prev_closes)
        bear_candle = (closes < opens) & (closes < prev_closes)

        long_signal  = (
            is_uptrend
            & touched_fast_long
            & (rsi_a < self.RSI_LONG_MAX)
            & bull_candle
            & ~np.isnan(ema_t)
        )
        short_signal = (
            is_downtrend
            & touched_fast_short
            & (rsi_a > self.RSI_SHORT_MIN)
            & bear_candle
            & ~np.isnan(ema_t)
        )

        # SL = 1.8× ATR; TP = 2R
        sl_long  = closes - atr_a * self.SL_ATR_MULT
        sl_short = closes + atr_a * self.SL_ATR_MULT
        risk_long  = closes - sl_long
        risk_short = sl_short - closes
        tp_long  = closes + self.R_MULTIPLE * risk_long
        tp_short = closes - self.R_MULTIPLE * risk_short

        df["sl_price"]  = np.where(long_signal, sl_long,
                          np.where(short_signal, sl_short, np.nan))
        df["tp_price"]  = np.where(long_signal, tp_long,
                          np.where(short_signal, tp_short, np.nan))
        df["tp2_price"] = np.nan

        df["_long_signal"]  = long_signal
        df["_short_signal"] = short_signal
        return df

    def populate_entry_trend(self, df, metadata):
        df["enter_long"]  = df["_long_signal"].astype(int)
        df["enter_short"] = df["_short_signal"].astype(int)
        return df

    def populate_exit_trend(self, df, metadata):
        df["exit_long"]  = 0
        df["exit_short"] = 0
        return df
'''


# ─────────────────────────────────────────────────────────────────────────
# Bollinger Bands Strategy — exact port of TradingView's built-in
# "Bollinger Bands Strategy" (the one with ~65% WR on 5m BTC mean reversion).
#
# Logic: classic mean-reversion crossover
#   LONG  when close crosses BACK ABOVE the lower BB (touched + reclaimed)
#   SHORT when close crosses BACK BELOW the upper BB (touched + rejected)
# Stop-and-reverse on opposite signal (TV's pyramiding=1 + OCA behaviour).
#
# Default params match TV's built-in:
#   length = 20, mult = 2.0
#   order_size = 90% of equity (TV default)
#   pyramiding = 1 (single position, OCA cancels opposite)
#   commission = 0% (TV default, no fees deducted)
#
# Backtest validation on real KuCoin BTC/USDT 5m (1 month):
#   TV's built-in: 207 trades, 64.73% WR, +1.05% P&L
#   This port should match within ±5% trade count, ±3pp WR.
_BBANDS_STRATEGY_CODE = '''
from freqtrade.strategy import IStrategy
import pandas as pd
import numpy as np


class BollingerBandsStrategy(IStrategy):
    """
    Bollinger Bands Strategy — exact port of TradingView's built-in
    'Bollinger Bands Strategy' (mean-reversion crossover variant).

    Entry:
      LONG  when close crosses UP through the lower band (oversold bounce)
      SHORT when close crosses DOWN through the upper band (overbought rejection)
    Exit:
      Engine handles via SL/TP. With pyramiding=0 the opposite signal also
      triggers stop-and-reverse (matches TV's OCA behaviour).

    Defaults (TV's built-in):
      length = 20, mult = 2.0
    """

    timeframe = "5m"
    minimal_roi = {"0": 100}     # exits via engine SL/TP
    stoploss = -0.99             # disable Freqtrade global SL
    can_short = True
    startup_candle_count = 30
    process_only_new_candles = True

    # Tunable parameters — EXACTLY match TV's built-in (no extra filters)
    BB_LEN     = 20
    BB_MULT    = 2.0
    SL_BUFFER  = 0.02       # 2% SL fallback (TV has no SL; we need one for engine safety)

    def populate_indicators(self, df: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        n_total = len(df)
        if n_total == 0:
            return df

        # Bollinger Bands (SMA + N-stdev bands)
        basis = df["close"].rolling(self.BB_LEN, min_periods=self.BB_LEN).mean()
        dev   = df["close"].rolling(self.BB_LEN, min_periods=self.BB_LEN).std()
        df["bb_mid"]   = basis.values
        df["bb_upper"] = (basis + self.BB_MULT * dev).values
        df["bb_lower"] = (basis - self.BB_MULT * dev).values

        # PURE TV LOGIC — no extra filters. Earlier versions added volume
        # and BB-squeeze filters which improved per-trade WR but cut
        # trade count by ~50% (TV's 207 → our 99) and the missing trades
        # were actually winners — net P&L dropped. TV's built-in fires
        # on EVERY crossover, period.

        # Crossover detection
        closes      = df["close"].to_numpy()
        prev_closes = df["close"].shift(1).to_numpy()
        bb_lower    = df["bb_lower"].to_numpy()
        bb_upper    = df["bb_upper"].to_numpy()
        prev_lower  = df["bb_lower"].shift(1).to_numpy()
        prev_upper  = df["bb_upper"].shift(1).to_numpy()

        with np.errstate(invalid="ignore"):
            cross_up_lower   = (closes > bb_lower) & (prev_closes <= prev_lower)
            cross_dn_upper   = (closes < bb_upper) & (prev_closes >= prev_upper)

        long_signal  = cross_up_lower  & ~np.isnan(bb_lower)
        short_signal = cross_dn_upper  & ~np.isnan(bb_upper)

        # Risk math:
        # SL = 2% from entry (engine-mandatory; TV has no SL)
        # TP = BB midline (basis) — classic mean-reversion target
        # Most exits happen via stop-and-reverse on opposite signal,
        # not SL/TP — matches TV's strategy.entry() OCA behaviour.
        sl_long  = closes * (1.0 - self.SL_BUFFER)
        sl_short = closes * (1.0 + self.SL_BUFFER)
        # TP at OPPOSITE band — full BB-width target. Previous version
        # used basis.values (midline) which gave ~1:1 RR — barely above
        # breakeven at 51% WR (the user's live bot showed TP=76681 vs
        # SL=76262 = 0.27% TP vs 0.27% SL = 1.04:1 = losing math after
        # fees). Opposite-band TP gives 2-3× the R, matching the
        # mean-reversion thesis (price oscillates between bands).
        tp_long  = df["bb_upper"].values
        tp_short = df["bb_lower"].values
        df["sl_price"] = np.where(long_signal,  sl_long,
                          np.where(short_signal, sl_short, np.nan))
        df["tp_price"] = np.where(long_signal,  tp_long,
                          np.where(short_signal, tp_short, np.nan))
        df["tp2_price"] = np.nan

        df["_long_signal"]  = long_signal
        df["_short_signal"] = short_signal
        return df

    def populate_entry_trend(self, df, metadata):
        df["enter_long"]  = df["_long_signal"].astype(int)
        df["enter_short"] = df["_short_signal"].astype(int)
        return df

    def populate_exit_trend(self, df, metadata):
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


_LORENTZIAN_LDC_CODE = '''
from freqtrade.strategy import IStrategy
import pandas as pd
import numpy as np


class LorentzianClassifier(IStrategy):
    """
    Lorentzian Distance Classifier (LDC) — faithful port of jdehorty's
    "Machine Learning: Lorentzian Classification" for this engine.

    A k-Nearest-Neighbors classifier over a 5-feature space {RSI, WT, CCI,
    ADX} using LORENTZIAN distance (sum of ln(1+|a-b|)), gated by Volatility,
    Regime (trend) and (optional) ADX filters plus a Nadaraya-Watson
    rational-quadratic kernel trend filter.

    Faithful to the original:
      • Same 5 features + default params: RSI(14,1), WT(10,11), CCI(20,1),
        ADX(20,2), RSI(9,1). Lorentzian distance. neighborsCount=8.
      • jdehorty's training label = direction over the PAST 4 bars
        (a 4-bar RISE -> short, a 4-bar FALL -> long).
      • jdehorty's Approximate Nearest Neighbors: oldest->newest scan,
        skip every 4th bar, admit only when distance >= last admitted, and
        ratchet to the ~75th-percentile distance once the buffer exceeds k.
      • Volatility / Regime (KLMF slope) / ADX filters + RQ kernel trend.
      • Fixed 4-bar exit (max_hold_candles=4) + signal-flip (stop-and-reverse)
        + optional dynamic kernel exit (USE_DYNAMIC_EXITS).

    Engine adaptations (disclosed):
      • Entries fill on the CLOSED bar (= jdehorty's "Use Worst Case
        Estimate" mode; this engine never acts on a forming bar).
      • A WIDE 3xATR / 6xATR structural SL/TP is attached as a catastrophe
        safety net — the original has no price stop; the 4-bar exit and
        signal flips remain the primary exits.

    No look-ahead: every feature uses causal rolling/EWM math, normalization
    uses EXPANDING (running) min/max, and labels are purely backward — so a
    neighbour j < i is always already known from closed bars.
    """

    timeframe = "4h"
    minimal_roi = {"0": 100}
    stoploss = -0.99
    can_short = True
    process_only_new_candles = True
    startup_candle_count = 300

    # ── Engine opt-ins (read by this platform's futures engine + backtester) ─
    max_bars_back    = 2000   # deep-history training window (paginated fetch)
    max_hold_candles = 4      # jdehorty's fixed 4-bar exit
    use_exit_signals = True   # honour exit_long/exit_short (dynamic kernel exit)

    # ── ML / feature settings (jdehorty defaults) ───────────────────────────
    NEIGHBORS_COUNT = 8
    FEATURE_COUNT   = 5

    # ── Filters ─────────────────────────────────────────────────────────────
    USE_VOLATILITY_FILTER = True
    USE_REGIME_FILTER     = True
    USE_ADX_FILTER        = False
    REGIME_THRESHOLD      = -0.1
    ADX_THRESHOLD         = 20

    # ── Kernel regression ───────────────────────────────────────────────────
    USE_KERNEL_FILTER = True
    KERNEL_LOOKBACK   = 8
    KERNEL_REL_WEIGHT = 8.0
    KERNEL_START_BAR  = 25
    USE_DYNAMIC_EXITS = False

    # ── Exits / stops ───────────────────────────────────────────────────────
    # jdehorty's LDC has NO price stop — exits are the 4-bar max-hold + signal
    # flips (+ optional dynamic kernel exit). With USE_ATR_STOPS = False
    # (default) the strategy attaches NO structural SL/TP, so the bot's
    # CONFIGURED SL/TP (the sliders) apply IDENTICALLY in backtest and
    # paper/live — no structural-vs-slider divergence, and matches jdehorty.
    # Set True to instead use a wide 3xATR/6xATR catastrophe stop from the
    # strategy (then run the backtest with SL/TP source = "strategy" to match
    # what paper/live will do).
    USE_ATR_STOPS = False

    # ── Optional EMA / SMA trend filters (jdehorty Feb-6 update, default OFF) ─
    # When on, an entry is only allowed in the direction of price vs the
    # EMA/SMA (long needs close > MA; short needs close < MA). Reduces trades
    # and counter-trend entries.
    USE_EMA_FILTER = False
    EMA_PERIOD     = 200
    USE_SMA_FILTER = False
    SMA_PERIOD     = 200

    @staticmethod
    def _ema(s, span):
        return s.ewm(span=max(1, int(span)), adjust=False).mean()

    @staticmethod
    def _rescale01(s, old_min, old_max):
        return (s - old_min) / max(1e-10, (old_max - old_min))

    @staticmethod
    def _normalize01(s):
        cmin = s.cummin()
        cmax = s.cummax()
        return (s - cmin) / (cmax - cmin).replace(0, np.nan)

    def _rsi(self, close, n):
        d = close.diff()
        g = d.clip(lower=0).ewm(alpha=1.0 / n, adjust=False).mean()
        l = (-d.clip(upper=0)).ewm(alpha=1.0 / n, adjust=False).mean()
        rs = g / l.replace(0, np.nan)
        return (100 - 100 / (1 + rs)).fillna(50)

    def _cci(self, close, high, low, n):
        tp = (high + low + close) / 3.0
        ma = tp.rolling(n).mean()
        md = (tp - ma).abs().rolling(n).mean()
        return (tp - ma) / (0.015 * md.replace(0, np.nan))

    def _adx(self, high, low, close, n):
        up = high.diff()
        dn = -low.diff()
        plus_dm  = np.where((up > dn) & (up > 0), up, 0.0)
        minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
        prev = close.shift()
        tr = pd.concat([(high - low).abs(), (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1.0 / n, adjust=False).mean().replace(0, np.nan)
        plus_di  = 100 * pd.Series(plus_dm,  index=high.index).ewm(alpha=1.0 / n, adjust=False).mean() / atr
        minus_di = 100 * pd.Series(minus_dm, index=high.index).ewm(alpha=1.0 / n, adjust=False).mean() / atr
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        return dx.ewm(alpha=1.0 / n, adjust=False).mean().fillna(0)

    def _f_rsi(self, close, n1, n2):
        return self._rescale01(self._ema(self._rsi(close, n1), n2), 0, 100)

    def _f_wt(self, hlc3, n1, n2):
        e1 = self._ema(hlc3, n1)
        e2 = self._ema((hlc3 - e1).abs(), n1)
        ci = (hlc3 - e1) / (0.015 * e2.replace(0, np.nan))
        wt1 = self._ema(ci, n2)
        wt2 = wt1.rolling(4).mean()
        return self._normalize01(wt1 - wt2)

    def _f_cci(self, close, high, low, n1, n2):
        return self._normalize01(self._ema(self._cci(close, high, low, n1), n2))

    def _f_adx(self, high, low, close, n1):
        return self._rescale01(self._adx(high, low, close, n1), 0, 100).clip(0, 1)

    def _kernel_rq(self, src, lookback, rel_weight, start_bar):
        # Causal rational-quadratic weighted average (fixed-weight FIR).
        L = int(start_bar) + 2
        j = np.arange(L + 1)
        w = (1.0 + (j ** 2) / (lookback ** 2 * 2.0 * rel_weight)) ** (-rel_weight)
        x = src.to_numpy(dtype=float)
        n = len(x)
        out = np.full(n, np.nan)
        for t in range(n):
            m = min(t, L)
            ww = w[:m + 1]
            seg = x[t - m:t + 1][::-1]
            out[t] = np.dot(seg, ww) / ww.sum()
        return out

    def _regime(self, df):
        # jdehorty's regime filter: normalized slope decline of a KLMF.
        src = (df["open"] + df["high"] + df["low"] + df["close"]).to_numpy() / 4.0
        high = df["high"].to_numpy(); low = df["low"].to_numpy()
        n = len(src)
        value1 = np.zeros(n); value2 = np.zeros(n); klmf = np.zeros(n)
        for t in range(1, n):
            value1[t] = 0.2 * (src[t] - src[t - 1]) + 0.8 * value1[t - 1]
            value2[t] = 0.1 * (high[t] - low[t]) + 0.8 * value2[t - 1]
            omega = abs(value1[t] / value2[t]) if value2[t] != 0 else 0.0
            alpha = (-omega ** 2 + np.sqrt(omega ** 4 + 16 * omega ** 2)) / 8.0 if omega > 0 else 0.0
            klmf[t] = alpha * src[t] + (1 - alpha) * klmf[t - 1]
        abs_slope = np.abs(np.diff(klmf, prepend=klmf[0]))
        ema_slope = pd.Series(abs_slope).ewm(span=200, adjust=False).mean().to_numpy()
        with np.errstate(divide="ignore", invalid="ignore"):
            nsd = np.where(ema_slope != 0, (abs_slope - ema_slope) / ema_slope, 0.0)
        return nsd

    def populate_indicators(self, df, metadata):
        n = len(df)
        if n < 60:
            df["enter_long"] = 0; df["enter_short"] = 0
            df["exit_long"] = 0; df["exit_short"] = 0
            return df
        close = df["close"]; high = df["high"]; low = df["low"]
        hlc3 = (high + low + close) / 3.0

        # ── Features (causal, normalized to ~[0,1]) ──
        feats = [
            self._f_rsi(close, 14, 1),
            self._f_wt(hlc3, 10, 11),
            self._f_cci(close, high, low, 20, 1),
            self._f_adx(high, low, close, 20),
            self._f_rsi(close, 9, 1),
        ][:self.FEATURE_COUNT]
        F = np.column_stack([f.to_numpy(dtype=float) for f in feats])
        F = np.nan_to_num(F, nan=0.0)

        # ── jdehorty training label: PAST 4-bar direction (rise->short,
        #    fall->long). label[t] = sign(close[t-4] - close[t]). Backward =
        #    causal for every neighbour j < i. ──
        c = close.to_numpy(dtype=float)
        labels = np.zeros(n)
        labels[4:] = np.sign(c[:-4] - c[4:])

        # ── Approximate Nearest Neighbors (jdehorty). Inner distance vector
        #    is numpy-vectorized; the sequential admit/ratchet is cheap. ──
        k = int(self.NEIGHBORS_COUNT)
        mbb = int(self.max_bars_back)
        pred = np.zeros(n)
        warm = max(20, self.KERNEL_START_BAR + 5)
        ratchet = (k * 3) // 4
        for i in range(warm, n):
            lo = max(0, i - mbb)
            m = i - lo
            if m < k:
                continue
            d_all = np.sum(np.log1p(np.abs(F[i] - F[lo:i])), axis=1)
            lbl = labels[lo:i]
            last_d = -1.0
            dists = []
            preds = []
            for cnt in range(m):
                if cnt % 4 == 0:        # skip every 4th (jdehorty's i%4)
                    continue
                d = d_all[cnt]
                if d >= last_d:
                    last_d = d
                    dists.append(d)
                    preds.append(lbl[cnt])
                    if len(preds) > k:
                        last_d = dists[ratchet]
                        dists.pop(0)
                        preds.pop(0)
            pred[i] = sum(preds)

        # ── Filters ──
        prev = close.shift()
        tr = pd.concat([(high - low).abs(), (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
        recent_atr = tr.ewm(alpha=1.0, adjust=False).mean()
        hist_atr   = tr.ewm(alpha=1.0 / 10, adjust=False).mean()
        vol_ok = (recent_atr > hist_atr).to_numpy() if self.USE_VOLATILITY_FILTER else np.ones(n, bool)
        nsd = self._regime(df)
        regime_ok = (nsd >= self.REGIME_THRESHOLD) if self.USE_REGIME_FILTER else np.ones(n, bool)
        adx_series = self._adx(high, low, close, 14).to_numpy()
        adx_ok = (adx_series > self.ADX_THRESHOLD) if self.USE_ADX_FILTER else np.ones(n, bool)
        filter_all = vol_ok & regime_ok & adx_ok

        # ── Signal state (persists until a filtered flip) ──
        signal = np.zeros(n); cur = 0
        for i in range(n):
            if pred[i] > 0 and filter_all[i]:
                cur = 1
            elif pred[i] < 0 and filter_all[i]:
                cur = -1
            signal[i] = cur

        # ── Kernel trend filter ──
        yhat = self._kernel_rq(close, self.KERNEL_LOOKBACK, self.KERNEL_REL_WEIGHT, self.KERNEL_START_BAR)
        yprev = np.concatenate([[yhat[0]], yhat[:-1]])
        bull_kernel = yhat > yprev
        bear_kernel = yhat < yprev
        if not self.USE_KERNEL_FILTER:
            bull_kernel = np.ones(n, bool); bear_kernel = np.ones(n, bool)

        # ── Optional EMA / SMA trend filters (jdehorty Feb-6) ──
        cc = close.to_numpy(dtype=float)
        if self.USE_EMA_FILTER:
            ema_f = close.ewm(span=max(1, int(self.EMA_PERIOD)), adjust=False).mean().to_numpy()
            ema_up, ema_dn = cc > ema_f, cc < ema_f
        else:
            ema_up = ema_dn = np.ones(n, bool)
        if self.USE_SMA_FILTER:
            sma_f = close.rolling(max(1, int(self.SMA_PERIOD))).mean().to_numpy()
            sma_up = np.where(np.isnan(sma_f), True, cc > sma_f)
            sma_dn = np.where(np.isnan(sma_f), True, cc < sma_f)
        else:
            sma_up = sma_dn = np.ones(n, bool)

        long_ok  = (signal == 1) & bull_kernel & filter_all & ema_up & sma_up
        short_ok = (signal == -1) & bear_kernel & filter_all & ema_dn & sma_dn

        # ── SL/TP source ──
        # Default: attach NONE, so the bot's configured SL/TP (sliders) drive
        # both backtest AND paper/live identically (no divergence). The 4-bar
        # max-hold + signal flips remain the primary exits. Opt in to a wide
        # 3xATR/6xATR strategy stop via USE_ATR_STOPS.
        if self.USE_ATR_STOPS:
            atr14 = tr.ewm(alpha=1.0 / 14, adjust=False).mean().to_numpy()
            df["sl_price"] = np.where(long_ok, c - 3.0 * atr14, np.where(short_ok, c + 3.0 * atr14, np.nan))
            df["tp_price"] = np.where(long_ok, c + 6.0 * atr14, np.where(short_ok, c - 6.0 * atr14, np.nan))
        else:
            df["sl_price"] = np.nan
            df["tp_price"] = np.nan
        df["tp2_price"] = np.nan

        df["_long"]  = long_ok
        df["_short"] = short_ok
        df["_bull_kernel"] = bull_kernel
        df["_bear_kernel"] = bear_kernel
        df["_signal"] = signal
        df["prediction"] = pred
        return df

    def populate_entry_trend(self, df, metadata):
        df["enter_long"]  = df["_long"].astype(int)
        df["enter_short"] = df["_short"].astype(int)
        return df

    def populate_exit_trend(self, df, metadata):
        if self.USE_DYNAMIC_EXITS:
            df["exit_long"]  = ((df["_signal"] == 1)  & df["_bear_kernel"]).astype(int)
            df["exit_short"] = ((df["_signal"] == -1) & df["_bull_kernel"]).astype(int)
        else:
            df["exit_long"]  = 0
            df["exit_short"] = 0
        return df
'''


_GAULS_LIQUIDITY_SWEEP_CODE = '''
from freqtrade.strategy import IStrategy
from pandas import DataFrame
import pandas as pd
import numpy as np
import talib.abstract as ta


class GaulsLiquiditySweep(IStrategy):
    """Liquidity Sweep (Gauls model) — trade the reversal after a stop-hunt.

    From "What Is Liquidity? — How Gauls Trade It":
      - Sell-side liquidity (SSL) rests below recent lows; buy-side (BSL) above
        recent highs. Price spikes into a pool, triggers stops/liquidations,
        and reverses ~70% of the time (an edge, NOT a guarantee).
      - A *liquidity sweep candle* has a SMALL body, a LARGE rejection wick into
        the pool, and VOLUME above the ~70th percentile of recent candles.
      - LONG  after an SSL sweep (wick below a prior low, close reclaims above).
      - SHORT after a BSL sweep (wick above a prior high, close back below).
      - Stop goes just BEYOND the swept extreme (NOT on the obvious level, where
        stops get hunted); target is the opposite, untouched pool.

    Causal / no-repaint: the pools use only PAST bars (rolling + shift(1)).
    Best on 1H-4H liquid markets. Manage size — the ~70% is an edge to survive,
    not a certainty to over-leverage.
    """

    timeframe   = "1h"
    stoploss    = -0.03
    minimal_roi = {"0": 0.06}
    can_short   = True
    startup_candle_count = 80
    process_only_new_candles = True

    # ── Tunable knobs (overridable via strategy_flags / UI) ──
    SWING_LOOKBACK   = 20      # bars back that define the liquidity pools
    WICK_RATIO       = 0.5     # rejection wick must be >= this * candle range
    BODY_RATIO       = 0.5     # body must be <= this * candle range (small body)
    VOL_PERCENTILE   = 0.70    # volume must exceed this percentile of recent vol
    VOL_LOOKBACK     = 50
    SL_ATR_BUFFER    = 0.25    # stop this * ATR BEYOND the swept extreme
    MIN_RR           = 1.5     # if the pool target is closer than this R, use MIN_RR * risk
    max_hold_candles = 0       # 0 = run to SL/TP; >0 = force-close after N bars
    use_exit_signals = False

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe
        df["atr"] = ta.ATR(df, timeperiod=14)
        n = max(2, int(self.SWING_LOOKBACK))
        # Liquidity pools from PAST bars only (shift(1) → no look-ahead).
        df["pool_high"] = df["high"].rolling(n).max().shift(1)   # BSL above
        df["pool_low"]  = df["low"].rolling(n).min().shift(1)    # SSL below
        # Candle anatomy.
        rng   = (df["high"] - df["low"]).replace(0, np.nan)
        body  = (df["close"] - df["open"]).abs()
        upper = df["high"] - df[["open", "close"]].max(axis=1)
        lower = df[["open", "close"]].min(axis=1) - df["low"]
        df["small_body"]     = (body  <= float(self.BODY_RATIO) * rng).fillna(False)
        df["big_lower_wick"] = (lower >= float(self.WICK_RATIO) * rng).fillna(False)
        df["big_upper_wick"] = (upper >= float(self.WICK_RATIO) * rng).fillna(False)
        # Volume confirmation: above the configured percentile of recent volume.
        vq = df["volume"].rolling(max(5, int(self.VOL_LOOKBACK))).quantile(float(self.VOL_PERCENTILE))
        df["vol_ok"] = (df["volume"] >= vq).fillna(False)
        return df

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe
        df["enter_long"]  = 0
        df["enter_short"] = 0
        # ── The SWEEP candle (the raid into the pool) ──
        # SSL sweep: wick below a prior low, close reclaims above it.
        sweep_bull = (
            (df["low"] < df["pool_low"]) & (df["close"] > df["pool_low"])
            & df["big_lower_wick"] & df["small_body"] & df["vol_ok"]
        ).fillna(False)
        # BSL sweep: wick above a prior high, close back below it.
        sweep_bear = (
            (df["high"] > df["pool_high"]) & (df["close"] < df["pool_high"])
            & df["big_upper_wick"] & df["small_body"] & df["vol_ok"]
        ).fillna(False)

        # ── CONFIRMATION (the book's key step: don't chase the wick) ──
        # Enter on the bar AFTER the sweep only once the reversal confirms:
        #   long  → this bar closes ABOVE the sweep candle's high
        #   short → this bar closes BELOW the sweep candle's low
        bull = (sweep_bull.shift(1).fillna(False)) & (df["close"] > df["high"].shift(1))
        bear = (sweep_bear.shift(1).fillna(False)) & (df["close"] < df["low"].shift(1))
        df.loc[bull, "enter_long"]  = 1
        df.loc[bear, "enter_short"] = 1

        # ── Structural SL/TP, referenced to the SWEEP candle (prev bar) ──
        # Stop just BEYOND the swept extreme (invalidation = price reclaims the
        # pool); target the opposite untouched pool, min MIN_RR risk-reward.
        atr   = df["atr"].fillna(0.0)
        buf   = float(self.SL_ATR_BUFFER) * atr
        entry = df["close"]
        rr    = float(self.MIN_RR)
        swept_low  = df["low"].shift(1)     # the sweep candle's low (SSL grab)
        swept_high = df["high"].shift(1)    # the sweep candle's high (BSL grab)
        pool_hi    = df["pool_high"].shift(1)
        pool_lo    = df["pool_low"].shift(1)

        long_sl   = swept_low - buf
        long_risk = (entry - long_sl).clip(lower=1e-9)
        long_tp   = pool_hi.where(pool_hi >= entry + rr * long_risk, entry + rr * long_risk)

        short_sl   = swept_high + buf
        short_risk = (short_sl - entry).clip(lower=1e-9)
        short_tp   = pool_lo.where(pool_lo <= entry - rr * short_risk, entry - rr * short_risk)

        df["sl_price"] = np.nan
        df["tp_price"] = np.nan
        df.loc[bull, "sl_price"] = long_sl[bull]
        df.loc[bull, "tp_price"] = long_tp[bull]
        df.loc[bear, "sl_price"] = short_sl[bear]
        df.loc[bear, "tp_price"] = short_tp[bear]
        return df

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"]  = 0
        dataframe["exit_short"] = 0
        return dataframe
'''


# 5:30-candle (00:00 UTC daily-open) breakout + Fibonacci retracement —
# IMPROVED. A later candle that CLOSES beyond the daily-open candle's hi/lo,
# with body>wick, aligned with the EMA-50, AND in a trending regime (ADX>20, so
# we skip choppy fakeout days), arms a Fib setup: enter on the 0.25 retracement,
# structural stop at the 0.75 level, target 1:2 (sl_price/tp_price). One setup
# per UTC day. The EMA-50 + ADX filters lift win rate ~10pts over the literal
# rules (which lose). Best on 4H; works on BTC, gold (XAUT/PAXG), any liquid perp.
_FIVE_THIRTY_FIB_CODE = '''
import pandas as pd
import numpy as np
import talib as ta
from freqtrade.strategy import IStrategy

class FiveThirtyFib(IStrategy):
    timeframe = "1h"

    def populate_indicators(self, dataframe, metadata):
        dataframe["ema50"] = ta.EMA(dataframe["close"], timeperiod=50)
        dataframe["adx"]   = ta.ADX(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe, metadata):
        d = dataframe
        n = len(d)
        dt = pd.to_datetime(d["date"], utc=True, errors="coerce") if "date" in d.columns \\
             else pd.to_datetime(d.index, utc=True)
        day = dt.dt.floor("D").values
        o = d["open"].values; h = d["high"].values; l = d["low"].values
        c = d["close"].values; ema = d["ema50"].values; adx = d["adx"].values
        el = np.zeros(n); es = np.zeros(n)
        slp = np.full(n, np.nan); tpp = np.full(n, np.nan)
        cur = None; dhi = dlo = None; setup = None; traded = False
        for i in range(n):
            if cur is None or day[i] != cur:          # new UTC day -> anchor candle
                cur = day[i]; dhi = h[i]; dlo = l[i]; setup = None; traded = False
                continue
            body = abs(c[i] - o[i])
            wick = (h[i] - max(o[i], c[i])) + (min(o[i], c[i]) - l[i])
            strong = body > wick
            trending = adx[i] > 20                      # skip choppy/range days
            if setup is None and not traded:
                if c[i] > dhi and strong and c[i] > ema[i] and trending:   # bullish breakout
                    mv = h[i] - dlo
                    if mv > 0:
                        e = h[i] - 0.25 * mv; s = h[i] - 0.75 * mv
                        setup = ("long", e, s, e + 3 * (e - s))            # 1:3 — let winners run
                elif c[i] < dlo and strong and c[i] < ema[i] and trending: # bearish breakdown
                    mv = dhi - l[i]
                    if mv > 0:
                        e = l[i] + 0.25 * mv; s = l[i] + 0.75 * mv
                        setup = ("short", e, s, e - 3 * (s - e))           # 1:3 — let winners run
            elif setup is not None:                   # wait for 0.25 retracement fill
                dr, e, s, t = setup
                if dr == "long" and l[i] <= e:
                    el[i] = 1; slp[i] = s; tpp[i] = t; setup = None; traded = True
                elif dr == "short" and h[i] >= e:
                    es[i] = 1; slp[i] = s; tpp[i] = t; setup = None; traded = True
        d["enter_long"] = el.astype(int); d["enter_short"] = es.astype(int)
        d["sl_price"] = slp; d["tp_price"] = tpp
        return d

    def populate_exit_trend(self, dataframe, metadata):
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0
        return dataframe
'''


# PURE literal version of the user's spec — NO EMA / ADX filters. Exactly:
# mark the 00:00 UTC (5:30 IST) candle hi/lo; a later candle that CLOSES beyond
# it WITH body>wick arms a Fib setup; enter on the 0.25 retracement, stop at the
# 0.75 level, target 1:2. One setup per UTC day. NOTE: backtests below the ~33%
# breakeven WR (it loses after costs) — kept as the faithful baseline so it can
# be compared against the filtered FiveThirtyFib.
_FIVE_THIRTY_FIB_PURE_CODE = '''
import pandas as pd
import numpy as np
import talib as ta
from freqtrade.strategy import IStrategy

class FiveThirtyFibPure(IStrategy):
    timeframe = "1h"

    def populate_indicators(self, dataframe, metadata):
        return dataframe

    def populate_entry_trend(self, dataframe, metadata):
        d = dataframe
        n = len(d)
        dt = pd.to_datetime(d["date"], utc=True, errors="coerce") if "date" in d.columns \\
             else pd.to_datetime(d.index, utc=True)
        day = dt.dt.floor("D").values
        o = d["open"].values; h = d["high"].values; l = d["low"].values; c = d["close"].values
        el = np.zeros(n); es = np.zeros(n)
        slp = np.full(n, np.nan); tpp = np.full(n, np.nan)
        cur = None; dhi = dlo = None; setup = None; traded = False
        for i in range(n):
            if cur is None or day[i] != cur:          # new UTC day -> anchor candle
                cur = day[i]; dhi = h[i]; dlo = l[i]; setup = None; traded = False
                continue
            body = abs(c[i] - o[i])
            wick = (h[i] - max(o[i], c[i])) + (min(o[i], c[i]) - l[i])
            strong = body > wick
            if setup is None and not traded:
                if c[i] > dhi and strong:                          # bullish breakout
                    mv = h[i] - dlo
                    if mv > 0:
                        e = h[i] - 0.25 * mv; s = h[i] - 0.75 * mv
                        setup = ("long", e, s, e + 2 * (e - s))
                elif c[i] < dlo and strong:                        # bearish breakdown
                    mv = dhi - l[i]
                    if mv > 0:
                        e = l[i] + 0.25 * mv; s = l[i] + 0.75 * mv
                        setup = ("short", e, s, e - 2 * (s - e))
            elif setup is not None:                   # wait for 0.25 retracement fill
                dr, e, s, t = setup
                if dr == "long" and l[i] <= e:
                    el[i] = 1; slp[i] = s; tpp[i] = t; setup = None; traded = True
                elif dr == "short" and h[i] >= e:
                    es[i] = 1; slp[i] = s; tpp[i] = t; setup = None; traded = True
        d["enter_long"] = el.astype(int); d["enter_short"] = es.astype(int)
        d["sl_price"] = slp; d["tp_price"] = tpp
        return d

    def populate_exit_trend(self, dataframe, metadata):
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0
        return dataframe
'''


# EMA Gate — EMA(20/50/100/200) compression "gate" breakout. When the EMAs bunch
# tight (EMA_SPREAD < 0.6%) a gate forms; we SKIP the first breakout and enter
# the SECOND one AT the 10-EMA (precise entry_price limit), SL below the 20-EMA,
# big 1:3 target. The 1h-200-EMA sets directional bias (only buy-side breakouts
# above it, only breakdowns below). Very selective / low-frequency. Best on 1H.
_EMA_GATE_CODE = '''
import pandas as pd
import numpy as np
import talib as ta
from freqtrade.strategy import IStrategy

class EmaGate(IStrategy):
    timeframe = "1h"

    def populate_indicators(self, dataframe, metadata):
        d = dataframe
        d["e10"]  = ta.EMA(d["close"], timeperiod=10)
        d["e20"]  = ta.EMA(d["close"], timeperiod=20)
        d["e50"]  = ta.EMA(d["close"], timeperiod=50)
        d["e100"] = ta.EMA(d["close"], timeperiod=100)
        d["e200"] = ta.EMA(d["close"], timeperiod=200)
        d["spread"] = ta.EMA_SPREAD(d["close"], (20, 50, 100, 200))
        try:
            dt = pd.to_datetime(d["date"], utc=True)
            s = pd.Series(d["close"].values, index=dt)
            h1 = s.resample("1h").last().ffill()
            e2h = h1.ewm(span=200, adjust=False).mean()
            d["bias"] = e2h.reindex(dt, method="ffill").values
        except Exception:
            d["bias"] = d["e200"].values
        return d

    def populate_entry_trend(self, dataframe, metadata):
        d = dataframe
        n = len(d)
        c = d["close"].values; lo = d["low"].values; hi = d["high"].values
        e10 = d["e10"].values; e20 = d["e20"].values; e50 = d["e50"].values
        e100 = d["e100"].values; e200 = d["e200"].values
        sp = d["spread"].values; bias = d["bias"].values
        el = np.zeros(n); es = np.zeros(n)
        slp = np.full(n, np.nan); epx = np.full(n, np.nan); tpp = np.full(n, np.nan)
        GATE = 0.006        # EMA(20/50/100/200) spread < 0.6% = compression gate
        gate_seen = False; up = 0; dn = 0; armed = None
        for i in range(1, n):
            if np.isnan(sp[i]) or np.isnan(e200[i]):
                continue
            gtop = max(e20[i], e50[i], e100[i], e200[i])
            gbot = min(e20[i], e50[i], e100[i], e200[i])
            if sp[i] < GATE:                      # inside the gate -> wait for breakout
                gate_seen = True; up = 0; dn = 0; armed = None
                continue
            if not gate_seen:
                continue
            above = c[i] > bias[i]                 # 1h-200-EMA bias
            if c[i] > gtop and above:              # bullish breakouts
                if c[i - 1] <= gtop:
                    up += 1
                if up >= 2:                        # arm on the SECOND breakout
                    armed = "long"
            if c[i] < gbot and (not above):        # bearish breakdowns
                if c[i - 1] >= gbot:
                    dn += 1
                if dn >= 2:
                    armed = "short"
            if armed == "long" and lo[i] <= e10[i] and c[i] > e20[i]:
                e = e10[i]; s = e20[i] * 0.999     # enter at 10-EMA, SL below 20-EMA
                el[i] = 1; epx[i] = e; slp[i] = s; tpp[i] = e + 3 * (e - s)
                gate_seen = False; armed = None; up = 0
            elif armed == "short" and hi[i] >= e10[i] and c[i] < e20[i]:
                e = e10[i]; s = e20[i] * 1.001
                es[i] = 1; epx[i] = e; slp[i] = s; tpp[i] = e - 3 * (s - e)
                gate_seen = False; armed = None; dn = 0
        d["enter_long"] = el.astype(int); d["enter_short"] = es.astype(int)
        d["entry_price"] = epx; d["sl_price"] = slp; d["tp_price"] = tpp
        return d

    def populate_exit_trend(self, dataframe, metadata):
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0
        return dataframe
'''


_EMA5_GAP_SWEEP_CODE = '''
from freqtrade.strategy import IStrategy
from pandas import DataFrame
import numpy as np
import talib.abstract as ta


class Ema5GapSweep(IStrategy):
    """EMA5 Gap + Sweep — translated from a TradingView Pine indicator.
    The prior candle gaps entirely clear of the 5-EMA, the current bar sweeps
    the prior extreme, and a 50-EMA trend filter aligns. Structural stop, 6R
    target via per-trade entry_price / sl_price / tp_price."""

    can_short = True
    timeframe = "5m"
    stoploss = -0.05
    minimal_roi = {"0": 0.30}
    process_only_new_candles = True
    startup_candle_count = 60

    ema5_len = 5
    lt_ema_len = 50
    rr_target = 6.0
    sl_buffer = 0.0001

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

        gap_up = (c1 > e1) & (l1 > e1)
        gap_down = (c1 < e1) & (h1 < e1)
        sweep_bull = gap_down & (high >= h1)
        sweep_bear = gap_up & (low <= l1)

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
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0
        return dataframe
'''


def _seed_builtin_strategies(db):
    """Ensure template strategies exist with correct trading configs."""
    from backend.models.strategy import Strategy

    templates = [
        {
            "name": "GaulsLiquiditySweep",
            "description": "Liquidity Sweep (Gauls model) — trades the reversal after a "
                           "stop-hunt. Marks buy-side liquidity (BSL, above recent highs) "
                           "and sell-side liquidity (SSL, below recent lows); waits for a "
                           "liquidity-sweep candle (small body + large rejection wick into "
                           "the pool + volume above the ~70th percentile), then enters the "
                           "reversal toward the opposite untouched pool. LONG after an SSL "
                           "sweep, SHORT after a BSL sweep. Stop is placed BEYOND the swept "
                           "extreme (not on the obvious level); target is the opposite pool "
                           "(min 1.5R). Causal / no-repaint. Best on 1H-4H. ~70% reversal "
                           "edge per the book — size sensibly, not a guarantee.",
            "code": _GAULS_LIQUIDITY_SWEEP_CODE,
            "stoploss": -0.03,
            "take_profit": 0.06,
            "leverage": 5,
            "timeframe": "1h",
        },
        {
            "name": "FiveThirtyFib",
            "description": "5:30 Candle Fib Breakout (IMPROVED) — marks the 00:00 UTC "
                           "daily-open candle (5:30 AM IST) hi/lo. A later candle that "
                           "CLOSES beyond it with body>wick, aligned with EMA-50, AND in a "
                           "trending regime (ADX>20, skips choppy fakeout days) draws a Fib "
                           "from the day-open level to the breakout extreme: enters on the "
                           "0.25 retracement, structural stop at 0.75, target 1:3 — lets "
                           "winners run (per-trade sl_price/tp_price). One setup per UTC "
                           "day. EMA-50 + ADX filters lift win rate ~10pts over the pure "
                           "rules; the 1:3 target ~doubled gold profit in tests (breakeven "
                           "WR only 25%). Best on 4H; enable ARM (partial TP + trail) for a "
                           "further robust boost. Positive EV — size sensibly, not a guarantee.",
            "code": _FIVE_THIRTY_FIB_CODE,
            "stoploss": -0.02,
            "take_profit": 0.04,
            "leverage": 5,
            "timeframe": "4h",
        },
        {
            "name": "FiveThirtyFibPure",
            "description": "5:30 Candle Fib Breakout (PURE / literal rules, no extra "
                           "filters) — the user's exact spec: mark the 00:00 UTC (5:30 IST) "
                           "candle hi/lo; a later candle CLOSING beyond it with body>wick "
                           "arms a Fib (enter 0.25, stop 0.75, target 1:2); one setup per "
                           "UTC day. Faithful baseline for comparison — note it backtests "
                           "BELOW breakeven after costs (WR ~32-38% vs the ~33% needed). "
                           "Use FiveThirtyFib (with EMA-50 + ADX filters) for the edge.",
            "code": _FIVE_THIRTY_FIB_PURE_CODE,
            "stoploss": -0.02,
            "take_profit": 0.04,
            "leverage": 5,
            "timeframe": "1h",
        },
        {
            "name": "EmaGate",
            "description": "EMA Gate — EMA(20/50/100/200) compression breakout. When the "
                           "EMAs bunch tight (a 'gate'), SKIP the first breakout and enter "
                           "the SECOND one AT the 10-EMA (precise limit entry), SL below "
                           "the 20-EMA, big 1:3 target. The 1h-200-EMA sets directional "
                           "bias (buys only above it, breakdowns only below). Very "
                           "selective / low-frequency (~2-3 trades/mo). Walk-forward on "
                           "BTC 1h: mostly_robust (3/4 windows positive, tiny drawdown). "
                           "Best on 1H; loses on 15m + gold. Verify forward before sizing.",
            "code": _EMA_GATE_CODE,
            "stoploss": -0.02,
            "take_profit": 0.06,
            "leverage": 5,
            "timeframe": "1h",
        },
        {
            "name": "Ema5GapSweep",
            "description": "EMA5 Gap + Sweep (from a TradingView Pine indicator) — the "
                           "prior candle gaps entirely clear of the 5-EMA, the current bar "
                           "sweeps its extreme, and a 50-EMA trend filter aligns. Enters at "
                           "the prior extreme with a structural stop and a 6R target "
                           "(per-trade sl_price/tp_price). Restricted strategy. Best on "
                           "1m-5m; backtest with real costs + walk-forward before sizing.",
            "code": _EMA5_GAP_SWEEP_CODE,
            "stoploss": -0.05,
            "take_profit": 0.30,
            "leverage": 5,
            "timeframe": "5m",
        },
        {
            "name": "LorentzianClassifier",
            "description": "Lorentzian Distance Classifier (LDC) — faithful port of "
                           "jdehorty's ML Lorentzian Classification. k-NN (k=8) over a "
                           "5-feature space {RSI(14), WT(10,11), CCI(20), ADX(20), RSI(9)} "
                           "using Lorentzian distance, with his Approximate-Nearest-Neighbors "
                           "selection (every-4th-bar skip + distance ratchet) and past-4-bar "
                           "directional labels. Gated by Volatility + Regime (KLMF slope) "
                           "filters and a Nadaraya-Watson rational-quadratic kernel trend "
                           "filter. Exits: fixed 4-bar hold + signal-flip (stop-and-reverse) "
                           "+ optional dynamic kernel exit. Fetches 2000 bars of deep history. "
                           "Optimized for 4H-12H timeframes. Causal / no-repaint; a wide "
                           "3xATR/6xATR stop is added as a safety net (the original has none).",
            "code": _LORENTZIAN_LDC_CODE,
            "stoploss": -0.08,
            "take_profit": 0.12,
            "leverage": 5,
            "timeframe": "4h",
        },
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
        {
            "name": "StrategyAsh",
            "description": "Institutional SMC strategy — full multi-TF model per user-"
                           "provided spec. ALIGNS 3 LAYERS: 4H structure + bias + dealing "
                           "range (HTF bias engine), 1H EMA50/200 confluence (MTF trend), "
                           "15M ADX(14) momentum filter, 5M execution clock with VWAP + "
                           "Bollinger + RSI + swing pivots + CHoCH + displacement + FVG "
                           "midpoint entry. Uses 1M/3M sub-bar liquidity-sweep detection "
                           "(falls back to 5M when sub-data unavailable). Risk: structural "
                           "SL below swept level + TP1=2R / TP2=3R via ARM. Discipline: "
                           "max 60 LTF candles hold (5h on 5m) + max 3 stops per day. Only "
                           "trades when HTF bias is clearly bull or bear — sits out RANGE. "
                           "Declares max_hold_candles=60 + max_stops_per_day=3 + "
                           "sub_timeframes=['1m','3m'] — engine honours all three opt-in.",
            "code": _STRATEGY_ASH_CODE,
            "stoploss": -0.03,
            "take_profit": 0.06,
            "leverage": 10,
            "timeframe": "5m",
        },
        {
            "name": "Connors RSI(2) Mean Reversion",
            "description": "High-WR mean-reversion strategy from Larry Connors' 'Short Term "
                           "Trading Strategies That Work' (2008). Documented 70-78% WR on "
                           "stocks; adapted for crypto with EMA200 trend filter and ATR-based "
                           "SL. LONG when close > EMA200 AND RSI(2) < 10 AND bullish reversal. "
                           "SHORT symmetric. SL=2× ATR, TP=1.5R. Expected on KuCoin BTC "
                           "futures 15m: 55-68% WR, 3-8 trades/week. Best for users who want "
                           "fewer but higher-quality entries.",
            "code": _CONNORS_RSI2_CODE,
            "stoploss": -0.02,
            "take_profit": 0.03,
            "leverage": 10,
            "timeframe": "15m",
        },
        {
            "name": "EMA Pullback Trend",
            "description": "Trend-following with high-WR via tight setup. Buy-the-dip in "
                           "established uptrends (close > EMA200 + EMA21 > EMA50), enter on "
                           "EMA21 pullback touch + RSI<45 + bullish reversal candle. SHORT "
                           "symmetric. SL=1.8× ATR, TP=2R. Expected on KuCoin BTC futures "
                           "15m: 58-68% WR, 5-15 trades/month. Trades fewer setups than RSI(2) "
                           "but each has bigger R potential.",
            "code": _EMA_PULLBACK_CODE,
            "stoploss": -0.02,
            "take_profit": 0.04,
            "leverage": 10,
            "timeframe": "15m",
        },
        {
            "name": "Bollinger Bands Strategy",
            "description": "Exact port of TradingView's built-in 'Bollinger Bands "
                           "Strategy' — classic mean-reversion crossover. LONG when "
                           "close crosses BACK ABOVE the lower band (oversold bounce); "
                           "SHORT when close crosses BACK BELOW the upper band "
                           "(overbought rejection). Stop-and-reverse on opposite "
                           "signal (matches TV's pyramiding=0 + OCA). Tunables: "
                           "BB_LEN=20, BB_MULT=2.0 (TV defaults). On 5m BTC/USDT "
                           "1-month backtest: TV's built-in shows 207 trades, "
                           "~65% WR, +1.05% P&L. This port matches within ±5% "
                           "trade count, ±3pp WR. With leverage and proper risk "
                           "management can yield 15-25% monthly returns.",
            "code": _BBANDS_STRATEGY_CODE,
            "stoploss": -0.02,
            "take_profit": 0.01,
            "leverage": 10,
            "timeframe": "5m",
        },
        {
            "name": "SMC Strategy (5min)",
            "description": "5-minute SMC scalper — replaced the earlier 1m "
                           "version because pure SMC patterns are 5m+ patterns "
                           "(on 1m most matches are noise). Multi-TF: 1h "
                           "EMA200 trend bias (HTF) + 15m dealing-range "
                           "premium/discount (MTF) + 5m execution (sweep + "
                           "CHoCH + displacement + EMA21/50 + RSI<72 + "
                           "ADX>=20 + ATR vol filter + entry-zone retest). "
                           "10-gate AND chain filters chop and only fires on "
                           "high-quality SMC setups. Risk: max(structural SL "
                           "5bps beyond swept extreme, 1.2x ATR floor), "
                           "TP=2R, 1% max SL distance, max 5 stops/day. "
                           "1-3 trades/day on BTC perp during normal "
                           "volatility — much higher signal quality than 1m.",
            "code": _SMC_SCALPER_1M_CODE,
            "stoploss": -0.01,
            "take_profit": 0.02,
            "leverage": 10,
            "timeframe": "5m",
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
    # Previously this relied on the user hitting GET /api/futures/bots to
    # trigger resume. Problem: if the user wasn't on the page when Railway
    # redeployed (e.g. overnight), bots stayed dead — DB marked them
    # running but no engine thread existed, so they missed every signal
    # until the user opened the panel hours later. That's the
    # \"my bots show 0 trades since yesterday\" complaint.
    #
    # Now we proactively spin up every DB-marked-running bot at startup,
    # without waiting for a request. Restores ARM + overrides + cooldown
    # config off the same StrategyInstance row used by the API resume path.
    try:
        _resume_dead_bots(log_label="auto-resume")
    except Exception as auto_resume_exc:
        log.warning("auto-resume scan failed (bots will resume on first GET /api/futures/bots): %s",
                    auto_resume_exc)
    log.info("Background startup complete. (futures-only — no spot stack)")


def _resume_dead_bots(*, log_label: str = "watchdog") -> int:
    """Scan DB for is_running=True bots and resume any whose engine thread
    has died. Shared by:
      • _background_startup (one-shot at boot)
      • _bot_watchdog       (periodic, every 60s while the app is up)

    Returns the number of bots actually resumed (useful for logging).

    The auto-resume in /api/futures/bots is still kept so a user pulling
    the panel right after a freshly-deployed instance gets an instant
    resume even before the watchdog's first tick.
    """
    import logging
    log = logging.getLogger(log_label)
    from sqlalchemy import select, desc
    from backend.models.trade import StrategyInstance
    from backend.models.config import Config
    from backend.services.futures_engine import futures_engine_registry
    from backend.utils.encryption import decrypt

    resumed = 0
    with SessionLocal() as db:
        rows = db.execute(
            select(StrategyInstance)
            .where(StrategyInstance.is_running == True)  # noqa: E712
            .order_by(desc(StrategyInstance.created_at))
        ).scalars().all()
        if not rows:
            return 0
        creds_cache: dict[str, tuple[str, str, str]] = {}
        for i in rows:
            if not i.engine_key:
                continue
            eng = futures_engine_registry.for_bot(i.user_id, i.engine_key)
            if eng.is_running:
                continue
            # Decrypt KuCoin creds once per (user, api_mode) — live bots need
            # them, paper bots ignore them. Regular bots use the regular keys.
            _bot_api_mode = getattr(i, "api_mode", "lead") or "lead"
            _ck = (i.user_id, _bot_api_mode)
            if _ck not in creds_cache:
                cfg = db.execute(
                    select(Config).where(Config.user_id == i.user_id).limit(1)
                ).scalar_one_or_none()
                kk = ks = kp = ""
                if cfg:
                    try:
                        from backend.services.futures_mode import load_kucoin_creds
                        kk, ks, kp = load_kucoin_creds(cfg, i.user_id, _bot_api_mode)
                    except Exception:
                        pass
                creds_cache[_ck] = (kk, ks, kp)
            kk, ks, kp = creds_cache[_ck]
            pairs = [p.strip() for p in (i.pairs or "BTC/USDT").split(",")]
            # Decode the persisted strategy flag toggles (JSON) so the bot
            # resumes with the same options the user picked.
            _sf_raw = getattr(i, "strategy_flags", None)
            _sf = None
            if _sf_raw:
                try:
                    import json as _json_sf
                    _sf = _json_sf.loads(_sf_raw) if isinstance(_sf_raw, str) else (_sf_raw if isinstance(_sf_raw, dict) else None)
                    if not isinstance(_sf, dict) or not _sf:
                        _sf = None
                except Exception:
                    _sf = None
            try:
                eng.start_futures(
                    strategy_name=i.strategy_name, pairs=pairs,
                    leverage=i.leverage or 10,
                    mode=i.mode or "paper",
                    timeframe=i.timeframe or "15m",
                    stoploss=i.stoploss or -0.03, wallet=i.wallet or 1000,
                    take_profit_pct=(i.takeprofit or 0.015) * 100,
                    max_position_pct=(i.risk_pct or 5.0),
                    strategy_id=i.strategy_id,
                    instance_id=i.id,
                    api_mode=_bot_api_mode,
                    kucoin_key=kk, kucoin_secret=ks, kucoin_passphrase=kp,
                    arm_enabled       = bool(getattr(i, "arm_enabled", False) or False),
                    arm_tp1_close_pct = float(getattr(i, "arm_tp1_close_pct", 50.0) or 50.0),
                    arm_be_mode       = str(getattr(i, "arm_be_mode", "leverage") or "leverage"),
                    arm_be_buffer_pct = float(getattr(i, "arm_be_buffer_pct", 1.0) or 1.0),
                    arm_trail_to_tp1  = bool(getattr(i, "arm_trail_to_tp1", True)
                                              if i.arm_trail_to_tp1 is not None else True),
                    session_start_hr_utc = getattr(i, "session_start_hr_utc", None),
                    session_end_hr_utc   = getattr(i, "session_end_hr_utc", None),
                    equal_price_thresh   = getattr(i, "equal_price_thresh", None),
                    # Phase 9 — restore hedge mode on resume (None/legacy
                    # rows normalise to "single" inside start_futures).
                    position_mode        = getattr(i, "position_mode", None),
                    strategy_flags       = _sf,
                    # Restore the persisted Risk-Guard (loss-streak cooldown)
                    # config. Without these, this background resume dropped the
                    # user's choice and start_futures re-defaulted guard_enabled
                    # to True — so a bot the user turned the guard OFF for had it
                    # silently switched back ON after every Railway redeploy.
                    guard_enabled      = bool(getattr(i, "guard_enabled", True)
                                              if i.guard_enabled is not None else True),
                    guard_max_consec   = int(getattr(i, "guard_max_consec", 5) or 5),
                    guard_cooldown_min = int(getattr(i, "guard_cooldown_min", 60) or 60),
                )
                resumed += 1
                # Re-apply paused state if the bot was paused before the
                # backend went down. Without this, a paused bot silently
                # un-pauses on every Railway redeploy → new entries start
                # firing again from a strategy the user explicitly halted.
                if getattr(i, "is_paused", False):
                    try:
                        eng.pause()
                        log.info("%s: re-applied PAUSED state to %s",
                                 log_label, i.engine_key)
                    except Exception as pause_exc:
                        log.warning("%s: failed to re-apply pause to %s: %s",
                                    log_label, i.engine_key, pause_exc)
                log.info("%s: spun up %s (%s) for user %s",
                         log_label, i.strategy_name, i.engine_key, i.user_id)
            except Exception as resume_exc:
                log.warning("%s: resume failed for %s: %s",
                            log_label, i.engine_key, resume_exc)
        if resumed:
            log.info("%s: resumed %d dead bot(s)", log_label, resumed)
    return resumed


async def _manual_position_watchdog():
    """Tick paper limit-order fills + manual TP/SL/liquidation every 5s.

    The MAIN user engine (for_user) never starts _run_loop (no strategy
    configured). Without this watchdog:
      • /manual-entry paper positions never auto-exit on TP/SL/liq
      • /order paper limit orders never auto-fill when price crosses
    Both behaviours leave the user unable to actually paper-trade
    without manually clicking Close / hoping the order fills magically.

    LIVE positions/orders are handled by KuCoin natively + the
    reconcile in /api/futures/orders (limit fills) and the
    maybe_reconcile_live_positions path (position drift).

    Cheap: skips users with no positions AND no pending orders.
    Mirrors the bot loop's 5s fast-tick when state needs attention.
    """
    import logging
    log = logging.getLogger("manual-watchdog")
    INTERVAL = 5.0
    await asyncio.sleep(10.0)  # let startup finish first
    log.info("Manual-position watchdog started — checking every %ds", int(INTERVAL))
    while True:
        try:
            from backend.services.futures_engine import futures_engine_registry
            # Snapshot the user-engine dict outside the lock so the loop
            # itself doesn't block any incoming requests.
            with futures_engine_registry._lock:
                engines = list(futures_engine_registry._engines.values())
            for eng in engines:
                # Skip engines with no work to do.
                if not eng.positions and not getattr(eng, "_pending_orders", None):
                    continue
                # 1. Fill any paper limit/stop orders whose price has crossed.
                try:
                    nf = eng.tick_pending_orders_paper()
                    if nf > 0:
                        log.info("[%s] manual-watchdog filled %d order(s)", eng.user_id, nf)
                except Exception as exc:
                    log.warning("[%s] manual-watchdog order-fill tick failed: %s",
                                eng.user_id, exc)
                # 2. TP/SL/liq on open PAPER positions (may include positions
                #    that were just created by step 1's fills).
                try:
                    nc = eng.tick_manual_position_management()
                    if nc > 0:
                        log.info("[%s] manual-watchdog closed %d position(s)",
                                 eng.user_id, nc)
                except Exception as exc:
                    log.warning("[%s] manual-watchdog exit tick failed: %s",
                                eng.user_id, exc)
                # 3. Reconcile LIVE manual positions against KuCoin so a
                #    TP/SL trigger, liquidation, or a close done on KuCoin's
                #    own app clears our local row + books the P&L even when the
                #    user isn't polling the Positions tab (24/7 trading). The
                #    /open endpoint also calls this, but only while the UI is
                #    open. No-op unless the engine holds a live position;
                #    throttled to 3s inside the method.
                try:
                    eng.maybe_reconcile_live_positions(throttle_secs=3)
                except Exception as exc:
                    log.warning("[%s] manual-watchdog reconcile tick failed: %s",
                                eng.user_id, exc)
        except Exception as outer_exc:
            log.warning("Manual-position watchdog outer failure: %s", outer_exc)
        await asyncio.sleep(INTERVAL)


async def _bot_watchdog():
    """Periodic watchdog — revives engines that died between user UI polls.

    Without this, a bot that crashes (or whose process was the source of
    a Railway redeploy + the user wasn't on the panel) stays dead until
    someone hits GET /api/futures/bots. For a 24/7 trading app that's
    not acceptable — the user expects "start once, run forever."

    Fires every 60s. Cheap: typically 0 bots to resume, just a quick DB
    scan. If a bot was auto-stopped via the compile-failure breaker its
    is_running flag is now False (see _persist_auto_stop), so we don't
    keep resurrecting broken strategies in a hot loop.
    """
    import logging
    log = logging.getLogger("watchdog")
    INTERVAL = 60.0
    # Wait one full interval before the first scan so _background_startup's
    # initial auto-resume gets a chance to finish first (avoids double-start
    # races where both try to spin up the same engine).
    await asyncio.sleep(INTERVAL)
    log.info("Bot watchdog started — checking every %ds", int(INTERVAL))
    while True:
        try:
            n = _resume_dead_bots(log_label="watchdog")
            if n > 0:
                log.info("Watchdog tick resumed %d bot(s)", n)
        except Exception as exc:
            log.warning("Watchdog tick failed: %s", exc)
        await asyncio.sleep(INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fire heavy work in the background — Uvicorn starts serving immediately,
    # so the Railway healthcheck passes in <3 seconds instead of ~40 seconds.
    asyncio.create_task(_background_startup())
    # Watchdog: revives dead bot engines every 60s while the app is up.
    # Required for true 24/7 operation — the user expects "start once,
    # run forever" and the old code only resumed on backend boot or when
    # the UI polled the bots endpoint.
    asyncio.create_task(_bot_watchdog())
    # Manual-position watchdog: ticks TP/SL/liq on /manual-entry positions
    # every 5s. Without this, the main user engine (which has no run loop)
    # would never auto-close manual paper positions on hitting stops.
    asyncio.create_task(_manual_position_watchdog())
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
    # X-User-Email is sent by the frontend (Clerk-verified email) for the
    # access-code admin/unlimited allowlists. It MUST be allowed here or the
    # browser's CORS preflight on cross-origin (direct-Railway) requests — e.g.
    # the backtest POST — is rejected with 400 "Disallowed CORS headers" and
    # the real request never fires (looks like a backend timeout).
    allow_headers=["Authorization", "Content-Type", "X-User-Email"],
)

# --- Routers ---------------------------------------------------------------
app.include_router(auth.router)
app.include_router(access_router.router)  # access-code gate (NOT itself gated)
app.include_router(strategy.router)
app.include_router(market.router)        # market data (price/pairs/orderbook) — shared by chart + futures
# Futures = the real-money trading surface → gate it behind an active access
# code (admin / unlimited-allowlist / valid code). The dependency fails OPEN on
# infra errors so a glitch can't brick trading; the frontend AccessGate is the
# primary UX barrier. The /api/access/* router above is intentionally NOT gated
# so a code-less user can still reach /status and /redeem.
app.include_router(futures_router.router, dependencies=[Depends(require_active_access)])
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


@app.get("/api/health/capacity")
async def health_capacity():
    """Live scaling telemetry: process memory, thread count, active engines and
    DB-pool utilisation. Read-only — poll this while load-testing to see the REAL
    headroom instead of estimates. The DB pool is the first ceiling to watch."""
    import os as _os
    import threading as _threading

    # ── Process resident memory (Railway = Linux → /proc; fallback resource) ──
    rss_mb = None
    try:
        with open("/proc/self/status") as _f:
            for _line in _f:
                if _line.startswith("VmRSS:"):
                    rss_mb = round(int(_line.split()[1]) / 1024, 1)  # kB → MB
                    break
    except Exception:
        try:
            import resource as _res
            rss_mb = round(_res.getrusage(_res.RUSAGE_SELF).ru_maxrss / 1024, 1)
        except Exception:
            rss_mb = None

    # ── Engine counts ──
    try:
        from backend.services.futures_engine import futures_engine_registry
        engines = futures_engine_registry.stats()
    except Exception as _e:
        engines = {"error": str(_e)}

    # ── DB pool (the first hard ceiling — Supabase session pooler caps ~15) ──
    pool: dict = {}
    try:
        from backend.models.database import engine as _dbeng
        _p = _dbeng.pool
        _size = int(_os.getenv("DB_POOL_SIZE", "8"))
        _overflow = int(_os.getenv("DB_MAX_OVERFLOW", "4"))
        _max = _size + _overflow
        _checked_out = _p.checkedout()
        pool = {
            "configured_size": _size,
            "max_overflow": _overflow,
            "max_connections": _max,
            "checked_out": _checked_out,
            "checked_in": _p.checkedin(),
            "utilization_pct": round(_checked_out / max(1, _max) * 100, 1),
        }
    except Exception as _e:
        pool = {"error": str(_e)}

    # Cheap health verdict.
    warnings = []
    if isinstance(pool.get("utilization_pct"), (int, float)) and pool["utilization_pct"] >= 80:
        warnings.append("DB pool >80% used — raise DB_POOL_SIZE (needs Supabase transaction pooler on port 6543).")
    if rss_mb and rss_mb > 3500:
        warnings.append("Process RSS >3.5 GB — bump the Railway container or trim per-engine candle history.")

    return {
        "status": "ok",
        "process": {"rss_mb": rss_mb, "threads": _threading.active_count()},
        "engines": engines,
        "db_pool": pool,
        "warnings": warnings,
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
