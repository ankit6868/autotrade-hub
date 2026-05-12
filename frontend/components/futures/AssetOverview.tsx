'use client';
import { useEffect, useState } from 'react';
import { api } from '@/lib/api';

interface Props {
  mode: 'paper' | 'live';
  pair?: string;
}

export default function AssetOverview({ mode, pair }: Props) {
  const [account, setAccount] = useState<any>(null);

  useEffect(() => {
    const fetchData = async () => {
      try {
        const data = await api.futures.account();
        setAccount(data);
      } catch { /* silent */ }
    };
    fetchData();
    const t = setInterval(fetchData, 10000);
    return () => clearInterval(t);
  }, [mode]);

  const baseCoin = pair?.split('/')[0] || 'BTC';
  const isLive = account?.source === 'kucoin_lead_trading';

  return (
    <div className="px-3 py-3 border-t border-white/[0.06]">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <h4 className="text-xs font-bold text-white">Asset Overview</h4>
          {isLive && (
            <span className="text-[9px] px-1.5 py-0.5 rounded bg-emerald-500/20 text-emerald-400 font-medium">Lead</span>
          )}
          {!isLive && (
            <span className="text-[9px] px-1.5 py-0.5 rounded bg-indigo-500/20 text-indigo-400 font-medium">Paper</span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <button onClick={() => api.futures.account().then(d => setAccount(d)).catch(() => {})} className="text-slate-500 hover:text-white">
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
              <span className="text-slate-300">{account?.margin_mode || 'Cross'}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-slate-400">Position Margin</span>
              <span className="text-white">
                {(account?.used_margin ?? 0) > 0 ? `${account.used_margin.toFixed(2)} USDT` : '— USDT'}
              </span>
            </div>
            <div className="flex justify-between">
              <span className="text-slate-400">Order Margin</span>
              <span className="text-white">
                {(account?.order_margin ?? 0) > 0 ? `${account.order_margin.toFixed(2)} USDT` : '— USDT'}
              </span>
            </div>
            <div className="flex justify-between">
              <span className="text-slate-400">Risk Ratio</span>
              <span className={`${(account?.risk_ratio ?? 0) > 50 ? 'text-red-400' : (account?.risk_ratio ?? 0) > 20 ? 'text-amber-400' : 'text-white'}`}>
                {(account?.risk_ratio ?? 0) > 0 ? `${account.risk_ratio.toFixed(2)}%` : '—'}
              </span>
            </div>
          </div>
        </div>

        {/* USDT-M section */}
        <div className="pt-1 border-t border-white/[0.04]">
          <p className="text-slate-500 text-[10px] font-medium mb-1">USDT-M</p>
          <div className="space-y-1 pl-1">
            <div className="flex justify-between">
              <span className="text-slate-400">Account Equity</span>
              <span className="text-white font-medium">{(account?.balance ?? 0).toFixed(2)} USDT</span>
            </div>
            <div className="flex justify-between">
              <span className="text-slate-400">Available Balance</span>
              <span className="text-white">{(account?.available_balance ?? 0).toFixed(2)} USDT</span>
            </div>
            <div className="flex justify-between">
              <span className="text-slate-400">Margin Balance</span>
              <span className="text-white">{(account?.margin_balance ?? account?.balance ?? 0).toFixed(2)} USDT</span>
            </div>
            <div className="flex justify-between">
              <span className="text-slate-400">Total Unrealized PNL</span>
              <span className={`font-medium ${(account?.unrealized_pnl ?? 0) > 0 ? 'text-emerald-400' : (account?.unrealized_pnl ?? 0) < 0 ? 'text-red-400' : 'text-slate-300'}`}>
                {(account?.unrealized_pnl ?? 0) > 0 ? '+' : ''}{(account?.unrealized_pnl ?? 0).toFixed(2)} USDT
              </span>
            </div>
            {(account?.frozen_funds ?? 0) > 0 && (
              <div className="flex justify-between">
                <span className="text-slate-400">Frozen Funds</span>
                <span className="text-amber-400">{account.frozen_funds.toFixed(2)} USDT</span>
              </div>
            )}
            {isLive && (account?.max_withdraw ?? 0) > 0 && (
              <div className="flex justify-between">
                <span className="text-slate-400">Max Withdraw</span>
                <span className="text-slate-300">{account.max_withdraw.toFixed(2)} USDT</span>
              </div>
            )}
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
