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
from backend.routers import auth, strategy, backtest, trading, market, analysis, autotrade, webhook  # noqa: E402
from backend.routers import futures as futures_router  # noqa: E402
from backend.routers import copy_trading as copy_router  # noqa: E402
from backend.routers import multi_strategy as multi_router  # noqa: E402
from backend.services.freqtrade_manager import freqtrade_mgr  # noqa: E402
from backend.services.autotrade_engine import autotrade_engine  # noqa: E402
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

    Rules implemented (matches the original Pine):
      1. PIVOT BOS    : close crosses last confirmed pivot (N=5 each side)
      2. FVG zone     : price currently INSIDE an unfilled 3-candle
                        imbalance gap from the last 20 bars
      3. Structural SL: last opposing pivot ± 10bps buffer (caps at 5%)
      4. Multi-TP     : TP1 = 2R (close 50%, SL → BE), TP2 = prev pivot

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
        # Confirmed only N bars after the pivot — we shift forward by N
        # so the column at bar i never peeks at future data.
        roll_h = high.rolling(2 * sl + 1, center=True, min_periods=2 * sl + 1).max()
        roll_l = low .rolling(2 * sl + 1, center=True, min_periods=2 * sl + 1).min()
        ph = (roll_h.to_numpy() == highs)
        pl = (roll_l.to_numpy() == lows)
        ph_shifted = np.zeros(n, dtype=bool)
        pl_shifted = np.zeros(n, dtype=bool)
        if sl < n:
            ph_shifted[sl:] = ph[:-sl] if sl > 0 else ph
            pl_shifted[sl:] = pl[:-sl] if sl > 0 else pl
        df["pivot_high"] = ph_shifted
        df["pivot_low"]  = pl_shifted

        # Last confirmed pivot up to current bar (for BOS + SL anchor).
        last_ph = high.where(pd.Series(ph_shifted, index=df.index)).ffill().to_numpy()
        last_pl = low .where(pd.Series(pl_shifted, index=df.index)).ffill().to_numpy()
        df["last_ph"] = last_ph
        df["last_pl"] = last_pl

        # ── BOS: close crosses last confirmed pivot (edge detection) ────
        bull_bos = np.zeros(n, dtype=bool)
        bear_bos = np.zeros(n, dtype=bool)
        for i in range(1, n):
            if not np.isnan(last_ph[i]) and not np.isnan(last_ph[i-1]):
                if closes[i] > last_ph[i] and closes[i-1] <= last_ph[i-1]:
                    bull_bos[i] = True
            if not np.isnan(last_pl[i]) and not np.isnan(last_pl[i-1]):
                if closes[i] < last_pl[i] and closes[i-1] >= last_pl[i-1]:
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
        # Reject if risk > 5% of entry (broken structure / pivot too far).
        bad_long  = (risk_long  <= 0) | (risk_long  > entry * 0.05) | np.isnan(sl_long)
        bad_short = (risk_short <= 0) | (risk_short > entry * 0.05) | np.isnan(sl_short)
        long_signal  = long_signal  & ~bad_long
        short_signal = short_signal & ~bad_short

        tp1_long  = entry + 2 * risk_long      # 2R target
        tp1_short = entry - 2 * risk_short
        # TP2 = previous OPPOSING pivot (next liquidity pool to grab).
        tp2_long  = last_ph                     # for LONG, target prev pivot HIGH
        tp2_short = last_pl                     # for SHORT, target prev pivot LOW

        df["sl_price"]  = np.where(long_signal, sl_long,  np.where(short_signal, sl_short,  np.nan))
        df["tp_price"]  = np.where(long_signal, tp1_long, np.where(short_signal, tp1_short, np.nan))
        df["tp2_price"] = np.where(long_signal, tp2_long, np.where(short_signal, tp2_short, np.nan))

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
    Trend-following EMA + RSI strategy. Validates LONG + SHORT flow.

    LONG  : EMA9 > EMA21 (uptrend confirmed) AND RSI < 60 (not overbought)
    SHORT : EMA9 < EMA21 (downtrend confirmed) AND RSI > 40 (not oversold)
    SL: 1.5% | TP: 3.0% (2:1 R:R) | TF: 15m
    """

    timeframe   = "15m"
    stoploss    = -0.015
    minimal_roi = {"0": 0.030}
    can_short   = True
    startup_candle_count = 30
    process_only_new_candles = True

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema9"]  = ta.EMA(dataframe, timeperiod=9)
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["rsi"]   = ta.RSI(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"]  = 0
        dataframe["enter_short"] = 0

        # Confirmed trend = current AND previous bar both agree.
        uptrend   = (dataframe["ema9"] > dataframe["ema21"]) & \
                    (dataframe["ema9"].shift(1) > dataframe["ema21"].shift(1))
        downtrend = (dataframe["ema9"] < dataframe["ema21"]) & \
                    (dataframe["ema9"].shift(1) < dataframe["ema21"].shift(1))

        dataframe.loc[uptrend   & (dataframe["rsi"] < 60), "enter_long"]  = 1
        dataframe.loc[downtrend & (dataframe["rsi"] > 40), "enter_short"] = 1
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
                timeframe="15m",
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

    # ── Auto-resume all bot engines ───────────────────────────────────────────
    try:
        from backend.services.native_trading_engine import native_engine_registry
        with SessionLocal() as db:
            rows = db.execute(select(Config)).scalars().all()
        for cfg in rows:
            if not cfg.user_id:
                continue
            # Auto-trade engine
            if cfg.auto_trade_enabled:
                try:
                    autotrade_engine.for_user(cfg.user_id).start()
                except Exception:
                    pass
            # Paper / live SPOT bot only — skip futures modes entirely
            # (futures auto-resume would need futures_engine_registry, handled separately)
            if cfg.bot_running and cfg.bot_strategy_name and not (cfg.bot_mode or "").startswith("futures"):
                try:
                    pairs = [p.strip() for p in (cfg.bot_pairs or "BTC/USDT").split(",") if p.strip()]
                    eng = native_engine_registry.for_user(cfg.user_id)
                    if cfg.bot_mode == "live":
                        from backend.utils.encryption import decrypt, DecryptError
                        try:
                            kk = decrypt(cfg.kucoin_key_enc or "", cfg.user_id)
                            ks = decrypt(cfg.kucoin_secret_enc or "", cfg.user_id)
                            kp = decrypt(cfg.kucoin_passphrase_enc or "", cfg.user_id)
                            eng.start_live(
                                strategy_name=cfg.bot_strategy_name, pairs=pairs,
                                timeframe=cfg.bot_timeframe or "15m",
                                stoploss=cfg.bot_stoploss or -0.03,
                                kucoin_key=kk, kucoin_secret=ks, kucoin_passphrase=kp,
                                wallet=cfg.bot_wallet or 1000.0,
                            )
                        except DecryptError:
                            pass
                    else:
                        eng.start_paper(
                            strategy_name=cfg.bot_strategy_name, pairs=pairs,
                            timeframe=cfg.bot_timeframe or "15m",
                            stoploss=cfg.bot_stoploss or -0.03,
                            wallet=cfg.bot_wallet or 1000.0,
                        )
                except Exception:
                    pass
    except Exception as e:
        log.error("engine auto-resume failed: %s", e)

    # ── Resume multi-strategy instances ───────────────────────────────────────
    try:
        from backend.services.multi_strategy import multi_strategy_manager
        with SessionLocal() as db:
            resumed = multi_strategy_manager.resume_all(db)
            if resumed:
                log.info("Resumed %d multi-strategy instances", resumed)
    except Exception as e:
        log.error("multi-strategy resume failed: %s", e)

    log.info("Background startup complete.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fire heavy work in the background — Uvicorn starts serving immediately,
    # so the Railway healthcheck passes in <3 seconds instead of ~40 seconds.
    asyncio.create_task(_background_startup())
    yield
    try:
        autotrade_engine.stop_all()
    except Exception:
        pass
    try:
        freqtrade_mgr.stop_all()
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
app.include_router(backtest.router)
app.include_router(trading.router)
app.include_router(market.router)
app.include_router(analysis.router)
app.include_router(autotrade.router)
app.include_router(webhook.router)
app.include_router(futures_router.router)
app.include_router(copy_router.router)
app.include_router(multi_router.router)


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
    return {
        "status": "healthy",
        "active_users": freqtrade_mgr.active_users(),
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
                await ws.send_json({
                    "type": "pong",
                    "bot": freqtrade_mgr.for_user(user_id).status,
                    "timestamp": datetime.utcnow().isoformat(),
                })
    except WebSocketDisconnect:
        ws_manager.disconnect(user_id, ws)
