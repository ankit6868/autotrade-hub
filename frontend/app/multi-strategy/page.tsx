'use client';
import { useEffect, useState, Suspense } from 'react';
import { api } from '@/lib/api';

const MARKET_TYPES = ['spot', 'futures'];
const MODES = ['paper', 'live'];
const TIMEFRAMES = ['1m', '5m', '15m', '30m', '1h', '4h'];

function MultiStrategyInner() {
  const [instances, setInstances] = useState<any[]>([]);
  const [strategies, setStrategies] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [showAdd, setShowAdd] = useState(false);
  const [stopping, setStopping] = useState<number | null>(null);
  const [msg, setMsg] = useState('');

  // New instance form state
  const [form, setForm] = useState({
    strategy_id: 0,
    market_type: 'spot',
    mode: 'paper',
    pairs: 'BTC/USDT',
    leverage: 1,
    timeframe: '15m',
    stoploss: -0.03,
    takeprofit: 0.015,
    wallet: 1000,
    risk_pct: 5,
  });

  async function refresh() {
    setLoading(true);
    try {
      const [inst, strats] = await Promise.all([
        api.multiStrategy.list(),
        api.strategy.list(),
      ]);
      setInstances(inst.instances || []);
      setStrategies(strats.strategies || []);
      if (strats.strategies?.length > 0 && !form.strategy_id) {
        setForm(f => ({ ...f, strategy_id: strats.strategies[0].id }));
      }
    } catch {}
    setLoading(false);
  }

  useEffect(() => { refresh(); const t = setInterval(refresh, 15000); return () => clearInterval(t); }, []);

  async function createInstance() {
    try {
      const r = await api.multiStrategy.create({
        ...form,
        stoploss: -(Math.abs(form.stoploss)),
        leverage: form.market_type === 'spot' ? 1 : form.leverage,
      });
      if (r.error) setMsg(`❌ ${r.error}`);
      else { setMsg('✅ Strategy instance started!'); setShowAdd(false); refresh(); }
    } catch (e) { setMsg(`❌ ${String(e)}`); }
    setTimeout(() => setMsg(''), 5000);
  }

  async function stopInstance(id: number) {
    setStopping(id);
    try { await api.multiStrategy.stop(id); refresh(); }
    catch {} finally { setStopping(null); }
  }

  const totalPnl = instances.reduce((s, i) => s + (i.total_pnl || 0), 0);
  const totalTrades = instances.reduce((s, i) => s + (i.total_trades || 0), 0);
  const runningCount = instances.filter(i => i.is_running).length;

  return (
    <div className="max-w-6xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="heading-xl">⚙️ Multi-Strategy Manager</h1>
          <p className="text-slate-400 text-sm mt-1">Run multiple strategies simultaneously — spot, futures, paper, live</p>
        </div>
        <button onClick={() => setShowAdd(true)} className="btn-primary">+ Add Strategy</button>
      </div>

      {msg && (
        <div className={`mb-4 p-3 rounded-xl text-sm border ${msg.startsWith('✅') ? 'bg-emerald-500/10 border-emerald-500/30 text-emerald-300' : 'bg-red-500/10 border-red-500/30 text-red-300'}`}>
          {msg}
        </div>
      )}

      {/* Overview stats */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
        {[
          { label: 'Running', value: `${runningCount} / ${instances.length}`, color: runningCount > 0 ? 'text-emerald-400' : 'text-white' },
          { label: 'Total Trades', value: totalTrades },
          { label: 'Combined P&L', value: `${totalPnl >= 0 ? '+' : ''}${totalPnl.toFixed(4)} USDT`, color: totalPnl >= 0 ? 'text-emerald-400' : 'text-red-400' },
          { label: 'Strategies', value: instances.length },
        ].map(m => (
          <div key={m.label} className="card card-hover">
            <p className="text-xs text-slate-400 uppercase mb-1">{m.label}</p>
            <p className={`stat-lg ${m.color || 'text-white'}`}>{m.value}</p>
          </div>
        ))}
      </div>

      {/* Add Instance Modal */}
      {showAdd && (
        <div className="card mb-6 border border-brand-500/30">
          <h3 className="font-semibold mb-4">New Strategy Instance</h3>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4">
            <div>
              <label className="label">Strategy</label>
              <select className="input" value={form.strategy_id} onChange={e => setForm(f => ({ ...f, strategy_id: Number(e.target.value) }))}>
                {strategies.map((s: any) => <option key={s.id} value={s.id}>{s.name}</option>)}
              </select>
            </div>
            <div>
              <label className="label">Market Type</label>
              <select className="input" value={form.market_type} onChange={e => setForm(f => ({ ...f, market_type: e.target.value, leverage: e.target.value === 'spot' ? 1 : 10 }))}>
                {MARKET_TYPES.map(t => <option key={t} value={t}>{t.charAt(0).toUpperCase() + t.slice(1)}</option>)}
              </select>
            </div>
            <div>
              <label className="label">Mode</label>
              <select className="input" value={form.mode} onChange={e => setForm(f => ({ ...f, mode: e.target.value }))}>
                {MODES.map(m => <option key={m} value={m}>{m.charAt(0).toUpperCase() + m.slice(1)}</option>)}
              </select>
            </div>
            <div>
              <label className="label">Timeframe</label>
              <select className="input" value={form.timeframe} onChange={e => setForm(f => ({ ...f, timeframe: e.target.value }))}>
                {TIMEFRAMES.map(t => <option key={t} value={t}>{t}</option>)}
              </select>
            </div>
            <div>
              <label className="label">Pairs (comma-separated)</label>
              <input className="input" value={form.pairs} onChange={e => setForm(f => ({ ...f, pairs: e.target.value }))} placeholder="BTC/USDT, ETH/USDT" />
            </div>
            <div>
              <label className="label">Wallet (USDT)</label>
              <input className="input" type="number" value={form.wallet} onChange={e => setForm(f => ({ ...f, wallet: Number(e.target.value) }))} />
            </div>
            <div>
              <label className="label">Risk per trade: {form.risk_pct}%</label>
              <input type="range" min={1} max={20} value={form.risk_pct} onChange={e => setForm(f => ({ ...f, risk_pct: Number(e.target.value) }))} className="w-full accent-brand-500 mt-2" />
            </div>
            {form.market_type === 'futures' && (
              <div>
                <label className="label">Leverage: {form.leverage}x</label>
                <input type="range" min={1} max={50} value={form.leverage} onChange={e => setForm(f => ({ ...f, leverage: Number(e.target.value) }))} className="w-full accent-blue-500 mt-2" />
              </div>
            )}
          </div>
          {form.mode === 'live' && (
            <div className="p-3 mb-4 rounded-lg bg-red-500/10 border border-red-500/20 text-red-300 text-xs">
              ⚠ Live mode will execute real trades. Ensure your KuCoin API is configured in Setup.
            </div>
          )}
          <div className="flex gap-3">
            <button onClick={createInstance} className="btn-primary">▶ Start Instance</button>
            <button onClick={() => setShowAdd(false)} className="btn-secondary">Cancel</button>
          </div>
        </div>
      )}

      {/* Instances List */}
      {loading ? (
        <div className="text-slate-500 text-center py-8">Loading instances…</div>
      ) : instances.length === 0 ? (
        <div className="card text-center py-12">
          <p className="text-4xl mb-3">⚙️</p>
          <p className="text-slate-400 font-medium">No strategy instances yet</p>
          <p className="text-slate-500 text-sm mt-1">Click "+ Add Strategy" to run multiple strategies simultaneously</p>
        </div>
      ) : (
        <div className="space-y-4">
          {instances.map((inst: any) => {
            const liveStatus = inst.live_status || {};
            const openPos = (liveStatus.positions || []).length;
            return (
              <div key={inst.id} className={`card border ${inst.is_running ? 'border-emerald-500/20' : 'border-[#2a3a52]'}`}>
                <div className="flex items-start justify-between gap-4">
                  <div className="flex-1">
                    <div className="flex items-center gap-3 mb-2">
                      <span className={`text-xs px-2 py-0.5 rounded font-medium ${inst.is_running ? 'bg-emerald-500/15 text-emerald-300' : 'bg-slate-700/40 text-slate-400'}`}>
                        {inst.is_running ? '🟢 Running' : '⚫ Stopped'}
                      </span>
                      <span className={`text-xs px-2 py-0.5 rounded ${inst.market_type === 'futures' ? 'bg-blue-500/15 text-blue-300' : 'bg-slate-700/30 text-slate-400'}`}>
                        {inst.market_type}
                      </span>
                      <span className={`text-xs px-2 py-0.5 rounded ${inst.mode === 'live' ? 'bg-red-500/15 text-red-300' : 'bg-slate-700/30 text-slate-400'}`}>
                        {inst.mode}
                      </span>
                      {inst.leverage > 1 && <span className="text-xs px-2 py-0.5 rounded bg-blue-500/10 text-blue-400">{inst.leverage}x</span>}
                    </div>
                    <h3 className="font-semibold text-base">{inst.strategy_name}</h3>
                    <p className="text-xs text-slate-400 mt-0.5">
                      {inst.pairs} · {inst.timeframe} · SL {Math.abs(inst.stoploss * 100).toFixed(1)}% · Risk {inst.risk_pct}%
                    </p>
                    {inst.is_running && (
                      <p className="text-xs text-slate-500 mt-1">
                        Balance: <span className="text-white">{(liveStatus.balance || inst.wallet || 0).toFixed(2)} USDT</span>
                        {' · '} Open: <span className={openPos > 0 ? 'text-emerald-400' : 'text-white'}>{openPos}</span>
                        {' · '} Trades: <span className="text-white">{inst.total_trades}</span>
                      </p>
                    )}
                  </div>
                  <div className="flex items-center gap-4">
                    <div className="text-right">
                      <p className="text-xs text-slate-400">P&L</p>
                      <p className={`font-semibold ${inst.total_pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                        {inst.total_pnl >= 0 ? '+' : ''}{(inst.total_pnl || 0).toFixed(4)}
                      </p>
                    </div>
                    {inst.is_running && (
                      <button onClick={() => stopInstance(inst.id)} disabled={stopping === inst.id}
                        className="text-xs px-3 py-1.5 rounded-lg bg-red-500/20 border border-red-500/30 text-red-400 hover:bg-red-500/30 transition-colors">
                        {stopping === inst.id ? '⏳' : '■ Stop'}
                      </button>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export default function MultiStrategyPage() {
  return <Suspense><MultiStrategyInner /></Suspense>;
}
