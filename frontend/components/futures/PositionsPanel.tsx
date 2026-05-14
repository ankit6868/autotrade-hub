'use client';
import { useEffect, useState, useCallback } from 'react';
import { api } from '@/lib/api';

interface Props {
  mode: 'paper' | 'live';
  onRefresh?: () => void;
  refreshTrigger?: number;
}

type Tab = 'positions' | 'open_orders' | 'order_history' | 'trade_history' | 'position_history' | 'assets' | 'bots';

export default function PositionsPanel({ mode, onRefresh, refreshTrigger }: Props) {
  const [tab, setTab] = useState<Tab>('positions');
  const [positions, setPositions] = useState<any[]>([]);
  const [openOrders, setOpenOrders] = useState<any[]>([]);
  const [orderHistory, setOrderHistory] = useState<any[]>([]);
  const [tradeHistory, setTradeHistory] = useState<any[]>([]);
  const [bots, setBots] = useState<any[]>([]);
  const [mainEngine, setMainEngine] = useState<any>(null);
  const [account, setAccount] = useState<any>(null);
  const [closingPair, setClosingPair] = useState<string | null>(null);

  const refreshAll = useCallback(async () => {
    // Each call wrapped independently so one failure doesn't block others
    const [pos, orders, history, acct, botList, engineStatus] = await Promise.all([
      api.futures.open(mode).catch(() => ({ trades: [] })),
      api.futures.orders({ status: 'pending', mode }).catch(() => ({ orders: [] })),
      api.futures.history({ mode, limit: '50' }).catch(() => ({ trades: [] })),
      api.futures.account(mode).catch(() => null),
      api.futures.bots.list(mode).catch(() => ({ bots: [] })),
      api.futures.status().catch(() => null),
    ]);
    setPositions(pos.trades || []);
    setOpenOrders(orders.orders || []);
    setTradeHistory(history.trades || []);
    if (acct) setAccount(acct);
    setBots(botList.bots || []);
    setMainEngine(engineStatus?.running ? engineStatus : null);
  }, [mode]);

  useEffect(() => {
    refreshAll();
    const t = setInterval(refreshAll, 8000);
    return () => clearInterval(t);
  }, [refreshAll]);

  // Immediate refresh when parent signals an order was placed
  useEffect(() => {
    if (refreshTrigger && refreshTrigger > 0) {
      refreshAll();
    }
  }, [refreshTrigger, refreshAll]);

  async function closePosition(pair: string) {
    setClosingPair(pair);
    try {
      await api.futures.forceClose(pair, mode);
      refreshAll();
      onRefresh?.();
    } catch { /* */ }
    setClosingPair(null);
  }

  async function cancelOrder(orderId: string) {
    try {
      const res = await api.futures.cancelOrder(orderId);
      if (res?.error) {
        // Backend returns a structured error when KuCoin refuses the
        // cancel — surface it to the user so they know the order is
        // still alive on the exchange.
        alert(res.error);
      }
      refreshAll();
    } catch (e) {
      alert(`Cancel failed: ${e}`);
    }
  }

  async function closeAllPositions() {
    for (const p of positions) {
      // Pass explicit mode so backend force-closes only the matching side.
      await api.futures.forceClose(p.pair, mode);
    }
    refreshAll();
    onRefresh?.();
  }

  const tabs: { key: Tab; label: string; count?: number }[] = [
    { key: 'open_orders', label: 'Open Orders', count: openOrders.length },
    { key: 'positions', label: 'Positions', count: positions.length },
    { key: 'assets', label: 'Assets' },
    { key: 'order_history', label: 'Order History' },
    { key: 'trade_history', label: 'Trade History' },
    { key: 'position_history', label: 'Position History' },
    { key: 'bots', label: 'Trading Algorithm', count: bots.length + (mainEngine ? 1 : 0) },
  ];

  return (
    <div className="flex flex-col h-full">
      {/* Tab bar */}
      <div className="flex items-center gap-1 px-2 border-b border-white/[0.06] overflow-x-auto scrollbar-none">
        {tabs.map(t => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className={`px-3 py-2 text-xs font-medium whitespace-nowrap border-b-2 transition-colors ${
              tab === t.key
                ? 'text-white border-emerald-500'
                : 'text-slate-400 border-transparent hover:text-white'
            }`}
          >
            {t.label}
            {t.count !== undefined && (
              <span className="ml-1 text-[10px] text-slate-500">({t.count})</span>
            )}
          </button>
        ))}
      </div>

      {/* Content */}
      <div className="flex-1 overflow-auto">
        {tab === 'positions' && (
          <PositionsTab
            positions={positions}
            closingPair={closingPair}
            onClose={closePosition}
            onCloseAll={closeAllPositions}
            onRefresh={refreshAll}
          />
        )}
        {tab === 'open_orders' && (
          <OrdersTab orders={openOrders} onCancel={cancelOrder} />
        )}
        {tab === 'order_history' && (
          <OrderHistoryTab mode={mode} />
        )}
        {tab === 'trade_history' && (
          <TradeHistoryTab trades={tradeHistory} />
        )}
        {tab === 'position_history' && (
          <TradeHistoryTab trades={tradeHistory} />
        )}
        {tab === 'assets' && (
          <AssetsTab account={account} />
        )}
        {tab === 'bots' && (
          <BotsTab bots={bots} mainEngine={mainEngine} />
        )}
      </div>
    </div>
  );
}

