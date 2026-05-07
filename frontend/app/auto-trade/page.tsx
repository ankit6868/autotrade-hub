'use client';

import { useEffect, useState, useCallback, useRef } from 'react';
import { api } from '@/lib/api';
import MetricCard from '@/components/ui/MetricCard';
import StatusBadge from '@/components/ui/StatusBadge';
import WebhookManager from '@/components/dashboard/WebhookManager';
import StrategyChart from '@/components/dashboard/StrategyChart';

// ─── Types ───────────────────────────────────────────────────────────────────

interface EngineState {
  running: boolean;
  ticks: number;
  deploys: number;
  last_action: string | null;
  last_opportunity: Record<string, unknown> | null;
  history: { ts: string; event?: string; action?: string; detail: unknown }[];
  started_at: string | null;
}

interface Settings {
  auto_trade_enabled: boolean;
  auto_trade_mode: string;
  auto_trade_min_score: number;
  auto_trade_timeframe: string;
  auto_trade_scan_interval_s: number;
  trailing_stop_pct: number;
  take_profit_pct: number;
  position_adjustment: boolean;
  max_open_trades: number;
  max_position_pct: number;
  auto_trade_strategy_id: number | null;
  auto_trade_pairs: string | null;
}

interface Strategy {
  id: number;
  name: string;
  description: string;
  timeframe: string;
  pairs: string[] | null;
  stoploss: number;
}

interface Trade {
  id: number;
  pair: string;
  side: string;
  entry_price: number;
  exit_price?: number;
  amount: number;
  stoploss_price?: number;
  profit_pct?: number;
  profit_abs?: number;
  entry_time: string;
  exit_time?: string;
  exit_reason?: string;
  mode: string;
}

const DEFAULT_SETTINGS: Settings = {
  auto_trade_enabled: false,
  auto_trade_mode: 'paper',
  auto_trade_min_score: 70,
  auto_trade_timeframe: '15m',
  auto_trade_scan_interval_s: 300,
  trailing_stop_pct: 1.0,
  take_profit_pct: 2.0,
  position_adjustment: false,
  max_open_trades: 3,
  max_position_pct: 10,
  auto_trade_strategy_id: null,
  auto_trade_pairs: null,
};

