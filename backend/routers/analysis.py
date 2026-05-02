"""
/api/analysis/* — opportunity scanning and per-pair-per-strategy deep dives.
"""
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func, desc
from sqlalchemy.orm import Session

from backend.models import get_db
from backend.models.trade import Trade
from backend.utils.clerk_auth import get_user_id
from backend.services.opportunity_scanner import (
    scan,
    DEFAULT_UNIVERSE,
    STRATEGY_PROFILES,
    _fetch_indicators,
    _score_pair_strategy,
)
from backend.services import kucoin_indicators as tv_fetcher
from backend.services.kucoin_volume import top_usdt_pairs, top_usdt_pairs_with_volume

router = APIRouter(prefix="/api/analysis", tags=["analysis"])


@router.get("/opportunities")
def opportunities(
    timeframe: str = Query("15m"),
    top_n: int = Query(15, ge=1, le=50),
    min_score: float = Query(0.0, ge=0.0, le=100.0),
    pairs: str | None = Query(None, description="Comma-separated pairs. Omit to use the default universe."),
    strategies: str | None = Query(None, description="Comma-separated strategy class names."),
    db: Session = Depends(get_db),
):
    pair_list = [p.strip() for p in pairs.split(",")] if pairs else None
    strat_list = [s.strip() for s in strategies.split(",")] if strategies else None
    return scan(
        db,
        pairs=pair_list,
        timeframe=timeframe,
        strategies=strat_list,
        top_n=top_n,
        min_score=min_score,
    )


@router.get("/universe")
def universe():
    """Return the default scanning universe + known strategy profiles so the
    UI can render filters without hard-coding anything."""
    return {
        "default_pairs": DEFAULT_UNIVERSE,
        "strategies": [
            {
                "name": name,
                "label": p["label"],
                "one_liner": p["one_liner"],
                "ideal_timeframes": p["ideal_timeframes"],
            }
            for name, p in STRATEGY_PROFILES.items()
        ],
    }


@router.post("/cache/clear")
def clear_cache():
    """Drop the indicator in-memory TTL cache (useful after adjusting
    strategy profiles or when you want a completely fresh scan)."""
    removed = tv_fetcher.clear_cache()
    return {"cleared": removed}


@router.get("/top-volume")
def top_volume(n: int = Query(50, ge=1, le=200)):
    """Top-N KuCoin USDT pairs by 24h quote volume. Used for dynamic
    universe selection in the scanner and the auto-trade engine."""
    return {"pairs": top_usdt_pairs_with_volume(n=n)}


