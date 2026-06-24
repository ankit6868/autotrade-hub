'use client';

import { useCallback, useEffect, useState } from 'react';
import { api } from '@/lib/api';
import { useVisibleInterval } from '@/lib/useVisibleInterval';

const fmt = (n: number) => `${n >= 0 ? '+' : ''}${n.toFixed(2)}`;
const cls = (n: number) => (n > 0 ? 'text-emerald-400' : n < 0 ? 'text-rose-400' : 'text-slate-400');

/** Tiny inline SVG equity-curve sparkline. */
function Sparkline({ points }: { points: { pnl: number }[] }) {
  if (points.length < 2) return null;
  const ys = points.map(p => p.pnl);
  const min = Math.min(...ys), max = Math.max(...ys), span = max - min || 1;
  const W = 600, H = 60;
  const path = points.map((p, i) => {
    const x = (i / (points.length - 1)) * W;
    const y = H - ((p.pnl - min) / span) * H;
    return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  const up = ys[ys.length - 1] >= ys[0];
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-14" preserveAspectRatio="none">
      <path d={path} fill="none" stroke={up ? '#34d399' : '#fb7185'} strokeWidth="1.5" />
    </svg>
  );
}

export default function DashboardPanel() {
  const [d, setD] = useState<any>(null);
  const [err, setErr] = useState('');

  const load = useCallback(async () => {
    try { setD(await api.futures.dashboard()); setErr(''); }
    catch { setErr('Could not load results.'); }
  }, []);
  useEffect(() => { load(); }, [load]);
  useVisibleInterval(load, 30000);

  if (err) return null;
  if (!d) return null;
  if (!d.trade_count) {
    return (
      <section className="rounded-2xl bg-[#0f1830] border border-white/[0.06] p-5">
        <h2 className="text-sm font-bold text-white mb-1">📊 Live results</h2>
        <p className="text-xs text-slate-500">No closed trades yet. Once your bots close trades, real P&L shows here.</p>
      </section>
    );
  }

  const todayTotal = (d.today_pnl?.paper || 0) + (d.today_pnl?.live || 0);
  const allTotal   = (d.total_pnl?.paper || 0) + (d.total_pnl?.live || 0);

  return (
    <section className="rounded-2xl bg-[#0f1830] border border-white/[0.06] p-5 space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <h2 className="text-sm font-bold text-white">📊 Live results <span className="text-[10px] text-slate-500 font-normal">· real trades, not backtests</span></h2>
        <button onClick={load} className="text-[11px] underline text-slate-400 hover:text-slate-200">refresh</button>
      </div>

      {/* Top stat cards */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        <div className="rounded-xl bg-[#0f1729] border border-white/[0.05] p-3">
          <div className="text-[10px] text-slate-500">Today P&L</div>
          <div className={`text-lg font-bold font-mono ${cls(todayTotal)}`}>{fmt(todayTotal)}</div>
          <div className="text-[9px] text-slate-600">P {fmt(d.today_pnl?.paper||0)} · L {fmt(d.today_pnl?.live||0)}</div>
        </div>
        <div className="rounded-xl bg-[#0f1729] border border-white/[0.05] p-3">
          <div className="text-[10px] text-slate-500">All-time P&L</div>
          <div className={`text-lg font-bold font-mono ${cls(allTotal)}`}>{fmt(allTotal)}</div>
          <div className="text-[9px] text-slate-600">P {fmt(d.total_pnl?.paper||0)} · L {fmt(d.total_pnl?.live||0)}</div>
        </div>
        <div className="rounded-xl bg-[#0f1729] border border-white/[0.05] p-3">
          <div className="text-[10px] text-slate-500">Closed trades</div>
          <div className="text-lg font-bold text-slate-200">{d.trade_count}</div>
        </div>
        <div className="rounded-xl bg-[#0f1729] border border-white/[0.05] p-3">
          <div className="text-[10px] text-slate-500">Active bots (with trades)</div>
          <div className="text-lg font-bold text-slate-200">{d.bots?.length || 0}</div>
        </div>
      </div>

      {/* Equity curve */}
      {d.equity_curve?.length > 1 && (
        <div className="rounded-xl bg-[#0f1729] border border-white/[0.05] p-3">
          <div className="text-[10px] text-slate-500 mb-1">Cumulative P&L (last {d.equity_curve.length} closed trades)</div>
          <Sparkline points={d.equity_curve} />
        </div>
      )}

      {/* Per-bot table */}
      {d.bots?.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-[#2a3a52]">
          <table className="w-full text-[11px]">
            <thead className="text-slate-500 bg-[#0f1830]">
              <tr><th className="text-left p-2">Strategy</th><th className="p-2">Mode</th><th className="p-2">Trades</th><th className="p-2">Win rate</th><th className="p-2">Today</th><th className="p-2">Total</th></tr>
            </thead>
            <tbody>
              {d.bots.map((b: any, i: number) => (
                <tr key={i} className="border-t border-white/[0.04]">
                  <td className="p-2 text-slate-200">{b.strategy}</td>
                  <td className="p-2 text-center"><span className={b.mode === 'live' ? 'text-emerald-300' : 'text-indigo-300'}>{b.mode}</span></td>
                  <td className="p-2 text-center text-slate-400">{b.trades}</td>
                  <td className="p-2 text-center text-slate-400">{b.win_rate}%</td>
                  <td className={`p-2 text-center font-mono ${cls(b.today_pnl)}`}>{fmt(b.today_pnl)}</td>
                  <td className={`p-2 text-center font-mono font-semibold ${cls(b.total_pnl)}`}>{fmt(b.total_pnl)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Recent history */}
      {d.history?.length > 0 && (
        <details className="text-[11px]">
          <summary className="cursor-pointer text-slate-400 hover:text-slate-200">Recent trades ({d.history.length})</summary>
          <div className="mt-2 max-h-64 overflow-auto rounded-lg border border-[#2a3a52]">
            <table className="w-full text-[10px]">
              <thead className="text-slate-500 sticky top-0 bg-[#0f1830]">
                <tr><th className="text-left p-1.5">When</th><th className="p-1.5">Strategy</th><th className="p-1.5">Pair</th><th className="p-1.5">Side</th><th className="p-1.5">P&L</th><th className="p-1.5">Reason</th></tr>
              </thead>
              <tbody>
                {d.history.map((t: any, i: number) => (
                  <tr key={i} className="border-t border-white/[0.03]">
                    <td className="p-1.5 text-slate-500">{t.exit_time ? t.exit_time.replace('T', ' ').slice(5, 16) : '—'}</td>
                    <td className="p-1.5 text-slate-400">{t.strategy}</td>
                    <td className="p-1.5 text-center text-slate-400">{t.pair}</td>
                    <td className="p-1.5 text-center"><span className={t.side === 'long' ? 'text-emerald-400' : 'text-rose-400'}>{t.side}</span></td>
                    <td className={`p-1.5 text-center font-mono ${cls(t.profit_abs)}`}>{fmt(t.profit_abs)}</td>
                    <td className="p-1.5 text-slate-600">{t.exit_reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}
    </section>
  );
}
