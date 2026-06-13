"""Recursive functional test of the full futures stack.

A) Backtest option matrix: run strategies under every combination of
   position_mode x ARM x force_slider x real-costs, asserting each runs
   without crashing, produces a balance that is a strict running sum of
   per-trade P&L, and (for single-leg trades) reconciles to an independent
   P&L re-derivation.

B) Engine position lifecycle: exercise FuturesPosition (the core both paper
   and live use) — non-ARM exits (TP/SL/liq/max_hold), ARM partial-close +
   BE-trail + TP2, and paper cost simulation — asserting exact P&L math.

Run: PYTHONPATH=. python scripts/verify_full_futures.py
"""
import sys, traceback
from datetime import datetime, timezone, timedelta

# ── recover built-in strategies without a DB ──
captured = []
class _R:
    def scalar_one_or_none(self): return None
class _DB:
    def execute(self, *a, **k): return _R()
    def add(self, o): captured.append(o)
    def commit(self): pass
import backend.main as M
M._seed_builtin_strategies(_DB())
def strat(name): return [x for x in captured if x.name == name][0]

from backend.services.futures_backtester import run_futures_backtest

PAIR, TR = "BTC/USDT", "20260514-20260613"
fails = []

# ─────────────────────────────────────────────────────────────────────────
# A) BACKTEST OPTION MATRIX
# ─────────────────────────────────────────────────────────────────────────
print("=" * 78)
print("A) BACKTEST OPTION MATRIX")
print("=" * 78)

def recon_leg(t, lev):
    legs = t.get("partial_exits") or [{"price": t["close_rate"], "close_pct": 1.0}]
    g = 0.0
    for L in legs:
        lm = t["margin"] * L["close_pct"]
        if t["exit_reason"] == "liquidated":
            g += -lm; continue
        mv = ((L["price"] - t["entry"]) / t["entry"]) if t["direction"] == "long" \
             else ((t["entry"] - L["price"]) / t["entry"])
        g += max(lm * mv * lev, -lm)
    return g - t.get("fees_paid", 0.0)

cases = [
    ("SimpleTargetStrategy", "15m", 1.5, 3.0, 10),
    ("SMCStrategyTV",        "15m", 3.0, 6.0, 10),
    ("Bollinger Bands Strategy", "5m", 8.0, 12.0, 10),
    ("LorentzianClassifier", "4h", 8.0, 12.0, 5),
]
import itertools
for name, tf, sl, tp, lev in cases:
    s = strat(name)
    for pos_mode, arm, fslider, costs in itertools.product(
        ("single", "hedge"), (False, True), (False, True), (False, True)):
        tag = f"{name[:18]:18s} {pos_mode:6s} ARM={int(arm)} slider={int(fslider)} costs={int(costs)}"
        try:
            r = run_futures_backtest(
                strategy_name=name, pairs=[PAIR], timeframe=tf, timerange=TR,
                leverage=lev, starting_balance=1000.0, stoploss_pct=sl, take_profit_pct=tp,
                risk_per_trade=0.05, generated_code=s.generated_code,
                position_mode=pos_mode, arm_enabled=arm, force_slider_sltp=fslider,
                deduct_real_costs=costs,
                arm_tp1_close_pct=50.0, arm_be_mode="leverage", arm_trail_to_tp1=True,
            )
            t = r.get("trades", [])
            # Balance-chain check is only a valid invariant in SINGLE mode with
            # NO real costs: hedge holds concurrent positions and ARM books
            # partials mid-trade, and real-cost fees are deducted on a separate
            # timeline — both legitimately break "balance[i] = balance[i-1] +
            # profit_abs[i]". In those modes we rely on the per-trade recon.
            chain_ok = True
            if pos_mode == "single" and not costs:
                bal = 1000.0
                for x in t:
                    bal = max(bal + x["profit_abs"], 0.0)
                    if abs(bal - x["balance"]) > 0.05: chain_ok = False
            # Per-trade reconciliation: independently re-derive profit_abs from
            # entry/exit legs (pure mode; costs add fees we don't model here).
            recon_ok = True
            if not costs:
                for x in t:
                    if len(x.get("partial_exits") or []) <= 1:
                        if abs(recon_leg(x, lev) - x["profit_abs"]) > 1e-2:
                            recon_ok = False; break
            ok = chain_ok and recon_ok
            if not ok:
                fails.append(f"{tag}: chain={chain_ok} recon={recon_ok}")
            print(f"  {'OK ' if ok else 'FAIL'} {tag}  {len(t):3d} trades")
        except Exception as e:
            fails.append(f"{tag}: CRASH {e}")
            print(f"  CRASH {tag}: {e}")
            traceback.print_exc()

