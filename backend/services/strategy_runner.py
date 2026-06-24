"""
Strategy runner — executes a user-defined Freqtrade IStrategy class against a
pandas DataFrame and returns the dataframe with entry/exit signal columns
populated.

Why this exists
---------------
The user authors strategies in natural language; the LLM converts them to
Freqtrade-style Python code (an IStrategy subclass with populate_indicators,
populate_entry_trend, populate_exit_trend), stored in Strategy.generated_code.

Before this module, the futures backtester only pattern-matched on the
strategy's *name* and ran one of the hardcoded built-in signal functions
(SMC, MACD, RSI-Bollinger, etc.). Two consequences:

  1. Custom strategies were silently ignored — their generated_code never
     ran. A strategy named "My Strategy" got mapped to SimpleTarget.
  2. Strategies named after built-ins (e.g. an LLM-generated SMC variant)
     ran the hardcoded SMC function instead of the user's actual logic.

This module fixes both: when generated_code is present, we exec it in a
sandbox, instantiate the IStrategy subclass, call populate_indicators +
populate_entry_trend + populate_exit_trend, and read the
enter_long / enter_short / exit_long / exit_short signal columns. The
backtest engine then iterates those signals bar-by-bar as before.

The sandbox provides stubs for freqtrade.strategy.IStrategy and the typical
TA imports (talib, pandas_ta, qtpylib) so user code doesn't need those
packages installed on Railway.
"""
from __future__ import annotations

import logging
import re
import types
from typing import Any

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)


# Indicators the chart can render as overlays/sub-panes, mapped to the regex
# that detects their use in a strategy's generated code. Used so the UI can
# auto-show exactly the indicators a strategy actually follows (and nothing
# else — indicators stay off unless a strategy uses them or the user adds them).
_INDICATOR_PATTERNS: dict[str, str] = {
    "vwap":      r"\bVWAP\b|qtpylib\.vwap",
    "ema":       r"\bEMA\b|\bema[_0-9]",
    "sma":       r"\bSMA\b",
    "rsi":       r"\bRSI\b",
    "macd":      r"\bMACD\b",
    "bbands":    r"\bBBANDS\b|bollinger",
    "stoch":     r"\bSTOCH\b|stochastic",
    "atr":       r"\bATR\b",
    "adx":       r"\bADX\b",
    "cci":       r"\bCCI\b",
}


def detect_strategy_indicators(generated_code: str | None) -> list[str]:
    """Return the list of chart indicators a strategy's code references, e.g.
    ['vwap', 'ema', 'rsi']. Empty list when there's no code or no match. Lets
    the frontend auto-enable only the overlays a strategy actually uses."""
    if not generated_code:
        return []
    found = [name for name, pat in _INDICATOR_PATTERNS.items()
             if re.search(pat, generated_code, re.IGNORECASE)]
    return found


