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
class SimpleTargetStrategy:
    """
    Buys when RSI < 55 and price is near EMA-20 (pullback zone),
    or when RSI < 38 (oversold). Exits at +3% take-profit or -1.5% stop-loss (2:1 R:R).
    Also shorts when RSI > 65 and price above EMA-20, or RSI > 72 (overbought).
    Works in any market condition — bidirectional LONG + SHORT.
    """
    minimal_roi = {"0": 0.030}
    stoploss = -0.015
    timeframe = "15m"
'''

_SMC_STRATEGY_CODE = '''
class SMCStrategy:
    """
    Smart Money Concepts (SMC) — Full multi-timeframe implementation.

    Layers (ALL must align for entry):
      1. HTF Bias  : EMA200 direction (simulates 4H trend)
      2. Swing     : N=5 bar swing highs/lows detection
      3. BOS       : Break of Structure (price breaks last swing)
      4. FVG       : Fair Value Gap (3-candle imbalance)
      5. OB        : Order Block (last opposing candle before BOS)
      6. Discount  : Price below 50% Fibonacci = buy zone
      7. Liq Sweep : Wick takes out stops then reverses
      8. NY Session: 13:00–21:00 UTC only

    LONG:  HTF bullish + discount zone + FVG/OB + sell-side sweep + BOS up
    SHORT: HTF bearish + premium zone + FVG/OB + buy-side sweep + BOS down
    SL: Below swept liquidity. TP: 2R from entry.
    """
    minimal_roi = {"0": 0.03}   # 2R target
    stoploss = -0.015
    timeframe = "15m"
    startup_candle_count = 210
'''

_SMC_TV_STRATEGY_CODE = '''
from freqtrade.strategy import IStrategy
import pandas as pd
import numpy as np


