'use client';

import { useEffect, useState, Suspense } from 'react';
import { api } from '@/lib/api';
import MetricCard from '@/components/ui/MetricCard';
import LoadingSpinner from '@/components/ui/LoadingSpinner';
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
  BarChart, Bar, Cell,
} from 'recharts';

// ─── Time-range helpers (same as spot backtest) ───────────────────────────────
const PRESETS = [
  { label: '1W',     days: 7,    note: '' },
  { label: '1M',     days: 30,   note: '' },
  { label: '3M',     days: 90,   note: '' },
  { label: '6M',     days: 180,  note: '' },
  { label: '1Y',     days: 365,  note: '' },
  { label: '2Y',     days: 730,  note: '~30s download' },
  { label: '5Y',     days: 1825, note: '~2 min download' },
  { label: '10Y',    days: 3650, note: '~5 min download' },
  { label: 'Custom', days: 0,    note: '' },
];

function toYMD(d: Date): string {
  const y  = d.getFullYear();
  const m  = String(d.getMonth() + 1).padStart(2, '0');
  const dy = String(d.getDate()).padStart(2, '0');
  return `${y}${m}${dy}`;
}
function fromYMD(s: string): string {
  if (s.length !== 8) return s;
  const d = new Date(Number(s.slice(0, 4)), Number(s.slice(4, 6)) - 1, Number(s.slice(6, 8)));
  return d.toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' });
}
function buildTimerange(days: number): string {
  const end   = new Date();
  const start = new Date();
  if (days === 7) {
    // 1W: align to Monday of current week (matches TradingView's "1W" period)
    const day = end.getDay();                      // 0=Sun … 6=Sat
    const daysToMonday = day === 0 ? 6 : day - 1; // days since last Monday
    start.setDate(end.getDate() - daysToMonday);
  } else {
    start.setDate(end.getDate() - days);
  }
  return `${toYMD(start)}-${toYMD(end)}`;
}

// ─────────────────────────────────────────────────────────────────────────────

