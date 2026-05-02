'use client';

import { useEffect, useState } from 'react';
import { api } from '@/lib/api';
import MetricCard from '@/components/ui/MetricCard';
import StatusBadge from '@/components/ui/StatusBadge';
import SignalsPanel from '@/components/dashboard/SignalsPanel';
import RiskMonitor from '@/components/dashboard/RiskMonitor';
import TradingViewWidget from '@/components/charts/TradingViewWidget';
import TradingViewTicker from '@/components/charts/TradingViewTicker';
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
      const [config, status, open, history] = await Promise.all([
        api.config.status(),
        api.trade.status(),
        api.trade.open(),
        api.trade.history({ limit: '10' }),
      ]);
      setConfigStatus(config);
      setBotStatus(status);
      setOpenTrades(open.trades);
      setRecentTrades(history.trades);
    } catch {
      // Backend not running yet
    }
  }

  const totalPnl = recentTrades.reduce((sum, t) => sum + (Number(t.profit_abs) || 0), 0);
  const winRate = recentTrades.length > 0
    ? (recentTrades.filter((t) => Number(t.profit_abs) > 0).length / recentTrades.length) * 100
    : 0;

  const tvSymbol = PAIR_TO_TV[chartPair] || 'KUCOIN:BTCUSDT';
  const tvInterval = INTERVAL_TO_TV[chartInterval] || '15';

  return (
    <div>
      {/* Live Ticker Tape */}
      <div className="mb-6 -mx-8 -mt-8 bg-[#111827] border-b border-[#2a3a52]">
        <TradingViewTicker />
      </div>

      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-3xl font-bold">Dashboard</h1>
          <p className="text-slate-400 mt-1">AutoTrade Hub Overview</p>
        </div>
        {botStatus && (
          <StatusBadge
            status={botStatus.running ? 'running' : 'stopped'}
            label={botStatus.running ? `${botStatus.mode} trading` : 'Bot stopped'}
          />
        )}
      </div>

      {/* Quick Setup Banner */}
      {configStatus && !configStatus.configured && (
        <div className="card mb-6 border-brand-500/50 bg-brand-900/20">
          <h2 className="text-lg font-semibold mb-2">Welcome to AutoTrade Hub!</h2>
          <p className="text-slate-400 mb-4">Get started by setting up your API keys. Everything is 100% free.</p>
          <Link href="/setup" className="btn-primary inline-block">Start Setup →</Link>
        </div>
      )}

      {/* Metrics */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
        <MetricCard title="Total P&L" value={`${totalPnl >= 0 ? '+' : ''}${totalPnl.toFixed(2)} USDT`} color={totalPnl >= 0 ? 'profit' : 'loss'} />
        <MetricCard title="Win Rate" value={`${winRate.toFixed(1)}%`} />
        <MetricCard title="Open Trades" value={openTrades.length} />
        <MetricCard title="Bot Status" value={botStatus?.running ? 'Running' : 'Stopped'} subtitle={botStatus?.mode ? String(botStatus.mode) : undefined} />
      </div>

      {/* Risk Monitor */}
      <div className="mb-6">
        <RiskMonitor />
      </div>

      {/* TradingView Chart + Signals */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 mb-6">
        <div className="lg:col-span-2 card p-0 overflow-hidden">
          {/* Chart header */}
          <div className="flex items-center justify-between px-4 py-3 border-b border-[#2a3a52]">
            <h2 className="font-semibold">Live Chart — TradingView</h2>
            <div className="flex gap-2">
              <select className="input py-1 text-sm w-32" value={chartPair} onChange={(e) => setChartPair(e.target.value)}>
                {Object.keys(PAIR_TO_TV).map((p) => <option key={p} value={p}>{p}</option>)}
              </select>
              <select className="input py-1 text-sm w-20" value={chartInterval} onChange={(e) => setChartInterval(e.target.value)}>
                {Object.keys(INTERVAL_TO_TV).map((i) => <option key={i} value={i}>{i}</option>)}
              </select>
            </div>
          </div>
          <TradingViewWidget symbol={tvSymbol} interval={tvInterval} height={420} />
        </div>
        <SignalsPanel pair={chartPair} interval={chartInterval} />
      </div>

      {/* Open Positions */}
      <div className="card mb-6">
        <h2 className="text-lg font-semibold mb-4">Open Positions</h2>
        {openTrades.length === 0 ? (
          <p className="text-slate-500 text-sm">No open trades</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-slate-400 border-b border-[#2a3a52]">
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
                  <tr key={t.id} className="border-b border-[#2a3a52]/50 hover:bg-[#2a3a52]/20">
                    <td className="py-3 px-2 font-medium">{t.pair}</td>
                    <td className="py-3 px-2">{t.side}</td>
                    <td className="py-3 px-2 text-right">{Number(t.entry_price).toFixed(4)}</td>
                    <td className="py-3 px-2 text-right">{Number(t.amount).toFixed(6)}</td>
                    <td className="py-3 px-2 text-right">{Number(t.stoploss_price).toFixed(4)}</td>
                    <td className="py-3 px-2">
                      <span className={`text-xs px-2 py-1 rounded ${t.mode === 'live' ? 'bg-red-500/20 text-red-400' : 'bg-blue-500/20 text-blue-400'}`}>
                        {t.mode}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Recent Trades */}
      <div className="card">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold">Recent Trades</h2>
          <Link href="/history" className="text-brand-400 text-sm hover:underline">View All →</Link>
        </div>
        {recentTrades.length === 0 ? (
          <p className="text-slate-500 text-sm">No trade history yet. Start paper trading to see results here.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-slate-400 border-b border-[#2a3a52]">
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
                  return (
                    <tr key={t.id} className="border-b border-[#2a3a52]/50 hover:bg-[#2a3a52]/20">
                      <td className="py-3 px-2 font-medium">{t.pair}</td>
                      <td className="py-3 px-2 text-right">{Number(t.entry_price).toFixed(4)}</td>
                      <td className="py-3 px-2 text-right">{Number(t.exit_price).toFixed(4)}</td>
                      <td className={`py-3 px-2 text-right ${profitPct >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                        {profitPct >= 0 ? '+' : ''}{profitPct.toFixed(2)}%
                      </td>
                      <td className={`py-3 px-2 text-right ${Number(t.profit_abs) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                        {Number(t.profit_abs) >= 0 ? '+' : ''}{Number(t.profit_abs).toFixed(2)}
                      </td>
                      <td className="py-3 px-2 text-slate-400">{t.exit_reason || ''}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