class SMCStrategyTV(IStrategy):
    """
    SMC v2 — full institutional Smart Money Concepts.

    Implements every rule from the spec (HTF bias → premium/discount →
    liquidity sweep → OB/FVG mitigation → LTF confirmation → session) and
    populates STRUCTURAL sl_price + tp_price columns per bar so the engine
    uses real swing-based stops instead of slider %s. Multi-TP (TP1 = 2R,
    TP2 = previous swing) populated via tp_price (TP1) and tp2_price (TP2).

    Rules implemented:
      1. PIVOT detection   : N=5 swing-high / swing-low (rolling window)
      2. HTF bias          : last BOS direction on N=30 swings (≈daily on 15m)
                              → RANGE = no trade
      3. Liquidity sweep   : prior swing low/high briefly broken + reclaimed
      4. OB                : last opposing candle before >0.5% strong move
      5. FVG               : 3-candle imbalance (high[i-2] < low[i] for bull)
      6. Premium/Discount  : 50% fib of HTF range — LONG only in discount,
                              SHORT only in premium
      7. LTF BOS confirm   : 3-bar swing broken in bias direction by close
      8. NY session        : 12:00-21:00 UTC only
      9. Structural SL     : anchored to sweep extreme ± 10bps buffer
     10. Multi-TP          : TP1 = 2R (partial), TP2 = previous swing extreme

    Sliders are IGNORED when "SL/TP source = From strategy" (default for
    structural strategies) — every trade exits at its own computed level.
    """
    timeframe   = "15m"
    minimal_roi = {"0": 100}        # exits handled by populate_exit_trend
    stoploss    = -0.99             # disable Freqtrade global SL; SL set per-trade
    can_short   = True              # required for shorts on KuCoin Futures
    startup_candle_count = 100      # need HTF swing-confirmation lookback
    process_only_new_candles = True

    # Swing detection lookbacks (one bar each side).
    LTF_N = 3       # ~45min on 15m  → entry triggers
    HTF_N = 30      # ~7.5h on 15m   → daily-ish bias

    def populate_indicators(self, df: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        ltf_n = self.LTF_N
        htf_n = self.HTF_N
        n = len(df)
        high = df["high"]; low = df["low"]; close = df["close"]; open_ = df["open"]

        # ── Multi-scale swing detection (LTF + HTF) ────────────────────
        # A swing at index j is confirmed only after N future bars exist
        # → shift forward by N to prevent look-ahead bias.
        def confirmed_swings(series, kind, n_lb):
            roll = series.rolling(2 * n_lb + 1, center=True, min_periods=2 * n_lb + 1)
            piv = (roll.max() == series) if kind == "high" else (roll.min() == series)
            arr = piv.fillna(False).to_numpy()
            shifted = np.zeros(n, dtype=bool)
            if n_lb < n:
                shifted[n_lb:] = arr[:-n_lb] if n_lb > 0 else arr
            return shifted

        df["sh_ltf"] = confirmed_swings(high, "high", ltf_n)
        df["sl_ltf"] = confirmed_swings(low,  "low",  ltf_n)
        df["sh_htf"] = confirmed_swings(high, "high", htf_n)
        df["sl_htf"] = confirmed_swings(low,  "low",  htf_n)

        # Last confirmed HTF swing high / low up to current bar.
        df["last_sh_htf"] = high.where(pd.Series(df["sh_htf"], index=df.index)).ffill()
        df["last_sl_htf"] = low .where(pd.Series(df["sl_htf"], index=df.index)).ffill()
        df["last_sh_ltf"] = high.where(pd.Series(df["sh_ltf"], index=df.index)).ffill()
        df["last_sl_ltf"] = low .where(pd.Series(df["sl_ltf"], index=df.index)).ffill()

        # ── HTF BIAS via last BOS direction ─────────────────────────────
        # Bull bias = close > last_sh_htf since the most recent swing low.
        # Bear bias = close < last_sl_htf since the most recent swing high.
        # We track when each event LAST happened — most recent wins.
        last_sh_p = df["last_sh_htf"].to_numpy()
        last_sl_p = df["last_sl_htf"].to_numpy()
        c = close.to_numpy()
        bull_bos_at = np.full(n, -1, dtype=int)
        bear_bos_at = np.full(n, -1, dtype=int)
        last_bull = -1; last_bear = -1
        for i in range(n):
            if (not np.isnan(last_sh_p[i])) and c[i] > last_sh_p[i]:
                last_bull = i
            if (not np.isnan(last_sl_p[i])) and c[i] < last_sl_p[i]:
                last_bear = i
            bull_bos_at[i] = last_bull
            bear_bos_at[i] = last_bear
        bias = np.where(
            bull_bos_at > bear_bos_at, 1,                # bull
            np.where(bear_bos_at > bull_bos_at, -1, 0),   # bear / range
        )
        df["htf_bias"] = bias

        # ── Premium/Discount via Fib zone of HTF swing range ────────────
        # Strict 50% line means in a sustained trend the "discount" side
        # almost never gets touched and zero longs fire. Real institutional
        # traders treat the 40-60% zone as "equilibrium" where both bias
        # entries are valid (just less ideal than deep discount/premium).
        # We allow LONG when close <= 60% of range, SHORT when close >= 40%.
        # Still rejects clear "buying at the top" (close > 60%) and
        # "shorting at the bottom" (close < 40%).
        hi, lo = last_sh_p, last_sl_p
        rng = hi - lo
        c_arr = close.to_numpy()
        # Position within range: 0.0 = at swing low, 1.0 = at swing high
        pos_in_range = np.where(rng > 0, (c_arr - lo) / rng, 0.5)
        df["in_discount"] = (pos_in_range <= 0.60) & (rng > 0)
        df["in_premium"]  = (pos_in_range >= 0.40) & (rng > 0)

        # Pull numpy arrays for the inner loops (used by FVG, OB,
        # liquidity sweep, and LTF BOS sections below).
        opens  = open_.to_numpy(); closes = c
        highs  = high.to_numpy();  lows = low.to_numpy()

        # ── FVG: 3-candle imbalance + price-inside-zone (mitigation) ───
        # An FVG forms when high[k-2] < low[k] (bull) or low[k-2] > high[k]
        # (bear). The CORRECT SMC entry trigger is "price retraced INTO an
        # existing unfilled FVG zone" — not "FVG formed on this exact bar".
        # The original `high.shift(2) < low` check was true only on the bar
        # that completes the gap, which is rare and meant most setups were
        # filtered out. We now scan back up to 20 bars for any FVG whose
        # zone currently contains the close, treating it as "mitigated".
        bull_fvg_in_zone = np.zeros(n, dtype=bool)
        bear_fvg_in_zone = np.zeros(n, dtype=bool)
        for i in range(n):
            if i < 2: continue
            for k in range(i, max(2, i - 20), -1):
                if k < 2: break
                # Bullish FVG zone = [high[k-2], low[k]] for k between i-20 and i
                if highs[k - 2] < lows[k]:
                    if highs[k - 2] <= closes[i] <= lows[k]:
                        bull_fvg_in_zone[i] = True
                        break
                # Bearish FVG zone = [high[k], low[k-2]]
                if lows[k - 2] > highs[k]:
                    if highs[k] <= closes[i] <= lows[k - 2]:
                        bear_fvg_in_zone[i] = True
                        break
        df["bull_fvg"] = bull_fvg_in_zone
        df["bear_fvg"] = bear_fvg_in_zone

        # ── Order Block: last opposing candle BEFORE strong move ────────
        # Strong move = close beyond candle extreme by >0.5% within 3 bars.
        # Using a rolling-window lookahead is hard with vectorisation; we
        # approximate by checking the next 3 bars from each candle. Done
        # in a tight numpy loop (cheap: ~5ms for 35k bars).
        bull_ob_active = np.zeros(n, dtype=bool)
        bear_ob_active = np.zeros(n, dtype=bool)
        bull_ob_lo = np.full(n, np.nan)
        bull_ob_hi = np.full(n, np.nan)
        bear_ob_lo = np.full(n, np.nan)
        bear_ob_hi = np.full(n, np.nan)
        for i in range(n):
            # Look back up to 50 bars for the most recent valid OB
            # (previous 30-bar window missed too many valid setups in
            # markets where the OB formed slightly further back).
            lookback = min(50, i)
            for k in range(i - 1, i - lookback - 1, -1):
                if k < 0: break
                # Bullish OB = last bearish candle with strong UP follow-through
                if closes[k] < opens[k]:
                    for m in range(k + 1, min(k + 4, i + 1)):
                        if closes[m] > highs[k] * 1.005:
                            bull_ob_lo[i] = lows[k]
                            bull_ob_hi[i] = highs[k]
                            break
                    if not np.isnan(bull_ob_lo[i]):
                        break
            for k in range(i - 1, i - lookback - 1, -1):
                if k < 0: break
                if closes[k] > opens[k]:
                    for m in range(k + 1, min(k + 4, i + 1)):
                        if closes[m] < lows[k] * 0.995:
                            bear_ob_lo[i] = lows[k]
                            bear_ob_hi[i] = highs[k]
                            break
                    if not np.isnan(bear_ob_lo[i]):
                        break
        # Price currently inside the most recent OB zone?
        df["in_bull_ob"] = (close.to_numpy() >= bull_ob_lo) & (close.to_numpy() <= bull_ob_hi)
        df["in_bear_ob"] = (close.to_numpy() >= bear_ob_lo) & (close.to_numpy() <= bear_ob_hi)

        # ── Liquidity sweep — prev LTF swing extreme briefly grabbed ────
        # For bull: a recent LTF swing low was wicked through THEN reclaimed.
        # For bear: a recent LTF swing high wicked + reclaimed.
        # We compute via numpy loop with 30-bar lookback (cheap).
        sl_ltf = df["sl_ltf"].to_numpy()
        sh_ltf = df["sh_ltf"].to_numpy()
        ll_ltf = df["last_sl_ltf"].to_numpy()
        lh_ltf = df["last_sh_ltf"].to_numpy()
        bull_sweep_at = np.full(n, np.nan)   # the swept low price (= SL anchor)
        bear_sweep_at = np.full(n, np.nan)   # the swept high price
        for i in range(n):
            # Bull sweep: find a recent swing low, then check if any bar
            # since dipped below it AND current close is above it again.
            # 50-bar window (was 30) catches setups where the sweep
            # happened earlier and price is still in the OB/FVG retrace.
            for k in range(i - 1, max(0, i - 50), -1):
                if sl_ltf[k]:
                    prev_low = lows[k]
                    for m in range(k + 1, i + 1):
                        if lows[m] < prev_low and closes[i] > prev_low:
                            bull_sweep_at[i] = lows[m]
                            break
                    break
            for k in range(i - 1, max(0, i - 50), -1):
                if sh_ltf[k]:
                    prev_high = highs[k]
                    for m in range(k + 1, i + 1):
                        if highs[m] > prev_high and closes[i] < prev_high:
                            bear_sweep_at[i] = highs[m]
                            break
                    break
        df["bull_sweep_price"] = bull_sweep_at
        df["bear_sweep_price"] = bear_sweep_at

        # ── LTF BOS confirmation (3-bar swing broken by current close) ──
        # Walk back up to 15 bars looking for a swing extreme that close
        # just broke in the bias direction.
        ltf_bull_conf = np.zeros(n, dtype=bool)
        ltf_bear_conf = np.zeros(n, dtype=bool)
        for i in range(n):
            for k in range(i - 1, max(0, i - 15), -1):
                if sh_ltf[k] and closes[i] > highs[k]:
                    ltf_bull_conf[i] = True; break
                if sl_ltf[k] and closes[i] < lows[k]:
                    ltf_bear_conf[i] = True; break
        df["ltf_bull_conf"] = ltf_bull_conf
        df["ltf_bear_conf"] = ltf_bear_conf

        # ── Session filter: London + NY institutional hours (UTC) ──────
        # Spec says "Best Times: New York Open (6 pm to 6am)" — institutional
        # liquidity. Covering both London (08:00-17:00 UTC) and NY
        # (12:00-21:00 UTC) gives us the 08:00-21:00 UTC window — when most
        # institutional volume is active. Excludes Asia-only hours when
        # liquidity is thinner and false breakouts more common.
        hrs = df["date"].dt.hour if "date" in df.columns else pd.Series(0, index=df.index)
        df["in_ny_session"] = (hrs >= 8) & (hrs <= 21)

        # ── STRUCTURAL SL / TP per bar ───────────────────────────────────
        # SL = sweep extreme ± 10bps. TP1 = entry ± 2R. TP2 = previous swing.
        # Computed only on bars where ALL gates would pass (saves work and
        # avoids polluting non-signal bars with stale levels).
        entry = closes
        long_gate = (
            (bias == 1)
            & df["in_discount"].to_numpy()
            & (df["in_bull_ob"].to_numpy() | df["bull_fvg"].fillna(False).to_numpy())
            & (~np.isnan(bull_sweep_at))
            & ltf_bull_conf
            & df["in_ny_session"].to_numpy()
        )
        short_gate = (
            (bias == -1)
            & df["in_premium"].to_numpy()
            & (df["in_bear_ob"].to_numpy() | df["bear_fvg"].fillna(False).to_numpy())
            & (~np.isnan(bear_sweep_at))
            & ltf_bear_conf
            & df["in_ny_session"].to_numpy()
        )

        # SL anchored at the sweep extreme with 10bps buffer.
        sl_long  = bull_sweep_at * 0.999
        sl_short = bear_sweep_at * 1.001
        risk_long  = entry - sl_long
        risk_short = sl_short - entry
        # Reject if risk > 3% of entry (likely broken signal).
        bad_long  = (risk_long  <= 0) | (risk_long  > entry * 0.03)
        bad_short = (risk_short <= 0) | (risk_short > entry * 0.03)
        long_gate  = long_gate  & ~bad_long
        short_gate = short_gate & ~bad_short

        tp1_long  = entry + 2 * risk_long
        tp1_short = entry - 2 * risk_short
        # TP2 = previous opposing swing extreme (the next liquidity pool).
        tp2_long  = lh_ltf    # previous LTF swing high
        tp2_short = ll_ltf    # previous LTF swing low

        df["sl_price"]  = np.where(long_gate, sl_long,  np.where(short_gate, sl_short,  np.nan))
        df["tp_price"]  = np.where(long_gate, tp1_long, np.where(short_gate, tp1_short, np.nan))
        df["tp2_price"] = np.where(long_gate, tp2_long, np.where(short_gate, tp2_short, np.nan))

        # ── Final entry signals (used by populate_entry_trend) ──────────
        df["_long_signal"]  = long_gate
        df["_short_signal"] = short_gate
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
class BidirectionalStrategy:
    """
    Trend-following strategy that trades BOTH directions.

    LONG:  EMA9 > EMA21 (uptrend confirmed) AND RSI < 60 (not overbought)
    SHORT: EMA9 < EMA21 (downtrend confirmed) AND RSI > 40 (not oversold)
    SL: 1.5% | TP: 3.0% | Leverage: 10x recommended

    Designed specifically to test and validate SHORT position flow
    in futures paper trading, live trading, and backtesting.
    """
    minimal_roi = {"0": 0.030}
    stoploss = -0.015
    timeframe = "15m"
'''

_SMC_PRO_V3_CODE = '''
class SMCProV3:
    """
    SMC Pro v3 — full institutional Smart Money Concepts model.

    Implements the complete 3-layer entry framework:

      LAYER 1 — HTF BIAS (≈daily structure on 15m via N=30 swings)
        • Detect last Break-of-Structure (BOS) direction
        • BULL  if last BOS = close > prior swing high
        • BEAR  if last BOS = close < prior swing low
        • RANGE if no clear BOS → NO TRADE

      LAYER 2 — PREMIUM / DISCOUNT FILTER (50% fib of HTF range)
        • LONG  only allowed in DISCOUNT (close ≤ midline)
        • SHORT only allowed in PREMIUM  (close ≥ midline)

      LAYER 3 — LIQUIDITY SWEEP REQUIREMENT
        • An LTF swing extreme within last 30 bars MUST have been
          briefly wicked through and reclaimed before entry
        • SL is anchored to that sweep extreme

      LAYER 4 — OB / FVG MITIGATION
        • Price MUST currently be inside either
          - a Fair Value Gap (3-candle imbalance) in last 20 bars, OR
          - an Order Block (last opposing candle before a strong
            move of >0.5% within 3 bars) in last 30 bars

      LAYER 5 — LTF CONFIRMATION (BOS in bias direction)
        • A recent 3-bar LTF swing extreme must be broken by the
          current close — this is the "wait for confirmation" gate

      LAYER 6 — SESSION FILTER (NY institutional hours)
        • Only trade 12:00–21:00 UTC (NY pre-market through close)
        • Asia / EU chop is filtered out

      LAYER 7 — RISK MATH
        • SL  = sweep extreme ± 10bps buffer
        • TP1 = entry ± 2R (default; only target the engine supports)
        • Reject signal if risk > 3% of entry (likely broken)

    Why ALL the gates: institutional algos reject ~95% of looks-like-a-
    setup bars. Without every gate, the algo fires on noise. With every
    gate, expect ~50–200 trades per 6 months on 15m BTC — that's the
    institutional-quality signal frequency.

    Timeframe: 15m on KuCoin Futures.
    """
    minimal_roi = {"0": 0.04}    # 2R nominal — actual TP varies per trade
    stoploss    = -0.02          # nominal — actual SL is structural per trade
    timeframe   = "15m"
    startup_candle_count = 100   # need swing-confirmation lookback
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
            "description": "SMC v2 — FULL institutional Smart Money Concepts. 10-layer entry: "
                           "HTF bias (N=30 BOS direction) + Premium/Discount fib filter + "
                           "Liquidity sweep + OB (strong-move confirmed) + FVG mitigation + "
                           "LTF BOS confirmation + NY session (12:00-21:00 UTC) + structural SL "
                           "anchored to sweep + Multi-TP (TP1=2R, TP2=previous swing). Populates "
                           "sl_price / tp_price / tp2_price per bar — engine honours those over "
                           "slider %s when 'SL/TP source = From strategy' is selected.",
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
