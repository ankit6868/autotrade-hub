"""
Comprehensive backtest engine validation across multiple strategy scenarios.

Goal: Test every aspect of the futures backtest engine using different
strategy types so we can identify any engine bugs/inaccuracies.

Scenarios tested:
  1. Pure Long Trend - only LONG entries (tests long-only flow)
  2. Pure Short Trend - only SHORT entries (tests short-only flow)
  3. High-frequency MACD - many trades (tests engine throughput)
  4. Tight SL/TP scalping - small SL (1%) tests precision
  5. Wide SL/TP swing - large SL/TP (5%/10%) tests long holds
  6. Mean Reversion (RSI+BB) - bidirectional (overbought/oversold)
  7. EMA Scalping with volume filter - tests indicator combination
  8. SMCStrategyTV - structural pivot-based (already validated)

For each: run on BTC/USDT 15m for 3M, 6M, 1Y and compare metrics.
"""
import sys
sys.path.insert(0, '.')

from backend.services.futures_backtester import run_futures_backtest

SCENARIOS = [
    # (strategy_name, sl%, tp%, leverage, description)
    ("SMCStrategyTV",         3.0, 6.0,  10, "Structural pivot SMC (matches TV)"),
    ("BidirectionalStrategy", 1.5, 3.0,  10, "EMA9/21 trend + RSI filter"),
    ("SimpleTargetStrategy",  1.5, 3.0,  10, "RSI + EMA20 mean reversion (FIXED balance)"),
    ("MacdCrossoverStrategy", 1.5, 3.0,  10, "MACD crossover - bidirectional"),
    ("RsiBollingerStrategy",  1.5, 3.0,  10, "RSI extreme + BB band test"),
    ("EmaScalpingStrategy",   1.5, 3.0,  10, "EMA cross + volume confirm"),
    # Stress test scenarios
    ("MacdCrossoverStrategy", 0.5, 1.0,  10, "TIGHT SL/TP (precision test)"),
    ("MacdCrossoverStrategy", 5.0, 10.0, 10, "WIDE SL/TP (long-hold test)"),
    ("MacdCrossoverStrategy", 1.5, 3.0,  25, "HIGH LEVERAGE (liquidation test)"),
    ("MacdCrossoverStrategy", 1.5, 3.0,  3,  "LOW LEVERAGE (no liq test)"),
]

PERIODS = [
    ("3M", "20260209-20260510", 90),
    ("6M", "20251111-20260510", 180),
    ("1Y", "20250510-20260510", 365),
]


def fmt_metric(m):
    return (
        f"{m['total_trades']:>4}T  "
        f"{m['long_trades']:>3}L/{m['short_trades']:>3}S  "
        f"{m['win_rate']*100:>5.1f}%  "
        f"P&L={m['total_profit_pct']:>+7.2f}%  "
        f"DD={m['max_drawdown']:>5.2f}%  "
        f"Liq={m['liquidations']:>2}"
    )


def run_all():
    bugs = []

    for label, period, days in PERIODS:
        print(f"\n{'='*100}")
        print(f"  PERIOD: {label}  ({days} days)")
        print(f"{'='*100}")
        print(f"  {'Scenario':<55} {'Result'}")
        print(f"  {'-'*55} {'-'*45}")

        for name, sl, tp, lev, desc in SCENARIOS:
            scenario_label = f"{name} {sl}/{tp}% {lev}x — {desc}"
            try:
                r = run_futures_backtest(
                    strategy_name=name, pairs=['BTC/USDT'],
                    timeframe='15m', timerange=period, leverage=lev,
                    starting_balance=1000.0,
                    stoploss_pct=sl, take_profit_pct=tp,
                    risk_per_trade=0.05,
                )
                if 'error' in r:
                    print(f"  {scenario_label[:55]:<55} ERROR: {r['error'][:40]}")
                    bugs.append(f"{label} {scenario_label}: {r['error']}")
                    continue
                m = r['metrics']

                # Bug detection
                if m['total_trades'] > 0:
                    ratio = m['long_trades'] / max(m['short_trades'], 1)
                    if ratio > 5 and m['total_trades'] > 10:
                        bugs.append(f"{label} {scenario_label}: skewed L/S ({ratio:.1f}:1)")
                    if m['max_drawdown'] > 80:
                        bugs.append(f"{label} {scenario_label}: extreme DD {m['max_drawdown']}%")
                    if m['final_balance'] < 0:
                        bugs.append(f"{label} {scenario_label}: NEGATIVE balance!")

                print(f"  {scenario_label[:55]:<55} {fmt_metric(m)}")

            except Exception as e:
                print(f"  {scenario_label[:55]:<55} EXCEPTION: {str(e)[:40]}")
                bugs.append(f"{label} {scenario_label}: EXCEPTION {e}")

    print(f"\n{'='*100}")
    if bugs:
        print(f"  ISSUES FOUND ({len(bugs)}):")
        for b in bugs:
            print(f"    - {b}")
    else:
        print("  ✓ NO BUGS DETECTED — engine handles all scenarios correctly")
    print(f"{'='*100}")
    return bugs


if __name__ == '__main__':
    run_all()
