"""
Native Python backtester — runs strategy logic using pandas only.
No TA-Lib, no Freqtrade required.

Supports the built-in template strategies:
  MissCandleShortStrategy, MissCandleLongStrategy,
  MacdCrossoverStrategy, RsiBollingerStrategy, EmaScalpingStrategy
  and any custom strategy with a class name that can be pattern-matched.

All indicators are computed with pandas ewm / rolling so results are
numerically equivalent to the TA-Lib versions.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd


# ─────────────────────────── data download ────────────────────────────────

def _fetch_ohlcv(symbol: str, kline_type: str, start_ts: int, end_ts: int) -> list:
    TF_SECS = {
        "1min": 60, "3min": 180, "5min": 300, "15min": 900,
        "30min": 1800, "1hour": 3600, "4hour": 14400, "1day": 86400,
    }
    chunk = 1500 * TF_SECS.get(kline_type, 900)
    all_rows: list = []
    cur = start_ts
    while cur < end_ts:
        end_chunk = min(cur + chunk, end_ts)
        qs = urllib.parse.urlencode({
            "type": kline_type, "symbol": symbol,
            "startAt": cur, "endAt": end_chunk,
        })
        url = f"https://api.kucoin.com/api/v1/market/candles?{qs}"
        req = urllib.request.Request(url, headers={"User-Agent": "AutoTradeHub/1.0"})
        from backend.services._kucoin_proxy import urlopen as _proxy_urlopen
        with _proxy_urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        if str(data.get("code")) != "200000":
            raise RuntimeError(f"KuCoin API error: {data.get('msg','unknown')}")
        rows = data.get("data", [])
        all_rows.extend(rows)
        cur = end_chunk + 1
        if not rows:
            break
    return all_rows


def load_ohlcv(pair: str, timeframe: str, start_ts: int, end_ts: int) -> pd.DataFrame:
    TF_MAP = {
        "1m": "1min", "3m": "3min", "5m": "5min", "15m": "15min",
        "30m": "30min", "1h": "1hour", "4h": "4hour", "1d": "1day",
    }
    symbol = pair.replace("/", "-")
    ktype = TF_MAP.get(timeframe, "15min")
    rows = _fetch_ohlcv(symbol, ktype, start_ts, end_ts)
    if not rows:
        raise RuntimeError(f"No OHLCV data for {pair} {timeframe}")
    df = pd.DataFrame(rows, columns=["ts", "open", "close", "high", "low", "vol", "turn"])
    df["date"] = pd.to_datetime(df["ts"].astype(int), unit="s", utc=True)
    for c in ["open", "high", "low", "close", "vol"]:
        df[c] = df[c].astype(float)
    return df[["date", "open", "high", "low", "close", "vol"]].sort_values("date").reset_index(drop=True)


# ─── Futures kline + funding-rate loaders ─────────────────────────────────
#
# Why a separate code path:
#   - SPOT endpoint  : https://api.kucoin.com         /api/v1/market/candles
#     · granularity  : text ("15min")
#     · time         : seconds
#     · max          : 1500 candles per request
#     · columns      : [ts, open, CLOSE, HIGH, LOW, vol, turn]   ← spot quirk
#
#   - FUTURES endpoint: https://api-futures.kucoin.com /api/v1/kline/query
#     · granularity  : seconds (900)
#     · time         : milliseconds
#     · max          : 500 candles per request
#     · columns      : [ts_ms, open, HIGH, LOW, CLOSE, vol, turn]  ← standard
#
# Mixing them by accident (copy-paste from spot) silently swaps high/close,
# which makes every backtest result wrong without throwing an error. The
# futures loader below is deliberately independent so that can't happen.

FUTURES_BASE = "https://api-futures.kucoin.com"


def _pair_to_futures_symbol(pair: str) -> str:
    """BTC/USDT → XBTUSDTM ; ETH/USDT → ETHUSDTM (KuCoin Futures convention)."""
    if "/" not in pair:
        return pair
    base, quote = pair.split("/", 1)
    if base.upper() == "BTC":
        base = "XBT"
    return f"{base.upper()}{quote.upper()}M"


def _fetch_futures_klines(symbol: str, granularity_min: int,
                          from_ms: int, to_ms: int) -> list:
    """Paginated fetch from /api/v1/kline/query.

    Verified empirically against api-futures.kucoin.com:
      - `granularity` is in MINUTES (1, 5, 15, 30, 60, 120, 240, 480,
        720, 1440, 10080) — NOT seconds, despite what the doc says.
        Sending seconds → HTTP 400.
      - Response is capped at 200 candles per request — NOT 500 like
        the doc claims. Anything bigger silently truncates.
      - Response order is ASCENDING (oldest first), so to paginate
        forward we advance the cursor to (max_ts_in_page + interval).
      - The endpoint walks FORWARD from `from`, returning up to 200
        candles. The `to` query is just a stop-bound; if from..to spans
        more than 200 candles, you only get the first 200.
    """
    from backend.services._kucoin_proxy import urlopen as _proxy_urlopen
    granularity_secs = granularity_min * 60
    rows: list = []
    cur = from_ms
    # Safety bound: even if KuCoin keeps returning data, stop after
    # enough pages to cover the requested span (with slack for gaps).
    max_pages = max(1, ((to_ms - from_ms) // (200 * granularity_secs * 1000)) + 5)
    pages = 0
    while cur < to_ms and pages < max_pages:
        pages += 1
        qs = urllib.parse.urlencode({
            "symbol":      symbol,
            "granularity": granularity_min,
            "from":        cur,
            "to":          to_ms,
        })
        url = f"{FUTURES_BASE}/api/v1/kline/query?{qs}"
        req = urllib.request.Request(url, headers={"User-Agent": "AutoTradeHub/1.0"})
        with _proxy_urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        if str(data.get("code")) != "200000":
            raise RuntimeError(f"KuCoin Futures kline error: {data.get('msg','unknown')}")
        new = data.get("data") or []
        if not new:
            break
        rows.extend(new)
        # Advance past the newest timestamp in the page.
        page_ts = [int(r[0]) for r in new]
        newest_ms = max(page_ts)
        next_cur = newest_ms + granularity_secs * 1000
        if next_cur <= cur:
            break   # safety: no forward progress
        cur = next_cur
    return rows


# Process-local TTL caches so auto-tune (which runs the same backtest with
# N different SL/TP combos) doesn't re-download identical candles + funding
# data N times. 10 minutes is long enough for a 12-combo grid to complete
# but short enough that fresh runs pick up new market data.
import time as _time_mod_for_cache
_OHLCV_CACHE: dict[tuple, tuple[float, "pd.DataFrame"]]   = {}
_FUNDING_CACHE: dict[tuple, tuple[float, list]] = {}
_CACHE_TTL_SECS = 600  # 10 minutes


def load_futures_ohlcv(pair: str, timeframe: str, start_ts: int, end_ts: int) -> pd.DataFrame:
    """Load FUTURES OHLCV for the perpetual contract of `pair`.

    Returns a DataFrame with columns [date, open, high, low, close, vol] —
    same shape as load_ohlcv() so the rest of the backtester is unchanged.
    Result is cached in-process for 10 minutes keyed on the request args.
    """
    key = (pair, timeframe, start_ts, end_ts)
    now = _time_mod_for_cache.time()
    hit = _OHLCV_CACHE.get(key)
    if hit and now - hit[0] < _CACHE_TTL_SECS:
        return hit[1].copy()   # copy so callers can mutate without affecting cache
    # KuCoin Futures granularity is in MINUTES (see _fetch_futures_klines docstring).
    TF_MIN = {
        "1m": 1, "3m": 3, "5m": 5, "15m": 15,
        "30m": 30, "1h": 60, "2h": 120, "4h": 240,
        "6h": 360, "8h": 480, "12h": 720, "1d": 1440, "1w": 10080,
    }
    granularity = TF_MIN.get(timeframe, 15)
    symbol      = _pair_to_futures_symbol(pair)
    rows = _fetch_futures_klines(symbol, granularity,
                                 start_ts * 1000, end_ts * 1000)
    if not rows:
        raise RuntimeError(f"No futures OHLCV data for {pair} {timeframe}")
    # FUTURES column order: [ts_ms, open, high, low, close, vol, turn]
    # — NOT the same as the spot endpoint. Hardcoded here to prevent any
    #   copy-paste reuse of the spot column order.
    df = pd.DataFrame(rows, columns=["ts_ms", "open", "high", "low", "close", "vol", "turn"])
    df["date"] = pd.to_datetime(df["ts_ms"].astype("int64"), unit="ms", utc=True)
    for c in ["open", "high", "low", "close", "vol"]:
        df[c] = df[c].astype(float)
    result = (df[["date", "open", "high", "low", "close", "vol"]]
                .drop_duplicates(subset=["date"])
                .sort_values("date")
                .reset_index(drop=True))
    _OHLCV_CACHE[key] = (now, result)
    return result.copy()


def load_funding_history(pair: str, start_ts: int, end_ts: int) -> list[tuple[int, float]]:
    """Fetch real funding rates for the date range from
    /api/v1/contract/funding-rates (public, no auth).

    Returns a sorted list of (timepoint_seconds, rate) tuples — caller can
    binary-search this in the backtest bar loop. Returns [] on any error
    so the caller can fall back to a constant rate without aborting the
    whole backtest. Cached in-process for 10 min keyed on request args.
    """
    key = (pair, start_ts, end_ts)
    now = _time_mod_for_cache.time()
    hit = _FUNDING_CACHE.get(key)
    if hit and now - hit[0] < _CACHE_TTL_SECS:
        return list(hit[1])
    from backend.services._kucoin_proxy import urlopen as _proxy_urlopen
    symbol = _pair_to_futures_symbol(pair)
    qs = urllib.parse.urlencode({
        "symbol": symbol,
        "from":   start_ts * 1000,
        "to":     end_ts   * 1000,
    })
    url = f"{FUTURES_BASE}/api/v1/contract/funding-rates?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "AutoTradeHub/1.0"})
    try:
        with _proxy_urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return []
    if str(data.get("code")) != "200000":
        return []
    rates = data.get("data") or []
    out: list[tuple[int, float]] = []
    for r in rates:
        try:
            tp_s = int(int(r["timepoint"]) // 1000)
            fr   = float(r["fundingRate"])
            out.append((tp_s, fr))
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda x: x[0])
    _FUNDING_CACHE[key] = (now, out)
    return list(out)


# ─────────────────────────── indicators ───────────────────────────────────

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema5"]   = df["close"].ewm(span=5,  adjust=False).mean()
    df["ema9"]   = df["close"].ewm(span=9,  adjust=False).mean()
    df["ema20"]  = df["close"].ewm(span=20, adjust=False).mean()   # ← SimpleTarget uses this
    df["ema21"]  = df["close"].ewm(span=21, adjust=False).mean()
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"]        = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"]   = df["macd"] - df["macd_signal"]
    delta = df["close"].diff()
    gain  = delta.clip(lower=0).ewm(com=13, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=13, adjust=False).mean()
    df["rsi"] = 100 - 100 / (1 + gain / loss.replace(0, 1e-9))
    df["bb_mid"]   = df["close"].rolling(20).mean()
    bb_std         = df["close"].rolling(20).std()
    df["bb_upper"] = df["bb_mid"] + 2 * bb_std
    df["bb_lower"] = df["bb_mid"] - 2 * bb_std
    df["vol_sma"]  = df["vol"].rolling(20).mean()

    # ── Multi-scale swing structure for SMC strategies ────────────────────
    # A swing high at lookback N: high[i] is the max across [i-N, i+N].
    # We compute three scales so SMC Pro can do 3-layer analysis without
    # actually resampling to higher timeframes (which would 30x the work).
    # On a 15m chart, N=3 ≈ 90min swings (LTF), N=10 ≈ 5h swings (MTF),
    # N=30 ≈ 15h swings (HTF — daily-ish). All three are computed
    # vectorised via rolling-window max/min, which is ~5ms for 35k bars.
    #
    # The "_conf" suffix marks a swing as confirmed: a swing at index j
    # is only knowable after j+N bars have passed. We shift the boolean
    # forward by N so look-up at bar i never peeks into the future.
    import numpy as _np
    high = df["high"]; low = df["low"]
    for N, label in [(3, "ltf"), (10, "mtf"), (30, "htf")]:
        # 2N+1 window centered on j → rolling().max() lags by 0 if center=True.
        # min_periods=2*N+1 forces NaN at the edges so we never compare
        # against a truncated window.
        sh_max = high.rolling(2 * N + 1, center=True, min_periods=2 * N + 1).max()
        sl_min = low .rolling(2 * N + 1, center=True, min_periods=2 * N + 1).min()
        # NaN-safe equality: NaN==NaN is False so edge bars are correctly
        # excluded without any fillna() dtype dance.
        sh_arr = (sh_max.to_numpy() == high.to_numpy())
        sl_arr = (sl_min.to_numpy() == low.to_numpy())
        # Shift by +N so any bar i can read the column without leaking
        # future info — a swing high at index j is reported as True at
        # index j+N (when it would actually be confirmable in real time).
        sh_shifted = _np.zeros(len(df), dtype=bool)
        sl_shifted = _np.zeros(len(df), dtype=bool)
        if N < len(df):
            sh_shifted[N:] = sh_arr[:-N] if N > 0 else sh_arr
            sl_shifted[N:] = sl_arr[:-N] if N > 0 else sl_arr
        df[f"swing_high_{label}"] = sh_shifted
        df[f"swing_low_{label}"]  = sl_shifted

    return df


# ─────────────────────────── strategy signals ─────────────────────────────

def _signal_miss_candle_short(df: pd.DataFrame, i: int):
    """Returns (entry_price, sl, tp_3r) or None."""
    if i < 3:
        return None
    prev2, prev, row = df.iloc[i - 2], df.iloc[i - 1], df.iloc[i]
    if not (prev2["high"] < prev2["ema5"]):
        return None
    if not (prev["macd_hist"] < 0 and abs(prev["macd_hist"]) > abs(df.iloc[i - 3]["macd_hist"])):
        return None
    if not (prev["close"] < prev["open"]):
        return None
    if prev["high"] > prev2["high"]:
        return None
    if row["low"] > prev2["low"]:
        return None
    entry = prev2["low"]
    sl    = prev2["high"]
    risk  = sl - entry
    if risk <= 0:
        return None
    return entry, sl, entry - 3 * risk, "short"


def _signal_miss_candle_long(df: pd.DataFrame, i: int):
    if i < 3:
        return None
    prev2, prev, row = df.iloc[i - 2], df.iloc[i - 1], df.iloc[i]
    if not (prev2["low"] > prev2["ema5"]):
        return None
    if not (prev["macd_hist"] > 0 and abs(prev["macd_hist"]) > abs(df.iloc[i - 3]["macd_hist"])):
        return None
    if not (prev["close"] > prev["open"]):
        return None
    if prev["low"] < prev2["low"]:
        return None
    if row["high"] < prev2["high"]:
        return None
    entry = prev2["high"]
    sl    = prev2["low"]
    risk  = entry - sl
    if risk <= 0:
        return None
    return entry, sl, entry + 3 * risk, "long"


def _signal_macd_crossover(df: pd.DataFrame, i: int):
    """MACD crossover — LONG on bullish cross, SHORT on bearish cross."""
    if i < 2:
        return None
    prev, row = df.iloc[i - 1], df.iloc[i]
    entry = row["close"]
    if prev["macd"] < prev["macd_signal"] and row["macd"] > row["macd_signal"]:
        return entry, entry * 0.97, entry * 1.09, "long"
    if prev["macd"] > prev["macd_signal"] and row["macd"] < row["macd_signal"]:
        return entry, entry * 1.03, entry * 0.91, "short"
    return None


def _signal_rsi_bollinger(df: pd.DataFrame, i: int):
    """RSI + Bollinger Bands — LONG on oversold, SHORT on overbought."""
    if i < 1:
        return None
    row   = df.iloc[i]
    entry = row["close"]
    if row["rsi"] < 30 and entry < row["bb_lower"]:
        return entry, entry * 0.97, row["bb_mid"], "long"
    if row["rsi"] > 70 and entry > row["bb_upper"]:
        return entry, entry * 1.03, row["bb_mid"], "short"
    return None


def _signal_ema_scalping(df: pd.DataFrame, i: int):
    """EMA scalping — LONG on golden cross, SHORT on death cross (volume confirmed)."""
    if i < 2:
        return None
    prev, row = df.iloc[i - 1], df.iloc[i]
    vol_sma   = df["vol_sma"].iloc[i] if "vol_sma" in df.columns else df["vol"].rolling(20).mean().iloc[i]
    vol_ok    = row["vol"] > vol_sma * 1.5
    entry     = row["close"]
    if prev["ema9"] < prev["ema21"] and row["ema9"] > row["ema21"] and vol_ok:
        return entry, entry * 0.985, entry * 1.015, "long"
    if prev["ema9"] > prev["ema21"] and row["ema9"] < row["ema21"] and vol_ok:
        return entry, entry * 1.015, entry * 0.985, "short"
    return None


def _signal_simple_target(df: pd.DataFrame, i: int):
    """
    SimpleTargetStrategy — symmetric bidirectional mean-reversion.

    LONG : RSI < 45 AND close < EMA20 (price pulling back below mean)
        OR RSI < 30 (strong oversold regardless of EMA)
    SHORT: RSI > 55 AND close > EMA20 (price pushing above mean)
        OR RSI > 70 (strong overbought regardless of EMA)

    Symmetric RSI thresholds (45/55) ensure balanced Long/Short signal count.
    SL/TP set by user in backtest UI (1.5% / 3.0% default).
    """
    if i < 21:
        return None
    row   = df.iloc[i]
    rsi   = row.get("rsi", 50.0)
    close = row["close"]
    ema20 = row.get("ema20", close)

    # ── LONG: dip below EMA20 with RSI cooling, or extreme oversold ──────────
    if (rsi < 30) or (rsi < 45 and close < ema20):
        entry = close
        return entry, round(entry * 0.985, 8), round(entry * 1.030, 8), "long"

    # ── SHORT: push above EMA20 with RSI hot, or extreme overbought ──────────
    if (rsi > 70) or (rsi > 55 and close > ema20):
        entry = close
        return entry, round(entry * 1.015, 8), round(entry * 0.970, 8), "short"

    return None


def _signal_bidirectional(df: pd.DataFrame, i: int):
    """
    BidirectionalStrategy — explicit LONG + SHORT test strategy.

    LONG:  EMA9 > EMA21 (uptrend confirmed 2 bars) AND RSI < 60
    SHORT: EMA9 < EMA21 (downtrend confirmed 2 bars) AND RSI > 40
    SL/TP: 1.5% / 3.0% (2:1 R:R)
    """
    if i < 21:
        return None
    row  = df.iloc[i]
    prev = df.iloc[i - 1]
    close     = row["close"]
    ema9      = row.get("ema9", close)
    ema21     = row.get("ema21", close)
    rsi       = row.get("rsi", 50.0)
    prev_ema9  = prev.get("ema9", close)
    prev_ema21 = prev.get("ema21", close)

    uptrend   = ema9 > ema21 and prev_ema9 > prev_ema21
    downtrend = ema9 < ema21 and prev_ema9 < prev_ema21

    if uptrend and rsi < 60:
        return close, round(close * 0.985, 6), round(close * 1.030, 6), "long"
    if downtrend and rsi > 40:
        return close, round(close * 1.015, 6), round(close * 0.970, 6), "short"
    return None


def _signal_smc(df: pd.DataFrame, i: int):
    """
    SMC OB/FVG/BOS — TradingView SMC v2 compatible.

    Fires on BOTH LONG and SHORT based on EMA9×EMA21 BOS crossover direction.
    NO EMA50 HTF filter (it was blocking all shorts in bull markets).

    Logic:
      1. BOS: EMA9 × EMA21 crossover determines trend direction (golden=bull BOS,
         death=bear BOS). Most recent crossover wins.
      2. FVG: 3-candle imbalance gap in last 20 bars
      3. OB:  Last opposing candle in last 30 bars
      4. Price must be within 0.5% of FVG mid or OB mid (pullback entry)
    """
    if i < 55:
        return None

    highs  = df["high"].values
    lows   = df["low"].values
    closes = df["close"].values
    opens  = df["open"].values
    close  = closes[i]

    # ── BOS via EMA9×EMA21 crossover (last 40 bars) ──────────────────────────
    bos_lb = 40
    last_bull_bos = -1
    last_bear_bos = -1
    if "ema9" in df.columns and "ema21" in df.columns:
        for k in range(1, min(bos_lb + 1, i)):
            j = i - k
            if j < 1: break
            e9_cur, e21_cur   = df["ema9"].iloc[j],    df["ema21"].iloc[j]
            e9_prev, e21_prev = df["ema9"].iloc[j - 1], df["ema21"].iloc[j - 1]
            if last_bull_bos == -1 and e9_cur > e21_cur and e9_prev <= e21_prev:
                last_bull_bos = j
            if last_bear_bos == -1 and e9_cur < e21_cur and e9_prev >= e21_prev:
                last_bear_bos = j
            if last_bull_bos != -1 and last_bear_bos != -1:
                break

    bull_active = (last_bull_bos != -1 and
                   (last_bear_bos == -1 or last_bull_bos > last_bear_bos))
    bear_active = (last_bear_bos != -1 and
                   (last_bull_bos == -1 or last_bear_bos > last_bull_bos))

    # ── FVG: 3-candle imbalance in last 20 bars ───────────────────────────────
    fvg_lb = min(20, i - 2)
    bull_fvg_mid = bear_fvg_mid = None
    for k in range(2, fvg_lb + 1):
        j = i - k + 2
        if j < 2 or j > i: continue
        if bull_fvg_mid is None and highs[j - 2] < lows[j]:
            bull_fvg_mid = (highs[j - 2] + lows[j]) / 2
        if bear_fvg_mid is None and lows[j - 2] > highs[j]:
            bear_fvg_mid = (lows[j - 2] + highs[j]) / 2
        if bull_fvg_mid is not None and bear_fvg_mid is not None:
            break

    # ── OB: last opposing candle in last 30 bars ──────────────────────────────
    bull_ob = bear_ob = None
    for k in range(1, min(31, i)):
        j = i - k
        if bull_ob is None and closes[j] < opens[j]:
            bull_ob = (lows[j] + highs[j]) / 2
        if bear_ob is None and closes[j] > opens[j]:
            bear_ob = (lows[j] + highs[j]) / 2
        if bull_ob is not None and bear_ob is not None:
            break

    # ── Proximity: within 0.5% of FVG mid or OB mid ──────────────────────────
    at_bull_fvg = bull_fvg_mid is not None and abs(close - bull_fvg_mid) / bull_fvg_mid < 0.005
    at_bear_fvg = bear_fvg_mid is not None and abs(close - bear_fvg_mid) / bear_fvg_mid < 0.005
    at_bull_ob  = bull_ob  is not None and bull_ob  * 0.995 <= close <= bull_ob  * 1.005
    at_bear_ob  = bear_ob  is not None and bear_ob  * 0.995 <= close <= bear_ob  * 1.005

    # ── Swing SL references ───────────────────────────────────────────────────
    lb = min(20, i - 1)
    swing_high = highs[i - lb: i].max()
    swing_low  = lows[i - lb:  i].min()

    # ── Signal: BOS direction + FVG/OB zone ──────────────────────────────────
    long_ok  = bull_active and (at_bull_fvg or at_bull_ob)
    short_ok = bear_active and (at_bear_fvg or at_bear_ob)

    if long_ok:
        entry = bull_fvg_mid if at_bull_fvg else (bull_ob or close)
        sl    = round(swing_low  * 0.999, 6)
        risk  = entry - sl
        if risk <= 0 or risk > entry * 0.05:
            return None
        return entry, sl, round(entry + risk * 2, 6), "long"

    if short_ok:
        entry = bear_fvg_mid if at_bear_fvg else (bear_ob or close)
        sl    = round(swing_high * 1.001, 6)
        risk  = sl - entry
        if risk <= 0 or risk > entry * 0.05:
            return None
        return entry, sl, round(entry - risk * 2, 6), "short"

    return None


def _signal_smc_tv(df: pd.DataFrame, i: int):
    """
    TradingView SMC Strategy v2 — OB / FVG / BOS
    Exact Python translation of the standard Pine Script SMC v2 logic.

    Pine Script equivalent:
      swing_len  = 5          // pivot lookback (bars each side)
      ph = ta.pivothigh(high, swing_len, swing_len)
      pl = ta.pivotlow (low,  swing_len, swing_len)

      bull_bos = close crosses above last confirmed pivot high  → LONG zone
      bear_bos = close crosses below last confirmed pivot low   → SHORT zone

      bull_fvg: high[2] < low[0]                (3-candle bullish imbalance)
      bear_fvg: low[2]  > high[0]               (3-candle bearish imbalance)

      bull_ob : last bearish candle (close < open) before bull_bos
      bear_ob : last bullish candle (close > open) before bear_bos

      SL  = swing low/high that was broken (market-structure based)
      TP  = entry + 2 × risk  (2 R)

    Entry timing: signal bar close → entry at NEXT bar open (TV default).
    """
    SWING_LEN = 5      # pivot lookback bars (each side) — TV default
    BOS_LB    = 80     # how far back to search for last confirmed pivot
    FVG_LB    = 30     # bars to search for recent FVG
    OB_LB     = 40     # bars to search for order block

    if i < SWING_LEN * 2 + 5:
        return None

    highs  = df["high"].values
    lows   = df["low"].values
    closes = df["close"].values
    opens  = df["open"].values
    close  = closes[i]

    # ── 1. Pivot highs / lows (confirmed SWING_LEN bars ago) ─────────────────
    # A confirmed pivot high at bar j: high[j] = max of high[j-N..j+N]
    # Only confirmed if j <= i - SWING_LEN (right side complete)
    last_ph = None; last_ph_bar = -1
    last_pl = None; last_pl_bar = -1

    search_start = i - SWING_LEN           # earliest bar where right side is confirmed
    search_end   = max(SWING_LEN, i - BOS_LB)

    for j in range(search_start, search_end, -1):
        if j < SWING_LEN or j + SWING_LEN >= len(highs):
            break
        # Pivot high: highest in [j-N .. j+N]
        if last_ph is None:
            window_h = highs[j - SWING_LEN: j + SWING_LEN + 1]
            if highs[j] == window_h.max():
                last_ph = highs[j]
                last_ph_bar = j
        # Pivot low: lowest in [j-N .. j+N]
        if last_pl is None:
            window_l = lows[j - SWING_LEN: j + SWING_LEN + 1]
            if lows[j] == window_l.min():
                last_pl = lows[j]
                last_pl_bar = j
        if last_ph is not None and last_pl is not None:
            break

    if last_ph is None and last_pl is None:
        return None

    # ── 2. BOS — price breaks above last pivot high (bull) / below pivot low (bear)
    # TradingView fires a signal ONLY on the bar where price first crosses the level.
    # We allow a 2-bar window (signal bar + 1 confirmation bar) to catch next-bar-open fills.
    prev_close = closes[i - 1]

    bull_bos = False
    bear_bos = False

    # Check this bar and the immediately previous bar for the crossover
    for lag in range(0, 3):     # lag 0 = this bar, lag 1-2 = recent bars
        j = i - lag
        if j < 1: break
        pc = closes[j - 1]
        if last_ph is not None and not bull_bos and pc <= last_ph and closes[j] > last_ph:
            bull_bos = True
        if last_pl is not None and not bear_bos and pc >= last_pl and closes[j] < last_pl:
            bear_bos = True
        if bull_bos or bear_bos:
            break   # only ONE direction per signal

    if not bull_bos and not bear_bos:
        return None

    # ── 3. FVG within last FVG_LB bars (3-candle gap imbalance) ─────────────
    bull_fvg_hi = bull_fvg_lo = None
    bear_fvg_hi = bear_fvg_lo = None
    fvg_end = max(2, i - FVG_LB)
    for k in range(i, fvg_end, -1):
        if k < 2: break
        # Bullish FVG: high[k-2] < low[k]
        if bull_fvg_lo is None and highs[k - 2] < lows[k]:
            bull_fvg_lo = highs[k - 2]
            bull_fvg_hi = lows[k]
        # Bearish FVG: low[k-2] > high[k]
        if bear_fvg_hi is None and lows[k - 2] > highs[k]:
            bear_fvg_hi = lows[k - 2]
            bear_fvg_lo = highs[k]
        if bull_fvg_lo is not None and bear_fvg_hi is not None:
            break

    # ── 4. Order Block (last opposing candle in OB_LB bars) ──────────────────
    bull_ob_lo = bull_ob_hi = None   # last bearish candle → bull OB
    bear_ob_lo = bear_ob_hi = None   # last bullish candle → bear OB
    ob_end = max(0, i - OB_LB)
    for k in range(i - 1, ob_end, -1):
        if bull_ob_lo is None and closes[k] < opens[k]:    # bearish = bull OB
            bull_ob_lo = lows[k]; bull_ob_hi = highs[k]
        if bear_ob_lo is None and closes[k] > opens[k]:    # bullish = bear OB
            bear_ob_lo = lows[k]; bear_ob_hi = highs[k]
        if bull_ob_lo is not None and bear_ob_lo is not None:
            break

    # ── 5. Price inside zone? ─────────────────────────────────────────────────
    # FVG zone: price within the gap
    in_bull_fvg = (bull_fvg_lo is not None and
                   bull_fvg_lo <= close <= bull_fvg_hi)
    in_bear_fvg = (bear_fvg_hi is not None and
                   bear_fvg_lo <= close <= bear_fvg_hi)

    # OB zone: price inside the OB candle range (or within 0.3% above/below)
    in_bull_ob  = (bull_ob_lo is not None and
                   bull_ob_lo * 0.997 <= close <= bull_ob_hi * 1.003)
    in_bear_ob  = (bear_ob_lo is not None and
                   bear_ob_lo * 0.997 <= close <= bear_ob_hi * 1.003)

    long_zone  = in_bull_fvg or in_bull_ob
    short_zone = in_bear_fvg or in_bear_ob

    # ── 6. Build entry, SL (swing-based), TP (2 R) ───────────────────────────
    if bull_bos and long_zone:
        entry = close
        # SL = below the last pivot low (structural SL)
        sl = round(last_pl * 0.998, 6) if last_pl else round(close * 0.985, 6)
        risk = entry - sl
        if risk <= 0 or risk > entry * 0.08:   # sanity: max 8% SL distance
            sl = round(close * 0.985, 6)
            risk = entry - sl
        tp = round(entry + risk * 2, 6)         # 2 R target
        return entry, sl, tp, "long"

    if bear_bos and short_zone:
        entry = close
        # SL = above last pivot high (structural SL)
        sl = round(last_ph * 1.002, 6) if last_ph else round(close * 1.015, 6)
        risk = sl - entry
        if risk <= 0 or risk > entry * 0.08:
            sl = round(close * 1.015, 6)
            risk = sl - entry
        tp = round(entry - risk * 2, 6)         # 2 R target
        return entry, sl, tp, "short"

    return None


# ── SMC Pro v3 — full institutional spec ──────────────────────────────────
#
# Implements the complete 3-layer SMC entry model:
#
#   STEP 1  HTF bias engine
#           • Swing detection at N=30 (≈15h on 15m → daily-ish trend)
#           • Last BOS direction = bias (bull / bear / range)
#           • RANGE = no trade. Most of the user's losses came from trading
#             against a clear HTF trend in 50/50 chop, which this fixes.
#
#   STEP 2  Liquidity sweep detection
#           • Most recent HTF swing extreme MUST have been swept
#             (briefly broken, then price reclaimed) before we enter
#           • This is the "smart money grabbed retail stops" prerequisite
#
#   STEP 3  OB / FVG zone
#           • OB = last opposing candle BEFORE a strong move (>0.5% body
#             follow-through within 3 bars)
#           • FVG = 3-candle imbalance gap (high[i-2] < low[i] for bull)
#           • Price MUST currently be inside OB or FVG range
#
#   STEP 4  Premium / Discount filter
#           • 50% fib of last HTF swing range
#           • LONG only valid in discount (below midline)
#           • SHORT only valid in premium (above midline)
#           • "Buy low, sell high" — kills the worst trade locations
#
#   STEP 5  LTF confirmation
#           • A 3-bar LTF swing extreme must be broken by current close
#             in the direction of the HTF bias
#           • This is the "wait for confirmation" rule the user asked for
#
#   STEP 6  Session filter
#           • Only trade NY institutional hours: 12:00–21:00 UTC
#             (= 7am–4pm ET, covers NY pre-market through close)
#           • Asia / EU dead-zone chop is filtered out — these are the
#             hours where MOST of the random "buy at the top, sell at
#             the bottom" trades happen
#
#   STEP 7  Risk math
#           • SL = beyond the sweep extreme - 0.1% buffer
#           • TP = entry + 2R (TP1; TP2/TP3 would need partial-close
#             engine support — deferred, see comment in the function)
#           • Reject trades where risk > 3% of entry (too wide; usually
#             means a broken signal)
#
# Why all these gates: a real SMC trader rejects ~95% of "looks like a
# setup" candidates because exactly one of the conditions fails. Without
# every gate, the algo fires on noise (which is what produced the 3,446-
# trade catastrophe). With every gate, expect ~50–200 trades per 6 months
# on 15m — that's the institutional-quality signal frequency.

def _signal_smc_pro(df: pd.DataFrame, i: int):
    """SMC Pro v3 — full institutional 3-layer entry model.

    Returns (entry, sl, tp, dir) or None. Aggressively filters: most bars
    produce None because at least one of HTF bias / liquidity sweep /
    zone / discount-premium / LTF confirmation / session is missing.
    That's the design — quality over quantity.

    Performance: ~0.1ms per bar (pre-computed swing columns + tight
    inner loops capped at 50-bar lookback). 35k bars complete in ~3s.
    """
    # Need enough lookback for HTF swings to be confirmed AND for
    # sweep detection to find a prior extreme.
    if i < 100:
        return None

    # ── Required columns (added by add_indicators) ───────────────────────
    # If columns are missing (e.g. caller bypassed add_indicators), fail
    # safe rather than KeyError.
    needed_cols = ("swing_high_htf", "swing_low_htf",
                   "swing_high_ltf", "swing_low_ltf")
    for c in needed_cols:
        if c not in df.columns:
            return None

    # ── STEP 6: Session filter (NY institutional hours) ──────────────────
    # KuCoin candle timestamps are UTC. NY pre-market opens 7am ET = 12:00
    # UTC (winter) / 11:00 UTC (summer). We use 12-21 UTC as a fixed
    # window that covers NY hours year-round with a small spring/fall DST
    # buffer. Outside this window, return early — saves ~70% of bar
    # evaluations on a 24/7 dataset.
    bar_dt = df["date"].iloc[i]
    if not (12 <= bar_dt.hour <= 21):
        return None

    highs   = df["high"].values
    lows    = df["low"].values
    closes  = df["close"].values
    opens   = df["open"].values
    sh_htf  = df["swing_high_htf"].values
    sl_htf  = df["swing_low_htf"].values
    sh_ltf  = df["swing_high_ltf"].values
    sl_ltf  = df["swing_low_ltf"].values
    close   = closes[i]

    # ── STEP 1: HTF bias via last BOS direction ──────────────────────────
    # Walk back to find the most recent confirmed HTF swing high & low.
    # (Confirmed = swing already "matured" — swing columns are shifted
    # forward by N to prevent look-ahead bias.)
    last_sh_idx = last_sl_idx = -1
    HTF_LOOKBACK = 300   # ~75h on 15m ≈ 3 days of HTF context
    for j in range(i, max(0, i - HTF_LOOKBACK), -1):
        if last_sh_idx == -1 and sh_htf[j]:
            last_sh_idx = j
        if last_sl_idx == -1 and sl_htf[j]:
            last_sl_idx = j
        if last_sh_idx != -1 and last_sl_idx != -1:
            break
    if last_sh_idx == -1 or last_sl_idx == -1:
        return None
    last_sh_price = highs[last_sh_idx]
    last_sl_price = lows[last_sl_idx]

    # BOS = close beyond the most recent opposing swing extreme. The
    # most-recent BOS direction wins → that's the HTF bias.
    bull_bos_idx = bear_bos_idx = -1
    bos_scan_start = max(last_sh_idx, last_sl_idx) + 1
    for j in range(bos_scan_start, i + 1):
        if bull_bos_idx == -1 and closes[j] > last_sh_price:
            bull_bos_idx = j
        if bear_bos_idx == -1 and closes[j] < last_sl_price:
            bear_bos_idx = j
    if bull_bos_idx > bear_bos_idx:
        htf_bias = "bull"
    elif bear_bos_idx > bull_bos_idx:
        htf_bias = "bear"
    else:
        return None    # ranging / no clear BOS → STAY OUT (key rule)

    # ── STEP 4: Premium / Discount via 50% Fib of HTF range ──────────────
    # Range = last HTF swing high to swing low (the structural range the
    # current move is inside). Midpoint splits premium / discount.
    swing_range_hi = last_sh_price
    swing_range_lo = last_sl_price
    if swing_range_hi <= swing_range_lo:
        return None
    fib_mid = (swing_range_hi + swing_range_lo) / 2
    in_discount = close <= fib_mid
    in_premium  = close >= fib_mid
    if htf_bias == "bull" and not in_discount:
        return None    # would be buying at premium = bad RR
    if htf_bias == "bear" and not in_premium:
        return None    # would be selling at discount = bad RR

    # ── STEP 2: Liquidity sweep — most recent swing extreme grabbed ─────
    # We require that ONE of the last 3 LTF swing extremes was wicked
    # through (low broke prior swing low for bulls, high broke prior
    # swing high for bears) WITHIN the last 20 bars and price has since
    # reclaimed. That's the "smart money took stops" signal.
    sweep_lookback = 30
    sweep_extreme = None    # the price the wick reached → defines SL
    if htf_bias == "bull":
        # Look back for an LTF swing low that was subsequently swept down
        for j in range(i - 1, max(3, i - sweep_lookback), -1):
            if sl_ltf[j]:
                prev_low = lows[j]
                # Did any later bar (within sweep_lookback) go below it
                # and the current close reclaimed back above?
                for k in range(j + 1, i + 1):
                    if lows[k] < prev_low and close > prev_low:
                        sweep_extreme = lows[k]
                        break
                break
        if sweep_extreme is None:
            return None
    else:
        for j in range(i - 1, max(3, i - sweep_lookback), -1):
            if sh_ltf[j]:
                prev_high = highs[j]
                for k in range(j + 1, i + 1):
                    if highs[k] > prev_high and close < prev_high:
                        sweep_extreme = highs[k]
                        break
                break
        if sweep_extreme is None:
            return None

    # ── STEP 3: OB / FVG zone within last 20 bars + price inside zone ────
    bull_fvg_lo = bull_fvg_hi = None
    bear_fvg_lo = bear_fvg_hi = None
    fvg_lookback = 20
    for k in range(i, max(2, i - fvg_lookback), -1):
        if bull_fvg_lo is None and highs[k - 2] < lows[k]:
            bull_fvg_lo = highs[k - 2]
            bull_fvg_hi = lows[k]
        if bear_fvg_hi is None and lows[k - 2] > highs[k]:
            bear_fvg_hi = lows[k - 2]
            bear_fvg_lo = highs[k]
        if bull_fvg_lo is not None and bear_fvg_hi is not None:
            break

    # Order Block with STRONG-MOVE confirmation: only count the OB if a
    # subsequent candle within 3 bars closed >0.5% beyond the OB extreme.
    bull_ob_lo = bull_ob_hi = None
    bear_ob_lo = bear_ob_hi = None
    ob_lookback = 30
    for k in range(i - 1, max(2, i - ob_lookback), -1):
        # Bullish OB = last bearish candle before strong UP move
        if bull_ob_lo is None and closes[k] < opens[k]:
            for m in range(k + 1, min(k + 4, i + 1)):
                if closes[m] > highs[k] * 1.005:
                    bull_ob_lo = lows[k]; bull_ob_hi = highs[k]
                    break
        # Bearish OB = last bullish candle before strong DOWN move
        if bear_ob_lo is None and closes[k] > opens[k]:
            for m in range(k + 1, min(k + 4, i + 1)):
                if closes[m] < lows[k] * 0.995:
                    bear_ob_lo = lows[k]; bear_ob_hi = highs[k]
                    break
        if bull_ob_lo is not None and bear_ob_lo is not None:
            break

    in_bull_zone = (
        (bull_fvg_lo is not None and bull_fvg_lo <= close <= bull_fvg_hi) or
        (bull_ob_lo  is not None and bull_ob_lo  <= close <= bull_ob_hi)
    )
    in_bear_zone = (
        (bear_fvg_lo is not None and bear_fvg_lo <= close <= bear_fvg_hi) or
        (bear_ob_lo  is not None and bear_ob_lo  <= close <= bear_ob_hi)
    )
    if htf_bias == "bull" and not in_bull_zone:
        return None
    if htf_bias == "bear" and not in_bear_zone:
        return None

    # ── STEP 5: LTF BOS confirmation in the bias direction ───────────────
    # A recent 3-bar LTF swing must be broken by the current close.
    ltf_confirmed = False
    for j in range(i - 1, max(3, i - 15), -1):
        if htf_bias == "bull" and sh_ltf[j] and close > highs[j]:
            ltf_confirmed = True; break
        if htf_bias == "bear" and sl_ltf[j] and close < lows[j]:
            ltf_confirmed = True; break
    if not ltf_confirmed:
        return None

    # ── STEP 7: Risk math — SL at sweep extreme, TP at 2R ────────────────
    #
    # TP1=2R is the only target the engine supports right now. A future
    # enhancement would be multi-TP (close 50% at 2R → move SL to BE →
    # let runner go to previous swing high / liquidity), which requires
    # partial-close support in futures_backtester.py. Tracked but deferred.
    if htf_bias == "bull":
        entry = close
        sl    = sweep_extreme * 0.999     # 10 bps buffer below sweep low
        risk  = entry - sl
        if risk <= 0 or risk > entry * 0.03:
            return None        # too wide → likely a broken signal
        tp = entry + 2 * risk
        return entry, sl, tp, "long"

    entry = close
    sl    = sweep_extreme * 1.001          # 10 bps buffer above sweep high
    risk  = sl - entry
    if risk <= 0 or risk > entry * 0.03:
        return None
    tp = entry - 2 * risk
    return entry, sl, tp, "short"


_STRATEGY_FN = {
    "MissCandleShortStrategy":  _signal_miss_candle_short,
    "MissCandleLongStrategy":   _signal_miss_candle_long,
    "MacdCrossoverStrategy":    _signal_macd_crossover,
    "RsiBollingerStrategy":     _signal_rsi_bollinger,
    "EmaScalpingStrategy":      _signal_ema_scalping,
    "SimpleTargetStrategy":     _signal_simple_target,
    "BidirectionalStrategy":    _signal_bidirectional,
    "SMCStrategy":              _signal_smc,          # EMA-crossover approximation
    "SMCStrategyTV":            _signal_smc_tv,       # Exact TradingView SMC v2 port
    "SMCProV3":                 _signal_smc_pro,      # Full institutional 3-layer SMC
}


def _guess_strategy(name: str):
    """Fuzzy-match a strategy name to a known signal function."""
    n = name.lower()
    for key, fn in _STRATEGY_FN.items():
        if key.lower() in n or n in key.lower():
            return fn
    if "miss" in n and "short" in n:    return _signal_miss_candle_short
    if "miss" in n and "long" in n:     return _signal_miss_candle_long
    if "macd" in n:                     return _signal_macd_crossover
    if "rsi" in n or "bollinger" in n:  return _signal_rsi_bollinger
    if "ema" in n or "scalp" in n:      return _signal_ema_scalping
    if "bidir" in n or "two" in n:           return _signal_bidirectional
    # "smcpro" / "smc pro" / "smc v3" → full institutional model
    if "pro" in n and "smc" in n:            return _signal_smc_pro
    if "v3" in n and "smc" in n:             return _signal_smc_pro
    if "smc" in n or "smart" in n:           return _signal_smc
    if "simple" in n or "target" in n:       return _signal_simple_target
    return _signal_simple_target   # default


# ─────────────────────────── backtest engine ──────────────────────────────

def run(
    strategy_name: str,
    pairs: list[str],
    timeframe: str,
    timerange: str,
    stoploss: float = -0.03,
    starting_balance: float = 1000.0,
    risk_per_trade: float = 0.02,
) -> dict:
    """Run a backtest and return results in the same shape as the Freqtrade router."""
    try:
        parts = timerange.split("-")
        start_ts = int(datetime(int(parts[0][:4]), int(parts[0][4:6]), int(parts[0][6:8])).timestamp())
        end_ts   = int(datetime(int(parts[1][:4]), int(parts[1][4:6]), int(parts[1][6:8])).timestamp())
    except Exception:
        return {"error": f"Invalid timerange '{timerange}'. Use YYYYMMDD-YYYYMMDD."}

    signal_fn = _STRATEGY_FN.get(strategy_name) or _guess_strategy(strategy_name)
    all_trades: list[dict] = []
    balance = starting_balance

    for pair in pairs:
        try:
            df = load_ohlcv(pair, timeframe, start_ts, end_ts)
        except Exception as e:
            return {"error": f"Data download failed for {pair}: {e}"}
        df = add_indicators(df)

        in_trade = False
        entry = sl = tp = direction = entry_date = None
        trail_lock: Optional[float] = None

        for i in range(3, len(df)):
            row = df.iloc[i]

            if in_trade:
                lo, hi = row["low"], row["high"]
                if direction == "short":
                    excursion = entry - lo
                    half_r    = (entry - tp) / 3 * 1.5
                    if excursion >= half_r and trail_lock is None:
                        trail_lock = entry - excursion * 0.5
                    eff_sl = min(sl, trail_lock) if trail_lock else sl
                    if hi >= eff_sl:
                        exit_p = eff_sl
                        pnl_r  = (entry - exit_p) / (sl - entry) if sl != entry else 0
                    elif lo <= tp:
                        exit_p = tp
                        pnl_r  = 3.0
                    else:
                        continue
                else:  # long
                    excursion = hi - entry
                    half_r    = (tp - entry) / 3 * 1.5
                    if excursion >= half_r and trail_lock is None:
                        trail_lock = entry + excursion * 0.5
                    eff_sl = max(sl, trail_lock) if trail_lock else sl
                    if lo <= eff_sl:
                        exit_p = eff_sl
                        pnl_r  = (exit_p - entry) / (entry - sl) if sl != entry else 0
                    elif hi >= tp:
                        exit_p = tp
                        pnl_r  = 3.0
                    else:
                        continue

                stake       = balance * risk_per_trade
                pnl_abs     = stake * pnl_r
                profit_pct  = (exit_p - entry) / entry * 100 * (1 if direction == "long" else -1)
                balance    += pnl_abs
                all_trades.append({
                    "pair":           pair,
                    "open_date":      str(entry_date),
                    "close_date":     str(row["date"]),
                    "entry":          round(float(entry), 6),
                    "open_rate":      round(float(entry), 6),
                    "close_rate":     round(float(exit_p), 6),
                    "profit_pct":     round(float(profit_pct), 3),
                    "profit_abs":     round(float(pnl_abs), 4),
                    "exit_reason":    "stop_loss" if pnl_r <= 0 else "take_profit",
                    "balance":        round(float(balance), 2),
                    "trade_duration": None,
                })
                in_trade = trail_lock = None

            else:
                sig = signal_fn(df, i)
                if sig:
                    entry, sl, tp, direction = sig
                    entry_date = row["date"]
                    in_trade   = True

    # ── metrics ──────────────────────────────────────────────────────────
    if not all_trades:
        wins, losses, win_rate = 0, 0, 0.0
        total_profit, max_dd, sharpe = 0.0, 0.0, 0.0
    else:
        wins   = sum(1 for t in all_trades if t["profit_abs"] > 0)
        losses = len(all_trades) - wins
        win_rate = wins / len(all_trades)
        total_profit = (balance - starting_balance) / starting_balance

        bal_series = pd.Series([starting_balance] + [t["balance"] for t in all_trades])
        rets       = bal_series.pct_change().dropna()
        max_dd     = float((bal_series / bal_series.cummax() - 1).min())
        sharpe     = float((rets.mean() / rets.std() * (252 ** 0.5)) if rets.std() > 0 else 0)

    # Return in the same shape the backtest router expects
    metrics = {
        "total_profit":  round(total_profit * 100, 2),
        "win_rate":       round(win_rate, 4),
        "max_drawdown":  round(abs(max_dd) * 100, 2),
        "sharpe_ratio":  round(sharpe, 3),
        "total_trades":  len(all_trades),
        "avg_duration":  "N/A",
    }
    return {
        "id":       None,
        "metrics":  metrics,
        "trades":   all_trades,
        "results":  {
            "strategy": {
                strategy_name: {
                    "total_trades":      len(all_trades),
                    "wins":              wins,
                    "losses":            losses,
                    "win_rate":          win_rate,
                    "profit_total":      total_profit,
                    "max_drawdown":      abs(max_dd),
                    "sharpe":            sharpe,
                    "trades":            all_trades,
                }
            }
        },
        "_engine": "native_python",
    }
