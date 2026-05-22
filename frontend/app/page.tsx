'use client';

import Link from 'next/link';
import { useEffect, useState } from 'react';
import { api } from '@/lib/api';

/**
 * Futures-only landing page. Replaces the old multi-widget spot dashboard
 * (SignalsPanel + RiskMonitor + spot positions) that was removed alongside
 * the spot trading stack. Single purpose: route the user to the futures
 * terminal as fast as possible, show a tiny health pulse so they know the
 * backend is reachable, and surface any active futures bots at a glance.
 */
export default function Home() {
  const [activeBots, setActiveBots] = useState<any[]>([]);
  const [error, setError]           = useState<string | null>(null);
  const [health, setHealth]         = useState<string>('checking…');

  useEffect(() => {
    // Backend reachability check — proves the deploy is live.
    fetch('/api/health')
      .then(r => r.ok ? r.json() : Promise.reject(r.statusText))
      .then(d => setHealth(`healthy · ${d.active_users || 0} active engines`))
      .catch(() => setHealth('unreachable'));

    // Pull the merged paper+live bot list once.
    Promise.all([
      api.futures.bots.list('paper').catch(() => ({ bots: [] })),
      api.futures.bots.list('live').catch(() => ({ bots: [] })),
    ])
      .then(([p, l]) => {
        const merged = [...(p?.bots || []), ...(l?.bots || [])]
          .filter((b: any) => b.is_running)
          .slice(0, 6);
        setActiveBots(merged);
      })
      .catch((e: any) => setError(String(e?.message || e)));
  }, []);

  return (
    <div className="max-w-4xl mx-auto px-4 sm:px-6 py-10 space-y-8">
      <header className="space-y-2">
        <h1 className="text-2xl sm:text-3xl font-bold text-white">AutoTrade Hub</h1>
        <p className="text-sm text-slate-400">
          KuCoin Futures terminal · paper + live lead-trading · backtest.
          <span className="ml-2 text-xs text-slate-500">backend: {health}</span>
        </p>
      </header>

      <section className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
        <Link
          href="/futures-trade"
          className="rounded-2xl bg-[#0f1830] border border-white/[0.06] hover:border-emerald-500/30 transition-colors p-5"
        >
          <div className="text-2xl mb-2">💹</div>
          <div className="text-white font-bold">Futures Terminal</div>
          <p className="text-xs text-slate-400 mt-1">Chart, manual orders, bot panel — paper + live in one tab.</p>
        </Link>
        <Link
          href="/futures-backtest"
          className="rounded-2xl bg-[#0f1830] border border-white/[0.06] hover:border-sky-500/30 transition-colors p-5"
        >
          <div className="text-2xl mb-2">🔬</div>
          <div className="text-white font-bold">Futures Backtest</div>
          <p className="text-xs text-slate-400 mt-1">TF-aware risk engine, ARM partial TPs, auto-tune.</p>
        </Link>
        <Link
          href="/strategy/upload"
          className="rounded-2xl bg-[#0f1830] border border-white/[0.06] hover:border-purple-500/30 transition-colors p-5"
        >
          <div className="text-2xl mb-2">📄</div>
          <div className="text-white font-bold">Upload Strategy</div>
          <p className="text-xs text-slate-400 mt-1">Paste rules in natural language — AI parses + validates.</p>
        </Link>
      </section>

      {/* Active bots — at-a-glance, links into the futures terminal. */}
      <section className="space-y-2">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-white">Active Bots</h2>
          <Link href="/futures-trade" className="text-xs text-emerald-400 hover:text-emerald-300">View all →</Link>
        </div>
        {error && <p className="text-xs text-red-400">Could not load bot list: {error}</p>}
        {activeBots.length === 0 && !error && (
          <p className="text-xs text-slate-500 px-3 py-4 bg-[#0f1830] rounded-xl border border-white/[0.04]">
            No bots running. Start one from the Futures Terminal → Bot panel.
          </p>
        )}
        {activeBots.length > 0 && (
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
            {activeBots.map(b => (
              <Link
                key={b.id}
                href="/futures-trade"
                className="p-3 bg-[#0f1830] border border-white/[0.06] rounded-xl hover:border-emerald-500/30 transition-colors"
              >
                <div className="flex items-center justify-between">
                  <span className="text-white font-bold text-sm">{b.strategy_name}</span>
                  <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${
                    b.mode === 'live' ? 'bg-emerald-500/20 text-emerald-400' : 'bg-indigo-500/20 text-indigo-400'
                  }`}>{b.mode === 'live' ? 'LIVE' : 'PAPER'}</span>
                </div>
                <div className="text-[10px] text-slate-500 mt-1">
                  {b.pairs} · {b.leverage}x · {b.total_trades || 0} trades · P&L {(b.total_pnl || 0) >= 0 ? '+' : ''}{(b.total_pnl || 0).toFixed(2)}
                </div>
              </Link>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
