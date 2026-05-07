'use client';
import { useEffect, useState, useCallback, Suspense } from 'react';
import { api } from '@/lib/api';
import StrategyChart from '@/components/dashboard/StrategyChart';
import StrategySignalMonitor from '@/components/dashboard/StrategySignalMonitor';
import PairPicker from '@/components/ui/PairPicker';

const SAFETY_ITEMS = [
  { id: 'leverage', label: 'I understand leverage amplifies BOTH profits AND losses' },
  { id: 'liquidation', label: 'I understand positions can be liquidated if market moves against me' },
  { id: 'risk', label: 'I only trade with funds I can afford to lose completely' },
  { id: 'api', label: 'My KuCoin Futures API key is configured in Setup' },
];

function FuturesLiveInner() {
  const [strategies, setStrategies] = useState<any[]>([]);
  const [strategyId, setStrategyId] = useState<number | null>(null);
  const [pairs, setPairs] = useState(['BTC/USDT']);
  const [timeframe, setTimeframe] = useState('15m');
  const [leverage, setLeverage] = useState(5);
  const [stoploss, setStoploss] = useState(2);
  const [takeProfit, setTakeProfit] = useState(1.5);
  const [acknowledged, setAcknowledged] = useState<Record<string, boolean>>({});
  const [botStatus, setBotStatus] = useState<any>({ running: false });
  const [openTrades, setOpenTrades] = useState<any[]>([]);
  const [tradeHistory, setTradeHistory] = useState<any[]>([]);
  const [starting, setStarting] = useState(false);
  const [closingId, setClosingId] = useState<string | null>(null);
  const [error, setError] = useState('');
  const allAcknowledged = SAFETY_ITEMS.every(i => acknowledged[i.id]);

  const refreshData = useCallback(async () => {
    try {
      const [status, open, history] = await Promise.all([
        api.futures.status(),
        api.futures.open('live'),
        api.futures.history({ mode: 'live', limit: '20' }),
      ]);
      setBotStatus(status);
      setOpenTrades(open.trades);
      setTradeHistory(history.trades);
    } catch {}
  }, []);

  useEffect(() => {
    api.strategy.list().then(d => {
      setStrategies(d.strategies || []);
      if (d.strategies?.length > 0) setStrategyId(Number(d.strategies[0].id));
    }).catch(() => {});
    refreshData();
    const t = setInterval(refreshData, 10000);
    return () => clearInterval(t);
  }, [refreshData]);

  async function startBot() {
    if (!strategyId || !allAcknowledged) return;
    setStarting(true); setError('');
    try {
      const r = await api.futures.start({
        strategy_id: strategyId, mode: 'live', pairs, leverage,
        timeframe, stoploss: -(stoploss / 100), take_profit_pct: takeProfit, max_position_pct: 5,
      });
      if (r.error) setError(r.error); else refreshData();
    } catch (e) { setError(String(e)); }
    setStarting(false);
  }

  const isRunning = Boolean(botStatus?.running) && botStatus?.mode === 'live';
  const totalPnl = tradeHistory.reduce((s: number, t: any) => s + (Number(t.profit_abs) || 0), 0);

  return (
    <div className="max-w-6xl mx-auto">
      {/* Warning */}
      <div className="mb-6 p-4 rounded-xl border border-red-500/50 bg-red-500/10 flex items-start gap-3">
        <span className="text-3xl">⚡</span>
        <div>
          <p className="font-bold text-red-400">LIVE FUTURES — REAL MONEY WITH LEVERAGE</p>
          <p className="text-red-300/70 text-xs mt-1">
            Leveraged futures can result in losses exceeding your initial margin. Positions can be liquidated.
            Never risk money you cannot afford to lose. Live trades execute on KuCoin with REAL funds.
          </p>
        </div>
      </div>

      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="heading-xl">⚡ Futures Live Trading</h1>
          <p className="text-slate-400 text-sm mt-1">Real leveraged trading on KuCoin Futures</p>
        </div>
        <span className={`chip ${isRunning ? 'bg-red-500/10 border-red-500/30 text-red-300' : 'bg-slate-500/10 border-slate-500/30 text-slate-400'}`}>
          {isRunning ? `🔴 LIVE Futures (${botStatus.leverage || leverage}x)` : '⚫ Stopped'}
        </span>
      </div>

      {/* Safety Checklist */}
      {!isRunning && (
        <div className="card mb-6 border-red-500/20">
          <h3 className="font-semibold text-red-400 mb-3">⚠ Safety Acknowledgments — tick all to enable</h3>
          <div className="space-y-2">
            {SAFETY_ITEMS.map(item => (
              <label key={item.id} className="flex items-center gap-3 cursor-pointer">
                <input type="checkbox" checked={!!acknowledged[item.id]}
                  onChange={e => setAcknowledged(prev => ({ ...prev, [item.id]: e.target.checked }))}
                  className="w-4 h-4 accent-red-500" />
                <span className="text-sm text-slate-300">{item.label}</span>
              </label>
            ))}
          </div>
        </div>
      )}

      {/* Bot Config */}
      <div className="card mb-6">
        <div className="grid grid-cols-2 md:grid-cols-5 gap-4 mb-4">
          <div>
            <label className="label">Strategy</label>
            <select className="input" value={strategyId || ''} onChange={e => setStrategyId(Number(e.target.value))} disabled={isRunning}>
              {strategies.map((s: any) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
          </div>
          <div className="md:col-span-2">
            <label className="label">Pairs</label>
            <PairPicker value={pairs} onChange={setPairs} disabled={isRunning} />
          </div>
          <div>
            <label className="label">Timeframe</label>
            <select className="input" value={timeframe} onChange={e => setTimeframe(e.target.value)} disabled={isRunning}>
              {['1m','5m','15m','30m','1h'].map(t => <option key={t} value={t}>{t}</option>)}
            </select>
          </div>
          <div>
            <label className="label">Leverage: {leverage}x</label>
            <input type="range" min={1} max={20} value={leverage} onChange={e => setLeverage(Number(e.target.value))} disabled={isRunning} className="w-full accent-blue-500 mt-2" />
            <p className="text-xs text-orange-400 mt-1">Liq. at ~{(100 / leverage).toFixed(1)}%</p>
          </div>
        </div>
        <div className="grid grid-cols-2 gap-4 mb-4">
          <div>
            <label className="label">Stop-Loss: {stoploss}%</label>
            <input type="range" min={0.5} max={5} step={0.5} value={stoploss} onChange={e => setStoploss(Number(e.target.value))} disabled={isRunning} className="w-full accent-red-500 mt-2" />
          </div>
          <div>
            <label className="label">Take-Profit: {takeProfit}% (leveraged: {(takeProfit * leverage).toFixed(1)}%)</label>
            <input type="range" min={0.1} max={5} step={0.1} value={takeProfit} onChange={e => setTakeProfit(Number(e.target.value))} disabled={isRunning} className="w-full accent-emerald-500 mt-2" />
          </div>
        </div>
        {error && <p className="text-red-400 text-xs mb-3">{error}</p>}
        <div className="flex gap-3">
          {isRunning
            ? <button onClick={async () => { await api.futures.stop(); refreshData(); }} className="btn-danger">■ Stop Bot</button>
            : <button onClick={startBot} disabled={starting || !strategyId || !allAcknowledged}
                className={`px-6 py-2.5 rounded-xl font-semibold text-sm border transition-all ${allAcknowledged ? 'bg-red-500/20 border-red-500/50 text-red-300 hover:bg-red-500/30' : 'bg-slate-700/30 border-slate-600/30 text-slate-500 cursor-not-allowed'}`}>
                {starting ? 'Starting…' : '🔴 Start Live Futures'}
              </button>
          }
        </div>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
        {[
          { label: 'Open Positions', value: openTrades.length, color: openTrades.length > 0 ? 'text-emerald-400' : 'text-white' },
          { label: 'Realized P&L', value: `${totalPnl >= 0 ? '+' : ''}${totalPnl.toFixed(4)} USDT`, color: totalPnl >= 0 ? 'text-emerald-400' : 'text-red-400' },
          { label: 'Total Trades', value: tradeHistory.length },
          { label: 'Win Rate', value: `${tradeHistory.length > 0 ? Math.round(tradeHistory.filter((t: any) => t.profit_abs > 0).length / tradeHistory.length * 100) : 0}%` },
        ].map(m => (
          <div key={m.label} className="card card-hover">
            <p className="text-xs text-slate-400 uppercase mb-1">{m.label}</p>
            <p className={`stat-lg ${m.color || 'text-white'}`}>{m.value}</p>
          </div>
        ))}
      </div>

      <StrategyChart pair={pairs[0] || 'BTC/USDT'} timeframe={timeframe} mode="live" height={420} />

      {/* Open Positions */}
      <div className="card mb-6">
        <h2 className="text-lg font-semibold mb-4">Open Futures Positions</h2>
        {openTrades.length === 0 ? <p className="text-slate-500 text-sm">No open positions</p> : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead><tr className="text-slate-400 border-b border-[#2a3a52]">
                <th className="text-left py-2 px-2">Pair</th><th className="text-right py-2 px-2">Side</th>
                <th className="text-right py-2 px-2">Lev</th><th className="text-right py-2 px-2">Entry</th>
                <th className="text-right py-2 px-2">Liq. Price</th><th className="text-right py-2 px-2">Unrealized</th>
                <th className="text-right py-2 px-2">Action</th>
              </tr></thead>
              <tbody>
                {openTrades.map((t: any) => (
                  <tr key={String(t.id)} className="border-b border-[#2a3a52]/50">
                    <td className="py-2 px-2 font-medium">{t.pair}</td>
                    <td className={`py-2 px-2 text-right font-semibold ${t.side === 'long' ? 'text-emerald-400' : 'text-red-400'}`}>{t.side?.toUpperCase()}</td>
                    <td className="py-2 px-2 text-right text-blue-400 font-bold">{t.leverage}x</td>
                    <td className="py-2 px-2 text-right font-mono">{Number(t.entry_price).toFixed(2)}</td>
                    <td className="py-2 px-2 text-right font-mono text-orange-400">{Number(t.liquidation_price || 0).toFixed(2)}</td>
                    <td className={`py-2 px-2 text-right font-semibold ${Number(t.unrealized_pnl) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                      {Number(t.unrealized_pnl) >= 0 ? '+' : ''}{Number(t.unrealized_pnl || 0).toFixed(4)}
                    </td>
                    <td className="py-2 px-2 text-right">
                      {closingId === String(t.id) ? (
                        <button disabled className="text-xs px-2 py-1 rounded text-slate-400 opacity-70">⏳</button>
                      ) : (
                        <button onClick={async () => {
                          setClosingId(String(t.id));
                          try { await api.futures.forceClose(t.pair); refreshData(); }
                          catch {} finally { setClosingId(null); }
                        }} className="text-xs px-2 py-1 rounded bg-red-500/20 border border-red-500/40 text-red-400 hover:bg-red-500/30">
                          📉 Close
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Trade Log */}
      <div className="card">
        <h2 className="text-lg font-semibold mb-4">Trade Log</h2>
        {tradeHistory.length === 0 ? <p className="text-slate-500 text-sm">No trades yet</p> : (
          <div className="overflow-x-auto max-h-[400px] overflow-y-auto">
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-[#1a2236]"><tr className="text-slate-400 border-b border-[#2a3a52]">
                <th className="text-left py-2 px-2">Pair</th><th className="text-right py-2 px-2">Lev</th>
                <th className="text-right py-2 px-2">Entry</th><th className="text-right py-2 px-2">Exit</th>
                <th className="text-right py-2 px-2">Profit%</th><th className="text-right py-2 px-2">USDT</th>
                <th className="text-left py-2 px-2">Reason</th>
              </tr></thead>
              <tbody>
                {tradeHistory.map((t: any) => (
                  <tr key={String(t.id)} className="border-b border-[#2a3a52]/50">
                    <td className="py-2 px-2">{t.pair}</td>
                    <td className="py-2 px-2 text-right text-blue-400">{t.leverage}x</td>
                    <td className="py-2 px-2 text-right font-mono text-xs">{Number(t.entry_price).toFixed(2)}</td>
                    <td className="py-2 px-2 text-right font-mono text-xs">{Number(t.exit_price).toFixed(2)}</td>
                    <td className={`py-2 px-2 text-right font-semibold ${(t.profit_pct || 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                      {(t.profit_pct || 0) >= 0 ? '+' : ''}{(t.profit_pct || 0).toFixed(2)}%
                    </td>
                    <td className={`py-2 px-2 text-right font-semibold ${(t.profit_abs || 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                      {(t.profit_abs || 0) >= 0 ? '+' : ''}{(t.profit_abs || 0).toFixed(4)}
                    </td>
                    <td className="py-2 px-2 text-slate-400 text-xs">{t.exit_reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

export default function FuturesLivePage() {
  return <Suspense><FuturesLiveInner /></Suspense>;
}