@router.get("/portfolio")
def portfolio(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Aggregate analytics across every closed trade in the DB.

    Returns:
      * total realised P&L (paper + live separately and combined)
      * trade counts, win rate, avg win/loss
      * per-strategy breakdown (P&L, trades, win rate)
      * per-pair breakdown
      * cumulative equity curve over time
      * advanced ratios: Sharpe, Sortino, Calmar, Profit Factor
      * monthly P&L breakdown
      * max drawdown details
    """
    import math

    rows = db.execute(
        select(Trade)
        .where(Trade.status == "closed", Trade.user_id == user_id)
        .order_by(Trade.exit_time.asc())
    ).scalars().all()

    def _bucket(trades):
        wins = [t for t in trades if (t.profit_abs or 0) > 0]
        losses = [t for t in trades if (t.profit_abs or 0) < 0]
        total_pnl = sum((t.profit_abs or 0) for t in trades)
        return {
            "trades": len(trades),
            "total_pnl": round(total_pnl, 4),
            "win_rate": round(len(wins) / len(trades), 4) if trades else 0,
            "avg_win": round(sum(t.profit_abs for t in wins) / len(wins), 4) if wins else 0,
            "avg_loss": round(sum(t.profit_abs for t in losses) / len(losses), 4) if losses else 0,
            "best": round(max((t.profit_abs or 0) for t in trades), 4) if trades else 0,
            "worst": round(min((t.profit_abs or 0) for t in trades), 4) if trades else 0,
        }

    def _advanced_ratios(trades, starting_balance: float = 1000.0):
        """Compute Sharpe, Sortino, Calmar, Profit Factor, Max Drawdown."""
        if len(trades) < 2:
            return {
                "sharpe_ratio": 0.0,
                "sortino_ratio": 0.0,
                "calmar_ratio": 0.0,
                "profit_factor": 0.0,
                "max_drawdown_pct": 0.0,
                "max_drawdown_abs": 0.0,
                "avg_trade_duration_min": 0.0,
                "expectancy": 0.0,
            }

        returns = [float(t.profit_pct or 0) for t in trades]
        n = len(returns)
        mean_r = sum(returns) / n
        variance = sum((r - mean_r) ** 2 for r in returns) / (n - 1)
        std_r = math.sqrt(variance) if variance > 0 else 0.0001

        # Annualise (assume ~365 trades/year as proxy — adjust by actual duration)
        ann_factor = math.sqrt(365)
        sharpe = (mean_r / std_r) * ann_factor if std_r else 0.0

        # Sortino — only downside std
        downside = [r for r in returns if r < 0]
        if downside:
            down_var = sum(r ** 2 for r in downside) / len(downside)
            down_std = math.sqrt(down_var)
        else:
            down_std = 0.0001
        sortino = (mean_r / down_std) * ann_factor if down_std else 0.0

        # Max drawdown
        peak = starting_balance
        equity = starting_balance
        max_dd = 0.0
        for t in trades:
            equity += float(t.profit_abs or 0)
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak * 100 if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd
        max_dd_abs = peak - equity if peak > equity else 0

        # Calmar ratio = annualised return / max drawdown
        total_pnl_pct = sum(returns)
        calmar = (total_pnl_pct / max_dd) if max_dd > 0.01 else 0.0

        # Profit factor = gross profit / |gross loss|
        gross_profit = sum(float(t.profit_abs or 0) for t in trades if (t.profit_abs or 0) > 0)
        gross_loss = abs(sum(float(t.profit_abs or 0) for t in trades if (t.profit_abs or 0) < 0))
        profit_factor = round(gross_profit / gross_loss, 3) if gross_loss > 0 else 0.0

        # Average trade duration in minutes
        durations = []
        for t in trades:
            if t.entry_time and t.exit_time:
                diff = (t.exit_time - t.entry_time).total_seconds() / 60
                if diff > 0:
                    durations.append(diff)
        avg_dur = round(sum(durations) / len(durations), 1) if durations else 0.0

        # Expectancy = (win_rate * avg_win) + (loss_rate * avg_loss)
        wins = [t for t in trades if (t.profit_abs or 0) > 0]
        losses = [t for t in trades if (t.profit_abs or 0) < 0]
        win_rate = len(wins) / n
        avg_win_abs = sum(t.profit_abs for t in wins) / len(wins) if wins else 0
        avg_loss_abs = sum(t.profit_abs for t in losses) / len(losses) if losses else 0
        expectancy = round(win_rate * avg_win_abs + (1 - win_rate) * avg_loss_abs, 4)

        return {
            "sharpe_ratio": round(sharpe, 3),
            "sortino_ratio": round(sortino, 3),
            "calmar_ratio": round(calmar, 3),
            "profit_factor": profit_factor,
            "max_drawdown_pct": round(max_dd, 3),
            "max_drawdown_abs": round(max_dd_abs, 4),
            "avg_trade_duration_min": avg_dur,
            "expectancy": expectancy,
        }

    def _monthly_breakdown(trades):
        """Group P&L by YYYY-MM for a monthly calendar view."""
        monthly: dict[str, dict] = {}
        for t in trades:
            dt = t.exit_time or t.entry_time
            if not dt:
                continue
            key = dt.strftime("%Y-%m")
            if key not in monthly:
                monthly[key] = {"month": key, "pnl": 0.0, "trades": 0, "wins": 0}
            monthly[key]["pnl"] += float(t.profit_abs or 0)
            monthly[key]["trades"] += 1
            if (t.profit_abs or 0) > 0:
                monthly[key]["wins"] += 1
        for v in monthly.values():
            v["pnl"] = round(v["pnl"], 4)
            v["win_rate"] = round(v["wins"] / v["trades"], 3) if v["trades"] else 0
        return sorted(monthly.values(), key=lambda x: x["month"])

    paper = [t for t in rows if t.mode == "paper"]
    live  = [t for t in rows if t.mode == "live"]

    by_strategy: dict[int, list] = {}
    by_pair: dict[str, list] = {}
    for t in rows:
        by_strategy.setdefault(t.strategy_id or 0, []).append(t)
        by_pair.setdefault(t.pair or "?", []).append(t)

    # Cumulative equity curve
    cumulative = []
    running = 0.0
    for t in rows:
        running += float(t.profit_abs or 0)
        cumulative.append({
            "ts": t.exit_time.isoformat() if t.exit_time else None,
            "pair": t.pair,
            "pnl": round(running, 4),
        })

    # Daily P&L for risk monitoring
    daily: dict[str, float] = {}
    for t in rows:
        dt = t.exit_time or t.entry_time
        if dt:
            key = dt.strftime("%Y-%m-%d")
            daily[key] = round(daily.get(key, 0.0) + float(t.profit_abs or 0), 4)
    daily_pnl = [{"date": k, "pnl": v} for k, v in sorted(daily.items())]

    return {
        "all":   _bucket(rows),
        "paper": _bucket(paper),
        "live":  _bucket(live),
        "ratios": _advanced_ratios(rows),
        "paper_ratios": _advanced_ratios(paper),
        "live_ratios": _advanced_ratios(live),
        "monthly": _monthly_breakdown(rows),
        "daily_pnl": daily_pnl,
        "by_strategy": [
            {"strategy_id": sid, **_bucket(ts)} for sid, ts in by_strategy.items()
        ],
        "by_pair": [
            {"pair": p, **_bucket(ts)} for p, ts in sorted(by_pair.items(), key=lambda kv: -sum(t.profit_abs or 0 for t in kv[1]))
        ],
        "equity_curve": cumulative,
    }


@router.get("/risk-monitor")
def risk_monitor(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Real-time risk dashboard: daily drawdown usage, open trade exposure,
    circuit breaker status. Used by the dashboard Risk Monitor widget."""
    from backend.models.trade import Trade as TradeModel
    from backend.models.config import Config
    from datetime import datetime
    cfg = db.execute(select(Config).where(Config.user_id == user_id).limit(1)).scalar_one_or_none()
    if not cfg:
        return {"error": "Not configured"}

    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    # Today's closed P&L (both modes)
    from sqlalchemy import func as sqlfunc
    daily_pnl_result = db.execute(
        select(sqlfunc.sum(TradeModel.profit_abs)).where(
            TradeModel.user_id == user_id,
            TradeModel.status == "closed",
            TradeModel.exit_time >= today,
        )
    ).scalar() or 0.0

    # Open trade count
    open_count = db.execute(
        select(sqlfunc.count(TradeModel.id)).where(
            TradeModel.user_id == user_id,
            TradeModel.status == "open",
        )
    ).scalar() or 0

    # Paper vs live separate daily P&L
    paper_daily = db.execute(
        select(sqlfunc.sum(TradeModel.profit_abs)).where(
            TradeModel.user_id == user_id,
            TradeModel.mode == "paper",
            TradeModel.status == "closed",
            TradeModel.exit_time >= today,
        )
    ).scalar() or 0.0
    live_daily = db.execute(
        select(sqlfunc.sum(TradeModel.profit_abs)).where(
            TradeModel.user_id == user_id,
            TradeModel.mode == "live",
            TradeModel.status == "closed",
            TradeModel.exit_time >= today,
        )
    ).scalar() or 0.0

    wallet = 1000.0  # default; in production would fetch live balance
    max_dd = cfg.max_daily_drawdown_pct or 5.0
    daily_dd_pct = abs(float(daily_pnl_result) / wallet * 100) if daily_pnl_result < 0 else 0.0
    dd_used_pct = round(min(daily_dd_pct / max_dd * 100, 100), 1) if max_dd > 0 else 0.0

    circuit_breaker_triggered = daily_dd_pct >= max_dd

    return {
        "daily_pnl": round(float(daily_pnl_result), 4),
        "paper_daily_pnl": round(float(paper_daily), 4),
        "live_daily_pnl": round(float(live_daily), 4),
        "daily_drawdown_pct": round(daily_dd_pct, 3),
        "max_daily_drawdown_pct": max_dd,
        "drawdown_used_pct": dd_used_pct,     # 0-100 gauge fill
        "circuit_breaker_triggered": circuit_breaker_triggered,
        "open_trades": open_count,
        "max_open_trades": cfg.max_open_trades or 3,
        "open_trades_used_pct": round(open_count / (cfg.max_open_trades or 3) * 100, 1),
        "max_position_pct": cfg.max_position_pct or 5.0,
        "auto_trade_running": bool(cfg.auto_trade_enabled),
    }


@router.get("/analyze/{pair:path}")
def analyze_single(
    pair: str,
    timeframe: str = Query("15m"),
    db: Session = Depends(get_db),
):
    """Score every strategy for a single pair — useful for 'I already picked
    this coin, which strategy should I run on it?'"""
    ind = _fetch_indicators(pair, timeframe)
    if ind is None:
        return {"pair": pair, "error": "Could not fetch indicators from TradingView"}

    results = []
    for strat in STRATEGY_PROFILES.keys():
        opp = _score_pair_strategy(pair, strat, timeframe, ind, db)
        if opp is not None:
            from dataclasses import asdict
            results.append(asdict(opp))
    results.sort(key=lambda r: r["overall_score"], reverse=True)
    return {
        "pair": pair,
        "timeframe": timeframe,
        "best": results[0] if results else None,
        "all": results,
    }
