'use client';

import { useEffect, useState, useCallback, Suspense } from 'react';
import { useSearchParams } from 'next/navigation';
import { api } from '@/lib/api';
import { tradeWS } from '@/lib/websocket';
import MetricCard from '@/components/ui/MetricCard';
import StatusBadge from '@/components/ui/StatusBadge';
import TradingViewWidget from '@/components/charts/TradingViewWidget';
import SignalsPanel from '@/components/dashboard/SignalsPanel';
import StrategySignalMonitor from '@/components/dashboard/StrategySignalMonitor';
import PairPicker from '@/components/ui/PairPicker';
import SignalContextPanel from '@/components/dashboard/SignalContextPanel';
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from 'recharts';

/** Convert any "BASE/QUOTE" KuCoin pair to a TradingView symbol string. */
function pairToTV(pair: string): string {
  return `KUCOIN:${pair.replace('/', '')}`;
}

const INTERVAL_TO_TV: Record<string, string> = {
  '1m': '1', '5m': '5', '15m': '15', '30m': '30',
  '1h': '60', '4h': '240', '1d': 'D',
};

function formatDuration(entryTime: string): string {
  try {
    const diff = Date.now() - new Date(entryTime).getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 60) return `${mins}m`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h ${mins % 60}m`;
    return `${Math.floor(hrs / 24)}d ${hrs % 24}h`;
  } catch {
    return '—';
  }
}

function PaperTradeInner() {
  const searchParams = useSearchParams();
  const fromOpp = searchParams.get('pair') !== null;
  const oppPair = searchParams.get('pair');
  const oppStrategy = searchParams.get('strategy');
  const oppTimeframe = searchParams.get('timeframe');
  const oppScore = searchParams.get('score');
  const oppAction = searchParams.get('action'); // 'buy' | 'sell'

  // Signal context params (indicator snapshot from Opportunities scanner)
  const sigRsi = searchParams.get('rsi');
  const sigAdx = searchParams.get('adx');
  const sigMacd = searchParams.get('macd');
  const sigBbPos = searchParams.get('bb_pos');
  const sigVolChange = searchParams.get('vol_change');
  const sigEntryQuality = searchParams.get('entry_quality');
  const sigConfidence = searchParams.get('confidence');
  const sigReasoning = searchParams.get('reasoning');
  const hasSignalData = !!(sigRsi || sigAdx || sigMacd);

  const [strategies, setStrategies] = useState<Record<string, unknown>[]>([]);
  const [strategyId, setStrategyId] = useState<number | null>(null);
  const [pairs, setPairs] = useState<string[]>(oppPair ? [oppPair] : ['BTC/USDT']);
  const [timeframe, setTimeframe] = useState(oppTimeframe || '15m');
  const [wallet, setWallet] = useState(1000);
  const [stoploss, setStoploss] = useState(3);
  const [takeProfit, setTakeProfit] = useState(0); // 0 = disabled

  const [botStatus, setBotStatus] = useState<Record<string, unknown>>({ running: false });
  const [openTrades, setOpenTrades] = useState<Record<string, unknown>[]>([]);
  const [tradeHistory, setTradeHistory] = useState<Record<string, unknown>[]>([]);
  const [currentPrices, setCurrentPrices] = useState<Record<string, number>>({});
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState('');
  const [closingId, setClosingId] = useState<string | number | null>(null);
  const [closeError, setCloseError] = useState<string>('');

  // Quick Backtest state
  const [qbRunning, setQbRunning] = useState(false);
  const [qbResult, setQbResult] = useState<Record<string, unknown> | null>(null);
  const [qbError, setQbError] = useState('');
  const [qbDays, setQbDays] = useState(90);

  const refreshData = useCallback(async () => {
    try {
      const [status, open, history] = await Promise.all([
        api.trade.status(),
        api.trade.open(),
        api.trade.history({ mode: 'paper', limit: '20' }),
      ]);
      setBotStatus(status);
      setOpenTrades(open.trades);
      setTradeHistory(history.trades);

      // Fetch current prices for open positions
      const uniquePairs = Array.from(new Set(open.trades.map((t: Record<string, unknown>) => String(t.pair))));
      const priceEntries = await Promise.all(
        uniquePairs.map(async (p) => {
          try {
            const r = await api.market.price(p);
            return [p, Number(r.price || r.last || 0)] as [string, number];
          } catch {
            return [p, 0] as [string, number];
          }
        })
      );
      setCurrentPrices(Object.fromEntries(priceEntries));
    } catch {}
  }, []);

  useEffect(() => {
    api.strategy.list().then((d) => {
      setStrategies(d.strategies);
      if (d.strategies.length > 0) {
        if (oppStrategy) {
          const match = d.strategies.find(
            (s: Record<string, unknown>) =>
              String(s.name) === oppStrategy || String(s.name).toLowerCase() === oppStrategy.toLowerCase()
          );
          setStrategyId(match ? Number(match.id) : Number(d.strategies[0].id));
        } else {
          setStrategyId(Number(d.strategies[0].id));
        }
      }
    }).catch(() => {});

    refreshData();
    tradeWS.connect();
    const unsub = tradeWS.onMessage(() => refreshData());
    const interval = setInterval(refreshData, 10000);
    return () => {
      unsub();
      tradeWS.disconnect();
      clearInterval(interval);
    };
  }, [refreshData]);

  async function startPaper() {
    if (!strategyId) return;
    setStarting(true);
    setError('');
    try {
      const result = await api.trade.start({
        strategy_id: strategyId,
        mode: 'paper',
        pairs,
        timeframe,
        stoploss: -(stoploss / 100),
        wallet,
        ...(takeProfit > 0 ? { take_profit: takeProfit / 100 } : {}),
      });
      if (result.error) setError(String(result.error));
      await refreshData();
    } catch (e) {
      setError(String(e));
    }
    setStarting(false);
  }

  async function stopPaper() {
    try {
      await api.trade.stop();
      await refreshData();
    } catch (e) {
      setError(String(e));
    }
  }

  async function forceClose(tradeId: string | number) {
    // First click: ask for confirmation inline (no blocking dialog)
    if (closingId !== String(tradeId)) {
      setClosingId(String(tradeId));
      setCloseError('');
      return;
    }
    // Second click (confirmed): execute close
    setCloseError('');
    try {
      await api.trade.forceClose(tradeId);
      setClosingId(null);
      await refreshData();
    } catch (e) {
      setCloseError(String(e));
      setClosingId(null);
    }
  }

  async function runQuickBacktest() {
    if (!strategyId) return;
    setQbRunning(true);
    setQbResult(null);
    setQbError('');
    try {
      const end = new Date();
      const start = new Date();
      start.setDate(end.getDate() - qbDays);
      const pad = (n: number) => String(n).padStart(2, '0');
      const toYMD = (d: Date) => `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}`;
      const timerange = `${toYMD(start)}-${toYMD(end)}`;

      const data = await api.backtest.run({
        strategy_id: strategyId,
        timerange,
        pairs: pairs.slice(0, 3), // limit to 3 pairs for speed
        timeframe,
        starting_balance: wallet,
        stoploss: -(stoploss / 100),
      }) as Record<string, unknown>;

      if (data.error) setQbError(String(data.error));
      else setQbResult(data);
    } catch (e) {
      setQbError(String(e));
    }
    setQbRunning(false);
  }

  const totalPnl = tradeHistory.reduce((sum, t) => sum + (Number(t.profit_abs) || 0), 0);
  void oppStrategy;
  const winRate =
    tradeHistory.length > 0
      ? (tradeHistory.filter((t) => Number(t.profit_abs) > 0).length / tradeHistory.length) * 100
      : 0;
  const isRunning = Boolean(botStatus.running);

  // Compute unrealized P&L for open trades.
  // The backend returns unrealized_pnl directly when available (native engine).
  // Fallback: stake * (current - entry) / entry  (amount field = USDT stake, not BTC qty)
  function getUnrealizedPnl(t: Record<string, unknown>): number {
    if (t.unrealized_pnl !== undefined && t.unrealized_pnl !== null) {
      return Number(t.unrealized_pnl);
    }
    const current = currentPrices[String(t.pair)] || 0;
    const entry = Number(t.entry_price);
    const stake = Number(t.amount);   // amount = USDT stake
    if (!current || !entry || !stake) return 0;
    return stake * (current - entry) / entry;
  }

  const totalUnrealizedPnl = openTrades.reduce((sum, t) => sum + getUnrealizedPnl(t), 0);

  return (
    <div className="max-w-6xl mx-auto">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-6 sm:mb-8">
        <div>
          <h1 className="heading-xl">Paper Trading</h1>
          <p className="text-slate-400 mt-1 text-sm">Test your strategy with virtual money</p>
        </div>
        <StatusBadge
          status={isRunning ? 'running' : 'stopped'}
          label={isRunning ? `Paper trading (${botStatus.strategy})` : 'Stopped'}
        />
      </div>

      {/* Signal Context from Opportunities — full indicator panel */}
      {fromOpp && hasSignalData && oppPair && (
        <SignalContextPanel
          pair={oppPair}
          strategy={oppStrategy}
          timeframe={oppTimeframe}
          score={oppScore}
          action={oppAction}
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

      {/* Simple banner if no indicator data (e.g. navigated without full params) */}
      {fromOpp && !hasSignalData && (
        <div className={`mb-6 p-4 rounded-xl border flex items-start gap-3 ${
          oppAction === 'sell'
            ? 'border-red-500/40 bg-red-500/10'
            : 'border-brand-500/40 bg-brand-500/10'
        }`}>
          <span className="text-2xl">{oppAction === 'sell' ? '📉' : '🎯'}</span>
          <div className="flex-1">
            <p className={`font-semibold text-sm ${oppAction === 'sell' ? 'text-red-300' : 'text-brand-300'}`}>
              {oppAction === 'sell' ? 'Sell Signal' : 'Pre-filled from Opportunities'}
            </p>
            <p className="text-slate-400 text-xs mt-1">
              <span className="text-white font-medium">{oppPair}</span>
              {oppStrategy && <> · <span className="text-white font-medium">{oppStrategy}</span></>}
              {oppScore && <span className="ml-2 text-emerald-400 font-medium">Score: {oppScore}</span>}
            </p>
          </div>
          <a href="/opportunities" className="text-xs text-slate-400 hover:text-white underline shrink-0">← Back</a>
        </div>
      )}

      {/* Controls */}
      <div className="card mb-8">
        <div className="grid grid-cols-2 md:grid-cols-5 gap-4 mb-4">
          <div>
            <label className="label">Strategy</label>
            <select className="input" value={strategyId || ''} onChange={(e) => setStrategyId(Number(e.target.value))} disabled={isRunning}>
              {strategies.map((s) => (
                <option key={String(s.id)} value={String(s.id)}>{String(s.name)}</option>
              ))}
            </select>
          </div>
          <div className="md:col-span-2">
            <label className="label">Pairs</label>
            <PairPicker value={pairs} onChange={setPairs} disabled={isRunning} />
          </div>
          <div>
            <label className="label">Timeframe</label>
            <select className="input" value={timeframe} onChange={(e) => setTimeframe(e.target.value)} disabled={isRunning}>
              {['5m', '15m', '30m', '1h', '4h'].map((tf) => (
                <option key={tf} value={tf}>{tf}</option>
              ))}
            </select>
          </div>
          <div>
            <label className="label">Wallet (USDT)</label>
            <input type="number" className="input" value={wallet} onChange={(e) => setWallet(Number(e.target.value))} disabled={isRunning} />
          </div>
          <div>
            <label className="label">Stop-Loss: {stoploss}%</label>
            <input type="range" min={1} max={15} step={0.5} value={stoploss} onChange={(e) => setStoploss(Number(e.target.value))} className="w-full accent-red-500 mt-2" disabled={isRunning} />
          </div>
          <div>
            <label className="label">Take-Profit: {takeProfit === 0 ? 'OFF' : `${takeProfit}%`}</label>
            <input type="range" min={0} max={20} step={0.5} value={takeProfit} onChange={(e) => setTakeProfit(Number(e.target.value))} className="w-full accent-emerald-500 mt-2" disabled={isRunning} />
          </div>
        </div>

        {/* SL/TP quick summary */}
        {!isRunning && (stoploss > 0 || takeProfit > 0) && (
          <div className="flex gap-3 mb-4 text-xs">
            <span className="px-2 py-1 rounded bg-red-500/10 border border-red-500/20 text-red-400">
              🛑 Stop-Loss: -{stoploss}%
            </span>
            {takeProfit > 0 && (
              <span className="px-2 py-1 rounded bg-emerald-500/10 border border-emerald-500/20 text-emerald-400">
                🎯 Take-Profit: +{takeProfit}%
              </span>
            )}
            {takeProfit === 0 && (
              <span className="px-2 py-1 rounded bg-slate-500/10 border border-slate-500/20 text-slate-400">
                🎯 Take-Profit: disabled
              </span>
            )}
          </div>
        )}

        <div className="flex gap-3">
          {!isRunning ? (
            <button onClick={startPaper} disabled={starting || !strategyId} className="btn-success">
              {starting ? 'Starting...' : '▶ Start Paper Trading'}
            </button>
          ) : (
            <button onClick={stopPaper} className="btn-danger">
              ■ Stop Bot
            </button>
          )}
        </div>

        {error && <p className="text-red-400 text-sm mt-3">⚠️ {error}</p>}
      </div>

      {/* ── Quick Backtest Panel ─────────────────────────────────────── */}
      <div className="card mb-8">
        <div className="flex items-center justify-between mb-3">
          <div>
            <h2 className="font-semibold flex items-center gap-2">
              📊 Quick Backtest
              <span className="text-xs text-slate-400 font-normal">— See historical profitability before running the bot</span>
            </h2>
          </div>
          <div className="flex items-center gap-2">
            <select
              className="input py-1 text-xs w-24"
              value={qbDays}
              onChange={(e) => setQbDays(Number(e.target.value))}
              disabled={qbRunning}
            >
              <option value={30}>1M</option>
              <option value={90}>3M</option>
              <option value={180}>6M</option>
              <option value={365}>1Y</option>
              <option value={730}>2Y</option>
            </select>
            <button
              onClick={runQuickBacktest}
              disabled={qbRunning || !strategyId}
              className="btn-secondary text-xs"
            >
              {qbRunning ? '⏳ Running...' : '▶ Run'}
            </button>
          </div>
        </div>

        {!qbResult && !qbRunning && !qbError && (
          <p className="text-slate-500 text-xs">
            Run a quick historical backtest with your current settings ({pairs.slice(0,3).join(', ')}, {timeframe}, SL {stoploss}%) to validate profitability before going live.
          </p>
        )}

        {qbRunning && (
          <div className="flex items-center gap-3 py-4">
            <div className="w-4 h-4 border-2 border-brand-400 border-t-transparent rounded-full animate-spin" />
            <p className="text-slate-400 text-sm">Downloading historical data and running backtest…</p>
          </div>
        )}

        {qbError && (
          <div className="p-3 rounded-lg bg-red-500/10 border border-red-500/20 text-red-400 text-xs">
            ⚠️ {qbError}
          </div>
        )}

        {qbResult && (() => {
          const m = (qbResult.metrics as Record<string, unknown>) || {};
          const trades = (qbResult.trades as Record<string, unknown>[]) || [];
          const totalReturn = Number(m.total_profit_pct || 0) * 100;
          const numTrades = Number(m.total_trades || trades.length);
          const wins = trades.filter((t) => Number(t.profit_abs) > 0).length;
          const wr = numTrades > 0 ? (wins / numTrades) * 100 : 0;
          const grossProfit = trades.filter((t) => Number(t.profit_abs) > 0).reduce((s, t) => s + Number(t.profit_abs), 0);
          const grossLoss = Math.abs(trades.filter((t) => Number(t.profit_abs) < 0).reduce((s, t) => s + Number(t.profit_abs), 0));
          const pf = grossLoss > 0 ? grossProfit / grossLoss : grossProfit > 0 ? Infinity : 0;

          // Build equity curve
          const curve = trades.reduce(
            (acc: { i: number; eq: number }[], t, idx) => {
              const prev = acc.length > 0 ? acc[acc.length - 1].eq : wallet;
              acc.push({ i: idx + 1, eq: +(prev + Number(t.profit_abs || 0)).toFixed(2) });
              return acc;
            },
            [{ i: 0, eq: wallet }]
          );

          const isProfit = totalReturn >= 0;

          return (
            <div>
              {/* Summary pills */}
              <div className="flex flex-wrap gap-3 mb-4">
                <div className={`px-3 py-2 rounded-lg border text-center min-w-[90px] ${isProfit ? 'bg-emerald-500/10 border-emerald-500/30' : 'bg-red-500/10 border-red-500/30'}`}>
                  <p className="text-xs text-slate-400">Total Return</p>
                  <p className={`text-lg font-bold ${isProfit ? 'text-emerald-400' : 'text-red-400'}`}>
                    {isProfit ? '+' : ''}{totalReturn.toFixed(1)}%
                  </p>
                </div>
                <div className="px-3 py-2 rounded-lg border border-[#2a3a52] text-center min-w-[90px]">
                  <p className="text-xs text-slate-400">Win Rate</p>
                  <p className={`text-lg font-bold ${wr >= 50 ? 'text-emerald-400' : 'text-yellow-400'}`}>{wr.toFixed(0)}%</p>
                </div>
                <div className="px-3 py-2 rounded-lg border border-[#2a3a52] text-center min-w-[90px]">
                  <p className="text-xs text-slate-400">Profit Factor</p>
                  <p className={`text-lg font-bold ${pf >= 1 ? 'text-emerald-400' : 'text-red-400'}`}>
                    {isFinite(pf) ? pf.toFixed(2) : '∞'}
                  </p>
                </div>
                <div className="px-3 py-2 rounded-lg border border-[#2a3a52] text-center min-w-[90px]">
                  <p className="text-xs text-slate-400">Trades</p>
                  <p className="text-lg font-bold text-white">{numTrades}</p>
                </div>
                <div className={`px-3 py-2 rounded-lg border text-center min-w-[110px] flex-1 ${isProfit ? 'bg-emerald-500/10 border-emerald-500/30' : 'bg-red-500/10 border-red-500/30'}`}>
                  <p className="text-xs text-slate-400">Final Balance</p>
                  <p className={`text-lg font-bold ${isProfit ? 'text-emerald-400' : 'text-red-400'}`}>
                    {curve[curve.length - 1]?.eq.toFixed(2)} USDT
                  </p>
                </div>
              </div>

              {/* Verdict */}
              <div className={`mb-4 px-3 py-2 rounded-lg text-xs flex items-center gap-2 ${
                isProfit && pf >= 1.2
                  ? 'bg-emerald-500/10 border border-emerald-500/30 text-emerald-300'
                  : isProfit
                  ? 'bg-yellow-500/10 border border-yellow-500/30 text-yellow-300'
                  : 'bg-red-500/10 border border-red-500/30 text-red-300'
              }`}>
                <span className="text-base">
                  {isProfit && pf >= 1.2 ? '✅' : isProfit ? '⚠️' : '❌'}
                </span>
                <span>
                  {isProfit && pf >= 1.2
                    ? `Looks promising! ${totalReturn.toFixed(1)}% return with profit factor ${pf.toFixed(2)}. Consider starting paper trading.`
                    : isProfit
                    ? `Marginally profitable. Returns are positive but profit factor is low (${pf.toFixed(2)}). Consider refining the strategy.`
                    : `Strategy was unprofitable over this period (${totalReturn.toFixed(1)}%). Try adjusting stop-loss, switching pairs, or a different time range.`
                  }
                </span>
              </div>

              {/* Equity curve */}
              {curve.length > 2 && (
                <div className="h-36">
                  <ResponsiveContainer width="100%" height="100%">
                    <AreaChart data={curve} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
                      <defs>
                        <linearGradient id="qbGrad" x1="0" y1="0" x2="0" y2="1">
                          <stop offset="5%" stopColor={isProfit ? '#10b981' : '#ef4444'} stopOpacity={0.3} />
                          <stop offset="95%" stopColor={isProfit ? '#10b981' : '#ef4444'} stopOpacity={0} />
                        </linearGradient>
                      </defs>
                      <CartesianGrid strokeDasharray="3 3" stroke="#2a3a52" />
                      <XAxis dataKey="i" tick={{ fill: '#64748b', fontSize: 10 }} label={{ value: 'Trade #', position: 'insideBottom', fill: '#64748b', fontSize: 10 }} />
                      <YAxis tick={{ fill: '#64748b', fontSize: 10 }} width={60} />
                      <Tooltip
                        contentStyle={{ background: '#1a2236', border: '1px solid #2a3a52', borderRadius: 8, fontSize: 12 }}
                        formatter={(v: number) => [`${v.toFixed(2)} USDT`, 'Balance']}
                        labelFormatter={(l) => `Trade #${l}`}
                      />
                      <Area
                        type="monotone"
                        dataKey="eq"
                        stroke={isProfit ? '#10b981' : '#ef4444'}
                        strokeWidth={2}
                        fill="url(#qbGrad)"
                        dot={false}
                      />
                    </AreaChart>
                  </ResponsiveContainer>
                </div>
              )}
            </div>
          );
        })()}
      </div>

      {/* Metrics */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-4 mb-8">
        <MetricCard title="Virtual Balance" value={`${wallet.toFixed(2)} USDT`} />
        <MetricCard
          title="Realized P&L"
          value={`${totalPnl >= 0 ? '+' : ''}${totalPnl.toFixed(2)} USDT`}
          color={totalPnl >= 0 ? 'profit' : 'loss'}
        />
        <MetricCard
          title="Unrealized P&L"
          value={`${totalUnrealizedPnl >= 0 ? '+' : ''}${totalUnrealizedPnl.toFixed(2)} USDT`}
          color={totalUnrealizedPnl >= 0 ? 'profit' : 'loss'}
        />
        <MetricCard title="Open Positions" value={openTrades.length} />
        <MetricCard title="Win Rate" value={`${winRate.toFixed(1)}%`} />
      </div>

      {/* Strategy Signal Monitor — shows conditions, fire count, manual buy/sell */}
      {strategyId && (
        <StrategySignalMonitor
          strategyName={String(botStatus.strategy || strategies.find(s => s.id === strategyId)?.name || 'Strategy')}
          pair={pairs[0] || 'BTC/USDT'}
          timeframe={timeframe}
          isRunning={isRunning}
          onManualBuy={() => { refreshData(); }}
          onManualSell={() => { refreshData(); }}
        />
      )}

      {/* Live Chart + Signals */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 mb-8">
        <div className="lg:col-span-2 card p-0 overflow-hidden">
          <div className="flex items-center justify-between px-4 py-3 border-b border-[#2a3a52]">
            <h2 className="font-semibold">Live Chart — {(pairs[0] || 'BTC/USDT')}</h2>
            <span className="text-xs text-slate-400">{timeframe} · TradingView</span>
          </div>
          <TradingViewWidget
            symbol={pairToTV(pairs[0] || 'BTC/USDT')}
            interval={INTERVAL_TO_TV[timeframe] || '15'}
          />
        </div>
        <SignalsPanel pair={(pairs[0] || 'BTC/USDT')} interval={timeframe} compact={false} />
      </div>

      {/* Open Positions */}
      <div className="card mb-8">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold">Open Positions</h2>
          {openTrades.length > 0 && (
            <span className={`text-sm font-mono font-semibold ${totalUnrealizedPnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
              Unrealized: {totalUnrealizedPnl >= 0 ? '+' : ''}{totalUnrealizedPnl.toFixed(2)} USDT
            </span>
          )}
        </div>
        {openTrades.length === 0 ? (
          <p className="text-slate-500 text-sm">No open positions</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-slate-400 border-b border-[#2a3a52]">
                  <th className="text-left py-3 px-2">Pair</th>
                  <th className="text-right py-3 px-2">Entry Price</th>
                  <th className="text-right py-3 px-2">Current Price</th>
                  <th className="text-right py-3 px-2">Amount</th>
                  <th className="text-right py-3 px-2">Stop Loss</th>
                  <th className="text-right py-3 px-2">Unreal. P&L</th>
                  <th className="text-left py-3 px-2">Duration</th>
                  <th className="text-right py-3 px-2">Action</th>
                </tr>
              </thead>
              <tbody>
                {openTrades.map((t) => {
                  const unrealized = getUnrealizedPnl(t);
                  const currentP = currentPrices[String(t.pair)] || 0;
                  return (
                    <tr key={String(t.id)} className="border-b border-[#2a3a52]/50 hover:bg-[#2a3a52]/10">
                      <td className="py-3 px-2 font-medium">{String(t.pair)}</td>
                      <td className="py-3 px-2 text-right font-mono">{Number(t.entry_price).toFixed(4)}</td>
                      <td className="py-3 px-2 text-right font-mono">
                        {currentP > 0 ? (
                          <span className={currentP >= Number(t.entry_price) ? 'text-emerald-400' : 'text-red-400'}>
                            {currentP.toFixed(4)}
                          </span>
                        ) : <span className="text-slate-500">—</span>}
                      </td>
                      <td className="py-3 px-2 text-right font-mono">{Number(t.amount).toFixed(6)}</td>
                      <td className="py-3 px-2 text-right text-red-400 font-mono">{Number(t.stoploss_price).toFixed(4)}</td>
                      <td className={`py-3 px-2 text-right font-mono font-semibold ${unrealized >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                        {unrealized >= 0 ? '+' : ''}{unrealized.toFixed(2)}
                      </td>
                      <td className="py-3 px-2 text-slate-400 text-xs">{formatDuration(String(t.entry_time))}</td>
                      <td className="py-3 px-2 text-right">
                        {closingId === String(t.id) ? (
                          <div className="flex items-center gap-1 justify-end">
                            <span className="text-xs text-amber-400 mr-1">Sure?</span>
                            <button
                              onClick={() => forceClose(String(t.id))}
                              className="px-2 py-1 rounded-lg text-xs font-semibold bg-red-500/30 border border-red-500/50 text-red-300 hover:bg-red-500/50 transition-colors"
                            >
                              ✓ Yes
                            </button>
                            <button
                              onClick={() => setClosingId(null)}
                              className="px-2 py-1 rounded-lg text-xs font-semibold bg-slate-700/40 border border-slate-600/40 text-slate-400 hover:text-white transition-colors"
                            >
                              ✕
                            </button>
                          </div>
                        ) : (
                          <button
                            onClick={() => forceClose(String(t.id))}
                            className="px-3 py-1 rounded-lg text-xs font-semibold bg-red-500/20 border border-red-500/30 text-red-400 hover:bg-red-500/30 transition-colors"
                          >
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
        {closeError && (
          <div className="mt-3 p-2 rounded-lg bg-red-500/15 border border-red-500/30 text-red-400 text-xs">
            ❌ Close failed: {closeError}
          </div>
        )}
      </div>

      {/* Trade Log */}
      <div className="card">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold">Trade Log</h2>
          {tradeHistory.length > 0 && (
            <span className="text-xs text-slate-500">{tradeHistory.length} trades</span>
          )}
        </div>
        {tradeHistory.length === 0 ? (
          <p className="text-slate-500 text-sm">No trades yet</p>
        ) : (
          <div className="overflow-x-auto max-h-[400px] overflow-y-auto">
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-[#1a2236]">
                <tr className="text-slate-400 border-b border-[#2a3a52]">
                  <th className="text-left py-3 px-2">Pair</th>
                  <th className="text-right py-3 px-2">Entry</th>
                  <th className="text-right py-3 px-2">Exit</th>
                  <th className="text-right py-3 px-2">Profit %</th>
                  <th className="text-right py-3 px-2">Profit USDT</th>
                  <th className="text-left py-3 px-2">Reason</th>
                  <th className="text-left py-3 px-2">Duration</th>
                </tr>
              </thead>
              <tbody>
                {tradeHistory.map((t) => (
                  <tr key={String(t.id)} className="border-b border-[#2a3a52]/50 hover:bg-[#2a3a52]/20">
                    <td className="py-2 px-2">{String(t.pair)}</td>
                    <td className="py-2 px-2 text-right font-mono">{Number(t.entry_price).toFixed(4)}</td>
                    <td className="py-2 px-2 text-right font-mono">{Number(t.exit_price).toFixed(4)}</td>
                    <td className={`py-2 px-2 text-right font-semibold ${Number(t.profit_pct) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                      {Number(t.profit_pct) >= 0 ? '+' : ''}{Number(t.profit_pct || 0).toFixed(2)}%
                    </td>
                    <td className={`py-2 px-2 text-right font-semibold ${Number(t.profit_abs) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                      {Number(t.profit_abs || 0) >= 0 ? '+' : ''}{Number(t.profit_abs || 0).toFixed(2)}
                    </td>
                    <td className="py-2 px-2 text-slate-400 text-xs">{String(t.exit_reason || '')}</td>
                    <td className="py-2 px-2 text-slate-500 text-xs">
                      {t.entry_time && t.exit_time
                        ? formatDuration(String(t.entry_time))
                        : String(t.exit_time || '')}
                    </td>
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

export default function PaperTradePage() {
  return (
    <Suspense fallback={<div className="p-8 text-slate-400">Loading...</div>}>
      <PaperTradeInner />
    </Suspense>
  );
}
