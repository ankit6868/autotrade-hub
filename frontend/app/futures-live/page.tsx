'use client';
import { useEffect, useState, useCallback, Suspense } from 'react';
import { api } from '@/lib/api';
import StrategyChart from '@/components/dashboard/StrategyChart';
import StrategySignalMonitor from '@/components/dashboard/StrategySignalMonitor';
import PairPicker from '@/components/ui/PairPicker';

// Safety acknowledgments — all must be ticked before live trading is enabled
const SAFETY_ITEMS = [
  { id: 'leverage',     label: 'I understand leverage amplifies BOTH profits AND losses' },
  { id: 'liquidation',  label: 'I understand positions can be liquidated if market moves against me' },
  { id: 'risk',         label: 'I only trade with funds I can afford to lose completely' },
  { id: 'api',          label: 'My KuCoin Futures API key is configured in Setup' },
];

function FuturesLiveInner() {
  // ── Config state ─────────────────────────────────────────────────────────
  const [strategies, setStrategies]     = useState<any[]>([]);
  const [strategyId, setStrategyId]     = useState<number | null>(null);
  const [pairs, setPairs]               = useState(['BTC/USDT']);
  const [timeframe, setTimeframe]       = useState('15m');
  const [leverage, setLeverage]         = useState(5);
  const [stoploss, setStoploss]         = useState(1.5);  // SL < TP for favorable R:R
  const [takeProfit, setTakeProfit]     = useState(3.0);  // 2:1 TP:SL minimum for live futures
  const [maxPositionPct, setMaxPositionPct] = useState(5); // % of balance per trade (conservative for live)
  const [acknowledged, setAcknowledged] = useState<Record<string, boolean>>({});
  const allAcknowledged = SAFETY_ITEMS.every(i => acknowledged[i.id]);

  // ── Bot / data state ──────────────────────────────────────────────────────
  const [botStatus, setBotStatus]       = useState<any>({ running: false });
  const [openTrades, setOpenTrades]     = useState<any[]>([]);
  const [tradeHistory, setTradeHistory] = useState<any[]>([]);
  const [starting, setStarting]         = useState(false);
  const [closingId, setClosingId]       = useState<string | null>(null);
  const [error, setError]               = useState('');

  // ── KuCoin Futures account balance (real money) ───────────────────────────
  const [liveBalance, setLiveBalance]   = useState<any>(null);
  const MIN_BALANCE = 10; // minimum USDT to start live futures trading

  const refreshBalance = useCallback(async () => {
    try {
      const b = await api.futures.balance();
      setLiveBalance(b);
    } catch (e) {
      // Show error in UI instead of silently swallowing it
      setLiveBalance({ error: 'Could not fetch balance — check API key in Setup', balance: 0 });
    }
  }, []);

  // ── Data refresh (polls every 10 s) ──────────────────────────────────────
  const refreshData = useCallback(async () => {
    try {
      const [status, open, history] = await Promise.all([
        api.futures.status(),
        api.futures.open('live'),
        api.futures.history({ mode: 'live', limit: '20' }),
      ]);
      setBotStatus(status);
      setOpenTrades(open.trades ?? []);
      setTradeHistory(history.trades ?? []);
    } catch { /* silent — backend may be starting */ }
  }, []);

  // Auto-fill from strategy — never show 1x leverage on a live futures page
  useEffect(() => {
    if (!strategyId || strategies.length === 0) return;
    const s = strategies.find((x: any) => x.id === strategyId);
    if (!s) return;
    const sl  = Math.abs(Number(s.stoploss ?? -0.03) * 100);
    const tp  = Number(s.take_profit ?? 0.015) * 100;
    const lev = Number(s.default_leverage ?? 10);
    setStoploss(sl   > 0 ? sl  : 2);
    setTakeProfit(tp > 0 ? tp  : 1.5);
    setLeverage(lev  > 1 ? lev : 5);   // live: default to 5x if not set
    if (s.timeframe) setTimeframe(s.timeframe);
  }, [strategyId, strategies]);

  useEffect(() => {
    api.strategy.list()
      .then(d => {
        setStrategies(d.strategies ?? []);
        if (d.strategies?.length > 0) setStrategyId(Number(d.strategies[0].id));
      })
      .catch(() => {});
    refreshData();
    refreshBalance();
    const t  = setInterval(refreshData, 10_000);
    const tb = setInterval(refreshBalance, 30_000); // balance refreshes every 30 s
    return () => { clearInterval(t); clearInterval(tb); };
  }, [refreshData, refreshBalance]);

  // ── Derived: sufficient balance to trade ──────────────────────────────────
  const hasBalance     = liveBalance?.balance != null && liveBalance.balance >= MIN_BALANCE;
  const balanceError   = liveBalance?.error ?? null;
  const canStart       = allAcknowledged && !!strategyId && hasBalance;

  // ── Start / Stop ──────────────────────────────────────────────────────────
  async function startBot() {
    if (!canStart) return;
    setStarting(true);
    setError('');
    try {
      const r = await api.futures.start({
        strategy_id:      strategyId,
        mode:             'live',
        pairs,
        leverage,
        timeframe,
        stoploss:         -(stoploss / 100),
        take_profit_pct:  takeProfit,
        max_position_pct: maxPositionPct,
        // For live: pass actual KuCoin balance as wallet so position sizing is correct
        wallet: liveBalance?.balance ?? 1000,
      });
      if (r.error) setError(r.error);
      else refreshData();
    } catch (e) {
      setError(String(e));
    }
    setStarting(false);
  }

  async function stopBot() {
    try { await api.futures.stop(); refreshData(); } catch { /* ignore */ }
  }

  // ── Derived values ────────────────────────────────────────────────────────
  // isRunning is true ONLY when the engine is live (not paper)
  const isRunning = Boolean(botStatus?.running) && botStatus?.mode === 'live';
  const totalPnl  = tradeHistory.reduce((s: number, t: any) => s + (Number(t.profit_abs) || 0), 0);
  const wins      = tradeHistory.filter((t: any) => (t.profit_abs ?? 0) > 0).length;
  const winRate   = tradeHistory.length > 0 ? Math.round((wins / tradeHistory.length) * 100) : 0;
  const unrealTotal = openTrades.reduce((s: number, t: any) => s + (Number(t.unrealized_pnl) || 0), 0);

  return (
    <div className="max-w-6xl mx-auto">

      {/* ── Real-money warning ─────────────────────────────────────── */}
      <div className="mb-6 p-4 rounded-xl border border-red-500/50 bg-red-500/10 flex items-start gap-3">
        <span className="text-3xl">⚡</span>
        <div>
          <p className="font-bold text-red-400">LIVE FUTURES — REAL MONEY WITH LEVERAGE</p>
          <p className="text-red-300/70 text-xs mt-1">
            Leveraged futures can result in losses exceeding your initial margin. Positions can be
            liquidated. Never risk money you cannot afford to lose. Live trades execute on KuCoin
            with REAL funds.
          </p>
        </div>
      </div>

      {/* ── Header ─────────────────────────────────────────────────── */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="heading-xl">⚡ Futures Live Trading</h1>
          <p className="text-slate-400 text-sm mt-1">Real leveraged trading on KuCoin Futures</p>
        </div>
        <span className={`chip ${
          isRunning
            ? 'bg-red-500/10 border-red-500/30 text-red-300'
            : 'bg-slate-500/10 border-slate-500/30 text-slate-400'
        }`}>
          {isRunning
            ? `🔴 LIVE Futures (${botStatus.leverage ?? leverage}x)`
            : '⚫ Stopped'}
        </span>
      </div>

      {/* ── Locked banner ─────────────────────────────────────────── */}
      {isRunning && (
        <div className="mb-4 p-3 rounded-xl border border-amber-500/40 bg-amber-500/10 flex items-center gap-3 text-sm">
          <span className="text-xl">🔒</span>
          <span className="text-amber-300">
            Bot is running — <strong>all settings are locked</strong>.
            Stop the bot to change strategy, leverage, SL, or TP.
          </span>
        </div>
      )}

      {/* ── KuCoin Futures Account Balance ─────────────────────────── */}
      <div className={`card mb-6 border ${
        balanceError ? 'border-red-500/30 bg-red-500/5'
          : hasBalance ? 'border-emerald-500/30 bg-emerald-500/5'
          : 'border-amber-500/30 bg-amber-500/5'
      }`}>
        <div className="flex items-center justify-between">
          <div>
            <p className="text-xs text-slate-400 uppercase tracking-wider mb-1">
              💰 KuCoin Futures Account Balance
            </p>
            {balanceError ? (
              <p className="text-red-400 text-sm font-medium">{balanceError}</p>
            ) : liveBalance?.balance == null ? (
              <p className="text-amber-400 text-sm">Loading balance… (requires KuCoin API key in Setup)</p>
            ) : (
              <div className="flex items-center gap-4">
                <div>
                  <span className={`text-2xl font-bold font-mono ${hasBalance ? 'text-emerald-400' : 'text-red-400'}`}>
                    {liveBalance.balance.toFixed(4)} USDT
                  </span>
                  <span className="text-slate-400 text-xs ml-2">available</span>
                </div>
                {liveBalance.equity != null && liveBalance.equity !== liveBalance.balance && (
                  <div className="text-xs text-slate-400">
                    Equity: <span className="text-white font-mono">{liveBalance.equity.toFixed(4)} USDT</span>
                  </div>
                )}
                {liveBalance.unrealized != null && liveBalance.unrealized !== 0 && (
                  <div className="text-xs">
                    Unrealized:{' '}
                    <span className={`font-mono font-semibold ${liveBalance.unrealized >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                      {liveBalance.unrealized >= 0 ? '+' : ''}{liveBalance.unrealized.toFixed(4)} USDT
                    </span>
                  </div>
                )}
              </div>
            )}
          </div>
          <button
            onClick={refreshBalance}
            className="text-xs text-slate-400 hover:text-white border border-[#2a3a52] px-2 py-1 rounded-lg transition-colors"
          >
            ↻
          </button>
        </div>
        {!balanceError && liveBalance?.balance != null && !hasBalance && (
          <p className="text-amber-400 text-xs mt-2">
            ⚠ Minimum {MIN_BALANCE} USDT required to start live futures trading.
            Add funds to your KuCoin Futures account.
          </p>
        )}
      </div>

      {/* ── Safety checklist (hidden while running) ────────────────── */}
      {!isRunning && (
        <div className="card mb-6 border-red-500/20">
          <h3 className="font-semibold text-red-400 mb-3">⚠ Safety Acknowledgments — tick all to enable</h3>
          <div className="space-y-2">
            {SAFETY_ITEMS.map(item => (
              <label key={item.id} className="flex items-center gap-3 cursor-pointer">
                <input
                  type="checkbox"
                  checked={!!acknowledged[item.id]}
                  onChange={e => setAcknowledged(prev => ({ ...prev, [item.id]: e.target.checked }))}
                  className="w-4 h-4 accent-red-500"
                />
                <span className="text-sm text-slate-300">{item.label}</span>
              </label>
            ))}
          </div>
        </div>
      )}

      {/* ── Bot config ─────────────────────────────────────────────── */}
      <div className="card mb-6">
        <div className="grid grid-cols-2 md:grid-cols-5 gap-4 mb-4">
          <div>
            <label className="label">Strategy</label>
            <select
              className="input"
              value={strategyId ?? ''}
              onChange={e => setStrategyId(Number(e.target.value))}
              disabled={isRunning}
            >
              {strategies.map((s: any) => (
                <option key={s.id} value={s.id}>{s.name}</option>
              ))}
            </select>
          </div>

          <div className="md:col-span-2">
            <label className="label">Pairs</label>
            <PairPicker value={pairs} onChange={setPairs} disabled={isRunning} />
          </div>

          <div>
            <label className="label">Timeframe</label>
            <select
              className="input"
              value={timeframe}
              onChange={e => setTimeframe(e.target.value)}
              disabled={isRunning}
            >
              {['1m','5m','15m','30m','1h'].map(t => (
                <option key={t} value={t}>{t}</option>
              ))}
            </select>
          </div>

          <div>
            <label className="label">Leverage: {leverage}x</label>
            <input
              type="range" min={1} max={20} value={leverage}
              onChange={e => setLeverage(Number(e.target.value))}
              disabled={isRunning}
              className="w-full accent-blue-500 mt-2"
            />
            <p className="text-xs text-orange-400 mt-1">
              Liq. at ~{(100 / leverage).toFixed(1)}%
            </p>
          </div>
        </div>

        <div className="grid grid-cols-3 gap-4 mb-4">
          <div>
            <label className="label">Stop-Loss: {stoploss}%</label>
            <input
              type="range" min={0.5} max={5} step={0.5} value={stoploss}
              onChange={e => setStoploss(Number(e.target.value))}
              disabled={isRunning}
              className="w-full accent-red-500 mt-2"
            />
            <p className="text-xs text-orange-400 mt-1">⚠ Liq at ~{(100 / leverage).toFixed(1)}% move</p>
          </div>
          <div>
            <label className="label">
              Take-Profit: {takeProfit}% (leveraged: {(takeProfit * leverage).toFixed(1)}%)
            </label>
            <input
              type="range" min={0.1} max={5} step={0.1} value={takeProfit}
              onChange={e => setTakeProfit(Number(e.target.value))}
              disabled={isRunning}
              className="w-full accent-emerald-500 mt-2"
            />
          </div>
          <div>
            <label className="label">Position Size: {maxPositionPct}% per trade</label>
            <input
              type="range" min={1} max={20} step={1} value={maxPositionPct}
              onChange={e => setMaxPositionPct(Number(e.target.value))}
              disabled={isRunning}
              className="w-full accent-purple-500 mt-2"
            />
            <p className="text-xs text-slate-400 mt-1">
              ${(((liveBalance?.balance ?? 0) * maxPositionPct) / 100).toFixed(2)} USDT margin per trade
            </p>
          </div>
        </div>

        {error && <p className="text-red-400 text-xs mb-3">{error}</p>}

        <div className="flex gap-3">
          {isRunning ? (
            <button onClick={stopBot} className="btn-danger">■ Stop Bot</button>
          ) : (
            <div className="flex flex-col gap-2">
              <button
                onClick={startBot}
                disabled={starting || !canStart}
                className={`px-6 py-2.5 rounded-xl font-semibold text-sm border transition-all ${
                  canStart
                    ? 'bg-red-500/20 border-red-500/50 text-red-300 hover:bg-red-500/30'
                    : 'bg-slate-700/30 border-slate-600/30 text-slate-500 cursor-not-allowed'
                }`}
              >
                {starting ? 'Starting…' : '🔴 Start Live Futures'}
              </button>
              {!hasBalance && allAcknowledged && (
                <p className="text-amber-400 text-xs">
                  ⚠ Insufficient balance — add at least {MIN_BALANCE} USDT to KuCoin Futures
                </p>
              )}
            </div>
          )}
        </div>
      </div>

      {/* ── Stats ──────────────────────────────────────────────────── */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-4 mb-6">
        {[
          {
            label: 'Open Positions',
            value: openTrades.length,
            color: openTrades.length > 0 ? 'text-emerald-400' : 'text-white',
          },
          {
            label: 'Realized P&L',
            value: `${totalPnl >= 0 ? '+' : ''}${totalPnl.toFixed(4)} USDT`,
            color: totalPnl >= 0 ? 'text-emerald-400' : 'text-red-400',
          },
          {
            label: 'Unrealized P&L',
            value: `${unrealTotal >= 0 ? '+' : ''}${unrealTotal.toFixed(4)} USDT`,
            color: unrealTotal >= 0 ? 'text-emerald-400' : 'text-red-400',
          },
          { label: 'Total Trades', value: tradeHistory.length },
          {
            label: 'Win Rate',
            value: `${winRate}%`,
            color: winRate >= 50 ? 'text-emerald-400' : winRate > 0 ? 'text-amber-400' : 'text-white',
          },
        ].map(m => (
          <div key={m.label} className="card card-hover">
            <p className="text-xs text-slate-400 uppercase tracking-wider mb-1">{m.label}</p>
            <p className={`stat-lg ${m.color ?? 'text-white'} truncate`}>{m.value}</p>
          </div>
        ))}
      </div>

      {/* ── Strategy Signal Monitor ─────────────────────────────────── */}
      {strategyId && (
        <StrategySignalMonitor
          strategyName={strategies.find(s => s.id === strategyId)?.name ?? 'Strategy'}
          pair={pairs[0] ?? 'BTC/USDT'}
          timeframe={timeframe}
          isRunning={isRunning}
          isLive={true}
          isFutures={true}
          manualStakePct={maxPositionPct}
          manualLeverage={leverage}
        />
      )}

      {/* ── Analytics chart (futures / live isolated) ───────────────── */}
      <StrategyChart
        pair={pairs[0] ?? 'BTC/USDT'}
        timeframe={timeframe}
        mode="live"
        marketType="futures"
        height={420}
      />

      {/* ── Open Positions ──────────────────────────────────────────── */}
      <div className="card mb-6">
        <h2 className="text-lg font-semibold mb-4 flex items-center gap-2">
          Open Futures Positions
          {openTrades.length > 0 && (
            <span className="text-sm text-slate-400">
              Unrealized:{' '}
              <span className={unrealTotal >= 0 ? 'text-emerald-400' : 'text-red-400'}>
                {unrealTotal >= 0 ? '+' : ''}{unrealTotal.toFixed(4)} USDT
              </span>
            </span>
          )}
        </h2>

        {openTrades.length === 0 ? (
          <p className="text-slate-500 text-sm">No open live futures positions</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-slate-400 border-b border-[#2a3a52]">
                  <th className="text-left  py-2 px-2">Pair</th>
                  <th className="text-right py-2 px-2">Side</th>
                  <th className="text-right py-2 px-2">Lev</th>
                  <th className="text-right py-2 px-2">Entry</th>
                  <th className="text-right py-2 px-2">Current</th>
                  <th className="text-right py-2 px-2">Liq. Price</th>
                  <th className="text-right py-2 px-2">Margin</th>
                  <th className="text-right py-2 px-2">Unreal. P&L</th>
                  <th className="text-right py-2 px-2">Action</th>
                </tr>
              </thead>
              <tbody>
                {openTrades.map((t: any) => {
                  const unreal    = Number(t.unrealized_pnl) || 0;
                  const liqPrice  = Number(t.liquidation_price) || 0;
                  const curPrice  = Number(t.current_price)     || 0;
                  const dangerClose =
                    liqPrice > 0 && curPrice > 0 &&
                    (t.side === 'long'
                      ? curPrice < liqPrice * 1.05
                      : curPrice > liqPrice * 0.95);
                  return (
                    <tr
                      key={String(t.id)}
                      className={`border-b border-[#2a3a52]/50 hover:bg-[#2a3a52]/10 ${
                        dangerClose ? 'bg-red-500/5' : ''
                      }`}
                    >
                      <td className="py-2 px-2 font-medium">{t.pair}</td>
                      <td className={`py-2 px-2 text-right font-semibold ${
                        t.side === 'long' ? 'text-emerald-400' : 'text-red-400'
                      }`}>
                        {t.side?.toUpperCase()}
                      </td>
                      <td className="py-2 px-2 text-right text-blue-400 font-bold">
                        {t.leverage ?? leverage}x
                      </td>
                      <td className="py-2 px-2 text-right font-mono">
                        {Number(t.entry_price).toFixed(2)}
                      </td>
                      <td className="py-2 px-2 text-right font-mono">
                        {curPrice > 0 ? curPrice.toFixed(2) : '—'}
                      </td>
                      <td className={`py-2 px-2 text-right font-mono text-xs ${
                        dangerClose ? 'text-red-400 font-bold animate-pulse' : 'text-orange-400'
                      }`}>
                        {liqPrice > 0 ? liqPrice.toFixed(2) : '—'}
                        {dangerClose && ' ⚠'}
                      </td>
                      <td className="py-2 px-2 text-right font-mono">
                        {Number(t.amount).toFixed(2)}
                      </td>
                      <td className={`py-2 px-2 text-right font-mono font-semibold ${
                        unreal >= 0 ? 'text-emerald-400' : 'text-red-400'
                      }`}>
                        {unreal >= 0 ? '+' : ''}{unreal.toFixed(4)}
                      </td>
                      <td className="py-2 px-2 text-right">
                        {closingId === String(t.id) ? (
                          <button disabled className="text-xs px-2 py-1 rounded bg-slate-700/40 text-slate-400 opacity-70">
                            ⏳
                          </button>
                        ) : (
                          <button
                            onClick={async () => {
                              setClosingId(String(t.id));
                              try { await api.futures.forceClose(t.pair); refreshData(); }
                              catch { /* ignore */ }
                              finally { setClosingId(null); }
                            }}
                            className="text-xs px-2 py-1 rounded bg-red-500/20 border border-red-500/40 text-red-400 hover:bg-red-500/30 transition-colors"
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
      </div>

      {/* ── Trade Log ───────────────────────────────────────────────── */}
      <div className="card">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold">Trade Log</h2>
          {tradeHistory.length > 0 && (
            <span className="text-xs text-slate-500">{tradeHistory.length} trades</span>
          )}
        </div>

        {tradeHistory.length === 0 ? (
          <p className="text-slate-500 text-sm">No closed live futures trades yet</p>
        ) : (
          <div className="overflow-x-auto max-h-[400px] overflow-y-auto">
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-[#1a2236]">
                <tr className="text-slate-400 border-b border-[#2a3a52]">
                  <th className="text-left  py-2 px-2">Pair</th>
                  <th className="text-right py-2 px-2">Side</th>
                  <th className="text-right py-2 px-2">Lev</th>
                  <th className="text-right py-2 px-2">Entry</th>
                  <th className="text-right py-2 px-2">Exit</th>
                  <th className="text-right py-2 px-2">Profit%</th>
                  <th className="text-right py-2 px-2">Profit USDT</th>
                  <th className="text-left  py-2 px-2">Reason</th>
                </tr>
              </thead>
              <tbody>
                {tradeHistory.map((t: any) => (
                  <tr
                    key={String(t.id)}
                    className="border-b border-[#2a3a52]/50 hover:bg-[#2a3a52]/20"
                  >
                    <td className="py-2 px-2">{t.pair}</td>
                    <td className={`py-2 px-2 text-right text-xs font-semibold ${
                      t.side === 'long' ? 'text-emerald-400' : 'text-red-400'
                    }`}>
                      {t.side?.toUpperCase()}
                    </td>
                    <td className="py-2 px-2 text-right text-blue-400 text-xs">
                      {t.leverage ?? 1}x
                    </td>
                    <td className="py-2 px-2 text-right font-mono text-xs">
                      {Number(t.entry_price).toFixed(2)}
                    </td>
                    <td className="py-2 px-2 text-right font-mono text-xs">
                      {Number(t.exit_price).toFixed(2)}
                    </td>
                    <td className={`py-2 px-2 text-right font-semibold ${
                      (t.profit_pct ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'
                    }`}>
                      {(t.profit_pct ?? 0) >= 0 ? '+' : ''}{(t.profit_pct ?? 0).toFixed(2)}%
                    </td>
                    <td className={`py-2 px-2 text-right font-semibold ${
                      (t.profit_abs ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'
                    }`}>
                      {(t.profit_abs ?? 0) >= 0 ? '+' : ''}{(t.profit_abs ?? 0).toFixed(4)}
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
