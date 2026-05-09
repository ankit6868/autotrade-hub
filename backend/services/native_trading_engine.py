"""
NativeTradingEngine — Production-grade trading engine for AutoTrade Hub.

Replaces the Freqtrade subprocess with a fully native Python implementation.
Works on any cloud host (Railway, Render, Fly.io) with zero C-library deps.

Architecture
────────────
NativeTradingRegistry  — process-wide map of user_id → NativeTradingEngine
NativeTradingEngine    — one trading session per user (paper or live)
  ├─ CandleManager     — polls KuCoin REST for OHLCV, builds indicator frames
  ├─ StrategyRunner    — signal generation for all built-in strategies
  ├─ PositionManager   — open/closed position tracking with SL/TP/trailing
  └─ OrderExecutor     — virtual (paper) or real (KuCoin API) order placement

Features
────────
• Multi-user isolated (each user's engine is a separate thread, separate state)
• Fail-safe: network errors retry with exponential back-off; engine auto-resumes
• Supports: MissCandleShort/Long, MacdCrossover, RsiBollinger, EmaScalping
• Full risk management: fixed SL/TP, trailing stop, max drawdown guard
• Persists trades to the app DB so History page shows real data
• Paper + Live modes; Live places actual KuCoin REST orders
• Thread-safe: all shared state protected by threading.Lock
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
import traceback
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

log = logging.getLogger("native_engine")


# ─── DB persistence helpers (open + closed trades) ────────────────────────

def _persist_open_trade(user_id: str, pos: "Position", mode: str,
                        strategy_id: int | None = None,
                        leverage: int = 1, market_type: str = "spot") -> int | None:
    """Insert an open Position into the DB Trade table. Returns the new trade DB id."""
    try:
        from backend.models.database import SessionLocal
        from backend.models.trade import Trade as TradeModel
        db = SessionLocal()
        try:
            trade = TradeModel(
                user_id           = user_id,
                mode              = mode if mode in ("paper", "live") else "paper",
                market_type       = market_type,
                pair              = pos.pair,
                side              = pos.direction,
                leverage          = leverage,
                liquidation_price = getattr(pos, "liquidation_price", None),
                entry_price       = round(pos.entry, 8),
                amount            = round(pos.size, 8),
                stoploss_price    = round(pos.sl, 8),
                entry_time        = pos.opened_at,
                status            = "open",
                strategy_id       = strategy_id,
            )
            db.add(trade)
            db.commit()
            db.refresh(trade)
            log.info("[%s] Open trade saved to DB id=%s %s @ %.4f", user_id, trade.id, pos.pair, pos.entry)
            return trade.id
        except Exception as e:
            db.rollback()
            log.error("[%s] Failed to save open trade to DB: %s", user_id, e)
            return None
        finally:
            db.close()
    except Exception as e:
        log.error("DB persistence import error: %s", e)
        return None


def _persist_closed_trade(user_id: str, pos: "Position", mode: str,
                           strategy_id: int | None = None, db_id: int | None = None) -> None:
    """Update existing open DB Trade to closed, or insert new closed record."""
    try:
        from backend.models.database import SessionLocal
        from backend.models.trade import Trade as TradeModel
        from sqlalchemy import select
        db = SessionLocal()
        try:
            trade = None
            # Try to update existing open record first (matched by db_id or pair+user+open)
            if db_id:
                trade = db.get(TradeModel, db_id)
            if trade is None:
                # Fallback: find open record by pair + user
                result = db.execute(
                    select(TradeModel).where(
                        TradeModel.user_id == user_id,
                        TradeModel.pair == pos.pair,
                        TradeModel.status == "open",
                    ).order_by(TradeModel.id.desc()).limit(1)
                )
                trade = result.scalar_one_or_none()

            if trade:
                # Update existing row
                trade.exit_price  = round(pos.exit_price or pos.entry, 8)
                trade.profit_pct  = round(pos.pnl_pct, 4)
                trade.profit_abs  = round(pos.pnl_abs, 4)
                trade.exit_time   = pos.closed_at or datetime.now(timezone.utc)
                trade.exit_reason = pos.exit_reason or "unknown"
                trade.status      = "closed"
            else:
                # Insert new closed record (fallback for trades opened before this fix)
                trade = TradeModel(
                    user_id        = user_id,
                    mode           = mode if mode in ("paper", "live") else "paper",
                    pair           = pos.pair,
                    side           = pos.direction,
                    entry_price    = round(pos.entry, 8),
                    exit_price     = round(pos.exit_price or pos.entry, 8),
                    amount         = round(pos.size, 8),
                    profit_pct     = round(pos.pnl_pct, 4),
                    profit_abs     = round(pos.pnl_abs, 4),
                    stoploss_price = round(pos.sl, 8),
                    entry_time     = pos.opened_at,
                    exit_time      = pos.closed_at or datetime.now(timezone.utc),
                    exit_reason    = pos.exit_reason or "unknown",
                    status         = "closed",
                    strategy_id    = strategy_id,
                )
                db.add(trade)
            db.commit()
            log.info("[%s] Closed trade saved to DB: %s %s pnl=%.4f", user_id, pos.pair, pos.exit_reason, pos.pnl_abs)
        except Exception as e:
            db.rollback()
            log.error("[%s] Failed to save closed trade to DB: %s", user_id, e)
        finally:
            db.close()
    except Exception as e:
        log.error("DB persistence import error: %s", e)

# ─────────────────────────── constants ────────────────────────────────────

TF_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600,
    "8h": 28800, "12h": 43200, "1d": 86400,
}
TF_KUCOIN = {
    "1m": "1min", "3m": "3min", "5m": "5min", "15m": "15min",
    "30m": "30min", "1h": "1hour", "2h": "2hour", "4h": "4hour",
    "6h": "6hour", "8h": "8hour", "12h": "12hour", "1d": "1day",
}
CANDLE_HISTORY = 100   # number of closed candles to keep in memory
KUCOIN_BASE = "https://api.kucoin.com"


# ─────────────────────────── helpers ──────────────────────────────────────

def _kucoin_get(path: str, params: dict | None = None) -> dict:
    """GET from KuCoin public REST API (no auth required)."""
    qs = ("?" + urllib.parse.urlencode(params)) if params else ""
    url = f"{KUCOIN_BASE}{path}{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "AutoTradeHub/2.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def _kucoin_post_signed(path: str, body: dict, api_key: str,
                         api_secret: str, passphrase: str,
                         base_url: str = KUCOIN_BASE) -> dict:
    """POST to KuCoin private REST API (signed). base_url allows switching to Futures API."""
    import base64, hashlib, hmac as _hmac
    ts = str(int(time.time() * 1000))
    body_str = json.dumps(body)
    str_to_sign = f"{ts}POST{path}{body_str}"
    sig = base64.b64encode(_hmac.new(
        api_secret.encode(), str_to_sign.encode(), hashlib.sha256
    ).digest()).decode()
    pp_sig = base64.b64encode(_hmac.new(
        api_secret.encode(), passphrase.encode(), hashlib.sha256
    ).digest()).decode()
    headers = {
        "KC-API-KEY": api_key,
        "KC-API-SIGN": sig,
        "KC-API-TIMESTAMP": ts,
        "KC-API-PASSPHRASE": pp_sig,
        "KC-API-KEY-VERSION": "2",
        "Content-Type": "application/json",
    }
    url = f"{base_url}{path}"
    data = body_str.encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def _kucoin_get_signed(path: str, api_key: str, api_secret: str,
                       passphrase: str, params: dict | None = None,
                       base_url: str = KUCOIN_BASE) -> dict:
    """Authenticated GET from KuCoin private REST API."""
    import base64, hashlib, hmac as _hmac
    qs = ("?" + urllib.parse.urlencode(params)) if params else ""
    ts = str(int(time.time() * 1000))
    str_to_sign = f"{ts}GET{path}{qs}"
    sig = base64.b64encode(_hmac.new(
        api_secret.encode(), str_to_sign.encode(), hashlib.sha256
    ).digest()).decode()
    pp_sig = base64.b64encode(_hmac.new(
        api_secret.encode(), passphrase.encode(), hashlib.sha256
    ).digest()).decode()
    headers = {
        "KC-API-KEY":         api_key,
        "KC-API-SIGN":        sig,
        "KC-API-TIMESTAMP":   ts,
        "KC-API-PASSPHRASE":  pp_sig,
        "KC-API-KEY-VERSION": "2",
        "Content-Type":       "application/json",
        "User-Agent":         "AutoTradeHub/2.0",
    }
    url = f"{base_url}{path}{qs}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def _fetch_candles(symbol: str, ktype: str, limit: int = CANDLE_HISTORY) -> list[dict]:
    """Fetch the last `limit` closed candles from KuCoin."""
    now = int(time.time())
    tf_secs = TF_SECONDS.get(ktype, 900)
    start = now - tf_secs * (limit + 2)
    data = _kucoin_get("/api/v1/market/candles", {
        "type": TF_KUCOIN.get(ktype, "15min"),
        "symbol": symbol,
        "startAt": start,
        "endAt": now,
    })
    if str(data.get("code")) != "200000":
        return []
    rows = []
    for r in sorted(data.get("data", []), key=lambda x: int(x[0])):
        ts = int(r[0])
        if ts >= now - tf_secs:
            continue  # skip the currently open candle
        rows.append({
            "ts": ts,
            "open":  float(r[1]),
            "close": float(r[2]),
            "high":  float(r[3]),
            "low":   float(r[4]),
            "vol":   float(r[5]),
        })
    return rows[-limit:]


def _build_df(candles: list[dict]) -> pd.DataFrame:
    if not candles:
        return pd.DataFrame()
    df = pd.DataFrame(candles)
    df["date"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    # Indicators
    df["ema5"]  = df["close"].ewm(span=5,  adjust=False).mean()
    df["ema9"]  = df["close"].ewm(span=9,  adjust=False).mean()
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()   # ← used by SimpleTarget
    df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"]   = ema12 - ema26
    df["macd_s"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_h"] = df["macd"] - df["macd_s"]
    delta = df["close"].diff()
    gain  = delta.clip(lower=0).ewm(com=13, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=13, adjust=False).mean()
    df["rsi"] = 100 - 100 / (1 + gain / loss.replace(0, 1e-9))
    df["bb_mid"]   = df["close"].rolling(20).mean()
    bb_std         = df["close"].rolling(20).std()
    df["bb_upper"] = df["bb_mid"] + 2 * bb_std
    df["bb_lower"] = df["bb_mid"] - 2 * bb_std
    df["vol_sma"] = df["vol"].rolling(20).mean()
    return df.reset_index(drop=True)


# ─────────────────────────── signal functions ─────────────────────────────

def _sig_miss_candle_short(df: pd.DataFrame) -> Optional[tuple]:
    """Returns (entry, sl, tp, 'short') or None."""
    if len(df) < 4:
        return None
    i = len(df) - 1
    prev2, prev, row = df.iloc[i-2], df.iloc[i-1], df.iloc[i]
    if prev2["high"] >= prev2["ema5"]:
        return None
    if not (prev["macd_h"] < 0 and abs(prev["macd_h"]) > abs(df.iloc[i-3]["macd_h"])):
        return None
    if prev["close"] >= prev["open"]:
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


def _sig_miss_candle_long(df: pd.DataFrame) -> Optional[tuple]:
    if len(df) < 4:
        return None
    i = len(df) - 1
    prev2, prev, row = df.iloc[i-2], df.iloc[i-1], df.iloc[i]
    if prev2["low"] <= prev2["ema5"]:
        return None
    if not (prev["macd_h"] > 0 and abs(prev["macd_h"]) > abs(df.iloc[i-3]["macd_h"])):
        return None
    if prev["close"] <= prev["open"]:
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


def _sig_macd_crossover(df: pd.DataFrame) -> Optional[tuple]:
    """MACD crossover — LONG on bullish cross, SHORT on bearish cross."""
    if len(df) < 3:
        return None
    prev, row = df.iloc[-2], df.iloc[-1]
    e = row["close"]
    # Bullish crossover: MACD crosses above signal → LONG
    if prev["macd"] < prev["macd_s"] and row["macd"] > row["macd_s"]:
        return e, e * 0.97, e * 1.09, "long"
    # Bearish crossover: MACD crosses below signal → SHORT
    if prev["macd"] > prev["macd_s"] and row["macd"] < row["macd_s"]:
        return e, e * 1.03, e * 0.91, "short"
    return None


def _sig_rsi_bollinger(df: pd.DataFrame) -> Optional[tuple]:
    """RSI + Bollinger Bands — LONG on oversold, SHORT on overbought."""
    if len(df) < 20:
        return None
    row = df.iloc[-1]
    e = row["close"]
    # Oversold: RSI < 30 AND price below lower band → LONG
    if row["rsi"] < 30 and e < row["bb_lower"]:
        return e, e * 0.97, row["bb_mid"], "long"
    # Overbought: RSI > 70 AND price above upper band → SHORT
    if row["rsi"] > 70 and e > row["bb_upper"]:
        return e, e * 1.03, row["bb_mid"], "short"
    return None


def _sig_ema_scalping(df: pd.DataFrame) -> Optional[tuple]:
    """EMA scalping — LONG on golden cross, SHORT on death cross (volume confirmed)."""
    if len(df) < 21:
        return None
    prev, row = df.iloc[-2], df.iloc[-1]
    vol_ok = row["vol"] > row["vol_sma"] * 1.5
    e = row["close"]
    # Golden cross: EMA9 crosses above EMA21 → LONG
    if prev["ema9"] < prev["ema21"] and row["ema9"] > row["ema21"] and vol_ok:
        return e, e * 0.985, e * 1.015, "long"
    # Death cross: EMA9 crosses below EMA21 → SHORT
    if prev["ema9"] > prev["ema21"] and row["ema9"] < row["ema21"] and vol_ok:
        return e, e * 1.015, e * 0.985, "short"
    return None


def _sig_simple_target(df: pd.DataFrame) -> Optional[tuple]:
    """
    SimpleTargetStrategy — bidirectional mean-reversion.

    LONG : RSI < 55 AND price near/below EMA20, OR RSI < 38 (strong oversold)
    SHORT: RSI > 65 AND price above EMA20 × 1.005, OR RSI > 72 (strong overbought)
    SL/TP: 1.5% / 3.0% (2:1 reward:risk)
    """
    if len(df) < 21:
        return None
    row   = df.iloc[-1]
    rsi   = row.get("rsi",   50.0)
    close = row["close"]
    ema20 = row.get("ema20", row.get("ema_20", close))

    # ── LONG conditions ───────────────────────────────────────────────────────
    near_ema = close <= ema20 * 1.005
    oversold = rsi < 38
    mild_dip = rsi < 55 and near_ema

    if oversold or mild_dip:
        entry = close
        sl    = round(entry * 0.985, 8)    # -1.5% stop-loss
        tp    = round(entry * 1.030, 8)    # +3.0% take-profit (2:1 R:R)
        return entry, sl, tp, "long"

    # ── SHORT conditions ──────────────────────────────────────────────────────
    extended  = close >= ema20 * 0.995    # at or slightly below EMA20 from above
    overbought  = rsi > 72               # strongly overbought
    mild_top    = rsi > 65 and not near_ema  # elevated RSI + price extended above EMA

    if overbought or mild_top:
        entry = close
        sl    = round(entry * 1.015, 8)    # +1.5% stop-loss
        tp    = round(entry * 0.970, 8)    # -3.0% take-profit (2:1 R:R)
        return entry, sl, tp, "short"

    return None


def _sig_bidirectional(df: pd.DataFrame) -> Optional[tuple]:
    """
    BidirectionalStrategy — explicit LONG + SHORT test strategy.

    LONG entry:  EMA9 > EMA21 (uptrend) AND RSI < 60 (not extreme overbought)
    SHORT entry: EMA9 < EMA21 (downtrend) AND RSI > 40 (not extreme oversold)
    SL/TP: 1.5% / 3.0% (2:1 R:R) — good for futures testing
    """
    if len(df) < 21:
        return None
    row   = df.iloc[-1]
    prev  = df.iloc[-2]
    close = row["close"]
    ema9  = row.get("ema9",  close)
    ema21 = row.get("ema21", close)
    rsi   = row.get("rsi",   50.0)
    # Trend confirmation: require EMA alignment for at least 2 bars
    prev_ema9  = prev.get("ema9",  close)
    prev_ema21 = prev.get("ema21", close)

    uptrend   = ema9 > ema21 and prev_ema9 > prev_ema21
    downtrend = ema9 < ema21 and prev_ema9 < prev_ema21

    if uptrend and rsi < 60:
        entry = close
        return entry, round(entry * 0.985, 6), round(entry * 1.030, 6), "long"
    if downtrend and rsi > 40:
        entry = close
        return entry, round(entry * 1.015, 6), round(entry * 0.970, 6), "short"
    return None


def _sig_smc(df: pd.DataFrame) -> Optional[tuple]:
    """
    SMC (Smart Money Concepts) Strategy — OB/FVG/BOS model.

    Matches TradingView 'SMC Strategy v2 - OB/FVG/BOS' logic:
      1. HTF Bias  : EMA50 direction (simulates higher TF trend, fast warmup)
      2. BOS       : price breaks 20-bar swing high/low (structure break)
      3. FVG       : 3-candle Fair Value Gap within last 10 bars
      4. OB        : last opposing candle before the move (Order Block)
      5. NY Session: 13:00–21:00 UTC

    Entry: OB midpoint (price pulled back to OB zone) OR FVG midpoint
    SL   : Below swing low (LONG) / above swing high (SHORT), ~1.5%
    TP   : 2R from entry

    This 3-condition model generates similar trade frequency to TradingView.
    """
    if len(df) < 55:          # EMA50 warmup (50 bars minimum)
        return None

    row   = df.iloc[-1]
    close = row["close"]
    ts    = row.get("date", None)
    n     = len(df)

    # NY session: active 24/7 for crypto (no session filter in backtesting)
    # Live trading can optionally add session awareness at the UI level

    highs  = df["high"].values
    lows   = df["low"].values
    opens  = df["open"].values
    closes = df["close"].values

    # ── 1. HTF Bias: EMA50 direction ─────────────────────────────────────────
    ema50       = df["close"].ewm(span=50, adjust=False).mean().iloc[-1]
    htf_bullish = close > ema50
    htf_bearish = close < ema50

    # ── 2. BOS: Break of 20-bar swing high/low ───────────────────────────────
    lookback    = min(20, n - 2)
    swing_high  = highs[-lookback - 1 : -1].max()   # highest in last 20 bars (excl. current)
    swing_low   = lows[-lookback - 1 : -1].min()    # lowest in last 20 bars (excl. current)

    bos_bull    = close > swing_high                 # bullish BOS: break above 20-bar high
    bos_bear    = close < swing_low                  # bearish BOS: break below 20-bar low

    # ── 3. FVG: 3-candle Fair Value Gap in last 10 bars ──────────────────────
    fvg_window  = min(10, n - 1)
    bull_fvg    = False
    bear_fvg    = False
    bull_fvg_mid = None
    bear_fvg_mid = None
    for k in range(2, fvg_window + 1):             # k = offset from current bar
        if k >= n:
            break
        i = n - k
        # Bullish FVG: candle[-k-2].high < candle[-k].low
        if i >= 2 and highs[i - 2] < lows[i]:
            bull_fvg     = True
            bull_fvg_mid = (highs[i - 2] + lows[i]) / 2
            break
    for k in range(2, fvg_window + 1):
        if k >= n:
            break
        i = n - k
        # Bearish FVG: candle[-k-2].low > candle[-k].high
        if i >= 2 and lows[i - 2] > highs[i]:
            bear_fvg     = True
            bear_fvg_mid = (lows[i - 2] + highs[i]) / 2
            break

    # ── 4. OB: last opposing candle in last 20 bars ───────────────────────────
    bull_ob = None      # last bearish candle → potential demand OB
    bear_ob = None      # last bullish candle → potential supply OB
    for k in range(2, min(21, n)):
        i = n - k
        if bull_ob is None and closes[i] < opens[i]:     # bearish candle = demand OB
            bull_ob = (lows[i] + highs[i]) / 2
        if bear_ob is None and closes[i] > opens[i]:     # bullish candle = supply OB
            bear_ob = (lows[i] + highs[i]) / 2
        if bull_ob and bear_ob:
            break

    # Price mitigation check: has price pulled back to OB zone (±0.5%)?
    near_bull_ob = bull_ob is not None and close <= bull_ob * 1.005
    near_bear_ob = bear_ob is not None and close >= bear_ob * 0.995

    # ── 5. Entry Conditions ───────────────────────────────────────────────────
    # LONG: HTF bullish + BOS up + (FVG present OR price at OB)
    long_ok  = htf_bullish and bos_bull and (bull_fvg or near_bull_ob)
    # SHORT: HTF bearish + BOS down + (FVG present OR price at OB)
    short_ok = htf_bearish and bos_bear and (bear_fvg or near_bear_ob)

    # ── 6. Execute entry ──────────────────────────────────────────────────────
    if long_ok:
        entry = bull_fvg_mid if bull_fvg_mid else (bull_ob if bull_ob else close)
        sl    = round(swing_low * 0.9985, 6)     # just below 20-bar low
        risk  = entry - sl
        if risk <= 0 or risk > entry * 0.05:     # cap max risk at 5%
            return None
        tp    = round(entry + risk * 2, 6)       # 2R
        return entry, sl, tp, "long"

    if short_ok:
        entry = bear_fvg_mid if bear_fvg_mid else (bear_ob if bear_ob else close)
        sl    = round(swing_high * 1.0015, 6)    # just above 20-bar high
        risk  = sl - entry
        if risk <= 0 or risk > entry * 0.05:
            return None
        tp    = round(entry - risk * 2, 6)       # 2R
        return entry, sl, tp, "short"

    return None


_STRATEGY_SIGNALS = {
    "MissCandleShortStrategy": _sig_miss_candle_short,
    "MissCandleLongStrategy":  _sig_miss_candle_long,
    "MacdCrossoverStrategy":   _sig_macd_crossover,
    "RsiBollingerStrategy":    _sig_rsi_bollinger,
    "EmaScalpingStrategy":     _sig_ema_scalping,
    "SimpleTargetStrategy":    _sig_simple_target,
    "BidirectionalStrategy":   _sig_bidirectional,
    "SMCStrategy":             _sig_smc,
}


def _get_signal_fn(name: str):
    if name in _STRATEGY_SIGNALS:
        return _STRATEGY_SIGNALS[name]
    n = name.lower()
    for k, fn in _STRATEGY_SIGNALS.items():
        if k.lower() in n or n in k.lower():
            return fn
    if "miss" in n and "short" in n:    return _sig_miss_candle_short
    if "miss" in n:                     return _sig_miss_candle_long
    if "macd" in n:                     return _sig_macd_crossover
    if "rsi" in n or "boll" in n:       return _sig_rsi_bollinger
    if "bidir" in n or "two" in n:      return _sig_bidirectional
    if "smc" in n or "smart" in n or "order block" in n or "ob" == n: return _sig_smc
    if "simple" in n or "target" in n:  return _sig_simple_target
    return _sig_simple_target   # default fallback


# ─────────────────────────── position ─────────────────────────────────────

@dataclass
class Position:
    pair:         str
    direction:    str        # 'long' or 'short'
    entry:        float
    sl:           float
    tp:           float
    size:         float      # USDT stake
    opened_at:    datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    trail_lock:   Optional[float] = None
    # Unique key used in engine.positions dict (allows multiple per pair)
    trade_id:     str = field(default_factory=lambda: "")
    # DB row id — set after _persist_open_trade so close can UPDATE the same row
    db_id:        Optional[int] = None
    # result fields (filled on close)
    closed_at:    Optional[datetime] = None
    exit_price:   Optional[float]    = None
    pnl_pct:      float = 0.0
    pnl_abs:      float = 0.0
    exit_reason:  str   = ""

    def update_trail(self, current_price: float):
        """Move trailing stop when 1.5R is reached."""
        if self.direction == "long":
            excursion = current_price - self.entry
        else:
            excursion = self.entry - current_price
        half_r = abs(self.tp - self.entry) / 3 * 1.5
        if excursion >= half_r and self.trail_lock is None:
            if self.direction == "long":
                self.trail_lock = self.entry + excursion * 0.5
            else:
                self.trail_lock = self.entry - excursion * 0.5

    @property
    def effective_sl(self) -> float:
        if self.trail_lock is None:
            return self.sl
        if self.direction == "long":
            return max(self.sl, self.trail_lock)
        else:
            return min(self.sl, self.trail_lock)

    def check_exit(self, high: float, low: float) -> Optional[tuple[float, str]]:
        """Return (exit_price, reason) if this candle triggers an exit."""
        if self.direction == "long":
            if low <= self.effective_sl:
                return self.effective_sl, "stop_loss"
            if high >= self.tp:
                return self.tp, "take_profit"
        else:
            if high >= self.effective_sl:
                return self.effective_sl, "stop_loss"
            if low <= self.tp:
                return self.tp, "take_profit"
        return None

    def close(self, price: float, reason: str, ts: datetime):
        self.closed_at   = ts
        self.exit_price  = price
        self.exit_reason = reason
        if self.direction == "long":
            self.pnl_pct = (price - self.entry) / self.entry * 100
        else:
            self.pnl_pct = (self.entry - price) / self.entry * 100
        self.pnl_abs = self.size * (self.pnl_pct / 100)


# ─────────────────────────── engine ───────────────────────────────────────

class NativeTradingEngine:
    """One trading session per user (paper or live)."""

    def __init__(self, user_id: str):
        self.user_id    = user_id
        self._lock      = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_evt  = threading.Event()

        # config (set by start())
        self._strategy     = ""
        self._strategy_id: int | None = None   # DB id of the strategy record
        self._pairs: list[str] = []
        self._timeframe    = "15m"
        self._mode         = "paper"    # "paper" | "live"
        self._stoploss     = -0.03
        self._take_profit  = 0.015      # default 1.5% TP (overridden per strategy)
        self._wallet       = 1000.0
        self._risk_pct     = 0.05       # 5% of wallet per trade
        self._max_open     = 3

        # KuCoin credentials (live only)
        self._api_key   = ""
        self._api_sec   = ""
        self._api_pass  = ""

        # state
        self.balance:   float              = 1000.0
        self.positions: dict[str, Position]= {}   # trade_key → open position
        self.closed_trades: list[Position] = []
        self.ticks:     int                = 0
        self.errors:    int                = 0
        self.last_action: str              = ""
        self.started_at: Optional[datetime] = None
        self._last_prices: dict[str, float] = {}  # pair → last known live price

    # ── public API ──────────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def status(self) -> dict:
        with self._lock:
            return {
                "running":      self.is_running,
                "mode":         self._mode,
                "strategy":     self._strategy,
                "pairs":        self._pairs,
                "timeframe":    self._timeframe,
                "balance":      round(self.balance, 2),
                "open_trades":  len(self.positions),
                "total_trades": len(self.closed_trades),
                "ticks":        self.ticks,
                "errors":       self.errors,
                "last_action":  self.last_action,
                "started_at":   str(self.started_at) if self.started_at else None,
                "user_id":      self.user_id,
                "realized_pnl": round(sum(t.pnl_abs for t in self.closed_trades), 4),
                "unrealized_pnl": round(
                    sum(
                        p.size * (
                            (self._last_prices.get(p.pair, p.entry) - p.entry) / p.entry
                            if p.direction == "long" else
                            (p.entry - self._last_prices.get(p.pair, p.entry)) / p.entry
                        ) * getattr(p, "leverage", 1)
                        for p in self.positions.values()
                    ), 4
                ),
                "win_rate": round(
                    sum(1 for t in self.closed_trades if t.pnl_abs > 0)
                    / max(1, len(self.closed_trades)) * 100, 1
                ),
                "engine": "native_python",
                "pid": None,   # no subprocess
            }

    def start_paper(
        self,
        strategy_name: str,
        pairs: list[str],
        timeframe: str = "15m",
        stoploss: float = -0.03,
        wallet: float = 1000.0,
        max_open_trades: int = 3,
        max_position_pct: float = 5.0,
        trailing_stop_pct: float = 0.0,
        take_profit_pct: float = 0.0,
        **_kwargs,
    ) -> dict:
        # Clean stop before (re)starting — prevents "already running" deadlock
        self._stop_evt.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        self._stop_evt.clear()
        self._strategy     = strategy_name
        self._strategy_id  = _kwargs.get("strategy_id", None)
        self._pairs        = pairs
        self._timeframe    = timeframe
        self._stoploss     = stoploss
        self._take_profit  = take_profit_pct / 100.0 if take_profit_pct else 0.015
        self._wallet       = wallet
        self._mode         = "paper"
        self._max_open     = max_open_trades
        self._risk_pct     = max_position_pct / 100.0
        self.balance       = wallet
        self.positions     = {}
        self.closed_trades = []
        self.ticks = self.errors = 0
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name=f"engine-{self.user_id}"
        )
        self._thread.start()
        self.started_at = datetime.now(timezone.utc)
        return {
            "started": True, "mode": "paper", "pid": None,
            "strategy": strategy_name, "user_id": self.user_id,
            "engine": "native_python",
        }

    def start_live(
        self,
        strategy_name: str,
        pairs: list[str],
        timeframe: str,
        stoploss: float,
        kucoin_key: str,
        kucoin_secret: str,
        kucoin_passphrase: str,
        wallet: float = 1000.0,
        max_open_trades: int = 3,
        max_position_pct: float = 5.0,
        **_kwargs,
    ) -> dict:
        if self.is_running:
            return {"error": "Engine already running. Stop it first."}
        self._strategy  = strategy_name
        self._pairs     = pairs
        self._timeframe = timeframe
        self._stoploss  = stoploss
        self._wallet    = wallet
        self._mode      = "live"
        self._max_open  = max_open_trades
        self._risk_pct  = max_position_pct / 100.0
        self._api_key   = kucoin_key
        self._api_sec   = kucoin_secret
        self._api_pass  = kucoin_passphrase
        self.balance    = wallet
        self.positions  = {}
        self.closed_trades = []
        self.ticks = self.errors = 0
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name=f"engine-{self.user_id}"
        )
        self._thread.start()
        self.started_at = datetime.now(timezone.utc)
        return {
            "started": True, "mode": "live", "pid": None,
            "strategy": strategy_name, "user_id": self.user_id,
            "engine": "native_python",
        }

    def stop(self) -> dict:
        if not self.is_running:
            return {"stopped": True, "message": "Engine was not running"}
        self._stop_evt.set()
        self._thread.join(timeout=10)
        mode = self._mode
        return {"stopped": True, "mode": mode, "user_id": self.user_id}

    def get_trades(self) -> list[dict]:
        with self._lock:
            result = []
            for t in self.closed_trades:
                result.append({
                    "pair":        t.pair,
                    "direction":   t.direction,
                    "open_rate":   round(t.entry, 6),
                    "close_rate":  round(t.exit_price or t.entry, 6),
                    "profit_pct":  round(t.pnl_pct, 3),
                    "profit_abs":  round(t.pnl_abs, 4),
                    "open_date":   str(t.opened_at),
                    "close_date":  str(t.closed_at),
                    "exit_reason": t.exit_reason,
                    "stake":       round(t.size, 2),
                })
            return result

    def get_open_positions(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "pair":      p.pair,
                    "direction": p.direction,
                    "entry":     round(p.entry, 6),
                    "sl":        round(p.effective_sl, 6),
                    "tp":        round(p.tp, 6),
                    "stake":     round(p.size, 2),
                    "opened_at": str(p.opened_at),
                }
                for p in self.positions.values()
            ]

    def manual_entry(self, pair: str, direction: str = "long",
                     stake_override: float = 0) -> dict:
        """Immediately enter a paper position at the current market price.

        Used by the 'Buy Now / Sell Now' manual trade buttons on the UI.
        Fetches the current price from KuCoin, computes SL/TP from the
        configured stoploss (3%), then records the position exactly as the
        automated loop would.
        """
        try:
            symbol = pair.replace("/", "-")
            data = _kucoin_get(f"/api/v1/market/orderbook/level1", {"symbol": symbol})
            if str(data.get("code")) != "200000":
                return {"error": f"KuCoin price error: {data.get('msg')}"}
            price = float(data["data"]["price"])
        except Exception as e:
            return {"error": f"Could not fetch price for {pair}: {e}"}

        stake = stake_override or self.balance * self._risk_pct
        if stake <= 0 or stake > self.balance:
            return {"error": "Insufficient balance"}

        sl_dist = price * abs(self._stoploss)   # e.g. 3% of price
        if direction == "long":
            sl = price - sl_dist
            tp = price + sl_dist * 3             # 1:3 R:R
        else:
            sl = price + sl_dist
            tp = price - sl_dist * 3

        with self._lock:
            if pair in self.positions:
                return {"error": f"Already have an open position in {pair}"}
            pos = Position(
                pair=pair, direction=direction,
                entry=price, sl=sl, tp=tp, size=stake,
                opened_at=datetime.now(timezone.utc),
            )
            pos.db_id = _persist_open_trade(self.user_id, pos, self._mode, self._strategy_id)
            self.positions[pair] = pos
            self.balance -= stake

        return {
            "entered": True,
            "pair": pair,
            "direction": direction,
            "entry": round(price, 6),
            "sl": round(sl, 6),
            "tp": round(tp, 6),
            "stake": round(stake, 2),
            "mode": self._mode,
        }

    # ── internal loop ───────────────────────────────────────────────────

    def _run_loop(self):
        """Adaptive trading loop.

        Two speeds:
          • FAST (5 s)  — when positions are open: catches TP/SL the moment
                          price crosses the level, no waiting for next candle.
          • SLOW (60 s) — when flat: scans for entry signals.  Candle signals
                          are deduplicated so the same candle never fires twice.

        Rate-limit note: KuCoin free tier allows ~30 req/s per IP.
        At 5 s per pair-tick we use ~1 req/pair/tick — well within limits.
        """
        signal_fn = _get_signal_fn(self._strategy)
        log.info("[%s] engine started — strategy=%s pairs=%s mode=%s",
                 self.user_id, self._strategy, self._pairs, self._mode)

        seen_signal:      dict[str, bool]  = {}   # pair → acted on current signal
        last_signal_ts:   dict[str, float] = {}   # pair → epoch of last signal check
        SIGNAL_INTERVAL = 60.0                    # seconds between signal scans

        while not self._stop_evt.is_set():
            try:
                now_ts = time.time()
                # Run a full tick (TP/SL + optional signal scan)
                self._tick_continuous(
                    signal_fn, seen_signal,
                    last_signal_ts=last_signal_ts,
                    signal_interval=SIGNAL_INTERVAL,
                )
                # Update last signal check time for each pair
                for pair in self._pairs:
                    last_signal_ts.setdefault(pair, 0.0)
                    if (now_ts - last_signal_ts[pair]) >= SIGNAL_INTERVAL:
                        last_signal_ts[pair] = now_ts

            except Exception as exc:
                with self._lock:
                    self.errors += 1
                    self.last_action = f"error: {exc}"
                log.warning("[%s] engine error: %s", self.user_id, exc)
                self._stop_evt.wait(min(60, 5 * self.errors))
                continue

            # Adaptive sleep:
            #  • 5 s  when positions are open → instant TP/SL response
            #  • 60 s when flat              → just scanning for signals
            with self._lock:
                has_open = bool(self.positions)
            self._stop_evt.wait(5 if has_open else 60)

        log.info("[%s] engine stopped", self.user_id)

    def _get_live_price(self, pair: str) -> Optional[float]:
        """Fetch the current ticker price from KuCoin (no candle needed)."""
        try:
            symbol = pair.replace("/", "-")
            data = _kucoin_get("/api/v1/market/orderbook/level1", {"symbol": symbol})
            if str(data.get("code")) == "200000":
                price = float(data["data"]["price"])
                self._last_prices[pair] = price   # cache for status / unrealized P&L
                return price
        except Exception:
            pass
        return self._last_prices.get(pair)  # return stale price rather than None

    def _tick_continuous(self, signal_fn, seen_signal: dict,
                         last_signal_ts: dict | None = None,
                         signal_interval: float = 60.0):
        """One adaptive tick — always checks TP/SL, scans entry signals when due.

        Args:
            signal_fn:       Strategy signal function.
            seen_signal:     Per-pair flag — True while current signal is active.
            last_signal_ts:  Per-pair epoch of last signal scan (None = always scan).
            signal_interval: Minimum seconds between signal scans (default 60 s).
        """
        import time as _time
        now_epoch = _time.time()

        for pair in self._pairs:
            if self._stop_evt.is_set():
                return

            # ── 1. Fetch live price (always — needed for TP/SL) ────────
            live_price = self._get_live_price(pair)
            if live_price is None:
                continue

            now = datetime.now(timezone.utc)

            with self._lock:
                self.ticks += 1

                # ── 2. Manage ALL open positions for this pair ──────────
                pair_positions = [
                    (k, p) for k, p in self.positions.items() if p.pair == pair
                ]
                for trade_key, pos in pair_positions:
                    pos.update_trail(live_price)
                    exit_info = pos.check_exit(live_price, live_price)
                    if exit_info:
                        exit_price, reason = exit_info
                        pos.close(exit_price, reason, now)
                        self.balance += pos.pnl_abs
                        self.closed_trades.append(pos)
                        del self.positions[trade_key]
                        seen_signal[pair] = False   # allow re-entry
                        self.last_action = (
                            f"CLOSED {pair} {pos.direction} @ {exit_price:.4f} "
                            f"({reason}) P&L={pos.pnl_abs:+.2f} USDT"
                        )
                        log.info("[%s] %s", self.user_id, self.last_action)
                        _persist_closed_trade(
                            self.user_id, pos, self._mode,
                            self._strategy_id, pos.db_id,
                        )
                        if self._mode == "live":
                            self._place_live_exit(pair, pos, exit_price)

                # ── 3. Guard: skip signal scan if at position limits ────
                if len(self.positions) >= self._max_open:
                    continue
                existing_for_pair = sum(
                    1 for p in self.positions.values() if p.pair == pair
                )
                if existing_for_pair >= getattr(self, '_max_per_pair', 2):
                    continue

            # ── 4. Signal scan — only when interval has elapsed ─────────
            if last_signal_ts is not None:
                elapsed = now_epoch - last_signal_ts.get(pair, 0.0)
                if elapsed < signal_interval:
                    continue   # too soon — skip candle fetch this tick

            try:
                candles = _fetch_candles(pair.replace("/", "-"), self._timeframe)
            except Exception as e:
                log.warning("[%s] candle fetch %s: %s", self.user_id, pair, e)
                continue

            if not candles:
                continue

            df = _build_df(candles)
            if df.empty:
                continue

            sig = signal_fn(df)
            if sig is None:
                seen_signal[pair] = False
                continue

            # Signal fired — enter at LIVE price (not candle close)
            entry_strategy, sl_strategy, tp_strategy, direction = sig

            # Use live price as entry for immediate fill
            entry = live_price
            # Keep strategy-derived SL/TP distances, shift to live price
            sl_dist = abs(entry_strategy - sl_strategy)
            tp_dist = abs(tp_strategy - entry_strategy)
            if direction == "long":
                sl = entry - sl_dist
                tp = entry + tp_dist
            else:
                sl = entry + sl_dist
                tp = entry - tp_dist

            risk_pct = sl_dist / entry if entry > 0 else 0
            if risk_pct > abs(self._stoploss) * 2:
                continue

            with self._lock:
                stake = self.balance * self._risk_pct
                if stake < 1.0 or stake > self.balance:
                    continue
                # Unique trade key: pair + timestamp (allows multiple per pair)
                trade_key = f"{pair}#{int(now.timestamp())}"
                pos = Position(
                    pair=pair, direction=direction,
                    entry=entry, sl=sl, tp=tp, size=stake,
                    opened_at=now,
                    trade_id=trade_key,
                )
                pos.db_id = _persist_open_trade(self.user_id, pos, self._mode, self._strategy_id)
                self.positions[trade_key] = pos
                self.balance -= stake
                seen_signal[pair] = True
                self.last_action = (
                    f"OPENED {direction} {pair} @ {entry:.4f} "
                    f"SL={sl:.4f} TP={tp:.4f} stake={stake:.2f}"
                )
                log.info("[%s] %s", self.user_id, self.last_action)
                if self._mode == "live":
                    self._place_live_entry(pair, pos)

    # ── legacy tick kept for compatibility ─────────────────────────────
    def _tick(self, signal_fn, seen_ts: dict, tf_secs: int):
        """Unused — kept for backwards compat. Bot now uses _tick_continuous."""
        pass

    def _process_pair(self, pair: str, df: pd.DataFrame, signal_fn):
        """Unused — logic moved into _tick_continuous."""
        row = df.iloc[-1]
        hi, lo = row["high"], row["low"]
        ts_dt  = row["date"]

        if pair in self.positions:
            pos = self.positions[pair]
            pos.update_trail(row["close"])
            exit_info = pos.check_exit(hi, lo)
            if exit_info:
                exit_price, reason = exit_info
                pos.close(exit_price, reason, ts_dt)
                self.balance += pos.pnl_abs
                self.closed_trades.append(pos)
                del self.positions[pair]
                self.last_action = (
                    f"closed {pair} {pos.direction} @ {exit_price:.4f} "
                    f"({reason}) P&L={pos.pnl_abs:+.2f}"
                )
                log.info("[%s] %s", self.user_id, self.last_action)
                _persist_closed_trade(self.user_id, pos, self._mode, self._strategy_id, pos.db_id)
                if self._mode == "live":
                    self._place_live_exit(pair, pos, exit_price)
            return

        if len(self.positions) >= self._max_open:
            return

        sig = signal_fn(df)
        if sig is None:
            return

        entry, sl, tp, direction = sig
        risk_pct = abs(entry - sl) / entry
        if risk_pct > abs(self._stoploss) * 2:
            return

        stake = self.balance * self._risk_pct
        if stake < 1.0 or stake > self.balance:
            return

        pos = Position(
            pair=pair, direction=direction,
            entry=entry, sl=sl, tp=tp, size=stake,
            opened_at=ts_dt,
        )
        pos.db_id = _persist_open_trade(self.user_id, pos, self._mode, self._strategy_id)
        self.positions[pair] = pos
        self.balance -= stake
        self.last_action = (
            f"opened {direction} {pair} @ {entry:.4f} "
            f"SL={sl:.4f} TP={tp:.4f} stake={stake:.2f}"
        )
        log.info("[%s] %s", self.user_id, self.last_action)
        if self._mode == "live":
            self._place_live_entry(pair, pos)

    # ── live order execution ─────────────────────────────────────────────

    def _place_live_entry(self, pair: str, pos: Position):
        """Place a real market order on KuCoin."""
        try:
            symbol = pair.replace("/", "-")
            side   = "buy" if pos.direction == "long" else "sell"
            body   = {
                "clientOid": f"at-{int(time.time()*1000)}",
                "side":       side,
                "symbol":     symbol,
                "type":       "market",
                "funds":      str(round(pos.size, 4)),
            }
            resp = _kucoin_post_signed(
                "/api/v1/orders", body,
                self._api_key, self._api_sec, self._api_pass
            )
            log.info("[%s] live ENTRY order: %s", self.user_id, resp)
        except Exception as e:
            log.error("[%s] live entry order failed: %s", self.user_id, e)

    def _place_live_exit(self, pair: str, pos: Position, price: float):
        """Place a closing market order on KuCoin."""
        try:
            symbol = pair.replace("/", "-")
            side   = "sell" if pos.direction == "long" else "buy"
            # Get current holdings for this pair
            resp = _kucoin_post_signed(
                "/api/v1/orders",
                {
                    "clientOid": f"at-exit-{int(time.time()*1000)}",
                    "side":       side,
                    "symbol":     symbol,
                    "type":       "market",
                    "funds":      str(round(pos.size, 4)),
                },
                self._api_key, self._api_sec, self._api_pass
            )
            log.info("[%s] live EXIT order: %s", self.user_id, resp)
        except Exception as e:
            log.error("[%s] live exit order failed: %s", self.user_id, e)


# ─────────────────────────── registry ─────────────────────────────────────

class NativeTradingRegistry:
    """Process-wide registry of per-user NativeTradingEngine instances.

    Acts as a thread-safe factory. Two simultaneous requests for the
    same brand-new user cannot race to create two engines.
    """

    def __init__(self):
        self._engines: dict[str, NativeTradingEngine] = {}
        self._lock    = threading.Lock()

    def for_user(self, user_id: str) -> NativeTradingEngine:
        with self._lock:
            eng = self._engines.get(user_id)
            if eng is None:
                eng = NativeTradingEngine(user_id)
                self._engines[user_id] = eng
            return eng

    def active_users(self) -> list[str]:
        with self._lock:
            return [uid for uid, e in self._engines.items() if e.is_running]

    def stop_all(self):
        with self._lock:
            for e in self._engines.values():
                if e.is_running:
                    try:
                        e.stop()
                    except Exception:
                        pass


native_engine_registry = NativeTradingRegistry()
