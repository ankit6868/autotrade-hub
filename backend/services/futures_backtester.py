"""
Futures backtester — extends the native backtester with leverage, liquidation,
and short positions.

Key differences from spot:
  • Leverage amplifies P&L: pnl_pct = price_move_pct × leverage
  • Liquidation: if price moves 100/leverage % against you → full margin loss
  • Both LONG and SHORT positions supported
  • Funding fee: 0.03% per 8h applied to open positions (KuCoin default)
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

import pandas as pd

import bisect

from backend.services.native_backtester import (
    add_indicators, load_futures_ohlcv, load_funding_history,
    _signal_miss_candle_short, _signal_miss_candle_long,
    _signal_macd_crossover, _signal_rsi_bollinger, _signal_ema_scalping,
    _guess_strategy,
)

# ── Production-grade market-realism constants ─────────────────────────────
#
# Real KuCoin futures retail (VIP0) fee schedule, published at
# https://www.kucoin.com/vip/level. These ARE deducted from the simulated
# balance — backtests are only meaningful if they include the same costs
# the strategy will pay in production. Treat the result as a realistic
# net-of-fees P&L, not a hypothetical gross figure.
#
# Application rules per fill type:
#   - ENTRY        : taker (strategy entries are market orders by default)
#   - STOP_LOSS    : taker (stop triggers market sell on KuCoin futures)
#   - LIQUIDATION  : taker (forced market liquidation)
#   - TAKE_PROFIT  : maker (TP set as a limit order, fills passively)
#
# This is conservative — a strategy that places limit entries (post-only)
# would pay maker on entry too, slightly improving net P&L. We assume
# market entries because that's what most strategy.entry() calls produce.

# KuCoin futures fee tiers (public schedule, retail VIP0):
KUCOIN_TAKER_FEE = 0.0006        # 0.06% — market orders, SL, liquidation
KUCOIN_MAKER_FEE = 0.0002        # 0.02% — limit fills (TP)

# Full VIP fee schedule (KuCoin Futures, retail). Backtest uses the tier
# selected by the user — hardcoded VIP0 over-estimates fees for anyone
# trading at meaningful volume. At VIP12 maker is NEGATIVE (rebate paid
# back to trader). Source: kucoin.com/vip/level
#
# Format: tier → (maker, taker) as decimals (0.0006 = 0.06%).
# Maker can be negative when the exchange pays a rebate at high tiers.
KUCOIN_FUTURES_FEE_TIERS: dict[int, tuple[float, float]] = {
    0:  (0.00020,  0.00060),
    1:  (0.00018,  0.00055),
    2:  (0.00016,  0.00050),
    3:  (0.00014,  0.00043),
    4:  (0.00012,  0.00038),
    5:  (0.00010,  0.00030),
    6:  (0.00008,  0.00025),
    7:  (0.00006,  0.00022),
    8:  (0.00003,  0.00020),
    9:  (0.00000,  0.00018),
    10: (-0.00005, 0.00015),
    11: (-0.00006, 0.00013),
    12: (-0.00008, 0.00012),
}


def _fees_for_tier(vip_tier: int) -> tuple[float, float]:
    """Return (maker_rate, taker_rate) for the given VIP tier.

    Clamps to [0, 12]; falls back to VIP0 for any unrecognised value so
    a misconfigured tier never silently produces wrong fees.
    """
    clamped = max(0, min(12, int(vip_tier)))
    return KUCOIN_FUTURES_FEE_TIERS.get(clamped, KUCOIN_FUTURES_FEE_TIERS[0])

# Slippage in basis points (1bp = 0.01%). Applied AGAINST the position on
# exits. Stops typically fill worse than the trigger price; take-profits
# usually fill at or near the limit (favourable book) so we apply less.
SLIPPAGE_BPS_STOP  = 5    # 5 bps = 0.05% adverse slippage on stop_loss
SLIPPAGE_BPS_TP    = 2    # 2 bps = 0.02% slippage on take_profit
SLIPPAGE_BPS_LIQ   = 15   # 15 bps on liquidation — books are typically thin
SLIPPAGE_BPS_ENTRY = 2    # 2 bps on entry (market fill)
SLIPPAGE_BPS_FLIP  = 5    # 5 bps when force-closing on new signal (market exit)

# When the user UNCHECKS "Include real trading costs" they want a PURE
# strategy backtest — matches TradingView's default (commission=0,
# slippage=0). The same constants apply when deduct_real_costs=False;
# they get overridden to 0 in run_futures_backtest. This way the user
# sees the strategy's THEORETICAL edge without execution drag.

# Fallback funding fee per settlement if KuCoin's history endpoint
# returns no data for the range. Real applied rate comes from
# /api/v1/contract/funding-rates per settlement.
FUNDING_RATE_FALLBACK = 0.0003   # 0.03%

# KuCoin funding settlements happen at fixed UTC clock times, not at
# arbitrary 8h intervals from trade open. We check these for each open
# position bar-by-bar and apply funding when a settlement falls inside
# the bar's window.
FUNDING_SETTLEMENT_HOURS_UTC = (0, 8, 16)   # 00:00, 08:00, 16:00 UTC


def _funding_rate_for_ts(funding_sorted: list[tuple[int, float]],
                         bar_ts_secs: int) -> float:
    """Binary-search the most recent funding rate at or before `bar_ts_secs`.

    Funding history is sorted by timepoint; we want the rate that was last
    settled before this bar — that's what would have actually been charged
    on a position open at this moment.
    """
    if not funding_sorted:
        return FUNDING_RATE_FALLBACK
    # `bisect_right` finds first index where ts > bar_ts; we want the one
    # right before that.
    timestamps = [t for (t, _) in funding_sorted]
    idx = bisect.bisect_right(timestamps, bar_ts_secs) - 1
    if idx < 0:
        return FUNDING_RATE_FALLBACK
    return funding_sorted[idx][1]


def _funding_settlements_in_window(window_start: int, window_end: int) -> list[int]:
    """Return UTC timestamps of any funding settlements occurring strictly
    inside (window_start, window_end] — used to charge funding on the bars
    that actually contain a 00/08/16 UTC settlement, instead of every Nth
    bar from trade open (which is what the legacy code did and which can
    mis-charge by up to 8 hours depending on entry time).
    """
    if window_end <= window_start:
        return []
    # Walk forward in 1h chunks from the first hour after window_start
    # until we pass window_end. 8 candidate hours per day → cheap.
    out = []
    start_dt = datetime.utcfromtimestamp(window_start)
    # Round up to next hour
    next_hour_ts = (window_start // 3600 + 1) * 3600
    cur = next_hour_ts
    while cur <= window_end:
        hour = datetime.utcfromtimestamp(cur).hour
        if hour in FUNDING_SETTLEMENT_HOURS_UTC:
            out.append(cur)
        cur += 3600
    return out


# Maintenance-margin schedule for liquidation. KuCoin uses a tiered
# system where larger positions have higher maintenance margin. For the
# starting balances this app supports ($100–$10k paper), the smallest
# tier (0.5%) is correct. Documented here so a future bump to position-
# sizing tiers is one place to change.
def _maintenance_margin_for_notional(notional_usdt: float) -> float:
    """Return the maintenance margin fraction for a given notional size."""
    # KuCoin BTC perpetual tier schedule (truncated; matches retail sizes):
    if   notional_usdt < 50_000:    return 0.005    # 0.5%
    elif notional_usdt < 250_000:   return 0.01     # 1%
    elif notional_usdt < 1_000_000: return 0.025    # 2.5%
    else:                           return 0.05     # 5%


def _calc_liquidation(entry: float, direction: str, leverage: int,
                      notional_usdt: float = 0.0) -> float:
    """Return the liquidation price using tiered maintenance margin."""
    mm = _maintenance_margin_for_notional(notional_usdt)
    if direction == "long":
        return round(entry * (1 - 1.0 / leverage + mm), 6)
    else:
        return round(entry * (1 + 1.0 / leverage - mm), 6)


def _resolve_intrabar_path(bar_open: float, bar_close: float,
                           direction: str) -> str:
    """OHLC-path inference for same-bar SL+TP ambiguity.

    When both SL and TP fall inside a bar's [low, high] range, the engine
    must decide which was touched FIRST. The bar's OHLC shape tells us
    the most likely intra-bar path:

      Bullish bar (close >= open) → path = open → low → high → close
        Price went DOWN first (touching low side), then UP.
        For LONG: SL (below entry) hit first → 'sl'
        For SHORT: TP (below entry) hit first → 'tp'

      Bearish bar (close < open) → path = open → high → low → close
        Price went UP first (touching high side), then DOWN.
        For LONG: TP (above entry) hit first → 'tp'
        For SHORT: SL (above entry) hit first → 'sl'

    This is the standard quant assumption (used in backtrader, vectorbt,
    and similar libraries) and is dramatically more accurate than the
    "closer to bar open" heuristic the engine used previously. The
    heuristic was a guess; this uses real bar information.

    Doji bars (close == open) get bullish treatment (low first). Rare
    enough that the bias is negligible.

    Returns 'sl' or 'tp'.
    """
    bullish_bar = bar_close >= bar_open
    if direction == "long":
        # SL below entry (low side), TP above entry (high side)
        return 'sl' if bullish_bar else 'tp'
    else:  # short
        # SL above entry (high side), TP below entry (low side)
        return 'tp' if bullish_bar else 'sl'


def _resolve_subbar_path(sub_bars_in_window: list, direction: str,
                         sl_price: float, tp_price: float) -> str:
    """Sub-bar SL/TP resolution using 1m candles within a higher-TF bar.

    sub_bars_in_window: list of (sub_high, sub_low) tuples in chronological
                       order for the sub-bars contained in the main bar's
                       time window.

    Walks the sub-bars in order. For each one, checks if SL or TP was
    touched. Returns 'sl' or 'tp' for the FIRST level hit. If both are
    touched in the SAME sub-bar (still ambiguous), the caller should
    fall back to OHLC-path inference using the main bar.

    Returns 'sl', 'tp', or 'still_ambiguous' (caller falls back).
    """
    for sub_h, sub_l in sub_bars_in_window:
        if direction == "long":
            sl_hit_here = sub_l <= sl_price
            tp_hit_here = sub_h >= tp_price
        else:
            sl_hit_here = sub_h >= sl_price
            tp_hit_here = sub_l <= tp_price

        if sl_hit_here and tp_hit_here:
            # Same sub-bar contains both — still ambiguous at this resolution.
            return 'still_ambiguous'
        if sl_hit_here:
            return 'sl'
        if tp_hit_here:
            return 'tp'

    # Neither was hit in any sub-bar (shouldn't happen if main bar contained
    # both — this means sub-bar data was incomplete). Caller falls back.
    return 'still_ambiguous'


def _resolve_ambiguous_first_hit(
    tp_level: float, sl_level: float, direction: str,
    bar_open: float, bar_close: float, bar_ts_secs: int,
    tick_precision: bool, sub_bars_by_main_ts: dict
) -> tuple[str, str]:
    """Single entry point for same-bar SL+TP ambiguity resolution.

    Priority (when tick_precision=True):
      1. Sub-bar replay (highest accuracy) — walks 1m bars within the
         main TF bar's window to find which level was hit first
      2. OHLC-path inference (fallback when sub-bar ambiguous or absent)

    When tick_precision=False: uses the legacy "closer to bar open"
    heuristic so existing behaviour is unchanged.

    Returns (first_hit, method) where:
      first_hit ∈ {'tp', 'sl'}
      method   ∈ {'sub_bar', 'path_infer', 'heuristic'}
    """
    if tick_precision:
        sub_bars = sub_bars_by_main_ts.get(bar_ts_secs, [])
        if sub_bars:
            verdict = _resolve_subbar_path(sub_bars, direction, sl_level, tp_level)
            if verdict in ('sl', 'tp'):
                return verdict, 'sub_bar'
            # Sub-bar said both hit in the same minute — fall through to
            # path inference using the parent bar's shape.
        verdict = _resolve_intrabar_path(bar_open, bar_close, direction)
        return verdict, 'path_infer'

    # Legacy heuristic — keep exact prior behaviour when precision is OFF.
    verdict = 'tp' if abs(bar_open - tp_level) < abs(bar_open - sl_level) else 'sl'
    return verdict, 'heuristic'


def _compute_be_price(entry_price: float, direction: str, leverage: int,
                      be_mode: str, be_buffer_pct: float) -> float:
    """Compute the break-even SL price per the ARM spec.

    Modes:
      - "leverage": buffer = leverage / 10 (in %). e.g. 20x → 2% buffer.
        Equivalent to `entry × (1 + leverage/1000)`. Higher leverage → wider
        BE because position is more sensitive (also matches the user's
        spec exactly).
      - "manual_pct": buffer = arm_be_buffer_pct (in %). User-supplied.
      - "entry": no buffer at all — SL moves exactly to entry price.
    """
    if be_mode == "leverage":
        buffer_pct = leverage / 1000.0       # 10x → 1%, 20x → 2%, etc.
    elif be_mode == "manual_pct":
        buffer_pct = max(0.0, be_buffer_pct or 0.0) / 100.0
    else:                                      # "entry" or unknown → no buffer
        buffer_pct = 0.0
    if direction == "long":
        return entry_price * (1.0 + buffer_pct)
    else:
        return entry_price * (1.0 - buffer_pct)


def _apply_slippage(price: float, direction: str, side: str, bps: float) -> float:
    """Apply slippage to an exit price.

    Slippage is ADVERSE — it makes the trade worse than the trigger level.
    For a long exit: slippage moves the fill DOWN (sells lower).
    For a short exit: slippage moves the fill UP (buys higher).
    """
    if bps <= 0:
        return price
    factor = bps / 10_000.0
    if side == "exit":
        if direction == "long":
            return price * (1 - factor)   # sell lower
        else:
            return price * (1 + factor)   # buy higher
    else:   # entry
        if direction == "long":
            return price * (1 + factor)   # buy higher
        else:
            return price * (1 - factor)   # sell lower


COMMISSION_RATE = KUCOIN_TAKER_FEE  # Real KuCoin taker fee, deducted from
                                     # balance. Per-fill rate (taker vs maker)
                                     # is decided at the call-site based on
                                     # whether the exit was SL/liq (taker) or
                                     # TP (maker).


def run_futures_backtest(
    strategy_name: str,
    pairs: list[str],
    timeframe: str,
    timerange: str,
    leverage: int = 10,
    starting_balance: float = 1000.0,
    stoploss_pct: float = 3.0,        # % e.g. 3 → -3%
    take_profit_pct: float = 1.5,     # % e.g. 1.5 → +1.5%
    risk_per_trade: float = 0.05,     # fraction of balance used as margin per trade
    generated_code: str | None = None,  # user's IStrategy Python class (Freqtrade-style)
    ml_filter_model: dict | None = None,  # optional ML loss-filter (deserialized
                                        # {model, mu, sd, conf}); when present, signals
                                        # the model scores below conf are SKIPPED.
    sl_structure_buffer_pct: float = 0.0,  # "stop beyond structure" (the book's #1 risk
                                        # rule): push a STRUCTURAL stop this % of entry
                                        # FURTHER from entry, so it sits past the swing
                                        # level instead of on it (where stops get hunted).
                                        # 0 = off (no change). Only affects strategy-
                                        # provided structural SLs, never slider SLs.
    force_slider_sltp: bool = False,    # when True, override strategy-defined structural SL/TP
                                        # with slider values. Used by auto-tune so each grid
                                        # cell actually tests a different SL/TP combo even
                                        # for strategies like SMCStrategyTV that normally
                                        # use their own structural levels.
    max_concurrent_positions: int = 1,  # TradingView-default pyramiding=0 → only 1 position
                                        # open at a time. Set to N to allow up to N positions
                                        # to stack (matches Pine's `pyramiding = N-1`). The
                                        # earlier "concurrent" mode (∞) inflated trade counts
                                        # because the same condition firing 4 bars in a row
                                        # would open 4 separate positions instead of 1.
    position_mode: str = "single",      # "single" | "hedge" | "concurrent"
                                        # - "single": TV-default. One position per pair.
                                        #   Opposite signal triggers stop-and-reverse
                                        #   (close existing + open new, both at bar open).
                                        # - "hedge": Up to 1 LONG + 1 SHORT open per pair
                                        #   simultaneously. No stop-and-reverse — each
                                        #   position runs to its own SL/TP/ARM independently.
                                        #   Useful for mean-reversion strategies (BB) where
                                        #   opposite signals fire often and stop-and-reverse
                                        #   was killing trades mid-range. NOT TV-compatible
                                        #   (Pine doesn't model this).
                                        # - "concurrent": legacy pyramiding mode. Use
                                        #   max_concurrent_positions to set the cap.
    deduct_real_costs: bool = False,    # When True, funding fees + KuCoin taker/maker fees
                                        # are deducted from the simulated balance — gives a
                                        # production-grade net P&L. When False (default), the
                                        # P&L reflects ONLY price action × leverage — useful
                                        # for evaluating the strategy's edge in isolation,
                                        # without the friction of execution costs. Slippage
                                        # is always applied because it's a fill-quality
                                        # assumption (not a cost the exchange collects).
    # ── Advanced Risk Management (ARM) — partial TP + BE trail + trail-to-TP1 ──
    arm_enabled: bool      = False,     # Master switch. When False, the engine uses the
                                        # existing single-TP or strategy-multi-TP behaviour
                                        # and ignores all the arm_* params below.
    arm_tp1_close_pct: float = 50.0,    # 1-99: % of position closed at TP1. Remainder closes
                                        # at TP2. Matches user spec ("TP1 Booking %").
    arm_be_mode: str       = "leverage",   # "leverage" | "manual_pct" | "entry"
                                            # - "leverage": BE = entry × (1 + leverage/1000)
                                            #   e.g. 10x → 1% buffer, 20x → 2%, 30x → 3%
                                            # - "manual_pct": BE = entry × (1 + arm_be_buffer_pct/100)
                                            # - "entry": BE = entry (no buffer — pure breakeven)
    arm_be_buffer_pct: float = 1.0,     # Used only when arm_be_mode == "manual_pct"
    arm_trail_to_tp1: bool = True,      # When True: after TP1 hit AND price progresses halfway
                                        # from TP1 to TP2, SL moves UP to TP1. Locks in TP1
                                        # profit on the remainder before TP2 hits.
    # ── Tick-level SL/TP precision ────────────────────────────────────
    tick_precision: bool = False,       # When True: replaces the "closer to bar open" heuristic
                                        # for same-bar SL+TP ambiguity with two better methods:
                                        #   • For 1m bars or any TF: OHLC-path inference
                                        #     (uses bar shape to infer likely intra-bar path).
                                        #   • For 5m+ bars: ALSO fetches 1m sub-bar data
                                        #     and replays each minute to find which level
                                        #     was hit first (highest accuracy).
                                        # Improves SL/TP fill accuracy on 1m scalping backtests
                                        # by ~30-50% vs the open-distance heuristic.
    # ── Fees: VIP tier + maker-only entry mode ─────────────────────────
    vip_tier: int = 0,                  # KuCoin Futures VIP tier 0..12. Each tier has its
                                        # own (maker, taker) rates. VIP0 default (0.06% taker)
                                        # over-estimates fees for anyone with real volume.
                                        # At VIP12 maker is -0.008% (rebate paid to trader).
    maker_only_entry: bool = False,     # When True: every entry is simulated as a maker
                                        # limit order at the signal price. Pays MAKER fee
                                        # instead of TAKER. Realistic non-fill simulation:
                                        # the order only "fills" if the NEXT bar's range
                                        # actually touches the limit price; otherwise the
                                        # signal is skipped (counted as no-fill). This is
                                        # the single largest scalping cost-saving lever —
                                        # at VIP3 it cuts entry fees by 3x vs taker.
    # ── Phase 4b: timeframe-aware risk engine ─────────────────────────
    risk_overrides_for_run: dict | None = None,  # NICE-4: per-user TF risk overrides
    use_risk_engine: bool = False,      # When True, every signal's SL/TP is routed through
                                        # backend.services.risk_engine.compute_tp_sl which:
                                        #   • Honours strategy-provided structural SL/TP if
                                        #     direction-valid AND RR meets the per-TF min RR
                                        #   • Otherwise computes ATR-based SL/TP using the
                                        #     per-TF multiplier table (1m scalp / 5m fast /
                                        #     15m intraday / 30m large / 1h swing / 4h position)
                                        #   • Rejects signals where RR < per-TF min RR
                                        #     (e.g. SMC pivot too close to price on 1m)
                                        #   • Rejects signals where ATR-based stop > 8% of entry
                                        #     (crash-spike noise)
                                        # Backtester counts and reports rejected signals in
                                        # `signal_dispositions.risk_engine_rejected`.
                                        # Default OFF for backward compatibility with previous
                                        # tuning runs — turn ON for production parity with
                                        # the live bot engine (which uses risk_engine always).
    strategy_flags: dict | None = None,  # UI flag overrides applied onto the strategy
                                        # instance (e.g. {'use_exit_signals': True} for
                                        # StrategyAsh's CHoCH exit, or {'USE_ATR_STOPS': True}
                                        # / {'USE_DYNAMIC_EXITS': True} for the LDC). None =
                                        # use the strategy's own class defaults.
) -> dict:
    """
    Run a leveraged futures backtest matching TradingView's methodology:

    TradingView parity fixes applied:
      1. Commission: 0.05% per side (entry + exit) on position value
      2. Entry timing: signal fires at bar[i] close → entry at bar[i+1] OPEN
         (matches TradingView's default calc_on_every_tick=false behaviour)
      3. SL/TP same-bar resolution: if both hit in same candle, use bar open
         to determine which was hit first (TradingView default logic)
      4. Funding fee: 0.03% per 8h on position value (KuCoin standard)

    Returns a dict matching the shape expected by the frontend results component.
    """
    # ── ZERO-FRICTION MODE for pure-strategy backtests ──────────────────
    # Shadow module-level SLIPPAGE_BPS_* to 0 when costs are off so the
    # output matches TradingView. The early-return paths below (invalid
    # timerange, no data) used to LEAK those zeros into subsequent calls;
    # the explicit restore at every return point + finally guards below
    # ensure the constants are always restored exactly once.
    global SLIPPAGE_BPS_STOP, SLIPPAGE_BPS_TP, SLIPPAGE_BPS_LIQ, SLIPPAGE_BPS_ENTRY, SLIPPAGE_BPS_FLIP
    _saved_slip = (SLIPPAGE_BPS_STOP, SLIPPAGE_BPS_TP, SLIPPAGE_BPS_LIQ, SLIPPAGE_BPS_ENTRY, SLIPPAGE_BPS_FLIP)
    if not deduct_real_costs:
        SLIPPAGE_BPS_STOP = 0
        SLIPPAGE_BPS_TP   = 0
        SLIPPAGE_BPS_LIQ  = 0
        SLIPPAGE_BPS_ENTRY = 0
        SLIPPAGE_BPS_FLIP = 0

    def _restore_slip():
        """Restore the module-level slippage constants. Called at every
        return path so subsequent backtests see correct values."""
        global SLIPPAGE_BPS_STOP, SLIPPAGE_BPS_TP, SLIPPAGE_BPS_LIQ, SLIPPAGE_BPS_ENTRY, SLIPPAGE_BPS_FLIP
        SLIPPAGE_BPS_STOP, SLIPPAGE_BPS_TP, SLIPPAGE_BPS_LIQ, SLIPPAGE_BPS_ENTRY, SLIPPAGE_BPS_FLIP = _saved_slip

    # ── Parse timerange ───────────────────────────────────────────────────
    try:
        parts = timerange.split("-")
        start_ts = int(datetime(int(parts[0][:4]), int(parts[0][4:6]),
                                int(parts[0][6:8])).timestamp())
        end_ts   = int(datetime(int(parts[1][:4]), int(parts[1][4:6]),
                                int(parts[1][6:8])).timestamp())
    except Exception:
        _restore_slip()
        return {"error": f"Invalid timerange '{timerange}'. Use YYYYMMDD-YYYYMMDD."}

    # ── Pick the signal function for this run ──────────────────────────
    # Priority:
    #   1. If the user supplied generated_code (their authored IStrategy
    #      class), exec it and read enter_long/enter_short signal columns.
    #      This is what runs for ALL strategies the user creates via the
    #      Strategy Editor — their actual code, not a name-match heuristic.
    #   2. Otherwise (built-in template names like "SMCStrategyTV") fall
    #      back to _guess_strategy which maps the name to one of the
    #      hardcoded Python signal functions in native_backtester.
    use_user_strategy = bool(generated_code and generated_code.strip())
    user_strategy_error: str | None = None
    signal_fn = None
    if not use_user_strategy:
        signal_fn = _guess_strategy(strategy_name)
    all_trades: list[dict] = []
    balance = starting_balance

    # Resolve the actual maker/taker fee rates for this run from the VIP
    # tier. These shadow the module-level constants throughout the function.
    maker_fee_rate, taker_fee_rate = _fees_for_tier(vip_tier)
    # Sanity-check tallies surfaced in the response so the user can see if
    # the kline range was incomplete (KuCoin sometimes has gaps on older
    # data) and the funding history loaded as expected.
    data_diagnostics: dict[str, dict] = {}

    # Resolve buffer: extra candles fetched AFTER `end_ts` so positions
    # opened near the end of the backtest can still hit their SL/TP/
    # liquidation rather than being excluded as "still open". Capped at
    # 30 days so we don't blow up downloads on small backtests.
    RESOLVE_BUFFER_SECS = 30 * 24 * 3600

    # ── Deep-history WARMUP for ML strategies (e.g. LDC max_bars_back=2000) ──
    # The LIVE bot deep-fetches `max_bars_back` candles so its ML model has its
    # full training window. The backtester previously fetched ONLY the user's
    # window, so on a short period (e.g. 1M/4h ≈ 180 bars) the Lorentzian
    # Classifier was data-starved and produced erratic / zero signals — and
    # results differed run-to-run. We now pre-read the strategy's declared
    # max_bars_back (cheap dummy compile) and fetch that many EXTRA bars BEFORE
    # the window as warm-up. Warm-up bars feed indicators only; NEW entries
    # still fire exclusively inside the user's [start_ts, end_ts] window.
    # Strategies that don't declare a deep window (the vast majority) get
    # warmup_bars=0 → byte-identical behaviour to before.
    warmup_bars = 0
    if use_user_strategy:
        try:
            from backend.services.strategy_runner import evaluate_strategy as _eval_probe
            import pandas as _pd_probe
            _dummy = _pd_probe.DataFrame({
                "date":  _pd_probe.date_range("2024-01-01", periods=300, freq="15min", tz="UTC"),
                "open":  [100.0] * 300, "high": [101.0] * 300,
                "low":   [99.0] * 300,  "close": [100.5] * 300, "vol": [1000.0] * 300,
            })
            _mbb = _eval_probe(generated_code, _dummy).attrs.get("class_max_bars_back")
            if isinstance(_mbb, int) and _mbb > 200:
                warmup_bars = min(5000, _mbb)
        except Exception:
            warmup_bars = 0

    for pair in pairs:
        tf_secs_per_bar = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800,
                           "1h": 3600, "4h": 14400, "1d": 86400}.get(timeframe, 900)
        # ── Load FUTURES OHLCV (not spot — see comment in native_backtester) ─
        # Extended range = user's window + resolve buffer.
        fetch_end_ts = end_ts + RESOLVE_BUFFER_SECS
        # Warm-up prefix for ML strategies: fetch `warmup_bars` candles BEFORE
        # the user's window so the model has its full history. 0 for everything
        # else → fetch_start_ts == start_ts (unchanged).
        fetch_start_ts = start_ts - warmup_bars * tf_secs_per_bar
        try:
            df = load_futures_ohlcv(pair, timeframe, fetch_start_ts, fetch_end_ts)
        except Exception as e:
            _restore_slip()
            return {"error": f"Futures data download failed for {pair}: {e}"}
        # ── Load real funding rates over the same extended range ─────────
        funding_sorted = load_funding_history(pair, start_ts, fetch_end_ts)
        # Per-pair coverage diagnostics — measured against the USER'S
        # requested window, not the extended fetch. Buffer bars are an
        # implementation detail; the user only cares whether their
        # requested period is well-covered.
        # Positional indices of the user's window [start_ts, end_ts] within the
        # fetched df. With a warm-up prefix the window no longer starts at index
        # 0, so we locate it by timestamp. first/last_in_window_idx bound where
        # NEW entries may fire; warm-up bars (before first) only feed indicators.
        _ts = (df["date"].astype("int64") // 10**9)
        _win = ((_ts >= start_ts) & (_ts <= end_ts)).to_numpy().nonzero()[0]
        if len(_win):
            first_in_window_idx = int(_win[0])
            last_in_window_idx  = int(_win[-1])
            in_window_count     = int(len(_win))
        else:
            first_in_window_idx = 0
            last_in_window_idx  = len(df) - 1
            in_window_count     = len(df)
        expected_bars = max(1, (end_ts - start_ts) // tf_secs_per_bar)
        data_diagnostics[pair] = {
            "candles_loaded":    in_window_count,
            "candles_expected":  int(expected_bars),
            "coverage_pct":      round(100.0 * in_window_count / expected_bars, 1),
            "funding_records":   len(funding_sorted),
            "funding_source":    "kucoin_history" if funding_sorted else "fallback_0.03%",
            "signal_source":     "user_strategy" if use_user_strategy else f"builtin:{strategy_name}",
            "resolve_buffer_bars": int(len(df) - (last_in_window_idx + 1)),
            "warmup_bars":       int(first_in_window_idx),
        }
        df = add_indicators(df)

        # ── Tick precision: load 1m sub-bar data for higher TFs ─────────
        # When enabled AND main TF > 1m, fetch 1m candles for the same
        # period and bucket them by main-bar timestamp. Used inside the
        # bar loop to resolve same-bar SL+TP ambiguity at finer granularity.
        sub_bars_by_main_ts: dict[int, list[tuple[float, float]]] = {}
        precision_data_loaded = False
        if tick_precision and timeframe != "1m":
            try:
                sub_df = load_futures_ohlcv(pair, "1m", start_ts, fetch_end_ts)
                # Bucket each 1m bar into its parent main-TF bar's window.
                # Main bar at timestamp T covers [T - tf_secs, T] in ms-secs.
                # So a 1m bar at sub_ts belongs to main bar whose ts is
                # the smallest multiple of tf_secs_per_bar that is >= sub_ts.
                sub_df["ts_secs"] = sub_df["date"].astype("int64") // 10**9
                for ts_s, sub_h, sub_l in zip(
                    sub_df["ts_secs"].to_numpy(),
                    sub_df["high"].to_numpy(),
                    sub_df["low"].to_numpy(),
                ):
                    # Round UP to the next main-bar boundary (KuCoin candles
                    # are timestamped at bar CLOSE, so a 1m bar at 10:01:00
                    # belongs to the 15m bar that closes at 10:15:00).
                    main_ts = ((int(ts_s) + tf_secs_per_bar - 1)
                               // tf_secs_per_bar) * tf_secs_per_bar
                    sub_bars_by_main_ts.setdefault(main_ts, []).append(
                        (float(sub_h), float(sub_l))
                    )
                precision_data_loaded = True
                data_diagnostics[pair]["precision_sub_bars_loaded"] = len(sub_df)
                data_diagnostics[pair]["precision_method"] = "sub_bar_1m"
            except Exception as e:
                # Non-fatal — fall back to OHLC path inference on each
                # ambiguous bar.
                data_diagnostics[pair]["precision_method"] = "path_inference (sub-bar fetch failed)"
                data_diagnostics[pair]["precision_fetch_error"] = str(e)
        elif tick_precision:
            data_diagnostics[pair]["precision_method"] = "path_inference (1m TF — no sub-bar source)"

        # Counters surfaced in the response so the user can see how many
        # ambiguous same-bar SL+TP bars were resolved by precision.
        precision_ambiguous_seen      = 0
        precision_resolved_sub_bar    = 0
        precision_resolved_path_infer = 0
        precision_resolved_heuristic  = 0   # fallback when precision off

        # Defaults that MUST exist for every code path (user-strategy success,
        # name-matched fallback, or compile failure) so the per-bar loop never
        # hits an undefined variable. pair_max_hold_candles was previously set
        # only inside the user-strategy try block — a strategy that failed to
        # compile (→ _guess_strategy fallback) crashed the loop with an
        # UnboundLocalError. The exit-signal trio is the same contract.
        pair_max_hold_candles = 0
        use_exit_signals = False
        exit_long_arr = None
        exit_short_arr = None

        # ── User-strategy path: exec their generated_code and pre-populate
        # enter_long / enter_short signal columns on the dataframe ─────
        if use_user_strategy:
            try:
                from backend.services.strategy_runner import (
                    evaluate_strategy, make_signal_fn_from_df,
                )
                # MTF analyzer: pass the current pair + tf so HTF context
                # is available to populate_indicators. historical_anchor_ts
                # caps HTF candles to the backtest's window end so we
                # don't fetch HTF bars from periods AFTER the backtest
                # window (correctness + avoids over-fetching).
                #
                # The earlier code commented this intent but never passed
                # the anchor — mtf_analyzer was fetching HTF candles all
                # the way to "now" (which on long-running deployments
                # could be years past the backtest period). For most
                # strategies merge_asof's `direction=backward` already
                # prevented LTF rows from seeing future HTF bars, but
                # explicitly anchoring is belt-and-suspenders correct
                # AND it cuts down REST traffic for old-period backtests.
                df = evaluate_strategy(
                    generated_code, df,
                    pair=pair, execution_tf=timeframe,
                    historical_anchor_ts=fetch_end_ts,
                    overrides=({"flags": strategy_flags} if strategy_flags else None),
                )
                # If the user's strategy class declares its OWN stoploss /
                # minimal_roi, prefer those over slider values BY DEFAULT —
                # the class is the source of truth for the strategy's risk
                # math. EXCEPT when force_slider_sltp=True (UI's "SL/TP
                # source = From sliders below"), in which case the user has
                # explicitly asked to override the strategy with their own
                # slider values. Previously this branch fired unconditionally
                # → flipping the UI toggle had no effect, the slider was
                # always silently swapped for the class's hardcoded -0.015
                # / 0.03. THAT was the "215 in-trade skips on 1m" bug.
                if not force_slider_sltp:
                    class_sl = df.attrs.get("class_stoploss_pct")
                    class_tp = df.attrs.get("class_take_profit_pct")
                    if class_sl is not None:
                        data_diagnostics[pair]["override_sl_from_class"] = (
                            f"{class_sl}% (slider was {stoploss_pct}%)"
                        )
                        stoploss_pct = class_sl
                    if class_tp is not None:
                        data_diagnostics[pair]["override_tp_from_class"] = (
                            f"{class_tp}% (slider was {take_profit_pct}%)"
                        )
                        take_profit_pct = class_tp
                else:
                    # The toggle was set to "From sliders" — record that
                    # we're respecting it so the UI diagnostic is honest.
                    data_diagnostics[pair]["slider_overrides_class"] = (
                        f"SL {stoploss_pct}% / TP {take_profit_pct}% "
                        f"(class declared "
                        f"{df.attrs.get('class_stoploss_pct')}% / "
                        f"{df.attrs.get('class_take_profit_pct')}% — ignored)"
                    )
                # Pick up the strategy's opt-in max-hold gate. When set,
                # any position open longer than this gets force-closed in
                # the per-bar loop — matches the live/paper engine's
                # behaviour so backtest results reflect what the bot
                # actually does in production. Strategies that don't
                # declare class_max_hold_candles see no change.
                class_max_hold = df.attrs.get("class_max_hold_candles")
                pair_max_hold_candles = int(class_max_hold) if class_max_hold else 0
                if pair_max_hold_candles > 0:
                    data_diagnostics[pair]["max_hold_candles"] = pair_max_hold_candles
                # ── Universal Bar-hold override (UI flag) ────────────────
                # The backtest UI exposes a Bar-hold timer for EVERY strategy
                # (off by default for those that don't declare one). When the
                # user sets max_hold_candles via strategy_flags it takes
                # precedence over the class-declared value — INCLUDING 0, which
                # DISABLES the timer (e.g. turning off StrategyAsh's 60-bar
                # backstop, or the LDC's 4-bar hold). Works for any strategy,
                # even ones that never declared class_max_hold_candles (the
                # instance setattr override is a no-op for those since the
                # attribute doesn't exist — this direct path is authoritative).
                if strategy_flags and "max_hold_candles" in strategy_flags:
                    try:
                        pair_max_hold_candles = max(0, int(strategy_flags["max_hold_candles"]))
                        data_diagnostics[pair]["max_hold_candles"] = pair_max_hold_candles
                        data_diagnostics[pair]["max_hold_source"] = "ui_override"
                    except (TypeError, ValueError):
                        pass
                signal_fn = make_signal_fn_from_df(
                    df, leverage, stoploss_pct, take_profit_pct,
                )
                # Opt-in explicit exit signals (close-to-flat) — parity with
                # the live/paper engine's _use_exit_signals path. Off unless
                # the strategy declares `use_exit_signals = True`, so existing
                # strategies are unaffected.
                use_exit_signals = bool(df.attrs.get("class_use_exit_signals"))
                exit_long_arr  = getattr(signal_fn, "exit_long", None)
                exit_short_arr = getattr(signal_fn, "exit_short", None)
                # Surface the count of fired signals in diagnostics so the
                # user can tell at a glance whether their strategy actually
                # produces entries on this data.
                el_count = int((df.get("enter_long", 0) == 1).sum()) if "enter_long" in df.columns else 0
                es_count = int((df.get("enter_short", 0) == 1).sum()) if "enter_short" in df.columns else 0
                data_diagnostics[pair]["entry_signals_long"]  = el_count
                data_diagnostics[pair]["entry_signals_short"] = es_count
                # Edge count (0→1 transitions) = unique trade opportunities,
                # which is what TradingView's strategy.entry() actually acts
                # on. A signal staying True for 20 bars is ONE entry, not 20.
                # The diff trick: shift by 1 and count rows where prev=0, cur=1.
                if "enter_long" in df.columns:
                    el = df["enter_long"].fillna(0).astype(int)
                    el_edges = int(((el == 1) & (el.shift(1).fillna(0) == 0)).sum())
                else:
                    el_edges = 0
                if "enter_short" in df.columns:
                    es = df["enter_short"].fillna(0).astype(int)
                    es_edges = int(((es == 1) & (es.shift(1).fillna(0) == 0)).sum())
                else:
                    es_edges = 0
                data_diagnostics[pair]["entry_clusters_long"]  = el_edges
                data_diagnostics[pair]["entry_clusters_short"] = es_edges
                # Echo back what methods the user's class defines so the UI
                # can show them when 0 signals fire (helps debug "why aren't
                # my entries triggering?").
                data_diagnostics[pair]["strategy_class"]   = df.attrs.get("strategy_class")
                data_diagnostics[pair]["strategy_methods"] = df.attrs.get("strategy_methods", [])
                data_diagnostics[pair]["signal_columns"]   = df.attrs.get("signal_columns", [])
                # When zero entries fire, include the first 800 chars of the
                # user's generated code in the diagnostics so it's possible
                # to see at a glance whether the strategy actually has
                # populate_entry_trend / produces enter_long signals.
                if (el_count == 0 and es_count == 0):
                    snippet = (generated_code or "").strip()
                    if len(snippet) > 800:
                        snippet = snippet[:800] + "\n... (truncated)"
                    data_diagnostics[pair]["code_preview"] = snippet
            except Exception as e:
                # Two sub-cases:
                #
                #  a) User's code has populate_* hooks but raised at runtime.
                #     → real error, show in red.
                #
                #  b) User's code is a plain Python class without populate_*
                #     hooks (legacy strategies authored as Pine Script ports
                #     for the name-matched signal-pattern path). The runner
                #     deliberately raises RuntimeError with "has none of the
                #     populate_* hooks" so we fall back gracefully here.
                #     → expected behaviour, show in neutral colour.
                user_strategy_error = str(e)
                is_intended_fallback = "populate_* hooks" in user_strategy_error
                tag = "name-match" if is_intended_fallback else "user code failed"
                data_diagnostics[pair]["signal_source"]   = (
                    f"builtin:{strategy_name} ({tag})"
                )
                data_diagnostics[pair]["user_code_error"] = user_strategy_error
                data_diagnostics[pair]["fallback_intended"] = is_intended_fallback
                signal_fn = _guess_strategy(strategy_name)

                # Even when code execution fails, still try to extract the
                # strategy's intended SL/TP from the source via regex so the
                # backtest doesn't run with stale DB-default values that
                # invert the RR ratio. Same sanity-bounds as the AST path:
                # values like -0.99 (no-stop) or 100 (placeholder ROI) are
                # IGNORED so we don't apply nonsense overrides.
                # SKIPPED when force_slider_sltp=True — same fix as the
                # eval-success path above; without this the UI toggle to
                # "From sliders" was inert for any strategy that emitted
                # a stoploss=-0.015 line in its source.
                import re
                if not force_slider_sltp:
                    m_sl = re.search(
                        r"^\s*stoploss\s*=\s*(-?\d+(?:\.\d+)?)",
                        generated_code or "", re.MULTILINE,
                    )
                    if m_sl:
                        parsed_sl = abs(float(m_sl.group(1)))
                        if 0.001 <= parsed_sl <= 0.25:
                            data_diagnostics[pair]["override_sl_from_class"] = (
                                f"{parsed_sl*100}% (slider was {stoploss_pct}%, parsed from source)"
                            )
                            stoploss_pct = parsed_sl * 100
                        else:
                            data_diagnostics[pair]["class_stoploss_ignored"] = (
                                f"{parsed_sl*100:.1f}% — outside sane range, keeping slider {stoploss_pct}%"
                            )
                    m_tp = re.search(
                        r"minimal_roi\s*=\s*\{\s*[\"']0[\"']\s*:\s*(\d+(?:\.\d+)?)",
                        generated_code or "",
                    )
                else:
                    m_sl = None
                    m_tp = None
                if m_tp:
                    parsed_tp = float(m_tp.group(1))
                    if 0.001 <= parsed_tp <= 0.50:
                        data_diagnostics[pair]["override_tp_from_class"] = (
                            f"{parsed_tp*100}% (slider was {take_profit_pct}%, parsed from source)"
                        )
                        take_profit_pct = parsed_tp * 100
                    else:
                        data_diagnostics[pair]["class_take_profit_ignored"] = (
                            f"{parsed_tp*100:.1f}% — outside sane range, keeping slider {take_profit_pct}%"
                        )

        # ── Concurrent-position state ─────────────────────────────────
        # Every entry signal opens a NEW position alongside any already-
        # open ones (subject to free-margin checks). Each position runs
        # independently until it hits its own SL / TP / liquidation —
        # signals NEVER force-close existing positions. This matches
        # TradingView pyramiding>=N behaviour and the user's explicit
        # requirement that "every signal should be its own trade and
        # exit only on strategy rules, not on the next signal arriving".
        open_positions: list[dict] = []   # active trades
        pending_entries: list[tuple]= []  # to fill at next bar's open
        committed_margin = 0.0            # sum of margin across open_positions
        tf_secs_map = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800,
                       "1h": 3600, "4h": 14400}
        tf_secs = tf_secs_map.get(timeframe, 900)
        # Per-pair counters for the data-quality panel.
        trades_opened_long  = 0
        trades_opened_short = 0
        skipped_no_margin   = 0   # signal fired but free margin < threshold
        skipped_maker_nofill = 0  # maker-only mode: limit not reached by bar range
        skipped_risk_engine = 0   # Phase 4b: signal rejected by risk_engine (RR too low, ATR cap)
        risk_engine_rejections = []   # list of (bar_index, reason) for the diagnostic panel
        # Legacy fields kept for response-shape compatibility.
        skipped_in_trade    = 0
        skipped_cooldown    = 0
        cooldown_bars       = 0   # not used in concurrent mode
        # How often the engine actually used the strategy's SL/TP vs falling
        # back to slider values — surfaced in diagnostics so the user can
        # see at a glance whether the slider is doing anything.
        sltp_from_signal    = 0
        sltp_from_slider    = 0
        # ML loss-filter: precompute take/skip per bar (True = take). Fail-open.
        ml_skipped = 0
        ml_take = None
        if ml_filter_model is not None:
            try:
                from backend.services.ml_filter import take_mask as _ml_take_mask
                ml_take = _ml_take_mask(ml_filter_model, df)
            except Exception:
                ml_take = None

        n = len(df)
        for i in range(3, n):
            # Once we're past the user's window AND every position has
            # resolved, there's nothing left to simulate — exit early so
            # we don't burn through 30 days of buffer bars uselessly.
            if i > last_in_window_idx and not open_positions and not pending_entries:
                break
            row   = df.iloc[i]
            bar_o = row["open"]
            lo, hi = row["low"], row["high"]
            bar_ts_secs  = int(row["date"].timestamp())
            bar_start_ts = bar_ts_secs - tf_secs_per_bar

            # ── A. Open any pending entries at THIS bar's OPEN ────────────
            # (Signal at bar[i-1] close → fill at bar[i] open; matches TV.)
            #
            # TradingView parity: in Single-position mode (pyramiding = 0),
            # Pine's `strategy.entry()` does STOP-AND-REVERSE when an
            # opposite-direction signal fires while a position is open —
            # it closes the existing position AND opens the new one in the
            # opposite direction, both filling at the same bar's open price.
            # Without this, the app instead opens a 2nd concurrent position
            # in the opposite direction and lets the original run to SL/TP.
            # In the user's 1m SMC backtest, that was ~8 trades that should
            # have closed at small profits/losses on reversal but instead
            # ran all the way to SL — exactly the WR gap vs Pine's 31%.
            #
            # We force-close opposite-direction positions BEFORE processing
            # the pending entries — that way the SHORT exits at bar_o and
            # the new LONG opens at bar_o (same price for both fills), and
            # the SHORT doesn't get a chance to hit its own SL on this bar.
            # HEDGE MODE: skip stop-and-reverse entirely. Both directions
            # coexist on the same pair — each position runs independently
            # to its own SL/TP/ARM. Stop-and-reverse only applies in
            # "single" mode (the TV-default behaviour).
            if position_mode == "single" and max_concurrent_positions == 1 and open_positions and pending_entries:
                pending_dirs = {pe[0] for pe in pending_entries}
                still_open_after_reversal: list[dict] = []
                for opp in open_positions:
                    opp_dir = opp["direction"]
                    reversal_dir = "long" if opp_dir == "short" else "short"
                    if reversal_dir in pending_dirs:
                        # Close at bar_o with entry-side slippage (forced
                        # market exit on the opposite signal — same fee/
                        # slippage profile as the opposing entry that triggers it).
                        rev_exit_p = _apply_slippage(bar_o, opp_dir, "exit",
                                                     SLIPPAGE_BPS_FLIP)
                        rev_margin = opp["margin"] * opp.get("remaining_pct", 1.0)
                        rev_pos_value = rev_margin * leverage
                        if opp_dir == "long":
                            move_pct = (rev_exit_p - opp["entry_price"]) / opp["entry_price"]
                        else:
                            move_pct = (opp["entry_price"] - rev_exit_p) / opp["entry_price"]
                        rev_leg_pnl = max(rev_margin * move_pct * leverage, -rev_margin)
                        rev_fee = rev_pos_value * taker_fee_rate
                        if deduct_real_costs:
                            opp["fees_paid"] += rev_fee
                            balance -= rev_fee
                        rev_slippage = abs(bar_o - rev_exit_p) * (rev_pos_value / max(bar_o, 1e-9))
                        opp["slippage_paid"] += rev_slippage
                        opp["partial_pnl"] += rev_leg_pnl
                        opp["partial_exits"].append({
                            "bar_index": i,
                            "reason":    "reversal",
                            "price":     round(float(rev_exit_p), 4),
                            "close_pct": round(float(opp.get("remaining_pct", 1.0)), 4),
                            "pnl":       round(float(rev_leg_pnl), 4),
                        })
                        committed_margin -= rev_margin
                        balance += rev_leg_pnl
                        balance = max(balance, 0)
                        net_pnl_abs = opp["partial_pnl"] - opp["fees_paid"]
                        profit_pct  = (net_pnl_abs / opp["margin"] * 100) if opp["margin"] > 0 else 0
                        all_trades.append({
                            "pair":          pair,
                            "direction":     opp_dir,
                            "leverage":      leverage,
                            "open_date":     str(opp["entry_date"]),
                            "close_date":    str(row["date"]),
                            "entry":         round(float(opp["entry_price"]), 4),
                            "open_rate":     round(float(opp["entry_price"]), 4),
                            "close_rate":    round(float(rev_exit_p), 4),
                            "sl_price":      round(float(opp["sl"]), 4),
                            "tp_price":      round(float(opp["tp"]), 4),
                            "tp2_price":     round(float(opp["tp2"]), 4) if opp.get("tp2") else None,
                            "liq_price":     round(float(opp["liq_price"]), 4),
                            "margin":        round(float(opp["margin"]), 4),
                            "profit_pct":    round(float(profit_pct), 3),
                            "profit_abs":    round(float(net_pnl_abs), 4),
                            "exit_reason":   "reversal",
                            "balance":       round(float(balance), 2),
                            "candles_held":  opp["candles_held"],
                            "funding_paid":  round(float(opp["funding_paid"]), 4),
                            "slippage_paid": round(float(opp["slippage_paid"]), 4),
                            "fees_paid":     round(float(opp["fees_paid"]), 4),
                            "exit_slippage_bps": int(SLIPPAGE_BPS_FLIP),
                            "signal_bar_index": opp["signal_bar_index"],
                            "entry_bar_index":  opp["entry_bar_index"],
                            "exit_bar_index":   i,
                            "sltp_source":      opp["sltp_source"],
                            "partial_exits":    opp["partial_exits"],
                        })
                    else:
                        still_open_after_reversal.append(opp)
                open_positions = still_open_after_reversal

            new_pending: list[tuple] = []
            for pe in pending_entries:
                # NOTE: position 1 is sig_entry (the signal-bar price) — it MUST
                # be unpacked, not discarded: the maker-only entry path below
                # references sig_entry to decide whether the limit filled. It
                # was previously `_`, so enabling maker-only entry crashed with
                # NameError. Position 4 (sig_liq) is unused here → kept as `_`.
                (sig_dir, sig_entry, sig_sl, sig_tp, _, sig_margin, use_signal_sltp,
                 signal_bar_idx, signal_bar_ts, sig_tp2) = pe

                # Free-margin check: don't open if we can't afford the margin.
                free_margin = balance - committed_margin
                if sig_margin > free_margin:
                    if free_margin > 1.0:
                        sig_margin = free_margin   # cap
                    else:
                        skipped_no_margin += 1
                        continue

                # ── Entry price + maker-vs-taker fill model ─────────────
                # Default (taker): market order fills at this bar's open
                # adjusted for entry slippage. Always fills.
                #
                # Maker-only mode: simulate a post-only limit order at the
                # SIGNAL price (the close of the bar where the signal fired).
                # Fill ONLY if this bar's range crosses the signal price:
                #   - LONG: bar_low <= sig_entry → limit was hit by a dip
                #   - SHORT: bar_high >= sig_entry → limit was hit by a pop
                # On a gap-through (bar_open already past the limit) the
                # fill happens at the more favourable bar_open instead.
                # If the range doesn't touch, the order doesn't fill and
                # the signal is dropped (counted under skipped_maker_nofill).
                used_maker_entry = False
                if maker_only_entry:
                    if sig_dir == "long":
                        if bar_o <= sig_entry:
                            entry_price = bar_o          # gap-through favours us
                            used_maker_entry = True
                        elif lo <= sig_entry:
                            entry_price = sig_entry      # limit was hit by dip
                            used_maker_entry = True
                        else:
                            skipped_maker_nofill += 1
                            continue                      # no fill, skip
                    else:  # short
                        if bar_o >= sig_entry:
                            entry_price = bar_o
                            used_maker_entry = True
                        elif hi >= sig_entry:
                            entry_price = sig_entry
                            used_maker_entry = True
                        else:
                            skipped_maker_nofill += 1
                            continue
                else:
                    entry_price = _apply_slippage(bar_o, sig_dir, "entry",
                                                  SLIPPAGE_BPS_ENTRY)

                if use_signal_sltp:
                    sl, tp = sig_sl, sig_tp
                else:
                    sl_dist = abs(entry_price * stoploss_pct / 100)
                    tp_dist = abs(entry_price * take_profit_pct / 100)
                    if sig_dir == "long":
                        sl = entry_price - sl_dist
                        tp = entry_price + tp_dist
                    else:
                        sl = entry_price + sl_dist
                        tp = entry_price - tp_dist

                # ── Phase 4b: timeframe-aware risk engine ──────────────────
                # When enabled, replace the SL/TP decision above with the
                # risk_engine's per-TF ATR plan. This makes backtest output
                # match the live bot engine's behaviour (which always uses
                # risk_engine since Phase 4). Rejected signals are counted
                # so the user can see how many setups risk_engine filtered.
                if use_risk_engine:
                    from backend.services import risk_engine
                    # Build a tiny tail slice for ATR computation. risk_engine
                    # expects an OHLC df at the same TF as the trade. We use
                    # the last ~60 bars including this one — enough for an
                    # ATR(14) with a healthy warmup buffer, cheap to slice.
                    tail_start = max(0, i - 60)
                    df_for_atr = df.iloc[tail_start:i + 1]
                    # NICE-4: pass through the same user overrides the live
                    # engine reads. We don't know which user is running the
                    # backtest in this scope, but the backtester is invoked
                    # by the router which can pass user_overrides if desired.
                    # Default None = use TIMEFRAME_CONFIG defaults.
                    plan = risk_engine.compute_tp_sl(
                        entry          = float(entry_price),
                        direction      = sig_dir,
                        df             = df_for_atr,
                        timeframe      = timeframe,
                        strategy_sl    = float(sig_sl) if (use_signal_sltp and sig_sl is not None) else None,
                        strategy_tp    = float(sig_tp) if (use_signal_sltp and sig_tp is not None) else None,
                        strategy_tp2   = float(sig_tp2) if (sig_tp2 is not None) else None,
                        user_overrides = risk_overrides_for_run,
                    )
                    if not plan.valid:
                        # Track + skip — the signal was generated but the risk
                        # engine rejected it (RR below min, ATR too high, etc.).
                        # Mirrors what the live engine does on every signal.
                        skipped_risk_engine += 1
                        if len(risk_engine_rejections) < 20:
                            risk_engine_rejections.append({
                                "bar_index": i,
                                "direction": sig_dir,
                                "reason":    plan.rejected_reason,
                                "atr":       round(plan.atr, 4),
                                "timeframe": timeframe,
                            })
                        continue
                    sl = plan.sl
                    tp = plan.tp
                    # When risk_engine adopted the strategy's structural
                    # SL/TP, sig_tp2 may also have been validated — leave
                    # the ARM block below to do its own midpoint math.

                # ── ARM transformation: treat the strategy's furthest TP
                # as TP2, compute TP1 as midpoint between entry and TP2.
                # Applies to EVERY strategy when ARM is enabled — single-TP
                # strategies get tp→TP2 promoted; multi-TP strategies get
                # their existing TP1 OVERRIDDEN by the midpoint (user's
                # spec is explicit: "TP1 will be calculated automatically
                # as the midpoint between the entry price and TP2"). This
                # makes the feature universally applicable instead of
                # silently skipping strategies that pre-set sig_tp2.
                arm_active = arm_enabled
                if arm_active:
                    # Pick the strategy's furthest target as TP2:
                    #   • For longs: max(tp, sig_tp2 or tp)
                    #   • For shorts: min(tp, sig_tp2 or tp)
                    # This is robust whether the strategy provided one TP
                    # (tp only, sig_tp2=None) or two (tp + sig_tp2).
                    if sig_tp2 is not None:
                        if sig_dir == "long":
                            final_tp2 = max(tp, sig_tp2)
                        else:
                            final_tp2 = min(tp, sig_tp2)
                    else:
                        final_tp2 = tp
                    sig_tp2 = final_tp2
                    tp      = entry_price + (sig_tp2 - entry_price) * 0.5   # TP1 = midpoint

                pos_value = sig_margin * leverage
                liq_price = _calc_liquidation(entry_price, sig_dir, leverage, pos_value)

                # ── KuCoin entry fee — maker rate when in maker-only mode,
                # taker otherwise. Uses the per-run rate from the VIP tier.
                # Tracked per-trade always; deducted from balance only when
                # the user enabled "realistic costs".
                entry_fee_rate = maker_fee_rate if used_maker_entry else taker_fee_rate
                entry_fee_real = pos_value * entry_fee_rate
                if deduct_real_costs:
                    entry_fee = entry_fee_real
                    balance  -= entry_fee
                else:
                    entry_fee = 0.0
                committed_margin += sig_margin

                open_positions.append({
                    "direction":    sig_dir,
                    "entry_price":  entry_price,
                    "sl":           sl,
                    "tp":           tp,
                    "liq_price":    liq_price,
                    "margin":       sig_margin,
                    "entry_date":   row["date"],
                    "entry_bar_ts": bar_ts_secs,
                    "candles_held": 0,
                    "funding_paid":     0.0,
                    "slippage_paid":    abs(bar_o - entry_price) * (pos_value / max(bar_o, 1e-9)),
                    "fees_paid":        entry_fee,   # accumulates exit fee at close
                    # Signal-trace fields — let the user prove the chain
                    # signal-bar → fill-bar → exit-bar without ambiguity.
                    "signal_bar_index": signal_bar_idx,
                    "signal_bar_ts":    signal_bar_ts,
                    "entry_bar_index":  i,
                    "sltp_source":      "strategy" if use_signal_sltp else "slider",
                    # Multi-TP state. tp2 is None for single-TP strategies
                    # (everything currently in use except the new SMCStrategyTV
                    # institutional model). When set:
                    #   • tp1 fill closes tp1_close_pct of margin, moves SL
                    #     to entry (breakeven trail)
                    #   • tp2 fill closes the remainder
                    # remaining_pct is what fraction of the original margin
                    # is still in the trade; starts at 1.0.
                    "tp2":              sig_tp2,
                    # ARM uses configurable TP1 close %. Legacy multi-TP (strategy
                    # provided its own sig_tp2 without ARM) still defaults to 50%.
                    # Single TP (no sig_tp2 at all) closes 100% at TP.
                    "tp1_close_pct":    (
                        max(0.01, min(0.99, arm_tp1_close_pct / 100.0))
                        if arm_active
                        else (0.5 if sig_tp2 is not None else 1.0)
                    ),
                    "remaining_pct":    1.0,
                    "tp1_hit":          False,
                    # ARM state tracking — let the position-management loop know
                    # whether to apply ARM-specific BE price + trail-to-TP1 logic.
                    "arm_active":       arm_active,
                    "trailed_to_tp1":   False,
                    "partial_pnl":      0.0,
                    "partial_fees":     0.0,
                    "partial_slippage": 0.0,
                    "partial_exits":    [],   # for trade record / signal trace
                })
            pending_entries = new_pending

            # ── B. Manage every open position (independent SL/TP/liq) ─────
            still_open: list[dict] = []
            for pos in open_positions:
                pos["candles_held"] += 1
                direction    = pos["direction"]
                entry_price  = pos["entry_price"]
                sl, tp       = pos["sl"], pos["tp"]
                liq_price    = pos["liq_price"]
                margin       = pos["margin"]
                entry_date   = pos["entry_date"]

                # ── ARM trail-to-TP1 ─────────────────────────────────────
                # After TP1 has been hit and the partial close booked, watch
                # for price to progress HALFWAY from TP1 to TP2. When it
                # does, ratchet SL up from BE to TP1 — locks in the TP1
                # profit on the remainder before TP2 hits (or in case price
                # reverses). One-shot per position (trailed_to_tp1 flag).
                if (pos.get("arm_active") and pos["tp1_hit"]
                        and arm_trail_to_tp1
                        and not pos.get("trailed_to_tp1", False)
                        and pos.get("tp2") is not None):
                    tp1_price = pos["tp"]
                    tp2_price = pos["tp2"]
                    midpoint  = tp1_price + (tp2_price - tp1_price) * 0.5
                    if direction == "long" and hi >= midpoint:
                        pos["sl"] = tp1_price
                        sl = tp1_price
                        pos["trailed_to_tp1"] = True
                    elif direction == "short" and lo <= midpoint:
                        pos["sl"] = tp1_price
                        sl = tp1_price
                        pos["trailed_to_tp1"] = True

                # Funding settlements that fall inside this bar's time window.
                # Always computed (so the diagnostic field is populated even
                # in pure-strategy mode), but only subtracted from leg P&L
                # when the user enabled "realistic costs".
                funding_cost = 0.0
                window_lo = (max(bar_start_ts, pos["entry_bar_ts"])
                             if pos["candles_held"] == 1 else bar_start_ts)
                settlements = _funding_settlements_in_window(window_lo, bar_ts_secs)
                if settlements:
                    pos_value = margin * leverage
                    for settle_ts in settlements:
                        applied_rate = _funding_rate_for_ts(funding_sorted, settle_ts)
                        signed_rate  = applied_rate if direction == "long" else -applied_rate
                        funding_cost += pos_value * signed_rate
                    pos["funding_paid"] += funding_cost
                # In pure-strategy mode, zero-out funding_cost so it doesn't
                # affect P&L (it's still tracked for transparency).
                if not deduct_real_costs:
                    funding_cost_for_pnl = 0.0
                else:
                    funding_cost_for_pnl = funding_cost

                # ── Exit detection with optional multi-TP partial close ──
                # When pos["tp2"] is None: single-TP path — TP closes 100%.
                # When pos["tp2"] is set:
                #   1. TP1 hit (first time) → close tp1_close_pct of margin,
                #      move SL to entry (breakeven trail), keep position open.
                #   2. SL hit → close remaining_pct.
                #   3. TP2 hit → close remaining_pct.
                # Liquidation always closes everything.
                #
                # We process at most ONE partial event per bar — even if both
                # TP1 and TP2 hit in the same candle, TP1 fires first and
                # the remainder rolls to the next bar. That's pessimistic but
                # honest: without tick data we can't know the intra-bar order.
                liquidated   = False
                exit_slippage_bps = 0
                raw_exit_p   = None
                exit_rsn     = ""
                partial_event = False   # True when we close part but not all

                tp = pos["tp"]
                tp2 = pos.get("tp2")
                has_tp2 = tp2 is not None and not pos["tp1_hit"]

                # ── Explicit exit signal (opt-in, close-to-flat) ─────────
                # When the strategy declares use_exit_signals=True and its
                # exit_long/exit_short column fires on THIS bar, close the
                # position at the bar CLOSE — symmetric with entries (which
                # fill at the signal bar's close) so there's no look-ahead,
                # and matching the live engine's _use_exit_signals close.
                # Highest precedence: an explicit exit is the strategy's
                # direct instruction.
                exit_signal_fire = use_exit_signals and (
                    (direction == "long"  and exit_long_arr  is not None
                     and i < len(exit_long_arr)  and exit_long_arr[i]) or
                    (direction == "short" and exit_short_arr is not None
                     and i < len(exit_short_arr) and exit_short_arr[i])
                )

                # ── max_hold force-exit (strategy-declared) ──────────────
                # When a strategy declares class_max_hold_candles, any
                # position open longer than that gets closed at THIS bar's
                # open. Matches the live engine's behaviour so backtest
                # results agree with what the bot actually does in paper /
                # live mode. Checked BEFORE liq/SL/TP so a max-hold exit
                # takes precedence on an ambiguous bar (the institutional
                # thesis has expired regardless of where price ends up).
                if exit_signal_fire:
                    raw_exit_p = row["close"]
                    exit_rsn = "exit_signal"
                    exit_slippage_bps = SLIPPAGE_BPS_STOP
                elif (pair_max_hold_candles > 0
                        and pos["candles_held"] >= pair_max_hold_candles):
                    raw_exit_p = bar_o
                    exit_rsn = "max_hold_expired"
                    exit_slippage_bps = SLIPPAGE_BPS_STOP
                elif direction == "long" and lo <= liq_price:
                    raw_exit_p = liq_price
                    liquidated = True
                    exit_slippage_bps = SLIPPAGE_BPS_LIQ
                elif direction == "short" and hi >= liq_price:
                    raw_exit_p = liq_price
                    liquidated = True
                    exit_slippage_bps = SLIPPAGE_BPS_LIQ
                else:
                    # TP1 is only checkable BEFORE the first partial fires.
                    # Once tp1_hit=True, the runner targets TP2 (or SL@BE).
                    # This prevents TP1 from re-triggering on the remainder
                    # and producing fake "take_profit" labels when the
                    # multi-TP intent was never fulfilled.
                    if direction == "long":
                        sl_hit  = lo <= sl
                        tp1_hit = (not pos["tp1_hit"]) and (hi >= tp)
                        tp2_hit = (pos["tp1_hit"] and tp2 is not None and hi >= tp2)
                    else:
                        sl_hit  = hi >= sl
                        tp1_hit = (not pos["tp1_hit"]) and (lo <= tp)
                        tp2_hit = (pos["tp1_hit"] and tp2 is not None and lo <= tp2)

                    if has_tp2 and sl_hit and tp1_hit:
                        # Same-bar SL + TP1 (multi-TP path).
                        precision_ambiguous_seen += 1
                        first, method = _resolve_ambiguous_first_hit(
                            tp, sl, direction, bar_o, row["close"], bar_ts_secs,
                            tick_precision, sub_bars_by_main_ts,
                        )
                        if   method == 'sub_bar':    precision_resolved_sub_bar    += 1
                        elif method == 'path_infer': precision_resolved_path_infer += 1
                        else:                        precision_resolved_heuristic  += 1
                        if first == 'tp':
                            raw_exit_p = tp; exit_rsn = "take_profit_1"
                            exit_slippage_bps = SLIPPAGE_BPS_TP
                            partial_event = True
                        else:
                            raw_exit_p = sl; exit_rsn = "stop_loss"
                            exit_slippage_bps = SLIPPAGE_BPS_STOP
                    elif has_tp2 and tp1_hit:
                        raw_exit_p = tp; exit_rsn = "take_profit_1"
                        exit_slippage_bps = SLIPPAGE_BPS_TP
                        partial_event = True
                    elif sl_hit and tp1_hit:
                        # Same-bar SL + TP (single-TP path).
                        precision_ambiguous_seen += 1
                        first, method = _resolve_ambiguous_first_hit(
                            tp, sl, direction, bar_o, row["close"], bar_ts_secs,
                            tick_precision, sub_bars_by_main_ts,
                        )
                        if   method == 'sub_bar':    precision_resolved_sub_bar    += 1
                        elif method == 'path_infer': precision_resolved_path_infer += 1
                        else:                        precision_resolved_heuristic  += 1
                        if first == 'tp':
                            raw_exit_p = tp; exit_rsn = "take_profit"
                            exit_slippage_bps = SLIPPAGE_BPS_TP
                        else:
                            raw_exit_p = sl; exit_rsn = "stop_loss"
                            exit_slippage_bps = SLIPPAGE_BPS_STOP
                    elif tp2_hit and sl_hit:
                        # Same-bar SL@BE + TP2 (after TP1 hit, multi-TP path).
                        precision_ambiguous_seen += 1
                        first, method = _resolve_ambiguous_first_hit(
                            tp2, sl, direction, bar_o, row["close"], bar_ts_secs,
                            tick_precision, sub_bars_by_main_ts,
                        )
                        if   method == 'sub_bar':    precision_resolved_sub_bar    += 1
                        elif method == 'path_infer': precision_resolved_path_infer += 1
                        else:                        precision_resolved_heuristic  += 1
                        if first == 'tp':
                            raw_exit_p = tp2; exit_rsn = "take_profit_2"
                            exit_slippage_bps = SLIPPAGE_BPS_TP
                        else:
                            raw_exit_p = sl; exit_rsn = "stop_loss"
                            exit_slippage_bps = SLIPPAGE_BPS_STOP
                    elif tp2_hit:
                        raw_exit_p = tp2; exit_rsn = "take_profit_2"
                        exit_slippage_bps = SLIPPAGE_BPS_TP
                    elif sl_hit:
                        raw_exit_p = sl; exit_rsn = "stop_loss"
                        exit_slippage_bps = SLIPPAGE_BPS_STOP
                    elif tp1_hit:
                        raw_exit_p = tp; exit_rsn = "take_profit"
                        exit_slippage_bps = SLIPPAGE_BPS_TP

                # Still running this bar → carry forward. Funding only hits
                # balance in realistic-costs mode.
                if raw_exit_p is None:
                    balance -= funding_cost_for_pnl
                    still_open.append(pos)
                    continue

                # ── Compute the leg P&L (full or partial) ────────────────
                exit_p = _apply_slippage(raw_exit_p, direction, "exit",
                                         exit_slippage_bps)
                # close_pct = fraction of the ORIGINAL margin being closed
                # on this event. For full exits and TP2/SL after partial,
                # this is whatever's left (remaining_pct).
                if partial_event:
                    close_pct = pos["tp1_close_pct"]
                else:
                    close_pct = pos["remaining_pct"]
                leg_margin = margin * close_pct
                leg_pos_value = leg_margin * leverage

                if liquidated:
                    leg_pnl = -leg_margin
                else:
                    if direction == "long":
                        price_move_pct = (exit_p - entry_price) / entry_price
                    else:
                        price_move_pct = (entry_price - exit_p) / entry_price
                    leveraged_pnl_pct = price_move_pct * leverage
                    # Funding only subtracts from P&L when realistic-costs
                    # mode is on (funding_cost_for_pnl is zeroed otherwise).
                    leg_pnl = leg_margin * leveraged_pnl_pct - funding_cost_for_pnl
                    leg_pnl = max(leg_pnl, -leg_margin)

                units = leg_pos_value / max(entry_price, 1e-9)
                leg_slippage = abs(raw_exit_p - exit_p) * units
                pos["slippage_paid"] += leg_slippage

                # KuCoin fees on this leg's notional, using the VIP-tier
                # rates. TP exits use MAKER (limit fills); SL / liquidation
                # use TAKER (forced market). Tracked always, deducted from
                # balance only in realistic-costs mode.
                if exit_rsn in ("take_profit", "take_profit_1", "take_profit_2"):
                    leg_fee_real = leg_pos_value * maker_fee_rate
                else:
                    leg_fee_real = leg_pos_value * taker_fee_rate
                if deduct_real_costs:
                    leg_fee = leg_fee_real
                    pos["fees_paid"] += leg_fee
                    balance -= leg_fee
                else:
                    leg_fee = 0.0

                # Record this leg.
                pos["partial_pnl"] += leg_pnl
                pos["partial_exits"].append({
                    "bar_index":  i,
                    "reason":     exit_rsn,
                    "price":      round(float(exit_p), 4),
                    "close_pct":  round(float(close_pct), 4),
                    "pnl":        round(float(leg_pnl), 4),
                })

                # Release this leg's margin, apply P&L to balance.
                committed_margin -= leg_margin
                balance += leg_pnl
                balance = max(balance, 0)

                if partial_event:
                    # Position still has remaining_pct left. Move SL to BE
                    # (ARM-configurable buffer; default = entry when ARM off).
                    pos["tp1_hit"] = True
                    pos["remaining_pct"] -= close_pct
                    if pos.get("arm_active"):
                        be_price = _compute_be_price(
                            entry_price, direction, leverage,
                            arm_be_mode, arm_be_buffer_pct,
                        )
                        # ── Safety clamp: BE must never be on the WRONG
                        # side of TP1. On tight setups (especially 1m
                        # scalps where TP1 is 0.05-0.5% from entry) at
                        # high leverage, the leverage-auto BE buffer
                        # (lev/1000 = 1% @ 10x, 2% @ 20x) often lands
                        # FURTHER from entry than TP1 itself.
                        # For LONG: BE > TP1 would mean SL is set ABOVE
                        # current price → instant stop-out of remainder.
                        # For SHORT: BE < TP1 has the symmetric problem.
                        # Clamp BE to TP1 (the just-touched profit level)
                        # so the remainder can never exit at a loss
                        # immediately after a TP1 hit.
                        tp1_price = pos["tp"]   # TP1 = the level just touched
                        if direction == "long" and be_price > tp1_price:
                            be_price = tp1_price
                        elif direction == "short" and be_price < tp1_price:
                            be_price = tp1_price
                    else:
                        be_price = entry_price
                    pos["sl"] = be_price
                    sl = be_price
                    still_open.append(pos)
                    continue

                # Position fully closed — assemble the aggregate trade record.
                #
                # P&L bookkeeping (single source of truth):
                #   • leg_pnl is gross of leg-level fees, computed per-leg
                #   • pos["partial_pnl"] = SUM of leg_pnl values
                #   • pos["fees_paid"]    = entry_fee + SUM of leg exit fees
                #     (each pushed when the leg fires)
                #   • Net P&L the user sees = partial_pnl - fees_paid
                #     (funding is already inside partial_pnl via leg_pnl)
                net_pnl_abs = pos["partial_pnl"] - pos["fees_paid"]
                profit_pct  = (net_pnl_abs / margin * 100) if margin > 0 else 0

                # Weighted-average exit price across all legs — single
                # representative "close_rate" for the trade row, even when
                # there were 2-3 fills.
                total_close_pct = sum(leg["close_pct"] for leg in pos["partial_exits"])
                if total_close_pct > 0:
                    wavg_exit = sum(
                        leg["price"] * leg["close_pct"]
                        for leg in pos["partial_exits"]
                    ) / total_close_pct
                else:
                    wavg_exit = exit_p

                # Headline exit reason for the row.
                if len(pos["partial_exits"]) > 1:
                    reasons = [leg["reason"] for leg in pos["partial_exits"]]
                    if "take_profit_2" in reasons:
                        final_reason = "multi_tp_completed"
                    elif "stop_loss" in reasons:
                        final_reason = "tp1_then_stop"
                    else:
                        final_reason = reasons[-1]
                else:
                    final_reason = "liquidated" if liquidated else exit_rsn
                    if final_reason == "take_profit_1":
                        # Single-leg take-profit on a multi-TP-enabled trade
                        # would imply TP1 closed everything (close_pct=1.0),
                        # which only happens for single-TP setups. Normalise.
                        final_reason = "take_profit"
                all_trades.append({
                    "pair":        pair,
                    "direction":   direction,
                    "leverage":    leverage,
                    "open_date":   str(entry_date),
                    "close_date":  str(row["date"]),
                    "entry":       round(float(entry_price), 4),
                    "open_rate":   round(float(entry_price), 4),
                    "close_rate":  round(float(wavg_exit), 4),
                    "sl_price":    round(float(sl), 4),
                    "tp_price":    round(float(tp), 4),
                    "tp2_price":   round(float(pos["tp2"]), 4) if pos.get("tp2") else None,
                    "liq_price":   round(float(liq_price), 4),
                    "margin":      round(float(margin), 4),
                    "profit_pct":  round(float(profit_pct), 3),
                    "profit_abs":  round(float(net_pnl_abs), 4),     # NET of all costs
                    "exit_reason": final_reason,
                    "balance":     round(float(balance), 2),
                    "candles_held": pos["candles_held"],
                    "funding_paid":      round(float(pos["funding_paid"]),    4),
                    "slippage_paid":     round(float(pos["slippage_paid"]),   4),
                    "fees_paid":         round(float(pos["fees_paid"]),       4),  # real KuCoin
                                                                                    # fees DEDUCTED
                    "exit_slippage_bps": int(exit_slippage_bps),
                    # Signal-trace: prove this trade came from a real signal.
                    "signal_bar_index": pos["signal_bar_index"],
                    "entry_bar_index":  pos["entry_bar_index"],
                    "exit_bar_index":   i,
                    "sltp_source":      pos["sltp_source"],
                    # Multi-TP partial-close trace. Single-TP trades have
                    # exactly one entry in this list with close_pct=1.0.
                    # Multi-TP trades typically have 2 entries (TP1 then
                    # SL-at-BE or TP2). Lets the user see the actual fill
                    # sequence behind a multi_tp_completed trade.
                    "partial_exits":    pos["partial_exits"],
                })
            open_positions = still_open

            # ── C. Check for new entry signal ─────────────────────────────
            # Buffer bars (past the user's end_ts) only manage existing
            # positions — no new entries — so the trade list reflects only
            # signals fired within the requested window.
            in_window = first_in_window_idx <= i <= last_in_window_idx
            sig = signal_fn(df, i) if (i >= 3 and in_window) else None
            if sig is not None and ml_take is not None and i < len(ml_take) and not ml_take[i]:
                sig = None          # ML loss-filter vetoed this signal
                ml_skipped += 1
            if sig is not None:
                # Accept either 4-tuple (entry, sl, tp, dir) or 5-tuple
                # (entry, sl, tp1, tp2, dir). Multi-TP unlocks partial-close
                # behaviour in the engine: TP1 closes 50% and moves SL to
                # entry (breakeven trail), TP2 closes the remainder.
                if len(sig) == 5:
                    sig_entry, sl_raw, tp_raw, tp2_raw, sig_dir = sig
                else:
                    sig_entry, sl_raw, tp_raw, sig_dir = sig
                    tp2_raw = None
                if balance <= 0:
                    # Wiped out — no more trades possible.
                    continue

                # Pyramiding cap — matches TradingView's `pyramiding` setting.
                # Default 1 = single position at a time (most common). When a
                # strategy fires the same condition for several bars in a row,
                # the unlimited-concurrent mode would open 4 separate trades
                # with near-identical outcomes; the user reads those as "4
                # trades" when conceptually it's the same setup. The cap
                # collapses those into ONE trade — matches how you'd actually
                # trade by hand or via Pine Script default.
                #
                # We count BOTH already-open positions AND pending entries
                # that haven't filled yet, so a cluster of signals at the
                # same bar can't all sneak through together.
                open_in_dir = sum(1 for p in open_positions if p["direction"] == sig_dir)
                pending_in_dir = sum(1 for pe in pending_entries if pe[0] == sig_dir)
                total_in_dir = open_in_dir + pending_in_dir
                if total_in_dir >= max_concurrent_positions:
                    # The signal fired, but we already hold the max number of
                    # positions in this direction. Tracked under "in-trade".
                    skipped_in_trade += 1
                    continue

                free_margin = balance - committed_margin
                sig_margin = balance * risk_per_trade
                # Cap by free margin; skip if can't even commit $1.
                if sig_margin > free_margin:
                    if free_margin > 1.0:
                        sig_margin = free_margin
                    else:
                        skipped_no_margin += 1
                        continue

                if sig_dir == "long":
                    trades_opened_long += 1
                else:
                    trades_opened_short += 1

                # Every built-in signal function returns its OWN (entry, sl,
                # tp, dir). Those values are the strategy's design intent —
                # MissCandle ports use prev-candle high/low, SMC uses swing
                # pivots + 2R, MACD/RSI use fixed-% targets matched to the
                # signal's expected hold time. The previous behaviour (only
                # SMCStrategyTV was whitelisted) silently discarded the
                # SL/TP from every other strategy and forced slider values,
                # which is why a real-SMC backtest showed every trade
                # exiting at exactly slider_sl × leverage (e.g. -15.49%) —
                # totally inconsistent with what an SMC strategy actually does.
                #
                # New default: honour whatever the signal function returned,
                # IF the values look plausible (positive risk, capped at 25%
                # so a buggy signal can't blow up the run). The slider only
                # wins when:
                #   • the strategy is user-authored (no signal function — we
                #     don't reach this code path for those; signal_fn comes
                #     from make_signal_fn_from_df which already builds SL/TP
                #     from sliders), or
                #   • force_slider_sltp=True (auto-tune grid sweep).
                sig_tp2: float | None = None
                if force_slider_sltp:
                    sl_dist = sig_entry * stoploss_pct / 100
                    tp_dist = sig_entry * take_profit_pct / 100
                    if sig_dir == "long":
                        sig_sl = sig_entry - sl_dist
                        sig_tp = sig_entry + tp_dist
                    else:
                        sig_sl = sig_entry + sl_dist
                        sig_tp = sig_entry - tp_dist
                    use_signal_sltp = False
                    # Force-slider mode IGNORES multi-TP — user explicitly
                    # asked for fixed % SL/TP, so we use slider values only.
                    sig_tp2 = None
                else:
                    # Sanity-cap the strategy's SL distance: anything wider
                    # than 25% of entry price is almost certainly a bug
                    # (would imply 250% leveraged loss at 10x — instant
                    # liquidation) and we fall back to slider values rather
                    # than open the trade with garbage levels.
                    risk_dist = abs(sig_entry - sl_raw)
                    if risk_dist > 0 and risk_dist <= sig_entry * 0.25:
                        sig_sl, sig_tp = sl_raw, tp_raw
                        # "Stop beyond structure" — push the structural stop a
                        # little FURTHER from entry so it sits past the swing
                        # (not on the obvious level that gets hunted). Off (0)
                        # by default → identical to before.
                        if sl_structure_buffer_pct > 0:
                            _buf = sig_entry * sl_structure_buffer_pct / 100.0
                            sig_sl = (sig_sl - _buf) if sig_dir == "long" else (sig_sl + _buf)
                        use_signal_sltp = True
                        # TP2 only honoured when (a) strategy returned it
                        # AND (b) it's further from entry than TP1 in the
                        # trade direction. We validated direction in
                        # make_signal_fn_from_df; defensive re-check here.
                        if tp2_raw is not None:
                            tp2_f = float(tp2_raw)
                            if sig_dir == "long" and tp2_f > sig_tp:
                                sig_tp2 = tp2_f
                            elif sig_dir == "short" and tp2_f < sig_tp:
                                sig_tp2 = tp2_f
                    else:
                        sl_dist = sig_entry * stoploss_pct / 100
                        tp_dist = sig_entry * take_profit_pct / 100
                        if sig_dir == "long":
                            sig_sl = sig_entry - sl_dist
                            sig_tp = sig_entry + tp_dist
                        else:
                            sig_sl = sig_entry + sl_dist
                            sig_tp = sig_entry - tp_dist
                        use_signal_sltp = False
                        sig_tp2 = None

                if use_signal_sltp:
                    sltp_from_signal += 1
                else:
                    sltp_from_slider += 1

                sig_liq = _calc_liquidation(sig_entry, sig_dir, leverage,
                                            sig_margin * leverage)
                # Carry the bar index where the signal fired AND the bar
                # timestamp through to the open position so the trade
                # record can prove the chain: "signal fired at bar X
                # (time T1) → entry filled at bar X+1 (time T2) → exit
                # at bar Y (time T3)". The user can match these against
                # the strategy's signal function to verify nothing fired
                # without a real condition.
                signal_bar_idx = i
                signal_bar_ts  = bar_ts_secs
                pending_entries.append(
                    (sig_dir, sig_entry, sig_sl, sig_tp, sig_liq,
                     sig_margin, use_signal_sltp, signal_bar_idx, signal_bar_ts,
                     sig_tp2)
                )

        # ── End of bar loop: handle leftover open positions ───────────────
        # Trades still open when the data window ends NEVER hit their
        # strategy-defined exit (SL/TP/liquidation), so their P&L is
        # unrealised — including them in the trade table would mix real
        # strategy outcomes with arbitrary mark-to-market snapshots and
        # distort win-rate / drawdown / avg P&L.
        # Correct behaviour: release the committed margin so balance is
        # honest, record the count for the diagnostics panel, but DON'T
        # add them to the trade list or affect realised P&L.
        trades_still_open_at_end = len(open_positions)
        unrealised_pnl_at_end    = 0.0
        if open_positions:
            last_row   = df.iloc[-1]
            last_close = float(last_row["close"])
            for pos in open_positions:
                direction   = pos["direction"]
                entry_price = pos["entry_price"]
                margin      = pos["margin"]
                # For partially-closed multi-TP trades, the remaining
                # margin is `margin × remaining_pct`. Mark-to-market
                # only on that remainder; the closed portion was already
                # released to balance when each TP fired.
                remaining_margin = margin * pos.get("remaining_pct", 1.0)
                if direction == "long":
                    move_pct = (last_close - entry_price) / entry_price
                else:
                    move_pct = (entry_price - last_close) / entry_price
                unrealised_pnl_at_end += max(
                    remaining_margin * move_pct * leverage, -remaining_margin
                )
                committed_margin -= remaining_margin
            open_positions = []

        # End of per-pair bar loop — write per-pair signal-disposition counts
        # so the UI can show the breakdown of "signal bars → clusters → trades
        # opened → skipped because in-trade / cooldown". Without this, the
        # raw "26 long / 28 short" figure looks like a bug.
        data_diagnostics[pair]["trades_opened_long"]   = trades_opened_long
        data_diagnostics[pair]["trades_opened_short"]  = trades_opened_short
        data_diagnostics[pair]["signals_skipped_in_trade"] = skipped_in_trade
        data_diagnostics[pair]["signals_skipped_ml_filter"] = ml_skipped
        data_diagnostics[pair]["signals_skipped_cooldown"] = skipped_cooldown
        data_diagnostics[pair]["cooldown_bars"]        = cooldown_bars
        # Pyramiding / position-model diagnostics
        data_diagnostics[pair]["signals_skipped_no_margin"] = skipped_no_margin
        # Maker-only mode: how many signals were dropped because the maker
        # limit was never reached by the next bar's range. Surfaced so the
        # user can see the realistic non-fill rate of their setup.
        data_diagnostics[pair]["signals_skipped_maker_nofill"] = skipped_maker_nofill
        data_diagnostics[pair]["maker_only_entry"]             = bool(maker_only_entry)
        # Phase 4b — risk_engine rejections. Surfaces to the UI so users
        # can see how many setups risk_engine filtered (RR below per-TF
        # min, ATR cap exceeded, etc.). Includes the first 20 rejections
        # with reason text for transparency / debugging.
        data_diagnostics[pair]["use_risk_engine"]              = bool(use_risk_engine)
        data_diagnostics[pair]["signals_skipped_risk_engine"]  = skipped_risk_engine
        data_diagnostics[pair]["risk_engine_rejections"]       = risk_engine_rejections
        data_diagnostics[pair]["vip_tier"]                     = int(max(0, min(12, vip_tier)))
        data_diagnostics[pair]["fee_rates_pct"] = {
            "maker": round(maker_fee_rate * 100, 5),
            "taker": round(taker_fee_rate * 100, 5),
        }
        data_diagnostics[pair]["max_concurrent_positions"]  = max_concurrent_positions
        data_diagnostics[pair]["position_mode"]              = position_mode
        data_diagnostics[pair]["position_model"] = (
            "hedge (long + short independent)" if position_mode == "hedge"
            else "single" if max_concurrent_positions == 1
            else f"pyramiding_{max_concurrent_positions}"
        )
        data_diagnostics[pair]["trades_still_open_at_end"]  = trades_still_open_at_end
        data_diagnostics[pair]["unrealised_pnl_at_end"]     = round(unrealised_pnl_at_end, 4)
        # How the engine decided SL/TP per trade. When sltp_from_signal
        # dominates, the slider is functionally inert for this strategy —
        # the UI uses this to explain "your slider is for reference; this
        # strategy defines its own SL/TP per signal".
        data_diagnostics[pair]["sltp_from_signal"]          = sltp_from_signal
        data_diagnostics[pair]["sltp_from_slider"]          = sltp_from_slider
        # Tick-precision diagnostics — how many same-bar SL+TP ambiguities
        # were encountered and which method resolved each. When precision
        # is OFF, all show under "resolved_heuristic" (legacy behaviour).
        # When ON: ideally most resolve via sub-bar (highest accuracy),
        # remainder via path inference. Heuristic count should be 0 with
        # precision on.
        data_diagnostics[pair]["precision_enabled"]         = bool(tick_precision)
        data_diagnostics[pair]["precision_ambiguous_seen"]  = precision_ambiguous_seen
        data_diagnostics[pair]["precision_resolved_sub_bar"]    = precision_resolved_sub_bar
        data_diagnostics[pair]["precision_resolved_path_infer"] = precision_resolved_path_infer
        data_diagnostics[pair]["precision_resolved_heuristic"]  = precision_resolved_heuristic

        # Effective SL / TP range across actual trades for this pair.
        # When the strategy returns structural levels, the slider value
        # tells the user nothing — they need to know "actually, your trades
        # ran SL between X% and Y% with avg Z%". This is what closes the
        # gap between "I set 1.5% SL" and "trade exited at -9.89%".
        pair_trades = [t for t in all_trades if t.get("pair") == pair]
        sl_dists = []
        tp_dists = []
        for t in pair_trades:
            op = float(t.get("open_rate") or 0)
            if op <= 0:
                continue
            sl_p = float(t.get("sl_price") or 0)
            tp_p = float(t.get("tp_price") or 0)
            if sl_p > 0:
                sl_dists.append(abs(sl_p - op) / op * 100)
            if tp_p > 0:
                tp_dists.append(abs(tp_p - op) / op * 100)
        if sl_dists:
            data_diagnostics[pair]["effective_sl_pct_avg"] = round(sum(sl_dists)/len(sl_dists), 3)
            data_diagnostics[pair]["effective_sl_pct_min"] = round(min(sl_dists), 3)
            data_diagnostics[pair]["effective_sl_pct_max"] = round(max(sl_dists), 3)
        if tp_dists:
            data_diagnostics[pair]["effective_tp_pct_avg"] = round(sum(tp_dists)/len(tp_dists), 3)
            data_diagnostics[pair]["effective_tp_pct_min"] = round(min(tp_dists), 3)
            data_diagnostics[pair]["effective_tp_pct_max"] = round(max(tp_dists), 3)

    # ── Compute aggregate metrics ─────────────────────────────────────────
    if not all_trades:
        return {
            "metrics": {
                "total_trades":    0,
                "winning_trades":  0,
                "losing_trades":   0,
                "liquidations":    0,
                "long_trades":     0,
                "short_trades":    0,
                "win_rate":        0.0,
                "total_profit_pct": 0.0,
                "total_profit_abs": 0.0,
                "final_balance":   starting_balance,
                "max_drawdown":    0.0,
                "avg_leverage_pnl": 0.0,
                "leverage":        leverage,
            },
            "trades":  [],
            "equity_curve": [{"date": "", "balance": starting_balance}],
            "data_quality": data_diagnostics,
            "user_strategy_error": user_strategy_error,
        }

    wins         = sum(1 for t in all_trades if t["profit_abs"] > 0)
    losses       = len(all_trades) - wins
    liquidations = sum(1 for t in all_trades if t["exit_reason"] == "liquidated")
    longs        = sum(1 for t in all_trades if t["direction"] == "long")
    shorts       = sum(1 for t in all_trades if t["direction"] == "short")
    win_rate     = wins / len(all_trades) if all_trades else 0

    total_profit_abs = balance - starting_balance
    total_profit_pct = (total_profit_abs / starting_balance * 100) if starting_balance else 0

    # Max drawdown from equity curve
    balances = [starting_balance] + [t["balance"] for t in all_trades]
    peak = starting_balance
    max_dd = 0.0
    for b in balances:
        if b > peak:
            peak = b
        dd = (peak - b) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    avg_pnl = sum(t["profit_pct"] for t in all_trades) / len(all_trades)

    equity_curve = [{"date": t["close_date"], "balance": t["balance"]} for t in all_trades]

    # Aggregate cost transparency rows. ALL THREE are deducted from the
    # simulated balance (real-cost modelling — backtests are only useful
    # if they include what production would actually charge).
    total_funding   = sum(t.get("funding_paid",   0) for t in all_trades)
    total_slippage  = sum(t.get("slippage_paid",  0) for t in all_trades)
    total_fees      = sum(t.get("fees_paid",      0) for t in all_trades)

    # ── Math-check rails: break-even WR + expected-value per trade ────────
    # Breakeven WR = SL / (SL + TP). If actual WR is below this, the
    # strategy is MATHEMATICALLY guaranteed to lose money (before fees)
    # — no amount of code tweaking fixes that, it's arithmetic.
    #
    # For strategies whose engine uses *structural* SL/TP per trade
    # (SMCStrategyTV — pivot-based stops + 2R targets), the slider values
    # are NOT what trades actually used, so deriving breakeven from sliders
    # is misleading. We compute the realised average SL% / TP% from the
    # ACTUAL trade outcomes and use those for the verdict — that's the
    # arithmetic the user is really running.
    realised_sl_pct: float | None = None
    realised_tp_pct: float | None = None
    sl_trades = [t for t in all_trades if t.get("exit_reason") == "stop_loss"]
    tp_trades = [t for t in all_trades if t.get("exit_reason") == "take_profit"]
    if sl_trades:
        realised_sl_pct = abs(sum(
            abs(t["close_rate"] - t["open_rate"]) / t["open_rate"] * 100
            for t in sl_trades
        ) / len(sl_trades))
    if tp_trades:
        realised_tp_pct = abs(sum(
            abs(t["close_rate"] - t["open_rate"]) / t["open_rate"] * 100
            for t in tp_trades
        ) / len(tp_trades))

    # Use realised values when both are present (strategy-defined SL/TP path),
    # otherwise fall back to the slider values (which match what the engine used).
    using_realised = realised_sl_pct is not None and realised_tp_pct is not None
    sl_pct = realised_sl_pct if using_realised else abs(stoploss_pct)
    tp_pct = realised_tp_pct if using_realised else abs(take_profit_pct)
    if sl_pct + tp_pct > 0:
        breakeven_wr = sl_pct / (sl_pct + tp_pct)
        rr_ratio     = tp_pct / sl_pct if sl_pct > 0 else 0
    else:
        breakeven_wr = 0
        rr_ratio     = 0
    # Expected-value per trade in % of margin (positive = profitable
    # expectation; negative = guaranteed loss given infinite trades).
    ev_per_trade_pct = (win_rate * tp_pct - (1 - win_rate) * sl_pct) * leverage
    is_negative_ev   = win_rate < breakeven_wr

    # Cost-drag per trade — the headline number for "why is my profitable
    # strategy losing money?". Includes ALL real costs now (funding +
    # slippage + fees), so this is the actual production-grade drag.
    cost_drag_usdt   = total_funding + total_slippage + total_fees
    cost_drag_per_trade_usdt = (cost_drag_usdt / len(all_trades)) if all_trades else 0
    # Estimate avg margin used per trade (declines as balance changes).
    avg_margin = (sum(t.get("margin", 0) for t in all_trades) / len(all_trades)) if all_trades else 0
    cost_drag_per_trade_pct = (
        (cost_drag_per_trade_usdt / avg_margin) * 100 if avg_margin > 0 else 0
    )
    net_ev_per_trade_pct = ev_per_trade_pct - cost_drag_per_trade_pct
    is_negative_ev_after_costs = net_ev_per_trade_pct < 0

    # Restore slippage constants so other backtests (with deduct_real_costs=True)
    # still see the realistic execution drag.
    SLIPPAGE_BPS_STOP, SLIPPAGE_BPS_TP, SLIPPAGE_BPS_LIQ, SLIPPAGE_BPS_ENTRY, SLIPPAGE_BPS_FLIP = _saved_slip

    return {
        "metrics": {
            "total_trades":     len(all_trades),
            "winning_trades":   wins,
            "losing_trades":    losses,
            "liquidations":     liquidations,
            "long_trades":      longs,
            "short_trades":     shorts,
            "win_rate":         round(win_rate, 4),
            "total_profit_pct": round(total_profit_pct, 2),
            "total_profit_abs": round(total_profit_abs, 4),
            "final_balance":    round(balance, 2),
            "max_drawdown":     round(max_dd * 100, 2),
            "avg_leverage_pnl": round(avg_pnl, 3),
            "leverage":         leverage,
            "starting_balance": starting_balance,
            # Production-grade cost-transparency rows. ALL THREE are
            # DEDUCTED from the simulated balance — the final P&L you see
            # is net of every cost you'd pay on KuCoin in production.
            "total_funding_paid":   round(total_funding, 4),
            "total_slippage_paid":  round(total_slippage, 4),
            "total_fees_paid":      round(total_fees, 4),     # real KuCoin fees, deducted
            "kucoin_taker_fee_pct": taker_fee_rate * 100,
            "kucoin_maker_fee_pct": maker_fee_rate * 100,
            "vip_tier":             int(max(0, min(12, vip_tier))),
            "maker_only_entry":     bool(maker_only_entry),
            # Math-check rails (see comment above the computation)
            "breakeven_win_rate":   round(breakeven_wr, 4),
            "risk_reward_ratio":    round(rr_ratio, 3),
            "expected_value_pct":   round(ev_per_trade_pct, 3),
            "is_negative_ev":       bool(is_negative_ev),
            # Cost-drag analysis — explains the gap between "WR above
            # breakeven, EV positive" and "balance ended negative". The
            # UI shows this when net EV flips sign vs gross EV.
            "cost_drag_per_trade_usdt": round(cost_drag_per_trade_usdt, 4),
            "cost_drag_per_trade_pct":  round(cost_drag_per_trade_pct, 3),
            "net_expected_value_pct":   round(net_ev_per_trade_pct, 3),
            "is_negative_ev_after_costs": bool(is_negative_ev_after_costs),
            # Source of the SL/TP used in the breakeven math. "realised"
            # means we measured actual trade outcomes (correct for strategies
            # that override SL/TP); "slider" means we used the UI values.
            "sltp_source_for_ev":   "realised" if using_realised else "slider",
            "realised_avg_sl_pct":  round(realised_sl_pct, 3) if realised_sl_pct is not None else None,
            "realised_avg_tp_pct":  round(realised_tp_pct, 3) if realised_tp_pct is not None else None,
        },
        "trades":              all_trades,
        "equity_curve":        equity_curve,
        # Per-pair data-coverage report so the user can spot incomplete
        # backtests at a glance (e.g. KuCoin returned only 60% of expected
        # candles or no funding history was found for the range).
        "data_quality":        data_diagnostics,
        # If the user's strategy code errored, surface it here. The
        # backtest still completes with the name-match fallback so the
        # user always sees a number, but with a clear warning of why
        # their custom code wasn't used.
        "user_strategy_error": user_strategy_error,
    }


# ── Auto-tune SL/TP grid ──────────────────────────────────────────────────
#
# Runs the SAME backtest with a small grid of SL/TP combinations and reports
# which combo gave the best result. Helps the user find the configuration
# that fits the strategy's actual signal quality on the chosen market —
# tight stops get whipsawed by noise, wide TPs are never reached, etc.
#
# The grid is small (3 × 4 = 12 runs) because each run is a real backtest
# (~5-30s); larger grids would time out the request. Data is cached at the
# load_futures_ohlcv / load_funding_history layer so all 12 runs share ONE
# KuCoin download.

# Default grid is intentionally tiny (2 SL × 3 TP = 6 cells) so the whole
# auto-tune fits inside Vercel's ~60s edge-proxy window — the previous 3×3
# grid still took 70-120s on 6M of 15m candles (≈35k bars × ≈10s/cell) and
# 502'd via "Application failed to respond". Six cells covers tight/medium
# SL with low/balanced/wide TP — enough to spot the breakeven cliff for
# most strategies, and the full grid completes in ~40-50s.
#
# Users who want a denser grid can pass sl_grid / tp_grid explicitly in the
# request body; the budget still applies so they always get partial results
# rather than a 502.
AUTO_TUNE_SL_GRID = [2.0, 4.0]                  # SL percentages
AUTO_TUNE_TP_GRID = [3.0, 6.0, 10.0]            # TP percentages
AUTO_TUNE_BUDGET_SECS = 50                       # hard deadline; returns partial


def walk_forward_backtest(
    strategy_name: str,
    pairs: list[str],
    timeframe: str,
    timerange: str,
    n_windows: int = 4,
    **bt_kwargs,
) -> dict:
    """Out-of-sample robustness check (WolfBot-style walk-forward, simplified).

    Splits `timerange` (YYYYMMDD-YYYYMMDD) into `n_windows` consecutive,
    non-overlapping windows and runs the SAME strategy on each. A genuine edge
    holds up across windows; a curve-fit one is positive in one window and
    negative in others. This is the anti-overfitting tool — it tells you whether
    a backtest result is real or a mirage BEFORE you risk money.

    Returns per-window results + a verdict (robust / mostly_robust / fragile).
    """
    from datetime import datetime as _dt, timedelta as _td
    try:
        s_str, e_str = timerange.split("-")
        start = _dt.strptime(s_str, "%Y%m%d")
        end = _dt.strptime(e_str, "%Y%m%d")
    except Exception:
        return {"error": f"Invalid timerange '{timerange}'. Use YYYYMMDD-YYYYMMDD."}
    n_windows = max(2, min(12, int(n_windows)))
    total_days = (end - start).days
    if total_days < n_windows * 5:
        return {"error": f"Period too short for {n_windows} windows "
                         f"({total_days} days). Use a longer range or fewer windows."}
    seg = total_days // n_windows

    windows = []
    for k in range(n_windows):
        ws = start + _td(days=k * seg)
        we = end if k == n_windows - 1 else start + _td(days=(k + 1) * seg)
        tr = f"{ws.strftime('%Y%m%d')}-{we.strftime('%Y%m%d')}"
        r = run_futures_backtest(strategy_name, pairs, timeframe, tr, **bt_kwargs)
        m = {} if r.get("error") else r.get("metrics", {})
        windows.append({
            "window":   k + 1,
            "timerange": tr,
            "trades":    m.get("total_trades", 0),
            "win_rate":  round(m.get("win_rate", 0) * 100, 1),
            "net_pct":   m.get("total_profit_pct", 0),
            "max_dd":    m.get("max_drawdown", 0),
            "error":     r.get("error"),
        })

    scored = [w for w in windows if w["trades"] > 0]
    nets = [w["net_pct"] for w in scored]
    positive = sum(1 for x in nets if x > 0)
    if not nets:
        verdict = "insufficient_data"
    elif positive == len(nets):
        verdict = "robust"            # positive in EVERY window
    elif positive >= max(1, round(len(nets) * 0.6)):
        verdict = "mostly_robust"
    else:
        verdict = "fragile_or_overfit"
    return {
        "windows":          windows,
        "windows_scored":   len(scored),
        "windows_positive": positive,
        "avg_net_pct":      round(sum(nets) / len(nets), 2) if nets else 0.0,
        "worst_net_pct":    round(min(nets), 2) if nets else 0.0,
        "best_net_pct":     round(max(nets), 2) if nets else 0.0,
        "verdict":          verdict,
    }


def auto_tune_sltp(
    strategy_name:    str,
    pairs:            list[str],
    timeframe:        str,
    timerange:        str,
    leverage:         int = 10,
    starting_balance: float = 1000.0,
    risk_per_trade:   float = 0.05,
    generated_code:   str | None = None,
    sl_grid:          list[float] | None = None,
    tp_grid:          list[float] | None = None,
) -> dict:
    """Run the SL/TP grid and return a ranked list of results.

    Wrapped in a time budget so we always return SOMETHING within
    Railway's HTTP timeout window — if half the grid completes in
    45s, we return those 4-5 results instead of letting the whole
    request 502 with the upstream proxy.
    """
    sl_grid = sl_grid or AUTO_TUNE_SL_GRID
    tp_grid = tp_grid or AUTO_TUNE_TP_GRID

    import time as _t
    deadline = _t.time() + AUTO_TUNE_BUDGET_SECS
    grid: list[dict] = []
    timed_out = False
    for sl in sl_grid:
        for tp in tp_grid:
            if _t.time() > deadline:
                timed_out = True
                break
            res = run_futures_backtest(
                strategy_name    = strategy_name,
                pairs            = pairs,
                timeframe        = timeframe,
                timerange        = timerange,
                leverage         = leverage,
                starting_balance = starting_balance,
                stoploss_pct     = sl,
                take_profit_pct  = tp,
                risk_per_trade   = risk_per_trade,
                generated_code   = generated_code,
                # Force slider SL/TP so each grid cell ACTUALLY tests its
                # configured combo — otherwise SMCStrategyTV (and any other
                # strategy with structural SL/TP) would produce identical
                # results in every cell and the grid would be useless.
                force_slider_sltp = True,
            )
            m = res.get("metrics", {})
            grid.append({
                "sl_pct":            sl,
                "tp_pct":            tp,
                "rr_ratio":          m.get("risk_reward_ratio", 0),
                "total_trades":      m.get("total_trades", 0),
                "win_rate":          m.get("win_rate", 0),
                "breakeven_wr":      m.get("breakeven_win_rate", 0),
                "expected_value":    m.get("expected_value_pct", 0),
                "is_negative_ev":    m.get("is_negative_ev", True),
                "total_profit_pct":  m.get("total_profit_pct", 0),
                "max_drawdown":      m.get("max_drawdown", 0),
                "liquidations":      m.get("liquidations", 0),
            })
        if timed_out:
            break

    if not grid:
        # Even ONE cell didn't fit in the budget. Return diagnostic error
        # the UI can show as a clean message instead of a 502.
        return {
            "error": (
                f"Auto-tune timed out before completing any backtest "
                f"({AUTO_TUNE_BUDGET_SECS}s budget). Try a shorter "
                f"timerange (1W or 1M) or a higher timeframe (1h/4h)."
            ),
            "verdict": "timeout",
            "grid":    [],
            "runs":    0,
        }

    # Best = highest profit among positive-EV rows; if no row has positive
    # EV, the best one is the LEAST bad (highest profit overall) with a
    # clear "no positive-EV combination found" warning attached.
    positive_ev = [r for r in grid if not r["is_negative_ev"]]
    if positive_ev:
        best = max(positive_ev, key=lambda r: r["total_profit_pct"])
        verdict = "found_positive_ev"
    else:
        best = max(grid, key=lambda r: r["total_profit_pct"])
        verdict = "no_positive_ev_in_grid"

    return {
        "verdict":      verdict,
        "best":         best,
        "grid":         grid,
        "sl_grid":      sl_grid,
        "tp_grid":      tp_grid,
        "strategy":     strategy_name,
        "pair":         pairs[0] if pairs else "",
        "timeframe":    timeframe,
        "timerange":    timerange,
        "leverage":     leverage,
        "runs":         len(grid),
        "expected_runs": len(sl_grid) * len(tp_grid),
        "timed_out":    timed_out,
        "budget_secs":  AUTO_TUNE_BUDGET_SECS,
    }
