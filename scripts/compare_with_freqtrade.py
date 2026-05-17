"""
Side-by-side comparison: our futures backtester vs Freqtrade (open source).

Purpose
-------
Sanity-check our `backend.services.futures_backtester` against a well-known
open-source backtest engine running the SAME strategy on the SAME KuCoin
futures candles. If both engines say "this strategy loses money," that's
strong evidence the result is honest. If they disagree wildly, the delta
table below the run pinpoints WHERE — number of trades, win rate, P&L,
max drawdown — so we know which subsystem to look at.

What this script is NOT
-----------------------
It is NOT a "ground truth oracle." Freqtrade has its own quirks:

  • Default fee is 0.05% per side; ours is 0% with informational KuCoin
    fees displayed separately. Easy: pass --fee 0 to Freqtrade.
  • Freqtrade does NOT model funding by default. Ours charges real
    KuCoin funding at 00/08/16 UTC settlements.
  • Freqtrade slippage is configurable; ours is 2-15 bps depending on
    fill type.
  • Concurrent positions: Freqtrade caps by max_open_trades; ours
    caps by free margin. With max_open_trades=999 they're equivalent.

The script prints a "Modelling differences explained" footer summarising
which deltas are expected vs which indicate a real bug.

Usage
-----
    python -m scripts.compare_with_freqtrade \
        --strategy MissCandleShortStrategy \
        --pair BTC/USDT:USDT \
        --timeframe 15m \
        --timerange 20260417-20260517 \
        --balance 1000 --leverage 10

Pair format: Freqtrade uses 'BTC/USDT:USDT' (the ':USDT' suffix marks it
as a perpetual futures contract). Our engine uses 'BTC/USDT'. The script
handles both.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

# Make the project root importable so we can reach backend.services.*
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ── Freqtrade harness ────────────────────────────────────────────────────

def _resolve_freqtrade_cmd() -> list[str]:
    """Same fallback chain as freqtrade_manager so this works on Windows
    pip installs that don't put `freqtrade` on PATH."""
    if shutil.which("freqtrade"):
        return [shutil.which("freqtrade")]
    try:
        import freqtrade  # noqa: F401
        return [sys.executable, "-m", "freqtrade"]
    except ImportError:
        raise RuntimeError(
            "freqtrade is not installed. Run: pip install freqtrade"
        )


def _build_freqtrade_config(pair_ft: str, timeframe: str, balance: float,
                            leverage: int, stoploss_pct: float,
                            stake_amount: float, user_dir: Path) -> Path:
    """Write a minimal Freqtrade config tuned for KuCoin futures backtest.

    Returns the path to the config file.
    """
    cfg = {
        "max_open_trades":        999,    # let signals stack like our engine
        "stake_currency":         "USDT",
        "stake_amount":           stake_amount,
        "tradable_balance_ratio": 1.0,
        "fiat_display_currency":  "USD",
        "dry_run":                True,
        "dry_run_wallet":         balance,
        "cancel_open_orders_on_exit": False,
        "trading_mode":           "futures",
        "margin_mode":            "isolated",
        "unfilledtimeout":        {"entry": 10, "exit": 10},
        "exchange": {
            "name":               "kucoin",
            "key":                "",
            "secret":             "",
            "ccxt_config":        {},
            "ccxt_async_config":  {},
            "pair_whitelist":     [pair_ft],
            "pair_blacklist":     [],
        },
        "pairlists": [{"method": "StaticPairList"}],
        "timeframe": timeframe,
        # Match our engine: 0% commission, slippage applied separately.
        "fee": 0.0,
        # Strategy stake config
        "use_exit_signal": True,
        "exit_profit_only": False,
        "ignore_roi_if_entry_signal": False,
        "stoploss": -abs(stoploss_pct) / 100.0,
        "internals": {"process_throttle_secs": 5},
        # Disable Freqtrade's API server — we read results from the JSON
        # export file, not via HTTP.
        "api_server": {"enabled": False},
    }
    # Per-pair leverage map for futures mode
    cfg["leverage"] = leverage   # informational — Freqtrade reads per-pair
    cfg["futures_funding_rate"] = 0   # turn OFF FT's stub funding so the
                                       # ONLY funding-rate difference comes
                                       # from our engine modelling real
                                       # KuCoin rates that FT doesn't have.

    cfg_path = user_dir / "config.json"
    cfg_path.write_text(json.dumps(cfg, indent=2))
    return cfg_path