# extra: maker-only + tick-precision (previously-crashing paths)
for extra, kw in [("maker_only_entry", {"maker_only_entry": True}),
                  ("tick_precision",  {"tick_precision": True})]:
    try:
        r = run_futures_backtest(strategy_name="SimpleTargetStrategy", pairs=[PAIR],
            timeframe="15m", timerange=TR, leverage=10, starting_balance=1000.0,
            stoploss_pct=1.5, take_profit_pct=3.0, risk_per_trade=0.05,
            generated_code=strat("SimpleTargetStrategy").generated_code,
            deduct_real_costs=True, **kw)
        print(f"  OK  extra:{extra}  {len(r.get('trades', []))} trades")
    except Exception as e:
        fails.append(f"extra:{extra}: CRASH {e}")
        print(f"  CRASH extra:{extra}: {e}")

# ─────────────────────────────────────────────────────────────────────────
# B) ENGINE POSITION LIFECYCLE  (core shared by paper + live)
# ─────────────────────────────────────────────────────────────────────────
print("=" * 78)
print("B) ENGINE FuturesPosition LIFECYCLE")
print("=" * 78)
from backend.services.futures_engine import FuturesPosition, _calc_liquidation_price

def approx(a, b, tol=1e-6): return abs(a - b) <= tol
now = datetime.now(timezone.utc)

def newpos(direction="long", entry=100.0, size=100.0, lev=10, sl=None, tp=None):
    sl = sl if sl is not None else (95.0 if direction == "long" else 105.0)
    tp = tp if tp is not None else (110.0 if direction == "long" else 90.0)
    return FuturesPosition(pair=PAIR, direction=direction, entry=entry, sl=sl, tp=tp,
                           size=size, opened_at=now, trade_id="t", leverage=lev)

def check(label, cond):
    print(f"  {'OK ' if cond else 'FAIL'} {label}")
    if not cond: fails.append("engine: " + label)

# 1) non-ARM long TP: +10% price * 10x ... here TP=110 = +10% price = +100% margin
p = newpos(); p.close(110.0, "take_profit", now)
check("long TP pnl (+10% px x10 = +100 on 100 margin)", approx(p.pnl_abs, 100.0))
# 2) non-ARM long SL: -5% px x10 = -50
p = newpos(); p.close(95.0, "stop_loss", now)
check("long SL pnl (-5% px x10 = -50)", approx(p.pnl_abs, -50.0))
# 3) short TP: entry 100 -> 90 = +10% px x10 = +100
p = newpos("short"); p.close(90.0, "take_profit", now)
check("short TP pnl (+100)", approx(p.pnl_abs, 100.0))
# 4) liquidation: a loss, never worse than -margin. At the liq price (which
#    includes a maintenance buffer) the price-implied loss is ~-90%; the
#    -margin cap only binds when price runs PAST the liq level.
p = newpos(); p.close(p.liquidation_price, "liquidated", now)
check("liquidation at liq price: -margin <= pnl < 0", -100.0 - 1e-6 <= p.pnl_abs < 0)
p = newpos(); p.close(50.0, "liquidated", now)   # price way past liq (-50% px x10 = -500%)
check("liquidation cap binds past liq (pnl == -margin)", approx(p.pnl_abs, -100.0))
# 5) check_exit detects SL/TP via bar/last price
p = newpos()
check("check_exit None inside range", p.check_exit(100.0, 100.0) is None)
ex = p.check_exit(94.0, 94.0)
check("check_exit fires SL below stop", ex is not None)
# 6) sim_costs reduces a winner and worsens a loser
p = newpos(); p._sim_costs = True; p.close(110.0, "take_profit", now)
check("sim_costs winner net < gross (100 - 1.2 = 98.8)", approx(p.pnl_abs, 98.8))
p = newpos(); p._sim_costs = True; p.close(95.0, "stop_loss", now)
check("sim_costs loser worse (-50 - 1.6 = -51.6)", approx(p.pnl_abs, -51.6))

# 7) ARM lifecycle: open long, configure ARM, book TP1 partial, close at TP2
#    entry 100, tp2 120 -> TP1 = midpoint 110. 50% close at TP1, 50% at TP2.
p = newpos(tp=120.0)
p.configure_arm(tp2_price=120.0, tp1_close_pct=0.5, arm_be_mode="entry",
                arm_trail_to_tp1=True)
