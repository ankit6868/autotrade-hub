'use client';

import { useEffect, useState } from 'react';
import { api } from '@/lib/api';
import MetricCard from '@/components/ui/MetricCard';
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
  BarChart, Bar, Cell, PieChart, Pie, ReferenceLine, Area, AreaChart,
} from 'recharts';

// ── Helpers ──────────────────────────────────────────────────────────────────
function fmt(n: number, dec = 2) {
  return `${n >= 0 ? '+' : ''}${n.toFixed(dec)}`;
}
function color(n: number) { return n >= 0 ? 'text-emerald-400' : 'text-red-400'; }

const CHART_STYLE = {
  contentStyle: { background: '#1a2236', border: '1px solid #2a3a52', borderRadius: 8, color: '#f1f5f9' },
};

function RatioCard({ label, value, hint, good }: { label: string; value: string; hint: string; good: boolean | null }) {
  const textColor = good === null ? 'text-white' : good ? 'text-emerald-400' : 'text-red-400';
  return (
    <div className="p-4 rounded-xl border border-[#2a3a52] bg-[#0f1a2e] flex flex-col gap-1">
      <p className="text-xs text-slate-400">{label}</p>
      <p className={`text-xl font-bold ${textColor}`}>{value}</p>
      <p className="text-xs text-slate-500">{hint}</p>
    </div>
  );
}

