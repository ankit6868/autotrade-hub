'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { api } from '@/lib/api';
import LoadingSpinner from '@/components/ui/LoadingSpinner';

function timeAgo(date: Date | null): string {
  if (!date) return '';
  const secs = Math.floor((Date.now() - date.getTime()) / 1000);
  if (secs < 10) return 'just now';
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  return `${Math.floor(mins / 60)}h ago`;
}

type Opportunity = {
  pair: string;
  strategy: string;
  strategy_label: string;
  timeframe: string;
  entry_quality: number;
  fit_score: number;
  overall_score: number;
  confidence: number;
  expected_profit_pct: number | null;
  expected_profit_source: string;
  recommendation: 'STRONG_BUY' | 'BUY' | 'HOLD' | 'AVOID';
  indicators: Record<string, unknown>;
  reasoning: string[];
};

type Universe = {
  default_pairs: string[];
  strategies: { name: string; label: string; one_liner: string; ideal_timeframes: string[] }[];
};

const TIMEFRAMES = ['5m', '15m', '30m', '1h', '4h', '1d'];

const recoStyles: Record<string, string> = {
  STRONG_BUY: 'bg-emerald-500/20 text-emerald-300 border-emerald-500/40',
  BUY: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20',
  HOLD: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
  AVOID: 'bg-red-500/10 text-red-400 border-red-500/30',
};

const recoEmoji: Record<string, string> = {
  STRONG_BUY: '🟢', BUY: '🟩', HOLD: '⬜', AVOID: '🔴',
};

