"""Definitive test: does the bot's live tick loop actually DETECT a signal
edge and OPEN a position (paper mode)?

We feed the engine a real historical candle window that ENDS exactly on a
Bollinger LONG signal-edge bar (by monkeypatching the candle fetch + live
price), start the real FuturesEngine in paper mode, let its tick loop run a
few seconds, then assert it opened a long with the right entry/SL/TP.

This exercises the full live orchestration: fetch -> build df -> evaluate
strategy -> signal_fn edge -> bias/edge/cooldown gates -> risk_engine plan
-> open position. (Paper mode: no real KuCoin order; live mode adds only the
order-placement layer, verified separately.)

Run: PYTHONPATH=. python scripts/verify_bot_fires.py
"""
import os, tempfile, time, warnings
warnings.filterwarnings("ignore")
os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/t.db"

import backend.main as M
from backend.models.database import init_db; init_db()
from backend.models import SessionLocal
from backend.models.strategy import Strategy
from sqlalchemy import select
with SessionLocal() as db:
    M._seed_builtin_strategies(db)
    srow = db.execute(select(Strategy).where(Strategy.name == "Bollinger Bands Strategy")).scalar_one_or_none()
    strat_id, strat_code = srow.id, srow.generated_code

from backend.services.native_backtester import load_futures_ohlcv
from backend.services.strategy_runner import evaluate_strategy, make_signal_fn_from_df
from backend.services import futures_engine as FE

# ── 1. find a real LONG signal-edge bar in Bollinger 5m data ──
end = int(time.time()); start = end - 31 * 24 * 3600
hist = load_futures_ohlcv("BTC/USDT", "5m", start, end)
d = evaluate_strategy(strat_code, hist, pair="BTC/USDT", execution_tf="5m")
el = d["enter_long"].fillna(0).astype(int).to_numpy()
edge = None
for i in range(210, len(el)):
    if el[i] == 1 and el[i-1] == 0:
        edge = i; break
assert edge is not None, "no long edge found"
entry_close = float(d.iloc[edge]["close"])
print(f"Found LONG signal edge at bar {edge}, close={entry_close}")

# ── 2. build the candle window ending exactly at the edge bar ──
window = hist.iloc[edge-199:edge+1]   # 200 bars ending at the edge
candles = [{"ts": int(r["date"].timestamp()), "open": float(r["open"]),
            "high": float(r["high"]), "low": float(r["low"]),
            "close": float(r["close"]), "vol": float(r["vol"])}
           for _, r in window.iterrows()]
print(f"Feeding {len(candles)} candles, last bar close={candles[-1]['close']}")

# ── 3. monkeypatch the engine's candle fetch + verify signal fires on it ──
FE._fetch_futures_candles = lambda pair, tf, limit=200: list(candles)
# sanity: the patched window must itself produce a long edge at last_idx
_chk = evaluate_strategy(strat_code, FE._build_df(list(candles)), pair="BTC/USDT", execution_tf="5m")
_fn = make_signal_fn_from_df(_chk, 10, 8.0, 12.0)
_sig = _fn(_chk, len(_chk) - 1)
print(f"Signal on fed window at last bar: {_sig}")
assert _sig and _sig[-1] == "long", "fed window does not fire a long edge at last bar!"

# ── 4. start a real paper engine and let it tick ──
eng = FE.futures_engine_registry.for_bot("bot-fires-test", "bf-key")
eng._get_live_price = lambda pair, _p=entry_close: _p   # deterministic entry
eng.start_futures(
    strategy_name="Bollinger Bands Strategy", strategy_id=strat_id,
    pairs=["BTC/USDT"], mode="paper", timeframe="5m", leverage=10,
    wallet=1000.0, stoploss=-0.08, take_profit_pct=12.0, max_position_pct=5.0,
)
print("Engine started; waiting for tick loop to scan...")
for _ in range(20):
    time.sleep(0.5)
    if eng.positions or eng.closed_trades:
        break
opened = list(eng.positions.values())
ticks, last = eng.ticks, eng.last_action
try: eng.stop()
except Exception: pass

# ── 5. report ──
print("=" * 70)
print(f"ticks run         : {ticks}")
print(f"last_action       : {last}")
print(f"open positions    : {len(opened)}")
ok = False
if opened:
    p = opened[0]
    print(f"  -> {p.direction} {p.pair} entry={p.entry} sl={p.sl} tp={p.tp} size={p.size} lev={p.leverage}")
    ok = (p.direction == "long" and p.sl < p.entry < p.tp and p.size > 0)
print("=" * 70)
print("BOT FIRES SIGNAL & OPENS POSITION (paper) ✅" if ok
      else f"BOT DID NOT OPEN AS EXPECTED ❌ (ticks={ticks}, last={last})")
import sys; sys.exit(0 if ok else 1)
