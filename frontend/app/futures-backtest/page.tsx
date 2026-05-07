'use client';
import { useEffect, useState, Suspense } from 'react';
import { api } from '@/lib/api';
import PairPicker from '@/components/ui/PairPicker';

function FuturesBacktestInner() {
  // ── Config ────────────────────────────────────────────────────────────────
  const [strategies, setStrategies]     = useState<any[]>([]);
  const [strategyId, setStrategyId]     = useState<number | null>(null);
  const [pairs, setPairs]               = useState(['BTC/USDT']);
  const [timeframe, setTimeframe]       = useState('15m');
  const [timerange, setTimerange]       = useState('20240101-20241231');
  const [leverage, setLeverage]         = useState(10);
  const [startBalance, setStartBalance] = useState(1000);
  const [stoploss, setStoploss]         = useState(3.0);
  const [takeProfit, setTakeProfit]     = useState(1.5);

  // ── State ─────────────────────────────────────────────────────────────────
  const [loading, setLoading]           = useState(false);
  const [result, setResult]             = useState<any>(null);
  const [history, setHistory]           = useState<any[]>([]);
  const [error, setError]               = useState('');
  const [activeTab, setActiveTab]       = useState<'trades' | 'equity' | 'history'>('trades');

  useEffect(() => {
    api.strategy.list().then(d => {
      setStrategies(d.strategies ?? []);
      if (d.strategies?.length > 0) {
        const first = d.strategies[0];
        setStrategyId(Number(first.id));
        if (first.stoploss)        setStoploss(Math.abs(Number(first.stoploss) * 100));
        if (first.take_profit)     setTakeProfit(Number(first.take_profit) * 100);
        if (first.default_leverage) setLeverage(Number(first.default_leverage));
        if (first.timeframe)       setTimeframe(first.timeframe);
      }
    }).catch(() => {});
    api.futures.backtest.history().then(d => setHistory(d.backtests ?? [])).catch(() => {});
  }, []);

  // Auto-fill when strategy changes
  useEffect(() => {
    if (!strategyId || strategies.length === 0) return;
    const s = strategies.find((x: any) => x.id === strategyId);
    if (!s) return;
    if (s.stoploss)         setStoploss(Math.abs(Number(s.stoploss) * 100));
    if (s.take_profit)      setTakeProfit(Number(s.take_profit) * 100);
    if (s.default_leverage) setLeverage(Number(s.default_leverage));
    if (s.timeframe)        setTimeframe(s.timeframe);
  }, [strategyId, strategies]);

  async function runBacktest() {
    if (!strategyId) return;
    setLoading(true); setError(''); setResult(null);
    try {
      const r = await api.futures.backtest.run({
        strategy_id:      strategyId,
        pairs,
        timeframe,
        timerange,
        leverage,
        starting_balance: startBalance,
        stoploss_pct:     stoploss,
        take_profit_pct:  takeProfit,
      });
      if (r.error) setError(r.error);
      else {
        setResult(r);
        // Refresh history
        api.futures.backtest.history().then(d => setHistory(d.backtests ?? [])).catch(() => {});
      }
    } catch (e) { setError(String(e)); }
    setLoading(false);
  }

  const m = result?.metrics;

  return (
    <div className="max-w-6xl mx-auto">
      {/* Header */}
      <div className="mb-6">
        <h1 className="heading-xl">📊 Futures Backtest</h1>
        <p className="text-slate-400 text-sm mt-1">
          Test your strategy with leverage on historical KuCoin data — includes liquidation simulation
        </p>
      </div>

      {/* Config card */}
      <div className="card mb-6">
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4">
          <div>
            <label className="label">Strategy</label>
            <select className="input" value={strategyId ?? ''} onChange={e => setStrategyId(Number(e.target.value))}>
              {strategies.map((s: any) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
          </div>
          <div className="md:col-span-2">
            <label className="label">Pairs</label>
            <PairPicker value={pairs} onChange={setPairs} />
          </div>
          <div>
            <label className="label">Timeframe</label>
            <select className="input" value={timeframe} onChange={e => setTimeframe(e.target.value)}>
              {['1m','5m','15m','30m','1h','4h'].map(t => <option key={t} value={t}>{t}</option>)}
            </select>
          </div>
        </div>

        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4">
          <div>
            <label className="label">Date Range</label>
            <input className="input font-mono text-xs" value={timerange}
              onChange={e => setTimerange(e.target.value)}
              placeholder="YYYYMMDD-YYYYMMDD" />
            <p className="text-xs text-slate-500 mt-0.5">e.g. 20240101-20241231</p>
          </div>
          <div>
            <label className="label">Starting Balance (USDT)</label>
            <input className="input" type="number" value={startBalance}
              onChange={e => setStartBalance(Number(e.target.value))} />
          </div>
          <div>
            <label className="label">Leverage: {leverage}x</label>
            <input type="range" min={1} max={50} value={leverage}
              onChange={e => setLeverage(Number(e.target.value))}
              className="w-full accent-blue-500 mt-2" />
            <p className="text-xs text-orange-400 mt-0.5">
              Liq. at ~{(100/leverage).toFixed(1)}% move
            </p>
          </div>
          <div>
            <label className="label">Stop-Loss: {stoploss}%</label>
            <input type="range" min={0.5} max={10} step={0.5} value={stoploss}
              onChange={e => setStoploss(Number(e.target.value))}
              className="w-full accent-red-500 mt-2" />
          </div>
        </div>

        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4">
          <div>
            <label className="label">Take-Profit: {takeProfit}% → leveraged: {(takeProfit * leverage).toFixed(1)}%</label>
            <input type="range" min={0.1} max={10} step={0.1} value={takeProfit}
              onChange={e => setTakeProfit(Number(e.target.value))}
              className="w-full accent-emerald-500 mt-2" />
          </div>
        </div>

        {error && <p className="text-red-400 text-xs mb-3">{error}</p>}
        <button onClick={runBacktest} disabled={loading || !strategyId} className="btn-primary">
          {loading ? '⏳ Running backtest…' : '▶ Run Futures Backtest'}
        </button>
        {loading && (
          <p className="text-slate-400 text-xs mt-2 animate-pulse">
            Downloading historical candles from KuCoin and simulating {leverage}x leverage trades…
          </p>
        )}
      </div>

      {/* Results */}
      {result && m && (
        <>
          {/* Metric cards */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
            {[
              {
                label: 'Final Balance',
                value: `${m.final_balance.toFixed(2)} USDT`,
                color: m.final_balance >= startBalance ? 'text-emerald-400' : 'text-red-400',
              },
              {
                label: 'Total P&L',
                value: `${m.total_profit_pct >= 0 ? '+' : ''}${m.total_profit_pct.toFixed(2)}%`,
                sub: `${m.total_profit_abs >= 0 ? '+' : ''}${m.total_profit_abs.toFixed(2)} USDT`,
                color: m.total_profit_pct >= 0 ? 'text-emerald-400' : 'text-red-400',
              },
              {
                label: 'Win Rate',
                value: `${(m.win_rate * 100).toFixed(1)}%`,
                sub: `${m.winning_trades}W / ${m.losing_trades}L`,
                color: m.win_rate >= 0.5 ? 'text-emerald-400' : 'text-amber-400',
              },
              {
                label: 'Max Drawdown',
                value: `-${m.max_drawdown.toFixed(2)}%`,
                color: m.max_drawdown > 30 ? 'text-red-400' : 'text-amber-400',
              },
            ].map(card => (
              <div key={card.label} className="card card-hover">
                <p className="text-xs text-slate-400 uppercase tracking-wider mb-1">{card.label}</p>
                <p className={`text-2xl font-bold font-mono ${card.color}`}>{card.value}</p>
                {card.sub && <p className="text-xs text-slate-400 mt-0.5">{card.sub}</p>}
              </div>
            ))}
          </div>

          {/* Extra stats row */}
          <div className="grid grid-cols-2 md:grid-cols-5 gap-4 mb-6">
            {[
              { label: 'Total Trades',    value: m.total_trades },
              { label: '⚡ Liquidations', value: m.liquidations, color: m.liquidations > 0 ? 'text-red-400' : 'text-white' },
              { label: '📈 Long Trades',  value: m.long_trades },
              { label: '📉 Short Trades', value: m.short_trades },
              { label: 'Avg P&L / Trade', value: `${m.avg_leverage_pnl >= 0 ? '+' : ''}${m.avg_leverage_pnl.toFixed(2)}%`,
                color: m.avg_leverage_pnl >= 0 ? 'text-emerald-400' : 'text-red-400' },
            ].map(s => (
              <div key={s.label} className="card card-hover">
                <p className="text-xs text-slate-400 uppercase tracking-wider mb-1">{s.label}</p>
                <p className={`stat-lg ${s.color ?? 'text-white'}`}>{s.value}</p>
              </div>
            ))}
          </div>

          {/* Tabs: Trades / Equity Curve / History */}
          <div className="card">
            <div className="flex gap-1 mb-4 border-b border-[#2a3a52] pb-3">
              {(['trades', 'equity', 'history'] as const).map(tab => (
                <button key={tab} onClick={() => setActiveTab(tab)}
                  className={`px-3 py-1.5 rounded-lg text-sm font-medium transition-colors ${
                    activeTab === tab
                      ? 'bg-blue-500/20 border border-blue-500/40 text-blue-300'
                      : 'text-slate-400 hover:text-white'
                  }`}>
                  {tab === 'trades' ? `📋 Trade Log (${result.trades?.length ?? 0})`
                    : tab === 'equity' ? '📈 Equity Curve'
                    : '🕐 History'}
                </button>
              ))}
            </div>

            {/* Trade Log */}
            {activeTab === 'trades' && (
              <div className="overflow-x-auto max-h-[500px] overflow-y-auto">
                {(!result.trades || result.trades.length === 0) ? (
                  <p className="text-slate-500 text-sm">No trades generated. Try a wider date range or different strategy.</p>
                ) : (
                  <table className="w-full text-sm">
                    <thead className="sticky top-0 bg-[#1a2236]">
                      <tr className="text-slate-400 border-b border-[#2a3a52]">
                        <th className="text-left py-2 px-2">Pair</th>
                        <th className="text-right py-2 px-2">Dir</th>
                        <th className="text-right py-2 px-2">Lev</th>
                        <th className="text-right py-2 px-2">Entry</th>
                        <th className="text-right py-2 px-2">Exit</th>
                        <th className="text-right py-2 px-2">Liq.</th>
                        <th className="text-right py-2 px-2">P&L%</th>
                        <th className="text-right py-2 px-2">P&L USDT</th>
                        <th className="text-right py-2 px-2">Balance</th>
                        <th className="text-left py-2 px-2">Reason</th>
                      </tr>
                    </thead>
                    <tbody>
                      {result.trades.map((t: any, i: number) => (
                        <tr key={i} className={`border-b border-[#2a3a52]/50 hover:bg-[#2a3a52]/20 ${
                          t.exit_reason === 'liquidated' ? 'bg-red-500/5' : ''
                        }`}>
                          <td className="py-2 px-2 font-medium">{t.pair}</td>
                          <td className={`py-2 px-2 text-right font-semibold text-xs ${
                            t.direction === 'long' ? 'text-emerald-400' : 'text-red-400'
                          }`}>{t.direction?.toUpperCase()}</td>
                          <td className="py-2 px-2 text-right text-blue-400 text-xs">{t.leverage}x</td>
                          <td className="py-2 px-2 text-right font-mono text-xs">{Number(t.open_rate).toFixed(2)}</td>
                          <td className="py-2 px-2 text-right font-mono text-xs">{Number(t.close_rate).toFixed(2)}</td>
                          <td className="py-2 px-2 text-right font-mono text-xs text-orange-400">{Number(t.liq_price).toFixed(2)}</td>
                          <td className={`py-2 px-2 text-right font-semibold ${(t.profit_pct ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                            {(t.profit_pct ?? 0) >= 0 ? '+' : ''}{(t.profit_pct ?? 0).toFixed(2)}%
                          </td>
                          <td className={`py-2 px-2 text-right font-semibold ${(t.profit_abs ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                            {(t.profit_abs ?? 0) >= 0 ? '+' : ''}{(t.profit_abs ?? 0).toFixed(4)}
                          </td>
                          <td className="py-2 px-2 text-right font-mono text-xs">{t.balance?.toFixed(2)}</td>
                          <td className={`py-2 px-2 text-xs ${t.exit_reason === 'liquidated' ? 'text-red-400 font-bold' : 'text-slate-400'}`}>
                            {t.exit_reason === 'liquidated' ? '⚡ LIQUIDATED' : t.exit_reason}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            )}

            {/* Equity Curve (simple text table) */}
            {activeTab === 'equity' && (
              <div>
                {(!result.equity_curve || result.equity_curve.length === 0) ? (
                  <p className="text-slate-500 text-sm">No equity data.</p>
                ) : (
                  <div className="overflow-x-auto max-h-[500px] overflow-y-auto">
                    <table className="w-full text-sm">
                      <thead className="sticky top-0 bg-[#1a2236]">
                        <tr className="text-slate-400 border-b border-[#2a3a52]">
                          <th className="text-left py-2 px-2">#</th>
                          <th className="text-left py-2 px-2">Date</th>
                          <th className="text-right py-2 px-2">Balance (USDT)</th>
                          <th className="text-right py-2 px-2">Change</th>
                        </tr>
                      </thead>
                      <tbody>
                        {result.equity_curve.map((pt: any, i: number) => {
                          const prev = i === 0 ? startBalance : result.equity_curve[i-1].balance;
                          const change = pt.balance - prev;
                          return (
                            <tr key={i} className="border-b border-[#2a3a52]/50">
                              <td className="py-1.5 px-2 text-slate-500 text-xs">{i + 1}</td>
                              <td className="py-1.5 px-2 text-xs font-mono text-slate-300">{String(pt.date).slice(0, 19)}</td>
                              <td className={`py-1.5 px-2 text-right font-mono font-bold ${pt.balance >= startBalance ? 'text-emerald-400' : 'text-red-400'}`}>
                                {pt.balance.toFixed(2)}
                              </td>
                              <td className={`py-1.5 px-2 text-right text-xs font-mono ${change >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                                {change >= 0 ? '+' : ''}{change.toFixed(4)}
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

            {/* Past runs history */}
            {activeTab === 'history' && (
              <div className="overflow-x-auto">
                {history.length === 0 ? (
                  <p className="text-slate-500 text-sm">No past backtest runs yet.</p>
                ) : (
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="text-slate-400 border-b border-[#2a3a52]">
                        <th className="text-left py-2 px-2">Strategy</th>
                        <th className="text-right py-2 px-2">Pairs</th>
                        <th className="text-right py-2 px-2">TF</th>
                        <th className="text-right py-2 px-2">Leverage</th>
                        <th className="text-right py-2 px-2">P&L%</th>
                        <th className="text-right py-2 px-2">Win Rate</th>
                        <th className="text-right py-2 px-2">Trades</th>
                        <th className="text-right py-2 px-2">Liq.</th>
                        <th className="text-right py-2 px-2">DD%</th>
                        <th className="text-left py-2 px-2">Date</th>
                      </tr>
                    </thead>
                    <tbody>
                      {history.map((h: any) => (
                        <tr key={h.id} className="border-b border-[#2a3a52]/50 hover:bg-[#2a3a52]/20">
                          <td className="py-2 px-2 font-medium text-xs">{h.strategy_name}</td>
                          <td className="py-2 px-2 text-right text-xs text-slate-400">{h.pairs}</td>
                          <td className="py-2 px-2 text-right text-xs">{h.timeframe}</td>
                          <td className="py-2 px-2 text-right text-blue-400 font-bold text-xs">{h.leverage}x</td>
                          <td className={`py-2 px-2 text-right font-semibold ${(h.total_profit_pct ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                            {(h.total_profit_pct ?? 0) >= 0 ? '+' : ''}{(h.total_profit_pct ?? 0).toFixed(2)}%
                          </td>
                          <td className="py-2 px-2 text-right text-xs">{((h.win_rate ?? 0) * 100).toFixed(1)}%</td>
                          <td className="py-2 px-2 text-right text-xs">{h.total_trades}</td>
                          <td className={`py-2 px-2 text-right text-xs ${(h.liquidations ?? 0) > 0 ? 'text-red-400 font-bold' : 'text-slate-400'}`}>
                            {h.liquidations ?? 0}
                          </td>
                          <td className="py-2 px-2 text-right text-xs text-amber-400">-{(h.max_drawdown ?? 0).toFixed(1)}%</td>
                          <td className="py-2 px-2 text-xs text-slate-400">{String(h.created_at).slice(0, 10)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            )}
          </div>
        </>
      )}

      {/* History (shown even before running a test) */}
      {!result && history.length > 0 && (
        <div className="card">
          <h2 className="font-semibold mb-4">🕐 Previous Futures Backtests</h2>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-slate-400 border-b border-[#2a3a52]">
                  <th className="text-left py-2 px-2">Strategy</th>
                  <th className="text-right py-2 px-2">Leverage</th>
                  <th className="text-right py-2 px-2">P&L%</th>
                  <th className="text-right py-2 px-2">Win Rate</th>
                  <th className="text-right py-2 px-2">Trades</th>
                  <th className="text-right py-2 px-2">⚡ Liq.</th>
                  <th className="text-left py-2 px-2">Date</th>
                </tr>
              </thead>
              <tbody>
                {history.map((h: any) => (
                  <tr key={h.id} className="border-b border-[#2a3a52]/50 hover:bg-[#2a3a52]/20">
                    <td className="py-2 px-2 font-medium text-xs">{h.strategy_name} — {h.pairs} — {h.timeframe}</td>
                    <td className="py-2 px-2 text-right text-blue-400 font-bold text-xs">{h.leverage}x</td>
                    <td className={`py-2 px-2 text-right font-semibold ${(h.total_profit_pct ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                      {(h.total_profit_pct ?? 0) >= 0 ? '+' : ''}{(h.total_profit_pct ?? 0).toFixed(2)}%
                    </td>
                    <td className="py-2 px-2 text-right text-xs">{((h.win_rate ?? 0) * 100).toFixed(1)}%</td>
                    <td className="py-2 px-2 text-right text-xs">{h.total_trades}</td>
                    <td className={`py-2 px-2 text-right text-xs font-bold ${(h.liquidations ?? 0) > 0 ? 'text-red-400' : 'text-slate-400'}`}>
                      {h.liquidations ?? 0}
                    </td>
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
