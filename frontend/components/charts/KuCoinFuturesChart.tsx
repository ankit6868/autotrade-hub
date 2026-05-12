'use client';

import { useEffect, useRef, useState, useCallback } from 'react';
import {
  createChart, ColorType, CrosshairMode,
  CandlestickData, Time, IChartApi, ISeriesApi,
  HistogramData,
} from 'lightweight-charts';
import { api } from '@/lib/api';

interface Props {
  pair: string;
  defaultInterval?: string;
}

const TIMEFRAMES = [
  { label: '1m',  value: '1m'  },
  { label: '5m',  value: '5m'  },
  { label: '15m', value: '15m' },
  { label: '30m', value: '30m' },
  { label: '1h',  value: '1h'  },
  { label: '4h',  value: '4h'  },
  { label: '1d',  value: '1d'  },
];

// ── Indicator math ────────────────────────────────────────────────────────────
function calcSMA(data: number[], period: number): (number | null)[] {
  return data.map((_, i) => {
    if (i < period - 1) return null;
    return data.slice(i - period + 1, i + 1).reduce((a, b) => a + b, 0) / period;
  });
}

function calcEMA(data: number[], period: number): (number | null)[] {
  const result: (number | null)[] = new Array(data.length).fill(null);
  const k = 2 / (period + 1);
  let ema: number | null = null;
  for (let i = 0; i < data.length; i++) {
    if (i < period - 1) continue;
    ema = ema === null
      ? data.slice(0, period).reduce((a, b) => a + b, 0) / period
      : data[i] * k + ema * (1 - k);
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
    const mean  = mid[i]!;
    const std   = Math.sqrt(slice.reduce((a, v) => a + (v - mean) ** 2, 0) / slice.length);
    upper.push(mean + mult * std);
    lower.push(mean - mult * std);
  }
  return { mid, upper, lower };
}

function calcRSI(closes: number[], period = 14): (number | null)[] {
  const result: (number | null)[] = new Array(closes.length).fill(null);
  if (closes.length < period + 1) return result;
  let avgGain = 0, avgLoss = 0;
  for (let i = 1; i <= period; i++) {
    const d = closes[i] - closes[i - 1];
    if (d > 0) avgGain += d; else avgLoss -= d;
  }
  avgGain /= period; avgLoss /= period;
  result[period] = 100 - 100 / (1 + (avgLoss === 0 ? 1e9 : avgGain / avgLoss));
  for (let i = period + 1; i < closes.length; i++) {
    const d = closes[i] - closes[i - 1];
    avgGain = (avgGain * (period - 1) + Math.max(d, 0)) / period;
    avgLoss = (avgLoss * (period - 1) + Math.max(-d, 0)) / period;
    result[i] = 100 - 100 / (1 + (avgLoss === 0 ? 1e9 : avgGain / avgLoss));
  }
  return result;
}

function calcMACD(closes: number[], fast = 12, slow = 26, sig = 9) {
  const emaF = calcEMA(closes, fast);
  const emaS = calcEMA(closes, slow);
  const macd = closes.map((_, i) =>
    emaF[i] !== null && emaS[i] !== null ? emaF[i]! - emaS[i]! : null
  );
  const validMacd = macd.filter((v): v is number => v !== null);
  const rawSig    = calcEMA(validMacd, sig);
  const signal: (number | null)[] = new Array(closes.length).fill(null);
  let si = 0;
  for (let i = 0; i < macd.length; i++) {
    if (macd[i] !== null) signal[i] = rawSig[si++] ?? null;
  }
  const hist = closes.map((_, i) =>
    macd[i] !== null && signal[i] !== null ? macd[i]! - signal[i]! : null
  );
  return { macd, signal, hist };
}

function hideAttribution(container: HTMLElement) {
  const els = container.querySelectorAll('a[href*="tradingview"], div[class*="apply-common"]');
  els.forEach(el => (el as HTMLElement).style.display = 'none');
}

