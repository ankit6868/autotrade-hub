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
// On screens <lg, we can only fit one of these panels at a time alongside
// the chart, so users pick via a small tab bar above the chart.
type MobileView = 'chart' | 'orderbook' | 'trade' | 'assets';

export default function FuturesTerminal() {
  const [pair, setPair] = useState('BTC/USDT');
  const [mode, setMode] = useState<'paper' | 'live'>('paper');
  const [rightPanel, setRightPanel] = useState<RightPanel>('manual');
  const [middlePanel, setMiddlePanel] = useState<MiddlePanel>('orderbook');
  const [mobileView, setMobileView] = useState<MobileView>('chart');
  const [leverage, setLeverage] = useState(3);
  const [marginMode, setMarginMode] = useState('isolated');
  const [account, setAccount] = useState<any>({ balance: 1000, available_balance: 1000 });
  const [lastPrice, setLastPrice] = useState(0);
  const [refreshTrigger, setRefreshTrigger] = useState(0);
  // Asset Overview can be collapsed to reclaim vertical space in the bottom
  // row — useful when the user wants more room for Positions / Open Orders.
  const [assetCollapsed, setAssetCollapsed] = useState(false);

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
    // app-fixed-page: matching CSS in globals.css updates `left` to track
    // the sidebar's open/closed state (16rem when open, 0 when closed) on
    // md+ viewports so the page properly reclaims the freed space.
    // Mobile uses left:0 with extra pt-14 to clear the fixed mobile header.
    <div className="app-fixed-page fixed inset-x-0 top-0 bottom-0 flex flex-col overflow-hidden bg-[#0d1117] z-20 pt-14 md:pt-0 transition-[left] duration-300 ease-out">
      {/* Top bar: pair tabs + mode toggle. Add left padding on md+ when the
          sidebar is closed so the floating hamburger doesn't overlap pair
          tabs. (When open, the sidebar takes the corner and the hamburger
          sits over the sidebar.) */}
      <div className="flex items-center justify-between bg-[#0d1117] border-b border-white/[0.06] pl-14 md:pl-0">
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

      {/* Mobile / tablet pane switcher — only visible <lg. Lets the user
          pick which panel to view alongside Positions below the chart
          since there's no room to show all three side-by-side. */}
      <div className="lg:hidden flex border-b border-white/[0.06] text-[11px] bg-[#0d1117]">
        {([
          { key: 'chart',     label: 'Chart' },
          { key: 'orderbook', label: 'Order Book' },
          { key: 'trade',     label: 'Trade' },
          { key: 'assets',    label: 'Assets' },
        ] as const).map(t => (
          <button
            key={t.key}
            onClick={() => setMobileView(t.key)}
            className={`flex-1 py-2 font-medium transition-colors ${
              mobileView === t.key
                ? 'text-white border-b-2 border-emerald-500'
                : 'text-slate-400 hover:text-white'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* Main layout. Different shape per breakpoint:
          - lg+ : 2-row grid (chart row | bottom row), each row has its
                  own columns. Same as KuCoin's desktop layout.
          - <lg : single column with the mobile tab switcher above
                  picking which panel to show alongside the always-visible
                  Positions strip at the bottom. */}
      <div className="flex-1 flex flex-col overflow-hidden">

        {/* ──────────────── lg+ TOP ROW (3 columns) ──────────────── */}
        <div className="hidden lg:flex flex-1 min-h-0 overflow-hidden">
          {/* Chart — grows */}
          <div className="flex-1 min-w-0 flex flex-col overflow-hidden">
            <KuCoinFuturesChart pair={pair} defaultInterval="15m" />
          </div>

          {/* Order Book / Recent Trades */}
          <div className="w-[220px] xl:w-[250px] border-l border-white/[0.06] bg-[#0d1117] flex-col flex overflow-hidden">
            <div className="flex border-b border-white/[0.06] shrink-0">
              <button
                onClick={() => setMiddlePanel('orderbook')}
                className={`flex-1 py-2 text-[11px] font-medium ${
                  middlePanel === 'orderbook' ? 'text-white border-b-2 border-emerald-500' : 'text-slate-400'
                }`}
              >Order Book</button>
              <button
                onClick={() => setMiddlePanel('recent_trades')}
                className={`flex-1 py-2 text-[11px] font-medium ${
                  middlePanel === 'recent_trades' ? 'text-white border-b-2 border-emerald-500' : 'text-slate-400'
                }`}
              >Recent Trades</button>
            </div>
            <div className="flex-1 overflow-hidden min-h-0">
              {middlePanel === 'orderbook'
                ? <OrderBook symbol={pair} onPriceClick={handlePriceClick} />
                : <RecentTrades symbol={pair} />}
            </div>
          </div>

          {/* Manual / Bot trading */}
          <div className="w-[300px] xl:w-[340px] border-l border-white/[0.06] bg-[#0d1117] flex-col flex overflow-hidden">
            <div className="flex border-b border-white/[0.06] shrink-0">
              <button
                onClick={() => setRightPanel('manual')}
                className={`flex-1 py-2 text-xs font-bold ${
                  rightPanel === 'manual' ? 'text-white border-b-2 border-emerald-500' : 'text-slate-400'
                }`}
              >Manual</button>
              <button
                onClick={() => setRightPanel('bot')}
                className={`flex-1 py-2 text-xs font-bold ${
                  rightPanel === 'bot' ? 'text-white border-b-2 border-emerald-500' : 'text-slate-400'
                }`}
              >Bot</button>
            </div>
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
          </div>
        </div>

        {/* ───────────── <lg MOBILE/TABLET single-pane ───────────── */}
        <div className="lg:hidden flex-1 min-h-0 overflow-hidden flex flex-col">
          {mobileView === 'chart' && (
            <div className="flex-1 min-h-0 overflow-hidden">
              <KuCoinFuturesChart pair={pair} defaultInterval="15m" />
            </div>
          )}
          {mobileView === 'orderbook' && (
            <>
              <div className="flex border-b border-white/[0.06] shrink-0 text-[11px] bg-[#0d1117]">
                <button
                  onClick={() => setMiddlePanel('orderbook')}
                  className={`flex-1 py-2 font-medium ${middlePanel === 'orderbook' ? 'text-white border-b-2 border-emerald-500' : 'text-slate-400'}`}
                >Order Book</button>
                <button
                  onClick={() => setMiddlePanel('recent_trades')}
                  className={`flex-1 py-2 font-medium ${middlePanel === 'recent_trades' ? 'text-white border-b-2 border-emerald-500' : 'text-slate-400'}`}
                >Recent Trades</button>
              </div>
              <div className="flex-1 overflow-hidden min-h-0">
                {middlePanel === 'orderbook'
                  ? <OrderBook symbol={pair} onPriceClick={handlePriceClick} />
                  : <RecentTrades symbol={pair} />}
              </div>
            </>
          )}
          {mobileView === 'trade' && (
            <>
              <div className="flex border-b border-white/[0.06] shrink-0 bg-[#0d1117]">
                <button
                  onClick={() => setRightPanel('manual')}
                  className={`flex-1 py-2 text-xs font-bold ${rightPanel === 'manual' ? 'text-white border-b-2 border-emerald-500' : 'text-slate-400'}`}
                >Manual</button>
                <button
                  onClick={() => setRightPanel('bot')}
                  className={`flex-1 py-2 text-xs font-bold ${rightPanel === 'bot' ? 'text-white border-b-2 border-emerald-500' : 'text-slate-400'}`}
                >Bot</button>
              </div>
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
            </>
          )}
          {mobileView === 'assets' && (
            <div className="flex-1 overflow-y-auto min-h-0">
              <AssetOverview mode={mode} pair={pair} />
            </div>
          )}
        </div>

        {/* ─── BOTTOM ROW — lg+ only. Two cells: Positions + Asset.
            Height collapses from 260px → 56px when Asset is collapsed,
            so the user can claim that vertical space back. The Asset
            Overview cell uses the controlled-collapse pattern so the
            chevron's state stays in sync with the row height. */}
        <div
          className={`hidden lg:flex border-t border-white/[0.06] bg-[#0d1117] overflow-hidden transition-[height] duration-300 ease-out ${
            assetCollapsed ? 'h-[56px]' : 'h-[260px]'
          }`}
        >
          <div className="flex-1 min-w-0 overflow-hidden border-r border-white/[0.06]">
            <PositionsPanel mode={mode} onRefresh={refreshAccount} refreshTrigger={refreshTrigger} />
          </div>
          <div className="w-[300px] xl:w-[340px] overflow-y-auto">
            <AssetOverview
              mode={mode}
              pair={pair}
              collapsed={assetCollapsed}
              onToggleCollapsed={() => setAssetCollapsed(v => !v)}
            />
          </div>
        </div>

        {/* Mobile bottom: just Positions, sized so user can scroll it.
            Asset Overview is reachable from the mobile tab switcher. */}
        <div className="lg:hidden h-[220px] border-t border-white/[0.06] bg-[#0d1117] overflow-hidden">
          <PositionsPanel mode={mode} onRefresh={refreshAccount} refreshTrigger={refreshTrigger} />
        </div>
      </div>
    </div>
  );
}
