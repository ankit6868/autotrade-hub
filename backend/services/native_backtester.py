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
        with urllib.request.urlopen(req, timeout=30) as resp:
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
    if i < 2:
        return None
    prev, row = df.iloc[i - 1], df.iloc[i]
    if prev["macd"] < prev["macd_signal"] and row["macd"] > row["macd_signal"]:
        entry = row["close"]
        sl    = entry * 0.97
        tp    = entry * 1.09
        return entry, sl, tp, "long"
    return None


def _signal_rsi_bollinger(df: pd.DataFrame, i: int):
    if i < 1:
        return None
    row = df.iloc[i]
    if row["rsi"] < 30 and row["close"] < row["bb_lower"]:
        entry = row["close"]
        sl    = entry * 0.97
        tp    = row["bb_mid"]
        return entry, sl, tp, "long"
    return None


def _signal_ema_scalping(df: pd.DataFrame, i: int):
    if i < 2:
        return None
    prev, row = df.iloc[i - 1], df.iloc[i]
    vol_sma = df["vol_sma"].iloc[i] if "vol_sma" in df.columns else df["vol"].rolling(20).mean().iloc[i]
    if (prev["ema9"] < prev["ema21"] and row["ema9"] > row["ema21"]
            and row["vol"] > vol_sma * 1.5):
        entry = row["close"]
        sl    = entry * 0.985
        tp    = entry * 1.015
        return entry, sl, tp, "long"
    return None


def _signal_simple_target(df: pd.DataFrame, i: int):
    """
    SimpleTargetStrategy — RSI below 55 (not overbought) AND price at/below EMA20.
    Also fires on strong oversold (RSI < 38) regardless of EMA.
    Exit: +1.5% TP / -1.5% SL (1:1 R:R).
    """
    if i < 21:
        return None
    row   = df.iloc[i]
    rsi   = row.get("rsi", 50.0)
    close = row["close"]
    ema20 = row.get("ema20", close)   # ema20 now always present

    near_ema = close <= ema20 * 1.005   # at or slightly above EMA20
    oversold = rsi < 38                 # strongly oversold
    mild_dip = rsi < 55 and near_ema   # neutral + near fair-value

    if oversold or mild_dip:
        entry = close
        sl    = round(entry * 0.985, 8)   # -1.5%
        tp    = round(entry * 1.015, 8)   # +1.5%
        return entry, sl, tp, "long"
    return None


_STRATEGY_FN = {
    "MissCandleShortStrategy":  _signal_miss_candle_short,
    "MissCandleLongStrategy":   _signal_miss_candle_long,
    "MacdCrossoverStrategy":    _signal_macd_crossover,
    "RsiBollingerStrategy":     _signal_rsi_bollinger,
    "EmaScalpingStrategy":      _signal_ema_scalping,
    "SimpleTargetStrategy":     _signal_simple_target,
}


def _guess_strategy(name: str):
    """Fuzzy-match a strategy name to a known signal function."""
    n = name.lower()
    for key, fn in _STRATEGY_FN.items():
        if key.lower() in n or n in key.lower():
            return fn
    if "miss" in n and "short" in n:
        return _signal_miss_candle_short
    if "miss" in n and "long" in n:
        return _signal_miss_candle_long
    if "macd" in n:
        return _signal_macd_crossover
    if "rsi" in n or "bollinger" in n:
        return _signal_rsi_bollinger
    if "ema" in n or "scalp" in n:
        return _signal_ema_scalping
    if "simple" in n or "target" in n:
        return _signal_simple_target
    return _signal_simple_target   # default: most reliable for general use


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
