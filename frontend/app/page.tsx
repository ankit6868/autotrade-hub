'use client';

import { useEffect, useState } from 'react';
import { api } from '@/lib/api';
import MetricCard from '@/components/ui/MetricCard';
import StatusBadge from '@/components/ui/StatusBadge';
import SignalsPanel from '@/components/dashboard/SignalsPanel';
import RiskMonitor from '@/components/dashboard/RiskMonitor';
import TradingViewWidget from '@/components/charts/TradingViewWidget';
import TradingViewTicker from '@/components/charts/TradingViewTicker';
import { SidebarSignOut } from '@/components/AuthShell';
import Link from 'next/link';

const PAIR_TO_TV: Record<string, string> = {
  'BTC/USDT': 'KUCOIN:BTCUSDT',
  'ETH/USDT': 'KUCOIN:ETHUSDT',
  'SOL/USDT': 'KUCOIN:SOLUSDT',
  'XRP/USDT': 'KUCOIN:XRPUSDT',
  'BNB/USDT': 'KUCOIN:BNBUSDT',
  'DOGE/USDT': 'KUCOIN:DOGEUSDT',
  'ADA/USDT': 'KUCOIN:ADAUSDT',
  'AVAX/USDT': 'KUCOIN:AVAXUSDT',
};

const INTERVAL_TO_TV: Record<string, string> = {
  '1m': '1', '5m': '5', '15m': '15', '30m': '30',
  '1h': '60', '4h': '240', '1d': 'D', '1w': 'W',
};

