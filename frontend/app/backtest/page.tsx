'use client';

import { useEffect, useState } from 'react';
import { api } from '@/lib/api';
import MetricCard from '@/components/ui/MetricCard';
import LoadingSpinner from '@/components/ui/LoadingSpinner';
import SignalContextPanel from '@/components/dashboard/SignalContextPanel';
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
  BarChart, Bar, Cell,
} from 'recharts';

// ─── Time-range helpers ────────────────────────────────────────────────────────
const PRESETS = [
  { label: '1M',  days: 30,   note: '' },
  { label: '3M',  days: 90,   note: '' },
  { label: '6M',  days: 180,  note: '' },
  { label: '1Y',  days: 365,  note: '' },
  { label: '2Y',  days: 730,  note: '~30s download' },
  { label: '5Y',  days: 1825, note: '~2 min download' },
  { label: '10Y', days: 3650, note: '~5 min download' },
  { label: 'Custom', days: 0, note: '' },
];

function toYMD(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}${m}${day}`;
}

function fromYMD(s: string): string {
  // "20240101" → "Jan 1, 2024"
  if (s.length !== 8) return s;
  const d = new Date(
    Number(s.slice(0, 4)),
    Number(s.slice(4, 6)) - 1,
    Number(s.slice(6, 8))
  );
  return d.toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' });
}

function buildTimerange(days: number): string {
  const end = new Date();
  const start = new Date();
  start.setDate(end.getDate() - days);
  return `${toYMD(start)}-${toYMD(end)}`;
}

// ─────────────────────────────────────────────────────────────────────────────

export default function BacktestPage() {
  const [strategies, setStrategies] = useState<Record<string, unknown>[]>([]);
  const [strategyId, setStrategyId] = useState<number | null>(null);
  const [selectedPreset, setSelectedPreset] = useState('1Y');
  const [timerange, setTimerange] = useState(() => buildTimerange(365));
  const [customRange, setCustomRange] = useState('');
  const [pairs, setPairs] = useState<string[]>(['BTC/USDT']);
  const [pairQuery, setPairQuery] = useState('');
  const [availablePairs, setAvailablePairs] = useState<string[]>([]);
  const [pairsLoading, setPairsLoading] = useState(false);
  const [pairsError, setPairsError] = useState('');
  const [showPairDropdown, setShowPairDropdown] = useState(false);
  const [timeframe, setTimeframe] = useState('15m');
  const [startingBalance, setStartingBalance] = useState(1000);
  const [stoploss, setStoploss] = useState(3);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    api.strategy.list().then((d) => {
      setStrategies(d.strategies);
      if (d.strategies.length > 0) setStrategyId(Number(d.strategies[0].id));
    }).catch(() => {});

    const qs = new URLSearchParams(window.location.search);
    const qPair = qs.get('pair');
    const qTf = qs.get('timeframe');
    const qStrat = qs.get('strategy');
    if (qPair) setPairs([qPair]);
    if (qTf) setTimeframe(qTf);
    if (qStrat) {
      api.strategy.list().then((d) => {
        // Try matching by: class name in code, strategy name, or label
        const match = d.strategies.find((s: Record<string, unknown>) =>
          String(s.generated_code || '').includes(`class ${qStrat}(`) ||
          String(s.name || '').toLowerCase() === qStrat.toLowerCase() ||
          String(s.name || '').toLowerCase().includes(qStrat.toLowerCase())
        );
        if (match) setStrategyId(Number(match.id));
      });
    }

    setPairsLoading(true);
    api.market.pairs()
      .then((d) => {
        const anyD = d as Record<string, unknown>;
        if (anyD.error) setPairsError(String(anyD.error));
        setAvailablePairs(d.pairs || []);
      })
      .catch((e) => setPairsError(String(e)))
      .finally(() => setPairsLoading(false));
  }, []);

  function selectPreset(label: string, days: number) {
    setSelectedPreset(label);
    if (label !== 'Custom') {
      setTimerange(buildTimerange(days));
    }
  }

  const filteredPairs = availablePairs.filter(
    (p) => p.toLowerCase().includes(pairQuery.toLowerCase()) && !pairs.includes(p)
  ).slice(0, 50);

  function addPair(p: string) {
    if (!pairs.includes(p)) setPairs([...pairs, p]);
    setPairQuery('');
    setShowPairDropdown(false);
  }
  function removePair(p: string) {
    setPairs(pairs.filter((x) => x !== p));
  }

  async function runBacktest() {
    if (!strategyId) return;
    setRunning(true);
    setResult(null);
    setError('');

    const activeRange = selectedPreset === 'Custom' ? customRange : timerange;

    try {
      const data = await api.backtest.run({
        strategy_id: strategyId,
        timerange: activeRange,
        pairs,
        timeframe,
        starting_balance: startingBalance,
        stoploss: -(stoploss / 100),
      }) as Record<string, unknown>;

      if (data.error) {
        setError(String(data.error));
      } else {
        setResult(data);
      }
    } catch (e) {
      setError(String(e));
    }
    setRunning(false);
  }

  // Read URL params for pre-fill banner and signal context
  const qs = typeof window !== 'undefined' ? new URLSearchParams(window.location.search) : null;
  const fromOpportunity = qs?.get('pair') || null;
  const sigAction = qs?.get('action');
  const sigScore = qs?.get('score');
  const sigRsi = qs?.get('rsi');
  const sigAdx = qs?.get('adx');
  const sigMacd = qs?.get('macd');
  const sigBbPos = qs?.get('bb_pos');
  const sigVolChange = qs?.get('vol_change');
  const sigEntryQuality = qs?.get('entry_quality');
  const sigConfidence = qs?.get('confidence');
  const sigReasoning = qs?.get('reasoning');
  const hasSignalData = !!(sigRsi || sigAdx || sigMacd);

  const metrics = result?.metrics as Record<string, unknown> | undefined;
  const trades = (result?.trades as Record<string, unknown>[]) || [];

  const equityCurve = trades.reduce(
    (acc: { trade: number; equity: number }[], t, i) => {
      const prev = acc.length > 0 ? acc[acc.length - 1].equity : startingBalance;
      acc.push({ trade: i + 1, equity: prev + (Number(t.profit_abs) || 0) });
      return acc;
    },
    [{ trade: 0, equity: startingBalance }]
  );

  // Parse active timerange for display
  const activeRange = selectedPreset === 'Custom' ? customRange : timerange;
  const [rangeStart, rangeEnd] = activeRange.split('-');
  const currentPreset = PRESETS.find((p) => p.label === selectedPreset);

  return (
    <div className="max-w-6xl mx-auto">
      <h1 className="heading-xl mb-2">Backtesting</h1>
      <p className="text-slate-400 mb-6 text-sm sm:text-base">
        Test your strategy on real historical KuCoin OHLCV data — up to 10 years back
      </p>

      {/* Signal Context from Opportunities */}
      {fromOpportunity && hasSignalData && (
        <SignalContextPanel
          pair={fromOpportunity}
          strategy={qs?.get('strategy')}
          timeframe={qs?.get('timeframe')}
          score={sigScore}
          action={sigAction}
          rsi={sigRsi}
          adx={sigAdx}
          macd={sigMacd}
          bbPos={sigBbPos}
          volChange={sigVolChange}
          entryQuality={sigEntryQuality}
          confidence={sigConfidence}
          reasoning={sigReasoning}
        />
      )}

      {/* Simple pre-fill banner if no indicator data */}
      {fromOpportunity && !hasSignalData && (
        <div className="mb-4 p-4 rounded-xl border border-brand-500/40 bg-brand-500/10 flex items-start gap-3">
          <span className="text-2xl">🎯</span>
          <div className="flex-1">
            <p className="text-brand-300 font-semibold text-sm">Pre-filled from Opportunities</p>
            <p className="text-slate-400 text-xs mt-1">
              Pair, strategy, and timeframe have been set. Hit <strong>Run Backtest</strong> to see historical performance.
            </p>
          </div>
          <a href="/opportunities" className="text-xs text-slate-400 hover:text-white underline shrink-0">← Back</a>
        </div>
      )}

      {/* Explain score vs backtest divergence (always show when from opportunities) */}
      {fromOpportunity && (
        <div className="mb-6 p-3 rounded-lg border border-yellow-500/20 bg-yellow-500/5 flex items-start gap-2">
          <span className="text-base shrink-0">💡</span>
          <div className="text-xs text-slate-400 space-y-1">
            <p>
              <strong className="text-yellow-300">Why might the backtest differ from the Opportunities score?</strong>{' '}
              The scanner measures <em>current live indicators</em> (what&apos;s happening right now). The backtest tests <em>historical performance</em> over months/years. Market regimes change — try 3M or 6M instead of 1Y, or lower the stop-loss.
            </p>
            <div className="flex flex-wrap gap-1.5 mt-1.5">
              <span className="px-2 py-0.5 rounded-full bg-slate-700 text-slate-300">💡 Try 3M or 6M</span>
              <span className="px-2 py-0.5 rounded-full bg-slate-700 text-slate-300">💡 Lower SL to 2%</span>
              <span className="px-2 py-0.5 rounded-full bg-slate-700 text-slate-300">💡 Try BTC/USDT</span>
              <span className="px-2 py-0.5 rounded-full bg-slate-700 text-slate-300">💡 Try a different strategy</span>
            </div>
          </div>
        </div>
      )}

      {/* Config card */}
      <div className="card mb-8">

        {/* ── Historical Period ──────────────────────────────────────────── */}
        <div className="mb-6">
          <label className="label mb-2">Historical Period</label>

          {/* Preset chips */}
          <div className="flex flex-wrap gap-2 mb-3">
            {PRESETS.map((p) => (
              <button
                key={p.label}
                onClick={() => selectPreset(p.label, p.days)}
                className={`relative px-4 py-2 rounded-xl text-sm font-semibold border transition-all ${
                  selectedPreset === p.label
                    ? 'bg-brand-600/30 border-brand-500 text-brand-200 shadow-lg shadow-brand-500/10'
                    : 'bg-[#1a2236] border-[#2a3a52] text-slate-400 hover:text-white hover:border-slate-500'
                }`}
              >
                {p.label}
                {p.note && (
                  <span className="absolute -top-1.5 -right-1 text-[9px] bg-amber-500/20 text-amber-400 border border-amber-500/30 px-1 rounded-full whitespace-nowrap">
                    {p.note}
                  </span>
                )}
              </button>
            ))}
          </div>

          {/* Date range display / custom input */}
          {selectedPreset === 'Custom' ? (
            <div>
              <input
                className="input max-w-xs"
                value={customRange}
                onChange={(e) => setCustomRange(e.target.value)}
                placeholder="YYYYMMDD-YYYYMMDD e.g. 20230101-20240101"
              />
              <p className="text-xs text-slate-500 mt-1">
                Format: <code className="text-slate-400">YYYYMMDD-YYYYMMDD</code>
              </p>
            </div>
          ) : (
            <div className="flex items-center gap-2 text-sm">
              <span className="bg-[#0a0f1c] border border-[#2a3a52] rounded-lg px-3 py-1.5 text-slate-300 font-mono text-xs">
                {fromYMD(rangeStart)} → {fromYMD(rangeEnd)}
              </span>
              <span className="text-slate-500 text-xs">({activeRange})</span>
            </div>
          )}

          {/* Download warning for long ranges */}
          {(selectedPreset === '5Y' || selectedPreset === '10Y') && (
            <div className="mt-3 flex items-start gap-2 p-3 rounded-lg bg-amber-500/10 border border-amber-500/30">
              <span className="text-amber-400 mt-0.5">⚠️</span>
              <div className="text-xs text-amber-300">
                <strong>{selectedPreset} of data</strong> needs to be downloaded from KuCoin on first run
                ({selectedPreset === '5Y' ? '~2 minutes' : '~5 minutes'} for 15m candles).
                Data is cached locally — future runs on the same pair/timeframe are instant.
              </div>
            </div>
          )}
        </div>

        {/* ── Rest of config ─────────────────────────────────────────────── */}
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-4 mb-6">
          <div className="col-span-2">
            <label className="label">Strategy</label>
            <select className="input" value={strategyId || ''} onChange={(e) => setStrategyId(Number(e.target.value))}>
              {strategies.map((s) => (
                <option key={String(s.id)} value={String(s.id)}>{String(s.name)}</option>
              ))}
            </select>
          </div>

          <div className="col-span-2 md:col-span-3 lg:col-span-2 relative">
            <label className="label">
              Pairs
              {pairsLoading && <span className="text-[10px] text-slate-500 ml-2">loading…</span>}
              {pairsError && <span className="text-[10px] text-red-400 ml-2">({pairsError})</span>}
              {!pairsLoading && !pairsError && availablePairs.length > 0 && (
                <span className="text-[10px] text-slate-500 ml-2">{availablePairs.length} KuCoin USDT pairs</span>
              )}
            </label>
            <div className="input flex flex-wrap gap-1.5 min-h-[42px] items-center">
              {pairs.map((p) => (
                <span key={p} className="flex items-center gap-1 px-2 py-0.5 bg-brand-600/20 text-brand-300 border border-brand-500/30 rounded text-xs">
                  {p}
                  <button type="button" onClick={() => removePair(p)} className="text-brand-300/60 hover:text-white ml-0.5">×</button>
                </span>
              ))}
              <input
                className="flex-1 bg-transparent outline-none text-sm min-w-[120px]"
                value={pairQuery}
                onChange={(e) => { setPairQuery(e.target.value); setShowPairDropdown(true); }}
                onFocus={() => setShowPairDropdown(true)}
                onBlur={() => setTimeout(() => setShowPairDropdown(false), 150)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && pairQuery.trim()) {
                    e.preventDefault();
                    const typed = pairQuery.trim().toUpperCase();
                    const match = availablePairs.find((p) => p.toUpperCase() === typed) || typed;
                    addPair(match);
                  }
                  if (e.key === 'Backspace' && !pairQuery && pairs.length) {
                    removePair(pairs[pairs.length - 1]);
                  }
                }}
                placeholder={pairs.length === 0 ? 'Search coin (e.g. SOL, ARB)…' : ''}
              />
            </div>
            {showPairDropdown && filteredPairs.length > 0 && (
              <div className="absolute z-20 mt-1 w-full max-h-64 overflow-y-auto bg-[#1a2236] border border-[#2a3a52] rounded-lg shadow-xl">
                {filteredPairs.map((p) => (
                  <button
                    type="button"
                    key={p}
                    onMouseDown={(e) => { e.preventDefault(); addPair(p); }}
                    className="w-full text-left px-3 py-2 text-sm hover:bg-[#2a3a52]/60 border-b border-[#2a3a52]/40 last:border-0"
                  >
                    {p}
                  </button>
                ))}
              </div>
            )}
          </div>

          <div>
            <label className="label">Timeframe</label>
            <select className="input" value={timeframe} onChange={(e) => setTimeframe(e.target.value)}>
              {['5m', '15m', '30m', '1h', '4h', '1d'].map((tf) => (
                <option key={tf} value={tf}>{tf}</option>
              ))}
            </select>
          </div>
        </div>

        <div className="flex items-center gap-4 flex-wrap">
          <div className="flex-1 min-w-[160px]">
            <label className="label">Balance (USDT)</label>
            <input type="number" className="input" value={startingBalance} onChange={(e) => setStartingBalance(Number(e.target.value))} />
          </div>
          <div className="flex-1 min-w-[200px]">
            <label className="label">Stop-Loss: {stoploss}%</label>
            <input type="range" min={1} max={10} step={0.5} value={stoploss} onChange={(e) => setStoploss(Number(e.target.value))} className="w-full accent-brand-500 mt-2" />
          </div>
          <div className="flex items-end">
            <button
              onClick={runBacktest}
              disabled={running || !strategyId || (selectedPreset === 'Custom' && !customRange)}
              className="btn-primary px-8 py-3 text-base"
            >
              {running
                ? `Running ${currentPreset && currentPreset.days > 365 ? '(downloading data…)' : ''}…`
                : `▶ Run ${selectedPreset} Backtest`}
            </button>
          </div>
        </div>

        {/* Summary row */}
        {!running && strategyId && (
          <div className="mt-4 pt-4 border-t border-[#2a3a52] text-xs text-slate-500 flex flex-wrap gap-x-4 gap-y-1">
            <span>📅 Period: <span className="text-slate-300">{selectedPreset === 'Custom' ? customRange : selectedPreset}</span></span>
            <span>📊 Pairs: <span className="text-slate-300">{pairs.join(', ')}</span></span>
            <span>⏱ Timeframe: <span className="text-slate-300">{timeframe}</span></span>
            <span>💰 Balance: <span className="text-slate-300">${startingBalance}</span></span>
            <span>🛑 Stop-loss: <span className="text-slate-300">{stoploss}%</span></span>
          </div>
        )}
      </div>

      {running && (
        <LoadingSpinner
          text={
            currentPreset && currentPreset.days > 365
              ? `Downloading ${selectedPreset} of historical data from KuCoin, then running backtest…`
              : 'Running backtest on historical data…'
          }
        />
      )}

      {error && (
        <div className="card mb-8 border-red-500/30 bg-red-500/10">
          <p className="text-red-400">{error}</p>
        </div>
      )}

      {metrics && (
        <>
          {/* Header */}
          <div className="flex items-center justify-between mb-4 flex-wrap gap-2">
            <h2 className="text-xl font-semibold">
              Results — {selectedPreset} backtest
              <span className="text-sm font-normal text-slate-400 ml-2">
                {fromYMD(rangeStart)} → {fromYMD(rangeEnd)}
              </span>
            </h2>
            <span className="text-xs text-slate-500 bg-[#1a2236] px-3 py-1 rounded-full border border-[#2a3a52]">
              {pairs.join(', ')} · {timeframe} · ${startingBalance}
            </span>
          </div>

          {/* Metrics */}
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4 mb-8">
            <MetricCard
              title="Total Profit"
              value={`${Number(metrics.total_profit) >= 0 ? '+' : ''}${Number(metrics.total_profit).toFixed(2)}%`}
              color={Number(metrics.total_profit) >= 0 ? 'profit' : 'loss'}
            />
            <MetricCard title="Win Rate" value={`${(Number(metrics.win_rate) * 100).toFixed(1)}%`} />
            <MetricCard title="Max Drawdown" value={`${Number(metrics.max_drawdown).toFixed(2)}%`} color="loss" />
            <MetricCard title="Sharpe Ratio" value={Number(metrics.sharpe_ratio).toFixed(2)} />
            <MetricCard title="Total Trades" value={Number(metrics.total_trades)} />
            <MetricCard title="Avg Duration" value={String(metrics.avg_duration)} />
          </div>

          {/* Equity Curve */}
          <div className="card mb-8">
            <h2 className="text-lg font-semibold mb-4">Equity Curve</h2>
            <ResponsiveContainer width="100%" height={300}>
              <LineChart data={equityCurve}>
                <CartesianGrid strokeDasharray="3 3" stroke="#2a3a52" />
                <XAxis dataKey="trade" stroke="#64748b" fontSize={12} label={{ value: 'Trade #', position: 'insideBottom', offset: -2, fill: '#64748b', fontSize: 11 }} />
                <YAxis stroke="#64748b" fontSize={12} tickFormatter={(v) => `$${v.toLocaleString()}`} />
                <Tooltip
                  contentStyle={{ background: '#1a2236', border: '1px solid #2a3a52', borderRadius: 8, color: '#f1f5f9' }}
                  formatter={(v: number) => [`$${v.toFixed(2)}`, 'Portfolio']}
                />
                <Line type="monotone" dataKey="equity" stroke="#3391ff" strokeWidth={2} dot={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>

          {/* Profit Distribution */}
          {trades.length > 0 && (
            <div className="card mb-8">
              <h2 className="text-lg font-semibold mb-4">Profit Distribution per Trade</h2>
              <ResponsiveContainer width="100%" height={200}>
                <BarChart data={trades.map((t, i) => ({ trade: i + 1, profit: Number(t.profit_pct) || 0 }))}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#2a3a52" />
                  <XAxis dataKey="trade" stroke="#64748b" fontSize={12} />
                  <YAxis stroke="#64748b" fontSize={12} tickFormatter={(v) => `${v}%`} />
                  <Tooltip
                    contentStyle={{ background: '#1a2236', border: '1px solid #2a3a52', borderRadius: 8, color: '#f1f5f9' }}
                    formatter={(v: number) => [`${v.toFixed(2)}%`, 'Profit']}
                  />
                  <Bar dataKey="profit">
                    {trades.map((t, i) => (
                      <Cell key={i} fill={Number(t.profit_pct) >= 0 ? '#22c55e' : '#ef4444'} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}

          {/* Trade Table */}
          <div className="card">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-lg font-semibold">Trade Details</h2>
              <span className="text-xs text-slate-500">{trades.length} trades</span>
            </div>
            <div className="overflow-x-auto max-h-[400px] overflow-y-auto">
              <table className="w-full text-sm">
                <thead className="sticky top-0 bg-[#1a2236]">
                  <tr className="text-slate-400 border-b border-[#2a3a52]">
                    <th className="text-left py-3 px-2">#</th>
                    <th className="text-left py-3 px-2">Pair</th>
                    <th className="text-right py-3 px-2">Entry</th>
                    <th className="text-right py-3 px-2">Exit</th>
                    <th className="text-right py-3 px-2">Profit %</th>
                    <th className="text-right py-3 px-2">Profit USDT</th>
                    <th className="text-left py-3 px-2">Open Date</th>
                    <th className="text-left py-3 px-2">Duration</th>
                    <th className="text-left py-3 px-2">Exit Reason</th>
                  </tr>
                </thead>
                <tbody>
                  {trades.map((t, i) => (
                    <tr key={i} className="border-b border-[#2a3a52]/50 hover:bg-[#2a3a52]/20">
                      <td className="py-2 px-2 text-slate-500">{i + 1}</td>
                      <td className="py-2 px-2 font-medium">{String(t.pair || '')}</td>
                      <td className="py-2 px-2 text-right font-mono text-xs">{Number(t.open_rate || 0).toFixed(4)}</td>
                      <td className="py-2 px-2 text-right font-mono text-xs">{Number(t.close_rate || 0).toFixed(4)}</td>
                      <td className={`py-2 px-2 text-right font-semibold ${Number(t.profit_pct) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                        {Number(t.profit_pct) >= 0 ? '+' : ''}{Number(t.profit_pct || 0).toFixed(2)}%
                      </td>
                      <td className={`py-2 px-2 text-right ${Number(t.profit_abs) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                        {Number(t.profit_abs || 0).toFixed(2)}
                      </td>
                      <td className="py-2 px-2 text-slate-400 text-xs">{String(t.open_date || '').slice(0, 10)}</td>
                      <td className="py-2 px-2 text-slate-400">{String(t.trade_duration || '')}</td>
                      <td className="py-2 px-2 text-slate-500 text-xs">{String(t.exit_reason || '')}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
