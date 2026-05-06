import json
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import get_db, Strategy
from backend.models.trade import Backtest
from backend.services.freqtrade_manager import freqtrade_mgr
from backend.utils.clerk_auth import get_user_id

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


class BacktestRequest(BaseModel):
    strategy_id: int
    timerange: str = "20240101-20240401"
    pairs: list[str] = ["BTC/USDT"]
    timeframe: str = "15m"
    starting_balance: float = 1000
    stoploss: float = -0.03


@router.post("/run")
def run_backtest(
    req: BacktestRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    result = db.execute(
        select(Strategy).where(Strategy.id == req.strategy_id, Strategy.user_id == user_id)
    )
    strategy = result.scalar_one_or_none()
    if not strategy:
        return {"error": "Strategy not found"}

    strategy_name = f"strategy_{strategy.id}"
    for line in (strategy.generated_code or "").split("\n"):
        if line.startswith("class ") and "IStrategy" in line:
            strategy_name = line.split("(")[0].replace("class ", "").strip()
            break

    bt_result = freqtrade_mgr.for_user(user_id).run_backtest(
        strategy_name=strategy_name,
        pairs=req.pairs,
        timeframe=req.timeframe,
        timerange=req.timerange,
        stoploss=req.stoploss,
        starting_balance=req.starting_balance,
    )

    # If Freqtrade is not available or fails (common on Railway where the
    # freqtrade subprocess is not installed), fall back to the native
    # Python backtester which uses only stdlib + pandas.
    if "error" in bt_result:
        ft_err = bt_result["error"]
        use_native = any(k in ft_err.lower() for k in [
            "freqtrade not found", "no such file", "filenotfound",
            "could not load markets", "exchangenotavailable",
            "temporaryerror", "missing dependency",
        ])
        if use_native:
            from backend.services import native_backtester
            return native_backtester.run(
                strategy_name=strategy_name,
                pairs=req.pairs,
                timeframe=req.timeframe,
                timerange=req.timerange,
                stoploss=req.stoploss,
                starting_balance=req.starting_balance,
            )
        return bt_result

    # Parse results
    results_data = bt_result.get("results", {})

    # Freqtrade nests per-strategy results under "strategy": {name: {...}}.
    # We take the first (there is only one for us).
    strategy_key = next(iter(results_data.get("strategy", {})), None)
    strategy_results = results_data.get("strategy", {}).get(strategy_key, {}) if strategy_key else {}

    # Freqtrade has renamed win_rate across versions. Try every known key, then
    # fall back to computing it from wins/losses or from the trades array.
    win_rate = (
        strategy_results.get("win_rate")
        or strategy_results.get("winrate")
        or strategy_results.get("wins_ratio")
    )
    if not win_rate:
        wins = strategy_results.get("wins") or 0
        losses = strategy_results.get("losses") or 0
        draws = strategy_results.get("draws") or 0
        total = wins + losses + draws
        if total > 0:
            win_rate = wins / total
        else:
            # Last resort: compute from trades[]
            rtrades = strategy_results.get("trades", []) or []
            wins_c = sum(1 for t in rtrades if (t.get("profit_ratio") or t.get("profit_pct") or 0) > 0)
            if rtrades:
                win_rate = wins_c / len(rtrades)
            else:
                win_rate = 0

    metrics = {
        "total_profit": (strategy_results.get("profit_total") or 0) * 100,
        # Keep win_rate as a fraction (0..1) so frontend can format consistently.
        "win_rate": float(win_rate or 0),
        "max_drawdown": (strategy_results.get("max_drawdown_account") or strategy_results.get("max_drawdown") or 0) * 100,
        "sharpe_ratio": strategy_results.get("sharpe") or 0,
        "total_trades": strategy_results.get("total_trades") or len(strategy_results.get("trades", []) or []),
        "avg_duration": strategy_results.get("holding_avg", "N/A"),
    }

    # Normalize trade rows so the frontend can render them without guessing
    # between freqtrade vs app field names.
    raw_trades = strategy_results.get("trades", []) or []
    trades_out = []
    for t in raw_trades:
        profit_ratio = t.get("profit_ratio", t.get("profit_pct", 0)) or 0
        trades_out.append(
            {
                "pair": t.get("pair"),
                "open_rate": t.get("open_rate"),
                "close_rate": t.get("close_rate"),
                "profit_pct": profit_ratio * 100 if abs(profit_ratio) < 1 else profit_ratio,
                "profit_abs": t.get("profit_abs"),
                "open_date": t.get("open_date"),
                "close_date": t.get("close_date"),
                "trade_duration": t.get("trade_duration") or t.get("trade_duration_s"),
                "exit_reason": t.get("exit_reason"),
            }
        )

    backtest = Backtest(
        user_id=user_id,
        strategy_id=strategy.id,
        timerange=req.timerange,
        starting_balance=req.starting_balance,
        total_profit=metrics["total_profit"],
        win_rate=metrics["win_rate"],
        max_drawdown=metrics["max_drawdown"],
        sharpe_ratio=metrics["sharpe_ratio"],
        total_trades=metrics["total_trades"],
        avg_duration=str(metrics["avg_duration"]),
        results_json=json.dumps(results_data),
    )
    db.add(backtest)
    db.commit()
    db.refresh(backtest)

    return {
        "id": backtest.id,
        "metrics": metrics,
        "trades": trades_out,
        "results": results_data,
    }


class BulkBacktestRequest(BaseModel):
    strategy_id: int
    timerange: str = "20240101-20240401"
    pairs: list[str]                     # explicit list; caller can use top scanner picks
    timeframe: str = "15m"
    starting_balance: float = 1000
    stoploss: float = -0.03


@router.post("/bulk")
def run_bulk_backtest(
    req: BulkBacktestRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Run one strategy across many pairs in a single Freqtrade invocation.

    Faster than looping per-pair from the frontend (one candle download, one
    Freqtrade launch). Returns per-pair metrics alongside aggregate totals."""
    result = db.execute(
        select(Strategy).where(Strategy.id == req.strategy_id, Strategy.user_id == user_id)
    )
    strategy = result.scalar_one_or_none()
    if not strategy:
        return {"error": "Strategy not found"}

    strategy_name = f"strategy_{strategy.id}"
    for line in (strategy.generated_code or "").split("\n"):
        if line.startswith("class ") and "IStrategy" in line:
            strategy_name = line.split("(")[0].replace("class ", "").strip()
            break

    bt = freqtrade_mgr.for_user(user_id).run_backtest(
        strategy_name=strategy_name,
        pairs=req.pairs,
        timeframe=req.timeframe,
        timerange=req.timerange,
        stoploss=req.stoploss,
        starting_balance=req.starting_balance,
    )
    if "error" in bt:
        return bt

    data = bt.get("results", {})
    strategy_key = next(iter(data.get("strategy", {})), None)
    sres = data.get("strategy", {}).get(strategy_key, {}) if strategy_key else {}

    # Per-pair summary
    per_pair_raw = sres.get("results_per_pair", []) or sres.get("pair_summary", []) or []
    per_pair = []
    for row in per_pair_raw:
        per_pair.append({
            "pair": row.get("key") or row.get("pair"),
            "trades": row.get("trades") or row.get("total_trades") or 0,
            "profit_pct": round(((row.get("profit_total") or row.get("profit_mean") or 0) * 100), 2),
            "profit_abs": row.get("profit_abs_sum") or row.get("profit_sum") or 0,
            "wins": row.get("wins") or 0,
            "losses": row.get("losses") or 0,
        })

    # Aggregate
    wins = sres.get("wins") or 0
    losses = sres.get("losses") or 0
    draws = sres.get("draws") or 0
    total_wlk = wins + losses + draws
    win_rate = (wins / total_wlk) if total_wlk > 0 else 0
    aggregate = {
        "total_profit_pct": round((sres.get("profit_total") or 0) * 100, 2),
        "total_trades": sres.get("total_trades") or 0,
        "win_rate": win_rate,
        "max_drawdown_pct": round((sres.get("max_drawdown_account") or sres.get("max_drawdown") or 0) * 100, 2),
        "sharpe": sres.get("sharpe") or 0,
    }
    return {
        "strategy": strategy.name,
        "timerange": req.timerange,
        "timeframe": req.timeframe,
        "pairs_scanned": req.pairs,
        "per_pair": per_pair,
        "aggregate": aggregate,
    }


@router.get("/results/{backtest_id}")
def get_results(
    backtest_id: int,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    result = db.execute(
        select(Backtest).where(Backtest.id == backtest_id, Backtest.user_id == user_id)
    )
    bt = result.scalar_one_or_none()
    if not bt:
        return {"error": "Backtest not found"}

    return {
        "id": bt.id,
        "strategy_id": bt.strategy_id,
        "timerange": bt.timerange,
        "starting_balance": bt.starting_balance,
        "total_profit": bt.total_profit,
        "win_rate": bt.win_rate,
        "max_drawdown": bt.max_drawdown,
        "sharpe_ratio": bt.sharpe_ratio,
        "total_trades": bt.total_trades,
        "avg_duration": bt.avg_duration,
        "results": json.loads(bt.results_json) if bt.results_json else {},
        "created_at": str(bt.created_at),
    }
