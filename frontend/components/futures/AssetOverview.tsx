'use client';
import { useEffect, useState } from 'react';
import { api } from '@/lib/api';

interface Props {
  mode: 'paper' | 'live';
  pair?: string;
}

export default function AssetOverview({ mode, pair }: Props) {
  const [account, setAccount] = useState<any>(null);
  const [leadStatus, setLeadStatus] = useState<{ connected: boolean; account_type?: string } | null>(null);

  useEffect(() => {
    const fetchData = async () => {
      try {
        const data = await api.futures.account();
        setAccount(data);
      } catch { /* silent */ }
    };
    fetchData();
    api.futures.leadTradingStatus().then(d => setLeadStatus(d)).catch(() => {});
    const t = setInterval(fetchData, 10000);
    return () => clearInterval(t);
  }, [mode]);

  const baseCoin = pair?.split('/')[0] || 'BTC';

  return (
    <div className="px-3 py-3 border-t border-white/[0.06]">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <h4 className="text-xs font-bold text-white">Asset Overview</h4>
          {mode === 'live' && leadStatus?.connected && (
            <span className="text-[9px] px-1.5 py-0.5 rounded bg-emerald-500/20 text-emerald-400 font-medium">Lead</span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <button className="text-slate-500 hover:text-white">
            <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" /><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" /></svg>
          </button>
          <button className="text-slate-500 hover:text-white">
            <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" /></svg>
          </button>
        </div>
      </div>

      <div className="space-y-2 text-[11px]">
        {/* Futures section */}
        <div>
          <p className="text-slate-500 text-[10px] font-medium mb-1">Futures</p>
          <div className="space-y-1 pl-1">
            <div className="flex justify-between">
              <span className="text-slate-400">{baseCoin}USDT Perp</span>
              <span className="text-slate-300 capitalize">{account?.margin_mode || 'Cross'}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-slate-400">Position Margin</span>
              <span className="text-white">— USDT</span>
            </div>
            <div className="flex justify-between">
              <span className="text-slate-400">Risk Ratio</span>
              <span className="text-white">—</span>
            </div>
          </div>
        </div>

        {/* USDT-M section */}
        <div className="pt-1 border-t border-white/[0.04]">
          <p className="text-slate-500 text-[10px] font-medium mb-1">USDT-M</p>
          <div className="space-y-1 pl-1">
            <div className="flex justify-between">
              <span className="text-slate-400">Total Balance</span>
              <span className="text-white">{(account?.balance ?? 0).toFixed(2)} USDT</span>
            </div>
            <div className="flex justify-between">
              <span className="text-slate-400">Available Balance</span>
              <span className="text-white">{(account?.available_balance ?? 0).toFixed(2)} USDT</span>
            </div>
            <div className="flex justify-between">
              <span className="text-slate-400">Total Unrealized PNL</span>
              <span className={`${(account?.unrealized_pnl ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                {(account?.unrealized_pnl ?? 0).toFixed(2)} USDT
              </span>
            </div>
          </div>
        </div>

        {/* Transfer button */}
        <div className="pt-2">
          <button className="w-full py-1.5 rounded text-[11px] font-medium bg-[#1e222d] text-slate-300 border border-white/[0.06] hover:bg-white/[0.06] transition-colors">
            Transfer
          </button>
        </div>
      </div>
    </div>
  );
}
