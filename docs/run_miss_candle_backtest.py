"""
Miss-Candle Sell-Only Strategy — Native Python Backtester
==========================================================
Downloads real OHLCV data from KuCoin public API (no API key needed)
and backtests the exact Miss-Candle rules:

Entry (SHORT):
  1. MACD histogram < 0 AND rising in magnitude (color 1/2/3)
  2. Previous candle is a miss-candle: high < 5-EMA
  3. Current candle closes bearish (close < open)
  4. Current candle does NOT break prev_high
  5. Current candle breaks prev_low → SHORT entry

Risk:
  SL  = miss-candle HIGH
  TP  = entry - 3 * (SL - entry)   [1:3 R:R]
  Trail at 1.5R: lock 50% of excursion

Saves results to docs/backtest_results.json
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pandas as pd
import numpy as np

OUT = Path(__file__).parent / "backtest_results.json"

KUCOIN_BASE = "https://api.kucoin.com"


# ─────────────────────────── data download ────────────────────────────────

def _fetch_kucoin_ohlcv(symbol: str, kline_type: str, start_ts: int, end_ts: int) -> list:
    """Download up to 1500-candle chunks from KuCoin public API."""
    TF_SECS = {
        "1min": 60, "3min": 180, "5min": 300, "15min": 900,
        "30min": 1800, "1hour": 3600, "4hour": 14400, "1day": 86400,
    }
    chunk = 1500 * TF_SECS.get(kline_type, 900)
    all_rows = []
    cur = start_ts
    with httpx.Client(timeout=30) as client:
        while cur < end_ts:
            end_chunk = min(cur + chunk, end_ts)
            resp = client.get(
                f"{KUCOIN_BASE}/api/v1/market/candles",
                params={"type": kline_type, "symbol": symbol,
                        "startAt": cur, "endAt": end_chunk},
            )
            data = resp.json()
            if str(data.get("code")) != "200000":
                raise RuntimeError(f"KuCoin error: {data}")
            rows = data.get("data", [])
            all_rows.extend(rows)
            cur = end_chunk + 1
            if not rows:
                break
    return all_rows


def get_ohlcv(pair: str, timeframe: str, days: int) -> pd.DataFrame:
    symbol = pair.replace("/", "-")
    kline_map = {
        "1m": "1min", "3m": "3min", "5m": "5min", "15m": "15min",
        "30m": "30min", "1h": "1hour", "4h": "4hour", "1d": "1day",
    }
    ktype = kline_map[timeframe]
    now = int(datetime.now(timezone.utc).timestamp())
    start = now - days * 86400

    print(f"  Downloading {pair} {timeframe} ({days}d) from KuCoin…")
    rows = _fetch_kucoin_ohlcv(symbol, ktype, start, now)
    if not rows:
        raise RuntimeError(f"No data returned for {pair} {timeframe}")

    df = pd.DataFrame(rows, columns=["ts", "open", "close", "high", "low", "vol", "turn"])
    df["date"] = pd.to_datetime(df["ts"].astype(int), unit="s", utc=True)
    for c in ["open", "high", "low", "close", "vol"]:
        df[c] = df[c].astype(float)
    df = df[["date", "open", "high", "low", "close", "vol"]].sort_values("date").reset_index(drop=True)
    print(f"  {len(df)} candles from {df['date'].iloc[0]} to {df['date'].iloc[-1]}")
    return df


# ─────────────────────────── indicators ───────────────────────────────────

def add_indicators(df: pd.DataFrame, ema_period: int = 5) -> pd.DataFrame:
    df = df.copy()

    # 5-EMA
    df["ema5"] = df["close"].ewm(span=ema_period, adjust=False).mean()

    # MACD (12, 26, 9)
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["macd_line"] = ema12 - ema26
    df["macd_signal"] = df["macd_line"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = df["macd_line"] - df["macd_signal"]

    # MACD color (magnitude vs prev bar — negative & strengthening = 1/2/3)
    df["macd_color"] = 0
    for i in range(1, len(df)):
        h = df.loc[i, "macd_hist"]
        ph = df.loc[i - 1, "macd_hist"]
        if h < 0 and abs(h) > abs(ph):
            df.loc[i, "macd_color"] = 1  # strengthening negative histogram

    return df


# ─────────────────────────── backtest engine ──────────────────────────────

def backtest(
    df: pd.DataFrame,
    starting_balance: float = 1000.0,
    risk_pct: float = 0.02,       # 2% of balance per trade
) -> dict:
    df = add_indicators(df)
    balance = starting_balance
    trades = []
    in_trade = False
    entry = sl = tp = trail_lock = None
    miss_high = miss_low = None
    entry_date = None

    for i in range(2, len(df)):
        row = df.iloc[i]
        prev = df.iloc[i - 1]
        prev2 = df.iloc[i - 2]

        # ── manage open trade ───────────────────────────────────────────
        if in_trade:
            low_i = row["low"]
            high_i = row["high"]
            close_i = row["close"]

            # Check trail trigger (1.5R)
            excursion = entry - low_i  # positive = moving in our favour
            half_r = (entry - tp) / 3 * 1.5  # 1.5 × 1R
            if excursion >= half_r and trail_lock is None:
                trail_lock = entry - excursion * 0.5  # lock 50% of excursion as new SL

            effective_sl = min(sl, trail_lock) if trail_lock else sl

            if high_i >= effective_sl:           # stop hit
                exit_price = effective_sl
                pnl = (entry - exit_price) / entry
                pnl_abs = balance * risk_pct * (pnl / (entry - sl) * entry)
                balance += pnl_abs
                trades.append({
                    "open_date": str(entry_date),
                    "close_date": str(row["date"]),
                    "entry": round(entry, 4),
                    "exit": round(exit_price, 4),
                    "sl": round(sl, 4),
                    "tp": round(tp, 4),
                    "exit_reason": "stop_loss" if trail_lock is None else "trail_stop",
                    "profit_pct": round(pnl * 100, 3),
                    "profit_abs": round(pnl_abs, 2),
                    "balance": round(balance, 2),
                })
                in_trade = False
                trail_lock = None

            elif low_i <= tp:                    # target hit
                exit_price = tp
                pnl = (entry - exit_price) / entry
                r_multiple = (entry - exit_price) / (sl - entry)  # negative sl distance
                pnl_abs = balance * risk_pct * r_multiple
                balance += pnl_abs
                trades.append({
                    "open_date": str(entry_date),
                    "close_date": str(row["date"]),
                    "entry": round(entry, 4),
                    "exit": round(exit_price, 4),
                    "sl": round(sl, 4),
                    "tp": round(tp, 4),
                    "exit_reason": "take_profit",
                    "profit_pct": round(pnl * 100, 3),
                    "profit_abs": round(pnl_abs, 2),
                    "balance": round(balance, 2),
                })
                in_trade = False
                trail_lock = None
            continue  # skip entry logic while in trade

        # ── scan for new setup ─────────────────────────────────────────
        # prev2 = miss candle candidate (i-2)
        # prev  = confirmation candle (i-1)
        # row   = entry bar (break of miss-candle low)

        # 1. MACD gate on confirmation bar
        if prev["macd_color"] != 1:
            continue

        # 2. Miss candle: prev2.high < prev2.ema5 (bar entirely below EMA)
        if prev2["high"] >= prev2["ema5"]:
            continue

        miss_high = prev2["high"]
        miss_low = prev2["low"]

        # 3. Confirmation bar bearish
        if prev["close"] >= prev["open"]:
            continue

        # 4. Confirmation bar does NOT break miss-candle high
        if prev["high"] > miss_high:
            continue

        # 5. Entry bar breaks miss-candle low
        if row["low"] > miss_low:
            continue

        # Valid setup → enter SHORT at miss_low
        entry = miss_low
        sl = miss_high
        risk = sl - entry
        if risk <= 0:
            continue
        tp = entry - 3 * risk  # 1:3 R:R
        in_trade = True
        entry_date = row["date"]
        trail_lock = None

    # ── compute metrics ───────────────────────────────────────────────
    if not trades:
        return {
            "total_trades": 0,
            "win_rate": 0,
            "total_profit_pct": 0,
            "max_drawdown_pct": 0,
            "sharpe": 0,
            "profit_factor": 0,
            "avg_win_pct": 0,
            "avg_loss_pct": 0,
            "trades": [],
            "equity_curve": [],
        }

    wins = [t for t in trades if t["profit_abs"] > 0]
    losses = [t for t in trades if t["profit_abs"] <= 0]
    win_rate = len(wins) / len(trades) if trades else 0

    gross_profit = sum(t["profit_abs"] for t in wins)
    gross_loss = abs(sum(t["profit_abs"] for t in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    balances = [starting_balance] + [t["balance"] for t in trades]
    equity = pd.Series(balances)
    returns = equity.pct_change().dropna()
    sharpe = (returns.mean() / returns.std() * (252 ** 0.5)) if returns.std() > 0 else 0

    running_max = equity.cummax()
    dd = (equity - running_max) / running_max * 100
    max_dd = float(dd.min())

    total_profit_pct = (balance - starting_balance) / starting_balance * 100

    avg_win = sum(t["profit_pct"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["profit_pct"] for t in losses) / len(losses) if losses else 0

    return {
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate * 100, 1),
        "total_profit_pct": round(total_profit_pct, 2),
        "total_profit_abs": round(balance - starting_balance, 2),
        "final_balance": round(balance, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe": round(float(sharpe), 3),
        "profit_factor": round(profit_factor, 2),
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "trades": trades,
        "equity_curve": balances,
    }


# ─────────────────────────── multi-run ────────────────────────────────────

def run_all():
    configs = [
        {"pair": "BTC/USDT", "tf": "5m",  "days": 90,  "label": "BTC/USDT 5m  (3 months)"},
        {"pair": "BTC/USDT", "tf": "15m", "days": 180, "label": "BTC/USDT 15m (6 months)"},
        {"pair": "ETH/USDT", "tf": "5m",  "days": 90,  "label": "ETH/USDT 5m  (3 months)"},
        {"pair": "SOL/USDT", "tf": "5m",  "days": 90,  "label": "SOL/USDT 5m  (3 months)"},
    ]
    all_results = {}
    for cfg in configs:
        print(f"\n{'='*60}")
        print(f"  {cfg['label']}")
        print('='*60)
        try:
            df = get_ohlcv(cfg["pair"], cfg["tf"], cfg["days"])
            result = backtest(df)
            result["pair"] = cfg["pair"]
            result["timeframe"] = cfg["tf"]
            result["days"] = cfg["days"]
            result["label"] = cfg["label"]
            all_results[cfg["label"]] = result
            print(f"  Trades: {result['total_trades']}  WR: {result['win_rate']}%  "
                  f"P&L: {result['total_profit_pct']:+.2f}%  DD: {result['max_drawdown_pct']:.2f}%  "
                  f"Sharpe: {result['sharpe']}")
        except Exception as e:
            print(f"  ERROR: {e}")
            all_results[cfg["label"]] = {"error": str(e)}

    OUT.write_text(json.dumps(all_results, indent=2, default=str))
    print(f"\nResults saved to {OUT}")
    return all_results


if __name__ == "__main__":
    run_all()
