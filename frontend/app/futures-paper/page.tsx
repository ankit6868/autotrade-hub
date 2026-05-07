'use client';
import { useEffect, useState, useCallback, Suspense } from 'react';
import { api } from '@/lib/api';
import StrategyChart from '@/components/dashboard/StrategyChart';
import StrategySignalMonitor from '@/components/dashboard/StrategySignalMonitor';
import PairPicker from '@/components/ui/PairPicker';

function FuturesPaperInner() {
  const [strategies, setStrategies] = useState<any[]>([]);
  const [strategyId, setStrategyId] = useState<number | null>(null);
  const [pairs, setPairs] = useState(['BTC/USDT']);
  const [timeframe, setTimeframe] = useState('15m');
  const [leverage, setLeverage] = useState(10);
  const [wallet, setWallet] = useState(1000);
  const [stoploss, setStoploss] = useState(3);
  const [takeProfit, setTakeProfit] = useState(1.5);
  const [botStatus, setBotStatus] = useState<any>({ running: false });
  const [openTrades, setOpenTrades] = useState<any[]>([]);
  const [tradeHistory, setTradeHistory] = useState<any[]>([]);
  const [starting, setStarting] = useState(false);
  const [closingId, setClosingId] = useState<string | null>(null);
  const [error, setError] = useState('');

  const refreshData = useCallback(async () => {
    try {
      const [status, open, history] = await Promise.all([
        api.futures.status(),
        api.futures.open('paper'),
        api.futures.history({ mode: 'paper', limit: '20' }),
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
    if (!strategyId) return;
    setStarting(true); setError('');
    try {
      const r = await api.futures.start({
        strategy_id: strategyId, mode: 'paper', pairs, leverage,
        timeframe, stoploss: -(stoploss / 100), wallet,
        take_profit_pct: takeProfit, max_position_pct: 5,
      });
      if (r.error) setError(r.error); else refreshData();
    } catch (e) { setError(String(e)); }
    setStarting(false);
  }

  async function stopBot() {
    try { await api.futures.stop(); refreshData(); } catch {}
  }

  const isRunning = Boolean(botStatus?.running);
  const totalPnl = tradeHistory.reduce((s: number, t: any) => s + (Number(t.profit_abs) || 0), 0);
  const wins = tradeHistory.filter((t: any) => (t.profit_abs || 0) > 0).length;
  const winRate = tradeHistory.length > 0 ? Math.round((wins / tradeHistory.length) * 100) : 0;

  return (
    <div className="max-w-6xl mx-auto">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="heading-xl">📊 Futures Paper Trading</h1>
          <p className="text-slate-400 text-sm mt-1">Test leverage trading with virtual money — no real risk</p>
        </div>
        <span className={`chip ${isRunning ? 'bg-emerald-500/10 border-emerald-500/30 text-emerald-300' : 'bg-slate-500/10 border-slate-500/30 text-slate-400'}`}>
          {isRunning ? `🟢 Futures Paper (${botStatus.leverage || leverage}x)` : '⚫ Stopped'}
        </span>
      </div>

      {/* Bot Config */}
      <div className="card mb-6">
        <div className="grid grid-cols-2 md:grid-cols-6 gap-4 mb-4">
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
              {['1m','5m','15m','30m','1h','4h'].map(t => <option key={t} value={t}>{t}</option>)}
            </select>
          </div>
          <div>
            <label className="label">Leverage: {leverage}x</label>
            <input type="range" min={1} max={125} value={leverage} onChange={e => setLeverage(Number(e.target.value))} disabled={isRunning} className="w-full accent-blue-500 mt-2" />
          </div>
          <div>
            <label className="label">Wallet (USDT)</label>
            <input className="input" type="number" value={wallet} onChange={e => setWallet(Number(e.target.value))} disabled={isRunning} />
          </div>
        </div>
        <div className="grid grid-cols-2 gap-4 mb-4">
          <div>
            <label className="label">Stop-Loss: {stoploss}% (liq at ~{(100 / leverage).toFixed(1)}%)</label>
            <input type="range" min={0.5} max={10} step={0.5} value={stoploss} onChange={e => setStoploss(Number(e.target.value))} disabled={isRunning} className="w-full accent-red-500 mt-2" />
            <p className="text-xs text-orange-400 mt-1">⚠ With {leverage}x leverage, liquidation at ~{(100 / leverage).toFixed(1)}% move</p>
          </div>
          <div>
            <label className="label">Take-Profit: {takeProfit}% → leveraged: {(takeProfit * leverage).toFixed(1)}%</label>
            <input type="range" min={0.1} max={10} step={0.1} value={takeProfit} onChange={e => setTakeProfit(Number(e.target.value))} disabled={isRunning} className="w-full accent-emerald-500 mt-2" />
          </div>
        </div>
        <div className="flex gap-3">
          {error && <p className="text-red-400 text-xs mr-2 self-center">{error}</p>}
          {isRunning
            ? <button onClick={stopBot} className="btn-danger">■ Stop Bot</button>
            : <button onClick={startBot} disabled={starting || !strategyId} className="btn-primary">
                {starting ? 'Starting…' : '▶ Start Futures Paper Trading'}
              </button>
          }
        </div>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-4 mb-6">
        {[
          { label: 'Virtual Margin', value: `${wallet.toFixed(0)} USDT` },
          { label: 'Realized P&L', value: `${totalPnl >= 0 ? '+' : ''}${totalPnl.toFixed(4)} USDT`, color: totalPnl >= 0 ? 'text-emerald-400' : 'text-red-400' },
          { label: 'Unrealized P&L', value: `${openTrades.reduce((s: number, t: any) => s + (Number(t.unrealized_pnl) || 0), 0).toFixed(4)} USDT` },
          { label: 'Open Positions', value: openTrades.length },
          { label: 'Win Rate', value: `${winRate}%` },
        ].map(m => (
          <div key={m.label} className="card card-hover">
            <p className="text-xs text-slate-400 uppercase tracking-wider mb-1">{m.label}</p>
            <p className={`stat-lg ${m.color || 'text-white'} truncate`}>{m.value}</p>
          </div>
        ))}
      </div>

      {/* Signal Monitor */}
      {strategyId && (
        <StrategySignalMonitor
          strategyName={strategies.find(s => s.id === strategyId)?.name || 'Strategy'}
          pair={pairs[0] || 'BTC/USDT'} timeframe={timeframe} isRunning={isRunning}
        />
      )}

      {/* Analytics Chart */}
      <StrategyChart pair={pairs[0] || 'BTC/USDT'} timeframe={timeframe} mode="paper" height={420} />

      {/* Open Positions */}
      <div className="card mb-6">
        <h2 className="text-lg font-semibold mb-4 flex items-center gap-2">
          Open Positions
          {openTrades.length > 0 && <span className="text-sm text-slate-400">Unrealized: <span className={openTrades.reduce((s: number, t: any) => s + (t.unrealized_pnl || 0), 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}>{openTrades.reduce((s: number, t: any) => s + (t.unrealized_pnl || 0), 0).toFixed(4)} USDT</span></span>}
        </h2>
        {openTrades.length === 0 ? <p className="text-slate-500 text-sm">No open futures positions</p> : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead><tr className="text-slate-400 border-b border-[#2a3a52]">
                <th className="text-left py-2 px-2">Pair</th>
                <th className="text-right py-2 px-2">Side</th>
                <th className="text-right py-2 px-2">Leverage</th>
                <th className="text-right py-2 px-2">Entry</th>
                <th className="text-right py-2 px-2">Current</th>
                <th className="text-right py-2 px-2">Liq. Price</th>
                <th className="text-right py-2 px-2">Margin</th>
                <th className="text-right py-2 px-2">Unreal. P&L</th>
                <th className="text-right py-2 px-2">Action</th>
              </tr></thead>
              <tbody>
                {openTrades.map((t: any) => {
                  const unreal = Number(t.unrealized_pnl) || 0;
                  const liqPrice = Number(t.liquidation_price) || 0;
                  const curPrice = Number(t.current_price) || 0;
                  const dangerClose = liqPrice > 0 && curPrice > 0 &&
                    (t.side === 'long' ? curPrice < liqPrice * 1.05 : curPrice > liqPrice * 0.95);
                  return (
                    <tr key={String(t.id)} className={`border-b border-[#2a3a52]/50 hover:bg-[#2a3a52]/10 ${dangerClose ? 'bg-red-500/5' : ''}`}>
                      <td className="py-2 px-2 font-medium">{t.pair}</td>
                      <td className={`py-2 px-2 text-right font-semibold ${t.side === 'long' ? 'text-emerald-400' : 'text-red-400'}`}>{t.side?.toUpperCase()}</td>
                      <td className="py-2 px-2 text-right text-blue-400 font-bold">{t.leverage || leverage}x</td>
                      <td className="py-2 px-2 text-right font-mono">{Number(t.entry_price).toFixed(2)}</td>
                      <td className="py-2 px-2 text-right font-mono">{curPrice > 0 ? curPrice.toFixed(2) : '—'}</td>
                      <td className={`py-2 px-2 text-right font-mono text-xs ${dangerClose ? 'text-red-400 font-bold animate-pulse' : 'text-orange-400'}`}>
                        {liqPrice > 0 ? liqPrice.toFixed(2) : '—'}
                        {dangerClose && ' ⚠'}
                      </td>
                      <td className="py-2 px-2 text-right font-mono">{Number(t.amount).toFixed(2)}</td>
                      <td className={`py-2 px-2 text-right font-mono font-semibold ${unreal >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                        {unreal >= 0 ? '+' : ''}{unreal.toFixed(4)}
                      </td>
                      <td className="py-2 px-2 text-right">
                        {closingId === String(t.id) ? (
                          <button disabled className="text-xs px-2 py-1 rounded bg-slate-700/40 text-slate-400 opacity-70">⏳</button>
                        ) : (
                          <button onClick={async () => {
                            setClosingId(String(t.id));
                            try { await api.futures.forceClose(t.pair); refreshData(); }
                            catch {} finally { setClosingId(null); }
                          }} className="text-xs px-2 py-1 rounded bg-red-500/20 border border-red-500/30 text-red-400 hover:bg-red-500/30 transition-colors">
                            📉 Close
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Trade Log */}
      <div className="card">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold">Trade Log</h2>
          {tradeHistory.length > 0 && <span className="text-xs text-slate-500">{tradeHistory.length} trades</span>}
        </div>
        {tradeHistory.length === 0 ? <p className="text-slate-500 text-sm">No closed futures trades yet</p> : (
          <div className="overflow-x-auto max-h-[400px] overflow-y-auto">
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-[#1a2236]"><tr className="text-slate-400 border-b border-[#2a3a52]">
                <th className="text-left py-2 px-2">Pair</th>
                <th className="text-right py-2 px-2">Side</th>
                <th className="text-right py-2 px-2">Lev</th>
                <th className="text-right py-2 px-2">Entry</th>
                <th className="text-right py-2 px-2">Exit</th>
                <th className="text-right py-2 px-2">Profit%</th>
                <th className="text-right py-2 px-2">Profit USDT</th>
                <th className="text-left py-2 px-2">Reason</th>
              </tr></thead>
              <tbody>
                {tradeHistory.map((t: any) => (
                  <tr key={String(t.id)} className="border-b border-[#2a3a52]/50 hover:bg-[#2a3a52]/20">
                    <td className="py-2 px-2">{t.pair}</td>
                    <td className={`py-2 px-2 text-right text-xs font-semibold ${t.side === 'long' ? 'text-emerald-400' : 'text-red-400'}`}>{t.side?.toUpperCase()}</td>
                    <td className="py-2 px-2 text-right text-blue-400 text-xs">{t.leverage || 1}x</td>
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

export default function FuturesPaperPage() {
  return <Suspense><FuturesPaperInner /></Suspense>;
}
