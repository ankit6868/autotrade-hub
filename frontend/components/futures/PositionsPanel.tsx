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
      api.futures.orders({ status: 'pending' }).catch(() => ({ orders: [] })),
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
      await api.futures.forceClose(pair);
      refreshAll();
      onRefresh?.();
    } catch { /* */ }
    setClosingPair(null);
  }

  async function cancelOrder(orderId: string) {
    try {
      await api.futures.cancelOrder(orderId);
      refreshAll();
    } catch { /* */ }
  }

  async function closeAllPositions() {
    for (const p of positions) {
      await api.futures.forceClose(p.pair);
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
          />
        )}
        {tab === 'open_orders' && (
          <OrdersTab orders={openOrders} onCancel={cancelOrder} />
        )}
        {tab === 'order_history' && (
          <OrderHistoryTab />
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

function PositionsTab({ positions, closingPair, onClose, onCloseAll }: {
  positions: any[]; closingPair: string | null;
  onClose: (pair: string) => void; onCloseAll: () => void;
}) {
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
                      <div className="text-[10px] text-slate-500 mt-0.5">
                        {p.mode === 'isolated' ? 'Isolated' : 'Cross'} {lev}x
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
    </div>
  );
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
            <td className="px-2 py-2 text-white">{o.symbol}</td>
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

function OrderHistoryTab() {
  const [orders, setOrders] = useState<any[]>([]);
  useEffect(() => {
    api.futures.ordersHistory({ limit: 50 }).then(d => setOrders(d.orders || [])).catch(() => {});
  }, []);
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
            <td className="px-2 py-2 text-white">{o.symbol}</td>
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
