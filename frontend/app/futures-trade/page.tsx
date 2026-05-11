'use client';
import { useEffect, useState, useCallback } from 'react';
import { api } from '@/lib/api';
import TradingViewWidget from '@/components/charts/TradingViewWidget';
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
  const [marginMode, setMarginMode] = useState('cross');
  const [account, setAccount] = useState<any>({ balance: 1000, available_balance: 1000 });
  const [lastPrice, setLastPrice] = useState(0);

  const tvSymbol = `KUCOIN:${pair.replace('/', '')}`;
  const futSymbol = pair.replace('/', '').replace('USDT', 'USDTM');

  const refreshAccount = useCallback(async () => {
    try {
      const data = await api.futures.account();
      setAccount(data);
    } catch { /* silent */ }
  }, []);

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
    try { await api.futures.setLeverage({ symbol: futSymbol, leverage: lev }); } catch { /* */ }
  }

  async function handleMarginModeChange(m: string) {
    setMarginMode(m);
    try { await api.futures.setMarginMode({ symbol: futSymbol, mode: m }); } catch { /* */ }
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

      {/* Main layout */}
      <div className="flex-1 flex overflow-hidden">
        {/* Left: Chart + Positions */}
        <div className="flex-1 flex flex-col min-w-0">
          {/* Chart area */}
          <div className="flex-1 flex min-h-0">
            {/* TradingView chart */}
            <div className="flex-1 min-w-0">
              <TradingViewWidget symbol={tvSymbol} interval="15" showToolbar={true} />
            </div>
          </div>

          {/* Bottom: Positions Panel */}
          <div className="h-[240px] border-t border-white/[0.06] bg-[#0d1117] overflow-hidden flex">
            <div className="flex-1 overflow-hidden">
              <PositionsPanel mode={mode} onRefresh={refreshAccount} />
            </div>
            {/* Asset Overview embedded in positions area on large screens */}
            <div className="hidden xl:block w-[280px] border-l border-white/[0.06] overflow-y-auto">
              <AssetOverview mode={mode} pair={pair} />
            </div>
          </div>
        </div>

        {/* Middle: Order Book / Recent Trades column */}
        <div className="w-[220px] xl:w-[250px] border-l border-white/[0.06] bg-[#0d1117] flex-col hidden lg:flex">
          {/* Toggle */}
          <div className="flex border-b border-white/[0.06]">
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

          {/* Content */}
          <div className="flex-1 overflow-hidden">
            {middlePanel === 'orderbook' ? (
              <OrderBook symbol={pair} onPriceClick={handlePriceClick} />
            ) : (
              <RecentTrades symbol={pair} />
            )}
          </div>
        </div>

        {/* Right: Manual / Bot panel */}
        <div className="w-[300px] xl:w-[340px] border-l border-white/[0.06] bg-[#0d1117] flex-col hidden lg:flex">
          {/* Manual | Bot tabs */}
          <div className="flex border-b border-white/[0.06]">
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

          {/* Panel content */}
          <div className="flex-1 overflow-hidden">
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
                onOrderPlaced={refreshAccount}
              />
            ) : (
              <BotPanel pair={pair} mode={mode} onBotCreated={refreshAccount} />
            )}
          </div>

          {/* Asset Overview - visible below xl */}
          <div className="xl:hidden">
            <AssetOverview mode={mode} pair={pair} />
          </div>
        </div>
      </div>
    </div>
  );
}
