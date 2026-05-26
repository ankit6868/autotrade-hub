'use client';

import React, { useEffect, useState, Suspense } from 'react';
import { api } from '@/lib/api';
import MetricCard from '@/components/ui/MetricCard';
import LoadingSpinner from '@/components/ui/LoadingSpinner';
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
  BarChart, Bar, Cell,
} from 'recharts';

// ─── Time-range helpers (same as spot backtest) ───────────────────────────────
const PRESETS = [
  { label: '1W',     days: 7,    note: '' },
  { label: '1M',     days: 30,   note: '' },
  { label: '3M',     days: 90,   note: '' },
  { label: '6M',     days: 180,  note: '' },
  { label: '1Y',     days: 365,  note: '' },
  { label: '2Y',     days: 730,  note: '~30s download' },
  { label: '5Y',     days: 1825, note: '~2 min download' },
  { label: '10Y',    days: 3650, note: '~5 min download' },
  { label: 'Custom', days: 0,    note: '' },
];

function toYMD(d: Date): string {
  const y  = d.getFullYear();
  const m  = String(d.getMonth() + 1).padStart(2, '0');
  const dy = String(d.getDate()).padStart(2, '0');
  return `${y}${m}${dy}`;
}
function fromYMD(s: string): string {
  if (s.length !== 8) return s;
  const d = new Date(Number(s.slice(0, 4)), Number(s.slice(4, 6)) - 1, Number(s.slice(6, 8)));
  return d.toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' });
}
function buildTimerange(days: number): string {
  const end   = new Date();
  const start = new Date();
  if (days === 7) {
    // 1W: align to Monday of current week (matches TradingView's "1W" period)
    const day = end.getDay();                      // 0=Sun … 6=Sat
    const daysToMonday = day === 0 ? 6 : day - 1; // days since last Monday
    start.setDate(end.getDate() - daysToMonday);
  } else {
    start.setDate(end.getDate() - days);
  }
  return `${toYMD(start)}-${toYMD(end)}`;
}

// ─────────────────────────────────────────────────────────────────────────────