// ── Main Component ────────────────────────────────────────────────────────────
export default function HistoryPage() {
  const [trades, setTrades] = useState<Record<string, unknown>[]>([]);
  const [portfolio, setPortfolio] = useState<Record<string, unknown> | null>(null);
  const [strategies, setStrategies] = useState<Record<string, unknown>[]>([]);
  const [filterMode, setFilterMode] = useState<string>('');
  const [filterStrategy, setFilterStrategy] = useState<string>('');
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState<'overview' | 'monthly' | 'pairs' | 'trades'>('overview');

  useEffect(() => { loadData(); }, [filterMode, filterStrategy]);
  useEffect(() => { api.strategy.list().then((d) => setStrategies(d.strategies)).catch(() => {}); }, []);

  async function loadData() {
    setLoading(true);
    try {
      const params: Record<string, string> = { limit: '500' };
      if (filterMode) params.mode = filterMode;
      if (filterStrategy) params.strategy_id = String(parseInt(filterStrategy, 10));
      const [histData, portData] = await Promise.all([
        api.trade.history(params),
        api.analysis.portfolio(),
      ]);
      setTrades(histData.trades);
      setPortfolio(portData);
    } catch {}
    setLoading(false);
  }

  // ── Derived stats from trade list (for filtered view) ──────────────────
  const totalPnl = trades.reduce((s, t) => s + (Number(t.profit_abs) || 0), 0);
  const wins = trades.filter((t) => Number(t.profit_abs) > 0);
  const losses = trades.filter((t) => Number(t.profit_abs) <= 0);
  const winRate = trades.length > 0 ? (wins.length / trades.length) * 100 : 0;
  const avgWin = wins.length > 0 ? wins.reduce((s, t) => s + Number(t.profit_abs), 0) / wins.length : 0;
  const avgLoss = losses.length > 0 ? losses.reduce((s, t) => s + Number(t.profit_abs), 0) / losses.length : 0;
  const bestTrade = trades.length > 0 ? Math.max(...trades.map((t) => Number(t.profit_abs) || 0)) : 0;
  const worstTrade = trades.length > 0 ? Math.min(...trades.map((t) => Number(t.profit_abs) || 0)) : 0;

  // Equity curve from portfolio API (all-time)
  const equityCurve = ((portfolio?.equity_curve as Record<string,unknown>[]) || []).map((p, i) => ({
    trade: i + 1,
    pnl: Number(p.pnl) || 0,
    pair: String(p.pair || ''),
  }));

  // Monthly P&L
  const monthly = (portfolio?.monthly as Record<string,unknown>[]) || [];

  // Profit per trade (for filtered trades)
  const profitDist = trades.slice().reverse().map((t, i) => ({
    trade: i + 1,
    profit: Number(t.profit_pct) || 0,
    pair: String(t.pair || ''),
  }));

  // By pair
  const pairMap: Record<string, number> = {};
  trades.forEach((t) => {
    const p = String(t.pair || 'Unknown');
    pairMap[p] = (pairMap[p] || 0) + (Number(t.profit_abs) || 0);
  });
  const profitByPair = Object.entries(pairMap)
    .map(([pair, profit]) => ({ pair, profit }))
    .sort((a, b) => b.profit - a.profit);

  // Pie
  const pieData = [
    { name: 'Wins', value: wins.length, fill: '#22c55e' },
    { name: 'Losses', value: losses.length, fill: '#ef4444' },
  ];

  // Advanced ratios from portfolio
  const ratios = (portfolio?.ratios as Record<string,unknown>) || {};
  const sharpe = Number(ratios.sharpe_ratio || 0);
  const sortino = Number(ratios.sortino_ratio || 0);
  const calmar = Number(ratios.calmar_ratio || 0);
  const profitFactor = Number(ratios.profit_factor || 0);
  const maxDdPct = Number(ratios.max_drawdown_pct || 0);
  const maxDdAbs = Number(ratios.max_drawdown_abs || 0);
  const avgDurMin = Number(ratios.avg_trade_duration_min || 0);
  const expectancy = Number(ratios.expectancy || 0);

  function exportCSV() {
    const header = 'Pair,Side,Entry Price,Exit Price,Profit %,Profit USDT,Entry Time,Exit Time,Reason,Mode\n';
    const rows = trades.map((t) =>
      `${t.pair},${t.side},${t.entry_price},${t.exit_price},${t.profit_pct},${t.profit_abs},${t.entry_time},${t.exit_time},${t.exit_reason},${t.mode}`
    ).join('\n');
    const blob = new Blob([header + rows], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a'); a.href = url;
    a.download = `trades_${new Date().toISOString().slice(0,10)}.csv`; a.click();
    URL.revokeObjectURL(url);
  }

  const tabs = [
    { key: 'overview', label: '📊 Overview' },
    { key: 'monthly', label: '📅 Monthly P&L' },
    { key: 'pairs', label: '🪙 By Pair' },
    { key: 'trades', label: '📋 All Trades' },
  ] as const;

  return (
    <div className="max-w-6xl mx-auto">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-6">
        <div>
          <h1 className="heading-xl">Trade History & Analytics</h1>
          <p className="text-slate-400 mt-1 text-sm">{trades.length} trades · {loading ? 'Loading...' : 'Up to date'}</p>
        </div>
        <button onClick={exportCSV} disabled={trades.length === 0} className="btn-secondary self-start sm:self-auto">
          ⬇ Export CSV
        </button>
      </div>

      {/* Filters */}
      <div className="card mb-6">
        <div className="flex flex-wrap gap-4 items-end">
          <div>
            <label className="label">Mode</label>
            <select className="input" value={filterMode} onChange={(e) => setFilterMode(e.target.value)}>
              <option value="">All</option>
              <option value="paper">Paper</option>
              <option value="live">Live</option>
            </select>
          </div>
          <div>
            <label className="label">Strategy</label>
            <select className="input" value={filterStrategy} onChange={(e) => setFilterStrategy(e.target.value)}>
              <option value="">All</option>
              {strategies.map((s) => (
                <option key={String(s.id)} value={String(s.id)}>{String(s.name)}</option>
              ))}
            </select>
          </div>
          {(filterMode || filterStrategy) && (
            <button onClick={() => { setFilterMode(''); setFilterStrategy(''); }} className="text-xs text-slate-400 hover:text-white underline mb-1">
              Clear filters
            </button>
          )}
        </div>
      </div>

      {/* Core Metrics */}
      <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3 mb-6">
        <MetricCard title="Total P&L" value={`${fmt(totalPnl)} USDT`} color={totalPnl >= 0 ? 'profit' : 'loss'} />
        <MetricCard title="Win Rate" value={`${winRate.toFixed(1)}%`} subtitle={`${wins.length}W / ${losses.length}L`} />
        <MetricCard title="Avg Win" value={`${fmt(avgWin)} USDT`} color="profit" />
        <MetricCard title="Avg Loss" value={`${avgLoss.toFixed(2)} USDT`} color="loss" />
        <MetricCard title="Best Trade" value={`${fmt(bestTrade)} USDT`} color="profit" />
        <MetricCard title="Worst Trade" value={`${worstTrade.toFixed(2)} USDT`} color="loss" />
      </div>

      {/* Advanced Ratio Cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-6">
        <RatioCard
          label="Sharpe Ratio"
          value={sharpe.toFixed(2)}
          hint={sharpe >= 1 ? 'Good risk-adjusted return' : sharpe >= 0 ? 'Below target (aim >1)' : 'Poor — losing money'}
          good={sharpe >= 1 ? true : sharpe >= 0 ? null : false}
        />
        <RatioCard
          label="Sortino Ratio"
          value={sortino.toFixed(2)}
          hint={sortino >= 1.5 ? 'Strong downside protection' : 'Aim for >1.5'}
          good={sortino >= 1.5 ? true : sortino >= 0.5 ? null : false}
        />
        <RatioCard
          label="Calmar Ratio"
          value={calmar.toFixed(2)}
          hint={calmar >= 1 ? 'Good return vs drawdown' : 'Aim for >1'}
          good={calmar >= 1 ? true : calmar >= 0 ? null : false}
        />
        <RatioCard
          label="Profit Factor"
          value={profitFactor.toFixed(2)}
          hint={profitFactor >= 1.5 ? 'Profitable' : profitFactor >= 1 ? 'Breakeven' : 'Losing'}
          good={profitFactor >= 1.5 ? true : profitFactor >= 1 ? null : false}
        />
        <RatioCard
          label="Max Drawdown"
          value={`-${maxDdPct.toFixed(1)}%`}
          hint={`${maxDdAbs.toFixed(2)} USDT absolute`}
          good={maxDdPct < 10 ? true : maxDdPct < 20 ? null : false}
        />
        <RatioCard
          label="Expectancy"
          value={`${fmt(expectancy)} USDT`}
          hint="Expected profit per trade"
          good={expectancy > 0 ? true : null}
        />
        <RatioCard
          label="Avg Duration"
          value={avgDurMin < 60 ? `${avgDurMin.toFixed(0)}m` : `${(avgDurMin/60).toFixed(1)}h`}
          hint="Average trade hold time"
          good={null}
        />
        <RatioCard
          label="Total Trades"
          value={String(trades.length)}
          hint={`${wins.length} winning · ${losses.length} losing`}
          good={null}
        />
      </div>

      {/* Tabs */}
      <div className="flex gap-1 mb-6 border-b border-[#2a3a52]">
        {tabs.map((t) => (
          <button
            key={t.key}
            onClick={() => setActiveTab(t.key)}
            className={`px-4 py-2 text-sm font-medium rounded-t transition-colors ${
              activeTab === t.key
                ? 'bg-brand-500/20 text-brand-400 border-b-2 border-brand-500'
                : 'text-slate-400 hover:text-white'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* Tab: Overview */}
      {activeTab === 'overview' && (
        <div className="space-y-6">
          {/* Equity Curve */}
          <div className="card">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-lg font-semibold">📈 Equity Curve</h2>
              <span className="text-xs text-slate-400">All-time cumulative P&L</span>
            </div>
            {equityCurve.length > 1 ? (
              <ResponsiveContainer width="100%" height={260}>
                <AreaChart data={equityCurve}>
                  <defs>
                    <linearGradient id="pnlGrad" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor={totalPnl >= 0 ? '#22c55e' : '#ef4444'} stopOpacity={0.3} />
                      <stop offset="95%" stopColor={totalPnl >= 0 ? '#22c55e' : '#ef4444'} stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" stroke="#2a3a52" />
                  <XAxis dataKey="trade" stroke="#64748b" fontSize={11} label={{ value: 'Trade #', position: 'insideBottom', offset: -2, fill: '#64748b', fontSize: 11 }} />
                  <YAxis stroke="#64748b" fontSize={11} tickFormatter={(v) => `${v >= 0 ? '+' : ''}${v.toFixed(1)}`} />
                  <Tooltip
                    {...CHART_STYLE}
                    formatter={(v: number) => [`${fmt(v, 2)} USDT`, 'Cumulative P&L']}
                  />
                  <ReferenceLine y={0} stroke="#64748b" strokeDasharray="4 4" />
                  <Area type="monotone" dataKey="pnl" stroke={totalPnl >= 0 ? '#22c55e' : '#ef4444'} strokeWidth={2} fill="url(#pnlGrad)" dot={false} />
                </AreaChart>
              </ResponsiveContainer>
            ) : (
              <p className="text-slate-500 text-sm py-16 text-center">No trade history yet — start paper trading to populate this chart</p>
            )}
          </div>

          {/* Win/Loss + Per-trade Profit */}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            <div className="card">
              <h2 className="text-lg font-semibold mb-4">Win/Loss Distribution</h2>
              {trades.length > 0 ? (
                <ResponsiveContainer width="100%" height={220}>
                  <PieChart>
                    <Pie data={pieData} cx="50%" cy="50%" outerRadius={80} dataKey="value"
                      label={({ name, value, percent }) => `${name}: ${value} (${(percent * 100).toFixed(0)}%)`}
                    >
                      {pieData.map((e, i) => <Cell key={i} fill={e.fill} />)}
                    </Pie>
                    <Tooltip {...CHART_STYLE} />
                  </PieChart>
                </ResponsiveContainer>
              ) : <p className="text-slate-500 text-sm py-16 text-center">No data to display</p>}
            </div>

            <div className="card">
              <h2 className="text-lg font-semibold mb-4">Profit per Trade (%)</h2>
              {profitDist.length > 0 ? (
                <ResponsiveContainer width="100%" height={220}>
                  <BarChart data={profitDist}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#2a3a52" />
                    <XAxis dataKey="trade" stroke="#64748b" fontSize={11} />
                    <YAxis stroke="#64748b" fontSize={11} tickFormatter={(v) => `${v}%`} />
                    <Tooltip {...CHART_STYLE} formatter={(v: number) => [`${v.toFixed(2)}%`, 'Profit']} />
                    <ReferenceLine y={0} stroke="#64748b" />
                    <Bar dataKey="profit">
                      {profitDist.map((d, i) => <Cell key={i} fill={d.profit >= 0 ? '#22c55e' : '#ef4444'} />)}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              ) : <p className="text-slate-500 text-sm py-16 text-center">No data to display</p>}
            </div>
          </div>
        </div>
      )}

      {/* Tab: Monthly P&L */}
      {activeTab === 'monthly' && (
        <div className="space-y-6">
          <div className="card">
            <h2 className="text-lg font-semibold mb-4">📅 Monthly P&L Breakdown</h2>
            {monthly.length > 0 ? (
              <>
                <ResponsiveContainer width="100%" height={260}>
                  <BarChart data={monthly}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#2a3a52" />
                    <XAxis dataKey="month" stroke="#64748b" fontSize={11} />
                    <YAxis stroke="#64748b" fontSize={11} tickFormatter={(v) => `${v >= 0 ? '+' : ''}${v.toFixed(1)}`} />
                    <Tooltip {...CHART_STYLE} formatter={(v: number) => [`${fmt(v, 2)} USDT`, 'P&L']} />
                    <ReferenceLine y={0} stroke="#64748b" strokeDasharray="4 4" />
                    <Bar dataKey="pnl" radius={[4, 4, 0, 0]}>
                      {monthly.map((m, i) => (
                        <Cell key={i} fill={Number((m as Record<string,unknown>).pnl) >= 0 ? '#22c55e' : '#ef4444'} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>

                {/* Monthly table */}
                <div className="mt-4 overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="text-slate-400 border-b border-[#2a3a52]">
                        <th className="text-left py-2 px-3">Month</th>
                        <th className="text-right py-2 px-3">P&L (USDT)</th>
                        <th className="text-right py-2 px-3">Trades</th>
                        <th className="text-right py-2 px-3">Win Rate</th>
                      </tr>
                    </thead>
                    <tbody>
                      {monthly.map((m: Record<string, unknown>) => (
                        <tr key={String(m.month)} className="border-b border-[#2a3a52]/40 hover:bg-[#2a3a52]/20">
                          <td className="py-2 px-3 font-medium">{String(m.month)}</td>
                          <td className={`py-2 px-3 text-right font-medium ${Number(m.pnl) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                            {fmt(Number(m.pnl))} USDT
                          </td>
                          <td className="py-2 px-3 text-right text-slate-300">{String(m.trades)}</td>
                          <td className="py-2 px-3 text-right text-slate-300">
                            {(Number(m.win_rate) * 100).toFixed(1)}%
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            ) : (
              <p className="text-slate-500 text-sm py-16 text-center">No monthly data yet — run some trades first</p>
            )}
          </div>
        </div>
      )}

      {/* Tab: By Pair */}
      {activeTab === 'pairs' && (
        <div className="card">
          <h2 className="text-lg font-semibold mb-4">🪙 Profit by Pair</h2>
          {profitByPair.length > 0 ? (
            <>
              <ResponsiveContainer width="100%" height={Math.max(200, profitByPair.length * 40)}>
                <BarChart data={profitByPair} layout="vertical">
                  <CartesianGrid strokeDasharray="3 3" stroke="#2a3a52" />
                  <XAxis type="number" stroke="#64748b" fontSize={11} tickFormatter={(v) => `${v >= 0 ? '+' : ''}${v.toFixed(1)}`} />
                  <YAxis type="category" dataKey="pair" stroke="#64748b" fontSize={11} width={90} />
                  <Tooltip {...CHART_STYLE} formatter={(v: number) => [`${fmt(v, 2)} USDT`, 'P&L']} />
                  <ReferenceLine x={0} stroke="#64748b" />
                  <Bar dataKey="profit" radius={[0, 4, 4, 0]}>
                    {profitByPair.map((d, i) => <Cell key={i} fill={d.profit >= 0 ? '#22c55e' : '#ef4444'} />)}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>

              <div className="mt-4 overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-slate-400 border-b border-[#2a3a52]">
                      <th className="text-left py-2 px-3">Pair</th>
                      <th className="text-right py-2 px-3">Total P&L</th>
                      <th className="text-right py-2 px-3">Trades</th>
                    </tr>
                  </thead>
                  <tbody>
                    {profitByPair.map(({ pair, profit }) => (
                      <tr key={pair} className="border-b border-[#2a3a52]/40 hover:bg-[#2a3a52]/20">
                        <td className="py-2 px-3 font-medium">{pair}</td>
                        <td className={`py-2 px-3 text-right font-medium ${profit >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                          {fmt(profit)} USDT
                        </td>
                        <td className="py-2 px-3 text-right text-slate-300">
                          {trades.filter((t) => t.pair === pair).length}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          ) : <p className="text-slate-500 text-sm py-16 text-center">No data to display</p>}
        </div>
      )}

      {/* Tab: All Trades */}
      {activeTab === 'trades' && (
        <div className="card">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-semibold">All Trades ({trades.length})</h2>
            <button onClick={exportCSV} disabled={trades.length === 0} className="btn-secondary text-sm">
              ⬇ Export CSV
            </button>
          </div>
          {trades.length === 0 ? (
            <p className="text-slate-500 text-sm">No trades match your filters</p>
          ) : (
            <div className="overflow-x-auto max-h-[600px] overflow-y-auto">
              <table className="w-full text-sm">
                <thead className="sticky top-0 bg-[#1a2236] z-10">
                  <tr className="text-slate-400 border-b border-[#2a3a52]">
                    <th className="text-left py-3 px-2">#</th>
                    <th className="text-left py-3 px-2">Pair</th>
                    <th className="text-left py-3 px-2">Side</th>
                    <th className="text-right py-3 px-2">Entry</th>
                    <th className="text-right py-3 px-2">Exit</th>
                    <th className="text-right py-3 px-2">Duration</th>
                    <th className="text-right py-3 px-2">Profit %</th>
                    <th className="text-right py-3 px-2">Profit USDT</th>
                    <th className="text-left py-3 px-2">Reason</th>
                    <th className="text-left py-3 px-2">Mode</th>
                  </tr>
                </thead>
                <tbody>
                  {trades.map((t, i) => {
                    const profitPct = Number(t.profit_pct || 0);
                    const profitAbs = Number(t.profit_abs || 0);
                    const isProfit = profitAbs >= 0;

                    // Duration
                    let duration = '—';
                    const entryT = t.entry_time ? new Date(String(t.entry_time)).getTime() : 0;
                    const exitT = t.exit_time ? new Date(String(t.exit_time)).getTime() : 0;
                    if (entryT && exitT) {
                      const mins = Math.floor((exitT - entryT) / 60000);
                      duration = mins < 60 ? `${mins}m` : mins < 1440 ? `${Math.floor(mins/60)}h ${mins%60}m` : `${Math.floor(mins/1440)}d`;
                    }

                    return (
                      <tr key={String(t.id)} className="border-b border-[#2a3a52]/40 hover:bg-[#2a3a52]/20">
                        <td className="py-2 px-2 text-slate-500">{i + 1}</td>
                        <td className="py-2 px-2 font-medium">{String(t.pair)}</td>
                        <td className="py-2 px-2">
                          <span className={`text-xs px-2 py-0.5 rounded ${String(t.side) === 'long' ? 'bg-emerald-500/15 text-emerald-400' : 'bg-red-500/15 text-red-400'}`}>
                            {String(t.side)}
                          </span>
                        </td>
                        <td className="py-2 px-2 text-right">{Number(t.entry_price).toFixed(4)}</td>
                        <td className="py-2 px-2 text-right">{Number(t.exit_price || 0).toFixed(4)}</td>
                        <td className="py-2 px-2 text-right text-slate-400 text-xs">{duration}</td>
                        <td className={`py-2 px-2 text-right font-medium ${color(profitPct)}`}>
                          {profitPct >= 0 ? '+' : ''}{profitPct.toFixed(2)}%
                        </td>
                        <td className={`py-2 px-2 text-right font-medium ${color(profitAbs)}`}>
                          {profitAbs >= 0 ? '+' : ''}{profitAbs.toFixed(2)}
                        </td>
                        <td className="py-2 px-2">
                          <span className={`text-xs px-2 py-0.5 rounded-full ${
                            String(t.exit_reason).includes('stop') ? 'bg-red-500/15 text-red-400' :
                            String(t.exit_reason).includes('roi') ? 'bg-emerald-500/15 text-emerald-400' :
                            'bg-slate-500/15 text-slate-400'
                          }`}>
                            {String(t.exit_reason || '—')}
                          </span>
                        </td>
                        <td className="py-2 px-2">
                          <span className={`text-xs px-2 py-0.5 rounded ${
                            String(t.mode) === 'live' ? 'bg-red-500/20 text-red-400' : 'bg-blue-500/20 text-blue-400'
                          }`}>
                            {String(t.mode)}
                          </span>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