function formatDetail(d: unknown): string {
  if (d == null) return '';
  if (typeof d === 'string') return d;
  if (typeof d === 'number' || typeof d === 'boolean') return String(d);
  if (typeof d === 'object')
    return Object.entries(d as Record<string, unknown>)
      .map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : String(v)}`)
      .join(', ');
  return String(d);
}

// ─── Page ────────────────────────────────────────────────────────────────────

type Tab = 'overview' | 'strategy' | 'trades';

export default function AutoTradePage() {
  const [tab, setTab] = useState<Tab>('overview');
  const [engineState, setEngineState] = useState<EngineState | null>(null);
  const [settings, setSettings] = useState<Settings>(DEFAULT_SETTINGS);
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [openTrades, setOpenTrades] = useState<Trade[]>([]);
  const [tradeHistory, setTradeHistory] = useState<Trade[]>([]);
  const [saving, setSaving] = useState(false);
  const [savedMsg, setSavedMsg] = useState('');
  const [error, setError] = useState('');
  const [pairsInput, setPairsInput] = useState('');
  const pairsInputDirty = useRef(false);   // true while user is editing
  const [forceBuying, setForceBuying] = useState(false);

  const refresh = useCallback(async () => {
    // Use allSettled so one failing request doesn't block the rest
    const [sRes, stRes, openRes, histRes] = await Promise.allSettled([
      api.autotrade.status(),
      api.autotrade.settings.get(),
      api.trade.open(settings.auto_trade_mode as 'paper' | 'live'),
      api.trade.history({ mode: settings.auto_trade_mode, limit: '30' }),
    ]);

    // Apply whatever succeeded
    if (sRes.status === 'fulfilled') {
      setEngineState(sRes.value as EngineState);
    }
    if (stRes.status === 'fulfilled' && !stRes.value?.error) {
      const merged = { ...DEFAULT_SETTINGS, ...stRes.value };
      setSettings(merged);
      // Only sync the pairs text-field if the user isn't actively editing it
      if (!pairsInputDirty.current) {
        setPairsInput(merged.auto_trade_pairs || '');
      }
    }
    if (openRes.status === 'fulfilled') {
      setOpenTrades((openRes.value.trades || []) as Trade[]);
    }
    if (histRes.status === 'fulfilled') {
      setTradeHistory((histRes.value.trades || []) as Trade[]);
    }

    // Only show an error if the critical engine status call failed
    if (sRes.status === 'rejected') {
      const msg = String(sRes.reason);
      // Don't show transient network errors as persistent errors
      if (!msg.includes('Failed to fetch') && !msg.includes('NetworkError')) {
        setError(msg);
      }
    } else {
      // Clear any previous transient error once we get a good response
      setError('');
    }
  }, []);

  useEffect(() => {
    refresh();
    api.strategy.list().then((d) => setStrategies((d.strategies || []) as Strategy[])).catch(() => {});
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, [refresh]);

  async function saveSettings(patch: Partial<Settings> = {}) {
    setSaving(true);
    setSavedMsg('');
    setError('');
    try {
      const payload = { ...settings, ...patch, auto_trade_pairs: pairsInput.trim() || null };
      await api.autotrade.settings.put(payload as unknown as Record<string, unknown>);
      pairsInputDirty.current = false;
      setSavedMsg('Saved');
      setTimeout(() => setSavedMsg(''), 2000);
      await refresh();
    } catch (e) {
      setError(String(e));
    }
    setSaving(false);
  }

  async function startEngine() {
    try { await api.autotrade.start(); await refresh(); } catch (e) { setError(String(e)); }
  }
  async function stopEngine() {
    try { await api.autotrade.stop(); await refresh(); } catch (e) { setError(String(e)); }
  }
  async function forceClose(id: number) {
    try { await api.trade.forceClose(id); await refresh(); } catch (e) { setError(String(e)); }
  }
  async function emergencyStop() {
    if (!confirm('EMERGENCY STOP: halt all trading and close all positions?')) return;
    try { await api.trade.emergencyStop(); await refresh(); } catch (e) { alert(String(e)); }
  }

  // Deploy the current best opportunity immediately without waiting for next scan tick
  async function deployNow() {
    if (!lastOpp) return;
    setForceBuying(true);
    setError('');
    try {
      // Find strategy ID from name
      const strat = strategies.find(
        (s) => s.name === String(lastOpp.strategy)
      );
      if (!strat) {
        setError(`Strategy "${lastOpp.strategy}" not found in your uploaded strategies. Upload it first.`);
        return;
      }
      const result = await api.trade.start({
        strategy_id: strat.id,
        mode: settings.auto_trade_mode,
        pairs: [String(lastOpp.pair)],
        timeframe: String(lastOpp.timeframe || settings.auto_trade_timeframe),
        stoploss: -((settings as unknown as Record<string, number>).default_stoploss_pct || 3) / 100,
        wallet: 1000,
        ...(settings.auto_trade_mode === 'live' ? { confirmation: 'CONFIRM' } : {}),
      });
      if (result.error) setError(String(result.error));
      else await refresh();
    } catch (e) {
      setError(String(e));
    }
    setForceBuying(false);
  }

  const isRunning = Boolean(engineState?.running);
  const lastOpp = engineState?.last_opportunity as Record<string, unknown> | null;
  const totalPnl = tradeHistory.reduce((sum, t) => sum + (t.profit_abs || 0), 0);
  const winRate = tradeHistory.length
    ? (tradeHistory.filter((t) => (t.profit_abs || 0) > 0).length / tradeHistory.length) * 100
    : 0;
  const pinnedStrategy = strategies.find((s) => s.id === settings.auto_trade_strategy_id);

  const tabs: { id: Tab; label: string }[] = [
    { id: 'overview', label: 'Overview' },
    { id: 'strategy', label: 'Strategy' },
    { id: 'trades', label: `Active Trades ${openTrades.length > 0 ? `(${openTrades.length})` : ''}` },
  ];

  return (
    <div className="max-w-6xl mx-auto">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-6">
        <div>
          <h1 className="heading-xl">Auto-Trade Engine</h1>
          <p className="text-slate-400 mt-1 text-sm">
            {settings.auto_trade_strategy_id
              ? `Pinned: ${pinnedStrategy?.name || `Strategy #${settings.auto_trade_strategy_id}`}`
              : 'Auto-selects the best pair + strategy from live market scanner'}
          </p>
        </div>
        <div className="flex items-center gap-3 flex-wrap">
          <StatusBadge status={isRunning ? 'running' : 'stopped'} label={isRunning ? 'Engine Running' : 'Stopped'} />
          {isRunning && (
            <button onClick={emergencyStop} className="btn-danger text-sm animate-pulse">
              🛑 Emergency Stop
            </button>
          )}
        </div>
      </div>

      {/* Engine Start/Stop */}
      <div className="card mb-6 flex items-center justify-between">
        <div>
          <p className="text-sm text-slate-400">
            Mode: <span className={`font-semibold ${settings.auto_trade_mode === 'live' ? 'text-red-400' : 'text-emerald-400'}`}>
              {settings.auto_trade_mode.toUpperCase()}
            </span>
            {' · '}Scan every {settings.auto_trade_scan_interval_s}s
            {' · '}Min score {settings.auto_trade_min_score}
          </p>
        </div>
        <div className="flex gap-3">
          {!isRunning
            ? <button onClick={startEngine} className="btn-success">Start Engine</button>
            : <button onClick={stopEngine} className="btn-danger">Stop Engine</button>}
        </div>
      </div>

      {error && <p className="text-red-400 text-sm mb-4">{error}</p>}

      {/* Tabs */}
      <div className="flex gap-1 mb-6 border-b border-[#2a3a52]">
        {tabs.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`px-4 py-2 text-sm font-medium transition-colors ${
              tab === t.id
                ? 'text-brand-400 border-b-2 border-brand-400 -mb-px'
                : 'text-slate-400 hover:text-white'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* ── TAB: OVERVIEW ─────────────────────────────────────────────────── */}
      {tab === 'overview' && (
        <>
          {/* Metrics */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
            <MetricCard title="Ticks Run" value={engineState?.ticks ?? 0} />
            <MetricCard title="Deploys" value={engineState?.deploys ?? 0} color="profit" />
            <MetricCard title="Last Action" value={engineState?.last_action || '—'} />
            <MetricCard
              title="Mode"
              value={settings.auto_trade_mode.toUpperCase()}
              color={settings.auto_trade_mode === 'live' ? 'loss' : 'profit'}
            />
          </div>

          {/* Last Opportunity */}
          {lastOpp && (
            <div className="card mb-6">
              <div className="flex items-center justify-between mb-4 flex-wrap gap-3">
                <h2 className="text-lg font-semibold">Latest Candidate</h2>
                <div className="flex items-center gap-2">
                  {/* Signal badge */}
                  {(() => {
                    const sig = String(lastOpp.action || lastOpp.recommendation || '');
                    const sigColor = sig.includes('STRONG_BUY') || sig.includes('BUY')
                      ? 'bg-emerald-500/20 border-emerald-500/40 text-emerald-300'
                      : sig.includes('SELL')
                      ? 'bg-red-500/20 border-red-500/40 text-red-300'
                      : 'bg-amber-500/20 border-amber-500/40 text-amber-300';
                    return (
                      <span className={`text-xs font-bold uppercase px-3 py-1 rounded-full border ${sigColor}`}>
                        {sig.includes('BUY') ? '📈' : sig.includes('SELL') ? '📉' : '⏳'} {sig.replace('_', ' ')}
                      </span>
                    );
                  })()}
                  {/* Deploy Now button */}
                  {isRunning && (
                    <button
                      onClick={deployNow}
                      disabled={forceBuying}
                      className="px-4 py-1.5 rounded-lg text-xs font-semibold bg-emerald-600/20 border border-emerald-500/50 text-emerald-300 hover:bg-emerald-600/40 transition-colors disabled:opacity-50"
                    >
                      {forceBuying ? '⏳ Deploying...' : `⚡ Deploy on ${String(lastOpp.pair)} Now`}
                    </button>
                  )}
                </div>
              </div>
              <div className="grid grid-cols-2 md:grid-cols-5 gap-4 text-sm">
                {[
                  ['Pair', lastOpp.pair],
                  ['Strategy', lastOpp.strategy],
                  ['Score', Number(lastOpp.overall_score || lastOpp.score || 0).toFixed(1)],
                  ['Signal', lastOpp.action || lastOpp.recommendation || '—'],
                  ['Timeframe', lastOpp.timeframe || settings.auto_trade_timeframe],
                ].map(([label, val]) => (
                  <div key={String(label)}>
                    <p className="text-slate-400 text-xs">{String(label)}</p>
                    <p className="font-bold">{String(val)}</p>
                  </div>
                ))}
              </div>
              <div className="mt-3 pt-3 border-t border-[#2a3a52] text-xs text-slate-400">
                <span className="inline-flex items-center gap-1.5">
                  <span className="w-2 h-2 rounded-full bg-amber-400 animate-pulse inline-block" />
                  Bot is running — will auto-enter when strategy signals a buy at the next {String(lastOpp.timeframe || '15m')} candle close
                </span>
              </div>
            </div>
          )}

          {/* Engine Settings */}
          <div className="card mb-6">
            <h2 className="text-lg font-semibold mb-4">Engine Settings</h2>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-4">
              <div>
                <label className="label">Persist on restart</label>
                <select className="input" value={settings.auto_trade_enabled ? 'on' : 'off'}
                  onChange={(e) => setSettings({ ...settings, auto_trade_enabled: e.target.value === 'on' })}>
                  <option value="off">Disabled</option>
                  <option value="on">Enabled</option>
                </select>
              </div>
              <div>
                <label className="label">Mode</label>
                <select className="input" value={settings.auto_trade_mode}
                  onChange={(e) => setSettings({ ...settings, auto_trade_mode: e.target.value })}>
                  <option value="paper">Paper (dry-run)</option>
                  <option value="live">Live (real money)</option>
                </select>
              </div>
              <div>
                <label className="label">Timeframe</label>
                <select className="input" value={settings.auto_trade_timeframe}
                  onChange={(e) => setSettings({ ...settings, auto_trade_timeframe: e.target.value })}>
                  {['5m', '15m', '30m', '1h', '4h'].map((tf) => <option key={tf}>{tf}</option>)}
                </select>
              </div>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-4">
              <div>
                <label className="label">Min Score: {settings.auto_trade_min_score}</label>
                <input type="range" min={50} max={95} step={1} value={settings.auto_trade_min_score}
                  onChange={(e) => setSettings({ ...settings, auto_trade_min_score: Number(e.target.value) })}
                  className="w-full accent-brand-500 mt-2" />
              </div>
              <div>
                <label className="label">Scan Interval (s)</label>
                <input type="number" className="input" value={settings.auto_trade_scan_interval_s}
                  min={60} max={3600}
                  onChange={(e) => setSettings({ ...settings, auto_trade_scan_interval_s: Number(e.target.value) })} />
              </div>
              <div>
                <label className="label">Max Open Trades</label>
                <input type="number" className="input" value={settings.max_open_trades}
                  min={1} max={10}
                  onChange={(e) => setSettings({ ...settings, max_open_trades: Number(e.target.value) })} />
              </div>
            </div>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4">
              <div>
                <label className="label">Position Size: {settings.max_position_pct}%</label>
                <input type="range" min={1} max={50} step={1} value={settings.max_position_pct}
                  onChange={(e) => setSettings({ ...settings, max_position_pct: Number(e.target.value) })}
                  className="w-full accent-brand-500 mt-2" />
              </div>
              <div>
                <label className="label">Trailing Stop: {settings.trailing_stop_pct}%</label>
                <input type="range" min={0} max={10} step={0.1} value={settings.trailing_stop_pct}
                  onChange={(e) => setSettings({ ...settings, trailing_stop_pct: Number(e.target.value) })}
                  className="w-full accent-brand-500 mt-2" />
              </div>
              <div>
                <label className="label">Take Profit: {settings.take_profit_pct}%</label>
                <input type="range" min={0} max={20} step={0.1} value={settings.take_profit_pct}
                  onChange={(e) => setSettings({ ...settings, take_profit_pct: Number(e.target.value) })}
                  className="w-full accent-brand-500 mt-2" />
              </div>
              <div>
                <label className="label">DCA</label>
                <select className="input" value={settings.position_adjustment ? 'on' : 'off'}
                  onChange={(e) => setSettings({ ...settings, position_adjustment: e.target.value === 'on' })}>
                  <option value="off">Off</option>
                  <option value="on">On</option>
                </select>
              </div>
            </div>
            <div className="flex items-center gap-3">
              <button onClick={() => saveSettings()} disabled={saving} className="btn-primary">
                {saving ? 'Saving…' : 'Save Settings'}
              </button>
              {savedMsg && <span className="text-emerald-400 text-sm">{savedMsg}</span>}
            </div>
          </div>

          {/* Event Log */}
          <div className="card">
            <h2 className="text-lg font-semibold mb-4">Engine Event Log</h2>
            {!engineState?.history?.length ? (
              <p className="text-slate-500 text-sm">No events yet. Start the engine to see ticks.</p>
            ) : (
              <div className="overflow-x-auto max-h-[360px] overflow-y-auto">
                <table className="w-full text-sm">
                  <thead className="sticky top-0 bg-[#1a2236]">
                    <tr className="text-slate-400 border-b border-[#2a3a52]">
                      <th className="text-left py-3 px-2">Time</th>
                      <th className="text-left py-3 px-2">Event</th>
                      <th className="text-left py-3 px-2">Detail</th>
                    </tr>
                  </thead>
                  <tbody>
                    {[...engineState.history].reverse().map((h, i) => {
                      const kind = (h.event || h.action || '').toString();
                      return (
                        <tr key={i} className="border-b border-[#2a3a52]/50 hover:bg-[#2a3a52]/20">
                          <td className="py-2 px-2 text-slate-400 text-xs whitespace-nowrap">{h.ts}</td>
                          <td className="py-2 px-2">
                            <span className={`inline-block px-2 py-0.5 rounded text-xs ${
                              kind === 'deploy' || kind === 'deployed' ? 'bg-emerald-500/20 text-emerald-400' :
                              kind === 'skip' ? 'bg-slate-500/20 text-slate-400' :
                              kind === 'error' || kind === 'deploy_failed' ? 'bg-red-500/20 text-red-400' :
                              'bg-blue-500/20 text-blue-400'
                            }`}>{kind || '—'}</span>
                          </td>
                          <td className="py-2 px-2 text-slate-300">{formatDetail(h.detail)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          {/* TradingView Webhook */}
          <WebhookManager />
        </>
      )}

      {/* ── TAB: STRATEGY ─────────────────────────────────────────────────── */}
      {tab === 'strategy' && (
        <div className="space-y-6">
          {/* Mode selector */}
          <div className="card">
            <h2 className="text-lg font-semibold mb-1">Strategy Selection</h2>
            <p className="text-slate-400 text-sm mb-5">
              Choose how the engine picks what to trade. Auto-select continuously scans
              the top 50 KuCoin pairs and deploys the highest-scoring match. Pin a strategy
              to force the engine to always use your specific strategy instead.
            </p>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-6">
              {/* Auto card */}
              <button
                onClick={() => setSettings({ ...settings, auto_trade_strategy_id: null })}
                className={`text-left p-4 rounded-xl border-2 transition-colors ${
                  !settings.auto_trade_strategy_id
                    ? 'border-brand-500 bg-brand-500/10'
                    : 'border-[#2a3a52] hover:border-[#3a4a62]'
                }`}
              >
                <div className="flex items-center gap-3 mb-2">
                  <span className="text-2xl">🤖</span>
                  <span className="font-semibold">Auto-select best</span>
                  {!settings.auto_trade_strategy_id && (
                    <span className="ml-auto text-xs bg-brand-500/20 text-brand-400 px-2 py-0.5 rounded">Active</span>
                  )}
                </div>
                <p className="text-sm text-slate-400">
                  Engine scans live market data every {settings.auto_trade_scan_interval_s}s, scores all
                  pair-strategy combos, and deploys the top result above your min-score threshold.
                </p>
              </button>

              {/* Pinned card */}
              <button
                onClick={() => {
                  if (strategies.length > 0 && !settings.auto_trade_strategy_id)
                    setSettings({ ...settings, auto_trade_strategy_id: strategies[0].id });
                }}
                className={`text-left p-4 rounded-xl border-2 transition-colors ${
                  settings.auto_trade_strategy_id
                    ? 'border-emerald-500 bg-emerald-500/10'
                    : 'border-[#2a3a52] hover:border-[#3a4a62]'
                }`}
              >
                <div className="flex items-center gap-3 mb-2">
                  <span className="text-2xl">📌</span>
                  <span className="font-semibold">Pin my strategy</span>
                  {settings.auto_trade_strategy_id && (
                    <span className="ml-auto text-xs bg-emerald-500/20 text-emerald-400 px-2 py-0.5 rounded">Active</span>
                  )}
                </div>
                <p className="text-sm text-slate-400">
                  Select one of your uploaded strategies. The engine will always deploy this
                  strategy — no scanner scoring needed.
                </p>
              </button>
            </div>

            {/* Pinned strategy config */}
            {settings.auto_trade_strategy_id !== null && (
              <div className="border-t border-[#2a3a52] pt-5 space-y-4">
                {strategies.length === 0 ? (
                  <div className="p-4 rounded-lg bg-yellow-500/10 border border-yellow-500/30">
                    <p className="text-yellow-400 text-sm">
                      No strategies uploaded yet. Go to <strong>Strategy → Upload</strong> to add one first.
                    </p>
                  </div>
                ) : (
                  <>
                    <div>
                      <label className="label">Select Strategy</label>
                      <select
                        className="input"
                        value={settings.auto_trade_strategy_id || ''}
                        onChange={(e) => setSettings({ ...settings, auto_trade_strategy_id: Number(e.target.value) })}
                      >
                        {strategies.map((s) => (
                          <option key={s.id} value={s.id}>{s.name}</option>
                        ))}
                      </select>
                    </div>

                    {/* Strategy detail card */}
                    {pinnedStrategy && (
                      <div className="p-4 rounded-lg bg-[#111827] border border-[#2a3a52] text-sm">
                        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 sm:gap-4">
                          <div>
                            <p className="text-slate-400 text-xs">Timeframe</p>
                            <p className="font-semibold">{pinnedStrategy.timeframe || '15m'}</p>
                          </div>
                          <div>
                            <p className="text-slate-400 text-xs">Default Stop-Loss</p>
                            <p className="font-semibold text-red-400">{((pinnedStrategy.stoploss || -0.03) * 100).toFixed(1)}%</p>
                          </div>
                          <div>
                            <p className="text-slate-400 text-xs">Default Pairs</p>
                            <p className="font-semibold">{pinnedStrategy.pairs?.join(', ') || 'Auto'}</p>
                          </div>
                        </div>
                        {pinnedStrategy.description && (
                          <p className="text-slate-400 mt-3 text-xs">{pinnedStrategy.description}</p>
                        )}
                      </div>
                    )}
                  </>
                )}
              </div>
            )}
          </div>

          {/* Pairs override */}
          <div className="card">
            <h2 className="text-lg font-semibold mb-1">Pairs Override</h2>
            <p className="text-slate-400 text-sm mb-4">
              Optionally restrict which pairs the engine trades. Leave blank to use the top 50
              KuCoin pairs by volume (auto-select) or top 3 by volume (pinned strategy).
            </p>
            <label className="label">Pairs (comma-separated, e.g. BTC/USDT, ETH/USDT)</label>
            <input
              className="input"
              value={pairsInput}
              onChange={(e) => { pairsInputDirty.current = true; setPairsInput(e.target.value); }}
              onBlur={() => { /* keep dirty until save */ }}
              placeholder="Leave blank for auto top-volume pairs"
            />
            {pairsInput && (
              <div className="flex gap-2 mt-2 flex-wrap">
                {pairsInput.split(',').map((p) => p.trim()).filter(Boolean).map((p) => (
                  <span key={p} className="text-xs px-2 py-1 rounded bg-brand-500/20 text-brand-400">{p}</span>
                ))}
              </div>
            )}
          </div>

          {/* Save */}
          <div className="flex items-center gap-3">
            <button onClick={() => saveSettings()} disabled={saving} className="btn-primary">
              {saving ? 'Saving…' : 'Save Strategy Settings'}
            </button>
            {settings.auto_trade_strategy_id && (
              <button
                onClick={() => { setSettings({ ...settings, auto_trade_strategy_id: null }); setPairsInput(''); }}
                className="btn-secondary"
              >
                Clear Pin (use auto-select)
              </button>
            )}
            {savedMsg && <span className="text-emerald-400 text-sm">{savedMsg}</span>}
            {error && <span className="text-red-400 text-sm">{error}</span>}
          </div>
        </div>
      )}

      {/* ── TAB: ACTIVE TRADES ────────────────────────────────────────────── */}
      {tab === 'trades' && (
        <div className="space-y-6">
          {/* P&L summary */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <MetricCard
              title="Total P&L"
              value={`${totalPnl >= 0 ? '+' : ''}${totalPnl.toFixed(2)} USDT`}
              color={totalPnl >= 0 ? 'profit' : 'loss'}
            />
            <MetricCard title="Open Positions" value={openTrades.length} />
            <MetricCard title="Win Rate" value={`${winRate.toFixed(1)}%`} color={winRate >= 50 ? 'profit' : 'loss'} />
            <MetricCard title="Closed Trades" value={tradeHistory.length} />
          </div>

          {/* ── Signal Status ── */}
          <div className="card">
            <h2 className="text-lg font-semibold mb-3">Live Signal Status</h2>
            {!isRunning ? (
              <div className="flex items-center gap-4 p-4 rounded-xl bg-slate-500/10 border border-slate-500/20">
                <span className="text-3xl">⏸️</span>
                <div className="flex-1">
                  <p className="font-semibold text-slate-300">Engine Stopped</p>
                  <p className="text-sm text-slate-400 mt-0.5">Start the engine to begin watching for buy signals.</p>
                </div>
                <button onClick={startEngine} className="btn-success text-sm whitespace-nowrap">
                  ▶ Start Engine
                </button>
              </div>
            ) : openTrades.length > 0 ? (
              <div className="flex items-center gap-4 p-4 rounded-xl bg-emerald-500/10 border border-emerald-500/20">
                <span className="w-4 h-4 rounded-full bg-emerald-400 animate-pulse flex-shrink-0" />
                <div>
                  <p className="font-semibold text-emerald-300">🟢 IN TRADE — {openTrades.length} open position{openTrades.length > 1 ? 's' : ''}</p>
                  <p className="text-sm text-slate-400 mt-0.5">
                    Bot is managing open position(s). Will exit at take-profit, stop-loss, or strategy sell signal.
                  </p>
                </div>
              </div>
            ) : (
              <div className="flex items-center gap-4 p-4 rounded-xl bg-amber-500/10 border border-amber-500/20">
                <span className="w-4 h-4 rounded-full bg-amber-400 animate-pulse flex-shrink-0" />
                <div>
                  <p className="font-semibold text-amber-300">⏳ Waiting for BUY Signal...</p>
                  <p className="text-sm text-slate-400 mt-0.5">
                    Bot is watching{' '}
                    <span className="text-white font-medium">
                      {lastOpp ? String(lastOpp.pair) : 'top-volume pairs'}
                    </span>
                    {lastOpp ? ` with strategy ${String(lastOpp.strategy)}` : ''}.
                    {' '}Entry fires automatically at the next candle close when the strategy signals a buy.
                  </p>
                </div>
              </div>
            )}
          </div>

          {/* ── Manual Override — Force Buy ── */}
          <div className="card">
            <div className="flex items-center justify-between mb-3">
              <div>
                <h2 className="text-lg font-semibold">Manual Override</h2>
                <p className="text-sm text-slate-400 mt-0.5">Force-deploy a trade now without waiting for the next strategy signal.</p>
              </div>
            </div>
            {!isRunning ? (
              <p className="text-sm text-slate-500 italic">Start the engine first to enable manual override.</p>
            ) : !lastOpp ? (
              <p className="text-sm text-slate-500 italic">No candidate available yet — wait for the engine to complete a scan tick.</p>
            ) : (
              <div className="flex items-center gap-4 flex-wrap p-4 rounded-xl bg-[#111827] border border-[#2a3a52]">
                <div className="flex-1 min-w-[220px]">
                  <p className="text-xs text-slate-400 mb-1">Current Best Candidate</p>
                  <p className="font-bold text-white">{String(lastOpp.pair)}</p>
                  <p className="text-xs text-slate-400 mt-0.5">
                    {String(lastOpp.strategy)} · Score {Number(lastOpp.overall_score || lastOpp.score || 0).toFixed(1)} · {String(lastOpp.timeframe || settings.auto_trade_timeframe)}
                  </p>
                </div>
                <div className="flex gap-2 flex-shrink-0">
                  <button
                    onClick={deployNow}
                    disabled={forceBuying}
                    className="flex items-center gap-2 px-5 py-2 rounded-lg font-semibold text-sm bg-emerald-600 hover:bg-emerald-500 text-white transition-colors disabled:opacity-50"
                  >
                    {forceBuying ? (
                      <>⏳ Opening trade...</>
                    ) : (
                      <>📈 BUY {String(lastOpp.pair)} Now</>
                    )}
                  </button>
                </div>
              </div>
            )}
            {error && <p className="text-red-400 text-xs mt-2">{error}</p>}
          </div>

          {/* ── Strategy Analytics Chart ── */}
          <StrategyChart
            pair={openTrades.length > 0 ? String(openTrades[0].pair) : 'BTC/USDT'}
            timeframe="15m"
            mode="paper"
            height={460}
          />

          {/* ── Open positions ── */}
          <div className="card">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-lg font-semibold">Open Positions</h2>
              {openTrades.length > 0 && (
                <button onClick={emergencyStop} className="btn-danger text-sm">
                  🛑 Emergency Stop All
                </button>
              )}
            </div>
            {openTrades.length === 0 ? (
              <div className="text-center py-8 text-slate-500">
                <p className="text-4xl mb-3">📭</p>
                <p className="font-medium">No open positions</p>
                <p className="text-sm mt-1">Use the Manual Override above or wait for an auto-buy signal.</p>
              </div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-slate-400 border-b border-[#2a3a52]">
                      <th className="text-left py-3 px-2">Pair</th>
                      <th className="text-left py-3 px-2">Mode</th>
                      <th className="text-right py-3 px-2">Entry Price</th>
                      <th className="text-right py-3 px-2">Amount</th>
                      <th className="text-right py-3 px-2">Stop Loss</th>
                      <th className="text-left py-3 px-2">Opened</th>
                      <th className="text-center py-3 px-2">Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {openTrades.map((t) => (
                      <tr key={t.id} className="border-b border-[#2a3a52]/50 hover:bg-[#2a3a52]/20">
                        <td className="py-3 px-2 font-bold text-white">{t.pair}</td>
                        <td className="py-3 px-2">
                          <span className={`text-xs px-2 py-0.5 rounded font-medium ${t.mode === 'live' ? 'bg-red-500/20 text-red-400' : 'bg-blue-500/20 text-blue-400'}`}>
                            {t.mode === 'live' ? '🔴 LIVE' : '📄 PAPER'}
                          </span>
                        </td>
                        <td className="py-3 px-2 text-right font-mono">{Number(t.entry_price).toFixed(4)}</td>
                        <td className="py-3 px-2 text-right font-mono">{Number(t.amount).toFixed(6)}</td>
                        <td className="py-3 px-2 text-right text-red-400 font-mono">
                          {t.stoploss_price ? Number(t.stoploss_price).toFixed(4) : '—'}
                        </td>
                        <td className="py-3 px-2 text-slate-400 text-xs whitespace-nowrap">{t.entry_time}</td>
                        <td className="py-3 px-2 text-center">
                          <button
                            onClick={() => forceClose(t.id)}
                            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold bg-red-500/20 border border-red-500/40 text-red-400 hover:bg-red-500/30 transition-colors"
                          >
                            📉 Force Sell
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          {/* ── Trade history ── */}
          <div className="card">
            <h2 className="text-lg font-semibold mb-4">Trade History</h2>
            {tradeHistory.length === 0 ? (
              <p className="text-slate-500 text-sm">No closed trades yet.</p>
            ) : (
              <div className="overflow-x-auto max-h-[400px] overflow-y-auto">
                <table className="w-full text-sm">
                  <thead className="sticky top-0 bg-[#1a2236]">
                    <tr className="text-slate-400 border-b border-[#2a3a52]">
                      <th className="text-left py-3 px-2">Pair</th>
                      <th className="text-left py-3 px-2">Mode</th>
                      <th className="text-right py-3 px-2">Entry</th>
                      <th className="text-right py-3 px-2">Exit</th>
                      <th className="text-right py-3 px-2">P&L %</th>
                      <th className="text-right py-3 px-2">P&L USDT</th>
                      <th className="text-left py-3 px-2">Reason</th>
                    </tr>
                  </thead>
                  <tbody>
                    {tradeHistory.map((t) => (
                      <tr key={t.id} className="border-b border-[#2a3a52]/50 hover:bg-[#2a3a52]/20">
                        <td className="py-2 px-2 font-medium">{t.pair}</td>
                        <td className="py-2 px-2">
                          <span className={`text-xs px-2 py-0.5 rounded ${t.mode === 'live' ? 'bg-red-500/20 text-red-400' : 'bg-slate-500/20 text-slate-400'}`}>
                            {t.mode}
                          </span>
                        </td>
                        <td className="py-2 px-2 text-right font-mono">{Number(t.entry_price).toFixed(4)}</td>
                        <td className="py-2 px-2 text-right font-mono">{Number(t.exit_price || 0).toFixed(4)}</td>
                        <td className={`py-2 px-2 text-right font-medium ${(t.profit_pct || 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                          {(t.profit_pct || 0) >= 0 ? '+' : ''}{Number(t.profit_pct || 0).toFixed(2)}%
                        </td>
                        <td className={`py-2 px-2 text-right font-medium ${(t.profit_abs || 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                          {(t.profit_abs || 0) >= 0 ? '+' : ''}{Number(t.profit_abs || 0).toFixed(2)}
                        </td>
                        <td className="py-2 px-2 text-slate-400 text-xs">{t.exit_reason || '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