function FuturesBacktestInner() {
  // ── Config ─────────────────────────────────────────────────────────────────
  const [strategies,      setStrategies]      = useState<any[]>([]);
  const [strategyId,      setStrategyId]      = useState<number | null>(null);
  const [selectedPreset,  setSelectedPreset]  = useState('1Y');
  const [timerange,       setTimerange]       = useState(() => buildTimerange(365));
  const [customRange,     setCustomRange]     = useState('');
  const [pairs,           setPairs]           = useState<string[]>(['BTC/USDT']);
  const [pairQuery,       setPairQuery]       = useState('');
  const [availablePairs,  setAvailablePairs]  = useState<string[]>([]);
  const [pairsLoading,    setPairsLoading]    = useState(false);
  const [showPairDrop,    setShowPairDrop]    = useState(false);
  const [timeframe,       setTimeframe]       = useState('15m');
  // Pine Script export modal — shows the TradingView equivalent of the
  // selected built-in strategy so the user can paste into TV's Pine
  // Editor and verify our backtest matches TV's backtest. TV has no
  // public backtest API, so manual paste+run is the only comparison path.
  const [showPineModal,   setShowPineModal]   = useState(false);
  const [startBalance,    setStartBalance]    = useState(1000);
  const [leverage,        setLeverage]        = useState(10);
  const [stoploss,        setStoploss]        = useState(1.5);   // SL ≤ TP for positive R:R
  const [takeProfit,      setTakeProfit]      = useState(3.0);   // TP should be ≥ SL (2:1 R:R)
  // Position model: 'single' (TV-default stop-and-reverse) or 'hedge'
  // (LONG + SHORT coexist independently per pair). Hedge mode is
  // strongly recommended for mean-reversion strategies (BB) where
  // stop-and-reverse exits were killing trades mid-range. SMC and
  // trend-following strategies typically want 'single'.
  const [positionMode, setPositionMode] = useState<'single' | 'hedge'>('single');
  // max_concurrent_positions is locked to 1 (per direction) in both
  // modes. In hedge mode this means 1 long + 1 short = 2 positions max.
  // In single mode it means 1 total position with stop-and-reverse.
  const pyramiding = 1;
  // Margin per trade as % of current balance. 5% = $50 margin on $1000
  // balance — gives you 20 losing trades before wipeout at 100% SL hit
  // rate. Above 10% gets aggressive; above 20% can liquidate the account
  // on a single bad streak at any meaningful leverage.
  const [riskPerTrade,    setRiskPerTrade]    = useState(5);
  // Whether to deduct funding fees + KuCoin taker/maker fees from the
  // simulated balance. Default OFF (pure strategy P&L) — user explicitly
  // asked: "no need of real trading cost and dont deduct my p&l from
  // this fee". When ON, balance reflects what the strategy would deliver
  // on KuCoin after all execution costs.
  const [deductCosts,     setDeductCosts]     = useState(false);
  // SL/TP source mode:
  //   'strategy' = use the SL/TP the signal function returns (e.g.
  //                SMCStrategyTV's structural swing-based stops + 2R TP)
  //   'slider'   = override structural values with the slider %s
  // Maps directly to the backend's force_slider_sltp flag (slider=true).
  // Default 'strategy' for structural strategies, 'slider' otherwise —
  // updated by the strategy-change effect.
  const [sltpMode,        setSltpMode]        = useState<'strategy' | 'slider'>('strategy');
  // Track WHERE each parameter's current value came from so we can label
  // the control with "from strategy" or "default" — transparent to the
  // user about what was inherited vs what's a fallback we picked.
  type Src = 'strategy' | 'default' | 'manual';
  const [slSrc,  setSlSrc]  = useState<Src>('default');
  const [tpSrc,  setTpSrc]  = useState<Src>('default');
  const [levSrc, setLevSrc] = useState<Src>('default');
  const [tfSrc,  setTfSrc]  = useState<Src>('default');

  // ── State ───────────────────────────────────────────────────────────────────
  const [running,  setRunning]  = useState(false);
  const [result,   setResult]   = useState<any>(null);
  const [tuning,   setTuning]   = useState(false);
  const [tuneResult, setTuneResult] = useState<any>(null);
  const [history,  setHistory]  = useState<any[]>([]);
  const [error,    setError]    = useState('');
  // Trade-table view mode: 'compact' = our dense one-row-per-trade view
  // (Margin/Position/Liq/SL%/TP%/Exit-reason etc.); 'tv' = TradingView's
  // "List of trades" shape — two rows per trade (Exit on top, Entry below)
  // with shared P&L. Easier to compare side-by-side with TV's CSV export.
  const [tradeView, setTradeView] = useState<'compact' | 'tv'>('compact');

  // ── Advanced Risk Management (ARM) state ──────────────────────────────────
  // When `armEnabled` is OFF (default), all the arm_* params are ignored
  // by the backend and the engine uses single-TP at the strategy's TP value
  // (matches Pine and matches every prior backtest in this session).
  // When ON, the strategy's TP value becomes TP2; TP1 = midpoint(entry, TP2).
  // At TP1 hit: `tp1ClosePct`% closes, SL moves to BE (mode below). If price
  // continues halfway from TP1 to TP2, SL trails up to TP1.
  const [armEnabled,       setArmEnabled]       = useState(false);
  const [armTp1ClosePct,   setArmTp1ClosePct]   = useState(50);          // 1-99
  const [armBeMode,        setArmBeMode]        = useState<'leverage' | 'manual_pct' | 'entry'>('leverage');
  const [armBeBufferPct,   setArmBeBufferPct]   = useState(1.0);         // used only when manual_pct
  const [armTrailToTp1,    setArmTrailToTp1]    = useState(true);
  // Tick-level SL/TP precision: when ON, the engine resolves same-bar
  // SL+TP ambiguity using OHLC-path inference (always) + sub-bar 1m
  // replay (for TFs > 1m). Major accuracy gain for 1m scalp backtests.
  const [tickPrecision,    setTickPrecision]    = useState(false);
  // KuCoin Futures VIP fee tier (0..12). Each tier has its own
  // maker/taker rates per the published schedule. Default 0 (retail).
  const [vipTier,          setVipTier]          = useState<number>(0);
  // Maker-only entry mode: simulates post-only limit at signal price.
  // Trades pay maker fees (cheaper) but some signals don't fill if
  // price moves past the limit before the next bar's range crosses it.
  const [makerOnlyEntry,   setMakerOnlyEntry]   = useState(false);
  // Phase 4b: timeframe-aware risk engine — routes every signal's SL/TP
  // through risk_engine.compute_tp_sl so backtest matches the live bot
  // engine's behaviour. Default OFF for backward compat with prior tuning.
  const [useRiskEngine,    setUseRiskEngine]    = useState(false);

  useEffect(() => {
    // MUST-1: deep-link query params from the bot panel's "Run Backtest Now"
    // CTA (after a live-guardrail block). Reads ?strategy_id=X&pair=BTC/USDT&tf=15m
    // and pre-fills the form so the user can hit Run immediately.
    let presetStrategyId: number | null = null;
    let presetPair:       string | null = null;
    let presetTf:         string | null = null;
    try {
      const sp = new URLSearchParams(window.location.search);
      const sid = sp.get('strategy_id');
      if (sid) presetStrategyId = Number(sid);
      const pp  = sp.get('pair');
      if (pp)  presetPair = pp;
      const tf  = sp.get('tf');
      if (tf)  presetTf = tf;
    } catch { /* ignore */ }

    api.strategy.list().then(d => {
      setStrategies(d.strategies ?? []);
      const list = d.strategies ?? [];
      if (presetStrategyId && list.some((s: any) => Number(s.id) === presetStrategyId)) {
        setStrategyId(presetStrategyId);
      } else if (list.length > 0) {
        setStrategyId(Number(list[0].id));
      }
    }).catch(() => {});

    setPairsLoading(true);
    api.market.pairs()
      .then(d => setAvailablePairs((d as any).pairs ?? []))
      .catch(() => {})
      .finally(() => setPairsLoading(false));

    api.futures.backtest.history()
      .then(d => setHistory(d.backtests ?? []))
      .catch(() => {});

    // Pre-fill pair + TF from deep-link params AFTER pairs load so the
    // dropdown actually has the value to select. Order doesn't matter
    // for setPairs/setTimeframe since they're independent of `availablePairs`.
    if (presetPair) {
      setPairs([presetPair]);
    }
    if (presetTf) {
      setTimeframe(presetTf);
      setTfSrc('manual' as any);
    }
  }, []);

  // Pull all risk parameters from a strategy row and write them into the
  // form. Used (a) automatically when the selected strategy changes, and
  // (b) on demand via the "Apply strategy params" button so the user can
  // revert manual edits without having to remember the original values.
  function applyStrategyParams(s: any | null | undefined): boolean {
    if (!s) return false;

    // Stoploss: stored as negative decimal (-0.03 = -3%). Null/0 = "not set".
    const rawSl = s.stoploss;
    if (rawSl !== null && rawSl !== undefined && Number(rawSl) !== 0) {
      setStoploss(Math.abs(Number(rawSl) * 100));
      setSlSrc('strategy');
    } else {
      setStoploss(3);
      setSlSrc('default');
    }

    // Take profit: stored as positive decimal (0.015 = 1.5%).
    const rawTp = s.take_profit;
    if (rawTp !== null && rawTp !== undefined && Number(rawTp) > 0) {
      setTakeProfit(Number(rawTp) * 100);
      setTpSrc('strategy');
    } else {
      setTakeProfit(1.5);
      setTpSrc('default');
    }

    // Leverage: stored as integer. 1× treated as "not set" (DB default).
    const rawLev = s.default_leverage;
    if (rawLev !== null && rawLev !== undefined && Number(rawLev) > 1) {
      setLeverage(Number(rawLev));
      setLevSrc('strategy');
    } else {
      setLeverage(10);
      setLevSrc('default');
    }

    if (s.timeframe) {
      setTimeframe(s.timeframe);
      setTfSrc('strategy');
    } else {
      setTimeframe('15m');
      setTfSrc('default');
    }
    return true;
  }

  // Auto-fill on strategy change so opening a fresh backtest pre-populates
  // SL/TP/leverage from the strategy without the user having to click anything.
  useEffect(() => {
    if (!strategyId || strategies.length === 0) return;
    const s = strategies.find((x: any) => x.id === strategyId);
    applyStrategyParams(s);
    // Reset SL/TP mode to whatever makes sense for the strategy: structural
    // strategies default to "from strategy"; fixed-% strategies don't have
    // meaningful structural levels so default to "from slider".
    const STRUCTURAL = new Set([
      'SMCStrategyTV', 'SMCStrategy', 'SMCProV3',
      'MissCandleLongStrategy', 'MissCandleShortStrategy',
      'MacdCrossoverStrategy', 'RsiBollingerStrategy',
    ]);
    if (s && STRUCTURAL.has(s.name)) {
      setSltpMode('strategy');
    } else {
      setSltpMode('slider');
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [strategyId, strategies]);

  // True when ALL inherited risk params on the form still match the
  // currently-selected strategy (i.e. nothing was tweaked by hand).
  // Used to disable the "Apply strategy params" button so the user can
  // see at a glance whether reverting would actually change anything.
  const selectedStrategy = strategies.find((x: any) => x.id === strategyId);
  const alreadyMatchesStrategy = selectedStrategy
    && slSrc !== 'manual'
    && tpSrc !== 'manual'
    && levSrc !== 'manual'
    && tfSrc !== 'manual';

  // High-frequency timeframe + long period guard.
  //
  // Backtest requests now go DIRECT to Railway (5min proxy timeout)
  // instead of through Vercel's 60s edge proxy, which means we can
  // safely handle much larger downloads than before. The new limits:
  //
  //   1m   safe up to ~30 days  (~43k bars, ~215 API calls, ~2min DL)
  //   5m   safe up to ~180 days (~52k bars, ~260 API calls, ~2.5min DL)
  //   15m  safe up to ~5 years
  //   1h+  safe at any preset
  //
  // Combos beyond these limits will still time out, just at Railway's
  // 5min mark instead of Vercel's 60s. The guard blocks them up-front
  // with a clear recommendation so the user doesn't waste 5 min waiting.
  const presetDays = (
    selectedPreset === 'Custom'
      ? (customRange.length === 17
          ? Math.round(
              (Date.UTC(
                Number(customRange.slice(9, 13)),
                Number(customRange.slice(13, 15)) - 1,
                Number(customRange.slice(15, 17)),
              ) -
                Date.UTC(
                  Number(customRange.slice(0, 4)),
                  Number(customRange.slice(4, 6)) - 1,
                  Number(customRange.slice(6, 8)),
                )) / 86400000,
            )
          : 0)
      : (PRESETS.find(p => p.label === selectedPreset)?.days ?? 0)
  );
  const isHighFreqTooLong = (
    (timeframe === '1m'  && presetDays > 90)  ||   // ~130k candles with parallel fetch
    (timeframe === '5m'  && presetDays > 730) ||   // 2Y on 5m, ~210k candles
    (timeframe === '15m' && presetDays > 3650)     // >10Y on 15m
  );

  // Pine Script equivalents of the built-in strategies. These are
  // hand-translated from our Python signal functions to match the same
  // signal logic so the user can paste into TradingView's Pine Editor →
  // Add to Chart → Strategy Tester, and compare TV's backtest output
  // against ours. Differences will exist (TV charges 0.1% commission by
  // default; ours runs commission-free unless user enables real costs)
  // but signal-bar counts and trade WR should match within a few percent.
  function pineScriptFor(strategyName: string | undefined): string {
    if (strategyName === 'Bollinger Bands Strategy') {
      return `//@version=5
// Bollinger Bands Strategy — port matching TradingView's built-in
// Mean-reversion: LONG when close crosses BACK ABOVE lower band,
// SHORT when close crosses BACK BELOW upper band.
strategy("Bollinger Bands Strategy — port from autotrade-hub",
     overlay = true,
     pyramiding = 0,
     default_qty_type = strategy.percent_of_equity,
     default_qty_value = 90,           // TV default
     commission_type = strategy.commission.percent,
     commission_value = 0.0,           // TV default = 0
     process_orders_on_close = false,
     calc_on_every_tick = false)

// ── Inputs (match TV's built-in) ──────────────────────────────────────
length = input.int(20,  "Length", minval = 1)
mult   = input.float(2.0, "Mult",  minval = 0.001, maxval = 50.0)
src    = close

// ── Bollinger Bands ───────────────────────────────────────────────────
basis = ta.sma(src, length)
dev   = mult * ta.stdev(src, length)
upper = basis + dev
lower = basis - dev

// ── Entries: mean-reversion crossover ─────────────────────────────────
longEntry  = ta.crossover (src, lower)
shortEntry = ta.crossunder(src, upper)

if longEntry
    strategy.entry("L", strategy.long)
if shortEntry
    strategy.entry("S", strategy.short)

plot(basis, "BB Basis", color = color.orange)
plot(upper, "BB Upper", color = color.purple)
plot(lower, "BB Lower", color = color.purple)
plotshape(longEntry,  title = "Long",  style = shape.triangleup,   location = location.belowbar, color = color.green, size = size.tiny)
plotshape(shortEntry, title = "Short", style = shape.triangledown, location = location.abovebar, color = color.red,   size = size.tiny)
`;
    }
    if (strategyName === 'SMC Strategy (5min)') {
      return `//@version=5
// SMC Strategy (5min) — exact port of autotrade-hub's SMCScalper5m.
// Multi-TF: HTF(1h) bias + MTF(15m) dealing range + LTF(5m) execution.
// 10-gate AND chain: htf_bias + zone + sweep + CHoCH + displacement +
// EMA confluence + RSI + ADX + ATR vol filter + entry-zone retest.
// Risk: max(structural SL beyond swept extreme + 5bps, 1.2x ATR), TP=2R.
strategy("SMC Strategy (5min) — port from autotrade-hub",
     overlay = true,
     pyramiding = 0,                  // single position (TV-default)
     default_qty_type = strategy.percent_of_equity,
     default_qty_value = 5,           // matches our 5% margin/trade
     commission_type = strategy.commission.percent,
     commission_value = 0.06,         // KuCoin Futures taker
     process_orders_on_close = false, // entry at NEXT bar open
     calc_on_every_tick = false)

// ── Inputs (match the Python strategy class attrs) ────────────────────
htfEmaLen    = input.int(200, "HTF (1h) EMA length", minval = 50)
mtfRangeLkb  = input.int(30,  "MTF (15m) range lookback", minval = 10)
ltfSwingN    = input.int(5,   "LTF (5m) pivot N", minval = 2)
sweepLkb     = input.int(20,  "LTF sweep lookback", minval = 5)
sweepValid   = input.int(8,   "Sweep freshness bars", minval = 1)
chochValid   = input.int(15,  "CHoCH freshness bars", minval = 1)
displValid   = input.int(5,   "Displacement freshness bars", minval = 1)
emaFastLen   = input.int(21,  "EMA fast", minval = 5)
emaSlowLen   = input.int(50,  "EMA slow", minval = 10)
rsiLen       = input.int(14,  "RSI length")
rsiMaxLong   = input.int(72,  "RSI max for long")
rsiMinShort  = input.int(28,  "RSI min for short")
adxLen       = input.int(14,  "ADX length")
adxMin       = input.int(20,  "ADX min")
displLkb     = input.int(20,  "Displacement avg body lookback")
displMult    = input.float(1.5, "Displacement body multiplier", step=0.1)
atrLen       = input.int(14,  "ATR length")
slAtrMult    = input.float(1.2, "SL = max(structural, ATR × this)", step=0.1)
maxAtrPct    = input.float(0.008, "Skip if ATR/price > this (vol filter)", step=0.001)
slBufBps     = input.float(5.0, "SL buffer beyond swept extreme (bps)", step=0.5)
rMultiple    = input.float(2.0, "R multiple for TP", step=0.1)
maxSlPct     = input.float(0.01, "Max SL distance (fraction)", step=0.001)
zoneEntryPct = input.float(0.012, "Entry zone band (within X of swept ext)", step=0.001)
sessStart    = input.int(0,  "Session start hour UTC", minval=0, maxval=23)
sessEnd      = input.int(23, "Session end hour UTC", minval=0, maxval=23)

// ── HTF (1h) EMA200 bias — uses request.security ──────────────────────
htf1hClose = request.security(syminfo.tickerid, "60", close, lookahead = barmerge.lookahead_off)
htf1hEma   = ta.ema(htf1hClose, htfEmaLen)
isBull = htf1hClose > htf1hEma
isBear = htf1hClose < htf1hEma

// ── MTF (15m) dealing range → discount/premium midpoint ───────────────
mtf15High = request.security(syminfo.tickerid, "15", high, lookahead = barmerge.lookahead_off)
mtf15Low  = request.security(syminfo.tickerid, "15", low,  lookahead = barmerge.lookahead_off)
rangeHi = ta.highest(mtf15High, mtfRangeLkb)
rangeLo = ta.lowest (mtf15Low,  mtfRangeLkb)
rangeMd = (rangeHi + rangeLo) / 2.0
inDiscount = close < rangeMd
inPremium  = close > rangeMd

// ── LTF (5m) liquidity sweep ──────────────────────────────────────────
recentLow  = ta.lowest (low [1], sweepLkb)
recentHigh = ta.highest(high[1], sweepLkb)
sweepLong  = low  < recentLow  and close > recentLow
sweepShort = high > recentHigh and close < recentHigh
// Rolling-window flag: sweep within last N bars
recentSweepLong  = ta.barssince(sweepLong)  <= sweepValid - 1
recentSweepShort = ta.barssince(sweepShort) <= sweepValid - 1
// Carry the swept level forward over the recency window
var float sweptLowFfill  = na
var float sweptHighFfill = na
var int   sweptLowAge    = 999
var int   sweptHighAge   = 999
if sweepLong
    sweptLowFfill := recentLow
    sweptLowAge := 0
else
    sweptLowAge := sweptLowAge + 1
    if sweptLowAge > sweepValid
        sweptLowFfill := na
if sweepShort
    sweptHighFfill := recentHigh
    sweptHighAge := 0
else
    sweptHighAge := sweptHighAge + 1
    if sweptHighAge > sweepValid
        sweptHighFfill := na

// ── LTF (5m) CHoCH via pivot break ────────────────────────────────────
ph = ta.pivothigh(high, ltfSwingN, ltfSwingN)
pl = ta.pivotlow (low,  ltfSwingN, ltfSwingN)
var float lastPh = na
var float lastPl = na
if not na(ph)
    lastPh := ph
if not na(pl)
    lastPl := pl
chochUp = not na(lastPh) and close > lastPh and close[1] <= lastPh
chochDn = not na(lastPl) and close < lastPl and close[1] >= lastPl
recentChochUp = ta.barssince(chochUp) <= chochValid - 1
recentChochDn = ta.barssince(chochDn) <= chochValid - 1

// ── LTF (5m) Displacement (large body candle) ─────────────────────────
body    = math.abs(close - open)
avgBody = ta.sma(body, displLkb)
displUp = (close > open) and (body > displMult * avgBody)
displDn = (close < open) and (body > displMult * avgBody)
recentDisplUp = ta.barssince(displUp) <= displValid - 1
recentDisplDn = ta.barssince(displDn) <= displValid - 1

// ── LTF (5m) EMA21/50 confluence (SOFT, NaN-tolerant in Python) ───────
emaFast = ta.ema(close, emaFastLen)
emaSlow = ta.ema(close, emaSlowLen)
emaAlignLong  = emaFast >= emaSlow * 0.998
emaAlignShort = emaFast <= emaSlow * 1.002

// ── Quality gates: RSI + ADX ──────────────────────────────────────────
rsiVal = ta.rsi(close, rsiLen)
rsiOkLong  = rsiVal < rsiMaxLong
rsiOkShort = rsiVal > rsiMinShort
[plusDi, minusDi, adxVal] = ta.dmi(adxLen, adxLen)
adxOk = adxVal >= adxMin

// ── ATR + volatility filter ───────────────────────────────────────────
atrVal  = ta.atr(atrLen)
atrPct  = atrVal / math.max(close, 0.000001)
volOk   = atrPct <= maxAtrPct

// ── Session filter (UTC hours) ────────────────────────────────────────
hourUtc = hour(time, "UTC")
inSession = hourUtc >= sessStart and hourUtc <= sessEnd

// ── Entry-zone: price within X of the swept extreme (retest) ──────────
inLongZone  = not na(sweptLowFfill)  and math.abs(close - sweptLowFfill)  / close < zoneEntryPct
inShortZone = not na(sweptHighFfill) and math.abs(close - sweptHighFfill) / close < zoneEntryPct

// ── FINAL ENTRY (10-gate AND chain — matches Python exactly) ──────────
longSetup = isBull and inDiscount and recentSweepLong and recentChochUp and recentDisplUp
        and inLongZone and emaAlignLong and rsiOkLong and adxOk and volOk and inSession
shortSetup = isBear and inPremium and recentSweepShort and recentChochDn and recentDisplDn
        and inShortZone and emaAlignShort and rsiOkShort and adxOk and volOk and inSession

// ── Risk: SL = max(structural, 1.2× ATR) ──────────────────────────────
buf = slBufBps / 10000.0
slLongStruct  = not na(sweptLowFfill)  ? sweptLowFfill  * (1.0 - buf) : (not na(lastPl) ? lastPl * (1.0 - buf) : na)
slShortStruct = not na(sweptHighFfill) ? sweptHighFfill * (1.0 + buf) : (not na(lastPh) ? lastPh * (1.0 + buf) : na)
slLongAtr  = close - atrVal * slAtrMult
slShortAtr = close + atrVal * slAtrMult
slLong  = na(slLongStruct)  ? slLongAtr  : math.min(slLongStruct,  slLongAtr)
slShort = na(slShortStruct) ? slShortAtr : math.max(slShortStruct, slShortAtr)
riskLong  = close - slLong
riskShort = slShort - close
maxDist   = close * maxSlPct
badLong   = riskLong  <= 0 or riskLong  > maxDist or na(slLong)
badShort  = riskShort <= 0 or riskShort > maxDist or na(slShort)
tpLong  = close + rMultiple * riskLong
tpShort = close - rMultiple * riskShort

longEntry  = longSetup  and not badLong
shortEntry = shortSetup and not badShort

if longEntry
    strategy.entry("L", strategy.long)
    strategy.exit("L-exit", "L", stop = slLong,  limit = tpLong)
if shortEntry
    strategy.entry("S", strategy.short)
    strategy.exit("S-exit", "S", stop = slShort, limit = tpShort)

plot(emaFast, "EMA21",  color = color.orange)
plot(emaSlow, "EMA50",  color = color.purple)
plot(rangeMd, "MTF Range Mid", color = color.gray, style = plot.style_linebr)
plotshape(longSetup,  title = "Long Setup",  style = shape.triangleup,   location = location.belowbar, color = color.green, size = size.tiny)
plotshape(shortSetup, title = "Short Setup", style = shape.triangledown, location = location.abovebar, color = color.red,   size = size.tiny)
`;
    }
    if (strategyName === 'SMCStrategyTV') {
      return `//@version=5
strategy("SMCStrategyTV — port from autotrade-hub",
     overlay = true,
     pyramiding = 0,                  // matches our Single position (TV) mode
     default_qty_type = strategy.percent_of_equity,
     default_qty_value = 5,           // matches our 5% margin/trade default
     commission_type = strategy.commission.percent,
     commission_value = 0.06,         // KuCoin Futures taker
     process_orders_on_close = false, // entry at NEXT bar open (TV parity)
     calc_on_every_tick = false)

// ── Inputs (match our slider/leverage defaults) ──────────────────────
swing_len = input.int(5, "Swing length N", minval = 1)

// ── Pivot detection (N=5 each side) ──────────────────────────────────
ph = ta.pivothigh(high, swing_len, swing_len)
pl = ta.pivotlow (low,  swing_len, swing_len)
var float last_ph = na
var float last_pl = na
if not na(ph)
    last_ph := ph
if not na(pl)
    last_pl := pl

// ── BOS: close crosses last confirmed pivot (edge detect) ────────────
bull_bos = not na(last_ph) and close > last_ph and close[1] <= last_ph
bear_bos = not na(last_pl) and close < last_pl and close[1] >= last_pl

// ── FVG zone (price currently INSIDE an unfilled 3-candle imbalance) ─
in_bull_fvg = false
in_bear_fvg = false
for k = 0 to 19
    if bar_index - k >= 2
        if high[k + 2] < low[k] and high[k + 2] <= close and close <= low[k]
            in_bull_fvg := true
            break
        if low[k + 2] > high[k] and high[k] <= close and close <= low[k + 2]
            in_bear_fvg := true
            break

// ── Structural SL/TP per signal ──────────────────────────────────────
long_entry  = bull_bos and in_bull_fvg
short_entry = bear_bos and in_bear_fvg

long_sl  = not na(last_pl) ? last_pl * 0.999 : na   // 10bps below last pivot low
short_sl = not na(last_ph) ? last_ph * 1.001 : na   // 10bps above last pivot high
long_risk  = close - long_sl
short_risk = short_sl - close

// Reject if risk > 5% of price (broken structure)
long_ok  = long_entry  and not na(long_sl)  and long_risk  > 0 and long_risk  <= close * 0.05
short_ok = short_entry and not na(short_sl) and short_risk > 0 and short_risk <= close * 0.05

long_tp1  = close + 2 * long_risk    // 2R
short_tp1 = close - 2 * short_risk

if long_ok
    strategy.entry("L", strategy.long)
    strategy.exit("L-exit", "L", stop = long_sl, limit = long_tp1)
if short_ok
    strategy.entry("S", strategy.short)
    strategy.exit("S-exit", "S", stop = short_sl, limit = short_tp1)

// Plot pivots for visual verification
plotshape(ph, "PH", shape.triangledown, location.abovebar, color.red,   size = size.tiny)
plotshape(pl, "PL", shape.triangleup,   location.belowbar, color.green, size = size.tiny)
plotshape(long_ok,  "LONG",  shape.labelup,   location.belowbar, color.lime,
          size = size.small, text = "L")
plotshape(short_ok, "SHORT", shape.labeldown, location.abovebar, color.fuchsia,
          size = size.small, text = "S")
`;
    }
    if (strategyName === 'BidirectionalStrategy') {
      // Updated to match the new pullback-in-trend logic (was: enter on
      // trend confirmation → 25% WR. Now: enter on pullbacks within HTF trend).
      return `//@version=5
strategy("BidirectionalStrategy — port from autotrade-hub",
     overlay = true,
     pyramiding = 0,
     default_qty_type = strategy.percent_of_equity,
     default_qty_value = 5,
     commission_type = strategy.commission.percent,
     commission_value = 0.06,
     process_orders_on_close = false,
     calc_on_every_tick = false)

ema21  = ta.ema(close, 21)
ema50  = ta.ema(close, 50)
ema200 = ta.ema(close, 200)
rsi    = ta.rsi(close, 14)

// HTF bias from EMA50 vs EMA200
bull_trend = ema50 > ema200
bear_trend = ema50 < ema200

// Pullback to EMA21 (within 0.5% of price)
near_ema21 = math.abs(close - ema21) < close * 0.005

// Oversold/overbought inside trend = good entry timing
long_entry  = bull_trend and near_ema21 and rsi < 40
short_entry = bear_trend and near_ema21 and rsi > 60

if long_entry
    strategy.entry("L", strategy.long)
    strategy.exit("L-exit", "L",
         stop  = close * 0.985,    // -1.5%
         limit = close * 1.030)    // +3.0%
if short_entry
    strategy.entry("S", strategy.short)
    strategy.exit("S-exit", "S",
         stop  = close * 1.015,
         limit = close * 0.970)

plot(ema21,  "EMA21",  color = color.aqua)
plot(ema50,  "EMA50",  color = color.orange)
plot(ema200, "EMA200", color = color.purple)
`;
    }
    if (strategyName === 'SMCStrategy') {
      return `//@version=5
strategy("SMCStrategy — port from autotrade-hub",
     overlay = true,
     pyramiding = 0,
     default_qty_type = strategy.percent_of_equity,
     default_qty_value = 5,
     commission_type = strategy.commission.percent,
     commission_value = 0.06,
     process_orders_on_close = false,
     calc_on_every_tick = false)

ema9  = ta.ema(close, 9)
ema21 = ta.ema(close, 21)

// EMA cross = BOS direction
bull_bos = ta.crossover(ema9, ema21)
bear_bos = ta.crossunder(ema9, ema21)

// 30-bar range midpoint for premium/discount split
range_hi  = ta.highest(high, 30)
range_lo  = ta.lowest(low, 30)
range_mid = (range_hi + range_lo) / 2

in_discount = close <= range_mid
in_premium  = close >= range_mid

long_entry  = bull_bos and in_discount
short_entry = bear_bos and in_premium

if long_entry
    strategy.entry("L", strategy.long)
    strategy.exit("L-exit", "L",
         stop  = close * 0.985,
         limit = close * 1.030)
if short_entry
    strategy.entry("S", strategy.short)
    strategy.exit("S-exit", "S",
         stop  = close * 1.015,
         limit = close * 0.970)

plot(ema9,  "EMA9",  color = color.aqua)
plot(ema21, "EMA21", color = color.orange)
plot(range_mid, "Range Mid", color = color.gray, style = plot.style_linebr)
`;
    }
    if (strategyName === 'SimpleTargetStrategy') {
      return `//@version=5
strategy("SimpleTargetStrategy — port from autotrade-hub",
     overlay = true,
     pyramiding = 0,
     default_qty_type = strategy.percent_of_equity,
     default_qty_value = 5,
     commission_type = strategy.commission.percent,
     commission_value = 0.06,
     process_orders_on_close = false,
     calc_on_every_tick = false)

rsi   = ta.rsi(close, 14)
ema20 = ta.ema(close, 20)

// LONG: deep oversold OR (mild oversold AND below EMA20)
long_entry  = rsi < 30 or (rsi < 45 and close < ema20)
// SHORT: deep overbought OR (mild overbought AND above EMA20)
short_entry = rsi > 70 or (rsi > 55 and close > ema20)

if long_entry
    strategy.entry("L", strategy.long)
    strategy.exit("L-exit", "L",
         stop  = close * 0.985,    // -1.5%
         limit = close * 1.030)    // +3.0%
if short_entry
    strategy.entry("S", strategy.short)
    strategy.exit("S-exit", "S",
         stop  = close * 1.015,
         limit = close * 0.970)

plot(ema20, "EMA20", color = color.orange)
hline(30, "RSI 30", color = color.green)
hline(70, "RSI 70", color = color.red)
`;
    }
    if (strategyName === 'SMCProV3') {
      return `//@version=5
strategy("SMCProV3 — port from autotrade-hub",
     overlay = true,
     pyramiding = 0,
     default_qty_type = strategy.percent_of_equity,
     default_qty_value = 5,
     commission_type = strategy.commission.percent,
     commission_value = 0.06,
     process_orders_on_close = false,
     calc_on_every_tick = false)

// Indicators
ema50  = ta.ema(close, 50)
ema200 = ta.ema(close, 200)
atr20  = ta.atr(20)

// 50-bar dealing range
range_hi  = ta.highest(high, 50)
range_lo  = ta.lowest(low, 50)
range_mid = (range_hi + range_lo) / 2

// HTF bias
bull_bias = close > ema200 and ema50 > ema200
bear_bias = close < ema200 and ema50 < ema200

// Premium / Discount
in_discount = close <= range_mid
in_premium  = close >= range_mid

// 20-bar sweep — current bar takes prev 20-bar low/high and closes back
prev_low_20  = ta.lowest(low,  20)[1]
prev_high_20 = ta.highest(high, 20)[1]
bull_sweep   = low  < prev_low_20  and close > prev_low_20
bear_sweep   = high > prev_high_20 and close < prev_high_20

// FVG-in-zone (3-candle imbalance containing current close)
in_bull_fvg = high[2] < low and high[2] <= close and close <= low
in_bear_fvg = low[2] > high and high <= close and close <= low[2]

// Strong move: current body ≥ 1.5× ATR20
body         = math.abs(close - open)
strong_move  = body >= 1.5 * atr20

// NY session: 12-21 UTC
in_session = hour(time, "UTC") >= 12 and hour(time, "UTC") <= 21

long_entry  = bull_bias and in_discount and bull_sweep and in_bull_fvg and strong_move and in_session
short_entry = bear_bias and in_premium  and bear_sweep and in_bear_fvg and strong_move and in_session

if long_entry
    strategy.entry("L", strategy.long)
    strategy.exit("L-exit", "L",
         stop  = close * 0.98,    // -2%
         limit = close * 1.04)    // +4%
if short_entry
    strategy.entry("S", strategy.short)
    strategy.exit("S-exit", "S",
         stop  = close * 1.02,
         limit = close * 0.96)

plot(ema50,  "EMA50",  color = color.orange)
plot(ema200, "EMA200", color = color.purple)
plot(range_mid, "Range Mid", color = color.gray, style = plot.style_linebr)
`;
    }
    // Fallback — generic note
    return `// No Pine Script port available for "${strategyName ?? '(unknown)'}".
// Pine ports exist for: SMCStrategyTV, SMCStrategy, SMCProV3,
// BidirectionalStrategy, SimpleTargetStrategy.
// Pick one of those from the Strategy dropdown to see its Pine port.
//
// Strategies with custom signal functions (MissCandleLong/Short,
// MacdCrossover, RsiBollinger, EmaScalping) don't have a direct Pine
// equivalent yet — open an issue if you'd like one written.`;
  }

  // Detect whether the selected strategy is one whose signal function
  // returns its own structural SL/TP (engine honors those over slider).
  // After the "honour strategy-returned SL/TP" fix, this is true for all
  // built-in signal functions that compute SL/TP from market structure
  // rather than fixed percentages: SMC variants, MissCandle, MACDCross,
  // RsiBollinger. The slider becomes a fallback only.
  const STRUCTURAL_SL_TP_STRATEGIES = new Set([
    'SMCStrategyTV', 'SMCStrategy', 'SMCProV3',
    'MissCandleLongStrategy', 'MissCandleShortStrategy',
    'MacdCrossoverStrategy', 'RsiBollingerStrategy',
  ]);
  const strategyOverridesSlTp =
    selectedStrategy && STRUCTURAL_SL_TP_STRATEGIES.has(selectedStrategy.name);

  // Small reusable badge that shows where a field's current value came from.
  function SourceBadge({ src }: { src: Src }) {
    if (src === 'manual') {
      return <span className="ml-2 text-[9px] font-medium px-1.5 py-0.5 rounded-full bg-slate-700/60 text-slate-300">manual</span>;
    }
    if (src === 'strategy') {
      return <span className="ml-2 text-[9px] font-medium px-1.5 py-0.5 rounded-full bg-emerald-500/15 text-emerald-300 border border-emerald-500/30" title="Value inherited from the selected strategy">from strategy</span>;
    }
    return <span className="ml-2 text-[9px] font-medium px-1.5 py-0.5 rounded-full bg-amber-500/10 text-amber-300 border border-amber-500/30" title="Strategy didn't define this field — using sensible futures default">default</span>;
  }

  function selectPreset(label: string, days: number) {
    setSelectedPreset(label);
    if (label !== 'Custom') setTimerange(buildTimerange(days));
  }

  const filteredPairs = availablePairs
    .filter(p => p.toLowerCase().includes(pairQuery.toLowerCase()) && !pairs.includes(p))
    .slice(0, 50);

  function addPair(p: string) {
    if (!pairs.includes(p)) setPairs([...pairs, p]);
    setPairQuery(''); setShowPairDrop(false);
  }
  function removePair(p: string) { setPairs(pairs.filter(x => x !== p)); }

  async function runBacktest() {
    if (!strategyId) return;
    setRunning(true); setResult(null); setError('');
    const activeRange = selectedPreset === 'Custom' ? customRange : timerange;
    try {
      const data = await api.futures.backtest.run({
        strategy_id:      strategyId,
        pairs,
        timeframe,
        timerange:        activeRange,
        leverage,
        starting_balance: startBalance,
        stoploss_pct:     stoploss,
        take_profit_pct:  takeProfit,
        max_concurrent_positions: pyramiding,
        position_mode: positionMode,
        risk_per_trade_pct: riskPerTrade,
        // sltpMode === 'slider' → tell backend to ignore strategy's
        // structural levels and use slider values instead.
        force_slider_sltp: sltpMode === 'slider',
        // When false, backend zeros out fee/funding deductions so the
        // returned P&L reflects pure price action × leverage.
        deduct_real_costs: deductCosts,
        // ── Advanced Risk Management (ARM) — see state declarations ──
        arm_enabled:        armEnabled,
        arm_tp1_close_pct:  armTp1ClosePct,
        arm_be_mode:        armBeMode,
        arm_be_buffer_pct:  armBeBufferPct,
        arm_trail_to_tp1:   armTrailToTp1,
        tick_precision:     tickPrecision,
        vip_tier:           vipTier,
        maker_only_entry:   makerOnlyEntry,
        use_risk_engine:    useRiskEngine,
      });
      if (data.error) setError(data.error);
      else {
        setResult(data);
        api.futures.backtest.history().then(d => setHistory(d.backtests ?? [])).catch(() => {});
      }
    } catch (e) { setError(friendlyError(e)); }
    setRunning(false);
  }

  // Export the trade table as a CSV file. Columns are deliberately ordered
  // and named to align with TradingView's "List of trades" CSV export
  // (Strategy Tester → Export...) so the user can drop both files side-by-
  // side in Excel/Google Sheets and compare row-by-row.
  // TV's columns: Trade #, Type, Date/Time, Signal, Price USD, Position size USD,
  //               Net Profit USD, Net Profit %, Cumulative Profit USD,
  //               Cumulative Profit %, Run-up USD, Run-up %, Drawdown USD, Drawdown %
  // We map ours to the same shape where applicable + keep our extra fields
  // (SL price, TP price, Liq price, signal-trace bar indices) appended at end.
  function downloadTradesCsv() {
    if (!result || !trades || trades.length === 0) return;
    const strategy = strategies.find((s: any) => s.id === strategyId);
    const stratName = (strategy?.name ?? 'strategy').replace(/[^a-zA-Z0-9_-]/g, '_');
    const pair = pairs[0]?.replace('/', '') ?? 'BTCUSDT';
    const ts = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
    const filename = `autotrade_hub_${stratName}_${pair}_${timeframe}_${selectedPreset}_${ts}.csv`;

    // CSV header. Quoted to be Excel-safe.
    const headers = [
      'Trade #',
      'Pair',
      'Direction',
      'Leverage',
      'Margin USDT',
      'Position USDT',
      'Entry Price',
      'Exit Price',
      'SL Price',
      'TP Price',
      'SL Distance %',
      'TP Distance %',
      'Liquidation Price',
      'Profit %',
      'P&L USDT',
      'Balance USDT',
      'Open Date',
      'Close Date',
      'Exit Reason',
      'Candles Held',
      'Signal Bar Index',
      'Entry Bar Index',
      'Exit Bar Index',
      'SL/TP Source',
      'Funding Paid USDT',
      'Slippage Paid USDT',
      'KuCoin Fees USDT',
    ];

    // Escape values per RFC 4180: wrap in quotes, double-up internal quotes.
    function esc(v: any): string {
      if (v === null || v === undefined) return '';
      const s = String(v);
      if (s.includes(',') || s.includes('"') || s.includes('\n')) {
        return '"' + s.replace(/"/g, '""') + '"';
      }
      return s;
    }

    const rows = trades.map((t: any, i: number) => {
      const slDist = (t.sl_price && t.open_rate)
        ? (Math.abs(Number(t.sl_price) - Number(t.open_rate)) / Number(t.open_rate) * 100).toFixed(3)
        : '';
      const tpDist = (t.tp_price && t.open_rate)
        ? (Math.abs(Number(t.tp_price) - Number(t.open_rate)) / Number(t.open_rate) * 100).toFixed(3)
        : '';
      return [
        i + 1,
        t.pair ?? '',
        (t.direction ?? '').toUpperCase(),
        t.leverage ?? '',
        Number(t.margin ?? 0).toFixed(4),
        (Number(t.margin ?? 0) * Number(t.leverage ?? 1)).toFixed(4),
        Number(t.open_rate ?? 0).toFixed(4),
        Number(t.close_rate ?? 0).toFixed(4),
        Number(t.sl_price ?? 0).toFixed(4),
        Number(t.tp_price ?? 0).toFixed(4),
        slDist,
        tpDist,
        Number(t.liq_price ?? 0).toFixed(4),
        Number(t.profit_pct ?? 0).toFixed(3),
        Number(t.profit_abs ?? 0).toFixed(4),
        Number(t.balance ?? 0).toFixed(2),
        String(t.open_date ?? ''),
        String(t.close_date ?? ''),
        t.exit_reason ?? '',
        t.candles_held ?? '',
        t.signal_bar_index ?? '',
        t.entry_bar_index ?? '',
        t.exit_bar_index ?? '',
        t.sltp_source ?? '',
        Number(t.funding_paid ?? 0).toFixed(4),
        Number(t.slippage_paid ?? 0).toFixed(4),
        Number(t.hyp_kucoin_fee ?? 0).toFixed(4),
      ].map(esc).join(',');
    });

    // Prepend a small metadata block as comment-style first lines (TV
    // ignores these on import, Excel shows them as one row). Useful for
    // the user to know what was tested without opening the app again.
    const meta = [
      `# AutoTrade Hub futures backtest export`,
      `# Strategy: ${strategy?.name ?? '?'}`,
      `# Pair: ${pairs.join(',')}  Timeframe: ${timeframe}  Period: ${selectedPreset} (${timerange})`,
      `# Leverage: ${leverage}x  Margin/trade: ${riskPerTrade}%  Starting balance: $${startBalance}`,
      `# SL/TP source: ${sltpMode === 'strategy' ? 'strategy structural' : `slider (SL ${stoploss}%, TP ${takeProfit}%)`}`,
      `# Real-trading costs deducted: ${deductCosts ? 'yes' : 'no (pure P&L mode)'}`,
      `# Position model: ${pyramiding === 1 ? 'Single (TradingView mode)' : 'Concurrent'}`,
      `# Advanced Risk Management: ${armEnabled
        ? `ON — TP1 close ${armTp1ClosePct}% / BE mode ${armBeMode}${armBeMode === 'manual_pct' ? ` (${armBeBufferPct}%)` : ''} / trail-to-TP1 ${armTrailToTp1 ? 'on' : 'off'}`
        : 'OFF (single TP, closes 100%)'}`,
      `# Tick precision: ${tickPrecision
        ? (timeframe === '1m' ? 'ON (OHLC-path inference)' : 'ON (sub-bar 1m + path inference)')
        : 'OFF (open-distance heuristic)'}`,
      `# Fees: VIP${vipTier} | Entry: ${makerOnlyEntry ? 'maker-only (post-only limit)' : 'taker (market)'}`,
      `# Results: ${m?.total_trades ?? 0} trades  WR ${((m?.win_rate ?? 0) * 100).toFixed(1)}%  Profit ${(m?.total_profit_pct ?? 0).toFixed(2)}%  MaxDD ${(m?.max_drawdown ?? 0).toFixed(2)}%`,
      `# Exported: ${new Date().toISOString()}`,
      '',
    ].join('\n');

    const csv = meta + headers.join(',') + '\n' + rows.join('\n') + '\n';

    // Trigger download via Blob (works in all modern browsers).
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  // Convert raw errors (often JSON blobs from Railway like
  // `{"status":"error","code":502,"message":"Application failed to respond"}`)
  // into a single human-readable line. Falls back to String(e) for anything
  // it can't parse.
  function friendlyError(e: unknown): string {
    const raw = e instanceof Error ? e.message : String(e);
    try {
      const j = JSON.parse(raw.replace(/^Error:\s*/, ''));
      if (j && typeof j === 'object') {
        if (Number(j.code) === 502 || /failed to respond/i.test(String(j.message ?? ''))) {
          return 'Backend timed out (502). The request exceeded the 60s edge-proxy window. '
               + 'Try a shorter timerange (1W/1M) or a higher timeframe (1h/4h).';
        }
        if (j.message) return String(j.message);
        if (j.error)   return String(j.error);
      }
    } catch { /* not JSON — fall through */ }
    return raw.replace(/^Error:\s*/, '');
  }

  async function autoTune() {
    if (!strategyId) return;
    setTuning(true); setTuneResult(null); setError('');
    const activeRange = selectedPreset === 'Custom' ? customRange : timerange;
    try {
      const data = await api.futures.backtest.autoTune({
        strategy_id:      strategyId,
        pairs,
        timeframe,
        timerange:        activeRange,
        leverage,
        starting_balance: startBalance,
      });
      if (data.error) setError(data.error);
      else setTuneResult(data);
    } catch (e) { setError(friendlyError(e)); }
    setTuning(false);
  }

  const activeRange   = selectedPreset === 'Custom' ? customRange : timerange;
  const [rangeStart, rangeEnd] = activeRange.split('-');
  const currentPreset = PRESETS.find(p => p.label === selectedPreset);
  const m             = result?.metrics;
  const trades        = result?.trades ?? [];

  // Build equity curve for chart
  const equityCurve = [{ trade: 0, equity: startBalance },
    ...trades.map((t: any, i: number) => ({
      trade:  i + 1,
      equity: t.balance,
    }))
  ];

  return (
    <div className="max-w-6xl mx-auto">
      {/* Header */}
      <h1 className="heading-xl mb-2">⚡ Futures Backtest</h1>
      <p className="text-slate-400 mb-6 text-sm">
        Test leveraged futures strategies on real KuCoin historical data — up to 10 years back.
        Includes liquidation simulation, funding fees, and long/short breakdown.
      </p>

      {/* ── Pine Script export modal (TradingView comparison) ─────────────
          TradingView has no public backtest API, so the only way to verify
          our results against TV is to manually paste an equivalent Pine
          Script into TV's Pine Editor and run it there. This modal shows
          the hand-translated Pine code for the selected strategy and gives
          step-by-step instructions for the user. */}
      {showPineModal && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
          onClick={() => setShowPineModal(false)}
        >
          <div
            className="bg-[#0d1424] border border-[#2a3a52] rounded-xl max-w-4xl w-full max-h-[85vh] overflow-hidden flex flex-col"
            onClick={e => e.stopPropagation()}
          >
            <div className="p-4 border-b border-[#2a3a52] flex items-center justify-between">
              <div>
                <h3 className="text-base font-semibold text-sky-300">
                  📊 Pine Script for TradingView comparison
                </h3>
                <p className="text-[11px] text-slate-400 mt-0.5">
                  Strategy: <b className="text-slate-200">{selectedStrategy?.name ?? '—'}</b>
                  {' · '}Timeframe: <b className="text-slate-200">{timeframe}</b>
                  {' · '}Pair: <b className="text-slate-200">{pairs[0] ?? 'BTC/USDT'}</b>
                </p>
              </div>
              <button
                onClick={() => setShowPineModal(false)}
                className="text-slate-400 hover:text-white text-lg leading-none px-2"
                title="Close"
              >×</button>
            </div>

            <div className="p-4 overflow-y-auto flex-1">
              <div className="mb-4 p-3 rounded-lg bg-sky-500/5 border border-sky-500/30 text-[11px] text-slate-300 leading-relaxed">
                <div className="text-sky-300 font-medium mb-1">How to compare against TradingView</div>
                <ol className="list-decimal pl-4 space-y-0.5">
                  <li>Copy the Pine Script below.</li>
                  <li>Open TradingView → load the same chart (e.g. <b>BINANCE:BTCUSDT.P</b> or <b>KUCOIN:XBTUSDTM</b>), same timeframe ({timeframe}).</li>
                  <li>Open the <b>Pine Editor</b> (bottom panel) → paste → click <b>Save</b> + <b>Add to Chart</b>.</li>
                  <li>Open <b>Strategy Tester</b> (bottom panel) → set the date range to match (e.g. {timerange.split('-')[0]} → {timerange.split('-')[1]}).</li>
                  <li>Compare Total Trades, Win Rate, and Net Profit between TV's report and ours.</li>
                </ol>
                <div className="mt-2 text-slate-400 text-[10px]">
                  Expected differences: TV defaults to 0.1% commission (we use 0.06% KuCoin taker; toggleable).
                  TV's <code>strategy.entry</code> fills at next bar open like us. Trade counts should match within ~5%;
                  win rate within ~3pp. Larger gaps mean a real signal-logic discrepancy worth investigating.
                </div>
              </div>

              <div className="relative">
                <button
                  type="button"
                  onClick={() => {
                    const code = pineScriptFor(selectedStrategy?.name);
                    navigator.clipboard?.writeText(code);
                  }}
                  className="absolute top-2 right-2 text-[10px] font-medium px-2 py-1 rounded-md bg-emerald-500/20 text-emerald-300 hover:bg-emerald-500/30 border border-emerald-500/40"
                  title="Copy Pine Script to clipboard"
                >
                  📋 Copy
                </button>
                <pre className="text-[10px] font-mono text-emerald-100 bg-black/40 border border-[#2a3a52] rounded-lg p-3 overflow-auto max-h-[55vh] whitespace-pre">
{pineScriptFor(selectedStrategy?.name)}
                </pre>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Config card */}
      <div className="card mb-8">

        {/* ── Historical Period ──────────────────────────────────────── */}
        <div className="mb-6">
          <label className="label mb-2">Historical Period</label>

          {/* Preset chips */}
          <div className="flex flex-wrap gap-2 mb-3">
            {PRESETS.map(p => (
              <button
                key={p.label}
                onClick={() => selectPreset(p.label, p.days)}
                className={`relative px-4 py-2 rounded-xl text-sm font-semibold border transition-all ${
                  selectedPreset === p.label
                    ? 'bg-brand-600/30 border-brand-500 text-brand-200 shadow-lg shadow-brand-500/10'
                    : 'bg-[#1a2236] border-[#2a3a52] text-slate-400 hover:text-white hover:border-slate-500'
                }`}
              >
                {p.label}
                {p.note && (
                  <span className="absolute -top-1.5 -right-1 text-[9px] bg-amber-500/20 text-amber-400 border border-amber-500/30 px-1 rounded-full whitespace-nowrap">
                    {p.note}
                  </span>
                )}
              </button>
            ))}
          </div>

          {/* Date display / custom input */}
          {selectedPreset === 'Custom' ? (
            <div className="space-y-2">
              <div className="flex items-center gap-2 flex-wrap">
                <input
                  className="input max-w-xs font-mono"
                  value={customRange}
                  onChange={e => setCustomRange(e.target.value)}
                  placeholder="e.g. 20240101-20241231"
                />
                {customRange && customRange.includes('-') && customRange.length === 17 && (
                  <span className="text-xs text-emerald-400">
                    ✅ {fromYMD(customRange.split('-')[0])} → {fromYMD(customRange.split('-')[1])}
                  </span>
                )}
              </div>
              <p className="text-xs text-slate-500">
                Format: <code className="text-slate-300">YYYYMMDD-YYYYMMDD</code>
                &nbsp;·&nbsp; Example quick picks:
              </p>
              <div className="flex flex-wrap gap-2">
                {[
                  { label: 'Jan–Mar 2024', range: '20240101-20240331' },
                  { label: 'Q2 2024',      range: '20240401-20240630' },
                  { label: 'Bull run 2024',range: '20241001-20241231' },
                  { label: 'Last 2 weeks', range: `${toYMD(new Date(Date.now()-14*86400000))}-${toYMD(new Date())}` },
                ].map(q => (
                  <button key={q.label} type="button"
                    onClick={() => setCustomRange(q.range)}
                    className="text-xs px-2 py-1 rounded-lg bg-[#1a2236] border border-[#2a3a52] text-slate-300 hover:border-brand-500 hover:text-white transition-colors">
                    {q.label}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <div className="flex items-center gap-2 text-sm">
              <span className="bg-[#0a0f1c] border border-[#2a3a52] rounded-lg px-3 py-1.5 text-slate-300 font-mono text-xs">
                {fromYMD(rangeStart)} → {fromYMD(rangeEnd)}
              </span>
              <span className="text-slate-500 text-xs">({activeRange})</span>
            </div>
          )}

          {(selectedPreset === '5Y' || selectedPreset === '10Y') && (
            <div className="mt-3 flex items-start gap-2 p-3 rounded-lg bg-amber-500/10 border border-amber-500/30">
              <span className="text-amber-400 mt-0.5">⚠️</span>
              <p className="text-xs text-amber-300">
                <strong>{selectedPreset} of data</strong> needs to be downloaded from KuCoin on first run
                ({selectedPreset === '5Y' ? '~2 minutes' : '~5 minutes'} for 15m candles).
              </p>
            </div>
          )}
        </div>

        {/* ── Strategy / Pairs / Timeframe ───────────────────────────── */}
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-4 mb-6">
          <div className="col-span-2">
            <label className="label flex items-center justify-between gap-2 flex-wrap">
              <span>Strategy</span>
              <span className="flex items-center gap-1.5">
                <button
                  type="button"
                  onClick={() => setShowPineModal(true)}
                  title="Show the TradingView Pine Script equivalent of this strategy so you can paste it into TV's Pine Editor and compare backtests."
                  className="text-[10px] font-medium px-2 py-0.5 rounded-md border border-sky-500/40 bg-sky-500/10 text-sky-300 hover:bg-sky-500/20"
                >
                  📊 Pine Script (for TV)
                </button>
                <button
                  type="button"
                  onClick={() => applyStrategyParams(selectedStrategy)}
                  disabled={!selectedStrategy || alreadyMatchesStrategy}
                  title={
                    alreadyMatchesStrategy
                      ? 'All risk parameters already match the selected strategy'
                      : "Reset leverage, stop-loss, take-profit and timeframe to this strategy's declared values"
                  }
                  className="text-[10px] font-medium px-2 py-0.5 rounded-md border border-emerald-500/40 bg-emerald-500/10 text-emerald-300 hover:bg-emerald-500/20 disabled:opacity-30 disabled:cursor-not-allowed"
                >
                  ⚙ Apply strategy params
                </button>
              </span>
            </label>
            <select className="input" value={strategyId ?? ''}
              onChange={e => setStrategyId(Number(e.target.value))}>
              {strategies.map((s: any) => (
                <option key={s.id} value={s.id}>{s.name}</option>
              ))}
            </select>
            {strategyOverridesSlTp && (
              <p className="mt-1 text-[10px] text-sky-300/80 leading-snug"
                 title="This strategy returns structural SL/TP per signal (e.g. SMC = swing-based stops + 2R targets, MissCandle = prev-candle high/low + 3R, MACD = fixed 1:3 RR). Use the 'SL / TP source' toggle to choose whether the engine honours those values or forces the slider values instead."
              >
                {sltpMode === 'strategy'
                  ? <>ℹ This strategy returns its own SL/TP per trade — sliders below are <b>ignored</b>. Use the toggle above to override with slider values.</>
                  : <>⚙ This strategy's structural SL/TP is <b>overridden by sliders</b>. Switch toggle back to "From strategy" to use its design.</>}
              </p>
            )}
          </div>

          {/* Pair search — same component as spot backtest */}
          <div className="col-span-2 md:col-span-3 lg:col-span-2 relative">
            <label className="label">
              Pairs
              {pairsLoading && <span className="text-[10px] text-slate-500 ml-2">loading…</span>}
            </label>
            <div className="input flex flex-wrap gap-1.5 min-h-[42px] items-center">
              {pairs.map(p => (
                <span key={p} className="flex items-center gap-1 px-2 py-0.5 bg-brand-600/20 text-brand-300 border border-brand-500/30 rounded text-xs">
                  {p}
                  <button type="button" onClick={() => removePair(p)}
                    className="text-brand-300/60 hover:text-white ml-0.5">×</button>
                </span>
              ))}
              <input
                className="flex-1 bg-transparent outline-none text-sm min-w-[120px]"
                value={pairQuery}
                onChange={e => { setPairQuery(e.target.value); setShowPairDrop(true); }}
                onFocus={() => setShowPairDrop(true)}
                onBlur={() => setTimeout(() => setShowPairDrop(false), 150)}
                onKeyDown={e => {
                  if (e.key === 'Enter' && pairQuery.trim()) {
                    e.preventDefault();
                    const typed = pairQuery.trim().toUpperCase();
                    const match = availablePairs.find(p => p.toUpperCase() === typed) ?? typed;
                    addPair(match);
                  }
                  if (e.key === 'Backspace' && !pairQuery && pairs.length)
                    removePair(pairs[pairs.length - 1]);
                }}
                placeholder={pairs.length === 0 ? 'Search coin (e.g. BTC, ETH)…' : ''}
              />
            </div>
            {showPairDrop && filteredPairs.length > 0 && (
              <div className="absolute z-20 mt-1 w-full max-h-64 overflow-y-auto bg-[#1a2236] border border-[#2a3a52] rounded-lg shadow-xl">
                {filteredPairs.map(p => (
                  <button type="button" key={p}
                    onMouseDown={e => { e.preventDefault(); addPair(p); }}
                    className="w-full text-left px-3 py-2 text-sm hover:bg-[#2a3a52]/60 border-b border-[#2a3a52]/40 last:border-0">
                    {p}
                  </button>
                ))}
              </div>
            )}
          </div>

          <div>
            <label className="label flex items-center">Timeframe <SourceBadge src={tfSrc} /></label>
            <select className="input" value={timeframe}
              onChange={e => { setTimeframe(e.target.value); setTfSrc('manual'); }}>
              {['1m','5m','15m','30m','1h','4h'].map(tf => (
                <option key={tf} value={tf}>{tf}</option>
              ))}
            </select>
          </div>
        </div>

        {/* ── SL/TP source toggle ─────────────────────────────────────────
            The strategy's signal function (e.g. SMCStrategyTV) returns
            structural SL/TP per trade — those vary per signal because they
            anchor to swing extremes. The slider below is a FIXED %. The
            user chooses which one wins. Default depends on the strategy:
            structural-SL strategies default to "from strategy"; fixed-%
            strategies default to "from slider" (their internal % matches
            the slider anyway). */}
        <div className="mb-5 flex items-center gap-3 flex-wrap">
          <label className="label !mb-0 flex items-center gap-2">
            <span>SL / TP source</span>
            <span className="text-[9px] font-medium px-1.5 py-0.5 rounded-full bg-purple-500/15 text-purple-300 border border-purple-500/30"
                  title="Decides whether the engine uses the SL/TP the strategy returns (structural — varies per trade) or the fixed slider values (uniform across all trades). Switch to 'sliders' if you want every trade to use your custom 1.5% / 3% etc; leave on 'strategy' to honour the strategy's design (e.g. SMC swing-based stops)."
            >
              your choice
            </span>
          </label>
          <div className="inline-flex rounded-lg overflow-hidden border border-[#2a3a52]">
            <button
              type="button"
              onClick={() => setSltpMode('strategy')}
              className={`px-3 py-1.5 text-xs font-medium transition-colors ${
                sltpMode === 'strategy'
                  ? 'bg-sky-500/30 text-sky-200'
                  : 'bg-[#1a2236] text-slate-400 hover:text-white'
              }`}
              title="Use the SL/TP the strategy returns per signal (e.g. SMC structural swing-based stops + 2R targets). Sliders below are ignored."
            >
              From strategy (structural)
            </button>
            <button
              type="button"
              onClick={() => setSltpMode('slider')}
              className={`px-3 py-1.5 text-xs font-medium transition-colors border-l border-[#2a3a52] ${
                sltpMode === 'slider'
                  ? 'bg-amber-500/30 text-amber-200'
                  : 'bg-[#1a2236] text-slate-400 hover:text-white'
              }`}
              title="Override the strategy's structural SL/TP — every trade exits at the fixed slider values."
            >
              From sliders below
            </button>
          </div>
          <span className="text-[11px] text-slate-400 leading-snug max-w-md">
            {sltpMode === 'strategy'
              ? <>Engine uses <b className="text-sky-300">strategy's own SL/TP</b> per signal — sliders are ignored (each trade exits at its own structural level).</>
              : <>Engine uses <b className="text-amber-300">slider SL/TP</b> for every trade — strategy's structural levels are overridden.</>}
          </span>
        </div>

        {/* ── Pure strategy P&L vs realistic costs toggle ────────────────
            Default OFF (pure) — user explicitly asked for trade P&L not
            to be reduced by funding + KuCoin fees. Slippage stays on
            always because it's fill-quality modeling not a "cost".
            When ON, balance and trade rows show what the strategy would
            actually deliver on KuCoin including all execution costs. */}
        <div className="mb-5 flex items-center gap-3 flex-wrap">
          <label className="label !mb-0 flex items-center gap-2 cursor-pointer">
            <input
              type="checkbox"
              checked={deductCosts}
              onChange={e => setDeductCosts(e.target.checked)}
              className="accent-sky-500"
            />
            <span>Include real trading costs</span>
            <span className="text-[9px] font-medium px-1.5 py-0.5 rounded-full bg-slate-700/60 text-slate-300"
                  title="When ON: KuCoin taker (0.06%) / maker (0.02%) fees + funding + 2-15 bps slippage deduct from balance. Production-grade simulation. When OFF: TradingView-equivalent pure backtest — ZERO fees, ZERO slippage, ZERO funding. Shows the strategy's theoretical edge in isolation."
            >
              {deductCosts ? 'realistic mode' : 'TV-equivalent (0% fees, 0 slippage)'}
            </span>
          </label>
          <span className="text-[11px] text-slate-400 leading-snug max-w-lg">
            {deductCosts
              ? <>Trade P&L is <b className="text-amber-300">net of KuCoin fees + funding + slippage</b> — production-grade.</>
              : <>Trade P&L is <b className="text-emerald-300">pure price action × leverage</b> — matches TradingView's default (commission=0, slippage=0).</>}
          </span>
        </div>

        {/* ── Advanced Risk Management (ARM) ─────────────────────────────
            Optional feature that splits every strategy-generated trade into
            two TP stages: TP1 = midpoint(entry, strategy_tp), TP2 = strategy_tp.
            At TP1: close X%, move SL to BE. After price moves halfway from
            TP1 to TP2: trail SL up to TP1.
            Master switch defaults OFF so existing behaviour is unchanged. */}
        <div className="mb-5 border border-[#2a3a52] rounded-lg p-3">
          <div className="flex items-center gap-3 flex-wrap">
            <label className="label !mb-0 flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={armEnabled}
                onChange={e => setArmEnabled(e.target.checked)}
                className="accent-sky-500"
              />
              <span>🎯 Advanced Risk Management</span>
              <span className="text-[9px] font-medium px-1.5 py-0.5 rounded-full bg-purple-500/20 text-purple-300"
                    title="When ON: TP1 = midpoint(entry, strategy_tp). At TP1 hit, close N% of position and move SL to break-even. If price reaches halfway from TP1 to TP2, trail SL up to TP1. When OFF: single TP at strategy's value (closes 100%)."
              >
                {armEnabled ? 'partial TP + BE trail' : 'single TP (off)'}
              </span>
            </label>
            <span className="text-[11px] text-slate-400 leading-snug max-w-md">
              {armEnabled
                ? <>Strategy's TP becomes <b className="text-purple-300">TP2</b>, TP1 = midpoint. Books partial at TP1, trails SL → BE → TP1.</>
                : <>Strategy's TP closes <b className="text-slate-300">100%</b> of the position (legacy behaviour).</>}
            </span>
          </div>

          {/* Reveal controls only when ARM is enabled */}
          {armEnabled && (
            <div className="mt-4 grid grid-cols-1 md:grid-cols-3 gap-5">
              {/* TP1 close % slider */}
              <div>
                <label className="text-xs text-slate-400 flex items-center justify-between mb-1">
                  <span>TP1 Booking: <b className="text-purple-300">{armTp1ClosePct}%</b></span>
                  <span className="text-[10px] text-slate-500">
                    TP2 Booking: {(100 - armTp1ClosePct).toFixed(0)}%
                  </span>
                </label>
                <input
                  type="range" min={1} max={99} step={1}
                  value={armTp1ClosePct}
                  onChange={e => setArmTp1ClosePct(Number(e.target.value))}
                  className="w-full accent-purple-500"
                  title="% of position closed at TP1 (midpoint of entry → strategy_tp). Remainder closes at TP2 or trailed SL."
                />
              </div>

              {/* Break-even mode picker */}
              <div>
                <label className="text-xs text-slate-400 mb-1 block">Break-even mode</label>
                <div className="inline-flex rounded-md border border-[#2a3a52] overflow-hidden text-[11px] font-medium w-full">
                  <button
                    type="button"
                    onClick={() => setArmBeMode('leverage')}
                    className={`flex-1 px-2 py-1 ${armBeMode === 'leverage'
                      ? 'bg-purple-500/20 text-purple-300'
                      : 'bg-transparent text-slate-400 hover:bg-[#2a3a52]/40'}`}
                    title={`BE = entry × (1 + leverage/1000). With ${leverage}x leverage → ${(leverage/10).toFixed(1)}% buffer.`}
                  >
                    Leverage-auto
                  </button>
                  <button
                    type="button"
                    onClick={() => setArmBeMode('manual_pct')}
                    className={`flex-1 px-2 py-1 ${armBeMode === 'manual_pct'
                      ? 'bg-purple-500/20 text-purple-300'
                      : 'bg-transparent text-slate-400 hover:bg-[#2a3a52]/40'}`}
                    title="BE = entry × (1 + buffer%) — user-set buffer below."
                  >
                    Manual %
                  </button>
                  <button
                    type="button"
                    onClick={() => setArmBeMode('entry')}
                    className={`flex-1 px-2 py-1 ${armBeMode === 'entry'
                      ? 'bg-purple-500/20 text-purple-300'
                      : 'bg-transparent text-slate-400 hover:bg-[#2a3a52]/40'}`}
                    title="BE = entry (no buffer — pure zero-loss line)."
                  >
                    At entry
                  </button>
                </div>
                {armBeMode === 'manual_pct' && (
                  <div className="mt-2">
                    <input
                      type="number"
                      min={0} max={10} step={0.1}
                      value={armBeBufferPct}
                      onChange={e => setArmBeBufferPct(Number(e.target.value))}
                      className="input !py-1 !text-xs"
                      placeholder="Buffer %"
                    />
                    <span className="text-[10px] text-slate-500 ml-1">% above entry (long) / below (short)</span>
                  </div>
                )}
                {armBeMode === 'leverage' && (
                  <div className="text-[10px] text-slate-500 mt-1">
                    With {leverage}x: BE buffer = <b className="text-purple-300">{(leverage/10).toFixed(1)}%</b>
                  </div>
                )}
              </div>

              {/* Trail-to-TP1 toggle */}
              <div>
                <label className="text-xs text-slate-400 mb-1 block">Trail SL upgrade</label>
                <label className="label !mb-0 flex items-center gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={armTrailToTp1}
                    onChange={e => setArmTrailToTp1(e.target.checked)}
                    className="accent-purple-500"
                  />
                  <span className="text-xs">Trail SL → TP1 after halfway to TP2</span>
                </label>
                <div className="text-[10px] text-slate-500 mt-1">
                  {armTrailToTp1
                    ? <>Once price reaches midpoint(TP1, TP2), SL moves up from BE to TP1.</>
                    : <>SL stays at BE after TP1 (no further trailing).</>}
                </div>
              </div>
            </div>
          )}
        </div>

        {/* ── Phase 4b: Timeframe-aware Risk Engine ─────────────────────
            When ON, every signal's SL/TP is routed through risk_engine
            which (a) honours strategy structural levels when valid +
            RR≥min, (b) otherwise computes ATR-based per-TF SL/TP, and
            (c) rejects signals that fail the RR or volatility cap. This
            makes backtest behaviour match the live bot engine, which
            uses risk_engine on every signal. */}
        <div className="mb-5 flex items-center gap-3 flex-wrap">
          <label className="label !mb-0 flex items-center gap-2 cursor-pointer">
            <input
              type="checkbox"
              checked={useRiskEngine}
              onChange={e => setUseRiskEngine(e.target.checked)}
              className="accent-emerald-500"
            />
            <span>📐 Timeframe-aware Risk Engine</span>
            <span
              className="text-[9px] font-medium px-1.5 py-0.5 rounded-full bg-emerald-500/20 text-emerald-300"
              title={
                'When ON: every signal\'s SL/TP runs through risk_engine.compute_tp_sl.\n' +
                '• Strategy SL/TP honoured if direction-valid and RR ≥ per-TF minimum.\n' +
                '• Otherwise SL/TP derived from ATR × per-TF multipliers:\n' +
                '   1m scalp:  SL=0.85×ATR  TP=2.0×ATR  min RR=2.0\n' +
                '   5m fast:   SL=1.0×ATR   TP=2.3×ATR  min RR=2.0\n' +
                '   15m intra: SL=1.3×ATR   TP=2.7×ATR  min RR=2.0\n' +
                '   30m large: SL=1.6×ATR   TP=3.2×ATR  min RR=2.0\n' +
                '   1h swing:  SL=2.0×ATR   TP=4.0×ATR  min RR=2.0\n' +
                '   4h pos:    SL=2.5×ATR   TP=5.0×ATR  min RR=2.0\n' +
                '• Rejects signals where stop > 8% of entry (crash-vol cap).\n' +
                'Backtest output matches the live bot engine when ON.'
              }
            >
              {useRiskEngine ? 'ATR × per-TF + RR gate' : 'strategy / slider SL-TP'}
            </span>
          </label>
          <span className="text-[11px] text-slate-400 leading-snug max-w-lg">
            {useRiskEngine
              ? <>SL/TP scales with <b className="text-emerald-300">{timeframe}</b>. Rejections (RR &lt; 2.0, ATR cap) appear in diagnostics.</>
              : <>Legacy: strategy structural OR slider %. Live bot uses risk_engine always — turn ON for parity.</>}
          </span>
        </div>

        {/* ── Tick-level SL/TP precision ─────────────────────────────────
            Optional toggle that resolves same-bar SL+TP ambiguity using
            higher-accuracy methods than the legacy "closer to bar open"
            heuristic. Specifically aimed at 1m scalping backtests where
            same-bar exits dominate. */}
        <div className="mb-5 flex items-center gap-3 flex-wrap">
          <label className="label !mb-0 flex items-center gap-2 cursor-pointer">
            <input
              type="checkbox"
              checked={tickPrecision}
              onChange={e => setTickPrecision(e.target.checked)}
              className="accent-sky-500"
            />
            <span>⚡ Tick-level SL/TP precision</span>
            <span className="text-[9px] font-medium px-1.5 py-0.5 rounded-full bg-cyan-500/20 text-cyan-300"
                  title="When ON: same-bar SL+TP ambiguity is resolved by (1) OHLC-path inference using each bar's open/close shape (works on any TF including 1m), and (2) for TF > 1m, additionally fetches 1m sub-bar data and replays each minute to find which level was hit first. Major accuracy improvement for 1m scalp backtests. Adds 20-60s extra download time on TFs > 1m due to sub-bar fetch."
            >
              {tickPrecision
                ? (timeframe === '1m' ? 'OHLC-path inference' : 'sub-bar + path inference')
                : 'open-distance heuristic (legacy)'}
            </span>
          </label>
          <span className="text-[11px] text-slate-400 leading-snug max-w-lg">
            {tickPrecision
              ? <>Same-bar SL+TP ambiguity resolved using <b className="text-cyan-300">bar shape + 1m sub-bars</b>. {timeframe !== '1m' && '⚠️ Adds extra data download.'}</>
              : <>Same-bar SL+TP picked by <b className="text-slate-300">"closer to open"</b> heuristic — fast but imprecise on 1m.</>}
          </span>
        </div>

        {/* ── KuCoin VIP fee tier + maker-only entry mode ────────────────
            Only shown when "Include real trading costs" is ON — these
            settings have NO effect in TV-equivalent (zero-friction) mode
            so showing them was confusing the user. When user wants to
            simulate real exchange execution, this panel exposes the knobs. */}
        {deductCosts && (
        <div className="mb-5 border border-[#2a3a52] rounded-lg p-3">
          <div className="text-xs font-semibold text-slate-200 mb-3">
            💰 Fees &amp; execution model
            <span className="text-[10px] font-normal text-slate-500 ml-2">
              (scalping-critical — fees dominate 1m strategies)
            </span>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
            {/* VIP tier selector */}
            <div>
              <label className="text-xs text-slate-400 mb-1 block">
                KuCoin Futures VIP tier
              </label>
              <select
                value={vipTier}
                onChange={e => setVipTier(Number(e.target.value))}
                className="input !py-1 !text-xs w-full"
                title="Each tier has its own maker/taker rates per KuCoin's published schedule. Default VIP0 over-estimates fees for traders with real volume. VIP12 has maker rebates (-0.008%)."
              >
                <option value={0}>VIP0 — maker 0.020% / taker 0.060% (retail)</option>
                <option value={1}>VIP1 — maker 0.018% / taker 0.055%</option>
                <option value={2}>VIP2 — maker 0.016% / taker 0.050%</option>
                <option value={3}>VIP3 — maker 0.014% / taker 0.043%</option>
                <option value={4}>VIP4 — maker 0.012% / taker 0.038%</option>
                <option value={5}>VIP5 — maker 0.010% / taker 0.030%</option>
                <option value={6}>VIP6 — maker 0.008% / taker 0.025%</option>
                <option value={7}>VIP7 — maker 0.006% / taker 0.022%</option>
                <option value={8}>VIP8 — maker 0.003% / taker 0.020%</option>
                <option value={9}>VIP9 — maker 0.000% / taker 0.018%</option>
                <option value={10}>VIP10 — maker -0.005% / taker 0.015%</option>
                <option value={11}>VIP11 — maker -0.006% / taker 0.013%</option>
                <option value={12}>VIP12 — maker -0.008% (REBATE) / taker 0.012%</option>
              </select>
              <div className="text-[10px] text-slate-500 mt-1">
                Used when "Include real trading costs" is ON. Sets the per-fill rate
                for every trade.
              </div>
            </div>

            {/* Maker-only entry toggle */}
            <div>
              <label className="text-xs text-slate-400 mb-1 block">
                Entry execution
              </label>
              <label className="label !mb-0 flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={makerOnlyEntry}
                  onChange={e => setMakerOnlyEntry(e.target.checked)}
                  className="accent-emerald-500"
                />
                <span className="text-xs">Maker-only entries (post-only limit)</span>
                <span className="text-[9px] font-medium px-1.5 py-0.5 rounded-full bg-emerald-500/20 text-emerald-300">
                  {makerOnlyEntry ? 'maker' : 'taker (market)'}
                </span>
              </label>
              <div className="text-[10px] text-slate-500 mt-1">
                {makerOnlyEntry
                  ? <>Simulates a post-only limit at the signal price. Order fills ONLY if the next bar's range touches the limit price — otherwise the signal is dropped (logged as <b className="text-amber-300">non-fill</b>). Realistic for scalping where ~15-30% of limit orders don't fill in fast markets.</>
                  : <>Default: every entry is a market taker order. Pays full taker fee. Always fills at the next bar's open + entry slippage.</>}
              </div>
            </div>
          </div>
        </div>
        )}
        {!deductCosts && (
        <div className="mb-5 border border-emerald-500/30 bg-emerald-500/5 rounded-lg p-3 text-[11px] text-slate-300">
          <span className="text-emerald-300 font-medium">✓ Pure-strategy mode active.</span>{' '}
          Fees, slippage, and funding are <b>not</b> deducted — backtest output
          matches TradingView's default (commission=0, slippage=0). Enable
          <b className="text-amber-300 mx-1">Include real trading costs</b>
          above to also configure KuCoin VIP fee tier and maker/taker mode.
        </div>
        )}

        {/* ── Futures-specific config ─────────────────────────────────── */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
          <div>
            <label
              className="label"
              title="Virtual paper-money starting balance for the simulation. No real funds are used."
            >
              Starting Balance (virtual USDT)
            </label>
            <input type="number" className="input" value={startBalance}
              onChange={e => setStartBalance(Number(e.target.value))} />
          </div>
          <div>
            <label className="label flex items-center flex-wrap">
              <span>Leverage: {leverage}x</span>
              <SourceBadge src={levSrc} />
              <span className="text-orange-400 ml-2 text-[10px]">Liq ~{(100/leverage).toFixed(1)}%</span>
            </label>
            <input type="range" min={1} max={50} value={leverage}
              onChange={e => { setLeverage(Number(e.target.value)); setLevSrc('manual'); }}
              className="w-full accent-blue-500 mt-2" />
            <div className="mt-2">
              <label
                className="label !mb-1 flex items-center flex-wrap text-[11px]"
                title="Margin (collateral) committed per trade, as % of current balance. 5% = $50 margin on $1000 = $500 notional at 10x leverage. Lower = safer, smaller compounding. Higher = bigger per-trade swings."
              >
                <span>Margin/trade: <b className="text-amber-300">{riskPerTrade}%</b></span>
                <span className="text-slate-500 ml-2 text-[10px]">
                  → ${(startBalance * riskPerTrade / 100).toFixed(0)} margin · ${(startBalance * riskPerTrade / 100 * leverage).toFixed(0)} notional
                </span>
              </label>
              <input type="range" min={1} max={50} step={1} value={riskPerTrade}
                onChange={e => setRiskPerTrade(Number(e.target.value))}
                className="w-full accent-amber-500" />
            </div>
          </div>
          <div>
            <label className="label flex items-center">
              <span>Stop-Loss: {stoploss}%</span>
              <SourceBadge src={slSrc} />
            </label>
            <input type="range" min={0.1} max={10} step={0.1} value={stoploss}
              onChange={e => { setStoploss(Number(e.target.value)); setSlSrc('manual'); }}
              className="w-full accent-red-500 mt-2" />
          </div>
          <div>
            <label className="label flex items-center flex-wrap">
              <span>Take-Profit: {takeProfit}%</span>
              <SourceBadge src={tpSrc} />
              <span className="text-emerald-400 ml-1 text-[10px]">→ {(takeProfit*leverage).toFixed(1)}% leveraged</span>
            </label>
            <input type="range" min={0.1} max={10} step={0.1} value={takeProfit}
              onChange={e => { setTakeProfit(Number(e.target.value)); setTpSrc('manual'); }}
              className="w-full accent-emerald-500 mt-2" />
          </div>
        </div>

        {/* ── Position model: Single (TV) | Hedge ─────────────────────────
            Single = TV-default stop-and-reverse (opposite signal force-
            closes the existing position).
            Hedge = LONG + SHORT can coexist on same pair, each runs to
            its own SL/TP/ARM. Recommended for mean-reversion strategies
            (Bollinger Bands) where stop-and-reverse was killing trades
            mid-range before TP1 could hit. */}
        <div className="mb-5 flex items-center gap-2 flex-wrap text-[12px]">
          <span className="text-slate-500 uppercase tracking-wider text-[10px]">Position model:</span>
          <div className="inline-flex rounded-lg border border-[#2a3a52] overflow-hidden">
            <button type="button"
              onClick={() => setPositionMode('single')}
              className={`px-3 py-1.5 text-[11px] font-medium transition-colors ${
                positionMode === 'single'
                  ? 'bg-sky-500/30 text-sky-200'
                  : 'bg-[#1a2236] text-slate-400 hover:text-white'}`}
              title="TV-default: one position per pair. Opposite signal closes existing AND opens new (stop-and-reverse).">
              Single (TV mode)
            </button>
            <button type="button"
              onClick={() => setPositionMode('hedge')}
              className={`px-3 py-1.5 text-[11px] font-medium transition-colors border-l border-[#2a3a52] ${
                positionMode === 'hedge'
                  ? 'bg-purple-500/30 text-purple-200'
                  : 'bg-[#1a2236] text-slate-400 hover:text-white'}`}
              title="LONG + SHORT both open simultaneously on same pair. No stop-and-reverse. Each position runs to its own SL/TP/ARM. Best for mean-reversion strategies (BB) where stop-and-reverse was killing trades.">
              Hedge (LONG + SHORT)
            </button>
          </div>
          <span className="text-slate-400 leading-snug max-w-md">
            {positionMode === 'hedge'
              ? '· both directions coexist — opposite signals open NEW positions, not stop-and-reverse'
              : '· while in a position, new same-direction signals are skipped (TV-default behaviour)'}
          </span>
        </div>

        {/* High-frequency-timeframe guard. With the new direct-Railway path
            we get a 5-minute budget instead of Vercel's 60s, but the
            absolute monster combos (1m × 1Y = 525k candles ≈ 22 min DL)
            still time out. Better to block them up-front than have the
            user wait 5 min for a failure. */}
        {isHighFreqTooLong && (
          <div className="mb-3 p-3 rounded-lg bg-red-500/10 border border-red-500/40 text-xs">
            <div className="text-red-300 font-medium">
              ⚠ {timeframe} timeframe × {selectedPreset} period exceeds the practical backtest budget
            </div>
            <div className="text-red-200/80 mt-1 leading-relaxed">
              This combo needs to download ~{Math.round(presetDays * (timeframe === '1m' ? 1440 : timeframe === '5m' ? 288 : 96) / 1000)}k candles,
              which takes more than the 5-minute Railway proxy timeout we
              have to work with. Pick a higher timeframe (try <b className="text-emerald-300">15m</b>{' '}
              or <b className="text-emerald-300">1h</b>) or a shorter period.
              Practical limits: <b>1m</b> ≤ 90 days · <b>5m</b> ≤ 2 years ·
              <b>15m+</b> all periods.
            </div>
          </div>
        )}

        {/* Run + Auto-tune buttons */}
        <div className="flex items-center gap-3 flex-wrap">
          <button onClick={runBacktest}
            disabled={running || tuning || !strategyId || isHighFreqTooLong || (selectedPreset === 'Custom' && (!customRange || customRange.length < 17))}
            className="btn-primary px-8 py-3 text-base">
            {running
              ? `Running ${currentPreset && currentPreset.days > 365 ? '(downloading data…)' : ''}…`
              : `▶ Run ${selectedPreset} Futures Backtest`}
          </button>
          <button onClick={autoTune}
            disabled={running || tuning || !strategyId || isHighFreqTooLong || (selectedPreset === 'Custom' && (!customRange || customRange.length < 17))}
            className="px-5 py-3 rounded-xl text-sm font-semibold border border-amber-500/40 bg-amber-500/10 text-amber-200 hover:bg-amber-500/20 disabled:opacity-40 disabled:cursor-not-allowed"
            title="Run a small grid of SL/TP combos (20 backtests) and show which combination gives the best result. Takes 1–3 minutes (data is cached so all runs share one download).">
            {tuning ? '🔬 Auto-tuning (20 runs)…' : '🔬 Auto-tune SL/TP'}
          </button>
        </div>

        {/* Summary row */}
        {!running && strategyId && (
          <div className="mt-4 pt-4 border-t border-[#2a3a52] text-xs text-slate-500 flex flex-wrap gap-x-4 gap-y-1">
            <span>📅 Period: <span className="text-slate-300">{selectedPreset === 'Custom' ? customRange : selectedPreset}</span></span>
            <span>📊 Pairs: <span className="text-slate-300">{pairs.join(', ')}</span></span>
            <span>⏱ Timeframe: <span className="text-slate-300">{timeframe}</span></span>
            <span>💰 Balance: <span className="text-slate-300">${startBalance}</span></span>
            <span>⚡ Leverage: <span className="text-slate-300">{leverage}x</span></span>
            <span>💵 Margin/trade: <span className="text-slate-300">{riskPerTrade}% (${(startBalance * riskPerTrade / 100).toFixed(0)})</span></span>
            <span>🛑 Stop-loss: <span className="text-slate-300">{stoploss}%</span></span>
            <span>🎯 Take-profit: <span className="text-slate-300">{takeProfit}%</span></span>
            <span>📐 Position: <span className="text-slate-300">
              {pyramiding === 1 ? 'Single (TV mode)' : 'Concurrent'}
            </span></span>
          </div>
        )}
      </div>

      {/* Loading */}
      {running && (
        <LoadingSpinner text={
          currentPreset && currentPreset.days > 365
            ? `Downloading ${selectedPreset} of historical data from KuCoin, then simulating ${leverage}x leveraged futures trades…`
            : `Simulating ${leverage}x leveraged futures trades on historical data…`
        } />
      )}

      {/* Error */}
      {error && (
        <div className="card mb-8 border-red-500/30 bg-red-500/10">
          <p className="text-red-400">{error}</p>
        </div>
      )}

      {/* Auto-tune in-progress notice */}
      {tuning && (
        <div className="card mb-8 border-amber-500/30 bg-amber-500/5">
          <div className="flex items-center gap-3">
            <span className="text-amber-300 animate-pulse">🔬</span>
            <div>
              <div className="text-amber-200 font-medium">Auto-tuning SL/TP grid…</div>
              <div className="text-xs text-slate-400 mt-0.5">
                Running 20 backtests (4 SL values × 5 TP values). Data is cached after the first run, so the remaining 19 are fast.
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Auto-tune results */}
      {tuneResult && tuneResult.grid && (
        <div className="card mb-8 border-amber-500/30">
          <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
            <h2 className="text-lg font-semibold">
              🔬 Auto-tune results — {tuneResult.strategy}
            </h2>
            <span className="text-xs text-slate-500">
              {tuneResult.runs} / {tuneResult.expected_runs ?? tuneResult.runs} combos tested
            </span>
          </div>
          {tuneResult.timed_out && (
            <div className="mb-3 px-3 py-2 rounded-lg bg-amber-500/10 border border-amber-500/30 text-xs text-amber-200">
              ⏱ Partial results: hit the {tuneResult.budget_secs}s time budget before finishing the full grid.
              The cells shown are real — but for a complete grid, try a shorter timerange (1W or 1M) or a higher timeframe (1h / 4h).
            </div>
          )}

          {/* Verdict + best combo */}
          <div className={`rounded-lg p-3 mb-4 border ${
            tuneResult.verdict === 'found_positive_ev'
              ? 'border-emerald-500/40 bg-emerald-500/5 text-emerald-200'
              : 'border-red-500/40 bg-red-500/5 text-red-200'
          }`}>
            {tuneResult.verdict === 'found_positive_ev' ? (
              <>
                <div className="font-medium text-sm">
                  ✓ Best combo found: SL <b>{tuneResult.best.sl_pct}%</b> · TP <b>{tuneResult.best.tp_pct}%</b> (1:{tuneResult.best.rr_ratio})
                </div>
                <div className="text-xs mt-1 text-slate-300">
                  Profit <b className={tuneResult.best.total_profit_pct >= 0 ? 'text-emerald-400' : 'text-red-400'}>
                    {tuneResult.best.total_profit_pct >= 0 ? '+' : ''}{tuneResult.best.total_profit_pct.toFixed(2)}%
                  </b>{' · '}
                  Win rate <b>{(tuneResult.best.win_rate * 100).toFixed(1)}%</b> vs breakeven {(tuneResult.best.breakeven_wr * 100).toFixed(1)}%{' · '}
                  EV <b>{tuneResult.best.expected_value >= 0 ? '+' : ''}{tuneResult.best.expected_value.toFixed(2)}%</b>/trade{' · '}
                  {tuneResult.best.total_trades} trades
                </div>
              </>
            ) : (
              <>
                <div className="font-medium text-sm">
                  ⚠️ No positive-EV combination in the tested grid
                </div>
                <div className="text-xs mt-1 text-slate-300 leading-snug">
                  Every SL/TP combination produced negative expected value. This is strong evidence the strategy's <b>signal logic</b> has no edge on this market — not just a tuning problem. Best-of-bad: SL {tuneResult.best.sl_pct}% · TP {tuneResult.best.tp_pct}% (still loses {tuneResult.best.total_profit_pct.toFixed(2)}%). Consider: changing timeframe, adding a trend filter, or trying a different strategy.
                </div>
              </>
            )}
          </div>

          {/* Grid heatmap */}
          <div className="overflow-x-auto">
            <table className="text-xs">
              <thead>
                <tr>
                  <th className="px-2 py-1 text-slate-500 text-right">SL ↓ / TP →</th>
                  {tuneResult.tp_grid.map((tp: number) => (
                    <th key={tp} className="px-3 py-1 text-slate-400 font-medium">TP {tp}%</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {tuneResult.sl_grid.map((sl: number) => (
                  <tr key={sl} className="border-t border-[#2a3a52]/50">
                    <th className="px-2 py-1 text-slate-400 text-right font-medium">SL {sl}%</th>
                    {tuneResult.tp_grid.map((tp: number) => {
                      const cell = tuneResult.grid.find(
                        (g: any) => g.sl_pct === sl && g.tp_pct === tp
                      );
                      if (!cell) return <td key={tp} className="px-3 py-1 text-slate-600">—</td>;
                      const isBest = cell.sl_pct === tuneResult.best.sl_pct
                                  && cell.tp_pct === tuneResult.best.tp_pct;
                      const profit = cell.total_profit_pct;
                      const cellColor = isBest
                        ? 'bg-amber-500/30 border-amber-400 ring-1 ring-amber-300'
                        : profit > 5  ? 'bg-emerald-500/25'
                        : profit > 0  ? 'bg-emerald-500/10'
                        : profit > -5 ? 'bg-red-500/10'
                                       : 'bg-red-500/25';
                      return (
                        <td key={tp} className={`px-3 py-1.5 text-center ${cellColor} border border-[#2a3a52]/30`}>
                          <div className={`font-mono font-semibold ${
                            profit >= 0 ? 'text-emerald-300' : 'text-red-300'
                          }`}>
                            {profit >= 0 ? '+' : ''}{profit.toFixed(1)}%
                          </div>
                          <div className="text-[9px] text-slate-500 mt-0.5">
                            WR {(cell.win_rate * 100).toFixed(0)}% · {cell.total_trades}t
                          </div>
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="text-[10px] text-slate-500 mt-2">
            Each cell = a full backtest at that SL/TP combo. Top number is total profit %, bottom is win rate · trade count.
            Brighter green = better; brighter red = worse. The amber-bordered cell is the recommended combo.
          </p>
        </div>
      )}

      {/* Results */}
      {m && (
        <>
          {/* Results header */}
          <div className="flex items-center justify-between mb-4 flex-wrap gap-2">
            <h2 className="text-xl font-semibold">
              Results — {selectedPreset} Futures Backtest
              <span className="text-sm font-normal text-slate-400 ml-2">
                {fromYMD(rangeStart)} → {fromYMD(rangeEnd)}
              </span>
            </h2>
            <span className="text-xs text-slate-500 bg-[#1a2236] px-3 py-1 rounded-full border border-[#2a3a52]">
              {pairs.join(', ')} · {timeframe} · {leverage}x · ${startBalance}
            </span>
          </div>

          {/* Simulation disclaimer — make it impossible to misread the
              backtest as touching real funds. The "$1000" is virtual
              starting capital; the "Funding: N · real KuCoin" further down
              is a COUNT of historical funding-rate data records (not money). */}
          <div className="mb-4 px-3 py-2 rounded-lg bg-emerald-500/5 border border-emerald-500/20 text-[11px] text-emerald-300/90 flex items-center gap-2">
            <span className="text-base">🧪</span>
            <span>
              <b className="text-emerald-200">Simulation only.</b> Starting balance{' '}
              <b className="text-emerald-200">${startBalance}</b> is virtual paper money.
              No real funds, no KuCoin account access — this replays your strategy
              against historical price + funding-rate data and computes a simulated P&amp;L.
            </span>
          </div>

          {/* Data quality + signal-source banner */}
          {result?.data_quality && Object.keys(result.data_quality).length > 0 && (
            <div className="card mb-4 border-[#243153] bg-[#0d1424]">
              <p className="text-xs uppercase tracking-wider text-slate-500 mb-2">Data quality &amp; signal source</p>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                {Object.entries(result.data_quality as Record<string, any>).map(([pair, d]) => {
                  const cov = Number(d.coverage_pct) || 0;
                  const covColor = cov >= 95 ? 'text-emerald-400' : cov >= 80 ? 'text-amber-400' : 'text-red-400';
                  const isUserStrat = String(d.signal_source || '').startsWith('user_strategy');
                  const isCodeFail  = String(d.signal_source || '').includes('user code failed');
                  const isNameMatch = Boolean(d.fallback_intended) || String(d.signal_source || '').includes('name-match');
                  return (
                    <div key={pair} className="text-xs text-slate-300 bg-[#0a0f1d] border border-[#1a2236] rounded px-2.5 py-2 space-y-1">
                      <div className="flex items-center justify-between gap-2">
                        <span className="font-semibold text-white">{pair}</span>
                        <span className={covColor}>
                          {d.candles_loaded} / {d.candles_expected} candles ({cov.toFixed(1)}%)
                        </span>
                      </div>
                      <div className="flex items-center justify-between gap-2 text-[10px]">
                        <span
                          className="text-slate-500"
                          title="Number of historical funding-rate records fetched from KuCoin's public funding-rates API. The simulation applies the real historical rate every 8h to your virtual position — no real money involved."
                        >
                          Funding records: {d.funding_records}{' '}
                          {d.funding_source === 'kucoin_history' ? '· historical data' : '· using 0.03% fallback'}
                        </span>
                        <span
                          className={
                            isCodeFail ? 'text-red-400 font-medium'
                            : isUserStrat ? 'text-emerald-300 font-medium'
                            : isNameMatch ? 'text-sky-300 font-medium'
                            : 'text-amber-300'
                          }
                          title={
                            isUserStrat ? 'Your strategy code was executed'
                            : isNameMatch ? "Your strategy is a Pine Script port that runs via the matching built-in signal function (this is expected, not an error)"
                            : 'Built-in pattern was used'
                          }
                        >
                          Signal: {
                            isUserStrat ? '✓ your strategy code'
                            : isCodeFail ? '⚠ user code failed → fallback'
                            : isNameMatch ? `↻ name-matched built-in (${strategies.find((s: any) => s.id === strategyId)?.name || 'strategy'})`
                            : d.signal_source
                          }
                        </span>
                      </div>
                      {(d.entry_signals_long !== undefined || d.entry_signals_short !== undefined) && (
                        <div className="text-[10px] text-slate-500 space-y-0.5">
                          <div>
                            Signal bars: <b className="text-emerald-400">{d.entry_signals_long ?? 0} long</b>
                            {' · '}<b className="text-red-400">{d.entry_signals_short ?? 0} short</b>
                            <span className="text-slate-600"> (every bar where condition is true)</span>
                          </div>
                          {(d.entry_clusters_long !== undefined || d.entry_clusters_short !== undefined) && (
                            <div>
                              Trade signals (edges): <b className="text-emerald-300">{d.entry_clusters_long ?? 0} long</b>
                              {' · '}<b className="text-red-300">{d.entry_clusters_short ?? 0} short</b>
                              <span className="text-slate-600"> (0→1 transitions — matches TV)</span>
                            </div>
                          )}
                          {(d.sltp_from_signal !== undefined || d.sltp_from_slider !== undefined) && (
                            (d.sltp_from_signal ?? 0) + (d.sltp_from_slider ?? 0) > 0 && (
                              <div title="Per-trade SL/TP source. When the strategy returns structural levels (swing/pivot-based), the engine uses those instead of slider values — the slider becomes a fallback for the rare trades where structural levels look implausible.">
                                SL/TP source: <b className="text-sky-300">{d.sltp_from_signal ?? 0} from strategy</b>
                                {' · '}<b className="text-amber-300">{d.sltp_from_slider ?? 0} from slider</b>
                                {(d.sltp_from_signal ?? 0) > 0 && (d.sltp_from_slider ?? 0) === 0 && (
                                  <span className="text-sky-400/80"> · slider values are inert for this strategy</span>
                                )}
                              </div>
                            )
                          )}
                          {/* Effective SL/TP range — the answer to "why did my
                              trade exit at -9.89% when SL is set to 1.5%?".
                              Strategy-returned structural SL varies per signal,
                              so showing the range upfront avoids confusion. */}
                          {(d.effective_sl_pct_avg !== undefined || d.effective_tp_pct_avg !== undefined) && (
                            <div title="Average / min / max SL and TP distances actually used across all trades. When 'min' and 'max' differ a lot, the strategy is computing structural SL/TP per trade (swing-based) — so individual trades will exit at very different P&L%, even with the same SL slider setting.">
                              Effective SL{' '}
                              <b className="text-red-300">{(d.effective_sl_pct_avg ?? 0).toFixed(2)}%</b>{' '}
                              <span className="text-slate-600">
                                ({(d.effective_sl_pct_min ?? 0).toFixed(2)}–{(d.effective_sl_pct_max ?? 0).toFixed(2)}%)
                              </span>
                              {' · TP '}
                              <b className="text-emerald-300">{(d.effective_tp_pct_avg ?? 0).toFixed(2)}%</b>{' '}
                              <span className="text-slate-600">
                                ({(d.effective_tp_pct_min ?? 0).toFixed(2)}–{(d.effective_tp_pct_max ?? 0).toFixed(2)}%)
                              </span>
                              <span className="text-slate-600"> · realised across all trades</span>
                            </div>
                          )}
                          {(d.trades_opened_long !== undefined || d.trades_opened_short !== undefined) && (
                            <div>
                              Trades opened: <b className="text-emerald-200">{d.trades_opened_long ?? 0} long</b>
                              {' · '}<b className="text-red-200">{d.trades_opened_short ?? 0} short</b>
                              {(d.signals_skipped_in_trade || d.signals_skipped_cooldown || d.signals_skipped_no_margin) ? (
                                <span className="text-slate-600">
                                  {' '}· skipped:
                                  {d.signals_skipped_in_trade ? ` ${d.signals_skipped_in_trade} in-trade` : ''}
                                  {d.signals_skipped_cooldown ? `, ${d.signals_skipped_cooldown} cooldown` : ''}
                                  {d.signals_skipped_no_margin ? `, ${d.signals_skipped_no_margin} no-free-margin` : ''}
                                </span>
                              ) : null}
                            </div>
                          )}
                          {d.trades_still_open_at_end !== undefined && d.trades_still_open_at_end > 0 && (
                            <div
                              className="text-amber-300 text-[10px] mt-0.5"
                              title="Trades still open after the 30-day resolve buffer past the end of your backtest window. These didn't hit SL/TP/liquidation even with the extra time — likely SL/TP set too wide, or strategy held through low volatility. Margin released back to balance; not counted in win-rate."
                            >
                              ⏳ {d.trades_still_open_at_end} position{d.trades_still_open_at_end === 1 ? '' : 's'} unresolved
                              even after 30-day buffer (excluded — unrealised P&amp;L ${(d.unrealised_pnl_at_end ?? 0).toFixed(2)})
                            </div>
                          )}
                          {(d.override_sl_from_class || d.override_tp_from_class) && (
                            <div className="text-sky-300 text-[10px] mt-0.5 border-l-2 border-sky-500/40 pl-2"
                                 title="Your strategy class declared its own stoploss / minimal_roi within sane bounds (SL: 0.1-25%, TP: 0.1-50%). The engine used THOSE instead of the slider values, since the class is the source of truth for its risk parameters.">
                              ⚙ Engine used strategy-declared SL/TP (overrode slider):
                              {d.override_sl_from_class && <> SL → <b className="text-sky-200">{d.override_sl_from_class}</b></>}
                              {d.override_tp_from_class && <> · TP → <b className="text-sky-200">{d.override_tp_from_class}</b></>}
                            </div>
                          )}
                          {(d.class_stoploss_ignored || d.class_take_profit_ignored) && (
                            <div className="text-amber-300 text-[10px] mt-0.5 border-l-2 border-amber-500/40 pl-2"
                                 title="Your strategy class declared SL/TP values outside the sane retail-trading range. Common cause: placeholder values like stoploss=-0.99 (no-stop, handled by custom_stoploss) or minimal_roi={0: 100} (ROI handled by custom_exit). These would liquidate every trade or never take profit, so the engine kept the slider values instead.">
                              ⚠ Ignored insane strategy-declared values, kept slider:
                              {d.class_stoploss_ignored && <> SL <b>{d.class_stoploss_ignored}</b></>}
                              {d.class_take_profit_ignored && <> · TP <b>{d.class_take_profit_ignored}</b></>}
                            </div>
                          )}
                          {d.resolve_buffer_bars !== undefined && d.resolve_buffer_bars > 0 && (
                            <div className="text-sky-400/70 text-[10px]"
                                 title="Extra candles fetched beyond your end date so positions opened late in the period can hit their SL/TP/liquidation properly. New entries don't fire in this buffer — only existing positions resolve.">
                              ↳ +{d.resolve_buffer_bars} buffer candles fetched past end date for trade resolution
                            </div>
                          )}
                        </div>
                      )}
                      {/* When the strategy fired 0 signals, show which class
                          + methods we found so the user can see whether
                          their populate_entry_trend is actually defined and
                          which entry/exit hooks the runner called. */}
                      {isUserStrat && (d.entry_signals_long ?? 0) === 0 && (d.entry_signals_short ?? 0) === 0 && d.strategy_class && (
                        <div className="text-[10px] text-amber-300 mt-1 leading-snug border-l-2 border-amber-500/40 pl-2 space-y-0.5">
                          <div>Strategy fired no signals on this data.</div>
                          <div className="text-slate-400">
                            Class: <code className="text-amber-200">{d.strategy_class}</code>
                          </div>
                          {Array.isArray(d.strategy_methods) && d.strategy_methods.length > 0 && (
                            <div className="text-slate-400">
                              Methods found: <code className="text-amber-200/80 text-[9px] break-all">
                                {d.strategy_methods.join(', ')}
                              </code>
                            </div>
                          )}
                          {Array.isArray(d.signal_columns) && d.signal_columns.length > 0 && (
                            <div className="text-slate-400">
                              Non-zero columns in dataframe: <code className="text-amber-200/80 text-[9px] break-all">
                                {d.signal_columns.join(', ')}
                              </code>
                            </div>
                          )}
                          {d.code_preview && (
                            <details className="mt-1.5 group">
                              <summary className="cursor-pointer text-slate-400 hover:text-amber-300 text-[10px]">
                                Show first 800 chars of strategy code ▾
                              </summary>
                              <pre className="mt-1 p-2 bg-black/30 border border-amber-500/20 rounded text-[10px] text-amber-100/90 whitespace-pre-wrap break-all font-mono overflow-auto max-h-72">
                                {d.code_preview}
                              </pre>
                            </details>
                          )}
                        </div>
                      )}
                      {d.user_code_error && !isNameMatch && (
                        <div className="text-[10px] text-red-400 mt-1 leading-snug border-l-2 border-red-500/40 pl-2">
                          User code error: <code>{d.user_code_error}</code>
                        </div>
                      )}
                      {isNameMatch && (
                        <div className="text-[10px] text-sky-300/80 mt-1 leading-snug border-l-2 border-sky-500/40 pl-2">
                          ℹ This strategy is a Python-class port (e.g. Pine Script translation),
                          so it runs via the matching built-in signal function instead of being
                          exec'd as a Freqtrade IStrategy. This is the intended path — not an error.
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
              <p className="text-[10px] text-slate-500 mt-2">
                Backtest replays <b>historical</b> KuCoin futures klines (api-futures.kucoin.com /api/v1/kline/query)
                and historical funding rates (/api/v1/contract/funding-rates) against a <b>simulated</b> portfolio.
                Nothing here touches your live KuCoin account or real funds. Custom strategies execute
                your authored IStrategy code; built-in names use the corresponding hardcoded signal function.
              </p>
            </div>
          )}

          {/* Math-check verdict — flags strategies that mathematically can't
              break even given their SL/TP ratio + observed win rate. */}
          {m.breakeven_win_rate !== undefined && (
            <div className={`card mb-4 ${
              m.is_negative_ev
                ? 'border-red-500/40 bg-red-500/5'
                : 'border-emerald-500/30 bg-emerald-500/5'
            }`}>
              <div className="flex items-center justify-between gap-3 flex-wrap">
                <div className="flex items-center gap-3">
                  <span className="text-2xl">{m.is_negative_ev ? '⚠️' : '✓'}</span>
                  <div>
                    <div className={`text-sm font-semibold ${
                      m.is_negative_ev ? 'text-red-300' : 'text-emerald-300'
                    }`}>
                      {m.is_negative_ev
                        ? 'Negative expected value — strategy loses on average'
                        : 'Positive expected value — strategy has mathematical edge'}
                    </div>
                    <div className="text-[11px] text-slate-400 mt-0.5">
                      Win rate <b className={m.is_negative_ev ? 'text-red-400' : 'text-emerald-400'}>
                        {(m.win_rate * 100).toFixed(1)}%
                      </b>{' '}
                      vs break-even <b className="text-slate-300">{(m.breakeven_win_rate * 100).toFixed(1)}%</b>{' '}
                      at 1:{(m.risk_reward_ratio ?? 0).toFixed(2)} risk/reward
                      {' · '}
                      EV/trade: <b className={(m.expected_value_pct ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}>
                        {(m.expected_value_pct ?? 0) >= 0 ? '+' : ''}{(m.expected_value_pct ?? 0).toFixed(2)}%
                      </b>
                      {m.sltp_source_for_ev === 'realised' && (
                        <span className="ml-2 text-[9px] px-1.5 py-0.5 rounded-full bg-sky-500/15 text-sky-300 border border-sky-500/30"
                              title={`Computed from realised average SL ${m.realised_avg_sl_pct?.toFixed?.(2)}% / TP ${m.realised_avg_tp_pct?.toFixed?.(2)}% across actual trades — slider values were ignored by this strategy's engine.`}>
                          from realised SL/TP
                        </span>
                      )}
                    </div>
                  </div>
                </div>
                {m.is_negative_ev && (
                  <div className="text-[10px] text-red-300/90 max-w-md leading-snug">
                    Either tighten SL, widen TP (need RR ≥ 1:{(1 / Math.max(m.win_rate, 0.01) - 1).toFixed(2)}{' '}
                    at this win rate), or add filters to lift WR above {(m.breakeven_win_rate * 100).toFixed(1)}%.
                    Code fixes can't beat this arithmetic.
                  </div>
                )}
              </div>
            </div>
          )}

          {/* Cost-drag insight — only shown when realistic costs are
              enabled. In pure-strategy mode this card would be misleading
              because no fees/funding were deducted, so there's nothing
              "eating" the edge. */}
          {deductCosts
            && m.cost_drag_per_trade_pct !== undefined
            && !m.is_negative_ev
            && m.total_profit_pct < 0 && (
            <div className="card mb-4 border-amber-500/40 bg-amber-500/5">
              <div className="flex items-start gap-3 flex-wrap">
                <span className="text-2xl">💸</span>
                <div className="flex-1">
                  <div className="text-sm font-semibold text-amber-200">
                    Real-trading costs are eating the edge
                  </div>
                  <div className="text-[11px] text-slate-300 mt-1 leading-relaxed">
                    Your strategy has a <b className="text-emerald-300">+{(m.expected_value_pct ?? 0).toFixed(2)}%</b>{' '}
                    gross edge per trade, but funding + slippage drag <b className="text-red-300">
                    -{(m.cost_drag_per_trade_pct ?? 0).toFixed(2)}%</b> per trade,
                    leaving net EV of <b className={(m.net_expected_value_pct ?? 0) >= 0 ? 'text-emerald-300' : 'text-red-300'}>
                    {(m.net_expected_value_pct ?? 0) >= 0 ? '+' : ''}{(m.net_expected_value_pct ?? 0).toFixed(2)}%</b>.
                    Across <b>{m.total_trades}</b> trades that compounded to a{' '}
                    <b className="text-red-300">{m.total_profit_pct.toFixed(2)}%</b> balance change.
                  </div>
                  <div className="text-[10px] text-slate-400 mt-2 leading-snug">
                    → Reduce trade frequency (add a cooldown / stricter filter),
                    increase TP so each winner pays for the cost overhead,
                    or move to a higher timeframe (15m → 1h cuts trade count ~4×
                    so total funding/slippage falls proportionally).
                  </div>
                </div>
              </div>
            </div>
          )}

          {/* Production-grade cost-transparency card — only shown when
              the user enabled "Realistic costs (funding + fees)". In pure-
              strategy mode the cards would all read $0 (or near-zero, just
              slippage) which is misleading; better to hide them entirely
              so the user knows the displayed P&L IS the pure number. */}
          {deductCosts && (m.total_funding_paid !== undefined ||
            m.total_slippage_paid !== undefined ||
            m.total_fees_paid !== undefined) && (
            <div className="card mb-4 border-[#243153] bg-[#0d1424]">
              <p className="text-xs uppercase tracking-wider text-slate-500 mb-2 flex items-center gap-2">
                <span>Real-trading costs (all deducted from balance)</span>
                <span className="text-[9px] font-medium px-1.5 py-0.5 rounded-full bg-emerald-500/15 text-emerald-300 border border-emerald-500/30"
                      title="Funding, slippage, and KuCoin taker/maker fees are all subtracted from the simulated P&L. Your final balance reflects the same costs you'd pay in live trading.">
                  production-grade
                </span>
              </p>
              <div className="grid grid-cols-1 md:grid-cols-3 gap-3 text-xs">
                <div className="bg-[#0a0f1d] border border-[#1a2236] rounded px-3 py-2">
                  <div className="text-slate-500 text-[10px] uppercase tracking-wider">Funding paid</div>
                  <div className="text-amber-300 font-semibold mt-0.5">
                    ${(m.total_funding_paid ?? 0).toFixed(2)}
                  </div>
                  <div className="text-[10px] text-slate-500 mt-0.5">
                    Charged at real KuCoin settlement times (00/08/16 UTC) using historical rates.
                    Deducted from balance.
                  </div>
                </div>
                <div className="bg-[#0a0f1d] border border-[#1a2236] rounded px-3 py-2">
                  <div className="text-slate-500 text-[10px] uppercase tracking-wider">Slippage paid</div>
                  <div className="text-amber-300 font-semibold mt-0.5">
                    ${(m.total_slippage_paid ?? 0).toFixed(2)}
                  </div>
                  <div className="text-[10px] text-slate-500 mt-0.5">
                    Adverse fill on stops (5bps), TPs (2bps), liquidations (15bps), entries (2bps).
                    Deducted from balance.
                  </div>
                </div>
                <div className="bg-[#0a0f1d] border border-[#1a2236] rounded px-3 py-2">
                  <div className="text-slate-500 text-[10px] uppercase tracking-wider">KuCoin fees</div>
                  <div className="text-amber-300 font-semibold mt-0.5">
                    ${(m.total_fees_paid ?? 0).toFixed(2)}
                  </div>
                  <div className="text-[10px] text-slate-500 mt-0.5">
                    Real KuCoin Futures rates: <b className="text-slate-400">{(m.kucoin_taker_fee_pct ?? 0.06).toFixed(2)}% taker</b>{' '}
                    (entries, SL, liq) / <b className="text-slate-400">{(m.kucoin_maker_fee_pct ?? 0.02).toFixed(2)}% maker</b>{' '}
                    (TP). Deducted from balance.
                  </div>
                </div>
              </div>
            </div>
          )}

          {/* Metrics row 1 — same as spot */}
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4 mb-4">
            <MetricCard
              title="Total Profit"
              value={`${m.total_profit_pct >= 0 ? '+' : ''}${m.total_profit_pct.toFixed(2)}%`}
              color={m.total_profit_pct >= 0 ? 'profit' : 'loss'}
            />
            <MetricCard title="Win Rate"     value={`${(m.win_rate * 100).toFixed(1)}%`} />
            <MetricCard title="Max Drawdown" value={`${m.max_drawdown.toFixed(2)}%`} color="loss" />
            <MetricCard title="Final Balance" value={`$${m.final_balance.toFixed(2)}`}
              color={m.final_balance >= startBalance ? 'profit' : 'loss'} />
            <MetricCard title="Total Trades" value={m.total_trades} />
            <MetricCard title="Avg P&L/Trade" value={`${m.avg_leverage_pnl >= 0 ? '+' : ''}${m.avg_leverage_pnl.toFixed(2)}%`}
              color={m.avg_leverage_pnl >= 0 ? 'profit' : 'loss'} />
          </div>

          {/* Futures-specific metrics row */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-8">
            <div className={`card ${m.liquidations > 0 ? 'border-red-500/30 bg-red-500/5' : ''}`}>
              <p className="text-xs text-slate-400 uppercase tracking-wider mb-1">⚡ Liquidations</p>
              <p className={`text-2xl font-bold ${m.liquidations > 0 ? 'text-red-400' : 'text-white'}`}>{m.liquidations}</p>
              {m.liquidations > 0 && <p className="text-xs text-red-400/70 mt-0.5">Full margin losses</p>}
            </div>
            <div className="card">
              <p className="text-xs text-slate-400 uppercase tracking-wider mb-1">📈 Long Trades</p>
              <p className="text-2xl font-bold text-emerald-400">{m.long_trades}</p>
            </div>
            <div className="card">
              <p className="text-xs text-slate-400 uppercase tracking-wider mb-1">📉 Short Trades</p>
              <p className="text-2xl font-bold text-red-400">{m.short_trades}</p>
            </div>
            <div className="card">
              <p className="text-xs text-slate-400 uppercase tracking-wider mb-1">W / L</p>
              <p className="text-2xl font-bold">
                <span className="text-emerald-400">{m.winning_trades}</span>
                <span className="text-slate-500 mx-1">/</span>
                <span className="text-red-400">{m.losing_trades}</span>
              </p>
            </div>
          </div>

          {/* Equity Curve — identical to spot backtest */}
          <div className="card mb-8">
            <h2 className="text-lg font-semibold mb-4">Equity Curve</h2>
            <ResponsiveContainer width="100%" height={300}>
              <LineChart data={equityCurve}>
                <CartesianGrid strokeDasharray="3 3" stroke="#2a3a52" />
                <XAxis dataKey="trade" stroke="#64748b" fontSize={12}
                  label={{ value: 'Trade #', position: 'insideBottom', offset: -2, fill: '#64748b', fontSize: 11 }} />
                <YAxis stroke="#64748b" fontSize={12} tickFormatter={v => `$${v.toLocaleString()}`} />
                <Tooltip
                  contentStyle={{ background: '#1a2236', border: '1px solid #2a3a52', borderRadius: 8, color: '#f1f5f9' }}
                  formatter={(v: number) => [`$${v.toFixed(2)}`, 'Portfolio']}
                />
                <Line type="monotone" dataKey="equity" stroke="#3391ff" strokeWidth={2} dot={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>

          {/* Profit Distribution — same as spot */}
          {trades.length > 0 && (
            <div className="card mb-8">
              <h2 className="text-lg font-semibold mb-4">Profit Distribution per Trade (Leveraged)</h2>
              <ResponsiveContainer width="100%" height={200}>
                <BarChart data={trades.map((t: any, i: number) => ({ trade: i + 1, profit: t.profit_pct ?? 0 }))}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#2a3a52" />
                  <XAxis dataKey="trade" stroke="#64748b" fontSize={12} />
                  <YAxis stroke="#64748b" fontSize={12} tickFormatter={v => `${v}%`} />
                  <Tooltip
                    contentStyle={{ background: '#1a2236', border: '1px solid #2a3a52', borderRadius: 8, color: '#f1f5f9' }}
                    formatter={(v: number) => [`${v.toFixed(2)}%`, 'Profit']}
                  />
                  <Bar dataKey="profit">
                    {trades.map((t: any, i: number) => (
                      <Cell key={i} fill={
                        t.exit_reason === 'liquidated' ? '#f97316'
                          : (t.profit_pct ?? 0) >= 0 ? '#22c55e' : '#ef4444'
                      } />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
              <div className="flex gap-4 mt-2 text-xs text-slate-400">
                <span className="flex items-center gap-1"><span className="w-3 h-3 rounded-sm bg-emerald-500 inline-block"/>Profit</span>
                <span className="flex items-center gap-1"><span className="w-3 h-3 rounded-sm bg-red-500 inline-block"/>Stop-Loss</span>
                <span className="flex items-center gap-1"><span className="w-3 h-3 rounded-sm bg-orange-500 inline-block"/>Liquidated</span>
              </div>
            </div>
          )}

          {/* Trade Table — Compact (dense) OR TradingView-style (2 rows per trade) */}
          <div className="card mb-8">
            <div className="flex items-center justify-between mb-4 flex-wrap gap-2">
              <h2 className="text-lg font-semibold">Trade Details</h2>
              <div className="flex items-center gap-3">
                <div className="inline-flex rounded-md border border-[#2a3a52] overflow-hidden text-[11px] font-medium"
                     title="Compact: one row per trade with all our extra columns (margin, liq, SL/TP %). TV Style: two rows per trade (Exit on top, Entry below) — same shape as TradingView's 'List of trades' for direct side-by-side comparison.">
                  <button
                    type="button"
                    onClick={() => setTradeView('compact')}
                    className={`px-3 py-1 ${tradeView === 'compact'
                      ? 'bg-blue-500/20 text-blue-300'
                      : 'bg-transparent text-slate-400 hover:bg-[#2a3a52]/40'}`}
                  >Compact</button>
                  <button
                    type="button"
                    onClick={() => setTradeView('tv')}
                    className={`px-3 py-1 ${tradeView === 'tv'
                      ? 'bg-blue-500/20 text-blue-300'
                      : 'bg-transparent text-slate-400 hover:bg-[#2a3a52]/40'}`}
                  >TV Style</button>
                </div>
                <button
                  type="button"
                  onClick={downloadTradesCsv}
                  disabled={!trades || trades.length === 0}
                  title="Download all trades as CSV (column-aligned with TradingView's Strategy Tester export for easy side-by-side comparison in Excel / Google Sheets)"
                  className="text-[11px] font-medium px-3 py-1 rounded-md border border-emerald-500/40 bg-emerald-500/10 text-emerald-300 hover:bg-emerald-500/20 disabled:opacity-30 disabled:cursor-not-allowed"
                >
                  ⬇ Download CSV
                </button>
                <span className="text-xs text-slate-500">{trades.length} trades</span>
              </div>
            </div>
            {tradeView === 'compact' && (
            <div className="overflow-x-auto max-h-[400px] overflow-y-auto">
              <table className="w-full text-sm">
                <thead className="sticky top-0 bg-[#1a2236]">
                  <tr className="text-slate-400 border-b border-[#2a3a52]">
                    <th className="text-left py-3 px-2">#</th>
                    <th className="text-left py-3 px-2">Pair</th>
                    <th className="text-right py-3 px-2">Dir</th>
                    <th className="text-right py-3 px-2">Lev</th>
                    <th className="text-right py-3 px-2" title="Margin used as collateral for this trade (= your money at risk)">Margin $</th>
                    <th className="text-right py-3 px-2" title="Notional position size = margin × leverage (= what KuCoin trades on your behalf)">Position $</th>
                    <th className="text-right py-3 px-2">Entry</th>
                    <th className="text-right py-3 px-2">Exit</th>
                    <th className="text-right py-3 px-2" title="Effective stop-loss distance from entry, as % of price. When strategy returns structural SL (e.g. SMC swing-based), this varies per trade. The slider value is only a fallback.">SL Dist %</th>
                    <th className="text-right py-3 px-2" title="Effective take-profit distance from entry, as % of price. Varies per trade for strategies with structural TP (e.g. SMC 2R targets).">TP Dist %</th>
                    <th className="text-right py-3 px-2">Liq.</th>
                    <th className="text-right py-3 px-2">Profit %</th>
                    <th className="text-right py-3 px-2">P&amp;L USDT</th>
                    <th className="text-right py-3 px-2">Balance</th>
                    <th className="text-left py-3 px-2">Open Date</th>
                    <th className="text-left py-3 px-2">Exit Reason</th>
                  </tr>
                </thead>
                <tbody>
                  {trades.map((t: any, i: number) => (
                    <tr key={i} className={`border-b border-[#2a3a52]/50 hover:bg-[#2a3a52]/20 ${
                      t.exit_reason === 'liquidated' ? 'bg-orange-500/5' : ''
                    }`}>
                      <td className="py-2 px-2 text-slate-500"
                          title={
                            t.signal_bar_index !== undefined
                              ? `Signal fired at bar #${t.signal_bar_index} → filled at bar #${t.entry_bar_index} `
                                + `(next bar's open, TV parity) → exited at bar #${t.exit_bar_index} `
                                + `(held ${t.candles_held} bars). SL/TP source: ${t.sltp_source ?? '?'}.`
                              : undefined
                          }>{i + 1}</td>
                      <td className="py-2 px-2 font-medium">{t.pair}</td>
                      <td className={`py-2 px-2 text-right font-semibold text-xs ${
                        t.direction === 'long' ? 'text-emerald-400' : 'text-red-400'
                      }`}>{t.direction?.toUpperCase()}</td>
                      <td className="py-2 px-2 text-right text-blue-400 text-xs font-bold">{t.leverage}x</td>
                      <td className="py-2 px-2 text-right font-mono text-xs text-amber-300"
                          title="Your margin (real $ at risk on this trade)">
                        ${Number(t.margin ?? 0).toFixed(2)}
                      </td>
                      <td className="py-2 px-2 text-right font-mono text-xs text-slate-400"
                          title={`Notional = margin × leverage = $${Number(t.margin ?? 0).toFixed(2)} × ${t.leverage}x`}>
                        ${(Number(t.margin ?? 0) * Number(t.leverage ?? 1)).toFixed(2)}
                      </td>
                      <td className="py-2 px-2 text-right font-mono text-xs">{Number(t.open_rate).toFixed(2)}</td>
                      <td className="py-2 px-2 text-right font-mono text-xs">{Number(t.close_rate).toFixed(2)}</td>
                      <td className="py-2 px-2 text-right font-mono text-xs text-red-300/80"
                          title={`SL price: ${Number(t.sl_price ?? 0).toFixed(2)} (${t.sltp_source ?? 'unknown'} source)`}>
                        {t.sl_price && t.open_rate
                          ? (Math.abs(Number(t.sl_price) - Number(t.open_rate)) / Number(t.open_rate) * 100).toFixed(2) + '%'
                          : '—'}
                      </td>
                      <td className="py-2 px-2 text-right font-mono text-xs text-emerald-300/80"
                          title={`TP price: ${Number(t.tp_price ?? 0).toFixed(2)} (${t.sltp_source ?? 'unknown'} source)`}>
                        {t.tp_price && t.open_rate
                          ? (Math.abs(Number(t.tp_price) - Number(t.open_rate)) / Number(t.open_rate) * 100).toFixed(2) + '%'
                          : '—'}
                      </td>
                      <td className="py-2 px-2 text-right font-mono text-xs text-orange-400">{Number(t.liq_price).toFixed(2)}</td>
                      <td className={`py-2 px-2 text-right font-semibold ${(t.profit_pct ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                        {(t.profit_pct ?? 0) >= 0 ? '+' : ''}{(t.profit_pct ?? 0).toFixed(2)}%
                      </td>
                      <td className={`py-2 px-2 text-right ${(t.profit_abs ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                        {(t.profit_abs ?? 0) >= 0 ? '+' : ''}{(t.profit_abs ?? 0).toFixed(2)}
                      </td>
                      <td className="py-2 px-2 text-right font-mono text-xs">{t.balance?.toFixed(2)}</td>
                      <td className="py-2 px-2 text-slate-400 text-xs">{String(t.open_date ?? '').slice(0, 10)}</td>
                      <td className={`py-2 px-2 text-xs ${t.exit_reason === 'liquidated' ? 'text-orange-400 font-bold' : 'text-slate-500'}`}>
                        {t.exit_reason === 'liquidated' ? '⚡ LIQUIDATED' : t.exit_reason}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            )}

            {tradeView === 'tv' && (
            <div className="overflow-x-auto max-h-[600px] overflow-y-auto">
              <table className="w-full text-sm">
                <thead className="sticky top-0 bg-[#1a2236]">
                  <tr className="text-slate-400 border-b border-[#2a3a52]">
                    <th className="text-left py-3 px-2">Trade #</th>
                    <th className="text-left py-3 px-2">Type</th>
                    <th className="text-left py-3 px-2">Date and time</th>
                    <th className="text-left py-3 px-2">Signal</th>
                    <th className="text-right py-3 px-2">Price USDT</th>
                    <th className="text-right py-3 px-2">Size (qty)</th>
                    <th className="text-right py-3 px-2">Size (value)</th>
                    <th className="text-right py-3 px-2">Net P&amp;L USDT</th>
                    <th className="text-right py-3 px-2">Net P&amp;L %</th>
                  </tr>
                </thead>
                <tbody>
                  {trades.map((t: any, i: number) => {
                    // TradingView shows Exit row above Entry row for each trade.
                    // Both rows share the trade's Net P&L (TV convention).
                    const dir       = String(t.direction || '').toLowerCase();
                    const posValue  = Number(t.margin ?? 0) * Number(t.leverage ?? 1);
                    const entryPx   = Number(t.open_rate ?? t.entry ?? 0);
                    const exitPx    = Number(t.close_rate ?? 0);
                    const qty       = entryPx > 0 ? posValue / entryPx : 0;
                    const pnlAbs    = Number(t.profit_abs ?? 0);
                    const pnlPct    = Number(t.profit_pct ?? 0);
                    // Map our exit_reason → TV-style signal label so users
                    // recognise "L-exit" / "S-exit" from their Pine code.
                    const exitSignal = (
                      t.exit_reason === 'liquidated' ? 'LIQ'
                      : t.exit_reason === 'stop_loss' ? (dir === 'long' ? 'L-exit' : 'S-exit')
                      : t.exit_reason === 'take_profit' ? (dir === 'long' ? 'L-exit' : 'S-exit')
                      : t.exit_reason === 'take_profit_1' ? (dir === 'long' ? 'L-tp1' : 'S-tp1')
                      : t.exit_reason === 'take_profit_2' ? (dir === 'long' ? 'L-tp2' : 'S-tp2')
                      : 'Open'  // forced close at end of window
                    );
                    const entrySignal = dir === 'long' ? 'L' : 'S';
                    const pnlClass    = pnlAbs >= 0 ? 'text-emerald-400' : 'text-red-400';
                    const isLiq       = t.exit_reason === 'liquidated';
                    const rowBg       = isLiq ? 'bg-orange-500/5' : '';
                    return (
                      <React.Fragment key={i}>
                        {/* Exit row first — matches TradingView's ordering */}
                        <tr className={`border-b border-[#2a3a52]/30 hover:bg-[#2a3a52]/20 ${rowBg}`}>
                          <td className="py-2 px-2 text-slate-500"
                              rowSpan={2}>
                            {i + 1}{' '}
                            <span className={`text-[10px] ${dir === 'long' ? 'text-emerald-400' : 'text-red-400'}`}>
                              {dir === 'long' ? 'Long' : 'Short'}
                            </span>
                          </td>
                          <td className="py-2 px-2 text-slate-300 text-xs">Exit {dir}</td>
                          <td className="py-2 px-2 text-slate-400 text-xs">
                            {String(t.close_date ?? '').replace('T', ' ').slice(0, 16)}
                          </td>
                          <td className={`py-2 px-2 text-xs ${isLiq ? 'text-orange-400 font-bold' : 'text-slate-400'}`}>
                            {exitSignal}
                          </td>
                          <td className="py-2 px-2 text-right font-mono text-xs">{exitPx.toFixed(2)}</td>
                          <td className="py-2 px-2 text-right font-mono text-xs text-slate-400" rowSpan={2}>{qty.toFixed(6)}</td>
                          <td className="py-2 px-2 text-right font-mono text-xs text-slate-400" rowSpan={2}>{posValue.toFixed(4)}</td>
                          <td className={`py-2 px-2 text-right font-mono text-xs ${pnlClass}`} rowSpan={2}>
                            {pnlAbs >= 0 ? '+' : ''}{pnlAbs.toFixed(2)}
                          </td>
                          <td className={`py-2 px-2 text-right font-mono text-xs ${pnlClass}`} rowSpan={2}>
                            {pnlPct >= 0 ? '+' : ''}{pnlPct.toFixed(2)}%
                          </td>
                        </tr>
                        {/* Entry row */}
                        <tr className={`border-b border-[#2a3a52] hover:bg-[#2a3a52]/20 ${rowBg}`}>
                          <td className="py-2 px-2 text-slate-300 text-xs">Entry {dir}</td>
                          <td className="py-2 px-2 text-slate-400 text-xs">
                            {String(t.open_date ?? '').replace('T', ' ').slice(0, 16)}
                          </td>
                          <td className="py-2 px-2 text-slate-400 text-xs">{entrySignal}</td>
                          <td className="py-2 px-2 text-right font-mono text-xs">{entryPx.toFixed(2)}</td>
                        </tr>
                      </React.Fragment>
                    );
                  })}
                </tbody>
              </table>
            </div>
            )}
          </div>
        </>
      )}

      {/* Past runs history (shown even before first run) */}
      {history.length > 0 && (
        <div className="card">
          <h2 className="text-lg font-semibold mb-4">🕐 Previous Futures Backtest Runs</h2>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-slate-400 border-b border-[#2a3a52]">
                  <th className="text-left py-2 px-2">Strategy</th>
                  <th className="text-right py-2 px-2">Period</th>
                  <th className="text-right py-2 px-2">Leverage</th>
                  <th className="text-right py-2 px-2">P&L%</th>
                  <th className="text-right py-2 px-2">Win Rate</th>
                  <th className="text-right py-2 px-2">Trades</th>
                  <th className="text-right py-2 px-2">⚡ Liq.</th>
                  <th className="text-right py-2 px-2">Max DD</th>
                  <th className="text-left py-2 px-2">Date</th>
                </tr>
              </thead>
              <tbody>
                {history.map((h: any) => (
                  <tr key={h.id} className="border-b border-[#2a3a52]/50 hover:bg-[#2a3a52]/20">
                    <td className="py-2 px-2 font-medium text-xs">{h.strategy_name} — {h.pairs}</td>
                    <td className="py-2 px-2 text-right text-xs text-slate-400">{h.timerange}</td>
                    <td className="py-2 px-2 text-right text-blue-400 font-bold text-xs">{h.leverage}x</td>
                    <td className={`py-2 px-2 text-right font-semibold ${(h.total_profit_pct ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                      {(h.total_profit_pct ?? 0) >= 0 ? '+' : ''}{(h.total_profit_pct ?? 0).toFixed(2)}%
                    </td>
                    <td className="py-2 px-2 text-right text-xs">{((h.win_rate ?? 0) * 100).toFixed(1)}%</td>
                    <td className="py-2 px-2 text-right text-xs">{h.total_trades}</td>
                    <td className={`py-2 px-2 text-right text-xs font-bold ${(h.liquidations ?? 0) > 0 ? 'text-orange-400' : 'text-slate-500'}`}>
                      {h.liquidations ?? 0}
                    </td>
                    <td className="py-2 px-2 text-right text-xs text-amber-400">-{(h.max_drawdown ?? 0).toFixed(1)}%</td>
                    <td className="py-2 px-2 text-xs text-slate-400">{String(h.created_at).slice(0, 10)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

export default function FuturesBacktestPage() {
  return <Suspense><FuturesBacktestInner /></Suspense>;
}