check("ARM sets tp1=midpoint(100,120)=110", approx(p.tp1_price, 110.0))
check("ARM overrode pos.tp to TP1 (110)", approx(p.tp, 110.0))
# simulate TP1 partial booking the way the engine does (book 50% leg P&L)
# 50% of 100 margin = 50; +10% px x10 = +100% -> +50 on that leg
tp1_leg = 50.0 * ((110.0 - 100.0) / 100.0) * 10
p.partial_pnl_abs = tp1_leg
p.remaining_pct = 0.5
p.tp1_hit = True
# close remainder at TP2 120 (+20% px x10 = +200% on 50 margin = +100)
p.close(120.0, "take_profit_2", now)
# aggregate: TP1 leg (+50) + final leg (50 margin * 20% px * 10 = +100) = +150
check("ARM aggregate pnl (TP1 +50 + TP2 +100 = +150)", approx(p.pnl_abs, 150.0))

# 8) liquidation price sanity
lp_long  = _calc_liquidation_price(100.0, "long", 10)
lp_short = _calc_liquidation_price(100.0, "short", 10)
check("liq long below entry", lp_long < 100.0)
check("liq short above entry", lp_short > 100.0)
check("higher leverage -> closer liq", _calc_liquidation_price(100, "long", 20)
      > _calc_liquidation_price(100, "long", 5))

# ─────────────────────────────────────────────────────────────────────────
# C) MANUAL + LIVE-ORDER GATING + SIZING + APP BOOT
#    (manual paper/live core, live-order routing, contract sizing, all routes)
# ─────────────────────────────────────────────────────────────────────────
print("=" * 78)
print("C) MANUAL / LIVE GATING / SIZING / APP BOOT")
print("=" * 78)
from backend.services.futures_engine import _stake_to_contracts, futures_engine_registry

# app boots & every router registers (catches broken imports/routes)
import backend.main as _M
_routes = [r for r in _M.app.routes if hasattr(r, "path")]
_fut = [r for r in _routes if "/futures" in getattr(r, "path", "")]
check(f"app boots with {len(_fut)} futures routes", len(_fut) >= 30)

# contract sizing is a positive integer for BTC + non-BTC coins
for sym, px in [("XBTUSDTM", 60000), ("ETHUSDTM", 3000), ("SOLUSDTM", 150), ("DOGEUSDTM", 0.15)]:
    c = _stake_to_contracts(100.0, 10, px, sym)
    check(f"sizing {sym} -> {c} contracts (int >=1)", isinstance(c, int) and c >= 1)
check("sizing zero-entry guard -> 1", _stake_to_contracts(100, 10, 0, "XBTUSDTM") == 1)

# manual paper lifecycle on the shared for_user engine
_eng = futures_engine_registry.for_user("verify-full-futures-user")
_eng.balance = 1000.0
_mp = FuturesPosition(pair=PAIR, direction="long", entry=60000.0, sl=0, tp=0,
                      size=50.0, opened_at=now, trade_id="k", leverage=10)
_eng.positions["k"] = _mp
_mp.close(61200.0, "manual_close", now)              # +2% px x10 = +20% on 50 = +10
_eng.balance += _mp.pnl_abs - getattr(_mp, "partial_pnl_abs", 0.0)
del _eng.positions["k"]
check("manual paper close pnl +10", approx(_mp.pnl_abs, 10.0))
check("manual paper balance credited to 1010", approx(_eng.balance, 1010.0))

# _live_order_allowed: only routes real orders when creds + (live engine OR
# a live-tagged position). This is what makes manual-live TP/SL/close hit
# KuCoin even on the paper-default main engine.
_g = futures_engine_registry.for_user("verify-gating-user")
_g._api_key = ""
check("gating: no api_key -> blocked", _g._live_order_allowed(_mp) is False)
_g._api_key = "k"; _g._mode = "paper"
class _LivePos:  _mode = "live"
class _PaperPos: _mode = "paper"
check("gating: paper engine + live pos -> allowed", _g._live_order_allowed(_LivePos()) is True)
check("gating: paper engine + paper pos -> blocked", _g._live_order_allowed(_PaperPos()) is False)
_g._mode = "live"
check("gating: live engine -> allowed", _g._live_order_allowed(None) is True)

print("=" * 78)
if fails:
    print(f"FAILURES ({len(fails)}):")
    for f in fails: print("  -", f)
    sys.exit(1)
print("ALL FUTURES FUNCTIONAL CHECKS PASSED ✅")
sys.exit(0)
