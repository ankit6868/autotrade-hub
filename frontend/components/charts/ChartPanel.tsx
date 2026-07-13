'use client';

import { useEffect, useState } from 'react';
import KuCoinFuturesChart from './KuCoinFuturesChart';
import TradingViewWidget from './TradingViewWidget';

type ChartView = 'tv' | 'advanced';
const LS_KEY = 'futures_chart_view';

interface Props {
  pair: string;
  defaultInterval?: string;
  mode?: 'paper' | 'live';
  tradeRefreshKey?: number;
  strategyIndicators?: string[];
  strategyId?: number;
  onTakeFormation?: (f: { direction: string; entry: number; sl: number | null; tp: number | null }) => void;
}

/**
 * Chart container with a slim toggle:
 *  • TradingView  — the real, tick-level TradingView chart (KUCOIN:<pair>.P),
 *                   the same engine KuCoin uses. Default.
 *  • Advanced     — our lightweight-charts chart, which draws YOUR manual
 *                   entry / TP / SL / exit markers (a sealed TV widget can't).
 * The choice is remembered in localStorage so both the desktop and mobile
 * chart instances stay in sync across reloads.
 */
export default function ChartPanel(props: Props) {
  const [view, setView] = useState<ChartView>('tv');

  // hydrate from localStorage (client only)
  useEffect(() => {
    try {
      const saved = localStorage.getItem(LS_KEY) as ChartView | null;
      if (saved === 'tv' || saved === 'advanced') setView(saved);
    } catch { /* ignore */ }
  }, []);

  const choose = (v: ChartView) => {
    setView(v);
    try { localStorage.setItem(LS_KEY, v); } catch { /* ignore */ }
  };

  const tab = (v: ChartView, label: string) => (
    <button
      onClick={() => choose(v)}
      className={`px-3 py-1 text-xs font-medium rounded transition-colors ${
        view === v
          ? 'bg-white/[0.12] text-white'
          : 'text-white/45 hover:text-white/80'
      }`}
    >
      {label}
    </button>
  );

  return (
    <div className="flex flex-col h-full w-full">
      <div className="flex items-center gap-1 px-2 py-1 border-b border-white/[0.06] shrink-0">
        {tab('tv', 'TradingView')}
        {tab('advanced', 'Advanced')}
        {view === 'tv' && (
          <span className="ml-auto pr-1 text-[10px] text-white/30 hidden sm:block">
            Live KuCoin perp · your trade markers are on “Advanced”
          </span>
        )}
      </div>
      <div className="flex-1 min-h-0 overflow-hidden">
        {view === 'tv' ? (
          <TradingViewWidget pair={props.pair} interval={props.defaultInterval || '15m'} />
        ) : (
          <KuCoinFuturesChart {...props} />
        )}
      </div>
    </div>
  );
}
