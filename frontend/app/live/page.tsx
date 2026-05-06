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

/** Convert any "BASE/QUOTE" KuCoin pair to a TradingView symbol string. */
function pairToTV(pair: string): string {
  return `KUCOIN:${pair.replace('/', '')}`;
}

const INTERVAL_TO_TV: Record<string, string> = {
  '1m': '1', '5m': '5', '15m': '15', '30m': '30',
  '1h': '60', '4h': '240', '1d': 'D',
};

/** Format elapsed time since entry, e.g. "5m", "2h 15m", "1d 3h" */
function formatDuration(entryTime: string): string {
  const entry = new Date(entryTime).getTime();
  if (isNaN(entry)) return '—';
  const diffMs = Date.now() - entry;
  const totalMinutes = Math.floor(diffMs / 60000);
  if (totalMinutes < 60) return `${totalMinutes}m`;
  const hours = Math.floor(totalMinutes / 60);
  const mins = totalMinutes % 60;
  if (hours < 24) return mins > 0 ? `${hours}h ${mins}m` : `${hours}h`;
  const days = Math.floor(hours / 24);
  const remHours = hours % 24;
  return remHours > 0 ? `${days}d ${remHours}h` : `${days}d`;
}

function LiveTradingInner() {
  const searchParams = useSearchParams();
  const fromOpp = searchParams.get('pair') !== null;
  const oppPair   = searchParams.get('pair');
  const oppStrategy = searchParams.get('strategy');
  const oppTimeframe = searchParams.get('timeframe');
  const oppScore  = searchParams.get('score');
  const oppAction = searchParams.get('action') as 'buy' | 'sell' | null; // 'buy' | 'sell' | null

  // Signal context params
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
  const [stoploss, setStoploss] = useState(3);
  const [takeProfit, setTakeProfit] = useState(0);   // 0 = disabled, 1-20%
  const [confirmation, setConfirmation] = useState('');

  const [botStatus, setBotStatus] = useState<Record<string, unknown>>({ running: false });
  const [openTrades, setOpenTrades] = useState<Record<string, unknown>[]>([]);
  const [tradeHistory, setTradeHistory] = useState<Record<string, unknown>[]>([]);
  const [safetyCheck, setSafetyCheck] = useState<Record<string, unknown> | null>(null);
  const [starting, setStarting] = useState(false);
  const [showConfirmModal, setShowConfirmModal] = useState(false);
  const [error, setError] = useState('');

  // Live current prices for unrealized P&L
  const [currentPrices, setCurrentPrices] = useState<Record<string, number>>({});

  // Safety acknowledgment checkboxes — user must tick all before enabling Start
  const SAFETY_ITEMS = [
    { id: 'paper7', label: 'I have completed paper trading to test my strategy first' },
    { id: 'profitable', label: 'I understand my strategy may not be profitable and losses are possible' },
    { id: 'api', label: 'My KuCoin API key has Trade permissions (NOT Withdrawal)' },
    { id: 'stoploss', label: 'A stop-loss is configured to limit potential losses' },
    { id: 'realMoney', label: 'I understand this will use REAL money — losses are permanent' },
  ];
  const [acknowledged, setAcknowledged] = useState<Record<string, boolean>>({});
  const allAcknowledged = SAFETY_ITEMS.every((item) => acknowledged[item.id]);

  const refreshData = useCallback(async () => {
    try {
      const [status, open, history] = await Promise.all([
        api.trade.status(),
        api.trade.open(),
        api.trade.history({ mode: 'live', limit: '20' }),
      ]);
      setBotStatus(status);
      setOpenTrades(open.trades);
      setTradeHistory(history.trades);
    } catch {}
  }, []);

  // Fetch current prices for all open-trade pairs to compute unrealized P&L
  const refreshPrices = useCallback(async (trades: Record<string, unknown>[]) => {
    const uniquePairs = Array.from(new Set(trades.map((t) => String(t.pair))));
    if (uniquePairs.length === 0) return;
    const entries = await Promise.all(
      uniquePairs.map(async (p) => {
        try {
          const res = await api.market.price(p);
          return [p, Number(res.price)] as [string, number];
        } catch {
          return [p, 0] as [string, number];
        }
      })
    );
    setCurrentPrices(Object.fromEntries(entries));
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
    return () => { unsub(); tradeWS.disconnect(); clearInterval(interval); };
  }, [refreshData]);

  // Refresh prices whenever open trades change
  useEffect(() => {
    if (openTrades.length > 0) refreshPrices(openTrades);
  }, [openTrades, refreshPrices]);

  async function handleStartClick() {
    if (!allAcknowledged) return;
    setShowConfirmModal(true);
    setConfirmation('');
    setError('');
    setSafetyCheck(null);
  }

  async function startLive() {
    if (!strategyId || confirmation !== 'CONFIRM') return;
    setStarting(true);
    setError('');
    try {
      const params: Record<string, unknown> = {
        strategy_id: strategyId,
        mode: 'live',
        pairs,
        timeframe,
        stoploss: -(stoploss / 100),
        confirmation: 'CONFIRM',
        override_safety: true,
      };
      if (takeProfit > 0) params.take_profit = takeProfit / 100;
      const result = await api.trade.start(params);
      if (result.error) {
        setError(String(result.error));
        if (result.details) setSafetyCheck({ errors: result.details });
      } else {
        setShowConfirmModal(false);
      }
      await refreshData();
    } catch (e) {
      setError(String(e));
    }
    setStarting(false);
  }

  async function stopLive() {
    try {
      await api.trade.stop();
      await refreshData();
    } catch (e) {
      setError(String(e));
    }
  }

  async function emergencyStop() {
    if (confirm('EMERGENCY STOP: This will immediately halt ALL trading and close all positions. Are you sure?')) {
      try {
        await api.trade.emergencyStop();
        await refreshData();
      } catch (e) {
        alert(String(e));
      }
    }
  }

  function getUnrealizedPnl(t: Record<string, unknown>): number {
    const pair = String(t.pair);
    const entry = Number(t.entry_price) || 0;
    const amount = Number(t.amount) || 0;
    const cur = currentPrices[pair] || 0;
    if (!cur || !entry) return 0;
    return (cur - entry) * amount;
  }

  const totalPnl = tradeHistory.reduce((sum, t) => sum + (Number(t.profit_abs) || 0), 0);
  const totalUnrealizedPnl = openTrades.reduce((sum, t) => sum + getUnrealizedPnl(t), 0);
  const winRate =
    tradeHistory.length > 0
      ? (tradeHistory.filter((t) => Number(t.profit_abs) > 0).length / tradeHistory.length) * 100
      : 0;
  const isRunning = Boolean(botStatus.running) && botStatus.mode === 'live';

  return (
    <div className="max-w-6xl mx-auto">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-6 sm:mb-8">
        <div>
          <h1 className="heading-xl">Live Trading</h1>
          <p className="text-slate-400 mt-1 text-sm">Trade with real money on KuCoin</p>
        </div>
        <div className="flex items-center gap-3 sm:gap-4 flex-wrap">
          <StatusBadge
            status={isRunning ? 'running' : 'stopped'}
            label={isRunning ? 'LIVE' : 'Stopped'}
          />
          {isRunning && (
            <button onClick={emergencyStop} className="btn-danger animate-pulse">
              🛑 EMERGENCY STOP
            </button>
          )}
        </div>
      </div>

      {/* Signal Context Panel — full indicator snapshot from Opportunities */}
      {fromOpp && hasSignalData && oppPair && (
        <SignalContextPanel
          pair={oppPair}
          strategy={oppStrategy}
          timeframe={oppTimeframe}
          score={oppScore}
          action={oppAction || 'buy'}
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

      {/* Simple banner when no indicator data */}
      {fromOpp && !hasSignalData && (
        <div className={`mb-6 p-4 rounded-xl border flex items-start gap-3 ${
          oppAction === 'sell' ? 'border-red-500/50 bg-red-500/10' : 'border-red-500/40 bg-red-500/10'
        }`}>
          <span className="text-2xl">{oppAction === 'sell' ? '📉' : '🎯'}</span>
          <div className="flex-1">
            <p className="text-red-300 font-semibold text-sm">
              {oppAction === 'sell' ? 'Sell Signal — LIVE MODE' : 'Pre-filled from Opportunities — LIVE MODE'}
            </p>
            <p className="text-slate-400 text-xs mt-1">
              <span className="text-white font-medium">{oppPair}</span>
              {oppScore && <span className="ml-2 text-emerald-400 font-medium">Score: {oppScore}</span>}
            </p>
            <p className="text-red-400/80 text-xs mt-1">⚠️ This will use REAL money. Verify all settings before starting.</p>
          </div>
          <a href="/opportunities" className="text-xs text-slate-400 hover:text-white underline shrink-0">← Back</a>
        </div>
      )}

      {/* Safety Checklist */}
      <div className={`card mb-8 border-2 transition-colors ${allAcknowledged ? 'border-emerald-500/40 bg-emerald-500/5' : 'border-yellow-500/30 bg-yellow-500/5'}`}>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-yellow-400 font-semibold flex items-center gap-2">
            ⚠️ Safety Checklist
          </h2>
          {allAcknowledged
            ? <span className="text-xs text-emerald-400 font-medium">✅ All acknowledged — Start is enabled</span>
            : <span className="text-xs text-yellow-400/70">Tick all boxes to enable Start Live Trading</span>
          }
        </div>
        <p className="text-slate-400 text-xs mb-4">
          Read and check each item to confirm you understand the risks of live trading with real money.
        </p>
        <div className="space-y-3">
          {SAFETY_ITEMS.map((item) => (
            <label
              key={item.id}
              className={`flex items-start gap-3 p-3 rounded-lg cursor-pointer transition-colors ${
                acknowledged[item.id]
                  ? 'bg-emerald-500/10 border border-emerald-500/30'
                  : 'bg-yellow-500/5 border border-yellow-500/20 hover:border-yellow-500/40'
              }`}
            >
              <input
                type="checkbox"
                checked={!!acknowledged[item.id]}
                onChange={(e) => setAcknowledged((prev) => ({ ...prev, [item.id]: e.target.checked }))}
                className="mt-0.5 w-4 h-4 rounded accent-emerald-500 cursor-pointer flex-shrink-0"
              />
              <span className={`text-sm ${acknowledged[item.id] ? 'text-emerald-300' : 'text-slate-300'}`}>
                {acknowledged[item.id] ? '✅ ' : '⚠️ '}{item.label}
              </span>
            </label>
          ))}
        </div>
        {!allAcknowledged && (
          <p className="text-yellow-400/70 text-xs mt-3">
            {SAFETY_ITEMS.filter((i) => !acknowledged[i.id]).length} item(s) remaining
          </p>
        )}
      </div>

      {/* Controls */}
      <div className="card mb-8">
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4">
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

          {/* Stop-Loss slider */}
          <div>
            <label className="label flex items-center justify-between">
              <span>Stop-Loss</span>
              <span className="text-red-400 font-semibold">{stoploss}%</span>
            </label>
            <input
              type="range" min={1} max={10} step={0.5} value={stoploss}
              onChange={(e) => setStoploss(Number(e.target.value))}
              className="w-full accent-red-500 mt-2" disabled={isRunning}
            />
          </div>

          {/* Take-Profit slider */}
          <div>
            <label className="label flex items-center justify-between">
              <span>Take-Profit</span>
              <span className={takeProfit > 0 ? 'text-emerald-400 font-semibold' : 'text-slate-500'}>
                {takeProfit > 0 ? `${takeProfit}%` : 'Disabled'}
              </span>
            </label>
            <input
              type="range" min={0} max={20} step={0.5} value={takeProfit}
              onChange={(e) => setTakeProfit(Number(e.target.value))}
              className="w-full accent-emerald-500 mt-2" disabled={isRunning}
            />
          </div>
        </div>

        {/* SL/TP quick summary */}
        {(stoploss > 0 || takeProfit > 0) && !isRunning && (
          <div className="flex gap-3 mb-4 flex-wrap">
            <span className="text-xs px-3 py-1 rounded-full bg-red-500/15 border border-red-500/30 text-red-400">
              🛡️ SL: -{stoploss}%
            </span>
            {takeProfit > 0 && (
              <span className="text-xs px-3 py-1 rounded-full bg-emerald-500/15 border border-emerald-500/30 text-emerald-400">
                🎯 TP: +{takeProfit}%
              </span>
            )}
          </div>
        )}

        {!isRunning ? (
          <div className="flex items-center gap-3 flex-wrap">
            <button
              onClick={handleStartClick}
              disabled={!strategyId || !allAcknowledged}
              className={`btn-danger ${!allAcknowledged ? 'opacity-40 cursor-not-allowed' : ''}`}
              title={!allAcknowledged ? 'Tick all safety checkboxes above first' : 'Start live trading'}
            >
              🔴 Start Live Trading
            </button>
            {!allAcknowledged && (
              <span className="text-yellow-400 text-xs">
                ← Tick all safety checkboxes above to enable
              </span>
            )}
          </div>
        ) : (
          <button onClick={stopLive} className="btn-secondary">
            Stop Live Trading
          </button>
        )}

        {error && <p className="text-red-400 text-sm mt-3">{error}</p>}
        {safetyCheck && (safetyCheck.errors as string[])?.length > 0 && (
          <div className="mt-3 p-3 rounded-lg bg-amber-500/10 border border-amber-500/30">
            <p className="text-amber-400 font-medium text-sm">⚠️ Safety warnings (proceeding anyway since you acknowledged):</p>
            <ul className="list-disc list-inside text-amber-300 text-xs mt-1">
              {(safetyCheck.errors as string[]).map((e, i) => <li key={i}>{e}</li>)}
            </ul>
          </div>
        )}
      </div>

      {/* Confirmation Modal */}
      {showConfirmModal && (
        <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50">
          <div className="card max-w-md w-full mx-4">
            <h2 className="text-xl font-bold text-red-400 mb-4">Confirm Live Trading</h2>
            <p className="text-slate-300 mb-2">
              This will use <strong>REAL MONEY</strong> from your KuCoin account.
            </p>
            <p className="text-slate-400 text-sm mb-2">
              Make sure you understand the risks. Losses are real and permanent.
            </p>
            {/* SL/TP reminder */}
            <div className="flex gap-2 mb-4 flex-wrap">
              <span className="text-xs px-2 py-0.5 rounded bg-red-500/15 border border-red-500/30 text-red-400">
                SL: -{stoploss}%
              </span>
              {takeProfit > 0 ? (
                <span className="text-xs px-2 py-0.5 rounded bg-emerald-500/15 border border-emerald-500/30 text-emerald-400">
                  TP: +{takeProfit}%
                </span>
              ) : (
                <span className="text-xs px-2 py-0.5 rounded bg-slate-500/20 border border-slate-500/30 text-slate-400">
                  TP: Disabled
                </span>
              )}
            </div>
            <div className="mb-4">
              <label className="label">Type &quot;CONFIRM&quot; to proceed</label>
              <input
                className="input"
                value={confirmation}
                onChange={(e) => setConfirmation(e.target.value)}
                placeholder="Type CONFIRM"
                autoFocus
              />
            </div>
            <div className="flex gap-3">
              <button onClick={() => setShowConfirmModal(false)} className="btn-secondary flex-1">Cancel</button>
              <button onClick={startLive} disabled={confirmation !== 'CONFIRM' || starting} className="btn-danger flex-1">
                {starting ? 'Starting...' : '🔴 Start Live Trading'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Metrics — 5 cards */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-4 mb-8">
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
        <MetricCard title="Open Trades" value={openTrades.length} />
        <MetricCard title="Win Rate" value={`${winRate.toFixed(1)}%`} />
        <MetricCard title="Total Trades" value={tradeHistory.length} />
      </div>

      {/* Strategy Signal Monitor */}
      {strategyId && (
        <StrategySignalMonitor
          strategyName={String(botStatus.strategy || strategies.find((s: {id: number; name: string}) => s.id === strategyId)?.name || 'Strategy')}
          pair={pairs[0] || 'BTC/USDT'}
          timeframe={timeframe}
          isRunning={isRunning}
          onManualBuy={() => refreshData()}
          onManualSell={() => refreshData()}
        />
      )}

      {/* Live Chart + Signals */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 mb-8">
        <div className="lg:col-span-2 card p-0 overflow-hidden">
          <div className="flex items-center justify-between px-4 py-3 border-b border-[#2a3a52]">
            <h2 className="font-semibold">Live Chart — {(pairs[0] || 'BTC/USDT')}</h2>
            <span className="text-xs px-2 py-1 rounded bg-red-500/20 text-red-400">LIVE MODE · {timeframe}</span>
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
            <span className={`text-sm font-medium ${totalUnrealizedPnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
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
                  <th className="text-right py-3 px-2">Unrealized P&L</th>
                  <th className="text-right py-3 px-2">Duration</th>
                  <th className="text-right py-3 px-2">Action</th>
                </tr>
              </thead>
              <tbody>
                {openTrades.map((t) => {
                  const pair = String(t.pair);
                  const entryPrice = Number(t.entry_price);
                  const curPrice = currentPrices[pair] || 0;
                  const unrealized = getUnrealizedPnl(t);
                  const isProfit = unrealized >= 0;
                  return (
                    <tr key={String(t.id)} className="border-b border-[#2a3a52]/50 hover:bg-[#2a3a52]/20">
                      <td className="py-3 px-2 font-medium">{pair}</td>
                      <td className="py-3 px-2 text-right">{entryPrice.toFixed(4)}</td>
                      <td className={`py-3 px-2 text-right font-medium ${curPrice >= entryPrice ? 'text-emerald-400' : 'text-red-400'}`}>
                        {curPrice > 0 ? curPrice.toFixed(4) : '—'}
                      </td>
                      <td className="py-3 px-2 text-right">{Number(t.amount).toFixed(6)}</td>
                      <td className="py-3 px-2 text-right text-red-400">{Number(t.stoploss_price).toFixed(4)}</td>
                      <td className={`py-3 px-2 text-right font-medium ${isProfit ? 'text-emerald-400' : 'text-red-400'}`}>
                        {isProfit ? '+' : ''}{unrealized.toFixed(2)} USDT
                      </td>
                      <td className="py-3 px-2 text-right text-slate-400 text-xs">
                        {formatDuration(String(t.entry_time))}
                      </td>
                      <td className="py-3 px-2 text-right">
                        <button
                          onClick={async () => {
                            if (confirm(`Force close ${pair} position? This will sell at market price using REAL money.`)) {
                              await api.trade.forceClose(Number(t.id));
                              refreshData();
                            }
                          }}
                          className="text-xs px-2 py-1 rounded bg-red-500/15 border border-red-500/30 text-red-400 hover:bg-red-500/25 transition-colors"
                        >
                          📉 Force Close
                        </button>
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
        <h2 className="text-lg font-semibold mb-4">Trade Log</h2>
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
                  <th className="text-right py-3 px-2">Duration</th>
                  <th className="text-right py-3 px-2">Profit %</th>
                  <th className="text-right py-3 px-2">Profit USDT</th>
                  <th className="text-left py-3 px-2">Reason</th>
                </tr>
              </thead>
              <tbody>
                {tradeHistory.map((t) => {
                  const profitPct = Number(t.profit_pct || 0);
                  const profitAbs = Number(t.profit_abs || 0);
                  const isProfit = profitAbs >= 0;
                  const entryTime = String(t.entry_time || '');
                  const exitTime = String(t.exit_time || '');
                  let duration = '—';
                  if (entryTime && exitTime) {
                    const diff = new Date(exitTime).getTime() - new Date(entryTime).getTime();
                    if (!isNaN(diff) && diff > 0) {
                      const totalMins = Math.floor(diff / 60000);
                      if (totalMins < 60) duration = `${totalMins}m`;
                      else {
                        const h = Math.floor(totalMins / 60);
                        const m = totalMins % 60;
                        duration = m > 0 ? `${h}h ${m}m` : `${h}h`;
                      }
                    }
                  }
                  return (
                    <tr key={String(t.id)} className="border-b border-[#2a3a52]/50 hover:bg-[#2a3a52]/20">
                      <td className="py-2 px-2 font-medium">{String(t.pair)}</td>
                      <td className="py-2 px-2 text-right">{Number(t.entry_price).toFixed(4)}</td>
                      <td className="py-2 px-2 text-right">{Number(t.exit_price).toFixed(4)}</td>
                      <td className="py-2 px-2 text-right text-slate-400 text-xs">{duration}</td>
                      <td className={`py-2 px-2 text-right font-medium ${isProfit ? 'text-emerald-400' : 'text-red-400'}`}>
                        {profitPct >= 0 ? '+' : ''}{profitPct.toFixed(2)}%
                      </td>
                      <td className={`py-2 px-2 text-right font-medium ${isProfit ? 'text-emerald-400' : 'text-red-400'}`}>
                        {profitAbs >= 0 ? '+' : ''}{profitAbs.toFixed(2)}
                      </td>
                      <td className="py-2 px-2 text-slate-400 text-xs">
                        <span className={`px-2 py-0.5 rounded-full text-xs ${
                          String(t.exit_reason).includes('stop') ? 'bg-red-500/15 text-red-400' :
                          String(t.exit_reason).includes('roi') ? 'bg-emerald-500/15 text-emerald-400' :
                          'bg-slate-500/20 text-slate-400'
                        }`}>
                          {String(t.exit_reason || '—')}
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
    </div>
  );
}

export default function LiveTradingPage() {
  return (
    <Suspense fallback={<div className="p-8 text-slate-400">Loading...</div>}>
      <LiveTradingInner />
    </Suspense>
  );
}
