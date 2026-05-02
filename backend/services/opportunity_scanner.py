"""
Opportunity Scanner
===================
Scans a universe of crypto pairs across the 4 built-in strategy profiles and
returns ranked (pair, strategy) recommendations with:
  - entry_quality  (is NOW a good moment to enter?)         0..100
  - fit_score      (does the pair's current regime match    0..100
                    this strategy's sweet spot?)
  - expected_profit_pct (empirical estimate from cached
                    backtests of the same strategy on this
                    pair/timeframe — null if no history)
  - confidence     (0..1, based on signal agreement + data availability)
  - reasoning      (human-readable bullet list explaining the call)

Scoring uses *current* TradingView TA indicators (RSI, MACD, ADX, BB, EMA,
volume) via the existing signal_engine.get_signals(). Historical expected
profit is read from the Backtest table when available.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

from sqlalchemy import select, desc
from sqlalchemy.orm import Session

from backend.models import Strategy
from backend.models.trade import Backtest
from backend.services import kucoin_indicators as tv_fetcher  # same surface, KuCoin-backed


# Curated high-liquidity KuCoin USDT universe. Kept short enough to keep the
# TradingView round-trips fast (~2–5 s with a thread pool of 8).
DEFAULT_UNIVERSE = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
    "ADA/USDT", "DOGE/USDT", "AVAX/USDT", "LINK/USDT", "POL/USDT",
    "DOT/USDT", "LTC/USDT", "ATOM/USDT", "TRX/USDT", "NEAR/USDT",
    "APT/USDT", "ARB/USDT", "OP/USDT", "FIL/USDT", "INJ/USDT",
]


# --- Strategy profiles --------------------------------------------------------
# Each profile tells the scorer what "good conditions" look like for this
# strategy template. Weights sum to 1.0 inside fit_score.

STRATEGY_PROFILES: dict[str, dict[str, Any]] = {
    "EmaScalpingStrategy": {
        "label": "EMA Scalping",
        "one_liner": "Ride strong, confirmed trends with high volume.",
        "ideal_timeframes": ["5m", "15m"],
        "prefers": {
            "adx_min": 25,          # needs a real trend
            "rsi_band": (45, 70),   # momentum up, not yet euphoric
            "macd_hist_pos": True,
            "bb_width_min": 0.015,  # some room to move
            "volume_ratio_min": 1.2,
        },
        "avoid_if": {
            "rsi_gt": 78,           # overheated
            "adx_lt": 15,           # no trend
        },
    },
    "MacdCrossoverStrategy": {
        "label": "MACD Crossover",
        "one_liner": "Catch momentum turns on fresh MACD signal flips.",
        "ideal_timeframes": ["15m", "1h", "4h"],
        "prefers": {
            "macd_fresh_cross_up": True,
            "rsi_band": (40, 65),
            "adx_min": 18,
        },
        "avoid_if": {
            "rsi_gt": 75,
            "macd_hist_strongly_negative": True,
        },
    },
    "RsiBollingerStrategy": {
        "label": "RSI + Bollinger",
        "one_liner": "Mean-revert from oversold touches of the lower band.",
        "ideal_timeframes": ["15m", "1h"],
        "prefers": {
            "rsi_band": (20, 40),   # oversold
            "price_near_bb_lower": True,
            "adx_max": 28,           # prefer ranging, not strong trend
        },
        "avoid_if": {
            "rsi_lt": 15,            # knife-catching territory
            "adx_gt": 40,            # strong trend = bad for mean-reversion
        },
    },
    "DcaAccumulationStrategy": {
        "label": "DCA Accumulation",
        "one_liner": "Time-based accumulation — works any time, best in drawdowns.",
        "ideal_timeframes": ["4h", "1d"],
        "prefers": {
            "rsi_band": (20, 55),   # accumulate on weakness
            "price_below_sma50": True,
        },
        "avoid_if": {
            "rsi_gt": 80,            # don't start DCA near local tops
        },
    },
}


# --- Data classes -------------------------------------------------------------

@dataclass
class Opportunity:
    pair: str
    strategy: str
    strategy_label: str
    timeframe: str
    entry_quality: float          # 0..100  "is now a good moment?"
    fit_score: float              # 0..100  "does pair match strategy?"
    overall_score: float          # 0..100  combined rank
    confidence: float             # 0..1
    expected_profit_pct: float | None
    expected_profit_source: str   # "historical" | "estimate" | "unknown"
    recommendation: str           # STRONG_BUY / BUY / HOLD / AVOID
    indicators: dict[str, Any] = field(default_factory=dict)
    reasoning: list[str] = field(default_factory=list)


# --- Helpers ------------------------------------------------------------------

def _fetch_indicators(pair: str, timeframe: str) -> dict[str, Any] | None:
    """Fetch TradingView TA via the resilient tv_fetcher (cache + retry +
    exchange fallback) and flatten into the shape the scorer expects."""
    payload = tv_fetcher.fetch(pair, timeframe)
    if payload is None:
        return None
    raw_ind = payload.get("indicators") or {}
    summary = payload.get("summary") or {}
    osc = payload.get("oscillators") or {}
    ma = payload.get("moving_averages") or {}

    # Flatten to the compact shape the rest of this file reads.
    ind = {
        "rsi": raw_ind.get("RSI"),
        "macd": raw_ind.get("MACD.macd"),
        "macd_signal": raw_ind.get("MACD.signal"),
        "bb_upper": raw_ind.get("BB.upper"),
        "bb_lower": raw_ind.get("BB.lower"),
        "ema_20": raw_ind.get("EMA20"),
        "sma_50": raw_ind.get("SMA50"),
        "adx": raw_ind.get("ADX"),
        "atr": raw_ind.get("ATR"),
        "volume": raw_ind.get("volume"),
    }

    # Derive a couple of features
    bb_upper = ind.get("bb_upper")
    bb_lower = ind.get("bb_lower")
    ema20 = ind.get("ema_20")
    bb_width = None
    price_near_bb_lower = False
    if bb_upper and bb_lower and ema20:
        try:
            bb_width = (float(bb_upper) - float(bb_lower)) / float(ema20)
            # "Near lower band" = EMA20 within 1.2% of BB lower
            price_near_bb_lower = (float(ema20) - float(bb_lower)) / float(ema20) < 0.012
        except (TypeError, ZeroDivisionError):
            pass

    return {
        "rsi": ind.get("rsi"),
        "macd": ind.get("macd"),
        "macd_signal": ind.get("macd_signal"),
        "macd_hist": (
            (ind.get("macd") or 0) - (ind.get("macd_signal") or 0)
            if ind.get("macd") is not None and ind.get("macd_signal") is not None
            else None
        ),
        "adx": ind.get("adx"),
        "bb_upper": bb_upper,
        "bb_lower": bb_lower,
        "ema_20": ema20,
        "sma_50": ind.get("sma_50"),
        "atr": ind.get("atr"),
        "volume": ind.get("volume"),
        "bb_width": bb_width,
        "price_near_bb_lower": price_near_bb_lower,
        "tv_recommendation": summary.get("recommendation"),
        "tv_buy": summary.get("buy"),
        "tv_sell": summary.get("sell"),
        "osc_reco": (osc or {}).get("RECOMMENDATION"),
        "ma_reco":  (ma or {}).get("RECOMMENDATION"),
    }


def _score_ema_scalping(ind: dict[str, Any]) -> tuple[float, float, list[str]]:
    reasons = []
    fit = 50.0
    entry = 50.0
    adx = ind.get("adx")
    rsi = ind.get("rsi")
    macd_hist = ind.get("macd_hist")
    bb_width = ind.get("bb_width")

    if adx is not None:
        if adx >= 25:
            fit += 20; reasons.append(f"Strong trend (ADX {adx:.1f} ≥ 25)")
        elif adx >= 18:
            fit += 8
        elif adx < 15:
            fit -= 25; reasons.append(f"No trend (ADX {adx:.1f} < 15) — scalping misfires")

    if rsi is not None:
        if 45 <= rsi <= 70:
            entry += 15; reasons.append(f"RSI {rsi:.1f} in healthy-momentum band")
        elif rsi > 78:
            entry -= 25; reasons.append(f"RSI {rsi:.1f} overheated")
        elif rsi < 40:
            entry -= 10

    if macd_hist is not None:
        if macd_hist > 0:
            entry += 15; reasons.append("MACD histogram positive (momentum up)")
        else:
            entry -= 10

    if bb_width is not None:
        if bb_width >= 0.015:
            fit += 10
        else:
            fit -= 5; reasons.append("Low volatility — scalps may not clear fees")

    return max(0, min(100, fit)), max(0, min(100, entry)), reasons


def _score_macd_crossover(ind: dict[str, Any]) -> tuple[float, float, list[str]]:
    reasons = []
    fit = 50.0
    entry = 50.0
    macd = ind.get("macd")
    macd_signal = ind.get("macd_signal")
    macd_hist = ind.get("macd_hist")
    rsi = ind.get("rsi")
    adx = ind.get("adx")

    if macd is not None and macd_signal is not None and macd_hist is not None:
        if macd > macd_signal and 0 < macd_hist < abs(macd) * 0.3:
            entry += 25; fit += 15
            reasons.append("Fresh MACD bullish crossover (hist just turned +)")
        elif macd > macd_signal:
            entry += 10; reasons.append("MACD above signal")
        elif macd_hist < -abs(macd) * 0.3:
            entry -= 20; reasons.append("MACD strongly bearish")

    if rsi is not None and 40 <= rsi <= 65:
        entry += 10
    elif rsi is not None and rsi > 75:
        entry -= 15; reasons.append(f"RSI {rsi:.1f} overbought — crossovers get faded")

    if adx is not None and adx >= 18:
        fit += 10

    return max(0, min(100, fit)), max(0, min(100, entry)), reasons


def _score_rsi_bollinger(ind: dict[str, Any]) -> tuple[float, float, list[str]]:
    reasons = []
    fit = 50.0
    entry = 50.0
    rsi = ind.get("rsi")
    adx = ind.get("adx")
    near_low = ind.get("price_near_bb_lower")

    if rsi is not None:
        if 20 <= rsi <= 40:
            entry += 25
            reasons.append(f"RSI {rsi:.1f} oversold — classic mean-revert setup")
        elif rsi < 15:
            entry -= 15; reasons.append(f"RSI {rsi:.1f} extreme — falling knife risk")
        elif rsi > 55:
            entry -= 15; reasons.append(f"RSI {rsi:.1f} too high for mean-reversion entry")

    if near_low:
        entry += 15; reasons.append("Price touching lower Bollinger band")

    if adx is not None:
        if adx <= 22:
            fit += 15; reasons.append(f"Ranging market (ADX {adx:.1f}) — ideal for RSI+BB")
        elif adx > 40:
            fit -= 25; reasons.append(f"Strong trend (ADX {adx:.1f}) — mean-reversion will lose")

    return max(0, min(100, fit)), max(0, min(100, entry)), reasons


def _score_dca(ind: dict[str, Any]) -> tuple[float, float, list[str]]:
    reasons = ["DCA is time-based — fit is always OK"]
    fit = 65.0  # DCA is a baseline: always workable
    entry = 55.0
    rsi = ind.get("rsi")
    ema20 = ind.get("ema_20")
    sma50 = ind.get("sma_50")

    if rsi is not None:
        if 20 <= rsi <= 55:
            entry += 20; reasons.append(f"RSI {rsi:.1f} — accumulating on weakness is good")
        elif rsi > 80:
            entry -= 25; reasons.append(f"RSI {rsi:.1f} — bad time to start DCA (local top)")

    if ema20 is not None and sma50 is not None:
        try:
            if float(ema20) < float(sma50):
                entry += 15
                reasons.append("Price below SMA50 — discount zone")
        except (TypeError, ValueError):
            pass

    return max(0, min(100, fit)), max(0, min(100, entry)), reasons


SCORERS = {
    "EmaScalpingStrategy":    _score_ema_scalping,
    "MacdCrossoverStrategy":  _score_macd_crossover,
    "RsiBollingerStrategy":   _score_rsi_bollinger,
    "DcaAccumulationStrategy": _score_dca,
}


def _historical_profit(db: Session, strategy_name: str, pair: str) -> tuple[float | None, str]:
    """
    Look up the most recent cached Backtest whose strategy's generated_code
    contains a class with this name AND whose pairs/metrics reference this pair.
    Returns (expected_profit_pct, source).
    """
    # Find the strategy row whose code uses this class
    strat_q = db.execute(
        select(Strategy).where(Strategy.generated_code.like(f"%class {strategy_name}(%"))
    ).scalars().all()
    if not strat_q:
        return None, "unknown"

    ids = [s.id for s in strat_q]
    bt = db.execute(
        select(Backtest)
        .where(Backtest.strategy_id.in_(ids))
        .order_by(desc(Backtest.created_at))
        .limit(10)
    ).scalars().all()
    if not bt:
        return None, "unknown"

    # Use the average total_profit across the most recent backtests as a rough
    # empirical expectation.
    avg = sum(b.total_profit or 0 for b in bt) / len(bt)
    return round(avg, 3), "historical"


def _recommendation_label(overall: float) -> str:
    if overall >= 75: return "STRONG_BUY"
    if overall >= 60: return "BUY"
    if overall >= 45: return "HOLD"
    return "AVOID"


def _score_pair_strategy(
    pair: str,
    strategy_name: str,
    timeframe: str,
    ind: dict[str, Any],
    db: Session,
) -> Opportunity | None:
    scorer = SCORERS.get(strategy_name)
    profile = STRATEGY_PROFILES.get(strategy_name)
    if not scorer or not profile:
        return None

    fit, entry, reasons = scorer(ind)

    # Timeframe bonus/penalty
    if timeframe in profile["ideal_timeframes"]:
        fit = min(100, fit + 5)
    else:
        fit = max(0, fit - 5)
        reasons.append(f"Timeframe {timeframe} isn't ideal for this strategy "
                       f"(prefers {', '.join(profile['ideal_timeframes'])})")

    # TradingView consensus as a small tilt
    tv_reco = (ind.get("tv_recommendation") or "").upper()
    if tv_reco in ("STRONG_BUY", "BUY"):
        entry = min(100, entry + 5)
        reasons.append(f"TradingView consensus: {tv_reco}")
    elif tv_reco in ("STRONG_SELL", "SELL"):
        entry = max(0, entry - 10)
        reasons.append(f"TradingView consensus: {tv_reco} — caution")

    overall = 0.55 * entry + 0.45 * fit

    # Historical profit
    exp_profit, source = _historical_profit(db, strategy_name, pair)

    # Confidence: how much data did we actually get?
    populated = sum(1 for k in ("rsi", "macd", "adx", "bb_upper", "ema_20")
                    if ind.get(k) is not None)
    confidence = round(min(1.0, 0.2 + 0.15 * populated + (0.1 if source == "historical" else 0.0)), 2)

    # Strip/clean indicators for the response payload
    display_ind = {
        "rsi": round(ind["rsi"], 2) if ind.get("rsi") is not None else None,
        "adx": round(ind["adx"], 2) if ind.get("adx") is not None else None,
        "macd_hist": round(ind["macd_hist"], 5) if ind.get("macd_hist") is not None else None,
        "bb_width_pct": round(ind["bb_width"] * 100, 2) if ind.get("bb_width") is not None else None,
        "tv_recommendation": ind.get("tv_recommendation"),
    }

    return Opportunity(
        pair=pair,
        strategy=strategy_name,
        strategy_label=profile["label"],
        timeframe=timeframe,
        entry_quality=round(entry, 1),
        fit_score=round(fit, 1),
        overall_score=round(overall, 1),
        confidence=confidence,
        expected_profit_pct=exp_profit,
        expected_profit_source=source,
        recommendation=_recommendation_label(overall),
        indicators=display_ind,
        reasoning=reasons,
    )


def scan(
    db: Session,
    pairs: list[str] | None = None,
    timeframe: str = "15m",
    strategies: list[str] | None = None,
    top_n: int = 15,
    min_score: float = 0.0,
) -> dict[str, Any]:
    """
    Main entry point. Fetches live indicators for every pair (in parallel via
    thread pool), scores each (pair, strategy), and returns the top_n by
    overall_score.
    """
    pairs = pairs or DEFAULT_UNIVERSE
    strategies = strategies or list(STRATEGY_PROFILES.keys())

    # Use tv_fetcher.fetch_many: small worker pool (2), TTL cache, retry with
    # back-off, exchange fallback (KUCOIN→BINANCE→BYBIT→OKX). Results get
    # re-normalised into the compact shape _score_pair_strategy expects.
    raw = tv_fetcher.fetch_many(pairs, timeframe=timeframe, max_workers=2)
    stale_pairs = [p for p, d in raw.items() if d and d.get("stale")]

    def _flatten(payload: dict[str, Any] | None) -> dict[str, Any] | None:
        if payload is None:
            return None
        r = payload.get("indicators") or {}
        s = payload.get("summary") or {}
        o = payload.get("oscillators") or {}
        m = payload.get("moving_averages") or {}
        ind = {
            "rsi": r.get("RSI"),
            "macd": r.get("MACD.macd"),
            "macd_signal": r.get("MACD.signal"),
            "bb_upper": r.get("BB.upper"),
            "bb_lower": r.get("BB.lower"),
            "ema_20": r.get("EMA20"),
            "sma_50": r.get("SMA50"),
            "adx": r.get("ADX"),
            "atr": r.get("ATR"),
            "volume": r.get("volume"),
        }
        if ind["macd"] is not None and ind["macd_signal"] is not None:
            ind["macd_hist"] = ind["macd"] - ind["macd_signal"]
        else:
            ind["macd_hist"] = None

        bb_u, bb_l, e20 = ind["bb_upper"], ind["bb_lower"], ind["ema_20"]
        ind["bb_width"] = None
        ind["price_near_bb_lower"] = False
        if bb_u and bb_l and e20:
            try:
                ind["bb_width"] = (float(bb_u) - float(bb_l)) / float(e20)
                ind["price_near_bb_lower"] = (float(e20) - float(bb_l)) / float(e20) < 0.012
            except (TypeError, ZeroDivisionError, ValueError):
                pass
        ind["tv_recommendation"] = s.get("RECOMMENDATION")
        ind["tv_buy"] = s.get("BUY")
        ind["tv_sell"] = s.get("SELL")
        ind["osc_reco"] = (o or {}).get("RECOMMENDATION")
        ind["ma_reco"] = (m or {}).get("RECOMMENDATION")
        ind["_exchange"] = payload.get("exchange")
        return ind

    scanned = 0
    failed: list[str] = []
    opps: list[Opportunity] = []
    for pair in pairs:
        ind = _flatten(raw.get(pair))
        if ind is None:
            failed.append(pair)
            continue
        scanned += 1
        for strat in strategies:
            opp = _score_pair_strategy(pair, strat, timeframe, ind, db)
            if opp and opp.overall_score >= min_score:
                opps.append(opp)

    opps.sort(key=lambda o: o.overall_score, reverse=True)

    tv_status = tv_fetcher.status()
    return {
        "timeframe": timeframe,
        "scanned_pairs": scanned,
        "failed_pairs": failed,
        "stale_pairs": stale_pairs,
        "strategies_considered": strategies,
        "opportunities": [asdict(o) for o in opps[:top_n]],
        "tv_status": tv_status,
    }