export default function OpportunitiesPage() {
  const router = useRouter();
  const [universe, setUniverse] = useState<Universe | null>(null);
  const [timeframe, setTimeframe] = useState('15m');
  const [topN, setTopN] = useState(12);
  const [minScore, setMinScore] = useState(0);
  const [selectedPairs, setSelectedPairs] = useState<string[]>([]);
  const [selectedStrategies, setSelectedStrategies] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [results, setResults] = useState<Opportunity[]>([]);
  const [scanned, setScanned] = useState<number>(0);
  const [failed, setFailed] = useState<string[]>([]);
  const [stalePairs, setStalePairs] = useState<string[]>([]);
  const [cooldown, setCooldown] = useState<number>(0);
  const [error, setError] = useState('');
  const [expanded, setExpanded] = useState<string | null>(null);
  const [scannedAt, setScannedAt] = useState<Date | null>(null);
  const [, setTick] = useState(0);
  const [copiedKey, setCopiedKey] = useState<string | null>(null);

  useEffect(() => {
    api.analysis.universe().then((u) => {
      setUniverse(u);
      setSelectedPairs(u.default_pairs);
      setSelectedStrategies(u.strategies.map((s: { name: string }) => s.name));
    }).catch((e) => setError(String(e)));
  }, []);

  // Tick every 15s so "X min ago" stays fresh
  useEffect(() => {
    const t = setInterval(() => setTick((n) => n + 1), 15000);
    return () => clearInterval(t);
  }, []);

  async function runScan() {
    setLoading(true);
    setError('');
    setResults([]);
    try {
      const params: Record<string, string> = {
        timeframe,
        top_n: String(topN),
        min_score: String(minScore),
      };
      if (selectedPairs.length && universe && selectedPairs.length !== universe.default_pairs.length) {
        params.pairs = selectedPairs.join(',');
      }
      if (selectedStrategies.length && universe && selectedStrategies.length !== universe.strategies.length) {
        params.strategies = selectedStrategies.join(',');
      }
      const data = await api.analysis.opportunities(params);
      setResults(data.opportunities as Opportunity[]);
      setScanned(data.scanned_pairs);
      setFailed(data.failed_pairs || []);
      setStalePairs(data.stale_pairs || []);
      setCooldown(data.tv_status?.cooldown_remaining_s || 0);
      setScannedAt(new Date());
    } catch (e) {
      setError(String(e));
    }
    setLoading(false);
  }

  // tick the cooldown countdown every second
  useEffect(() => {
    if (cooldown <= 0) return;
    const t = setInterval(() => {
      setCooldown((c) => (c > 1 ? c - 1 : 0));
    }, 1000);
    return () => clearInterval(t);
  }, [cooldown]);

  // auto-scan once the universe loads
  useEffect(() => {
    if (universe && selectedPairs.length && selectedStrategies.length && !loading && results.length === 0) {
      runScan();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [universe]);

  function togglePair(p: string) {
    setSelectedPairs((prev) => (prev.includes(p) ? prev.filter((x) => x !== p) : [...prev, p]));
  }
  function toggleStrategy(s: string) {
    setSelectedStrategies((prev) => (prev.includes(s) ? prev.filter((x) => x !== s) : [...prev, s]));
  }

  function buildOpportunityParams(o: Opportunity): URLSearchParams {
    const ind = o.indicators as Record<string, unknown>;
    const q = new URLSearchParams({
      pair: o.pair,
      strategy: o.strategy,
      timeframe: o.timeframe,
      score: o.overall_score.toFixed(0),
      action: o.recommendation.toLowerCase(),
      entry_quality: o.entry_quality.toFixed(0),
      confidence: o.confidence.toFixed(3),
    });
    if (ind.rsi != null) q.set('rsi', String(Number(ind.rsi).toFixed(2)));
    if (ind.adx != null) q.set('adx', String(Number(ind.adx).toFixed(2)));
    if (ind.macd != null) q.set('macd', String(Number(ind.macd).toFixed(6)));
    if (ind.bb_position != null) q.set('bb_pos', String(Number(ind.bb_position).toFixed(4)));
    if (ind.volume_change_pct != null) q.set('vol_change', String(Number(ind.volume_change_pct).toFixed(4)));
    if (o.reasoning.length > 0) q.set('reasoning', o.reasoning.slice(0, 4).join('|'));
    return q;
  }

  function goTrade(mode: 'paper' | 'live', o: Opportunity) {
    const base = mode === 'paper' ? '/paper-trade' : '/live';
    router.push(`${base}?${buildOpportunityParams(o).toString()}`);
  }

  function buildOpportunitySignalText(o: Opportunity): string {
    const now = new Date().toUTCString().replace(' GMT', ' UTC');
    const emoji = recoEmoji[o.recommendation] || '⬜';
    const ind = o.indicators as Record<string, unknown>;
    const lines = [
      `${emoji} ${o.recommendation.replace('_', ' ')} — ${o.pair} (${o.timeframe})`,
      `📊 Strategy: ${o.strategy_label}`,
      `🏆 Score: ${o.overall_score.toFixed(0)}/100  |  Entry: ${o.entry_quality.toFixed(0)}  |  Fit: ${o.fit_score.toFixed(0)}  |  Confidence: ${(o.confidence * 100).toFixed(0)}%`,
    ];
    if (o.expected_profit_pct !== null) {
      lines.push(`💰 Expected profit: ${o.expected_profit_pct >= 0 ? '+' : ''}${o.expected_profit_pct.toFixed(2)}% (${o.expected_profit_source})`);
    }
    const indParts: string[] = [];
    if (ind.rsi != null) indParts.push(`RSI: ${Number(ind.rsi).toFixed(1)}`);
    if (ind.macd != null) indParts.push(`MACD: ${Number(ind.macd).toFixed(4)}`);
    if (ind.adx != null) indParts.push(`ADX: ${Number(ind.adx).toFixed(1)}`);
    if (indParts.length > 0) lines.push(`📈 ${indParts.join(' | ')}`);
    if (o.reasoning.length > 0) {
      lines.push(`💡 ${o.reasoning.slice(0, 2).join(' • ')}`);
    }
    lines.push(`🕒 ${now}`);
    lines.push(`📱 Sent via AutoTrade Hub`);
    return lines.join('\n');
  }

  async function copyOpportunity(o: Opportunity) {
    const key = `${o.pair}-${o.strategy}`;
    const text = buildOpportunitySignalText(o);
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const el = document.createElement('textarea');
      el.value = text;
      document.body.appendChild(el);
      el.select();
      document.execCommand('copy');
      document.body.removeChild(el);
    }
    setCopiedKey(key);
    setTimeout(() => setCopiedKey(null), 2500);
  }

  return (
    <div className="max-w-7xl mx-auto">
      <h1 className="heading-xl mb-1">🎯 Opportunities</h1>
      <p className="text-slate-400 mb-6 text-sm sm:text-base">
        Scan the market for the best (coin × strategy) matches right now. Scoring uses live
        TradingView indicators (RSI, MACD, ADX, Bollinger) and historical backtest data.
      </p>

      {/* Controls */}
      <div className="card mb-6">
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4">
          <div>
            <label className="label">Timeframe</label>
            <select className="input" value={timeframe} onChange={(e) => setTimeframe(e.target.value)}>
              {TIMEFRAMES.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
          </div>
          <div>
            <label className="label">Top N results</label>
            <input type="number" className="input" min={1} max={50} value={topN}
                   onChange={(e) => setTopN(Number(e.target.value))} />
          </div>
          <div>
            <label className="label">Minimum score: {minScore}</label>
            <input type="range" min={0} max={90} step={5} value={minScore}
                   onChange={(e) => setMinScore(Number(e.target.value))}
                   className="w-full accent-brand-500 mt-3" />
          </div>
          <div className="flex items-end">
            <button onClick={runScan} disabled={loading} className="btn-primary w-full">
              {loading ? 'Scanning...' : '🔍 Rescan'}
            </button>
          </div>
        </div>

        {universe && (
          <>
            <div className="mb-3">
              <div className="label mb-2">Pairs ({selectedPairs.length}/{universe.default_pairs.length})</div>
              <div className="flex flex-wrap gap-2">
                {universe.default_pairs.map((p) => (
                  <button key={p} onClick={() => togglePair(p)}
                    className={`px-3 py-1 rounded-full text-xs border ${
                      selectedPairs.includes(p)
                        ? 'bg-brand-600/20 border-brand-500 text-brand-300'
                        : 'bg-[#1a2236] border-[#2a3a52] text-slate-500 hover:text-slate-300'}`}>
                    {p}
                  </button>
                ))}
              </div>
            </div>
            <div>
              <div className="label mb-2">Strategies</div>
              <div className="flex flex-wrap gap-2">
                {universe.strategies.map((s) => (
                  <button key={s.name} onClick={() => toggleStrategy(s.name)}
                    title={s.one_liner}
                    className={`px-3 py-1.5 rounded-lg text-xs border ${
                      selectedStrategies.includes(s.name)
                        ? 'bg-brand-600/20 border-brand-500 text-brand-300'
                        : 'bg-[#1a2236] border-[#2a3a52] text-slate-500 hover:text-slate-300'}`}>
                    {s.label}
                  </button>
                ))}
              </div>
            </div>
          </>
        )}
      </div>

      {error && (
        <div className="card mb-6 border-red-500/30 bg-red-500/10">
          <p className="text-red-400">{error}</p>
        </div>
      )}

      {cooldown > 0 && (
        <div className="card mb-4 border-amber-500/30 bg-amber-500/10">
          <div className="flex items-center gap-3">
            <span className="text-2xl">⏳</span>
            <div className="flex-1">
              <p className="text-amber-300 font-semibold">
                TradingView rate limit cooldown — {Math.ceil(cooldown)}s remaining
              </p>
              <p className="text-amber-400/80 text-xs mt-0.5">
                Requests are being served from cache to avoid hammering the upstream API. Fresh data will resume automatically.
              </p>
            </div>
          </div>
        </div>
      )}

      {stalePairs.length > 0 && (
        <div className="card mb-4 border-sky-500/30 bg-sky-500/10">
          <div className="flex items-center gap-3">
            <span className="text-2xl">📦</span>
            <div className="flex-1">
              <p className="text-sky-300 font-semibold">
                {stalePairs.length} pair{stalePairs.length === 1 ? '' : 's'} served from stale cache
              </p>
              <p className="text-sky-400/80 text-xs mt-1">
                {stalePairs.slice(0, 8).join(', ')}
                {stalePairs.length > 8 ? `, +${stalePairs.length - 8} more` : ''}
              </p>
            </div>
          </div>
        </div>
      )}

      {!loading && failed.length > 0 && failed.length === scanned + failed.length && scanned === 0 && (
        <div className="card mb-4 border-red-500/30 bg-red-500/10">
          <p className="text-red-300 font-semibold">All pairs failed to fetch.</p>
          <p className="text-red-400/80 text-xs mt-1">
            TradingView may be rate-limiting your IP. The scanner will retry automatically after the cooldown;
            try again in a minute, or lower the pair count.
          </p>
        </div>
      )}

      {loading && <LoadingSpinner text="Scanning market across pairs and strategies..." />}

      {!loading && results.length > 0 && (
        <>
          <div className="flex items-center justify-between mb-4 flex-wrap gap-2">
            <span className="text-sm text-slate-400">
              <span className="text-white font-medium">{results.length}</span> opportunities ·{' '}
              <span className="text-white font-medium">{scanned}</span> pairs scanned
              {failed.length > 0 && <span className="text-red-400"> · {failed.length} failed</span>}
              {stalePairs.length > 0 && <span className="text-sky-400"> · {stalePairs.length} stale</span>}
            </span>
            {scannedAt && (
              <div className="flex items-center gap-2 text-xs">
                <span className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse inline-block" />
                <span className="text-slate-300 font-medium">Found {timeAgo(scannedAt)}</span>
                <span className="text-slate-500">
                  at {scannedAt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
                </span>
                <button onClick={runScan} className="ml-1 text-brand-400 hover:text-brand-300 underline">
                  Refresh
                </button>
              </div>
            )}
          </div>

          <div className="grid gap-3">
            {results.map((o, i) => {
              const key = `${o.pair}-${o.strategy}`;
              const isOpen = expanded === key;
              const profit = o.expected_profit_pct;
              return (
                <div key={key} className="card hover:border-brand-500/40 transition-colors">
                  {/* Main row */}
                  <div className="flex items-start gap-3">
                    {/* Clickable info section */}
                    <div className="flex items-start gap-3 flex-1 min-w-0 cursor-pointer"
                         onClick={() => setExpanded(isOpen ? null : key)}>
                      <div className="text-xl font-bold text-slate-500 w-8 text-center shrink-0 pt-1">#{i + 1}</div>
                      <div className="flex flex-col items-center w-16 shrink-0">
                        <div className={`text-2xl font-bold ${o.overall_score >= 70 ? 'text-emerald-400' : o.overall_score >= 55 ? 'text-amber-400' : 'text-red-400'}`}>
                          {o.overall_score.toFixed(0)}
                        </div>
                        <div className="text-[10px] uppercase text-slate-500 tracking-wide">Score</div>
                      </div>
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className="text-lg font-bold">{o.pair}</span>
                          <span className="text-slate-500">·</span>
                          <span className="text-slate-300 text-sm">{o.strategy_label}</span>
                          <span className={`text-[10px] uppercase px-2 py-0.5 rounded border ${recoStyles[o.recommendation]}`}>
                            {o.recommendation.replace('_', ' ')}
                          </span>
                          <span className="text-[10px] uppercase text-slate-500 bg-[#1a2236] px-2 py-0.5 rounded">{o.timeframe}</span>
                        </div>
                        <div className="flex gap-4 mt-1.5 text-xs text-slate-400 flex-wrap">
                          <span>Entry <span className="text-white font-medium">{o.entry_quality.toFixed(0)}</span></span>
                          <span>Fit <span className="text-white font-medium">{o.fit_score.toFixed(0)}</span></span>
                          <span>Confidence <span className="text-white font-medium">{(o.confidence * 100).toFixed(0)}%</span></span>
                          {profit === null ? (
                            <span className="text-slate-600">no history</span>
                          ) : (
                            <span className={`font-medium ${profit >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                              {profit >= 0 ? '+' : ''}{profit.toFixed(2)}%
                              <span className="text-slate-500 font-normal"> ({o.expected_profit_source})</span>
                            </span>
                          )}
                          {scannedAt && (
                            <span className="text-slate-600">
                              🕒 {scannedAt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                            </span>
                          )}
                        </div>
                      </div>
                    </div>

                    {/* Quick-action buttons — always visible */}
                    <div className="flex flex-col gap-1.5 shrink-0">
                      <button
                        onClick={() => goTrade('paper', o)}
                        className="px-3 py-1.5 rounded-lg text-xs font-medium bg-brand-600/20 border border-brand-500/40 text-brand-300 hover:bg-brand-600/40 transition-colors whitespace-nowrap"
                      >
                        📄 Paper Trade
                      </button>
                      <button
                        onClick={() => goTrade('live', o)}
                        className="px-3 py-1.5 rounded-lg text-xs font-medium bg-red-500/10 border border-red-500/30 text-red-400 hover:bg-red-500/20 transition-colors whitespace-nowrap"
                      >
                        🔴 Live Trade
                      </button>
                      <button
                        onClick={() => copyOpportunity(o)}
                        className={`px-3 py-1.5 rounded-lg text-xs font-medium border transition-colors whitespace-nowrap ${
                          copiedKey === `${o.pair}-${o.strategy}`
                            ? 'bg-emerald-500/20 border-emerald-500/40 text-emerald-300'
                            : 'bg-[#1a2236] border-[#2a3a52] text-slate-400 hover:text-white hover:border-brand-500'
                        }`}
                        title="Copy signal to clipboard"
                      >
                        {copiedKey === `${o.pair}-${o.strategy}` ? '✅ Copied!' : '📋 Copy Signal'}
                      </button>
                    </div>
                    <div className="text-slate-500 cursor-pointer self-center ml-1"
                         onClick={() => setExpanded(isOpen ? null : key)}>
                      {isOpen ? '▼' : '▶'}
                    </div>
                  </div>

                  {/* Expanded detail */}
                  {isOpen && (
                    <div className="mt-4 pt-4 border-t border-[#2a3a52] grid md:grid-cols-2 gap-4">
                      <div>
                        <h4 className="text-xs font-semibold uppercase text-slate-400 mb-2">Why this setup</h4>
                        <ul className="space-y-1.5 text-sm">
                          {o.reasoning.map((r, j) => (
                            <li key={j} className="flex gap-2">
                              <span className="text-brand-400 mt-0.5 shrink-0">•</span>
                              <span className="text-slate-300">{r}</span>
                            </li>
                          ))}
                        </ul>
                      </div>
                      <div>
                        <h4 className="text-xs font-semibold uppercase text-slate-400 mb-2">Live indicators</h4>
                        <div className="grid grid-cols-2 gap-2 text-xs mb-4">
                          {Object.entries(o.indicators).map(([k, v]) => (
                            <div key={k} className="flex justify-between bg-[#1a2236] rounded px-3 py-1.5">
                              <span className="text-slate-500">{k}</span>
                              <span className="text-white font-mono">{v === null || v === undefined ? '—' : String(v)}</span>
                            </div>
                          ))}
                        </div>
                        <div className="flex gap-2">
                          <a href={`/backtest?${buildOpportunityParams(o).toString()}`}
                             className="btn-secondary text-xs flex-1 text-center">
                            📊 Backtest
                          </a>
                          <button onClick={() => goTrade('paper', o)} className="btn-primary text-xs flex-1">
                            📄 Paper Trade
                          </button>
                          <button
                            onClick={() => goTrade('live', o)}
                            className="flex-1 px-3 py-2 rounded-lg text-xs font-medium bg-red-500/15 border border-red-500/40 text-red-400 hover:bg-red-500/25 transition-colors"
                          >
                            🔴 Live Trade
                          </button>
                        </div>
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </>
      )}

      {!loading && results.length === 0 && !error && universe && (
        <div className="card text-center text-slate-400">
          No opportunities scored above the minimum. Lower the min-score slider or change timeframe.
        </div>
      )}
    </div>
  );
}
