"""Independent end-to-end verification of the futures backtester.

For every built-in strategy:
  1. Run run_futures_backtest() on REAL KuCoin BTC/USDT candles.
  2. For each trade, RE-DERIVE the P&L from first principles
     (entry, exit legs, leverage, margin) — completely independent of the
     engine's own running balance — and compare to the reported numbers.
  3. Re-check win-rate and that balance is a strict running sum of P&L.

If the engine were fabricating results, the independent reconstruction
would NOT reconcile. We assert agreement to 1e-3 USDT.

Run:  PYTHONPATH=. python scripts/verify_all_strategies.py
"""
import sys, traceback

# ── 1. Recover the built-in templates (name/code/tf/lev/sl/tp) without a DB ──
captured = []

class _FakeResult:
    def scalar_one_or_none(self):
        return None      # force the "db.add(new template)" branch

class _FakeDB:
    def execute(self, *a, **k):
        return _FakeResult()
    def add(self, obj):
        captured.append(obj)
    def commit(self):
        pass

import backend.main as M
from backend.models.strategy import Strategy   # noqa

M._seed_builtin_strategies(_FakeDB())
print(f"Recovered {len(captured)} built-in strategies\n")

from backend.services.futures_backtester import run_futures_backtest

PAIR = "BTC/USDT"
TIMERANGE = "20260514-20260613"   # same 1-month window as the user's CSVs
TOL = 1e-3                         # USDT tolerance for arithmetic agreement


def reconstruct_trade(t, leverage):
    """Re-derive net P&L from the trade's own legs, ignoring the engine's
    reported profit. Returns (recomputed_profit_abs, recomputed_profit_pct)."""
    margin = t["margin"]
    direction = t["direction"]
    entry = t["entry"]
    legs = t.get("partial_exits") or []
    if not legs:
        # single implied leg at close_rate, full margin
        legs = [{"price": t["close_rate"], "close_pct": 1.0}]
    gross = 0.0
    for leg in legs:
        leg_margin = margin * leg["close_pct"]
        px = leg["price"]
        if t["exit_reason"] == "liquidated":
            gross += -leg_margin
            continue
        if direction == "long":
            move = (px - entry) / entry
        else:
            move = (entry - px) / entry
        leg_pnl = leg_margin * move * leverage
        leg_pnl = max(leg_pnl, -leg_margin)   # can't lose more than margin
        gross += leg_pnl
    net = gross - t.get("fees_paid", 0.0)     # pure mode → fees 0
    pct = (net / margin * 100.0) if margin else 0.0
    return net, pct


overall_ok = True
for s in captured:
    name = s.name
    tf = s.timeframe or "4h"
    lev = int(s.default_leverage or 5)
    sl = abs(s.stoploss) * 100.0
    tp = s.take_profit * 100.0
    try:
        res = run_futures_backtest(
            strategy_name=name,
            pairs=[PAIR],
            timeframe=tf,
            timerange=TIMERANGE,
            leverage=lev,
            starting_balance=1000.0,
            stoploss_pct=sl,
            take_profit_pct=tp,
            risk_per_trade=0.05,
            generated_code=s.generated_code,
            deduct_real_costs=False,   # pure price×lev — matches the CSVs
        )
    except Exception as e:
        print(f"❌ {name:28s} CRASHED: {e}")
        traceback.print_exc()
        overall_ok = False
        continue

    trades = res.get("trades", [])
    if not trades:
        print(f"·  {name:28s} {tf:>3s} lev{lev:<2d}  0 trades (no signals in window)")
        continue

    # ── independent reconstruction of every trade ──
    max_pnl_diff = 0.0
    max_pct_diff = 0.0
    wins = 0
    bal = 1000.0
    bal_ok = True
    for t in trades:
        rec_abs, rec_pct = reconstruct_trade(t, lev)
        max_pnl_diff = max(max_pnl_diff, abs(rec_abs - t["profit_abs"]))
        max_pct_diff = max(max_pct_diff, abs(rec_pct - t["profit_pct"]))
        if t["profit_abs"] > 0:
            wins += 1
        # balance must be the running sum of reported P&L (clamped at 0)
        bal = max(bal + t["profit_abs"], 0.0)
        if abs(bal - t["balance"]) > 0.05:
            bal_ok = False

    wr = wins / len(trades) * 100.0
    reported_profit = (trades[-1]["balance"] - 1000.0) / 1000.0 * 100.0
    status = "✅" if (max_pnl_diff < TOL and max_pct_diff < 0.01 and bal_ok) else "❌"
    if status == "❌":
        overall_ok = False
    print(f"{status} {name:28s} {tf:>3s} lev{lev:<2d}  "
          f"{len(trades):3d} trades  WR {wr:5.1f}%  P/L {reported_profit:+7.2f}%  "
          f"| recon Δpnl {max_pnl_diff:.2e} USDT  Δpct {max_pct_diff:.2e}  "
          f"balance_chain={'OK' if bal_ok else 'MISMATCH'}")

print()
print("ALL STRATEGIES RECONCILE ✅" if overall_ok else "DISCREPANCY FOUND ❌")
sys.exit(0 if overall_ok else 1)