function FuturesBacktestInner() {
  // ── Config ─────────────────────────────────────────────────────────────────
  const [strategies,      setStrategies]      = useState<any[]>([]);
  const [strategyId,      setStrategyId]      = useState<number | null>(null);
  const [selectedPreset,  setSelectedPreset]  = useState('1Y');
  const [timerange,       setTimerange]       = useState(() => buildTimerange(365));
  const [customRange,     setCustomRange]     = useState('');
  const [pairs,           setPairs]           = useState<string[]>(['BTC/USDT']);
  const [pairQuery,       setPairQuery]       = useState('');
  const [availablePairs,  setAvailablePairs]  = useState<string[]>([]);
  const [pairsLoading,    setPairsLoading]    = useState(false);
  const [showPairDrop,    setShowPairDrop]    = useState(false);
  const [timeframe,       setTimeframe]       = useState('15m');
  const [startBalance,    setStartBalance]    = useState(1000);
  const [leverage,        setLeverage]        = useState(10);
  const [stoploss,        setStoploss]        = useState(1.5);   // SL ≤ TP for positive R:R
  const [takeProfit,      setTakeProfit]      = useState(3.0);   // TP should be ≥ SL (2:1 R:R)
  // Track WHERE each parameter's current value came from so we can label
  // the control with "from strategy" or "default" — transparent to the
  // user about what was inherited vs what's a fallback we picked.
  type Src = 'strategy' | 'default' | 'manual';
  const [slSrc,  setSlSrc]  = useState<Src>('default');
  const [tpSrc,  setTpSrc]  = useState<Src>('default');
  const [levSrc, setLevSrc] = useState<Src>('default');
  const [tfSrc,  setTfSrc]  = useState<Src>('default');

  // ── State ───────────────────────────────────────────────────────────────────
  const [running,  setRunning]  = useState(false);
  const [result,   setResult]   = useState<any>(null);
  const [history,  setHistory]  = useState<any[]>([]);
  const [error,    setError]    = useState('');

  useEffect(() => {
    api.strategy.list().then(d => {
      setStrategies(d.strategies ?? []);
      if (d.strategies?.length > 0) {
        setStrategyId(Number(d.strategies[0].id));
      }
    }).catch(() => {});

    setPairsLoading(true);
    api.market.pairs()
      .then(d => setAvailablePairs((d as any).pairs ?? []))
      .catch(() => {})
      .finally(() => setPairsLoading(false));

    api.futures.backtest.history()
      .then(d => setHistory(d.backtests ?? []))
      .catch(() => {});
  }, []);

  // Auto-fill from the selected strategy. For each field we record whether
  // the value was actually defined on the strategy ("strategy"), or whether
  // we had to fall back to a sensible futures default ("default"). The
  // source is rendered next to each control so the user can see at a glance
  // what was inherited.
  useEffect(() => {
    if (!strategyId || strategies.length === 0) return;
    const s = strategies.find((x: any) => x.id === strategyId);
    if (!s) return;

    // Stoploss: strategy stores as negative decimal (-0.03 = -3%).
    // Treat null/undefined/0 as "not set on the strategy".
    const rawSl = s.stoploss;
    if (rawSl !== null && rawSl !== undefined && Number(rawSl) !== 0) {
      setStoploss(Math.abs(Number(rawSl) * 100));
      setSlSrc('strategy');
    } else {
      setStoploss(3);
      setSlSrc('default');
    }

    // Take profit: strategy stores as positive decimal (0.015 = 1.5%).
    const rawTp = s.take_profit;
    if (rawTp !== null && rawTp !== undefined && Number(rawTp) > 0) {
      setTakeProfit(Number(rawTp) * 100);
      setTpSrc('strategy');
    } else {
      setTakeProfit(1.5);
      setTpSrc('default');
    }

    // Leverage: strategy stores as integer. We treat 1× as "not set"
    // because that's the SQLAlchemy default — a real futures strategy
    // wouldn't deliberately ship at 1× leverage.
    const rawLev = s.default_leverage;
    if (rawLev !== null && rawLev !== undefined && Number(rawLev) > 1) {
      setLeverage(Number(rawLev));
      setLevSrc('strategy');
    } else {
      setLeverage(10);
      setLevSrc('default');
    }

    // Timeframe.
    if (s.timeframe && s.timeframe !== '15m') {
      setTimeframe(s.timeframe);
      setTfSrc('strategy');
    } else if (s.timeframe === '15m') {
      // Strategy explicitly chose 15m (could be the default too, but we
      // treat the explicit field as authoritative if present).
      setTimeframe('15m');
      setTfSrc('strategy');
    } else {
      setTimeframe('15m');
      setTfSrc('default');
    }
  }, [strategyId, strategies]);

  // Small reusable badge that shows where a field's current value came from.
  function SourceBadge({ src }: { src: Src }) {
    if (src === 'manual') {
      return <span className="ml-2 text-[9px] font-medium px-1.5 py-0.5 rounded-full bg-slate-700/60 text-slate-300">manual</span>;
    }
    if (src === 'strategy') {
      return <span className="ml-2 text-[9px] font-medium px-1.5 py-0.5 rounded-full bg-emerald-500/15 text-emerald-300 border border-emerald-500/30" title="Value inherited from the selected strategy">from strategy</span>;
    }
    return <span className="ml-2 text-[9px] font-medium px-1.5 py-0.5 rounded-full bg-amber-500/10 text-amber-300 border border-amber-500/30" title="Strategy didn't define this field — using sensible futures default">default</span>;
  }

  function selectPreset(label: string, days: number) {
    setSelectedPreset(label);
    if (label !== 'Custom') setTimerange(buildTimerange(days));
  }

  const filteredPairs = availablePairs
    .filter(p => p.toLowerCase().includes(pairQuery.toLowerCase()) && !pairs.includes(p))
    .slice(0, 50);

  function addPair(p: string) {
    if (!pairs.includes(p)) setPairs([...pairs, p]);
    setPairQuery(''); setShowPairDrop(false);
  }
  function removePair(p: string) { setPairs(pairs.filter(x => x !== p)); }

  async function runBacktest() {
    if (!strategyId) return;
    setRunning(true); setResult(null); setError('');
    const activeRange = selectedPreset === 'Custom' ? customRange : timerange;
    try {
      const data = await api.futures.backtest.run({
        strategy_id:      strategyId,
        pairs,
        timeframe,
        timerange:        activeRange,
        leverage,
        starting_balance: startBalance,
        stoploss_pct:     stoploss,
        take_profit_pct:  takeProfit,
      });
      if (data.error) setError(data.error);
      else {
        setResult(data);
        api.futures.backtest.history().then(d => setHistory(d.backtests ?? [])).catch(() => {});
      }
    } catch (e) { setError(String(e)); }
    setRunning(false);
  }

  const activeRange   = selectedPreset === 'Custom' ? customRange : timerange;
  const [rangeStart, rangeEnd] = activeRange.split('-');
  const currentPreset = PRESETS.find(p => p.label === selectedPreset);
  const m             = result?.metrics;
  const trades        = result?.trades ?? [];

  // Build equity curve for chart
  const equityCurve = [{ trade: 0, equity: startBalance },
    ...trades.map((t: any, i: number) => ({
      trade:  i + 1,
      equity: t.balance,
    }))
  ];

  return (
    <div className="max-w-6xl mx-auto">
      {/* Header */}
      <h1 className="heading-xl mb-2">⚡ Futures Backtest</h1>
      <p className="text-slate-400 mb-6 text-sm">
        Test leveraged futures strategies on real KuCoin historical data — up to 10 years back.
        Includes liquidation simulation, funding fees, and long/short breakdown.
      </p>

      {/* Config card */}
      <div className="card mb-8">

        {/* ── Historical Period ──────────────────────────────────────── */}
        <div className="mb-6">
          <label className="label mb-2">Historical Period</label>

          {/* Preset chips */}
          <div className="flex flex-wrap gap-2 mb-3">
            {PRESETS.map(p => (
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

          {/* Date display / custom input */}
          {selectedPreset === 'Custom' ? (
            <div className="space-y-2">
              <div className="flex items-center gap-2 flex-wrap">
                <input
                  className="input max-w-xs font-mono"
                  value={customRange}
                  onChange={e => setCustomRange(e.target.value)}
                  placeholder="e.g. 20240101-20241231"
                />
                {customRange && customRange.includes('-') && customRange.length === 17 && (
                  <span className="text-xs text-emerald-400">
                    ✅ {fromYMD(customRange.split('-')[0])} → {fromYMD(customRange.split('-')[1])}
                  </span>
                )}
              </div>
              <p className="text-xs text-slate-500">
                Format: <code className="text-slate-300">YYYYMMDD-YYYYMMDD</code>
                &nbsp;·&nbsp; Example quick picks:
              </p>
              <div className="flex flex-wrap gap-2">
                {[
                  { label: 'Jan–Mar 2024', range: '20240101-20240331' },
                  { label: 'Q2 2024',      range: '20240401-20240630' },
                  { label: 'Bull run 2024',range: '20241001-20241231' },
                  { label: 'Last 2 weeks', range: `${toYMD(new Date(Date.now()-14*86400000))}-${toYMD(new Date())}` },
                ].map(q => (
                  <button key={q.label} type="button"
                    onClick={() => setCustomRange(q.range)}
                    className="text-xs px-2 py-1 rounded-lg bg-[#1a2236] border border-[#2a3a52] text-slate-300 hover:border-brand-500 hover:text-white transition-colors">
                    {q.label}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <div className="flex items-center gap-2 text-sm">
              <span className="bg-[#0a0f1c] border border-[#2a3a52] rounded-lg px-3 py-1.5 text-slate-300 font-mono text-xs">
                {fromYMD(rangeStart)} → {fromYMD(rangeEnd)}
              </span>
              <span className="text-slate-500 text-xs">({activeRange})</span>
            </div>
          )}

          {(selectedPreset === '5Y' || selectedPreset === '10Y') && (
            <div className="mt-3 flex items-start gap-2 p-3 rounded-lg bg-amber-500/10 border border-amber-500/30">
              <span className="text-amber-400 mt-0.5">⚠️</span>
              <p className="text-xs text-amber-300">
                <strong>{selectedPreset} of data</strong> needs to be downloaded from KuCoin on first run
                ({selectedPreset === '5Y' ? '~2 minutes' : '~5 minutes'} for 15m candles).
              </p>
            </div>
          )}
        </div>

        {/* ── Strategy / Pairs / Timeframe ───────────────────────────── */}
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-4 mb-6">
          <div className="col-span-2">
            <label className="label">Strategy</label>
            <select className="input" value={strategyId ?? ''}
              onChange={e => setStrategyId(Number(e.target.value))}>
              {strategies.map((s: any) => (
                <option key={s.id} value={s.id}>{s.name}</option>
              ))}
            </select>
          </div>

          {/* Pair search — same component as spot backtest */}
          <div className="col-span-2 md:col-span-3 lg:col-span-2 relative">
            <label className="label">
              Pairs
              {pairsLoading && <span className="text-[10px] text-slate-500 ml-2">loading…</span>}
            </label>
            <div className="input flex flex-wrap gap-1.5 min-h-[42px] items-center">
              {pairs.map(p => (
                <span key={p} className="flex items-center gap-1 px-2 py-0.5 bg-brand-600/20 text-brand-300 border border-brand-500/30 rounded text-xs">
                  {p}
                  <button type="button" onClick={() => removePair(p)}
                    className="text-brand-300/60 hover:text-white ml-0.5">×</button>
                </span>
              ))}
              <input
                className="flex-1 bg-transparent outline-none text-sm min-w-[120px]"
                value={pairQuery}
                onChange={e => { setPairQuery(e.target.value); setShowPairDrop(true); }}
                onFocus={() => setShowPairDrop(true)}
                onBlur={() => setTimeout(() => setShowPairDrop(false), 150)}
                onKeyDown={e => {
                  if (e.key === 'Enter' && pairQuery.trim()) {
                    e.preventDefault();
                    const typed = pairQuery.trim().toUpperCase();
                    const match = availablePairs.find(p => p.toUpperCase() === typed) ?? typed;
                    addPair(match);
                  }
                  if (e.key === 'Backspace' && !pairQuery && pairs.length)
                    removePair(pairs[pairs.length - 1]);
                }}
                placeholder={pairs.length === 0 ? 'Search coin (e.g. BTC, ETH)…' : ''}
              />
            </div>
            {showPairDrop && filteredPairs.length > 0 && (
              <div className="absolute z-20 mt-1 w-full max-h-64 overflow-y-auto bg-[#1a2236] border border-[#2a3a52] rounded-lg shadow-xl">
                {filteredPairs.map(p => (
                  <button type="button" key={p}
                    onMouseDown={e => { e.preventDefault(); addPair(p); }}
                    className="w-full text-left px-3 py-2 text-sm hover:bg-[#2a3a52]/60 border-b border-[#2a3a52]/40 last:border-0">
                    {p}
                  </button>
                ))}
              </div>
            )}
          </div>

          <div>
            <label className="label flex items-center">Timeframe <SourceBadge src={tfSrc} /></label>
            <select className="input" value={timeframe}
              onChange={e => { setTimeframe(e.target.value); setTfSrc('manual'); }}>
              {['1m','5m','15m','30m','1h','4h'].map(tf => (
                <option key={tf} value={tf}>{tf}</option>
              ))}
            </select>
          </div>
        </div>

        {/* ── Futures-specific config ─────────────────────────────────── */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
          <div>
            <label
              className="label"
              title="Virtual paper-money starting balance for the simulation. No real funds are used."
            >
              Starting Balance (virtual USDT)
            </label>
            <input type="number" className="input" value={startBalance}
              onChange={e => setStartBalance(Number(e.target.value))} />
          </div>
          <div>
            <label className="label flex items-center flex-wrap">
              <span>Leverage: {leverage}x</span>
              <SourceBadge src={levSrc} />
              <span className="text-orange-400 ml-2 text-[10px]">Liq ~{(100/leverage).toFixed(1)}%</span>
            </label>
            <input type="range" min={1} max={50} value={leverage}
              onChange={e => { setLeverage(Number(e.target.value)); setLevSrc('manual'); }}
              className="w-full accent-blue-500 mt-2" />
          </div>
          <div>
            <label className="label flex items-center">
              <span>Stop-Loss: {stoploss}%</span>
              <SourceBadge src={slSrc} />
            </label>
            <input type="range" min={0.5} max={10} step={0.5} value={stoploss}
              onChange={e => { setStoploss(Number(e.target.value)); setSlSrc('manual'); }}
              className="w-full accent-red-500 mt-2" />
          </div>
          <div>
            <label className="label flex items-center flex-wrap">
              <span>Take-Profit: {takeProfit}%</span>
              <SourceBadge src={tpSrc} />
              <span className="text-emerald-400 ml-1 text-[10px]">→ {(takeProfit*leverage).toFixed(1)}% leveraged</span>
            </label>
            <input type="range" min={0.1} max={10} step={0.1} value={takeProfit}
              onChange={e => { setTakeProfit(Number(e.target.value)); setTpSrc('manual'); }}
              className="w-full accent-emerald-500 mt-2" />
          </div>
        </div>

        {/* Run button */}
        <div className="flex items-center gap-4 flex-wrap">
          <button onClick={runBacktest}
            disabled={running || !strategyId || (selectedPreset === 'Custom' && (!customRange || customRange.length < 17))}
            className="btn-primary px-8 py-3 text-base">
            {running
              ? `Running ${currentPreset && currentPreset.days > 365 ? '(downloading data…)' : ''}…`
              : `▶ Run ${selectedPreset} Futures Backtest`}
          </button>
        </div>

        {/* Summary row */}
        {!running && strategyId && (
          <div className="mt-4 pt-4 border-t border-[#2a3a52] text-xs text-slate-500 flex flex-wrap gap-x-4 gap-y-1">
            <span>📅 Period: <span className="text-slate-300">{selectedPreset === 'Custom' ? customRange : selectedPreset}</span></span>
            <span>📊 Pairs: <span className="text-slate-300">{pairs.join(', ')}</span></span>
            <span>⏱ Timeframe: <span className="text-slate-300">{timeframe}</span></span>
            <span>💰 Balance: <span className="text-slate-300">${startBalance}</span></span>
            <span>⚡ Leverage: <span className="text-slate-300">{leverage}x</span></span>
            <span>🛑 Stop-loss: <span className="text-slate-300">{stoploss}%</span></span>
            <span>🎯 Take-profit: <span className="text-slate-300">{takeProfit}%</span></span>
          </div>
        )}
      </div>

      {/* Loading */}
      {running && (
        <LoadingSpinner text={
          currentPreset && currentPreset.days > 365
            ? `Downloading ${selectedPreset} of historical data from KuCoin, then simulating ${leverage}x leveraged futures trades…`
            : `Simulating ${leverage}x leveraged futures trades on historical data…`
        } />
      )}

      {/* Error */}
      {error && (
        <div className="card mb-8 border-red-500/30 bg-red-500/10">
          <p className="text-red-400">{error}</p>
        </div>
      )}

      {/* Results */}
      {m && (
        <>
          {/* Results header */}
          <div className="flex items-center justify-between mb-4 flex-wrap gap-2">
            <h2 className="text-xl font-semibold">
              Results — {selectedPreset} Futures Backtest
              <span className="text-sm font-normal text-slate-400 ml-2">
                {fromYMD(rangeStart)} → {fromYMD(rangeEnd)}
              </span>
            </h2>
            <span className="text-xs text-slate-500 bg-[#1a2236] px-3 py-1 rounded-full border border-[#2a3a52]">
              {pairs.join(', ')} · {timeframe} · {leverage}x · ${startBalance}
            </span>
          </div>

          {/* Simulation disclaimer — make it impossible to misread the
              backtest as touching real funds. The "$1000" is virtual
              starting capital; the "Funding: N · real KuCoin" further down
              is a COUNT of historical funding-rate data records (not money). */}
          <div className="mb-4 px-3 py-2 rounded-lg bg-emerald-500/5 border border-emerald-500/20 text-[11px] text-emerald-300/90 flex items-center gap-2">
            <span className="text-base">🧪</span>
            <span>
              <b className="text-emerald-200">Simulation only.</b> Starting balance{' '}
              <b className="text-emerald-200">${startBalance}</b> is virtual paper money.
              No real funds, no KuCoin account access — this replays your strategy
              against historical price + funding-rate data and computes a simulated P&amp;L.
            </span>
          </div>

          {/* Data quality + signal-source banner */}
          {result?.data_quality && Object.keys(result.data_quality).length > 0 && (
            <div className="card mb-4 border-[#243153] bg-[#0d1424]">
              <p className="text-xs uppercase tracking-wider text-slate-500 mb-2">Data quality &amp; signal source</p>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                {Object.entries(result.data_quality as Record<string, any>).map(([pair, d]) => {
                  const cov = Number(d.coverage_pct) || 0;
                  const covColor = cov >= 95 ? 'text-emerald-400' : cov >= 80 ? 'text-amber-400' : 'text-red-400';
                  const isUserStrat = String(d.signal_source || '').startsWith('user_strategy');
                  const isCodeFail  = String(d.signal_source || '').includes('user code failed');
                  const isNameMatch = Boolean(d.fallback_intended) || String(d.signal_source || '').includes('name-match');
                  return (
                    <div key={pair} className="text-xs text-slate-300 bg-[#0a0f1d] border border-[#1a2236] rounded px-2.5 py-2 space-y-1">
                      <div className="flex items-center justify-between gap-2">
                        <span className="font-semibold text-white">{pair}</span>
                        <span className={covColor}>
                          {d.candles_loaded} / {d.candles_expected} candles ({cov.toFixed(1)}%)
                        </span>
                      </div>
                      <div className="flex items-center justify-between gap-2 text-[10px]">
                        <span
                          className="text-slate-500"
                          title="Number of historical funding-rate records fetched from KuCoin's public funding-rates API. The simulation applies the real historical rate every 8h to your virtual position — no real money involved."
                        >
                          Funding records: {d.funding_records}{' '}
                          {d.funding_source === 'kucoin_history' ? '· historical data' : '· using 0.03% fallback'}
                        </span>
                        <span
                          className={
                            isCodeFail ? 'text-red-400 font-medium'
                            : isUserStrat ? 'text-emerald-300 font-medium'
                            : isNameMatch ? 'text-sky-300 font-medium'
                            : 'text-amber-300'
                          }
                          title={
                            isUserStrat ? 'Your strategy code was executed'
                            : isNameMatch ? "Your strategy is a Pine Script port that runs via the matching built-in signal function (this is expected, not an error)"
                            : 'Built-in pattern was used'
                          }
                        >
                          Signal: {
                            isUserStrat ? '✓ your strategy code'
                            : isCodeFail ? '⚠ user code failed → fallback'
                            : isNameMatch ? `↻ name-matched built-in (${strategies.find((s: any) => s.id === strategyId)?.name || 'strategy'})`
                            : d.signal_source
                          }
                        </span>
                      </div>
                      {(d.entry_signals_long !== undefined || d.entry_signals_short !== undefined) && (
                        <div className="text-[10px] text-slate-500 space-y-0.5">
                          <div>
                            Signal bars: <b className="text-emerald-400">{d.entry_signals_long ?? 0} long</b>
                            {' · '}<b className="text-red-400">{d.entry_signals_short ?? 0} short</b>
                            <span className="text-slate-600"> (every bar where condition is true)</span>
                          </div>
                          {(d.entry_clusters_long !== undefined || d.entry_clusters_short !== undefined) && (
                            <div>
                              Trade signals (edges): <b className="text-emerald-300">{d.entry_clusters_long ?? 0} long</b>
                              {' · '}<b className="text-red-300">{d.entry_clusters_short ?? 0} short</b>
                              <span className="text-slate-600"> (0→1 transitions — matches TV)</span>
                            </div>
                          )}
                          {(d.trades_opened_long !== undefined || d.trades_opened_short !== undefined) && (
                            <div>
                              Trades opened: <b className="text-emerald-200">{d.trades_opened_long ?? 0} long</b>
                              {' · '}<b className="text-red-200">{d.trades_opened_short ?? 0} short</b>
                              {(d.signals_skipped_in_trade || d.signals_skipped_cooldown) ? (
                                <span className="text-slate-600">
                                  {' '}· skipped: {d.signals_skipped_in_trade || 0} in-trade
                                  {d.signals_skipped_cooldown ? `, ${d.signals_skipped_cooldown} cooldown` : ''}
                                </span>
                              ) : null}
                            </div>
                          )}
                        </div>
                      )}
                      {/* When the strategy fired 0 signals, show which class
                          + methods we found so the user can see whether
                          their populate_entry_trend is actually defined and
                          which entry/exit hooks the runner called. */}
                      {isUserStrat && (d.entry_signals_long ?? 0) === 0 && (d.entry_signals_short ?? 0) === 0 && d.strategy_class && (
                        <div className="text-[10px] text-amber-300 mt-1 leading-snug border-l-2 border-amber-500/40 pl-2 space-y-0.5">
                          <div>Strategy fired no signals on this data.</div>
                          <div className="text-slate-400">
                            Class: <code className="text-amber-200">{d.strategy_class}</code>
                          </div>
                          {Array.isArray(d.strategy_methods) && d.strategy_methods.length > 0 && (
                            <div className="text-slate-400">
                              Methods found: <code className="text-amber-200/80 text-[9px] break-all">
                                {d.strategy_methods.join(', ')}
                              </code>
                            </div>
                          )}
                          {Array.isArray(d.signal_columns) && d.signal_columns.length > 0 && (
                            <div className="text-slate-400">
                              Non-zero columns in dataframe: <code className="text-amber-200/80 text-[9px] break-all">
                                {d.signal_columns.join(', ')}
                              </code>
                            </div>
                          )}
                          {d.code_preview && (
                            <details className="mt-1.5 group">
                              <summary className="cursor-pointer text-slate-400 hover:text-amber-300 text-[10px]">
                                Show first 800 chars of strategy code ▾
                              </summary>
                              <pre className="mt-1 p-2 bg-black/30 border border-amber-500/20 rounded text-[10px] text-amber-100/90 whitespace-pre-wrap break-all font-mono overflow-auto max-h-72">
                                {d.code_preview}
                              </pre>
                            </details>
                          )}
                        </div>
                      )}
                      {d.user_code_error && !isNameMatch && (
                        <div className="text-[10px] text-red-400 mt-1 leading-snug border-l-2 border-red-500/40 pl-2">
                          User code error: <code>{d.user_code_error}</code>
                        </div>
                      )}
                      {isNameMatch && (
                        <div className="text-[10px] text-sky-300/80 mt-1 leading-snug border-l-2 border-sky-500/40 pl-2">
                          ℹ This strategy is a Python-class port (e.g. Pine Script translation),
                          so it runs via the matching built-in signal function instead of being
                          exec'd as a Freqtrade IStrategy. This is the intended path — not an error.
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
              <p className="text-[10px] text-slate-500 mt-2">
                Backtest replays <b>historical</b> KuCoin futures klines (api-futures.kucoin.com /api/v1/kline/query)
                and historical funding rates (/api/v1/contract/funding-rates) against a <b>simulated</b> portfolio.
                Nothing here touches your live KuCoin account or real funds. Custom strategies execute
                your authored IStrategy code; built-in names use the corresponding hardcoded signal function.
              </p>
            </div>
          )}

          {/* Production-grade cost-transparency card.
              Funding + slippage are deducted from balance (real-cost
              modelling); the KuCoin fee line is informational — it shows
              what the exchange would have charged if these trades were
              live. The app itself doesn't charge anything. */}
          {(m.total_funding_paid !== undefined ||
            m.total_slippage_paid !== undefined ||
            m.total_hyp_kucoin_fees !== undefined) && (
            <div className="card mb-4 border-[#243153] bg-[#0d1424]">
              <p className="text-xs uppercase tracking-wider text-slate-500 mb-2">
                Real-trading costs (transparency)
              </p>
              <div className="grid grid-cols-1 md:grid-cols-3 gap-3 text-xs">
                <div className="bg-[#0a0f1d] border border-[#1a2236] rounded px-3 py-2">
                  <div className="text-slate-500 text-[10px] uppercase tracking-wider">Funding paid</div>
                  <div className="text-amber-300 font-semibold mt-0.5">
                    ${(m.total_funding_paid ?? 0).toFixed(2)}
                  </div>
                  <div className="text-[10px] text-slate-500 mt-0.5">
                    Charged at real KuCoin settlement times (00/08/16 UTC) using historical rates.
                    Deducted from simulated P&amp;L.
                  </div>
                </div>
                <div className="bg-[#0a0f1d] border border-[#1a2236] rounded px-3 py-2">
                  <div className="text-slate-500 text-[10px] uppercase tracking-wider">Slippage paid</div>
                  <div className="text-amber-300 font-semibold mt-0.5">
                    ${(m.total_slippage_paid ?? 0).toFixed(2)}
                  </div>
                  <div className="text-[10px] text-slate-500 mt-0.5">
                    Adverse fill on stops (5bps), TPs (2bps), liquidations (15bps), market exits (5bps).
                    Deducted from simulated P&amp;L.
                  </div>
                </div>
                <div className="bg-[#0a0f1d] border border-sky-500/20 rounded px-3 py-2">
                  <div className="text-sky-400/80 text-[10px] uppercase tracking-wider">KuCoin would charge (info)</div>
                  <div className="text-sky-300 font-semibold mt-0.5">
                    ${(m.total_hyp_kucoin_fees ?? 0).toFixed(2)}
                  </div>
                  <div className="text-[10px] text-slate-500 mt-0.5">
                    Hypothetical fees at KuCoin's rates ({(m.kucoin_taker_fee_pct ?? 0.06).toFixed(2)}% taker /
                    {' '}{(m.kucoin_maker_fee_pct ?? 0.02).toFixed(2)}% maker).
                    <b className="text-sky-300/90"> Not deducted</b> from your simulated balance — this app
                    is not a broker.
                  </div>
                </div>
              </div>
            </div>
          )}

          {/* Metrics row 1 — same as spot */}
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4 mb-4">
            <MetricCard
              title="Total Profit"
              value={`${m.total_profit_pct >= 0 ? '+' : ''}${m.total_profit_pct.toFixed(2)}%`}
              color={m.total_profit_pct >= 0 ? 'profit' : 'loss'}
            />
            <MetricCard title="Win Rate"     value={`${(m.win_rate * 100).toFixed(1)}%`} />
            <MetricCard title="Max Drawdown" value={`${m.max_drawdown.toFixed(2)}%`} color="loss" />
            <MetricCard title="Final Balance" value={`$${m.final_balance.toFixed(2)}`}
              color={m.final_balance >= startBalance ? 'profit' : 'loss'} />
            <MetricCard title="Total Trades" value={m.total_trades} />
            <MetricCard title="Avg P&L/Trade" value={`${m.avg_leverage_pnl >= 0 ? '+' : ''}${m.avg_leverage_pnl.toFixed(2)}%`}
              color={m.avg_leverage_pnl >= 0 ? 'profit' : 'loss'} />
          </div>

          {/* Futures-specific metrics row */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-8">
            <div className={`card ${m.liquidations > 0 ? 'border-red-500/30 bg-red-500/5' : ''}`}>
              <p className="text-xs text-slate-400 uppercase tracking-wider mb-1">⚡ Liquidations</p>
              <p className={`text-2xl font-bold ${m.liquidations > 0 ? 'text-red-400' : 'text-white'}`}>{m.liquidations}</p>
              {m.liquidations > 0 && <p className="text-xs text-red-400/70 mt-0.5">Full margin losses</p>}
            </div>
            <div className="card">
              <p className="text-xs text-slate-400 uppercase tracking-wider mb-1">📈 Long Trades</p>
              <p className="text-2xl font-bold text-emerald-400">{m.long_trades}</p>
            </div>
            <div className="card">
              <p className="text-xs text-slate-400 uppercase tracking-wider mb-1">📉 Short Trades</p>
              <p className="text-2xl font-bold text-red-400">{m.short_trades}</p>
            </div>
            <div className="card">
              <p className="text-xs text-slate-400 uppercase tracking-wider mb-1">W / L</p>
              <p className="text-2xl font-bold">
                <span className="text-emerald-400">{m.winning_trades}</span>
                <span className="text-slate-500 mx-1">/</span>
                <span className="text-red-400">{m.losing_trades}</span>
              </p>
            </div>
          </div>

          {/* Equity Curve — identical to spot backtest */}
          <div className="card mb-8">
            <h2 className="text-lg font-semibold mb-4">Equity Curve</h2>
            <ResponsiveContainer width="100%" height={300}>
              <LineChart data={equityCurve}>
                <CartesianGrid strokeDasharray="3 3" stroke="#2a3a52" />
                <XAxis dataKey="trade" stroke="#64748b" fontSize={12}
                  label={{ value: 'Trade #', position: 'insideBottom', offset: -2, fill: '#64748b', fontSize: 11 }} />
                <YAxis stroke="#64748b" fontSize={12} tickFormatter={v => `$${v.toLocaleString()}`} />
                <Tooltip
                  contentStyle={{ background: '#1a2236', border: '1px solid #2a3a52', borderRadius: 8, color: '#f1f5f9' }}
                  formatter={(v: number) => [`$${v.toFixed(2)}`, 'Portfolio']}
                />
                <Line type="monotone" dataKey="equity" stroke="#3391ff" strokeWidth={2} dot={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>

          {/* Profit Distribution — same as spot */}
          {trades.length > 0 && (
            <div className="card mb-8">
              <h2 className="text-lg font-semibold mb-4">Profit Distribution per Trade (Leveraged)</h2>
              <ResponsiveContainer width="100%" height={200}>
                <BarChart data={trades.map((t: any, i: number) => ({ trade: i + 1, profit: t.profit_pct ?? 0 }))}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#2a3a52" />
                  <XAxis dataKey="trade" stroke="#64748b" fontSize={12} />
                  <YAxis stroke="#64748b" fontSize={12} tickFormatter={v => `${v}%`} />
                  <Tooltip
                    contentStyle={{ background: '#1a2236', border: '1px solid #2a3a52', borderRadius: 8, color: '#f1f5f9' }}
                    formatter={(v: number) => [`${v.toFixed(2)}%`, 'Profit']}
                  />
                  <Bar dataKey="profit">
                    {trades.map((t: any, i: number) => (
                      <Cell key={i} fill={
                        t.exit_reason === 'liquidated' ? '#f97316'
                          : (t.profit_pct ?? 0) >= 0 ? '#22c55e' : '#ef4444'
                      } />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
              <div className="flex gap-4 mt-2 text-xs text-slate-400">
                <span className="flex items-center gap-1"><span className="w-3 h-3 rounded-sm bg-emerald-500 inline-block"/>Profit</span>
                <span className="flex items-center gap-1"><span className="w-3 h-3 rounded-sm bg-red-500 inline-block"/>Stop-Loss</span>
                <span className="flex items-center gap-1"><span className="w-3 h-3 rounded-sm bg-orange-500 inline-block"/>Liquidated</span>
              </div>
            </div>
          )}

          {/* Trade Table — same structure as spot + futures columns */}
          <div className="card mb-8">
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
                    <th className="text-right py-3 px-2">Dir</th>
                    <th className="text-right py-3 px-2">Lev</th>
                    <th className="text-right py-3 px-2" title="Margin used as collateral for this trade (= your money at risk)">Margin $</th>
                    <th className="text-right py-3 px-2" title="Notional position size = margin × leverage (= what KuCoin trades on your behalf)">Position $</th>
                    <th className="text-right py-3 px-2">Entry</th>
                    <th className="text-right py-3 px-2">Exit</th>
                    <th className="text-right py-3 px-2">Liq.</th>
                    <th className="text-right py-3 px-2">Profit %</th>
                    <th className="text-right py-3 px-2">P&amp;L USDT</th>
                    <th className="text-right py-3 px-2">Balance</th>
                    <th className="text-left py-3 px-2">Open Date</th>
                    <th className="text-left py-3 px-2">Exit Reason</th>
                  </tr>
                </thead>
                <tbody>
                  {trades.map((t: any, i: number) => (
                    <tr key={i} className={`border-b border-[#2a3a52]/50 hover:bg-[#2a3a52]/20 ${
                      t.exit_reason === 'liquidated' ? 'bg-orange-500/5' : ''
                    }`}>
                      <td className="py-2 px-2 text-slate-500">{i + 1}</td>
                      <td className="py-2 px-2 font-medium">{t.pair}</td>
                      <td className={`py-2 px-2 text-right font-semibold text-xs ${
                        t.direction === 'long' ? 'text-emerald-400' : 'text-red-400'
                      }`}>{t.direction?.toUpperCase()}</td>
                      <td className="py-2 px-2 text-right text-blue-400 text-xs font-bold">{t.leverage}x</td>
                      <td className="py-2 px-2 text-right font-mono text-xs text-amber-300"
                          title="Your margin (real $ at risk on this trade)">
                        ${Number(t.margin ?? 0).toFixed(2)}
                      </td>
                      <td className="py-2 px-2 text-right font-mono text-xs text-slate-400"
                          title={`Notional = margin × leverage = $${Number(t.margin ?? 0).toFixed(2)} × ${t.leverage}x`}>
                        ${(Number(t.margin ?? 0) * Number(t.leverage ?? 1)).toFixed(2)}
                      </td>
                      <td className="py-2 px-2 text-right font-mono text-xs">{Number(t.open_rate).toFixed(2)}</td>
                      <td className="py-2 px-2 text-right font-mono text-xs">{Number(t.close_rate).toFixed(2)}</td>
                      <td className="py-2 px-2 text-right font-mono text-xs text-orange-400">{Number(t.liq_price).toFixed(2)}</td>
                      <td className={`py-2 px-2 text-right font-semibold ${(t.profit_pct ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                        {(t.profit_pct ?? 0) >= 0 ? '+' : ''}{(t.profit_pct ?? 0).toFixed(2)}%
                      </td>
                      <td className={`py-2 px-2 text-right ${(t.profit_abs ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                        {(t.profit_abs ?? 0) >= 0 ? '+' : ''}{(t.profit_abs ?? 0).toFixed(2)}
                      </td>
                      <td className="py-2 px-2 text-right font-mono text-xs">{t.balance?.toFixed(2)}</td>
                      <td className="py-2 px-2 text-slate-400 text-xs">{String(t.open_date ?? '').slice(0, 10)}</td>
                      <td className={`py-2 px-2 text-xs ${t.exit_reason === 'liquidated' ? 'text-orange-400 font-bold' : 'text-slate-500'}`}>
                        {t.exit_reason === 'liquidated' ? '⚡ LIQUIDATED' : t.exit_reason}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}

      {/* Past runs history (shown even before first run) */}
      {history.length > 0 && (
        <div className="card">
          <h2 className="text-lg font-semibold mb-4">🕐 Previous Futures Backtest Runs</h2>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-slate-400 border-b border-[#2a3a52]">
                  <th className="text-left py-2 px-2">Strategy</th>
                  <th className="text-right py-2 px-2">Period</th>
                  <th className="text-right py-2 px-2">Leverage</th>
                  <th className="text-right py-2 px-2">P&L%</th>
                  <th className="text-right py-2 px-2">Win Rate</th>
                  <th className="text-right py-2 px-2">Trades</th>
                  <th className="text-right py-2 px-2">⚡ Liq.</th>
                  <th className="text-right py-2 px-2">Max DD</th>
                  <th className="text-left py-2 px-2">Date</th>
                </tr>
              </thead>
              <tbody>
                {history.map((h: any) => (
                  <tr key={h.id} className="border-b border-[#2a3a52]/50 hover:bg-[#2a3a52]/20">
                    <td className="py-2 px-2 font-medium text-xs">{h.strategy_name} — {h.pairs}</td>
                    <td className="py-2 px-2 text-right text-xs text-slate-400">{h.timerange}</td>
                    <td className="py-2 px-2 text-right text-blue-400 font-bold text-xs">{h.leverage}x</td>
                    <td className={`py-2 px-2 text-right font-semibold ${(h.total_profit_pct ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                      {(h.total_profit_pct ?? 0) >= 0 ? '+' : ''}{(h.total_profit_pct ?? 0).toFixed(2)}%
                    </td>
                    <td className="py-2 px-2 text-right text-xs">{((h.win_rate ?? 0) * 100).toFixed(1)}%</td>
                    <td className="py-2 px-2 text-right text-xs">{h.total_trades}</td>
                    <td className={`py-2 px-2 text-right text-xs font-bold ${(h.liquidations ?? 0) > 0 ? 'text-orange-400' : 'text-slate-500'}`}>
                      {h.liquidations ?? 0}
                    </td>
                    <td className="py-2 px-2 text-right text-xs text-amber-400">-{(h.max_drawdown ?? 0).toFixed(1)}%</td>
                    <td className="py-2 px-2 text-xs text-slate-400">{String(h.created_at).slice(0, 10)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

export default function FuturesBacktestPage() {
  return <Suspense><FuturesBacktestInner /></Suspense>;
}
