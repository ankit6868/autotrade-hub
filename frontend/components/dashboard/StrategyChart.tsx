'use client';
/**
 * StrategyChart — candlestick chart with strategy entry/exit markers.
 *
 * Shows:
 *  • OHLCV candlestick chart (lightweight-charts)
 *  • 🟢 Green up-arrow markers  = bot BUY entries
 *  • 🔴 Red down-arrow markers  = bot SELL exits (TP = emerald, SL = red)
 *  • Horizontal SL line (red dashed) for open position
 *  • Horizontal TP line (green dashed) for open position
 *  • Volume histogram at bottom
 *  • Auto-refreshes every 30 s
 */
import { useEffect, useRef, useCallback, useState } from 'react';
import {
  createChart,
  ColorType,
  CrosshairMode,
  LineStyle,
  type IChartApi,
  type ISeriesApi,
  type CandlestickData,
  type Time,
} from 'lightweight-charts';
import { api } from '@/lib/api';

interface Trade {
  id: string | number;
  pair: string;
  entry_price: number;
  exit_price?: number;
  entry_time: string;
  exit_time?: string;
  exit_reason?: string;
  profit_abs?: number;
  profit_pct?: number;
  status?: string;
}

interface OpenPosition {
  pair: string;
  entry_price: number;
  stoploss_price?: number;
  tp_price?: number;
  unrealized_pnl?: number;
}

interface Props {
  pair: string;
  timeframe?: string;
  mode?: 'paper' | 'live';
  height?: number;
}