export default function Dashboard() {
  const [configStatus, setConfigStatus] = useState<any>(null);
  const [botStatus, setBotStatus] = useState<any>(null);
  const [futuresStatus, setFuturesStatus] = useState<any>(null);
  const [openTrades, setOpenTrades] = useState<any[]>([]);
  const [recentTrades, setRecentTrades] = useState<any[]>([]);
  const [chartPair, setChartPair] = useState('BTC/USDT');
  const [chartInterval, setChartInterval] = useState('15m');

  useEffect(() => {
    loadDashboard();
    const interval = setInterval(loadDashboard, 10000);
    return () => clearInterval(interval);
  }, []);

  async function loadDashboard() {
    try {
      const [config, status, futStatus, open, futOpen, history, futHistory] = await Promise.all([
        api.config.status(),
        api.trade.status(),
        api.futures.status().catch(() => null),
        api.trade.open(),
        api.futures.open().catch(() => ({ trades: [] })),
        api.trade.history({ limit: '20' }),
        api.futures.history({ limit: '20' }).catch(() => ({ trades: [] })),
      ]);
      setConfigStatus(config);
      setBotStatus(status);
      setFuturesStatus(futStatus);
      // Merge spot + futures open trades, tag each with market type
      const allOpen = [
        ...(open.trades || []).map((t: any) => ({ ...t, _market: 'spot' })),
        ...(futOpen.trades || []).map((t: any) => ({ ...t, _market: 'futures' })),
      ];
      setOpenTrades(allOpen);
      // Merge spot + futures closed trades for P&L / win rate
      const allTrades = [
        ...(history.trades || []),
        ...(futHistory.trades || []),
      ].sort((a: any, b: any) => new Date(b.exit_time || b.close_date || 0).getTime() - new Date(a.exit_time || a.close_date || 0).getTime())
        .slice(0, 20);
      setRecentTrades(allTrades);
    } catch {
      // Backend not running yet
    }
  }

  // Combined P&L and win rate across spot + futures
  const totalPnl = recentTrades.reduce((sum, t) => sum + (Number(t.profit_abs) || 0), 0);
  const winRate = recentTrades.length > 0
    ? (recentTrades.filter((t) => Number(t.profit_abs) > 0).length / recentTrades.length) * 100
    : 0;
  // Determine active bot (futures or spot)
  const activeBotStatus = (futuresStatus?.running) ? futuresStatus : botStatus;
  const activeBotMode = futuresStatus?.running
    ? `futures-${futuresStatus.mode}${futuresStatus.leverage ? ` ${futuresStatus.leverage}x` : ''}`
    : botStatus?.mode;

  const tvSymbol = PAIR_TO_TV[chartPair] || 'KUCOIN:BTCUSDT';
  const tvInterval = INTERVAL_TO_TV[chartInterval] || '15';

  return (
    <div className="space-y-5 sm:space-y-6">
      {/* Live Ticker Tape — full-bleed across the responsive padding */}
      <div className="-mx-4 sm:-mx-6 lg:-mx-8 bg-[#0d1424] border-y border-[#243153]">
        <TradingViewTicker />
      </div>

      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-end sm:justify-between gap-3">
        <div>
          <h1 className="heading-xl">
            Welcome back <span className="text-gradient-brand">👋</span>
          </h1>
          <p className="text-slate-400 mt-1 text-sm">AutoTrade Hub overview</p>
        </div>
        {(botStatus || futuresStatus) && (
          <StatusBadge
            status={activeBotStatus?.running ? 'running' : 'stopped'}
            label={activeBotStatus?.running ? `${activeBotMode} trading` : 'Bot stopped'}
          />
        )}
      </div>

      {/* Quick Setup Banner */}
      {configStatus && !configStatus.configured && (
        <div className="card border-brand-500/40 bg-gradient-to-br from-brand-900/30 to-brand-700/10">
          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
            <div>
              <h2 className="text-base sm:text-lg font-semibold">Welcome to AutoTrade Hub! 👋</h2>
              <p className="text-slate-400 text-sm mt-1">Get started by setting up your API keys. Everything is 100% free.</p>
            </div>
            <Link href="/setup" className="btn-primary whitespace-nowrap self-start sm:self-auto">
              Start Setup →
            </Link>
          </div>
        </div>
      )}

      {/* Metrics — hero card on left (like reference's Portfolio Balance), supporting metrics on right */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-3 sm:gap-4">
        <div className="lg:col-span-1">
          <MetricCard
            title="Total P&L"
            value={`${totalPnl >= 0 ? '+' : ''}${totalPnl.toFixed(2)} USDT`}
            subtitle={`${recentTrades.length} recent trades`}
            variant="hero"
            icon={<span className="text-base">💰</span>}
          />
        </div>
        <div className="lg:col-span-2 grid grid-cols-2 sm:grid-cols-3 gap-3 sm:gap-4">
          <MetricCard title="Win Rate" value={`${winRate.toFixed(1)}%`} icon={<span>🎯</span>} />
          <MetricCard title="Open Trades" value={openTrades.length} icon={<span>📈</span>} />
          <MetricCard
            title="Bot Status"
            value={activeBotStatus?.running ? 'Running' : 'Stopped'}
            subtitle={activeBotMode || undefined}
            color={activeBotStatus?.running ? 'profit' : 'default'}
            icon={<span>{activeBotStatus?.running ? '🟢' : '⚪'}</span>}
          />
        </div>
      </div>

      {/* Risk Monitor */}
      <RiskMonitor />

      {/* TradingView Chart + Signals */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 sm:gap-6">
        <div className="lg:col-span-2 card p-0 overflow-hidden">
          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2 px-4 py-3 border-b border-[#243153]">
            <h2 className="font-semibold text-sm sm:text-base">Live Chart — TradingView</h2>
            <div className="flex gap-2">
              <select className="input py-1.5 text-sm w-32" value={chartPair} onChange={(e) => setChartPair(e.target.value)}>
                {Object.keys(PAIR_TO_TV).map((p) => <option key={p} value={p}>{p}</option>)}
              </select>
              <select className="input py-1.5 text-sm w-20" value={chartInterval} onChange={(e) => setChartInterval(e.target.value)}>
                {Object.keys(INTERVAL_TO_TV).map((i) => <option key={i} value={i}>{i}</option>)}
              </select>
            </div>
          </div>
          <TradingViewWidget symbol={tvSymbol} interval={tvInterval} />
        </div>
        <SignalsPanel pair={chartPair} interval={chartInterval} />
      </div>

      {/* Open Positions */}
      <div className="card">
        <h2 className="text-base sm:text-lg font-semibold mb-4">Open Positions</h2>
        {openTrades.length === 0 ? (
          <p className="text-slate-500 text-sm">No open trades</p>
        ) : (
          <>
            {/* Desktop / tablet table */}
            <div className="hidden md:block overflow-x-auto -mx-4 sm:-mx-6 px-4 sm:px-6">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-slate-400 border-b border-[#243153]">
                    <th className="text-left py-3 px-2">Pair</th>
                    <th className="text-left py-3 px-2">Side</th>
                    <th className="text-right py-3 px-2">Entry Price</th>
                    <th className="text-right py-3 px-2">Amount</th>
                    <th className="text-right py-3 px-2">Stop Loss</th>
                    <th className="text-left py-3 px-2">Mode</th>
                  </tr>
                </thead>
                <tbody>
                  {openTrades.map((t) => (
                    <tr key={t.id} className="border-b border-[#243153]/50 hover:bg-white/[0.02] transition-colors">
                      <td className="py-3 px-2 font-medium">{t.pair}</td>
                      <td className="py-3 px-2">{t.side}</td>
                      <td className="py-3 px-2 text-right tabular-nums">{Number(t.entry_price).toFixed(4)}</td>
                      <td className="py-3 px-2 text-right tabular-nums">{Number(t.amount).toFixed(6)}</td>
                      <td className="py-3 px-2 text-right tabular-nums">{Number(t.stoploss_price).toFixed(4)}</td>
                      <td className="py-3 px-2">
                        <span className={`text-xs px-2 py-1 rounded-full ${t.mode === 'live' ? 'bg-red-500/20 text-red-300' : 'bg-blue-500/20 text-blue-300'}`}>
                          {t.mode}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* Mobile cards */}
            <div className="md:hidden space-y-2.5">
              {openTrades.map((t) => (
                <div key={t.id} className="rounded-xl bg-[#0d1424] border border-[#243153] p-3">
                  <div className="flex items-center justify-between mb-2">
                    <div className="font-medium">{t.pair}</div>
                    <span className={`text-[10px] px-2 py-0.5 rounded-full ${t.mode === 'live' ? 'bg-red-500/20 text-red-300' : 'bg-blue-500/20 text-blue-300'}`}>
                      {t.mode}
                    </span>
                  </div>
                  <div className="grid grid-cols-2 gap-x-3 gap-y-1.5 text-xs">
                    <div className="text-slate-500">Side</div><div className="text-right">{t.side}</div>
                    <div className="text-slate-500">Entry</div><div className="text-right tabular-nums">{Number(t.entry_price).toFixed(4)}</div>
                    <div className="text-slate-500">Amount</div><div className="text-right tabular-nums">{Number(t.amount).toFixed(6)}</div>
                    <div className="text-slate-500">Stop</div><div className="text-right tabular-nums">{Number(t.stoploss_price).toFixed(4)}</div>
                  </div>
                </div>
              ))}
            </div>
          </>
        )}
      </div>

      {/* Recent Trades */}
      <div className="card">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-base sm:text-lg font-semibold">Recent Trades</h2>
          <Link href="/history" className="text-brand-400 text-sm hover:text-brand-300 hover:underline">View All →</Link>
        </div>
        {recentTrades.length === 0 ? (
          <p className="text-slate-500 text-sm">No trade history yet. Start paper trading to see results here.</p>
        ) : (
          <>
            <div className="hidden md:block overflow-x-auto -mx-4 sm:-mx-6 px-4 sm:px-6">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-slate-400 border-b border-[#243153]">
                    <th className="text-left py-3 px-2">Pair</th>
                    <th className="text-right py-3 px-2">Entry</th>
                    <th className="text-right py-3 px-2">Exit</th>
                    <th className="text-right py-3 px-2">Profit %</th>
                    <th className="text-right py-3 px-2">Profit USDT</th>
                    <th className="text-left py-3 px-2">Reason</th>
                  </tr>
                </thead>
                <tbody>
                  {recentTrades.map((t) => {
                    const profitPct = Number(t.profit_pct) || 0;
                    const profitAbs = Number(t.profit_abs) || 0;
                    return (
                      <tr key={t.id} className="border-b border-[#243153]/50 hover:bg-white/[0.02] transition-colors">
                        <td className="py-3 px-2 font-medium">{t.pair}</td>
                        <td className="py-3 px-2 text-right tabular-nums">{Number(t.entry_price).toFixed(4)}</td>
                        <td className="py-3 px-2 text-right tabular-nums">{Number(t.exit_price).toFixed(4)}</td>
                        <td className={`py-3 px-2 text-right tabular-nums ${profitPct >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                          {profitPct >= 0 ? '+' : ''}{profitPct.toFixed(2)}%
                        </td>
                        <td className={`py-3 px-2 text-right tabular-nums ${profitAbs >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                          {profitAbs >= 0 ? '+' : ''}{profitAbs.toFixed(2)}
                        </td>
                        <td className="py-3 px-2 text-slate-400">{t.exit_reason || ''}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            <div className="md:hidden space-y-2.5">
              {recentTrades.map((t) => {
                const profitPct = Number(t.profit_pct) || 0;
                const profitAbs = Number(t.profit_abs) || 0;
                const positive = profitAbs >= 0;
                return (
                  <div key={t.id} className="rounded-xl bg-[#0d1424] border border-[#243153] p-3">
                    <div className="flex items-center justify-between mb-2">
                      <div className="font-medium">{t.pair}</div>
                      <div className={`text-sm font-semibold tabular-nums ${positive ? 'text-emerald-400' : 'text-red-400'}`}>
                        {positive ? '+' : ''}{profitAbs.toFixed(2)} USDT
                        <span className="text-xs text-slate-500 ml-1">
                          ({positive ? '+' : ''}{profitPct.toFixed(2)}%)
                        </span>
                      </div>
                    </div>
                    <div className="grid grid-cols-2 gap-x-3 gap-y-1.5 text-xs">
                      <div className="text-slate-500">Entry</div><div className="text-right tabular-nums">{Number(t.entry_price).toFixed(4)}</div>
                      <div className="text-slate-500">Exit</div><div className="text-right tabular-nums">{Number(t.exit_price).toFixed(4)}</div>
                      {t.exit_reason && (
                        <>
                          <div className="text-slate-500">Reason</div>
                          <div className="text-right text-slate-400 truncate">{t.exit_reason}</div>
                        </>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          </>
        )}
      </div>

      {/* Mobile-only Sign out at end of dashboard.
          Desktop already shows UserButton in the top-right and the
          sidebar footer, so this is a fallback for mobile browsers
          where the bottom URL bar can hide the sidebar Sign out. */}
      <div className="md:hidden pt-2 pb-4">
        <SidebarSignOut />
      </div>
    </div>
  );
}