def _run_freqtrade(strategy_name: str, strategy_dir: Path, pair_ft: str,
                   timeframe: str, timerange: str, config_path: Path,
                   user_dir: Path) -> dict:
    """Download data + run backtesting + parse result JSON."""
    cmd_base = _resolve_freqtrade_cmd()

    # 1) Download historical candles for the period
    dl_cmd = cmd_base + [
        "download-data",
        "--config",   str(config_path),
        "--userdir",  str(user_dir),
        "--pairs",    pair_ft,
        "--timeframes", timeframe,
        "--timerange", timerange,
        "--trading-mode", "futures",
    ]
    print(f"[freqtrade] downloading data: {pair_ft} {timeframe} {timerange}")
    r = subprocess.run(dl_cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        return {"error": "data download failed",
                "stderr": (r.stderr or "").splitlines()[-10:]}

    # 2) Run the backtest
    results_dir = user_dir / "backtest_results"
    results_dir.mkdir(exist_ok=True)
    before = {p.name for p in results_dir.glob("backtest-result-*.zip")
              if not p.name.endswith(".meta.json")}

    bt_cmd = cmd_base + [
        "backtesting",
        "--config",       str(config_path),
        "--strategy",     strategy_name,
        "--userdir",      str(user_dir),
        "--timerange",    timerange,
        "--strategy-path", str(strategy_dir),
        "--export",       "trades",
    ]
    print(f"[freqtrade] running backtest…")
    r = subprocess.run(bt_cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        return {"error": "backtest failed",
                "stderr": (r.stderr or "").splitlines()[-10:]}

    # 3) Parse result JSON (Freqtrade writes a zip with .json inside)
    new = sorted(
        (p for p in results_dir.glob("backtest-result-*.zip")
         if p.name not in before and not p.name.endswith(".meta.json")),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    if not new:
        return {"error": "no result file produced"}
    with zipfile.ZipFile(new[0]) as zf:
        result_json = next(n for n in zf.namelist()
                           if n.endswith(".json") and "_config" not in n)
        data = json.loads(zf.read(result_json))

    strat_key = next(iter(data.get("strategy", {})), None)
    sres = data.get("strategy", {}).get(strat_key, {}) if strat_key else {}
    return {
        "total_trades":   sres.get("total_trades", 0),
        "wins":           sres.get("wins", 0),
        "losses":         sres.get("losses", 0),
        "win_rate":       (sres.get("wins", 0) /
                           max(sres.get("total_trades", 0), 1)),
        "profit_total":   (sres.get("profit_total") or 0) * 100,   # → %
        "profit_abs":     sres.get("profit_total_abs") or 0,
        "max_drawdown":   (sres.get("max_drawdown_account") or
                           sres.get("max_drawdown") or 0) * 100,
        "raw":            sres,
    }


# ── Our engine ───────────────────────────────────────────────────────────

def _run_our_engine(strategy_name: str, strategy_code: str, pair_ours: str,
                    timeframe: str, timerange: str, balance: float,
                    leverage: int, stoploss_pct: float,
                    take_profit_pct: float) -> dict:
    from backend.services.futures_backtester import run_futures_backtest
    res = run_futures_backtest(
        strategy_name    = strategy_name,
        pairs            = [pair_ours],
        timeframe        = timeframe,
        timerange        = timerange,
        leverage         = leverage,
        starting_balance = balance,
        stoploss_pct     = stoploss_pct,
        take_profit_pct  = take_profit_pct,
        generated_code   = strategy_code,
    )
    m = res.get("metrics", {})
    return {
        "total_trades":  m.get("total_trades", 0),
        "wins":          m.get("winning_trades", 0),
        "losses":        m.get("losing_trades", 0),
        "win_rate":      m.get("win_rate", 0),
        "profit_total":  m.get("total_profit_pct", 0),
        "profit_abs":    m.get("total_profit_abs", 0),
        "max_drawdown":  m.get("max_drawdown", 0),
        "ev_pct":        m.get("expected_value_pct", 0),
        "breakeven_wr":  m.get("breakeven_win_rate", 0),
        "is_negative_ev": m.get("is_negative_ev", False),
    }


# ── Reporting ────────────────────────────────────────────────────────────

def _print_comparison(ours: dict, ft: dict) -> None:
    def fmt(val, suffix=""):
        if isinstance(val, float):
            return f"{val:+.2f}{suffix}" if suffix == "%" else f"{val:.4f}"
        return str(val)

    print()
    print("=" * 72)
    print("  COMPARISON: Our engine vs Freqtrade (same strategy + data)")
    print("=" * 72)
    cols = ["Metric", "Our engine", "Freqtrade", "Δ"]
    rows = [
        ("Total trades",   ours["total_trades"],   ft["total_trades"],
         ours["total_trades"] - ft["total_trades"]),
        ("Wins",           ours["wins"],           ft["wins"],
         ours["wins"] - ft["wins"]),
        ("Losses",         ours["losses"],         ft["losses"],
         ours["losses"] - ft["losses"]),
        ("Win rate %",     f"{ours['win_rate']*100:.2f}",
                           f"{ft['win_rate']*100:.2f}",
         f"{(ours['win_rate']-ft['win_rate'])*100:+.2f}"),
        ("Profit %",       f"{ours['profit_total']:+.2f}",
                           f"{ft['profit_total']:+.2f}",
         f"{ours['profit_total']-ft['profit_total']:+.2f}"),
        ("Profit USDT",    f"{ours['profit_abs']:+.2f}",
                           f"{ft['profit_abs']:+.2f}",
         f"{ours['profit_abs']-ft['profit_abs']:+.2f}"),
        ("Max drawdown %", f"{ours['max_drawdown']:.2f}",
                           f"{ft['max_drawdown']:.2f}",
         f"{ours['max_drawdown']-ft['max_drawdown']:+.2f}"),
    ]
    widths = [22, 14, 14, 12]
    print("  " + "  ".join(c.ljust(w) for c, w in zip(cols, widths)))
    print("  " + "  ".join("-" * w for w in widths))
    for r in rows:
        print("  " + "  ".join(str(v).ljust(w) for v, w in zip(r, widths)))
    print()
    print(f"  Math verdict (our engine): EV/trade {ours['ev_pct']:+.2f}%, "
          f"breakeven WR {ours['breakeven_wr']*100:.1f}%, "
          f"negative-EV: {ours['is_negative_ev']}")
    print()
    print("  Modelling differences (expected, not bugs):")
    print("  • Our engine charges real KuCoin funding at 00/08/16 UTC.")
    print("    Freqtrade futures_funding_rate=0 → FT P&L higher on longs in")
    print("    contango markets / higher on shorts in backwardation.")
    print("  • Our slippage: 2-15 bps depending on exit type. FT slippage=0.")
    print("  • Our entries at next-bar open. FT same (default since 2024.x).")
    print("  • Our liquidation uses KuCoin tier schedule; FT uses simplified.")
    print()
    print("  Verdict heuristic:")
    print("    Δ trades within ±5%        → engines agree on signal count")
    print("    Δ win rate within ±3 pp    → engines agree on exit timing")
    print("    Δ profit within ±10 pp     → expected modelling differences")
    print("    Larger deltas              → investigate which subsystem")
    print("=" * 72)


# ── CLI entrypoint ───────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--strategy",    required=True,
                   help="Class name inside the strategy file (e.g. MissCandleShortStrategy)")
    p.add_argument("--strategy-file", default=None,
                   help="Path to .py file containing the strategy class. "
                        "Defaults to strategies/templates/<strategy>.py.")
    p.add_argument("--pair",        default="BTC/USDT",
                   help="Pair in 'BTC/USDT' form (our engine). Freqtrade form derived.")
    p.add_argument("--timeframe",   default="15m")
    p.add_argument("--timerange",   default="20260417-20260517")
    p.add_argument("--balance",     type=float, default=1000)
    p.add_argument("--leverage",    type=int,   default=10)
    p.add_argument("--stoploss",    type=float, default=3.0,   help="SL as percent (e.g. 3 = 3 percent)")
    p.add_argument("--take-profit", type=float, default=6.0,   help="TP as percent")
    args = p.parse_args()

    pair_ours = args.pair                      # BTC/USDT
    pair_ft   = args.pair + ":USDT"            # BTC/USDT:USDT (perpetual)

    # Locate strategy file
    if args.strategy_file:
        strat_file = Path(args.strategy_file)
    else:
        # Default: strategies/templates/<snake_case>.py
        snake = "".join(["_" + c.lower() if c.isupper() and i else c.lower()
                         for i, c in enumerate(args.strategy)])
        snake = snake.removesuffix("_strategy")
        strat_file = PROJECT_ROOT / "strategies" / "templates" / f"{snake}.py"
    if not strat_file.exists():
        print(f"ERROR: strategy file not found: {strat_file}", file=sys.stderr)
        return 1
    strategy_code = strat_file.read_text()
    strategy_dir  = strat_file.parent

    # --- Run our engine ---
    print(f"[ours] running futures_backtester…")
    ours = _run_our_engine(
        strategy_name   = args.strategy,
        strategy_code   = strategy_code,
        pair_ours       = pair_ours,
        timeframe       = args.timeframe,
        timerange       = args.timerange,
        balance         = args.balance,
        leverage        = args.leverage,
        stoploss_pct    = args.stoploss,
        take_profit_pct = args.take_profit,
    )

    # --- Run Freqtrade in a temp userdir ---
    with tempfile.TemporaryDirectory(prefix="ft_compare_") as tmp:
        user_dir = Path(tmp)
        (user_dir / "data").mkdir(exist_ok=True)
        stake = args.balance * 0.05   # match our risk_per_trade=0.05
        cfg = _build_freqtrade_config(pair_ft, args.timeframe, args.balance,
                                      args.leverage, args.stoploss, stake,
                                      user_dir)
        ft = _run_freqtrade(args.strategy, strategy_dir, pair_ft,
                            args.timeframe, args.timerange, cfg, user_dir)

    if "error" in ft:
        print()
        print(f"  ❌ Freqtrade run failed: {ft['error']}")
        for line in ft.get("stderr", []):
            print(f"     {line}")
        print()
        print("  Our engine ran successfully:")
        for k, v in ours.items():
            print(f"     {k}: {v}")
        return 2

    _print_comparison(ours, ft)
    return 0


if __name__ == "__main__":
    sys.exit(main())
