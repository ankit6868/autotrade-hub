'use client';
import { useEffect, useState } from 'react';
import { api } from '@/lib/api';

interface Props {
  mode: 'paper' | 'live';
}

export default function AssetOverview({ mode }: Props) {
  const [account, setAccount] = useState<any>(null);

  useEffect(() => {
    const fetch = async () => {
      try {
        const data = await api.futures.account();
        setAccount(data);
      } catch { /* silent */ }
    };
    fetch();
    const t = setInterval(fetch, 10000);
    return () => clearInterval(t);
  }, [mode]);

  return (
    <div className="px-3 py-3 border-t border-white/[0.06]">
      <h4 className="text-xs font-bold text-white mb-2">Asset Overview</h4>
      <div className="space-y-1.5 text-[11px]">
        <div className="flex justify-between">
          <span className="text-slate-500">Trading Account</span>
          <span className="text-white">{(account?.balance ?? 0).toFixed(2)} USDT</span>
        </div>
        <div className="flex justify-between">
          <span className="text-slate-500">Equity</span>
          <span className="text-white">{(account?.equity ?? 0).toFixed(2)} USDT</span>
        </div>
        <div className="flex justify-between">
          <span className="text-slate-500">Unrealized PNL</span>
          <span className={(account?.unrealized_pnl ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}>
            {(account?.unrealized_pnl ?? 0).toFixed(2)} USDT
          </span>
        </div>
        <div className="flex justify-between">
          <span className="text-slate-500">Mode</span>
          <span className="text-slate-300 capitalize">{mode}</span>
        </div>
      </div>

      {mode === 'live' && (
        <div className="flex gap-2 mt-3">
          <button className="flex-1 py-1.5 rounded text-[10px] font-medium bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 hover:bg-emerald-500/20">
            Deposit
          </button>
          <button className="flex-1 py-1.5 rounded text-[10px] font-medium bg-slate-700 text-slate-300 hover:bg-slate-600">
            Transfer
          </button>
        </div>
      )}
    </div>
  );
}