function chartOpts(bg = '#0d1117') {
  return {
    layout: {
      background: { type: ColorType.Solid as const, color: bg },
      textColor: '#64748b',
      fontFamily: "'Inter', -apple-system, system-ui, sans-serif",
      fontSize: 11,
    },
    grid: {
      vertLines: { color: 'rgba(255,255,255,0.03)' },
      horzLines: { color: 'rgba(255,255,255,0.03)' },
    },
    crosshair: {
      mode: CrosshairMode.Normal,
      vertLine: { color: 'rgba(100,180,255,0.3)', width: 1 as const, style: 2, labelBackgroundColor: '#1e293b' },
      horzLine: { color: 'rgba(100,180,255,0.3)', width: 1 as const, style: 2, labelBackgroundColor: '#1e293b' },
    },
    watermark: { visible: false },
    handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
    handleScale: { mouseWheel: true, pinch: true, axisPressedMouseMove: { time: true, price: true } },
    rightPriceScale: {
      borderColor: 'rgba(255,255,255,0.06)',
      scaleMargins: { top: 0.05, bottom: 0.1 },
      entireTextOnly: true,
    },
    timeScale: {
      borderColor: 'rgba(255,255,255,0.06)',
      timeVisible: true,
      secondsVisible: false,
      rightOffset: 8,
      barSpacing: 8,
      minBarSpacing: 2,
    },
  };
}

