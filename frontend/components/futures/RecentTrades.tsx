'use client';
import { useEffect, useState, useCallback } from 'react';
import { api } from '@/lib/api';

interface Props {
  symbol: string;
}

export default function RecentTrades({ symbol }: Props) {
  const [trades, setTrades] = useState<any[]>([]);

  const fetchTrades = useCallback(async () => {
    try {
      const futSymbol = symbol.replace('/', '').replace('USDT', 'USDTM');
      const data = await api.futures.recentTrades(futSymbol);
      setTrades((data.trades || []).slice(0, 30));
    } catch { /* silent */ }
  }, [symbol]);

  useEffect(() => {
    fetchTrades();
    const t = setInterval(fetchTrades, 2000);
    return () => clearInterval(t);
  }, [fetchTrades]);

  return (
    <div className="flex flex-col h-full text-xs">
      <div className="flex items-center justify-between px-2 py-1.5 border-b border-white/[0.06]">
        <span className="text-slate-300 font-medium">Recent Trades</span>
      </div>

      <div className="grid grid-cols-3 gap-1 px-2 py-1 text-[10px] text-slate-500 border-b border-white/[0.04]">
        <span>Price (USDT)</span>
        <span className="text-right">Amount</span>
        <span className="text-right">Time</span>
      </div>

      <div className="flex-1 overflow-y-auto">
        {trades.map((t, i) => {
          const isBuy = t.side === 'buy';
          const ts = t.ts ? new Date(typeof t.ts === 'number' && t.ts > 1e12 ? t.ts / 1e6 : t.ts * 1000) : new Date();
          const timeStr = ts.toLocaleTimeString('en-US', { hour12: false });
          return (
            <div key={i} className="grid grid-cols-3 gap-1 px-2 py-[2px]">
              <span className={isBuy ? 'text-emerald-400' : 'text-red-400'}>
                {parseFloat(t.price || '0').toFixed(2)}
              </span>
              <span className="text-right text-slate-300">
                {parseFloat(String(t.size || '0')).toFixed(3)}
              </span>
              <span className="text-right text-slate-500">{timeStr}</span>
            </div>
          );
        })}
        {trades.length === 0 && (
          <div className="text-center text-slate-600 py-4">No trades yet</div>
        )}
      </div>
    </div>
  );
}
