'use client';

import { useEffect, useRef, useState, useCallback } from 'react';
import {
  createChart, ColorType, CrosshairMode,
  CandlestickData, Time, IChartApi, ISeriesApi,
  HistogramData,
} from 'lightweight-charts';
import { api } from '@/lib/api';

interface Props {
  pair: string;          // e.g. "BTC/USDT"
  defaultInterval?: string; // e.g. "15m"
}

const TIMEFRAMES = [
  { label: '1m',  value: '1m',   limit: 300 },
  { label: '5m',  value: '5m',   limit: 300 },
  { label: '15m', value: '15m',  limit: 300 },
  { label: '30m', value: '30m',  limit: 300 },
  { label: '1h',  value: '1h',   limit: 300 },
  { label: '4h',  value: '4h',   limit: 300 },
  { label: '1d',  value: '1d',   limit: 300 },
];

// ── Indicator helpers ────────────────────────────────────────────────────────

function calcSMA(data: number[], period: number): (number | null)[] {
  return data.map((_, i) => {
    if (i < period - 1) return null;
    const sum = data.slice(i - period + 1, i + 1).reduce((a, b) => a + b, 0);
    return sum / period;
  });
}

function calcEMA(data: number[], period: number): (number | null)[] {
  const result: (number | null)[] = new Array(data.length).fill(null);
  const k = 2 / (period + 1);
  let ema: number | null = null;
  for (let i = 0; i < data.length; i++) {
    if (i < period - 1) continue;
    if (ema === null) {
      ema = data.slice(0, period).reduce((a, b) => a + b, 0) / period;
    } else {
      ema = data[i] * k + ema * (1 - k);
    }
    result[i] = ema;
  }
  return result;
}

function calcBB(closes: number[], period = 20, mult = 2) {
  const mid = calcSMA(closes, period);
  const upper: (number | null)[] = [];
  const lower: (number | null)[] = [];
  for (let i = 0; i < closes.length; i++) {
    if (mid[i] === null) { upper.push(null); lower.push(null); continue; }
    const slice = closes.slice(Math.max(0, i - period + 1), i + 1);
    const mean = mid[i]!;
    const variance = slice.reduce((acc, v) => acc + (v - mean) ** 2, 0) / slice.length;
    const std = Math.sqrt(variance);
    upper.push(mean + mult * std);
    lower.push(mean - mult * std);
  }
  return { mid, upper, lower };
}

function calcRSI(closes: number[], period = 14): (number | null)[] {
  const result: (number | null)[] = new Array(closes.length).fill(null);
  if (closes.length < period + 1) return result;
  let gains = 0, losses = 0;
  for (let i = 1; i <= period; i++) {
    const diff = closes[i] - closes[i - 1];
    if (diff > 0) gains += diff; else losses -= diff;
  }
  let avgGain = gains / period;
  let avgLoss = losses / period;
  result[period] = 100 - 100 / (1 + (avgLoss === 0 ? Infinity : avgGain / avgLoss));
  for (let i = period + 1; i < closes.length; i++) {
    const diff = closes[i] - closes[i - 1];
    avgGain = (avgGain * (period - 1) + Math.max(diff, 0)) / period;
    avgLoss = (avgLoss * (period - 1) + Math.max(-diff, 0)) / period;
    result[i] = 100 - 100 / (1 + (avgLoss === 0 ? Infinity : avgGain / avgLoss));
  }
  return result;
}

function calcMACD(closes: number[], fast = 12, slow = 26, signal = 9) {
  const emaFast = calcEMA(closes, fast);
  const emaSlow = calcEMA(closes, slow);
  const macdLine = closes.map((_, i) =>
    emaFast[i] !== null && emaSlow[i] !== null ? emaFast[i]! - emaSlow[i]! : null
  );
  const validMacd = macdLine.filter(v => v !== null) as number[];
  const rawSignal = calcEMA(validMacd, signal);
  // Re-align signal with macdLine
  const signalLine: (number | null)[] = new Array(closes.length).fill(null);
  let sigIdx = 0;
  for (let i = 0; i < macdLine.length; i++) {
    if (macdLine[i] !== null) {
      signalLine[i] = rawSignal[sigIdx++] ?? null;
    }
  }
  const histogram = closes.map((_, i) =>
    macdLine[i] !== null && signalLine[i] !== null ? macdLine[i]! - signalLine[i]! : null
  );
  return { macdLine, signalLine, histogram };
}

// ── Component ────────────────────────────────────────────────────────────────