// ── Component ─────────────────────────────────────────────────────────────────
export default function KuCoinFuturesChart({ pair, defaultInterval = '15m' }: Props) {
  const wrapperRef = useRef<HTMLDivElement>(null);
  const mainRef    = useRef<HTMLDivElement>(null);
  const rsiRef     = useRef<HTMLDivElement>(null);
  const macdRef    = useRef<HTMLDivElement>(null);

  const chartMain  = useRef<IChartApi | null>(null);
  const chartRSI   = useRef<IChartApi | null>(null);
  const chartMACD  = useRef<IChartApi | null>(null);

  const serCandle  = useRef<ISeriesApi<'Candlestick'> | null>(null);
  const serVol     = useRef<ISeriesApi<'Histogram'>   | null>(null);
  const serBBMid   = useRef<ISeriesApi<'Line'>        | null>(null);
  const serBBUp    = useRef<ISeriesApi<'Line'>        | null>(null);
  const serBBLow   = useRef<ISeriesApi<'Line'>        | null>(null);
  const serRSI     = useRef<ISeriesApi<'Line'>        | null>(null);
  const serMACDL   = useRef<ISeriesApi<'Line'>        | null>(null);
  const serMACDS   = useRef<ISeriesApi<'Line'>        | null>(null);
  const serMACDH   = useRef<ISeriesApi<'Histogram'>   | null>(null);

  const [tf, setTf]             = useState(defaultInterval);
  const [loading, setLoading]   = useState(true);
  const [lastBar, setLastBar]   = useState<{ o: number; h: number; l: number; c: number; v: number; pct: number } | null>(null);
  const [showBB, setShowBB]     = useState(true);
  const [showRSI, setShowRSI]   = useState(true);
  const [showMACD, setShowMACD] = useState(true);
  const [error, setError]       = useState('');
  const [isMobile, setIsMobile] = useState(false);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [candleCount, setCandleCount] = useState(300);

  useEffect(() => {
    const check = () => setIsMobile(window.innerWidth < 768);
    check();
    window.addEventListener('resize', check);
    return () => window.removeEventListener('resize', check);
  }, []);

  // Hide TV attribution after charts render
  const hideAllAttribution = useCallback(() => {
    if (wrapperRef.current) {
      hideAttribution(wrapperRef.current);
    }
  }, []);

  // ── Build charts once ──────────────────────────────────────────────────────
  useEffect(() => {
    if (!mainRef.current) return;
    const BG = '#0d1117';

    const mc = createChart(mainRef.current, {
      ...chartOpts(BG),
      autoSize: true,
    });
    chartMain.current = mc;

    serCandle.current = mc.addCandlestickSeries({
      upColor: '#26a69a', downColor: '#ef5350',
      borderUpColor: '#26a69a', borderDownColor: '#ef5350',
      wickUpColor: '#26a69a', wickDownColor: '#ef5350',
    });

    serVol.current = mc.addHistogramSeries({
      priceFormat: { type: 'volume' },
      priceScaleId: 'vol',
      lastValueVisible: false,
      priceLineVisible: false,
    });
    mc.priceScale('vol').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });

    const lineBase = { lineWidth: 1 as const, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false };
    serBBMid.current = mc.addLineSeries({ ...lineBase, color: 'rgba(255,165,0,0.65)' });
    serBBUp.current  = mc.addLineSeries({ ...lineBase, color: 'rgba(100,180,255,0.45)' });
    serBBLow.current = mc.addLineSeries({ ...lineBase, color: 'rgba(100,180,255,0.45)' });

    if (rsiRef.current) {
      const rc = createChart(rsiRef.current, {
        ...chartOpts(BG),
        autoSize: true,
        rightPriceScale: { ...chartOpts().rightPriceScale, scaleMargins: { top: 0.1, bottom: 0.1 } },
      });
      chartRSI.current = rc;
      rc.timeScale().applyOptions({ visible: false });
      serRSI.current = rc.addLineSeries({
        color: '#b39ddb', lineWidth: 1,
        priceLineVisible: false, lastValueVisible: true,
        crosshairMarkerVisible: true, crosshairMarkerRadius: 3,
      });
    }

    if (macdRef.current) {
      const mc2 = createChart(macdRef.current, {
        ...chartOpts(BG),
        autoSize: true,
        rightPriceScale: { ...chartOpts().rightPriceScale, scaleMargins: { top: 0.05, bottom: 0.05 } },
      });
      chartMACD.current = mc2;
      mc2.timeScale().applyOptions({ visible: true });
      serMACDL.current = mc2.addLineSeries({ color: '#42a5f5', lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
      serMACDS.current = mc2.addLineSeries({ color: '#ef9a9a', lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
      serMACDH.current = mc2.addHistogramSeries({
        priceScaleId: 'macd_h',
        lastValueVisible: false, priceLineVisible: false,
      });
      mc2.priceScale('macd_h').applyOptions({ scaleMargins: { top: 0.7, bottom: 0 } });
    }

    const syncRange = (src: IChartApi, others: IChartApi[]) => {
      src.timeScale().subscribeVisibleLogicalRangeChange(r => {
        if (r) others.forEach(o => o.timeScale().setVisibleLogicalRange(r));
      });
    };
    const others = [chartRSI.current, chartMACD.current].filter(Boolean) as IChartApi[];
    syncRange(mc, others);
    if (chartRSI.current)  syncRange(chartRSI.current,  [mc, ...(chartMACD.current ? [chartMACD.current] : [])]);
    if (chartMACD.current) syncRange(chartMACD.current, [mc, ...(chartRSI.current  ? [chartRSI.current]  : [])]);

    // Hide attribution logos after a short delay to let the library render
    setTimeout(hideAllAttribution, 100);
    setTimeout(hideAllAttribution, 500);

    return () => {
      mc.remove(); chartRSI.current?.remove(); chartMACD.current?.remove();
      chartMain.current = chartRSI.current = chartMACD.current = null;
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Load & render data ─────────────────────────────────────────────────────
  const loadData = useCallback(async () => {
    if (!serCandle.current) return;
    setLoading(true); setError('');
    try {
      const data   = await api.market.ohlcv(pair, tf, candleCount);
      const raw    = (data.candles ?? []).sort((a: {time:number}, b: {time:number}) => a.time - b.time);
      if (!raw.length) { setError('No data'); setLoading(false); return; }

      const times  = raw.map((c: {time:number}) => c.time as Time);
      const opens  = raw.map((c: {open:number}) => c.open);
      const highs  = raw.map((c: {high:number}) => c.high);
      const lows   = raw.map((c: {low:number}) => c.low);
      const closes = raw.map((c: {close:number}) => c.close);
      const vols   = raw.map((c: {volume:number}) => c.volume);

      serCandle.current!.setData(
        raw.map((_: unknown, i: number) => ({
          time: times[i],
          open: opens[i], high: highs[i], low: lows[i], close: closes[i],
        }) as CandlestickData)
      );

      serVol.current?.setData(
        raw.map((_: unknown, i: number) => ({
          time: times[i], value: vols[i],
          color: closes[i] >= opens[i] ? 'rgba(38,166,154,0.35)' : 'rgba(239,83,80,0.35)',
        }) as HistogramData)
      );

      const toLine = (vals: (number | null)[]) =>
        vals.map((v, i) => v !== null ? { time: times[i], value: v } : null).filter(Boolean) as { time: Time; value: number }[];

      const bb = calcBB(closes);
      if (showBB) {
        serBBMid.current?.setData(toLine(bb.mid));
        serBBUp.current?.setData(toLine(bb.upper));
        serBBLow.current?.setData(toLine(bb.lower));
      } else {
        [serBBMid, serBBUp, serBBLow].forEach(s => s.current?.setData([]));
      }

      if (showRSI && serRSI.current) {
        serRSI.current.setData(toLine(calcRSI(closes)));
      } else serRSI.current?.setData([]);

      if (showMACD && serMACDL.current) {
        const { macd, signal, hist } = calcMACD(closes);
        serMACDL.current.setData(toLine(macd));
        serMACDS.current?.setData(toLine(signal));
        serMACDH.current?.setData(
          hist.map((v, i) => v !== null
            ? { time: times[i], value: v, color: v >= 0 ? 'rgba(38,166,154,0.7)' : 'rgba(239,83,80,0.7)' }
            : null
          ).filter(Boolean) as (HistogramData & { color: string })[]
        );
      } else {
        [serMACDL, serMACDS, serMACDH].forEach(s => s.current?.setData([]));
      }

      chartMain.current?.timeScale().fitContent();

      const last = raw[raw.length - 1];
      const prev = raw[raw.length - 2];
      if (last && prev) {
        setLastBar({
          o: last.open, h: last.high, l: last.low, c: last.close,
          v: last.volume,
          pct: (last.close - prev.close) / prev.close * 100,
        });
      }

      // Hide attribution after data load
      setTimeout(hideAllAttribution, 50);
    } catch { setError('Failed to load chart data'); }
    setLoading(false);
  }, [pair, tf, candleCount, showBB, showRSI, showMACD, hideAllAttribution]);

  useEffect(() => { loadData(); }, [loadData]);

  useEffect(() => {
    const t = window.setInterval(loadData, 30_000);
    return () => window.clearInterval(t);
  }, [loadData]);

  // ── Zoom controls ──────────────────────────────────────────────────────────
  const zoomIn = () => {
    const ts = chartMain.current?.timeScale();
    if (!ts) return;
    const range = ts.getVisibleLogicalRange();
    if (range) {
      const mid = (range.from + range.to) / 2;
      const span = (range.to - range.from) * 0.4;
      ts.setVisibleLogicalRange({ from: mid - span, to: mid + span });
    }
  };

  const zoomOut = () => {
    const ts = chartMain.current?.timeScale();
    if (!ts) return;
    const range = ts.getVisibleLogicalRange();
    if (range) {
      const mid = (range.from + range.to) / 2;
      const span = (range.to - range.from) * 0.75;
      ts.setVisibleLogicalRange({ from: mid - span, to: mid + span });
    }
  };

  const fitAll = () => {
    chartMain.current?.timeScale().fitContent();
  };

  const toggleFullscreen = () => {
    if (!wrapperRef.current) return;
    if (!document.fullscreenElement) {
      wrapperRef.current.requestFullscreen().then(() => setIsFullscreen(true)).catch(() => {});
    } else {
      document.exitFullscreen().then(() => setIsFullscreen(false)).catch(() => {});
    }
  };

  useEffect(() => {
    const handler = () => setIsFullscreen(!!document.fullscreenElement);
    document.addEventListener('fullscreenchange', handler);
    return () => document.removeEventListener('fullscreenchange', handler);
  }, []);

  // ── Layout ────────────────────────────────────────────────────────────────
  const rsiH  = showRSI  ? (isMobile ? 60 : 80)  : 0;
  const macdH = showMACD ? (isMobile ? 65 : 90)  : 0;

  const fmtPrice = (v: number) => v >= 1 ? v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
    : v.toPrecision(5);
  const fmtVol = (v: number) => v >= 1e9 ? (v / 1e9).toFixed(2) + 'B' : v >= 1e6 ? (v / 1e6).toFixed(2) + 'M' : v >= 1e3 ? (v / 1e3).toFixed(1) + 'K' : v.toFixed(0);

  return (
    <div ref={wrapperRef} className={`flex flex-col w-full h-full bg-[#0d1117] overflow-hidden ${isFullscreen ? 'fixed inset-0 z-50' : ''}`}>

      {/* ── Global style to hide TradingView attribution ── */}
      <style jsx global>{`
        .tv-lightweight-charts a[href*="tradingview"],
        div[class*="apply-common-tooltip"],
        a[target="_blank"][href*="tradingview.com"] {
          display: none !important;
          visibility: hidden !important;
          opacity: 0 !important;
          pointer-events: none !important;
          width: 0 !important;
          height: 0 !important;
          overflow: hidden !important;
        }
      `}</style>

      {/* ── Toolbar ── */}
      <div className="flex items-center flex-wrap gap-x-2 gap-y-1 px-2 py-1.5 border-b border-white/[0.05] bg-[#0d1117] shrink-0">

        {/* Pair + OHLCV info */}
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-white font-bold text-[11px] whitespace-nowrap">
            {pair}
            <span className="text-slate-500 font-normal ml-1">{tf}</span>
            <span className="text-slate-600 font-normal ml-1">· KuCoin Futures</span>
          </span>
          {lastBar && (
            <div className="flex items-center gap-2 text-[10px] whitespace-nowrap">
              <span className={`font-semibold ${lastBar.pct >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                {fmtPrice(lastBar.c)}
              </span>
              <span className={`${lastBar.pct >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                {lastBar.pct >= 0 ? '+' : ''}{lastBar.pct.toFixed(2)}%
              </span>
              <span className="text-slate-500 hidden md:inline">
                O <span className="text-slate-400">{fmtPrice(lastBar.o)}</span>{' '}
                H <span className="text-slate-400">{fmtPrice(lastBar.h)}</span>{' '}
                L <span className="text-slate-400">{fmtPrice(lastBar.l)}</span>{' '}
                V <span className="text-slate-400">{fmtVol(lastBar.v)}</span>
              </span>
            </div>
          )}
        </div>

        {/* Timeframe row */}
        <div className="flex items-center bg-[#161b27] rounded overflow-hidden shrink-0 ml-auto sm:ml-0">
          {TIMEFRAMES.map(t => (
            <button
              key={t.value}
              onClick={() => setTf(t.value)}
              className={`px-2 sm:px-2.5 py-1 text-[10px] sm:text-[11px] font-medium transition-colors ${
                tf === t.value
                  ? 'bg-emerald-500/25 text-emerald-300'
                  : 'text-slate-400 hover:text-white hover:bg-white/5'
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>

        {/* Candle count selector */}
        <div className="flex items-center bg-[#161b27] rounded overflow-hidden shrink-0 hidden sm:flex">
          {[100, 200, 300, 500].map(n => (
            <button
              key={n}
              onClick={() => setCandleCount(n)}
              className={`px-1.5 py-1 text-[10px] font-medium transition-colors ${
                candleCount === n
                  ? 'bg-slate-600/30 text-slate-200'
                  : 'text-slate-500 hover:text-slate-300'
              }`}
            >
              {n}
            </button>
          ))}
        </div>

        {/* Indicator toggles */}
        <div className="flex items-center gap-1 shrink-0">
          {[
            { k: 'BB',   on: showBB,   set: setShowBB,   cls: 'text-orange-400 border-orange-400/50 bg-orange-400/10' },
            { k: 'RSI',  on: showRSI,  set: setShowRSI,  cls: 'text-purple-400 border-purple-400/50 bg-purple-400/10' },
            { k: 'MACD', on: showMACD, set: setShowMACD, cls: 'text-blue-400   border-blue-400/50   bg-blue-400/10'   },
          ].map(ind => (
            <button
              key={ind.k}
              onClick={() => ind.set(v => !v)}
              className={`px-1.5 py-0.5 rounded text-[9px] sm:text-[10px] font-bold border transition-all ${
                ind.on ? ind.cls : 'border-white/10 text-slate-600 hover:text-slate-400'
              }`}
            >
              {ind.k}
            </button>
          ))}
        </div>

        {/* Zoom + Fullscreen + Refresh */}
        <div className="flex items-center gap-0.5 shrink-0 ml-auto">
          <button onClick={zoomIn} className="text-slate-500 hover:text-white text-[14px] w-6 h-6 flex items-center justify-center rounded hover:bg-white/5 transition-colors" title="Zoom In">+</button>
          <button onClick={zoomOut} className="text-slate-500 hover:text-white text-[14px] w-6 h-6 flex items-center justify-center rounded hover:bg-white/5 transition-colors" title="Zoom Out">−</button>
          <button onClick={fitAll} className="text-slate-500 hover:text-white text-[11px] w-6 h-6 flex items-center justify-center rounded hover:bg-white/5 transition-colors" title="Fit All">⊞</button>
          <button onClick={toggleFullscreen} className="text-slate-500 hover:text-white text-[11px] w-6 h-6 flex items-center justify-center rounded hover:bg-white/5 transition-colors" title="Fullscreen">
            {isFullscreen ? '⊟' : '⛶'}
          </button>
          <button
            onClick={loadData}
            className="text-slate-500 hover:text-white text-[12px] w-6 h-6 flex items-center justify-center rounded hover:bg-white/5 transition-colors"
            title="Refresh"
          >
            {loading ? (
              <span className="animate-spin inline-block text-[10px]">⟳</span>
            ) : '⟳'}
          </button>
        </div>
      </div>

      {/* Error bar */}
      {error && (
        <div className="text-red-400 text-[10px] text-center py-0.5 bg-red-500/10 shrink-0">{error}</div>
      )}

      {/* ── Chart panes ── */}
      <div className="flex-1 flex flex-col min-h-0 overflow-hidden">

        {/* Main candle chart */}
        <div ref={mainRef} className="flex-1 min-h-0 w-full" />

        {/* RSI pane */}
        {showRSI && (
          <div className="shrink-0 border-t border-white/[0.04] relative">
            <span className="absolute left-2 top-0.5 text-[9px] text-purple-400/60 font-medium z-10 pointer-events-none">RSI(14)</span>
            <div ref={rsiRef} style={{ height: rsiH }} className="w-full" />
          </div>
        )}

        {/* MACD pane */}
        {showMACD && (
          <div className="shrink-0 border-t border-white/[0.04] relative">
            <span className="absolute left-2 top-0.5 text-[9px] text-blue-400/60 font-medium z-10 pointer-events-none">MACD(12,26,9)</span>
            <div ref={macdRef} style={{ height: macdH }} className="w-full" />
          </div>
        )}
      </div>
    </div>
  );
}