# ── Minimal IStrategy stub the user's code subclasses ──────────────────────
class IStrategy:
    """Mock Freqtrade IStrategy base class.

    The real IStrategy has dozens of methods and configuration hooks; we only
    need the four methods the user's code overrides. Class attributes like
    `timeframe`, `stoploss`, `minimal_roi` are read by our outer engine
    from the Strategy DB row, not from the class — so we don't need to
    honour them here.
    """

    def populate_indicators(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        return dataframe

    def populate_entry_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        return dataframe

    def populate_exit_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        return dataframe

    # Some Freqtrade strategies override the older buy/sell trend hooks.
    def populate_buy_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        return dataframe

    def populate_sell_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        return dataframe


def _build_qtpylib_stub() -> types.ModuleType:
    """Provide the handful of qtpylib helpers the LLM commonly emits."""
    mod = types.ModuleType("qtpylib")

    def crossed_above(a: pd.Series, b) -> pd.Series:
        b_series = b if isinstance(b, pd.Series) else pd.Series(b, index=a.index)
        prev_a = a.shift(1)
        prev_b = b_series.shift(1)
        return (a > b_series) & (prev_a <= prev_b)

    def crossed_below(a: pd.Series, b) -> pd.Series:
        b_series = b if isinstance(b, pd.Series) else pd.Series(b, index=a.index)
        prev_a = a.shift(1)
        prev_b = b_series.shift(1)
        return (a < b_series) & (prev_a >= prev_b)

    mod.crossed_above = crossed_above
    mod.crossed_below = crossed_below
    mod.indicators = types.ModuleType("qtpylib.indicators")
    mod.indicators.crossed_above = crossed_above
    mod.indicators.crossed_below = crossed_below
    return mod


def _build_talib_stub() -> types.ModuleType:
    """Minimal TA-Lib subset implemented in pandas. The LLM emits these names
    most often: RSI, EMA, SMA, MACD, BBANDS, ATR, ADX, STOCH."""
    mod = types.ModuleType("talib")

    def _to_series(x) -> pd.Series:
        if isinstance(x, pd.DataFrame):
            return x["close"]
        if isinstance(x, np.ndarray):
            return pd.Series(x)
        return x

    def SMA(close, timeperiod: int = 14):
        s = _to_series(close)
        return s.rolling(timeperiod).mean()

    def EMA(close, timeperiod: int = 14):
        s = _to_series(close)
        return s.ewm(span=timeperiod, adjust=False).mean()

    def RSI(close, timeperiod: int = 14):
        s = _to_series(close)
        delta = s.diff()
        gain = delta.clip(lower=0).ewm(com=timeperiod - 1, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(com=timeperiod - 1, adjust=False).mean()
        rs = gain / loss.replace(0, 1e-9)
        return 100 - 100 / (1 + rs)

    def MACD(close, fastperiod: int = 12, slowperiod: int = 26, signalperiod: int = 9):
        s = _to_series(close)
        ef = s.ewm(span=fastperiod, adjust=False).mean()
        es = s.ewm(span=slowperiod, adjust=False).mean()
        macd = ef - es
        signal = macd.ewm(span=signalperiod, adjust=False).mean()
        hist = macd - signal
        # Freqtrade strategies use `ta.MACD(df)["macd"]` (dict-style key
        # access on the returned DataFrame). Returning a tuple breaks
        # `macd["macd"]` with TypeError. DataFrame supports both styles.
        return pd.DataFrame({
            "macd":       macd,
            "macdsignal": signal,
            "macdhist":   hist,
        })

    def BBANDS(close, timeperiod: int = 20, nbdevup: float = 2, nbdevdn: float = 2, matype: int = 0):
        s = _to_series(close)
        mid = s.rolling(timeperiod).mean()
        std = s.rolling(timeperiod).std()
        # Freqtrade uses bb["upperband"], bb["middleband"], bb["lowerband"]
        return pd.DataFrame({
            "upperband":  mid + nbdevup * std,
            "middleband": mid,
            "lowerband":  mid - nbdevdn * std,
        })

    def _hlc(high, low=None, close=None):
        """Bug-fix: Freqtrade strategies often call multi-arg indicators
        with a single dataframe arg — e.g. `ta.ATR(df, timeperiod=14)`.
        Real talib requires three separate arrays (high, low, close).
        The stub now accepts EITHER convention. If low/close are None
        AND high is a DataFrame, extract from the df."""
        if isinstance(high, pd.DataFrame) and low is None and close is None:
            return high["high"], high["low"], high["close"]
        return _to_series(high), _to_series(low), _to_series(close)

    def ATR(high, low=None, close=None, timeperiod: int = 14):
        h, l, c = _hlc(high, low, close)
        tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
        return tr.rolling(timeperiod).mean()

    def ADX(high, low=None, close=None, timeperiod: int = 14):
        h, l, c = _hlc(high, low, close)
        up = h.diff()
        dn = -l.diff()
        plus_dm = up.where((up > dn) & (up > 0), 0.0)
        minus_dm = dn.where((dn > up) & (dn > 0), 0.0)
        tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1 / timeperiod, adjust=False).mean()
        plus_di = 100 * plus_dm.ewm(alpha=1 / timeperiod, adjust=False).mean() / atr.replace(0, 1e-9)
        minus_di = 100 * minus_dm.ewm(alpha=1 / timeperiod, adjust=False).mean() / atr.replace(0, 1e-9)
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-9)
        return dx.ewm(alpha=1 / timeperiod, adjust=False).mean()

    def STOCH(high, low=None, close=None,
              fastk_period: int = 5, slowk_period: int = 3, slowk_matype: int = 0,
              slowd_period: int = 3, slowd_matype: int = 0):
        h, l, c = _hlc(high, low, close)
        ll = l.rolling(fastk_period).min()
        hh = h.rolling(fastk_period).max()
        fastk = 100 * (c - ll) / (hh - ll).replace(0, 1e-9)
        slowk = fastk.rolling(slowk_period).mean()
        slowd = slowk.rolling(slowd_period).mean()
        return slowk, slowd

    def CCI(high, low=None, close=None, timeperiod: int = 20):
        """Commodity Channel Index — same convention as TA-Lib's CCI.
        Added for the Lorentzian Distance Classifier feature set (RSI/WT/
        CCI/ADX). Accepts a single df arg OR separate h/l/c arrays."""
        h, l, c = _hlc(high, low, close)
        tp = (h + l + c) / 3.0
        sma_tp = tp.rolling(timeperiod).mean()
        # Mean absolute deviation of typical price over the window.
        mad = (tp - sma_tp).abs().rolling(timeperiod).mean()
        return (tp - sma_tp) / (0.015 * mad.replace(0, 1e-9))

    def WT(high, low=None, close=None, channel_len: int = 10, average_len: int = 11):
        """WaveTrend oscillator (LazyBear). NOT a native TA-Lib function, but
        commonly used as a feature in the Lorentzian Distance Classifier.
        Returns wt1 (the smoothed tci line). `high` may be a df (hlc3 is
        derived) or the explicit high with low/close supplied."""
        h, l, c = _hlc(high, low, close)
        ap = (h + l + c) / 3.0
        esa = ap.ewm(span=channel_len, adjust=False).mean()
        d = (ap - esa).abs().ewm(span=channel_len, adjust=False).mean()
        ci = (ap - esa) / (0.015 * d.replace(0, 1e-9))
        return ci.ewm(span=average_len, adjust=False).mean()   # wt1 (tci)

    def VWAP(high, low=None, close=None, volume=None, timeperiod: int = 0):
        """Volume-Weighted Average Price — the #1 intraday/scalping reference.
        NOT in native TA-Lib, so provided here for scalp strategies.

        Accepts a single df (extracts high/low/close/volume) OR explicit
        arrays. Behaviour:
          • timeperiod == 0 (default): SESSION VWAP anchored to the UTC day if
            the df has a 'date' column (the standard intraday VWAP that resets
            each session); falls back to cumulative VWAP if no timestamp.
          • timeperiod  > 0: ROLLING VWAP over that many bars — handy on
            crypto's 24/7 tape where there is no natural session reset.
        """
        if isinstance(high, pd.DataFrame) and low is None and close is None:
            df = high
            h, l, c, v = df["high"], df["low"], df["close"], df["volume"]
            dates = df["date"] if "date" in df.columns else None
        else:
            h, l, c = _to_series(high), _to_series(low), _to_series(close)
            v = _to_series(volume) if volume is not None else pd.Series(1.0, index=h.index)
            dates = None
        tp = (h + l + c) / 3.0       # typical price
        pv = tp * v
        if timeperiod and timeperiod > 0:
            return pv.rolling(timeperiod).sum() / v.rolling(timeperiod).sum().replace(0, 1e-9)
        if dates is not None:
            day = pd.to_datetime(dates, utc=True, errors="coerce").dt.floor("D")
            return pv.groupby(day).cumsum() / v.groupby(day).cumsum().replace(0, 1e-9)
        return pv.cumsum() / v.cumsum().replace(0, 1e-9)

    def EMA_SPREAD(close, periods=(20, 50, 100, 200)):
        """EMA compression / 'gate' detector. Returns the normalized spread of
        several EMAs: (max_ema - min_ema) / close, per bar. SMALL = the EMAs are
        tightly bunched (a compression 'gate' before a breakout); large = fanned
        out (trending). Quantifies the otherwise-subjective 'EMAs stuck together'
        rule — e.g. a gate is `ta.EMA_SPREAD(df["close"]) < 0.004` (within 0.4%).
        Accepts a df (uses its close) or a close series/array, and any EMA set."""
        s = _to_series(close["close"] if isinstance(close, pd.DataFrame) else close)
        mat = pd.concat([s.ewm(span=int(p), adjust=False).mean() for p in periods], axis=1)
        return (mat.max(axis=1) - mat.min(axis=1)) / s.replace(0, 1e-9)

    # ── Candlestick patterns (TA-Lib-compatible: +100 bullish, -100 bearish,
    # 0 none) — confirmation filters for entries, e.g. only take a breakout when
    # CDLENGULFING(df) > 0. Each accepts a df OR explicit open/high/low/close.
    def _ohlc(o, h=None, l=None, c=None):
        if isinstance(o, pd.DataFrame) and h is None:
            return o["open"], o["high"], o["low"], o["close"]
        return _to_series(o), _to_series(h), _to_series(l), _to_series(c)

    def _wb(o, h, l, c):
        """body, lower-wick, upper-wick, range as Series."""
        body = (c - o).abs()
        oc_max = pd.concat([o, c], axis=1).max(axis=1)
        oc_min = pd.concat([o, c], axis=1).min(axis=1)
        return body, (oc_min - l), (h - oc_max), (h - l).replace(0, 1e-9)

    def CDLENGULFING(o, h=None, l=None, c=None):
        o, h, l, c = _ohlc(o, h, l, c)
        pb = c.shift(1) < o.shift(1); pbu = c.shift(1) > o.shift(1)
        bull = pb & (c > o) & (o <= c.shift(1)) & (c >= o.shift(1))
        bear = pbu & (c < o) & (o >= c.shift(1)) & (c <= o.shift(1))
        out = pd.Series(0, index=c.index); out[bull] = 100; out[bear] = -100
        return out

    def CDLHAMMER(o, h=None, l=None, c=None):
        o, h, l, c = _ohlc(o, h, l, c)
        body, low_w, up_w, rng = _wb(o, h, l, c)
        out = pd.Series(0, index=c.index)
        out[(low_w >= 2 * body) & (up_w <= body) & (body > 0) & (body / rng < 0.4)] = 100
        return out

    def CDLSHOOTINGSTAR(o, h=None, l=None, c=None):
        o, h, l, c = _ohlc(o, h, l, c)
        body, low_w, up_w, rng = _wb(o, h, l, c)
        out = pd.Series(0, index=c.index)
        out[(up_w >= 2 * body) & (low_w <= body) & (body > 0) & (body / rng < 0.4)] = -100
        return out

    def CDLDOJI(o, h=None, l=None, c=None):
        o, h, l, c = _ohlc(o, h, l, c)
        body, _, _, rng = _wb(o, h, l, c)
        out = pd.Series(0, index=c.index); out[(body / rng) < 0.1] = 100
        return out

    def CDLINSIDE(o, h=None, l=None, c=None):
        o, h, l, c = _ohlc(o, h, l, c)
        out = pd.Series(0, index=c.index)
        out[(h <= h.shift(1)) & (l >= l.shift(1))] = 100   # inside bar (compression)
        return out

    # ── Extended indicator set (trend / momentum / volatility / volume) ──
    # Harmless to add: a function is only executed when a strategy calls it.
    def _wma_s(s, p):
        w = np.arange(1, p + 1)
        return s.rolling(p).apply(lambda x: np.dot(x, w) / w.sum(), raw=True)

    def WMA(close, timeperiod=30):
        return _wma_s(_to_series(close), int(timeperiod))

    def DEMA(close, timeperiod=30):
        s = _to_series(close); e1 = s.ewm(span=timeperiod, adjust=False).mean()
        return 2 * e1 - e1.ewm(span=timeperiod, adjust=False).mean()

    def TEMA(close, timeperiod=30):
        s = _to_series(close); e1 = s.ewm(span=timeperiod, adjust=False).mean()
        e2 = e1.ewm(span=timeperiod, adjust=False).mean(); e3 = e2.ewm(span=timeperiod, adjust=False).mean()
        return 3 * e1 - 3 * e2 + e3

    def HMA(close, timeperiod=16):
        s = _to_series(close); n = int(timeperiod)
        return _wma_s(2 * _wma_s(s, max(1, n // 2)) - _wma_s(s, n), max(1, int(np.sqrt(n))))

    def ROC(close, timeperiod=10):
        s = _to_series(close); return (s / s.shift(timeperiod) - 1) * 100

    def MOM(close, timeperiod=10):
        s = _to_series(close); return s - s.shift(timeperiod)

    def WILLR(high, low=None, close=None, timeperiod=14):
        h, l, c = _hlc(high, low, close)
        hh = h.rolling(timeperiod).max(); ll = l.rolling(timeperiod).min()
        return -100 * (hh - c) / (hh - ll).replace(0, 1e-9)

    def MFI(high, low=None, close=None, volume=None, timeperiod=14):
        if isinstance(high, pd.DataFrame) and low is None:
            df = high; h, l, c, v = df["high"], df["low"], df["close"], df["volume"]
        else:
            h, l, c, v = _to_series(high), _to_series(low), _to_series(close), _to_series(volume)
        tp = (h + l + c) / 3.0; mf = tp * v
        pos = mf.where(tp > tp.shift(1), 0.0).rolling(timeperiod).sum()
        neg = mf.where(tp < tp.shift(1), 0.0).rolling(timeperiod).sum()
        return 100 - 100 / (1 + pos / neg.replace(0, 1e-9))

    def OBV(close, volume=None):
        if isinstance(close, pd.DataFrame) and volume is None:
            df = close; c, v = df["close"], df["volume"]
        else:
            c, v = _to_series(close), _to_series(volume)
        return (np.sign(c.diff().fillna(0)) * v).cumsum()

    def STOCHRSI(close, timeperiod=14, fastk_period=5, fastd_period=3):
        s = _to_series(close); d = s.diff()
        g = d.clip(lower=0).rolling(timeperiod).mean(); ls = (-d.clip(upper=0)).rolling(timeperiod).mean()
        rsi = 100 - 100 / (1 + g / ls.replace(0, 1e-9))
        mn = rsi.rolling(timeperiod).min(); mx = rsi.rolling(timeperiod).max()
        k = (100 * (rsi - mn) / (mx - mn).replace(0, 1e-9)).rolling(fastk_period).mean()
        return k, k.rolling(fastd_period).mean()

    def TRIX(close, timeperiod=15):
        s = _to_series(close); e1 = s.ewm(span=timeperiod, adjust=False).mean()
        e2 = e1.ewm(span=timeperiod, adjust=False).mean(); e3 = e2.ewm(span=timeperiod, adjust=False).mean()
        return (e3 / e3.shift(1) - 1) * 100

    def NATR(high, low=None, close=None, timeperiod=14):
        h, l, c = _hlc(high, low, close)
        tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
        return 100 * tr.rolling(timeperiod).mean() / c.replace(0, 1e-9)

    def TRANGE(high, low=None, close=None):
        h, l, c = _hlc(high, low, close)
        return pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)

    def STDDEV(close, timeperiod=5, nbdev=1.0):
        return _to_series(close).rolling(timeperiod).std() * nbdev

    def PPO(close, fastperiod=12, slowperiod=26):
        s = _to_series(close); f = s.ewm(span=fastperiod, adjust=False).mean()
        sl = s.ewm(span=slowperiod, adjust=False).mean()
        return 100 * (f - sl) / sl.replace(0, 1e-9)

    def KELTNER(high, low=None, close=None, timeperiod=20, mult=2.0):
        h, l, c = _hlc(high, low, close)
        mid = c.ewm(span=timeperiod, adjust=False).mean()
        tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
        atr = tr.rolling(timeperiod).mean()
        return pd.DataFrame({"upperband": mid + mult * atr, "middleband": mid, "lowerband": mid - mult * atr})

    def DONCHIAN(high, low=None, close=None, timeperiod=20):
        h, l, c = _hlc(high, low, close)
        up = h.rolling(timeperiod).max(); dn = l.rolling(timeperiod).min()
        return pd.DataFrame({"upperband": up, "middleband": (up + dn) / 2, "lowerband": dn})

    def SUPERTREND(high, low=None, close=None, timeperiod=10, multiplier=3.0):
        """Returns (supertrend_line, trend) where trend = +1 up / -1 down."""
        h, l, c = _hlc(high, low, close)
        hl2 = (h + l) / 2.0
        tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
        atr = tr.rolling(timeperiod).mean()
        ub = (hl2 + multiplier * atr).values; lb = (hl2 - multiplier * atr).values
        cv = c.values; n = len(cv)
        fub = ub.copy(); flb = lb.copy()
        for i in range(1, n):
            fub[i] = ub[i] if (ub[i] < fub[i - 1] or cv[i - 1] > fub[i - 1]) else fub[i - 1]
            flb[i] = lb[i] if (lb[i] > flb[i - 1] or cv[i - 1] < flb[i - 1]) else flb[i - 1]
        st = np.zeros(n); trend = np.ones(n); st[0] = fub[0]
        for i in range(1, n):
            if st[i - 1] == fub[i - 1]:
                st[i], trend[i] = (fub[i], -1) if cv[i] <= fub[i] else (flb[i], 1)
            else:
                st[i], trend[i] = (flb[i], 1) if cv[i] >= flb[i] else (fub[i], -1)
        return pd.Series(st, index=c.index), pd.Series(trend, index=c.index)

    # ── Extended set 2 — MAs, momentum, trend, volume, stats, Fib ──
    def TRIMA(close, timeperiod=30):
        s = _to_series(close); half = (int(timeperiod) + 1) // 2
        return s.rolling(half).mean().rolling(half).mean()

    def VWMA(close, volume=None, timeperiod=20):
        if isinstance(close, pd.DataFrame) and volume is None:
            c, v = close["close"], close["volume"]
        else:
            c, v = _to_series(close), _to_series(volume)
        return (c * v).rolling(timeperiod).sum() / v.rolling(timeperiod).sum().replace(0, 1e-9)

    def ZLEMA(close, timeperiod=20):
        s = _to_series(close); lag = (int(timeperiod) - 1) // 2
        return (s + (s - s.shift(lag))).ewm(span=timeperiod, adjust=False).mean()

    def KAMA(close, timeperiod=10, fast=2, slow=30):
        s = _to_series(close); n = int(timeperiod); vals = s.values; m = len(vals)
        chg = (s - s.shift(n)).abs(); vol = s.diff().abs().rolling(n).sum()
        er = (chg / vol.replace(0, 1e-9)).fillna(0).values
        sc = (er * (2 / (fast + 1) - 2 / (slow + 1)) + 2 / (slow + 1)) ** 2
        out = np.full(m, np.nan)
        if m > n:
            out[n] = vals[n]
            for i in range(n + 1, m):
                prev = out[i - 1] if not np.isnan(out[i - 1]) else vals[i - 1]
                out[i] = prev + sc[i] * (vals[i] - prev)
        return pd.Series(out, index=s.index)

    def CMO(close, timeperiod=14):
        s = _to_series(close); d = s.diff()
        up = d.clip(lower=0).rolling(timeperiod).sum(); dn = (-d.clip(upper=0)).rolling(timeperiod).sum()
        return 100 * (up - dn) / (up + dn).replace(0, 1e-9)

    def BOP(open_, high=None, low=None, close=None):
        o, h, l, c = _ohlc(open_, high, low, close)
        return (c - o) / (h - l).replace(0, 1e-9)

    def APO(close, fastperiod=12, slowperiod=26):
        s = _to_series(close)
        return s.ewm(span=fastperiod, adjust=False).mean() - s.ewm(span=slowperiod, adjust=False).mean()

    def ROCP(close, timeperiod=10):
        s = _to_series(close); return (s - s.shift(timeperiod)) / s.shift(timeperiod).replace(0, 1e-9)

    def ROCR(close, timeperiod=10):
        s = _to_series(close); return s / s.shift(timeperiod).replace(0, 1e-9)

    def DPO(close, timeperiod=20):
        s = _to_series(close); return s.shift(int(timeperiod) // 2 + 1) - s.rolling(timeperiod).mean()

    def TSI(close, r=25, s_=13):
        s = _to_series(close); mm = s.diff()
        m1 = mm.ewm(span=r, adjust=False).mean().ewm(span=s_, adjust=False).mean()
        a1 = mm.abs().ewm(span=r, adjust=False).mean().ewm(span=s_, adjust=False).mean()
        return 100 * m1 / a1.replace(0, 1e-9)

    def AROON(high, low=None, close=None, timeperiod=14):
        h, l, c = _hlc(high, low, close); p = int(timeperiod)
        up = h.rolling(p + 1).apply(lambda x: 100 * np.argmax(x) / p, raw=True)
        dn = l.rolling(p + 1).apply(lambda x: 100 * np.argmin(x) / p, raw=True)
        return up, dn

    def AROONOSC(high, low=None, close=None, timeperiod=14):
        up, dn = AROON(high, low, close, timeperiod); return up - dn

    def ULTOSC(high, low=None, close=None, t1=7, t2=14, t3=28):
        h, l, c = _hlc(high, low, close); pc = c.shift(1)
        bp = c - pd.concat([l, pc], axis=1).min(axis=1)
        tr = pd.concat([h, pc], axis=1).max(axis=1) - pd.concat([l, pc], axis=1).min(axis=1)
        a = lambda t: bp.rolling(t).sum() / tr.rolling(t).sum().replace(0, 1e-9)
        return 100 * (4 * a(t1) + 2 * a(t2) + a(t3)) / 7

    def PLUS_DI(high, low=None, close=None, timeperiod=14):
        h, l, c = _hlc(high, low, close); up = h.diff(); dn = -l.diff()
        plus = up.where((up > dn) & (up > 0), 0.0)
        tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1 / timeperiod, adjust=False).mean()
        return 100 * plus.ewm(alpha=1 / timeperiod, adjust=False).mean() / atr.replace(0, 1e-9)

    def MINUS_DI(high, low=None, close=None, timeperiod=14):
        h, l, c = _hlc(high, low, close); up = h.diff(); dn = -l.diff()
        minus = dn.where((dn > up) & (dn > 0), 0.0)
        tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1 / timeperiod, adjust=False).mean()
        return 100 * minus.ewm(alpha=1 / timeperiod, adjust=False).mean() / atr.replace(0, 1e-9)

    def VORTEX(high, low=None, close=None, timeperiod=14):
        h, l, c = _hlc(high, low, close)
        vmp = (h - l.shift(1)).abs(); vmm = (l - h.shift(1)).abs()
        tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
        trn = tr.rolling(timeperiod).sum().replace(0, 1e-9)
        return vmp.rolling(timeperiod).sum() / trn, vmm.rolling(timeperiod).sum() / trn

    def PSAR(high, low=None, close=None, acceleration=0.02, maximum=0.2):
        h, l, c = _hlc(high, low, close); n = len(c); hv = h.values; lv = l.values
        ps = np.zeros(n)
        if n:
            bull = True; af = acceleration; ep = hv[0]; ps[0] = lv[0]
            for i in range(1, n):
                ps[i] = ps[i - 1] + af * (ep - ps[i - 1])
                if bull:
                    if lv[i] < ps[i]:
                        bull = False; ps[i] = ep; ep = lv[i]; af = acceleration
                    elif hv[i] > ep:
                        ep = hv[i]; af = min(af + acceleration, maximum)
                else:
                    if hv[i] > ps[i]:
                        bull = True; ps[i] = ep; ep = hv[i]; af = acceleration
                    elif lv[i] < ep:
                        ep = lv[i]; af = min(af + acceleration, maximum)
        return pd.Series(ps, index=c.index)

    def AD(high, low=None, close=None, volume=None):
        if isinstance(high, pd.DataFrame) and low is None:
            df = high; h, l, c, v = df["high"], df["low"], df["close"], df["volume"]
        else:
            h, l, c, v = _to_series(high), _to_series(low), _to_series(close), _to_series(volume)
        mfm = ((c - l) - (h - c)) / (h - l).replace(0, 1e-9)
        return (mfm * v).cumsum()

    def ADOSC(high, low=None, close=None, volume=None, fast=3, slow=10):
        ad = AD(high, low, close, volume)
        return ad.ewm(span=fast, adjust=False).mean() - ad.ewm(span=slow, adjust=False).mean()

    def CMF(high, low=None, close=None, volume=None, timeperiod=20):
        if isinstance(high, pd.DataFrame) and low is None:
            df = high; h, l, c, v = df["high"], df["low"], df["close"], df["volume"]
        else:
            h, l, c, v = _to_series(high), _to_series(low), _to_series(close), _to_series(volume)
        mfv = (((c - l) - (h - c)) / (h - l).replace(0, 1e-9)) * v
        return mfv.rolling(timeperiod).sum() / v.rolling(timeperiod).sum().replace(0, 1e-9)

    def PVT(close, volume=None):
        if isinstance(close, pd.DataFrame) and volume is None:
            c, v = close["close"], close["volume"]
        else:
            c, v = _to_series(close), _to_series(volume)
        return (c.pct_change().fillna(0) * v).cumsum()

    def FORCE(close, volume=None, timeperiod=13):
        if isinstance(close, pd.DataFrame) and volume is None:
            c, v = close["close"], close["volume"]
        else:
            c, v = _to_series(close), _to_series(volume)
        return (c.diff() * v).ewm(span=timeperiod, adjust=False).mean()

    def LINEARREG_SLOPE(close, timeperiod=14):
        s = _to_series(close); p = int(timeperiod); x = np.arange(p); xm = x.mean()
        denom = ((x - xm) ** 2).sum()
        return s.rolling(p).apply(lambda y: ((x - xm) * (y - y.mean())).sum() / denom, raw=True)

    def LINEARREG(close, timeperiod=14):
        s = _to_series(close); p = int(timeperiod); x = np.arange(p); xm = x.mean()
        denom = ((x - xm) ** 2).sum()
        def _lr(y):
            mm = ((x - xm) * (y - y.mean())).sum() / denom
            return mm * (p - 1) + (y.mean() - mm * xm)
        return s.rolling(p).apply(_lr, raw=True)

    def MIDPOINT(close, timeperiod=14):
        s = _to_series(close); return (s.rolling(timeperiod).max() + s.rolling(timeperiod).min()) / 2

    def MIDPRICE(high, low=None, close=None, timeperiod=14):
        h, l, c = _hlc(high, low, close)
        return (h.rolling(timeperiod).max() + l.rolling(timeperiod).min()) / 2

    def FIB_RETRACEMENT(high, low=None, close=None, timeperiod=50):
        """Rolling Fibonacci retracement levels from the swing high/low over
        `timeperiod` bars. DataFrame cols: level_0 (low) … level_1 (high)."""
        h, l, c = _hlc(high, low, close)
        hi = h.rolling(timeperiod).max(); lo = l.rolling(timeperiod).min(); rng = hi - lo
        return pd.DataFrame({
            "level_0": lo, "level_236": lo + 0.236 * rng, "level_382": lo + 0.382 * rng,
            "level_5": lo + 0.5 * rng, "level_618": lo + 0.618 * rng,
            "level_786": lo + 0.786 * rng, "level_1": hi,
        })

    # ── More candlestick patterns ──
    def CDLMARUBOZU(o, h=None, l=None, c=None):
        o, h, l, c = _ohlc(o, h, l, c); body, lw, uw, rng = _wb(o, h, l, c)
        out = pd.Series(0, index=c.index); strong = (lw < 0.05 * rng) & (uw < 0.05 * rng) & (body > 0.7 * rng)
        out[strong & (c > o)] = 100; out[strong & (c < o)] = -100; return out

    def CDLHARAMI(o, h=None, l=None, c=None):
        o, h, l, c = _ohlc(o, h, l, c)
        pbh = pd.concat([o.shift(1), c.shift(1)], axis=1).max(axis=1)
        pbl = pd.concat([o.shift(1), c.shift(1)], axis=1).min(axis=1)
        cbh = pd.concat([o, c], axis=1).max(axis=1); cbl = pd.concat([o, c], axis=1).min(axis=1)
        inside = (cbh <= pbh) & (cbl >= pbl)
        out = pd.Series(0, index=c.index)
        out[inside & (c.shift(1) < o.shift(1)) & (c > o)] = 100
        out[inside & (c.shift(1) > o.shift(1)) & (c < o)] = -100
        return out

    def CDLHANGINGMAN(o, h=None, l=None, c=None):
        o, h, l, c = _ohlc(o, h, l, c); body, lw, uw, rng = _wb(o, h, l, c)
        out = pd.Series(0, index=c.index)
        out[(lw >= 2 * body) & (uw <= body) & (body > 0) & (body / rng < 0.4)] = -100; return out

    def CDLINVERTEDHAMMER(o, h=None, l=None, c=None):
        o, h, l, c = _ohlc(o, h, l, c); body, lw, uw, rng = _wb(o, h, l, c)
        out = pd.Series(0, index=c.index)
        out[(uw >= 2 * body) & (lw <= body) & (body > 0) & (body / rng < 0.4)] = 100; return out

    def CDLSPINNINGTOP(o, h=None, l=None, c=None):
        o, h, l, c = _ohlc(o, h, l, c); body, lw, uw, rng = _wb(o, h, l, c)
        out = pd.Series(0, index=c.index); out[(body < 0.3 * rng) & (lw > body) & (uw > body)] = 100; return out

    def CDLGRAVESTONEDOJI(o, h=None, l=None, c=None):
        o, h, l, c = _ohlc(o, h, l, c); body, lw, uw, rng = _wb(o, h, l, c)
        out = pd.Series(0, index=c.index); out[(body < 0.1 * rng) & (lw < 0.1 * rng) & (uw > 0.5 * rng)] = -100; return out

    def CDLDRAGONFLYDOJI(o, h=None, l=None, c=None):
        o, h, l, c = _ohlc(o, h, l, c); body, lw, uw, rng = _wb(o, h, l, c)
        out = pd.Series(0, index=c.index); out[(body < 0.1 * rng) & (uw < 0.1 * rng) & (lw > 0.5 * rng)] = 100; return out

    def CDLPIERCING(o, h=None, l=None, c=None):
        o, h, l, c = _ohlc(o, h, l, c); mid = (o.shift(1) + c.shift(1)) / 2
        out = pd.Series(0, index=c.index)
        out[(c.shift(1) < o.shift(1)) & (c > o) & (o < c.shift(1)) & (c > mid) & (c < o.shift(1))] = 100; return out

    def CDLDARKCLOUDCOVER(o, h=None, l=None, c=None):
        o, h, l, c = _ohlc(o, h, l, c); mid = (o.shift(1) + c.shift(1)) / 2
        out = pd.Series(0, index=c.index)
        out[(c.shift(1) > o.shift(1)) & (c < o) & (o > c.shift(1)) & (c < mid) & (c > o.shift(1))] = -100; return out

    def CDLMORNINGSTAR(o, h=None, l=None, c=None):
        o, h, l, c = _ohlc(o, h, l, c); body = (c - o).abs()
        small = body.shift(1) < body.shift(2) * 0.5
        out = pd.Series(0, index=c.index)
        out[(c.shift(2) < o.shift(2)) & small & (c > o) & (c > (o.shift(2) + c.shift(2)) / 2)] = 100; return out

    def CDLEVENINGSTAR(o, h=None, l=None, c=None):
        o, h, l, c = _ohlc(o, h, l, c); body = (c - o).abs()
        small = body.shift(1) < body.shift(2) * 0.5
        out = pd.Series(0, index=c.index)
        out[(c.shift(2) > o.shift(2)) & small & (c < o) & (c < (o.shift(2) + c.shift(2)) / 2)] = -100; return out

    def CDL3WHITESOLDIERS(o, h=None, l=None, c=None):
        o, h, l, c = _ohlc(o, h, l, c)
        out = pd.Series(0, index=c.index)
        out[(c > o) & (c.shift(1) > o.shift(1)) & (c.shift(2) > o.shift(2)) & (c > c.shift(1)) & (c.shift(1) > c.shift(2))] = 100
        return out

    def CDL3BLACKCROWS(o, h=None, l=None, c=None):
        o, h, l, c = _ohlc(o, h, l, c)
        out = pd.Series(0, index=c.index)
        out[(c < o) & (c.shift(1) < o.shift(1)) & (c.shift(2) < o.shift(2)) & (c < c.shift(1)) & (c.shift(1) < c.shift(2))] = -100
        return out

    def CDLTRISTAR(o, h=None, l=None, c=None):
        o, h, l, c = _ohlc(o, h, l, c); body = (c - o).abs(); rng = (h - l).replace(0, 1e-9)
        d = (body / rng) < 0.1
        out = pd.Series(0, index=c.index); out[d & d.shift(1) & d.shift(2)] = 100; return out

    for name, fn in dict(
        SMA=SMA, EMA=EMA, RSI=RSI, MACD=MACD, BBANDS=BBANDS,
        ATR=ATR, ADX=ADX, STOCH=STOCH, CCI=CCI, WT=WT, VWAP=VWAP,
        EMA_SPREAD=EMA_SPREAD,
        CDLENGULFING=CDLENGULFING, CDLHAMMER=CDLHAMMER,
        CDLSHOOTINGSTAR=CDLSHOOTINGSTAR, CDLDOJI=CDLDOJI, CDLINSIDE=CDLINSIDE,
        WMA=WMA, DEMA=DEMA, TEMA=TEMA, HMA=HMA, ROC=ROC, MOM=MOM,
        WILLR=WILLR, MFI=MFI, OBV=OBV, STOCHRSI=STOCHRSI, TRIX=TRIX,
        NATR=NATR, TRANGE=TRANGE, STDDEV=STDDEV, PPO=PPO,
        KELTNER=KELTNER, DONCHIAN=DONCHIAN, SUPERTREND=SUPERTREND,
        TRIMA=TRIMA, VWMA=VWMA, ZLEMA=ZLEMA, KAMA=KAMA, CMO=CMO, BOP=BOP,
        APO=APO, ROCP=ROCP, ROCR=ROCR, DPO=DPO, TSI=TSI, AROON=AROON,
        AROONOSC=AROONOSC, ULTOSC=ULTOSC, PLUS_DI=PLUS_DI, MINUS_DI=MINUS_DI,
        VORTEX=VORTEX, PSAR=PSAR, AD=AD, ADOSC=ADOSC, CMF=CMF, PVT=PVT,
        FORCE=FORCE, LINEARREG=LINEARREG, LINEARREG_SLOPE=LINEARREG_SLOPE,
        MIDPOINT=MIDPOINT, MIDPRICE=MIDPRICE, FIB_RETRACEMENT=FIB_RETRACEMENT,
        CDLMARUBOZU=CDLMARUBOZU, CDLHARAMI=CDLHARAMI, CDLHANGINGMAN=CDLHANGINGMAN,
        CDLINVERTEDHAMMER=CDLINVERTEDHAMMER, CDLSPINNINGTOP=CDLSPINNINGTOP,
        CDLGRAVESTONEDOJI=CDLGRAVESTONEDOJI, CDLDRAGONFLYDOJI=CDLDRAGONFLYDOJI,
        CDLPIERCING=CDLPIERCING, CDLDARKCLOUDCOVER=CDLDARKCLOUDCOVER,
        CDLMORNINGSTAR=CDLMORNINGSTAR, CDLEVENINGSTAR=CDLEVENINGSTAR,
        CDL3WHITESOLDIERS=CDL3WHITESOLDIERS, CDL3BLACKCROWS=CDL3BLACKCROWS,
        CDLTRISTAR=CDLTRISTAR,
    ).items():
        setattr(mod, name, fn)
    return mod


def _build_sandbox() -> dict[str, Any]:
    """Construct the namespace the user's strategy code is exec'd in."""
    # Freqtrade replacement modules
    freqtrade = types.ModuleType("freqtrade")
    freqtrade.strategy = types.ModuleType("freqtrade.strategy")
    freqtrade.strategy.IStrategy = IStrategy
    freqtrade.strategy.IntParameter = lambda *a, **kw: a[0] if a else 0
    freqtrade.strategy.DecimalParameter = lambda *a, **kw: a[0] if a else 0.0
    freqtrade.strategy.CategoricalParameter = lambda *a, **kw: (a[0][0] if a and a[0] else None)
    freqtrade.strategy.BooleanParameter = lambda *a, **kw: a[0] if a else False
    freqtrade.exchange = types.ModuleType("freqtrade.exchange")
    freqtrade.exchange.timeframe_to_minutes = lambda tf: {"1m": 1, "5m": 5, "15m": 15, "30m": 30,
                                                          "1h": 60, "4h": 240, "1d": 1440}.get(tf, 15)
    freqtrade.persistence = types.ModuleType("freqtrade.persistence")

    class _Trade:
        pass
    freqtrade.persistence.Trade = _Trade

    return {
        # When Python defines a class, it reads __name__ from the current
        # module's globals to set the class's __module__ attribute. Setting
        # this lets us cleanly distinguish user-defined classes from
        # imported ones when we hunt for the IStrategy subclass.
        "__name__":    "user_strategy",
        # Standard libs the LLM uses
        "pd":          pd,
        "pandas":      pd,
        "np":          np,
        "numpy":       np,
        "DataFrame":   pd.DataFrame,
        "Series":      pd.Series,
        # Stubs in import-able form via __import__ hook below
        "__builtins__": _safe_builtins(freqtrade),
        # Direct top-level injections so `IStrategy` etc. resolve even if the
        # user wrote `from freqtrade.strategy import IStrategy`.
        "IStrategy":   IStrategy,
    }


def _safe_builtins(freqtrade_mod) -> dict[str, Any]:
    """Restricted builtins. Block file/network/process access but let the
    user's strategy import the standard scientific-Python ecosystem and our
    freqtrade stub."""
    import builtins as _b
    allowed = {
        # Common safe names
        "abs": _b.abs, "all": _b.all, "any": _b.any, "bool": _b.bool,
        "dict": _b.dict, "enumerate": _b.enumerate, "filter": _b.filter,
        "float": _b.float, "int": _b.int, "isinstance": _b.isinstance,
        "len": _b.len, "list": _b.list, "map": _b.map, "max": _b.max,
        "min": _b.min, "next": _b.next, "print": _b.print, "range": _b.range,
        "round": _b.round, "set": _b.set, "slice": _b.slice, "sorted": _b.sorted,
        "str": _b.str, "sum": _b.sum, "tuple": _b.tuple, "type": _b.type,
        "zip": _b.zip, "iter": _b.iter, "object": _b.object, "True": True,
        "False": False, "None": None, "getattr": _b.getattr, "setattr": _b.setattr,
        "hasattr": _b.hasattr, "Exception": _b.Exception, "ValueError": _b.ValueError,
        "TypeError": _b.TypeError, "KeyError": _b.KeyError, "AttributeError": _b.AttributeError,
        "min": _b.min, "max": _b.max, "callable": _b.callable, "vars": _b.vars,
        "property": _b.property, "super": _b.super, "staticmethod": _b.staticmethod,
        "classmethod": _b.classmethod, "id": _b.id, "repr": _b.repr,
        # Class machinery — Python's `class Foo: ...` syntax compiles to a
        # call to __build_class__. Without it, every IStrategy subclass
        # definition fails with "__build_class__ not found".
        "__build_class__": _b.__build_class__,
        "__name__": "user_strategy",
        # Additional commonly-used safe builtins by LLM-generated code.
        "frozenset": _b.frozenset, "format": _b.format, "ord": _b.ord,
        "chr": _b.chr, "divmod": _b.divmod, "pow": _b.pow, "hex": _b.hex,
        "oct": _b.oct, "bin": _b.bin, "complex": _b.complex, "bytes": _b.bytes,
        "bytearray": _b.bytearray, "reversed": _b.reversed,
        "RuntimeError": _b.RuntimeError, "ZeroDivisionError": _b.ZeroDivisionError,
        "IndexError": _b.IndexError, "StopIteration": _b.StopIteration,
        "NotImplementedError": _b.NotImplementedError,
        "NotImplemented": _b.NotImplemented,
    }

    original_import = _b.__import__
    talib_stub = _build_talib_stub()
    qtpylib_stub = _build_qtpylib_stub()
    # `talib.abstract` is the most common import in Freqtrade strategies
    # (e.g. `import talib.abstract as ta`). Without exposing the same stub
    # under that submodule name, the import fails → user's whole strategy
    # is rejected and the engine falls back to a name-matched built-in
    # with stale DB-default SL/TP. Aliasing the submodule to the same
    # stub object means every `ta.EMA(...)` / `ta.RSI(...)` works.
    talib_stub.abstract = talib_stub
    safe_modules = {
        "freqtrade":              freqtrade_mod,
        "freqtrade.strategy":     freqtrade_mod.strategy,
        "freqtrade.exchange":     freqtrade_mod.exchange,
        "freqtrade.persistence":  freqtrade_mod.persistence,
        "talib":                  talib_stub,
        "talib.abstract":         talib_stub,   # import talib.abstract as ta
        "pandas_ta":              talib_stub,   # close enough for most LLM-emitted code
        "qtpylib":                qtpylib_stub,
        "qtpylib.indicators":     qtpylib_stub.indicators,
        "technical":              types.ModuleType("technical"),
        "technical.indicators":   types.ModuleType("technical.indicators"),
    }

    def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
        # Standard scientific stack: pandas, numpy, math, datetime are fine.
        if name in ("pandas", "numpy", "math", "datetime", "typing",
                    "decimal", "functools", "itertools"):
            return original_import(name, globals, locals, fromlist, level)
        if name in safe_modules:
            return safe_modules[name]
        # Try fall-through to real import for harmless modules. If it's
        # something dangerous (os, subprocess, socket, requests, etc.) we
        # raise so the strategy fails fast and visibly.
        BANNED = {"os", "sys", "subprocess", "socket", "ssl", "requests",
                  "urllib", "urllib2", "urllib3", "httpx", "asyncio",
                  "shutil", "pathlib", "tempfile", "io", "ctypes"}
        if name.split(".")[0] in BANNED:
            raise ImportError(f"Module '{name}' is not available in the strategy sandbox")
        try:
            return original_import(name, globals, locals, fromlist, level)
        except ImportError:
            raise

    allowed["__import__"] = safe_import
    return allowed


def evaluate_strategy(
    generated_code: str,
    df: pd.DataFrame,
    *,
    pair: str = "BTC/USDT",
    execution_tf: str = "15m",
    historical_anchor_ts: int | None = None,
    overrides: dict | None = None,
) -> pd.DataFrame:
    """Run the user's IStrategy code against `df` and return a copy with
    signal columns added: enter_long, enter_short, exit_long, exit_short.

    Raises RuntimeError with a clear message on any failure so the caller
    can surface it to the user.

    Multi-TF context (PDF §5)
    -------------------------
    When the user's strategy class declares `bias_timeframes = ["1h", "4h"]`,
    the analyzer pre-fetches closed-only HTF candles BEFORE populate_indicators
    runs and exposes them via `metadata['htf']`. Strategies that don't
    declare it see no change (metadata['htf'] is just empty).

    For backtests pass `historical_anchor_ts` (epoch seconds of the
    current bar) so HTF candles are clipped to the past — prevents
    future-data peek when running the same strategy through backtest.
    """
    if not generated_code or not generated_code.strip():
        raise RuntimeError("strategy has no generated_code to execute")

    sandbox = _build_sandbox()
    try:
        exec(compile(generated_code, "<user_strategy>", "exec"), sandbox)
    except Exception as e:
        raise RuntimeError(f"Strategy code failed to import: {e}")

    # Find the IStrategy subclass in the sandbox namespace.
    # We walk the MRO and look for ANY ancestor named "IStrategy" rather
    # than strict identity-equality against our stub. The LLM-generated
    # code sometimes imports IStrategy from a slightly different path
    # (freqtrade.strategy.interface, freqtrade.strategy.istrategy, etc.)
    # which would create a different IStrategy object that fails an
    # identity-based issubclass check — even though semantically it's
    # the same Freqtrade interface.
    strategy_cls = None
    user_classes: list[tuple[str, type]] = []
    # Set of class identities we INJECTED into the sandbox (so we can skip
    # them when iterating to find the user's class).
    injected_ids = {id(IStrategy)}
    for name, val in sandbox.items():
        if not isinstance(val, type):
            continue
        if id(val) in injected_ids:
            continue
        # Heuristic: skip anything that came in via the typing module or
        # similar import side-effects. We only care about classes whose
        # module is unknown (created in user code) or whose MRO mentions
        # IStrategy.
        mro_names = [c.__name__ for c in val.__mro__]
        if "IStrategy" in mro_names:
            strategy_cls = val
            break
        # Track non-IStrategy classes too — used as last-resort fallback.
        if getattr(val, "__module__", None) in (None, "__main__", "user_strategy", "<user_strategy>"):
            user_classes.append((name, val))

    # Fallback: if no IStrategy subclass found but exactly one user class
    # is defined, use it. Covers the case where the LLM forgot the explicit
    # subclass or used a different base class name.
    if strategy_cls is None and len(user_classes) == 1:
        strategy_cls = user_classes[0][1]

    if strategy_cls is None:
        defined = [n for n, _ in user_classes] or ["<none>"]
        raise RuntimeError(
            f"strategy code does not define a class subclassing IStrategy. "
            f"Classes found: {', '.join(defined)}"
        )

    try:
        instance = strategy_cls()
    except Exception as e:
        raise RuntimeError(f"Strategy class could not be instantiated: {e}")

    # ── UI flag overrides ──────────────────────────────────────────────
    # The UI can flip a strategy's boolean/scalar options (e.g. StrategyAsh's
    # `use_exit_signals` CHoCH exit, or the LDC's `USE_DYNAMIC_EXITS` /
    # `USE_ATR_STOPS`) without editing code, by passing
    # overrides={'flags': {<ATTR_NAME>: <value>, ...}}. We apply them onto the
    # INSTANCE right after construction, so both populate_* (which read self.X)
    # and the class-attr surfacing below reflect the toggle. Only attributes
    # the strategy already defines are set, so a stray key can't inject state.
    if isinstance(overrides, dict):
        _flags = overrides.get("flags")
        if isinstance(_flags, dict):
            for _fk, _fv in _flags.items():
                if hasattr(instance, _fk):
                    try:
                        setattr(instance, _fk, _fv)
                    except Exception:
                        pass

    work = df.copy()
    # Freqtrade convention uses `volume`; our KuCoin loader uses `vol`.
    # Alias before the user's code runs, otherwise any reference to
    # dataframe["volume"] raises KeyError → strategy_runner falls back to
    # name-matched built-in → user's edits silently ignored.
    if "vol" in work.columns and "volume" not in work.columns:
        work["volume"] = work["vol"]
    metadata = {"pair": pair, "execution_tf": execution_tf}
    # Per-bot overrides (session window, equal-price threshold, etc.) get
    # surfaced to populate_indicators via metadata so strategies can read
    # them with a class-default fallback. Strategies that don't look at
    # metadata['overrides'] are unaffected.
    if overrides:
        metadata["overrides"] = dict(overrides)

    # ── Multi-TF Analyzer (PDF §5) ────────────────────────────────────
    # Opt-in: strategies that declare `bias_timeframes = ["1h","4h"]`
    # get closed HTF candles in metadata['htf']. Strategies without it
    # see metadata['htf'] = {} — fully backward compatible.
    try:
        from backend.services import mtf_analyzer
        htf_map = mtf_analyzer.attach_htf_context(
            strategy_instance     = instance,
            pair                  = pair,
            execution_tf          = execution_tf,
            metadata              = metadata,
            df                    = work,
            historical_anchor_ts  = historical_anchor_ts,
        )
        if htf_map:
            work.attrs["bias_timeframes"] = list(htf_map.keys())
    except Exception as _mtf_exc:
        # Never block strategy execution on MTF fetch issues.
        log.debug("mtf_analyzer attach failed (continuing without HTF context): %s", _mtf_exc)

    # Diagnostic: log the user-defined methods on their strategy so we can
    # see in Railway logs what entry/exit hooks they actually have. This
    # is invaluable when their class uses non-Freqtrade conventions.
    user_methods = [m for m in dir(instance)
                    if not m.startswith("_") and callable(getattr(instance, m, None))]
    log.info("strategy=%s methods=%s", strategy_cls.__name__, user_methods)

    # Be defensive: not every LLM-emitted strategy inherits from OUR
    # IStrategy stub (the import may resolve to a different object than
    # we provide). In that case populate_* methods won't be inherited and
    # we have to skip the ones the user didn't define on their class.
    def _call_if_exists(method_name: str, what: str):
        nonlocal work
        method = getattr(instance, method_name, None)
        if method is None or not callable(method):
            return  # method not defined on this strategy — skip
        try:
            result = method(work, metadata)
            if isinstance(result, pd.DataFrame):
                work = result
        except Exception as e:
            raise RuntimeError(f"{method_name} failed: {e}")

    # Detect "non-Freqtrade" strategies: classes that don't define any of
    # the populate_* hooks the engine knows how to call. These are usually
    # built-in Python ports (Pine Script translations etc.) authored to
    # run via the name-pattern path (_guess_strategy) — NOT to be exec'd
    # as IStrategy subclasses. Raise so the caller can gracefully fall
    # back to the name-matched signal function.
    HOOKS = ("populate_indicators", "populate_entry_trend",
             "populate_buy_trend",  "populate_exit_trend",
             "populate_sell_trend")
    available = [h for h in HOOKS if callable(getattr(instance, h, None))]
    if not available:
        raise RuntimeError(
            f"Strategy class '{strategy_cls.__name__}' has none of the "
            f"populate_* hooks (populate_indicators / populate_entry_trend / "
            f"populate_buy_trend / populate_exit_trend). This looks like a "
            f"Python-class strategy designed for the built-in signal-pattern "
            f"path — falling back to the name-matched signal function."
        )

    _call_if_exists("populate_indicators", "indicators")
    # Try the new-style entry/exit hooks first, fall back to old buy/sell.
    if hasattr(instance, "populate_entry_trend"):
        _call_if_exists("populate_entry_trend", "entry signals")
    if hasattr(instance, "populate_buy_trend"):
        _call_if_exists("populate_buy_trend", "buy signals (legacy)")
        if "buy" in work.columns and "enter_long" not in work.columns:
            work["enter_long"] = work["buy"]
    if hasattr(instance, "populate_exit_trend"):
        _call_if_exists("populate_exit_trend", "exit signals")
    if hasattr(instance, "populate_sell_trend"):
        _call_if_exists("populate_sell_trend", "sell signals (legacy)")
        if "sell" in work.columns and "exit_long" not in work.columns:
            work["exit_long"] = work["sell"]

    # ── Fallback column-name detection ─────────────────────────────────
    # If the strategy didn't fill enter_long/enter_short directly, look
    # for common Freqtrade-historical column names and map them. Order:
    # newer naming wins.
    _COL_ALIASES = {
        "enter_long":  ["enter_long",  "buy",  "long",  "signal_long",  "go_long",  "entry_long"],
        "enter_short": ["enter_short", "sell_short", "short", "signal_short", "go_short", "entry_short"],
        "exit_long":   ["exit_long",   "sell", "close_long",  "exit_long_signal"],
        "exit_short":  ["exit_short",  "exit_short_signal", "close_short"],
    }
    for target, candidates in _COL_ALIASES.items():
        if target in work.columns and (work[target].fillna(0).astype(int) != 0).any():
            continue   # already populated by user code
        for alt in candidates:
            if alt == target:
                continue
            if alt in work.columns and (work[alt].fillna(0).astype(int) != 0).any():
                # Bool/numeric-friendly copy. Keep both columns alive for
                # downstream debugging, just mirror the values.
                work[target] = work[alt].fillna(0).astype(int)
                break

    # Default any still-missing signal columns to 0 so the engine doesn't KeyError.
    for col in ("enter_long", "enter_short", "exit_long", "exit_short"):
        if col not in work.columns:
            work[col] = 0

    # Diagnostics for the UI: which columns have any non-zero values?
    # When entries fired = 0, the user can see if their strategy populated
    # `buy_signal` instead of `enter_long` etc.
    non_zero_cols = []
    for col in work.columns:
        if col in ("date", "open", "high", "low", "close", "vol"):
            continue
        try:
            if work[col].dtype.kind in "biu" or work[col].dtype == bool:
                if (work[col].fillna(0).astype(int) != 0).any():
                    non_zero_cols.append(col)
            elif work[col].dtype.kind == "f":
                # Floats: include only if it looks like a binary signal
                # (only 0s and 1s) — otherwise it's an indicator value.
                vals = work[col].dropna().unique()
                if len(vals) <= 3 and set(vals).issubset({0.0, 1.0, -1.0}):
                    if (work[col].fillna(0).astype(int) != 0).any():
                        non_zero_cols.append(col)
        except Exception:
            pass

    # Attach a non-data attribute we can inspect in the backtester to
    # surface "which methods did the user define" diagnostics back to UI.
    work.attrs["strategy_methods"] = user_methods
    work.attrs["strategy_class"]   = strategy_cls.__name__
    work.attrs["signal_columns"]   = non_zero_cols

    # Extract the strategy's OWN risk parameters so the backtester can use
    # them instead of stale DB defaults. The Strategy SQLAlchemy model
    # carries column defaults (stoploss=-0.03, take_profit=0.015) that
    # leak through whenever a row was created without explicit values,
    # silently flipping a 1:3 RR strategy into a 1:0.5 RR run that's
    # mathematically guaranteed to lose. The class is the source of truth
    # — but only when its values are SANE. Some strategies use
    # placeholder values like stoploss=-0.99 (a Freqtrade idiom for
    # "no engine-level stop, use custom_stoploss") or minimal_roi={"0": 100}
    # (a placeholder for "ROI handled by custom_exit"). Taking those
    # literally would mean liquidating every trade on the first bar or
    # never taking profit. Sanity-cap: only honour declared values that
    # are inside a realistic retail-trading band.
    SANE_SL_MIN, SANE_SL_MAX = 0.001, 0.25   # 0.1% to 25%
    SANE_TP_MIN, SANE_TP_MAX = 0.001, 0.50   # 0.1% to 50%

    cls_stoploss = getattr(strategy_cls, "stoploss", None)
    if isinstance(cls_stoploss, (int, float)) and cls_stoploss != 0:
        sl_abs = abs(float(cls_stoploss))
        if SANE_SL_MIN <= sl_abs <= SANE_SL_MAX:
            work.attrs["class_stoploss_pct"] = sl_abs * 100
        else:
            work.attrs["class_stoploss_ignored"] = (
                f"{sl_abs*100:.1f}% — outside sane range [{SANE_SL_MIN*100}%–{SANE_SL_MAX*100}%]"
            )

    cls_roi = getattr(strategy_cls, "minimal_roi", None)
    if isinstance(cls_roi, dict) and cls_roi:
        # minimal_roi maps minute-since-entry → required profit fraction.
        # The "0" key is the IMMEDIATE take-profit; use it as the static TP.
        # (Time-decayed ROIs are a Freqtrade concept our engine doesn't
        # honour bar-by-bar — picking the t=0 value is the closest
        # approximation that preserves the strategy author's intent.)
        roi_at_zero = cls_roi.get("0") or cls_roi.get(0)
        if isinstance(roi_at_zero, (int, float)) and roi_at_zero > 0:
            tp_abs = float(roi_at_zero)
            if SANE_TP_MIN <= tp_abs <= SANE_TP_MAX:
                work.attrs["class_take_profit_pct"] = tp_abs * 100
            else:
                work.attrs["class_take_profit_ignored"] = (
                    f"{tp_abs*100:.1f}% — outside sane range [{SANE_TP_MIN*100}%–{SANE_TP_MAX*100}%]"
                )

    # Trade-limit class attrs — surfaced to the validator so it can use
    # the strategy's declared values (when present) instead of the safe
    # "999/day, 0 cooldown" defaults. A strategy can opt into a hard
    # cap via:    max_trades_per_day = 5   # in the class body
    cls_max_trades = getattr(strategy_cls, "max_trades_per_day", None)
    if isinstance(cls_max_trades, int) and 1 <= cls_max_trades <= 1000:
        work.attrs["class_max_trades_per_day"] = cls_max_trades
    cls_cooldown = getattr(strategy_cls, "cooldown_candles", None)
    if isinstance(cls_cooldown, int) and 0 <= cls_cooldown <= 100:
        work.attrs["class_cooldown_candles"] = cls_cooldown
    # Optional risk gates (added 2026-05-24) — opt-in per strategy.
    # max_hold_candles: force-close trades open longer than N bars.
    # max_stops_per_day: halt new entries after N stops today.
    # Both default to 0 (disabled) in the engine when not set, so
    # strategies that don't declare these get unchanged behaviour.
    # Read from the INSTANCE so a UI flag override of max_hold_candles takes
    # effect (e.g. the LDC's "Bar hold" control: 0 = disable the fixed exit
    # and let trades run on signal flips / kernel / SL-TP).
    cls_max_hold = getattr(instance, "max_hold_candles", None)
    if isinstance(cls_max_hold, int) and 1 <= cls_max_hold <= 5000:
        work.attrs["class_max_hold_candles"] = cls_max_hold
    cls_max_stops = getattr(strategy_cls, "max_stops_per_day", None)
    if isinstance(cls_max_stops, int) and 1 <= cls_max_stops <= 100:
        work.attrs["class_max_stops_per_day"] = cls_max_stops
    # Deep-history strategies (e.g. the Lorentzian Distance Classifier, which
    # wants Max Bars Back = 2000 for its nearest-neighbour training set) can
    # declare:    max_bars_back = 2000   # in the class body
    # The live/paper engine reads this once at start and paginates KuCoin's
    # 200-cap kline endpoint to fetch that many candles. Default-absent =
    # engine keeps its lightweight 200-bar fetch, so existing strategies are
    # completely unchanged.
    cls_max_bars = getattr(strategy_cls, "max_bars_back", None)
    if isinstance(cls_max_bars, int) and 200 < cls_max_bars <= 5000:
        work.attrs["class_max_bars_back"] = cls_max_bars
    # Opt-in explicit exit signals (exit_long / exit_short). Strategies that
    # declare:    use_exit_signals = True
    # get their exit columns honoured by the engine + backtester (close-to-
    # flat without a reverse). Absent/False = legacy behaviour (exits only via
    # SL/TP, max-hold, stop-and-reverse), so existing strategies are unchanged.
    # Read from the INSTANCE (not the class) so a UI flag override of
    # use_exit_signals is reflected in what the engine/backtester honours.
    cls_use_exits = getattr(instance, "use_exit_signals", None)
    if cls_use_exits is True:
        work.attrs["class_use_exit_signals"] = True
    # Sub-bar timeframes for lower-TF analysis (e.g. liquidity sweep on
    # 1m when execution_tf=5m). Symmetric to bias_timeframes but for
    # the downward direction. Only fetched when declared.
    cls_sub_tfs = getattr(strategy_cls, "sub_timeframes", None)
    if isinstance(cls_sub_tfs, (list, tuple)):
        work.attrs["class_sub_timeframes"] = list(cls_sub_tfs)
    return work


def make_signal_fn_from_df(df: pd.DataFrame, leverage: int,
                           stoploss_pct: float, take_profit_pct: float,
                           force_slider: bool = False):
    """Adapter: given a dataframe that already has enter_long/enter_short
    columns populated, return a signal_fn(df, i) → (entry_px, sl, tp, dir)
    matching the existing engine's expected shape.

    Per-trade SL/TP source priority (highest wins):
      1. `sl_price` / `tp_price` columns populated by the user's strategy
         in populate_indicators — these are the strategy's *structural*
         per-bar levels (e.g. SMCStrategyTV computes SL from the last
         confirmed pivot and TP = entry ± 2R). When non-NaN at the signal
         bar, the engine honours them and the slider %s are ignored.
      2. Slider stoploss_pct / take_profit_pct from the UI — used as a
         fallback whenever the strategy didn't populate the columns
         (typical for fixed-% strategies like EmaScalping). This is also
         what gets used when the user explicitly picks "From sliders"
         in the SL/TP source toggle (the engine sets force_slider_sltp
         which makes the futures backtester rebuild SL/TP from sliders
         AFTER the signal fn returns — see futures_backtester.py).
    """
    enter_long = df["enter_long"].astype(int).values if "enter_long" in df.columns else None
    enter_short = df["enter_short"].astype(int).values if "enter_short" in df.columns else None
    # Optional explicit EXIT columns. Attached to the returned signal_fn so
    # an opt-in engine/backtester can close-to-flat when they fire. Strategies
    # that don't populate them leave these None → no exit-signal behaviour.
    exit_long  = df["exit_long"].astype(int).values  if "exit_long"  in df.columns else None
    exit_short = df["exit_short"].astype(int).values if "exit_short" in df.columns else None

    # Pull strategy-populated structural SL/TP columns into numpy arrays
    # once, so the bar-by-bar signal_fn doesn't pay dict-lookup cost on
    # every call. `sl_price` / `tp_price` are TP1; `tp2_price` is the
    # optional multi-TP second target (e.g. SMC's previous swing extreme).
    sl_col  = df["sl_price"].to_numpy()  if "sl_price"  in df.columns else None
    tp_col  = df["tp_price"].to_numpy()  if "tp_price"  in df.columns else None
    tp2_col = df["tp2_price"].to_numpy() if "tp2_price" in df.columns else None
    # Optional strategy-specified EXACT entry level (e.g. the 10-EMA, or a Fib
    # 0.25 retracement). When populated, the engine treats it as a limit price:
    # combined with maker-only entry it fills at this exact level if the bar
    # touched it — instead of approximating with the signal bar's close. Lets
    # "enter at <level>" strategies backtest at the price they'd really fill.
    entry_col = df["entry_price"].to_numpy() if "entry_price" in df.columns else None

    def _entry_price_at(i: int, fallback: float) -> float:
        if entry_col is not None and i < len(entry_col):
            ev = entry_col[i]
            if ev is not None and not (isinstance(ev, float) and np.isnan(ev)) and float(ev) > 0:
                return float(ev)
        return fallback

    def _structural_or_slider(entry: float, direction: str, i: int):
        """Return (sl, tp1, tp2_or_None) for this bar — structural if the
        strategy populated valid (non-NaN, positive) values; otherwise
        slider-based. tp2 is only populated when the strategy returned a
        valid second target and direction validates."""
        # Force-slider: ignore structural columns and use the slider %s for
        # EVERY trade (live/paper equivalent of the backtest's "From sliders
        # below"). No TP2 — ARM derives TP1/TP2 from this single TP downstream.
        if force_slider:
            if direction == "long":
                return entry * (1 - stoploss_pct / 100), entry * (1 + take_profit_pct / 100), None
            return entry * (1 + stoploss_pct / 100), entry * (1 - take_profit_pct / 100), None
        # Try strategy-populated columns first.
        if sl_col is not None and tp_col is not None and i < len(sl_col):
            sl_v = sl_col[i]
            tp_v = tp_col[i]
            tp2_v = tp2_col[i] if tp2_col is not None and i < len(tp2_col) else None
            if (sl_v is not None and tp_v is not None
                and not (isinstance(sl_v, float) and np.isnan(sl_v))
                and not (isinstance(tp_v, float) and np.isnan(tp_v))
                and float(sl_v) > 0 and float(tp_v) > 0):
                # Sanity-check direction.
                sl_f, tp_f = float(sl_v), float(tp_v)
                tp2_f: float | None = None
                if (tp2_v is not None
                    and not (isinstance(tp2_v, float) and np.isnan(tp2_v))
                    and float(tp2_v) > 0):
                    tp2_candidate = float(tp2_v)
                    # TP2 must be FURTHER from entry than TP1 in the same
                    # direction; otherwise treat as missing.
                    if direction == "long" and tp2_candidate > tp_f:
                        tp2_f = tp2_candidate
                    elif direction == "short" and tp2_candidate < tp_f:
                        tp2_f = tp2_candidate
                if direction == "long" and sl_f < entry and tp_f > entry:
                    return sl_f, tp_f, tp2_f
                if direction == "short" and sl_f > entry and tp_f < entry:
                    return sl_f, tp_f, tp2_f
        # Fallback: slider %s, no TP2.
        if direction == "long":
            return entry * (1 - stoploss_pct / 100), entry * (1 + take_profit_pct / 100), None
        return entry * (1 + stoploss_pct / 100), entry * (1 - take_profit_pct / 100), None

    def signal_fn(_df, i):
        # Edge-only firing (matches TradingView's strategy.entry behaviour):
        # we treat a signal as fired only on the bar where the condition
        # *transitions* from False→True.
        #
        # Returns either a 4-tuple (entry, sl, tp, dir) — single-TP — or a
        # 5-tuple (entry, sl, tp1, tp2, dir) when the strategy populated
        # a valid tp2_price. The engine handles both shapes.
        if enter_long is not None and i < len(enter_long) and enter_long[i]:
            prev = enter_long[i - 1] if i > 0 else 0
            if not prev:
                entry = _entry_price_at(i, float(_df.iloc[i]["close"]))
                sl, tp1, tp2 = _structural_or_slider(entry, "long", i)
                if tp2 is not None:
                    return entry, sl, tp1, tp2, "long"
                return entry, sl, tp1, "long"
        if enter_short is not None and i < len(enter_short) and enter_short[i]:
            prev = enter_short[i - 1] if i > 0 else 0
            if not prev:
                entry = _entry_price_at(i, float(_df.iloc[i]["close"]))
                sl, tp1, tp2 = _structural_or_slider(entry, "short", i)
                if tp2 is not None:
                    return entry, sl, tp1, tp2, "short"
                return entry, sl, tp1, "short"
        return None

    # Expose the raw exit arrays so an opt-in engine/backtester can close a
    # held position the moment its exit column fires (close-to-flat, no
    # reverse). Strategies without exit columns leave these None.
    signal_fn.exit_long  = exit_long    # type: ignore[attr-defined]
    signal_fn.exit_short = exit_short   # type: ignore[attr-defined]
    return signal_fn