export default function KuCoinFuturesChart({ pair, defaultInterval = '15m' }: Props) {
  const wrapperRef    = useRef<HTMLDivElement>(null);
  const mainRef       = useRef<HTMLDivElement>(null);
  const rsiRef        = useRef<HTMLDivElement>(null);
  const macdRef       = useRef<HTMLDivElement>(null);

  const mainChart     = useRef<IChartApi | null>(null);
  const rsiChart      = useRef<IChartApi | null>(null);
  const macdChart     = useRef<IChartApi | null>(null);
  const candleSeries  = useRef<ISeriesApi<'Candlestick'> | null>(null);
  const volSeries     = useRef<ISeriesApi<'Histogram'> | null>(null);
  const bbMidSeries   = useRef<ISeriesApi<'Line'> | null>(null);
  const bbUpSeries    = useRef<ISeriesApi<'Line'> | null>(null);
  const bbLowSeries   = useRef<ISeriesApi<'Line'> | null>(null);
  const rsiSeries     = useRef<ISeriesApi<'Line'> | null>(null);
  const macdLineSeries= useRef<ISeriesApi<'Line'> | null>(null);
  const macdSigSeries = useRef<ISeriesApi<'Line'> | null>(null);
  const macdHistSeries= useRef<ISeriesApi<'Histogram'> | null>(null);

  const [interval, setInterval]   = useState(defaultInterval);
  const [loading, setLoading]     = useState(true);
  const [lastBar, setLastBar]     = useState<{ close: number; change: number; pct: number } | null>(null);
  const [showBB, setShowBB]       = useState(true);
  const [showRSI, setShowRSI]     = useState(true);
  const [showMACD, setShowMACD]   = useState(true);
  const [error, setError]         = useState('');

  const CHART_BG = '#0d1117';
  const TEXT_COLOR = '#64748b';
  const GRID_COLOR = 'rgba(255,255,255,0.03)';

  const commonOpts = useCallback(() => ({
    layout:      { background: { type: ColorType.Solid as const, color: CHART_BG }, textColor: TEXT_COLOR },
    grid:        { vertLines: { color: GRID_COLOR }, horzLines: { color: GRID_COLOR } },
    crosshair:   { mode: CrosshairMode.Normal },
    rightPriceScale: { borderColor: 'rgba(255,255,255,0.06)', scaleMargins: { top: 0.05, bottom: 0.1 } },
    timeScale:   { borderColor: 'rgba(255,255,255,0.06)', timeVisible: true, secondsVisible: false },
    handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true },
    handleScale:  { mouseWheel: true, pinch: true },
  }), []);

  // ── Build charts once ──────────────────────────────────────────────────────
  useEffect(() => {
    if (!mainRef.current) return;

    const mc = createChart(mainRef.current, {
      ...commonOpts(),
      width: mainRef.current.clientWidth,
      height: mainRef.current.clientHeight || 380,
    });
    mainChart.current = mc;

    candleSeries.current = mc.addCandlestickSeries({
      upColor: '#26a69a', downColor: '#ef5350',
      borderUpColor: '#26a69a', borderDownColor: '#ef5350',
      wickUpColor: '#26a69a', wickDownColor: '#ef5350',
    });

    // Volume on price scale 'vol'
    volSeries.current = mc.addHistogramSeries({
      priceFormat: { type: 'volume' },
      priceScaleId: 'vol',
    });
    mc.priceScale('vol').applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });

    // BB overlays
    const lineOpts = { lineWidth: 1 as const, priceLineVisible: false, lastValueVisible: false, crossHairMarkerVisible: false };
    bbMidSeries.current  = mc.addLineSeries({ ...lineOpts, color: 'rgba(255,165,0,0.7)' });
    bbUpSeries.current   = mc.addLineSeries({ ...lineOpts, color: 'rgba(100,180,255,0.5)' });
    bbLowSeries.current  = mc.addLineSeries({ ...lineOpts, color: 'rgba(100,180,255,0.5)' });

    // RSI chart
    if (rsiRef.current) {
      const rc = createChart(rsiRef.current, {
        ...commonOpts(),
        width: rsiRef.current.clientWidth,
        height: rsiRef.current.clientHeight || 80,
        rightPriceScale: { ...commonOpts().rightPriceScale, scaleMargins: { top: 0.1, bottom: 0.1 } },
      });
      rsiChart.current = rc;
      rc.timeScale().applyOptions({ visible: false });
      rsiSeries.current = rc.addLineSeries({ color: '#b39ddb', lineWidth: 1, priceLineVisible: false, lastValueVisible: true });
    }

    // MACD chart
    if (macdRef.current) {
      const mc2 = createChart(macdRef.current, {
        ...commonOpts(),
        width: macdRef.current.clientWidth,
        height: macdRef.current.clientHeight || 80,
        rightPriceScale: { ...commonOpts().rightPriceScale, scaleMargins: { top: 0.1, bottom: 0.1 } },
      });
      macdChart.current = mc2;
      mc2.timeScale().applyOptions({ visible: true });
      macdLineSeries.current = mc2.addLineSeries({ color: '#42a5f5', lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
      macdSigSeries.current  = mc2.addLineSeries({ color: '#ef9a9a', lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
      macdHistSeries.current = mc2.addHistogramSeries({
        priceScaleId: 'macd_hist',
        priceFormat: { type: 'price', precision: 4 },
      });
      mc2.priceScale('macd_hist').applyOptions({ scaleMargins: { top: 0.7, bottom: 0 } });
    }

    // Sync time scales between charts
    const syncCrossHair = (src: IChartApi, targets: IChartApi[]) => {
      src.timeScale().subscribeVisibleLogicalRangeChange(range => {
        if (!range) return;
        targets.forEach(t => t.timeScale().setVisibleLogicalRange(range));
      });
    };
    if (rsiChart.current && macdChart.current) {
      syncCrossHair(mc, [rsiChart.current, macdChart.current]);
      syncCrossHair(rsiChart.current, [mc, macdChart.current]);
      syncCrossHair(macdChart.current, [mc, rsiChart.current]);
    }

    // Resize observer
    const ro = new ResizeObserver(() => {
      if (mainRef.current) mc.applyOptions({ width: mainRef.current.clientWidth });
      if (rsiRef.current && rsiChart.current) rsiChart.current.applyOptions({ width: rsiRef.current.clientWidth });
      if (macdRef.current && macdChart.current) macdChart.current.applyOptions({ width: macdRef.current.clientWidth });
    });
    if (wrapperRef.current) ro.observe(wrapperRef.current);

    return () => {
      ro.disconnect();
      mc.remove();
      rsiChart.current?.remove();
      macdChart.current?.remove();
      mainChart.current = rsiChart.current = macdChart.current = null;
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Fetch and render data ──────────────────────────────────────────────────
  const loadData = useCallback(async () => {
    if (!candleSeries.current) return;
    setLoading(true);
    setError('');
    try {
      const data = await api.market.ohlcv(pair, interval, 300);
      const candles = data.candles ?? [];
      if (!candles.length) { setError('No data'); setLoading(false); return; }

      const sorted = [...candles].sort((a, b) => a.time - b.time);
      const times  = sorted.map(c => c.time as Time);
      const opens  = sorted.map(c => c.open);
      const highs  = sorted.map(c => c.high);
      const lows   = sorted.map(c => c.low);
      const closes = sorted.map(c => c.close);
      const vols   = sorted.map(c => c.volume);

      // Candlestick data
      const cdData: CandlestickData[] = sorted.map((c, i) => ({
        time: times[i],
        open: opens[i], high: highs[i], low: lows[i], close: closes[i],
      }));
      candleSeries.current!.setData(cdData);

      // Volume
      const volData: HistogramData[] = sorted.map((c, i) => ({
        time: times[i],
        value: vols[i],
        color: closes[i] >= opens[i] ? 'rgba(38,166,154,0.35)' : 'rgba(239,83,80,0.35)',
      }));
      volSeries.current?.setData(volData);

      // Bollinger Bands
      const bb = calcBB(closes);
      if (showBB) {
        const toBBLine = (vals: (number | null)[]) => vals
          .map((v, i) => v !== null ? { time: times[i], value: v } : null)
          .filter(Boolean) as { time: Time; value: number }[];
        bbMidSeries.current?.setData(toBBLine(bb.mid));
        bbUpSeries.current?.setData(toBBLine(bb.upper));
        bbLowSeries.current?.setData(toBBLine(bb.lower));
      } else {
        bbMidSeries.current?.setData([]);
        bbUpSeries.current?.setData([]);
        bbLowSeries.current?.setData([]);
      }

      // RSI
      if (showRSI && rsiSeries.current) {
        const rsi = calcRSI(closes);
        const rsiData = rsi
          .map((v, i) => v !== null ? { time: times[i], value: v } : null)
          .filter(Boolean) as { time: Time; value: number }[];
        rsiSeries.current.setData(rsiData);
      } else {
        rsiSeries.current?.setData([]);
      }

      // MACD
      if (showMACD && macdLineSeries.current) {
        const macd = calcMACD(closes);
        const toLine = (vals: (number | null)[]) => vals
          .map((v, i) => v !== null ? { time: times[i], value: v } : null)
          .filter(Boolean) as { time: Time; value: number }[];
        const toHist = (vals: (number | null)[]) => vals
          .map((v, i) => v !== null ? {
            time: times[i], value: v,
            color: v >= 0 ? 'rgba(38,166,154,0.6)' : 'rgba(239,83,80,0.6)',
          } : null)
          .filter(Boolean) as (HistogramData & { color: string })[];

        macdLineSeries.current.setData(toLine(macd.macdLine));
        macdSigSeries.current?.setData(toLine(macd.signalLine));
        macdHistSeries.current?.setData(toHist(macd.histogram));
      } else {
        macdLineSeries.current?.setData([]);
        macdSigSeries.current?.setData([]);
        macdHistSeries.current?.setData([]);
      }

      // Fit content
      mainChart.current?.timeScale().fitContent();

      // Update last bar stats
      const last = sorted[sorted.length - 1];
      const prev = sorted[sorted.length - 2];
      if (last && prev) {
        const change = last.close - prev.close;
        setLastBar({ close: last.close, change, pct: (change / prev.close) * 100 });
      }
    } catch (e) {
      setError('Failed to load chart data');
    }
    setLoading(false);
  }, [pair, interval, showBB, showRSI, showMACD]);

  useEffect(() => { loadData(); }, [loadData]);

  // Auto-refresh every 30s
  useEffect(() => {
    const t = setInterval(loadData, 30_000);
    return () => clearInterval(t);
  }, [loadData]);

  // ── Render ─────────────────────────────────────────────────────────────────
  const rsiHeight  = showRSI  ? 70  : 0;
  const macdHeight = showMACD ? 80  : 0;

  return (
    <div ref={wrapperRef} className="flex flex-col h-full bg-[#0d1117] select-none">
      {/* Toolbar */}
      <div className="flex items-center gap-1 px-2 py-1 border-b border-white/[0.06] bg-[#0d1117] shrink-0 flex-wrap">
        {/* Pair + price */}
        <div className="flex items-center gap-2 mr-2">
          <span className="text-white text-[11px] font-bold">
            {pair} <span className="text-slate-500 font-normal">· {interval} · KuCoin</span>
          </span>
          {lastBar && (
            <span className={`text-[11px] font-medium ${lastBar.pct >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
              {lastBar.close.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
              {' '}{lastBar.pct >= 0 ? '+' : ''}{lastBar.pct.toFixed(2)}%
            </span>
          )}
        </div>

        {/* Timeframe buttons */}
        <div className="flex items-center gap-0.5 bg-[#1a2236] rounded p-0.5">
          {TIMEFRAMES.map(tf => (
            <button
              key={tf.value}
              onClick={() => setInterval(tf.value)}
              className={`px-2 py-0.5 rounded text-[10px] font-medium transition-colors ${
                interval === tf.value
                  ? 'bg-emerald-500/30 text-emerald-300'
                  : 'text-slate-400 hover:text-white'
              }`}
            >
              {tf.label}
            </button>
          ))}
        </div>

        {/* Indicator toggles */}
        <div className="flex items-center gap-1 ml-1">
          {[
            { key: 'BB', state: showBB, toggle: () => setShowBB(v => !v), color: 'text-orange-400' },
            { key: 'RSI', state: showRSI, toggle: () => setShowRSI(v => !v), color: 'text-purple-400' },
            { key: 'MACD', state: showMACD, toggle: () => setShowMACD(v => !v), color: 'text-blue-400' },
          ].map(ind => (
            <button
              key={ind.key}
              onClick={ind.toggle}
              className={`px-1.5 py-0.5 rounded text-[9px] font-bold border transition-colors ${
                ind.state
                  ? `border-current ${ind.color} bg-current/10`
                  : 'border-white/10 text-slate-600'
              }`}
            >
              {ind.key}
            </button>
          ))}
        </div>

        {/* Refresh */}
        <button
          onClick={loadData}
          title="Refresh"
          className="ml-auto text-slate-500 hover:text-white text-[10px] px-1"
        >
          {loading ? '⏳' : '↻'}
        </button>
      </div>

      {/* Error */}
      {error && (
        <div className="text-red-400 text-[10px] text-center py-1">{error}</div>
      )}

      {/* Main candlestick chart */}
      <div
        ref={mainRef}
        style={{ flex: `1 1 0`, minHeight: 0 }}
        className="w-full"
      />

      {/* RSI pane */}
      {showRSI && (
        <div
          ref={rsiRef}
          style={{ height: `${rsiHeight}px` }}
          className="w-full border-t border-white/[0.04] shrink-0"
        />
      )}

      {/* MACD pane */}
      {showMACD && (
        <div
          ref={macdRef}
          style={{ height: `${macdHeight}px` }}
          className="w-full border-t border-white/[0.04] shrink-0"
        />
      )}
    </div>
  );
}
