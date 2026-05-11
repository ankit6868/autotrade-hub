'use client';
import { useEffect, useState, useCallback } from 'react';
import { api } from '@/lib/api';

interface Props {
  symbol: string;
  onPriceClick?: (price: number) => void;
}

export default function OrderBook({ symbol, onPriceClick }: Props) {
  const [asks, setAsks] = useState<[string, string][]>([]);
  const [bids, setBids] = useState<[string, string][]>([]);
  const [lastPrice, setLastPrice] = useState<number>(0);
  const [spread, setSpread] = useState<number>(0);

  const fetchBook = useCallback(async () => {
    try {
      const futSymbol = symbol.replace('/', '').replace('USDT', 'USDTM');
      const data = await api.futures.orderbook(futSymbol);
      const a = (data.asks || []).slice(0, 15);
      const b = (data.bids || []).slice(0, 15);
      setAsks(a);
      setBids(b);
      if (a.length && b.length) {
        const bestAsk = parseFloat(a[0][0]);
        const bestBid = parseFloat(b[0][0]);
        setLastPrice((bestAsk + bestBid) / 2);
        setSpread(bestAsk - bestBid);
      }
    } catch { /* silent */ }
  }, [symbol]);

  useEffect(() => {
    fetchBook();
    const t = setInterval(fetchBook, 2000);
    return () => clearInterval(t);
  }, [fetchBook]);

  const maxAskVol = Math.max(...asks.map(a => parseFloat(a[1]) || 0), 0.001);
  const maxBidVol = Math.max(...bids.map(b => parseFloat(b[1]) || 0), 0.001);

  return (
    <div className="flex flex-col h-full text-xs">
      <div className="flex items-center justify-between px-2 py-1.5 border-b border-white/[0.06]">
        <span className="text-slate-300 font-medium">Order Book</span>
      </div>

      {/* Header */}
      <div className="grid grid-cols-3 gap-1 px-2 py-1 text-[10px] text-slate-500 border-b border-white/[0.04]">
        <span>Price (USDT)</span>
        <span className="text-right">Amount</span>
        <span className="text-right">Total</span>
      </div>

      {/* Asks (reversed so lowest ask is at bottom) */}
      <div className="flex-1 overflow-hidden flex flex-col justify-end">
        {[...asks].reverse().map(([price, size], i) => {
          const vol = parseFloat(size);
          const pct = (vol / maxAskVol) * 100;
          return (
            <div
              key={`a-${i}`}
              className="grid grid-cols-3 gap-1 px-2 py-[2px] cursor-pointer hover:bg-white/[0.04] relative"
              onClick={() => onPriceClick?.(parseFloat(price))}
            >
              <div
                className="absolute inset-y-0 right-0 bg-red-500/10"
                style={{ width: `${pct}%` }}
              />
              <span className="text-red-400 relative z-10">{parseFloat(price).toFixed(2)}</span>
              <span className="text-right text-slate-300 relative z-10">{vol.toFixed(3)}</span>
              <span className="text-right text-slate-500 relative z-10">
                {(parseFloat(price) * vol).toFixed(2)}
              </span>
            </div>
          );
        })}
      </div>

      {/* Spread / Last Price */}
      <div className="px-2 py-1.5 border-y border-white/[0.06] flex items-center justify-between bg-slate-900/50">
        <span className={`text-sm font-bold ${lastPrice > 0 ? 'text-emerald-400' : 'text-slate-300'}`}>
          {lastPrice > 0 ? lastPrice.toFixed(2) : '—'}
        </span>
        <span className="text-[10px] text-slate-500">
          Spread: {spread.toFixed(2)}
        </span>
      </div>

      {/* Bids */}
      <div className="flex-1 overflow-hidden">
        {bids.map(([price, size], i) => {
          const vol = parseFloat(size);
          const pct = (vol / maxBidVol) * 100;
          return (
            <div
              key={`b-${i}`}
              className="grid grid-cols-3 gap-1 px-2 py-[2px] cursor-pointer hover:bg-white/[0.04] relative"
              onClick={() => onPriceClick?.(parseFloat(price))}
            >
              <div
                className="absolute inset-y-0 right-0 bg-emerald-500/10"
                style={{ width: `${pct}%` }}
              />
              <span className="text-emerald-400 relative z-10">{parseFloat(price).toFixed(2)}</span>
              <span className="text-right text-slate-300 relative z-10">{vol.toFixed(3)}</span>
              <span className="text-right text-slate-500 relative z-10">
                {(parseFloat(price) * vol).toFixed(2)}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
