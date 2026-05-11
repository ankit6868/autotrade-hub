'use client';
import { useEffect, useState } from 'react';
import { api } from '@/lib/api';

interface Props {
  activePair: string;
  onPairChange: (pair: string) => void;
}

const DEFAULT_PAIRS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT'];

export default function PairTabs({ activePair, onPairChange }: Props) {
  const [openPairs, setOpenPairs] = useState<string[]>([activePair]);
  const [prices, setPrices] = useState<Record<string, number>>({});
  const [showAdd, setShowAdd] = useState(false);
  const [searchPairs, setSearchPairs] = useState<string[]>([]);
  const [search, setSearch] = useState('');

  // Fetch prices for open tabs
  useEffect(() => {
    const fetchPrices = async () => {
      for (const pair of openPairs) {
        try {
          const data = await api.market.price(pair);
          if (data.price) setPrices(prev => ({ ...prev, [pair]: parseFloat(data.price) }));
        } catch { /* */ }
      }
    };
    fetchPrices();
    const t = setInterval(fetchPrices, 5000);
    return () => clearInterval(t);
  }, [openPairs]);

  function addPair(pair: string) {
    if (!openPairs.includes(pair)) {
      setOpenPairs([...openPairs, pair]);
    }
    onPairChange(pair);
    setShowAdd(false);
    setSearch('');
  }

  function removePair(pair: string) {
    if (openPairs.length <= 1) return;
    const newPairs = openPairs.filter(p => p !== pair);
    setOpenPairs(newPairs);
    if (activePair === pair) onPairChange(newPairs[0]);
  }

  useEffect(() => {
    if (showAdd && searchPairs.length === 0) {
      api.market.pairs().then(d => setSearchPairs(d.pairs || [])).catch(() => {});
    }
  }, [showAdd, searchPairs.length]);

  const filteredSearch = searchPairs
    .filter(p => p.endsWith('/USDT') && p.toLowerCase().includes(search.toLowerCase()))
    .slice(0, 20);

  return (
    <div className="relative flex items-center border-b border-white/[0.06] bg-[#0d1117]">
      <div className="flex items-center overflow-x-auto scrollbar-none">
        {openPairs.map(pair => {
          const isActive = pair === activePair;
          const price = prices[pair];
          return (
            <div
              key={pair}
              onClick={() => onPairChange(pair)}
              className={`flex items-center gap-2 px-3 py-2 cursor-pointer border-r border-white/[0.04] text-xs group ${
                isActive ? 'bg-[#131720] text-white' : 'text-slate-400 hover:text-white hover:bg-white/[0.02]'
              }`}
            >
              <span className="font-medium whitespace-nowrap">
                {pair.replace('/', '')} Perp
              </span>
              {price && (
                <span className="text-[10px] text-slate-500">{price.toFixed(price > 100 ? 1 : 4)}</span>
              )}
              {openPairs.length > 1 && (
                <button
                  onClick={e => { e.stopPropagation(); removePair(pair); }}
                  className="opacity-0 group-hover:opacity-100 text-slate-500 hover:text-red-400 text-[10px] ml-1"
                >
                  &times;
                </button>
              )}
            </div>
          );
        })}
      </div>

      {/* Add pair button */}
      <button
        onClick={() => setShowAdd(!showAdd)}
        className="px-2 py-2 text-slate-500 hover:text-white text-sm"
      >
        +
      </button>

      {/* Pair search dropdown */}
      {showAdd && (
        <div className="absolute top-full left-0 z-30 bg-[#1a1e2e] border border-white/[0.1] rounded-lg shadow-xl w-64 mt-1">
          <div className="p-2">
            <input
              type="text"
              value={search}
              onChange={e => setSearch(e.target.value)}
              placeholder="Search pairs..."
              autoFocus
              className="w-full bg-slate-800 border border-white/[0.06] rounded px-2 py-1.5 text-xs text-white outline-none"
            />
          </div>
          <div className="max-h-48 overflow-y-auto">
            {(search ? filteredSearch : DEFAULT_PAIRS.filter(p => !openPairs.includes(p))).map(pair => (
              <button
                key={pair}
                onClick={() => addPair(pair)}
                className="w-full text-left px-3 py-1.5 text-xs text-slate-300 hover:bg-white/[0.06]"
              >
                {pair.replace('/', '')} Perp
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