function PositionsTab({ positions, closingPair, onClose, onCloseAll, onRefresh }: {
  positions: any[]; closingPair: string | null;
  onClose: (pair: string) => void; onCloseAll: () => void;
  onRefresh?: () => void;
}) {
  // TP/SL editor state — opens an inline modal for the selected position.
  const [tpslPair, setTpslPair] = useState<string | null>(null);
  const tpslPosition = positions.find(p => p.pair === tpslPair) || null;

  if (positions.length === 0) {
    return <div className="text-center text-slate-500 py-8 text-sm">No open positions</div>;
  }
  return (
    <div>
      <div className="flex items-center justify-end gap-2 px-2 py-1">
        {positions.length > 1 && (
          <button onClick={onCloseAll} className="text-[10px] text-red-400 hover:text-red-300">Close All</button>
        )}
      </div>
      <table className="w-full text-xs">
        <thead>
          <tr className="text-slate-500 text-[10px]">
            <th className="text-left px-2 py-1">Contract</th>
            <th className="text-right px-2 py-1">Amount</th>
            <th className="text-right px-2 py-1">Value</th>
            <th className="text-right px-2 py-1">Entry Price</th>
            <th className="text-right px-2 py-1">Mark Price</th>
            <th className="text-right px-2 py-1">Est. Liq.</th>
            <th className="text-right px-2 py-1">Margin</th>
            <th className="text-right px-2 py-1">Unrealized PNL (ROI)</th>
            <th className="text-right px-2 py-1">TP / SL</th>
            <th className="text-right px-2 py-1">Risk Ratio</th>
            <th className="text-center px-2 py-1">Actions</th>
          </tr>
        </thead>
        <tbody>
          {positions.map((p, i) => {
            const entry = p.entry_price || 0;
            const current = p.current_price || entry;
            const margin = p.amount || 0;
            const pnl = p.unrealized_pnl || 0;
            const roi = margin > 0 ? (pnl / margin * 100) : 0;
            const lev = p.leverage || 1;
            const riskRatio = entry > 0 && p.liquidation_price
              ? Math.abs((current - p.liquidation_price) / current * 100).toFixed(1)
              : '--';
            return (
              <tr key={i} className="border-t border-white/[0.04] hover:bg-white/[0.02]">
                <td className="px-2 py-2">
                  <div className="flex items-center gap-1.5">
                    <div>
                      <div className="flex items-center gap-1.5">
                        <span className={`text-[9px] font-bold px-1.5 py-0.5 rounded ${
                          (p.side || p.direction) === 'long' ? 'bg-emerald-500/20 text-emerald-400' : 'bg-red-500/20 text-red-400'
                        }`}>{((p.side || p.direction) === 'long' ? 'LONG' : 'SHORT')}</span>
                        <span className="text-white font-medium">{p.pair} Perp</span>
                      </div>
                      {/* Leverage is locked while a position is open — KuCoin
                          rejects leverage changes on an active symbol. The tiny
                          padlock icon makes that visually obvious; the
                          tooltip explains why. Users close the position first
                          if they want a different leverage on the next trade. */}
                      <div
                        className="text-[10px] text-slate-500 mt-0.5 flex items-center gap-1"
                        title="Leverage is locked while a position is open. Close the position to change it."
                      >
                        <span>{p.mode === 'isolated' ? 'Isolated' : 'Cross'} {lev}x</span>
                        <svg
                          className="w-2.5 h-2.5 text-slate-500"
                          viewBox="0 0 24 24" fill="none" stroke="currentColor"
                          strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
                          aria-hidden="true"
                        >
                          <rect x="3" y="11" width="18" height="11" rx="2" ry="2" />
                          <path d="M7 11V7a5 5 0 0110 0v4" />
                        </svg>
                      </div>
                    </div>
                  </div>
                </td>
                <td className="text-right px-2 py-2 text-slate-300">{margin.toFixed(2)} USDT</td>
                <td className="text-right px-2 py-2 text-slate-300">{(margin * lev).toFixed(2)} USDT</td>
                <td className="text-right px-2 py-2 text-slate-300">{entry.toFixed(2)}</td>
                <td className="text-right px-2 py-2 text-white">{current.toFixed(2)}</td>
                <td className="text-right px-2 py-2 text-orange-400">
                  {p.liquidation_price ? p.liquidation_price.toFixed(2) : '--'}
                </td>
                <td className="text-right px-2 py-2 text-slate-300">{margin.toFixed(2)} USDT</td>
                <td className="text-right px-2 py-2">
                  <span className={pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}>
                    {pnl >= 0 ? '+' : ''}{pnl.toFixed(2)} USDT
                  </span>
                  <div className={`text-[10px] ${roi >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                    ({roi >= 0 ? '+' : ''}{roi.toFixed(2)}%)
                  </div>
                </td>
                <td className="text-right px-2 py-2">
                  <div className="text-[10px] tabular-nums">
                    <div className={p.tp_price ? 'text-emerald-400' : 'text-slate-500'}>
                      TP: {p.tp_price ? Number(p.tp_price).toFixed(2) : '—'}
                    </div>
                    <div className={p.stoploss_price ? 'text-red-400' : 'text-slate-500'}>
                      SL: {p.stoploss_price ? Number(p.stoploss_price).toFixed(2) : '—'}
                    </div>
                    <button
                      onClick={() => setTpslPair(p.pair)}
                      className="text-[9px] text-brand-400 hover:text-brand-300 underline mt-0.5"
                    >
                      Edit
                    </button>
                  </div>
                </td>
                <td className="text-right px-2 py-2 text-slate-400">{riskRatio}%</td>
                <td className="text-center px-2 py-2">
                  <div className="flex items-center justify-center gap-1">
                    <button
                      onClick={() => onClose(p.pair)}
                      disabled={closingPair === p.pair}
                      className="px-2 py-1 rounded bg-slate-700 text-slate-300 text-[10px] hover:bg-slate-600 disabled:opacity-50"
                    >
                      Close
                    </button>
                    <button
                      onClick={() => onClose(p.pair)}
                      disabled={closingPair === p.pair}
                      className="px-2 py-1 rounded bg-red-500/20 text-red-400 text-[10px] hover:bg-red-500/30"
                    >
                      Market Close
                    </button>
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {tpslPosition && (
        <TpSlEditor
          position={tpslPosition}
          onClose={() => setTpslPair(null)}
          onSaved={() => { setTpslPair(null); onRefresh?.(); }}
        />
      )}
    </div>
  );
}

// Inline modal for setting Take Profit / Stop Loss on an existing position.
// Works for both paper (engine state only) and live (also places reduceOnly
// TP/SL stop orders on KuCoin Lead Trading via /api/futures/position/tp-sl).
// Take-Profit / Stop-Loss editor.
//
// Two control surfaces for each value:
//   - A percent slider 0-100 (how far from entry, in the profitable
//     direction for TP / unprofitable direction for SL).
//   - A USDT price field that the user can type directly.
//
// They stay in sync: editing one updates the other. The slider gives quick
// access to common levels (1%, 5%, 10%, …) without arithmetic; the field
// lets users place TP/SL at an exact swing high / support level.
//
// Calculations are direction-aware:
//   LONG  TP price = entry × (1 + pct/100)
//   LONG  SL price = entry × (1 − pct/100)
//   SHORT TP price = entry × (1 − pct/100)
//   SHORT SL price = entry × (1 + pct/100)
//
// "Est. PnL" preview multiplies by the position's notional and shows the
// USDT gain/loss at trigger — same UX as KuCoin's TP/SL panel.
function TpSlEditor({ position, onClose, onSaved }: {
  position: any; onClose: () => void; onSaved: () => void;
}) {
  const entry = Number(position.entry_price) || 0;
  const isLong = (position.side || position.direction) === 'long';
  const leverage = Number(position.leverage) || 1;
  const margin = Number(position.amount) || 0;       // USDT margin locked
  const notional = margin * leverage;                 // position value

  // Slider state — 0-100% (capped at 100, but in practice 1-50% is the
  // useful range). 0 means "no TP/SL set".
  const [tpPct, setTpPct] = useState<number>(() =>
    position.tp_price && entry > 0
      ? Math.abs(((position.tp_price - entry) / entry) * 100)
      : 0
  );
  const [slPct, setSlPct] = useState<number>(() =>
    position.stoploss_price && entry > 0
      ? Math.abs(((position.stoploss_price - entry) / entry) * 100)
      : 0
  );

  // Derived prices from percentages (single source of truth = the slider).
  const tpPrice = tpPct > 0
    ? (isLong ? entry * (1 + tpPct / 100) : entry * (1 - tpPct / 100))
    : 0;
  const slPrice = slPct > 0
    ? (isLong ? entry * (1 - slPct / 100) : entry * (1 + slPct / 100))
    : 0;

  // Est. P&L at trigger = notional × pct/100 (leveraged gain/loss in USDT).
  const tpPnl = tpPct > 0 ? (notional * tpPct) / 100 : 0;
  const slPnl = slPct > 0 ? -(notional * slPct) / 100 : 0;
  // ROI on margin = pct × leverage
  const tpRoi = tpPct * leverage;
  const slRoi = -slPct * leverage;

  // Text-field state — only used for typed overrides. Empty means "follow
  // the slider". Editing it updates the slider too.
  const [tpInput, setTpInput] = useState<string>('');
  const [slInput, setSlInput] = useState<string>('');

  function handleTpInput(v: string) {
    setTpInput(v);
    const num = parseFloat(v);
    if (!isFinite(num) || num <= 0 || entry <= 0) return;
    const pct = isLong
      ? ((num - entry) / entry) * 100
      : ((entry - num) / entry) * 100;
    if (pct > 0) setTpPct(Math.min(pct, 100));
  }

  function handleSlInput(v: string) {
    setSlInput(v);
    const num = parseFloat(v);
    if (!isFinite(num) || num <= 0 || entry <= 0) return;
    const pct = isLong
      ? ((entry - num) / entry) * 100
      : ((num - entry) / entry) * 100;
    if (pct > 0) setSlPct(Math.min(pct, 100));
  }

  // When slider moves, sync the readout field so the user sees the price.
  useEffect(() => {
    if (tpPct > 0) setTpInput(tpPrice.toFixed(2));
    else setTpInput('');
  }, [tpPct, tpPrice]);

  useEffect(() => {
    if (slPct > 0) setSlInput(slPrice.toFixed(2));
    else setSlInput('');
  }, [slPct, slPrice]);

  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string>('');

  async function save() {
    setSubmitting(true);
    setError('');
    const tpNum = tpPct > 0 ? tpPrice : undefined;
    const slNum = slPct > 0 ? slPrice : undefined;
    if (!tpNum && !slNum) {
      setError('Set at least one of Take Profit or Stop Loss.');
      setSubmitting(false);
      return;
    }
    try {
      const r = await api.futures.setTpSl({
        pair: position.pair,
        ...(tpNum ? { tp_price: tpNum } : {}),
        ...(slNum ? { sl_price: slNum } : {}),
      });
      if (r?.error) {
        setError(r.error);
        setSubmitting(false);
        return;
      }
      // Highlight which side reached KuCoin so users see live confirmation.
      const kc = r?.kucoin || {};
      const tpOk = kc?.tp?.code === '200000';
      const slOk = kc?.sl?.code === '200000';
      if (r?.source === 'kucoin' || tpOk || slOk) {
        // Live success — let the parent refresh the positions to pick up
        // the newly-attached TP/SL values.
      }
      onSaved();
    } catch (e) {
      setError(String(e));
      setSubmitting(false);
    }
  }

  // Quick-set chips for common percentages.
  const TP_CHIPS = [1, 3, 5, 10, 20];
  const SL_CHIPS = [1, 2, 3, 5, 10];

  return (
    <div className="fixed inset-0 z-50 bg-black/60 flex items-center justify-center p-4" onClick={onClose}>
      <div
        onClick={e => e.stopPropagation()}
        className="bg-[#0d1424] border border-[#243153] rounded-xl p-5 w-full max-w-md shadow-2xl"
      >
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-semibold text-white">Take Profit / Stop Loss</h3>
          <button onClick={onClose} className="text-slate-400 hover:text-white text-lg leading-none">×</button>
        </div>
        <div className="text-[11px] text-slate-400 mb-4">
          {position.pair} ·{' '}
          <span className={isLong ? 'text-emerald-400' : 'text-red-400'}>{isLong ? 'LONG' : 'SHORT'}</span>
          {' '}· {leverage}× · Entry <span className="text-white">{entry.toFixed(2)}</span>
        </div>

        {/* ─────────── Take Profit ─────────── */}
        <div className="mb-5">
          <div className="flex items-center justify-between mb-1.5">
            <label className="text-[11px] font-medium text-emerald-400">Take Profit</label>
            <span className="text-[10px] text-slate-500 tabular-nums">
              {tpPct > 0
                ? `+${tpPct.toFixed(2)}% from entry · ROI ${tpRoi >= 0 ? '+' : ''}${tpRoi.toFixed(1)}%`
                : 'not set'}
            </span>
          </div>
          {/* Native range slider — continuous, full 0-100% range. */}
          <div className="relative h-6 mb-2">
            <div className="absolute top-1/2 left-0 right-0 h-[3px] bg-slate-700 -translate-y-1/2 rounded" />
            <div
              className="absolute top-1/2 left-0 h-[3px] bg-emerald-500 -translate-y-1/2 rounded transition-[width]"
              style={{ width: `${tpPct}%` }}
            />
            <input
              type="range" min={0} max={100} step={0.1} value={tpPct}
              onChange={e => setTpPct(Number(e.target.value))}
              aria-label="Take profit percentage"
              className="absolute inset-0 w-full h-full opacity-0 cursor-pointer"
            />
            <div
              className="absolute top-1/2 -translate-y-1/2 -translate-x-1/2 w-3.5 h-3.5 rounded-full bg-emerald-500 border-2 border-white shadow pointer-events-none transition-[left]"
              style={{ left: `${tpPct}%` }}
            />
          </div>
          {/* Quick chips */}
          <div className="flex gap-1 mb-2">
            {TP_CHIPS.map(pct => (
              <button
                key={`tp-${pct}`}
                onClick={() => setTpPct(pct)}
                className={`flex-1 py-1 rounded text-[10px] font-medium border transition-colors ${
                  Math.abs(tpPct - pct) < 0.05
                    ? 'bg-emerald-500/20 border-emerald-500/40 text-emerald-300'
                    : 'border-white/10 text-slate-400 hover:border-emerald-500/30 hover:text-emerald-400'
                }`}
              >
                {pct}%
              </button>
            ))}
            <button
              onClick={() => setTpPct(0)}
              className="px-2 py-1 rounded text-[10px] font-medium border border-white/10 text-slate-500 hover:text-slate-300"
              title="Clear take-profit"
            >
              ✕
            </button>
          </div>
          {/* Trigger price input */}
          <div className="flex gap-2 items-center">
            <label className="text-[10px] text-slate-500 shrink-0">Trigger</label>
            <input
              type="number" step="any" value={tpInput}
              onChange={e => handleTpInput(e.target.value)}
              placeholder={isLong ? `> ${entry.toFixed(2)}` : `< ${entry.toFixed(2)}`}
              className="flex-1 bg-[#06091a] border border-[#243153] rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-emerald-500 tabular-nums"
            />
            <span className="text-[10px] text-slate-500">USDT</span>
          </div>
          {tpPct > 0 && (
            <div className="text-[10px] text-emerald-400/80 mt-1.5 tabular-nums">
              Est. P&L at trigger: <b>+{tpPnl.toFixed(2)} USDT</b>
            </div>
          )}
        </div>

        {/* ─────────── Stop Loss ─────────── */}
        <div className="mb-4">
          <div className="flex items-center justify-between mb-1.5">
            <label className="text-[11px] font-medium text-red-400">Stop Loss</label>
            <span className="text-[10px] text-slate-500 tabular-nums">
              {slPct > 0
                ? `−${slPct.toFixed(2)}% from entry · ROI ${slRoi.toFixed(1)}%`
                : 'not set'}
            </span>
          </div>
          <div className="relative h-6 mb-2">
            <div className="absolute top-1/2 left-0 right-0 h-[3px] bg-slate-700 -translate-y-1/2 rounded" />
            <div
              className="absolute top-1/2 left-0 h-[3px] bg-red-500 -translate-y-1/2 rounded transition-[width]"
              style={{ width: `${slPct}%` }}
            />
            <input
              type="range" min={0} max={100} step={0.1} value={slPct}
              onChange={e => setSlPct(Number(e.target.value))}
              aria-label="Stop loss percentage"
              className="absolute inset-0 w-full h-full opacity-0 cursor-pointer"
            />
            <div
              className="absolute top-1/2 -translate-y-1/2 -translate-x-1/2 w-3.5 h-3.5 rounded-full bg-red-500 border-2 border-white shadow pointer-events-none transition-[left]"
              style={{ left: `${slPct}%` }}
            />
          </div>
          <div className="flex gap-1 mb-2">
            {SL_CHIPS.map(pct => (
              <button
                key={`sl-${pct}`}
                onClick={() => setSlPct(pct)}
                className={`flex-1 py-1 rounded text-[10px] font-medium border transition-colors ${
                  Math.abs(slPct - pct) < 0.05
                    ? 'bg-red-500/20 border-red-500/40 text-red-300'
                    : 'border-white/10 text-slate-400 hover:border-red-500/30 hover:text-red-400'
                }`}
              >
                {pct}%
              </button>
            ))}
            <button
              onClick={() => setSlPct(0)}
              className="px-2 py-1 rounded text-[10px] font-medium border border-white/10 text-slate-500 hover:text-slate-300"
              title="Clear stop-loss"
            >
              ✕
            </button>
          </div>
          <div className="flex gap-2 items-center">
            <label className="text-[10px] text-slate-500 shrink-0">Trigger</label>
            <input
              type="number" step="any" value={slInput}
              onChange={e => handleSlInput(e.target.value)}
              placeholder={isLong ? `< ${entry.toFixed(2)}` : `> ${entry.toFixed(2)}`}
              className="flex-1 bg-[#06091a] border border-[#243153] rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-red-500 tabular-nums"
            />
            <span className="text-[10px] text-slate-500">USDT</span>
          </div>
          {slPct > 0 && (
            <div className="text-[10px] text-red-400/80 mt-1.5 tabular-nums">
              Est. P&L at trigger: <b>{slPnl.toFixed(2)} USDT</b>
            </div>
          )}
        </div>

        {error && <div className="text-[11px] text-red-400 mb-2 leading-snug">{error}</div>}

        <div className="flex gap-2 mt-2">
          <button
            onClick={onClose}
            className="flex-1 px-3 py-2 rounded-lg bg-slate-800 text-slate-300 text-sm hover:bg-slate-700"
          >
            Cancel
          </button>
          <button
            onClick={save}
            disabled={submitting || (tpPct === 0 && slPct === 0)}
            className="flex-1 px-3 py-2 rounded-lg bg-emerald-500 text-white text-sm font-medium hover:bg-emerald-400 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {submitting ? 'Saving…' : 'Save'}
          </button>
        </div>

        <div className="text-[10px] text-slate-500 mt-3 leading-snug">
          For live positions, TP/SL is placed on KuCoin Lead Trading as
          reduceOnly stop orders — you&apos;ll see them in KuCoin&apos;s
          Open Orders tab right after saving.
        </div>
      </div>
    </div>
  );
}

// Translate KuCoin's internal futures symbol back to the human pair so the
// Open Orders table reads "BTC/USDT" instead of the awkward "XBTUSDTM".
//   XBTUSDTM → BTC/USDT
//   ETHUSDTM → ETH/USDT
//   …
// Falls through to the raw symbol if it doesn't follow the pattern.
function _displayPair(symbol: string | undefined): string {
  if (!symbol) return '—';
  let s = symbol;
  if (s.toUpperCase().startsWith('XBT')) s = 'BTC' + s.slice(3);
  if (s.toUpperCase().endsWith('USDTM')) s = s.slice(0, -5) + '/USDT';
  return s;
}

function OrdersTab({ orders, onCancel }: { orders: any[]; onCancel: (id: string) => void }) {
  if (orders.length === 0) {
    return <div className="text-center text-slate-500 py-8 text-sm">No open orders</div>;
  }
  return (
    <table className="w-full text-xs">
      <thead>
        <tr className="text-slate-500 text-[10px]">
          <th className="text-left px-2 py-1">Symbol</th>
          <th className="text-left px-2 py-1">Type</th>
          <th className="text-left px-2 py-1">Side</th>
          <th className="text-right px-2 py-1">Price</th>
          <th className="text-right px-2 py-1">Size</th>
          <th className="text-right px-2 py-1">Leverage</th>
          <th className="text-center px-2 py-1">Cancel</th>
        </tr>
      </thead>
      <tbody>
        {orders.map((o, i) => (
          <tr key={i} className="border-t border-white/[0.04]">
            <td className="px-2 py-2 text-white">{_displayPair(o.symbol)}</td>
            <td className="px-2 py-2 text-slate-300 capitalize">{o.order_type}</td>
            <td className={`px-2 py-2 capitalize ${o.side === 'buy' ? 'text-emerald-400' : 'text-red-400'}`}>{o.side}</td>
            <td className="text-right px-2 py-2 text-slate-300">{o.price ?? '--'}</td>
            <td className="text-right px-2 py-2 text-slate-300">{o.size}</td>
            <td className="text-right px-2 py-2 text-slate-400">{o.leverage}x</td>
            <td className="text-center px-2 py-2">
              <button
                onClick={() => onCancel(o.order_id)}
                className="px-2 py-1 rounded bg-red-500/20 text-red-400 text-[10px] hover:bg-red-500/30"
              >
                Cancel
              </button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function OrderHistoryTab({ mode }: { mode: 'paper' | 'live' }) {
  const [orders, setOrders] = useState<any[]>([]);
  useEffect(() => {
    api.futures.ordersHistory({ limit: 50, mode }).then(d => setOrders(d.orders || [])).catch(() => {});
  }, [mode]);
  if (orders.length === 0) {
    return <div className="text-center text-slate-500 py-8 text-sm">No order history</div>;
  }
  return (
    <table className="w-full text-xs">
      <thead>
        <tr className="text-slate-500 text-[10px]">
          <th className="text-left px-2 py-1">Symbol</th>
          <th className="text-left px-2 py-1">Type</th>
          <th className="text-left px-2 py-1">Side</th>
          <th className="text-right px-2 py-1">Price</th>
          <th className="text-right px-2 py-1">Filled</th>
          <th className="text-left px-2 py-1">Status</th>
          <th className="text-right px-2 py-1">Time</th>
        </tr>
      </thead>
      <tbody>
        {orders.map((o, i) => (
          <tr key={i} className="border-t border-white/[0.04]">
            <td className="px-2 py-2 text-white">{_displayPair(o.symbol)}</td>
            <td className="px-2 py-2 text-slate-300 capitalize">{o.order_type}</td>
            <td className={`px-2 py-2 capitalize ${o.side === 'buy' ? 'text-emerald-400' : 'text-red-400'}`}>{o.side}</td>
            <td className="text-right px-2 py-2 text-slate-300">{o.price ?? '--'}</td>
            <td className="text-right px-2 py-2 text-slate-300">{o.filled_size ?? 0}/{o.size}</td>
            <td className="px-2 py-2 capitalize text-slate-400">{o.status}</td>
            <td className="text-right px-2 py-2 text-slate-500">{o.created_at ? new Date(o.created_at).toLocaleString() : '--'}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function TradeHistoryTab({ trades }: { trades: any[] }) {
  if (trades.length === 0) {
    return <div className="text-center text-slate-500 py-8 text-sm">No trade history</div>;
  }
  return (
    <table className="w-full text-xs">
      <thead>
        <tr className="text-slate-500 text-[10px]">
          <th className="text-left px-2 py-1">Pair</th>
          <th className="text-left px-2 py-1">Side</th>
          <th className="text-right px-2 py-1">Entry</th>
          <th className="text-right px-2 py-1">Exit</th>
          <th className="text-right px-2 py-1">Amount</th>
          <th className="text-right px-2 py-1">Leverage</th>
          <th className="text-right px-2 py-1">PNL</th>
          <th className="text-right px-2 py-1">PNL %</th>
          <th className="text-left px-2 py-1">Reason</th>
          <th className="text-right px-2 py-1">Time</th>
        </tr>
      </thead>
      <tbody>
        {trades.map((t, i) => (
          <tr key={i} className="border-t border-white/[0.04]">
            <td className="px-2 py-2 text-white">{t.pair}</td>
            <td className={`px-2 py-2 capitalize ${t.side === 'long' ? 'text-emerald-400' : 'text-red-400'}`}>{t.side}</td>
            <td className="text-right px-2 py-2 text-slate-300">{t.entry_price?.toFixed(2)}</td>
            <td className="text-right px-2 py-2 text-slate-300">{t.exit_price?.toFixed(2)}</td>
            <td className="text-right px-2 py-2 text-slate-300">{t.amount?.toFixed(2)}</td>
            <td className="text-right px-2 py-2 text-slate-400">{t.leverage}x</td>
            <td className={`text-right px-2 py-2 ${(t.profit_abs || 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
              {(t.profit_abs || 0) >= 0 ? '+' : ''}{(t.profit_abs || 0).toFixed(2)}
            </td>
            <td className={`text-right px-2 py-2 ${(t.profit_pct || 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
              {(t.profit_pct || 0) >= 0 ? '+' : ''}{(t.profit_pct || 0).toFixed(2)}%
            </td>
            <td className="px-2 py-2 text-slate-500 capitalize">{t.exit_reason}</td>
            <td className="text-right px-2 py-2 text-slate-500 text-[10px]">
              {t.exit_time ? new Date(t.exit_time).toLocaleString() : '--'}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function AssetsTab({ account }: { account: any }) {
  return (
    <div className="p-4 space-y-3">
      <h3 className="text-sm font-bold text-white">Asset Overview</h3>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        {[
          { label: 'Balance', value: `${(account?.balance ?? 0).toFixed(2)} USDT` },
          { label: 'Equity', value: `${(account?.equity ?? 0).toFixed(2)} USDT` },
          { label: 'Unrealized PNL', value: `${(account?.unrealized_pnl ?? 0).toFixed(2)} USDT`, color: (account?.unrealized_pnl ?? 0) >= 0 },
          { label: 'Used Margin', value: `${(account?.used_margin ?? 0).toFixed(2)} USDT` },
          { label: 'Available', value: `${(account?.available_balance ?? 0).toFixed(2)} USDT` },
          { label: 'Positions', value: `${account?.position_count ?? 0}` },
          { label: 'Mode', value: account?.mode ?? 'paper' },
          { label: 'Currency', value: account?.currency ?? 'USDT' },
        ].map((item, i) => (
          <div key={i} className="bg-slate-800/50 rounded-lg p-3 border border-white/[0.04]">
            <div className="text-[10px] text-slate-500 mb-1">{item.label}</div>
            <div className={`text-sm font-bold ${
              'color' in item ? (item.color ? 'text-emerald-400' : 'text-red-400') : 'text-white'
            }`}>
              {item.value}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function BotsTab({ bots, mainEngine }: { bots: any[]; mainEngine: any }) {
  const hasContent = bots.length > 0 || mainEngine;
  if (!hasContent) {
    return (
      <div className="text-center py-8">
        <p className="text-slate-500 text-sm mb-2">No trading algorithms</p>
        <p className="text-slate-600 text-xs">Create a bot from the Bot panel on the right to start automated trading.</p>
      </div>
    );
  }

  const runningBots = bots.filter(b => b.is_running);
  const stoppedBots = bots.filter(b => !b.is_running);

  return (
    <div>
      <table className="w-full text-xs">
        <thead>
          <tr className="text-slate-500 text-[10px]">
            <th className="text-left px-2 py-1">Strategy</th>
            <th className="text-left px-2 py-1">Pairs</th>
            <th className="text-right px-2 py-1">Leverage</th>
            <th className="text-right px-2 py-1">Wallet</th>
            <th className="text-right px-2 py-1">Trades</th>
            <th className="text-right px-2 py-1">PNL</th>
            <th className="text-left px-2 py-1">Status</th>
          </tr>
        </thead>
        <tbody>
          {mainEngine && (
            <tr className="border-t border-cyan-500/20 bg-cyan-500/[0.03]">
              <td className="px-2 py-2 text-white">
                <span className="inline-block bg-cyan-500/20 text-cyan-300 text-[9px] font-bold px-1 rounded mr-1">MAIN</span>
                {mainEngine.strategy || 'Engine'}
              </td>
              <td className="px-2 py-2 text-slate-300">{mainEngine.pairs?.join(', ') || mainEngine.pair || '—'}</td>
              <td className="text-right px-2 py-2 text-slate-300">{mainEngine.leverage || '—'}x</td>
              <td className="text-right px-2 py-2 text-slate-300">{mainEngine.wallet?.toFixed(2) || '—'}</td>
              <td className="text-right px-2 py-2 text-slate-400">{mainEngine.total_trades ?? '—'}</td>
              <td className={`text-right px-2 py-2 ${(mainEngine.total_pnl ?? 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                {mainEngine.total_pnl != null ? `${mainEngine.total_pnl >= 0 ? '+' : ''}${mainEngine.total_pnl.toFixed(2)}` : '—'}
              </td>
              <td className="px-2 py-2">
                <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] bg-cyan-500/10 text-cyan-400">
                  Running
                </span>
              </td>
            </tr>
          )}
          {runningBots.map((b, i) => (
            <tr key={`run-${i}`} className="border-t border-white/[0.04]">
              <td className="px-2 py-2 text-white">
                <span className="inline-flex items-center gap-1">
                  <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
                  {b.strategy_name}
                  <span className={`text-[8px] font-bold px-1 rounded ${
                    b.mode === 'live' ? 'bg-orange-500/20 text-orange-400' : 'bg-blue-500/20 text-blue-400'
                  }`}>{(b.mode || 'paper').toUpperCase()}</span>
                </span>
              </td>
              <td className="px-2 py-2 text-slate-300">{b.pairs}</td>
              <td className="text-right px-2 py-2 text-slate-300">{b.leverage}x</td>
              <td className="text-right px-2 py-2 text-slate-300">{b.wallet}</td>
              <td className="text-right px-2 py-2 text-slate-400">{b.total_trades}</td>
              <td className={`text-right px-2 py-2 ${(b.total_pnl || 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                {(b.total_pnl || 0) >= 0 ? '+' : ''}{(b.total_pnl || 0).toFixed(2)}
              </td>
              <td className="px-2 py-2">
                <span className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] ${
                  b.winding_down ? 'bg-amber-500/10 text-amber-400' : 'bg-emerald-500/10 text-emerald-400'
                }`}>
                  {b.winding_down ? 'Closing' : 'Running'}
                </span>
              </td>
            </tr>
          ))}
          {stoppedBots.map((b, i) => (
            <tr key={`stop-${i}`} className="border-t border-white/[0.04] opacity-60">
              <td className="px-2 py-2 text-slate-400">
                <span className="inline-flex items-center gap-1">
                  <span className="w-1.5 h-1.5 rounded-full bg-slate-500" />
                  {b.strategy_name}
                  <span className={`text-[8px] font-bold px-1 rounded ${
                    b.mode === 'live' ? 'bg-orange-500/20 text-orange-400' : 'bg-blue-500/20 text-blue-400'
                  }`}>{(b.mode || 'paper').toUpperCase()}</span>
                </span>
              </td>
              <td className="px-2 py-2 text-slate-500">{b.pairs}</td>
              <td className="text-right px-2 py-2 text-slate-500">{b.leverage}x</td>
              <td className="text-right px-2 py-2 text-slate-500">{b.wallet}</td>
              <td className="text-right px-2 py-2 text-slate-500">{b.total_trades}</td>
              <td className={`text-right px-2 py-2 ${(b.total_pnl || 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                {(b.total_pnl || 0) >= 0 ? '+' : ''}{(b.total_pnl || 0).toFixed(2)}
              </td>
              <td className="px-2 py-2">
                <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] bg-red-500/10 text-red-400">
                  {b.engine_running === false && b.is_running === false ? 'Crashed' : 'Stopped'}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
