"""Functional test of the Futures Terminal HTTP endpoints (PAPER flow).

Spins up the FastAPI app against a TEMP sqlite DB (never touches real data),
Clerk disabled (anonymous user), and drives the real paper manual + bot flow
the frontend uses, reporting exactly which endpoint works vs errors.

Live order placement is NOT exercised (needs a funded KuCoin account); this
covers everything else end-to-end through the HTTP layer.

Run: PYTHONPATH=. python scripts/verify_terminal_endpoints.py
"""
import os, tempfile, json, warnings
warnings.filterwarnings("ignore")

# temp DB + Clerk disabled BEFORE importing the app
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/t.db"
os.environ.pop("CLERK_SECRET_KEY", None)
os.environ.pop("CLERK_PUBLISHABLE_KEY", None)
os.environ.pop("NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY", None)

from fastapi.testclient import TestClient
import backend.main as M
from backend.models.database import init_db
init_db()
try:
    with __import__("backend.models", fromlist=["SessionLocal"]).SessionLocal() as _db:
        M._seed_builtin_strategies(_db)
except Exception as _e:
    print("seed warn:", _e)

# Bypass Clerk auth for the test by overriding the dependency with a fixed user.
# The futures router is now gated by an access code, so authenticate the test
# user as an ADMIN (admin emails bypass the gate) — set ADMIN_EMAILS to this
# user's email and stash it on request.state the way the real dependency does.
os.environ["ADMIN_EMAILS"] = "terminal-test-admin@gmail.com"
from fastapi import Request
from backend.utils.clerk_auth import get_user_id as _get_user_id
def _fake_uid(request: Request):
    request.state.user_id = "terminal-test-user"
    request.state.user_email = "terminal-test-admin@gmail.com"
    return "terminal-test-user"
M.app.dependency_overrides[_get_user_id] = _fake_uid

c = TestClient(M.app)
results = []
def step(label, method, path, **kw):
    try:
        r = getattr(c, method)(path, **kw)
        ok = r.status_code < 400
        try: body = r.json()
        except Exception: body = r.text[:200]
        # treat an {"error": ...} 200 as a soft-fail to surface
        soft = isinstance(body, dict) and body.get("error")
        tag = "OK " if (ok and not soft) else ("ERR" if not ok else "ERR*")
        results.append((tag, label, r.status_code, body))
        short = body if not isinstance(body, (dict, list)) else json.dumps(body)[:170]
        print(f"  {tag} {label:34s} [{r.status_code}] {short}")
        return body if isinstance(body, dict) else {}
    except Exception as e:
        results.append(("CRASH", label, 0, str(e)))
        print(f"  CRASH {label:34s} {str(e)[:170]}")
        return {}

print("=" * 80)
print("PAPER — read endpoints")
print("=" * 80)
step("GET /balance",  "get", "/api/futures/balance")
step("GET /status",   "get", "/api/futures/status")
step("GET /contracts","get", "/api/futures/contracts")
step("GET /open",     "get", "/api/futures/open?mode=paper")
step("GET /bots",     "get", "/api/futures/bots?mode=paper")
step("GET /history",  "get", "/api/futures/history")
step("GET /lead-trading-status", "get", "/api/futures/lead-trading-status")
step("GET /risk-config", "get", "/api/futures/risk-config")

print("=" * 80)
print("PAPER — MANUAL trade lifecycle")
print("=" * 80)
me = step("POST /manual-entry (paper long)", "post", "/api/futures/manual-entry",
          json={"pair": "BTC/USDT", "direction": "long", "cost_usdt": 50,
                "leverage": 10, "mode": "paper", "sl_pct": 2.0, "tp_pct": 4.0})
opn = step("GET /open after entry", "get", "/api/futures/open?mode=paper")
# find the opened position's identifier
pos_list = opn.get("positions") or opn.get("open_positions") or (opn if isinstance(opn, list) else [])
pid = None; ppair = "BTC/USDT"
if isinstance(pos_list, list) and pos_list:
    p0 = pos_list[0]
    pid = p0.get("position_id") or p0.get("id") or p0.get("db_id")
    ppair = p0.get("pair") or p0.get("symbol") or "BTC/USDT"
print(f"   -> opened position id={pid} pair={ppair}")

step("POST /position/tp-sl",        "post", "/api/futures/position/tp-sl",
     json={"pair": ppair, "position_id": pid, "sl_price": 0, "tp_price": 0, "mode": "paper"})
step("POST /position/add-margin",   "post", "/api/futures/position/add-margin",
     json={"pair": ppair, "position_id": pid, "amount": 5, "mode": "paper"})
step("POST /position/partial-close","post", "/api/futures/position/partial-close",
     json={"pair": ppair, "position_id": pid, "close_pct": 50, "mode": "paper"})
step("POST /force-close/{pair}",    "post", f"/api/futures/force-close/{ppair}",
     json={"mode": "paper"})
step("GET /history after close",    "get", "/api/futures/history")

print("=" * 80)
print("PAPER — BOT lifecycle")
print("=" * 80)
# find a strategy id
strat_id = None
try:
    from backend.models import SessionLocal
    from backend.models.strategy import Strategy
    from sqlalchemy import select
    with SessionLocal() as db:
        row = db.execute(select(Strategy).where(Strategy.name == "Bollinger Bands Strategy")).scalar_one_or_none()
        if row: strat_id = row.id
except Exception as e:
    print("strategy lookup warn:", e)
print(f"   -> Bollinger strategy_id={strat_id}")
bot = step("POST /bots (create paper Bollinger)", "post", "/api/futures/bots",
           json={"strategy_id": strat_id, "strategy_name": "Bollinger Bands Strategy",
                 "mode": "paper", "pairs": ["BTC/USDT"], "timeframe": "5m",
                 "leverage": 10, "wallet": 1000, "stoploss": -0.08, "takeprofit": 0.12})
bot_id = bot.get("bot_id") or bot.get("id") or (bot.get("bot") or {}).get("id")
print(f"   -> created bot_id={bot_id}")
step("GET /bots after create", "get", "/api/futures/bots?mode=paper")
if bot_id:
    step("POST /bots/{id}/pause",  "post", f"/api/futures/bots/{bot_id}/pause")
    step("POST /bots/{id}/resume", "post", f"/api/futures/bots/{bot_id}/resume")
    step("GET /bots/{id}/performance", "get", f"/api/futures/bots/{bot_id}/performance")
    step("DELETE /bots/{id}", "delete", f"/api/futures/bots/{bot_id}")

print("=" * 80)
bad = [r for r in results if r[0] in ("ERR", "ERR*", "CRASH")]
print(f"TOTAL {len(results)} calls — {len(bad)} not-working:")
for tag, label, code, body in bad:
    b = body if not isinstance(body, (dict, list)) else json.dumps(body)[:200]
    print(f"  {tag} {label} [{code}] {b}")
if not bad:
    print("ALL TERMINAL ENDPOINTS WORKING ✅")