export default function StrategyChart({ pair, timeframe = '15m', mode = 'paper', height = 420 }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef     = useRef<IChartApi | null>(null);
  const candleRef    = useRef<ISeriesApi<'Candlestick'> | null>(null);
  const volRef       = useRef<ISeriesApi<'Histogram'> | null>(null);
  const slLineRef    = useRef<ISeriesApi<'Line'> | null>(null);
  const tpLineRef    = useRef<ISeriesApi<'Line'> | null>(null);

  const [status, setStatus] = useState<'loading' | 'ok' | 'error'>('loading');
  const [lastUpdate, setLastUpdate] = useState<string>('');
  const [openPos, setOpenPos] = useState<OpenPosition | null>(null);
  const [stats, setStats] = useState({ trades: 0, wins: 0, pnl: 0 });

  // ── build / rebuild the chart once the container is mounted ───────────
  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: '#0a0f1c' },
        textColor: '#94a3b8',
        fontSize: 11,
      },
      grid: {
        vertLines: { color: '#1a2236' },
        horzLines: { color: '#1a2236' },
      },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: '#2a3a52' },
      timeScale: {
        borderColor: '#2a3a52',
        timeVisible: true,
        secondsVisible: false,
      },
      width:  containerRef.current.clientWidth,
      height: height - 40,
    });

    const candleSeries = chart.addCandlestickSeries({
      upColor:          '#10b981',
      downColor:        '#ef4444',
      borderUpColor:    '#10b981',
      borderDownColor:  '#ef4444',
      wickUpColor:      '#10b981',
      wickDownColor:    '#ef4444',
    });

    const volSeries = chart.addHistogramSeries({
      color:      '#2a3a52',
      priceFormat: { type: 'volume' },
      priceScaleId: 'vol',
    });
    chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });

    // SL line (red dashed) — only shown when position open
    const slLine = chart.addLineSeries({
      color:     '#ef4444',
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      lastValueVisible: true,
      priceLineVisible: false,
      title: 'SL',
    });

    // TP line (emerald dashed)
    const tpLine = chart.addLineSeries({
      color:     '#10b981',
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      lastValueVisible: true,
      priceLineVisible: false,
      title: 'TP',
    });

    chartRef.current  = chart;
    candleRef.current = candleSeries;
    volRef.current    = volSeries;
    slLineRef.current = slLine;
    tpLineRef.current = tpLine;

    // Responsive resize
    const ro = new ResizeObserver(() => {
      if (containerRef.current) chart.applyOptions({ width: containerRef.current.clientWidth });
    });
    ro.observe(containerRef.current);

    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current  = null;
      candleRef.current = null;
    };
  }, [height]);

  // ── fetch + render data ────────────────────────────────────────────────
  const refresh = useCallback(async () => {
    if (!candleRef.current || !chartRef.current) return;
    try {
      const [ohlcvRes, histRes, openRes] = await Promise.all([
        api.market.ohlcv(pair, timeframe, 150),
        api.trade.history({ mode, limit: '200' }) as Promise<{ trades: Trade[] }>,
        api.trade.open(mode as 'paper' | 'live')  as Promise<{ trades: OpenPosition[] }>,
      ]);

      const candles = ohlcvRes.candles || [];
      if (!candles.length) { setStatus('error'); return; }

      // ── candlestick data ───────────────────────────────────────────────
      const candleData: CandlestickData[] = candles.map(c => ({
        time:  c.time as Time,
        open:  c.open,
        high:  c.high,
        low:   c.low,
        close: c.close,
      }));
      candleRef.current.setData(candleData);

      // ── volume ─────────────────────────────────────────────────────────
      volRef.current?.setData(candles.map(c => ({
        time:  c.time as Time,
        value: c.volume,
        color: c.close >= c.open ? '#10b98140' : '#ef444440',
      })));

      // ── trade markers ──────────────────────────────────────────────────
      const trades: Trade[] = (histRes.trades || []).filter(t => t.pair === pair);
      const markers: any[]  = [];
      let wins = 0, totalPnl = 0;

      trades.forEach(t => {
        const entryTs = Math.floor(new Date(t.entry_time).getTime() / 1000);
        markers.push({
          time:     entryTs as Time,
          position: 'belowBar',
          color:    '#10b981',
          shape:    'arrowUp',
          text:     `BUY ${Number(t.entry_price).toFixed(1)}`,
          size:     1,
        });
        if (t.exit_time && t.exit_price) {
          const exitTs  = Math.floor(new Date(t.exit_time).getTime() / 1000);
          const isProfit = (t.profit_abs ?? 0) >= 0;
          if (isProfit) wins++;
          totalPnl += t.profit_abs ?? 0;
          markers.push({
            time:     exitTs as Time,
            position: 'aboveBar',
            color:    isProfit ? '#10b981' : '#ef4444',
            shape:    'arrowDown',
            text:     `${isProfit ? '✓TP' : '✗SL'} ${Number(t.exit_price).toFixed(1)}`,
            size:     1,
          });
        }
      });

      // Sort markers by time (required by lightweight-charts)
      markers.sort((a, b) => (a.time as number) - (b.time as number));
      candleRef.current.setMarkers(markers);

      setStats({ trades: trades.length, wins, pnl: totalPnl });

      // ── SL / TP lines for open position ───────────────────────────────
      const openForPair = (openRes.trades || []).find((t: any) => t.pair === pair);
      setOpenPos(openForPair || null);

      if (openForPair && candles.length > 0) {
        const firstT = candles[0].time as number;
        const lastT  = candles[candles.length - 1].time as number + 900; // +1 candle ahead
        const sl     = Number(openForPair.stoploss_price);
        const tp     = Number((openForPair as any).tp_price || 0);

        if (sl > 0) {
          slLineRef.current?.setData([
            { time: firstT as Time, value: sl },
            { time: lastT  as Time, value: sl },
          ]);
        } else {
          slLineRef.current?.setData([]);
        }
        if (tp > 0) {
          tpLineRef.current?.setData([
            { time: firstT as Time, value: tp },
            { time: lastT  as Time, value: tp },
          ]);
        } else {
          tpLineRef.current?.setData([]);
        }
      } else {
        slLineRef.current?.setData([]);
        tpLineRef.current?.setData([]);
      }

      setStatus('ok');
      setLastUpdate(new Date().toLocaleTimeString());
    } catch {
      setStatus('error');
    }
  }, [pair, timeframe, mode]);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 30_000);
    return () => clearInterval(t);
  }, [refresh]);

  const winRate = stats.trades > 0 ? Math.round((stats.wins / stats.trades) * 100) : 0;

  return (
    <div className="card mb-6">
      {/* Header */}
      <div className="flex items-center justify-between mb-3">
        <div>
          <h2 className="font-semibold text-base flex items-center gap-2">
            📈 Strategy Analytics Chart
          </h2>
          <p className="text-xs text-slate-400 mt-0.5">
            {pair} · {timeframe} · entry/exit markers · auto-refreshes every 30s
          </p>
        </div>
        <div className="flex items-center gap-3">
          {/* Mini stats */}
          <div className="hidden sm:flex gap-3 text-xs">
            <span className="px-2 py-1 rounded bg-[#1a2236] border border-[#2a3a52]">
              Trades: <span className="text-white font-semibold">{stats.trades}</span>
            </span>
            <span className="px-2 py-1 rounded bg-[#1a2236] border border-[#2a3a52]">
              Win rate: <span className={`font-semibold ${winRate >= 50 ? 'text-emerald-400' : 'text-red-400'}`}>{winRate}%</span>
            </span>
            <span className="px-2 py-1 rounded bg-[#1a2236] border border-[#2a3a52]">
              P&L: <span className={`font-semibold font-mono ${stats.pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                {stats.pnl >= 0 ? '+' : ''}{stats.pnl.toFixed(2)}
              </span>
            </span>
          </div>
          <button
            onClick={refresh}
            className="text-xs text-slate-400 hover:text-white border border-[#2a3a52] px-2 py-1 rounded-lg transition-colors"
          >
            ↻ Refresh
          </button>
        </div>
      </div>

      {/* Legend */}
      <div className="flex flex-wrap gap-3 mb-3 text-xs text-slate-400">
        <span className="flex items-center gap-1"><span className="text-emerald-400">▲</span> BUY entry</span>
        <span className="flex items-center gap-1"><span className="text-emerald-400">▼</span> Take-profit exit</span>
        <span className="flex items-center gap-1"><span className="text-red-400">▼</span> Stop-loss exit</span>
        {openPos && (
          <>
            <span className="flex items-center gap-1"><span className="text-red-400">— —</span> Stop-loss line</span>
            <span className="flex items-center gap-1"><span className="text-emerald-400">— —</span> Take-profit line</span>
          </>
        )}
        {openPos && (
          <span className="ml-auto text-amber-400 font-medium">
            🟡 OPEN: {openPos.pair} @ {Number(openPos.entry_price).toFixed(2)}
            {' '}
            <span className={Number(openPos.unrealized_pnl) >= 0 ? 'text-emerald-400' : 'text-red-400'}>
              ({Number(openPos.unrealized_pnl) >= 0 ? '+' : ''}{Number(openPos.unrealized_pnl).toFixed(2)} USDT)
            </span>
          </span>
        )}
      </div>

      {/* Chart container */}
      <div
        ref={containerRef}
        style={{ height: `${height - 40}px` }}
        className="w-full rounded-xl overflow-hidden border border-[#2a3a52] relative"
      >
        {status === 'loading' && (
          <div className="absolute inset-0 flex items-center justify-center bg-[#0a0f1c] z-10">
            <div className="text-slate-400 text-sm animate-pulse">Loading chart data…</div>
          </div>
        )}
        {status === 'error' && (
          <div className="absolute inset-0 flex items-center justify-center bg-[#0a0f1c] z-10">
            <div className="text-red-400 text-sm">⚠ Could not load chart data</div>
          </div>
        )}
      </div>

      {/* Footer */}
      <div className="flex justify-between mt-2 text-xs text-slate-600">
        <span>🕐 Last updated: {lastUpdate || '…'}</span>
        <span>Green candle = bullish · Red candle = bearish</span>
      </div>

      {/* No-trades hint */}
      {status === 'ok' && stats.trades === 0 && (
        <div className="mt-3 p-3 rounded-xl bg-amber-500/10 border border-amber-500/20 text-amber-300 text-xs">
          💡 No trades yet — markers will appear here once the bot enters and exits positions.
          The chart is live and ready.
        </div>
      )}
    </div>
  );
}
