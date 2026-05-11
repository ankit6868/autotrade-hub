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

  async function handleLeverageChange(lev: number) {
    setLeverage(lev);
    try { await api.futures.setLeverage({ symbol: futSymbol, leverage: lev }); } catch { /* */ }
  }

  async function handleMarginModeChange(m: string) {
    setMarginMode(m);
    try { await api.futures.setMarginMode({ symbol: futSymbol, mode: m }); } catch { /* */ }
  }

  function handlePriceClick(price: number) {
    // This could be used to fill order price from order book
  }

  return (
    <div className="fixed inset-0 md:left-64 flex flex-col overflow-hidden bg-[#0d1117] z-20 pt-14 md:pt-0">
      {/* Top bar: pair tabs + mode toggle */}
      <div className="flex items-center justify-between bg-[#0d1117] border-b border-white/[0.06]">
        <PairTabs activePair={pair} onPairChange={setPair} />
        <div className="flex items-center gap-2 px-3">
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

      {/* Main grid */}
      <div className="flex-1 flex overflow-hidden">
        {/* Left: Chart */}
        <div className="flex-1 flex flex-col min-w-0">
          <div className="flex-1 min-h-0">
            <TradingViewWidget symbol={tvSymbol} interval="15" showToolbar={true} />
          </div>

          {/* Bottom: Positions Panel */}
          <div className="h-[280px] border-t border-white/[0.06] bg-[#0d1117] overflow-hidden">
            <PositionsPanel mode={mode} onRefresh={refreshAccount} />
          </div>
        </div>

        {/* Right sidebar: Order Book + Order Panel */}
        <div className="w-[340px] xl:w-[380px] border-l border-white/[0.06] bg-[#0d1117] flex flex-col hidden lg:flex">
          {/* Order Book / Recent Trades toggle */}
          <div className="flex border-b border-white/[0.06]">
            <button
              onClick={() => setMiddlePanel('orderbook')}
              className={`flex-1 py-2 text-xs font-medium ${
                middlePanel === 'orderbook' ? 'text-white border-b-2 border-emerald-500' : 'text-slate-400'
              }`}
            >
              Order Book
            </button>
            <button
              onClick={() => setMiddlePanel('recent_trades')}
              className={`flex-1 py-2 text-xs font-medium ${
                middlePanel === 'recent_trades' ? 'text-white border-b-2 border-emerald-500' : 'text-slate-400'
              }`}
            >
              Recent Trades
            </button>
          </div>

          {/* Order Book or Recent Trades */}
          <div className="h-[300px] overflow-hidden">
            {middlePanel === 'orderbook' ? (
              <OrderBook symbol={pair} onPriceClick={handlePriceClick} />
            ) : (
              <RecentTrades symbol={pair} />
            )}
          </div>

          {/* Manual | Bot tabs */}
          <div className="flex border-y border-white/[0.06]">
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

          {/* Manual Order Panel or Bot Panel */}
          <div className="flex-1 overflow-hidden">
            {rightPanel === 'manual' ? (
              <ManualOrderPanel
                symbol={futSymbol}
                pair={pair}
                mode={mode}
                leverage={leverage}
                marginMode={marginMode}
                availableBalance={account?.available_balance ?? account?.balance ?? 1000}
                onLeverageChange={handleLeverageChange}
                onMarginModeChange={handleMarginModeChange}
                onOrderPlaced={refreshAccount}
              />
            ) : (
              <BotPanel pair={pair} mode={mode} onBotCreated={refreshAccount} />
            )}
          </div>

          {/* Asset Overview */}
          <AssetOverview mode={mode} />
        </div>
      </div>
    </div>
  );
}
