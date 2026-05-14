'use client';
import { useEffect, useState, useCallback } from 'react';
import { api } from '@/lib/api';
import KuCoinFuturesChart from '@/components/charts/KuCoinFuturesChart';
import PairTabs from '@/components/futures/PairTabs';
import OrderBook from '@/components/futures/OrderBook';
import RecentTrades from '@/components/futures/RecentTrades';
import ManualOrderPanel from '@/components/futures/ManualOrderPanel';
import BotPanel from '@/components/futures/BotPanel';
import PositionsPanel from '@/components/futures/PositionsPanel';
import AssetOverview from '@/components/futures/AssetOverview';

type RightPanel = 'manual' | 'bot';
type MiddlePanel = 'orderbook' | 'recent_trades';

export default function FuturesTerminal() {
  const [pair, setPair] = useState('BTC/USDT');
  const [mode, setMode] = useState<'paper' | 'live'>('paper');
  const [rightPanel, setRightPanel] = useState<RightPanel>('manual');
  const [middlePanel, setMiddlePanel] = useState<MiddlePanel>('orderbook');
  const [leverage, setLeverage] = useState(3);
  const [marginMode, setMarginMode] = useState('isolated');
  const [account, setAccount] = useState<any>({ balance: 1000, available_balance: 1000 });
  const [lastPrice, setLastPrice] = useState(0);
  const [refreshTrigger, setRefreshTrigger] = useState(0);

  const tvSymbol = `KUCOIN:${pair.replace('/', '')}`;
  const futSymbol = pair.replace('/', '').replace('USDT', 'USDTM');

  const refreshAccount = useCallback(async () => {
    try {
      const data = await api.futures.account(mode);
      setAccount(data);
    } catch { /* silent */ }
  }, [mode]);

  const fetchLeverage = useCallback(async () => {
    try {
      const data = await api.futures.getLeverage(futSymbol);
      if (data.leverage) setLeverage(data.leverage);
      if (data.margin_mode) setMarginMode(data.margin_mode);
    } catch { /* silent */ }
  }, [futSymbol]);

  useEffect(() => {
    refreshAccount();
    fetchLeverage();
    const t = setInterval(refreshAccount, 15000);
    return () => clearInterval(t);
  }, [refreshAccount, fetchLeverage]);

  useEffect(() => {
    api.market.price(pair).then(d => {
      if (d.price) setLastPrice(parseFloat(d.price));
    }).catch(() => {});
    const t = setInterval(() => {
      api.market.price(pair).then(d => {
        if (d.price) setLastPrice(parseFloat(d.price));
      }).catch(() => {});
    }, 5000);
    return () => clearInterval(t);
  }, [pair]);

  async function handleLeverageChange(lev: number) {
    setLeverage(lev);
    try {
      await api.futures.setLeverage({ symbol: futSymbol, leverage: lev });
    } catch { /* */ }
    // Re-read from KuCoin — Cross mode may keep its own per-symbol leverage
    // that overrides our request, and the user needs to see the real value.
    try {
      const back = await api.futures.getLeverage(futSymbol);
      if (back.leverage && back.leverage !== lev) {
        setLeverage(back.leverage);
      }
    } catch { /* */ }
  }

  async function handleMarginModeChange(m: string) {
    setMarginMode(m);
    try {
      await api.futures.setMarginMode({ symbol: futSymbol, mode: m });
    } catch { /* */ }
    // Re-read after toggle in case KuCoin rejected (e.g. open position
    // locks the mode) — the UI should snap back to the real mode.
    try {
      const back = await api.futures.getLeverage(futSymbol);
      if (back.margin_mode && back.margin_mode !== m) {
        setMarginMode(back.margin_mode);
      }
    } catch { /* */ }
  }

  function handlePriceClick(price: number) {
    setLastPrice(price);
  }

  return (
    <div className="fixed inset-0 md:left-64 flex flex-col overflow-hidden bg-[#0d1117] z-20 pt-14 md:pt-0">
      {/* Top bar: pair tabs + mode toggle */}
      <div className="flex items-center justify-between bg-[#0d1117] border-b border-white/[0.06]">
        <PairTabs activePair={pair} onPairChange={setPair} />
        <div className="flex items-center gap-2 px-3 shrink-0">
          <div className="flex rounded-md overflow-hidden border border-white/[0.1] text-[11px]">
            <button
              onClick={() => setMode('paper')}
              className={`px-3 py-1.5 ${mode === 'paper' ? 'bg-emerald-500/20 text-emerald-400' : 'text-slate-400 hover:text-white'}`}
            >
              Paper
            </button>
            <button
              onClick={() => setMode('live')}
              className={`px-3 py-1.5 ${mode === 'live' ? 'bg-red-500/20 text-red-400' : 'text-slate-400 hover:text-white'}`}
            >
              Live
            </button>
          </div>
        </div>
      </div>

      {/* Main layout — KuCoin-style. Top row has three side-by-side
          columns (Chart | OrderBook | Manual panel), each constrained to
          the same height so the OrderBook doesn't stretch beyond the
          chart. The Positions panel below spans the full width like
          KuCoin's terminal, so users get a wide Open Orders / Positions /
          History view instead of a squashed one. */}
      <div className="flex-1 flex flex-col overflow-hidden">
        {/* Top row: chart + order book + manual/bot panel */}
        <div className="flex-1 flex min-h-0 overflow-hidden">
          {/* Chart column — grows to fill remaining horizontal space */}
          <div className="flex-1 min-w-0 flex flex-col overflow-hidden">
            <KuCoinFuturesChart pair={pair} defaultInterval="15m" />
          </div>

          {/* Order Book / Recent Trades column. Fixed width and bounded
              to the chart row's height — internal content scrolls within
              `flex-1 overflow-hidden` so the long order list never pushes
              the layout to full page height. */}
          <div className="w-[220px] xl:w-[250px] border-l border-white/[0.06] bg-[#0d1117] flex-col hidden lg:flex overflow-hidden">
            <div className="flex border-b border-white/[0.06] shrink-0">
              <button
                onClick={() => setMiddlePanel('orderbook')}
                className={`flex-1 py-2 text-[11px] font-medium ${
                  middlePanel === 'orderbook' ? 'text-white border-b-2 border-emerald-500' : 'text-slate-400'
                }`}
              >
                Order Book
              </button>
              <button
                onClick={() => setMiddlePanel('recent_trades')}
                className={`flex-1 py-2 text-[11px] font-medium ${
                  middlePanel === 'recent_trades' ? 'text-white border-b-2 border-emerald-500' : 'text-slate-400'
                }`}
              >
                Recent Trades
              </button>
            </div>
            <div className="flex-1 overflow-hidden min-h-0">
              {middlePanel === 'orderbook' ? (
                <OrderBook symbol={pair} onPriceClick={handlePriceClick} />
              ) : (
                <RecentTrades symbol={pair} />
              )}
            </div>
          </div>

          {/* Right column: Manual / Bot trading + Asset Overview at the
              bottom (KuCoin layout). Asset Overview lives ONLY here now —
              the old duplicate inside the bottom positions strip has been
              removed so the page never has two copies of the same panel. */}
          <div className="w-[300px] xl:w-[340px] border-l border-white/[0.06] bg-[#0d1117] flex-col hidden lg:flex overflow-hidden">
            <div className="flex border-b border-white/[0.06] shrink-0">
              <button
                onClick={() => setRightPanel('manual')}
                className={`flex-1 py-2 text-xs font-bold ${
                  rightPanel === 'manual' ? 'text-white border-b-2 border-emerald-500' : 'text-slate-400'
                }`}
              >
                Manual
              </button>
              <button
                onClick={() => setRightPanel('bot')}
                className={`flex-1 py-2 text-xs font-bold ${
                  rightPanel === 'bot' ? 'text-white border-b-2 border-emerald-500' : 'text-slate-400'
                }`}
              >
                Bot
              </button>
            </div>

            {/* Manual/Bot panel — scrolls internally if needed */}
            <div className="flex-1 overflow-y-auto min-h-0">
              {rightPanel === 'manual' ? (
                <ManualOrderPanel
                  symbol={futSymbol}
                  pair={pair}
                  mode={mode}
                  leverage={leverage}
                  marginMode={marginMode}
                  availableBalance={account?.available_balance ?? account?.balance ?? 1000}
                  lastPrice={lastPrice}
                  onLeverageChange={handleLeverageChange}
                  onMarginModeChange={handleMarginModeChange}
                  onOrderPlaced={() => {
                    refreshAccount();
                    // Re-fetch leverage/margin-mode from KuCoin — Cross orders
                    // can land at a different leverage than the UI selector
                    // showed when the user clicked Buy/Sell.
                    fetchLeverage();
                    setRefreshTrigger(n => n + 1);
                  }}
                />
              ) : (
                <BotPanel
                  pair={pair}
                  mode={mode}
                  paperBalance={account?.available_balance ?? account?.balance ?? 1000}
                  onBotCreated={refreshAccount}
                />
              )}
            </div>

            {/* Asset Overview pinned to the bottom of the right column —
                always visible (no xl:hidden trick), matches KuCoin's
                trade page where the panel sits directly under the order
                form. `shrink-0` keeps it from being squeezed by the
                Manual panel above. */}
            <div className="shrink-0 max-h-[40%] overflow-y-auto">
              <AssetOverview mode={mode} pair={pair} />
            </div>
          </div>
        </div>

        {/* Bottom row: Positions panel — now spans the full page width
            (was previously stuck under just the chart column). Gives the
            tab strip (Open Orders / Positions / Assets / Order History /
            Trade History / Position History / Trading Algorithm) the
            horizontal room it needs without overflowing. */}
        <div className="h-[240px] border-t border-white/[0.06] bg-[#0d1117] overflow-hidden">
          <PositionsPanel mode={mode} onRefresh={refreshAccount} refreshTrigger={refreshTrigger} />
        </div>
      </div>
    </div>
  );
}
